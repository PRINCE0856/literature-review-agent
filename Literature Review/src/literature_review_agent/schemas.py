"""Pydantic models for every structured record the pipeline produces.

These models are the contract between the Python pipeline and the Claude Code
subagents: each subagent reads and writes JSON that validates against the models
in this module, so the orchestrator can trust the shape of what comes back.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .utils import (
    coerce_year,
    normalize_doi,
    normalize_issn,
    normalize_title,
    utc_now_iso,
)


# ---------------------------------------------------------------------------
# Controlled vocabularies
# ---------------------------------------------------------------------------


class EvidenceStance(str, Enum):
    """How a piece of information in a paper analysis was established.

    The pipeline never blurs these four states: an inference is never written as
    if the authors had said it, and an absent fact is never filled in.
    """

    AUTHOR_STATED = "Author explicitly states this"
    AGENT_INFERENCE = "Agent inference based on evidence"
    NOT_REPORTED = "Information not reported"
    UNVERIFIED = "Information could not be verified"


class Q1Status(str, Enum):
    """The only permitted journal-quartile verification states."""

    VERIFIED_Q1 = "Verified Q1"
    VERIFIED_NON_Q1 = "Verified non-Q1"
    UNVERIFIED = "Unverified"
    CONFLICTING = "Conflicting information"
    NOT_APPLICABLE = "Not applicable"


class Q1Mode(str, Enum):
    """How strictly Q1 status gates inclusion."""

    ONLY = "only"
    PREFERRED = "preferred"
    IGNORE = "ignore"


class DownloadStatus(str, Enum):
    """Lifecycle of a PDF retrieval attempt."""

    NOT_ATTEMPTED = "Not attempted"
    DOWNLOADED = "Downloaded"
    FAILED = "Failed"
    SKIPPED_NO_LEGAL_URL = "Skipped - no authorised open-access PDF URL"
    SKIPPED_NOT_SELECTED = "Skipped - not selected for inclusion"
    ALREADY_PRESENT = "Already present"


class CitationStyle(str, Enum):
    """Supported reference formatting styles."""

    APA7 = "APA 7"
    HARVARD = "Harvard"
    IEEE = "IEEE"
    VANCOUVER = "Vancouver"
    CHICAGO = "Chicago 17"


class CheckOutcome(str, Enum):
    """Result of one independent verification check."""

    PASS = "Pass"
    WARNING = "Warning"
    FAIL = "Fail"
    NOT_APPLICABLE = "Not applicable"


class GapCategory(str, Enum):
    """Research-gap taxonomy used by ``Research_Gaps.docx``."""

    AUTHOR_STATED = "Author-stated gaps"
    METHODOLOGICAL = "Methodological gaps"
    DATA = "Data gaps"
    GEOGRAPHIC = "Geographic gaps"
    POPULATION = "Population or sample gaps"
    MODEL_LIMITATION = "Model limitations"
    VALIDATION = "Validation gaps"
    APPLICATION = "Application gaps"
    POLICY = "Policy gaps"
    CONTRADICTORY = "Contradictory findings"
    AGENT_INFERRED = "Agent-inferred gaps"


class StageName(str, Enum):
    """Resumable pipeline stages, in execution order."""

    KEYWORDS = "keywords"
    SEARCH = "search"
    DEDUPLICATE = "deduplicate"
    Q1_VERIFY = "q1_verify"
    SELECT = "select"
    DOWNLOAD = "download"
    EXTRACT = "extract"
    ANALYSE = "analyse"
    VERIFY_EVIDENCE = "verify_evidence"
    REPORT = "report"
    FINAL_VERIFY = "final_verify"


STAGE_ORDER: tuple[StageName, ...] = (
    StageName.KEYWORDS,
    StageName.SEARCH,
    StageName.DEDUPLICATE,
    StageName.Q1_VERIFY,
    StageName.SELECT,
    StageName.DOWNLOAD,
    StageName.EXTRACT,
    StageName.ANALYSE,
    StageName.VERIFY_EVIDENCE,
    StageName.REPORT,
    StageName.FINAL_VERIFY,
)


class StageStatus(str, Enum):
    """Checkpoint state for a single stage."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    SKIPPED = "skipped"


# ---------------------------------------------------------------------------
# Base model
# ---------------------------------------------------------------------------


class StrictModel(BaseModel):
    """Base model with the settings every record in this package shares."""

    model_config = ConfigDict(
        extra="allow",  # tolerate richer payloads from evolving APIs
        use_enum_values=False,
        validate_assignment=True,
        str_strip_whitespace=True,
    )


