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
policy consults (CLAUDE.md §11 COMMON). Only rate-limit + timeout are retryable;
the Groww hierarchy has no distinct server-error class, so 5xx is not separately
classified here (Golden Rule #1 — no invented vendor error code).
"""

from __future__ import annotations

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
#: neutral error. ``429`` = rate-limit; ``408``/``504`` = timeout
#: (``docs/groww/api-reference/05-exceptions.md``; the SDK's ``GrowwAPIException``
#: carries ``code``).
_CODE_TO_ERROR: dict[int, type[GrowwError]] = {
    429: GrowwRateLimitError,
    408: GrowwTimeoutError,
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


def _classify_generic(exc: Exception) -> type[GrowwError]:
    """Classify a GENERIC SDK exception by its ``code`` attr + message (retryable-first).

    Used only when the class name is NOT a known typed subclass — i.e. the SDK
    raised a bare ``GrowwAPIException`` / ``GrowwBaseException``. Checks the
    HTTP-ish ``code`` first (``429``→rate-limit, ``408``/``504``→timeout, int or
    digit-string), then sniffs the message for the two transient classes. Anything
    unrecognised stays the terminal base :class:`GrowwError` (conservative — an
    unknown failure is never retried, Golden Rule #1).
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
    """
    name = type(exc).__name__
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

    Retryable: rate-limit + timeout. Terminal: bad-request, not-found, auth
    (needs re-auth), feed (out of scope), and any bare :class:`GrowwError`.
    """
    return isinstance(exc, (GrowwRateLimitError, GrowwTimeoutError))


__all__ = [
    "GrowwAuthError",
    "GrowwBadRequestError",
    "GrowwError",
    "GrowwFeedError",
    "GrowwNotFoundError",
    "GrowwRateLimitError",
    "GrowwTimeoutError",
    "from_groww_exception",
    "is_auth_failure",
    "is_rate_limit",
    "is_retryable",
]
