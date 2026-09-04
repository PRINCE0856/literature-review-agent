"""Structured analysis of extracted paper text.

Two rules govern everything here:

1. **Every extracted value carries its evidence.** A value is stored with the
   page numbers it came from and the sentence that supports it.
2. **Absence is recorded, not filled.** If a paper does not state its sample
   size, the field is ``Information not reported``. The analyser never infers a
   detail merely because a method usually implies it — an inference is only made
   when the text itself supports it, and is then labelled
   ``Agent inference based on evidence``.

The deterministic implementation locates the relevant section, scores candidate
sentences by cue words, and keeps the best-supported sentence with its page
number. When ``ANTHROPIC_API_KEY`` is available, Claude refines the same fields,
but it may only return content grounded in the supplied text and its output is
merged conservatively — never overwriting an author-stated value with a guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .config import Settings
from .llm import complete_json, llm_available
from .logging_setup import get_logger
from .pdf_extractor import PageText, load_pages
from .schemas import (
    ANALYSIS_FIELDS,
    EvidenceField,
    EvidenceStance,
    JobConfig,
    PaperAnalysis,
    PaperRecord,
)
from .utils import normalize_title, title_tokens, truncate_text

LOG = get_logger("analysis")


# ---------------------------------------------------------------------------
# Section detection
# ---------------------------------------------------------------------------

#: Heading patterns that mark the start of each canonical section.
SECTION_PATTERNS: dict[str, tuple[str, ...]] = {
    "abstract": (r"abstract", r"summary"),
    "introduction": (r"introduction", r"background"),
    "literature": (r"literature review", r"related work", r"prior work", r"state of the art"),
    "methods": (
        r"method", r"methodology", r"materials and methods", r"data and methods",
        r"model specification", r"research design", r"study design", r"empirical strategy",
        r"data", r"model",
    ),
    "results": (r"results", r"findings", r"empirical results", r"estimation results", r"analysis"),
    "discussion": (r"discussion", r"interpretation"),
    "conclusion": (
        r"conclusion", r"conclusions", r"concluding remarks", r"summary and conclusion",
        r"policy implications", r"implications",
    ),
    "limitations": (
        r"limitations?",
        r"limitations? and future (?:research|work|directions)",
        r"future (?:research|work|directions)",
        r"caveats?",
    ),
    "references": (r"references", r"bibliography", r"works cited"),
}


@dataclass
class Sentence:
    """One sentence with the page it appeared on."""

    text: str
    page: int
    section: str = ""


def _split_sentences(text: str) -> list[str]:
    """Split text into sentences, tolerating academic abbreviations."""
    if not text:
        return []
    protected = text
    for abbreviation in ("e.g.", "i.e.", "et al.", "cf.", "vs.", "Fig.", "Eq.", "No.",
                         "Dr.", "Prof.", "approx.", "ca.", "viz."):
        protected = protected.replace(abbreviation, abbreviation.replace(".", "\x00"))
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z(\[])", protected)
    return [p.replace("\x00", ".").strip() for p in parts if p.strip()]


def detect_section(line: str) -> str | None:
    """Return the canonical section a heading line introduces, if any."""
    cleaned = " ".join(line.split())
    if not cleaned or len(cleaned) > 90:
        return None
    # Strip leading numbering: "3.", "3.1", "IV."
    stripped = re.sub(r"^(\d+(\.\d+)*\.?|[IVXLC]+\.)\s*", "", cleaned).strip()
    lowered = stripped.lower().rstrip(":.")
    if not lowered or len(lowered.split()) > 6:
        return None
    for section, patterns in SECTION_PATTERNS.items():
        for pattern in patterns:
            if re.fullmatch(pattern, lowered) or lowered.startswith(pattern + " "):
                return section
    return None


def build_sentences(pages: list[PageText]) -> list[Sentence]:
    """Flatten pages into section-tagged sentences with page numbers.

    Reference lists are dropped: a sentence from someone else's title in the
    bibliography must never become evidence about this paper.
    """
    sentences: list[Sentence] = []
    current_section = "front matter"
    in_references = False

    for page in pages:
        if not page.readable or not page.text:
            continue
        for line in page.text.splitlines():
            if detected := detect_section(line):
                current_section = detected
                in_references = detected == "references"
                continue
            if in_references:
                continue
            cleaned = " ".join(line.split())
            if len(cleaned) < 25:
                continue
            for sentence in _split_sentences(cleaned):
                if 25 <= len(sentence) <= 700:
                    sentences.append(
                        Sentence(text=sentence, page=page.number, section=current_section)
                    )
    return sentences


# ---------------------------------------------------------------------------
# Field cues
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldSpec:
    """How to find one analysis field in a paper's text."""

    name: str
    cues: tuple[str, ...]
    sections: tuple[str, ...] = ()
    label: str = ""
    #: Cues that, when present, indicate the sentence is *about* the field but
    #: describes someone else's work rather than this study.
    negative_cues: tuple[str, ...] = ("in contrast to", "unlike previous", "other studies")


