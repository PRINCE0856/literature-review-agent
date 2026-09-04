"""Configuration loading: YAML files, environment variables, and API-key gating.

Secrets are never stored in YAML. Optional API keys are read from the process
environment (optionally seeded from ``.env``), and any source whose key is
missing is reported as unavailable rather than failing the run.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from .utils import ensure_dir


class ConfigError(RuntimeError):
    """Raised when configuration files are missing or structurally invalid."""


# ---------------------------------------------------------------------------
# Project root discovery
# ---------------------------------------------------------------------------

#: Files that mark the project root, in priority order.
_ROOT_MARKERS = ("pyproject.toml", "CLAUDE.md", "config/default_config.yaml")


def find_project_root(start: Path | None = None) -> Path:
    """Walk upwards from *start* until a project-root marker is found.

    Used by the CLI and the notebook so neither needs a hard-coded absolute
    path. Falls back to the package's own grandparent (``src/..``).
    """
    if env_root := os.environ.get("LITERATURE_REVIEW_ROOT"):
        candidate = Path(env_root).expanduser().resolve()
        if candidate.exists():
            return candidate

    here = Path(start).resolve() if start else Path.cwd().resolve()
    for directory in (here, *here.parents):
        if any((directory / marker).exists() for marker in _ROOT_MARKERS):
            return directory

    # Package layout is <root>/src/literature_review_agent/config.py
    return Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Environment / credentials
# ---------------------------------------------------------------------------

#: Environment variables recognised for optional, authorised API access.
OPTIONAL_KEY_ENVS: tuple[str, ...] = (
    "OPENALEX_API_KEY",
    "SEMANTIC_SCHOLAR_API_KEY",
    "CORE_API_KEY",
    "ELSEVIER_API_KEY",
    "ELSEVIER_INSTTOKEN",
    "SPRINGER_API_KEY",
    "ANTHROPIC_API_KEY",
)

#: Environment variables that point at Google credential *files*.
#: The files themselves are git-ignored; their contents are never read into
#: configuration, logs, or any generated artefact.
DRIVE_PATH_ENVS: tuple[str, ...] = (
    "GOOGLE_DRIVE_CREDENTIALS_FILE",
    "GOOGLE_DRIVE_TOKEN_FILE",
    "GOOGLE_SERVICE_ACCOUNT_FILE",
)


def load_environment(root: Path | None = None) -> None:
    """Load ``.env`` from the project root without overriding real env vars."""
    root = root or find_project_root()
    env_path = root / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=False)


def get_secret(name: str) -> str | None:
    """Return an environment secret, treating blanks and placeholders as absent."""
    raw = os.environ.get(name)
    if raw is None:
        return None
    value = raw.strip()
    if not value:
        return None
    lowered = value.lower()
    if lowered.startswith(("your-", "your_", "changeme", "replace-me", "<")):
        return None
    if lowered in {"none", "null", "todo", "n/a"}:
        return None
    return value


def available_keys() -> dict[str, bool]:
    """Report which optional credentials are present in this environment."""
    return {name: get_secret(name) is not None for name in OPTIONAL_KEY_ENVS}


def missing_keys() -> list[str]:
    """Return the optional credentials that are not configured."""
    return [name for name, present in available_keys().items() if not present]


# ---------------------------------------------------------------------------
# YAML loading
# ---------------------------------------------------------------------------


def _load_yaml(path: Path) -> dict[str, Any]:
    """Load a YAML mapping from *path*, raising :class:`ConfigError` on problems."""
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"Configuration file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:  # pragma: no cover - malformed user edit
        raise ConfigError(f"Could not parse YAML in {path}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"Expected a mapping at the top level of {path}")
    return data


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge *override* into a copy of *base*."""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _resolve_dynamic_values(data: Any) -> Any:
    """Replace sentinel strings such as ``current_year`` with real values."""
    if isinstance(data, dict):
        return {k: _resolve_dynamic_values(v) for k, v in data.items()}
    if isinstance(data, list):
        return [_resolve_dynamic_values(v) for v in data]
    if data == "current_year":
        return date.today().year
    return data


