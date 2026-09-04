"""Structured logging for the pipeline.

Every job writes a human-readable ``pipeline.log`` inside its own
``05 Logs and State`` folder while also emitting rich-formatted console output,
so an interrupted run leaves a complete, inspectable trail on disk.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.logging import RichHandler

from .utils import ensure_dir

LOGGER_NAME = "literature_review_agent"

#: Shared console so progress output and log output do not fight each other.
console = Console(stderr=False, soft_wrap=False)


class JsonLineFormatter(logging.Formatter):
    """Format records as one JSON object per line for machine consumption."""

    def format(self, record: logging.LogRecord) -> str:
        """Render *record* as a compact JSON line."""
        payload: dict[str, Any] = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in getattr(record, "context", {}).items():
            payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class ContextLogger(logging.LoggerAdapter):
    """Logger adapter that attaches a fixed context dict to every record."""

    def process(self, msg: Any, kwargs: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
        """Merge adapter context with any per-call ``context`` keyword."""
        extra = dict(kwargs.get("extra") or {})
        merged = dict(self.extra or {})
        merged.update(extra.pop("context", {}) or {})
        extra["context"] = merged
        kwargs["extra"] = extra
        return msg, kwargs


def setup_logging(
    log_file: Path | None = None,
    *,
    level: int | str = logging.INFO,
    quiet: bool = False,
    json_file: Path | None = None,
) -> logging.Logger:
    """Configure and return the package logger.

    Safe to call repeatedly: existing handlers are cleared first, so resuming a
    job does not duplicate log lines.
    """
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    if not quiet:
        rich_handler = RichHandler(
            console=console,
            show_time=True,
            show_path=False,
            rich_tracebacks=True,
            markup=False,
        )
        rich_handler.setFormatter(logging.Formatter("%(message)s"))
        rich_handler.setLevel(level)
        logger.addHandler(rich_handler)
    else:
        stream = logging.StreamHandler(sys.stderr)
        stream.setLevel(logging.WARNING)
        stream.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        logger.addHandler(stream)

    if log_file is not None:
        log_file = Path(log_file)
        ensure_dir(log_file.parent)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        logger.addHandler(file_handler)

    if json_file is not None:
        json_file = Path(json_file)
        ensure_dir(json_file.parent)
        json_handler = logging.FileHandler(json_file, encoding="utf-8")
        json_handler.setLevel(logging.DEBUG)
        json_handler.setFormatter(JsonLineFormatter())
        logger.addHandler(json_handler)

    return logger


def get_logger(name: str | None = None, **context: Any) -> ContextLogger:
    """Return a context-bound child logger.

    ``get_logger("search", source="crossref")`` tags every message from that
    adapter with ``source=crossref`` in the JSON log.
    """
    base = logging.getLogger(LOGGER_NAME if not name else f"{LOGGER_NAME}.{name}")
    if not logging.getLogger(LOGGER_NAME).handlers:
        setup_logging()
    return ContextLogger(base, context)
