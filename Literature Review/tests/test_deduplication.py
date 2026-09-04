"""DOI normalisation, metadata merging, and the deduplication cascade."""

from __future__ import annotations

import pytest

from literature_review_agent.config import Settings
from literature_review_agent.deduplicator import (
    authors_agree,
    deduplicate,
    shared_identifier,
    title_similarity,
    years_agree,
)
from literature_review_agent.metadata import (
    choose_primary,
    completeness_score,
    merge_records,
    normalise_record,
    score_relevance,
)
from literature_review_agent.schemas import PaperRecord
from literature_review_agent.utils import normalize_doi, normalize_issn, normalize_title


class TestNormalizeDoi:
    """DOI normalisation must be strict: a bad DOI must never key a merge."""

    @pytest.mark.parametrize(
        "raw",
        [
            "10.1016/j.tra.2021.01.001",
            "https://doi.org/10.1016/j.tra.2021.01.001",
            "http://dx.doi.org/10.1016/j.tra.2021.01.001",
            "doi:10.1016/j.tra.2021.01.001",
            "DOI: 10.1016/j.tra.2021.01.001",
            "  10.1016/j.tra.2021.01.001  ",
            "10.1016/J.TRA.2021.01.001",
            "10.1016/j.tra.2021.01.001.",
        ],
    )
    def test_all_forms_normalise_identically(self, raw: str) -> None:
        assert normalize_doi(raw) == "10.1016/j.tra.2021.01.001"

    @pytest.mark.parametrize(
        "raw",
        ["", None, "not-a-doi", "11.1016/x", "10.1016", "10.x/abc", "https://example.org/paper"],
    )
    def test_invalid_input_returns_none(self, raw: str | None) -> None:
        assert normalize_doi(raw) is None

    def test_preserves_suffix_case_insensitively(self) -> None:
        # DOI suffixes are case-insensitive in practice; lowercasing makes the
        # deduplication key stable.
        assert normalize_doi("10.1234/AbC") == normalize_doi("10.1234/abc")


class TestNormalizeIssn:
    """ISSN normalisation used for journal-ranking matches."""

    @pytest.mark.parametrize("raw", ["0965-8564", "09658564", "0965 8564", "0965-8564 "])
    def test_forms_normalise_identically(self, raw: str) -> None:
        assert normalize_issn(raw) == "0965-8564"

    def test_preserves_check_digit_x(self) -> None:
        assert normalize_issn("2049-363X") == "2049-363X"

    @pytest.mark.parametrize("raw", ["", None, "123", "not-an-issn"])
    def test_invalid_returns_none(self, raw: str | None) -> None:
        assert normalize_issn(raw) is None


class TestNormalizeTitle:
    """Title normalisation for exact-match deduplication."""

    def test_case_and_punctuation_insensitive(self) -> None:
        assert normalize_title("Rainfall: A Study!") == normalize_title("rainfall a study")

    def test_expands_ampersand(self) -> None:
        assert normalize_title("Rain & Travel") == "rain and travel"

    def test_strips_accents(self) -> None:
        assert normalize_title("Café Study") == "cafe study"

    def test_collapses_whitespace(self) -> None:
        assert normalize_title("Rain   fall\n\nstudy") == "rain fall study"

    def test_handles_none(self) -> None:
        assert normalize_title(None) == ""


class TestTitleSimilarity:
    """Fuzzy title comparison must not match unrelated papers."""

    def test_identical_titles_score_full(self) -> None:
        assert title_similarity("Rainfall and mode choice", "Rainfall and mode choice") == 100

    def test_subtitle_difference_still_matches(self) -> None:
        score = title_similarity(
            "Rainfall and mode choice in Delhi",
            "Rainfall and mode choice in Delhi: a mixed logit approach",
        )
        assert score >= 87

    def test_case_and_punctuation_ignored(self) -> None:
        assert title_similarity("Rainfall, Mode Choice!", "rainfall mode choice") >= 95

    def test_unrelated_titles_score_low(self) -> None:
        score = title_similarity(
            "Rainfall and mode choice in Delhi",
            "Groundwater recharge in arid basins of Rajasthan",
        )
        assert score < 87, f"unrelated titles must not reach the merge threshold (got {score})"

    def test_shared_common_words_do_not_force_a_match(self) -> None:
        # These share "urban" and "study" but describe different research.
        score = title_similarity(
            "Urban travel study of rainfall effects",
            "Urban water study of groundwater depletion",
        )
        assert score < 87

    def test_empty_titles_score_zero(self) -> None:
        assert title_similarity("", "Rainfall") == 0.0


