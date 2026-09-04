"""PDF validation: is this file really the paper it claims to be?

A download is only accepted when the bytes pass every check here. The most
common failure this catches is an HTML error, login, or paywall page saved with
a ``.pdf`` extension — which would otherwise flow silently into the analysis
stage and corrupt the review.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .logging_setup import get_logger
from .utils import normalize_title, sha256_file, title_tokens

LOG = get_logger("pdf-validate")

#: Every valid PDF begins with this signature.
PDF_SIGNATURE = b"%PDF-"

#: Byte markers that identify HTML/XML content masquerading as a PDF.
HTML_MARKERS = (
    b"<!doctype html",
    b"<html",
    b"<head",
    b"<body",
    b"<?xml",
    b"<!DOCTYPE HTML",
)

#: Content types that are acceptable for a PDF download.
ACCEPTABLE_CONTENT_TYPES = (
    "application/pdf",
    "application/x-pdf",
    "application/octet-stream",
    "binary/octet-stream",
)


@dataclass
class ValidationResult:
    """Outcome of validating one candidate PDF file."""

    path: str
    valid: bool = False
    reason: str = ""
    checks_passed: list[str] = field(default_factory=list)
    checks_failed: list[str] = field(default_factory=list)
    size_bytes: int = 0
    page_count: int | None = None
    sha256: str | None = None
    is_html: bool = False
    is_encrypted: bool = False
    extractable_chars: int | None = None
    requires_ocr: bool = False
    title_in_pdf: str | None = None

    def add(self, name: str, passed: bool, detail: str = "") -> bool:
        """Record one check and return whether it passed."""
        label = f"{name}: {detail}" if detail else name
        (self.checks_passed if passed else self.checks_failed).append(label)
        return passed


def looks_like_pdf(data: bytes) -> bool:
    """True when the leading bytes carry the PDF signature.

    Tolerates the small amount of leading whitespace some servers prepend.
    """
    if not data:
        return False
    head = data[:1024]
    if head.startswith(PDF_SIGNATURE):
        return True
    stripped = head.lstrip(b"\r\n\t ")
    return stripped.startswith(PDF_SIGNATURE)


def looks_like_html(data: bytes) -> bool:
    """True when the leading bytes look like an HTML or XML document."""
    if not data:
        return False
    sample = data[:4096].lower()
    return any(marker.lower() in sample for marker in HTML_MARKERS)


def content_type_acceptable(content_type: str | None) -> bool:
    """True when a response's content type is plausible for a PDF."""
    if not content_type:
        # Absent header is not by itself disqualifying; the signature check decides.
        return True
    main = content_type.split(";")[0].strip().lower()
    if main in ACCEPTABLE_CONTENT_TYPES:
        return True
    return main.endswith("/pdf")


def open_pdf(path: Path):
    """Open a PDF with PyMuPDF, returning ``None`` if it cannot be parsed."""
    try:
        import pymupdf
    except ImportError:  # pragma: no cover - fallback for older installs
        try:
            import fitz as pymupdf  # type: ignore[no-redef]
        except ImportError:
            return None
    try:
        return pymupdf.open(str(path))
    except Exception as exc:  # noqa: BLE001 - a corrupt PDF is expected here
        LOG.debug(f"PyMuPDF could not open {Path(path).name}: {exc}")
        return None


def _pypdf_page_count(path: Path) -> int | None:
    """Second-opinion page count via pypdf, for PDFs PyMuPDF rejects."""
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover
        return None
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            try:
                reader.decrypt("")  # many PDFs use an empty owner password
            except Exception:  # noqa: BLE001
                return None
        return len(reader.pages)
    except Exception as exc:  # noqa: BLE001
        LOG.debug(f"pypdf could not read {Path(path).name}: {exc}")
        return None


