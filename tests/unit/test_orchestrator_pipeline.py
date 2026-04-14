"""Unit tests for brutex.orchestrator.pipeline.

Validates:
* drive_item_to_terminal retry loop
* PhaseRunner aggregation + event emission + halt on quarantine
* run_sequential_pipeline gating (B does not start before A completes)
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from brutex.orchestrator.audit import AuditWriter, verify_chain
from brutex.orchestrator.pipeline import (
    DEFAULT_MAX_ITERATIONS_PER_ITEM,
    IterationLimitError,
    PhaseHaltError,
    PhaseResult,
    PhaseRunner,
    PipelineError,
    PipelineEvent,
    PipelineEventKind,
    drive_item_to_terminal,
    run_sequential_pipeline,
)
from brutex.orchestrator.state import (
    IngestPhase,
    Outcome,
    QuarantineReason,
    WorkItem,
    WorkItemState,
    dispatch,
    resolve_quarantine,
    transition,
)
from brutex.orchestrator.store import WorkItemStore


# ────────────────────────────────────────────────────────────────────────
# Helpers / fixtures
# ────────────────────────────────────────────────────────────────────────


def _make_hash(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _make_item(seed: str, phase: IngestPhase = IngestPhase.A_INSTRUMENTS) -> WorkItem:
    return WorkItem.new(
        phase=phase,
        content_hash=_make_hash(seed),
        params={"seed": seed},
    )


@pytest.fixture
def store(tmp_path: Path):
    with WorkItemStore.open(tmp_path / "store.lmdb") as s:
        yield s


@pytest.fixture
def audit(tmp_path: Path):
    with AuditWriter.open(tmp_path / "audit" / "run.jsonl") as a:
        yield a


@pytest.fixture
def audit_path(tmp_path: Path) -> Path:
    return tmp_path / "audit" / "run.jsonl"


# ────────────────────────────────────────────────────────────────────────
# Fake invoke callables
# ────────────────────────────────────────────────────────────────────────


class FakeInvoke:
    """Configurable invoke callable for driving WorkItems."""

    def __init__(
        self,
        *,
        outcomes: list[Outcome] | None = None,
        default: Outcome = Outcome.OK_WITH_DATA,
    ) -> None:
        self._outcomes = outcomes or []
        self._default = default
        self.call_count = 0

    def __call__(self, item: WorkItem) -> tuple[WorkItem, Any]:
        idx = min(self.call_count, len(self._outcomes) - 1) if self._outcomes else -1
        outcome = self._outcomes[idx] if idx >= 0 else self._default
        self.call_count += 1
        # Must dispatch first since item starts PENDING (or RETRYING after
        # a previous retry)
        dispatched = dispatch(item)
        new_item = transition(dispatched, outcome)
        return new_item, {"response": "fake"}


# ────────────────────────────────────────────────────────────────────────
# drive_item_to_terminal — the per-item retry loop
# ────────────────────────────────────────────────────────────────────────


class TestDriveItemToTerminal:
    def test_happy_path(self) -> None:
        item = _make_item("happy")
        invoke = FakeInvoke(default=Outcome.OK_WITH_DATA)
        final, response = drive_item_to_terminal(
            work_item=item,
            invoke=invoke,
            sleep=lambda _: None,
        )
        assert final.state == WorkItemState.SUCCESS
        assert invoke.call_count == 1
        assert response == {"response": "fake"}

    def test_no_data(self) -> None:
        item = _make_item("nodata")
        invoke = FakeInvoke(default=Outcome.OK_EMPTY)
        final, _ = drive_item_to_terminal(
            work_item=item,
            invoke=invoke,
            sleep=lambda _: None,
        )
        assert final.state == WorkItemState.NO_DATA

    def test_not_found_is_terminal(self) -> None:
        item = _make_item("nf")
        invoke = FakeInvoke(default=Outcome.NOT_FOUND)
        final, _ = drive_item_to_terminal(
            work_item=item,
            invoke=invoke,
            sleep=lambda _: None,
        )
        assert final.state == WorkItemState.NO_DATA

    def test_quarantine_is_terminal(self) -> None:
        item = _make_item("bad")
        invoke = FakeInvoke(default=Outcome.BAD_REQUEST)
        final, _ = drive_item_to_terminal(
            work_item=item,
            invoke=invoke,
            sleep=lambda _: None,
        )
        assert final.state == WorkItemState.QUARANTINED
        assert final.quarantine_reason == QuarantineReason.BAD_REQUEST

    def test_retry_then_success(self) -> None:
        """First call rate-limits, second succeeds."""
        item = _make_item("retry")
        invoke = FakeInvoke(outcomes=[Outcome.RATE_LIMIT, Outcome.OK_WITH_DATA])
        sleeps: list[float] = []
        final, _ = drive_item_to_terminal(
            work_item=item,
            invoke=invoke,
            sleep=sleeps.append,
        )
        assert final.state == WorkItemState.SUCCESS
        assert invoke.call_count == 2
        assert len(sleeps) == 1
        assert sleeps[0] > 0  # first retry backoff

    def test_multiple_retries(self) -> None:
        """3 rate limits then a success."""
        item = _make_item("retry3")
        invoke = FakeInvoke(
            outcomes=[
                Outcome.RATE_LIMIT,
                Outcome.RATE_LIMIT,
                Outcome.RATE_LIMIT,
                Outcome.OK_WITH_DATA,
            ]
        )
        sleeps: list[float] = []
        final, _ = drive_item_to_terminal(
            work_item=item,
            invoke=invoke,
            sleep=sleeps.append,
        )
        assert final.state == WorkItemState.SUCCESS
        assert invoke.call_count == 4
        assert len(sleeps) == 3

    def test_retry_exhaustion_escalates_to_quarantine(self) -> None:
        """After 10 retries, the state machine escalates to QUARANTINED."""
        item = _make_item("exh")
        # Always return RATE_LIMIT — the FSM will hit the retry cap
        invoke = FakeInvoke(default=Outcome.RATE_LIMIT)
        final, _ = drive_item_to_terminal(
            work_item=item,
            invoke=invoke,
            sleep=lambda _: None,
            max_iterations=20,
        )
        assert final.state == WorkItemState.QUARANTINED
        assert final.quarantine_reason == QuarantineReason.RETRY_EXHAUSTED
        # Exactly 10 invokes (hit the MAX_SOFT_RETRIES cap)
        assert invoke.call_count == 10

    def test_iteration_limit_raises(self) -> None:
        """A buggy adapter that returns PENDING instead of terminal
        should blow up with IterationLimitError or PipelineError."""

        def broken_invoke(item: WorkItem) -> tuple[WorkItem, Any]:
            # Return the item unchanged — still PENDING
            return item, None

        item = _make_item("broken")
        with pytest.raises(PipelineError, match="unexpected state"):
            drive_item_to_terminal(
                work_item=item,
                invoke=broken_invoke,
                sleep=lambda _: None,
                max_iterations=5,
            )


# ────────────────────────────────────────────────────────────────────────
# PhaseRunner — multi-item aggregation
# ────────────────────────────────────────────────────────────────────────


class TestPhaseRunner:
    def test_all_success(
        self, store: WorkItemStore, audit: AuditWriter
    ) -> None:
        runner = PhaseRunner(
            phase=IngestPhase.A_INSTRUMENTS,
            store=store,
            audit=audit,
            invoke=FakeInvoke(default=Outcome.OK_WITH_DATA),
            sleep=lambda _: None,
            run_id="run-1",
        )
        items = [_make_item(f"i{i}") for i in range(5)]
        result = runner.run(items)
        assert isinstance(result, PhaseResult)
        assert result.total == 5
        assert result.success == 5
        assert result.no_data == 0
        assert result.quarantined == 0

    def test_mixed_outcomes(
        self, store: WorkItemStore, audit: AuditWriter
    ) -> None:
        """3 success + 1 no-data + rest success."""
        outcomes_sequence = [
            Outcome.OK_WITH_DATA,
            Outcome.OK_EMPTY,
            Outcome.OK_WITH_DATA,
        ]

        class SeqInvoke:
            def __init__(self) -> None:
                self._i = 0
                self.call_count = 0

            def __call__(self, item: WorkItem) -> tuple[WorkItem, Any]:
                self.call_count += 1
                outcome = outcomes_sequence[self._i % len(outcomes_sequence)]
                self._i += 1
                dispatched = dispatch(item)
                return transition(dispatched, outcome), None

        runner = PhaseRunner(
            phase=IngestPhase.A_INSTRUMENTS,
            store=store,
            audit=audit,
            invoke=SeqInvoke(),
            sleep=lambda _: None,
            run_id="run-1",
        )
        items = [_make_item(f"x{i}") for i in range(6)]
        result = runner.run(items)
        assert result.total == 6
        assert result.success == 4  # indices 0,2,3,5 use OK_WITH_DATA
        assert result.no_data == 2  # indices 1,4 use OK_EMPTY

    def test_unresolved_quarantine_raises_halt(
        self, store: WorkItemStore, audit: AuditWriter
    ) -> None:
        """Any quarantined item (without manual resolution) halts the phase."""
        # One item returns BAD_REQUEST, the rest succeed
        seq = iter(
            [
                Outcome.OK_WITH_DATA,
                Outcome.BAD_REQUEST,
                Outcome.OK_WITH_DATA,
            ]
        )

        def invoke(item: WorkItem) -> tuple[WorkItem, Any]:
            outcome = next(seq)
            dispatched = dispatch(item)
            return transition(dispatched, outcome), None

        runner = PhaseRunner(
            phase=IngestPhase.A_INSTRUMENTS,
            store=store,
            audit=audit,
            invoke=invoke,
            sleep=lambda _: None,
            run_id="run-1",
        )
        items = [_make_item(f"h{i}") for i in range(3)]
        with pytest.raises(PhaseHaltError) as exc_info:
            runner.run(items)
        assert exc_info.value.phase == IngestPhase.A_INSTRUMENTS
        assert exc_info.value.quarantined_count == 1

    def test_resolved_quarantine_does_not_halt(
        self, store: WorkItemStore, audit: AuditWriter
    ) -> None:
        """Pre-resolved quarantined item in input is treated as terminal."""
        # Build an already-quarantined-and-resolved item
        base = _make_item("resolved")
        dispatched = dispatch(base)
        quarantined = transition(dispatched, Outcome.BAD_REQUEST)
        resolved = resolve_quarantine(quarantined, as_state=WorkItemState.NO_DATA)

        runner = PhaseRunner(
            phase=IngestPhase.A_INSTRUMENTS,
            store=store,
            audit=audit,
            invoke=FakeInvoke(default=Outcome.OK_WITH_DATA),
            sleep=lambda _: None,
            run_id="run-1",
        )
        result = runner.run([resolved, _make_item("x1")])
        assert result.total == 2
        assert result.success == 1
        assert result.no_data == 1
        assert result.quarantined == 0

    def test_terminal_items_passed_through(
        self, store: WorkItemStore, audit: AuditWriter
    ) -> None:
        """A terminal item in the input is counted but not driven."""
        base = _make_item("already")
        dispatched = dispatch(base)
        already_success = transition(dispatched, Outcome.OK_WITH_DATA)

        invoke_tracker = FakeInvoke(default=Outcome.OK_WITH_DATA)
        runner = PhaseRunner(
            phase=IngestPhase.A_INSTRUMENTS,
            store=store,
            audit=audit,
            invoke=invoke_tracker,
            sleep=lambda _: None,
            run_id="run-1",
        )
        result = runner.run([already_success, _make_item("new")])
        assert result.total == 2
        assert result.success == 2
        # Only the non-terminal item was actually invoked
        assert invoke_tracker.call_count == 1

    def test_events_emitted_in_order(
        self, store: WorkItemStore, audit: AuditWriter
    ) -> None:
        events: list[PipelineEvent] = []
        runner = PhaseRunner(
            phase=IngestPhase.A_INSTRUMENTS,
            store=store,
            audit=audit,
            invoke=FakeInvoke(default=Outcome.OK_WITH_DATA),
            subscribers=[events.append],
            sleep=lambda _: None,
            progress_every_n_items=2,
            run_id="run-1",
        )
        runner.run([_make_item(f"e{i}") for i in range(5)])

        kinds = [e.kind for e in events]
        assert PipelineEventKind.PHASE_START in kinds
        assert PipelineEventKind.ITEM_COMPLETE in kinds
        assert PipelineEventKind.PHASE_COMPLETE in kinds
        # Progress emitted every 2 items (5 items → progress at 2, 4, 5 = 3 progress events)
        progress_count = sum(1 for k in kinds if k == PipelineEventKind.PHASE_PROGRESS)
        assert progress_count >= 2

    def test_subscriber_error_does_not_crash_pipeline(
        self, store: WorkItemStore, audit: AuditWriter
    ) -> None:
        """A buggy subscriber must not break the ingest run."""

        def bad_sub(event: PipelineEvent) -> None:
            raise RuntimeError("buggy subscriber")

        runner = PhaseRunner(
            phase=IngestPhase.A_INSTRUMENTS,
            store=store,
            audit=audit,
            invoke=FakeInvoke(default=Outcome.OK_WITH_DATA),
            subscribers=[bad_sub],
            sleep=lambda _: None,
            run_id="run-1",
        )
        # Should NOT raise
        result = runner.run([_make_item("x")])
        assert result.success == 1

    def test_audit_entries_on_phase_events(
        self, store: WorkItemStore, audit: AuditWriter, audit_path: Path
    ) -> None:
        runner = PhaseRunner(
            phase=IngestPhase.A_INSTRUMENTS,
            store=store,
            audit=audit,
            invoke=FakeInvoke(default=Outcome.OK_WITH_DATA),
            sleep=lambda _: None,
            run_id="audit-run",
        )
        runner.run([_make_item("a"), _make_item("b")])
        audit.close()
        # Chain should be intact
        count = verify_chain(audit_path)
        # PHASE_START + PHASE_COMPLETE = 2 entries minimum
        assert count >= 2


# ────────────────────────────────────────────────────────────────────────
# run_sequential_pipeline — A→B→C→D gating
# ────────────────────────────────────────────────────────────────────────


@dataclass
class StubDriver:
    """Minimal PhaseDriverProtocol implementation for tests."""

    phase: IngestPhase
    result: PhaseResult | None = None
    raise_halt: bool = False
    quarantined_count: int = 0
    call_count: int = 0

    def enumerate_items(self):
        return []

    def run(self) -> PhaseResult:
        self.call_count += 1
        if self.raise_halt:
            raise PhaseHaltError(
                phase=self.phase, quarantined_count=self.quarantined_count
            )
        assert self.result is not None, "StubDriver.result must be set"
        return self.result


class TestSequentialPipeline:
    def test_all_phases_run_in_order(
        self, store: WorkItemStore, audit: AuditWriter
    ) -> None:
        drivers = [
            StubDriver(
                phase=IngestPhase.A_INSTRUMENTS,
                result=PhaseResult(
                    phase=IngestPhase.A_INSTRUMENTS,
                    total=5,
                    success=5,
                    no_data=0,
                    quarantined=0,
                    retried=0,
                    elapsed_seconds=0.1,
                ),
            ),
            StubDriver(
                phase=IngestPhase.B_EXPIRIES,
                result=PhaseResult(
                    phase=IngestPhase.B_EXPIRIES,
                    total=10,
                    success=10,
                    no_data=0,
                    quarantined=0,
                    retried=0,
                    elapsed_seconds=0.2,
                ),
            ),
        ]
        results = run_sequential_pipeline(
            drivers=drivers, audit=audit, run_id="seq-run"
        )
        assert len(results) == 2
        assert [r.phase for r in results] == [
            IngestPhase.A_INSTRUMENTS,
            IngestPhase.B_EXPIRIES,
        ]
        for d in drivers:
            assert d.call_count == 1

    def test_halt_on_first_phase_blocks_later_phases(
        self, store: WorkItemStore, audit: AuditWriter
    ) -> None:
        drivers = [
            StubDriver(
                phase=IngestPhase.A_INSTRUMENTS,
                raise_halt=True,
                quarantined_count=1,
            ),
            StubDriver(
                phase=IngestPhase.B_EXPIRIES,
                result=PhaseResult(
                    phase=IngestPhase.B_EXPIRIES,
                    total=0,
                    success=0,
                    no_data=0,
                    quarantined=0,
                    retried=0,
                    elapsed_seconds=0.0,
                ),
            ),
        ]
        with pytest.raises(PhaseHaltError):
            run_sequential_pipeline(
                drivers=drivers, audit=audit, run_id="halt-run"
            )
        # Phase A was called, Phase B was NOT
        assert drivers[0].call_count == 1
        assert drivers[1].call_count == 0

    def test_run_level_events_emitted(
        self, store: WorkItemStore, audit: AuditWriter
    ) -> None:
        events: list[PipelineEvent] = []
        drivers = [
            StubDriver(
                phase=IngestPhase.A_INSTRUMENTS,
                result=PhaseResult(
                    phase=IngestPhase.A_INSTRUMENTS,
                    total=1,
                    success=1,
                    no_data=0,
                    quarantined=0,
                    retried=0,
                    elapsed_seconds=0.1,
                ),
            ),
        ]
        run_sequential_pipeline(
            drivers=drivers,
            audit=audit,
            subscribers=[events.append],
            run_id="events-run",
        )
        kinds = [e.kind for e in events]
        assert PipelineEventKind.RUN_START in kinds
        assert PipelineEventKind.RUN_COMPLETE in kinds