class TestAuthorAndYearAgreement:
    """Corroborating signals for a fuzzy merge."""

    def test_same_first_author_agrees(self) -> None:
        left = PaperRecord(title="A", authors=["Sharma, Ravi"])
        right = PaperRecord(title="A", authors=["Sharma, R."])
        assert authors_agree(left, right)

    def test_shared_later_author_agrees(self) -> None:
        left = PaperRecord(title="A", authors=["Sharma, Ravi", "Iyer, Anita"])
        right = PaperRecord(title="A", authors=["Iyer, Anita"])
        assert authors_agree(left, right)

    def test_different_authors_disagree(self) -> None:
        left = PaperRecord(title="A", authors=["Sharma, Ravi"])
        right = PaperRecord(title="A", authors=["Zhang, Wei"])
        assert not authors_agree(left, right)

    def test_missing_authors_is_not_a_match(self) -> None:
        # Absent data must count as "no disagreement", never as evidence.
        left = PaperRecord(title="A", authors=[])
        right = PaperRecord(title="A", authors=["Sharma, Ravi"])
        assert authors_agree(left, right)

    def test_year_tolerance_allows_online_first(self) -> None:
        left = PaperRecord(title="A", year=2021)
        right = PaperRecord(title="A", year=2022)
        assert years_agree(left, right, 1)

    def test_year_beyond_tolerance_disagrees(self) -> None:
        left = PaperRecord(title="A", year=2015)
        right = PaperRecord(title="A", year=2021)
        assert not years_agree(left, right, 1)


class TestSharedIdentifier:
    """Rule 4 of the cascade: other strong identifiers."""

    def test_matching_pmid(self) -> None:
        left = PaperRecord(title="A", external_ids={"pmid": "12345"})
        right = PaperRecord(title="B", external_ids={"pmid": "12345"})
        assert shared_identifier(left, right) == "pmid"

    def test_no_match_returns_none(self) -> None:
        left = PaperRecord(title="A", external_ids={"pmid": "12345"})
        right = PaperRecord(title="B", external_ids={"pmid": "99999"})
        assert shared_identifier(left, right) is None

    def test_empty_identifiers_do_not_match(self) -> None:
        left = PaperRecord(title="A", external_ids={"pmid": ""})
        right = PaperRecord(title="B", external_ids={"pmid": ""})
        assert shared_identifier(left, right) is None


class TestCompletenessAndMerge:
    """Metadata merging must be additive and audited."""

    def test_richer_record_scores_higher(self) -> None:
        thin = PaperRecord(title="A", doi="10.1234/abc")
        rich = PaperRecord(
            title="A",
            doi="10.1234/abc",
            authors=["Sharma, R", "Iyer, A"],
            year=2021,
            journal="TR-A",
            abstract="x" * 900,
            issn="0965-8564",
            publisher="Elsevier",
        )
        assert completeness_score(rich) > completeness_score(thin)

    def test_choose_primary_prefers_the_richer_record(self) -> None:
        thin = PaperRecord(title="A", discovery_source="arXiv")
        rich = PaperRecord(
            title="A", authors=["Sharma, R"], year=2021, journal="TR-A",
            doi="10.1234/abc", discovery_source="Crossref",
        )
        primary, secondary = choose_primary(thin, rich)
        assert primary is rich and secondary is thin

    def test_merge_fills_missing_fields(self) -> None:
        primary = PaperRecord(title="A", doi="10.1234/abc", discovery_source="Crossref")
        secondary = PaperRecord(
            title="A", journal="TR-A", year=2021, publisher="Elsevier",
            discovery_source="OpenAlex",
        )
        merged = merge_records(primary, secondary)
        assert merged.journal == "TR-A"
        assert merged.year == 2021
        assert merged.publisher == "Elsevier"

    def test_merge_records_an_audit_trail(self) -> None:
        primary = PaperRecord(title="A", doi="10.1234/abc", discovery_source="Crossref")
        secondary = PaperRecord(title="A", journal="TR-A", discovery_source="OpenAlex")
        merged = merge_records(primary, secondary)
        assert merged.merge_audit, "every merge must leave an audit trail"
        assert any("journal" in entry for entry in merged.merge_audit)

    def test_merge_prefers_the_longer_abstract(self) -> None:
        primary = PaperRecord(title="A", abstract="short", discovery_source="Crossref")
        secondary = PaperRecord(title="A", abstract="a much longer abstract " * 10,
                                discovery_source="OpenAlex")
        merged = merge_records(primary, secondary)
        assert len(merged.abstract) > 50

    def test_merge_keeps_the_higher_citation_count(self) -> None:
        primary = PaperRecord(title="A", citation_count=10, discovery_source="Crossref")
        secondary = PaperRecord(title="A", citation_count=42, discovery_source="OpenAlex")
        assert merge_records(primary, secondary).citation_count == 42

    def test_merge_unions_pdf_urls_and_sources(self) -> None:
        primary = PaperRecord(
            title="A", candidate_pdf_urls=["https://a.org/1.pdf"], discovery_source="Crossref"
        )
        secondary = PaperRecord(
            title="A", candidate_pdf_urls=["https://b.org/2.pdf"], discovery_source="OpenAlex"
        )
        merged = merge_records(primary, secondary)
        assert len(merged.candidate_pdf_urls) == 2
        assert "OpenAlex" in merged.metadata_sources

    def test_merge_does_not_lose_the_absorbed_record_id(self) -> None:
        primary = PaperRecord(record_id="p1", title="A", discovery_source="Crossref")
        secondary = PaperRecord(record_id="s1", title="A", discovery_source="OpenAlex")
        assert "s1" in merge_records(primary, secondary).merged_from


