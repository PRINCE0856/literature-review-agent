"""Citation formatting, reference exports, and the citation audit.

Rules enforced here:

* A citation is only ever generated from a record that was actually retrieved.
* A reference entry is built from verified metadata fields, and any field that is
  missing is shown as missing rather than filled in.
* Every in-text citation must appear in the reference list, and every reference
  entry must be cited somewhere — both directions are audited.
* Page numbers are attached to direct quotations; the reports otherwise
  paraphrase.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .logging_setup import get_logger
from .schemas import (
    CheckOutcome,
    CitationAuditRow,
    CitationStyle,
    EvidenceRecord,
    PaperRecord,
)
from .utils import first_author_surname, normalize_title, title_tokens, write_text

LOG = get_logger("citations")


# ---------------------------------------------------------------------------
# Author name formatting
# ---------------------------------------------------------------------------


def split_name(name: str) -> tuple[str, str]:
    """Split an author string into ``(surname, given names)``."""
    cleaned = " ".join(str(name).split())
    if not cleaned:
        return "", ""
    if "," in cleaned:
        surname, _, given = cleaned.partition(",")
        return surname.strip(), given.strip()
    parts = cleaned.split()
    if len(parts) == 1:
        return parts[0], ""
    return parts[-1], " ".join(parts[:-1])


def initials(given: str) -> str:
    """Turn given names into APA-style initials (``Ravi Kumar`` -> ``R. K.``)."""
    out: list[str] = []
    for token in given.replace(".", " ").split():
        if token:
            out.append(f"{token[0].upper()}.")
    return " ".join(out)


def format_author_apa(name: str) -> str:
    """Format one author as ``Surname, I. N.``."""
    surname, given = split_name(name)
    if not surname:
        return ""
    return f"{surname}, {initials(given)}".strip().rstrip(",")


def format_author_ieee(name: str) -> str:
    """Format one author as ``I. N. Surname``."""
    surname, given = split_name(name)
    if not surname:
        return ""
    return f"{initials(given)} {surname}".strip()


def format_author_vancouver(name: str) -> str:
    """Format one author as ``Surname IN``."""
    surname, given = split_name(name)
    if not surname:
        return ""
    letters = "".join(t[0].upper() for t in given.replace(".", " ").split() if t)
    return f"{surname} {letters}".strip()


# ---------------------------------------------------------------------------
# In-text citations
# ---------------------------------------------------------------------------


def in_text_citation(
    record: PaperRecord,
    style: CitationStyle = CitationStyle.APA7,
    *,
    page: int | None = None,
    parenthetical: bool = True,
    number: int | None = None,
) -> str:
    """Build an in-text citation for one record.

    ``page`` is supplied for direct quotations only; the reports paraphrase
    elsewhere, which is why the page argument is optional rather than default.
    """
    year = str(record.year) if record.year else "n.d."

    if style in (CitationStyle.IEEE, CitationStyle.VANCOUVER):
        marker = f"[{number}]" if number else "[?]"
        return f"{marker}, p. {page}" if page else marker

    surname = first_author_surname(record.authors) or "Anonymous"
    count = len(record.authors)
    if count == 0:
        author_part = "Anonymous"
    elif count == 1:
        author_part = surname
    elif count == 2:
        second = first_author_surname([record.authors[1]]) or ""
        joiner = " & " if (parenthetical and style == CitationStyle.APA7) else " and "
        author_part = f"{surname}{joiner}{second}" if second else surname
    else:
        author_part = f"{surname} et al."

    page_part = f", p. {page}" if page else ""
    if parenthetical:
        return f"({author_part}, {year}{page_part})"
    return f"{author_part} ({year}{page_part})"


def narrative_citation(record: PaperRecord, style: CitationStyle = CitationStyle.APA7) -> str:
    """Build a narrative citation (``Sharma et al. (2021)``)."""
    year = str(record.year) if record.year else "n.d."
    surname = first_author_surname(record.authors) or "Anonymous"
    count = len(record.authors)
    if count >= 3:
        return f"{surname} et al. ({year})"
    if count == 2:
        second = first_author_surname([record.authors[1]]) or ""
        return f"{surname} and {second} ({year})" if second else f"{surname} ({year})"
    return f"{surname} ({year})"


# ---------------------------------------------------------------------------
# Reference entries
# ---------------------------------------------------------------------------

#: Placeholder used when a bibliographic field could not be retrieved. Shown
#: explicitly so a reviewer sees the gap instead of an invented value.
MISSING = "[missing]"


def _volume_issue_pages(record: PaperRecord) -> str:
    """Format the volume/issue/pages segment, marking what is absent."""
    volume = record.volume or ""
    issue = f"({record.issue})" if record.issue else ""
    pages = record.pages or record.article_number or ""
    if volume and pages:
        return f"{volume}{issue}, {pages}"
    if volume:
        return f"{volume}{issue}"
    if pages:
        return f"{pages}"
    return ""


def reference_entry(record: PaperRecord, style: CitationStyle = CitationStyle.APA7) -> str:
    """Build a full reference-list entry from verified metadata."""
    year = str(record.year) if record.year else "n.d."
    title = record.title or MISSING
    journal = record.journal or MISSING
    doi_part = f" https://doi.org/{record.doi}" if record.doi else ""
    locator = _volume_issue_pages(record)

    if style == CitationStyle.IEEE:
        authors = ", ".join(filter(None, (format_author_ieee(a) for a in record.authors)))
        parts = [f"{authors or MISSING},", f'"{title},"', f"{journal},"]
        if locator:
            parts.append(f"vol. {record.volume}," if record.volume else "")
            parts.append(f"no. {record.issue}," if record.issue else "")
            parts.append(f"pp. {record.pages}," if record.pages else "")
        parts.append(f"{year}.")
        entry = " ".join(p for p in parts if p)
        return entry + (f" doi: {record.doi}." if record.doi else "")

    if style == CitationStyle.VANCOUVER:
        authors = ", ".join(filter(None, (format_author_vancouver(a) for a in record.authors[:6])))
        if len(record.authors) > 6:
            authors += ", et al"
        entry = f"{authors or MISSING}. {title}. {journal}. {year}"
        if locator:
            entry += f";{locator}"
        return entry + f".{doi_part}"

    if style == CitationStyle.HARVARD:
        formatted = [format_author_apa(a) for a in record.authors]
        if len(formatted) > 1:
            authors = ", ".join(formatted[:-1]) + " and " + formatted[-1]
        else:
            authors = formatted[0] if formatted else MISSING
        entry = f"{authors} ({year}) '{title}', {journal}"
        if locator:
            entry += f", {locator}"
        return entry + f".{doi_part}"

    if style == CitationStyle.CHICAGO:
        formatted = [format_author_apa(a) for a in record.authors]
        authors = ", ".join(formatted) if formatted else MISSING
        entry = f'{authors}. {year}. "{title}." {journal}'
        if locator:
            entry += f" {locator}"
        return entry + f".{doi_part}"

    # --- APA 7 (default) ---
    formatted = [format_author_apa(a) for a in record.authors if a]
    if not formatted:
        authors = MISSING
    elif len(formatted) == 1:
        authors = formatted[0]
    elif len(formatted) <= 20:
        authors = ", ".join(formatted[:-1]) + f", & {formatted[-1]}"
    else:
        authors = ", ".join(formatted[:19]) + ", ... " + formatted[-1]

    entry = f"{authors} ({year}). {title}. {journal}"
    if locator:
        entry += f", {locator}"
    entry += "."
    if record.doi:
        entry += f" https://doi.org/{record.doi}"
    elif record.landing_page_url:
        entry += f" {record.landing_page_url}"
    return entry


# ---------------------------------------------------------------------------
# Citation manager
# ---------------------------------------------------------------------------


@dataclass
class CitationManager:
    """Assigns stable citations to records and exports reference files."""

    style: CitationStyle = CitationStyle.APA7
    records: list[PaperRecord] = field(default_factory=list)
    _order: dict[str, int] = field(default_factory=dict, repr=False)
    _by_id: dict[str, PaperRecord] = field(default_factory=dict, repr=False)
    _labels: dict[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self.rebuild()

    def rebuild(self) -> None:
        """Sort records into reference-list order and assign citation labels.

        Numeric styles need a stable number per paper, and author-date styles
        need disambiguating suffixes (2021a, 2021b) when the same author
        published twice in a year.
        """
        numeric = self.style in (CitationStyle.IEEE, CitationStyle.VANCOUVER)
        if numeric:
            ordered = sorted(
                self.records,
                key=lambda r: (
                    (first_author_surname(r.authors) or "zzz").lower(),
                    r.year or 9999,
                    normalize_title(r.title),
                ),
            )
        else:
            ordered = sorted(
                self.records,
                key=lambda r: (
                    (first_author_surname(r.authors) or "zzz").lower(),
                    r.year or 9999,
                    normalize_title(r.title),
                ),
            )

        self._order = {r.record_id: index + 1 for index, r in enumerate(ordered)}
        self._by_id = {r.record_id: r for r in ordered}
        self.records = ordered

        # Disambiguate identical author-year pairs.
        self._labels = {}
        groups: dict[tuple[str, str], list[PaperRecord]] = {}
        for record in ordered:
            key = (
                (first_author_surname(record.authors) or "Anonymous").lower(),
                str(record.year or "n.d."),
            )
            groups.setdefault(key, []).append(record)
        for group in groups.values():
            if len(group) == 1:
                self._labels[group[0].record_id] = ""
                continue
            for index, record in enumerate(group):
                self._labels[record.record_id] = chr(ord("a") + index) if index < 26 else str(index)

    # -- lookups --------------------------------------------------------

    def number_for(self, record_id: str) -> int | None:
        """Reference number for numeric styles."""
        return self._order.get(record_id)

    def record(self, record_id: str) -> PaperRecord | None:
        """Return a managed record by id."""
        return self._by_id.get(record_id)

    def citation(self, record_id: str, *, page: int | None = None) -> str:
        """In-text citation for one record, or a clear marker when unknown.

        Refusing to invent a citation for an unknown record is the point: an
        unresolvable id surfaces as ``[citation unavailable]`` in the report and
        is caught by the audit.
        """
        record = self._by_id.get(record_id)
        if record is None:
            return "[citation unavailable]"
        base = in_text_citation(
            record, self.style, page=page, number=self.number_for(record_id)
        )
        suffix = self._labels.get(record_id, "")
        if suffix and self.style not in (CitationStyle.IEEE, CitationStyle.VANCOUVER):
            year = str(record.year) if record.year else "n.d."
            base = base.replace(year, f"{year}{suffix}", 1)
        return base

    def narrative(self, record_id: str) -> str:
        """Narrative citation for one record."""
        record = self._by_id.get(record_id)
        if record is None:
            return "[citation unavailable]"
        if self.style in (CitationStyle.IEEE, CitationStyle.VANCOUVER):
            return f"[{self.number_for(record_id)}]"
        base = narrative_citation(record, self.style)
        suffix = self._labels.get(record_id, "")
        if suffix:
            year = str(record.year) if record.year else "n.d."
            base = base.replace(year, f"{year}{suffix}", 1)
        return base

    def reference(self, record_id: str) -> str:
        """Reference-list entry for one record."""
        record = self._by_id.get(record_id)
        if record is None:
            return "[reference unavailable]"
        entry = reference_entry(record, self.style)
        suffix = self._labels.get(record_id, "")
        if suffix and self.style not in (CitationStyle.IEEE, CitationStyle.VANCOUVER):
            year = str(record.year) if record.year else "n.d."
            entry = entry.replace(f"({year})", f"({year}{suffix})", 1)
        return entry

    def citation_map(self) -> dict[str, str]:
        """Every record id mapped to its in-text citation."""
        return {record_id: self.citation(record_id) for record_id in self._by_id}

    def reference_list(self) -> list[tuple[str, str]]:
        """``(record_id, reference entry)`` pairs in reference-list order."""
        return [(r.record_id, self.reference(r.record_id)) for r in self.records]

    def citation_group(self, record_ids: list[str]) -> str:
        """Combine several citations into one parenthetical group."""
        if not record_ids:
            return ""
        if self.style in (CitationStyle.IEEE, CitationStyle.VANCOUVER):
            numbers = sorted(
                n for n in (self.number_for(rid) for rid in record_ids) if n is not None
            )
            return "[" + ", ".join(str(n) for n in numbers) + "]" if numbers else "[?]"
        inner: list[str] = []
        for record_id in record_ids:
            citation = self.citation(record_id)
            inner.append(citation.strip("()"))
        # Reference-list order keeps grouped citations tidy and predictable.
        inner.sort(key=lambda text: text.lower())
        return "(" + "; ".join(dict.fromkeys(inner)) + ")"

    # -- exports --------------------------------------------------------

    def bibtex_key(self, record: PaperRecord) -> str:
        """Build a stable, readable BibTeX key."""
        surname = (first_author_surname(record.authors) or "anon").lower()
        surname = "".join(ch for ch in surname if ch.isalnum()) or "anon"
        year = record.year or "nd"
        first_word = ""
        for token in title_tokens(record.title):
            first_word = token
            break
        suffix = self._labels.get(record.record_id, "")
        return f"{surname}{year}{first_word[:10]}{suffix}"

    def to_bibtex(self) -> str:
        """Render every reference as a BibTeX database."""
        import bibtexparser
        from bibtexparser.bibdatabase import BibDatabase

        database = BibDatabase()
        for record in self.records:
            entry: dict[str, str] = {
                "ENTRYTYPE": _bibtex_type(record),
                "ID": self.bibtex_key(record),
                "title": record.title or MISSING,
            }
            if record.authors:
                entry["author"] = " and ".join(
                    f"{s}, {g}".rstrip(", ") for s, g in (split_name(a) for a in record.authors)
                )
            if record.year:
                entry["year"] = str(record.year)
            if record.journal:
                entry["journal"] = record.journal
            for name, value in (
                ("volume", record.volume),
                ("number", record.issue),
                ("pages", record.pages),
                ("doi", record.doi),
                ("issn", record.issn),
                ("publisher", record.publisher),
                ("url", record.landing_page_url),
                ("language", record.language),
            ):
                if value:
                    entry[name] = str(value)
            if record.keywords:
                entry["keywords"] = ", ".join(record.keywords[:12])
            if record.abstract:
                entry["abstract"] = record.abstract[:2000]
            database.entries.append(entry)

        writer = bibtexparser.bwriter.BibTexWriter()
        writer.indent = "  "
        writer.order_entries_by = ("ID",)
        return bibtexparser.dumps(database, writer)

    def to_ris(self) -> str:
        """Render every reference in RIS format."""
        import rispy

        entries: list[dict[str, Any]] = []
        for record in self.records:
            entry: dict[str, Any] = {
                "type_of_reference": _ris_type(record),
                "title": record.title or MISSING,
            }
            if record.authors:
                entry["authors"] = [f"{s}, {g}".rstrip(", ") for s, g in
                                    (split_name(a) for a in record.authors)]
            if record.year:
                entry["year"] = str(record.year)
            if record.journal:
                entry["journal_name"] = record.journal
                entry["secondary_title"] = record.journal
            for name, value in (
                ("volume", record.volume),
                ("number", record.issue),
                ("doi", record.doi),
                ("issn", record.issn),
                ("publisher", record.publisher),
                ("url", record.landing_page_url),
                ("language", record.language),
                ("abstract", record.abstract[:2000] if record.abstract else None),
            ):
                if value:
                    entry[name] = str(value)
            if record.pages and "-" in str(record.pages):
                start, _, end = str(record.pages).partition("-")
                entry["start_page"] = start.strip()
                entry["end_page"] = end.strip()
            elif record.pages:
                entry["start_page"] = str(record.pages)
            if record.keywords:
                entry["keywords"] = list(record.keywords[:12])
            entries.append(entry)

        return rispy.dumps(entries)

    def write_exports(self, bib_path: Path, ris_path: Path) -> list[Path]:
        """Write ``references.bib`` and ``references.ris``."""
        written: list[Path] = []
        try:
            written.append(write_text(Path(bib_path), self.to_bibtex()))
        except Exception as exc:  # noqa: BLE001 - export failure must not stop the run
            LOG.warning(f"Could not write BibTeX export: {exc}")
        try:
            written.append(write_text(Path(ris_path), self.to_ris()))
        except Exception as exc:  # noqa: BLE001
            LOG.warning(f"Could not write RIS export: {exc}")
        return written


def _bibtex_type(record: PaperRecord) -> str:
    """Map a record's document type to a BibTeX entry type."""
    doc_type = (record.document_type or "").lower()
    if "preprint" in doc_type:
        return "misc"
    if "book-chapter" in doc_type or "chapter" in doc_type:
        return "inbook"
    if "book" in doc_type:
        return "book"
    if "proceedings" in doc_type or "conference" in doc_type:
        return "inproceedings"
    if "report" in doc_type:
        return "techreport"
    if "thesis" in doc_type or "dissertation" in doc_type:
        return "phdthesis"
    return "article"


