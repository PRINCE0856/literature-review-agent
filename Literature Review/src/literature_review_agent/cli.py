"""Command-line interface built with Typer.

Every stage is independently runnable against a saved job, which is what makes
the workflow resumable:

    python -m literature_review_agent init
    python -m literature_review_agent run --topic "..." --max-papers 50
    python -m literature_review_agent search   --job JOB_PATH
    python -m literature_review_agent download --job JOB_PATH
    python -m literature_review_agent resume   --job JOB_PATH
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .config import available_keys, find_project_root, load_settings, missing_keys
from .drive_storage import DriveClient, DriveNotConfiguredError, describe_drive_readiness
from .job_manager import Job, JobError, list_jobs
from .orchestrator import Orchestrator
from .schemas import STAGE_ORDER, CitationStyle, Q1Mode, StageName, StageStatus
from .storage import StorageManager

app = typer.Typer(
    name="literature-review-agent",
    help=(
        "A resumable, verification-first literature-review agent. Outputs are stored "
        "in Google Drive; local folders are staging only."
    ),
    add_completion=False,
    no_args_is_help=True,
)
console = Console()


# ---------------------------------------------------------------------------
# Shared option types
# ---------------------------------------------------------------------------

JobOption = typer.Option(
    ...,
    "--job",
    "-j",
    help="Path to the job folder (printed when the job was created).",
)
RootOption = typer.Option(
    None, "--root", help="Project root. Detected automatically when omitted."
)
DriveOption = typer.Option(
    None,
    "--drive/--no-drive",
    help="Override Google Drive syncing for this run.",
)


def _load_job(job_path: Path, root: Path | None) -> Job:
    """Load a job, exiting with a clear message when it cannot be found."""
    try:
        settings = load_settings(root)
        return Job.load(job_path, settings=settings)
    except JobError as exc:
        console.print(f"[red]{exc}[/red]")
        console.print(
            "\nList the jobs you have with: "
            "[cyan]python -m literature_review_agent jobs[/cyan]"
        )
        raise typer.Exit(code=2) from exc


def _run_stages(
    job: Job,
    stages: list[StageName],
    *,
    drive: bool | None,
    force: bool = False,
) -> None:
    """Run a subset of stages and print the outcome of each."""
    orchestrator = Orchestrator(job, enable_drive=drive)
    orchestrator.load_state()
    orchestrator.storage.ensure_remote_tree()

    for stage in stages:
        console.print(f"\n[bold cyan]-> {stage.value}[/bold cyan]")
        outcome = orchestrator.run_stage(stage, force=force)
        colour = {
            StageStatus.COMPLETE: "green",
            StageStatus.SKIPPED: "yellow",
            StageStatus.FAILED: "red",
        }.get(outcome.status, "white")
        console.print(f"   [{colour}]{outcome.status.value}[/{colour}] {outcome.message}")
        if outcome.status == StageStatus.FAILED:
            console.print(
                "\n[red]Stage failed.[/red] Progress is checkpointed; fix the cause and "
                f"re-run:\n  [cyan]python -m literature_review_agent resume "
                f'--job "{job.paths.logs}"[/cyan]'
            )
            raise typer.Exit(code=1)

    _print_summary(orchestrator)


def _print_summary(orchestrator: Orchestrator) -> None:
    """Print the completion summary panel."""
    summary = orchestrator.completion_summary()
    counts = summary["counts"]

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()
    for label, key in (
        ("Papers discovered", "discovered"),
        ("Unique after dedup", "unique"),
        ("Included in review", "included"),
        ("Verified Q1", "verified_q1"),
        ("Unverified quartile", "unverified_quartile"),
        ("PDFs downloaded", "downloaded"),
        ("Failed downloads", "failed_downloads"),
        ("Papers analysed", "analysed"),
        ("Evidence records", "evidence_records"),
        ("Unresolved problems", "unresolved_problems"),
    ):
        table.add_row(label, str(counts.get(key, 0)))

    console.print(
        Panel(
            table,
            title=f"[bold]{summary['topic']}[/bold]",
            subtitle=f"job: {summary['job_date']}",
            border_style="cyan",
        )
    )

    drive = summary["drive"]
    if drive["drive_enabled"]:
        console.print(
            f"\n[bold]Google Drive:[/bold] "
            f"{drive['artefacts_uploaded_and_verified']} of {drive['artefacts_tracked']} "
            "artefacts uploaded and verified."
        )
        if drive["artefacts_pending_upload"]:
            console.print(
                f"[yellow]{drive['artefacts_pending_upload']} artefact(s) are not yet "
                "verified in Drive. Retry with:[/yellow]\n"
                f'  [cyan]python -m literature_review_agent drive-sync --job "'
                f'{orchestrator.job.paths.logs}"[/cyan]'
            )
    else:
        console.print(f"\n[yellow]Google Drive: {drive['drive_status']}[/yellow]")
        console.print(
            "[yellow]All outputs are in local staging only. Nothing has been "
            "uploaded.[/yellow]"
        )

    if summary["assumptions"]:
        console.print("\n[bold]Assumptions used:[/bold]")
        for assumption in summary["assumptions"]:
            console.print(f"  - {assumption}")

    if summary["limitations"]:
        console.print("\n[bold yellow]Limitations and outstanding items:[/bold yellow]")
        for limitation in summary["limitations"]:
            console.print(f"  - {limitation}")

    if summary["complete"]:
        console.print("\n[green]All stages complete.[/green]")
    else:
        next_stage = orchestrator.job.next_stage()
        console.print(
            f"\n[yellow]Next stage: {next_stage.value if next_stage else 'none'}[/yellow]"
        )


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


@app.command()
def init(root: Optional[Path] = RootOption) -> None:
    """Check the installation, create the output folders, and report readiness."""
    project_root = Path(root).resolve() if root else find_project_root()
    console.print(f"[bold]Project root:[/bold] {project_root}\n")

    try:
        settings = load_settings(project_root)
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Configuration could not be loaded: {exc}[/red]")
        raise typer.Exit(code=1) from exc

    created = settings.ensure_top_level_dirs()
    console.print("[green]Output folders ready:[/green]")
    for name, path in created.items():
        console.print(f"  {path.name}/")

    console.print("\n[bold]Search sources[/bold]")
    table = Table("Source", "Status", "Detail")
    for name, spec in settings.source_specs().items():
        if spec.available:
            table.add_row(spec.label, "[green]available[/green]", spec.notes[:60])
        else:
            table.add_row(spec.label, "[yellow]skipped[/yellow]", spec.unavailable_reason)
    console.print(table)

    console.print("\n[bold]Optional credentials[/bold]")
    key_table = Table("Variable", "Present")
    for name, present in available_keys().items():
        key_table.add_row(name, "[green]yes[/green]" if present else "[dim]no[/dim]")
    console.print(key_table)

    console.print("\n[bold]Google Drive[/bold]")
    readiness = describe_drive_readiness(settings)
    if readiness.ready:
        console.print(f"  [green]{readiness.summary()}[/green]")
    else:
        console.print(f"  [yellow]{readiness.summary()}[/yellow]")
        if readiness.setup_steps:
            console.print("\n  [bold]To finish Drive setup:[/bold]")
            for index, step in enumerate(readiness.setup_steps, 1):
                console.print(f"    {index}. {step}")

    ranking = settings.q1_ranking.get("file")
    console.print("\n[bold]Journal-ranking data[/bold]")
    if ranking and settings.resolve_path(str(ranking)) and settings.resolve_path(str(ranking)).exists():
        console.print(f"  [green]configured: {ranking}[/green]")
    else:
        console.print(
            "  [yellow]Not configured. Quartiles will be reported as 'Unverified'.[/yellow]\n"
            "  Supply a licensed Scimago or JCR export and set q1_ranking.file in\n"
            "  config/default_config.yaml. No ranking data ships with this project."
        )

    if not (project_root / ".env").exists():
        console.print(
            "\n[yellow]No .env file found.[/yellow] Copy the template with:\n"
            "  [cyan]cp .env.example .env[/cyan]"
        )

    console.print("\n[green]Initialisation complete.[/green] Start a review with:")
    console.print(
        '  [cyan]python -m literature_review_agent run --topic "your topic here"[/cyan]'
    )


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


@app.command()
def run(
    topic: str = typer.Option(..., "--topic", "-t", help="The research topic."),
    research_question: list[str] = typer.Option(
        [], "--research-question", "-q", help="A research question. Repeatable."
    ),
    year_from: Optional[int] = typer.Option(None, "--year-from", help="Earliest year."),
    year_to: Optional[int] = typer.Option(None, "--year-to", help="Latest year."),
    max_papers: Optional[int] = typer.Option(
        None, "--max-papers", "-n", help="Maximum papers to include."
    ),
    q1_mode: Optional[Q1Mode] = typer.Option(
        None, "--q1-mode", help="only | preferred | ignore."
    ),
    geography: Optional[str] = typer.Option(
        None, "--geography", "-g", help="Study geography, for example 'India'."
    ),
    language: Optional[str] = typer.Option(None, "--language", help="Publication language."),
    paper_type: list[str] = typer.Option(
        [], "--paper-type", help="Document type to include. Repeatable."
    ),
    keyword: list[str] = typer.Option(
        [], "--keyword", "-k", help="A keyword you want searched. Repeatable."
    ),
    exclude: list[str] = typer.Option(
        [], "--exclude", "-x", help="An exclusion term. Repeatable."
    ),
    citation_style: Optional[CitationStyle] = typer.Option(
        None, "--citation-style", help="Reference style."
    ),
    output_root: Optional[str] = typer.Option(
        None, "--output-root", help="Where the numbered folders live."
    ),
    ranking_file: Optional[str] = typer.Option(
        None, "--ranking-file", help="Licensed journal-ranking CSV or Excel file."
    ),
    drive: Optional[bool] = DriveOption,
    root: Optional[Path] = RootOption,
) -> None:
    """Run the complete pipeline for a new topic."""
    settings = load_settings(root)
    overrides = {
        "research_questions": list(research_question),
        "year_from": year_from,
        "year_to": year_to,
        "maximum_papers": max_papers,
        "q1_mode": q1_mode.value if q1_mode else None,
        "geography": geography,
        "language": language,
        "paper_types": list(paper_type),
        "user_keywords": list(keyword),
        "exclusion_terms": list(exclude),
        "citation_style": citation_style.value if citation_style else None,
        "output_root": output_root,
        "ranking_file": ranking_file,
    }
    overrides = {k: v for k, v in overrides.items() if v not in (None, [], "")}

    job = Job.create(topic, settings=settings, **overrides)
    console.print(
        Panel(
            f"[bold]{job.config.topic}[/bold]\n\n"
            f"Job folder: {job.paths.logs}\n"
            f"Years: {job.config.year_from}-{job.config.year_to}    "
            f"Max papers: {job.config.maximum_papers}    "
            f"Q1 mode: {job.config.q1_mode.value}",
            title="Literature review job created",
            border_style="green",
        )
    )
    _run_stages(job, list(STAGE_ORDER), drive=drive)


# ---------------------------------------------------------------------------
# Individual stages
# ---------------------------------------------------------------------------


def _stage_command(name: str, stages: list[StageName], help_text: str) -> None:
    """Register a command that runs a fixed set of stages against a job."""

    @app.command(name=name, help=help_text)
    def _command(  # noqa: D401 - help text is supplied above
        job: Path = JobOption,
        force: bool = typer.Option(
            False, "--force", help="Re-run even if the stage is already complete."
        ),
        drive: Optional[bool] = DriveOption,
        root: Optional[Path] = RootOption,
    ) -> None:
        _run_stages(_load_job(job, root), stages, drive=drive, force=force)


_stage_command(
    "keywords",
    [StageName.KEYWORDS],
    "Generate the keyword strategy and database-ready search strings.",
)
_stage_command(
    "search",
    [StageName.SEARCH, StageName.DEDUPLICATE, StageName.Q1_VERIFY, StageName.SELECT],
    "Search every available source, deduplicate, verify quartiles, and select papers.",
)
_stage_command(
    "download", [StageName.DOWNLOAD], "Download the legally accessible PDFs."
)
_stage_command(
    "extract", [StageName.EXTRACT], "Extract page-marked text from the downloaded PDFs."
)
_stage_command(
    "analyse",
    [StageName.ANALYSE, StageName.VERIFY_EVIDENCE],
    "Analyse the papers and build the evidence ledger.",
)
_stage_command(
    "report",
    [StageName.ANALYSE, StageName.VERIFY_EVIDENCE, StageName.REPORT],
    "Generate the Excel matrix and the five Word reports.",
)
_stage_command(
    "verify",
    [StageName.FINAL_VERIFY],
    "Run the independent verifiers and write the verification report.",
)


@app.command()
def resume(
    job: Path = JobOption,
    drive: Optional[bool] = DriveOption,
    root: Optional[Path] = RootOption,
) -> None:
    """Continue an interrupted job from its last completed stage."""
    loaded = _load_job(job, root)
    next_stage = loaded.next_stage()
    if next_stage is None:
        console.print("[green]This job is already complete.[/green]")
        orchestrator = Orchestrator(loaded, enable_drive=drive)
        orchestrator.load_state()
        _print_summary(orchestrator)
        return

    console.print(f"[bold]Resuming from stage:[/bold] {next_stage.value}")
    remaining = list(STAGE_ORDER[STAGE_ORDER.index(next_stage) :])
    _run_stages(loaded, remaining, drive=drive)


# ---------------------------------------------------------------------------
# status / jobs
# ---------------------------------------------------------------------------


@app.command()
def status(job: Path = JobOption, root: Optional[Path] = RootOption) -> None:
    """Show the stage-by-stage state of one job."""
    loaded = _load_job(job, root)
    description = loaded.describe()

    console.print(
        Panel(
            f"[bold]{description['topic']}[/bold]\n\n"
            f"Folder: {description['job_folder']}\n"
            f"Date: {description['job_date']}    Job ID: {description['job_id']}",
            border_style="cyan",
        )
    )
    table = Table("Stage", "Status", "Attempts", "Items done", "Message")
    for stage in STAGE_ORDER:
        info = description["stages"].get(stage.value, {})
        state = info.get("status", "pending")
        colour = {
            "complete": "green",
            "skipped": "yellow",
            "failed": "red",
            "running": "cyan",
        }.get(state, "dim")
        table.add_row(
            stage.value,
            f"[{colour}]{state}[/{colour}]",
            str(info.get("attempts", 0)),
            str(info.get("items_done", 0)),
            (info.get("message") or "")[:70],
        )
    console.print(table)

    storage = StorageManager(
        loaded.settings,
        loaded.paths,
        job_date=loaded.config.job_date,
        topic_slug=loaded.config.topic_slug,
    )
    drive_summary = storage.summary()
    console.print(
        f"\n[bold]Drive:[/bold] {drive_summary['artefacts_uploaded_and_verified']} "
        f"verified, {drive_summary['artefacts_pending_upload']} pending "
        f"({drive_summary['drive_status']})"
    )


@app.command()
def jobs(root: Optional[Path] = RootOption) -> None:
    """List every job found in this project."""
    settings = load_settings(root)
    found = list_jobs(settings)
    if not found:
        console.print("[yellow]No jobs found yet.[/yellow]")
        console.print(
            'Start one with: [cyan]python -m literature_review_agent run --topic "..."[/cyan]'
        )
        return
    table = Table("Date", "Topic", "Stages", "Job folder")
    for entry in found:
        table.add_row(
            entry["job_date"],
            entry["topic"][:50],
            entry["stages_complete"],
            entry["job_folder"],
        )
    console.print(table)


# ---------------------------------------------------------------------------
# Google Drive
# ---------------------------------------------------------------------------


@app.command(name="drive-status")
def drive_status(root: Optional[Path] = RootOption) -> None:
    """Report whether Google Drive syncing is configured and usable."""
    settings = load_settings(root)
    readiness = describe_drive_readiness(settings)

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()
    table.add_row("Enabled", "yes" if readiness.enabled else "no")
    table.add_row("Auth method", readiness.auth_method)
    table.add_row("Destination", readiness.location)
    table.add_row("Google libraries", "installed" if readiness.libraries_installed else "missing")
    table.add_row("Credential file", "present" if readiness.credentials_present else "missing")
    table.add_row("Cached token", "present" if readiness.token_present else "none")
    table.add_row("Ready", "[green]yes[/green]" if readiness.ready else "[yellow]no[/yellow]")
    console.print(Panel(table, title="Google Drive", border_style="cyan"))

    if readiness.problems:
        console.print("[yellow]Outstanding problems:[/yellow]")
        for problem in readiness.problems:
            console.print(f"  - {problem}")
    if readiness.setup_steps:
        console.print("\n[bold]Setup steps:[/bold]")
        for index, step in enumerate(readiness.setup_steps, 1):
            console.print(f"  {index}. {step}")
        console.print(
            "\n[dim]Credential files stay on your machine. Never commit them and never "
            "paste their contents into a chat.[/dim]"
        )


@app.command(name="drive-login")
def drive_login(root: Optional[Path] = RootOption) -> None:
    """Authorise Google Drive access once and cache the token locally."""
    settings = load_settings(root)
    readiness = describe_drive_readiness(settings)
    if not readiness.enabled:
        console.print("[yellow]Drive syncing is disabled in config/google_drive.yaml.[/yellow]")
        raise typer.Exit(code=1)
    if not readiness.credentials_present:
        console.print(f"[red]{readiness.summary()}[/red]\n")
        for index, step in enumerate(readiness.setup_steps, 1):
            console.print(f"  {index}. {step}")
        raise typer.Exit(code=2)

    console.print("Opening a browser window for Google authorisation...")
    try:
        client = DriveClient(settings)
        about = client.about()
        user = (about.get("user") or {}).get("emailAddress", "unknown account")
        console.print(f"[green]Authorised as {user}.[/green]")
        root_id = client.ensure_root_folder()
        console.print(
            f"[green]Root folder ready:[/green] "
            f"{settings.drive.get('root_folder_name')} (id {root_id})"
        )
    except DriveNotConfiguredError as exc:
        console.print(f"[red]{exc}[/red]")
        for index, step in enumerate(exc.steps, 1):
            console.print(f"  {index}. {step}")
        raise typer.Exit(code=2) from exc
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Authorisation failed: {type(exc).__name__}: {exc}[/red]")
        raise typer.Exit(code=1) from exc


@app.command(name="drive-sync")
def drive_sync(job: Path = JobOption, root: Optional[Path] = RootOption) -> None:
    """Retry every artefact that is not yet verified in Google Drive."""
    loaded = _load_job(job, root)
    storage = StorageManager(
        loaded.settings,
        loaded.paths,
        job_date=loaded.config.job_date,
        topic_slug=loaded.config.topic_slug,
    )
    if not storage.drive_enabled:
        console.print(f"[red]Drive syncing is not usable: {storage.readiness.summary()}[/red]")
        for index, step in enumerate(storage.readiness.setup_steps, 1):
            console.print(f"  {index}. {step}")
        raise typer.Exit(code=2)

    pending = storage.pending_uploads()
    if not pending:
        console.print("[green]Every tracked artefact is already uploaded and verified.[/green]")
        return

    console.print(f"Retrying {len(pending)} artefact(s)...")
    storage.ensure_remote_tree()
    outcomes = storage.retry_pending()
    verified = sum(1 for o in outcomes if o.verified)
    console.print(
        f"[green]{verified}[/green] verified, "
        f"[yellow]{len(outcomes) - verified}[/yellow] still pending."
    )
    for outcome in outcomes:
        if not outcome.verified:
            console.print(f"  [yellow]{outcome.local_path.name}: {outcome.status}[/yellow]")


@app.command(name="drive-tree")
def drive_tree(job: Path = JobOption, root: Optional[Path] = RootOption) -> None:
    """Create the job's Google Drive folder structure without uploading."""
    loaded = _load_job(job, root)
    storage = StorageManager(
        loaded.settings,
        loaded.paths,
        job_date=loaded.config.job_date,
        topic_slug=loaded.config.topic_slug,
    )
    tree = storage.ensure_remote_tree()
    if tree is None:
        console.print(f"[red]Could not create the Drive tree: {storage.readiness.summary()}[/red]")
        raise typer.Exit(code=2)
    table = Table("Drive folder", "Folder ID")
    for name, folder_id in tree.items():
        table.add_row(name, folder_id)
    console.print(table)


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


