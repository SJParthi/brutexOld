"""Dhan client error model — neutral exceptions + retry classification.

DEC-DHAN-SPOT-PULL-001 (ADR-0022). Unlike Groww's typed SDK exception tree, the
``dhanhq`` SDK's ``dhan_http.py`` returns a FLAT dict on failure — a single broad
``except Exception`` maps every non-2xx to
``{"status": "failure", "remarks": {"errorCode", "errorType", "errorMessage"}}``
(VERIFIED-live: SDK ``dhan_http.py``). There are NO typed exceptions, NO 429/401
branches, NO retry/backoff — BruteX owns all of that.

This module maps that flat envelope into a small neutral hierarchy the rest of
BruteX reasons about (the analogue of Groww's ``from_groww_exception``):

* :func:`classify_dhan_failure` — a ``remarks`` dict → the neutral
  :class:`DhanError` subclass, by ``errorCode`` (DH-901 / DH-905) then a
  conservative throttle heuristic (the exact throttle ``errorCode`` is UNVERIFIED
  — the annexure is host-blocked — so it is a HEURISTIC over ``errorType`` /
  message until a probe confirms it; Golden Rule #1).
* :func:`from_dhan_exception` — a stdlib exception (a freeze-guard
  :class:`TimeoutError` on a hung SSM re-read, a decode error) → the neutral
  retryable class.

:func:`is_retryable` is the single source of truth the retry policy consults
(CLAUDE.md §11 COMMON). :class:`DhanError` is a :class:`~brutex.models.BruteXError`
(hence a :class:`BaseException` subclass), so it satisfies the
:class:`~brutex.feeds.binding.FeedErrorPolicy` ``terminal_base`` contract and the
ingest spine's per-symbol quarantine gate.
"""

from __future__ import annotations

from collections.abc import Mapping

from brutex.adapters.dhan.constants import (
    DHAN_ERR_AUTHORIZATION,
    DHAN_ERR_INVALID_AUTH,
)
from brutex.models import BruteXError


class DhanError(BruteXError):
    """Base class for every Dhan adapter failure (terminal unless a subclass)."""


class DhanAuthError(DhanError):
    """Authentication failure — DH-901 ``Invalid_Authentication`` (triggers a RE-READ).

    A bad / expired 24-h JWT. On a read-only host BruteX NEVER mints — the reauth
    path RE-READS the shared SSM SecureString to adopt the minter's fresh token
    (:class:`~brutex.adapters.dhan.reauth.DhanSdkRefresher`), never a local mint.
    A NON-retryable :class:`DhanError` at the client layer (it drives a re-read,
    not a blind fetch retry); the retryable :class:`DhanTokenStillStaleError` is
    raised only when the re-read still holds the dead token.
    """


class DhanSubscriptionError(DhanError):
    """DH-905 authorization / subscription / missing-field class — TERMINAL.

    Dhan's historical/intraday endpoints require an ACTIVE Data API subscription;
    without it the call returns DH-905 (a ``403``-class). NOT retryable — the fix is
    to activate the subscription, not to re-issue the request. Rides the per-symbol
    quarantine (never a fabricated bar).
    """


class DhanThrottleError(DhanError):
    """Rate-limit / throttle — RETRYABLE (the limiter should back off).

    Dhan caps the Data API at 5 req/s + 100k/day with NO typed 429 (the SDK returns
    a flat failure dict). The exact throttle ``errorCode`` is UNVERIFIED (the
    annexure is host-blocked), so :func:`classify_dhan_failure` recognises it by a
    CONSERVATIVE ``errorType`` / message heuristic until a probe confirms it
    (Golden Rule #1). Feeds the AIMD ladder (:func:`is_rate_limit` is ``True``).
    """


class DhanEmptyResponseError(DhanError):
    """Empty / non-JSON vendor body under a transient throttle — RETRYABLE.

    A blank / garbled body decodes to nothing; treated as a transient blip (the
    in-call fast-retry re-issues it; a residual is re-pulled by the until-clean
    loop). NOT a quota signal, so it rides the gentle timeout ladder, never stepping
    the per-second rate ceiling down.
    """


