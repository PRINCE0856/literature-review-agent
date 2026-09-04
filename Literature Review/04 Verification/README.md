# 04 Verification

Evidence ledger, verification report, and citation audit.

## This folder is local staging only

Contents are created here, uploaded to **Google Drive**, verified against a real
Drive file ID, and only then marked complete. Nothing inside is committed to
GitHub — see `.gitignore`. The matching Drive path is:

```text
Literature Review/04 Verification/YYYY-MM-DD/topic_slug/
```

Each job writes into its own date and topic folder, so runs never overwrite one
another. Drive file IDs and web-view links for every artefact are recorded in
`05 Logs and State/YYYY-MM-DD/topic_slug/drive_manifest.json`.

If an upload fails, the local copy is retained and the artefact stays queued for
a resumable retry via:

```bash
python -m literature_review_agent drive-sync --job "05 Logs and State/YYYY-MM-DD/topic_slug"
```
