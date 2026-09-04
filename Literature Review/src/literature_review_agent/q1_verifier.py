"""Journal-quartile verification from a user-supplied ranking file.

The governing rule: **a quartile is only ever reported when a ranking source
says so.** This module ships no ranking data. Without a licensed file every
paper is marked ``Unverified`` — never inferred from a journal's reputation,
impact factor, publisher, or citation count.

Quartiles are year-specific and often subject-category-specific, so the ranking
year actually used is always recorded alongside the result, and a journal that is
Q1 in one category and Q2 in another is reported as
``Conflicting information`` rather than silently resolved.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd
from rapidfuzz import fuzz, process

from .config import Settings
from .logging_setup import get_logger
from .schemas import JobConfig, PaperRecord, Q1Mode, Q1Status, Q1Verification
from .utils import coerce_year, normalize_issn, normalize_title, today_stamp, utc_now_iso

LOG = get_logger("q1")

#: Quartile labels the verifier recognises.
VALID_QUARTILES = ("Q1", "Q2", "Q3", "Q4")


class RankingDataError(RuntimeError):
    """Raised when a ranking file exists but cannot be interpreted."""


@dataclass
class RankingEntry:
    """One journal-year-category row from the ranking file."""

    journal_name: str
    normalised_name: str
    issns: set[str]
    quartile: str | None
    subject_category: str | None
    ranking_year: int | None
    publisher: str | None = None
    source_row: int = -1


@dataclass
class RankingTable:
    """An indexed journal-ranking dataset loaded from the user's file."""

    source_name: str
    source_path: str
    entries: list[RankingEntry] = field(default_factory=list)
    by_issn: dict[str, list[RankingEntry]] = field(default_factory=dict)
    by_name: dict[str, list[RankingEntry]] = field(default_factory=dict)
    years_present: set[int] = field(default_factory=set)

    @property
    def is_empty(self) -> bool:
        """True when no usable ranking rows were loaded."""
        return not self.entries

    def index(self) -> None:
        """Build the ISSN and name lookups."""
        self.by_issn.clear()
        self.by_name.clear()
        for entry in self.entries:
            for issn in entry.issns:
                self.by_issn.setdefault(issn, []).append(entry)
            if entry.normalised_name:
                self.by_name.setdefault(entry.normalised_name, []).append(entry)
            if entry.ranking_year:
                self.years_present.add(entry.ranking_year)

    def name_candidates(self) -> list[str]:
        """Normalised journal names available for fuzzy matching."""
        return list(self.by_name.keys())


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _split_issns(value: Any) -> set[str]:
    """Parse the many ISSN formats ranking files use into normalised ISSNs.

    Scimago packs several ISSNs into one comma-separated cell without hyphens;
    JCR exports use separate columns. Both are handled.
    """
    if value is None:
        return set()
    text = str(value)
    if text.lower() in {"nan", "none", "-", ""}:
        return set()
    out: set[str] = set()
    for chunk in text.replace(";", ",").replace("|", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if normalised := normalize_issn(chunk):
            out.add(normalised)
    return out


def _clean_quartile(value: Any, value_map: dict[str, Any]) -> str | None:
    """Map a raw quartile cell to ``Q1``-``Q4`` or ``None``."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "-", "n/a", "na"}:
        return None
    if text in value_map:
        mapped = value_map[text]
        return str(mapped) if mapped else None
    upper = text.upper().replace(" ", "")
    for quartile in VALID_QUARTILES:
        if upper == quartile or upper.endswith(quartile) or upper.startswith(quartile):
            return quartile
    return None


def load_ranking_table(settings: Settings, path: str | Path | None = None) -> RankingTable | None:
    """Load the configured journal-ranking file, or return ``None``.

    Supports CSV, TSV, and Excel. Column names are taken from
    ``q1_ranking.column_map`` so any licensed export can be used.
    """
    config = settings.q1_ranking
    if not config.get("enabled", True):
        LOG.info("Q1 verification is disabled in configuration.")
        return None

    raw_path = path or config.get("file")
    if not raw_path:
        LOG.warning(
            "No journal-ranking file is configured. Every paper will be reported as "
            "'Unverified' rather than being assigned a guessed quartile."
        )
        return None

    file_path = settings.resolve_path(str(raw_path))
    if file_path is None or not file_path.exists():
        LOG.warning(
            f"The configured ranking file was not found at {file_path}. Quartiles "
            "will be reported as 'Unverified'."
        )
        return None

    try:
        frame = _read_table(file_path)
    except Exception as exc:  # noqa: BLE001 - surfaced as a clear config error
        raise RankingDataError(f"Could not read the ranking file {file_path}: {exc}") from exc

    column_map = dict(config.get("column_map") or {})
    value_map = dict(config.get("quartile_value_map") or {})
    resolved = _resolve_columns(frame, column_map)

    if not resolved.get("journal_name") and not resolved.get("issn"):
        raise RankingDataError(
            f"The ranking file {file_path.name} has neither a journal-name nor an "
            f"ISSN column matching config/default_config.yaml. Columns found: "
            f"{', '.join(map(str, frame.columns))}"
        )

    default_year = coerce_year(config.get("ranking_year")) or coerce_year(file_path.stem)
    table = RankingTable(
        source_name=str(config.get("source_name") or file_path.name),
        source_path=str(file_path),
    )

    name_col = resolved.get("journal_name")
    issn_col = resolved.get("issn")
    eissn_col = resolved.get("eissn")
    quartile_col = resolved.get("quartile")
    category_col = resolved.get("subject_category")
    year_col = resolved.get("ranking_year")
    publisher_col = resolved.get("publisher")

    for position, row in enumerate(frame.to_dict("records")):
        name = str(row.get(name_col, "") or "").strip() if name_col else ""
        issns = _split_issns(row.get(issn_col)) if issn_col else set()
        if eissn_col:
            issns |= _split_issns(row.get(eissn_col))
        quartile = _clean_quartile(row.get(quartile_col), value_map) if quartile_col else None
        if not name and not issns:
            continue
        table.entries.append(
            RankingEntry(
                journal_name=name,
                normalised_name=normalize_title(name),
                issns=issns,
                quartile=quartile,
                subject_category=(
                    str(row.get(category_col)).strip() if category_col and row.get(category_col) else None
                ),
                ranking_year=(coerce_year(row.get(year_col)) if year_col else None) or default_year,
                publisher=(
                    str(row.get(publisher_col)).strip() if publisher_col and row.get(publisher_col) else None
                ),
                source_row=position + 2,  # +2 accounts for the header row
            )
        )

    table.index()
    LOG.info(
        f"Loaded {len(table.entries)} ranking rows from {file_path.name} "
        f"(years: {sorted(table.years_present) or 'not stated'})."
    )
    return table


def _read_table(path: Path) -> pd.DataFrame:
    """Read a CSV/TSV/Excel ranking file into a DataFrame."""
    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xlsm", ".xls"}:
        return pd.read_excel(path, dtype=str)
    if suffix in {".tsv", ".tab"}:
        return pd.read_csv(path, sep="\t", dtype=str, keep_default_na=False)
    # Scimago exports are semicolon-delimited; sniff the separator.
    sample = path.read_text(encoding="utf-8", errors="replace")[:4096]
    separator = ";" if sample.count(";") > sample.count(",") else ","
    return pd.read_csv(path, sep=separator, dtype=str, keep_default_na=False)


def _resolve_columns(frame: pd.DataFrame, column_map: dict[str, Any]) -> dict[str, str]:
    """Match configured column names to the file's actual columns.

    Matching is case- and whitespace-insensitive, with a small set of fallback
    names so common Scimago/JCR exports work without editing the config.
    """
    lookup = {str(c).strip().lower(): str(c) for c in frame.columns}
    fallbacks: dict[str, tuple[str, ...]] = {
        "journal_name": ("title", "journal name", "journal", "source title", "full journal title"),
        "issn": ("issn", "issn (linking)", "print issn", "issn-l"),
        "eissn": ("eissn", "e-issn", "online issn", "electronic issn"),
        "quartile": ("sjr best quartile", "quartile", "jif quartile", "best quartile", "q"),
        "subject_category": ("categories", "subject category", "category", "areas", "wos categories"),
        "ranking_year": ("year", "ranking year", "jcr year"),
        "publisher": ("publisher",),
    }

    resolved: dict[str, str] = {}
    for logical, configured in column_map.items():
        if configured and str(configured).strip().lower() in lookup:
            resolved[logical] = lookup[str(configured).strip().lower()]
    for logical, names in fallbacks.items():
        if logical in resolved:
            continue
        for name in names:
            if name in lookup:
                resolved[logical] = lookup[name]
                break
    return resolved


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def _pick_year_entries(entries: list[RankingEntry], publication_year: int | None) -> list[RankingEntry]:
    """Choose the ranking rows whose year is most appropriate for the paper.

    Prefers the exact publication year; otherwise the closest year that is not
    later than publication; otherwise the closest available year overall.
    """
    if not entries:
        return []
    dated = [e for e in entries if e.ranking_year]
    if not dated or publication_year is None:
        return entries

    exact = [e for e in dated if e.ranking_year == publication_year]
    if exact:
        return exact
    earlier = [e for e in dated if e.ranking_year is not None and e.ranking_year <= publication_year]
    pool = earlier or dated
    best_year = min(
        (e.ranking_year for e in pool if e.ranking_year is not None),
        key=lambda y: abs(y - publication_year),
    )
    return [e for e in pool if e.ranking_year == best_year]


def _resolve_quartiles(entries: list[RankingEntry]) -> tuple[str | None, list[str], bool]:
    """Collapse matched rows into a quartile decision.

    Returns ``(quartile, categories, conflicting)``. A journal that is Q1 in one
    subject category and Q2 in another is reported as conflicting: the reviewer
    decides which category applies, not the agent.
    """
    quartiles = {e.quartile for e in entries if e.quartile}
    categories = sorted({e.subject_category for e in entries if e.subject_category})
    if not quartiles:
        return None, categories, False
    if len(quartiles) == 1:
        return quartiles.pop(), categories, False
    # Multiple distinct quartiles: Q1 in any category is meaningful, but it is a
    # conflict that must be surfaced rather than resolved silently.
    return ("Q1" if "Q1" in quartiles else sorted(quartiles)[0]), categories, True


def verify_record(
    record: PaperRecord,
    table: RankingTable | None,
    settings: Settings,
) -> Q1Verification:
    """Determine the quartile evidence for one paper.

    Never returns a quartile that did not come from *table*.
    """
    verification = Q1Verification(
        journal_name=record.journal or "",
        issn=record.issn,
        eissn=record.eissn,
        publication_year=record.year,
        verification_date=today_stamp(),
        verification_status=Q1Status.UNVERIFIED,
    )

    if (record.document_type or "").lower() == "preprint" or not record.journal:
        verification.verification_status = Q1Status.NOT_APPLICABLE
        verification.notes = (
            "Preprint or no journal recorded, so a journal quartile does not apply."
        )
        return verification

    if table is None or table.is_empty:
        verification.notes = (
            "No journal-ranking data is configured, so the quartile could not be "
            "verified. Supply a licensed ranking file via q1_ranking.file to enable "
            "verification. No quartile has been guessed."
        )
        return verification

    verification.ranking_source = table.source_name
    match_config = settings.q1_ranking.get("match", {}) or {}

    matched: list[RankingEntry] = []
    matched_on: str | None = None

    # --- ISSN match (strongest) ---
    if match_config.get("use_issn", True):
        for issn in {i for i in (record.issn, record.eissn) if i}:
            if hits := table.by_issn.get(issn):
                matched.extend(hits)
                matched_on = f"ISSN {issn}"
                break

    # --- Exact journal-name match ---
    if not matched and match_config.get("use_exact_name", True):
        key = normalize_title(record.journal)
        if key and (hits := table.by_name.get(key)):
            matched = list(hits)
            matched_on = "exact journal name"

    # --- Fuzzy journal-name match ---
    if not matched and match_config.get("use_fuzzy_name", True):
        threshold = float(match_config.get("fuzzy_name_threshold", 95))
        key = normalize_title(record.journal)
        candidates = table.name_candidates()
        if key and candidates:
            best = process.extractOne(key, candidates, scorer=fuzz.token_sort_ratio)
            if best and best[1] >= threshold:
                matched = list(table.by_name.get(best[0], []))
                matched_on = f"fuzzy journal name ({best[1]:.0f}% similar to '{best[0]}')"

    if not matched:
        verification.notes = (
            f"'{record.journal}' was not found in {table.source_name} by ISSN or name, "
            "so the quartile remains unverified."
        )
        return verification

    year_entries = _pick_year_entries(matched, record.year)
    quartile, categories, conflicting = _resolve_quartiles(year_entries)

    ranking_years = sorted({e.ranking_year for e in year_entries if e.ranking_year})
    verification.ranking_year = ranking_years[0] if ranking_years else None
    verification.subject_category = "; ".join(categories) if categories else None
    verification.matched_on = matched_on
    verification.quartile = quartile

    if quartile is None:
        verification.verification_status = Q1Status.UNVERIFIED
        verification.notes = (
            f"The journal was found in {table.source_name} but the row carries no "
            "quartile value for the relevant year."
        )
        return verification

    if conflicting:
        verification.verification_status = Q1Status.CONFLICTING
        verification.notes = (
            "The journal holds different quartiles across subject categories "
            f"({'; '.join(categories) or 'categories not stated'}). The reviewer must "
            "choose the category relevant to this paper."
        )
        return verification

    if verification.ranking_year and record.year and verification.ranking_year != record.year:
        verification.notes = (
            f"Quartile taken from the {verification.ranking_year} ranking because no "
            f"{record.year} row was available for this journal."
        )

    verification.verification_status = (
        Q1Status.VERIFIED_Q1 if quartile == "Q1" else Q1Status.VERIFIED_NON_Q1
    )
    return verification


def verify_records(
    records: list[PaperRecord],
    settings: Settings,
    *,
    table: RankingTable | None = None,
    ranking_file: str | Path | None = None,
) -> dict[str, int]:
    """Attach quartile evidence to every record and return the tallies."""
    if table is None:
        table = load_ranking_table(settings, ranking_file)

    counters = {status.value: 0 for status in Q1Status}
    for record in records:
        record.q1 = verify_record(record, table, settings)
        counters[record.q1.verification_status.value] += 1

    LOG.info(
        "Q1 verification: "
        + ", ".join(f"{name}={count}" for name, count in counters.items() if count)
    )
    return counters


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


@dataclass
class SelectionResult:
    """Which papers were included, and which await quartile verification."""

    selected: list[PaperRecord] = field(default_factory=list)
    pending_q1: list[PaperRecord] = field(default_factory=list)
    rejected: list[PaperRecord] = field(default_factory=list)
    counters: dict[str, int] = field(default_factory=dict)


def select_records(
    records: list[PaperRecord],
    config: JobConfig,
    settings: Settings,
) -> SelectionResult:
    """Apply the inclusion rules and the Q1 mode to choose the review set.

    In ``q1_mode=only``, papers whose quartile could not be verified are placed
    in ``pending_q1`` — they are never silently treated as Q1.
    """
    selection_config = settings.selection
    min_score = float(selection_config.get("min_relevance_score", 0.10))
    prefer_open_access = bool(selection_config.get("prefer_open_access", True))

    result = SelectionResult()
    eligible: list[PaperRecord] = []

    for record in records:
        if record.relevance_score < min_score:
            record.selected = False
            record.selection_reason = (
                f"Relevance score {record.relevance_score:.2f} is below the "
                f"minimum {min_score:.2f}."
            )
            result.rejected.append(record)
            continue

        status = record.q1.verification_status
        if config.q1_mode == Q1Mode.ONLY:
            if status == Q1Status.VERIFIED_Q1:
                eligible.append(record)
            elif status in (Q1Status.UNVERIFIED, Q1Status.CONFLICTING):
                record.pending_q1_verification = True
                record.selection_reason = (
                    f"Q1-only mode: quartile status is '{status.value}', so this "
                    "paper awaits manual quartile verification instead of being "
                    "assumed Q1."
                )
                result.pending_q1.append(record)
            else:
                record.selected = False
                record.selection_reason = (
                    f"Q1-only mode: quartile status is '{status.value}'."
                )
                result.rejected.append(record)
        else:
            eligible.append(record)

    def sort_key(record: PaperRecord) -> tuple:
        """Rank eligible papers: Q1 first (in preferred mode), then relevance."""
        q1_rank = 0
        if config.q1_mode == Q1Mode.PREFERRED:
            order = {
                Q1Status.VERIFIED_Q1: 0,
                Q1Status.UNVERIFIED: 1,
                Q1Status.CONFLICTING: 1,
                Q1Status.NOT_APPLICABLE: 2,
                Q1Status.VERIFIED_NON_Q1: 2,
            }
            q1_rank = order.get(record.q1.verification_status, 3)
        open_access_rank = 0
        if prefer_open_access:
            open_access_rank = 0 if record.candidate_pdf_urls else 1
        return (q1_rank, open_access_rank, -record.relevance_score, -(record.year or 0))

    eligible.sort(key=sort_key)
    chosen = eligible[: config.maximum_papers]
    for record in chosen:
        record.selected = True
        parts = [f"relevance {record.relevance_score:.2f}"]
        if config.q1_mode != Q1Mode.IGNORE:
            parts.append(f"Q1 status '{record.q1.verification_status.value}'")
        if record.candidate_pdf_urls:
            parts.append("legal open-access copy available")
        record.selection_reason = "Included: " + ", ".join(parts) + "."

    for record in eligible[config.maximum_papers :]:
        record.selected = False
        record.selection_reason = (
            f"Ranked below the maximum of {config.maximum_papers} papers for this job."
        )
        result.rejected.append(record)

    result.selected = chosen
    result.counters = {
        "eligible": len(eligible),
        "selected": len(chosen),
        "pending_q1_verification": len(result.pending_q1),
        "rejected": len(result.rejected),
        "verified_q1_selected": sum(
            1 for r in chosen if r.q1.verification_status == Q1Status.VERIFIED_Q1
        ),
    }
    LOG.info(
        f"Selection: {len(chosen)} of {len(records)} papers included "
        f"({result.counters['verified_q1_selected']} verified Q1, "
        f"{len(result.pending_q1)} pending quartile verification)."
    )
    return result


def write_pending_q1_csv(records: list[PaperRecord], path: Path, settings: Settings) -> Path:
    """Write the pending-verification list used in ``q1_mode=only``."""
    import csv

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "record_id", "title", "journal", "issn", "eissn", "year", "doi",
        "quartile_found", "verification_status", "ranking_source", "notes",
        "recommended_action",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for record in records:
            writer.writerow(
                {
                    "record_id": record.record_id,
                    "title": record.title,
                    "journal": record.journal,
                    "issn": record.issn or "",
                    "eissn": record.eissn or "",
                    "year": record.year or "",
                    "doi": record.doi or "",
                    "quartile_found": record.q1.quartile or "",
                    "verification_status": record.q1.verification_status.value,
                    "ranking_source": record.q1.ranking_source or "none configured",
                    "notes": record.q1.notes,
                    "recommended_action": (
                        "Check this journal and year in your licensed Scimago or JCR "
                        "subscription, then re-run 'verify' with the ranking file set."
                    ),
                }
            )
    LOG.info(f"Wrote {len(records)} pending-verification candidates to {path.name}.")
    return path
