"""Legal PDF retrieval, validation, renaming, and failure recording.

What this module will do: fetch a PDF from a legitimate open-access location, or
from a direct PDF URL that a publisher's own API has told us is authorised.

What it will never do: bypass a paywall, an institutional login, a CAPTCHA, an
anti-bot protection, or a publisher's download restriction; construct a
publisher PDF URL by guessing; or contact an unauthorised mirror. When a paper
cannot be obtained legally, it is recorded in the ``Unable to Download``
register with a recommended manual action — that is the correct outcome, not a
failure to work around.

The workflow per paper: gather candidate URLs, screen them for legality,
download to a temporary ``.part`` file, validate the bytes as a real PDF, move
into place under the paper's title, record the checksum and licence, and log
every attempt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .config import Settings
from .http_client import (
    AccessRestrictedError,
    BlockedHostError,
    HttpClient,
    TransientHTTPError,
    host_matches,
    host_of,
    looks_like_challenge,
)
from .logging_setup import get_logger
from .metadata import enrich_open_access
from .pdf_validator import (
    ValidationResult,
    content_type_acceptable,
    looks_like_html,
    looks_like_pdf,
    validate_pdf,
)
from .schemas import DownloadAttempt, DownloadStatus, JobConfig, PaperRecord
from .utils import (
    append_jsonl,
    disambiguated_stem,
    ensure_dir,
    first_author_surname,
    resolve_collision,
    safe_filename_stem,
    utc_now_iso,
)

LOG = get_logger("download")


@dataclass
class DownloadOutcome:
    """Result of attempting one paper's PDF."""

    record: PaperRecord
    success: bool
    path: Path | None = None
    validation: ValidationResult | None = None
    attempts: list[DownloadAttempt] = field(default_factory=list)
    failure_reason: str = ""
    recommended_action: str = ""


@dataclass
class DownloadReport:
    """Aggregate results of the download stage."""

    downloaded: list[DownloadOutcome] = field(default_factory=list)
    failed: list[DownloadOutcome] = field(default_factory=list)
    skipped: list[DownloadOutcome] = field(default_factory=list)
    counters: dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# URL screening
# ---------------------------------------------------------------------------

#: URL fragments that indicate an access wall rather than a document.
SUSPICIOUS_URL_MARKERS = (
    "/login",
    "/signin",
    "/sso",
    "shibboleth",
    "wayf",
    "openathens",
    "/checkout",
    "/purchase",
    "/paywall",
    "captcha",
    "cookieabsent",
    "/action/cookieabsent",
)


def screen_url(url: str, record: PaperRecord, settings: Settings) -> tuple[bool, str]:
    """Decide whether a candidate PDF URL may legally be fetched.

    Returns ``(allowed, reason)``. The reason is recorded on refusal so the
    manual-retrieval register can explain why the agent stopped.
    """
    if not url or not url.strip():
        return False, "empty URL"

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False, f"unsupported URL scheme '{parsed.scheme}'"

    if host_matches(url, settings.blocked_hosts):
        return False, (
            f"{host_of(url)} is on the blocked-host list (unauthorised source or a "
            "service that prohibits automated access)"
        )

    lowered = url.lower()
    for marker in SUSPICIOUS_URL_MARKERS:
        if marker in lowered:
            return False, (
                f"the URL points at an authentication or purchase flow ('{marker}'), "
                "which the agent does not attempt to pass"
            )

    if host_matches(url, settings.authorised_pdf_only_hosts):
        # Publisher host: allowed only when the record itself is open access and
        # the URL came from an API rather than being constructed here.
        oa_status = (record.open_access_status or "").lower()
        is_open = oa_status in {"gold", "green", "hybrid", "bronze", "diamond"}
        if not is_open:
            return False, (
                f"{host_of(url)} is a subscription publisher host and this record is "
                f"not marked open access (status: {record.open_access_status or 'unknown'}). "
                "Retrieve it through your own institutional access."
            )
        return True, (
            f"{host_of(url)} is a publisher host, but the record is open access "
            f"({record.open_access_status}) and the URL was supplied by the publisher API"
        )

    if host_matches(url, settings.trusted_oa_hosts):
        return True, f"{host_of(url)} is a recognised open-access host"

    # Unknown host: allowed, but the PDF validator remains the gatekeeper.
    return True, f"{host_of(url)} is not a known subscription host; validating the bytes"


