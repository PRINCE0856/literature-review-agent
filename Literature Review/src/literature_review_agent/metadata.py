"""Metadata normalisation, merging, and enrichment.

Merging is *additive*: when two records describe the same paper, the richer value
wins field by field and the audit trail records every substitution. A record is
never discarded because it arrived second — its useful fields are absorbed first.
"""

from __future__ import annotations

from typing import Any

from .config import Settings
from .http_client import HttpClient
from .logging_setup import get_logger
from .schemas import PaperRecord
from .utils import (
    coerce_year,
    normalize_doi,
    normalize_issn,
    normalize_title,
    stable_id,
    utc_now_iso,
)

LOG = get_logger("metadata")

#: Field-completeness weights used to decide which record is the better base.
COMPLETENESS_WEIGHTS: dict[str, float] = {
    "doi": 3.0,
    "abstract": 2.5,
    "authors": 2.0,
    "journal": 1.5,
    "year": 1.5,
    "issn": 1.0,
    "publisher": 0.8,
    "volume": 0.5,
    "issue": 0.5,
    "pages": 0.5,
    "keywords": 0.8,
    "pdf_url": 1.2,
    "landing_page_url": 0.6,
    "open_access_status": 0.6,
    "licence": 0.4,
    "citation_count": 0.4,
    "document_type": 0.4,
}

#: Preferred source order when two records disagree on a scalar field.
SOURCE_PRIORITY: tuple[str, ...] = (
    "Crossref",
    "OpenAlex",
    "Europe PMC",
    "Springer Nature",
    "Elsevier (Scopus/ScienceDirect)",
    "Semantic Scholar",
    "CORE",
    "arXiv",
)


def completeness_score(record: PaperRecord) -> float:
    """Score how much usable metadata a record carries."""
    score = 0.0
    for field_name, weight in COMPLETENESS_WEIGHTS.items():
        value = getattr(record, field_name, None)
        if value in (None, "", [], {}):
            continue
        if isinstance(value, str) and field_name == "abstract":
            # Longer abstracts are genuinely more useful, up to a point.
            score += weight * min(len(value) / 800.0, 1.0)
        elif isinstance(value, list):
            score += weight * min(len(value) / 5.0, 1.0)
        else:
            score += weight
    return round(score, 3)


def source_rank(record: PaperRecord) -> int:
    """Return the priority rank of a record's discovery source (lower is better)."""
    for index, label in enumerate(SOURCE_PRIORITY):
        if record.discovery_source == label:
            return index
    return len(SOURCE_PRIORITY)


def normalise_record(record: PaperRecord) -> PaperRecord:
    """Clean up a record in place: identifiers, whitespace, and derived fields."""
    record.doi = normalize_doi(record.doi)
    record.issn = normalize_issn(record.issn)
    record.eissn = normalize_issn(record.eissn)
    record.year = coerce_year(record.year)
    record.title = " ".join((record.title or "").split())
    record.journal = " ".join((record.journal or "").split())
    record.abstract = " ".join((record.abstract or "").split())
    record.authors = [" ".join(a.split()) for a in record.authors if a and a.strip()]
    record.keywords = [
        " ".join(k.split()) for k in record.keywords if k and str(k).strip()
    ][:20]

    # Deduplicate candidate PDF URLs while keeping the preferred one first.
    urls: list[str] = []
    for url in [record.pdf_url, *record.candidate_pdf_urls]:
        if url and url not in urls:
            urls.append(url)
    record.candidate_pdf_urls = urls
    record.pdf_url = urls[0] if urls else None

    if not record.record_id:
        record.record_id = stable_id(record.doi or record.normalized_title, record.year)
    return record


