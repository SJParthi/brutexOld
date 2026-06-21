"""Groww client error model — neutral exceptions + retry classification.

The Groww SDK raises a typed exception hierarchy (``growwapi.groww.exceptions``,
``docs/groww/api-reference/05-exceptions.md``)::

    GrowwBaseException
    ├── GrowwAPIException
    │   ├── GrowwAPIAuthenticationException
    │   ├── GrowwAPIAuthorisationException
    │   ├── GrowwAPIBadRequestException
    │   ├── GrowwAPINotFoundException
    │   ├── GrowwAPIRateLimitException
    │   └── GrowwAPITimeoutException
    └── GrowwFeedException
        ├── GrowwFeedConnectionException
        └── GrowwFeedNotSubscribedException

This module maps those into a small neutral hierarchy the rest of BruteX reasons
about — the analogue of an HTTP-status-mapping error layer.
Because the Groww SDK is an OPTIONAL extra (``DEC-GROWW-DEPS-REINSTATE-001``),
:func:`from_groww_exception` matches on the exception's **class name** (a string)
rather than importing the SDK classes, so this module imports + tests cleanly
without ``growwapi`` installed.

:func:`is_retryable` is the single source of truth the orchestrator's retry
policy consults (CLAUDE.md §11 COMMON). Rate-limit + timeout + empty-body +
transient 5xx server errors are retryable. The Groww SDK has no distinct
server-error CLASS (only a generic ``GrowwAPIException(msg, code)``), so a 5xx is
mapped to the neutral :class:`GrowwServerError` by its HTTP ``code``
(``500``/``502``/``503``) or an unambiguous message signature — never by inventing
an SDK class (DEC-GROWW-SERVER-ERROR-RETRY-001; Golden Rule #1). A ``504``
gateway-timeout stays a :class:`GrowwTimeoutError` (already retryable) so the
gentle timeout ladder, not a new path, governs it.
"""

from __future__ import annotations

import json

from brutex.models import BruteXError


class GrowwError(BruteXError):
    """Base class for every Groww adapter failure (terminal unless a subclass)."""


class GrowwBadRequestError(GrowwError):
    """Malformed request parameters (terminal)."""


class GrowwAuthError(GrowwError):
    """Authentication / authorisation failure (triggers re-auth, not blind retry)."""


class GrowwNotFoundError(GrowwError):
    """Unknown instrument / endpoint (terminal)."""


class GrowwRateLimitError(GrowwError):
    """Per-second or daily quota exceeded (back off + retry)."""


class GrowwTimeoutError(GrowwError):
    """Network read/connect timeout (retry)."""


class GrowwServerError(GrowwError):
    """Transient vendor server error (HTTP 5xx) — RETRYABLE.

    The Groww SDK has no dedicated server-error subclass; a 5xx surfaces as a
    generic ``GrowwAPIException`` carrying a ``500``/``502``/``503`` ``code`` (or an
    "internal server error" / "bad gateway" / "service unavailable" message). The
    old classifier let that fall to the terminal base :class:`GrowwError`, so a
    momentary upstream blip TERMINALLY quarantined a real symbol instead of
    re-pulling it. This maps such a failure to a RETRYABLE error
    (DEC-GROWW-SERVER-ERROR-RETRY-001): the in-call fast-retry re-issues it and any
    residual is re-queued by the until-clean loop — never dropped, never faked.

    It is deliberately NOT a :class:`GrowwRateLimitError`: a 5xx is not a quota
    signal, so it must not step the per-second rate ladder down — it rides the
    gentler timeout ladder (:func:`is_rate_limit` stays ``False`` for it). A genuine
    ``404`` still maps to a terminal :class:`GrowwNotFoundError`, so this never
    makes a real "not on Groww" retryable.
    """


class GrowwEmptyResponseError(GrowwError):
    """Empty / non-JSON vendor body — a transient throttle, RETRYABLE.

    The Groww SDK decodes every REST body with ``response.json()``. Under a
    transient throttle the vendor returns an EMPTY (or HTML/garbled) body and the
    SDK's decode raises a :class:`json.JSONDecodeError` (a :class:`ValueError`
    subclass) whose message is ``"Expecting value: line 1 column 1 (char 0)"`` —
    NOT a real ``404``. The old classifier let that fall to the terminal base
    :class:`GrowwError`, so a live pull mis-saw an empty-body blip as "not on
    Groww" and TERMINALLY quarantined real F&O stocks (TITAN / SHRIRAMFIN).

    This maps it to a RETRYABLE error (DEC-GROWW-EMPTY-BODY-RETRY-001): the
    in-call fast-retry re-issues it, and any residual that exhausts the budget is
    re-queued by the until-clean loop — never dropped. A genuine missing symbol
    still returns a real ``404`` → :class:`GrowwNotFoundError` (stays terminal),
    so this never makes a real 404 retryable.
    """