#: Cue vocabulary per field. Deliberately conservative: a field is only filled
#: when the sentence contains an explicit cue.
FIELD_SPECS: tuple[FieldSpec, ...] = (
    FieldSpec(
        "research_problem",
        ("problem", "challenge", "issue", "gap", "concern", "lack of", "remains unclear",
         "poorly understood", "little is known", "motivat"),
        ("abstract", "introduction"),
        "Research problem",
    ),
    FieldSpec(
        "research_objective",
        ("this paper", "this study", "this article", "we aim", "the aim", "the objective",
         "the purpose", "we investigate", "we examine", "we analyse", "we analyze",
         "we assess", "we develop", "this research"),
        ("abstract", "introduction", "conclusion"),
        "Research objective",
    ),
    FieldSpec(
        "research_questions",
        ("research question", "we ask", "the question of whether", "?", "we address"),
        ("abstract", "introduction"),
        "Research questions",
    ),
    FieldSpec(
        "hypotheses",
        ("hypothes", "we expect", "we posit", "we predict", "h1", "h2", "null hypothesis"),
        ("introduction", "methods", "literature"),
        "Hypotheses",
    ),
    FieldSpec(
        "study_geography",
        ("in the city of", "case study of", "data from", "in india", "in china", "in the us",
         "study area", "study region", "metropolitan", "province", "district", "country",
         "region of", "conducted in"),
        ("abstract", "methods", "introduction"),
        "Study geography",
    ),
    FieldSpec(
        "study_context",
        ("context", "setting", "during the", "period from", "between 20", "case of",
         "in the context of"),
        ("abstract", "methods", "introduction"),
        "Study context",
    ),
    FieldSpec(
        "study_design",
        ("cross-sectional", "longitudinal", "panel", "experiment", "quasi-experiment",
         "survey", "case study", "simulation", "randomised", "randomized", "observational",
         "mixed method", "qualitative", "quantitative", "systematic review", "meta-analysis",
         "before-and-after", "difference-in-difference"),
        ("methods", "abstract"),
        "Study design",
    ),
    FieldSpec(
        "data_source",
        ("data were", "data was", "dataset", "we use data", "obtained from", "collected from",
         "sourced from", "census", "survey data", "administrative data", "satellite",
         "records from", "provided by", "database"),
        ("methods", "abstract"),
        "Data source",
    ),
    FieldSpec(
        "sample_size",
        ("sample of", "n =", "n=", "respondents", "participants", "observations",
         "households", "trips", "records", "sample size"),
        ("methods", "abstract", "results"),
        "Sample size",
    ),
    FieldSpec(
        "unit_of_analysis",
        ("unit of analysis", "unit of observation", "at the individual level",
         "at the household level", "at the trip level", "at the city level",
         "per capita", "each respondent", "each household"),
        ("methods",),
        "Unit of analysis",
    ),
    FieldSpec(
        "variables",
        ("variable", "covariate", "predictor", "regressor", "indicator", "measure of"),
        ("methods",),
        "Variables",
    ),
    FieldSpec(
        "dependent_variables",
        ("dependent variable", "outcome variable", "response variable", "explained variable",
         "the outcome is", "we model the"),
        ("methods",),
        "Dependent variables",
    ),
    FieldSpec(
        "independent_variables",
        ("independent variable", "explanatory variable", "predictor variable",
         "key variable of interest", "treatment variable", "regressors include"),
        ("methods",),
        "Independent variables",
    ),
    FieldSpec(
        "control_variables",
        ("control variable", "we control for", "controlling for", "controls include",
         "after controlling"),
        ("methods",),
        "Control variables",
    ),
    FieldSpec(
        "model_or_method",
        ("model", "regression", "estimation", "logit", "probit", "we estimate",
         "we apply", "we employ", "algorithm", "framework", "approach"),
        ("methods", "abstract"),
        "Model or method",
    ),
    FieldSpec(
        "model_equations",
        ("equation", "eq.", "is given by", "is specified as", "can be written as",
         "formulated as", "where y", "= \\beta", "specification is"),
        ("methods",),
        "Model equations",
    ),
    FieldSpec(
        "software_or_tools",
        ("software", "stata", "spss", "r package", " in r ", "python", "matlab", "arcgis",
         "qgis", "gams", "biogeme", "nlogit", "sas", "eviews", "scikit", "tensorflow",
         "pytorch", "implemented in", "using the package"),
        ("methods",),
        "Software or tools",
    ),
    FieldSpec(
        "validation_approach",
        ("validat", "cross-validation", "out-of-sample", "goodness of fit", "robustness check",
         "sensitivity analysis", "holdout", "calibrat", "rmse", "r-squared", "we test the"),
        ("methods", "results"),
        "Validation approach",
    ),
    FieldSpec(
        "main_findings",
        ("we find", "results show", "results indicate", "results suggest", "findings show",
         "findings indicate", "we show that", "significantly", "associated with",
         "increase", "decrease", "reduction of", "was higher", "was lower", "effect of"),
        ("abstract", "results", "conclusion", "discussion"),
        "Main findings",
    ),
    FieldSpec(
        "policy_implications",
        ("policy", "policymakers", "planners", "should consider", "recommend",
         "implication", "practitioners", "decision-makers", "intervention"),
        ("conclusion", "discussion", "abstract"),
        "Policy implications",
    ),
    FieldSpec(
        "limitations_stated",
        ("limitation", "caveat", "we cannot", "does not allow", "constrained by",
         "beyond the scope", "should be interpreted with caution", "a shortcoming"),
        ("limitations", "conclusion", "discussion"),
        "Author-stated limitations",
    ),
    FieldSpec(
        "gaps_stated_by_authors",
        ("future research", "further research", "future work", "further study",
         "remains to be", "warrants investigation", "should be explored",
         "would benefit from", "little research", "few studies", "no study has"),
        ("limitations", "conclusion", "discussion", "introduction"),
        "Author-stated research gaps",
    ),
)

