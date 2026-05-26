"""Property-based invariants for the work-item FSM.

These tests use ``hypothesis`` to exhaustively explore random sequences
of :class:`Outcome` values applied to a single :class:`WorkItem` and
assert that the fundamental invariants from ``docs/REQUIREMENTS.md
§8.6`` and ``§8.7`` hold for every possible sequence.

## Invariants checked

1. **Never-skip (NFR-REL-006).** Starting from PENDING, any non-empty
   sequence of outcomes must eventually drive the item to a *terminal*
   state (SUCCESS / NO_DATA / QUARANTINED). The item is never lost,
   never returned to PENDING silently, never left in a limbo state.

2. **Terminal absorbs.** Once an item is terminal, any further
   :func:`transition` call raises :class:`TransitionError`. The FSM
   does not allow "undoing" a terminal state except through the
   explicit :func:`resolve_quarantine` escape hatch.

3. **Retry monotonicity.** ``retry_count`` is non-decreasing across
   every transition. It only moves forward.

4. **Retry exhaustion → quarantine.** If we feed the item
   ``max_soft_retries`` consecutive transient outcomes, it must
   land in QUARANTINED with reason ``RETRY_EXHAUSTED``.

5. **Quarantine reason consistency.** Whenever state == QUARANTINED,
   quarantine_reason is non-None. Whenever state != QUARANTINED,
   quarantine_reason is None (except via
   :func:`resolve_quarantine` which sets
   ``resolved_by_human=True``).

6. **Dispatch legality.** :func:`dispatch` only succeeds from PENDING
   or RETRYING. Any other state must raise.

7. **Params canonicalisation.** Two :class:`WorkItem` constructed from
   equivalent ``params`` dicts have identical canonicalised params
   tuples (sorted by key).

Every test uses bounded Hypothesis strategies to stay deterministic
and fast (< 1 second per file).
"""

from __future__ import annotations

import hashlib

import pytest
from brutex.orchestrator.state import (
    MAX_SOFT_RETRIES,
    IngestPhase,
    Outcome,
    QuarantineReason,
    TransitionError,
    WorkItem,
    WorkItemState,
    dispatch,
    is_terminal,
    phase_complete,
    transition,
)
from hypothesis import given, settings
from hypothesis import strategies as st

# ────────────────────────────────────────────────────────────────────────
# Strategies
# ────────────────────────────────────────────────────────────────────────


_ALL_OUTCOMES: list[Outcome] = list(Outcome)
_ALL_PHASES: list[IngestPhase] = list(IngestPhase)
_ALL_STATES: list[WorkItemState] = list(WorkItemState)


#: A hypothesis strategy yielding any single Outcome.
outcome_st = st.sampled_from(_ALL_OUTCOMES)


#: A hypothesis strategy yielding a bounded sequence of outcomes.
#: Bounded at 30 to keep per-example time predictable.
outcome_sequence_st = st.lists(outcome_st, min_size=1, max_size=30)


#: A strategy yielding only the transient outcomes (retry-triggering).
transient_outcome_st = st.sampled_from(
    [Outcome.RATE_LIMIT, Outcome.TIMEOUT, Outcome.GENERIC_API_ERROR]
)


#: A strategy yielding a fresh PENDING WorkItem with deterministic hash.
def _fresh_item_strategy() -> st.SearchStrategy[WorkItem]:
    phase_st = st.sampled_from(_ALL_PHASES)
    key_st = st.text(
        alphabet=st.characters(min_codepoint=0x61, max_codepoint=0x7A),
        min_size=1,
        max_size=8,
    )
    val_st = st.text(
        alphabet=st.characters(min_codepoint=0x30, max_codepoint=0x39),
        min_size=1,
        max_size=5,
    )
    params_st = st.dictionaries(keys=key_st, values=val_st, max_size=4)

    def _build(phase: IngestPhase, params: dict[str, str]) -> WorkItem:
        canonical = repr(sorted(params.items())).encode()
        content_hash = hashlib.sha256(canonical).hexdigest()
        return WorkItem.new(
            phase=phase,
            content_hash=content_hash,
            params=params,
        )

    return st.builds(_build, phase_st, params_st)


fresh_item_st = _fresh_item_strategy()


