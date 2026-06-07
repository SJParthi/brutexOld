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


#: Dhan's "no data / incorrect parameters" 400 marker (errorCode ``DH-905``).
#: Returned for a genuinely-empty range (e.g. an index whose DAILY history
#: starts after the requested ``fromDate``) AND for a true param bug — Dhan does
#: not distinguish, so the caller treats it as a re-tryable KNOWN GAP, never a
#: silent skip (DEC-DHAN-NO-DATA-KNOWN-GAP-001 / Golden Rule #1).
_NO_DATA_MARKERS: tuple[str, ...] = ("DH-905", "no data present")


def is_no_data_error(exc: DhanError) -> bool:
    """True if *exc* is a Dhan HTTP-400 signalling NO DATA (``DH-905``).

    Distinguishes a "no data for this symbol/interval/range" 400 from a generic
    malformed-request 400 so the ingest spine can record it as a visible
    ``KNOWN_GAP`` (isolated, NOT marked complete → re-tried next pull) rather than
    a scary terminal quarantine — a transient/param case then self-heals while a
    genuine gap is surfaced, never silently masked (Golden Rule #1).

    Parameters
    ----------
    exc
        Any :class:`DhanError`.

    Returns
    -------
    bool
        ``True`` iff *exc* is a :class:`DhanBadRequestError` whose message carries
        a Dhan no-data marker (``DH-905`` / "no data present").
    """
    if not isinstance(exc, DhanBadRequestError):
        return False
    text = str(exc)
    return any(marker in text for marker in _NO_DATA_MARKERS)


#: Dhan markers for a TOKEN-INVALID HTTP-400 (``DH-906`` / "Invalid Token") that
#: masquerades as a generic bad request. The 24h web token sometimes expires into
#: a 400 carrying ``DH-906`` rather than a clean 401 — so the "token is dead"
#: predicate must treat it as an auth failure, not a per-symbol terminal bug
#: (DEC-DHAN-AUTH-FAILFAST-RENEW-001 / Golden Rule #1).
_TOKEN_INVALID_MARKERS: tuple[str, ...] = ("DH-906", "Invalid Token")


def is_auth_failure(exc: DhanError) -> bool:
    """True if *exc* means the Dhan access token is dead (expired/invalid).

    The single source of truth for "the token died" detection
    (DEC-DHAN-AUTH-FAILFAST-RENEW-001). On a real ``make run`` the operator's 24h
    web token expired mid-pull: from that moment EVERY remaining symbol failed
    with a 401 (:class:`DhanAuthError`) or a few token-invalid 400s
    (:class:`DhanBadRequestError` carrying ``DH-906`` / "Invalid Token"). Both are
    the SAME root cause — a dead token — so the ingest spine trips a one-way
    fail-fast switch on either, draining the rest of the pull instantly instead
    of firing ~800 doomed 401 calls one symbol at a time.

    Parameters
    ----------
    exc
        Any :class:`DhanError`.

    Returns
    -------
    bool
        ``True`` iff *exc* is a :class:`DhanAuthError` (401/403) OR a
        :class:`DhanBadRequestError` whose message carries a token-invalid marker
        (``DH-906`` / "Invalid Token"). ``False`` for a plain malformed-request
        400, a 404, a 429, or any other error — those stay per-symbol terminal /
        retryable as before (:func:`is_retryable` is unchanged).
    """
    if isinstance(exc, DhanAuthError):
        return True
    if isinstance(exc, DhanBadRequestError):
        text = str(exc)
        return any(marker in text for marker in _TOKEN_INVALID_MARKERS)
    return False


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
    "is_auth_failure",
    "is_no_data_error",
    "is_rate_limit",
    "is_retryable",
]