#: Named methods recognised for the models report and the landscape statistics.
METHOD_VOCABULARY: dict[str, str] = {
    "multinomial logit": "Discrete choice model",
    "mixed logit": "Discrete choice model",
    "nested logit": "Discrete choice model",
    "conditional logit": "Discrete choice model",
    "binary logit": "Discrete choice model",
    "logistic regression": "Regression model",
    "ordered probit": "Discrete choice model",
    "probit": "Discrete choice model",
    "discrete choice": "Discrete choice model",
    "structural equation model": "Structural model",
    "sem": "Structural model",
    "ordinary least squares": "Regression model",
    "ols": "Regression model",
    "fixed effects": "Panel data model",
    "random effects": "Panel data model",
    "panel data": "Panel data model",
    "difference-in-differences": "Causal inference design",
    "instrumental variable": "Causal inference design",
    "propensity score": "Causal inference design",
    "regression discontinuity": "Causal inference design",
    "quantile regression": "Regression model",
    "spatial regression": "Spatial model",
    "geographically weighted regression": "Spatial model",
    "spatial autoregressive": "Spatial model",
    "arima": "Time series model",
    "vector autoregression": "Time series model",
    "time series": "Time series model",
    "random forest": "Machine learning",
    "gradient boosting": "Machine learning",
    "xgboost": "Machine learning",
    "support vector machine": "Machine learning",
    "neural network": "Machine learning",
    "deep learning": "Machine learning",
    "lstm": "Machine learning",
    "convolutional neural network": "Machine learning",
    "k-means": "Machine learning",
    "cluster analysis": "Machine learning",
    "latent class": "Latent variable model",
    "agent-based model": "Simulation model",
    "microsimulation": "Simulation model",
    "system dynamics": "Simulation model",
    "monte carlo": "Simulation model",
    "cellular automata": "Simulation model",
    "linear programming": "Optimisation model",
    "mixed integer": "Optimisation model",
    "genetic algorithm": "Optimisation model",
    "multi-criteria": "Decision analysis",
    "analytic hierarchy process": "Decision analysis",
    "cost-benefit analysis": "Economic appraisal",
    "life cycle assessment": "Environmental assessment",
    "input-output analysis": "Economic accounting",
    "computable general equilibrium": "Economic accounting",
    "hydrological model": "Process-based model",
    "swat": "Process-based model",
    "rainfall-runoff": "Process-based model",
    "energy system model": "Process-based model",
    "times": "Process-based model",
    "message": "Process-based model",
    "integrated assessment model": "Process-based model",
    "meta-analysis": "Evidence synthesis",
    "systematic review": "Evidence synthesis",
    "thematic analysis": "Qualitative analysis",
    "content analysis": "Qualitative analysis",
    "grounded theory": "Qualitative analysis",
    "traffic assignment": "Network model",
    "gravity model": "Network model",
    "four-step model": "Network model",
}