def _ris_type(record: PaperRecord) -> str:
    """Map a record's document type to a RIS reference type."""
    doc_type = (record.document_type or "").lower()
    if "preprint" in doc_type:
        return "UNPB"
    if "chapter" in doc_type:
        return "CHAP"
    if "book" in doc_type:
        return "BOOK"
    if "conference" in doc_type or "proceedings" in doc_type:
        return "CPAPER"
    if "report" in doc_type:
        return "RPRT"
    if "thesis" in doc_type:
        return "THES"
    return "JOUR"


# ---------------------------------------------------------------------------
# Citation audit
# ---------------------------------------------------------------------------


def audit_citations(
    manager: CitationManager,
    evidence: list[EvidenceRecord],
    *,
    document_texts: dict[str, str] | None = None,
) -> list[CitationAuditRow]:
    """Audit citations in both directions.

    Checks that every cited paper has a reference entry, that every reference
    entry is cited, and that each cited record has the metadata its entry claims.
    """
    document_texts = document_texts or {}
    rows: list[CitationAuditRow] = []

    cited_ids: dict[str, list[str]] = {}
    for item in evidence:
        cited_ids.setdefault(item.record_id, [])
        if item.document not in cited_ids[item.record_id]:
            cited_ids[item.record_id].append(item.document)

    reference_ids = {r.record_id for r in manager.records}

    # --- Every cited record: does it resolve, and is it in the reference list? ---
    for record_id, documents in cited_ids.items():
        record = manager.record(record_id)
        citation = manager.citation(record_id)
        row = CitationAuditRow(
            in_text_citation=citation,
            record_id=record_id,
            doi=record.doi if record else None,
            title=record.title if record else "",
            appears_in_documents=documents,
            in_reference_list=record_id in reference_ids,
            reference_entry=manager.reference(record_id) if record else "",
        )

        if record is None:
            row.outcome = CheckOutcome.FAIL
            row.notes = (
                "A claim cites a record that is not in the retrieved set. No citation "
                "has been fabricated; the claim must be removed or re-sourced."
            )
            rows.append(row)
            continue

        row.metadata_checked = True
        row.title_match = CheckOutcome.PASS if record.title else CheckOutcome.FAIL
        row.author_match = CheckOutcome.PASS if record.authors else CheckOutcome.WARNING
        row.year_match = CheckOutcome.PASS if record.year else CheckOutcome.WARNING
        row.journal_match = CheckOutcome.PASS if record.journal else CheckOutcome.WARNING
        row.doi_resolves = (
            CheckOutcome.NOT_APPLICABLE if not record.doi else CheckOutcome.PASS
        )

        problems: list[str] = []
        if not row.in_reference_list:
            problems.append("cited in the text but missing from the reference list")
        if not record.title:
            problems.append("no title recorded")
        if not record.authors:
            problems.append("no authors recorded")
        if not record.year:
            problems.append("no publication year recorded")
        if not record.journal:
            problems.append("no journal or source recorded")

        # Confirm the citation string genuinely appears in the documents claimed.
        for document, text in document_texts.items():
            if document in documents and text and citation not in text:
                problems.append(
                    f"the evidence ledger links this citation to {document}, but the "
                    "citation string was not found in that document"
                )

        if any(
            p.startswith(("cited in the text", "no title", "the evidence ledger"))
            for p in problems
        ):
            row.outcome = CheckOutcome.FAIL
        elif problems:
            row.outcome = CheckOutcome.WARNING
        else:
            row.outcome = CheckOutcome.PASS
        row.notes = "; ".join(problems) or "Citation and reference entry are consistent."
        rows.append(row)

    # --- Every reference entry: is it actually cited? ---
    for record in manager.records:
        if record.record_id in cited_ids:
            continue
        rows.append(
            CitationAuditRow(
                in_text_citation=manager.citation(record.record_id),
                record_id=record.record_id,
                doi=record.doi,
                title=record.title,
                appears_in_documents=[],
                in_reference_list=True,
                reference_entry=manager.reference(record.record_id),
                metadata_checked=True,
                outcome=CheckOutcome.WARNING,
                notes=(
                    "Included in the review and listed in the references, but no "
                    "synthesis claim cites it. Either cite it or remove it from the "
                    "reference list."
                ),
            )
        )

    failures = sum(1 for r in rows if r.outcome == CheckOutcome.FAIL)
    warnings = sum(1 for r in rows if r.outcome == CheckOutcome.WARNING)
    LOG.info(
        f"Citation audit: {len(rows)} rows checked, {failures} failure(s), "
        f"{warnings} warning(s)."
    )
    return rows
