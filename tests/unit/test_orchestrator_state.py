"""Unit tests for brutex.orchestrator.state — the work-item FSM.

Covers every transition path in ``transition()``, ``dispatch()``,
``resolve_quarantine()``, and ``phase_complete()``. Also includes the
property-style test for ``NFR-REL-006`` (never-skip invariant): every
exhaust-mappable ``Outcome`` value must drive the item to a terminal
state or keep it retrying — never "silently lost".
"""

from __future__ import annotations

import pytest
from brutex.orchestrator.state import (
    MAX_SOFT_RETRIES,
    NON_TERMINAL_STATES,
    TERMINAL_STATES,
    IncompletePhaseError,
    IngestPhase,
    NonTerminalStateError,
    Outcome,
    QuarantineReason,
    TransitionError,
    WorkItem,
    WorkItemState,
    dispatch,
    is_terminal,
    phase_complete,
    resolve_quarantine,
    transition,
)

# ────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────


def _new_item(phase: IngestPhase = IngestPhase.A_INSTRUMENTS) -> WorkItem:
    return WorkItem.new(
        phase=phase,
        content_hash="a" * 64,
        params={"exchange": "NSE", "underlying_symbol": "NIFTY"},
    )


def _in_flight_item(phase: IngestPhase = IngestPhase.A_INSTRUMENTS) -> WorkItem:
    return dispatch(_new_item(phase))


# ────────────────────────────────────────────────────────────────────────
# State sets
# ────────────────────────────────────────────────────────────────────────


class TestStateSets:
    def test_terminal_states_are_exactly_three(self) -> None:
        assert {
            WorkItemState.SUCCESS,
            WorkItemState.NO_DATA,
            WorkItemState.QUARANTINED,
        } == TERMINAL_STATES

    def test_non_terminal_states_are_exactly_three(self) -> None:
        assert {
            WorkItemState.PENDING,
            WorkItemState.IN_FLIGHT,
            WorkItemState.RETRYING,
        } == NON_TERMINAL_STATES

    def test_terminal_and_non_terminal_are_disjoint_and_cover_all(self) -> None:
        all_states = set(WorkItemState)
        assert all_states == TERMINAL_STATES | NON_TERMINAL_STATES
        assert set() == TERMINAL_STATES & NON_TERMINAL_STATES

    def test_is_terminal_function(self) -> None:
        for s in TERMINAL_STATES:
            assert is_terminal(s) is True
        for s in NON_TERMINAL_STATES:
            assert is_terminal(s) is False


# ────────────────────────────────────────────────────────────────────────
# WorkItem construction
# ────────────────────────────────────────────────────────────────────────


class TestWorkItemNew:
    def test_new_is_pending(self) -> None:
        item = _new_item()
        assert item.state == WorkItemState.PENDING
        assert item.retry_count == 0
        assert item.quarantine_reason is None
        assert item.resolved_by_human is False

    def test_new_has_stable_uuid(self) -> None:
        item = _new_item()
        # item_id is a UUID4 string
        assert len(item.item_id) == 36
        assert item.item_id.count("-") == 4

    def test_params_are_canonical_sorted(self) -> None:
        item = WorkItem.new(
            phase=IngestPhase.B_EXPIRIES,
            content_hash="b" * 64,
            params={"year": "2024", "exchange": "NSE", "underlying_symbol": "NIFTY"},
        )
        # Sorted lexicographically by key
        assert item.params == (
            ("exchange", "NSE"),
            ("underlying_symbol", "NIFTY"),
            ("year", "2024"),
        )

    def test_frozen_dataclass(self) -> None:
        item = _new_item()
        with pytest.raises(Exception):
            item.state = WorkItemState.SUCCESS  # type: ignore[misc]


# ────────────────────────────────────────────────────────────────────────
# dispatch helper
# ────────────────────────────────────────────────────────────────────────


