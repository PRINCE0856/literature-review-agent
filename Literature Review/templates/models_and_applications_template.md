# Models and Applications Template

Structure used by `Models_and_Applications.docx`. Models are grouped by family,
and each is described attribute by attribute.

---

## Overview table

| Model family | Models in family | Papers |
| --- | --- | --- |
| {category} | {model names} | {count} |

## Per family

A plain-language explanation of how the family works, written for a reader who
does not know the method.

## Per model

| Attribute | Source of the value |
| --- | --- |
| Model category | Method vocabulary classification |
| Papers using it | Count from the analysed records |
| Citations | The papers that used it |
| Main assumptions | Only what the papers state |
| Required inputs | The papers' data sources |
| Outputs | The papers' dependent variables |
| Study application | The papers' research objectives |
| Software used | Named in the papers' methods |
| Calibration approach | Only what the papers state |
| Validation approach | Only what the papers state |
| Advantages | General property of the method family |
| Limitations | General property of the method family |
| How it works (plain language) | Method family explanation |

Any attribute the papers do not report reads "Not stated in the reviewed
papers."

## The note that follows every model

> Advantages and limitations describe the method family in general. They are
> not claims made by the cited papers, which are cited only for how they
> applied the method.

---

## Plain-language explanation rules

Write for a reader outside the field. Two or three sentences. Say what the
model represents and what it produces — not its equations.

| Family | Explanation shape |
| --- | --- |
| Discrete choice | A person chooses one option; attributes shift the probability |
| Regression | A line through the data, holding other factors constant |
| Panel data | The same units over time, separating within from between variation |
| Causal inference | Groups differing only in the exposure of interest |
| Machine learning | Learns patterns, judged on unseen data; predicts without explaining cause |
| Simulation | A computational world with explicit rules, run forward |
| Optimisation | The best combination of decisions under stated constraints |
| Process-based | Physical or engineering processes as equations |

## Rules

- **Never attribute a general method property to a paper.** Keep the
  distinction between "this method family assumes X" and "this paper says X".
- **Never describe a model a paper did not name.** The verifier checks each
  attributed method against the paper's own text.
- **An unreported attribute stays unreported.** Do not fill in the calibration
  approach because the method usually needs one.
- **Group before you list.** Six variants of one family read better as a family
  with variants than as six unrelated models.
