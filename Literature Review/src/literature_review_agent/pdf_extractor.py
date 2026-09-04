"""Text extraction with page boundaries preserved.

Page boundaries matter: every claim the analysis stage makes carries the page
numbers it came from, and the citation verifier checks those numbers. Extracted
text therefore keeps explicit page markers rather than being flattened.

Scanned (image-only) PDFs are detected and flagged as requiring OCR. OCR is an
optional extra; when it is unavailable the pages are marked as unreadable and
**no text is invented** to fill the gap.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings
from .logging_setup import get_logger
from .schemas import PaperRecord
from .utils import ensure_dir, safe_filename_stem

LOG = get_logger("extract")

#: Marker written before each page's text. The analyser parses these back into
#: page numbers, so the format must stay stable.
PAGE_MARKER = "=== PAGE {number} ==="

#: Regex matching the marker when reading extracted text back.
PAGE_MARKER_RE = re.compile(r"^=== PAGE (\d+) ===$", re.MULTILINE)

#: Text written in place of an unreadable page. Never replaced with guesses.
UNREADABLE_NOTE = "[No extractable text on this page. OCR required.]"


@dataclass
class PageText:
    """Text extracted from one page."""

    number: int
    text: str
    characters: int = 0
    readable: bool = True

    def __post_init__(self) -> None:
        self.characters = len(self.text.strip())


@dataclass
class ExtractionResult:
    """Everything the extraction stage learned about one PDF."""

    record_id: str
    pdf_path: str
    text_path: str | None = None
    pages: list[PageText] = field(default_factory=list)
    page_count: int = 0
    total_characters: int = 0
    requires_ocr: bool = False
    ocr_applied: bool = False
    extractor: str = "pymupdf"
    error: str = ""

    @property
    def success(self) -> bool:
        """True when at least some readable text was recovered."""
        return bool(self.pages) and self.total_characters > 0

    @property
    def unreadable_pages(self) -> list[int]:
        """Page numbers with no extractable text."""
        return [p.number for p in self.pages if not p.readable]

    def full_text(self) -> str:
        """The page-marked document text."""
        parts: list[str] = []
        for page in self.pages:
            parts.append(PAGE_MARKER.format(number=page.number))
            parts.append(page.text if page.readable else UNREADABLE_NOTE)
            parts.append("")
        return "\n".join(parts)


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def _open_document(path: Path):
    """Open a PDF with PyMuPDF, returning ``None`` on failure."""
    try:
        import pymupdf
    except ImportError:  # pragma: no cover - older installs expose `fitz`
        try:
            import fitz as pymupdf  # type: ignore[no-redef]
        except ImportError:
            return None
    try:
        return pymupdf.open(str(path))
    except Exception as exc:  # noqa: BLE001
        LOG.debug(f"PyMuPDF could not open {path.name}: {exc}")
        return None


def extract_with_pymupdf(path: Path, *, max_pages: int, min_chars: int) -> ExtractionResult | None:
    """Extract page-by-page text using PyMuPDF."""
    document = _open_document(path)
    if document is None:
        return None

    result = ExtractionResult(record_id="", pdf_path=str(path), extractor="pymupdf")
    try:
        if getattr(document, "is_encrypted", False) and not document.authenticate(""):
            result.error = (
                "The PDF is password protected; the agent does not remove protection."
            )
            return result

        result.page_count = document.page_count
        limit = min(document.page_count, max_pages)
        for index in range(limit):
            try:
                raw = document.load_page(index).get_text("text") or ""
            except Exception as exc:  # noqa: BLE001 - keep going past a bad page
                LOG.debug(f"Page {index + 1} of {path.name} could not be read: {exc}")
                raw = ""
            cleaned = _clean_page_text(raw)
            readable = len(cleaned.strip()) >= min_chars
            result.pages.append(
                PageText(number=index + 1, text=cleaned, readable=readable)
            )
    finally:
        document.close()

    result.total_characters = sum(p.characters for p in result.pages if p.readable)
    readable_pages = sum(1 for p in result.pages if p.readable)
    result.requires_ocr = bool(result.pages) and readable_pages == 0
    return result


def extract_with_pypdf(path: Path, *, max_pages: int, min_chars: int) -> ExtractionResult | None:
    """Fallback extraction using pypdf, for PDFs PyMuPDF cannot open."""
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover
        return None
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:  # noqa: BLE001
                return None
    except Exception as exc:  # noqa: BLE001
        LOG.debug(f"pypdf could not open {path.name}: {exc}")
        return None

    result = ExtractionResult(record_id="", pdf_path=str(path), extractor="pypdf")
    result.page_count = len(reader.pages)
    for index, page in enumerate(reader.pages[:max_pages]):
        try:
            raw = page.extract_text() or ""
        except Exception:  # noqa: BLE001
            raw = ""
        cleaned = _clean_page_text(raw)
        result.pages.append(
            PageText(number=index + 1, text=cleaned, readable=len(cleaned.strip()) >= min_chars)
        )
    result.total_characters = sum(p.characters for p in result.pages if p.readable)
    result.requires_ocr = bool(result.pages) and not any(p.readable for p in result.pages)
    return result


def _clean_page_text(text: str) -> str:
    """Tidy extracted page text without altering its content.

    Repairs hyphenated line breaks and collapses runaway whitespace. It never
    adds, reorders, or paraphrases words.
    """
    if not text:
        return ""
    # Join words split across a line break: "trans-\nport" -> "transport".
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)
    # Collapse three or more blank lines.
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Strip trailing spaces per line.
    lines = [line.rstrip() for line in text.splitlines()]
    return "\n".join(lines).strip()


def apply_ocr(result: ExtractionResult, path: Path, *, min_chars: int) -> ExtractionResult:
    """Attempt OCR on unreadable pages, if the optional extras are installed.

    When OCR is unavailable the pages stay marked unreadable. Nothing is
    fabricated to fill them.
    """
    try:
        import pytesseract  # noqa: F401
        from PIL import Image  # noqa: F401
    except ImportError:
        LOG.warning(
            "OCR was requested but pytesseract/Pillow are not installed. Pages with "
            "no text layer remain marked as requiring OCR; no text has been invented. "
            "Install with: pip install 'literature-review-agent[ocr]'"
        )
        return result

    document = _open_document(path)
    if document is None:
        return result

    import io

    import pytesseract
    from PIL import Image

    recovered = 0
    try:
        for page in result.pages:
            if page.readable:
                continue
            try:
                pixmap = document.load_page(page.number - 1).get_pixmap(dpi=300)
                image = Image.open(io.BytesIO(pixmap.tobytes("png")))
                text = _clean_page_text(pytesseract.image_to_string(image))
            except Exception as exc:  # noqa: BLE001 - OCR failure is not fatal
                LOG.debug(f"OCR failed on page {page.number}: {exc}")
                continue
            if len(text.strip()) >= min_chars:
                page.text = text
                page.characters = len(text.strip())
                page.readable = True
                recovered += 1
    finally:
        document.close()

    if recovered:
        result.ocr_applied = True
        result.total_characters = sum(p.characters for p in result.pages if p.readable)
        result.requires_ocr = not any(p.readable for p in result.pages)
        LOG.info(f"OCR recovered text from {recovered} page(s) of {Path(path).name}.")
    return result


def extract_pdf(
    path: Path,
    settings: Settings,
    *,
    record_id: str = "",
    output_dir: Path | None = None,
    output_name: str | None = None,
) -> ExtractionResult:
    """Extract one PDF's text and, when asked, write it alongside the paper."""
    path = Path(path)
    pdf_config = settings.pdf
    max_pages = int(settings.analysis.get("max_pages_to_scan", 60))
    min_chars = int(pdf_config.get("min_extractable_chars_per_page", 60))

    result = extract_with_pymupdf(path, max_pages=max_pages, min_chars=min_chars)
    if result is None or not result.pages:
        fallback = extract_with_pypdf(path, max_pages=max_pages, min_chars=min_chars)
        if fallback is not None and fallback.pages:
            LOG.info(f"Used the pypdf fallback for {path.name}.")
            result = fallback
    if result is None:
        result = ExtractionResult(
            record_id=record_id,
            pdf_path=str(path),
            error="No PDF library could open the file.",
        )
        return result

    result.record_id = record_id

    if result.requires_ocr and bool(pdf_config.get("ocr_enabled", False)):
        result = apply_ocr(result, path, min_chars=min_chars)

    if result.requires_ocr:
        LOG.warning(
            f"{path.name} has no extractable text layer on any scanned page. It is "
            "flagged as requiring OCR and will be excluded from evidence-based claims."
        )

    if output_dir is not None:
        stem = safe_filename_stem(output_name or path.stem)
        text_path = ensure_dir(Path(output_dir)) / f"{stem}.txt"
        text_path.write_text(result.full_text(), encoding="utf-8")
        result.text_path = str(text_path)

    return result