def merge_records(primary: PaperRecord, secondary: PaperRecord) -> PaperRecord:
    """Merge *secondary* into *primary*, keeping the richer value per field.

    Returns *primary*, mutated. Every field taken from *secondary* is written to
    ``merge_audit`` so a reviewer can see exactly what came from where.
    """
    audit: list[str] = []

    scalar_fields = (
        "title", "year", "journal", "volume", "issue", "pages", "article_number",
        "doi", "issn", "eissn", "publisher", "document_type", "language",
        "landing_page_url", "open_access_status", "licence", "abstract",
        "citation_count", "citation_count_retrieved",
    )
    for name in scalar_fields:
        current = getattr(primary, name, None)
        incoming = getattr(secondary, name, None)
        if incoming in (None, "", []):
            continue
        if current in (None, "", []):
            setattr(primary, name, incoming)
            audit.append(f"{name} taken from {secondary.discovery_source}")
            continue
        # Both present: prefer the longer abstract and the higher citation count.
        if name == "abstract" and len(str(incoming)) > len(str(current)) * 1.15:
            setattr(primary, name, incoming)
            audit.append(f"abstract replaced with the longer version from {secondary.discovery_source}")
        elif name == "citation_count":
            try:
                if int(incoming) > int(current):
                    primary.citation_count = int(incoming)
                    primary.citation_count_retrieved = (
                        secondary.citation_count_retrieved or utc_now_iso()
                    )
                    audit.append(f"citation_count updated from {secondary.discovery_source}")
            except (TypeError, ValueError):
                pass
        elif str(incoming) != str(current) and source_rank(secondary) < source_rank(primary):
            setattr(primary, name, incoming)
            audit.append(
                f"{name} changed to the {secondary.discovery_source} value "
                f"(was {current!r} from {primary.discovery_source})"
            )

    # Authors: prefer the longer, more complete list.
    if len(secondary.authors) > len(primary.authors):
        primary.authors = secondary.authors
        audit.append(f"author list taken from {secondary.discovery_source}")

    # Keywords, PDF URLs, identifiers, and sources are unioned.
    for keyword in secondary.keywords:
        if keyword not in primary.keywords:
            primary.keywords.append(keyword)
    for url in [secondary.pdf_url, *secondary.candidate_pdf_urls]:
        if url and url not in primary.candidate_pdf_urls:
            primary.candidate_pdf_urls.append(url)
    if not primary.pdf_url and primary.candidate_pdf_urls:
        primary.pdf_url = primary.candidate_pdf_urls[0]
        audit.append(f"pdf_url taken from {secondary.discovery_source}")
    for key, value in secondary.external_ids.items():
        primary.external_ids.setdefault(key, value)
    for source in [secondary.discovery_source, *secondary.metadata_sources]:
        if source and source not in primary.metadata_sources:
            primary.metadata_sources.append(source)

    # Relevance evidence accumulates rather than being overwritten.
    primary.relevance_score = max(primary.relevance_score, secondary.relevance_score)
    for reason in secondary.relevance_reasons:
        if reason not in primary.relevance_reasons:
            primary.relevance_reasons.append(reason)

    if secondary.record_id and secondary.record_id not in primary.merged_from:
        primary.merged_from.append(secondary.record_id)
    primary.merge_audit.extend(audit)
    if secondary.notes and secondary.notes not in primary.notes:
        primary.notes = " ".join(filter(None, [primary.notes, secondary.notes]))

    return primary


def choose_primary(left: PaperRecord, right: PaperRecord) -> tuple[PaperRecord, PaperRecord]:
    """Return ``(primary, secondary)`` ordered by richness then source priority."""
    left_score = completeness_score(left)
    right_score = completeness_score(right)
    if right_score > left_score:
        return right, left
    if right_score == left_score and source_rank(right) < source_rank(left):
        return right, left
    return left, right


# ---------------------------------------------------------------------------
# Enrichment
# ---------------------------------------------------------------------------


