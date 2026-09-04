# Literature Review Agent

A resumable, verification-first literature-review system. It develops a search
vocabulary, searches scholarly databases, verifies journal quartiles, downloads
legally accessible PDFs named by their paper titles, extracts and analyses them,
and writes an Excel review matrix plus five Word reports — with every claim
traceable to a page in a retrieved paper.

Built as a Claude Code project: one orchestrating agent, seven specialist
subagents, a reusable skill, a Python package, and a notebook.

---

## What it does

1. Develops keywords and database-ready Boolean search strings.
2. Searches eight scholarly sources and merges the results.
3. Prioritises and verifies Q1 journal papers.
4. Retrieves metadata from authoritative registries.
5. Downloads legally accessible PDFs.
6. Renames each PDF to its paper title.
7. Stores everything in date and topic partitioned folders.
8. Records every paper it could not obtain, with a manual action.
9. Extracts text with page boundaries preserved, and analyses each paper.
10. Builds a ten-sheet Excel literature-review matrix.
11. Writes an evidence-based introduction with citations.
12. Identifies research gaps across eleven categories.
13. Describes the research landscape of the reviewed evidence.
14. Documents every model and method found.
15. Runs three independent verifiers over metadata, claims, citations, and files.

**Outputs live in Google Drive.** The local numbered folders are staging: a file
is written locally, uploaded, verified against Drive's own metadata, and only
then counted as delivered.

**The workflow is resumable.** If it stops for a dropped connection, an API
limit, or a killed process, re-running continues from the saved state — and a
long stage resumes at the individual paper it stopped on.

---

## The three rules it holds to

**1. It never fabricates.** No invented citation, quartile, page number,
statistic, or finding. Where something could not be established, the output says
so. Every analysed field is labelled as one of:

```
Author explicitly states this
Agent inference based on evidence
Information not reported
Information could not be verified
```

**2. It only retrieves legally.** PDFs come from legitimate open-access
locations or from a direct PDF URL a publisher's own API has identified as
authorised. It never bypasses a paywall, an institutional login, a CAPTCHA, or
an anti-bot protection; never contacts Sci-Hub or comparable mirrors; and never
scrapes Google Scholar. These boundaries are enforced in code, not merely
documented — see `screen_url()` in `downloader.py` and the host lists in
`config/search_sources.yaml`.

**3. It never claims an upload it cannot prove.** A file is in Google Drive only
when the job's `drive_manifest.json` records `verified: true` with a real
`file_id`, after a size and checksum comparison. Otherwise the output says the
file is in local staging.

---

## Architecture

```
                        ┌─────────────────────────┐
                        │  Main agent (CLAUDE.md) │
                        └───────────┬─────────────┘
                                    │ delegates bounded work
     ┌──────────────┬───────────────┼───────────────┬──────────────┐
     ▼              ▼               ▼               ▼              ▼
 keyword-      paper-          download-       paper-        synthesis-
 strategy      discovery         agent        analysis        report
   agent         agent                          agent          agent
     │              │               │               │              │
     └──────────────┴───────────────┴───────┬───────┴──────────────┘
                                            │ verified independently by
                          ┌─────────────────┴──────────────────┐
                          ▼                                    ▼
                 metadata-q1-verifier          evidence-citation-verifier
                          │                                    │
                          └────────────────┬───────────────────┘
                                           ▼
                          Excel matrix + 5 Word reports + verification report
                                           │
                                           ▼
                              Google Drive (verified upload)
```

The analysis agent never verifies its own work. Verification is a separate
stage, run by different agents, working from the saved evidence.

### Responsibilities

| Agent | Does | Never does |
| --- | --- | --- |
| **Main agent** (`CLAUDE.md`) | Parses the request, creates the job, delegates, requests corrections, presents the summary | Declares completion while verification fails |
| `keyword-strategy-agent` | Concepts, synonyms, abbreviations, spellings, methods, applications, geography, exclusions, Boolean strings | Invents keywords unrelated to the question |
| `paper-discovery-agent` | Searches, merges, ranks, records provenance | Claims a paper is Q1 before verification |
| `download-agent` | Legal retrieval, validation, renaming, failure register | Bypasses any access restriction |
| `paper-analysis-agent` | Structured analysis with page evidence | Infers a detail because a method usually implies it |
| `metadata-q1-verifier` | DOI resolution, metadata agreement, quartile evidence, merge soundness | Assigns a quartile no source supplied |
| `evidence-citation-verifier` | Audits claims, pages, numbers, citations, references | Fixes the reports itself |
| `synthesis-report-agent` | The five documents and the matrix | Writes a claim with no evidence record |

