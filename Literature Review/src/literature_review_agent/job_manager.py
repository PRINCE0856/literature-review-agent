"""Job lifecycle: folder layout, ``job_config.yaml``, and resumable checkpoints.

A *job* is one topic run on one date. Its state lives entirely on disk, which is
what makes the pipeline resumable: if the process dies during downloads, the next
run reads ``checkpoints.json``, sees which stages and which individual papers
already finished, and continues from there.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from .config import Settings, load_settings
from .logging_setup import get_logger
from .schemas import (
    STAGE_ORDER,
    JobCheckpoints,
    JobConfig,
    Q1Mode,
    StageName,
    StageStatus,
)
from .utils import (
    ensure_dir,
    read_json,
    slugify,
    stable_id,
    today_stamp,
    utc_now_iso,
    write_json,
)

LOG = get_logger("job")


class JobError(RuntimeError):
    """Raised when a job folder is missing, malformed, or unreadable."""


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JobPaths:
    """Every path a job stage might need, resolved once."""

    root: Path
    job_date: str
    topic_slug: str

    keywords: Path
    papers: Path
    reports: Path
    verification: Path
    logs: Path

    @property
    def downloaded_papers(self) -> Path:
        """Folder holding validated PDFs named after their paper titles."""
        return self.papers / "Downloaded Papers"

    @property
    def extracted_text(self) -> Path:
        """Folder holding per-paper extracted text files."""
        return self.papers / "Extracted Text"

    @property
    def unable_to_download(self) -> Path:
        """Folder holding the ``Unable_to_Download.docx`` register."""
        return self.papers / "Unable to Download"

    @property
    def partial(self) -> Path:
        """Scratch folder for ``.part`` files during downloads."""
        return self.papers / ".partial"

    @property
    def agent_workspace(self) -> Path:
        """Parent of the per-subagent scratch folders."""
        return self.logs / "agent_workspace"

    # -- named files ----------------------------------------------------

    @property
    def job_config_file(self) -> Path:
        """The job's saved configuration, including the untouched topic."""
        return self.logs / "job_config.yaml"

    @property
    def checkpoints_file(self) -> Path:
        """Stage-by-stage resume state."""
        return self.logs / "checkpoints.json"

    @property
    def pipeline_log(self) -> Path:
        """Human-readable run log."""
        return self.logs / "pipeline.log"

    @property
    def search_log(self) -> Path:
        """One JSON line per executed query."""
        return self.logs / "search_log.jsonl"

    @property
    def download_log(self) -> Path:
        """One JSON line per download attempt."""
        return self.logs / "download_log.jsonl"

    @property
    def keyword_strategy_file(self) -> Path:
        """Machine-readable keyword strategy (intermediate state)."""
        return self.logs / "keyword_strategy.json"

    @property
    def raw_search_results_file(self) -> Path:
        """All raw records returned by the adapters, before deduplication."""
        return self.logs / "raw_search_results.json"

    @property
    def records_file(self) -> Path:
        """Canonical deduplicated paper records — the pipeline's working set."""
        return self.logs / "paper_records.json"

    @property
    def analyses_file(self) -> Path:
        """Structured per-paper analyses."""
        return self.logs / "paper_analyses.json"

    @property
    def evidence_file(self) -> Path:
        """Evidence ledger records backing every synthesis claim."""
        return self.logs / "evidence_records.json"

    @property
    def findings_file(self) -> Path:
        """Verification findings from all independent verifiers."""
        return self.logs / "verification_findings.json"

    @property
    def synthesis_file(self) -> Path:
        """Gaps, model profiles, and landscape statistics."""
        return self.logs / "synthesis.json"

    @property
    def paper_manifest_csv(self) -> Path:
        """CSV manifest of every candidate paper."""
        return self.papers / "paper_manifest.csv"

    @property
    def paper_manifest_json(self) -> Path:
        """JSON manifest of every candidate paper."""
        return self.papers / "paper_manifest.json"

    @property
    def references_bib(self) -> Path:
        """BibTeX reference list for the included papers."""
        return self.papers / "references.bib"

    @property
    def references_ris(self) -> Path:
        """RIS reference list for the included papers."""
        return self.papers / "references.ris"

    def agent_dir(self, agent_name: str) -> Path:
        """Return (and create) a private scratch folder for one subagent.

        Each subagent gets its own directory so concurrent agents can never
        overwrite one another's intermediate output.
        """
        return ensure_dir(self.agent_workspace / agent_name)

    def all_dirs(self) -> list[Path]:
        """Every directory that a fresh job needs."""
        return [
            self.keywords,
            self.papers,
            self.downloaded_papers,
            self.extracted_text,
            self.unable_to_download,
            self.reports,
            self.verification,
            self.logs,
            self.agent_workspace,
        ]


