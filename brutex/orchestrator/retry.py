"""Retry classifier — map broker SDK exception types to :class:`Outcome`.

Implements the ``FR-ING-008`` (refined) classification table from
``docs/REQUIREMENTS.md §8.6`` and the Dhan API error catalog.

The classifier is a **pure function** over exception types and
exception-class names (strings). It deliberately does NOT import
`a broker SDK` at module load time — this keeps unit tests running
without the real SDK installed. Classification works on either:

* A real exception instance (``isinstance`` dispatch)
* A class name string (e.g., ``"VendorAPIRateLimitException"``)
  — used by tests and by the golden-master integration test

Both paths return the same :class:`Outcome` value for the same
exception, so the classifier is drift-proof.

Usage from the worker loop::

    from brutex.orchestrator.retry import classify_exception, backoff_delay
    from brutex.orchestrator.state import Outcome, transition, WorkItemState

    try:
        response = Dhan.get_historical_candles(...)
    except Exception as e:  # noqa: BLE001 — intentional catch-all
        outcome = classify_exception(e)
    else:
        outcome = Outcome.OK_WITH_DATA if response["candles"] else Outcome.OK_EMPTY

    new_item = transition(item, outcome)
    if new_item.state == WorkItemState.RETRYING:
        delay = backoff_delay(new_item.retry_count)
        time.sleep(delay)
"""

from __future__ import annotations

import random
from typing import Final

from brutex.models import BruteXError
from brutex.orchestrator.state import Outcome

# ────────────────────────────────────────────────────────────────────────
# Exception-name → Outcome table
# ────────────────────────────────────────────────────────────────────────

#: Mapping from Dhan API exception class names (as reported by
#: ``type(exc).__name__``) to the :class:`Outcome` the FSM should see.
#:
#: See the Dhan API error catalog for the source of truth.
#: If Dhan renames or adds an exception, update this table AND the
#: ``tests/golden/test_vendor_sdk_constants_frozen.py`` golden-master
#: in the same commit.
BROKER_EXCEPTION_CLASSIFICATION: Final[dict[str, Outcome]] = {
    # Rate-limit & transient — loop through RETRYING
    "VendorAPIRateLimitException": Outcome.RATE_LIMIT,
    "VendorAPITimeoutException": Outcome.TIMEOUT,
    # Generic API / base — treat as unknown transient, retry once
    "VendorAPIException": Outcome.GENERIC_API_ERROR,
    "VendorBaseException": Outcome.GENERIC_API_ERROR,
    # Permanent "no such resource" — recorded as NO_DATA (not skipped)
    "VendorAPINotFoundException": Outcome.NOT_FOUND,
    # Permanent client error — QUARANTINE (auth halt, bad-request halt)
    "VendorAPIBadRequestException": Outcome.BAD_REQUEST,
    "VendorAPIAuthenticationException": Outcome.AUTH_FAILED,
    "VendorAPIAuthorisationException": Outcome.AUTHZ_FAILED,  # British spelling
    # Feed-specific (v1.2+); stay on the generic path for now
    "VendorFeedException": Outcome.GENERIC_API_ERROR,
    "VendorFeedConnectionException": Outcome.GENERIC_API_ERROR,
    "VendorFeedNotSubscribedException": Outcome.GENERIC_API_ERROR,
}

#: Class names that MUST be present on ``the Dhan adapter's exception hierarchy``.
#: Used by the golden-master test to catch upstream renames. Same as
#: :data:`the deleted broker adapter`
#: but duplicated here so the retry module is self-contained.
REQUIRED_EXCEPTION_NAMES: Final[frozenset[str]] = frozenset(BROKER_EXCEPTION_CLASSIFICATION.keys())


class UnknownExceptionError(BruteXError):
    """Raised when :func:`classify_exception_name` receives a name it
    does not recognise.

    This is treated as a bug in the caller, not a runtime condition —
    the set of Dhan API exception names is closed and any new one MUST be
    added to :data:`BROKER_EXCEPTION_CLASSIFICATION` before the code that
    raises it can be merged.
    """


def classify_exception_name(class_name: str) -> Outcome:
    """Return the :class:`Outcome` for a Dhan API exception class name.

    Parameters
    ----------
    class_name
        The ``__name__`` of the exception class, e.g.
        ``"VendorAPIRateLimitException"``.

    Raises
    ------
    UnknownExceptionError
        If *class_name* is not in :data:`BROKER_EXCEPTION_CLASSIFICATION`.
        Forces explicit handling of every new exception type.

    Returns
    -------
    Outcome
        The outcome the FSM should consume.
    """
    try:
        return BROKER_EXCEPTION_CLASSIFICATION[class_name]
    except KeyError as exc:
        raise UnknownExceptionError(
            f"Unknown Dhan API exception class: {class_name!r}. "
            f"Add it to BROKER_EXCEPTION_CLASSIFICATION in brutex/orchestrator/retry.py "
            f"and re-run tests/unit/test_orchestrator_retry.py."
        ) from exc


