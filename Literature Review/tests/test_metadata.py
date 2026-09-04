"""Search adapters, Q1 verification, and selection — all with mocked HTTP."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from literature_review_agent.config import Settings
from literature_review_agent.http_client import (
    AccessRestrictedError,
    BlockedHostError,
    HttpClient,
    host_matches,
    host_of,
    looks_like_challenge,
)
from literature_review_agent.job_manager import build_job_config
from literature_review_agent.metadata import enrich_from_crossref, enrich_open_access
from literature_review_agent.q1_verifier import (
    load_ranking_table,
    select_records,
    verify_record,
    verify_records,
    write_pending_q1_csv,
)
from literature_review_agent.schemas import JobConfig, PaperRecord, Q1Mode, Q1Status
from literature_review_agent.search import (
    ArxivAdapter,
    CrossrefAdapter,
    EuropePMCAdapter,
    OpenAlexAdapter,
    SemanticScholarAdapter,
    build_adapters,
    filter_records,
    queries_for_source,
)


@pytest.fixture
def config(settings: Settings) -> JobConfig:
    """A job config for the rainfall topic."""
    return build_job_config(
        "Effect of rainfall on urban travel behaviour",
        settings,
        research_questions=["How does rainfall influence mode choice?"],
    )


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


class TestHostHelpers:
    """Host parsing and matching."""

    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://api.crossref.org/works", "api.crossref.org"),
            ("https://www.sciencedirect.com/x", "sciencedirect.com"),
            ("not a url", ""),
        ],
    )
    def test_host_of(self, url: str, expected: str) -> None:
        assert host_of(url) == expected

    def test_subdomain_matching(self) -> None:
        assert host_matches("https://pmc.ncbi.nlm.nih.gov/x", ["ncbi.nlm.nih.gov"])
        assert not host_matches("https://evil-ncbi.nlm.nih.gov.attacker.com/x",
                                ["ncbi.nlm.nih.gov"])

    def test_challenge_detection(self) -> None:
        assert looks_like_challenge(b"<html>Please solve this CAPTCHA to continue</html>")
        assert looks_like_challenge(b"<html>Institutional login required</html>")
        assert not looks_like_challenge(b"%PDF-1.7 stream")


class TestHttpClient:
    """Retries, refusals, and access-restriction handling."""

    @respx.mock
    def test_blocked_host_is_refused_before_any_request(self, settings: Settings) -> None:
        route = respx.get("https://sci-hub.se/x").mock(return_value=httpx.Response(200))
        client = HttpClient(settings, requests_per_second=0)
        with pytest.raises(BlockedHostError):
            client.request("GET", "https://sci-hub.se/x")
        assert not route.called, "a blocked host must never be contacted at all"
        client.close()

    @respx.mock
    def test_403_raises_access_restricted_and_is_not_retried(self, settings: Settings) -> None:
        route = respx.get("https://example.org/paper").mock(
            return_value=httpx.Response(403, text="Subscribe to view")
        )
        client = HttpClient(settings, requests_per_second=0)
        with pytest.raises(AccessRestrictedError) as info:
            client.request("GET", "https://example.org/paper")
        assert info.value.status == 403
        assert route.call_count == 1, "an access denial must not be retried"
        client.close()

    @respx.mock
    def test_transient_error_is_retried_then_succeeds(self, settings: Settings) -> None:
        route = respx.get("https://example.org/data").mock(
            side_effect=[
                httpx.Response(503),
                httpx.Response(200, json={"ok": True}),
            ]
        )
        client = HttpClient(settings, requests_per_second=0)
        client.backoff_initial = 0.0
        client.backoff_max = 0.0
        assert client.get_json("https://example.org/data") == {"ok": True}
        assert route.call_count == 2
        client.close()

    @respx.mock
    def test_sends_a_descriptive_user_agent(self, settings: Settings) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["ua"] = request.headers.get("user-agent")
            return httpx.Response(200, json={})

        respx.get("https://example.org/x").mock(side_effect=handler)
        client = HttpClient(settings, requests_per_second=0)
        client.get_json("https://example.org/x")
        assert "LiteratureReviewAgent" in captured["ua"]
        assert "mailto" in captured["ua"]
        client.close()


# ---------------------------------------------------------------------------
# Search adapters
# ---------------------------------------------------------------------------


class TestCrossrefAdapter:
    """Crossref parsing."""

    @respx.mock
    def test_parses_a_work_completely(
        self, settings: Settings, config: JobConfig, crossref_payload: dict[str, Any]
    ) -> None:
        respx.get(url__startswith="https://api.crossref.org/works").mock(
            return_value=httpx.Response(200, json=crossref_payload)
        )
        spec = settings.source_specs()["crossref"]
        adapter = CrossrefAdapter(spec, settings, client=HttpClient(settings, requests_per_second=0))
        records = adapter.search("rainfall mode choice", config, limit=10)
        assert len(records) == 1

        record = records[0]
        assert record.title == "Rainfall Intensity and Mode Choice in Delhi"
        assert record.authors == ["Sharma, Ravi", "Patel, Neha"]
        assert record.year == 2021
        assert record.journal == "Transportation Research Part A"
        assert record.doi == "10.1016/j.tra.2021.01.001"
        assert record.issn == "0965-8564"
        assert record.eissn == "1879-2375"
        assert record.citation_count == 42
        assert record.discovery_source == "Crossref"
        assert record.discovery_query == "rainfall mode choice"
        adapter.close()

    @respx.mock
    def test_strips_jats_markup_from_the_abstract(
        self, settings: Settings, config: JobConfig, crossref_payload: dict[str, Any]
    ) -> None:
        respx.get(url__startswith="https://api.crossref.org/works").mock(
            return_value=httpx.Response(200, json=crossref_payload)
        )
        spec = settings.source_specs()["crossref"]
        adapter = CrossrefAdapter(spec, settings, client=HttpClient(settings, requests_per_second=0))
        record = adapter.search("x", config, limit=1)[0]
        assert record.abstract == "Rainfall reduces cycling."
        assert "<" not in record.abstract
        adapter.close()

    @respx.mock
    def test_records_without_a_title_are_dropped(
        self, settings: Settings, config: JobConfig
    ) -> None:
        respx.get(url__startswith="https://api.crossref.org/works").mock(
            return_value=httpx.Response(
                200, json={"message": {"items": [{"DOI": "10.1234/abc", "title": []}]}}
            )
        )
        spec = settings.source_specs()["crossref"]
        adapter = CrossrefAdapter(spec, settings, client=HttpClient(settings, requests_per_second=0))
        assert adapter.search("x", config, limit=10) == []
        adapter.close()

    @respx.mock
    def test_safe_search_converts_a_failure_into_a_log(
        self, settings: Settings, config: JobConfig
    ) -> None:
        respx.get(url__startswith="https://api.crossref.org/works").mock(
            return_value=httpx.Response(403)
        )
        spec = settings.source_specs()["crossref"]
        adapter = CrossrefAdapter(spec, settings, client=HttpClient(settings, requests_per_second=0))
        records, log = adapter.safe_search("x", config, limit=10)
        assert records == []
        assert log.outcome == "access restricted"
        assert log.http_status == 403
        adapter.close()


class TestOpenAlexAdapter:
    """OpenAlex parsing, including the inverted abstract index."""

    @respx.mock
    def test_parses_and_rebuilds_the_abstract(
        self, settings: Settings, config: JobConfig, openalex_payload: dict[str, Any]
    ) -> None:
        respx.get(url__startswith="https://api.openalex.org/works").mock(
            return_value=httpx.Response(200, json=openalex_payload)
        )
        spec = settings.source_specs()["openalex"]
        adapter = OpenAlexAdapter(spec, settings, client=HttpClient(settings, requests_per_second=0))
        record = adapter.search("monsoon ridership", config, limit=10)[0]
        assert record.title == "Monsoon Rainfall and Transit Ridership in Mumbai"
        assert record.abstract == "Monsoon rainfall raises ridership"
        assert record.doi == "10.1016/j.jtrangeo.2019.02.002"
        assert record.pages == "10-22"
        assert record.open_access_status == "green"
        assert "https://europepmc.org/mumbai.pdf" in record.candidate_pdf_urls
        adapter.close()


class TestSemanticScholarAdapter:
    """Semantic Scholar parsing."""

    @respx.mock
    def test_parses_open_access_pdf(self, settings: Settings, config: JobConfig) -> None:
        respx.get(url__startswith="https://api.semanticscholar.org").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "title": "Weather and Cycling in Copenhagen",
                            "abstract": "We study weather effects.",
                            "year": 2020,
                            "venue": "Cities",
                            "externalIds": {"DOI": "10.1016/j.cities.2020.01.001"},
                            "authors": [{"name": "Lars Nielsen"}],
                            "citationCount": 12,
                            "openAccessPdf": {
                                "url": "https://arxiv.org/pdf/2001.00001.pdf",
                                "license": "CCBY",
                            },
                            "publicationTypes": ["JournalArticle"],
                            "journal": {"name": "Cities", "volume": "99"},
                            "fieldsOfStudy": ["Engineering"],
                        }
                    ]
                },
            )
        )
        spec = settings.source_specs()["semantic_scholar"]
        adapter = SemanticScholarAdapter(
            spec, settings, client=HttpClient(settings, requests_per_second=0)
        )
        record = adapter.search("weather cycling", config, limit=10)[0]
        assert record.doi == "10.1016/j.cities.2020.01.001"
        assert record.pdf_url == "https://arxiv.org/pdf/2001.00001.pdf"
        assert record.licence == "CCBY"
        adapter.close()


class TestEuropePMCAdapter:
    """Europe PMC parsing."""

    @respx.mock
    def test_parses_a_result(self, settings: Settings, config: JobConfig) -> None:
        respx.get(url__startswith="https://www.ebi.ac.uk/europepmc").mock(
            return_value=httpx.Response(
                200,
                json={
                    "resultList": {
                        "result": [
                            {
                                "id": "12345",
                                "source": "MED",
                                "pmid": "12345",
                                "doi": "10.1016/j.envres.2020.01.001",
                                "title": "Rainfall and Respiratory Health.",
                                "authorString": "Rao, S., Kumar, A.",
                                "pubYear": "2020",
                                "journalInfo": {
                                    "volume": "12",
                                    "journal": {
                                        "title": "Environmental Research",
                                        "issn": "0013-9351",
                                    },
                                },
                                "isOpenAccess": "Y",
                                "citedByCount": 8,
                                "fullTextUrlList": {
                                    "fullTextUrl": [
                                        {
                                            "documentStyle": "pdf",
                                            "url": "https://europepmc.org/articles/PMC1/pdf",
                                        }
                                    ]
                                },
                            }
                        ]
                    }
                },
            )
        )
        spec = settings.source_specs()["europe_pmc"]
        adapter = EuropePMCAdapter(spec, settings, client=HttpClient(settings, requests_per_second=0))
        record = adapter.search("rainfall health", config, limit=10)[0]
        assert record.title == "Rainfall and Respiratory Health"
        assert record.authors == ["Rao", "S.", "Kumar", "A."]
        assert record.open_access_status == "gold"
        assert record.external_ids["pmid"] == "12345"
        adapter.close()


class TestArxivAdapter:
    """arXiv parsing; results must be labelled as preprints."""

    @respx.mock
    def test_parses_atom_and_labels_preprint(
        self, settings: Settings, config: JobConfig
    ) -> None:
        atom = """<?xml version="1.0"?>
        <feed xmlns="http://www.w3.org/2005/Atom"
              xmlns:arxiv="http://arxiv.org/schemas/atom">
          <entry>
            <id>http://arxiv.org/abs/2101.00001v1</id>
            <published>2021-01-01T00:00:00Z</published>
            <title>Deep Learning for Rainfall Nowcasting</title>
            <summary>We forecast rainfall with neural networks.</summary>
            <author><name>Wei Zhang</name></author>
            <link rel="alternate" href="http://arxiv.org/abs/2101.00001v1"/>
            <link title="pdf" href="http://arxiv.org/pdf/2101.00001v1"/>
            <category term="cs.LG"/>
          </entry>
        </feed>"""
        respx.get(url__startswith="http://export.arxiv.org").mock(
            return_value=httpx.Response(200, text=atom)
        )
        spec = settings.source_specs()["arxiv"]
        adapter = ArxivAdapter(spec, settings, client=HttpClient(settings, requests_per_second=0))
        record = adapter.search("rainfall nowcasting", config, limit=10)[0]
        assert record.title == "Deep Learning for Rainfall Nowcasting"
        assert record.document_type == "preprint"
        assert record.year == 2021
        assert "not peer reviewed" in record.notes
        adapter.close()

    @respx.mock
    def test_malformed_xml_returns_no_records(
        self, settings: Settings, config: JobConfig
    ) -> None:
        respx.get(url__startswith="http://export.arxiv.org").mock(
            return_value=httpx.Response(200, text="<not valid xml")
        )
        spec = settings.source_specs()["arxiv"]
        adapter = ArxivAdapter(spec, settings, client=HttpClient(settings, requests_per_second=0))
        assert adapter.search("x", config, limit=10) == []
        adapter.close()


class TestAdapterRegistry:
    """Keyless sources work; keyed sources self-skip."""

    def test_keyless_sources_are_available(self, settings: Settings) -> None:
        adapters, skipped = build_adapters(settings)
        names = {a.name for a in adapters}
        assert {"crossref", "openalex", "europe_pmc", "arxiv", "semantic_scholar"} <= names
        for adapter in adapters:
            adapter.close()

    def test_keyed_sources_are_skipped_with_a_reason(self, settings: Settings) -> None:
        _, skipped = build_adapters(settings)
        for name in ("core", "elsevier", "springer"):
            assert name in skipped
            assert "not set" in skipped[name]

    def test_query_planner_produces_usable_queries(
        self, settings: Settings, config: JobConfig
    ) -> None:
        from literature_review_agent.keyword_generator import build_keyword_strategy

        strategy = build_keyword_strategy(config, settings)
        queries = queries_for_source(strategy, "crossref", 4)
        assert 1 <= len(queries) <= 4
        assert all(query and breadth for query, breadth in queries)


class TestFilterRecords:
    """Mechanical screening rules."""

    def test_drops_out_of_range_years(self, settings: Settings, config: JobConfig) -> None:
        records = [
            PaperRecord(title="In range paper title", year=2020),
            PaperRecord(title="Too old paper title", year=1999),
        ]
        kept, counters = filter_records(records, config, settings)
        assert len(kept) == 1
        assert counters["dropped_year_out_of_range"] == 1

    def test_drops_non_research_types(self, settings: Settings, config: JobConfig) -> None:
        records = [
            PaperRecord(title="Proper article title here", year=2020,
                        document_type="journal-article"),
            PaperRecord(title="An editorial about things", year=2020, document_type="editorial"),
            PaperRecord(title="A correction notice here", year=2020, document_type="correction"),
        ]
        kept, counters = filter_records(records, config, settings)
        assert len(kept) == 1
        assert counters["dropped_non_research_type"] == 2

    def test_applies_user_exclusion_terms(self, settings: Settings) -> None:
        config = build_job_config(
            "Rainfall and travel", settings, exclusion_terms=["laboratory"]
        )
        records = [
            PaperRecord(title="Field study of rainfall and travel", year=2020),
            PaperRecord(title="Laboratory simulation of rainfall", year=2020),
        ]
        kept, counters = filter_records(records, config, settings)
        assert len(kept) == 1
        assert counters["dropped_exclusion_term"] == 1


# ---------------------------------------------------------------------------
# Enrichment
# ---------------------------------------------------------------------------


class TestEnrichment:
    """Enrichment fills gaps without overwriting existing values."""

    @respx.mock
    def test_fills_only_missing_fields(
        self, settings: Settings, crossref_payload: dict[str, Any]
    ) -> None:
        respx.get(url__startswith="https://api.crossref.org/works/").mock(
            return_value=httpx.Response(200, json={"message": crossref_payload["message"]["items"][0]})
        )
        record = PaperRecord(
            title="A title the user already trusts",
            doi="10.1016/j.tra.2021.01.001",
        )
        enrich_from_crossref(record, settings, client=HttpClient(settings, requests_per_second=0))
        # The existing title must survive; the missing journal must be filled.
        assert record.title == "A title the user already trusts"
        assert record.journal == "Transportation Research Part A"
        assert record.merge_audit

    @respx.mock
    def test_unpaywall_adds_legal_locations(self, settings: Settings) -> None:
        respx.get(url__startswith="https://api.unpaywall.org").mock(
            return_value=httpx.Response(
                200,
                json={
                    "oa_status": "green",
                    "best_oa_location": {
                        "url_for_pdf": "https://europepmc.org/x.pdf",
                        "license": "cc-by",
                    },
                    "oa_locations": [{"url_for_pdf": "https://europepmc.org/x.pdf"}],
                },
            )
        )
        record = PaperRecord(title="X", doi="10.1016/j.tra.2021.01.001")
        enrich_open_access(record, settings, client=HttpClient(settings, requests_per_second=0))
        assert record.pdf_url == "https://europepmc.org/x.pdf"
        assert record.open_access_status == "green"
        assert record.licence == "cc-by"

    @respx.mock
    def test_enrichment_failure_is_non_fatal(self, settings: Settings) -> None:
        respx.get(url__startswith="https://api.crossref.org/works/").mock(
            return_value=httpx.Response(500)
        )
        record = PaperRecord(title="X", doi="10.1016/j.tra.2021.01.001")
        client = HttpClient(settings, requests_per_second=0)
        client.backoff_initial = 0.0
        enrich_from_crossref(record, settings, client=client)
        assert record.title == "X", "a failed lookup must leave the record untouched"

    def test_record_without_doi_is_skipped(self, settings: Settings) -> None:
        record = PaperRecord(title="X")
        assert enrich_from_crossref(record, settings) is record


# ---------------------------------------------------------------------------
# Q1 verification
# ---------------------------------------------------------------------------


class TestQ1Verification:
    """A quartile is only ever reported when a source supplies it."""

    def test_no_ranking_file_yields_unverified(self, settings: Settings) -> None:
        record = PaperRecord(
            title="X", journal="Transportation Research Part A", year=2021, issn="0965-8564"
        )
        verification = verify_record(record, None, settings)
        assert verification.verification_status == Q1Status.UNVERIFIED
        assert verification.quartile is None
        assert "No journal-ranking data" in verification.notes

    def test_famous_journal_is_not_assumed_q1(self, settings: Settings) -> None:
        # The whole point: reputation is not evidence.
        record = PaperRecord(title="X", journal="Nature", year=2021, citation_count=50000)
        assert verify_record(record, None, settings).verification_status == Q1Status.UNVERIFIED

    def test_preprint_is_not_applicable(self, settings: Settings) -> None:
        record = PaperRecord(title="X", journal="arXiv", document_type="preprint", year=2021)
        verification = verify_record(record, None, settings)
        assert verification.verification_status == Q1Status.NOT_APPLICABLE

    def test_issn_match_gives_verified_q1(
        self, settings: Settings, ranking_csv: Path
    ) -> None:
        table = load_ranking_table(settings, ranking_csv)
        assert table is not None and not table.is_empty
        record = PaperRecord(
            title="X", journal="Transportation Research Part A", year=2021, issn="0965-8564"
        )
        verification = verify_record(record, table, settings)
        assert verification.verification_status == Q1Status.VERIFIED_Q1
        assert verification.quartile == "Q1"
        assert verification.ranking_year == 2021
        assert "ISSN" in (verification.matched_on or "")
        assert verification.ranking_source
        assert verification.verification_date

    def test_non_q1_journal_is_verified_non_q1(
        self, settings: Settings, ranking_csv: Path
    ) -> None:
        table = load_ranking_table(settings, ranking_csv)
        record = PaperRecord(
            title="X", journal="Journal of Transport Geography", year=2021, issn="0966-6923"
        )
        verification = verify_record(record, table, settings)
        assert verification.verification_status == Q1Status.VERIFIED_NON_Q1
        assert verification.quartile == "Q2"

    def test_journal_name_match_when_issn_absent(
        self, settings: Settings, ranking_csv: Path
    ) -> None:
        table = load_ranking_table(settings, ranking_csv)
        record = PaperRecord(title="X", journal="Transportation Research Part A", year=2021)
        verification = verify_record(record, table, settings)
        assert verification.verification_status == Q1Status.VERIFIED_Q1
        assert "name" in (verification.matched_on or "")

    def test_journal_absent_from_ranking_stays_unverified(
        self, settings: Settings, ranking_csv: Path
    ) -> None:
        table = load_ranking_table(settings, ranking_csv)
        record = PaperRecord(title="X", journal="A Journal Nobody Has Ranked", year=2021)
        verification = verify_record(record, table, settings)
        assert verification.verification_status == Q1Status.UNVERIFIED
        assert "not found" in verification.notes

    def test_blank_quartile_cell_stays_unverified(
        self, settings: Settings, ranking_csv: Path
    ) -> None:
        table = load_ranking_table(settings, ranking_csv)
        record = PaperRecord(
            title="X", journal="Obscure Regional Bulletin", year=2021, issn="1234-5678"
        )
        verification = verify_record(record, table, settings)
        assert verification.verification_status == Q1Status.UNVERIFIED
        assert verification.quartile is None

    def test_cross_category_disagreement_is_conflicting(
        self, settings: Settings, ranking_csv: Path
    ) -> None:
        # Q1 in Economics but Q3 in Sociology: the reviewer must choose.
        table = load_ranking_table(settings, ranking_csv)
        record = PaperRecord(
            title="X", journal="Dual Category Review", year=2021, issn="2222-3333"
        )
        verification = verify_record(record, table, settings)
        assert verification.verification_status == Q1Status.CONFLICTING
        assert "different quartiles" in verification.notes

    def test_ranking_year_difference_is_disclosed(
        self, settings: Settings, ranking_csv: Path
    ) -> None:
        table = load_ranking_table(settings, ranking_csv)
        record = PaperRecord(
            title="X", journal="Transportation Research Part A", year=2024, issn="0965-8564"
        )
        verification = verify_record(record, table, settings)
        assert verification.ranking_year == 2021
        assert "2021 ranking" in verification.notes

    def test_missing_ranking_file_path_is_handled(self, settings: Settings) -> None:
        assert load_ranking_table(settings, "/nonexistent/ranking.csv") is None

    def test_verify_records_returns_counters(
        self, settings: Settings, sample_records: list[PaperRecord], ranking_csv: Path
    ) -> None:
        counters = verify_records(sample_records, settings, ranking_file=ranking_csv)
        assert counters[Q1Status.VERIFIED_Q1.value] == 1
        assert counters[Q1Status.VERIFIED_NON_Q1.value] == 1


class TestSelection:
    """Selection honours the Q1 mode without ever assuming a quartile."""

    def test_preferred_mode_includes_unverified_papers(
        self, settings: Settings, sample_records: list[PaperRecord]
    ) -> None:
        config = build_job_config("Rainfall and travel", settings, q1_mode="preferred")
        verify_records(sample_records, settings, table=None)
        for record in sample_records:
            record.relevance_score = 0.5
        result = select_records(sample_records, config, settings)
        assert len(result.selected) == 2
        assert not result.pending_q1

    def test_only_mode_sends_unverified_to_pending(
        self, settings: Settings, sample_records: list[PaperRecord]
    ) -> None:
        # The critical behaviour: unverified must never be silently treated as Q1.
        config = build_job_config("Rainfall and travel", settings, q1_mode="only")
        verify_records(sample_records, settings, table=None)
        for record in sample_records:
            record.relevance_score = 0.5
        result = select_records(sample_records, config, settings)
        assert result.selected == []
        assert len(result.pending_q1) == 2
        assert all(r.pending_q1_verification for r in result.pending_q1)
        assert all("awaits manual quartile verification" in r.selection_reason
                   for r in result.pending_q1)

    def test_only_mode_includes_verified_q1(
        self, settings: Settings, sample_records: list[PaperRecord], ranking_csv: Path
    ) -> None:
        config = build_job_config("Rainfall and travel", settings, q1_mode="only")
        verify_records(sample_records, settings, ranking_file=ranking_csv)
        for record in sample_records:
            record.relevance_score = 0.5
        result = select_records(sample_records, config, settings)
        assert len(result.selected) == 1
        assert result.selected[0].q1.verification_status == Q1Status.VERIFIED_Q1

    def test_preferred_mode_ranks_q1_first(
        self, settings: Settings, sample_records: list[PaperRecord], ranking_csv: Path
    ) -> None:
        config = build_job_config("Rainfall and travel", settings, q1_mode="preferred")
        verify_records(sample_records, settings, ranking_file=ranking_csv)
        # Give the non-Q1 paper the higher relevance to prove Q1 still leads.
        sample_records[0].relevance_score = 0.4
        sample_records[1].relevance_score = 0.9
        result = select_records(sample_records, config, settings)
        assert result.selected[0].q1.verification_status == Q1Status.VERIFIED_Q1

    def test_max_papers_is_respected(
        self, settings: Settings, sample_records: list[PaperRecord]
    ) -> None:
        config = build_job_config("Rainfall and travel", settings, maximum_papers=1)
        verify_records(sample_records, settings, table=None)
        for record in sample_records:
            record.relevance_score = 0.5
        result = select_records(sample_records, config, settings)
        assert len(result.selected) == 1
        assert len(result.rejected) == 1

    def test_low_relevance_is_rejected(
        self, settings: Settings, sample_records: list[PaperRecord]
    ) -> None:
        config = build_job_config("Rainfall and travel", settings)
        verify_records(sample_records, settings, table=None)
        for record in sample_records:
            record.relevance_score = 0.0
        result = select_records(sample_records, config, settings)
        assert result.selected == []
        assert all("below the minimum" in r.selection_reason for r in result.rejected)

    def test_pending_csv_is_written(
        self, settings: Settings, sample_records: list[PaperRecord], tmp_path: Path
    ) -> None:
        verify_records(sample_records, settings, table=None)
        path = write_pending_q1_csv(sample_records, tmp_path / "pending.csv", settings)
        content = path.read_text(encoding="utf-8-sig")
        assert "record_id" in content
        assert "Unverified" in content
        assert "recommended_action" in content