# ---------------------------------------------------------------------------
# Job
# ---------------------------------------------------------------------------


class Job:
    """A single literature-review job: its config, paths, and checkpoints."""

    def __init__(self, config: JobConfig, paths: JobPaths, settings: Settings) -> None:
        self.config = config
        self.paths = paths
        self.settings = settings
        self._checkpoints: JobCheckpoints | None = None

    # -- construction ---------------------------------------------------

    @classmethod
    def create(
        cls,
        topic: str,
        *,
        settings: Settings | None = None,
        job_date: str | None = None,
        **overrides: Any,
    ) -> Job:
        """Create (or adopt) a job folder for *topic* on *job_date*.

        Re-running with the same topic and date deliberately reuses the existing
        folder — that is what makes ``resume`` work.
        """
        if not topic or not topic.strip():
            raise JobError("A research topic is required to create a job.")

        settings = settings or load_settings()
        stamp = job_date or today_stamp()
        slug = slugify(topic)
        config = build_job_config(topic, settings, job_date=stamp, **overrides)
        paths = build_job_paths(settings, stamp, slug, output_root=config.output_root)

        for directory in paths.all_dirs():
            ensure_dir(directory)

        job = cls(config, paths, settings)
        job.save_config()
        job.checkpoints  # materialise checkpoints.json
        LOG.info(f"Job ready: {paths.logs.parent.name}/{stamp}/{slug}")
        return job

    @classmethod
    def load(cls, job_path: Path | str, *, settings: Settings | None = None) -> Job:
        """Load an existing job from any of its folders or its config file.

        Accepts the ``05 Logs and State/<date>/<slug>`` folder, the
        ``job_config.yaml`` path itself, or any sibling job folder.
        """
        candidate = Path(job_path).expanduser()
        config_file = _locate_job_config(candidate)
        if config_file is None:
            raise JobError(
                f"Could not find job_config.yaml at or under {candidate}. "
                "Pass the job folder printed when the job was created."
            )

        raw = yaml.safe_load(config_file.read_text(encoding="utf-8")) or {}
        config = JobConfig.model_validate(raw)
        settings = settings or load_settings()
        paths = build_job_paths(
            settings,
            config.job_date,
            config.topic_slug,
            output_root=config.output_root,
        )
        for directory in paths.all_dirs():
            ensure_dir(directory)
        return cls(config, paths, settings)

    # -- persistence ----------------------------------------------------

    def save_config(self) -> Path:
        """Write ``job_config.yaml``, preserving the complete original topic."""
        payload = self.config.model_dump(mode="json")
        payload["_note"] = (
            "topic holds the complete original research topic; topic_slug is only "
            "the filesystem-safe folder name."
        )
        ensure_dir(self.paths.job_config_file.parent)
        self.paths.job_config_file.write_text(
            yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        return self.paths.job_config_file

    @property
    def checkpoints(self) -> JobCheckpoints:
        """Lazily loaded checkpoint state, created on first access."""
        if self._checkpoints is None:
            raw = read_json(self.paths.checkpoints_file)
            if raw:
                try:
                    self._checkpoints = JobCheckpoints.model_validate(raw)
                except Exception as exc:  # corrupted checkpoint file
                    LOG.warning(f"Checkpoint file unreadable ({exc}); starting a fresh one.")
                    self._checkpoints = self._fresh_checkpoints()
            else:
                self._checkpoints = self._fresh_checkpoints()
            self.save_checkpoints()
        return self._checkpoints

    def _fresh_checkpoints(self) -> JobCheckpoints:
        """Build an all-pending checkpoint set for this job."""
        checkpoints = JobCheckpoints(
            job_id=self.config.job_id,
            topic=self.config.topic,
            job_date=self.config.job_date,
        )
        for stage in STAGE_ORDER:
            checkpoints.stage(stage)
        return checkpoints

    def save_checkpoints(self) -> Path:
        """Persist checkpoint state atomically."""
        if self._checkpoints is None:
            return self.paths.checkpoints_file
        self._checkpoints.updated_at = utc_now_iso()
        return write_json(
            self.paths.checkpoints_file, self._checkpoints.model_dump(mode="json")
        )

    # -- stage control --------------------------------------------------

    def stage_status(self, stage: StageName) -> StageStatus:
        """Current status of *stage*."""
        return self.checkpoints.stage(stage).status

    def is_stage_complete(self, stage: StageName) -> bool:
        """True when *stage* completed in this or an earlier run."""
        return self.checkpoints.is_complete(stage)

    def start_stage(self, stage: StageName) -> None:
        """Mark *stage* as running and increment its attempt counter."""
        checkpoint = self.checkpoints.stage(stage)
        checkpoint.status = StageStatus.RUNNING
        checkpoint.started_at = utc_now_iso()
        checkpoint.attempts += 1
        checkpoint.message = ""
        self.save_checkpoints()

    def complete_stage(
        self,
        stage: StageName,
        *,
        message: str = "",
        artefacts: list[Path] | None = None,
        counters: dict[str, int] | None = None,
    ) -> None:
        """Mark *stage* complete and record what it produced."""
        checkpoint = self.checkpoints.stage(stage)
        checkpoint.status = StageStatus.COMPLETE
        checkpoint.finished_at = utc_now_iso()
        checkpoint.message = message
        if artefacts:
            checkpoint.artefacts = [str(p) for p in artefacts]
        if counters:
            checkpoint.counters.update(counters)
        self.save_checkpoints()

    def fail_stage(self, stage: StageName, message: str) -> None:
        """Mark *stage* failed, keeping any item-level progress for resume."""
        checkpoint = self.checkpoints.stage(stage)
        checkpoint.status = StageStatus.FAILED
        checkpoint.finished_at = utc_now_iso()
        checkpoint.message = message[:2000]
        self.save_checkpoints()

    def skip_stage(self, stage: StageName, message: str) -> None:
        """Mark *stage* skipped (for example, nothing to download)."""
        checkpoint = self.checkpoints.stage(stage)
        checkpoint.status = StageStatus.SKIPPED
        checkpoint.finished_at = utc_now_iso()
        checkpoint.message = message
        self.save_checkpoints()

    def reset_stage(self, stage: StageName, *, cascade: bool = True) -> None:
        """Reset *stage* (and by default all later stages) to pending."""
        stages = STAGE_ORDER[STAGE_ORDER.index(stage) :] if cascade else (stage,)
        for name in stages:
            checkpoint = self.checkpoints.stage(name)
            checkpoint.status = StageStatus.PENDING
            checkpoint.started_at = None
            checkpoint.finished_at = None
            checkpoint.message = ""
            checkpoint.completed_items = []
        self.save_checkpoints()

    # -- item-level progress -------------------------------------------

    def mark_item_done(self, stage: StageName, item_id: str) -> None:
        """Record that one work item within *stage* finished.

        This is what lets a download or extraction stage resume in the middle of
        a list of 50 papers instead of starting over.
        """
        checkpoint = self.checkpoints.stage(stage)
        if item_id not in checkpoint.completed_items:
            checkpoint.completed_items.append(item_id)
            self.save_checkpoints()

    def done_items(self, stage: StageName) -> set[str]:
        """Work items already completed within *stage*."""
        return set(self.checkpoints.stage(stage).completed_items)

    def next_stage(self) -> StageName | None:
        """The first stage that is not complete or skipped, or ``None``."""
        for stage in STAGE_ORDER:
            status = self.stage_status(stage)
            if status not in (StageStatus.COMPLETE, StageStatus.SKIPPED):
                return stage
        return None

    # -- convenience ----------------------------------------------------

    @property
    def q1_mode(self) -> Q1Mode:
        """The job's Q1 strictness setting."""
        return self.config.q1_mode

    def describe(self) -> dict[str, Any]:
        """A compact dict summary for the CLI ``status`` command."""
        return {
            "topic": self.config.topic,
            "topic_slug": self.config.topic_slug,
            "job_date": self.config.job_date,
            "job_id": self.config.job_id,
            "job_folder": str(self.paths.logs),
            "stages": {
                name: {
                    "status": cp.status.value,
                    "attempts": cp.attempts,
                    "items_done": len(cp.completed_items),
                    "message": cp.message,
                }
                for name, cp in self.checkpoints.stages.items()
            },
            "next_stage": (self.next_stage().value if self.next_stage() else None),
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def build_job_paths(
    settings: Settings,
    job_date: str,
    topic_slug: str,
    *,
    output_root: str | Path = ".",
) -> JobPaths:
    """Compose the date/topic-partitioned paths for one job."""
    root = Path(output_root)
    if not root.is_absolute():
        root = (settings.root / root).resolve()
    names = settings.paths
    suffix = Path(job_date) / topic_slug
    return JobPaths(
        root=root,
        job_date=job_date,
        topic_slug=topic_slug,
        keywords=root / names.get("keywords_dir", "01 Keywords") / suffix,
        papers=root / names.get("papers_dir", "02 Literature Papers") / suffix,
        reports=root / names.get("reports_dir", "03 Reports") / suffix,
        verification=root / names.get("verification_dir", "04 Verification") / suffix,
        logs=root / names.get("logs_dir", "05 Logs and State") / suffix,
    )


def build_job_config(
    topic: str,
    settings: Settings,
    *,
    job_date: str | None = None,
    **overrides: Any,
) -> JobConfig:
    """Merge user input with configured defaults into a :class:`JobConfig`.

    Any default that had to be assumed is recorded in ``assumptions`` so the
    final completion message can state exactly what was inferred.
    """
    defaults = dict(settings.job_defaults)
    stamp = job_date or today_stamp()
    supplied = {k: v for k, v in overrides.items() if v not in (None, [], "")}

    assumptions: list[str] = []

    def pick(key: str, default_key: str | None = None, fallback: Any = None) -> Any:
        """Take the user value, else the configured default, recording which."""
        if key in supplied:
            return supplied[key]
        default_value = defaults.get(default_key or key, fallback)
        if default_value is not None:
            assumptions.append(f"{key} not supplied; used default {default_value!r}")
        return default_value

    year_to = pick("year_to", fallback=date.today().year)
    payload: dict[str, Any] = {
        "topic": topic.strip(),
        "topic_slug": slugify(topic),
        "job_date": stamp,
        "job_id": stable_id(topic.strip().lower(), stamp),
        "research_questions": supplied.get("research_questions", []),
        "year_from": pick("year_from", fallback=2015),
        "year_to": year_to,
        "maximum_papers": pick("maximum_papers", fallback=50),
        "q1_mode": pick("q1_mode", fallback="preferred"),
        "geography": pick("geography", fallback="global"),
        "language": pick("language", fallback="English"),
        "paper_types": pick("paper_types", fallback=["journal article"]),
        "user_keywords": supplied.get("user_keywords", []),
        "exclusion_terms": supplied.get("exclusion_terms", []),
        "citation_style": pick("citation_style", fallback="APA 7"),
        "output_root": supplied.get("output_root", "."),
        "download_only_legal_and_authorized_content": defaults.get(
            "download_only_legal_and_authorized_content", True
        ),
        "enabled_sources": supplied.get(
            "enabled_sources", sorted(settings.available_sources())
        ),
        "ranking_file": supplied.get("ranking_file") or settings.q1_ranking.get("file"),
    }

    if not payload["research_questions"]:
        assumptions.append(
            "No explicit research question supplied; the topic itself was used as the "
            "guiding question."
        )
        payload["research_questions"] = [topic.strip()]

    if not payload["ranking_file"]:
        assumptions.append(
            "No licensed journal-ranking file configured; quartiles are reported as "
            "'Unverified' rather than guessed."
        )

    unavailable = settings.unavailable_sources()
    for name, reason in unavailable.items():
        assumptions.append(f"Source '{name}' skipped ({reason}).")

    payload["assumptions"] = assumptions
    return JobConfig.model_validate(payload)


def _locate_job_config(candidate: Path) -> Path | None:
    """Find ``job_config.yaml`` from a file, its folder, or a sibling folder."""
    if candidate.is_file() and candidate.name == "job_config.yaml":
        return candidate
    if candidate.is_dir():
        direct = candidate / "job_config.yaml"
        if direct.exists():
            return direct
        nested = sorted(candidate.rglob("job_config.yaml"))
        if nested:
            return nested[0]
        # The user may have passed e.g. "03 Reports/<date>/<slug>"; look for the
        # matching logs folder by walking up to the output root.
        slug = candidate.name
        stamp = candidate.parent.name
        for parent in candidate.parents:
            probe = parent / "05 Logs and State" / stamp / slug / "job_config.yaml"
            if probe.exists():
                return probe
    return None


def list_jobs(settings: Settings | None = None) -> list[dict[str, Any]]:
    """Return a summary of every job found under ``05 Logs and State``."""
    settings = settings or load_settings()
    logs_root = settings.root / settings.paths.get("logs_dir", "05 Logs and State")
    jobs: list[dict[str, Any]] = []
    if not logs_root.exists():
        return jobs
    for config_file in sorted(logs_root.rglob("job_config.yaml")):
        try:
            raw = yaml.safe_load(config_file.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            continue
        checkpoints = read_json(config_file.parent / "checkpoints.json", {}) or {}
        stages = checkpoints.get("stages", {})
        complete = sum(
            1 for cp in stages.values() if cp.get("status") in ("complete", "skipped")
        )
        jobs.append(
            {
                "topic": raw.get("topic", ""),
                "job_date": raw.get("job_date", ""),
                "topic_slug": raw.get("topic_slug", ""),
                "job_folder": str(config_file.parent),
                "stages_complete": f"{complete}/{len(STAGE_ORDER)}",
            }
        )
    return jobs