def candidate_urls(record: PaperRecord, settings: Settings) -> list[tuple[str, str]]:
    """Return screened ``(url, reason)`` pairs to try, best first."""
    seen: set[str] = set()
    allowed: list[tuple[str, str]] = []
    for url in [record.pdf_url, *record.candidate_pdf_urls]:
        if not url or url in seen:
            continue
        seen.add(url)
        ok, reason = screen_url(url, record, settings)
        if ok:
            allowed.append((url, reason))
        else:
            LOG.debug(f"Skipping candidate URL for '{record.title[:60]}': {reason}")
            record.download_attempts.append(
                DownloadAttempt(
                    url=url,
                    outcome="not attempted",
                    error=f"URL not eligible: {reason}",
                )
            )
    return allowed


# ---------------------------------------------------------------------------
# Filenames
# ---------------------------------------------------------------------------


def target_filename(
    record: PaperRecord,
    directory: Path,
    *,
    reserved: set[str] | None = None,
) -> Path:
    """Choose the final on-disk path for a paper's PDF.

    Preference order:

    1. ``Full Paper Title.pdf``
    2. ``Full Paper Title - FirstAuthor - Year.pdf`` when the first is taken
    3. ``Full Paper Title (2).pdf`` as a deterministic last resort
    """
    reserved = reserved or set()
    directory = Path(directory)
    stem = safe_filename_stem(record.title)

    primary = directory / f"{stem}.pdf"
    if not primary.exists() and primary.name.lower() not in {r.lower() for r in reserved}:
        return primary

    disambiguated = directory / f"{disambiguated_stem(record.title, first_author_surname(record.authors), record.year)}.pdf"
    if not disambiguated.exists() and disambiguated.name.lower() not in {
        r.lower() for r in reserved
    }:
        return disambiguated

    return resolve_collision(directory, stem, ".pdf", taken=reserved)


# ---------------------------------------------------------------------------
# Single download
# ---------------------------------------------------------------------------