# ---------------------------------------------------------------------------
# Job configuration
# ---------------------------------------------------------------------------


class JobConfig(StrictModel):
    """Everything needed to reproduce or resume one literature-review job."""

    topic: str = Field(description="The complete, unmodified research topic from the user.")
    topic_slug: str = Field(description="Filesystem-safe folder name derived from the topic.")
    job_date: str = Field(description="YYYY-MM-DD date partition for this job's folders.")
    job_id: str = Field(description="Short deterministic id for logs and cross-references.")

    research_questions: list[str] = Field(default_factory=list)
    year_from: int = 2015
    year_to: int = 2026
    maximum_papers: int = 50
    q1_mode: Q1Mode = Q1Mode.PREFERRED
    geography: str = "global"
    language: str = "English"
    paper_types: list[str] = Field(default_factory=lambda: ["journal article"])
    user_keywords: list[str] = Field(default_factory=list)
    exclusion_terms: list[str] = Field(default_factory=list)
    citation_style: CitationStyle = CitationStyle.APA7
    output_root: str = "."
    download_only_legal_and_authorized_content: bool = True

    enabled_sources: list[str] = Field(default_factory=list)
    ranking_file: str | None = None
    assumptions: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=utc_now_iso)
    agent_version: str = "1.0.0"

    @field_validator("year_to")
    @classmethod
    def _year_to_not_before_year_from(cls, value: int, info: Any) -> int:
        year_from = info.data.get("year_from")
        if year_from is not None and value < year_from:
            raise ValueError(f"year_to ({value}) cannot be before year_from ({year_from})")
        return value

    @field_validator("maximum_papers")
    @classmethod
    def _positive_max(cls, value: int) -> int:
        if value < 1:
            raise ValueError("maximum_papers must be at least 1")
        return value


# ---------------------------------------------------------------------------
# Keyword strategy
# ---------------------------------------------------------------------------


class TermProvenance(str, Enum):
    """Whether a search term came from the user or was generated by the agent."""

    USER = "user-supplied"
    AGENT = "agent-generated"


class KeywordTerm(StrictModel):
    """One search term plus where it came from and which concept it serves."""

    term: str
    category: str
    concept: str = ""
    provenance: TermProvenance = TermProvenance.AGENT
    rationale: str = ""


class SearchString(StrictModel):
    """A database-ready Boolean query string."""

    database: str
    breadth: str = Field(default="balanced", description="broad | balanced | narrow")
    query: str
    notes: str = ""


class InclusionCriteria(StrictModel):
    """Preliminary screening rules for the job."""

    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)


class KeywordStrategy(StrictModel):
    """Complete output of the keyword-strategy stage."""

    topic: str
    main_concepts: list[str] = Field(default_factory=list)
    terms: list[KeywordTerm] = Field(default_factory=list)
    search_strings: list[SearchString] = Field(default_factory=list)
    criteria: InclusionCriteria = Field(default_factory=InclusionCriteria)
    generator: str = "deterministic"
    created_at: str = Field(default_factory=utc_now_iso)

    def terms_in(self, category: str) -> list[str]:
        """Return the terms belonging to *category*, preserving order."""
        return [t.term for t in self.terms if t.category == category]

    def query_terms(self) -> list[str]:
        """Return the de-duplicated positive terms usable as API queries."""
        seen: set[str] = set()
        out: list[str] = []
        for term in self.terms:
            if term.category == "exclusion":
                continue
            key = term.term.lower()
            if key not in seen:
                seen.add(key)
                out.append(term.term)
        return out


# ---------------------------------------------------------------------------
# Papers
# ---------------------------------------------------------------------------


class DownloadAttempt(StrictModel):
    """One recorded PDF retrieval attempt, successful or not."""

    url: str
    http_status: int | None = None
    content_type: str | None = None
    outcome: str = ""
    error: str | None = None
    bytes_received: int | None = None
    attempted_at: str = Field(default_factory=utc_now_iso)


class Q1Verification(StrictModel):
    """Journal-quartile evidence for one paper. Never guessed, only sourced."""

    journal_name: str = ""
    issn: str | None = None
    eissn: str | None = None
    publication_year: int | None = None
    ranking_year: int | None = None
    subject_category: str | None = None
    quartile: str | None = None
    ranking_source: str | None = None
    verification_date: str | None = None
    verification_status: Q1Status = Q1Status.UNVERIFIED
    notes: str = ""
    matched_on: str | None = None