def enrich_from_crossref(
    record: PaperRecord, settings: Settings, *, client: HttpClient | None = None
) -> PaperRecord:
    """Fill gaps in a record from Crossref's DOI metadata.

    Only missing fields are populated; existing values are left untouched so an
    enrichment pass can never quietly rewrite a verified field.
    """
    if not record.doi:
        return record

    owns_client = client is None
    http = client or HttpClient(settings, requests_per_second=3.0)
    try:
        payload = http.get_json(
            f"https://api.crossref.org/works/{record.doi}",
            params={"mailto": settings.contact_email},
        )
    except Exception as exc:  # noqa: BLE001 - enrichment is best-effort
        LOG.debug(f"Crossref enrichment failed for {record.doi}: {exc}")
        return record
    finally:
        if owns_client:
            http.close()

    message = payload.get("message") or {}
    if not message:
        return record

    titles = message.get("title") or []
    containers = message.get("container-title") or []
    issns = message.get("ISSN") or []
    issued = ((message.get("issued") or {}).get("date-parts") or [[None]])[0]

    updates: dict[str, Any] = {
        "title": titles[0] if titles else None,
        "journal": containers[0] if containers else None,
        "volume": message.get("volume"),
        "issue": message.get("issue"),
        "pages": message.get("page"),
        "article_number": message.get("article-number"),
        "publisher": message.get("publisher"),
        "issn": issns[0] if issns else None,
        "eissn": issns[1] if len(issns) > 1 else None,
        "document_type": message.get("type"),
        "language": message.get("language"),
        "year": coerce_year(issued[0] if issued else None),
        "landing_page_url": message.get("URL"),
    }
    filled: list[str] = []
    for name, value in updates.items():
        if value in (None, "", []):
            continue
        if getattr(record, name, None) in (None, "", []):
            setattr(record, name, value)
            filled.append(name)

    if not record.authors and message.get("author"):
        record.authors = [
            f"{a.get('family', '')}, {a.get('given', '')}".strip(", ")
            for a in message["author"]
        ]
        filled.append("authors")

    if filled:
        record.merge_audit.append(f"Crossref enrichment filled: {', '.join(filled)}")
        if "Crossref" not in record.metadata_sources:
            record.metadata_sources.append("Crossref")
    return record


def enrich_open_access(
    record: PaperRecord, settings: Settings, *, client: HttpClient | None = None
) -> PaperRecord:
    """Add legal open-access PDF locations for a DOI via Unpaywall.

    Unpaywall reports where a *legally* free copy is hosted. Nothing here
    attempts to obtain subscription content.
    """
    if not record.doi:
        return record

    specs = settings.source_specs()
    spec = specs.get("unpaywall")
    if spec is None or not spec.available:
        return record

    owns_client = client is None
    http = client or HttpClient(settings, requests_per_second=spec.requests_per_second)
    try:
        payload = http.get_json(
            f"{spec.base_url}/{record.doi}",
            params={"email": settings.contact_email},
        )
    except Exception as exc:  # noqa: BLE001 - best-effort OA lookup
        LOG.debug(f"Unpaywall lookup failed for {record.doi}: {exc}")
        return record
    finally:
        if owns_client:
            http.close()

    if not payload:
        return record

    if status := payload.get("oa_status"):
        record.open_access_status = record.open_access_status or status

    locations = [payload.get("best_oa_location") or {}, *(payload.get("oa_locations") or [])]
    added = 0
    for location in locations:
        if not location:
            continue
        url = location.get("url_for_pdf") or location.get("url")
        if url and url not in record.candidate_pdf_urls:
            record.candidate_pdf_urls.append(url)
            added += 1
        if not record.licence and location.get("license"):
            record.licence = location["license"]

    if not record.pdf_url and record.candidate_pdf_urls:
        record.pdf_url = record.candidate_pdf_urls[0]
    if added:
        record.merge_audit.append(f"Unpaywall added {added} legal open-access location(s)")
        if "Unpaywall" not in record.metadata_sources:
            record.metadata_sources.append("Unpaywall")
    return record