#: Software names recognised in method sections.
SOFTWARE_VOCABULARY: tuple[str, ...] = (
    "Stata", "SPSS", "SAS", "EViews", "R", "Python", "MATLAB", "ArcGIS", "QGIS",
    "GAMS", "Biogeme", "NLOGIT", "AMOS", "Mplus", "LISREL", "scikit-learn",
    "TensorFlow", "PyTorch", "Keras", "NetLogo", "AnyLogic", "VISSIM", "SUMO",
    "TransCAD", "EMME", "Cube", "SWAT", "HEC-RAS", "MODFLOW", "OpenLCA", "SimaPro",
    "LEAP", "TIMES", "MESSAGE", "PLEXOS", "HOMER", "EnergyPLAN", "Excel", "NVivo",
    "ATLAS.ti", "Gurobi", "CPLEX",
)

#: Countries and regions recognised for the landscape report.
COUNTRY_VOCABULARY: tuple[str, ...] = (
    "India", "China", "United States", "USA", "United Kingdom", "UK", "Germany",
    "France", "Italy", "Spain", "Netherlands", "Belgium", "Sweden", "Norway",
    "Denmark", "Finland", "Poland", "Portugal", "Greece", "Switzerland", "Austria",
    "Ireland", "Canada", "Mexico", "Brazil", "Argentina", "Chile", "Colombia",
    "Peru", "Australia", "New Zealand", "Japan", "South Korea", "Korea", "Taiwan",
    "Singapore", "Malaysia", "Indonesia", "Thailand", "Vietnam", "Philippines",
    "Bangladesh", "Pakistan", "Nepal", "Sri Lanka", "Iran", "Turkey", "Israel",
    "Saudi Arabia", "United Arab Emirates", "Egypt", "Nigeria", "Ghana", "Kenya",
    "Ethiopia", "Tanzania", "Uganda", "South Africa", "Morocco", "Russia",
    "Ukraine", "Czech Republic", "Hungary", "Romania", "Delhi", "Mumbai",
    "Bengaluru", "Bangalore", "Chennai", "Kolkata", "Hyderabad", "Pune",
    "Ahmedabad", "Beijing", "Shanghai", "London", "New York", "Los Angeles",
    "Tokyo", "Seoul", "Paris", "Berlin", "Amsterdam", "Copenhagen", "Sydney",
    "Melbourne", "Toronto", "Nairobi", "Lagos", "Jakarta", "Bangkok", "Dhaka",
)

#: Region groupings used in the landscape report.
REGION_MAP: dict[str, tuple[str, ...]] = {
    "South Asia": ("India", "Bangladesh", "Pakistan", "Nepal", "Sri Lanka", "Delhi",
                   "Mumbai", "Bengaluru", "Bangalore", "Chennai", "Kolkata",
                   "Hyderabad", "Pune", "Ahmedabad", "Dhaka"),
    "East Asia": ("China", "Japan", "South Korea", "Korea", "Taiwan", "Beijing",
                  "Shanghai", "Tokyo", "Seoul"),
    "Southeast Asia": ("Singapore", "Malaysia", "Indonesia", "Thailand", "Vietnam",
                       "Philippines", "Jakarta", "Bangkok"),
    "Europe": ("United Kingdom", "UK", "Germany", "France", "Italy", "Spain",
               "Netherlands", "Belgium", "Sweden", "Norway", "Denmark", "Finland",
               "Poland", "Portugal", "Greece", "Switzerland", "Austria", "Ireland",
               "Russia", "Ukraine", "Czech Republic", "Hungary", "Romania",
               "London", "Paris", "Berlin", "Amsterdam", "Copenhagen"),
    "North America": ("United States", "USA", "Canada", "Mexico", "New York",
                      "Los Angeles", "Toronto"),
    "Latin America": ("Brazil", "Argentina", "Chile", "Colombia", "Peru"),
    "Sub-Saharan Africa": ("Nigeria", "Ghana", "Kenya", "Ethiopia", "Tanzania",
                           "Uganda", "South Africa", "Nairobi", "Lagos"),
    "Middle East and North Africa": ("Iran", "Turkey", "Israel", "Saudi Arabia",
                                     "United Arab Emirates", "Egypt", "Morocco"),
    "Oceania": ("Australia", "New Zealand", "Sydney", "Melbourne"),
}


# ---------------------------------------------------------------------------
# Sentence scoring
# ---------------------------------------------------------------------------


