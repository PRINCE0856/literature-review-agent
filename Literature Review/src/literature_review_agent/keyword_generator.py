"""Keyword strategy: concepts, synonyms, exclusions, and Boolean search strings.

The generator is deterministic by default. It expands only terms that are
anchored in the user's topic and research questions — it never wanders off into
adjacent fields — and it records for every term whether the user supplied it or
the agent generated it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .config import Settings
from .llm import complete_json, llm_available
from .logging_setup import get_logger
from .schemas import (
    InclusionCriteria,
    JobConfig,
    KeywordStrategy,
    KeywordTerm,
    SearchString,
    TermProvenance,
)
from .utils import STOPWORDS, slugify, write_text

LOG = get_logger("keywords")

# ---------------------------------------------------------------------------
# Domain lexicon
# ---------------------------------------------------------------------------
# A compact, curated thesaurus. Only entries whose key actually appears in the
# user's topic or questions are ever expanded, so the strategy cannot drift.

SYNONYM_LEXICON: dict[str, list[str]] = {
    # transport & mobility
    "travel behaviour": ["travel behavior", "travel demand", "mobility behaviour", "trip making"],
    "mode choice": ["modal choice", "mode selection", "modal split", "mode shift"],
    "commuting": ["commute", "journey to work", "work trip"],
    "public transport": ["public transit", "mass transit", "bus ridership", "metro ridership"],
    "traffic": ["road traffic", "traffic flow", "vehicular traffic"],
    "congestion": ["traffic congestion", "delay", "network congestion"],
    "cycling": ["bicycling", "bike use", "active travel"],
    "walking": ["pedestrian activity", "active travel"],
    "accessibility": ["access", "reachability", "spatial accessibility"],
    "ridership": ["patronage", "passenger demand", "boardings"],
    # weather & climate
    "rainfall": ["precipitation", "rain", "rainy weather", "wet weather"],
    "weather": ["meteorological conditions", "weather conditions"],
    "temperature": ["thermal conditions", "heat", "air temperature"],
    "flood": ["flooding", "inundation", "waterlogging", "pluvial flooding"],
    "heat wave": ["heatwave", "extreme heat", "heat stress"],
    "climate change": ["global warming", "climate variability", "climatic change"],
    "monsoon": ["monsoonal rainfall", "rainy season"],
    "drought": ["water scarcity", "dry spell", "meteorological drought"],
    "extreme weather": ["extreme events", "weather extremes", "severe weather"],
    # energy
    "renewable energy": ["clean energy", "green energy", "renewables"],
    "solar": ["solar power", "photovoltaic", "solar PV"],
    "wind": ["wind power", "wind energy", "wind turbine"],
    "energy transition": ["low-carbon transition", "decarbonisation", "decarbonization"],
    "electricity": ["power sector", "electric power", "grid"],
    "energy efficiency": ["energy conservation", "demand-side management"],
    "electric vehicle": ["electric vehicles", "EV", "battery electric vehicle", "e-mobility"],
    "grid": ["power grid", "electricity network", "transmission network"],
    "storage": ["energy storage", "battery storage", "BESS"],
    "hydrogen": ["green hydrogen", "hydrogen economy"],
    # water
    "groundwater": ["aquifer", "subsurface water", "water table"],
    "water quality": ["water pollution", "contamination"],
    "irrigation": ["irrigated agriculture", "crop water use"],
    "water demand": ["water consumption", "water use"],
    "watershed": ["catchment", "river basin", "drainage basin"],
    "sanitation": ["wastewater", "sewerage", "faecal sludge"],
    # environment & health
    "air pollution": ["air quality", "particulate matter", "PM2.5", "ambient pollution"],
    "emissions": ["greenhouse gas emissions", "GHG emissions", "carbon emissions"],
    "adaptation": ["climate adaptation", "adaptive capacity"],
    "mitigation": ["emission reduction", "climate mitigation"],
    "resilience": ["robustness", "coping capacity"],
    "vulnerability": ["exposure", "susceptibility", "risk"],
    "health": ["public health", "morbidity", "health outcomes"],
    "agriculture": ["farming", "crop production", "agricultural productivity"],
    "urban": ["city", "metropolitan", "urban area"],
    "rural": ["village", "non-urban", "countryside"],
    "household": ["households", "domestic", "family"],
    "policy": ["regulation", "governance", "policy instrument"],
    "livelihood": ["income", "employment", "household welfare"],
}

ABBREVIATION_LEXICON: dict[str, list[str]] = {
    "electric vehicle": ["EV", "BEV", "PHEV"],
    "public transport": ["PT"],
    "greenhouse gas": ["GHG"],
    "particulate matter": ["PM2.5", "PM10"],
    "photovoltaic": ["PV"],
    "gross domestic product": ["GDP"],
    "geographic information system": ["GIS"],
    "machine learning": ["ML"],
    "artificial intelligence": ["AI"],
    "internet of things": ["IoT"],
    "life cycle assessment": ["LCA"],
    "levelised cost of electricity": ["LCOE"],
    "battery energy storage system": ["BESS"],
    "state of charge": ["SOC"],
    "climate change": ["CC"],
    "sustainable development goals": ["SDG", "SDGs"],
    "energy storage": ["BESS"],
    "travel behaviour": ["TB"],
    "mode choice": ["MC"],
    "air quality": ["AQ"],
}

#: Terms whose British/American spellings must both be searched.
SPELLING_VARIANTS: dict[str, str] = {
    "behaviour": "behavior",
    "modelling": "modeling",
    "modelled": "modeled",
    "labour": "labor",
    "centre": "center",
    "analyse": "analyze",
    "analysed": "analyzed",
    "urbanisation": "urbanization",
    "optimisation": "optimization",
    "decarbonisation": "decarbonization",
    "utilisation": "utilization",
    "characterisation": "characterization",
    "programme": "program",
    "vapour": "vapor",
    "litre": "liter",
    "metre": "meter",
    "fibre": "fiber",
    "colour": "color",
    "favour": "favor",
    "organisation": "organization",
    "prioritise": "prioritize",
    "sulphur": "sulfur",
}

#: Method families the strategy can offer, keyed by a trigger word in the topic.
METHOD_LEXICON: dict[str, list[str]] = {
    "choice": ["discrete choice model", "multinomial logit", "mixed logit", "nested logit"],
    "mode": ["discrete choice model", "multinomial logit", "structural equation model"],
    "behaviour": ["regression analysis", "structural equation model", "panel data model"],
    "demand": ["econometric model", "time series analysis", "elasticity estimation"],
    "forecast": ["time series analysis", "ARIMA", "machine learning", "neural network"],
    "predict": ["machine learning", "random forest", "gradient boosting", "neural network"],
    "spatial": ["spatial regression", "GIS analysis", "geographically weighted regression"],
    "impact": ["difference-in-differences", "regression analysis", "counterfactual analysis"],
    "effect": ["difference-in-differences", "fixed effects model", "regression analysis"],
    "optimis": ["linear programming", "mixed integer programming", "optimisation model"],
    "optimiz": ["linear programming", "mixed integer programming", "optimization model"],
    "scenario": ["scenario analysis", "integrated assessment model", "energy system model"],
    "risk": ["risk assessment", "probabilistic modelling", "Monte Carlo simulation"],
    "cost": ["cost-benefit analysis", "levelised cost analysis"],
    "survey": ["household survey", "stated preference survey", "revealed preference survey"],
    "simulat": ["agent-based model", "microsimulation", "system dynamics"],
    "network": ["network analysis", "graph theory", "traffic assignment model"],
    "energy": ["energy system model", "TIMES model", "MESSAGE model", "load flow analysis"],
    "hydro": ["hydrological model", "SWAT model", "rainfall-runoff model"],
    "emission": ["emission inventory", "life cycle assessment", "input-output analysis"],
    "policy": ["policy evaluation", "cost-benefit analysis", "multi-criteria analysis"],
}

#: Application/outcome vocabulary offered alongside the core concepts.
APPLICATION_LEXICON: dict[str, list[str]] = {
    "urban": ["urban planning", "land use planning", "city management"],
    "transport": ["transport planning", "transport policy", "infrastructure planning"],
    "travel": ["transport planning", "demand management"],
    "energy": ["energy planning", "energy policy", "capacity planning"],
    "water": ["water resources management", "water policy"],
    "climate": ["climate policy", "adaptation planning", "climate risk management"],
    "health": ["health policy", "exposure assessment"],
    "agricultur": ["agricultural policy", "food security"],
    "emission": ["emission reduction policy", "carbon pricing"],
    "flood": ["flood risk management", "drainage planning"],
}

#: Geographic vocabulary, only added when the job names a geography.
GEOGRAPHY_LEXICON: dict[str, list[str]] = {
    "india": ["India", "Indian", "Delhi", "Mumbai", "Bengaluru", "South Asia"],
    "south asia": ["South Asia", "India", "Bangladesh", "Pakistan", "Nepal", "Sri Lanka"],
    "global south": ["Global South", "developing countries", "low- and middle-income countries"],
    "africa": ["Africa", "Sub-Saharan Africa", "African"],
    "europe": ["Europe", "European", "EU"],
    "china": ["China", "Chinese"],
    "usa": ["United States", "USA", "US"],
    "united states": ["United States", "USA", "US"],
    "asia": ["Asia", "Asian", "Southeast Asia"],
    "latin america": ["Latin America", "South America"],
    "global": [],
}

#: Terms almost always worth excluding from an empirical review.
BASE_EXCLUSIONS: list[str] = [
    "editorial",
    "erratum",
    "retracted",
    "book review",
    "conference announcement",
]


# ---------------------------------------------------------------------------
# Concept extraction
# ---------------------------------------------------------------------------


def extract_concepts(text: str, *, max_concepts: int = 8) -> list[str]:
    """Extract the main concepts from a topic or research question.

    Prefers multi-word phrases that match the lexicon, then falls back to
    significant single words. Output order is deterministic.
    """
    lowered = " " + re.sub(r"[^a-z0-9\s-]+", " ", text.lower()) + " "
    lowered = re.sub(r"\s+", " ", lowered)

    concepts: list[str] = []

    # 1) Known multi-word phrases present verbatim in the text.
    for phrase in sorted(SYNONYM_LEXICON, key=lambda p: -len(p)):
        if " " in phrase and f" {phrase} " in lowered and phrase not in concepts:
            concepts.append(phrase)

    # 2) Known single-word lexicon keys.
    words = [w for w in lowered.split() if w]
    for phrase in SYNONYM_LEXICON:
        if " " not in phrase and phrase in words and phrase not in concepts:
            concepts.append(phrase)

    # 3) Remaining significant words, so novel topics still produce concepts.
    for word in words:
        if len(word) < 4 or word in STOPWORDS:
            continue
        if any(word in c.split() for c in concepts):
            continue
        if word not in concepts:
            concepts.append(word)

    return concepts[:max_concepts]


def _spelling_variants(term: str) -> list[str]:
    """Return alternative spellings for *term* in both directions."""
    variants: list[str] = []
    lower = term.lower()
    for british, american in SPELLING_VARIANTS.items():
        if british in lower:
            variants.append(re.sub(british, american, lower, flags=re.IGNORECASE))
        if american in lower:
            variants.append(re.sub(american, british, lower, flags=re.IGNORECASE))
    return [v for v in dict.fromkeys(variants) if v != lower]


def _lexicon_hits(lexicon: dict[str, list[str]], haystack: str) -> list[str]:
    """Collect lexicon values whose trigger key occurs in *haystack*."""
    hits: list[str] = []
    for trigger, values in lexicon.items():
        if trigger in haystack:
            for value in values:
                if value not in hits:
                    hits.append(value)
    return hits


# ---------------------------------------------------------------------------
# Strategy construction
# ---------------------------------------------------------------------------


def build_keyword_strategy(
    config: JobConfig,
    settings: Settings,
    *,
    use_llm: bool | None = None,
) -> KeywordStrategy:
    """Build the complete keyword strategy for a job.

    ``use_llm`` defaults to whatever the environment supports; the deterministic
    strategy is always produced first and the LLM can only add terms to it.
    """
    corpus = " ".join([config.topic, *config.research_questions]).lower()
    haystack = " " + re.sub(r"[^a-z0-9\s-]+", " ", corpus) + " "

    concepts = extract_concepts(f"{config.topic} {' '.join(config.research_questions)}")
    terms: list[KeywordTerm] = []
    seen: set[str] = set()

    def add(term: str, category: str, concept: str = "", provenance=TermProvenance.AGENT,
            rationale: str = "") -> None:
        """Add a term once, recording its category and provenance."""
        clean = " ".join(str(term).split())
        if not clean or len(clean) < 2:
            return
        key = (clean.lower(), category)
        if key in seen:
            return
        seen.add(key)
        terms.append(
            KeywordTerm(
                term=clean,
                category=category,
                concept=concept,
                provenance=provenance,
                rationale=rationale,
            )
        )

    # --- user-supplied terms come first and are never dropped ---
    for keyword in config.user_keywords:
        add(
            keyword,
            "user keyword",
            provenance=TermProvenance.USER,
            rationale="Supplied directly by the user.",
        )

    # --- main concepts ---
    for concept in concepts:
        add(concept, "main concept", concept, rationale="Extracted from the topic/questions.")

    # --- synonyms (anchored on concepts only) ---
    for concept in concepts:
        for synonym in SYNONYM_LEXICON.get(concept, []):
            add(synonym, "synonym", concept, rationale=f"Curated synonym of '{concept}'.")

    # --- abbreviations ---
    for trigger, abbreviations in ABBREVIATION_LEXICON.items():
        if trigger in haystack or trigger in concepts:
            for abbreviation in abbreviations:
                add(
                    abbreviation,
                    "abbreviation",
                    trigger,
                    rationale=f"Common abbreviation for '{trigger}'.",
                )

    # --- alternative spellings ---
    for source in [config.topic, *config.research_questions, *concepts, *config.user_keywords]:
        for variant in _spelling_variants(source):
            add(variant, "alternative spelling", rationale="British/American spelling variant.")

    # --- related methods ---
    for method in _lexicon_hits(METHOD_LEXICON, haystack):
        add(method, "related method", rationale="Method family commonly used for this question.")

    # --- application terms ---
    for application in _lexicon_hits(APPLICATION_LEXICON, haystack):
        add(application, "application term", rationale="Applied/decision context of the topic.")

    # --- geographic terms ---
    geography = (config.geography or "").strip().lower()
    if geography and geography not in {"global", "worldwide", "world", ""}:
        matched = False
        for trigger, values in GEOGRAPHY_LEXICON.items():
            if trigger and trigger in geography:
                matched = True
                for value in values:
                    add(
                        value,
                        "geographic term",
                        rationale=f"Study geography requested: {config.geography}.",
                    )
        if not matched:
            add(
                config.geography.strip(),
                "geographic term",
                provenance=TermProvenance.USER,
                rationale="Study geography named by the user.",
            )

    # --- exclusion terms ---
    for term in config.exclusion_terms:
        add(
            term,
            "exclusion",
            provenance=TermProvenance.USER,
            rationale="Exclusion term supplied by the user.",
        )
    for term in BASE_EXCLUSIONS:
        add(term, "exclusion", rationale="Standard non-research document type.")

    strategy = KeywordStrategy(
        topic=config.topic,
        main_concepts=concepts,
        terms=terms,
        generator="deterministic",
    )
    strategy.criteria = build_criteria(config, concepts)

    # --- optional LLM enrichment (additive only) ---
    should_use_llm = llm_available(settings) if use_llm is None else use_llm
    if should_use_llm:
        added = _enrich_with_llm(strategy, config, settings, add)
        if added:
            strategy.terms = terms
            strategy.generator = "deterministic + Claude enrichment"
            LOG.info(f"Claude added {added} candidate terms anchored to the topic.")

    strategy.search_strings = build_search_strings(strategy, config)
    return strategy


def _enrich_with_llm(
    strategy: KeywordStrategy,
    config: JobConfig,
    settings: Settings,
    add: Any,
) -> int:
    """Ask Claude for extra synonyms/methods, rejecting off-topic suggestions."""
    prompt = (
        "You are helping build a systematic literature search strategy.\n"
        f"Research topic: {config.topic}\n"
        f"Research questions: {'; '.join(config.research_questions)}\n"
        f"Main concepts already identified: {', '.join(strategy.main_concepts)}\n\n"
        "Return JSON with keys 'synonyms', 'abbreviations', 'related_methods', "
        "'application_terms' and 'exclusions'. Each value must be a list of short "
        "search terms. Only include terms that a reviewer would accept as directly "
        "relevant to the stated topic. Do not include broad umbrella terms or "
        "terms from unrelated fields."
    )
    payload = complete_json(prompt, settings=settings, max_tokens=1500)
    if not payload:
        return 0

    category_map = {
        "synonyms": "synonym",
        "abbreviations": "abbreviation",
        "related_methods": "related method",
        "application_terms": "application term",
        "exclusions": "exclusion",
    }
    before = len(strategy.terms)
    topic_tokens = {
        t for t in re.split(r"\W+", f"{config.topic} {' '.join(config.research_questions)}".lower())
        if len(t) > 3
    }
    for key, category in category_map.items():
        for value in payload.get(key, []) or []:
            term = str(value).strip()
            if not term or len(term) > 60:
                continue
            # Guardrail: keep only terms that share a token with the topic, are a
            # short abbreviation, or are a known method/application phrase.
            tokens = {t for t in re.split(r"\W+", term.lower()) if len(t) > 3}
            anchored = bool(tokens & topic_tokens)
            is_abbrev = category == "abbreviation" and term.isupper() and len(term) <= 8
            known = any(
                term.lower() in [v.lower() for vals in lex.values() for v in vals]
                for lex in (METHOD_LEXICON, APPLICATION_LEXICON, SYNONYM_LEXICON)
            )
            if not (anchored or is_abbrev or known or category == "exclusion"):
                continue
            add(term, category, rationale="Suggested by Claude, anchored to the stated topic.")
    return len(strategy.terms) - before


def build_criteria(config: JobConfig, concepts: list[str]) -> InclusionCriteria:
    """Derive preliminary inclusion and exclusion criteria for the job."""
    include = [
        f"Published between {config.year_from} and {config.year_to} (inclusive).",
        f"Document type is one of: {', '.join(config.paper_types)}.",
        f"Written in {config.language}.",
        "Addresses at least one main concept: " + ", ".join(concepts) + ".",
        "Reports its own empirical analysis, model, or systematic synthesis.",
        "Full bibliographic metadata (title, authors, year, venue) is retrievable.",
    ]
    if config.geography and config.geography.lower() not in {"global", "worldwide", "world"}:
        include.append(f"Study context relates to {config.geography}.")
    if config.q1_mode.value == "only":
        include.append(
            "Published in a journal with an evidence-backed Q1 quartile for the "
            "publication year; candidates without ranking evidence go to the "
            "pending-verification list rather than being treated as Q1."
        )
    elif config.q1_mode.value == "preferred":
        include.append("Q1-ranked venues are ranked higher but non-Q1 papers remain eligible.")

    exclude = [
        "Editorials, errata, retracted items, book reviews, and conference announcements.",
        "Records without a retrievable title or venue.",
        f"Publications outside {config.year_from}-{config.year_to}.",
        "Duplicate records of an already-included paper (merged instead).",
        "Sources requiring paywall, CAPTCHA, or authentication bypass to obtain.",
    ]
    exclude.extend(f"Records matching the user exclusion term '{t}'." for t in config.exclusion_terms)
    return InclusionCriteria(include=include, exclude=exclude)


# ---------------------------------------------------------------------------
# Boolean search strings
# ---------------------------------------------------------------------------


def _quote(term: str) -> str:
    """Quote a multi-word term for Boolean queries."""
    return f'"{term}"' if " " in term else term


def _or_group(terms: list[str], limit: int) -> str:
    """Build an ``(a OR b OR c)`` group from the first *limit* terms."""
    chosen = [_quote(t) for t in terms[:limit] if t]
    if not chosen:
        return ""
    if len(chosen) == 1:
        return chosen[0]
    return "(" + " OR ".join(chosen) + ")"


def _concept_groups(strategy: KeywordStrategy, per_concept: int) -> list[str]:
    """Build one OR-group per main concept, including its synonyms."""
    groups: list[str] = []
    for concept in strategy.main_concepts:
        variants = [concept]
        for term in strategy.terms:
            if term.concept == concept and term.category in {
                "synonym",
                "abbreviation",
                "alternative spelling",
            }:
                variants.append(term.term)
        group = _or_group(list(dict.fromkeys(variants)), per_concept)
        if group:
            groups.append(group)
    return groups


def build_search_strings(strategy: KeywordStrategy, config: JobConfig) -> list[SearchString]:
    """Build broad, balanced, and narrow strings plus database-specific queries."""
    concepts = strategy.main_concepts
    synonyms = strategy.terms_in("synonym")
    methods = strategy.terms_in("related method")
    applications = strategy.terms_in("application term")
    geography = strategy.terms_in("geographic term")
    exclusions = strategy.terms_in("exclusion")
    user_terms = strategy.terms_in("user keyword")

    core_broad = _or_group(list(dict.fromkeys([*concepts[:3], *user_terms, *synonyms[:6]])), 12)
    balanced_groups = _concept_groups(strategy, per_concept=5)[:3]
    balanced = " AND ".join(g for g in balanced_groups if g) or core_broad
    narrow_parts = [*balanced_groups[:2]]
    if methods:
        narrow_parts.append(_or_group(methods, 4))
    if geography:
        narrow_parts.append(_or_group(geography, 4))
    narrow = " AND ".join(p for p in narrow_parts if p) or balanced

    not_clause = _or_group(exclusions, 6)
    strings: list[SearchString] = [
        SearchString(
            database="generic",
            breadth="broad",
            query=core_broad,
            notes="Highest recall; use for scoping and to check the concept vocabulary.",
        ),
        SearchString(
            database="generic",
            breadth="balanced",
            query=balanced,
            notes="Recommended default: one AND-group per main concept.",
        ),
        SearchString(
            database="generic",
            breadth="narrow",
            query=narrow,
            notes="Highest precision; adds method and geography constraints.",
        ),
    ]

    year_range = f"{config.year_from}-{config.year_to}"

    # --- Scopus / Elsevier ---
    scopus = f"TITLE-ABS-KEY({balanced})"
    if not_clause:
        scopus += f" AND NOT TITLE-ABS-KEY({not_clause})"
    scopus += f" AND PUBYEAR > {config.year_from - 1} AND PUBYEAR < {config.year_to + 1}"
    if "journal article" in [p.lower() for p in config.paper_types]:
        scopus += " AND DOCTYPE(ar)"
    strings.append(
        SearchString(
            database="Scopus (Elsevier)",
            breadth="balanced",
            query=scopus,
            notes="Paste into Scopus Advanced Search. DOCTYPE(ar) limits to articles.",
        )
    )

    # --- Web of Science ---
    wos = f"TS=({balanced})"
    if not_clause:
        wos += f" NOT TS=({not_clause})"
    wos += f" AND PY=({config.year_from}-{config.year_to})"
    strings.append(
        SearchString(
            database="Web of Science",
            breadth="balanced",
            query=wos,
            notes="TS = topic field. Use the Advanced Search box.",
        )
    )

    # --- Crossref (REST, no Boolean operators) ---
    strings.append(
        SearchString(
            database="Crossref",
            breadth="balanced",
            query=" ".join(dict.fromkeys([*concepts[:4], *user_terms[:2]])),
            notes=(
                "Crossref query.bibliographic is a relevance-ranked bag of words; "
                f"filter from-pub-date:{config.year_from}-01-01,"
                f"until-pub-date:{config.year_to}-12-31."
            ),
        )
    )

    # --- OpenAlex ---
    openalex_terms = "%20".join(t.replace(" ", "%20") for t in concepts[:3])
    strings.append(
        SearchString(
            database="OpenAlex",
            breadth="balanced",
            query=(
                f"search={openalex_terms}"
                f"&filter=from_publication_date:{config.year_from}-01-01,"
                f"to_publication_date:{config.year_to}-12-31,type:article"
            ),
            notes="OpenAlex /works parameters; type:article excludes datasets and errata.",
        )
    )

    # --- Semantic Scholar ---
    strings.append(
        SearchString(
            database="Semantic Scholar",
            breadth="balanced",
            query=" ".join(dict.fromkeys(concepts[:4])),
            notes=f"Use with year={config.year_from}-{config.year_to} on /paper/search.",
        )
    )

    # --- IEEE Xplore ---
    if _topic_matches(config, ("energy", "electric", "grid", "network", "sensor", "vehicle",
                               "machine learning", "signal", "power", "control")):
        ieee = " AND ".join(f'("All Metadata":{g})' for g in balanced_groups[:3] if g)
        strings.append(
            SearchString(
                database="IEEE Xplore",
                breadth="balanced",
                query=ieee or f'("All Metadata":{core_broad})',
                notes=f"IEEE command search; set the year filter to {year_range}.",
            )
        )

    # --- PubMed / Europe PMC ---
    if _topic_matches(config, ("health", "disease", "mortality", "morbidity", "clinical",
                               "patient", "exposure", "epidemi", "nutrition", "sanitation",
                               "air pollution", "water quality")):
        pubmed_groups = [
            g.replace(" OR ", "[tiab] OR ").replace(")", "[tiab])")
            for g in balanced_groups[:2]
        ]
        strings.append(
            SearchString(
                database="PubMed / Europe PMC",
                breadth="balanced",
                query=" AND ".join(pubmed_groups)
                + f' AND ("{config.year_from}"[dp] : "{config.year_to}"[dp])',
                notes="[tiab] restricts to title/abstract; [dp] is the publication date field.",
            )
        )

    # --- TRID ---
    if _topic_matches(config, ("transport", "travel", "traffic", "mobility", "road", "transit",
                               "commut", "freight", "vehicle", "pedestrian", "cycl")):
        strings.append(
            SearchString(
                database="TRID",
                breadth="balanced",
                query=balanced,
                notes=f"TRID transport research database; restrict publication year to {year_range}.",
            )
        )

    return strings


def _topic_matches(config: JobConfig, triggers: tuple[str, ...]) -> bool:
    """True when the topic or questions mention any of *triggers*."""
    corpus = f"{config.topic} {' '.join(config.research_questions)}".lower()
    return any(trigger in corpus for trigger in triggers)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

#: Category order used in ``keywords.md``.
_CATEGORY_ORDER = (
    "user keyword",
    "main concept",
    "synonym",
    "abbreviation",
    "alternative spelling",
    "related method",
    "application term",
    "geographic term",
    "exclusion",
)


def render_keywords_markdown(strategy: KeywordStrategy, config: JobConfig) -> str:
    """Render ``keywords.md``."""
    lines = [
        "# Keyword Strategy",
        "",
        f"- **Topic:** {config.topic}",
        f"- **Research questions:** {'; '.join(config.research_questions)}",
        f"- **Year range:** {config.year_from}-{config.year_to}",
        f"- **Study geography:** {config.geography}",
        f"- **Generated:** {strategy.created_at}",
        f"- **Generator:** {strategy.generator}",
        "",
        "## Main concepts",
        "",
    ]
    lines += [f"{i}. {c}" for i, c in enumerate(strategy.main_concepts, 1)] or ["_None extracted._"]
    lines.append("")

    for category in _CATEGORY_ORDER:
        items = [t for t in strategy.terms if t.category == category]
        if not items:
            continue
        lines += [f"## {category.title()}s" if not category.endswith("s") else f"## {category.title()}", ""]
        lines += ["| Term | Concept | Provenance | Rationale |", "| --- | --- | --- | --- |"]
        for term in items:
            lines.append(
                f"| {term.term} | {term.concept or '-'} | {term.provenance.value} | "
                f"{term.rationale or '-'} |"
            )
        lines.append("")

    lines += [
        "## Provenance summary",
        "",
        f"- User-supplied terms: "
        f"{sum(1 for t in strategy.terms if t.provenance == TermProvenance.USER)}",
        f"- Agent-generated terms: "
        f"{sum(1 for t in strategy.terms if t.provenance == TermProvenance.AGENT)}",
        "",
        "> Only terms anchored in the stated topic or research questions are generated. "
        "No keyword was invented from an unrelated field.",
        "",
    ]
    return "\n".join(lines)


def render_search_strings_markdown(strategy: KeywordStrategy, config: JobConfig) -> str:
    """Render ``search_strings.md``."""
    lines = [
        "# Database-Ready Search Strings",
        "",
        f"- **Topic:** {config.topic}",
        f"- **Year range:** {config.year_from}-{config.year_to}",
        "",
        "Copy a block into the matching database. Adjust field codes if your "
        "subscription uses a different interface version.",
        "",
    ]
    for breadth in ("broad", "balanced", "narrow"):
        generic = [
            s for s in strategy.search_strings if s.database == "generic" and s.breadth == breadth
        ]
        for item in generic:
            lines += [
                f"## Generic - {breadth}",
                "",
                "```text",
                item.query or "(no terms available)",
                "```",
                "",
                f"_{item.notes}_",
                "",
            ]
    for item in strategy.search_strings:
        if item.database == "generic":
            continue
        lines += [
            f"## {item.database}",
            "",
            "```text",
            item.query or "(no terms available)",
            "```",
            "",
            f"_{item.notes}_",
            "",
        ]
    return "\n".join(lines)


def render_criteria_markdown(strategy: KeywordStrategy, config: JobConfig) -> str:
    """Render ``inclusion_exclusion_criteria.md``."""
    lines = [
        "# Preliminary Inclusion and Exclusion Criteria",
        "",
        f"- **Topic:** {config.topic}",
        f"- **Q1 mode:** {config.q1_mode.value}",
        f"- **Maximum papers:** {config.maximum_papers}",
        "",
        "## Inclusion",
        "",
    ]
    lines += [f"- {c}" for c in strategy.criteria.include]
    lines += ["", "## Exclusion", ""]
    lines += [f"- {c}" for c in strategy.criteria.exclude]
    lines += [
        "",
        "## Screening note",
        "",
        "These criteria are applied automatically where the metadata allows it "
        "(year, document type, duplicate status) and are recorded for manual "
        "screening where judgement is required (study context, empirical content).",
        "",
    ]
    return "\n".join(lines)


def keywords_csv_rows(strategy: KeywordStrategy) -> list[dict[str, str]]:
    """Rows for ``keywords.csv``."""
    return [
        {
            "term": t.term,
            "category": t.category,
            "concept": t.concept,
            "provenance": t.provenance.value,
            "rationale": t.rationale,
        }
        for t in strategy.terms
    ]


def write_keyword_outputs(
    strategy: KeywordStrategy,
    config: JobConfig,
    keywords_dir: Path,
) -> list[Path]:
    """Write all four keyword artefacts and return their paths."""
    import csv

    keywords_dir = Path(keywords_dir)
    keywords_dir.mkdir(parents=True, exist_ok=True)

    md_path = write_text(keywords_dir / "keywords.md", render_keywords_markdown(strategy, config))
    strings_path = write_text(
        keywords_dir / "search_strings.md", render_search_strings_markdown(strategy, config)
    )
    criteria_path = write_text(
        keywords_dir / "inclusion_exclusion_criteria.md",
        render_criteria_markdown(strategy, config),
    )

    csv_path = keywords_dir / "keywords.csv"
    rows = keywords_csv_rows(strategy)
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["term", "category", "concept", "provenance", "rationale"]
        )
        writer.writeheader()
        writer.writerows(rows)

    LOG.info(f"Wrote {len(rows)} keyword terms to {keywords_dir.name}/ ({slugify(config.topic)}).")
    return [md_path, csv_path, strings_path, criteria_path]
