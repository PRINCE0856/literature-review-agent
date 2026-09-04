"""Independent verification: metadata and Q1, evidence and citations, files.

The analysis agent never verifies its own work. Three separate verifiers run over
the saved artefacts:

* :class:`MetadataQ1Verifier` — does the DOI resolve, and does its registered
  metadata match what we recorded? Is the quartile actually sourced?
* :class:`EvidenceCitationVerifier` — is every claim supported, are the page
  numbers real, do citations and references correspond, and are author statements
  kept separate from agent inferences?
* :class:`FileVerifier` — is each PDF a valid, non-HTML file that belongs to its
  intended paper, and does its extracted text match?

Questionable data is never silently rewritten: a finding preserves the original
value beside any corrected value, with the source and the reason.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Settings
from .evidence_ledger import EvidenceLedger
from .http_client import AccessRestrictedError, HttpClient
from .logging_setup import get_logger
from .pdf_extractor import load_pages
from .pdf_validator import validate_pdf, verify_extracted_text_matches
from .schemas import (
    CheckOutcome,
    CitationAuditRow,
    DownloadStatus,
    EvidenceStance,
    JobConfig,
    PaperAnalysis,
    PaperRecord,
    Q1Status,
    VerificationFinding,
    VerificationSummary,
)
from .utils import normalize_title, safe_filename_stem, sha256_file, stable_id, title_tokens

LOG = get_logger("verify")


@dataclass
class VerificationResult:
    """All findings plus the summary counts for the report."""

    findings: list[VerificationFinding] = field(default_factory=list)
    summary: VerificationSummary = field(default_factory=VerificationSummary)

    def add(
        self,
        verifier: str,
        check: str,
        target: str,
        outcome: CheckOutcome,
        *,
        detail: str = "",
        record_id: str | None = None,
        original_value: str = "",
        corrected_value: str = "",
        correction_source: str = "",
        correction_reason: str = "",
        recommended_action: str = "",
    ) -> VerificationFinding:
        """Record one check and return the finding."""
        finding = VerificationFinding(
            finding_id="VF-" + stable_id(verifier, check, target, detail),
            verifier=verifier,
            check=check,
            target=target,
            record_id=record_id,
            outcome=outcome,
            detail=detail,
            original_value=original_value,
            corrected_value=corrected_value,
            correction_source=correction_source,
            correction_reason=correction_reason,
            recommended_action=recommended_action,
        )
        self.findings.append(finding)
        return finding

    def counts(self) -> dict[str, int]:
        """Outcome tallies across every finding."""
        return {
            outcome.value: sum(1 for f in self.findings if f.outcome == outcome)
            for outcome in CheckOutcome
        }

    def failures(self) -> list[VerificationFinding]:
        """Findings that failed."""
        return [f for f in self.findings if f.outcome == CheckOutcome.FAIL]

    def warnings(self) -> list[VerificationFinding]:
        """Findings that raised a warning."""
        return [f for f in self.findings if f.outcome == CheckOutcome.WARNING]


# ---------------------------------------------------------------------------
# 1. Metadata and Q1 verifier
# ---------------------------------------------------------------------------


class MetadataQ1Verifier:
    """Independently checks bibliographic metadata and quartile evidence."""

    name = "metadata-q1-verifier"

    def __init__(
        self,
        settings: Settings,
        *,
        client: HttpClient | None = None,
        resolve_dois: bool | None = None,
    ) -> None:
        self.settings = settings
        self._client = client
        self._owns_client = client is None
        self.resolve_dois = (
            bool(settings.verification.get("resolve_dois", True))
            if resolve_dois is None
            else resolve_dois
        )

    @property
    def client(self) -> HttpClient:
        """HTTP client used for DOI resolution and Crossref comparison."""
        if self._client is None:
            self._client = HttpClient(self.settings, requests_per_second=3.0)
        return self._client

    def close(self) -> None:
        """Release the HTTP client if this verifier created it."""
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    def verify(
        self,
        records: list[PaperRecord],
        result: VerificationResult,
        *,
        merge_events: list[dict[str, Any]] | None = None,
    ) -> VerificationResult:
        """Run every metadata and quartile check over *records*."""
        limit = int(self.settings.verification.get("max_dois_to_resolve", 200))
        resolved = 0

        for record in records:
            target = f"{record.record_id}: {record.title[:60]}"

            # --- Required fields present ---
            for field_name, label in (
                ("title", "title"),
                ("authors", "authors"),
                ("year", "publication year"),
                ("journal", "journal or source"),
            ):
                value = getattr(record, field_name)
                if value in (None, "", []):
                    result.add(
                        self.name,
                        f"{label} recorded",
                        target,
                        CheckOutcome.WARNING,
                        detail=f"No {label} was retrieved for this record.",
                        record_id=record.record_id,
                        recommended_action=(
                            f"Check the {label} on the publisher page and correct it "
                            "before citing this paper."
                        ),
                    )
                else:
                    result.add(
                        self.name,
                        f"{label} recorded",
                        target,
                        CheckOutcome.PASS,
                        record_id=record.record_id,
                    )

            # --- DOI resolution and Crossref comparison ---
            if not record.doi:
                result.add(
                    self.name,
                    "DOI resolves",
                    target,
                    CheckOutcome.NOT_APPLICABLE,
                    detail="No DOI is recorded for this paper.",
                    record_id=record.record_id,
                    recommended_action=(
                        "Search Crossref by title to find the DOI, so the citation can "
                        "be verified."
                    ),
                )
            elif self.resolve_dois and resolved < limit:
                resolved += 1
                self._check_doi(record, result, target)
            else:
                result.add(
                    self.name,
                    "DOI resolves",
                    target,
                    CheckOutcome.NOT_APPLICABLE,
                    detail=(
                        "DOI resolution was not attempted (disabled in configuration or "
                        "the per-run limit was reached)."
                    ),
                    record_id=record.record_id,
                )

            # --- Quartile evidence ---
            self._check_quartile(record, result, target)

        # --- Duplicate merges ---
        self._check_merges(records, result, merge_events or [])

        return result

    def _check_doi(self, record: PaperRecord, result: VerificationResult, target: str) -> None:
        """Resolve a DOI and compare its registered metadata with our record."""
        doi = record.doi
        try:
            payload = self.client.get_json(
                f"https://api.crossref.org/works/{doi}",
                params={"mailto": self.settings.contact_email},
            )
        except AccessRestrictedError as exc:
            result.add(
                self.name,
                "DOI resolves",
                target,
                CheckOutcome.WARNING,
                detail=f"Crossref refused the lookup for {doi} ({exc}).",
                record_id=record.record_id,
                recommended_action=f"Open https://doi.org/{doi} manually to confirm it resolves.",
            )
            return
        except Exception as exc:  # noqa: BLE001 - network problems are warnings, not failures
            result.add(
                self.name,
                "DOI resolves",
                target,
                CheckOutcome.WARNING,
                detail=(
                    f"Could not check {doi} against Crossref ({type(exc).__name__}: {exc}). "
                    "This is a lookup problem, not proof the DOI is wrong."
                ),
                record_id=record.record_id,
                recommended_action=f"Open https://doi.org/{doi} manually to confirm it resolves.",
            )
            return

        message = payload.get("message") or {}
        if not message:
            result.add(
                self.name,
                "DOI resolves",
                target,
                CheckOutcome.FAIL,
                detail=f"{doi} returned no registered metadata, so the DOI may be wrong.",
                record_id=record.record_id,
                recommended_action="Verify the DOI on the publisher page before citing this paper.",
            )
            return

        result.add(
            self.name, "DOI resolves", target, CheckOutcome.PASS,
            detail=f"{doi} resolves in Crossref.", record_id=record.record_id,
        )

        # --- Title comparison ---
        titles = message.get("title") or []
        registered_title = titles[0] if titles else ""
        if registered_title:
            ours = title_tokens(record.title)
            theirs = title_tokens(registered_title)
            overlap = len(ours & theirs) / max(len(ours or theirs) or 1, 1)
            if overlap >= 0.7:
                result.add(
                    self.name, "title matches DOI metadata", target, CheckOutcome.PASS,
                    record_id=record.record_id,
                )
            else:
                result.add(
                    self.name,
                    "title matches DOI metadata",
                    target,
                    CheckOutcome.FAIL,
                    detail=(
                        f"The recorded title does not match the title registered for {doi} "
                        f"({overlap:.0%} token overlap). The DOI may belong to a different paper."
                    ),
                    record_id=record.record_id,
                    original_value=record.title,
                    corrected_value=registered_title,
                    correction_source=f"Crossref registered metadata for {doi}",
                    correction_reason=(
                        "The original value is preserved; no field was overwritten "
                        "automatically."
                    ),
                    recommended_action=(
                        "Decide which title is correct, then fix either the DOI or the "
                        "title before citing."
                    ),
                )

        # --- Author comparison ---
        registered_authors = [
            f"{a.get('family', '')}".strip().lower()
            for a in (message.get("author") or [])
            if a.get("family")
        ]
        ours_authors = {
            (a.split(",")[0].strip().lower()) for a in record.authors if a
        }
        if registered_authors and ours_authors:
            if ours_authors & set(registered_authors):
                result.add(
                    self.name, "authors match DOI metadata", target, CheckOutcome.PASS,
                    record_id=record.record_id,
                )
            else:
                result.add(
                    self.name,
                    "authors match DOI metadata",
                    target,
                    CheckOutcome.FAIL,
                    detail=(
                        "No recorded author surname appears in the metadata registered "
                        f"for {doi}."
                    ),
                    record_id=record.record_id,
                    original_value="; ".join(record.authors),
                    corrected_value="; ".join(registered_authors),
                    correction_source=f"Crossref registered metadata for {doi}",
                    correction_reason="Original preserved; nothing was overwritten.",
                    recommended_action="Confirm the author list on the publisher page.",
                )
        elif registered_authors and not ours_authors:
            result.add(
                self.name,
                "authors match DOI metadata",
                target,
                CheckOutcome.WARNING,
                detail="No authors were recorded locally, so no comparison was possible.",
                record_id=record.record_id,
                corrected_value="; ".join(registered_authors),
                correction_source=f"Crossref registered metadata for {doi}",
                correction_reason="Suggested value only; the record was not modified.",
                recommended_action="Add the author list from Crossref.",
            )

        # --- Year comparison ---
        issued = ((message.get("issued") or {}).get("date-parts") or [[None]])[0]
        registered_year = issued[0] if issued else None
        if registered_year and record.year:
            if abs(int(registered_year) - record.year) <= 1:
                result.add(
                    self.name, "year matches DOI metadata", target, CheckOutcome.PASS,
                    record_id=record.record_id,
                )
            else:
                result.add(
                    self.name,
                    "year matches DOI metadata",
                    target,
                    CheckOutcome.FAIL,
                    detail=(
                        f"Recorded year {record.year} differs from the registered year "
                        f"{registered_year} by more than one year."
                    ),
                    record_id=record.record_id,
                    original_value=str(record.year),
                    corrected_value=str(registered_year),
                    correction_source=f"Crossref registered metadata for {doi}",
                    correction_reason="Original preserved; nothing was overwritten.",
                    recommended_action="Use the year printed on the article itself.",
                )

        # --- Journal comparison ---
        containers = message.get("container-title") or []
        registered_journal = containers[0] if containers else ""
        if registered_journal and record.journal:
            ours = normalize_title(record.journal)
            theirs = normalize_title(registered_journal)
            if ours == theirs or ours in theirs or theirs in ours:
                result.add(
                    self.name, "journal matches DOI metadata", target, CheckOutcome.PASS,
                    record_id=record.record_id,
                )
            else:
                result.add(
                    self.name,
                    "journal matches DOI metadata",
                    target,
                    CheckOutcome.WARNING,
                    detail=(
                        f"Recorded journal '{record.journal}' differs from the registered "
                        f"'{registered_journal}'. Abbreviated titles often cause this."
                    ),
                    record_id=record.record_id,
                    original_value=record.journal,
                    corrected_value=registered_journal,
                    correction_source=f"Crossref registered metadata for {doi}",
                    correction_reason="Original preserved; nothing was overwritten.",
                    recommended_action="Use the full journal title in the reference list.",
                )

        # --- ISSN comparison ---
        registered_issns = {
            i.replace("-", "").upper() for i in (message.get("ISSN") or []) if i
        }
        ours_issns = {
            i.replace("-", "").upper() for i in (record.issn, record.eissn) if i
        }
        if registered_issns and ours_issns:
            outcome = (
                CheckOutcome.PASS if ours_issns & registered_issns else CheckOutcome.WARNING
            )
            result.add(
                self.name,
                "ISSN matches DOI metadata",
                target,
                outcome,
                detail=(
                    ""
                    if outcome == CheckOutcome.PASS
                    else "The recorded ISSN is not among those registered for this DOI, "
                         "which can affect the journal-ranking match."
                ),
                record_id=record.record_id,
                original_value="; ".join(sorted(ours_issns)),
                corrected_value="; ".join(sorted(registered_issns)),
                correction_source=f"Crossref registered metadata for {doi}",
                correction_reason="Original preserved; nothing was overwritten.",
                recommended_action=(
                    ""
                    if outcome == CheckOutcome.PASS
                    else "Re-check the quartile using the registered ISSN."
                ),
            )

    def _check_quartile(
        self, record: PaperRecord, result: VerificationResult, target: str
    ) -> None:
        """Confirm any claimed quartile has a real, dated source behind it."""
        q1 = record.q1
        status = q1.verification_status

        if status in (Q1Status.VERIFIED_Q1, Q1Status.VERIFIED_NON_Q1):
            problems: list[str] = []
            if not q1.ranking_source:
                problems.append("no ranking source is named")
            if not q1.quartile:
                problems.append("no quartile value is recorded")
            if not q1.ranking_year:
                problems.append("no ranking year is recorded")
            if not q1.matched_on:
                problems.append("the match basis (ISSN or journal name) is not recorded")
            if problems:
                result.add(
                    self.name,
                    "quartile has a valid source",
                    target,
                    CheckOutcome.FAIL,
                    detail=(
                        f"The status is '{status.value}' but {', '.join(problems)}. A "
                        "quartile without a dated source is not evidence."
                    ),
                    record_id=record.record_id,
                    original_value=status.value,
                    corrected_value=Q1Status.UNVERIFIED.value,
                    correction_source="Verification of the quartile evidence chain",
                    correction_reason=(
                        "Recommended downgrade; the record was not modified "
                        "automatically."
                    ),
                    recommended_action=(
                        "Re-run the Q1 stage with a licensed ranking file, or treat this "
                        "paper as Unverified."
                    ),
                )
            else:
                detail = (
                    f"{q1.quartile} from {q1.ranking_source} "
                    f"({q1.ranking_year} ranking, matched on {q1.matched_on})."
                )
                if record.year and q1.ranking_year and q1.ranking_year != record.year:
                    result.add(
                        self.name,
                        "quartile has a valid source",
                        target,
                        CheckOutcome.WARNING,
                        detail=(
                            detail
                            + f" The paper was published in {record.year}, so a "
                            "different ranking year was used."
                        ),
                        record_id=record.record_id,
                        recommended_action=(
                            "Confirm the quartile for the publication year if the "
                            "distinction matters for your inclusion rule."
                        ),
                    )
                else:
                    result.add(
                        self.name, "quartile has a valid source", target,
                        CheckOutcome.PASS, detail=detail, record_id=record.record_id,
                    )
        elif status == Q1Status.CONFLICTING:
            result.add(
                self.name,
                "quartile has a valid source",
                target,
                CheckOutcome.WARNING,
                detail=(
                    "The journal holds different quartiles across subject categories: "
                    f"{q1.subject_category or 'categories not recorded'}. This was "
                    "reported rather than resolved."
                ),
                record_id=record.record_id,
                recommended_action=(
                    "Choose the subject category relevant to this paper and record the "
                    "quartile for it."
                ),
            )
        elif status == Q1Status.NOT_APPLICABLE:
            result.add(
                self.name, "quartile has a valid source", target,
                CheckOutcome.NOT_APPLICABLE, detail=q1.notes, record_id=record.record_id,
            )
        else:
            result.add(
                self.name,
                "quartile has a valid source",
                target,
                CheckOutcome.WARNING,
                detail=(
                    "The quartile is unverified. "
                    + (q1.notes or "No ranking evidence was available.")
                    + " No quartile has been assumed."
                ),
                record_id=record.record_id,
                recommended_action=(
                    "Supply a licensed Scimago or JCR export via q1_ranking.file and "
                    "re-run the verify stage."
                ),
            )

    def _check_merges(
        self,
        records: list[PaperRecord],
        result: VerificationResult,
        merge_events: list[dict[str, Any]],
    ) -> None:
        """Check that duplicate merges were sound and left no duplicates behind."""
        # Any surviving duplicate DOI is a merge failure.
        by_doi: dict[str, list[PaperRecord]] = {}
        for record in records:
            if record.doi:
                by_doi.setdefault(record.doi, []).append(record)
        for doi, group in by_doi.items():
            if len(group) > 1:
                result.add(
                    self.name,
                    "duplicates correctly merged",
                    f"DOI {doi}",
                    CheckOutcome.FAIL,
                    detail=(
                        f"{len(group)} separate records still share DOI {doi}, so "
                        "deduplication did not merge them."
                    ),
                    recommended_action="Re-run the deduplicate stage.",
                )

        by_title: dict[str, list[PaperRecord]] = {}
        for record in records:
            key = record.normalized_title
            if key:
                by_title.setdefault(key, []).append(record)
        for title, group in by_title.items():
            if len(group) > 1:
                result.add(
                    self.name,
                    "duplicates correctly merged",
                    f"Title: {title[:60]}",
                    CheckOutcome.WARNING,
                    detail=(
                        f"{len(group)} records share an identical normalised title but "
                        "were not merged, which happens when their DOIs differ."
                    ),
                    recommended_action=(
                        "Check whether these are genuinely different papers, such as a "
                        "preprint and its published version."
                    ),
                )

        for event in merge_events:
            similarity = float(event.get("similarity", 0) or 0)
            rule = str(event.get("rule", ""))
            if rule.startswith("fuzzy") and similarity < 90:
                result.add(
                    self.name,
                    "duplicates correctly merged",
                    f"{event.get('survivor_id')} <- {event.get('absorbed_id')}",
                    CheckOutcome.WARNING,
                    detail=(
                        f"Merged on fuzzy title similarity of only {similarity:.0f}%: "
                        f"'{str(event.get('survivor_title'))[:60]}' and "
                        f"'{str(event.get('absorbed_title'))[:60]}'."
                    ),
                    recommended_action="Confirm these are the same paper.",
                )
            else:
                result.add(
                    self.name,
                    "duplicates correctly merged",
                    f"{event.get('survivor_id')} <- {event.get('absorbed_id')}",
                    CheckOutcome.PASS,
                    detail=f"Merged by {rule} ({similarity:.0f}% similarity).",
                )

        if not merge_events and not any(len(g) > 1 for g in by_doi.values()):
            result.add(
                self.name,
                "duplicates correctly merged",
                "all records",
                CheckOutcome.PASS,
                detail="No duplicate merges were needed and none remain.",
            )


# ---------------------------------------------------------------------------
# 2. Evidence and citation verifier
# ---------------------------------------------------------------------------


class EvidenceCitationVerifier:
    """Audits claims, page references, numeric values, and citations."""

    name = "evidence-citation-verifier"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def verify(
        self,
        records: list[PaperRecord],
        analyses: dict[str, PaperAnalysis],
        ledger: EvidenceLedger,
        audit_rows: list[CitationAuditRow],
        result: VerificationResult,
        *,
        document_texts: dict[str, str] | None = None,
    ) -> VerificationResult:
        """Run every evidence and citation check."""
        document_texts = document_texts or {}
        by_id = {r.record_id: r for r in records}

        if not ledger.records:
            result.add(
                self.name,
                "every claim is supported",
                "evidence ledger",
                CheckOutcome.WARNING,
                detail=(
                    "The evidence ledger is empty, so no synthesis claim could be "
                    "checked. This happens when no paper was analysed."
                ),
                recommended_action="Obtain and analyse at least one paper, then re-run.",
            )

        # --- Claim support and page evidence ---
        page_cache: dict[str, dict[int, str]] = {}
        for evidence in ledger.records:
            target = f"{evidence.evidence_id} ({evidence.document})"

            record = by_id.get(evidence.record_id)
            if record is None:
                result.add(
                    self.name,
                    "every claim is supported",
                    target,
                    CheckOutcome.FAIL,
                    detail=(
                        f"The claim cites record {evidence.record_id}, which is not in "
                        "the retrieved set."
                    ),
                    record_id=evidence.record_id,
                    recommended_action="Remove the claim or re-source it.",
                )
                continue

            if not evidence.claim.strip():
                result.add(
                    self.name, "every claim is supported", target, CheckOutcome.FAIL,
                    detail="The ledger record has an empty claim.",
                    record_id=evidence.record_id,
                    recommended_action="Remove the empty ledger record.",
                )
                continue

            result.add(
                self.name, "every claim is supported", target, CheckOutcome.PASS,
                detail=f"Traced to {evidence.record_id}.", record_id=evidence.record_id,
            )

            # --- Page numbers exist and are within the document ---
            if evidence.stance == EvidenceStance.AUTHOR_STATED and not evidence.pages:
                result.add(
                    self.name,
                    "evidence page numbers are correct",
                    target,
                    CheckOutcome.WARNING,
                    detail=(
                        "An author-stated claim carries no page reference, so a reviewer "
                        "cannot locate it quickly."
                    ),
                    record_id=evidence.record_id,
                    recommended_action="Add the page number from the source PDF.",
                )
            elif evidence.pages:
                self._check_pages(evidence, record, result, target, page_cache)

            # --- Quoted text really appears on the cited page ---
            if evidence.supporting_text and evidence.pages:
                self._check_quote(evidence, record, result, target, page_cache)

        # --- Numeric values in claims match the source text ---
        for evidence in ledger.records:
            self._check_numbers(evidence, by_id.get(evidence.record_id), result, page_cache)

        # --- Model descriptions match their source papers ---
        for record_id, analysis in analyses.items():
            self._check_model_description(record_id, analysis, by_id.get(record_id), result)

        # --- Author-stated and agent-inferred gaps are separated ---
        self._check_gap_separation(ledger, result)

        # --- Citation audit outcomes ---
        for row in audit_rows:
            target = f"{row.in_text_citation} ({row.record_id})"
            if row.outcome == CheckOutcome.FAIL:
                result.add(
                    self.name,
                    "citations appear in the reference list",
                    target,
                    CheckOutcome.FAIL,
                    detail=row.notes,
                    record_id=row.record_id,
                    recommended_action=(
                        "Fix the citation or remove the claim; no citation has been "
                        "fabricated."
                    ),
                )
            elif row.outcome == CheckOutcome.WARNING:
                result.add(
                    self.name,
                    "reference-list entries are cited in the text",
                    target,
                    CheckOutcome.WARNING,
                    detail=row.notes,
                    record_id=row.record_id,
                    recommended_action=(
                        "Cite the paper in the synthesis or remove it from the reference "
                        "list."
                    ),
                )
            else:
                result.add(
                    self.name, "citations appear in the reference list", target,
                    CheckOutcome.PASS, record_id=row.record_id,
                )

        # --- No unsupported citation appears in a document ---
        self._check_no_unsupported_citations(ledger, document_texts, result)

        return result

    def _pages_for(
        self, record: PaperRecord, cache: dict[str, dict[int, str]]
    ) -> dict[int, str]:
        """Load and cache a paper's extracted pages by page number."""
        if record.record_id in cache:
            return cache[record.record_id]
        pages: dict[int, str] = {}
        if record.extracted_text_path and Path(record.extracted_text_path).exists():
            for page in load_pages(Path(record.extracted_text_path)):
                if page.readable:
                    pages[page.number] = page.text
        cache[record.record_id] = pages
        return pages

    def _check_pages(
        self,
        evidence: Any,
        record: PaperRecord,
        result: VerificationResult,
        target: str,
        cache: dict[str, dict[int, str]],
    ) -> None:
        """Confirm cited page numbers exist in the extracted document."""
        pages = self._pages_for(record, cache)
        if not pages:
            result.add(
                self.name,
                "evidence page numbers are correct",
                target,
                CheckOutcome.NOT_APPLICABLE,
                detail=(
                    "No extracted text is available for this paper, so the page "
                    "references could not be checked."
                ),
                record_id=record.record_id,
            )
            return

        invalid = [p for p in evidence.pages if p not in pages]
        if invalid:
            result.add(
                self.name,
                "evidence page numbers are correct",
                target,
                CheckOutcome.FAIL,
                detail=(
                    f"Page(s) {invalid} are cited but do not exist as readable pages in "
                    f"the extracted text (available: 1-{max(pages)})."
                ),
                record_id=record.record_id,
                recommended_action="Re-run the analysis stage for this paper.",
            )
        else:
            result.add(
                self.name, "evidence page numbers are correct", target,
                CheckOutcome.PASS,
                detail=f"Page(s) {evidence.pages} exist in the extracted text.",
                record_id=record.record_id,
            )

    def _check_quote(
        self,
        evidence: Any,
        record: PaperRecord,
        result: VerificationResult,
        target: str,
        cache: dict[str, dict[int, str]],
    ) -> None:
        """Confirm the supporting text really appears on a cited page."""
        pages = self._pages_for(record, cache)
        if not pages:
            return
        needle = normalize_title(evidence.supporting_text)[:120]
        if not needle:
            return
        haystack = " ".join(
            normalize_title(pages.get(p, "")) for p in evidence.pages if p in pages
        )
        if needle in haystack:
            result.add(
                self.name, "supporting text appears on the cited page", target,
                CheckOutcome.PASS, record_id=record.record_id,
            )
        else:
            whole = " ".join(normalize_title(text) for text in pages.values())
            if needle in whole:
                result.add(
                    self.name,
                    "supporting text appears on the cited page",
                    target,
                    CheckOutcome.WARNING,
                    detail=(
                        "The supporting text is in the paper but not on the page cited, "
                        "so the page reference is wrong."
                    ),
                    record_id=record.record_id,
                    recommended_action="Correct the page reference for this claim.",
                )
            else:
                result.add(
                    self.name,
                    "supporting text appears on the cited page",
                    target,
                    CheckOutcome.FAIL,
                    detail=(
                        "The quoted supporting text does not appear in the paper's "
                        "extracted text at all."
                    ),
                    record_id=record.record_id,
                    recommended_action=(
                        "Remove this claim; the quotation cannot be verified against "
                        "the source."
                    ),
                )

    def _check_numbers(
        self,
        evidence: Any,
        record: PaperRecord | None,
        result: VerificationResult,
        cache: dict[str, dict[int, str]],
    ) -> None:
        """Confirm numeric values in a claim appear in the source text."""
        if record is None:
            return
        numbers = re.findall(r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)(?![\w])", evidence.claim)
        meaningful = [n for n in numbers if len(n.replace(",", "").replace(".", "")) >= 2]
        if not meaningful:
            return

        pages = self._pages_for(record, cache)
        if not pages:
            return
        source_text = " ".join(pages.values())
        source_numbers = set(
            re.findall(r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)(?![\w])", source_text)
        )
        normalised_source = {n.replace(",", "") for n in source_numbers}

        target = f"{evidence.evidence_id} numeric values"
        missing = [
            n for n in meaningful if n.replace(",", "") not in normalised_source
        ]
        if missing:
            result.add(
                self.name,
                "numerical results match the paper",
                target,
                CheckOutcome.FAIL,
                detail=(
                    f"The claim contains number(s) {missing} that do not appear in the "
                    "paper's extracted text."
                ),
                record_id=record.record_id,
                recommended_action=(
                    "Check the figure against the source and correct or remove the claim."
                ),
            )
        else:
            result.add(
                self.name, "numerical results match the paper", target,
                CheckOutcome.PASS,
                detail=f"All {len(meaningful)} number(s) in the claim appear in the source.",
                record_id=record.record_id,
            )

    def _check_model_description(
        self,
        record_id: str,
        analysis: PaperAnalysis,
        record: PaperRecord | None,
        result: VerificationResult,
    ) -> None:
        """Confirm a described model is actually named in the paper's text."""
        model_field = analysis.field("model_or_method")
        target = f"{record_id}: model description"
        if not model_field.is_reported:
            result.add(
                self.name, "model description matches the source paper", target,
                CheckOutcome.NOT_APPLICABLE,
                detail="No model or method was extracted for this paper.",
                record_id=record_id,
            )
            return

        if record is None or not record.extracted_text_path:
            result.add(
                self.name, "model description matches the source paper", target,
                CheckOutcome.NOT_APPLICABLE,
                detail="No extracted text is available for comparison.",
                record_id=record_id,
            )
            return

        path = Path(record.extracted_text_path)
        if not path.exists():
            result.add(
                self.name, "model description matches the source paper", target,
                CheckOutcome.NOT_APPLICABLE,
                detail="The extracted text file is missing.", record_id=record_id,
            )
            return

        source = normalize_title(path.read_text(encoding="utf-8", errors="replace"))
        unfound = [m for m in analysis.detected_methods if normalize_title(m) not in source]
        if unfound:
            result.add(
                self.name,
                "model description matches the source paper",
                target,
                CheckOutcome.FAIL,
                detail=(
                    f"Method(s) {unfound} are attributed to this paper but do not appear "
                    "in its extracted text."
                ),
                record_id=record_id,
                recommended_action="Re-run the analysis stage for this paper.",
            )
        else:
            result.add(
                self.name, "model description matches the source paper", target,
                CheckOutcome.PASS,
                detail=(
                    f"{len(analysis.detected_methods)} method name(s) confirmed in the "
                    "source text."
                    if analysis.detected_methods
                    else "The model is described in prose that the source text supports."
                ),
                record_id=record_id,
            )

    def _check_gap_separation(self, ledger: EvidenceLedger, result: VerificationResult) -> None:
        """Confirm author-stated and agent-inferred gaps are kept apart."""
        gap_records = [
            r for r in ledger.records if r.document == "Research_Gaps.docx"
        ]
        if not gap_records:
            result.add(
                self.name,
                "author-stated and inferred gaps are separated",
                "Research_Gaps.docx",
                CheckOutcome.NOT_APPLICABLE,
                detail="No gap records were produced.",
            )
            return

        misfiled = [
            r
            for r in gap_records
            if r.section == "Agent-inferred gaps"
            and r.stance == EvidenceStance.AUTHOR_STATED
        ]
        misfiled += [
            r
            for r in gap_records
            if r.section == "Author-stated gaps"
            and r.stance == EvidenceStance.AGENT_INFERENCE
        ]
        if misfiled:
            result.add(
                self.name,
                "author-stated and inferred gaps are separated",
                "Research_Gaps.docx",
                CheckOutcome.FAIL,
                detail=(
                    f"{len(misfiled)} gap record(s) sit in a section that contradicts "
                    "their evidence stance."
                ),
                recommended_action="Re-run the report stage.",
            )
        else:
            result.add(
                self.name,
                "author-stated and inferred gaps are separated",
                "Research_Gaps.docx",
                CheckOutcome.PASS,
                detail=(
                    f"All {len(gap_records)} gap records sit in a section consistent "
                    "with their stance."
                ),
            )

    def _check_no_unsupported_citations(
        self,
        ledger: EvidenceLedger,
        document_texts: dict[str, str],
        result: VerificationResult,
    ) -> None:
        """Confirm no document cites something the ledger does not support."""
        if not document_texts:
            result.add(
                self.name,
                "no unsupported citation is present",
                "synthesis documents",
                CheckOutcome.NOT_APPLICABLE,
                detail="Document text was not supplied for scanning.",
            )
            return

        for document, text in document_texts.items():
            supported = {
                r.in_text_citation
                for r in ledger.for_document(document)
                if r.in_text_citation
            }
            found = set(re.findall(r"\(([A-Z][^()]{2,80}?, (?:\d{4}[a-z]?|n\.d\.))\)", text))
            unsupported = {
                f"({c})" for c in found if f"({c})" not in supported
            }
            # Grouped citations are split on ';' before comparison.
            truly_unsupported: set[str] = set()
            for candidate in unsupported:
                parts = [p.strip() for p in candidate.strip("()").split(";")]
                if not any(f"({p})" in supported for p in parts):
                    truly_unsupported.add(candidate)

            if truly_unsupported:
                result.add(
                    self.name,
                    "no unsupported citation is present",
                    document,
                    CheckOutcome.WARNING,
                    detail=(
                        f"{len(truly_unsupported)} citation string(s) in {document} have "
                        f"no matching ledger record: {sorted(truly_unsupported)[:5]}."
                    ),
                    recommended_action=(
                        "Check these citations; each claim must map to an evidence "
                        "ledger record."
                    ),
                )
            else:
                result.add(
                    self.name, "no unsupported citation is present", document,
                    CheckOutcome.PASS,
                    detail="Every citation in this document maps to a ledger record.",
                )


