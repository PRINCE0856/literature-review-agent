"""PDF validation, legal URL screening, and text extraction."""

from __future__ import annotations

from pathlib import Path

import pytest

from literature_review_agent.config import Settings
from literature_review_agent.downloader import candidate_urls, screen_url
from literature_review_agent.analysis import build_sentences, detect_section
from literature_review_agent.pdf_extractor import (
    PAGE_MARKER,
    UNREADABLE_NOTE,
    extract_pdf,
    load_pages,
)
from literature_review_agent.pdf_validator import (
    content_type_acceptable,
    looks_like_html,
    looks_like_pdf,
    title_plausibly_matches,
    validate_pdf,
    verify_extracted_text_matches,
)
from literature_review_agent.schemas import PaperRecord


class TestByteSignatures:
    """Low-level byte checks."""

    def test_pdf_signature_recognised(self) -> None:
        assert looks_like_pdf(b"%PDF-1.7\n...")

    def test_leading_whitespace_tolerated(self) -> None:
        # Some servers prepend a newline; the file is still a valid PDF.
        assert looks_like_pdf(b"\r\n  %PDF-1.4")

    def test_html_not_mistaken_for_pdf(self) -> None:
        assert not looks_like_pdf(b"<!DOCTYPE html><html>")

    def test_empty_bytes_are_not_a_pdf(self) -> None:
        assert not looks_like_pdf(b"")

    @pytest.mark.parametrize(
        "body",
        [
            b"<!DOCTYPE html><html><body>Login</body></html>",
            b"<html><head><title>Error</title></head></html>",
            b"<?xml version='1.0'?><root/>",
            b"   <HTML><BODY>Access denied</BODY></HTML>",
        ],
    )
    def test_html_variants_detected(self, body: bytes) -> None:
        assert looks_like_html(body)

    def test_pdf_bytes_are_not_html(self) -> None:
        assert not looks_like_html(b"%PDF-1.7\nstream\nbinary")


class TestContentType:
    """Content-type screening."""

    @pytest.mark.parametrize(
        "value",
        ["application/pdf", "application/pdf; charset=utf-8", "application/octet-stream",
         "APPLICATION/PDF", None],
    )
    def test_acceptable_types(self, value: str | None) -> None:
        assert content_type_acceptable(value)

    @pytest.mark.parametrize("value", ["text/html", "text/html; charset=utf-8", "image/png"])
    def test_unacceptable_types(self, value: str) -> None:
        assert not content_type_acceptable(value)