# ────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────


def _drive_once(item: WorkItem, outcome: Outcome) -> WorkItem:
    """Run one IN_FLIGHT cycle on *item* and apply *outcome*.

    Real workers call ``dispatch`` to move the item from PENDING (or
    RETRYING) to IN_FLIGHT, then invoke ``transition`` with whatever
    outcome the Groww API call produced. This helper composes the two so
    the tests can build long transition chains without repeating
    the boilerplate.
    """
    if is_terminal(item.state):
        raise TransitionError(f"cannot drive terminal item (state={item.state})")
    in_flight = dispatch(item)
    return transition(in_flight, outcome)


def _drive_sequence(
    item: WorkItem,
    outcomes: list[Outcome],
    *,
    max_soft_retries: int = MAX_SOFT_RETRIES,
) -> tuple[WorkItem, int]:
    """Apply outcomes in order, stopping when the item becomes terminal.

    Returns the final item and the number of outcomes actually consumed
    (may be less than ``len(outcomes)`` if the item reached a terminal
    state early).
    """
    current = item
    consumed = 0
    for outcome in outcomes:
        if is_terminal(current.state):
            break
        in_flight = dispatch(current)
        current = transition(in_flight, outcome, max_soft_retries=max_soft_retries)
        consumed += 1
    return current, consumed


# ────────────────────────────────────────────────────────────────────────
# Invariant 1 — Never-skip: any non-empty sequence terminates
# ────────────────────────────────────────────────────────────────────────


class TestNeverSkipInvariant:
    """Verify NFR-REL-006: no item is ever silently dropped."""

    @given(item=fresh_item_st, outcomes=outcome_sequence_st)
    @settings(max_examples=200, deadline=None)
    def test_every_sequence_reaches_terminal(self, item: WorkItem, outcomes: list[Outcome]) -> None:
        """A non-empty random outcome sequence must drive the item to
        a terminal state (eventually)."""
        # Use a small max_soft_retries so that retry-exhaustion kicks
        # in quickly even for sequences of just 3 transients.
        final, _consumed = _drive_sequence(item, outcomes, max_soft_retries=3)
        # Either we hit a terminal during the sequence, or we ran out
        # of outcomes while still non-terminal. Pad with transients
        # until we're guaranteed terminal — this checks the FSM always
        # eventually terminates.
        if not is_terminal(final.state):
            padding = [Outcome.TIMEOUT] * 5
            final, _ = _drive_sequence(final, padding, max_soft_retries=3)
        assert is_terminal(final.state)

    @given(item=fresh_item_st)
    @settings(max_examples=100, deadline=None)
    def test_single_ok_with_data_reaches_success(self, item: WorkItem) -> None:
        final = _drive_once(item, Outcome.OK_WITH_DATA)
        assert final.state == WorkItemState.SUCCESS
        assert final.quarantine_reason is None

    @given(item=fresh_item_st)
    @settings(max_examples=100, deadline=None)
    def test_single_ok_empty_reaches_no_data(self, item: WorkItem) -> None:
        final = _drive_once(item, Outcome.OK_EMPTY)
        assert final.state == WorkItemState.NO_DATA

    @given(item=fresh_item_st)
    @settings(max_examples=100, deadline=None)
    def test_single_not_found_reaches_no_data(self, item: WorkItem) -> None:
        """NOT_FOUND is explicitly a recorded NO_DATA, not a skip."""
        final = _drive_once(item, Outcome.NOT_FOUND)
        assert final.state == WorkItemState.NO_DATA


# ────────────────────────────────────────────────────────────────────────
# Invariant 2 — Terminal states are absorbing
# ────────────────────────────────────────────────────────────────────────


class TestTerminalAbsorbs:
    @given(item=fresh_item_st, first=outcome_st, second=outcome_st)
    @settings(max_examples=200, deadline=None)
    def test_cannot_transition_terminal_item(
        self,
        item: WorkItem,
        first: Outcome,
        second: Outcome,
    ) -> None:
        """Once terminal, any further transition() call must raise."""
        # Drive to some state.
        after_first = _drive_once(item, first)
        if not is_terminal(after_first.state):
            return  # precondition not met; hypothesis will find other examples

        # Cannot re-dispatch.
        with pytest.raises(TransitionError):
            dispatch(after_first)

        # Cannot re-transition either (transition() rejects terminal).
        with pytest.raises(TransitionError):
            transition(after_first, second)