class DhanTimeoutError(DhanError):
    """Network read/connect timeout — RETRYABLE (the freeze-guard bound).

    The ``dhanhq`` SDK carries no inherent wall-clock bound, so a hung SSM re-read
    or a stalled transport is bounded by :func:`~brutex.config.deadline.run_with_deadline`;
    the resulting stdlib :class:`TimeoutError` is mapped here (:func:`from_dhan_exception`)
    so a stall is re-pulled, never a permanent freeze. Rides the timeout ladder, not
    the rate ladder (a stall is not a quota signal).
    """


class DhanVendorDriftError(DhanError):
    """Response-shape drift — parallel column arrays of UNEQUAL length — TERMINAL-per-chunk.

    Dhan returns OHLCV as parallel COLUMN arrays (``open[] high[] low[] close[]
    volume[] timestamp[]``) and the SDK does ZERO validation. A length mismatch
    means the vendor payload is internally inconsistent — zipping it would silently
    mis-pair prices with timestamps (a Golden-Rule-#1 corruption), so the chunk is
    REJECTED (never partially stored) and re-pulled. NOT a per-account failure, so
    it rides the per-window quarantine, not a pull-wide halt.
    """


class DhanTokenReadOnlyError(DhanAuthError):
    """A token WRITE was attempted while the read-only Dhan token store is active.

    BruteX is a strictly READ-ONLY consumer of the shared Dhan access token (a
    dedicated Lambda / TickVault mints it into AWS SSM). A ``store`` call means the
    caller tried to MINT — forbidden on a read-only host (a stray write could
    invalidate the token TickVault is actively using). This fails LOUD (a terminal
    :class:`DhanAuthError`) instead of a silent local write; the operator fix is to
    deploy / run the minter.
    """


class DhanTokenStillStaleError(DhanError):
    """A read-only RE-READ returned the SAME dead token (or an SSM miss) — RETRYABLE.

    On a mid-run DH-901 the reauth path RE-READS the shared SSM token to adopt the
    minter's fresh one. If the re-read returns the SAME token that just failed auth
    — the minter has not rotated it yet — or an SSM miss, adopting it would loop the
    dead token; this is raised INSTEAD so the window quarantines + re-pulls LATER
    (when the fresh token lands), never an infinite loop, never a fail-loud restart.
    A read that returns a NEW token is adopted with no error, so this never fires on
    the healthy rotation path.
    """


#: Message / ``errorType`` PHRASES (lower-cased) that mark a RETRYABLE throttle when
#: the SDK returns a failure with no recognised ``errorCode``. The exact Dhan
#: throttle ``errorCode`` is UNVERIFIED (the annexure is host-blocked), so this is a
#: CONSERVATIVE heuristic (Golden Rule #1) — narrow phrase-only, never a bare number.
_THROTTLE_HINTS: tuple[str, ...] = (
    "rate limit",
    "rate-limit",
    "too many request",
    "throttle",
    "rate_limit",
)

#: ``errorType`` / message signatures for an empty / non-JSON body (a transient blip).
_EMPTY_BODY_HINTS: tuple[str, ...] = (
    "empty response",
    "no data",
    "expecting value",
)


def _remark_text(remarks: Mapping[str, object]) -> str:
    """Lower-cased join of ``errorType`` + ``errorMessage`` for phrase matching."""
    error_type = str(remarks.get("errorType", ""))
    error_message = str(remarks.get("errorMessage", ""))
    return f"{error_type} {error_message}".casefold()