class GrowwFeedError(GrowwError):
    """Live-feed (WebSocket) failure — OUT of Track-1 scope (terminal)."""


#: Groww SDK exception class-name → neutral GrowwError subclass. Keyed by NAME
#: (string) so the SDK need not be importable (the optional-extra resilience).
_SDK_NAME_TO_ERROR: dict[str, type[GrowwError]] = {
    "GrowwAPIAuthenticationException": GrowwAuthError,
    "GrowwAPIAuthorisationException": GrowwAuthError,
    "GrowwAPIBadRequestException": GrowwBadRequestError,
    "GrowwAPINotFoundException": GrowwNotFoundError,
    "GrowwAPIRateLimitException": GrowwRateLimitError,
    "GrowwAPITimeoutException": GrowwTimeoutError,
    "GrowwFeedException": GrowwFeedError,
    "GrowwFeedConnectionException": GrowwFeedError,
    "GrowwFeedNotSubscribedException": GrowwFeedError,
}


#: HTTP-ish status codes the SDK may carry on a GENERIC ``GrowwAPIException(msg,
#: code)`` when it does NOT raise the typed subclass — mapped to the retryable
#: neutral error. ``429`` = rate-limit; ``408``/``504`` = timeout; ``500``/``502``/
#: ``503`` = transient server error (DEC-GROWW-SERVER-ERROR-RETRY-001). ``504`` is a
#: gateway-timeout and intentionally stays a TimeoutError (already retryable — the
#: timeout ladder governs it, not a new path).
#: (``docs/groww/api-reference/05-exceptions.md``; the SDK's ``GrowwAPIException``
#: carries ``code``).
_CODE_TO_ERROR: dict[int, type[GrowwError]] = {
    429: GrowwRateLimitError,
    408: GrowwTimeoutError,
    500: GrowwServerError,
    502: GrowwServerError,
    503: GrowwServerError,
    504: GrowwTimeoutError,
}

#: Message substrings (lower-cased) that mark a RETRYABLE failure when the SDK
#: raised a generic exception with no typed subclass + no recognised ``code``. The
#: live Groww SDK surfaces a rate-limit as
#: ``GrowwAPIException("Rate limit has breached for your request. ...")`` — the
#: BASE client-error class, NOT ``GrowwAPIRateLimitException`` — so the message is
#: the only reliable signal. Kept narrow (only the two transient classes) so an
#: unknown failure is never blindly retried (Golden Rule #1).
_RATE_LIMIT_HINTS: tuple[str, ...] = ("rate limit", "too many request", "429")
_TIMEOUT_HINTS: tuple[str, ...] = ("timed out", "timeout", "deadline exceeded")
#: Unambiguous 5xx signatures for a GENERIC exception with no recognised ``code``
#: (DEC-GROWW-SERVER-ERROR-RETRY-001). Phrase-only — NO bare ``"500"``/``"503"``
#: numeric substrings (they would false-match unrelated numbers). Checked AFTER the
#: rate-limit + timeout hints so a 429/timeout never reads as a server error.
_SERVER_ERROR_HINTS: tuple[str, ...] = (
    "internal server error",
    "bad gateway",
    "service unavailable",
    "server error",
)