# ────────────────────────────────────────────────────────────────────────
# Invariant 3 — retry_count monotonic non-decreasing
# ────────────────────────────────────────────────────────────────────────


class TestRetryMonotonic:
    @given(item=fresh_item_st, outcomes=outcome_sequence_st)
    @settings(max_examples=200, deadline=None)
    def test_retry_count_never_decreases(self, item: WorkItem, outcomes: list[Outcome]) -> None:
        current = item
        prev_retry = current.retry_count
        for outcome in outcomes:
            if is_terminal(current.state):
                break
            in_flight = dispatch(current)
            current = transition(in_flight, outcome)
            assert current.retry_count >= prev_retry
            prev_retry = current.retry_count


# ────────────────────────────────────────────────────────────────────────
# Invariant 4 — retry exhaustion → QUARANTINED(retry_exhausted)
# ────────────────────────────────────────────────────────────────────────


class TestRetryExhaustion:
    @given(
        item=fresh_item_st,
        transients=st.lists(transient_outcome_st, min_size=5, max_size=12),
    )
    @settings(max_examples=100, deadline=None)
    def test_many_transients_quarantine_retry_exhausted(
        self,
        item: WorkItem,
        transients: list[Outcome],
    ) -> None:
        """At max_soft_retries=3, 5+ transient outcomes must quarantine
        with reason RETRY_EXHAUSTED."""
        final, _ = _drive_sequence(item, transients, max_soft_retries=3)
        assert final.state == WorkItemState.QUARANTINED
        assert final.quarantine_reason == QuarantineReason.RETRY_EXHAUSTED

    @given(item=fresh_item_st)
    @settings(max_examples=50, deadline=None)
    def test_retry_counter_equals_transitions_to_retrying(self, item: WorkItem) -> None:
        """Each transient outcome bumps retry_count by exactly one."""
        current = item
        for i in range(1, 4):  # max_soft_retries=5 so 3 is safe
            in_flight = dispatch(current)
            current = transition(in_flight, Outcome.TIMEOUT, max_soft_retries=5)
            assert current.retry_count == i
            assert current.state == WorkItemState.RETRYING


# ────────────────────────────────────────────────────────────────────────
# Invariant 5 — quarantine reason ↔ QUARANTINED state
# ────────────────────────────────────────────────────────────────────────


class TestQuarantineReasonConsistency:
    @given(item=fresh_item_st, outcomes=outcome_sequence_st)
    @settings(max_examples=200, deadline=None)
    def test_quarantine_reason_set_iff_quarantined(
        self, item: WorkItem, outcomes: list[Outcome]
    ) -> None:
        final, _ = _drive_sequence(item, outcomes, max_soft_retries=3)

        if final.state == WorkItemState.QUARANTINED:
            assert final.quarantine_reason is not None
        else:
            assert final.quarantine_reason is None

    @given(item=fresh_item_st)
    @settings(max_examples=50, deadline=None)
    def test_bad_request_outcome_yields_bad_request_reason(self, item: WorkItem) -> None:
        final = _drive_once(item, Outcome.BAD_REQUEST)
        assert final.state == WorkItemState.QUARANTINED
        assert final.quarantine_reason == QuarantineReason.BAD_REQUEST

    @given(item=fresh_item_st)
    @settings(max_examples=50, deadline=None)
    def test_auth_failed_outcome_yields_auth_failed_reason(self, item: WorkItem) -> None:
        final = _drive_once(item, Outcome.AUTH_FAILED)
        assert final.state == WorkItemState.QUARANTINED
        assert final.quarantine_reason == QuarantineReason.AUTH_FAILED

    @given(item=fresh_item_st)
    @settings(max_examples=50, deadline=None)
    def test_authz_failed_outcome_yields_authz_failed_reason(self, item: WorkItem) -> None:
        final = _drive_once(item, Outcome.AUTHZ_FAILED)
        assert final.state == WorkItemState.QUARANTINED
        assert final.quarantine_reason == QuarantineReason.AUTHZ_FAILED