def classify_dhan_failure(remarks: Mapping[str, object]) -> DhanError:
    """Map a Dhan ``remarks`` failure dict to a neutral :class:`DhanError`.

    Classification is by ``errorCode`` FIRST (the reliable structured signal —
    DH-901 → auth, DH-905 → subscription), then a CONSERVATIVE throttle / empty-body
    heuristic over the ``errorType`` / ``errorMessage`` text (the throttle code is
    UNVERIFIED). Anything unrecognised stays the terminal base :class:`DhanError`
    (an unknown failure is NEVER blindly retried — Golden Rule #1).

    Parameters
    ----------
    remarks
        The ``remarks`` sub-dict of the SDK failure envelope
        (``{"errorCode", "errorType", "errorMessage"}``); missing keys tolerated.
    """
    code = str(remarks.get("errorCode", "")).strip().upper()
    message = str(remarks.get("errorMessage", remarks.get("errorType", "")))
    if code == DHAN_ERR_INVALID_AUTH:
        return DhanAuthError(f"{DHAN_ERR_INVALID_AUTH}: {message}")
    if code == DHAN_ERR_AUTHORIZATION:
        return DhanSubscriptionError(f"{DHAN_ERR_AUTHORIZATION}: {message}")
    text = _remark_text(remarks)
    if any(hint in text for hint in _THROTTLE_HINTS):
        return DhanThrottleError(f"{code or 'throttle'}: {message}")
    if any(hint in text for hint in _EMPTY_BODY_HINTS):
        return DhanEmptyResponseError(f"{code or 'empty'}: {message}")
    return DhanError(f"{code or 'DH-UNKNOWN'}: {message}")


def from_dhan_exception(exc: BaseException) -> DhanError:
    """Map a stdlib transport exception to a neutral retryable :class:`DhanError`.

    Used at the freeze-guard boundary: a hung SSM re-read / transport stall raises a
    stdlib :class:`TimeoutError` bounded by
    :func:`~brutex.config.deadline.run_with_deadline`, which is mapped to a RETRYABLE
    :class:`DhanTimeoutError` (a stall is transient — re-pull, never a permanent
    freeze). Any other exception maps to the terminal base :class:`DhanError`
    (conservative — never blindly retried; Golden Rule #1).
    """
    if isinstance(exc, TimeoutError):
        return DhanTimeoutError(f"{type(exc).__name__}: {exc}")
    return DhanError(f"{type(exc).__name__}: {exc}")


def is_rate_limit(exc: DhanError) -> bool:
    """True if *exc* is a rate-limit / throttle error (the limiter should back off)."""
    return isinstance(exc, DhanThrottleError)


def is_retryable(exc: DhanError) -> bool:
    """True if *exc* is transient and the orchestrator should retry it.

    Retryable: throttle (:class:`DhanThrottleError`), empty/non-JSON body
    (:class:`DhanEmptyResponseError`), timeout (:class:`DhanTimeoutError`), and a
    read-only re-read that still holds the SAME dead token
    (:class:`DhanTokenStillStaleError` — re-pull later when the minter rotates).
    Terminal: DH-901 auth (needs a re-read, not a blind retry — incl. the read-only
    :class:`DhanTokenReadOnlyError`), DH-905 subscription, vendor drift, and any bare
    :class:`DhanError` (an unknown failure is never retried — Golden Rule #1).
    """
    return isinstance(
        exc,
        (
            DhanThrottleError,
            DhanEmptyResponseError,
            DhanTimeoutError,
            DhanTokenStillStaleError,
        ),
    )


def is_auth_failure(exc: DhanError) -> bool:
    """True if *exc* means Dhan auth failed (drives a re-read, not a blind retry)."""
    return isinstance(exc, DhanAuthError)


def steps_timeout_ladder(exc: DhanError) -> bool:
    """True iff *exc* may step the AIMD TIMEOUT ladder down — a server-stress signal only.

    A retryable NON-rate-limit, NON-auth-family transport failure (timeout / empty
    body / vendor stall) is server-stress evidence and may ease the pull rate; a
    rate-limit keeps its own ``on_rate_limit`` path, and an auth-family stall (a
    token race) says nothing about candle-endpoint health, so it must NOT ease the
    rate (the Groww ``steps_timeout_ladder`` precedent, CLAUDE.md §11 COMMON).
    """
    return is_retryable(exc) and not is_rate_limit(exc) and not is_auth_failure(exc)


__all__ = [
    "DhanAuthError",
    "DhanEmptyResponseError",
    "DhanError",
    "DhanSubscriptionError",
    "DhanThrottleError",
    "DhanTimeoutError",
    "DhanTokenReadOnlyError",
    "DhanTokenStillStaleError",
    "DhanVendorDriftError",
    "classify_dhan_failure",
    "from_dhan_exception",
    "is_auth_failure",
    "is_rate_limit",
    "is_retryable",
    "steps_timeout_ladder",
]