def download_one(
    record: PaperRecord,
    directory: Path,
    settings: Settings,
    *,
    client: HttpClient | None = None,
    partial_dir: Path | None = None,
    reserved: set[str] | None = None,
    log_path: Path | None = None,
) -> DownloadOutcome:
    """Attempt every eligible URL for one paper until a valid PDF is obtained."""
    directory = ensure_dir(Path(directory))
    partial_dir = ensure_dir(Path(partial_dir) if partial_dir else directory / ".partial")
    reserved = reserved if reserved is not None else set()

    # --- Requirement: prevent duplicate downloads ---
    if record.local_path and Path(record.local_path).exists():
        existing = Path(record.local_path)
        record.download_status = DownloadStatus.ALREADY_PRESENT
        LOG.info(f"Already present, skipping: {existing.name}")
        return DownloadOutcome(record=record, success=True, path=existing)

    existing_by_title = directory / f"{safe_filename_stem(record.title)}.pdf"
    if existing_by_title.exists():
        validation = validate_pdf(
            existing_by_title,
            min_bytes=int(settings.pdf.get("min_bytes", 1024)),
            expected_title=record.title,
            min_chars_per_page=int(settings.pdf.get("min_extractable_chars_per_page", 60)),
        )
        if validation.valid:
            _apply_success(record, existing_by_title, validation, source_url=record.pdf_url or "")
            record.download_status = DownloadStatus.ALREADY_PRESENT
            LOG.info(f"Already downloaded and valid: {existing_by_title.name}")
            return DownloadOutcome(
                record=record, success=True, path=existing_by_title, validation=validation
            )

    # --- Requirement: check legitimate open-access locations ---
    if not record.candidate_pdf_urls and record.doi:
        enrich_open_access(record, settings, client=client)

    urls = candidate_urls(record, settings)
    if not urls:
        record.download_status = DownloadStatus.SKIPPED_NO_LEGAL_URL
        record.failure_reason = (
            "No authorised open-access PDF URL is available for this paper."
        )
        outcome = DownloadOutcome(
            record=record,
            success=False,
            failure_reason=record.failure_reason,
            recommended_action=_recommended_action(record, "no-oa-url"),
        )
        _log_attempt(log_path, record, None, "skipped", record.failure_reason)
        return outcome

    owns_client = client is None
    http = client or HttpClient(settings, requests_per_second=2.0)
    max_bytes = int(settings.network.get("max_pdf_bytes", 100 * 1024 * 1024))
    last_error = ""

    try:
        for url, screen_reason in urls:
            attempt = DownloadAttempt(url=url, outcome="attempted")
            partial = partial_dir / f"{record.record_id or 'paper'}.part"
            try:
                status, content_type, bytes_written = _stream_to_file(
                    http, url, partial, max_bytes=max_bytes
                )
                attempt.http_status = status
                attempt.content_type = content_type
                attempt.bytes_received = bytes_written

                if not content_type_acceptable(content_type):
                    attempt.outcome = "rejected"
                    attempt.error = (
                        f"Server returned content type '{content_type}', which is not a PDF."
                    )
                    _cleanup(partial)
                    last_error = attempt.error
                    record.download_attempts.append(attempt)
                    _log_attempt(log_path, record, attempt, "rejected", attempt.error)
                    continue

                # --- Requirement: validate before accepting ---
                validation = validate_pdf(
                    partial,
                    min_bytes=int(settings.pdf.get("min_bytes", 1024)),
                    expected_title=record.title,
                    min_chars_per_page=int(
                        settings.pdf.get("min_extractable_chars_per_page", 60)
                    ),
                    content_type=content_type,
                )
                if not validation.valid:
                    attempt.outcome = "invalid"
                    attempt.error = validation.reason
                    _cleanup(partial)
                    last_error = validation.reason
                    record.download_attempts.append(attempt)
                    _log_attempt(log_path, record, attempt, "invalid", validation.reason)
                    continue

                # --- Requirement: move into place only after validation ---
                final_path = target_filename(record, directory, reserved=reserved)
                partial.replace(final_path)
                reserved.add(final_path.name)

                attempt.outcome = "downloaded"
                record.download_attempts.append(attempt)
                _apply_success(record, final_path, validation, source_url=url)
                record.notes = " ".join(
                    filter(None, [record.notes, f"PDF source: {screen_reason}."])
                )
                _log_attempt(log_path, record, attempt, "downloaded", "")
                LOG.info(
                    f"Downloaded '{final_path.name}' "
                    f"({validation.size_bytes:,} bytes, {validation.page_count} pages)."
                )
                return DownloadOutcome(
                    record=record, success=True, path=final_path, validation=validation,
                    attempts=record.download_attempts,
                )

            except AccessRestrictedError as exc:
                attempt.outcome = "access restricted"
                attempt.http_status = exc.status
                attempt.error = (
                    f"HTTP {exc.status}: the content is behind a paywall or login. "
                    "The agent does not bypass access controls."
                )
                last_error = attempt.error
                _cleanup(partial)
                record.download_attempts.append(attempt)
                _log_attempt(log_path, record, attempt, "access restricted", attempt.error)
            except BlockedHostError as exc:
                attempt.outcome = "blocked"
                attempt.error = str(exc)
                last_error = attempt.error
                _cleanup(partial)
                record.download_attempts.append(attempt)
                _log_attempt(log_path, record, attempt, "blocked", attempt.error)
            except TransientHTTPError as exc:
                attempt.outcome = "failed"
                attempt.http_status = exc.status
                attempt.error = str(exc)
                last_error = attempt.error
                _cleanup(partial)
                record.download_attempts.append(attempt)
                _log_attempt(log_path, record, attempt, "failed", attempt.error)
            except Exception as exc:  # noqa: BLE001 - one paper must not stop the run
                attempt.outcome = "error"
                attempt.error = f"{type(exc).__name__}: {exc}"
                last_error = attempt.error
                _cleanup(partial)
                record.download_attempts.append(attempt)
                _log_attempt(log_path, record, attempt, "error", attempt.error)
    finally:
        if owns_client:
            http.close()

    record.download_status = DownloadStatus.FAILED
    record.failure_reason = last_error or "All candidate PDF URLs failed."
    LOG.warning(f"Could not download '{record.title[:70]}': {record.failure_reason}")
    return DownloadOutcome(
        record=record,
        success=False,
        attempts=record.download_attempts,
        failure_reason=record.failure_reason,
        recommended_action=_recommended_action(record, "failed"),
    )