class TestValidatePdf:
    """The gatekeeper: only genuine, readable PDFs may pass."""

    def test_accepts_a_real_pdf(self, real_pdf: Path) -> None:
        result = validate_pdf(real_pdf)
        assert result.valid, result.reason
        assert result.page_count == 5
        assert result.sha256 and len(result.sha256) == 64
        assert not result.is_html
        assert not result.requires_ocr

    def test_rejects_html_disguised_as_pdf(self, html_disguised_as_pdf: Path) -> None:
        # This is the single most important rejection: a login page saved as
        # .pdf would otherwise flow into the analysis and corrupt the review.
        result = validate_pdf(html_disguised_as_pdf)
        assert not result.valid
        assert result.is_html
        assert "HTML" in result.reason

    def test_rejects_zero_byte_file(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.pdf"
        path.write_bytes(b"")
        result = validate_pdf(path)
        assert not result.valid
        assert "zero bytes" in result.reason

    def test_rejects_tiny_file(self, tmp_path: Path) -> None:
        path = tmp_path / "tiny.pdf"
        path.write_bytes(b"%PDF-1.4\n" + b"x" * 50)
        result = validate_pdf(path)
        assert not result.valid
        assert "minimum" in result.reason

    def test_rejects_missing_file(self, tmp_path: Path) -> None:
        result = validate_pdf(tmp_path / "nope.pdf")
        assert not result.valid
        assert "does not exist" in result.reason

    def test_rejects_corrupt_pdf(self, tmp_path: Path) -> None:
        path = tmp_path / "corrupt.pdf"
        path.write_bytes(b"%PDF-1.4\n" + b"\x00garbage" * 300)
        result = validate_pdf(path)
        assert not result.valid

    def test_flags_scanned_pdf_for_ocr_without_rejecting_it(self, scanned_pdf: Path) -> None:
        # A scanned PDF is a valid file that needs OCR, not a failed download.
        result = validate_pdf(scanned_pdf, min_bytes=100)
        assert result.valid, result.reason
        assert result.requires_ocr
        assert "OCR" in result.reason

    def test_records_a_checksum(self, real_pdf: Path) -> None:
        first = validate_pdf(real_pdf)
        second = validate_pdf(real_pdf)
        assert first.sha256 == second.sha256

    def test_checksum_can_be_skipped(self, real_pdf: Path) -> None:
        assert validate_pdf(real_pdf, compute_checksum=False).sha256 is None

    def test_confirms_the_expected_title(self, real_pdf: Path) -> None:
        result = validate_pdf(
            real_pdf, expected_title="Rainfall Intensity and Mode Choice in Delhi"
        )
        assert result.valid
        assert any("content matches" in c for c in result.checks_passed)

    def test_warns_when_the_pdf_is_a_different_paper(self, real_pdf: Path) -> None:
        result = validate_pdf(
            real_pdf, expected_title="Groundwater Recharge in Arid Basins of Rajasthan"
        )
        # Still a valid PDF, but the content mismatch must be surfaced.
        assert result.valid
        assert any("content matches" in c for c in result.checks_failed)

    def test_records_every_check(self, real_pdf: Path) -> None:
        result = validate_pdf(real_pdf)
        assert len(result.checks_passed) >= 6


class TestTitleMatching:
    """Confirming a file belongs to its intended paper."""

    def test_matching_text_passes(self) -> None:
        assert title_plausibly_matches(
            "Rainfall Intensity and Mode Choice",
            "This paper on rainfall intensity and mode choice in Delhi...",
        )

    def test_unrelated_text_fails(self) -> None:
        assert not title_plausibly_matches(
            "Rainfall Intensity and Mode Choice",
            "Groundwater recharge in arid basins depends on soil permeability.",
        )

    def test_empty_title_passes_vacuously(self) -> None:
        assert title_plausibly_matches("", "any text")

    def test_empty_text_fails(self) -> None:
        assert not title_plausibly_matches("Rainfall Study", "")

    def test_extracted_text_pairing_check(self) -> None:
        assert verify_extracted_text_matches(
            "Rainfall Intensity Mode Choice", "rainfall intensity and mode choice study"
        )
        assert not verify_extracted_text_matches(
            "Rainfall Intensity Mode Choice", "an essay about medieval pottery kilns"
        )


class TestScreenUrl:
    """Legal boundaries, enforced rather than documented."""

    def test_blocked_host_refused(self, settings: Settings) -> None:
        record = PaperRecord(title="X")
        allowed, reason = screen_url("https://sci-hub.se/10.1016/j.tra.2021.01.001",
                                     record, settings)
        assert not allowed
        assert "blocked" in reason.lower()

    def test_google_scholar_refused(self, settings: Settings) -> None:
        record = PaperRecord(title="X")
        allowed, _ = screen_url("https://scholar.google.com/citations?x=1", record, settings)
        assert not allowed

    @pytest.mark.parametrize(
        "url",
        [
            "https://example.org/login?next=/paper.pdf",
            "https://example.org/signin",
            "https://idp.example.org/shibboleth/sso",
            "https://example.org/action/cookieAbsent",
            "https://example.org/purchase/article",
            "https://example.org/captcha-check",
        ],
    )
    def test_authentication_and_purchase_flows_refused(
        self, url: str, settings: Settings
    ) -> None:
        record = PaperRecord(title="X")
        allowed, reason = screen_url(url, record, settings)
        assert not allowed
        assert "authentication" in reason or "purchase" in reason

    def test_subscription_publisher_refused_when_closed(self, settings: Settings) -> None:
        record = PaperRecord(title="X", open_access_status="closed")
        allowed, reason = screen_url(
            "https://www.sciencedirect.com/science/article/pii/S0965856421000010/pdf",
            record, settings,
        )
        assert not allowed
        assert "institutional access" in reason

    def test_subscription_publisher_allowed_when_open_access(self, settings: Settings) -> None:
        # An API-supplied URL for a genuinely open-access article is authorised.
        record = PaperRecord(title="X", open_access_status="gold")
        allowed, reason = screen_url(
            "https://www.sciencedirect.com/science/article/pii/S0965856421000010/pdf",
            record, settings,
        )
        assert allowed
        assert "open access" in reason

    def test_publisher_refused_when_status_unknown(self, settings: Settings) -> None:
        record = PaperRecord(title="X", open_access_status=None)
        allowed, _ = screen_url("https://onlinelibrary.wiley.com/doi/pdf/10.1002/x",
                                record, settings)
        assert not allowed

    @pytest.mark.parametrize(
        "url",
        [
            "https://arxiv.org/pdf/2101.00001.pdf",
            "https://europepmc.org/articles/PMC123/pdf",
            "https://www.mdpi.com/1234/5/6/pdf",
            "https://journals.plos.org/plosone/article/file?id=1&type=printable",
        ],
    )
    def test_open_access_hosts_allowed(self, url: str, settings: Settings) -> None:
        record = PaperRecord(title="X")
        allowed, _ = screen_url(url, record, settings)
        assert allowed

    def test_non_http_scheme_refused(self, settings: Settings) -> None:
        record = PaperRecord(title="X")
        assert not screen_url("ftp://example.org/paper.pdf", record, settings)[0]
        assert not screen_url("file:///etc/passwd", record, settings)[0]

    def test_empty_url_refused(self, settings: Settings) -> None:
        assert not screen_url("", PaperRecord(title="X"), settings)[0]

    def test_unknown_host_allowed_but_validated(self, settings: Settings) -> None:
        record = PaperRecord(title="X")
        allowed, reason = screen_url("https://repository.example.edu/1.pdf", record, settings)
        assert allowed
        assert "validating the bytes" in reason


class TestCandidateUrls:
    """URL selection records why anything was refused."""

    def test_refused_urls_are_recorded_as_attempts(self, settings: Settings) -> None:
        record = PaperRecord(
            title="X",
            candidate_pdf_urls=["https://sci-hub.se/x.pdf", "https://arxiv.org/pdf/1.pdf"],
        )
        allowed = candidate_urls(record, settings)
        assert len(allowed) == 1
        assert "arxiv.org" in allowed[0][0]
        # The refusal must be auditable, not silent.
        assert any("not eligible" in (a.error or "") for a in record.download_attempts)

    def test_deduplicates_urls(self, settings: Settings) -> None:
        record = PaperRecord(
            title="X",
            pdf_url="https://arxiv.org/pdf/1.pdf",
            candidate_pdf_urls=["https://arxiv.org/pdf/1.pdf"],
        )
        assert len(candidate_urls(record, settings)) == 1

    def test_no_urls_yields_empty(self, settings: Settings) -> None:
        assert candidate_urls(PaperRecord(title="X"), settings) == []


class TestSectionDetection:
    """Section headings drive where each field is looked for."""

    @pytest.mark.parametrize(
        "line,expected",
        [
            ("Abstract", "abstract"),
            ("1. Introduction", "introduction"),
            ("2. Methodology", "methods"),
            ("3 Results", "results"),
            ("4. Conclusions", "conclusion"),
            ("References", "references"),
            ("IV. Discussion", "discussion"),
            ("Limitations", "limitations"),
        ],
    )
    def test_headings_recognised(self, line: str, expected: str) -> None:
        assert detect_section(line) == expected

    @pytest.mark.parametrize(
        "line",
        [
            "This is an ordinary sentence of body text that runs on for a while.",
            "",
            "The introduction of new policy measures was widely debated among planners.",
        ],
    )
    def test_body_text_is_not_a_heading(self, line: str) -> None:
        assert detect_section(line) is None


class TestExtraction:
    """Extraction preserves page boundaries and never invents text."""

    def test_extracts_with_page_numbers(self, real_pdf: Path, settings: Settings) -> None:
        result = extract_pdf(real_pdf, settings, record_id="r1")
        assert result.success
        assert result.page_count == 5
        assert [p.number for p in result.pages] == [1, 2, 3, 4, 5]
        assert all(p.readable for p in result.pages)

    def test_writes_a_page_marked_text_file(
        self, real_pdf: Path, settings: Settings, tmp_path: Path
    ) -> None:
        result = extract_pdf(real_pdf, settings, record_id="r1", output_dir=tmp_path)
        assert result.text_path
        content = Path(result.text_path).read_text(encoding="utf-8")
        assert PAGE_MARKER.format(number=1) in content
        assert PAGE_MARKER.format(number=5) in content

    def test_round_trips_through_load_pages(
        self, real_pdf: Path, settings: Settings, tmp_path: Path
    ) -> None:
        result = extract_pdf(real_pdf, settings, record_id="r1", output_dir=tmp_path)
        pages = load_pages(Path(result.text_path))
        assert len(pages) == 5
        assert pages[0].number == 1
        assert "rainfall" in pages[0].text.lower()

    def test_scanned_pdf_is_flagged_not_fabricated(
        self, scanned_pdf: Path, settings: Settings, tmp_path: Path
    ) -> None:
        result = extract_pdf(scanned_pdf, settings, record_id="r2", output_dir=tmp_path)
        assert result.requires_ocr
        content = Path(result.text_path).read_text(encoding="utf-8")
        assert UNREADABLE_NOTE in content, "an unreadable page must say so explicitly"

    def test_unreadable_pages_are_marked_on_reload(
        self, scanned_pdf: Path, settings: Settings, tmp_path: Path
    ) -> None:
        result = extract_pdf(scanned_pdf, settings, record_id="r2", output_dir=tmp_path)
        pages = load_pages(Path(result.text_path))
        assert pages and not pages[0].readable

    def test_missing_file_reports_an_error(self, settings: Settings, tmp_path: Path) -> None:
        result = extract_pdf(tmp_path / "nope.pdf", settings, record_id="r3")
        assert not result.success
        assert result.error

    def test_load_pages_of_missing_file_is_empty(self, tmp_path: Path) -> None:
        assert load_pages(tmp_path / "nope.txt") == []

    def test_reference_list_is_excluded_from_sentences(
        self, real_pdf: Path, settings: Settings
    ) -> None:
        # The fixture's reference list mentions groundwater, which must not
        # become evidence about this rainfall paper.
        result = extract_pdf(real_pdf, settings, record_id="r1")
        sentences = build_sentences(result.pages)
        joined = " ".join(s.text for s in sentences).lower()
        assert "rainfall" in joined
        assert "groundwater recharge in arid basins" not in joined
