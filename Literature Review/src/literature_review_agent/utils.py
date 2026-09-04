"""Filesystem, text, hashing, and rate-limiting helpers.

Every helper here is deliberately dependency-light and side-effect free unless the
name says otherwise, so the rest of the package (and the test suite) can rely on
these primitives without touching the network.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
from collections.abc import Iterable, Iterator
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Characters that Windows forbids in file and directory names.
WINDOWS_INVALID_CHARS = '<>:"/\\|?*'

#: Device names that Windows reserves regardless of extension.
WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)

#: Conservative maximum stem length, leaving room for suffixes and long parents.
MAX_FILENAME_STEM = 150

#: Words ignored when building topic slugs and comparing titles.
STOPWORDS = frozenset(
    """
    a an the and or of for in on at to from with without by as is are was were be been being
    this that these those into over under between about across during than then also such
    how what why when which who whom whose where does do did done doing has have had
    can could shall should will would may might must its it their there here more most
    other others using used use very much many some any both each all
    """.split()
)


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string (second precision)."""
    return utc_now().replace(microsecond=0).isoformat()


def today_stamp(when: date | None = None) -> str:
    """Return a ``YYYY-MM-DD`` stamp used for the date-partitioned job folders."""
    return (when or date.today()).isoformat()


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------


def strip_accents(text: str) -> str:
    """Return *text* with combining marks removed (``café`` -> ``cafe``)."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def slugify(text: str, *, max_length: int = 60, keep_stopwords: bool = False) -> str:
    """Convert free text into a lowercase, filesystem-safe, hyphen-separated slug.

    The slug is only ever used for folder names; the untouched original text is
    always preserved in ``job_config.yaml``.
    """
    ascii_text = strip_accents(text).lower()
    ascii_text = re.sub(r"[^a-z0-9]+", " ", ascii_text)
    words = [w for w in ascii_text.split() if w]
    if not keep_stopwords:
        filtered = [w for w in words if w not in STOPWORDS]
        # Never return an empty slug just because every word was a stopword.
        words = filtered or words
    slug = ""
    for word in words:
        candidate = f"{slug}-{word}" if slug else word
        if len(candidate) > max_length:
            break
        slug = candidate
    if not slug:
        digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]
        slug = f"topic-{digest}"
    return slug


def normalize_title(title: str | None) -> str:
    """Normalise a title for exact-match comparison and deduplication."""
    if not title:
        return ""
    text = strip_accents(title).lower()
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def title_tokens(title: str | None) -> set[str]:
    """Return the significant word tokens of a title, for cheap overlap checks."""
    return {tok for tok in normalize_title(title).split() if tok not in STOPWORDS and len(tok) > 2}


def normalize_doi(doi: str | None) -> str | None:
    """Return a canonical bare DOI (``10.xxxx/yyyy``) or ``None``.

    Accepts full URLs, ``doi:`` prefixes, whitespace, and mixed case. Returns
    ``None`` for anything that does not look like a DOI, so callers never key a
    deduplication decision off a malformed identifier.
    """
    if not doi:
        return None
    text = str(doi).strip().strip(".,;")
    text = re.sub(r"^\s*(doi|DOI)\s*:\s*", "", text)
    text = re.sub(r"^https?://(dx\.)?doi\.org/", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^https?://doi\.org/", "", text, flags=re.IGNORECASE)
    text = text.strip().lower()
    if not text.startswith("10."):
        return None
    if "/" not in text:
        return None
    prefix, _, suffix = text.partition("/")
    if not re.fullmatch(r"10\.\d{4,9}", prefix) or not suffix:
        return None
    return f"{prefix}/{suffix}"


def normalize_issn(issn: str | None) -> str | None:
    """Return an ISSN as ``NNNN-NNNX`` upper-case, or ``None`` if unparseable."""
    if not issn:
        return None
    digits = re.sub(r"[^0-9Xx]", "", str(issn)).upper()
    if len(digits) != 8:
        return None
    return f"{digits[:4]}-{digits[4:]}"


def truncate_text(text: str, limit: int, *, suffix: str = "...") -> str:
    """Truncate *text* to *limit* characters on a word boundary where possible."""
    if text is None:
        return ""
    text = str(text)
    if len(text) <= limit:
        return text
    if limit <= len(suffix):
        return text[:limit]
    cut = text[: limit - len(suffix)]
    if " " in cut:
        cut = cut[: cut.rindex(" ")]
    return cut.rstrip() + suffix


# ---------------------------------------------------------------------------
# Filename helpers
# ---------------------------------------------------------------------------


def safe_filename_stem(title: str, *, max_length: int = MAX_FILENAME_STEM) -> str:
    """Turn a paper title into a cross-platform-safe filename stem.

    Handles Windows-invalid characters, Windows reserved device names, control
    characters, trailing dots/spaces (which Windows silently strips), and length
    limits — while keeping the title readable.
    """
    if title is None:
        title = ""
    text = str(title).replace("\n", " ").replace("\r", " ").replace("\t", " ")
    # Replace path separators and Windows-invalid characters with readable stand-ins.
    text = text.replace("/", "-").replace("\\", "-")
    text = text.replace(":", " -").replace("|", "-")
    text = "".join(" " if ch in WINDOWS_INVALID_CHARS else ch for ch in text)
    # Drop control characters.
    text = "".join(ch for ch in text if ord(ch) >= 32 and ch != "\x7f")
    text = re.sub(r"\s+", " ", text).strip()
    # Windows strips trailing dots and spaces; remove them ourselves so the name
    # on disk is exactly the name we recorded in the manifest.
    text = text.strip(" .")
    if not text:
        text = "Untitled Paper"
    text = truncate_text(text, max_length, suffix="")
    text = text.strip(" .-") or "Untitled Paper"
    if text.split(".")[0].upper() in WINDOWS_RESERVED_NAMES:
        text = f"{text} (paper)"
    return text


def disambiguated_stem(title: str, first_author: str | None, year: int | None) -> str:
    """Build the collision-resolution stem ``Title - FirstAuthor - Year``."""
    author = (first_author or "Unknown Author").split(",")[0].strip() or "Unknown Author"
    year_part = str(year) if year else "n.d."
    base = truncate_text(safe_filename_stem(title), MAX_FILENAME_STEM - 40, suffix="")
    return safe_filename_stem(f"{base} - {author} - {year_part}")


def resolve_collision(
    directory: Path,
    stem: str,
    extension: str,
    *,
    taken: Iterable[str] = (),
) -> Path:
    """Return a free path in *directory* for ``stem + extension``.

    Deterministic: the first free candidate wins, and the numeric suffix order is
    stable, so re-running a job yields the same filenames. ``taken`` lets callers
    reserve names that are planned but not yet written to disk.
    """
    extension = extension if extension.startswith(".") else f".{extension}"
    reserved = {t.lower() for t in taken}

    def is_free(candidate: Path) -> bool:
        return not candidate.exists() and candidate.name.lower() not in reserved

    candidate = directory / f"{stem}{extension}"
    if is_free(candidate):
        return candidate
    for index in range(2, 1000):
        candidate = directory / f"{stem} ({index}){extension}"
        if is_free(candidate):
            return candidate
    digest = hashlib.sha1(f"{stem}{time.time_ns()}".encode()).hexdigest()[:8]
    return directory / f"{stem} ({digest}){extension}"


# ---------------------------------------------------------------------------
# Hashing helpers
# ---------------------------------------------------------------------------


def sha256_file(path: Path, *, chunk_size: int = 1 << 20) -> str:
    """Return the hex SHA-256 digest of a file, read in streaming chunks."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_id(*parts: Any) -> str:
    """Return a short, deterministic identifier derived from *parts*."""
    joined = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# IO helpers