# ────────────────────────────────────────────────────────────────────────
# Invariant 6 — dispatch only from PENDING / RETRYING
# ────────────────────────────────────────────────────────────────────────


class TestDispatchLegality:
    @given(item=fresh_item_st)
    @settings(max_examples=100, deadline=None)
    def test_dispatch_from_pending_ok(self, item: WorkItem) -> None:
        dispatched = dispatch(item)
        assert dispatched.state == WorkItemState.IN_FLIGHT

    @given(item=fresh_item_st)
    @settings(max_examples=100, deadline=None)
    def test_cannot_dispatch_in_flight(self, item: WorkItem) -> None:
        in_flight = dispatch(item)
        with pytest.raises(TransitionError):
            dispatch(in_flight)

    @given(item=fresh_item_st)
    @settings(max_examples=100, deadline=None)
    def test_cannot_dispatch_success(self, item: WorkItem) -> None:
        success = _drive_once(item, Outcome.OK_WITH_DATA)
        with pytest.raises(TransitionError):
            dispatch(success)

    @given(item=fresh_item_st)
    @settings(max_examples=100, deadline=None)
    def test_dispatch_from_retrying_ok(self, item: WorkItem) -> None:
        retrying = _drive_once(item, Outcome.TIMEOUT)
        assert retrying.state == WorkItemState.RETRYING
        dispatched = dispatch(retrying)
        assert dispatched.state == WorkItemState.IN_FLIGHT


# ────────────────────────────────────────────────────────────────────────
# Invariant 7 — params canonicalisation
# ────────────────────────────────────────────────────────────────────────


class TestParamsCanonicalisation:
    @given(
        phase=st.sampled_from(_ALL_PHASES),
        params=st.dictionaries(
            keys=st.text(
                alphabet=st.characters(min_codepoint=0x61, max_codepoint=0x7A),
                min_size=1,
                max_size=6,
            ),
            values=st.text(
                alphabet=st.characters(min_codepoint=0x30, max_codepoint=0x39),
                min_size=1,
                max_size=6,
            ),
            min_size=1,
            max_size=6,
        ),
    )
    @settings(max_examples=150, deadline=None)
    def test_param_order_irrelevant(self, phase: IngestPhase, params: dict[str, str]) -> None:
        """WorkItem.new must canonicalise params to a sorted tuple —
        the order in which the caller builds the dict is irrelevant."""
        h = hashlib.sha256(repr(sorted(params.items())).encode()).hexdigest()
        item1 = WorkItem.new(phase=phase, content_hash=h, params=params)

        # Shuffle the dict by rebuilding it in reverse key order.
        reversed_params = dict(reversed(list(params.items())))
        item2 = WorkItem.new(phase=phase, content_hash=h, params=reversed_params)

        assert item1.params == item2.params
        # And the canonical form is in sorted order.
        keys = [k for k, _ in item1.params]
        assert keys == sorted(keys)


# ────────────────────────────────────────────────────────────────────────
# phase_complete: list of driven items must satisfy §8.7 completeness
# ────────────────────────────────────────────────────────────────────────


class TestPhaseCompleteProperty:
    @given(
        items_and_outcomes=st.lists(
            st.tuples(fresh_item_st, outcome_st),
            min_size=1,
            max_size=20,
        ),
    )
    @settings(max_examples=100, deadline=None)
    def test_phase_complete_after_driving_all_items(
        self, items_and_outcomes: list[tuple[WorkItem, Outcome]]
    ) -> None:
        """After driving every item to terminal (and manually
        resolving any quarantine), phase_complete must return True."""
        from brutex.orchestrator.state import resolve_quarantine

        terminal_items: list[WorkItem] = []
        for item, outcome in items_and_outcomes:
            final = _drive_once(item, outcome)
            # Make sure it's terminal (may need retry padding for
            # transient outcomes).
            if not is_terminal(final.state):
                final, _ = _drive_sequence(final, [Outcome.TIMEOUT] * 12, max_soft_retries=3)
            # Resolve any quarantines so phase_complete can pass.
            if final.state == WorkItemState.QUARANTINED:
                final = resolve_quarantine(final, as_state=WorkItemState.NO_DATA)
            terminal_items.append(final)

        assert phase_complete(terminal_items) is True