class PaperRecord(StrictModel):
    """The canonical bibliographic + workflow record for one candidate paper."""

    record_id: str = ""

    # --- core bibliographic metadata ---
    title: str = ""
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    journal: str = ""
    volume: str | None = None
    issue: str | None = None
    pages: str | None = None
    article_number: str | None = None
    doi: str | None = None
    issn: str | None = None
    eissn: str | None = None
    publisher: str | None = None
    abstract: str = ""
    keywords: list[str] = Field(default_factory=list)
    document_type: str | None = None
    language: str | None = None

    # --- bibliometrics ---
    citation_count: int | None = None
    citation_count_retrieved: str | None = None

    # --- access ---
    landing_page_url: str | None = None
    open_access_status: str | None = None
    licence: str | None = None
    pdf_url: str | None = None
    candidate_pdf_urls: list[str] = Field(default_factory=list)

    # --- provenance ---
    discovery_source: str = ""
    discovery_query: str = ""
    metadata_sources: list[str] = Field(default_factory=list)
    external_ids: dict[str, str] = Field(default_factory=dict)
    merged_from: list[str] = Field(default_factory=list)
    merge_audit: list[str] = Field(default_factory=list)

    # --- scoring / selection ---
    relevance_score: float = 0.0
    relevance_reasons: list[str] = Field(default_factory=list)
    selected: bool = False
    selection_reason: str = ""
    pending_q1_verification: bool = False

    # --- download ---
    download_status: DownloadStatus = DownloadStatus.NOT_ATTEMPTED
    local_filename: str | None = None
    local_path: str | None = None
    file_sha256: str | None = None
    file_bytes: int | None = None
    download_source_url: str | None = None
    download_attempts: list[DownloadAttempt] = Field(default_factory=list)
    failure_reason: str | None = None

    # --- text extraction ---
    extracted_text_path: str | None = None
    extracted_pages: int | None = None
    extracted_characters: int | None = None
    requires_ocr: bool = False

    # --- verification ---
    q1: Q1Verification = Field(default_factory=Q1Verification)
    verification_confidence: str = "Not assessed"
    notes: str = ""

    @field_validator("doi", mode="before")
    @classmethod
    def _clean_doi(cls, value: Any) -> str | None:
        return normalize_doi(value) if value else None

    @field_validator("issn", "eissn", mode="before")
    @classmethod
    def _clean_issn(cls, value: Any) -> str | None:
        return normalize_issn(value) if value else None

    @field_validator("year", mode="before")
    @classmethod
    def _clean_year(cls, value: Any) -> int | None:
        return coerce_year(value)

    @field_validator("authors", mode="before")
    @classmethod
    def _clean_authors(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [v.strip() for v in value.split(";") if v.strip()]
        return [str(v).strip() for v in value if str(v).strip()]

    # -- derived helpers -------------------------------------------------

    @property
    def normalized_title(self) -> str:
        """Title normalised for comparison and deduplication."""
        return normalize_title(self.title)

    @property
    def first_author(self) -> str | None:
        """The first author string as recorded, or ``None``."""
        return self.authors[0] if self.authors else None

    def dedup_key(self) -> str:
        """Best available identity key: DOI if present, else normalised title."""
        return self.doi or self.normalized_title or (self.record_id or "unknown")


# ---------------------------------------------------------------------------
# Paper analysis
# ---------------------------------------------------------------------------


class EvidenceField(StrictModel):
    """One analysed field with its evidence, stance, and confidence.

    ``stance`` is what keeps the reports honest: a value is only presented as the
    authors' own when ``stance`` is ``AUTHOR_STATED``.
    """

    value: str = ""
    stance: EvidenceStance = EvidenceStance.NOT_REPORTED
    pages: list[int] = Field(default_factory=list)
    quote: str = ""
    confidence: str = "Low"

    @property
    def is_reported(self) -> bool:
        """True when the field carries usable, evidence-backed content."""
        return bool(self.value) and self.stance in (
            EvidenceStance.AUTHOR_STATED,
            EvidenceStance.AGENT_INFERENCE,
        )


#: Field names that every :class:`PaperAnalysis` carries, in report order.
ANALYSIS_FIELDS: tuple[str, ...] = (
    "research_problem",
    "research_objective",
    "research_questions",
    "hypotheses",
    "study_geography",
    "study_context",
    "study_design",
    "data_source",
    "sample_size",
    "unit_of_analysis",
    "variables",
    "dependent_variables",
    "independent_variables",
    "control_variables",
    "model_or_method",
    "model_equations",
    "software_or_tools",
    "validation_approach",
    "main_findings",
    "policy_implications",
    "limitations_stated",
    "gaps_stated_by_authors",
    "relevance_to_topic",
)


class PaperAnalysis(StrictModel):
    """Structured analysis of one downloaded paper, field by field."""

    record_id: str
    doi: str | None = None
    title: str = ""
    full_citation: str = ""

    research_problem: EvidenceField = Field(default_factory=EvidenceField)
    research_objective: EvidenceField = Field(default_factory=EvidenceField)
    research_questions: EvidenceField = Field(default_factory=EvidenceField)
    hypotheses: EvidenceField = Field(default_factory=EvidenceField)
    study_geography: EvidenceField = Field(default_factory=EvidenceField)
    study_context: EvidenceField = Field(default_factory=EvidenceField)
    study_design: EvidenceField = Field(default_factory=EvidenceField)
    data_source: EvidenceField = Field(default_factory=EvidenceField)
    sample_size: EvidenceField = Field(default_factory=EvidenceField)
    unit_of_analysis: EvidenceField = Field(default_factory=EvidenceField)
    variables: EvidenceField = Field(default_factory=EvidenceField)
    dependent_variables: EvidenceField = Field(default_factory=EvidenceField)
    independent_variables: EvidenceField = Field(default_factory=EvidenceField)
    control_variables: EvidenceField = Field(default_factory=EvidenceField)
    model_or_method: EvidenceField = Field(default_factory=EvidenceField)
    model_equations: EvidenceField = Field(default_factory=EvidenceField)
    software_or_tools: EvidenceField = Field(default_factory=EvidenceField)
    validation_approach: EvidenceField = Field(default_factory=EvidenceField)
    main_findings: EvidenceField = Field(default_factory=EvidenceField)
    policy_implications: EvidenceField = Field(default_factory=EvidenceField)
    limitations_stated: EvidenceField = Field(default_factory=EvidenceField)
    gaps_stated_by_authors: EvidenceField = Field(default_factory=EvidenceField)
    relevance_to_topic: EvidenceField = Field(default_factory=EvidenceField)

    agent_inferred_gap: EvidenceField = Field(default_factory=EvidenceField)
    detected_methods: list[str] = Field(default_factory=list)
    detected_countries: list[str] = Field(default_factory=list)
    detected_software: list[str] = Field(default_factory=list)

    overall_confidence: str = "Low"
    missing_information: list[str] = Field(default_factory=list)
    analysed_at: str = Field(default_factory=utc_now_iso)
    analyser: str = "deterministic"

    def field(self, name: str) -> EvidenceField:
        """Return the :class:`EvidenceField` called *name*."""
        value = getattr(self, name, None)
        return value if isinstance(value, EvidenceField) else EvidenceField()


# ---------------------------------------------------------------------------
# Evidence ledger and citations
# ---------------------------------------------------------------------------


class EvidenceRecord(StrictModel):
    """One claim in a synthesis report tied back to its source evidence."""

    evidence_id: str
    document: str
    section: str
    claim: str
    record_id: str
    doi: str | None = None
    in_text_citation: str = ""
    field_name: str = ""
    stance: EvidenceStance = EvidenceStance.AUTHOR_STATED
    pages: list[int] = Field(default_factory=list)
    supporting_text: str = ""
    confidence: str = "Medium"
    created_at: str = Field(default_factory=utc_now_iso)


class CitationAuditRow(StrictModel):
    """One row of the citation audit: does this citation check out?"""

    in_text_citation: str
    record_id: str
    doi: str | None = None
    title: str = ""
    appears_in_documents: list[str] = Field(default_factory=list)
    in_reference_list: bool = False
    reference_entry: str = ""
    metadata_checked: bool = False
    title_match: CheckOutcome = CheckOutcome.NOT_APPLICABLE
    author_match: CheckOutcome = CheckOutcome.NOT_APPLICABLE
    year_match: CheckOutcome = CheckOutcome.NOT_APPLICABLE
    journal_match: CheckOutcome = CheckOutcome.NOT_APPLICABLE
    doi_resolves: CheckOutcome = CheckOutcome.NOT_APPLICABLE
    outcome: CheckOutcome = CheckOutcome.PASS
    notes: str = ""


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


class VerificationFinding(StrictModel):
    """One independent check, its outcome, and what should happen next.

    Questionable data is never silently rewritten: ``original_value`` is always
    preserved next to any ``corrected_value``, with the source and reason.
    """

    finding_id: str
    verifier: str
    check: str
    target: str
    record_id: str | None = None
    outcome: CheckOutcome = CheckOutcome.PASS
    detail: str = ""
    original_value: str = ""
    corrected_value: str = ""
    correction_source: str = ""
    correction_reason: str = ""
    recommended_action: str = ""
    resolved: bool = False
    created_at: str = Field(default_factory=utc_now_iso)


class VerificationSummary(StrictModel):
    """Counts and confidence for ``Verification_Report.docx``."""

    total_discovered: int = 0
    total_unique: int = 0
    total_included: int = 0
    total_verified_q1: int = 0
    total_unverified_quartile: int = 0
    total_pdfs_downloaded: int = 0
    total_failed_downloads: int = 0
    total_papers_analysed: int = 0
    total_claims_checked: int = 0
    passed_checks: int = 0
    warnings: int = 0
    unresolved_problems: int = 0
    recommended_manual_checks: list[str] = Field(default_factory=list)
    overall_confidence: str = "Low"
    generated_at: str = Field(default_factory=utc_now_iso)


# ---------------------------------------------------------------------------
# Synthesis
# ---------------------------------------------------------------------------


class GapItem(StrictModel):
    """One research gap, categorised and cited."""

    gap_id: str
    category: GapCategory
    statement: str
    supporting_record_ids: list[str] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    stance: EvidenceStance = EvidenceStance.AUTHOR_STATED


class ModelProfile(StrictModel):
    """One model or method found across the reviewed evidence."""

    model_config = ConfigDict(protected_namespaces=())

    model_name: str
    model_category: str = "Not classified"
    purpose: str = ""
    assumptions: list[str] = Field(default_factory=list)
    required_inputs: list[str] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)
    study_application: list[str] = Field(default_factory=list)
    software_used: list[str] = Field(default_factory=list)
    calibration_approach: list[str] = Field(default_factory=list)
    validation_approach: list[str] = Field(default_factory=list)
    advantages: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    paper_record_ids: list[str] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)
    plain_explanation: str = ""