class TestDeduplicate:
    """The four-stage cascade, in order."""

    def test_rule_1_merges_on_doi(self, settings: Settings) -> None:
        records = [
            PaperRecord(title="Rainfall study", doi="10.1016/j.tra.2021.01.001",
                        discovery_source="Crossref"),
            PaperRecord(title="A completely different rendering of the title",
                        doi="10.1016/j.tra.2021.01.001", discovery_source="OpenAlex"),
        ]
        result = deduplicate(records, settings)
        assert len(result.records) == 1
        assert result.merges[0].rule == "normalised DOI"

    def test_rule_2_merges_on_exact_normalised_title(self, settings: Settings) -> None:
        records = [
            PaperRecord(title="Rainfall and Mode Choice!", year=2021, discovery_source="Crossref"),
            PaperRecord(title="rainfall and mode choice", year=2021, discovery_source="OpenAlex"),
        ]
        result = deduplicate(records, settings)
        assert len(result.records) == 1
        assert result.merges[0].rule == "exact normalised title"

    def test_rule_3_merges_on_fuzzy_title_with_author_and_year(self, settings: Settings) -> None:
        records = [
            PaperRecord(
                title="Rainfall intensity and mode choice in Delhi",
                authors=["Sharma, Ravi"], year=2021, discovery_source="Crossref",
            ),
            PaperRecord(
                title="Rainfall intensity and mode choice in Delhi: a mixed logit approach",
                authors=["Sharma, R."], year=2021, discovery_source="Semantic Scholar",
            ),
        ]
        result = deduplicate(records, settings)
        assert len(result.records) == 1
        assert "fuzzy" in result.merges[0].rule

    def test_rule_4_merges_on_shared_identifier(self, settings: Settings) -> None:
        records = [
            PaperRecord(title="Alpha beta gamma delta", external_ids={"pmid": "555"},
                        discovery_source="Europe PMC"),
            PaperRecord(title="Totally unrelated wording here entirely",
                        external_ids={"pmid": "555"}, discovery_source="OpenAlex"),
        ]
        result = deduplicate(records, settings)
        assert len(result.records) == 1
        assert "pmid" in result.merges[0].rule

    def test_distinct_papers_are_not_merged(self, settings: Settings) -> None:
        records = [
            PaperRecord(title="Rainfall and mode choice in Delhi",
                        doi="10.1016/j.tra.2021.01.001", year=2021),
            PaperRecord(title="Groundwater recharge in arid basins of Rajasthan",
                        doi="10.1016/j.jhydrol.2019.05.002", year=2019),
        ]
        result = deduplicate(records, settings)
        assert len(result.records) == 2
        assert not result.merges

    def test_different_dois_are_never_merged_however_similar(self, settings: Settings) -> None:
        # A preprint and its published version have different DOIs and must stay apart.
        records = [
            PaperRecord(title="Rainfall and mode choice", doi="10.1016/j.tra.2021.01.001",
                        year=2021, authors=["Sharma, R"]),
            PaperRecord(title="Rainfall and mode choice", doi="10.48550/arxiv.2101.00001",
                        year=2021, authors=["Sharma, R"]),
        ]
        result = deduplicate(records, settings)
        assert len(result.records) == 2

    def test_counters_are_reported(self, settings: Settings) -> None:
        records = [
            PaperRecord(title="A study", doi="10.1234/abc", discovery_source="Crossref"),
            PaperRecord(title="A study", doi="10.1234/abc", discovery_source="OpenAlex"),
            PaperRecord(title="Another study", doi="10.1234/xyz"),
        ]
        result = deduplicate(records, settings)
        assert result.counters["input_records"] == 3
        assert result.counters["unique_records"] == 2
        assert result.counters["merged_by_doi"] == 1

    def test_richer_metadata_survives_the_merge(self, settings: Settings) -> None:
        thin = PaperRecord(title="A study", doi="10.1234/abc", discovery_source="arXiv")
        rich = PaperRecord(
            title="A study", doi="10.1234/abc", authors=["Sharma, R"], year=2021,
            journal="TR-A", abstract="x" * 500, discovery_source="Crossref",
        )
        result = deduplicate([thin, rich], settings)
        survivor = result.records[0]
        assert survivor.journal == "TR-A"
        assert survivor.year == 2021
        assert survivor.authors == ["Sharma, R"]

    def test_empty_input_is_handled(self, settings: Settings) -> None:
        result = deduplicate([], settings)
        assert result.records == []
        assert result.counters["unique_records"] == 0