# ---------------------------------------------------------------------------
# 3. File verifier
# ---------------------------------------------------------------------------


class FileVerifier:
    """Checks that downloaded files are valid PDFs of the intended papers."""

    name = "file-verifier"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def verify(self, records: list[PaperRecord], result: VerificationResult) -> VerificationResult:
        """Run every file check over the downloaded papers."""
        downloaded = [
            r for r in records if r.download_status in
            (DownloadStatus.DOWNLOADED, DownloadStatus.ALREADY_PRESENT)
        ]
        if not downloaded:
            result.add(
                self.name,
                "downloaded files are valid PDFs",
                "all records",
                CheckOutcome.NOT_APPLICABLE,
                detail="No PDF was downloaded in this job.",
            )
            return result

        min_bytes = int(self.settings.pdf.get("min_bytes", 1024))
        min_chars = int(self.settings.pdf.get("min_extractable_chars_per_page", 60))

        for record in downloaded:
            target = f"{record.record_id}: {record.local_filename or 'no filename'}"

            if not record.local_path:
                result.add(
                    self.name, "downloaded files are valid PDFs", target, CheckOutcome.FAIL,
                    detail="The record is marked downloaded but no local path is recorded.",
                    record_id=record.record_id,
                    recommended_action="Re-run the download stage for this paper.",
                )
                continue

            path = Path(record.local_path)
            if not path.exists():
                result.add(
                    self.name, "downloaded files are valid PDFs", target, CheckOutcome.FAIL,
                    detail=f"The recorded file is missing from disk: {path}.",
                    record_id=record.record_id,
                    recommended_action="Re-run the download stage for this paper.",
                )
                continue

            validation = validate_pdf(
                path,
                min_bytes=min_bytes,
                expected_title=record.title,
                min_chars_per_page=min_chars,
            )

            # --- Valid PDF, not HTML, not zero-byte, not corrupt ---
            if validation.valid:
                result.add(
                    self.name, "downloaded files are valid PDFs", target, CheckOutcome.PASS,
                    detail=f"{validation.size_bytes:,} bytes, {validation.page_count} pages.",
                    record_id=record.record_id,
                )
            else:
                result.add(
                    self.name, "downloaded files are valid PDFs", target, CheckOutcome.FAIL,
                    detail=validation.reason,
                    record_id=record.record_id,
                    recommended_action=(
                        "Delete the file and re-run the download stage, or obtain the "
                        "PDF manually."
                    ),
                )

            result.add(
                self.name,
                "no HTML error page is present",
                target,
                CheckOutcome.FAIL if validation.is_html else CheckOutcome.PASS,
                detail=(
                    "The file contains HTML, not PDF data."
                    if validation.is_html
                    else "The file is genuine PDF data."
                ),
                record_id=record.record_id,
                recommended_action=(
                    "Delete the file and retrieve the paper manually."
                    if validation.is_html
                    else ""
                ),
            )
            result.add(
                self.name,
                "no zero-byte or corrupt file is present",
                target,
                CheckOutcome.PASS if validation.size_bytes > min_bytes else CheckOutcome.FAIL,
                detail=f"{validation.size_bytes:,} bytes on disk.",
                record_id=record.record_id,
            )

            # --- The PDF corresponds to the intended paper ---
            title_check = any(
                c.startswith("content matches the intended paper")
                for c in validation.checks_passed
            )
            title_failed = any(
                c.startswith("content matches the intended paper")
                for c in validation.checks_failed
            )
            if title_check:
                result.add(
                    self.name, "the PDF corresponds to the intended paper", target,
                    CheckOutcome.PASS,
                    detail="Title tokens were found in the document text or metadata.",
                    record_id=record.record_id,
                )
            elif title_failed:
                result.add(
                    self.name,
                    "the PDF corresponds to the intended paper",
                    target,
                    CheckOutcome.WARNING,
                    detail=(
                        "The document text did not confirm the expected title. The file "
                        f"may be a different paper. First readable line: "
                        f"'{validation.title_in_pdf or 'none'}'."
                    ),
                    record_id=record.record_id,
                    original_value=record.title,
                    corrected_value=validation.title_in_pdf or "",
                    correction_source="First readable line of the downloaded PDF",
                    correction_reason="Reported for review; nothing was changed.",
                    recommended_action="Open the PDF and confirm it is the intended paper.",
                )
            else:
                result.add(
                    self.name, "the PDF corresponds to the intended paper", target,
                    CheckOutcome.NOT_APPLICABLE,
                    detail=(
                        "The PDF has no extractable text, so its content could not be "
                        "compared with the expected title."
                    ),
                    record_id=record.record_id,
                )

            # --- The filename matches the paper title ---
            expected_stem = safe_filename_stem(record.title)
            actual_stem = path.stem
            if actual_stem == expected_stem or actual_stem.startswith(expected_stem[:60]):
                result.add(
                    self.name, "the filename matches the paper title", target,
                    CheckOutcome.PASS, record_id=record.record_id,
                )
            else:
                result.add(
                    self.name,
                    "the filename matches the paper title",
                    target,
                    CheckOutcome.WARNING,
                    detail=(
                        f"The filename stem '{actual_stem[:60]}' differs from the title-derived "
                        f"stem '{expected_stem[:60]}'. This is expected when a filename "
                        "collision was resolved."
                    ),
                    record_id=record.record_id,
                    original_value=actual_stem,
                    corrected_value=expected_stem,
                    correction_source="Paper title",
                    correction_reason="Reported only; the file was not renamed.",
                )

            # --- Checksum recorded and still correct ---
            if record.file_sha256:
                current = sha256_file(path)
                if current == record.file_sha256:
                    result.add(
                        self.name, "file checksum is recorded and matches", target,
                        CheckOutcome.PASS, detail=current[:16], record_id=record.record_id,
                    )
                else:
                    result.add(
                        self.name,
                        "file checksum is recorded and matches",
                        target,
                        CheckOutcome.FAIL,
                        detail="The file on disk no longer matches its recorded checksum.",
                        record_id=record.record_id,
                        original_value=record.file_sha256,
                        corrected_value=current,
                        correction_source="Re-computed SHA-256 of the file on disk",
                        correction_reason="Reported only; the record was not modified.",
                        recommended_action="Confirm the file was not replaced or corrupted.",
                    )
            else:
                result.add(
                    self.name, "file checksum is recorded and matches", target,
                    CheckOutcome.WARNING,
                    detail="No checksum was recorded for this download.",
                    record_id=record.record_id,
                    recommended_action="Re-run the download stage to record a checksum.",
                )

            # --- Extracted text belongs to the same paper ---
            if record.extracted_text_path and Path(record.extracted_text_path).exists():
                text = Path(record.extracted_text_path).read_text(
                    encoding="utf-8", errors="replace"
                )
                if verify_extracted_text_matches(record.title, text):
                    result.add(
                        self.name, "extracted text belongs to the same paper", target,
                        CheckOutcome.PASS, record_id=record.record_id,
                    )
                else:
                    result.add(
                        self.name,
                        "extracted text belongs to the same paper",
                        target,
                        CheckOutcome.WARNING,
                        detail=(
                            "The extracted text does not clearly contain the paper's "
                            "title, so the text file may be paired with the wrong PDF."
                        ),
                        record_id=record.record_id,
                        recommended_action=(
                            "Delete the .txt file and re-run the extract stage."
                        ),
                    )
            else:
                result.add(
                    self.name, "extracted text belongs to the same paper", target,
                    CheckOutcome.NOT_APPLICABLE,
                    detail="No extracted text file exists for this paper.",
                    record_id=record.record_id,
                )

        return result


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run_verification(
    records: list[PaperRecord],
    analyses: dict[str, PaperAnalysis],
    ledger: EvidenceLedger,
    audit_rows: list[CitationAuditRow],
    config: JobConfig,
    settings: Settings,
    *,
    merge_events: list[dict[str, Any]] | None = None,
    document_texts: dict[str, str] | None = None,
    discovered_count: int = 0,
    client: HttpClient | None = None,
) -> VerificationResult:
    """Run all three verifiers and compute the summary."""
    result = VerificationResult()

    metadata_verifier = MetadataQ1Verifier(settings, client=client)
    try:
        metadata_verifier.verify(records, result, merge_events=merge_events)
    finally:
        metadata_verifier.close()

    EvidenceCitationVerifier(settings).verify(
        records, analyses, ledger, audit_rows, result, document_texts=document_texts
    )
    FileVerifier(settings).verify(records, result)

    result.summary = summarise(
        records, analyses, ledger, result, discovered_count=discovered_count
    )
    _apply_confidence(records, result)

    LOG.info(
        f"Verification complete: {result.summary.passed_checks} passed, "
        f"{result.summary.warnings} warning(s), "
        f"{result.summary.unresolved_problems} unresolved problem(s). "
        f"Overall confidence: {result.summary.overall_confidence}."
    )
    return result