# ---------------------------------------------------------------------------
# Source availability
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceSpec:
    """One configured discovery source and whether it can actually run."""

    name: str
    label: str
    kind: str
    base_url: str
    enabled: bool
    requests_per_second: float
    api_key_env: str | None
    insttoken_env: str | None
    requires_email: bool
    notes: str

    @property
    def api_key(self) -> str | None:
        """The configured API key, if any."""
        return get_secret(self.api_key_env) if self.api_key_env else None

    @property
    def insttoken(self) -> str | None:
        """The configured institutional token, if any (Elsevier)."""
        return get_secret(self.insttoken_env) if self.insttoken_env else None

    @property
    def available(self) -> bool:
        """True when the source is enabled and its required key is present."""
        if not self.enabled:
            return False
        if self.api_key_env and self.api_key_env in _REQUIRED_KEY_ENVS:
            return self.api_key is not None
        return True

    @property
    def unavailable_reason(self) -> str:
        """Human-readable explanation for a skipped source."""
        if not self.enabled:
            return "disabled in config/search_sources.yaml"
        if self.api_key_env and self.api_key_env in _REQUIRED_KEY_ENVS and not self.api_key:
            return f"{self.api_key_env} is not set"
        return ""


#: Sources that cannot function at all without their key.
_REQUIRED_KEY_ENVS = frozenset({"CORE_API_KEY", "ELSEVIER_API_KEY", "SPRINGER_API_KEY"})


# ---------------------------------------------------------------------------
# Settings container
# ---------------------------------------------------------------------------