@app.command()
def doctor(root: Optional[Path] = RootOption) -> None:
    """Diagnose the environment and report anything that needs attention."""
    project_root = Path(root).resolve() if root else find_project_root()
    problems: list[str] = []

    console.print(f"[bold]Project root:[/bold] {project_root}")

    for name in (
        "httpx", "pydantic", "pandas", "openpyxl", "docx", "pymupdf", "pypdf",
        "rapidfuzz", "typer", "rich", "bibtexparser", "rispy", "yaml", "dotenv",
        "tenacity", "nbformat",
    ):
        try:
            __import__(name)
        except ImportError:
            problems.append(f"The '{name}' package is not installed.")
    console.print(
        "[green]Core packages installed.[/green]"
        if not problems
        else f"[red]{len(problems)} package(s) missing.[/red]"
    )

    try:
        settings = load_settings(project_root)
        console.print("[green]Configuration files load correctly.[/green]")
        console.print(f"  Sources available: {len(settings.available_sources())}")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"Configuration could not be loaded: {exc}")
        console.print(f"[red]{problems[-1]}[/red]")
        settings = None

    if settings is not None:
        readiness = describe_drive_readiness(settings)
        if readiness.enabled and not readiness.ready:
            problems.append(f"Google Drive is not usable: {'; '.join(readiness.problems)}")
        console.print(
            "[green]Google Drive ready.[/green]"
            if readiness.ready
            else f"[yellow]{readiness.summary()}[/yellow]"
        )

        ranking = settings.q1_ranking.get("file")
        resolved = settings.resolve_path(str(ranking)) if ranking else None
        if not resolved or not resolved.exists():
            console.print(
                "[yellow]No journal-ranking file: quartiles will be 'Unverified'.[/yellow]"
            )

    absent = missing_keys()
    if absent:
        console.print(
            f"[dim]Optional credentials not set ({len(absent)}): {', '.join(absent)}[/dim]"
        )

    if problems:
        console.print(f"\n[red]{len(problems)} problem(s) need attention:[/red]")
        for problem in problems:
            console.print(f"  - {problem}")
        raise typer.Exit(code=1)
    console.print("\n[green]No blocking problems found.[/green]")


def main() -> None:
    """Entry point for the console script."""
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