def _stream_to_file(
    http: HttpClient, url: str, destination: Path, *, max_bytes: int
) -> tuple[int | None, str | None, int]:
    """Stream a URL to a temporary file, aborting on non-PDF or oversized content.

    Returns ``(http_status, content_type, bytes_written)``. Raises
    :class:`AccessRestrictedError` when the response is a login or challenge page.
    """
    ensure_dir(destination.parent)
    bytes_written = 0
    first_chunk = b""

    with http.stream(url) as response:
        status = response.status_code
        content_type = response.headers.get("content-type")

        if status in {401, 402, 403, 407, 451}:
            raise AccessRestrictedError(
                f"HTTP {status}: access to this PDF is restricted.", status=status, url=url
            )
        if status in {408, 425, 429, 500, 502, 503, 504}:
            raise TransientHTTPError(f"HTTP {status} while fetching the PDF.", status=status)
        if not (200 <= status < 300):
            raise TransientHTTPError(f"Unexpected HTTP {status} while fetching the PDF.", status=status)

        content_length = response.headers.get("content-length")
        if content_length and content_length.isdigit() and int(content_length) > max_bytes:
            raise TransientHTTPError(
                f"The file is {int(content_length):,} bytes, above the "
                f"{max_bytes:,}-byte safety limit."
            )

        with destination.open("wb") as handle:
            for chunk in response.iter_bytes(chunk_size=65536):
                if not chunk:
                    continue
                if not first_chunk:
                    first_chunk = chunk[:8192]
                    # Abort early on a login wall or bot challenge.
                    if looks_like_challenge(first_chunk):
                        raise AccessRestrictedError(
                            "The server returned an access wall or bot challenge page "
                            "instead of the PDF. The agent does not attempt to pass it.",
                            status=status,
                            url=url,
                        )
                    if looks_like_html(first_chunk) and not looks_like_pdf(first_chunk):
                        # Keep writing nothing further; the validator will reject it,
                        # but stopping now avoids downloading a whole HTML page.
                        handle.write(chunk)
                        bytes_written += len(chunk)
                        break
                handle.write(chunk)
                bytes_written += len(chunk)
                if bytes_written > max_bytes:
                    raise TransientHTTPError(
                        f"Download exceeded the {max_bytes:,}-byte safety limit."
                    )

    return status, content_type, bytes_written


def _apply_success(
    record: PaperRecord,
    path: Path,
    validation: ValidationResult,
    *,
    source_url: str,
) -> None:
    """Record a successful, validated download on the paper record."""
    record.download_status = DownloadStatus.DOWNLOADED
    record.local_filename = path.name
    record.local_path = str(path)
    record.file_sha256 = validation.sha256
    record.file_bytes = validation.size_bytes
    record.download_source_url = source_url or record.pdf_url
    record.requires_ocr = validation.requires_ocr
    record.failure_reason = None
    if validation.requires_ocr:
        record.notes = " ".join(
            filter(
                None,
                [
                    record.notes,
                    "The PDF has no extractable text layer and requires OCR before analysis.",
                ],
            )
        )


def _cleanup(path: Path) -> None:
    """Remove a partial download, ignoring absence."""
    try:
        Path(path).unlink(missing_ok=True)
    except OSError as exc:  # pragma: no cover
        LOG.debug(f"Could not remove partial file {path}: {exc}")


def _log_attempt(
    log_path: Path | None,
    record: PaperRecord,
    attempt: DownloadAttempt | None,
    outcome: str,
    error: str,
) -> None:
    """Append one download attempt to ``download_log.jsonl``."""
    if log_path is None:
        return
    append_jsonl(
        log_path,
        {
            "timestamp": utc_now_iso(),
            "record_id": record.record_id,
            "title": record.title,
            "doi": record.doi,
            "url": attempt.url if attempt else None,
            "http_status": attempt.http_status if attempt else None,
            "content_type": attempt.content_type if attempt else None,
            "bytes": attempt.bytes_received if attempt else None,
            "outcome": outcome,
            "error": error,
        },
    )


def _recommended_action(record: PaperRecord, kind: str) -> str:
    """Suggest how a human can obtain a paper the agent could not."""
    parts: list[str] = []
    if record.doi:
        parts.append(f"Open https://doi.org/{record.doi} through your institutional access")
    elif record.landing_page_url:
        parts.append(f"Open the publisher page: {record.landing_page_url}")
    else:
        parts.append("Search your library discovery service for the title")

    if kind == "no-oa-url":
        parts.append("no legal open-access copy was found, so a subscription or an "
                     "interlibrary-loan request is likely needed")
    else:
        parts.append("the automated attempt failed, so download the PDF manually and "
                     "place it in the 'Downloaded Papers' folder using the exact paper title "
                     "as the filename")
    parts.append("or email the corresponding author for a copy")
    return "; ".join(parts) + "."