### Python modules

| Module | Responsibility |
| --- | --- |
| `orchestrator.py` | The eleven resumable stages |
| `job_manager.py` | Job folders, configuration, checkpoints |
| `storage.py` | Staging-to-Drive routing and the per-job manifest |
| `drive_storage.py` | Drive v3 API: folders, resumable upload, verification |
| `keyword_generator.py` | Vocabulary and Boolean strings |
| `search.py` | Eight source adapters behind one interface |
| `http_client.py` | Retries, rate limits, blocked hosts, access-wall detection |
| `metadata.py` | Normalisation, additive merging, enrichment, relevance |
| `deduplicator.py` | The four-stage duplicate cascade |
| `q1_verifier.py` | Quartile verification and selection |
| `downloader.py` | Legal retrieval workflow |
| `pdf_validator.py` | Is this really the paper it claims to be? |
| `pdf_extractor.py` | Page-marked text, OCR detection |
| `analysis.py` | Field extraction with evidence and stance |
| `evidence_ledger.py` | Claim-to-source records |
| `synthesis.py` | Gaps, model profiles, landscape statistics |
| `citation_manager.py` | Five citation styles, BibTeX, RIS, audit |
| `excel_report.py` | The ten-sheet matrix |
| `word_reports.py` | Seven Word documents |
| `verification.py` | Three independent verifiers |
| `cli.py` | Eighteen commands |

---

## The legal-download limitation — read this before you rely on the tool

**The agent can only download what is legally free to download.** In practice
that means open-access papers, author-deposited preprints, and repository
copies. For a subscription-only paper it will record the attempt, name the
reason, and put the paper in `Unable_to_Download.docx` with a recommended
manual action.

This is a deliberate design choice, not a gap to be worked around. It has a
real consequence you must account for in your own writing: **your evidence base
may be biased towards open-access publishing.** Every generated report discloses
this in its Limitations section.

To include subscription papers, download them yourself through your
institutional access, place them in the job's `Downloaded Papers` folder using
the exact paper title as the filename, and re-run the `extract` and `report`
stages.

---

## Supported search sources

| Source | Credential | Notes |
| --- | --- | --- |
| Crossref | none | Authoritative DOI metadata |
| OpenAlex | none | Broad index with open-access locations |
| Semantic Scholar | optional key | Raises the rate limit considerably |
| Europe PMC | none | Health and life sciences |
| arXiv | none | Preprints, always labelled as such |
| Unpaywall | none (email) | Legal open-access location lookup |
| CORE | `CORE_API_KEY` | Repository aggregator; skipped without the key |
| Elsevier (Scopus) | `ELSEVIER_API_KEY` | Metadata; full text only when open access |
| Springer Nature | `SPRINGER_API_KEY` | Metadata; PDFs only from returned OA URLs |

**Not used:** Google Scholar (prohibits automated access), Sci-Hub, LibGen, and
comparable mirrors.

The pipeline runs fully with **no credentials at all** — six sources need none.
A source without its key is skipped with a stated reason, which appears in the
job's assumptions.

---

## API keys

All optional. Copy the template and fill in only what you have:

```bash
cp .env.example .env
```

| Variable | For |
| --- | --- |
| `LITERATURE_REVIEW_CONTACT_EMAIL` | Polite-pool access to Crossref, OpenAlex, Unpaywall (recommended) |
| `SEMANTIC_SCHOLAR_API_KEY` | Higher Semantic Scholar rate limit |
| `CORE_API_KEY` | CORE repository search |
| `ELSEVIER_API_KEY`, `ELSEVIER_INSTTOKEN` | Scopus metadata search |
| `SPRINGER_API_KEY` | Springer Nature metadata search |
| `ANTHROPIC_API_KEY` | Claude-assisted keyword expansion and analysis (deterministic fallback otherwise) |

`.env` is git-ignored. Never commit a key.

---

## Q1 ranking data — you must supply it

**This project ships no journal-ranking data**, because that data is licensed.
Without it, every paper is reported `Unverified` — which is the honest answer,
not a failure.

To enable real quartile verification:

1. Export your licensed Scimago or JCR data to CSV or Excel.
2. Save it outside version control, for example `config/scimago_2024.csv`
   (the `.gitignore` already excludes ranking files).
3. Point the config at it:

```yaml
# config/default_config.yaml
q1_ranking:
  file: config/scimago_2024.csv
  source_name: "Scimago Journal Rank 2024"
  column_map:
    journal_name: Title
    issn: Issn
    quartile: SJR Best Quartile
    subject_category: Categories
    ranking_year: Year
```

Common Scimago and JCR column names are recognised automatically, so the
mapping above usually needs no change.

**What the verifier will and will not do.** It matches by ISSN first, then exact
journal name, then a high-threshold fuzzy name match, and records which was
used. It records the ranking year actually applied and discloses when that
differs from the publication year. A journal that is Q1 in one subject category
and Q3 in another is reported as `Conflicting information` for you to resolve.
It will never mark a journal Q1 because the journal is famous.

---

## Folder structure

```
Literature Review/
├── CLAUDE.md                     Main agent instructions
├── README.md, QUICK_START.md
├── requirements.txt, pyproject.toml, .env.example, .gitignore
│
├── .claude/
│   ├── agents/                   7 subagent definitions
│   └── skills/literature-review/SKILL.md
│
├── config/
│   ├── default_config.yaml        Job defaults, network, thresholds
│   ├── search_sources.yaml        Sources, blocked hosts, OA hosts
│   ├── report_columns.yaml        All 10 Excel sheets
│   └── google_drive.yaml          Drive location, auth, verification policy
│
├── src/literature_review_agent/  21 modules (see the table above)
├── notebooks/literature_review_pipeline.ipynb
├── templates/                    Report templates + analysis JSON schema
├── tests/                        342 tests, no live network
│
├── 01 Keywords/YYYY-MM-DD/topic_slug/
│   ├── keywords.md, keywords.csv
│   ├── search_strings.md
│   └── inclusion_exclusion_criteria.md
├── 02 Literature Papers/YYYY-MM-DD/topic_slug/
│   ├── Downloaded Papers/Paper Title.pdf
│   ├── Extracted Text/Paper Title.txt
│   ├── Unable to Download/Unable_to_Download.docx
│   ├── paper_manifest.csv, paper_manifest.json
│   └── references.bib, references.ris
├── 03 Reports/YYYY-MM-DD/topic_slug/
│   ├── Literature_Review_Matrix.xlsx
│   ├── Introduction.docx, Research_Gaps.docx
│   ├── Global_Research_Landscape.docx
│   ├── Models_and_Applications.docx, Paper_Summaries.docx
├── 04 Verification/YYYY-MM-DD/topic_slug/
│   ├── Evidence_Ledger.xlsx, Verification_Report.docx
│   ├── Citation_Audit.xlsx, unresolved_issues.csv
└── 05 Logs and State/YYYY-MM-DD/topic_slug/
    ├── pipeline.log, search_log.jsonl, download_log.jsonl
    ├── checkpoints.json, job_config.yaml, drive_manifest.json
    └── agent_workspace/<agent-name>/     Private per-subagent scratch
```

The numbered folders hold **staging copies only** and are never committed.
Each subagent gets its own `agent_workspace/` directory, so two agents can never
overwrite one another's intermediate output.

---

## Installation

Python 3.11 or later.

```bash
cd "Literature Review"

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\Activate.ps1

pip install -r requirements.txt
pip install -e .

python -m literature_review_agent init
```

`init` reports which sources are available, whether Drive is configured, and
whether ranking data is present. Then:

```bash
cp .env.example .env               # optional; fill in what you have
```

`QUICK_START.md` has step-by-step instructions for Windows PowerShell and for
macOS/Linux, written for a non-programmer.

---

## Google Drive setup

Outputs are stored in Drive. Two authentication methods are supported.

### OAuth — your own Google account (default)