class TestDispatch:
    def test_pending_to_in_flight(self) -> None:
        item = _new_item()
        dispatched = dispatch(item)
        assert dispatched.state == WorkItemState.IN_FLIGHT
        assert dispatched.item_id == item.item_id

    def test_retrying_to_in_flight(self) -> None:
        item = _in_flight_item()
        retrying = transition(item, Outcome.RATE_LIMIT)
        assert retrying.state == WorkItemState.RETRYING
        dispatched = dispatch(retrying)
        assert dispatched.state == WorkItemState.IN_FLIGHT

    def test_cannot_dispatch_from_terminal(self) -> None:
        item = transition(_in_flight_item(), Outcome.OK_WITH_DATA)
        assert item.state == WorkItemState.SUCCESS
        with pytest.raises(TransitionError, match="only dispatch"):
            dispatch(item)

    def test_cannot_dispatch_from_in_flight(self) -> None:
        item = _in_flight_item()
        with pytest.raises(TransitionError, match="only dispatch"):
            dispatch(item)


# ────────────────────────────────────────────────────────────────────────
# transition() — success path
# ────────────────────────────────────────────────────────────────────────


class TestTransitionSuccess:
    def test_in_flight_ok_with_data_to_success(self) -> None:
        item = _in_flight_item()
        result = transition(item, Outcome.OK_WITH_DATA)
        assert result.state == WorkItemState.SUCCESS
        assert result.retry_count == 0
        assert result.quarantine_reason is None

    def test_in_flight_ok_empty_to_no_data(self) -> None:
        item = _in_flight_item()
        result = transition(item, Outcome.OK_EMPTY)
        assert result.state == WorkItemState.NO_DATA
        assert result.quarantine_reason is None

    def test_in_flight_not_found_to_no_data(self) -> None:
        """NotFound is NOT quarantined — it is recorded as 'Groww says
        nothing here'. This is the core of the never-skip guarantee.
        """
        item = _in_flight_item()
        result = transition(item, Outcome.NOT_FOUND)
        assert result.state == WorkItemState.NO_DATA


# ────────────────────────────────────────────────────────────────────────
# transition() — quarantine path
# ────────────────────────────────────────────────────────────────────────


class TestTransitionQuarantine:
    def test_bad_request_quarantines_with_reason(self) -> None:
        item = _in_flight_item()
        result = transition(item, Outcome.BAD_REQUEST)
        assert result.state == WorkItemState.QUARANTINED
        assert result.quarantine_reason == QuarantineReason.BAD_REQUEST
        assert result.resolved_by_human is False

    def test_auth_failed_quarantines(self) -> None:
        item = _in_flight_item()
        result = transition(item, Outcome.AUTH_FAILED)
        assert result.state == WorkItemState.QUARANTINED
        assert result.quarantine_reason == QuarantineReason.AUTH_FAILED

    def test_authz_failed_quarantines(self) -> None:
        item = _in_flight_item()
        result = transition(item, Outcome.AUTHZ_FAILED)
        assert result.state == WorkItemState.QUARANTINED
        assert result.quarantine_reason == QuarantineReason.AUTHZ_FAILED


# ────────────────────────────────────────────────────────────────────────
# transition() — retry path
# ────────────────────────────────────────────────────────────────────────


class TestTransitionRetry:
    @pytest.mark.parametrize(
        "outcome",
        [Outcome.RATE_LIMIT, Outcome.TIMEOUT, Outcome.GENERIC_API_ERROR],
    )
    def test_transient_first_time_to_retrying(self, outcome: Outcome) -> None:
        item = _in_flight_item()
        result = transition(item, outcome)
        assert result.state == WorkItemState.RETRYING
        assert result.retry_count == 1
        assert result.quarantine_reason is None

    def test_retry_count_increments_each_time(self) -> None:
        item = _in_flight_item()
        for expected_count in range(1, 6):
            item = transition(item, Outcome.RATE_LIMIT)
            assert item.state == WorkItemState.RETRYING
            assert item.retry_count == expected_count
            # Re-dispatch before the next transition
            item = dispatch(item)

    def test_retry_exhausted_upgrades_to_quarantined(self) -> None:
        """After MAX_SOFT_RETRIES (default 10), the item moves to
        QUARANTINED with reason 'retry_exhausted'.
        """
        item = _in_flight_item()
        for _ in range(MAX_SOFT_RETRIES - 1):
            item = transition(item, Outcome.RATE_LIMIT)
            assert item.state == WorkItemState.RETRYING
            item = dispatch(item)

        # The final retry should exhaust
        item = transition(item, Outcome.RATE_LIMIT)
        assert item.state == WorkItemState.QUARANTINED
        assert item.quarantine_reason == QuarantineReason.RETRY_EXHAUSTED
        assert item.retry_count == MAX_SOFT_RETRIES

    def test_custom_max_retries(self) -> None:
        """Tests can pass a lower retry cap to keep them fast."""
        item = _in_flight_item()
        item = transition(item, Outcome.RATE_LIMIT, max_soft_retries=2)
        assert item.state == WorkItemState.RETRYING
        item = dispatch(item)
        # Second attempt exhausts
        item = transition(item, Outcome.RATE_LIMIT, max_soft_retries=2)
        assert item.state == WorkItemState.QUARANTINED
        assert item.quarantine_reason == QuarantineReason.RETRY_EXHAUSTED