#: Patterns in which authors describe their own study — first person, or the
#: impersonal academic voice ("results show", "the findings indicate").
AUTHOR_VOICE_RE = re.compile(
    r"\b("
    r"we|our|us"
    r"|this (?:study|paper|article|research|analysis|work)"
    r"|the (?:present|current) (?:study|paper|analysis)"
    r"|(?:the )?(?:results?|findings?|analysis|estimates?|model)\s+"
    r"(?:show|shows|showed|indicate|indicates|indicated|suggest|suggests|suggested|"
    r"reveal|reveals|revealed|confirm|confirms|demonstrate|demonstrates)"
    r"|(?:a|one) limitation"
    r"|future (?:research|work|studies?)\s+(?:should|could|may|might|is needed)"
    r"|further (?:research|work|studies?)\s+(?:should|could|is needed)"
    r"|(?:policymakers|planners|practitioners)\s+should"
    r"|(?:is|are|was|were)\s+(?:estimated|implemented|conducted|collected|specified|"
    r"calibrated|validated)"
    r")\b",
    re.IGNORECASE,
)


def is_author_voice(text: str) -> bool:
    """True when a sentence reads as the authors describing their own study.

    Distinguishing this from the agent's own reading is what keeps
    ``Author explicitly states this`` honest.
    """
    return bool(AUTHOR_VOICE_RE.search(text or ""))


def score_sentence(sentence: Sentence, spec: FieldSpec) -> float:
    """Score how strongly a sentence evidences one field."""
    lowered = sentence.text.lower()
    hits = sum(1 for cue in spec.cues if cue in lowered)
    if not hits:
        return 0.0

    score = float(hits)
    if spec.sections and sentence.section in spec.sections:
        score += 1.5
    # The authors' own voice signals a statement about this study.
    if is_author_voice(sentence.text):
        score += 1.0
    for negative in spec.negative_cues:
        if negative in lowered:
            score -= 1.5
    # Penalise very long sentences: they make poor, unfocused evidence.
    if len(sentence.text) > 400:
        score -= 0.5
    return score


def extract_field(
    sentences: list[Sentence],
    spec: FieldSpec,
    *,
    max_quote_chars: int,
    max_sentences: int = 2,
) -> EvidenceField:
    """Extract one field, or record that the paper does not report it."""
    scored = [(score_sentence(s, spec), s) for s in sentences]
    candidates = sorted(
        [(score, s) for score, s in scored if score > 0],
        key=lambda pair: (-pair[0], pair[1].page),
    )
    if not candidates:
        return EvidenceField(
            value="",
            stance=EvidenceStance.NOT_REPORTED,
            confidence="Not applicable",
        )

    chosen = candidates[:max_sentences]
    value = " ".join(sentence.text for _, sentence in chosen)
    pages = sorted({sentence.page for _, sentence in chosen})
    top_score = chosen[0][0]

    # Author-stated requires the authors describing their own work, in either the
    # first person or the impersonal academic voice; otherwise the sentence is
    # evidence the agent is interpreting.
    stance = (
        EvidenceStance.AUTHOR_STATED
        if any(is_author_voice(s.text) for _, s in chosen) or top_score >= 3.0
        else EvidenceStance.AGENT_INFERENCE
    )
    confidence = "High" if top_score >= 4.0 else "Medium" if top_score >= 2.0 else "Low"

    return EvidenceField(
        value=truncate_text(value, max_quote_chars),
        stance=stance,
        pages=pages,
        quote=truncate_text(chosen[0][1].text, max_quote_chars),
        confidence=confidence,
    )


def find_vocabulary(text: str, vocabulary: tuple[str, ...] | dict[str, str]) -> list[str]:
    """Return vocabulary terms that genuinely appear in *text*.

    Word-boundary matched so ``R`` does not match every capital R, and ``ols``
    does not match ``controls``.
    """
    found: list[str] = []
    terms = vocabulary.keys() if isinstance(vocabulary, dict) else vocabulary
    for term in terms:
        pattern = r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])"
        if re.search(pattern, text, re.IGNORECASE):
            found.append(term)
    return found


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------


