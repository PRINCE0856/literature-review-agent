"""Deduplication in a fixed, auditable order.

The cascade is deliberate and always applied in this sequence:

1. Normalised DOI — the only identifier strong enough to merge on alone.
2. Exact normalised title.
3. Fuzzy title similarity, corroborated by author and year.
4. Other external identifiers (PMID, PMCID, arXiv, OpenAlex).

Every merge is recorded. Nothing is dropped: the losing record's fields are
absorbed into the survivor by :func:`metadata.merge_records` first.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from rapidfuzz import fuzz

from .config import Settings
from .logging_setup import get_logger
from .metadata import choose_primary, merge_records, normalise_record
from .schemas import PaperRecord
from .utils import first_author_surname, normalize_title, title_tokens, utc_now_iso

LOG = get_logger("dedup")


@dataclass
class MergeEvent:
    """One recorded duplicate merge, for the audit trail."""

    survivor_id: str
    absorbed_id: str
    rule: str
    similarity: float
    survivor_title: str
    absorbed_title: str
    survivor_source: str
    absorbed_source: str
    detail: str = ""
    merged_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        """Serialise for the deduplication log."""
        return {
            "survivor_id": self.survivor_id,
            "absorbed_id": self.absorbed_id,
            "rule": self.rule,
            "similarity": self.similarity,
            "survivor_title": self.survivor_title,
            "absorbed_title": self.absorbed_title,
            "survivor_source": self.survivor_source,
            "absorbed_source": self.absorbed_source,
            "detail": self.detail,
            "merged_at": self.merged_at,
        }


@dataclass
class DedupResult:
    """Deduplicated records plus a full merge audit trail."""

    records: list[PaperRecord] = field(default_factory=list)
    merges: list[MergeEvent] = field(default_factory=list)
    counters: dict[str, int] = field(default_factory=dict)

    @property
    def total_merged(self) -> int:
        """How many duplicate records were absorbed."""
        return len(self.merges)


# ---------------------------------------------------------------------------
# Similarity helpers
# ---------------------------------------------------------------------------


def title_similarity(left: str, right: str) -> float:
    """Return a 0-100 similarity score between two titles.

    Uses ``token_set_ratio`` so subtitle differences and word order do not
    prevent a genuine match, and takes the stricter of it and a token-overlap
    ratio to avoid matching two titles that merely share common words.
    """
    left_norm = normalize_title(left)
    right_norm = normalize_title(right)
    if not left_norm or not right_norm:
        return 0.0
    fuzzy = float(fuzz.token_set_ratio(left_norm, right_norm))

    left_tokens = title_tokens(left)
    right_tokens = title_tokens(right)
    if left_tokens and right_tokens:
        # Containment, not symmetric overlap: when one title is the other plus a
        # subtitle, every token of the shorter one is present and the pair should
        # still match. Using the larger set as the denominator would wrongly
        # penalise exactly that case.
        containment = len(left_tokens & right_tokens) / min(len(left_tokens), len(right_tokens))
        # A high fuzzy score with low real containment is usually a false
        # positive driven by shared generic words.
        return min(fuzzy, 60.0 + containment * 40.0) if containment < 0.6 else fuzzy
    return fuzzy


def authors_agree(left: PaperRecord, right: PaperRecord) -> bool:
    """True when the two records' author evidence is compatible.

    Absent author data is treated as "no disagreement" rather than a match, so it
    can never be the sole reason two records are merged.
    """
    left_surname = first_author_surname(left.authors)
    right_surname = first_author_surname(right.authors)
    if not left_surname or not right_surname:
        return True
    if left_surname.lower() == right_surname.lower():
        return True
    left_all = {(a.split(",")[0].strip().lower()) for a in left.authors if a}
    right_all = {(a.split(",")[0].strip().lower()) for a in right.authors if a}
    return bool(left_all & right_all)


def years_agree(left: PaperRecord, right: PaperRecord, tolerance: int) -> bool:
    """True when publication years match within *tolerance*.

    A tolerance is needed because online-first and issue years often differ by
    one for the same paper.
    """
    if left.year is None or right.year is None:
        return True
    return abs(left.year - right.year) <= tolerance


def shared_identifier(left: PaperRecord, right: PaperRecord) -> str | None:
    """Return the name of a strong external identifier both records share."""
    strong_keys = ("pmid", "pmcid", "arxiv", "openalex", "mag")
    for key in strong_keys:
        left_value = str(left.external_ids.get(key, "")).strip().lower()
        right_value = str(right.external_ids.get(key, "")).strip().lower()
        if left_value and left_value == right_value:
            return key
    return None


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


def deduplicate(records: list[PaperRecord], settings: Settings) -> DedupResult:
    """Deduplicate *records* through the four-stage cascade."""
    config = settings.deduplication
    fuzzy_threshold = float(config.get("fuzzy_title_threshold", 92))
    fuzzy_with_author = float(config.get("fuzzy_title_threshold_with_author", 87))
    year_tolerance = int(config.get("year_tolerance", 1))

    for record in records:
        normalise_record(record)

    result = DedupResult(
        counters={
            "input_records": len(records),
            "merged_by_doi": 0,
            "merged_by_exact_title": 0,
            "merged_by_fuzzy_title": 0,
            "merged_by_identifier": 0,
        }
    )

    survivors: list[PaperRecord] = []
    by_doi: dict[str, PaperRecord] = {}
    by_title: dict[str, PaperRecord] = {}

    def absorb(
        survivor: PaperRecord,
        candidate: PaperRecord,
        rule: str,
        similarity: float,
        detail: str,
    ) -> PaperRecord:
        """Merge *candidate* into *survivor* and log the event."""
        primary, secondary = choose_primary(survivor, candidate)
        merged = merge_records(primary, secondary)
        result.merges.append(
            MergeEvent(
                survivor_id=merged.record_id,
                absorbed_id=secondary.record_id,
                rule=rule,
                similarity=round(similarity, 2),
                survivor_title=merged.title,
                absorbed_title=secondary.title,
                survivor_source=merged.discovery_source,
                absorbed_source=secondary.discovery_source,
                detail=detail,
            )
        )
        return merged

    def replace_survivor(old: PaperRecord, new: PaperRecord) -> None:
        """Keep the index structures pointing at the surviving object."""
        for index, existing in enumerate(survivors):
            if existing is old:
                survivors[index] = new
                break
        else:
            survivors.append(new)
        if new.doi:
            by_doi[new.doi] = new
        if old.doi and old.doi != new.doi:
            by_doi[old.doi] = new
        for key in (normalize_title(old.title), normalize_title(new.title)):
            if key:
                by_title[key] = new

    for candidate in records:
        # --- Rule 1: normalised DOI ---
        if candidate.doi and candidate.doi in by_doi:
            existing = by_doi[candidate.doi]
            merged = absorb(existing, candidate, "normalised DOI", 100.0,
                            f"Both records carry DOI {candidate.doi}.")
            result.counters["merged_by_doi"] += 1
            replace_survivor(existing, merged)
            continue

        # --- Rule 2: exact normalised title ---
        title_key = normalize_title(candidate.title)
        if title_key and title_key in by_title:
            existing = by_title[title_key]
            # Two different DOIs are two different records, however identical the
            # titles look — a preprint and its published version, for instance.
            dois_conflict = bool(
                candidate.doi and existing.doi and candidate.doi != existing.doi
            )
            if not dois_conflict and years_agree(existing, candidate, year_tolerance):
                merged = absorb(
                    existing, candidate, "exact normalised title", 100.0,
                    "Titles are identical after normalisation.",
                )
                result.counters["merged_by_exact_title"] += 1
                replace_survivor(existing, merged)
                continue

        # --- Rule 3: fuzzy title corroborated by author and year ---
        best_match: PaperRecord | None = None
        best_score = 0.0
        for existing in survivors:
            if candidate.doi and existing.doi and candidate.doi != existing.doi:
                # Two different DOIs are two different records, however similar.
                continue
            score = title_similarity(candidate.title, existing.title)
            if score <= best_score:
                continue
            author_ok = authors_agree(existing, candidate)
            year_ok = years_agree(existing, candidate, year_tolerance)
            threshold = fuzzy_with_author if (author_ok and year_ok) else fuzzy_threshold
            if score >= threshold and year_ok:
                best_match, best_score = existing, score
        if best_match is not None:
            merged = absorb(
                best_match, candidate, "fuzzy title + author + year", best_score,
                f"Title similarity {best_score:.1f}; author and year evidence compatible.",
            )
            result.counters["merged_by_fuzzy_title"] += 1
            replace_survivor(best_match, merged)
            continue

        # --- Rule 4: other shared identifiers ---
        identifier_match: PaperRecord | None = None
        identifier_name = None
        for existing in survivors:
            if key := shared_identifier(existing, candidate):
                identifier_match, identifier_name = existing, key
                break
        if identifier_match is not None:
            merged = absorb(
                identifier_match, candidate, f"shared {identifier_name}", 100.0,
                f"Both records carry the same {identifier_name} identifier.",
            )
            result.counters["merged_by_identifier"] += 1
            replace_survivor(identifier_match, merged)
            continue

        # --- No duplicate: keep as a new survivor ---
        survivors.append(candidate)
        if candidate.doi:
            by_doi[candidate.doi] = candidate
        if title_key:
            by_title[title_key] = candidate

    result.records = survivors
    result.counters["unique_records"] = len(survivors)
    result.counters["total_merged"] = len(result.merges)
    LOG.info(
        f"Deduplication: {len(records)} records -> {len(survivors)} unique "
        f"({len(result.merges)} merged)."
    )
    return result
