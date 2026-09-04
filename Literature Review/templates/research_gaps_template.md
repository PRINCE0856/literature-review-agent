# Research Gaps Template

Structure used by `Research_Gaps.docx`. The eleven categories are always
present; a category with no gaps says so rather than being omitted.

---

## How to read this document

{n_author_stated} of {n_total} gap statements are the authors' own words. The
remaining {n_inferred} are the agent's inferences from what the reviewed papers
do and do not report. The two are never merged.

## The eleven categories

| # | Category | What belongs there |
| --- | --- | --- |
| 1 | Author-stated gaps | The authors' own "future research should..." statements |
| 2 | Methodological gaps | Identification, estimation, specification, bias |
| 3 | Data gaps | Coverage, resolution, measurement, availability |
| 4 | Geographic gaps | Contexts absent from the reviewed evidence |
| 5 | Population or sample gaps | Groups not studied |
| 6 | Model limitations | Assumptions and simplifications the authors state |
| 7 | Validation gaps | Missing robustness, out-of-sample, or sensitivity work |
| 8 | Application gaps | Distance between findings and practice |
| 9 | Policy gaps | Decisions the evidence cannot yet inform |
| 10 | Contradictory findings | Papers whose results run in different directions |
| 11 | Agent-inferred gaps | Inferences from observed absences only |

## Per-gap format

```
- {gap statement} {citation group}
  Evidence: {stance}. Ledger IDs: {evidence_ids}. Gap ID: {gap_id}.
```

Every gap names its supporting papers and the ledger records that link it to
page-level evidence.

---

## Rules

- **An author-stated gap must be the authors' words.** If you are summarising
  or interpreting, it is an agent-inferred gap.
- **An agent-inferred gap must rest on an observed absence**, not on an
  expectation of what a paper should have done. "No validation strategy is
  reported" is inferable; "the model is probably misspecified" is not.
- **Contradictions are reported, not resolved.** Cite both sides and state that
  the papers do not reconcile the difference.
- **A geographic gap describes this evidence base.** "Europe is not represented
  in the reviewed evidence" is supportable; "no European research exists" is not.
- **Every gap carries citations.** A gap with no supporting paper does not
  belong in the document.
