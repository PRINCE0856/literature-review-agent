# Global Research Landscape Template

Structure used by `Global_Research_Landscape.docx`. Every count refers to the
reviewed papers, never to world research.

---

## The warning that opens the document

> This is a description of the reviewed evidence, not of world research. A
> country, method, or dataset appearing rarely here means it is rare in this
> retrieved set; it does not establish that little research exists elsewhere.

## Sections

| # | Section | Content | When empty |
| --- | --- | --- | --- |
| 1 | Countries and cities | Count table with share of reviewed evidence | State that no paper named an extractable location |
| 2 | Regions | Grouped counts | State that no region could be derived |
| 3 | Research institutions | Only verified values | State that affiliations are not retrieved by this pipeline |
| 4 | Dominant applications | Applied contexts named | State that none was extractable |
| 5 | Common datasets | Recognised dataset types | State that none was named |
| 6 | Common methods | Method counts | State that none was identified |
| 7 | Emerging methods | Methods only in recent papers | State that none is confined to recent papers |
| 8 | Temporal trends | Year counts, with an indexing caveat | State that no years were recorded |
| 9 | Publication venues | Journal counts | State that no journals were recorded |
| 10 | Under-represented areas | Absences in this set | State that every region checked appears |
| 11 | Global and local contexts | Where the evidence is weighted, versus the requested geography | State that no location could be extracted |

## Table format

| Item | Papers | Share of reviewed evidence |
| --- | --- | --- |
| {item} | {count} | {percentage} |

---

## Rules

- **Never describe the sample as the world.** Every count is prefaced or
  suffixed with "within the reviewed evidence".
- **Institutions require verified data.** Author affiliations are not retrieved,
  so the section says so. It does not guess from publisher names.
- **Publication counts are not research activity.** They reflect both the
  research done and what the searched databases index. Say so.
- **An absence is an absence in this set.** Confirming a genuine research gap
  needs a search designed for it.
- **Emerging is a suggestion, not a finding.** With a small set, a method
  appearing only in recent papers is suggestive at best.
- **Local transferability is untested unless a paper tested it.** Findings from
  other settings are transferable hypotheses, not established local results.