def summarise(
    records: list[PaperRecord],
    analyses: dict[str, PaperAnalysis],
    ledger: EvidenceLedger,
    result: VerificationResult,
    *,
    discovered_count: int = 0,
) -> VerificationSummary:
    """Compute the counts and overall confidence for the verification report."""
    counts = result.counts()
    selected = [r for r in records if r.selected]
    downloaded = [
        r for r in records
        if r.download_status in (DownloadStatus.DOWNLOADED, DownloadStatus.ALREADY_PRESENT)
    ]
    failed = [
        r for r in selected
        if r.download_status in (DownloadStatus.FAILED, DownloadStatus.SKIPPED_NO_LEGAL_URL)
    ]

    summary = VerificationSummary(
        total_discovered=discovered_count or len(records),
        total_unique=len(records),
        total_included=len(selected),
        total_verified_q1=sum(
            1 for r in records if r.q1.verification_status == Q1Status.VERIFIED_Q1
        ),
        total_unverified_quartile=sum(
            1 for r in records
            if r.q1.verification_status in (Q1Status.UNVERIFIED, Q1Status.CONFLICTING)
        ),
        total_pdfs_downloaded=len(downloaded),
        total_failed_downloads=len(failed),
        total_papers_analysed=len(analyses),
        total_claims_checked=len(ledger.records),
        passed_checks=counts.get(CheckOutcome.PASS.value, 0),
        warnings=counts.get(CheckOutcome.WARNING.value, 0),
        unresolved_problems=counts.get(CheckOutcome.FAIL.value, 0),
    )

    # --- Recommended manual checks ---
    checks: list[str] = []
    if summary.total_unverified_quartile:
        checks.append(
            f"Verify the journal quartile for {summary.total_unverified_quartile} paper(s) "
            "in your licensed Scimago or JCR subscription, then re-run the verify stage."
        )
    if summary.total_failed_downloads:
        checks.append(
            f"Retrieve {summary.total_failed_downloads} paper(s) listed in "
            "Unable_to_Download.docx through your own institutional access."
        )
    ocr_needed = sum(1 for r in records if r.requires_ocr)
    if ocr_needed:
        checks.append(
            f"{ocr_needed} PDF(s) have no text layer. Run OCR on them, or accept that "
            "they contribute no evidence."
        )
    for finding in result.failures()[:15]:
        if finding.recommended_action:
            checks.append(f"{finding.check}: {finding.recommended_action}")
    no_pages = ledger.claims_without_pages()
    if no_pages:
        checks.append(
            f"Add page references to {len(no_pages)} author-stated claim(s) that "
            "currently have none."
        )
    if not analyses:
        checks.append(
            "No paper was analysed, so every synthesis document is empty of findings. "
            "Obtain at least one accessible paper and re-run."
        )
    summary.recommended_manual_checks = checks

    # --- Overall confidence ---
    total_checks = max(
        summary.passed_checks + summary.warnings + summary.unresolved_problems, 1
    )
    pass_rate = summary.passed_checks / total_checks
    if summary.total_papers_analysed == 0:
        summary.overall_confidence = "None - no paper was analysed"
    elif summary.unresolved_problems > 0:
        summary.overall_confidence = (
            f"Low - {summary.unresolved_problems} unresolved problem(s) must be reviewed"
        )
    elif pass_rate >= 0.9 and summary.total_papers_analysed >= 5 and summary.warnings <= total_checks * 0.1:
        summary.overall_confidence = "High"
    elif pass_rate >= 0.75:
        summary.overall_confidence = "Medium"
    else:
        summary.overall_confidence = "Low"
    return summary


