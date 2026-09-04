"""Topic slugs, Windows-safe filenames, and collision resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from literature_review_agent.downloader import target_filename
from literature_review_agent.schemas import PaperRecord
from literature_review_agent.utils import (
    MAX_FILENAME_STEM,
    WINDOWS_INVALID_CHARS,
    disambiguated_stem,
    resolve_collision,
    safe_filename_stem,
    slugify,
    truncate_text,
)


class TestSlugify:
    """Topic slug generation."""

    def test_basic_topic(self) -> None:
        assert slugify("Effect of Rainfall on Urban Travel Behaviour") == (
            "effect-rainfall-urban-travel-behaviour"
        )

    def test_strips_punctuation_and_accents(self) -> None:
        assert slugify("Café Résumé: A Study (2021)!") == "cafe-resume-study-2021"

    def test_drops_question_words(self) -> None:
        # Question words carry no discriminating information in a folder name.
        assert "does" not in slugify("How does rainfall affect travel?")
        assert "how" not in slugify("How does rainfall affect travel?")

    def test_respects_max_length(self) -> None:
        slug = slugify("word " * 100, max_length=30)
        assert len(slug) <= 30

    def test_never_empty(self) -> None:
        # Even input with no usable characters must produce a stable slug.
        slug = slugify("!!!???---")
        assert slug
        assert slug == slugify("!!!???---"), "slug must be deterministic"

    def test_all_stopwords_still_yields_a_slug(self) -> None:
        assert slugify("the and of for") == "the-and-of-for"

    def test_is_filesystem_safe(self) -> None:
        slug = slugify('A/B\\C:D*E?F"G<H>I|J')
        assert not any(ch in slug for ch in WINDOWS_INVALID_CHARS)


class TestSafeFilenameStem:
    """Cross-platform-safe filename stems built from paper titles."""

    def test_keeps_a_clean_title_intact(self) -> None:
        title = "Rainfall Intensity and Mode Choice in Delhi"
        assert safe_filename_stem(title) == title

    @pytest.mark.parametrize("char", list(WINDOWS_INVALID_CHARS))
    def test_removes_every_windows_invalid_character(self, char: str) -> None:
        stem = safe_filename_stem(f"Rainfall{char}Study")
        assert char not in stem
        assert stem, "a stem must survive character removal"

    def test_converts_colon_readably(self) -> None:
        # A subtitle colon is common in paper titles and must stay readable.
        stem = safe_filename_stem("Rainfall: A Delhi Case Study")
        assert ":" not in stem
        assert "Rainfall" in stem and "Delhi" in stem

    def test_replaces_path_separators(self) -> None:
        stem = safe_filename_stem("Travel/Transport\\Mobility")
        assert "/" not in stem and "\\" not in stem

    @pytest.mark.parametrize("reserved", ["CON", "PRN", "AUX", "NUL", "COM1", "LPT9"])
    def test_handles_windows_reserved_names(self, reserved: str) -> None:
        stem = safe_filename_stem(reserved)
        assert stem.split(".")[0].upper() != reserved
        assert reserved in stem, "the original word should still be recognisable"

    def test_reserved_name_with_extension_like_title(self) -> None:
        assert safe_filename_stem("NUL.pdf").split(".")[0].upper() != "NUL"

    def test_strips_control_characters(self) -> None:
        stem = safe_filename_stem("Rain\x00fall\x07 Study\x1f")
        assert all(ord(c) >= 32 for c in stem)

    def test_strips_trailing_dots_and_spaces(self) -> None:
        # Windows silently drops these, which would break manifest lookups.
        stem = safe_filename_stem("Rainfall Study...   ")
        assert not stem.endswith((".", " "))

    def test_collapses_whitespace(self) -> None:
        assert safe_filename_stem("Rainfall    and\n\nMode\tChoice") == (
            "Rainfall and Mode Choice"
        )

    def test_truncates_long_titles(self) -> None:
        stem = safe_filename_stem("A very long title " * 40)
        assert len(stem) <= MAX_FILENAME_STEM

    def test_truncation_keeps_meaning(self) -> None:
        title = (
            "Rainfall intensity and its influence on urban mode choice across "
            "metropolitan India during monsoon months with policy implications"
        )
        stem = safe_filename_stem(title, max_length=60)
        assert stem.startswith("Rainfall intensity")
        assert len(stem) <= 60

    def test_empty_title_yields_placeholder(self) -> None:
        assert safe_filename_stem("") == "Untitled Paper"
        assert safe_filename_stem("   ") == "Untitled Paper"

    def test_none_title_yields_placeholder(self) -> None:
        assert safe_filename_stem(None) == "Untitled Paper"

    def test_is_deterministic(self) -> None:
        title = "Rainfall: Mode/Choice <Study>"
        assert safe_filename_stem(title) == safe_filename_stem(title)


class TestDisambiguatedStem:
    """The ``Title - FirstAuthor - Year`` fallback."""

    def test_includes_author_and_year(self) -> None:
        stem = disambiguated_stem("Rainfall Study", "Sharma", 2021)
        assert stem == "Rainfall Study - Sharma - 2021"

    def test_handles_missing_author(self) -> None:
        assert "Unknown Author" in disambiguated_stem("Rainfall Study", None, 2021)

    def test_handles_missing_year(self) -> None:
        # The trailing dot of "n.d." is deliberately stripped: Windows removes
        # trailing dots itself, so keeping it would make the recorded filename
        # differ from the one actually on disk.
        assert disambiguated_stem("Rainfall Study", "Sharma", None) == (
            "Rainfall Study - Sharma - n.d"
        )

    def test_stays_within_length_limit(self) -> None:
        stem = disambiguated_stem("Long title " * 50, "Sharma", 2021)
        assert len(stem) <= MAX_FILENAME_STEM


class TestResolveCollision:
    """Deterministic numeric suffixes when a name is taken."""

    def test_returns_plain_name_when_free(self, tmp_path: Path) -> None:
        assert resolve_collision(tmp_path, "Paper", ".pdf").name == "Paper.pdf"

    def test_adds_numeric_suffix(self, tmp_path: Path) -> None:
        (tmp_path / "Paper.pdf").write_bytes(b"x")
        assert resolve_collision(tmp_path, "Paper", ".pdf").name == "Paper (2).pdf"

    def test_increments_past_multiple_collisions(self, tmp_path: Path) -> None:
        (tmp_path / "Paper.pdf").write_bytes(b"x")
        (tmp_path / "Paper (2).pdf").write_bytes(b"x")
        assert resolve_collision(tmp_path, "Paper", ".pdf").name == "Paper (3).pdf"

    def test_honours_reserved_names(self, tmp_path: Path) -> None:
        # A name planned but not yet written must still be avoided.
        result = resolve_collision(tmp_path, "Paper", ".pdf", taken={"Paper.pdf"})
        assert result.name == "Paper (2).pdf"

    def test_reserved_matching_is_case_insensitive(self, tmp_path: Path) -> None:
        result = resolve_collision(tmp_path, "Paper", ".pdf", taken={"paper.pdf"})
        assert result.name == "Paper (2).pdf"

    def test_is_deterministic(self, tmp_path: Path) -> None:
        (tmp_path / "Paper.pdf").write_bytes(b"x")
        first = resolve_collision(tmp_path, "Paper", ".pdf")
        second = resolve_collision(tmp_path, "Paper", ".pdf")
        assert first == second

    def test_normalises_extension(self, tmp_path: Path) -> None:
        assert resolve_collision(tmp_path, "Paper", "pdf").suffix == ".pdf"


class TestTargetFilename:
    """The downloader's filename preference order."""

    def test_prefers_the_bare_title(self, tmp_path: Path) -> None:
        record = PaperRecord(title="Rainfall and Mode Choice", authors=["Sharma, R"], year=2021)
        assert target_filename(record, tmp_path).name == "Rainfall and Mode Choice.pdf"

    def test_falls_back_to_author_and_year(self, tmp_path: Path) -> None:
        (tmp_path / "Rainfall and Mode Choice.pdf").write_bytes(b"x")
        record = PaperRecord(title="Rainfall and Mode Choice", authors=["Sharma, R"], year=2021)
        assert target_filename(record, tmp_path).name == (
            "Rainfall and Mode Choice - Sharma - 2021.pdf"
        )

    def test_falls_back_to_numeric_suffix(self, tmp_path: Path) -> None:
        # Same title, same first author, same year: only a counter can separate them.
        (tmp_path / "Rainfall and Mode Choice.pdf").write_bytes(b"x")
        (tmp_path / "Rainfall and Mode Choice - Sharma - 2021.pdf").write_bytes(b"x")
        record = PaperRecord(title="Rainfall and Mode Choice", authors=["Sharma, R"], year=2021)
        assert target_filename(record, tmp_path).name == "Rainfall and Mode Choice (2).pdf"

    def test_two_papers_sharing_a_title_get_distinct_names(self, tmp_path: Path) -> None:
        first = PaperRecord(title="Weather and Travel", authors=["Sharma, R"], year=2021)
        second = PaperRecord(title="Weather and Travel", authors=["Iyer, A"], year=2019)
        reserved: set[str] = set()
        first_path = target_filename(first, tmp_path, reserved=reserved)
        reserved.add(first_path.name)
        second_path = target_filename(second, tmp_path, reserved=reserved)
        assert first_path.name != second_path.name
        assert "Iyer" in second_path.name

    def test_unsafe_title_produces_a_safe_path(self, tmp_path: Path) -> None:
        record = PaperRecord(title='Rain/fall: "Mode" <Choice>?', authors=["Sharma, R"], year=2021)
        path = target_filename(record, tmp_path)
        assert not any(ch in path.name for ch in WINDOWS_INVALID_CHARS)
        assert path.suffix == ".pdf"


class TestTruncateText:
    """Text truncation used across filenames and report cells."""

    def test_leaves_short_text_alone(self) -> None:
        assert truncate_text("short", 20) == "short"

    def test_truncates_on_a_word_boundary(self) -> None:
        result = truncate_text("one two three four five", 14)
        assert result.endswith("...")
        assert "thre" not in result.replace("three", "")

    def test_handles_none(self) -> None:
        assert truncate_text(None, 10) == ""