# ---------------------------------------------------------------------------
# Batch download
# ---------------------------------------------------------------------------


def download_records(
    records: list[PaperRecord],
    directory: Path,
    settings: Settings,
    config: JobConfig,
    *,
    client: HttpClient | None = None,
    partial_dir: Path | None = None,
    log_path: Path | None = None,
    already_done: set[str] | None = None,
    on_complete: Any = None,
) -> DownloadReport:
    """Download every selected paper, continuing past individual failures.

    ``already_done`` holds record ids completed in an earlier run, which is how
    the stage resumes mid-list. ``on_complete`` is called with each record id as
    it finishes, so the caller can checkpoint after every paper.
    """
    if not config.download_only_legal_and_authorized_content:
        # The switch exists in configuration for transparency, but the agent has
        # no other mode: unauthorised retrieval is never implemented.
        LOG.warning(
            "download_only_legal_and_authorized_content was set to false. The agent "
            "has no unauthorised retrieval path, so legal-only behaviour still applies."
        )

    directory = ensure_dir(Path(directory))
    already_done = already_done or set()
    reserved = {p.name for p in directory.glob("*.pdf")}
    report = DownloadReport()

    selected = [r for r in records if r.selected]
    for index, record in enumerate(selected, 1):
        if record.record_id in already_done:
            LOG.debug(f"Skipping already-processed record {record.record_id}.")
            continue
        LOG.info(f"[{index}/{len(selected)}] {record.title[:80]}")
        outcome = download_one(
            record,
            directory,
            settings,
            client=client,
            partial_dir=partial_dir,
            reserved=reserved,
            log_path=log_path,
        )
        if outcome.success:
            report.downloaded.append(outcome)
        elif record.download_status == DownloadStatus.SKIPPED_NO_LEGAL_URL:
            report.skipped.append(outcome)
        else:
            report.failed.append(outcome)
        if on_complete is not None:
            on_complete(record.record_id)

    for record in records:
        if not record.selected and record.download_status == DownloadStatus.NOT_ATTEMPTED:
            record.download_status = DownloadStatus.SKIPPED_NOT_SELECTED

    report.counters = {
        "attempted": len(selected),
        "downloaded": len(report.downloaded),
        "failed": len(report.failed),
        "skipped_no_legal_url": len(report.skipped),
        "requires_ocr": sum(1 for r in records if r.requires_ocr),
    }
    LOG.info(
        f"Download stage: {report.counters['downloaded']} downloaded, "
        f"{report.counters['failed']} failed, "
        f"{report.counters['skipped_no_legal_url']} had no legal open-access URL."
    )
    return report


def failure_rows(records: list[PaperRecord]) -> list[dict[str, Any]]:
    """Build the rows for ``Unable_to_Download.docx`` and the Excel sheet."""
    rows: list[dict[str, Any]] = []
    failed = [
        r
        for r in records
        if r.selected
        and r.download_status in (DownloadStatus.FAILED, DownloadStatus.SKIPPED_NO_LEGAL_URL)
    ]
    for index, record in enumerate(failed, 1):
        attempted = [a for a in record.download_attempts if a.outcome != "not attempted"]
        statuses = [a.http_status for a in record.download_attempts if a.http_status]
        rows.append(
            {
                "serial": index,
                "title": record.title,
                "authors": "; ".join(record.authors) or "Not recorded",
                "year": record.year or "Not recorded",
                "journal": record.journal or "Not recorded",
                "doi": record.doi or "Not recorded",
                "landing_page_url": record.landing_page_url or "Not recorded",
                "attempted_urls": (
                    "\n".join(a.url for a in record.download_attempts) or "None eligible"
                ),
                "open_access_status": record.open_access_status or "Unknown",
                "failure_reason": record.failure_reason or "Not recorded",
                "http_status": ", ".join(str(s) for s in dict.fromkeys(statuses)) or "Not returned",
                "attempted_at": (
                    record.download_attempts[-1].attempted_at
                    if record.download_attempts
                    else utc_now_iso()
                ),
                "recommended_action": _recommended_action(
                    record,
                    "no-oa-url"
                    if record.download_status == DownloadStatus.SKIPPED_NO_LEGAL_URL
                    else "failed",
                ),
                "q1_status": record.q1.verification_status.value,
                "attempt_count": len(attempted),
            }
        )
    return rows