def analyse_text(
    record: PaperRecord,
    pages: list[PageText],
    config: JobConfig,
    settings: Settings,
    *,
    citation: str = "",
    use_llm: bool | None = None,
) -> PaperAnalysis:
    """Analyse one paper's extracted text into a structured record."""
    max_quote = int(settings.analysis.get("max_evidence_quote_chars", 400))
    sentences = build_sentences(pages)
    readable_pages = [p for p in pages if p.readable]

    analysis = PaperAnalysis(
        record_id=record.record_id,
        doi=record.doi,
        title=record.title,
        full_citation=citation,
        analyser="deterministic",
    )

    if not sentences:
        # No readable text: mark everything unverified rather than inventing it.
        for name in ANALYSIS_FIELDS:
            setattr(
                analysis,
                name,
                EvidenceField(
                    value="",
                    stance=EvidenceStance.UNVERIFIED,
                    confidence="Not applicable",
                ),
            )
        analysis.missing_information = [
            "The PDF produced no readable text"
            + (" (it appears to be a scanned document requiring OCR)" if record.requires_ocr else "")
            + ", so no field could be verified from the source."
        ]
        analysis.overall_confidence = "None"
        return analysis

    for spec in FIELD_SPECS:
        setattr(analysis, spec.name, extract_field(sentences, spec, max_quote_chars=max_quote))

    # --- relevance to the user's topic (agent judgement, labelled as such) ---
    analysis.relevance_to_topic = _assess_relevance(record, sentences, config, max_quote)

    # --- vocabulary detection over the methods-heavy text ---
    method_text = " ".join(
        s.text for s in sentences if s.section in {"methods", "abstract", "results"}
    ) or " ".join(s.text for s in sentences)
    analysis.detected_methods = find_vocabulary(method_text, METHOD_VOCABULARY)
    analysis.detected_software = find_vocabulary(method_text, SOFTWARE_VOCABULARY)
    all_text = " ".join(s.text for s in sentences)
    analysis.detected_countries = find_vocabulary(all_text, COUNTRY_VOCABULARY)

    # Promote detected vocabulary into fields the cue search left empty.
    if analysis.detected_methods and not analysis.model_or_method.is_reported:
        analysis.model_or_method = EvidenceField(
            value="; ".join(analysis.detected_methods[:4]),
            stance=EvidenceStance.AGENT_INFERENCE,
            pages=_pages_containing(sentences, analysis.detected_methods[0]),
            quote=_first_sentence_containing(sentences, analysis.detected_methods[0], max_quote),
            confidence="Medium",
        )
    if analysis.detected_software and not analysis.software_or_tools.is_reported:
        analysis.software_or_tools = EvidenceField(
            value="; ".join(analysis.detected_software[:4]),
            stance=EvidenceStance.AGENT_INFERENCE,
            pages=_pages_containing(sentences, analysis.detected_software[0]),
            quote=_first_sentence_containing(sentences, analysis.detected_software[0], max_quote),
            confidence="Medium",
        )
    if analysis.detected_countries and not analysis.study_geography.is_reported:
        analysis.study_geography = EvidenceField(
            value="; ".join(dict.fromkeys(analysis.detected_countries[:4])),
            stance=EvidenceStance.AGENT_INFERENCE,
            pages=_pages_containing(sentences, analysis.detected_countries[0]),
            quote=_first_sentence_containing(sentences, analysis.detected_countries[0], max_quote),
            confidence="Medium",
        )

    # --- sample size: prefer an explicit number ---
    if numeric := _extract_sample_number(sentences, max_quote):
        analysis.sample_size = numeric

    # --- agent-inferred gap, derived from what this paper did not do ---
    analysis.agent_inferred_gap = _infer_gap(analysis, record, config)

    # --- bookkeeping ---
    analysis.missing_information = [
        _humanise_field(name)
        for name in ANALYSIS_FIELDS
        if not analysis.field(name).is_reported
    ]
    analysis.overall_confidence = _overall_confidence(analysis, len(readable_pages))

    # --- optional LLM refinement (conservative merge) ---
    should_use_llm = llm_available(settings) if use_llm is None else use_llm
    if should_use_llm:
        if _refine_with_llm(analysis, record, sentences, settings, max_quote):
            analysis.analyser = "deterministic + Claude refinement"

    return analysis


def _pages_containing(sentences: list[Sentence], term: str) -> list[int]:
    """Pages where *term* appears."""
    lowered = term.lower()
    return sorted({s.page for s in sentences if lowered in s.text.lower()})[:4]


def _first_sentence_containing(sentences: list[Sentence], term: str, limit: int) -> str:
    """The first sentence containing *term*, truncated."""
    lowered = term.lower()
    for sentence in sentences:
        if lowered in sentence.text.lower():
            return truncate_text(sentence.text, limit)
    return ""


def _extract_sample_number(sentences: list[Sentence], limit: int) -> EvidenceField | None:
    """Find an explicitly stated sample size."""
    patterns = (
        r"\bn\s*=\s*([\d,]{2,12})",
        r"sample of ([\d,]{2,12})",
        r"([\d,]{3,12})\s+(?:respondents|participants|households|observations|trips|records|surveys)",
    )
    for sentence in sentences:
        for pattern in patterns:
            if match := re.search(pattern, sentence.text, re.IGNORECASE):
                number = match.group(1).replace(",", "")
                if number.isdigit() and 1 < int(number) < 100_000_000:
                    return EvidenceField(
                        value=f"{int(number):,}",
                        stance=EvidenceStance.AUTHOR_STATED,
                        pages=[sentence.page],
                        quote=truncate_text(sentence.text, limit),
                        confidence="High",
                    )
    return None