def _apply_confidence(records: list[PaperRecord], result: VerificationResult) -> None:
    """Write a per-paper verification confidence onto each record."""
    by_record: dict[str, list[VerificationFinding]] = {}
    for finding in result.findings:
        if finding.record_id:
            by_record.setdefault(finding.record_id, []).append(finding)

    for record in records:
        findings = by_record.get(record.record_id, [])
        if not findings:
            record.verification_confidence = "Not assessed"
            continue
        failures = sum(1 for f in findings if f.outcome == CheckOutcome.FAIL)
        warnings = sum(1 for f in findings if f.outcome == CheckOutcome.WARNING)
        if failures:
            record.verification_confidence = f"Low - {failures} failed check(s)"
        elif warnings > 2:
            record.verification_confidence = f"Medium - {warnings} warning(s)"
        elif warnings:
            record.verification_confidence = f"High - {warnings} minor warning(s)"
        else:
            record.verification_confidence = "High - all checks passed"


def write_unresolved_issues_csv(result: VerificationResult, path: Path) -> Path:
    """Write ``unresolved_issues.csv`` with every failure and warning."""
    import csv

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "finding_id", "verifier", "check", "target", "record_id", "outcome", "detail",
        "original_value", "corrected_value", "correction_source", "correction_reason",
        "recommended_action", "created_at",
    ]
    rows = [f for f in result.findings if f.outcome in (CheckOutcome.FAIL, CheckOutcome.WARNING)]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for finding in rows:
            writer.writerow(
                {
                    "finding_id": finding.finding_id,
                    "verifier": finding.verifier,
                    "check": finding.check,
                    "target": finding.target,
                    "record_id": finding.record_id or "",
                    "outcome": finding.outcome.value,
                    "detail": finding.detail,
                    "original_value": finding.original_value,
                    "corrected_value": finding.corrected_value,
                    "correction_source": finding.correction_source,
                    "correction_reason": finding.correction_reason,
                    "recommended_action": finding.recommended_action,
                    "created_at": finding.created_at,
                }
            )
    LOG.info(f"Wrote {path.name} with {len(rows)} outstanding issue(s).")
    return path
