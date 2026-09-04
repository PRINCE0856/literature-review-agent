"""End-to-end mocked pipeline, resume behaviour, and job management."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
import yaml

from literature_review_agent.config import Settings
from literature_review_agent.job_manager import Job, JobError, build_job_config, list_jobs
from literature_review_agent.orchestrator import Orchestrator
from literature_review_agent.schemas import (
    STAGE_ORDER,
    DownloadStatus,
    Q1Status,
    StageName,
    StageStatus,
)


# ---------------------------------------------------------------------------
# Job configuration and folders
# ---------------------------------------------------------------------------


class TestJobConfig:
    """Defaults, assumptions, and validation."""

    def test_applies_documented_defaults(self, settings: Settings) -> None:
        config = build_job_config("Rainfall and travel", settings)
        assert config.year_from == 2015
        assert config.maximum_papers == 50
        assert config.q1_mode.value == "preferred"
        assert config.language == "English"
        assert config.paper_types == ["journal article"]
        assert config.citation_style.value == "APA 7"
        assert config.download_only_legal_and_authorized_content is True

    def test_records_every_assumption(self, settings: Settings) -> None:
        config = build_job_config("Rainfall and travel", settings)
        assert config.assumptions
        assert any("year_from" in a for a in config.assumptions)
        assert any("ranking file" in a for a in config.assumptions)

    def test_user_values_override_defaults(self, settings: Settings) -> None:
        config = build_job_config(
            "Rainfall and travel", settings, year_from=2018, maximum_papers=5, q1_mode="only"
        )
        assert config.year_from == 2018
        assert config.maximum_papers == 5
        assert config.q1_mode.value == "only"

    def test_preserves_the_complete_topic(self, settings: Settings) -> None:
        topic = "Effect of Rainfall (mm/h) on Urban Travel: A Delhi Case Study, 2015-2024"
        config = build_job_config(topic, settings)
        assert config.topic == topic
        assert config.topic_slug != topic
        assert "/" not in config.topic_slug

    def test_defaults_the_question_to_the_topic(self, settings: Settings) -> None:
        config = build_job_config("Rainfall and travel", settings)
        assert config.research_questions == ["Rainfall and travel"]
        assert any("guiding question" in a for a in config.assumptions)

    def test_rejects_an_inverted_year_range(self, settings: Settings) -> None:
        with pytest.raises(ValueError, match="cannot be before"):
            build_job_config("X", settings, year_from=2020, year_to=2010)

    def test_rejects_zero_papers(self, settings: Settings) -> None:
        with pytest.raises(ValueError, match="at least 1"):
            build_job_config("X", settings, maximum_papers=0)

    def test_job_id_is_deterministic(self, settings: Settings) -> None:
        first = build_job_config("Rainfall and travel", settings, job_date="2026-01-01")
        second = build_job_config("Rainfall and travel", settings, job_date="2026-01-01")
        assert first.job_id == second.job_id


class TestJobFolders:
    """Date and topic partitioning."""

    def test_creates_the_full_folder_tree(self, job: Job) -> None:
        for path in job.paths.all_dirs():
            assert path.is_dir(), f"missing folder: {path}"

    def test_folders_are_date_and_topic_partitioned(self, job: Job) -> None:
        assert job.paths.keywords.name == job.config.topic_slug
        assert job.paths.keywords.parent.name == job.config.job_date
        assert job.paths.papers.parent.parent.name == "02 Literature Papers"
        assert job.paths.reports.parent.parent.name == "03 Reports"
        assert job.paths.verification.parent.parent.name == "04 Verification"
        assert job.paths.logs.parent.parent.name == "05 Logs and State"

    def test_paper_subfolders_exist(self, job: Job) -> None:
        assert job.paths.downloaded_papers.is_dir()
        assert job.paths.extracted_text.is_dir()
        assert job.paths.unable_to_download.is_dir()

    def test_writes_job_config_with_the_original_topic(self, job: Job) -> None:
        raw = yaml.safe_load(job.paths.job_config_file.read_text(encoding="utf-8"))
        assert raw["topic"] == "Effect of rainfall on urban travel behaviour"
        assert raw["topic_slug"] == job.config.topic_slug
        assert raw["assumptions"]

    def test_each_subagent_gets_a_private_workspace(self, job: Job) -> None:
        first = job.paths.agent_dir("paper-analysis-agent")
        second = job.paths.agent_dir("metadata-q1-verifier")
        assert first.is_dir() and second.is_dir()
        assert first != second, "subagents must not share an output path"

    def test_reload_finds_the_same_job(self, job: Job, workspace_settings: Settings) -> None:
        reloaded = Job.load(job.paths.logs, settings=workspace_settings)
        assert reloaded.config.job_id == job.config.job_id
        assert reloaded.config.topic == job.config.topic

    def test_reload_accepts_the_config_file_path(
        self, job: Job, workspace_settings: Settings
    ) -> None:
        reloaded = Job.load(job.paths.job_config_file, settings=workspace_settings)
        assert reloaded.config.job_id == job.config.job_id

    def test_reload_accepts_a_sibling_folder(
        self, job: Job, workspace_settings: Settings
    ) -> None:
        reloaded = Job.load(job.paths.reports, settings=workspace_settings)
        assert reloaded.config.job_id == job.config.job_id

    def test_missing_job_raises_a_clear_error(
        self, tmp_path: Path, workspace_settings: Settings
    ) -> None:
        with pytest.raises(JobError, match="Could not find job_config.yaml"):
            Job.load(tmp_path / "nowhere", settings=workspace_settings)

    def test_rerunning_the_same_topic_reuses_the_folder(
        self, job: Job, workspace_settings: Settings
    ) -> None:
        again = Job.create(job.config.topic, settings=workspace_settings)
        assert again.paths.logs == job.paths.logs

    def test_list_jobs_reports_the_job(self, job: Job, workspace_settings: Settings) -> None:
        found = list_jobs(workspace_settings)
        assert len(found) == 1
        assert found[0]["topic"] == job.config.topic


# ---------------------------------------------------------------------------
# Checkpoints and resume
# ---------------------------------------------------------------------------


class TestCheckpoints:
    """Resumability rests entirely on these."""

    def test_all_stages_start_pending(self, job: Job) -> None:
        for stage in STAGE_ORDER:
            assert job.stage_status(stage) == StageStatus.PENDING
        assert job.next_stage() == StageName.KEYWORDS

    def test_completing_a_stage_advances_the_next(self, job: Job) -> None:
        job.start_stage(StageName.KEYWORDS)
        job.complete_stage(StageName.KEYWORDS, message="done", counters={"terms": 40})
        assert job.is_stage_complete(StageName.KEYWORDS)
        assert job.next_stage() == StageName.SEARCH
        assert job.checkpoints.stage(StageName.KEYWORDS).counters["terms"] == 40

    def test_checkpoints_survive_a_reload(self, job: Job, workspace_settings: Settings) -> None:
        job.start_stage(StageName.KEYWORDS)
        job.complete_stage(StageName.KEYWORDS, message="done")
        reloaded = Job.load(job.paths.logs, settings=workspace_settings)
        assert reloaded.is_stage_complete(StageName.KEYWORDS)

    def test_item_level_progress_is_recorded(self, job: Job) -> None:
        job.mark_item_done(StageName.DOWNLOAD, "rec-1")
        job.mark_item_done(StageName.DOWNLOAD, "rec-2")
        job.mark_item_done(StageName.DOWNLOAD, "rec-1")  # idempotent
        assert job.done_items(StageName.DOWNLOAD) == {"rec-1", "rec-2"}

    def test_item_progress_survives_a_reload(
        self, job: Job, workspace_settings: Settings
    ) -> None:
        # This is what lets a download stage resume mid-list rather than restart.
        job.mark_item_done(StageName.DOWNLOAD, "rec-1")
        reloaded = Job.load(job.paths.logs, settings=workspace_settings)
        assert "rec-1" in reloaded.done_items(StageName.DOWNLOAD)

    def test_failure_keeps_item_progress(self, job: Job) -> None:
        job.mark_item_done(StageName.DOWNLOAD, "rec-1")
        job.fail_stage(StageName.DOWNLOAD, "network dropped")
        assert job.stage_status(StageName.DOWNLOAD) == StageStatus.FAILED
        assert "rec-1" in job.done_items(StageName.DOWNLOAD)
        assert job.next_stage() == StageName.KEYWORDS  # earlier stages still pending

    def test_attempts_are_counted(self, job: Job) -> None:
        job.start_stage(StageName.SEARCH)
        job.fail_stage(StageName.SEARCH, "boom")
        job.start_stage(StageName.SEARCH)
        assert job.checkpoints.stage(StageName.SEARCH).attempts == 2

    def test_reset_cascades_to_later_stages(self, job: Job) -> None:
        for stage in (StageName.KEYWORDS, StageName.SEARCH, StageName.DEDUPLICATE):
            job.start_stage(stage)
            job.complete_stage(stage)
        job.reset_stage(StageName.SEARCH)
        assert job.is_stage_complete(StageName.KEYWORDS)
        assert not job.is_stage_complete(StageName.SEARCH)
        assert not job.is_stage_complete(StageName.DEDUPLICATE)

    def test_reset_without_cascade(self, job: Job) -> None:
        for stage in (StageName.KEYWORDS, StageName.SEARCH):
            job.start_stage(stage)
            job.complete_stage(stage)
        job.reset_stage(StageName.KEYWORDS, cascade=False)
        assert not job.is_stage_complete(StageName.KEYWORDS)
        assert job.is_stage_complete(StageName.SEARCH)

    def test_corrupt_checkpoint_file_is_rebuilt(
        self, job: Job, workspace_settings: Settings
    ) -> None:
        job.paths.checkpoints_file.write_text("{not valid json", encoding="utf-8")
        reloaded = Job.load(job.paths.logs, settings=workspace_settings)
        assert reloaded.next_stage() == StageName.KEYWORDS

    def test_describe_summarises_state(self, job: Job) -> None:
        job.start_stage(StageName.KEYWORDS)
        job.complete_stage(StageName.KEYWORDS, message="ok")
        description = job.describe()
        assert description["topic"] == job.config.topic
        assert description["stages"]["keywords"]["status"] == "complete"
        assert description["next_stage"] == "search"


# ---------------------------------------------------------------------------
# Mocked end-to-end pipeline
# ---------------------------------------------------------------------------


def _mock_all_apis(router: Any, fixtures_dir: Path) -> None:
    """Route every scholarly API and the PDF hosts to local fixtures.

    Routes are registered on the *active* router so they apply inside the
    surrounding ``respx.mock`` context.
    """
    router.get(url__startswith="https://api.crossref.org/works/").mock(
        return_value=httpx.Response(404, json={})
    )
    router.get(url__startswith="https://api.crossref.org/works").mock(
        return_value=httpx.Response(
            200,
            json={
                "message": {
                    "items": [
                        {
                            "DOI": "10.1016/j.tra.2021.01.001",
                            "title": ["Rainfall Intensity and Mode Choice in Delhi"],
                            "author": [
                                {"family": "Sharma", "given": "Ravi"},
                                {"family": "Patel", "given": "Neha"},
                            ],
                            "issued": {"date-parts": [[2021]]},
                            "container-title": ["Transportation Research Part A"],
                            "volume": "150",
                            "page": "45-61",
                            "ISSN": ["0965-8564"],
                            "publisher": "Elsevier",
                            "abstract": "<jats:p>Rainfall reduces cycling.</jats:p>",
                            "type": "journal-article",
                            "language": "en",
                            "is-referenced-by-count": 42,
                            "URL": "https://doi.org/10.1016/j.tra.2021.01.001",
                        }
                    ]
                }
            },
        )
    )
    router.get(url__startswith="https://api.openalex.org/works").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "https://openalex.org/W1",
                        "doi": "https://doi.org/10.1016/j.jtrangeo.2019.02.002",
                        "title": "Monsoon Rainfall and Transit Ridership in Mumbai",
                        "publication_year": 2019,
                        "type": "article",
                        "language": "en",
                        "cited_by_count": 17,
                        "authorships": [{"author": {"display_name": "Anita Iyer"}}],
                        "primary_location": {
                            "landing_page_url": "https://example.org/mumbai",
                            "license": "cc-by",
                            "source": {
                                "display_name": "Journal of Transport Geography",
                                "issn_l": "0966-6923",
                                "issn": ["0966-6923"],
                            },
                        },
                        "best_oa_location": {
                            "pdf_url": "https://europepmc.org/mumbai.pdf"
                        },
                        "locations": [{"pdf_url": "https://europepmc.org/mumbai.pdf"}],
                        "open_access": {"oa_status": "green"},
                        "biblio": {"volume": "74", "first_page": "10", "last_page": "22"},
                        "ids": {"doi": "10.1016/j.jtrangeo.2019.02.002"},
                    }
                ]
            },
        )
    )
    # The remaining sources return nothing, which the pipeline must tolerate.
    router.get(url__startswith="https://api.semanticscholar.org").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    router.get(url__startswith="https://www.ebi.ac.uk/europepmc").mock(
        return_value=httpx.Response(200, json={"resultList": {"result": []}})
    )
    router.get(url__startswith="http://export.arxiv.org").mock(
        return_value=httpx.Response(
            200, text='<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"/>'
        )
    )
    router.get(url__startswith="https://api.unpaywall.org").mock(
        return_value=httpx.Response(
            200,
            json={
                "oa_status": "green",
                "best_oa_location": {
                    "url_for_pdf": "https://europepmc.org/delhi.pdf",
                    "license": "cc-by",
                },
                "oa_locations": [{"url_for_pdf": "https://europepmc.org/delhi.pdf"}],
            },
        )
    )

    delhi = (fixtures_dir / "rainfall_delhi.pdf").read_bytes()
    mumbai = (fixtures_dir / "monsoon_mumbai.pdf").read_bytes()
    router.get("https://europepmc.org/delhi.pdf").mock(
        return_value=httpx.Response(
            200, content=delhi, headers={"content-type": "application/pdf"}
        )
    )
    router.get("https://europepmc.org/mumbai.pdf").mock(
        return_value=httpx.Response(
            200, content=mumbai, headers={"content-type": "application/pdf"}
        )
    )
    router.head(url__startswith="https://doi.org/").mock(return_value=httpx.Response(302))


class TestEndToEndPipeline:
    """A complete run with every network call mocked."""

    @pytest.fixture
    def completed(
        self, workspace_settings: Settings, fixtures_dir: Path, ranking_csv: Path
    ) -> tuple[Job, Orchestrator, list[Any]]:
        """Run the whole pipeline once and return the result."""
        with respx.mock(assert_all_called=False) as mock:
            _mock_all_apis(mock, fixtures_dir)
            job = Job.create(
                "Effect of rainfall on urban travel behaviour",
                settings=workspace_settings,
                research_questions=["How does rainfall influence mode choice?"],
                maximum_papers=10,
                ranking_file=str(ranking_csv),
            )
            orchestrator = Orchestrator(job, settings=workspace_settings, enable_drive=False)
            outcomes = orchestrator.run()
        return job, orchestrator, outcomes

    def test_every_stage_completes(self, completed: tuple[Job, Orchestrator, list]) -> None:
        job, _, outcomes = completed
        failed = [o for o in outcomes if o.status == StageStatus.FAILED]
        assert not failed, f"stages failed: {[(o.stage.value, o.message) for o in failed]}"
        assert job.next_stage() is None, "the pipeline must reach the end"

    def test_papers_are_discovered_and_deduplicated(
        self, completed: tuple[Job, Orchestrator, list]
    ) -> None:
        _, orchestrator, _ = completed
        assert len(orchestrator.state.records) == 2
        dois = {r.doi for r in orchestrator.state.records}
        assert dois == {"10.1016/j.tra.2021.01.001", "10.1016/j.jtrangeo.2019.02.002"}

    def test_q1_status_comes_from_the_ranking_file(
        self, completed: tuple[Job, Orchestrator, list]
    ) -> None:
        _, orchestrator, _ = completed
        statuses = {r.q1.verification_status for r in orchestrator.state.records}
        assert Q1Status.VERIFIED_Q1 in statuses
        for record in orchestrator.state.records:
            if record.q1.quartile:
                assert record.q1.ranking_source, "a quartile needs a named source"

    def test_pdfs_are_downloaded_and_renamed_to_the_title(
        self, completed: tuple[Job, Orchestrator, list]
    ) -> None:
        job, orchestrator, _ = completed
        downloaded = [
            r for r in orchestrator.state.records
            if r.download_status == DownloadStatus.DOWNLOADED
        ]
        assert len(downloaded) == 2
        for record in downloaded:
            path = Path(record.local_path)
            assert path.exists()
            assert path.parent == job.paths.downloaded_papers
            assert path.stem.lower().startswith(record.title.split()[0].lower())
            assert record.file_sha256, "a checksum must be recorded"

    def test_text_is_extracted_with_page_markers(
        self, completed: tuple[Job, Orchestrator, list]
    ) -> None:
        _, orchestrator, _ = completed
        for record in orchestrator.state.records:
            assert record.extracted_text_path
            content = Path(record.extracted_text_path).read_text(encoding="utf-8")
            assert "=== PAGE 1 ===" in content

    def test_papers_are_analysed_with_evidence(
        self, completed: tuple[Job, Orchestrator, list]
    ) -> None:
        _, orchestrator, _ = completed
        assert len(orchestrator.state.analyses) == 2
        for analysis in orchestrator.state.analyses.values():
            assert analysis.field("research_objective").is_reported
            assert analysis.field("research_objective").pages

    def test_every_expected_artefact_exists(
        self, completed: tuple[Job, Orchestrator, list]
    ) -> None:
        job, _, _ = completed
        expected = [
            job.paths.keywords / "keywords.md",
            job.paths.keywords / "keywords.csv",
            job.paths.keywords / "search_strings.md",
            job.paths.keywords / "inclusion_exclusion_criteria.md",
            job.paths.papers / "paper_manifest.csv",
            job.paths.papers / "paper_manifest.json",
            job.paths.papers / "references.bib",
            job.paths.papers / "references.ris",
            job.paths.unable_to_download / "Unable_to_Download.docx",
            job.paths.reports / "Literature_Review_Matrix.xlsx",
            job.paths.reports / "Introduction.docx",
            job.paths.reports / "Research_Gaps.docx",
            job.paths.reports / "Global_Research_Landscape.docx",
            job.paths.reports / "Models_and_Applications.docx",
            job.paths.reports / "Paper_Summaries.docx",
            job.paths.verification / "Evidence_Ledger.xlsx",
            job.paths.verification / "Verification_Report.docx",
            job.paths.verification / "Citation_Audit.xlsx",
            job.paths.verification / "unresolved_issues.csv",
            job.paths.logs / "pipeline.log",
            job.paths.logs / "search_log.jsonl",
            job.paths.logs / "download_log.jsonl",
            job.paths.logs / "checkpoints.json",
            job.paths.logs / "job_config.yaml",
        ]
        missing = [p for p in expected if not p.exists()]
        assert not missing, f"missing artefacts: {[str(p.name) for p in missing]}"

    def test_evidence_ledger_backs_the_claims(
        self, completed: tuple[Job, Orchestrator, list]
    ) -> None:
        _, orchestrator, _ = completed
        assert orchestrator.state.ledger.records
        for record in orchestrator.state.ledger.records:
            assert record.claim
            assert record.record_id

    def test_completion_summary_is_honest(
        self, completed: tuple[Job, Orchestrator, list]
    ) -> None:
        _, orchestrator, _ = completed
        summary = orchestrator.completion_summary()
        assert summary["complete"] is True
        assert summary["counts"]["unique"] == 2
        assert summary["counts"]["downloaded"] == 2
        assert summary["counts"]["analysed"] == 2
        assert summary["assumptions"]
        # Drive is off in this run, which must be disclosed rather than implied.
        assert any("Google Drive" in item for item in summary["limitations"])

    def test_no_upload_is_claimed_when_drive_is_off(
        self, completed: tuple[Job, Orchestrator, list]
    ) -> None:
        _, orchestrator, _ = completed
        drive = orchestrator.storage.summary()
        assert drive["drive_enabled"] is False
        assert drive["artefacts_uploaded_and_verified"] == 0
        assert drive["verified_links"] == []


class TestResumeBehaviour:
    """An interrupted run must continue, not restart."""

    def test_completed_stages_are_skipped(
        self, workspace_settings: Settings, fixtures_dir: Path
    ) -> None:
        with respx.mock(assert_all_called=False) as mock:
            _mock_all_apis(mock, fixtures_dir)
            job = Job.create("Rainfall and travel", settings=workspace_settings,
                             maximum_papers=5)
            orchestrator = Orchestrator(job, settings=workspace_settings, enable_drive=False)
            orchestrator.run(stages=[StageName.KEYWORDS])
            assert job.is_stage_complete(StageName.KEYWORDS)

            first_mtime = (job.paths.keywords / "keywords.md").stat().st_mtime_ns
            outcome = orchestrator.run_stage(StageName.KEYWORDS)
            assert outcome.message == "already complete"
            assert (job.paths.keywords / "keywords.md").stat().st_mtime_ns == first_mtime

    def test_force_reruns_a_completed_stage(
        self, workspace_settings: Settings, fixtures_dir: Path
    ) -> None:
        with respx.mock(assert_all_called=False) as mock:
            _mock_all_apis(mock, fixtures_dir)
            job = Job.create("Rainfall and travel", settings=workspace_settings)
            orchestrator = Orchestrator(job, settings=workspace_settings, enable_drive=False)
            orchestrator.run(stages=[StageName.KEYWORDS])
            outcome = orchestrator.run_stage(StageName.KEYWORDS, force=True)
            assert outcome.status == StageStatus.COMPLETE
            assert outcome.message != "already complete"

    def test_interrupted_download_resumes_without_refetching(
        self, workspace_settings: Settings, fixtures_dir: Path
    ) -> None:
        with respx.mock(assert_all_called=False) as mock:
            _mock_all_apis(mock, fixtures_dir)
            job = Job.create("Rainfall and travel", settings=workspace_settings,
                             maximum_papers=5)
            orchestrator = Orchestrator(job, settings=workspace_settings, enable_drive=False)
            orchestrator.run(
                stages=[StageName.KEYWORDS, StageName.SEARCH, StageName.DEDUPLICATE,
                        StageName.Q1_VERIFY, StageName.SELECT, StageName.DOWNLOAD]
            )
            first_calls = len(mock.calls)
            assert job.is_stage_complete(StageName.DOWNLOAD)
            assert job.done_items(StageName.DOWNLOAD), "items must be checkpointed"

            # A fresh orchestrator on the same job must not re-download anything.
            resumed = Orchestrator(job, settings=workspace_settings, enable_drive=False)
            resumed.load_state()
            resumed.run_stage(StageName.DOWNLOAD)
            assert len(mock.calls) == first_calls

    def test_resume_continues_from_the_next_stage(
        self, workspace_settings: Settings, fixtures_dir: Path
    ) -> None:
        with respx.mock(assert_all_called=False) as mock:
            _mock_all_apis(mock, fixtures_dir)
            job = Job.create("Rainfall and travel", settings=workspace_settings,
                             maximum_papers=5)
            first = Orchestrator(job, settings=workspace_settings, enable_drive=False)
            first.run(stages=[StageName.KEYWORDS, StageName.SEARCH])

            reloaded = Job.load(job.paths.logs, settings=workspace_settings)
            assert reloaded.next_stage() == StageName.DEDUPLICATE

            second = Orchestrator(reloaded, settings=workspace_settings, enable_drive=False)
            remaining = list(STAGE_ORDER[STAGE_ORDER.index(StageName.DEDUPLICATE):])
            outcomes = second.run(stages=remaining)
            assert not [o for o in outcomes if o.status == StageStatus.FAILED]
            assert reloaded.next_stage() is None

    def test_state_reloads_from_disk_after_a_restart(
        self, workspace_settings: Settings, fixtures_dir: Path
    ) -> None:
        with respx.mock(assert_all_called=False) as mock:
            _mock_all_apis(mock, fixtures_dir)
            job = Job.create("Rainfall and travel", settings=workspace_settings,
                             maximum_papers=5)
            first = Orchestrator(job, settings=workspace_settings, enable_drive=False)
            first.run(stages=[StageName.KEYWORDS, StageName.SEARCH, StageName.DEDUPLICATE])

            reloaded = Job.load(job.paths.logs, settings=workspace_settings)
            second = Orchestrator(reloaded, settings=workspace_settings, enable_drive=False)
            state = second.load_state()
            assert len(state.records) == 2
            assert state.strategy is not None


class TestDegradedConditions:
    """The pipeline must survive an empty or hostile network."""

    def test_no_search_results_does_not_crash(self, workspace_settings: Settings) -> None:
        with respx.mock(assert_all_called=False) as mock:
            mock.get(url__startswith="https://api.crossref.org").mock(
                return_value=httpx.Response(200, json={"message": {"items": []}})
            )
            mock.get(url__startswith="https://api.openalex.org").mock(
                return_value=httpx.Response(200, json={"results": []})
            )
            mock.get(url__startswith="https://api.semanticscholar.org").mock(
                return_value=httpx.Response(200, json={"data": []})
            )
            mock.get(url__startswith="https://www.ebi.ac.uk").mock(
                return_value=httpx.Response(200, json={"resultList": {"result": []}})
            )
            mock.get(url__startswith="http://export.arxiv.org").mock(
                return_value=httpx.Response(
                    200, text='<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"/>'
                )
            )
            job = Job.create("An obscure topic with no literature",
                             settings=workspace_settings)
            orchestrator = Orchestrator(job, settings=workspace_settings, enable_drive=False)
            outcomes = orchestrator.run()

        assert not [o for o in outcomes if o.status == StageStatus.FAILED]
        skipped = [o for o in outcomes if o.status == StageStatus.SKIPPED]
        assert skipped, "downstream stages must be skipped, not failed"
        summary = orchestrator.completion_summary()
        assert summary["counts"]["unique"] == 0

    def test_all_sources_failing_does_not_crash(self, workspace_settings: Settings) -> None:
        with respx.mock(assert_all_called=False) as mock:
            mock.route().mock(return_value=httpx.Response(500))
            job = Job.create("Rainfall and travel", settings=workspace_settings)
            orchestrator = Orchestrator(job, settings=workspace_settings, enable_drive=False)
            # Keep the retry backoff instant for the test.
            outcomes = orchestrator.run(
                stages=[StageName.KEYWORDS, StageName.SEARCH]
            )
        statuses = {o.stage: o.status for o in outcomes}
        assert statuses[StageName.KEYWORDS] == StageStatus.COMPLETE
        assert statuses[StageName.SEARCH] == StageStatus.COMPLETE
        assert orchestrator.state.raw_records == []

    def test_paywalled_pdf_is_recorded_not_bypassed(
        self, workspace_settings: Settings, fixtures_dir: Path
    ) -> None:
        with respx.mock(assert_all_called=False) as mock:
            _mock_all_apis(mock, fixtures_dir)
            # Both PDF hosts now refuse access.
            mock.get("https://europepmc.org/delhi.pdf").mock(
                return_value=httpx.Response(403, text="Please sign in to view")
            )
            mock.get("https://europepmc.org/mumbai.pdf").mock(
                return_value=httpx.Response(403, text="Please sign in to view")
            )
            job = Job.create("Rainfall and travel", settings=workspace_settings,
                             maximum_papers=5)
            orchestrator = Orchestrator(job, settings=workspace_settings, enable_drive=False)
            outcomes = orchestrator.run()

        assert not [o for o in outcomes if o.status == StageStatus.FAILED]
        failed = [
            r for r in orchestrator.state.records
            if r.download_status == DownloadStatus.FAILED
        ]
        assert failed, "a 403 must be recorded as a failure"
        assert all(r.failure_reason for r in failed)

        register = job.paths.unable_to_download / "Unable_to_Download.docx"
        assert register.exists()
        from docx import Document as ReadDocx

        text = "\n".join(p.text for p in ReadDocx(register).paragraphs)
        assert "does not bypass paywalls" in text

    def test_html_page_served_as_pdf_is_rejected(
        self, workspace_settings: Settings, fixtures_dir: Path
    ) -> None:
        with respx.mock(assert_all_called=False) as mock:
            _mock_all_apis(mock, fixtures_dir)
            html = (fixtures_dir / "paywall_page.pdf").read_bytes()
            mock.get("https://europepmc.org/delhi.pdf").mock(
                return_value=httpx.Response(
                    200, content=html, headers={"content-type": "application/pdf"}
                )
            )
            mock.get("https://europepmc.org/mumbai.pdf").mock(
                return_value=httpx.Response(
                    200, content=html, headers={"content-type": "application/pdf"}
                )
            )
            job = Job.create("Rainfall and travel", settings=workspace_settings,
                             maximum_papers=5)
            orchestrator = Orchestrator(job, settings=workspace_settings, enable_drive=False)
            orchestrator.run(
                stages=[StageName.KEYWORDS, StageName.SEARCH, StageName.DEDUPLICATE,
                        StageName.Q1_VERIFY, StageName.SELECT, StageName.DOWNLOAD]
            )

        # No HTML file may reach the Downloaded Papers folder.
        assert list(job.paths.downloaded_papers.glob("*.pdf")) == []
        assert all(
            r.download_status != DownloadStatus.DOWNLOADED
            for r in orchestrator.state.records
        )

    def test_partial_files_are_cleaned_up(
        self, workspace_settings: Settings, fixtures_dir: Path
    ) -> None:
        with respx.mock(assert_all_called=False) as mock:
            _mock_all_apis(mock, fixtures_dir)
            job = Job.create("Rainfall and travel", settings=workspace_settings,
                             maximum_papers=5)
            orchestrator = Orchestrator(job, settings=workspace_settings, enable_drive=False)
            orchestrator.run(
                stages=[StageName.KEYWORDS, StageName.SEARCH, StageName.DEDUPLICATE,
                        StageName.Q1_VERIFY, StageName.SELECT, StageName.DOWNLOAD]
            )
        assert list(job.paths.partial.glob("*.part")) == []


class TestScannedPdfHandling:
    """A scanned PDF must be flagged, never filled with invented text."""

    def test_scanned_pdf_is_flagged_for_ocr(
        self, workspace_settings: Settings, fixtures_dir: Path
    ) -> None:
        with respx.mock(assert_all_called=False) as mock:
            _mock_all_apis(mock, fixtures_dir)
            scanned = (fixtures_dir / "scanned_paper.pdf").read_bytes()
            mock.get("https://europepmc.org/delhi.pdf").mock(
                return_value=httpx.Response(
                    200, content=scanned, headers={"content-type": "application/pdf"}
                )
            )
            job = Job.create("Rainfall and travel", settings=workspace_settings,
                             maximum_papers=5)
            orchestrator = Orchestrator(job, settings=workspace_settings, enable_drive=False)
            orchestrator.run()

        ocr_records = [r for r in orchestrator.state.records if r.requires_ocr]
        assert ocr_records, "a scanned PDF must be flagged as needing OCR"
        for record in ocr_records:
            assert "OCR" in record.notes
            if record.extracted_text_path:
                content = Path(record.extracted_text_path).read_text(encoding="utf-8")
                assert "OCR required" in content
