"""The evidence ledger: every synthesis claim tied back to its source.

The rule the whole report layer depends on: **a substantive statement in a
synthesis document must map to at least one ledger record.** The ledger stores
the claim, the paper it came from, the page numbers, the supporting text, and
whether the authors said it or the agent inferred it.

The citation verifier audits reports against this ledger, so a claim that never
reached the ledger is reported as unsupported rather than quietly published.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .logging_setup import get_logger
from .schemas import EvidenceRecord, EvidenceStance, PaperAnalysis, PaperRecord
from .utils import stable_id, truncate_text, write_json

LOG = get_logger("evidence")


@dataclass
class EvidenceLedger:
    """An append-only collection of evidence records for one job."""

    records: list[EvidenceRecord] = field(default_factory=list)
    _by_id: dict[str, EvidenceRecord] = field(default_factory=dict, repr=False)

    def add(
        self,
        *,
        document: str,
        section: str,
        claim: str,
        record_id: str,
        doi: str | None = None,
        in_text_citation: str = "",
        field_name: str = "",
        stance: EvidenceStance = EvidenceStance.AUTHOR_STATED,
        pages: list[int] | None = None,
        supporting_text: str = "",
        confidence: str = "Medium",
    ) -> EvidenceRecord:
        """Add one evidence record and return it.

        Identical claims from the same paper in the same document section are
        deduplicated, so a repeated statement does not inflate the audit.
        """
        evidence_id = "EV-" + stable_id(document, section, claim, record_id, field_name)
        if existing := self._by_id.get(evidence_id):
            for page in pages or []:
                if page not in existing.pages:
                    existing.pages.append(page)
            return existing

        record = EvidenceRecord(
            evidence_id=evidence_id,
            document=document,
            section=section,
            claim=claim.strip(),
            record_id=record_id,
            doi=doi,
            in_text_citation=in_text_citation,
            field_name=field_name,
            stance=stance,
            pages=sorted(set(pages or [])),
            supporting_text=truncate_text(supporting_text, 800),
            confidence=confidence,
        )
        self.records.append(record)
        self._by_id[evidence_id] = record
        return record

    def add_from_analysis(
        self,
        analysis: PaperAnalysis,
        field_name: str,
        *,
        document: str,
        section: str,
        in_text_citation: str,
        claim: str | None = None,
    ) -> EvidenceRecord | None:
        """Create a ledger record from one analysed field.

        Returns ``None`` when the field carries nothing usable, which is what
        stops an unreported field from becoming a cited claim.
        """
        evidence_field = analysis.field(field_name)
        if not evidence_field.is_reported:
            return None
        return self.add(
            document=document,
            section=section,
            claim=claim or evidence_field.value,
            record_id=analysis.record_id,
            doi=analysis.doi,
            in_text_citation=in_text_citation,
            field_name=field_name,
            stance=evidence_field.stance,
            pages=evidence_field.pages,
            supporting_text=evidence_field.quote,
            confidence=evidence_field.confidence,
        )

    # -- queries --------------------------------------------------------

    def get(self, evidence_id: str) -> EvidenceRecord | None:
        """Return one record by id."""
        return self._by_id.get(evidence_id)

    def for_document(self, document: str) -> list[EvidenceRecord]:
        """Every record supporting claims in one document."""
        return [r for r in self.records if r.document == document]

    def for_record(self, record_id: str) -> list[EvidenceRecord]:
        """Every record derived from one paper."""
        return [r for r in self.records if r.record_id == record_id]

    def cited_record_ids(self) -> set[str]:
        """Papers that at least one claim depends on."""
        return {r.record_id for r in self.records}

    def documents(self) -> list[str]:
        """Documents the ledger covers, in first-seen order."""
        seen: list[str] = []
        for record in self.records:
            if record.document not in seen:
                seen.append(record.document)
        return seen

    def counts_by_stance(self) -> dict[str, int]:
        """How many claims rest on author statements versus inference."""
        counts: dict[str, int] = {}
        for record in self.records:
            counts[record.stance.value] = counts.get(record.stance.value, 0) + 1
        return counts

    def claims_without_pages(self) -> list[EvidenceRecord]:
        """Author-stated claims lacking a page reference.

        These are reported as warnings: the claim may be sound, but a reviewer
        cannot check it quickly without a page number.
        """
        return [
            r
            for r in self.records
            if r.stance == EvidenceStance.AUTHOR_STATED and not r.pages
        ]

    # -- persistence ----------------------------------------------------

    def to_list(self) -> list[dict[str, Any]]:
        """Serialise every record."""
        return [r.model_dump(mode="json") for r in self.records]

    def save(self, path: Path) -> Path:
        """Write the ledger to JSON."""
        return write_json(path, self.to_list())

    @classmethod
    def load(cls, path: Path) -> EvidenceLedger:
        """Load a ledger previously written by :meth:`save`."""
        from .utils import read_json

        ledger = cls()
        for raw in read_json(Path(path), []) or []:
            try:
                record = EvidenceRecord.model_validate(raw)
            except Exception as exc:  # noqa: BLE001 - skip a corrupt row, keep the rest
                LOG.debug(f"Skipping unreadable evidence record: {exc}")
                continue
            ledger.records.append(record)
            ledger._by_id[record.evidence_id] = record
        return ledger

    def rows(self) -> list[dict[str, Any]]:
        """Rows for ``Evidence_Ledger.xlsx``."""
        return [
            {
                "serial": index,
                "evidence_id": record.evidence_id,
                "document": record.document,
                "section": record.section,
                "claim": record.claim,
                "in_text_citation": record.in_text_citation,
                "record_id": record.record_id,
                "doi": record.doi or "",
                "field_name": record.field_name,
                "stance": record.stance.value,
                "pages": ", ".join(str(p) for p in record.pages) or "not recorded",
                "supporting_text": record.supporting_text,
                "confidence": record.confidence,
                "created_at": record.created_at,
            }
            for index, record in enumerate(self.records, 1)
        ]


def build_paper_index(records: list[PaperRecord]) -> dict[str, PaperRecord]:
    """Index papers by record id for fast ledger lookups."""
    return {record.record_id: record for record in records}
