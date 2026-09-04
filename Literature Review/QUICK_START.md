# Quick Start

A step-by-step setup guide. No programming knowledge assumed — you will copy
and paste commands into a terminal.

**Time needed:** about 15 minutes for setup, then 10-30 minutes per review.

---

## What you will end up with

Type a research topic, and you get:

- a list of search keywords and ready-made database search strings
- the PDFs of papers that are legally free to download, each named after its
  paper title
- a list of the papers that could not be downloaded, and how to get them
- an Excel workbook with one row per paper
- five Word documents: an introduction, research gaps, the research landscape,
  the models used, and a summary of each paper
- a verification report saying which parts were checked and which were not

Everything is saved to your Google Drive.

---

## Step 1 — Install Python 3.11 or later

### Windows

1. Go to [python.org/downloads](https://www.python.org/downloads/).
2. Download the latest Python 3 installer.
3. Run it. **Tick "Add python.exe to PATH"** on the first screen — this matters.
4. Click *Install Now*.

Check it worked. Press `Win + X`, choose **Terminal** or **Windows PowerShell**,
and type:

```powershell
python --version
```

You should see `Python 3.11.x` or higher. If you see an error, restart your
computer and try again.

### macOS

Open **Terminal** (press `Cmd + Space`, type `Terminal`, press Enter):

```bash
python3 --version
```

If it says 3.11 or higher, you are done. Otherwise install
[Homebrew](https://brew.sh/) and then:

```bash
brew install python@3.12
```

### Linux

```bash
python3 --version
sudo apt install python3.11 python3.11-venv python3-pip    # Debian/Ubuntu
```

---

## Step 2 — Open a terminal in the project folder

You need your terminal "pointing at" the `Literature Review` folder.

### Windows PowerShell

Open the `Literature Review` folder in File Explorer, then type `powershell` in
the address bar and press Enter. Or:

```powershell
cd "C:\path\to\Literature Review"
```

### macOS

Right-click the `Literature Review` folder → **Services** → **New Terminal at
Folder**. Or:

```bash
cd "/path/to/Literature Review"
```

### Linux

```bash
cd "/path/to/Literature Review"
```

**Check you are in the right place.** This should list `CLAUDE.md`,
`config`, `src`:

```powershell
ls        # PowerShell, macOS, and Linux all accept this
```

---

## Step 3 — Create a virtual environment

A virtual environment keeps this project's packages separate from the rest of
your computer. You create it once.

### Windows PowerShell

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

If you see *"running scripts is disabled on this system"*, run this once and
then try again:

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

### macOS / Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
```

**You will know it worked** when your prompt starts with `(.venv)`.

> **Every time you come back**, repeat just the activate line —
> `.venv\Scripts\Activate.ps1` on Windows, `source .venv/bin/activate` on
> macOS and Linux. You do not need to create the environment again.

---

## Step 4 — Install the packages

```powershell
pip install -r requirements.txt
pip install -e .
```

This takes two to five minutes and prints a lot of text. That is normal.

---

## Step 5 — Check the installation

```powershell
python -m literature_review_agent init
```

You should see which search sources are available, that the output folders were
created, and a note about Google Drive and journal-ranking data.

**It is fine and expected to see "not set" next to the API keys, and Drive
reported as not yet configured.** Six search sources need no credentials at all.

If something is wrong:

```powershell
python -m literature_review_agent doctor
```

---

## Step 6 — Set up Google Drive

Your review outputs are saved to Google Drive. This takes about five minutes,
once.

### 6a. Create a Google Cloud project and turn on the Drive API

1. Go to [console.cloud.google.com](https://console.cloud.google.com/) and sign
   in with the Google account whose Drive you want to use.
2. At the top, click the project dropdown → **New Project**. Name it anything,
   for example `literature-review`. Click **Create**.
3. Make sure your new project is selected in the dropdown.
4. In the search bar at the top, type `Google Drive API` and open it.
5. Click **Enable**.

### 6b. Create a credential

1. In the left menu, go to **APIs & Services → Credentials**.
2. Click **+ Create Credentials → OAuth client ID**.
3. If it asks you to configure a consent screen first:
   - Choose **External**, click **Create**.
   - App name: anything, for example `Literature Review Agent`.
   - Enter your own email where asked. Save and continue through the screens.
   - Under **Test users**, click **+ Add users** and add your own email address.
   - Then go back to **Credentials** and start again at step 2.
4. Application type: **Desktop app**. Name: anything. Click **Create**.
5. Click **Download JSON** in the dialog.

### 6c. Put the file where the agent expects it

Create a folder called `secrets` inside `Literature Review`, and save the
downloaded file there as exactly `credentials.json`:

```
Literature Review/
└── secrets/
    └── credentials.json
```

> **Keep this file private.** Do not email it, do not paste its contents into a
> chat, and do not upload it anywhere. The project is already configured to
> never include it in version control.

### 6d. Authorise once

```powershell
python -m literature_review_agent drive-login
```

A browser window opens asking you to allow access. Approve it. If you see a
warning that the app is not verified, click **Advanced → Go to ... (unsafe)** —
this appears because the app is your own, not published.

You should then see `Authorised as your.email@example.com`.

### 6e. Confirm

```powershell
python -m literature_review_agent drive-status
```

Look for **Ready: yes**.

### Prefer not to use Drive?

Skip step 6 entirely and add `--no-drive` to your commands. Everything stays in
the numbered folders on your computer.

---

## Step 7 — Run your first review

```powershell
python -m literature_review_agent run --topic "Effect of rainfall on urban travel behaviour"
```

Use your own topic in the quotation marks. Adding a research question improves
the result:

```powershell
python -m literature_review_agent run `
  --topic "Effect of rainfall on urban travel behaviour" `
  --research-question "How does rainfall intensity influence mode choice?" `
  --max-papers 25
```

> **Line continuation differs by system.** PowerShell uses a backtick `` ` `` at
> the end of each line, as above. macOS and Linux use a backslash `\`:
>
> ```bash
> python -m literature_review_agent run \
>   --topic "Effect of rainfall on urban travel behaviour" \
>   --research-question "How does rainfall intensity influence mode choice?" \
>   --max-papers 25
> ```
>
> Or simply write it all on one line, which works everywhere.

**Expect 10 to 30 minutes.** The agent pauses between requests because the
scholarly databases ask users to be polite. Progress is printed as it goes.

**Start with `--max-papers 25`** for your first run, so you see the result
sooner.

---

## Step 8 — Find your results

The agent prints the job folder when it starts. Your files are in the numbered
folders, organised by date and topic:

```
01 Keywords/2026-09-04/effect-rainfall-urban-travel-behaviour/
02 Literature Papers/2026-09-04/effect-rainfall-urban-travel-behaviour/
03 Reports/2026-09-04/effect-rainfall-urban-travel-behaviour/
04 Verification/2026-09-04/effect-rainfall-urban-travel-behaviour/
05 Logs and State/2026-09-04/effect-rainfall-urban-travel-behaviour/
```

And in Google Drive, under a folder called **Literature Review** with the same
structure.

**Read these first:**

| File | Where | Why |
| --- | --- | --- |
| `Literature_Review_Matrix.xlsx` | `03 Reports/...` | One row per paper; the fastest overview |
| `Introduction.docx` | `03 Reports/...` | A written synthesis with citations |
| `Research_Gaps.docx` | `03 Reports/...` | What the papers say is still unknown |
| `Verification_Report.docx` | `04 Verification/...` | What was checked, and what was not |
| `Unable_to_Download.docx` | `02 Literature Papers/.../Unable to Download/` | Papers you need to fetch yourself |

---

## Step 9 — If it stops partway

This is normal and safe. Your internet may drop, or a database may ask you to
slow down.

**Just run resume.** Nothing is lost and nothing is repeated:

```powershell
python -m literature_review_agent resume --job "05 Logs and State/2026-09-04/effect-rainfall-urban-travel-behaviour"
```

To see where it stopped:

```powershell
python -m literature_review_agent status --job "05 Logs and State/2026-09-04/effect-rainfall-urban-travel-behaviour"
```

Forgotten the folder name?

```powershell
python -m literature_review_agent jobs
```

---

## Using the notebook instead

If you prefer a step-by-step interface where you can see each stage:

```powershell
pip install jupyterlab
jupyter lab notebooks/literature_review_pipeline.ipynb
```

Edit **section 2** with your topic, then run each cell in order using
`Shift + Enter`.

---

## Two things to understand about the results

### 1. It can only download papers that are legally free

The agent downloads open-access papers and author-deposited copies. It will not
get past a paywall or a login — deliberately.

Subscription papers appear in `Unable_to_Download.docx` with a link and a
suggested action. To include one, download it yourself through your library,
save it into the job's `Downloaded Papers` folder using the **exact paper title**
as the filename, and re-run:

```powershell
python -m literature_review_agent extract --job JOB_PATH
python -m literature_review_agent report  --job JOB_PATH --force
```

This matters for your writing: your evidence base may lean towards open-access
publishing. Every report says so in its Limitations section.

### 2. Q1 journal status needs data you supply

The agent cannot tell you a journal is Q1 without ranking data, and it will not
guess. Every paper will read **Unverified** until you supply a file.

If your institution subscribes to Scimago or Journal Citation Reports:

1. Export the journal list to CSV or Excel.
2. Save it in the `config` folder, for example `config/scimago_2024.csv`.
3. Use it:

```powershell
python -m literature_review_agent run --topic "your topic" --ranking-file config/scimago_2024.csv
```

Without it, everything else still works — the quartile column simply reads
`Unverified`, which is the honest answer.

---

## Common problems

| What you see | What to do |
| --- | --- |
| `python: command not found` | Python is not on your PATH. Reinstall, ticking "Add python.exe to PATH", and restart. |
| `running scripts is disabled` | Run `Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned`, then activate again. |
| `No module named literature_review_agent` | The environment is not active, or `pip install -e .` was missed. Activate, then re-run it. |
| `No records were returned` | Try a broader topic, widen `--year-from`, or check your internet connection. |
| Everything failed to download | The topic is likely dominated by subscription journals. Check `Unable_to_Download.docx`. |
| `Drive is enabled but not yet usable` | Finish step 6, then run `drive-status` to confirm. |
| Browser says "app is not verified" | Expected for your own app. Click **Advanced → Go to ... (unsafe)**. |
| Quartiles all say `Unverified` | Correct, until you supply a ranking file. See section 2 above. |
| Some PDFs "need OCR" | They are scans with no text. They are excluded from claims rather than guessed at. |
| It is slow | Rate limits are deliberate. Use `--max-papers 25` for a faster first run. |

Still stuck? This prints a full diagnosis:

```powershell
python -m literature_review_agent doctor
```

---

## Command reference

```powershell
# Setup and diagnosis
python -m literature_review_agent init
python -m literature_review_agent doctor

# Run a review
python -m literature_review_agent run --topic "your topic"

# Continue or inspect
python -m literature_review_agent resume --job JOB_PATH
python -m literature_review_agent status --job JOB_PATH
python -m literature_review_agent jobs

# Google Drive
python -m literature_review_agent drive-status
python -m literature_review_agent drive-login
python -m literature_review_agent drive-sync --job JOB_PATH

# One stage at a time
python -m literature_review_agent keywords --job JOB_PATH
python -m literature_review_agent search   --job JOB_PATH
python -m literature_review_agent download --job JOB_PATH
python -m literature_review_agent extract  --job JOB_PATH
python -m literature_review_agent analyse  --job JOB_PATH
python -m literature_review_agent report   --job JOB_PATH
python -m literature_review_agent verify   --job JOB_PATH

# See every option
python -m literature_review_agent --help
python -m literature_review_agent run --help
```

---

## One last thing

This tool organises the literature and shows you its evidence. It does not judge
whether the research it finds is any good, and it is not a substitute for
reading the papers.

Treat its output as a well-organised starting point: check the sources it cites,
read the ones that matter, and write the review yourself. Every document it
produces says the same thing in its verification note.

For full detail on the architecture, configuration, and limitations, see
`README.md`.
