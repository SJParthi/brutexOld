"""Groww adapter error model — neutral exceptions + retry classification.

Groww REST API failures are mapped from **standard HTTP status semantics**
(Golden Rule #1) into a small neutral hierarchy the orchestrator reasons about:

* ``400`` → :class:`GrowwBadRequestError` — malformed request (terminal).
* ``401`` / ``403`` → :class:`GrowwAuthError` — token expired (triggers TOTP
  refresh, not a blind retry).
* ``404`` → :class:`GrowwNotFoundError` — unknown symbol/endpoint (terminal).
* ``429`` → :class:`GrowwRateLimitError` — back off + retry.
* ``5xx`` → :class:`GrowwServerError` — transient vendor fault, retry.
* Network timeout / connection drop → :class:`GrowwTimeoutError` /
  :class:`GrowwTransportError` — retry.
* Groww JSON ``status: FAILURE`` → :class:`GrowwApiFailureError` — inspect
  ``code`` to decide retry policy.

:func:`is_retryable` is the single source of truth the orchestrator's retry
policy consults, so retry semantics live in ONE place (CLAUDE.md §11 COMMON).
"""

from __future__ import annotations

from http import HTTPStatus

from brutex.models import BruteXError

#: Exclusive upper bound of the HTTP 5xx range.
_SERVER_ERROR_CEILING: int = 600


class GrowwAdapterError(BruteXError):
    """Base class for every Groww adapter failure."""


class GrowwBadRequestError(GrowwAdapterError):
    """HTTP 400 — malformed request parameters (terminal)."""


class GrowwAuthError(GrowwAdapterError):
    """HTTP 401/403 — access token expired, invalid, or unauthorised.

    The client must regenerate the TOTP code and issue a new access token
    before retrying — do NOT retry with the old token.
    """


class GrowwNotFoundError(GrowwAdapterError):
    """HTTP 404 — unknown groww_symbol or endpoint (terminal)."""


class GrowwRateLimitError(GrowwAdapterError):
    """HTTP 429 — rate limit exceeded (retry after adaptive backoff)."""


class GrowwServerError(GrowwAdapterError):
    """HTTP 5xx — transient Groww-side fault (retry)."""


class GrowwTimeoutError(GrowwAdapterError):
    """Network read or connect timeout (retry)."""


class GrowwTransportError(GrowwAdapterError):
    """Connection reset / DNS / other transport failure (retry)."""


class GrowwApiFailureError(GrowwAdapterError):
    """Groww JSON response body reports ``status: FAILURE``.

    Attributes
    ----------
    code
        The Groww error code string from the response body.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"Groww API failure [{code}]: {message}")
        self.code = code


def from_http_status(status: int, message: str) -> GrowwAdapterError:
    """Map an HTTP status code to the matching :class:`GrowwAdapterError`."""
    if status == HTTPStatus.BAD_REQUEST:
        return GrowwBadRequestError(f"HTTP 400 bad request: {message}")
    if status in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
        return GrowwAuthError(f"HTTP {status} auth failure: {message}")
    if status == HTTPStatus.NOT_FOUND:
        return GrowwNotFoundError(f"HTTP 404 not found: {message}")
    if status == HTTPStatus.TOO_MANY_REQUESTS:
        return GrowwRateLimitError(f"HTTP 429 rate limited: {message}")
    if HTTPStatus.INTERNAL_SERVER_ERROR <= status < _SERVER_ERROR_CEILING:
        return GrowwServerError(f"HTTP {status} server error: {message}")
    return GrowwAdapterError(f"unexpected HTTP {status}: {message}")


def is_retryable(exc: GrowwAdapterError) -> bool:
    """Return ``True`` if *exc* is a transient failure worth retrying.

    Terminal errors (bad request, not found, auth) return ``False``.
    The orchestrator uses this to decide between re-queue and quarantine.
    """
    return isinstance(
        exc,
        (GrowwRateLimitError, GrowwServerError, GrowwTimeoutError, GrowwTransportError),
    )


def is_rate_limit(exc: GrowwAdapterError) -> bool:
    """Return ``True`` if *exc* is specifically a rate-limit error.

    The adaptive token bucket uses this to trigger its step-down logic.
    """
    return isinstance(exc, GrowwRateLimitError)


def is_auth_failure(exc: GrowwAdapterError) -> bool:
    """Return ``True`` if *exc* requires a TOTP token refresh before retry."""
    return isinstance(exc, GrowwAuthError)


__all__ = [
    "GrowwAdapterError",
    "GrowwApiFailureError",
    "GrowwAuthError",
    "GrowwBadRequestError",
    "GrowwNotFoundError",
    "GrowwRateLimitError",
    "GrowwServerError",
    "GrowwTimeoutError",
    "GrowwTransportError",
    "from_http_status",
    "is_auth_failure",
    "is_rate_limit",
    "is_retryable",
]