# ────────────────────────────────────────────────────────────────────────
# transition() — illegal paths
# ────────────────────────────────────────────────────────────────────────


class TestTransitionIllegal:
    @pytest.mark.parametrize(
        "terminal_state",
        [WorkItemState.SUCCESS, WorkItemState.NO_DATA, WorkItemState.QUARANTINED],
    )
    def test_cannot_transition_from_terminal(self, terminal_state: WorkItemState) -> None:
        """Terminal states are, well, terminal."""
        # Build an item already in the terminal state via legal paths
        if terminal_state == WorkItemState.SUCCESS:
            item = transition(_in_flight_item(), Outcome.OK_WITH_DATA)
        elif terminal_state == WorkItemState.NO_DATA:
            item = transition(_in_flight_item(), Outcome.OK_EMPTY)
        else:
            item = transition(_in_flight_item(), Outcome.BAD_REQUEST)

        with pytest.raises(TransitionError, match="terminal state"):
            transition(item, Outcome.OK_WITH_DATA)

    def test_cannot_transition_from_pending_directly(self) -> None:
        """Must dispatch() first — catches the "forgot to dispatch" bug."""
        item = _new_item()
        with pytest.raises(TransitionError, match="PENDING"):
            transition(item, Outcome.OK_WITH_DATA)


# ────────────────────────────────────────────────────────────────────────
# NFR-REL-006 — never-skip invariant
# ────────────────────────────────────────────────────────────────────────


class TestNeverSkipInvariant:
    """For EVERY Outcome value, drive a fresh item through the FSM
    and assert it reaches a terminal state (directly or via RETRYING
    → exhaustion → QUARANTINED). No outcome may ever leave an item in
    a 'silently missing' state.
    """

    @pytest.mark.parametrize("outcome", list(Outcome))
    def test_every_outcome_resolves_to_terminal_or_retry(self, outcome: Outcome) -> None:
        item = _in_flight_item()
        result = transition(item, outcome)
        # After one transition, the item is either terminal or retrying
        assert result.state in (TERMINAL_STATES | {WorkItemState.RETRYING})

    @pytest.mark.parametrize("outcome", list(Outcome))
    def test_every_outcome_eventually_terminates(self, outcome: Outcome) -> None:
        """Drive the FSM with the same outcome up to MAX_SOFT_RETRIES+1
        times — the item MUST be terminal at the end no matter what.
        """
        item = _in_flight_item()
        for _ in range(MAX_SOFT_RETRIES + 2):
            if is_terminal(item.state):
                break
            if item.state == WorkItemState.RETRYING:
                item = dispatch(item)
            item = transition(item, outcome)

        assert is_terminal(item.state), (
            f"Outcome {outcome.value} left item in non-terminal state {item.state.value}"
        )

    def test_mixed_outcome_sequence_terminates(self) -> None:
        """A realistic sequence: rate-limit twice, then timeout, then
        recover with data — should end SUCCESS with retry_count=3.
        """
        item = _in_flight_item()

        for outcome in [Outcome.RATE_LIMIT, Outcome.RATE_LIMIT, Outcome.TIMEOUT]:
            item = transition(item, outcome)
            assert item.state == WorkItemState.RETRYING
            item = dispatch(item)

        item = transition(item, Outcome.OK_WITH_DATA)
        assert item.state == WorkItemState.SUCCESS
        assert item.retry_count == 3


# ────────────────────────────────────────────────────────────────────────
# resolve_quarantine
# ────────────────────────────────────────────────────────────────────────