def classify_exception(exc: BaseException) -> Outcome:
    """Classify a live exception instance.

    Walks the exception's MRO (method resolution order) and picks the
    most-specific match in :data:`BROKER_EXCEPTION_CLASSIFICATION`. This
    correctly handles subclass relationships — e.g., a hypothetical
    ``VendorAPIRateLimitBurstException(VendorAPIRateLimitException)``
    would classify as ``RATE_LIMIT`` without needing its own table entry.

    Parameters
    ----------
    exc
        The caught exception.

    Raises
    ------
    UnknownExceptionError
        If none of the exception's MRO entries match. This indicates
        either a non-Dhan API exception reaching this classifier (caller
        bug — should catch only Dhan API exceptions) or a new exception type
        exception type that needs an entry.

    Returns
    -------
    Outcome
    """
    for cls in type(exc).__mro__:
        if cls.__name__ in BROKER_EXCEPTION_CLASSIFICATION:
            return BROKER_EXCEPTION_CLASSIFICATION[cls.__name__]
    # No match anywhere in the MRO — this is a bug
    raise UnknownExceptionError(
        f"Exception {type(exc).__name__!r} (MRO: "
        f"{[c.__name__ for c in type(exc).__mro__]}) does not match "
        f"any entry in BROKER_EXCEPTION_CLASSIFICATION. "
        f"This exception should not have reached the retry classifier — "
        f"verify the adapter only catches Dhan API exceptions."
    )


# ────────────────────────────────────────────────────────────────────────
# Exponential backoff schedule — §8.6 / FR-ING-008
# ────────────────────────────────────────────────────────────────────────

#: Exponential backoff ladder in seconds. Matches the spec in
#: ``REQUIREMENTS.md FR-ING-008`` (refined): 2s → 4s → 8s → 16s →
#: 32s → 60s → 120s → 240s → 480s → 900s. Ten slots — the 11th
#: entry into ``RETRYING`` escalates to ``QUARANTINED`` with reason
#: ``retry_exhausted`` (handled in :func:`brutex.orchestrator.state.transition`).
BACKOFF_SCHEDULE_SECONDS: Final[tuple[float, ...]] = (
    2.0,
    4.0,
    8.0,
    16.0,
    32.0,
    60.0,
    120.0,
    240.0,
    480.0,
    900.0,
)

#: Fraction used for random jitter (±20% of the scheduled delay).
JITTER_FRACTION: Final[float] = 0.2


def backoff_delay(
    retry_count: int,
    *,
    schedule: tuple[float, ...] = BACKOFF_SCHEDULE_SECONDS,
    jitter_fraction: float = JITTER_FRACTION,
    rng: random.Random | None = None,
) -> float:
    """Compute the wall-clock delay (in seconds) for a given retry count.

    Deterministic when *rng* is provided (used by tests and reproducibility
    guarantees). Non-deterministic when *rng* is ``None`` (production).

    Parameters
    ----------
    retry_count
        Number of times the item has already entered ``RETRYING`` —
        the value after :func:`brutex.orchestrator.state.transition`
        has incremented it. Callers pass ``item.retry_count`` directly.
    schedule
        Base delays in seconds. Overridable for tests.
    jitter_fraction
        Fraction of the base delay to randomly add or subtract, e.g.
        ``0.2`` = ±20%. Set to ``0.0`` for deterministic behavior.
    rng
        RNG instance for jitter. Pass a seeded ``random.Random(seed)``
        for reproducibility in tests.

    Returns
    -------
    float
        Non-negative wall-clock delay in seconds. At ``retry_count ≥
        len(schedule)`` the last slot is clamped (the state machine
        escalates to QUARANTINED separately — this function just
        returns a sensible fallback).
    """
    if retry_count <= 0:
        raise ValueError(f"retry_count must be >= 1, got {retry_count}")

    # Clamp to the last slot if we're past the schedule length. The
    # FSM should have escalated to QUARANTINED by now, but belt-and-
    # braces.
    idx = min(retry_count - 1, len(schedule) - 1)
    base = schedule[idx]

    if jitter_fraction == 0.0:
        return base

    if rng is not None:
        jitter = rng.uniform(-jitter_fraction, jitter_fraction) * base
    else:
        jitter = random.uniform(-jitter_fraction, jitter_fraction) * base  # noqa: S311  # nosec B311 — backoff jitter, non-crypto
    delay = base + jitter
    return max(0.0, delay)


# ────────────────────────────────────────────────────────────────────────
# High-level classification summary — used by dashboards / audit log
# ────────────────────────────────────────────────────────────────────────


def outcome_severity(outcome: Outcome) -> str:
    """Return a one-word severity label for the dashboard and Telegram.

    ``info``   — happy path or expected terminal (OK, NO_DATA via NotFound)
    ``warn``   — retryable transient (rate limit, timeout, generic)
    ``error``  — halts the phase (auth, authz, bad request)
    """
    if outcome in {Outcome.OK_WITH_DATA, Outcome.OK_EMPTY, Outcome.NOT_FOUND}:
        return "info"
    if outcome in {Outcome.RATE_LIMIT, Outcome.TIMEOUT, Outcome.GENERIC_API_ERROR}:
        return "warn"
    # BAD_REQUEST, AUTH_FAILED, AUTHZ_FAILED
    return "error"


__all__ = [
    "BACKOFF_SCHEDULE_SECONDS",
    "BROKER_EXCEPTION_CLASSIFICATION",
    "JITTER_FRACTION",
    "REQUIRED_EXCEPTION_NAMES",
    "UnknownExceptionError",
    "backoff_delay",
    "classify_exception",
    "classify_exception_name",
    "outcome_severity",
]