def _assess_relevance(
    record: PaperRecord,
    sentences: list[Sentence],
    config: JobConfig,
    limit: int,
) -> EvidenceField:
    """Judge relevance to the user's topic, labelled as agent inference."""
    topic_tokens = title_tokens(f"{config.topic} {' '.join(config.research_questions)}")
    if not topic_tokens:
        return EvidenceField(value="", stance=EvidenceStance.NOT_REPORTED)

    best: tuple[int, Sentence] | None = None
    for sentence in sentences:
        haystack = normalize_title(sentence.text)
        hits = sum(1 for token in topic_tokens if token in haystack)
        if hits and (best is None or hits > best[0]):
            best = (hits, sentence)

    coverage = 0.0
    title_haystack = normalize_title(f"{record.title} {record.abstract}")
    if topic_tokens:
        coverage = sum(1 for t in topic_tokens if t in title_haystack) / len(topic_tokens)

    if best is None and coverage == 0:
        return EvidenceField(
            value="No direct textual overlap with the review topic was found.",
            stance=EvidenceStance.AGENT_INFERENCE,
            confidence="Low",
        )

    strength = "directly addresses" if coverage >= 0.5 else "partially addresses"
    matched = sorted(t for t in topic_tokens if t in title_haystack)[:6]
    value = (
        f"This paper {strength} the review topic; overlapping concepts: "
        f"{', '.join(matched) or 'found in the body text only'}."
    )
    return EvidenceField(
        value=value,
        stance=EvidenceStance.AGENT_INFERENCE,
        pages=[best[1].page] if best else [],
        quote=truncate_text(best[1].text, limit) if best else "",
        confidence="High" if coverage >= 0.5 else "Medium",
    )


def _infer_gap(analysis: PaperAnalysis, record: PaperRecord, config: JobConfig) -> EvidenceField:
    """Infer a gap from what this paper demonstrably did not cover.

    Every inference is grounded in an *absence* the analysis actually observed,
    and is labelled as the agent's inference — never as an author statement.
    """
    observations: list[str] = []

    if not analysis.validation_approach.is_reported:
        observations.append("no validation or robustness strategy is reported")
    if not analysis.control_variables.is_reported and analysis.model_or_method.is_reported:
        observations.append("no control variables are described alongside the model")
    if analysis.detected_countries:
        observations.append(
            f"the evidence is specific to {', '.join(dict.fromkeys(analysis.detected_countries[:2]))}, "
            "so transferability to other contexts is untested here"
        )
    elif not analysis.study_geography.is_reported:
        observations.append("the study geography is not stated, limiting contextual transfer")
    if not analysis.policy_implications.is_reported:
        observations.append("policy or practice implications are not drawn out")
    if not analysis.sample_size.is_reported:
        observations.append("the sample size is not reported, so precision cannot be judged")
    if (record.document_type or "").lower() == "preprint":
        observations.append("the paper is a preprint and has not completed peer review")

    if not observations:
        return EvidenceField(
            value=(
                "No gap is inferable from absent reporting: this paper documents its "
                "design, model, validation, and implications."
            ),
            stance=EvidenceStance.AGENT_INFERENCE,
            confidence="Medium",
        )

    return EvidenceField(
        value=(
            "Based on what this paper does not report: "
            + "; ".join(observations[:3])
            + "."
        ),
        stance=EvidenceStance.AGENT_INFERENCE,
        pages=[],
        confidence="Medium",
    )


def _overall_confidence(analysis: PaperAnalysis, readable_pages: int) -> str:
    """Grade confidence in the analysis as a whole."""
    reported = sum(1 for name in ANALYSIS_FIELDS if analysis.field(name).is_reported)
    ratio = reported / len(ANALYSIS_FIELDS)
    author_stated = sum(
        1
        for name in ANALYSIS_FIELDS
        if analysis.field(name).stance == EvidenceStance.AUTHOR_STATED
        and analysis.field(name).is_reported
    )
    if readable_pages == 0:
        return "None"
    if ratio >= 0.7 and author_stated >= 10 and readable_pages >= 5:
        return "High"
    if ratio >= 0.45 and readable_pages >= 3:
        return "Medium"
    return "Low"


def _humanise_field(name: str) -> str:
    """Turn a field name into readable prose for the missing-information list."""
    return name.replace("_", " ").capitalize()