1. Open the [Google Cloud Console](https://console.cloud.google.com/), create or
   select a project.
2. **APIs & Services → Library → Google Drive API → Enable.**
3. **APIs & Services → Credentials → Create credentials → OAuth client ID.**
   Application type: **Desktop app**.
4. Download the JSON and save it as `secrets/credentials.json`.
5. Authorise once:

```bash
python -m literature_review_agent drive-login
```

A browser window asks for consent. The token is cached at `secrets/token.json`.
Both files are git-ignored.

### Service account — headless runs and Shared Drives

1. Create a service account and a JSON key for it.
2. Save the key as `secrets/service_account.json`.
3. Set `drive.auth_method: service_account` in `config/google_drive.yaml`.
4. Share the destination folder or Shared Drive with the service account's email
   address, granting **Content manager** (Shared Drive) or **Editor** (My Drive).

### Shared Drive

```yaml
# config/google_drive.yaml
drive:
  location: shared_drive
```

```bash
# .env — the ID is the last segment of drive.google.com/drive/folders/<ID>
GOOGLE_SHARED_DRIVE_ID=0ABCdEfGhIJKLMNOPQRs
```

### Checking and retrying

```bash
python -m literature_review_agent drive-status                 # what is configured
python -m literature_review_agent drive-tree --job JOB_PATH    # create folders only
python -m literature_review_agent drive-sync --job JOB_PATH    # retry pending uploads
```

To run entirely locally, set `GOOGLE_DRIVE_ENABLED=false` or pass `--no-drive`.

**Never commit `secrets/`, and never paste credential contents into a chat.**

---

## CLI usage

```bash
# Check the installation
python -m literature_review_agent init

# Run a complete review
python -m literature_review_agent run \
  --topic "Effect of rainfall on urban travel behaviour" \
  --research-question "How does rainfall intensity influence mode choice?" \
  --year-from 2015 --year-to 2026 --max-papers 50 --q1-mode preferred

# One stage at a time (each takes --job)
python -m literature_review_agent keywords --job JOB_PATH
python -m literature_review_agent search   --job JOB_PATH
python -m literature_review_agent download --job JOB_PATH
python -m literature_review_agent extract  --job JOB_PATH
python -m literature_review_agent analyse  --job JOB_PATH
python -m literature_review_agent report   --job JOB_PATH
python -m literature_review_agent verify   --job JOB_PATH

# Manage jobs
python -m literature_review_agent resume --job JOB_PATH
python -m literature_review_agent status --job JOB_PATH
python -m literature_review_agent jobs
python -m literature_review_agent doctor
```

`JOB_PATH` is the folder printed when the job is created, of the form
`05 Logs and State/YYYY-MM-DD/topic_slug`.

### Useful options for `run`

| Option | Effect |
| --- | --- |
| `--geography India` | Adds geographic search terms and a screening criterion |
| `--keyword "mode choice"` | A term you want searched (repeatable) |
| `--exclude laboratory` | A term marking a record irrelevant (repeatable) |
| `--citation-style IEEE` | APA 7, Harvard, IEEE, Vancouver, Chicago 17 |
| `--paper-type "review"` | Document types to include (repeatable) |
| `--ranking-file FILE` | Licensed journal-ranking export |
| `--no-drive` | Run locally without uploading |
| `--force` | Re-run a stage that already completed |

### Defaults

Anything you do not specify uses these, and the assumption is recorded in
`job_config.yaml` and repeated in the completion summary:

```yaml
year_from: 2015
year_to: <current year>
maximum_papers: 50
q1_mode: preferred
language: English
paper_types: [journal article]
citation_style: APA 7
geography: global
download_only_legal_and_authorized_content: true
```

---

## Notebook usage

```bash
source .venv/bin/activate
jupyter lab notebooks/literature_review_pipeline.ipynb
```

Edit **section 2** — the only cell you need to change — then run the cells in
order. The notebook locates the project root by itself and contains no
hard-coded paths, so it works wherever you cloned the project.

It calls the same package functions as the CLI and holds no research logic of
its own, so you can start in the notebook and finish on the command line, or the
reverse.

To rebuild the notebook after editing `notebooks/build_notebook.py`:

```bash
python notebooks/build_notebook.py
```

---

## How to resume

Interruptions are expected. Nothing is lost and nothing is repeated.

```bash
python -m literature_review_agent status --job JOB_PATH   # see where it stopped
python -m literature_review_agent resume --job JOB_PATH   # continue
```

`resume` skips completed stages and continues long stages at the item they
stopped on: an interrupted download of 50 papers picks up at paper 31 rather
than starting again.

Everything needed lives in the job folder:

| File | Holds |
| --- | --- |
| `checkpoints.json` | Stage status, attempts, and per-item progress |
| `job_config.yaml` | The complete original topic and every assumption |
| `paper_records.json` | The canonical working set |
| `paper_analyses.json` | Per-paper analysis records |
| `evidence_records.json` | Claim-to-source ledger |
| `drive_manifest.json` | Upload state per artefact, with retry counts |

To redo a stage deliberately, add `--force`.

---

## How to inspect failures

**Downloads that did not happen**

```
02 Literature Papers/<date>/<slug>/Unable to Download/Unable_to_Download.docx
05 Logs and State/<date>/<slug>/download_log.jsonl
```

The Word register gives, per paper: serial number, title, authors, year,
journal, DOI, landing page, every attempted PDF link, open-access status,
failure reason, HTTP status, timestamp, recommended manual action, and Q1
status.

**Searches that returned nothing**

```
05 Logs and State/<date>/<slug>/search_log.jsonl
```

One line per query with the source, result count, HTTP status, and outcome. The
Excel matrix has the same content in its `Search Log` sheet.

**Verification problems**

```
04 Verification/<date>/<slug>/unresolved_issues.csv
04 Verification/<date>/<slug>/Verification_Report.docx
```

The CSV has one row per failure or warning, with the original value, any
corrected value, the source of the correction, and the reason. Nothing is
silently rewritten.

**Pending uploads**

```
05 Logs and State/<date>/<slug>/drive_manifest.json
```

Each artefact records its Drive file ID, web-view link, verification result,
error, and attempt count.

**Stage failures**

```
05 Logs and State/<date>/<slug>/pipeline.log
05 Logs and State/<date>/<slug>/pipeline.jsonl
```

---

## How to rerun verification

Verification is independent of analysis, so you can re-run it alone — after
adding a ranking file, after manually adding PDFs, or after editing a report:

```bash
python -m literature_review_agent verify --job JOB_PATH --force
```

This re-runs all three verifiers and rewrites `Verification_Report.docx`,
`unresolved_issues.csv`, `Evidence_Ledger.xlsx`, and `Citation_Audit.xlsx`.

To re-verify quartiles after supplying ranking data:

```bash
python -m literature_review_agent search --job JOB_PATH --force \
  --ranking-file config/scimago_2024.csv
python -m literature_review_agent report --job JOB_PATH --force
python -m literature_review_agent verify --job JOB_PATH --force
```

---

## Testing

```bash
python -m pytest tests/ -q
```

The suite needs no internet: every HTTP call is mocked with `respx`, and the
Drive API is replaced by an in-memory fake. Credentials are stripped from the
environment during tests, so the results are identical on your machine and on a
clean runner.

PDF fixtures are generated on first run — `.gitignore` excludes `*.pdf`, so no
binary is ever committed.

```bash
python -m pytest tests/ -q -k "drive"        # Drive storage only
python -m pytest tests/ -q -m "not live"     # skip optional live tests
```

---

## Known limitations

**Coverage**
- Only literature indexed by the configured sources is found. This is a search,
  not a census.
- Without API keys, CORE, Elsevier, and Springer are not searched.
- Google Scholar is not used, so some grey literature is out of reach.

**Access**
- Only legally free PDFs can be analysed. Subscription papers are recorded, not
  retrieved, which may bias the evidence base towards open-access publishing.
- Scanned PDFs with no text layer are flagged for OCR and support no
  evidence-based claim. OCR is an optional extra
  (`pip install 'literature-review-agent[ocr]'`) and never invents text.

**Quartiles**
- No ranking data ships with the project. Without a licensed file, every paper
  is `Unverified`.
- Quartiles are year-specific and category-specific; cross-category
  disagreement is reported for you to resolve, not decided.

**Analysis**
- Field extraction is automated. It reads cue words and sentence structure, not
  meaning, so it can miss information a human reader would find. Confidence
  grades and the `Agent inference` label tell you where to look harder.
- Author affiliations are not retrieved, so the landscape report cannot name
  contributing institutions and says so.
- Contradiction detection compares the direction of reported effects; it will
  not catch every substantive disagreement.

**Scale**
- Rate limits make large jobs slow. arXiv asks for three seconds between calls.
  A 50-paper review typically takes 10-30 minutes.
- Very large PDFs are capped at 100 MB by default.

**What it is not**
- It does not judge whether the research it finds is any good.
- It does not replace reading the papers. Every generated document says so, and
  you should treat its output as a well-organised starting point for your own
  reading, not as a finished review.

---

## Licence

MIT for the code. The papers it retrieves remain under their own licences, and
the reports record the licence of every downloaded PDF.
