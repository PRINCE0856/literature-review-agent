"""Synthesis: gaps, model profiles, and the landscape of the reviewed evidence.

Everything here is derived from verified :class:`PaperAnalysis` records and is
written into the evidence ledger as it is produced, so each synthesis statement
carries its supporting papers, pages, and evidence stance.

The language is deliberately bounded: statistics describe *the reviewed
evidence*, never world research. A count of five Indian studies is reported as
five studies in this review, not as India dominating the field.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from .analysis import METHOD_VOCABULARY, method_category, region_for
from .citation_manager import CitationManager
from .evidence_ledger import EvidenceLedger
from .logging_setup import get_logger
from .schemas import (
    ANALYSIS_FIELDS,
    EvidenceStance,
    GapCategory,
    GapItem,
    JobConfig,
    LandscapeSummary,
    ModelProfile,
    PaperAnalysis,
    PaperRecord,
)
from .utils import stable_id, truncate_text

LOG = get_logger("synthesis")


@dataclass
class SynthesisResult:
    """The synthesis products used by the Word reports and the Excel matrix."""

    gaps: list[GapItem] = field(default_factory=list)
    models: list[ModelProfile] = field(default_factory=list)
    landscape: LandscapeSummary = field(default_factory=LandscapeSummary)
    contradictions: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Research gaps
# ---------------------------------------------------------------------------

#: Cue words mapping an author-stated gap to a more specific category.
GAP_CATEGORY_CUES: tuple[tuple[GapCategory, tuple[str, ...]], ...] = (
    (GapCategory.DATA, ("data", "dataset", "measurement", "sample of", "coverage",
                        "resolution", "availability of data", "self-reported")),
    (GapCategory.METHODOLOGICAL, ("method", "methodolog", "identification", "endogeneity",
                                  "causal", "estimation", "specification", "bias")),
    (GapCategory.GEOGRAPHIC, ("other cities", "other countries", "other regions",
                              "generalis", "generaliz", "transferab", "context-specific",
                              "single city", "one country")),
    (GapCategory.POPULATION, ("population", "sample", "respondents", "subgroup",
                              "demographic", "gender", "low-income", "cohort")),
    (GapCategory.MODEL_LIMITATION, ("model", "assumption", "simplif", "linear",
                                    "does not capture", "cannot account")),
    (GapCategory.VALIDATION, ("validat", "robustness", "out-of-sample", "sensitivity",
                              "calibrat", "external validity")),
    (GapCategory.APPLICATION, ("application", "practice", "implement", "deploy",
                               "operational", "real-world")),
    (GapCategory.POLICY, ("policy", "regulation", "governance", "planning",
                          "cost-effectiveness", "policymakers")),
)


def _categorise_gap(text: str) -> GapCategory:
    """Assign a specific category to an author-stated gap statement."""
    lowered = (text or "").lower()
    scores: list[tuple[int, GapCategory]] = []
    for category, cues in GAP_CATEGORY_CUES:
        hits = sum(1 for cue in cues if cue in lowered)
        if hits:
            scores.append((hits, category))
    if not scores:
        return GapCategory.AUTHOR_STATED
    scores.sort(key=lambda pair: -pair[0])
    return scores[0][1]


def build_gaps(
    records: list[PaperRecord],
    analyses: dict[str, PaperAnalysis],
    manager: CitationManager,
    ledger: EvidenceLedger,
    config: JobConfig,
) -> tuple[list[GapItem], list[str]]:
    """Derive categorised research gaps, each cited and ledger-backed.

    Author-stated gaps and agent-inferred gaps are kept strictly separate, which
    is what the Research Gaps document reports under different headings.
    """
    document = "Research_Gaps.docx"
    gaps: list[GapItem] = []
    contradictions: list[str] = []

    # --- 1. Author-stated gaps, categorised by their own wording ---
    for record in records:
        analysis = analyses.get(record.record_id)
        if analysis is None:
            continue
        stated = analysis.field("gaps_stated_by_authors")
        if not stated.is_reported:
            continue
        category = _categorise_gap(stated.value)
        citation = manager.citation(record.record_id)
        evidence = ledger.add(
            document=document,
            section=category.value,
            claim=stated.value,
            record_id=record.record_id,
            doi=record.doi,
            in_text_citation=citation,
            field_name="gaps_stated_by_authors",
            stance=stated.stance,
            pages=stated.pages,
            supporting_text=stated.quote,
            confidence=stated.confidence,
        )
        gaps.append(
            GapItem(
                gap_id="GAP-" + stable_id(record.record_id, category.value, stated.value),
                category=category,
                statement=truncate_text(stated.value, 700),
                supporting_record_ids=[record.record_id],
                citations=[citation],
                evidence_ids=[evidence.evidence_id],
                stance=stated.stance,
            )
        )

    # --- 2. Author-stated limitations become model/validation gaps ---
    for record in records:
        analysis = analyses.get(record.record_id)
        if analysis is None:
            continue
        limitation = analysis.field("limitations_stated")
        if not limitation.is_reported:
            continue
        category = _categorise_gap(limitation.value)
        if category == GapCategory.AUTHOR_STATED:
            category = GapCategory.MODEL_LIMITATION
        citation = manager.citation(record.record_id)
        evidence = ledger.add(
            document=document,
            section=category.value,
            claim=limitation.value,
            record_id=record.record_id,
            doi=record.doi,
            in_text_citation=citation,
            field_name="limitations_stated",
            stance=limitation.stance,
            pages=limitation.pages,
            supporting_text=limitation.quote,
            confidence=limitation.confidence,
        )
        gaps.append(
            GapItem(
                gap_id="GAP-" + stable_id(record.record_id, "limitation", limitation.value),
                category=category,
                statement=truncate_text(limitation.value, 700),
                supporting_record_ids=[record.record_id],
                citations=[citation],
                evidence_ids=[evidence.evidence_id],
                stance=limitation.stance,
            )
        )

    analysed = [r for r in records if r.record_id in analyses]

    # --- 3. Aggregate gaps: what the reviewed set as a whole lacks ---
    if analysed:
        gaps.extend(
            _aggregate_gaps(analysed, analyses, manager, ledger, config, document)
        )

    # --- 4. Contradictory findings ---
    contradiction_gaps, contradictions = _find_contradictions(
        analysed, analyses, manager, ledger, document
    )
    gaps.extend(contradiction_gaps)

    # --- 5. Per-paper agent-inferred gaps ---
    for record in analysed:
        analysis = analyses[record.record_id]
        inferred = analysis.agent_inferred_gap
        if not inferred.is_reported:
            continue
        citation = manager.citation(record.record_id)
        evidence = ledger.add(
            document=document,
            section=GapCategory.AGENT_INFERRED.value,
            claim=inferred.value,
            record_id=record.record_id,
            doi=record.doi,
            in_text_citation=citation,
            field_name="agent_inferred_gap",
            stance=EvidenceStance.AGENT_INFERENCE,
            pages=inferred.pages,
            supporting_text=inferred.quote,
            confidence=inferred.confidence,
        )
        gaps.append(
            GapItem(
                gap_id="GAP-" + stable_id(record.record_id, "inferred"),
                category=GapCategory.AGENT_INFERRED,
                statement=truncate_text(inferred.value, 700),
                supporting_record_ids=[record.record_id],
                citations=[citation],
                evidence_ids=[evidence.evidence_id],
                stance=EvidenceStance.AGENT_INFERENCE,
            )
        )

    LOG.info(f"Synthesis identified {len(gaps)} gap statement(s) across all categories.")
    return gaps, contradictions


def _aggregate_gaps(
    records: list[PaperRecord],
    analyses: dict[str, PaperAnalysis],
    manager: CitationManager,
    ledger: EvidenceLedger,
    config: JobConfig,
    document: str,
) -> list[GapItem]:
    """Derive gaps from coverage patterns across the whole reviewed set."""
    gaps: list[GapItem] = []
    total = len(records)

    def add_aggregate(
        category: GapCategory,
        statement: str,
        supporting: list[PaperRecord],
        field_name: str,
    ) -> None:
        """Record one aggregate gap with all its supporting citations."""
        if not supporting:
            return
        citations = [manager.citation(r.record_id) for r in supporting]
        evidence_ids: list[str] = []
        for record in supporting:
            evidence = ledger.add(
                document=document,
                section=category.value,
                claim=statement,
                record_id=record.record_id,
                doi=record.doi,
                in_text_citation=manager.citation(record.record_id),
                field_name=field_name,
                stance=EvidenceStance.AGENT_INFERENCE,
                pages=analyses[record.record_id].field(field_name).pages,
                supporting_text=analyses[record.record_id].field(field_name).quote,
                confidence="Medium",
            )
            evidence_ids.append(evidence.evidence_id)
        gaps.append(
            GapItem(
                gap_id="GAP-" + stable_id(category.value, statement),
                category=category,
                statement=statement,
                supporting_record_ids=[r.record_id for r in supporting],
                citations=citations,
                evidence_ids=evidence_ids,
                stance=EvidenceStance.AGENT_INFERENCE,
            )
        )

    # Validation gap: papers with a model but no reported validation.
    unvalidated = [
        r
        for r in records
        if analyses[r.record_id].field("model_or_method").is_reported
        and not analyses[r.record_id].field("validation_approach").is_reported
    ]
    if len(unvalidated) >= max(2, total // 4):
        add_aggregate(
            GapCategory.VALIDATION,
            (
                f"Within the reviewed evidence, {len(unvalidated)} of {total} papers "
                "specify a model or method without reporting a validation, "
                "out-of-sample test, or robustness strategy. Comparative confidence in "
                "their estimates therefore cannot be established from the papers "
                "themselves."
            ),
            unvalidated[:8],
            "validation_approach",
        )

    # Data gap: papers without a stated data source or sample size.
    thin_data = [
        r
        for r in records
        if not analyses[r.record_id].field("data_source").is_reported
        or not analyses[r.record_id].field("sample_size").is_reported
    ]
    if len(thin_data) >= max(2, total // 4):
        add_aggregate(
            GapCategory.DATA,
            (
                f"{len(thin_data)} of {total} reviewed papers do not report both a data "
                "source and a sample size, which limits replication and prevents the "
                "reviewed evidence from being pooled quantitatively."
            ),
            thin_data[:8],
            "data_source",
        )

    # Geographic concentration.
    country_counter: Counter[str] = Counter()
    country_papers: dict[str, list[PaperRecord]] = {}
    for record in records:
        for country in dict.fromkeys(analyses[record.record_id].detected_countries):
            country_counter[country] += 1
            country_papers.setdefault(country, []).append(record)
    if country_counter:
        top_country, top_count = country_counter.most_common(1)[0]
        if total >= 3 and top_count / total >= 0.4:
            add_aggregate(
                GapCategory.GEOGRAPHIC,
                (
                    f"The reviewed evidence is concentrated in {top_country}, which "
                    f"appears in {top_count} of {total} papers. Findings from other "
                    "contexts are under-represented in this review, so transferability "
                    "beyond the dominant setting is untested here."
                ),
                country_papers[top_country][:8],
                "study_geography",
            )
    elif total >= 3:
        add_aggregate(
            GapCategory.GEOGRAPHIC,
            (
                "No reviewed paper states its study geography explicitly enough to be "
                "extracted, so the geographic coverage of this evidence base cannot be "
                "characterised."
            ),
            records[:8],
            "study_geography",
        )

    # Policy gap.
    no_policy = [
        r for r in records if not analyses[r.record_id].field("policy_implications").is_reported
    ]
    if len(no_policy) >= max(2, total // 3):
        add_aggregate(
            GapCategory.POLICY,
            (
                f"{len(no_policy)} of {total} reviewed papers draw no policy or practice "
                "implications, so the reviewed evidence offers limited direct guidance "
                "for decision-makers."
            ),
            no_policy[:8],
            "policy_implications",
        )

    # Methodological concentration.
    method_counter: Counter[str] = Counter()
    method_papers: dict[str, list[PaperRecord]] = {}
    for record in records:
        for method in dict.fromkeys(analyses[record.record_id].detected_methods):
            method_counter[method] += 1
            method_papers.setdefault(method, []).append(record)
    if method_counter and total >= 3:
        top_method, top_count = method_counter.most_common(1)[0]
        if top_count / total >= 0.5:
            add_aggregate(
                GapCategory.METHODOLOGICAL,
                (
                    f"Method choice in the reviewed evidence is concentrated: "
                    f"{top_method} is used in {top_count} of {total} papers. Alternative "
                    "identification strategies are largely absent from this set, so "
                    "method-driven differences in the findings cannot be ruled out."
                ),
                method_papers[top_method][:8],
                "model_or_method",
            )

    # Application gap.
    no_application = [
        r
        for r in records
        if not analyses[r.record_id].field("study_context").is_reported
        and not analyses[r.record_id].field("policy_implications").is_reported
    ]
    if len(no_application) >= max(2, total // 3):
        add_aggregate(
            GapCategory.APPLICATION,
            (
                f"{len(no_application)} of {total} reviewed papers describe neither an "
                "applied study context nor practical implications, leaving the route "
                "from these results to application undocumented."
            ),
            no_application[:6],
            "study_context",
        )

    # Population gap.
    no_unit = [
        r for r in records if not analyses[r.record_id].field("unit_of_analysis").is_reported
    ]
    if len(no_unit) >= max(2, total // 3):
        add_aggregate(
            GapCategory.POPULATION,
            (
                f"{len(no_unit)} of {total} reviewed papers do not state their unit of "
                "analysis or study population, so results cannot be compared on a "
                "like-for-like basis across the evidence base."
            ),
            no_unit[:6],
            "unit_of_analysis",
        )

    return gaps


#: Direction words used to detect disagreement between papers.
_INCREASE_WORDS = ("increase", "increases", "increased", "higher", "positive",
                   "rise", "rises", "grew", "growth", "more likely", "improves")
_DECREASE_WORDS = ("decrease", "decreases", "decreased", "lower", "negative",
                   "reduce", "reduces", "reduced", "decline", "fell", "less likely",
                   "worsens")
_NULL_WORDS = ("no significant", "not significant", "no effect", "insignificant",
               "no association", "no relationship")


def _finding_direction(text: str) -> str | None:
    """Classify a findings statement as increase, decrease, or null."""
    lowered = (text or "").lower()
    if any(word in lowered for word in _NULL_WORDS):
        return "null"
    increase = sum(1 for word in _INCREASE_WORDS if word in lowered)
    decrease = sum(1 for word in _DECREASE_WORDS if word in lowered)
    if increase and increase > decrease:
        return "increase"
    if decrease and decrease > increase:
        return "decrease"
    return None


def _find_contradictions(
    records: list[PaperRecord],
    analyses: dict[str, PaperAnalysis],
    manager: CitationManager,
    ledger: EvidenceLedger,
    document: str,
) -> tuple[list[GapItem], list[str]]:
    """Detect papers whose reported directions of effect disagree."""
    grouped: dict[str, list[tuple[PaperRecord, str]]] = {}
    for record in records:
        analysis = analyses[record.record_id]
        findings = analysis.field("main_findings")
        if not findings.is_reported:
            continue
        direction = _finding_direction(findings.value)
        if direction is None:
            continue
        # Group by method so method-specific disagreement is visible, and always
        # also group across the whole reviewed set, so a disagreement between two
        # papers that used different methods is not missed.
        for method in [*dict.fromkeys(analysis.detected_methods), "the reviewed evidence"]:
            grouped.setdefault(method, []).append((record, direction))

    gaps: list[GapItem] = []
    summaries: list[str] = []
    for topic, entries in grouped.items():
        directions = {direction for _, direction in entries}
        if len(directions) < 2 or len(entries) < 2:
            continue
        citations = [manager.citation(r.record_id) for r, _ in entries]
        evidence_ids: list[str] = []
        detail_parts: list[str] = []
        for record, direction in entries:
            findings = analyses[record.record_id].field("main_findings")
            evidence = ledger.add(
                document=document,
                section=GapCategory.CONTRADICTORY.value,
                claim=findings.value,
                record_id=record.record_id,
                doi=record.doi,
                in_text_citation=manager.citation(record.record_id),
                field_name="main_findings",
                stance=findings.stance,
                pages=findings.pages,
                supporting_text=findings.quote,
                confidence=findings.confidence,
            )
            evidence_ids.append(evidence.evidence_id)
            detail_parts.append(
                f"{manager.citation(record.record_id)} reports a {direction} effect"
            )
        subject = (
            topic if topic == "the reviewed evidence" else f"papers using {topic}"
        )
        statement = (
            f"Within {subject}, the reported effects run in different "
            f"directions: {'; '.join(detail_parts)}. The papers themselves do not "
            "reconcile this, so the disagreement is reported rather than resolved."
        )
        summaries.append(statement)
        gaps.append(
            GapItem(
                gap_id="GAP-" + stable_id("contradiction", topic),
                category=GapCategory.CONTRADICTORY,
                statement=statement,
                supporting_record_ids=[r.record_id for r, _ in entries],
                citations=citations,
                evidence_ids=evidence_ids,
                stance=EvidenceStance.AGENT_INFERENCE,
            )
        )
    return gaps, summaries


# ---------------------------------------------------------------------------
# Model profiles
# ---------------------------------------------------------------------------

#: Plain-language explanations for recognised methods, by category.
CATEGORY_EXPLANATIONS: dict[str, str] = {
    "Discrete choice model": (
        "It represents a person choosing one option from a set. Each option is given a "
        "score built from its attributes, and the model estimates how much each "
        "attribute shifts the probability of that option being chosen."
    ),
    "Regression model": (
        "It fits a line or curve through the data to estimate how much an outcome "
        "changes when one factor changes while the others are held constant."
    ),
    "Panel data model": (
        "It follows the same units over several time periods, which lets differences "
        "between units be separated from changes over time within a unit."
    ),
    "Causal inference design": (
        "It compares groups or periods that differ only in the exposure of interest, so "
        "the difference between them can be attributed to that exposure."
    ),
    "Spatial model": (
        "It accounts for the fact that nearby places influence one another, so an "
        "estimate for one location partly reflects its neighbours."
    ),
    "Time series model": (
        "It uses a variable's own history to explain its present value and to project "
        "forward."
    ),
    "Machine learning": (
        "It learns patterns from a training portion of the data and is then judged on "
        "data it has not seen. It usually predicts well but does not by itself explain "
        "cause and effect."
    ),
    "Latent variable model": (
        "It infers groups or traits that are not directly observed from the pattern of "
        "the answers or behaviours that are observed."
    ),
    "Simulation model": (
        "It builds a computational world with explicit rules, runs it forward, and "
        "observes the aggregate outcomes those rules produce."
    ),
    "Optimisation model": (
        "It searches for the best combination of decisions subject to stated "
        "constraints, such as the lowest cost that still meets demand."
    ),
    "Decision analysis": (
        "It scores options against several weighted criteria so alternatives can be "
        "ranked when no single measure captures everything."
    ),
    "Economic appraisal": (
        "It converts the costs and benefits of an intervention into comparable money "
        "terms to judge whether it is worthwhile."
    ),
    "Environmental assessment": (
        "It traces the environmental burdens of a product or system across its life "
        "cycle to produce comparable impact totals."
    ),
    "Economic accounting": (
        "It traces flows between sectors of an economy so an effect in one sector can "
        "be followed through to the others."
    ),
    "Process-based model": (
        "It represents the underlying physical or engineering processes with equations, "
        "so behaviour emerges from the mechanism rather than from fitted correlations."
    ),
    "Evidence synthesis": (
        "It gathers existing studies and combines their results systematically rather "
        "than collecting new primary data."
    ),
    "Qualitative analysis": (
        "It codes text or interview material into themes to identify recurring "
        "meanings and explanations."
    ),
    "Network model": (
        "It represents the system as nodes and links and studies how flows distribute "
        "across that network."
    ),
    "Structural model": (
        "It estimates several linked relationships at once, allowing a factor to affect "
        "an outcome both directly and through intermediate variables."
    ),
    "Not classified": (
        "The reviewed papers name this approach without describing its mechanism in "
        "enough detail for a plain-language explanation to be given here."
    ),
}

#: Generic strengths and weaknesses by category, framed as observations about
#: the method family rather than claims attributed to any paper.
CATEGORY_TRADEOFFS: dict[str, tuple[list[str], list[str]]] = {
    "Discrete choice model": (
        ["Grounded in individual decision-making", "Yields interpretable trade-off ratios"],
        ["Needs disaggregate choice data", "Assumes a specified error structure"],
    ),
    "Regression model": (
        ["Transparent and widely understood", "Coefficients are directly interpretable"],
        ["Sensitive to omitted variables", "Assumes a specified functional form"],
    ),
    "Panel data model": (
        ["Controls for stable unobserved differences", "Separates within from between variation"],
        ["Requires repeated observations", "Vulnerable to attrition"],
    ),
    "Causal inference design": (
        ["Supports causal interpretation", "Testable identifying assumptions"],
        ["Depends on a credible comparison group", "Estimates apply to the studied setting"],
    ),
    "Machine learning": (
        ["Captures non-linear interactions", "Strong predictive accuracy"],
        ["Limited causal interpretation", "Needs large datasets and careful tuning"],
    ),
    "Simulation model": (
        ["Represents mechanisms explicitly", "Allows scenario experimentation"],
        ["Results depend on assumed rules", "Hard to validate against observation"],
    ),
    "Optimisation model": (
        ["Identifies best-case configurations", "Constraints are stated explicitly"],
        ["Assumes a single well-defined objective", "Sensitive to input cost assumptions"],
    ),
    "Process-based model": (
        ["Physically interpretable", "Transferable where processes hold"],
        ["Data-hungry to parameterise", "Computationally demanding"],
    ),
    "Evidence synthesis": (
        ["Aggregates across contexts", "Reduces single-study bias"],
        ["Limited by primary-study quality", "Vulnerable to publication bias"],
    ),
    "Time series model": (
        ["Captures temporal dependence", "Well suited to short-term projection"],
        ["Assumes the past pattern continues", "Weak for structural change"],
    ),
    "Spatial model": (
        ["Handles spatial dependence", "Reveals geographic variation"],
        ["Sensitive to the chosen spatial weights", "Boundary effects can bias estimates"],
    ),
    "Qualitative analysis": (
        ["Rich contextual explanation", "Surfaces mechanisms surveys miss"],
        ["Findings are not statistically generalisable", "Coding depends on the analyst"],
    ),
}


def build_model_profiles(
    records: list[PaperRecord],
    analyses: dict[str, PaperAnalysis],
    manager: CitationManager,
    ledger: EvidenceLedger,
) -> list[ModelProfile]:
    """Build one profile per model or method found across the reviewed papers."""
    document = "Models_and_Applications.docx"
    grouped: dict[str, list[PaperRecord]] = {}
    for record in records:
        analysis = analyses.get(record.record_id)
        if analysis is None:
            continue
        for method in dict.fromkeys(analysis.detected_methods):
            grouped.setdefault(method, []).append(record)

    # Papers whose method was named in prose but matched no vocabulary entry.
    for record in records:
        analysis = analyses.get(record.record_id)
        if analysis is None or analysis.detected_methods:
            continue
        if analysis.field("model_or_method").is_reported:
            grouped.setdefault("Method described in prose only", []).append(record)

    profiles: list[ModelProfile] = []
    for method, papers in sorted(grouped.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        category = method_category(method) if method in METHOD_VOCABULARY else "Not classified"
        profile = ModelProfile(
            model_name=method.title() if method.islower() else method,
            model_category=category,
            plain_explanation=CATEGORY_EXPLANATIONS.get(
                category, CATEGORY_EXPLANATIONS["Not classified"]
            ),
            paper_record_ids=[r.record_id for r in papers],
            citations=[manager.citation(r.record_id) for r in papers],
        )
        advantages, limitations = CATEGORY_TRADEOFFS.get(category, ([], []))
        profile.advantages = list(advantages)
        profile.limitations = list(limitations)

        for record in papers:
            analysis = analyses[record.record_id]
            citation = manager.citation(record.record_id)

            for field_name, target in (
                ("research_objective", profile.study_application),
                ("data_source", profile.required_inputs),
                ("dependent_variables", profile.outputs),
                ("software_or_tools", profile.software_used),
                ("validation_approach", profile.validation_approach),
                ("hypotheses", profile.assumptions),
            ):
                evidence = analysis.field(field_name)
                if not evidence.is_reported:
                    continue
                entry = f"{truncate_text(evidence.value, 220)} {citation}"
                if entry not in target:
                    target.append(entry)
                ledger.add_from_analysis(
                    analysis,
                    field_name,
                    document=document,
                    section=f"{profile.model_name} - {field_name.replace('_', ' ')}",
                    in_text_citation=citation,
                )

            calibration = analysis.field("validation_approach")
            if calibration.is_reported and "calibrat" in calibration.value.lower():
                entry = f"{truncate_text(calibration.value, 220)} {citation}"
                if entry not in profile.calibration_approach:
                    profile.calibration_approach.append(entry)

            if not profile.purpose:
                objective = analysis.field("research_objective")
                if objective.is_reported:
                    profile.purpose = (
                        f"As applied in the reviewed evidence: "
                        f"{truncate_text(objective.value, 300)} {citation}"
                    )

        if not profile.purpose:
            profile.purpose = (
                "The reviewed papers use this approach without stating its purpose in "
                "extractable terms."
            )
        profiles.append(profile)

    LOG.info(f"Built {len(profiles)} model profile(s) from the reviewed evidence.")
    return profiles


# ---------------------------------------------------------------------------
# Landscape
# ---------------------------------------------------------------------------

#: Dataset-type words recognised in data-source statements.
DATASET_CUES: tuple[str, ...] = (
    "household travel survey", "travel survey", "census", "national survey",
    "household survey", "panel survey", "administrative records", "smart card data",
    "mobile phone data", "gps data", "satellite imagery", "remote sensing",
    "reanalysis data", "weather station data", "traffic counts", "loop detector data",
    "social media data", "electricity consumption data", "meter data", "questionnaire",
    "interviews", "focus groups", "field experiment", "laboratory experiment",
    "secondary data", "open data", "simulation output",
)


def build_landscape(
    records: list[PaperRecord],
    analyses: dict[str, PaperAnalysis],
    config: JobConfig,
) -> LandscapeSummary:
    """Compute descriptive statistics of the reviewed evidence base."""
    summary = LandscapeSummary(total_papers=len(records))

    countries: Counter[str] = Counter()
    regions: Counter[str] = Counter()
    methods: Counter[str] = Counter()
    datasets: Counter[str] = Counter()
    applications: Counter[str] = Counter()
    years: Counter[str] = Counter()
    journals: Counter[str] = Counter()
    institutions: Counter[str] = Counter()

    for record in records:
        if record.year:
            years[str(record.year)] += 1
        if record.journal:
            journals[record.journal] += 1
        # Publisher is verified metadata; affiliations are not retrieved, so
        # institutions are only reported when a publisher is a research body.
        if record.publisher and any(
            marker in record.publisher.lower()
            for marker in ("university", "institute", "academy", "council", "society")
        ):
            institutions[record.publisher] += 1

        analysis = analyses.get(record.record_id)
        if analysis is None:
            continue
        for country in dict.fromkeys(analysis.detected_countries):
            countries[country] += 1
            if region := region_for(country):
                regions[region] += 1
        for method in dict.fromkeys(analysis.detected_methods):
            methods[method] += 1

        data_source = analysis.field("data_source")
        if data_source.is_reported:
            lowered = data_source.value.lower()
            for cue in DATASET_CUES:
                if cue in lowered:
                    datasets[cue] += 1

        context = analysis.field("policy_implications")
        if context.is_reported:
            for cue in ("policy", "planning", "management", "design", "investment",
                        "regulation", "operations", "forecasting"):
                if cue in context.value.lower():
                    applications[cue] += 1

    summary.countries = dict(countries.most_common(30))
    summary.regions = dict(regions.most_common())
    summary.methods = dict(methods.most_common(25))
    summary.datasets = dict(datasets.most_common(20))
    summary.applications = dict(applications.most_common(15))
    summary.year_counts = dict(sorted(years.items()))
    summary.journals = dict(journals.most_common(25))
    summary.institutions = dict(institutions.most_common(15))

    # Emerging methods: present only in the most recent third of the year range.
    if records:
        recent_cutoff = max(
            config.year_from, config.year_to - max(2, (config.year_to - config.year_from) // 3)
        )
        recent_methods: set[str] = set()
        older_methods: set[str] = set()
        for record in records:
            analysis = analyses.get(record.record_id)
            if analysis is None or not record.year:
                continue
            target = recent_methods if record.year >= recent_cutoff else older_methods
            target.update(analysis.detected_methods)
        summary.emerging_methods = sorted(recent_methods - older_methods)

    # Under-represented dimensions, stated as absences in this review only.
    under: list[str] = []
    if not summary.countries:
        under.append("No study geography could be extracted from any reviewed paper")
    if not summary.datasets:
        under.append("No recognised dataset type was named in the reviewed papers")
    for region in REGION_LIST:
        if region not in summary.regions:
            under.append(f"{region} is not represented in the reviewed evidence")
    summary.under_researched = under

    return summary


#: Regions checked for representation in the reviewed evidence.
REGION_LIST: tuple[str, ...] = (
    "South Asia", "East Asia", "Southeast Asia", "Europe", "North America",
    "Latin America", "Sub-Saharan Africa", "Middle East and North Africa", "Oceania",
)


def build_synthesis(
    records: list[PaperRecord],
    analyses: dict[str, PaperAnalysis],
    manager: CitationManager,
    ledger: EvidenceLedger,
    config: JobConfig,
) -> SynthesisResult:
    """Run every synthesis step and return the combined result."""
    gaps, contradictions = build_gaps(records, analyses, manager, ledger, config)
    return SynthesisResult(
        gaps=gaps,
        models=build_model_profiles(records, analyses, manager, ledger),
        landscape=build_landscape(records, analyses, config),
        contradictions=contradictions,
    )


def analysed_field_coverage(analyses: dict[str, PaperAnalysis]) -> dict[str, int]:
    """Count how many papers reported each analysis field.

    Used by the introduction and verification report to describe the evidence
    base honestly, including what it does not cover.
    """
    coverage: dict[str, int] = {}
    for name in ANALYSIS_FIELDS:
        coverage[name] = sum(
            1 for analysis in analyses.values() if analysis.field(name).is_reported
        )
    return coverage