# ---------------------------------------------------------------------------


def ensure_dir(path: Path) -> Path:
    """Create *path* (and parents) if needed and return it."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _json_default(value: Any) -> Any:
    """Fallback JSON encoder for datetimes, dates, paths, and sets."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return str(value)


def write_json(path: Path, payload: Any, *, indent: int = 2) -> Path:
    """Write *payload* as UTF-8 JSON, creating parent directories as needed."""
    path = Path(path)
    ensure_dir(path.parent)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, indent=indent, ensure_ascii=False, default=_json_default),
        encoding="utf-8",
    )
    tmp.replace(path)
    return path


def read_json(path: Path, default: Any = None) -> Any:
    """Read JSON from *path*, returning *default* when the file is absent/blank."""
    path = Path(path)
    if not path.exists() or path.stat().st_size == 0:
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def append_jsonl(path: Path, record: Any) -> Path:
    """Append one JSON record as a line to a ``.jsonl`` log."""
    path = Path(path)
    ensure_dir(path.parent)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, default=_json_default) + "\n")
    return path


def read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    """Yield each record from a ``.jsonl`` log, skipping unparseable lines."""
    path = Path(path)
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def write_text(path: Path, text: str) -> Path:
    """Write UTF-8 text to *path*, creating parent directories as needed."""
    path = Path(path)
    ensure_dir(path.parent)
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


class RateLimiter:
    """Simple monotonic-clock rate limiter shared by the search adapters.

    ``requests_per_second`` of ``0`` or below disables throttling, which keeps the
    mocked test suite fast.
    """

    def __init__(self, requests_per_second: float) -> None:
        self.min_interval = 1.0 / requests_per_second if requests_per_second > 0 else 0.0
        self._last_call = 0.0

    def wait(self) -> None:
        """Block just long enough to respect the configured request rate."""
        if self.min_interval <= 0:
            return
        elapsed = time.monotonic() - self._last_call
        remaining = self.min_interval - elapsed
        if remaining > 0:
            time.sleep(remaining)
        self._last_call = time.monotonic()


def chunked(items: list[Any], size: int) -> Iterator[list[Any]]:
    """Yield *items* in lists of at most *size* elements."""
    if size <= 0:
        raise ValueError("chunk size must be positive")
    for start in range(0, len(items), size):
        yield items[start : start + size]


def coerce_year(value: Any) -> int | None:
    """Extract a plausible 4-digit publication year from arbitrary input."""
    if value is None:
        return None
    if isinstance(value, int) and 1000 < value < 3000:
        return value
    match = re.search(r"(1[89]\d{2}|20\d{2}|21\d{2})", str(value))
    return int(match.group(1)) if match else None


def first_author_surname(authors: list[str] | None) -> str | None:
    """Return the surname of the first author, handling both name orders."""
    if not authors:
        return None
    name = str(authors[0]).strip()
    if not name:
        return None
    if "," in name:
        return name.split(",")[0].strip() or None
    parts = [p for p in name.split() if p]
    return parts[-1] if parts else None