@dataclass
class Settings:
    """Merged, validated configuration for one process run."""

    root: Path
    defaults: dict[str, Any] = field(default_factory=dict)
    sources_config: dict[str, Any] = field(default_factory=dict)
    columns_config: dict[str, Any] = field(default_factory=dict)
    drive_config: dict[str, Any] = field(default_factory=dict)

    # -- section accessors ----------------------------------------------

    @property
    def job_defaults(self) -> dict[str, Any]:
        """Default job parameters (year range, max papers, and so on)."""
        return self.defaults.get("job_defaults", {})

    @property
    def paths(self) -> dict[str, str]:
        """Names of the five top-level output folders."""
        return self.defaults.get("paths", {})

    @property
    def network(self) -> dict[str, Any]:
        """Timeouts, retries, rate limits, and user-agent settings."""
        return self.defaults.get("network", {})

    @property
    def search(self) -> dict[str, Any]:
        """Search breadth and filtering settings."""
        return self.defaults.get("search", {})

    @property
    def deduplication(self) -> dict[str, Any]:
        """Fuzzy-matching thresholds used when merging duplicates."""
        return self.defaults.get("deduplication", {})

    @property
    def relevance(self) -> dict[str, Any]:
        """Relevance-scoring weights."""
        return self.defaults.get("relevance", {})

    @property
    def selection(self) -> dict[str, Any]:
        """Inclusion thresholds and pending-list settings."""
        return self.defaults.get("selection", {})

    @property
    def q1_ranking(self) -> dict[str, Any]:
        """Journal-ranking file location and column mapping."""
        return self.defaults.get("q1_ranking", {})

    @property
    def pdf(self) -> dict[str, Any]:
        """PDF validation and OCR settings."""
        return self.defaults.get("pdf", {})

    @property
    def analysis(self) -> dict[str, Any]:
        """Analysis limits and optional LLM settings."""
        return self.defaults.get("analysis", {})

    @property
    def reporting(self) -> dict[str, Any]:
        """Excel/Word reporting options."""
        return self.defaults.get("reporting", {})

    @property
    def verification(self) -> dict[str, Any]:
        """Verification strictness options."""
        return self.defaults.get("verification", {})

    # -- Google Drive ---------------------------------------------------

    @property
    def drive(self) -> dict[str, Any]:
        """Drive location, auth method, and credential *paths* (never secrets)."""
        return self.drive_config.get("drive", {})

    @property
    def upload(self) -> dict[str, Any]:
        """Upload, verification, and retry policy for Drive."""
        return self.drive_config.get("upload", {})

    @property
    def drive_mime_types(self) -> dict[str, str]:
        """Extension-to-MIME-type map applied on upload."""
        return dict(self.drive_config.get("mime_types", {}))

    @property
    def drive_enabled(self) -> bool:
        """True when Drive syncing is switched on in configuration.

        Whether it can actually authenticate is a separate question, answered by
        :func:`literature_review_agent.drive_storage.describe_drive_readiness`.
        """
        env_override = get_secret("GOOGLE_DRIVE_ENABLED")
        if env_override is not None:
            return env_override.strip().lower() in {"1", "true", "yes", "on"}
        return bool(self.drive.get("enabled", False))

    def resolve_path(self, value: str | None) -> Path | None:
        """Resolve a configured path against the project root."""
        if not value:
            return None
        path = Path(str(value)).expanduser()
        return path if path.is_absolute() else (self.root / path)

    @property
    def blocked_hosts(self) -> list[str]:
        """Hosts the pipeline must never contact."""
        return list(self.sources_config.get("blocked_hosts", []))

    @property
    def authorised_pdf_only_hosts(self) -> list[str]:
        """Publisher hosts requiring an API-supplied authorised PDF URL."""
        return list(self.sources_config.get("authorised_pdf_only_hosts", []))

    @property
    def trusted_oa_hosts(self) -> list[str]:
        """Open-access hosts a validated PDF may be fetched from directly."""
        return list(self.sources_config.get("trusted_oa_hosts", []))

    @property
    def contact_email(self) -> str:
        """Contact email used for polite-pool API access."""
        return (
            get_secret("LITERATURE_REVIEW_CONTACT_EMAIL")
            or self.network.get("contact_email")
            or "researcher@example.org"
        )

    @property
    def user_agent(self) -> str:
        """Descriptive user agent sent with every HTTP request."""
        template = self.network.get(
            "user_agent_template", "LiteratureReviewAgent/1.0 (mailto:{contact_email})"
        )
        return template.format(contact_email=self.contact_email)

    # -- sources --------------------------------------------------------

    def source_specs(self) -> dict[str, SourceSpec]:
        """Return every configured source as a :class:`SourceSpec`."""
        specs: dict[str, SourceSpec] = {}
        for name, raw in (self.sources_config.get("sources") or {}).items():
            specs[name] = SourceSpec(
                name=name,
                label=raw.get("label", name),
                kind=raw.get("kind", "metadata"),
                base_url=raw.get("base_url", ""),
                enabled=bool(raw.get("enabled", True)),
                requests_per_second=float(
                    raw.get(
                        "requests_per_second",
                        self.network.get("default_requests_per_second", 3.0),
                    )
                ),
                api_key_env=raw.get("api_key_env"),
                insttoken_env=raw.get("insttoken_env"),
                requires_email=bool(raw.get("requires_email", False)),
                notes=str(raw.get("notes", "")).strip(),
            )
        return specs

    def available_sources(self) -> dict[str, SourceSpec]:
        """Return only the sources that can actually run right now."""
        return {name: spec for name, spec in self.source_specs().items() if spec.available}

    def unavailable_sources(self) -> dict[str, str]:
        """Map each skipped source to the reason it was skipped."""
        return {
            name: spec.unavailable_reason
            for name, spec in self.source_specs().items()
            if not spec.available
        }

    # -- paths ----------------------------------------------------------

    def top_level_dirs(self) -> dict[str, Path]:
        """Absolute paths of the five numbered output folders."""
        return {key: self.root / value for key, value in self.paths.items()}

    def ensure_top_level_dirs(self) -> dict[str, Path]:
        """Create the numbered output folders if they do not exist."""
        created = {}
        for key, path in self.top_level_dirs().items():
            created[key] = ensure_dir(path)
        return created

    def sheet_columns(self, sheet_key: str) -> list[dict[str, Any]]:
        """Return the column definitions for one Excel sheet."""
        sheets = self.columns_config.get("sheets", {})
        return list((sheets.get(sheet_key) or {}).get("columns", []))

    def sheet_name(self, sheet_key: str) -> str:
        """Return the display name for one Excel sheet."""
        sheets = self.columns_config.get("sheets", {})
        return (sheets.get(sheet_key) or {}).get("name", sheet_key.replace("_", " ").title())

    @property
    def status_colours(self) -> dict[str, str]:
        """Fill colours used for verification/download status cells."""
        return dict(self.columns_config.get("status_colours", {}))


def load_settings(root: Path | None = None, *, overrides: dict[str, Any] | None = None) -> Settings:
    """Load and merge all configuration for the project rooted at *root*."""
    root = Path(root).resolve() if root else find_project_root()
    load_environment(root)

    config_dir = root / "config"
    defaults = _resolve_dynamic_values(_load_yaml(config_dir / "default_config.yaml"))
    if overrides:
        defaults = _deep_merge(defaults, overrides)

    drive_path = config_dir / "google_drive.yaml"
    drive_config = _load_yaml(drive_path) if drive_path.exists() else {}

    return Settings(
        root=root,
        defaults=defaults,
        sources_config=_load_yaml(config_dir / "search_sources.yaml"),
        columns_config=_load_yaml(config_dir / "report_columns.yaml"),
        drive_config=drive_config,
    )


@lru_cache(maxsize=8)
def cached_settings(root_str: str) -> Settings:
    """Memoised :func:`load_settings` for repeated CLI/notebook calls."""
    return load_settings(Path(root_str))
