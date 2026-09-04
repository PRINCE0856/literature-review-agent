---
name: literature-review
description: >
  Run a complete, verifiable literature review on a research topic: develop
  search keywords and Boolean strings, search scholarly databases, identify Q1
  journals, download legally accessible PDFs named by paper title, extract and
  analyse the papers, build an Excel literature-review matrix, and write an
  introduction, research gaps, global research landscape, models and
  applications, and paper summaries as Word documents, with independent
  verification of all metadata, citations, claims, and files. Outputs are stored
  in Google Drive. Use when someone asks for a literature review, systematic
  review, evidence synthesis, research gap analysis, state of the art, or asks
  to find and analyse academic papers on a topic. Also use to resume an
  interrupted review or to re-run verification on an existing one.
---

# Literature Review

Runs a resumable, verification-first literature review. Every claim in the
output traces to a page in a retrieved paper.

## Before you start

Check the environment once:

```bash
python -m literature_review_agent init
```

This reports which search sources are available, whether Google Drive is
configured, and whether journal-ranking data is present. Nothing here blocks a
review: Crossref, OpenAlex, Semantic Scholar, Europe PMC, arXiv and Unpaywall
all work with no credentials.

## Run a review

```bash
python -m literature_review_agent run \
  --topic "Effect of rainfall on urban travel behaviour" \
  --research-question "How does rainfall intensity influence mode choice?" \
  --year-from 2015 --year-to 2026 --max-papers 50 --q1-mode preferred
```

Useful options: `--geography India`, `--keyword "mode choice"` (repeatable),
`--exclude laboratory` (repeatable), `--citation-style "IEEE"`,
`--ranking-file config/scimago_2024.csv`, `--no-drive`.

The command prints the job folder. Every later command takes it as `--job`.

## Defaults — do not interrogate the user

Apply these, record them in `job_config.yaml`, and state them in the final
message:

| Setting | Default |
| --- | --- |
| `year_from` | 2015 |
| `year_to` | current year |
| `maximum_papers` | 50 |
| `q1_mode` | `preferred` |
| `language` | English |
| `paper_types` | journal article |
| `citation_style` | APA 7 |
| `geography` | global |

Ask only when no sensible default exists and the answer changes the work.

## Resume an interrupted review

Interruptions are expected: rate limits, dropped connections, a killed process.
Nothing is lost and nothing is repeated.

```bash
python -m literature_review_agent status --job "05 Logs and State/<date>/<slug>"
python -m literature_review_agent resume --job "05 Logs and State/<date>/<slug>"
```

Resume skips completed stages and continues long stages at the item they
stopped on, so an interrupted download of 50 papers picks up at paper 31.

## Run one stage at a time

```bash
python -m literature_review_agent keywords --job JOB   # vocabulary and Boolean strings
python -m literature_review_agent search   --job JOB   # search, dedupe, Q1, select
python -m literature_review_agent download --job JOB   # legal PDF retrieval
python -m literature_review_agent extract  --job JOB   # page-marked text
python -m literature_review_agent analyse  --job JOB   # per-paper analysis
python -m literature_review_agent report   --job JOB   # Excel and Word outputs
python -m literature_review_agent verify   --job JOB   # independent verification
```

Add `--force` to re-run a completed stage.

## What you get

```
01 Keywords/<date>/<slug>/     keywords.md, keywords.csv, search_strings.md,
                               inclusion_exclusion_criteria.md
02 Literature Papers/<date>/<slug>/
                               Downloaded Papers/Paper Title.pdf
                               Extracted Text/Paper Title.txt
                               Unable to Download/Unable_to_Download.docx
                               paper_manifest.csv/.json, references.bib/.ris
03 Reports/<date>/<slug>/      Literature_Review_Matrix.xlsx (10 sheets)
                               Introduction.docx, Research_Gaps.docx,
                               Global_Research_Landscape.docx,
                               Models_and_Applications.docx, Paper_Summaries.docx
04 Verification/<date>/<slug>/ Evidence_Ledger.xlsx, Verification_Report.docx,
                               Citation_Audit.xlsx, unresolved_issues.csv
05 Logs and State/<date>/<slug>/
                               pipeline.log, search_log.jsonl, download_log.jsonl,
                               checkpoints.json, job_config.yaml, drive_manifest.json
```

## Google Drive

All outputs belong in Drive; local folders are staging. A file counts as
delivered only when Drive returns a file ID and the size and checksum checks
pass.

```bash
python -m literature_review_agent drive-status              # what is configured
python -m literature_review_agent drive-login               # authorise once
python -m literature_review_agent drive-sync --job JOB      # retry pending uploads
```

If Drive is not configured, `drive-status` prints the exact steps. Relay them
and stop; do not ask for credential contents in chat, and do not claim anything
was uploaded.

## Rules you must hold to

**Never fabricate.** No invented citation, quartile, page number, statistic, or
finding. Where something could not be established, the output says so.

**Only legal retrieval.** PDFs come from open-access locations or an
API-authorised publisher URL. Never bypass a paywall, login, CAPTCHA, or
anti-bot protection. Never use Sci-Hub or a comparable mirror. Never scrape
Google Scholar. Papers that cannot be obtained legally go to
`Unable_to_Download.docx` with a recommended manual action.

**Never guess a quartile.** A journal is Q1 only when a licensed ranking file
says so, for a stated year. Fame, impact factor, and publisher are not
evidence. With no ranking file every paper is `Unverified`, and you tell the
user that.

**Keep inference visible.** Analysis fields carry one of: `Author explicitly
states this`, `Agent inference based on evidence`, `Information not reported`,
`Information could not be verified`. The distinction reaches the final
documents.

**Describe the sample, not the world.** Write "within the reviewed evidence" or
"in these N papers". Never "research shows" on the strength of a retrieved
sample.

**Verification is independent.** The agent that analysed the papers does not
certify them. Do not declare a review complete while verification fails —
either fix the problem or document it as an unresolved limitation.

## Subagents

Delegate bounded work rather than doing everything in one context:
`keyword-strategy-agent`, `paper-discovery-agent`, `download-agent`,
`paper-analysis-agent`, `metadata-q1-verifier`, `evidence-citation-verifier`,
`synthesis-report-agent`. Each writes to its own workspace under
`agent_workspace/`.

## Notebook

`notebooks/literature_review_pipeline.ipynb` runs the same pipeline
step by step with editable inputs at the top. It calls the package functions
and holds no research logic of its own.

## If something goes wrong

```bash
python -m literature_review_agent doctor          # diagnose the environment
python -m literature_review_agent jobs            # list every job
```

- **No results**: widen the year range, or ask `keyword-strategy-agent` to
  broaden the vocabulary.
- **Every download failed**: usually a closed-access topic. Check
  `Unable_to_Download.docx`; the papers need the user's own institutional access.
- **PDFs need OCR**: they were scans with no text layer. They are flagged and
  excluded from evidence-based claims rather than guessed at.
- **A stage failed**: `resume` continues from the checkpoint.
