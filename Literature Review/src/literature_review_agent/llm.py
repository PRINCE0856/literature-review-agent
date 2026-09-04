"""Optional Claude assistance for keyword expansion and paper analysis.

The pipeline is fully functional without any LLM: every stage has a deterministic
implementation. When ``ANTHROPIC_API_KEY`` is present *and* the ``anthropic``
package is installed, these helpers enrich the deterministic output — they never
replace evidence, and a failed or absent LLM call simply falls back.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .config import Settings, get_secret
from .logging_setup import get_logger

LOG = get_logger("llm")

#: Default model used when the config does not name one.
DEFAULT_MODEL = "claude-opus-5"


def llm_available(settings: Settings | None = None) -> bool:
    """True when an LLM call could actually succeed right now.

    Checks the ``analysis.enable_llm`` policy, the presence of the API key, and
    the importability of the ``anthropic`` SDK — in that order, cheaply.
    """
    policy = "auto"
    if settings is not None:
        policy = str(settings.analysis.get("enable_llm", "auto")).lower()
    if policy == "never":
        return False
    if get_secret("ANTHROPIC_API_KEY") is None:
        return False
    try:  # the SDK is an optional extra, not a hard dependency
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return True


def unavailable_reason(settings: Settings | None = None) -> str:
    """Explain why LLM assistance is off, for the assumptions list."""
    policy = "auto"
    if settings is not None:
        policy = str(settings.analysis.get("enable_llm", "auto")).lower()
    if policy == "never":
        return "analysis.enable_llm is set to 'never' in default_config.yaml"
    if get_secret("ANTHROPIC_API_KEY") is None:
        return "ANTHROPIC_API_KEY is not set"
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return "the optional 'anthropic' package is not installed"
    return ""


def complete_json(
    prompt: str,
    *,
    system: str = "",
    settings: Settings | None = None,
    max_tokens: int | None = None,
) -> dict[str, Any] | None:
    """Ask Claude for a JSON object, returning ``None`` on any failure.

    Deliberately forgiving: a network error, a rate limit, or a non-JSON reply
    all return ``None`` so the caller keeps its deterministic result.
    """
    if not llm_available(settings):
        return None

    model = DEFAULT_MODEL
    tokens = max_tokens or 4096
    if settings is not None:
        model = str(settings.analysis.get("llm_model") or DEFAULT_MODEL)
        tokens = int(settings.analysis.get("llm_max_output_tokens", tokens))

    try:
        import anthropic

        client = anthropic.Anthropic(api_key=get_secret("ANTHROPIC_API_KEY"))
        response = client.messages.create(
            model=model,
            max_tokens=tokens,
            system=system or "Reply with a single valid JSON object and nothing else.",
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(
            block.text for block in response.content if getattr(block, "type", "") == "text"
        )
        return _extract_json(text)
    except Exception as exc:  # noqa: BLE001 - any failure must be non-fatal
        LOG.warning(f"LLM assistance unavailable for this call ({exc}); using deterministic path.")
        return None


def _extract_json(text: str) -> dict[str, Any] | None:
    """Pull the first JSON object out of a model reply."""
    if not text:
        return None
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None