def enrich_records(
    records: list[PaperRecord],
    settings: Settings,
    *,
    client: HttpClient | None = None,
    limit: int | None = None,
    fill_metadata: bool = True,
    find_open_access: bool = True,
) -> list[PaperRecord]:
    """Enrich a list of records, tolerating individual failures."""
    subset = records if limit is None else records[:limit]
    for record in subset:
        if fill_metadata and record.doi and not (record.journal and record.year):
            enrich_from_crossref(record, settings, client=client)
        if find_open_access and record.doi and not record.candidate_pdf_urls:
            enrich_open_access(record, settings, client=client)
    return records


# ---------------------------------------------------------------------------
# Relevance scoring
# ---------------------------------------------------------------------------


def score_relevance(
    record: PaperRecord,
    concepts: list[str],
    keywords: list[str],
    settings: Settings,
    *,
    current_year: int,
) -> float:
    """Score how well a record answers the review question.

    Weights come from ``relevance`` in ``default_config.yaml``. The Q1 component
    rewards a *candidate* signal only; it never asserts a verified quartile.
    """
    weights = settings.relevance
    title_norm = normalize_title(record.title)
    abstract_norm = normalize_title(record.abstract)
    record_keywords = {normalize_title(k) for k in record.keywords}
    reasons: list[str] = []

    concept_terms = [normalize_title(c) for c in concepts if c]
    keyword_terms = [normalize_title(k) for k in keywords if k]

    def coverage(terms: list[str], haystack: str) -> float:
        """Fraction of *terms* appearing in *haystack*."""
        if not terms or not haystack:
            return 0.0
        hits = sum(1 for term in terms if term and term in haystack)
        return hits / len(terms)

    title_score = coverage(concept_terms, title_norm)
    abstract_score = coverage(concept_terms, abstract_norm)
    keyword_score = (
        sum(1 for term in keyword_terms if any(term in k for k in record_keywords))
        / len(keyword_terms)
        if keyword_terms
        else 0.0
    )

    half_life = float(weights.get("recency_half_life_years", 6)) or 6.0
    if record.year:
        age = max(0, current_year - record.year)
        recency = 0.5 ** (age / half_life)
    else:
        recency = 0.0

    open_access = 1.0 if (record.open_access_status or "").lower() in {
        "gold", "green", "hybrid", "bronze", "diamond"
    } or record.candidate_pdf_urls else 0.0

    q1_candidate = 1.0 if _looks_like_indexed_journal(record) else 0.0

    score = (
        float(weights.get("weight_title_match", 0.4)) * title_score
        + float(weights.get("weight_abstract_match", 0.25)) * abstract_score
        + float(weights.get("weight_keyword_match", 0.10)) * keyword_score
        + float(weights.get("weight_recency", 0.10)) * recency
        + float(weights.get("weight_open_access", 0.05)) * open_access
        + float(weights.get("weight_q1_candidate", 0.10)) * q1_candidate
    )

    if title_score:
        reasons.append(f"{title_score:.0%} of main concepts appear in the title")
    if abstract_score:
        reasons.append(f"{abstract_score:.0%} of main concepts appear in the abstract")
    if open_access:
        reasons.append("a legal open-access copy appears to be available")
    if q1_candidate:
        reasons.append("published in an indexed journal (quartile not yet verified)")
    if record.year:
        reasons.append(f"published in {record.year}")

    record.relevance_score = round(min(score, 1.0), 4)
    record.relevance_reasons = reasons
    return record.relevance_score


def _looks_like_indexed_journal(record: PaperRecord) -> bool:
    """Weak signal that a record sits in a properly indexed journal.

    Used only for ranking candidates. It is never treated as quartile evidence:
    :mod:`q1_verifier` is the sole authority on Q1 status.
    """
    if (record.document_type or "").lower() == "preprint":
        return False
    return bool(record.journal and (record.issn or record.eissn) and record.doi)