def _classify_generic(exc: Exception) -> type[GrowwError]:
    """Classify a GENERIC SDK exception by its ``code`` attr + message (retryable-first).

    Used only when the class name is NOT a known typed subclass — i.e. the SDK
    raised a bare ``GrowwAPIException`` / ``GrowwBaseException``. Checks the
    HTTP-ish ``code`` first (``429``→rate-limit, ``408``/``504``→timeout,
    ``500``/``502``/``503``→server-error, int or digit-string), then sniffs the
    message for the transient classes in priority order (rate-limit, then timeout,
    then 5xx server error — so a 429 or timeout that also mentions a server never
    mis-reads as a plain server error). Anything unrecognised stays the terminal
    base :class:`GrowwError` (conservative — an unknown failure is never retried,
    Golden Rule #1).
    """
    code = getattr(exc, "code", None)
    if isinstance(code, bool):  # bool is an int subclass — never a status code
        code = None
    if isinstance(code, int) and code in _CODE_TO_ERROR:
        return _CODE_TO_ERROR[code]
    if isinstance(code, str) and code.strip().isdigit() and int(code) in _CODE_TO_ERROR:
        return _CODE_TO_ERROR[int(code)]
    message = str(exc).casefold()
    if any(hint in message for hint in _RATE_LIMIT_HINTS):
        return GrowwRateLimitError
    if any(hint in message for hint in _TIMEOUT_HINTS):
        return GrowwTimeoutError
    if any(hint in message for hint in _SERVER_ERROR_HINTS):
        return GrowwServerError
    return GrowwError


def from_groww_exception(exc: Exception) -> GrowwError:
    """Map a ``growwapi`` SDK exception instance to a neutral :class:`GrowwError`.

    Classification is by the exception's class name (string), so no SDK import is
    needed. **A bare ``GrowwAPIException`` / ``GrowwBaseException`` (the SDK does
    NOT always raise the typed subclass — a rate-limit commonly arrives as the
    base ``GrowwAPIException`` with a ``429`` ``code`` and/or a "Rate limit has
    breached" message) is then classified by :func:`_classify_generic` so it is
    still recognised as retryable** (DEC-GROWW-RATELIMIT-RETRY-001 — without this
    the live derivatives chain pull mis-saw a 429 as terminal and died instantly).
    Any still-unrecognised failure maps to the terminal base :class:`GrowwError` —
    conservative: an unknown failure is never blindly retried (Golden Rule #1).

    **An empty / non-JSON vendor body** (the SDK's ``response.json()`` raising a
    :class:`json.JSONDecodeError`) is recognised FIRST as a RETRYABLE
    :class:`GrowwEmptyResponseError` (DEC-GROWW-EMPTY-BODY-RETRY-001) — a transient
    throttle, NOT a terminal "not on Groww". It is matched by ``isinstance`` (the
    SDK is an optional extra, but :mod:`json` is stdlib so this import is always
    safe). Narrow on purpose: ONLY a ``json.JSONDecodeError`` is treated this way,
    NOT every :class:`ValueError`, so a genuine code bug surfacing as a plain
    ``ValueError`` still maps to the terminal base (never blindly retried).
    """
    name = type(exc).__name__
    if isinstance(exc, json.JSONDecodeError):
        return GrowwEmptyResponseError(f"{name}: {exc}")
    error_cls = _SDK_NAME_TO_ERROR.get(name)
    if error_cls is None:
        error_cls = _classify_generic(exc)
    return error_cls(f"{name}: {exc}")


def is_rate_limit(exc: GrowwError) -> bool:
    """True if *exc* is a rate-limit error (the limiter should back off)."""
    return isinstance(exc, GrowwRateLimitError)


def is_auth_failure(exc: GrowwError) -> bool:
    """True if *exc* means Groww auth failed (drives a re-auth, not a retry)."""
    return isinstance(exc, GrowwAuthError)


def is_retryable(exc: GrowwError) -> bool:
    """True if *exc* is transient and the orchestrator should retry it.

    Retryable: rate-limit + timeout + empty/non-JSON body (a transient throttle —
    DEC-GROWW-EMPTY-BODY-RETRY-001) + transient 5xx server error (a momentary
    upstream blip — DEC-GROWW-SERVER-ERROR-RETRY-001). Terminal: bad-request,
    not-found, auth (needs re-auth), feed (out of scope), and any bare
    :class:`GrowwError`.
    """
    return isinstance(
        exc,
        (GrowwRateLimitError, GrowwTimeoutError, GrowwEmptyResponseError, GrowwServerError),
    )


__all__ = [
    "GrowwAuthError",
    "GrowwBadRequestError",
    "GrowwEmptyResponseError",
    "GrowwError",
    "GrowwFeedError",
    "GrowwNotFoundError",
    "GrowwRateLimitError",
    "GrowwServerError",
    "GrowwTimeoutError",
    "from_groww_exception",
    "is_auth_failure",
    "is_rate_limit",
    "is_retryable",
]