class LandscapeSummary(StrictModel):
    """Descriptive statistics of the reviewed evidence base."""

    countries: dict[str, int] = Field(default_factory=dict)
    regions: dict[str, int] = Field(default_factory=dict)
    institutions: dict[str, int] = Field(default_factory=dict)
    applications: dict[str, int] = Field(default_factory=dict)
    datasets: dict[str, int] = Field(default_factory=dict)
    methods: dict[str, int] = Field(default_factory=dict)
    emerging_methods: list[str] = Field(default_factory=list)
    year_counts: dict[str, int] = Field(default_factory=dict)
    journals: dict[str, int] = Field(default_factory=dict)
    under_researched: list[str] = Field(default_factory=list)
    total_papers: int = 0


# ---------------------------------------------------------------------------
# Checkpoints
# ---------------------------------------------------------------------------


class StageCheckpoint(StrictModel):
    """Persistent state for one resumable stage."""

    stage: StageName
    status: StageStatus = StageStatus.PENDING
    started_at: str | None = None
    finished_at: str | None = None
    attempts: int = 0
    message: str = ""
    artefacts: list[str] = Field(default_factory=list)
    counters: dict[str, int] = Field(default_factory=dict)
    #: Item-level progress, so a stage can resume mid-way through a work list.
    completed_items: list[str] = Field(default_factory=list)


class JobCheckpoints(StrictModel):
    """The whole checkpoint file: one entry per stage plus job identity."""

    job_id: str
    topic: str
    job_date: str
    stages: dict[str, StageCheckpoint] = Field(default_factory=dict)
    updated_at: str = Field(default_factory=utc_now_iso)
    schema_version: int = 1

    def stage(self, name: StageName) -> StageCheckpoint:
        """Return (creating if needed) the checkpoint for stage *name*."""
        key = name.value
        if key not in self.stages:
            self.stages[key] = StageCheckpoint(stage=name)
        return self.stages[key]

    def is_complete(self, name: StageName) -> bool:
        """True when stage *name* finished successfully in an earlier run."""
        return self.stage(name).status == StageStatus.COMPLETE
