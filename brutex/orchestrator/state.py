"""Work-item state machine for the 4-phase ingest pipeline.

Implements ``FR-ING-014`` (6-state FSM) and the completeness invariants
from ``§8.6`` and ``§8.7`` of ``docs/REQUIREMENTS.md``.

Every work item — one instrument row, one ``get_expiries`` call, one
``get_contracts`` call, one ``get_historical_candles`` chunk — moves
through this state machine. The goal of the machine is the never-skip
guarantee: **every work item must reach a terminal state** before its
phase is allowed to complete.

The code here is **pure** (no I/O, no network, no threading primitives).
All transitions are functions from ``(old_state, outcome) -> new_state``.
Higher layers (``pipeline.py``, the worker loop, the audit log writer)
glue this machine to the real world.

Why pure?

* Easy to test exhaustively — every transition is a pytest parametrize row
* No hidden state — the caller owns the ``WorkItem`` object and passes
  it back in
* Deterministic — replaying the same sequence of outcomes always yields
  the same final state (required by ``NFR-DET-002``)
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final
from uuid import uuid4

from brutex.models import BruteXError

# ────────────────────────────────────────────────────────────────────────
# States
# ────────────────────────────────────────────────────────────────────────


class WorkItemState(StrEnum):
    """Six states in the ingest work-item FSM.

    Terminal: :attr:`SUCCESS`, :attr:`NO_DATA`, :attr:`QUARANTINED`.
    Non-terminal: :attr:`PENDING`, :attr:`IN_FLIGHT`, :attr:`RETRYING`.

    See ``docs/REQUIREMENTS.md §8.6`` for the full diagram.
    """

    # ── Non-terminal ──────────────────────────────────────────────
    PENDING = "PENDING"  # pragma: no mutate
    IN_FLIGHT = "IN_FLIGHT"  # pragma: no mutate
    RETRYING = "RETRYING"  # pragma: no mutate

    # ── Terminal ──────────────────────────────────────────────────
    SUCCESS = "SUCCESS"  # pragma: no mutate
    NO_DATA = "NO_DATA"  # pragma: no mutate
    QUARANTINED = "QUARANTINED"  # pragma: no mutate


#: Frozen sets used by ``is_terminal`` and ``phase_complete``.
TERMINAL_STATES: Final[frozenset[WorkItemState]] = frozenset(
    {WorkItemState.SUCCESS, WorkItemState.NO_DATA, WorkItemState.QUARANTINED}
)
NON_TERMINAL_STATES: Final[frozenset[WorkItemState]] = frozenset(
    {WorkItemState.PENDING, WorkItemState.IN_FLIGHT, WorkItemState.RETRYING}
)


def is_terminal(state: WorkItemState) -> bool:
    """Return True if *state* is a terminal state (SUCCESS/NO_DATA/QUARANTINED)."""
    return state in TERMINAL_STATES


# ────────────────────────────────────────────────────────────────────────
# Phases
# ────────────────────────────────────────────────────────────────────────


class IngestPhase(StrEnum):
    """The four sequential ingest phases. See ``§8.5``."""

    A_INSTRUMENTS = "A_INSTRUMENTS"  # pragma: no mutate
    B_EXPIRIES = "B_EXPIRIES"  # pragma: no mutate
    C_CONTRACTS = "C_CONTRACTS"  # pragma: no mutate
    D_CANDLES = "D_CANDLES"  # pragma: no mutate


# ────────────────────────────────────────────────────────────────────────
# Outcomes — what the worker reports after calling the Groww API
# ────────────────────────────────────────────────────────────────────────


class Outcome(StrEnum):
    """Outcomes the worker can report to the FSM.

    The FSM translates each outcome + current state into a new state.
    These map onto a Groww API exception hierarchy (see
    the Groww error hierarchy in ``brutex/adapters/groww/errors.py``) plus
    two non-error outcomes for empty/valid responses.
    """

    # ── Success path ──────────────────────────────────────────────
    OK_WITH_DATA = "OK_WITH_DATA"  # pragma: no mutate
    OK_EMPTY = "OK_EMPTY"  # pragma: no mutate

    # ── Transient errors (retry loop) ──────────────────────────────
    RATE_LIMIT = "RATE_LIMIT"  # pragma: no mutate
    TIMEOUT = "TIMEOUT"  # pragma: no mutate
    GENERIC_API_ERROR = "GENERIC_API_ERROR"  # pragma: no mutate

    # ── Permanent errors (quarantine or no-data) ──────────────────
    NOT_FOUND = "NOT_FOUND"  # pragma: no mutate
    NO_DATA = "NO_DATA"  # pragma: no mutate
    BAD_REQUEST = "BAD_REQUEST"  # pragma: no mutate
    AUTH_FAILED = "AUTH_FAILED"  # pragma: no mutate
    AUTHZ_FAILED = "AUTHZ_FAILED"  # pragma: no mutate
    FATAL = "FATAL"  # pragma: no mutate


# ────────────────────────────────────────────────────────────────────────
# Quarantine reason codes (stable strings for audit log + Telegram)
# ────────────────────────────────────────────────────────────────────────


class QuarantineReason(StrEnum):
    """Stable reason codes for why an item is quarantined.

    These appear in the audit log, the Telegram message, and the
    dashboard's quarantine-queue table.
    """

    BAD_REQUEST = "bad_request"  # pragma: no mutate
    AUTH_FAILED = "auth_failed"  # pragma: no mutate
    AUTHZ_FAILED = "authz_failed"  # pragma: no mutate
    RETRY_EXHAUSTED = "retry_exhausted"  # pragma: no mutate
    FATAL_ERROR = "fatal_error"  # pragma: no mutate


# ────────────────────────────────────────────────────────────────────────
# WorkItem — the container that moves through the FSM
# ────────────────────────────────────────────────────────────────────────


#: Maximum number of times an item may enter ``RETRYING`` before it is
#: automatically upgraded to ``QUARANTINED`` with reason
#: ``retry_exhausted``. Configurable per-run but defaults to 10.
MAX_SOFT_RETRIES: Final[int] = 10


@dataclass(frozen=True, slots=True)
class WorkItem:
    """One unit of work in the ingest pipeline.

    Instances are immutable — :func:`transition` returns a new ``WorkItem``
    with the updated state rather than mutating the old one. This makes
    concurrency trivial (no locks needed on reads) and makes the state
    machine easy to test.

    Attributes
    ----------
    item_id
        Stable UUID for correlation in logs and audit chain.
    phase
        Which phase this item belongs to (A/B/C/D).
    content_hash
        sha256 hex digest of ``canonical(params)``. Serves as the primary
        key in the LMDB dedup cache — re-issuing a call with identical
        params yields the same hash and is deduplicated.
    params
        Immutable mapping of the arguments passed to the Groww API method.
        Example for Phase B: ``{"exchange": "NSE", "underlying_symbol":
        "NIFTY", "year": 2024}``.
    state
        Current FSM state.
    retry_count
        How many times this item has entered ``RETRYING`` so far.
    quarantine_reason
        Set when ``state == QUARANTINED``; None otherwise.
    resolved_by_human
        Only meaningful for quarantined items. ``True`` once the operator
        has reviewed and re-queued / marked the item. The phase-complete
        verifier requires this flag for every quarantined item.
    created_at
        When the item was first enqueued.
    last_transition_at
        When the last state transition happened. Used by the dashboard's
        worker activity table and the watchdog (``NFR-REL-005``).
    """

    item_id: str
    phase: IngestPhase
    content_hash: str  # sha256 hex (64 chars)
    params: tuple[tuple[str, str], ...]  # canonical sorted key/value pairs
    state: WorkItemState = WorkItemState.PENDING
    retry_count: int = 0
    quarantine_reason: QuarantineReason | None = None
    resolved_by_human: bool = False
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_transition_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @staticmethod
    def new(
        *,
        phase: IngestPhase,
        content_hash: str,
        params: dict[str, str],
    ) -> WorkItem:
        """Construct a fresh ``PENDING`` work item."""
        canonical = tuple(sorted(params.items()))
        return WorkItem(
            item_id=str(uuid4()),
            phase=phase,
            content_hash=content_hash,
            params=canonical,
            state=WorkItemState.PENDING,
        )


# ────────────────────────────────────────────────────────────────────────
# Exceptions
# ────────────────────────────────────────────────────────────────────────


class TransitionError(BruteXError):
    """Raised when a state transition is illegal given the current state.

    Example: trying to transition a ``SUCCESS`` item to ``IN_FLIGHT``.
    This indicates a bug in the caller — terminal states are, well,
    terminal.
    """


class NonTerminalStateError(BruteXError):
    """Raised when ``phase_complete`` is called on a queue containing
    any item in ``{PENDING, IN_FLIGHT, RETRYING}``.

    The phase is not actually complete; the caller should not have
    claimed otherwise.
    """


class IncompletePhaseError(BruteXError):
    """Raised when ``phase_complete`` finds unresolved quarantines.

    Every quarantined item must have ``resolved_by_human == True``
    before its phase can advance to the next one.
    """


# ────────────────────────────────────────────────────────────────────────
# Core transition function — pure
# ────────────────────────────────────────────────────────────────────────


def transition(
    item: WorkItem,
    outcome: Outcome,
    *,
    max_soft_retries: int = MAX_SOFT_RETRIES,
) -> WorkItem:
    """Compute the next state of *item* given the worker's reported *outcome*.

    This is the core of ``FR-ING-014``. It is pure — no I/O, no clocks
    except the default ``last_transition_at`` stamp. Caller is
    responsible for persisting the returned item and writing an audit
    log entry.

    The ``from PENDING`` and ``from RETRYING`` branches both assume the
    worker has just finished its call. The ``from IN_FLIGHT`` branch is
    the main business logic.

    Parameters
    ----------
    item
        The work item (must be in a non-terminal state; ``TransitionError``
        is raised otherwise).
    outcome
        What the worker reported after calling the Groww API.
    max_soft_retries
        Override the default retry-escalation threshold (default
        :data:`MAX_SOFT_RETRIES` = 10). Lower values are useful in tests.

    Returns
    -------
    WorkItem
        A new immutable ``WorkItem`` with ``state`` updated and
        ``last_transition_at`` refreshed. ``retry_count`` is incremented
        when entering ``RETRYING``.

    Raises
    ------
    TransitionError
        If *item* is already in a terminal state.
    """
    if is_terminal(item.state):
        raise TransitionError(
            f"{item.item_id}: cannot transition from terminal state {item.state.value}"
        )

    now = datetime.now(UTC)

    # Normalise: PENDING -> IN_FLIGHT happens separately (dispatch).
    # This function handles IN_FLIGHT / RETRYING -> terminal or RETRYING.
    if item.state == WorkItemState.PENDING:
        # The caller should have dispatched the item to IN_FLIGHT first.
        raise TransitionError(
            f"{item.item_id}: cannot apply outcome {outcome.value} to PENDING item; "
            f"call dispatch() first to move it to IN_FLIGHT."
        )

    # ── Success path ──────────────────────────────────────────────
    if outcome == Outcome.OK_WITH_DATA:
        return replace(
            item,
            state=WorkItemState.SUCCESS,
            last_transition_at=now,
        )

    if outcome == Outcome.OK_EMPTY:
        return replace(
            item,
            state=WorkItemState.NO_DATA,
            last_transition_at=now,
        )

    # ── NOT_FOUND is a terminal NO_DATA (Groww says "no such thing":
    # HTTP 404 → GrowwNotFoundError) ─
    if outcome == Outcome.NOT_FOUND:
        return replace(
            item,
            state=WorkItemState.NO_DATA,
            last_transition_at=now,
        )

    # ── NO_DATA is a terminal NO_DATA (Groww empty-candle response — no
    # bars for this specific query window; advance watermark, do not retry).
    if outcome == Outcome.NO_DATA:
        return replace(
            item,
            state=WorkItemState.NO_DATA,
            last_transition_at=now,
        )

    # ── FATAL: hard-fail terminal quarantine (Groww terminal client errors,
    # e.g. GrowwBadRequestError / GrowwAuthError).
    # Pipeline-level halt logic outside transition() reads this state and
    # marks the entire phase HALTED on its next scan.
    if outcome == Outcome.FATAL:
        return replace(
            item,
            state=WorkItemState.QUARANTINED,
            quarantine_reason=QuarantineReason.FATAL_ERROR,
            last_transition_at=now,
        )

    # ── Auth / bad-request outcomes → QUARANTINED ──────────────────
    if outcome == Outcome.BAD_REQUEST:
        return replace(
            item,
            state=WorkItemState.QUARANTINED,
            quarantine_reason=QuarantineReason.BAD_REQUEST,
            last_transition_at=now,
        )
    if outcome == Outcome.AUTH_FAILED:
        return replace(
            item,
            state=WorkItemState.QUARANTINED,
            quarantine_reason=QuarantineReason.AUTH_FAILED,
            last_transition_at=now,
        )
    if outcome == Outcome.AUTHZ_FAILED:
        return replace(
            item,
            state=WorkItemState.QUARANTINED,
            quarantine_reason=QuarantineReason.AUTHZ_FAILED,
            last_transition_at=now,
        )

    # ── Transient outcomes → RETRYING or QUARANTINED on exhaustion ─
    if outcome in {Outcome.RATE_LIMIT, Outcome.TIMEOUT, Outcome.GENERIC_API_ERROR}:
        new_retry_count = item.retry_count + 1
        if new_retry_count >= max_soft_retries:
            return replace(
                item,
                state=WorkItemState.QUARANTINED,
                quarantine_reason=QuarantineReason.RETRY_EXHAUSTED,
                retry_count=new_retry_count,
                last_transition_at=now,
            )
        return replace(
            item,
            state=WorkItemState.RETRYING,
            retry_count=new_retry_count,
            last_transition_at=now,
        )

    # Exhaustive: every Outcome must be handled above.
    raise TransitionError(f"{item.item_id}: unhandled outcome {outcome.value}")


def dispatch(item: WorkItem) -> WorkItem:
    """Move a PENDING or RETRYING item into IN_FLIGHT.

    Called by the worker pool when a worker pulls the item off the queue.
    Does not touch ``retry_count`` — that's bumped by :func:`transition`.
    """
    if item.state not in {WorkItemState.PENDING, WorkItemState.RETRYING}:
        raise TransitionError(
            f"{item.item_id}: can only dispatch from PENDING or RETRYING, not {item.state.value}"
        )
    return replace(
        item,
        state=WorkItemState.IN_FLIGHT,
        last_transition_at=datetime.now(UTC),
    )


def resolve_quarantine(
    item: WorkItem,
    *,
    as_state: WorkItemState,
    reason_override: str | None = None,
) -> WorkItem:
    """Operator action — resolve a QUARANTINED item manually.

    *as_state* must be a terminal state (typically ``SUCCESS`` if the
    operator retried and it worked, or ``NO_DATA`` if the operator
    confirmed there's genuinely nothing to fetch).

    Parameters
    ----------
    item
        The quarantined item.
    as_state
        The terminal state to flip it to (``SUCCESS`` or ``NO_DATA``).
    reason_override
        Optional human-supplied reason string (appended to audit log).

    Raises
    ------
    TransitionError
        If *item* is not in ``QUARANTINED`` state, or *as_state* is
        not terminal, or *as_state* is ``QUARANTINED`` itself.
    """
    if item.state != WorkItemState.QUARANTINED:
        raise TransitionError(
            f"{item.item_id}: resolve_quarantine called on non-quarantined item "
            f"(state={item.state.value})"
        )
    if as_state not in {WorkItemState.SUCCESS, WorkItemState.NO_DATA}:
        raise TransitionError(
            f"{item.item_id}: can only resolve to SUCCESS or NO_DATA, not {as_state.value}"
        )
    del reason_override  # reserved for future audit-log enrichment
    return replace(
        item,
        state=as_state,
        resolved_by_human=True,
        last_transition_at=datetime.now(UTC),
    )


# ────────────────────────────────────────────────────────────────────────
# Completeness invariant — §8.7
# ────────────────────────────────────────────────────────────────────────


def phase_complete(items: list[WorkItem]) -> bool:
    """Return True iff every item is in a terminal state AND every
    quarantined item has been resolved by a human.

    This is the formal invariant from ``§8.7``:

    ::

        phase_complete(P) ≡
            (∀ item: item.state ∈ {SUCCESS, NO_DATA, QUARANTINED})
            ∧ (∀ item s.t. state == QUARANTINED: resolved_by_human == True)
            ∧ (no item in {PENDING, IN_FLIGHT, RETRYING})

    Raises
    ------
    NonTerminalStateError
        If any item is still in a non-terminal state.
    IncompletePhaseError
        If any quarantined item is unresolved.

    Returns
    -------
    bool
        ``True`` on success. Callers can either inspect the return value
        or let the exceptions propagate — both are valid usage patterns.
    """
    for item in items:
        if item.state in NON_TERMINAL_STATES:
            raise NonTerminalStateError(
                f"{item.item_id}: still in non-terminal state {item.state.value}"
            )
    for item in items:
        if item.state == WorkItemState.QUARANTINED and not item.resolved_by_human:
            raise IncompletePhaseError(
                f"{item.item_id}: quarantined with reason "
                f"{item.quarantine_reason.value if item.quarantine_reason else 'unknown'} "
                f"and not yet resolved"
            )
    return True


__all__ = [
    "MAX_SOFT_RETRIES",
    "NON_TERMINAL_STATES",
    "TERMINAL_STATES",
    "IncompletePhaseError",
    "IngestPhase",
    "NonTerminalStateError",
    "Outcome",
    "QuarantineReason",
    "TransitionError",
    "WorkItem",
    "WorkItemState",
    "dispatch",
    "is_terminal",
    "phase_complete",
    "resolve_quarantine",
    "transition",
]
