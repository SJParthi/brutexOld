"""Dhan client error model — neutral exceptions + retry classification.

The Dhan v2 Data API is plain REST, so failures are mapped from **standard
HTTP status semantics** (Golden Rule #1 — no invented vendor-specific error
codes) into a small neutral hierarchy the rest of BruteX reasons about:

* ``400`` → :class:`DhanBadRequestError` — malformed request (terminal).
* ``401`` / ``403`` → :class:`DhanAuthError` — token expired/invalid
  (triggers a re-auth, not a blind retry).
* ``404`` → :class:`DhanNotFoundError` — unknown securityId/endpoint.
* ``429`` → :class:`DhanRateLimitError` — back off + retry.
* ``5xx`` → :class:`DhanServerError` — transient vendor fault, retry.
* network timeout / connection drop → :class:`DhanTimeoutError` /
  :class:`DhanTransportError` — retry.

:func:`is_retryable` is the single source of truth the orchestrator's
retry policy consults, so retry semantics live in ONE place (CLAUDE.md
§11 COMMON).
"""

from __future__ import annotations

from http import HTTPStatus

from brutex.models import BruteXError

#: Exclusive upper bound of the HTTP 5xx (server-error) range.
_SERVER_ERROR_CEILING = 600


class DhanError(BruteXError):
    """Base class for every Dhan adapter failure."""


class DhanBadRequestError(DhanError):
    """HTTP 400 — malformed request body / parameters (terminal)."""


class DhanAuthError(DhanError):
    """HTTP 401/403 — access token expired, invalid, or unauthorised."""


class DhanNotFoundError(DhanError):
    """HTTP 404 — unknown securityId / endpoint (terminal)."""


class DhanRateLimitError(DhanError):
    """HTTP 429 — per-second or daily quota exceeded (retry after backoff)."""


class DhanServerError(DhanError):
    """HTTP 5xx — transient vendor-side fault (retry)."""


class DhanTimeoutError(DhanError):
    """Network read/connect timeout (retry)."""


class DhanTransportError(DhanError):
    """Connection reset / DNS / other transport failure (retry)."""


def from_http_status(status: int, message: str) -> DhanError:
    """Map an HTTP status code to the matching neutral :class:`DhanError`."""
    if status == HTTPStatus.BAD_REQUEST:
        return DhanBadRequestError(f"HTTP 400 bad request: {message}")
    if status in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
        return DhanAuthError(f"HTTP {status} auth failure: {message}")
    if status == HTTPStatus.NOT_FOUND:
        return DhanNotFoundError(f"HTTP 404 not found: {message}")
    if status == HTTPStatus.TOO_MANY_REQUESTS:
        return DhanRateLimitError(f"HTTP 429 rate limited: {message}")
    if HTTPStatus.INTERNAL_SERVER_ERROR <= status < _SERVER_ERROR_CEILING:
        return DhanServerError(f"HTTP {status} server error: {message}")
    return DhanError(f"unexpected HTTP {status}: {message}")


def is_rate_limit(exc: DhanError) -> bool:
    """True if *exc* is a rate-limit error (the limiter should back off)."""
    return isinstance(exc, DhanRateLimitError)


def is_retryable(exc: DhanError) -> bool:
    """True if *exc* is transient and the orchestrator should retry it.

    Retryable: rate-limit (429), server (5xx), timeout, transport. Terminal:
    bad-request (400), not-found (404), auth (401/403 — needs re-auth, not a
    blind retry), and any bare :class:`DhanError`.
    """
    return isinstance(
        exc,
        (DhanRateLimitError, DhanServerError, DhanTimeoutError, DhanTransportError),
    )


__all__ = [
    "DhanAuthError",
    "DhanBadRequestError",
    "DhanError",
    "DhanNotFoundError",
    "DhanRateLimitError",
    "DhanServerError",
    "DhanTimeoutError",
    "DhanTransportError",
    "from_http_status",
    "is_rate_limit",
    "is_retryable",
]
