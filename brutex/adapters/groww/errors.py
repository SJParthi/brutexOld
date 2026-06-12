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
about — the analogue of ``brutex.adapters.dhan.errors`` (which maps HTTP status).
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


def from_groww_exception(exc: Exception) -> GrowwError:
    """Map a ``growwapi`` SDK exception instance to a neutral :class:`GrowwError`.

    Classification is by the exception's class name (string), so no SDK import is
    needed. Any unmapped exception (a bare ``GrowwAPIException`` /
    ``GrowwBaseException`` / non-SDK error) maps to the terminal base
    :class:`GrowwError` — conservative: an unknown failure is never blindly
    retried (Golden Rule #1).
    """
    error_cls = _SDK_NAME_TO_ERROR.get(type(exc).__name__, GrowwError)
    return error_cls(f"{type(exc).__name__}: {exc}")


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