def _refine_with_llm(
    analysis: PaperAnalysis,
    record: PaperRecord,
    sentences: list[Sentence],
    settings: Settings,
    max_quote: int,
) -> bool:
    """Let Claude refine fields, grounded strictly in the supplied text.

    Only fills fields the deterministic pass left empty, and only accepts a
    value whose quote actually appears in the paper. Author-stated values are
    never overwritten.
    """
    missing = [name for name in ANALYSIS_FIELDS if not analysis.field(name).is_reported]
    if not missing:
        return False

    excerpt_parts: list[str] = []
    budget = 24000
    for sentence in sentences:
        piece = f"[p{sentence.page}] {sentence.text}"
        if budget - len(piece) < 0:
            break
        excerpt_parts.append(piece)
        budget -= len(piece)
    excerpt = "\n".join(excerpt_parts)

    prompt = (
        "You are extracting structured information from one research paper.\n\n"
        f"Paper title: {record.title}\n\n"
        "Below are sentences from the paper, each prefixed with its page number.\n"
        f"---\n{excerpt}\n---\n\n"
        f"For each of these fields, extract what the paper states: {', '.join(missing)}.\n\n"
        "Return JSON: an object whose keys are the field names above. Each value must "
        "be an object with keys 'value' (a concise statement), 'pages' (list of integers "
        "taken from the page prefixes), 'quote' (a verbatim sentence copied exactly from "
        "the text above), and 'stance' (either 'author' when the paper explicitly states "
        "it, or 'inference' when you are interpreting the text).\n\n"
        "Rules: omit any field the text does not support. Never guess a value because a "
        "method commonly implies it. The quote must be copied verbatim from the text "
        "above. Do not add fields that are not listed."
    )
    payload = complete_json(prompt, settings=settings, max_tokens=3000)
    if not payload:
        return False

    verbatim_pool = normalize_title(" ".join(s.text for s in sentences))
    accepted = 0
    for name in missing:
        entry = payload.get(name)
        if not isinstance(entry, dict):
            continue
        value = str(entry.get("value", "")).strip()
        quote = str(entry.get("quote", "")).strip()
        if not value or not quote:
            continue
        # Reject any quote that is not genuinely in the paper.
        if normalize_title(quote)[:120] not in verbatim_pool:
            LOG.debug(f"Rejected LLM value for '{name}': the quote is not in the source text.")
            continue
        pages = [int(p) for p in (entry.get("pages") or []) if str(p).isdigit()]
        stance = (
            EvidenceStance.AUTHOR_STATED
            if str(entry.get("stance", "")).lower().startswith("author")
            else EvidenceStance.AGENT_INFERENCE
        )
        setattr(
            analysis,
            name,
            EvidenceField(
                value=truncate_text(value, max_quote),
                stance=stance,
                pages=sorted(set(pages)),
                quote=truncate_text(quote, max_quote),
                confidence="Medium",
            ),
        )
        accepted += 1

    if accepted:
        analysis.missing_information = [
            _humanise_field(name)
            for name in ANALYSIS_FIELDS
            if not analysis.field(name).is_reported
        ]
        LOG.info(f"Claude filled {accepted} previously unreported field(s) with grounded quotes.")
    return accepted > 0


def analyse_records(
    records: list[PaperRecord],
    config: JobConfig,
    settings: Settings,
    *,
    citations: dict[str, str] | None = None,
    already_done: set[str] | None = None,
    on_complete=None,
) -> dict[str, PaperAnalysis]:
    """Analyse every paper with extracted text, resuming where it left off."""
    from pathlib import Path

    citations = citations or {}
    already_done = already_done or set()
    analyses: dict[str, PaperAnalysis] = {}

    candidates = [
        r for r in records if r.extracted_text_path and Path(r.extracted_text_path).exists()
    ]
    for index, record in enumerate(candidates, 1):
        if record.record_id in already_done:
            continue
        LOG.info(f"[{index}/{len(candidates)}] Analysing: {record.title[:70]}")
        pages = load_pages(Path(record.extracted_text_path))
        analyses[record.record_id] = analyse_text(
            record,
            pages,
            config,
            settings,
            citation=citations.get(record.record_id, ""),
        )
        if on_complete is not None:
            on_complete(record.record_id)

    LOG.info(f"Analysis complete for {len(analyses)} paper(s).")
    return analyses


def method_category(method_name: str) -> str:
    """Return the category of a recognised method, or a neutral default."""
    return METHOD_VOCABULARY.get(method_name.lower(), "Not classified")


def region_for(place: str) -> str | None:
    """Map a country or city to its region grouping."""
    for region, members in REGION_MAP.items():
        if place in members:
            return region
    return None
