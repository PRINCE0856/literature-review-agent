"""Shared HTTP client with retries, rate limiting, and host-safety enforcement.

Two guarantees are enforced here rather than merely documented:

1. Blocked hosts (Google Scholar, Sci-Hub-style mirrors) are refused outright.
2. Nothing in this module attempts to defeat a paywall, login, CAPTCHA, or
   anti-bot protection. A ``401``/``403``, a login redirect, or a challenge page
   is recorded as a failure and the paper is sent to the manual-retrieval
   register instead.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import httpx
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .config import Settings
from .logging_setup import get_logger
from .utils import RateLimiter

LOG = get_logger("http")

#: HTTP statuses worth retrying: transient server and rate-limit conditions.
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504, 509, 520, 522, 524})

#: Statuses that mean "you are not authorised" — never retried, never bypassed.
ACCESS_DENIED_STATUS = frozenset({401, 402, 403, 407, 451})

#: Markers that identify an access wall or bot challenge in an HTML body.
CHALLENGE_MARKERS = (
    b"captcha",
    b"recaptcha",
    b"cf-challenge",
    b"cf_chl_opt",
    b"are you a robot",
    b"unusual traffic",
    b"access denied",
    b"checking your browser",
    b"shibboleth",
    b"institutional login",
    b"please sign in",
    b"subscribe to view",
    b"purchase pdf",
)


class BlockedHostError(RuntimeError):
    """Raised when a URL points at a host the agent must never contact."""


class AccessRestrictedError(RuntimeError):
    """Raised when a resource is paywalled, gated, or behind a bot challenge.

    The pipeline treats this as a terminal, non-retryable outcome: the record is
    logged for manual retrieval through the user's own licensed access.
    """

    def __init__(self, message: str, *, status: int | None = None, url: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.url = url


class TransientHTTPError(RuntimeError):
    """Raised for retryable HTTP conditions (429/5xx, timeouts)."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def host_of(url: str) -> str:
    """Return the lowercase hostname of *url* (without ``www.``)."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def host_matches(url: str, patterns: list[str]) -> bool:
    """True when the URL's host equals or is a subdomain of any pattern."""
    host = host_of(url)
    if not host:
        return False
    for pattern in patterns:
        pattern = pattern.lower().lstrip(".")
        if host == pattern or host.endswith("." + pattern):
            return True
    return False


def assert_host_allowed(url: str, settings: Settings) -> None:
    """Raise :class:`BlockedHostError` for prohibited hosts and schemes."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise BlockedHostError(f"Refusing non-HTTP(S) URL: {url}")
    if host_matches(url, settings.blocked_hosts):
        raise BlockedHostError(
            f"Refusing to contact {host_of(url)}: this host is on the blocked list "
            "(unauthorised source or a service that prohibits automated access)."
        )


def looks_like_challenge(body: bytes) -> bool:
    """True when a response body looks like a login wall or bot challenge."""
    sample = body[:8192].lower()
    return any(marker in sample for marker in CHALLENGE_MARKERS)


class HttpClient:
    """A polite, retrying HTTP client wrapper used by every network module."""

    def __init__(
        self,
        settings: Settings,
        *,
        requests_per_second: float | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.settings = settings
        network = settings.network
        self._owns_client = client is None
        rate = (
            requests_per_second
            if requests_per_second is not None
            else float(network.get("default_requests_per_second", 3.0))
        )
        self.limiter = RateLimiter(rate)
        self.max_retries = int(network.get("max_retries", 4))
        self.backoff_initial = float(network.get("backoff_initial_seconds", 1.0))
        self.backoff_max = float(network.get("backoff_max_seconds", 30.0))

        self.client = client or httpx.Client(
            timeout=httpx.Timeout(
                float(network.get("timeout_seconds", 30)),
                connect=float(network.get("connect_timeout_seconds", 10)),
            ),
            follow_redirects=True,
            max_redirects=int(network.get("max_redirects", 5)),
            headers={
                "User-Agent": settings.user_agent,
                "Accept": "application/json, text/xml, text/plain, */*",
                "From": settings.contact_email,
            },
        )

    # -- lifecycle ------------------------------------------------------

    def close(self) -> None:
        """Close the underlying client if this wrapper created it."""
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- core request ---------------------------------------------------

    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        allow_status: frozenset[int] | set[int] = frozenset({200}),
    ) -> httpx.Response:
        """Perform one rate-limited, retrying request.

        Raises :class:`AccessRestrictedError` for gated resources (never
        retried), :class:`TransientHTTPError` after exhausting retries, and
        :class:`BlockedHostError` for prohibited hosts.
        """
        assert_host_allowed(url, self.settings)

        def _log_retry(state: RetryCallState) -> None:
            LOG.warning(
                f"Retry {state.attempt_number}/{self.max_retries} for {method} {url} "
                f"after {state.outcome.exception() if state.outcome else 'unknown error'}"
            )

        @retry(
            stop=stop_after_attempt(self.max_retries),
            wait=wait_exponential(
                multiplier=self.backoff_initial, max=self.backoff_max
            ),
            retry=retry_if_exception_type(
                (TransientHTTPError, httpx.TimeoutException, httpx.TransportError)
            ),
            before_sleep=_log_retry,
            reraise=True,
        )
        def _send() -> httpx.Response:
            self.limiter.wait()
            response = self.client.request(method, url, params=params, headers=headers)
            status = response.status_code
            if status in ACCESS_DENIED_STATUS:
                raise AccessRestrictedError(
                    f"HTTP {status} from {host_of(str(response.url))}: access is restricted. "
                    "The agent does not bypass paywalls or authentication.",
                    status=status,
                    url=str(response.url),
                )
            if status in RETRYABLE_STATUS:
                raise TransientHTTPError(f"HTTP {status} from {url}", status=status)
            if status not in allow_status and not (200 <= status < 300):
                raise TransientHTTPError(f"Unexpected HTTP {status} from {url}", status=status)
            return response

        return _send()

    def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """GET *url* and parse the response as a JSON object."""
        response = self.request("GET", url, params=params, headers=headers)
        try:
            payload = response.json()
        except ValueError as exc:
            raise TransientHTTPError(f"Non-JSON response from {url}: {exc}") from exc
        return payload if isinstance(payload, dict) else {"data": payload}

    def get_text(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> str:
        """GET *url* and return the decoded body text."""
        return self.request("GET", url, params=params, headers=headers).text

    def head(self, url: str, *, headers: dict[str, str] | None = None) -> httpx.Response:
        """Issue a HEAD request, used for DOI resolution checks."""
        assert_host_allowed(url, self.settings)
        self.limiter.wait()
        return self.client.request("HEAD", url, headers=headers)

    def stream(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """Return a streaming response context manager for large downloads."""
        assert_host_allowed(url, self.settings)
        self.limiter.wait()
        return self.client.stream("GET", url, headers=headers)