def extract_records(
    records: list[PaperRecord],
    settings: Settings,
    output_dir: Path,
    *,
    already_done: set[str] | None = None,
    on_complete=None,
) -> dict[str, ExtractionResult]:
    """Extract text for every downloaded paper, resuming where it left off."""
    already_done = already_done or set()
    output_dir = ensure_dir(Path(output_dir))
    results: dict[str, ExtractionResult] = {}

    downloadable = [r for r in records if r.local_path and Path(r.local_path).exists()]
    for index, record in enumerate(downloadable, 1):
        if record.record_id in already_done and record.extracted_text_path:
            existing = Path(record.extracted_text_path)
            if existing.exists():
                LOG.debug(f"Text already extracted for {record.record_id}.")
                continue

        LOG.info(f"[{index}/{len(downloadable)}] Extracting: {record.title[:70]}")
        result = extract_pdf(
            Path(record.local_path),
            settings,
            record_id=record.record_id,
            output_dir=output_dir,
            output_name=Path(record.local_path).stem,
        )
        results[record.record_id] = result

        record.extracted_text_path = result.text_path
        record.extracted_pages = result.page_count
        record.extracted_characters = result.total_characters
        record.requires_ocr = result.requires_ocr
        if result.error:
            record.notes = " ".join(
                filter(None, [record.notes, f"Text extraction problem: {result.error}"])
            )
        if result.requires_ocr:
            record.notes = " ".join(
                filter(
                    None,
                    [
                        record.notes,
                        "No text layer: this paper needs OCR before it can support "
                        "evidence-based claims.",
                    ],
                )
            )
        if on_complete is not None:
            on_complete(record.record_id)

    readable = sum(1 for r in results.values() if r.success)
    LOG.info(
        f"Extraction: {readable} of {len(results)} PDFs produced readable text "
        f"({sum(1 for r in results.values() if r.requires_ocr)} need OCR)."
    )
    return results


# ---------------------------------------------------------------------------
# Reading extracted text back
# ---------------------------------------------------------------------------


def load_pages(text_path: Path) -> list[PageText]:
    """Parse a page-marked ``.txt`` file back into pages.

    This is how the analysis and verification agents work from saved evidence
    without needing the original PDF or any network access.
    """
    text_path = Path(text_path)
    if not text_path.exists():
        return []

    content = text_path.read_text(encoding="utf-8", errors="replace")
    matches = list(PAGE_MARKER_RE.finditer(content))
    if not matches:
        # No markers: treat the whole file as a single page rather than failing.
        return [PageText(number=1, text=content.strip())]

    pages: list[PageText] = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
        body = content[start:end].strip()
        readable = body != UNREADABLE_NOTE and bool(body)
        pages.append(
            PageText(number=int(match.group(1)), text="" if not readable else body, readable=readable)
        )
    return pages


def pages_to_text(pages: list[PageText]) -> str:
    """Join pages into plain text without the markers."""
    return "\n\n".join(p.text for p in pages if p.readable and p.text)