class TestResolveQuarantine:
    def test_resolve_to_success(self) -> None:
        item = transition(_in_flight_item(), Outcome.BAD_REQUEST)
        resolved = resolve_quarantine(item, as_state=WorkItemState.SUCCESS)
        assert resolved.state == WorkItemState.SUCCESS
        assert resolved.resolved_by_human is True

    def test_resolve_to_no_data(self) -> None:
        item = transition(_in_flight_item(), Outcome.BAD_REQUEST)
        resolved = resolve_quarantine(item, as_state=WorkItemState.NO_DATA)
        assert resolved.state == WorkItemState.NO_DATA
        assert resolved.resolved_by_human is True

    def test_cannot_resolve_non_quarantined(self) -> None:
        item = transition(_in_flight_item(), Outcome.OK_WITH_DATA)
        with pytest.raises(TransitionError, match="non-quarantined"):
            resolve_quarantine(item, as_state=WorkItemState.SUCCESS)

    @pytest.mark.parametrize(
        "illegal_target",
        [
            WorkItemState.QUARANTINED,
            WorkItemState.PENDING,
            WorkItemState.IN_FLIGHT,
            WorkItemState.RETRYING,
        ],
    )
    def test_cannot_resolve_to_non_terminal_or_quarantined(
        self, illegal_target: WorkItemState
    ) -> None:
        item = transition(_in_flight_item(), Outcome.BAD_REQUEST)
        with pytest.raises(TransitionError, match="SUCCESS or NO_DATA"):
            resolve_quarantine(item, as_state=illegal_target)


# ────────────────────────────────────────────────────────────────────────
# phase_complete() — §8.7 completeness invariant
# ────────────────────────────────────────────────────────────────────────


class TestPhaseComplete:
    def test_empty_list_is_complete(self) -> None:
        assert phase_complete([]) is True

    def test_all_success_is_complete(self) -> None:
        items = [transition(_in_flight_item(), Outcome.OK_WITH_DATA) for _ in range(3)]
        assert phase_complete(items) is True

    def test_mixed_success_and_no_data_is_complete(self) -> None:
        items = [
            transition(_in_flight_item(), Outcome.OK_WITH_DATA),
            transition(_in_flight_item(), Outcome.OK_EMPTY),
            transition(_in_flight_item(), Outcome.NOT_FOUND),
        ]
        assert phase_complete(items) is True

    def test_pending_item_raises(self) -> None:
        items = [
            transition(_in_flight_item(), Outcome.OK_WITH_DATA),
            _new_item(),  # PENDING
        ]
        with pytest.raises(NonTerminalStateError, match="PENDING"):
            phase_complete(items)

    def test_in_flight_item_raises(self) -> None:
        items = [
            transition(_in_flight_item(), Outcome.OK_WITH_DATA),
            _in_flight_item(),
        ]
        with pytest.raises(NonTerminalStateError, match="IN_FLIGHT"):
            phase_complete(items)

    def test_retrying_item_raises(self) -> None:
        items = [
            transition(_in_flight_item(), Outcome.OK_WITH_DATA),
            transition(_in_flight_item(), Outcome.RATE_LIMIT),
        ]
        with pytest.raises(NonTerminalStateError, match="RETRYING"):
            phase_complete(items)

    def test_unresolved_quarantine_raises(self) -> None:
        items = [
            transition(_in_flight_item(), Outcome.OK_WITH_DATA),
            transition(_in_flight_item(), Outcome.BAD_REQUEST),
        ]
        with pytest.raises(IncompletePhaseError, match="not yet resolved"):
            phase_complete(items)

    def test_resolved_quarantine_is_complete(self) -> None:
        quarantined = transition(_in_flight_item(), Outcome.BAD_REQUEST)
        resolved = resolve_quarantine(quarantined, as_state=WorkItemState.NO_DATA)
        items = [
            transition(_in_flight_item(), Outcome.OK_WITH_DATA),
            resolved,
        ]
        assert phase_complete(items) is True


# ────────────────────────────────────────────────────────────────────────
# IngestPhase enum
# ────────────────────────────────────────────────────────────────────────


class TestIngestPhase:
    def test_four_phases_exist(self) -> None:
        assert {p.value for p in IngestPhase} == {
            "A_INSTRUMENTS",
            "B_EXPIRIES",
            "C_CONTRACTS",
            "D_CANDLES",
        }