def validate_pdf(
    path: Path,
    *,
    min_bytes: int = 1024,
    expected_title: str | None = None,
    min_chars_per_page: int = 60,
    content_type: str | None = None,
    compute_checksum: bool = True,
) -> ValidationResult:
    """Validate a downloaded file as a genuine, readable PDF.

    Checks, in order: existence, non-zero size, minimum size, content type,
    PDF signature, absence of HTML markers, parseability, page count, text
    extractability (flagging scanned PDFs for OCR), and — when
    ``expected_title`` is given — whether the document text plausibly belongs to
    the intended paper.
    """
    path = Path(path)
    result = ValidationResult(path=str(path))

    if not result.add("file exists", path.exists()):
        result.reason = "The file does not exist."
        return result

    result.size_bytes = path.stat().st_size
    if not result.add("non-zero size", result.size_bytes > 0):
        result.reason = "The file is zero bytes."
        return result
    if not result.add(
        "minimum size", result.size_bytes >= min_bytes, f"{result.size_bytes} bytes"
    ):
        result.reason = (
            f"The file is only {result.size_bytes} bytes, below the {min_bytes}-byte "
            "minimum, so it is almost certainly an error page rather than a paper."
        )
        return result

    result.add("content type", content_type_acceptable(content_type), content_type or "not sent")

    with path.open("rb") as handle:
        head = handle.read(8192)

    result.is_html = looks_like_html(head)
    if not result.add("not an HTML page", not result.is_html):
        result.reason = (
            "The file contains HTML, not PDF data. This is typically a publisher "
            "landing page, a login wall, or an error page saved with a .pdf name."
        )
        return result

    if not result.add("PDF signature", looks_like_pdf(head)):
        result.reason = "The file does not begin with the %PDF- signature."
        return result

    document = open_pdf(path)
    if document is None:
        fallback_pages = _pypdf_page_count(path)
        if fallback_pages:
            result.page_count = fallback_pages
            result.add("parseable", True, "via pypdf fallback")
        else:
            result.add("parseable", False)
            result.reason = "No PDF library could open the file; it appears corrupt."
            return result
    else:
        try:
            result.is_encrypted = bool(getattr(document, "is_encrypted", False))
            if result.is_encrypted and not document.authenticate(""):
                result.add("not password protected", False)
                result.reason = (
                    "The PDF is password protected. The agent does not attempt to "
                    "remove protection; obtain the file through your own access."
                )
                document.close()
                return result
            result.add("not password protected", True)
            result.page_count = document.page_count
            result.add("parseable", True, f"{result.page_count} pages")

            if result.page_count and result.page_count > 0:
                sample_pages = min(result.page_count, 5)
                text_parts = []
                for index in range(sample_pages):
                    try:
                        text_parts.append(document.load_page(index).get_text("text") or "")
                    except Exception:  # noqa: BLE001 - skip unreadable pages
                        continue
                sample_text = "\n".join(text_parts)
                result.extractable_chars = len(sample_text.strip())
                per_page = result.extractable_chars / max(sample_pages, 1)
                result.requires_ocr = per_page < min_chars_per_page
                result.add(
                    "text extractable",
                    not result.requires_ocr,
                    f"{per_page:.0f} chars/page in the first {sample_pages} pages",
                )
                if expected_title:
                    result.title_in_pdf = _first_meaningful_line(sample_text)
                    matched = title_plausibly_matches(expected_title, sample_text, document)
                    result.add(
                        "content matches the intended paper",
                        matched,
                        "title tokens found in the document" if matched else "title tokens not found",
                    )
        finally:
            if document is not None:
                document.close()

    if not result.add("has pages", bool(result.page_count and result.page_count > 0)):
        result.reason = "The PDF reports zero pages."
        return result

    if compute_checksum:
        result.sha256 = sha256_file(path)
        result.add("checksum recorded", True, result.sha256[:12])

    # A scanned PDF is a valid file that simply needs OCR; it is not a failure.
    hard_failures = [
        c for c in result.checks_failed
        if not c.startswith(("text extractable", "content matches", "content type"))
    ]
    result.valid = not hard_failures
    if result.valid:
        notes = []
        if result.requires_ocr:
            notes.append("no extractable text layer, so it needs OCR before analysis")
        if any(c.startswith("content matches") for c in result.checks_failed):
            notes.append("the document text did not clearly confirm the expected title")
        result.reason = (
            "Valid PDF" + (f"; {', '.join(notes)}" if notes else "")
        )
    else:
        result.reason = result.reason or f"Failed checks: {'; '.join(hard_failures)}"
    return result


def _first_meaningful_line(text: str) -> str | None:
    """Return the first line long enough to plausibly be a title."""
    for line in (text or "").splitlines():
        cleaned = " ".join(line.split())
        if len(cleaned) >= 20 and not cleaned.lower().startswith(("doi", "http", "www")):
            return cleaned[:300]
    return None


def title_plausibly_matches(expected_title: str, sample_text: str, document=None) -> bool:
    """Check that a PDF's own text supports the expected paper title.

    Requires a majority of the title's significant tokens to appear in the
    document's opening pages or its embedded metadata. Deliberately lenient:
    the aim is to catch a wrong or substituted file, not to reject a paper over
    typesetting differences.
    """
    tokens = title_tokens(expected_title)
    if not tokens:
        return True

    haystack = normalize_title(sample_text)
    if document is not None:
        try:
            metadata = document.metadata or {}
            haystack = f"{haystack} {normalize_title(metadata.get('title', ''))}"
        except Exception:  # noqa: BLE001
            pass

    if not haystack.strip():
        return False

    hits = sum(1 for token in tokens if token in haystack)
    return (hits / len(tokens)) >= 0.5


def verify_extracted_text_matches(
    expected_title: str, text: str, *, minimum_ratio: float = 0.4
) -> bool:
    """Confirm extracted text belongs to the expected paper.

    Used by the file verifier so a mis-paired ``.txt``/``.pdf`` combination is
    caught before analysis relies on it.
    """
    tokens = title_tokens(expected_title)
    if not tokens or not text:
        return not tokens
    haystack = normalize_title(text[:20000])
    hits = sum(1 for token in tokens if token in haystack)
    return (hits / len(tokens)) >= minimum_ratio