class TestNormaliseRecord:
    """Record clean-up before deduplication."""

    def test_normalises_identifiers_and_whitespace(self) -> None:
        record = PaperRecord(
            title="  Rainfall   Study  ",
            doi="https://doi.org/10.1234/ABC",
            issn="09658564",
        )
        normalise_record(record)
        assert record.title == "Rainfall Study"
        assert record.doi == "10.1234/abc"
        assert record.issn == "0965-8564"

    def test_deduplicates_candidate_urls(self) -> None:
        record = PaperRecord(
            title="A",
            pdf_url="https://a.org/1.pdf",
            candidate_pdf_urls=["https://a.org/1.pdf", "https://a.org/1.pdf",
                                "https://b.org/2.pdf"],
        )
        normalise_record(record)
        assert record.candidate_pdf_urls == ["https://a.org/1.pdf", "https://b.org/2.pdf"]

    def test_assigns_a_record_id(self) -> None:
        record = PaperRecord(title="A study", doi="10.1234/abc")
        normalise_record(record)
        assert record.record_id


class TestRelevanceScoring:
    """Relevance scoring drives selection order."""

    def test_on_topic_paper_scores_higher(self, settings: Settings) -> None:
        concepts = ["rainfall", "mode choice"]
        on_topic = PaperRecord(
            title="Rainfall and mode choice in Delhi",
            abstract="We study rainfall and mode choice.",
            year=2022, journal="TR-A", issn="0965-8564", doi="10.1016/j.tra.2022.01.001",
        )
        off_topic = PaperRecord(
            title="Groundwater recharge in arid basins",
            abstract="We study aquifers.", year=2016,
        )
        on_score = score_relevance(on_topic, concepts, [], settings, current_year=2026)
        off_score = score_relevance(off_topic, concepts, [], settings, current_year=2026)
        assert on_score > off_score

    def test_score_is_bounded(self, settings: Settings) -> None:
        record = PaperRecord(
            title="Rainfall mode choice", abstract="rainfall mode choice",
            keywords=["rainfall"], year=2026, journal="TR-A", issn="0965-8564",
            doi="10.1016/j.tra.2026.01.001", open_access_status="gold",
        )
        score = score_relevance(record, ["rainfall", "mode choice"], ["rainfall"],
                                settings, current_year=2026)
        assert 0.0 <= score <= 1.0

    def test_reasons_are_recorded(self, settings: Settings) -> None:
        record = PaperRecord(title="Rainfall study", year=2021)
        score_relevance(record, ["rainfall"], [], settings, current_year=2026)
        assert record.relevance_reasons

    def test_preprint_is_not_treated_as_indexed(self, settings: Settings) -> None:
        preprint = PaperRecord(
            title="Rainfall study", document_type="preprint", journal="arXiv",
            issn="0965-8564", doi="10.48550/arxiv.2101.00001", year=2021,
        )
        article = PaperRecord(
            title="Rainfall study", document_type="journal-article", journal="TR-A",
            issn="0965-8564", doi="10.1016/j.tra.2021.01.001", year=2021,
        )
        preprint_score = score_relevance(preprint, ["rainfall"], [], settings, current_year=2026)
        article_score = score_relevance(article, ["rainfall"], [], settings, current_year=2026)
        assert article_score > preprint_score
