"""Unit tests for brutex.dashboard.app — state model only.

FastAPI is lazy-imported so these tests pass without it installed.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from brutex.dashboard.app import (
    DashboardState,
    PhaseSnapshot,
    SweepFunnelSnapshot,
    snapshot_to_json,
)
from brutex.orchestrator.events import IngestPhase, PipelineEvent, PipelineEventKind

# ────────────────────────────────────────────────────────────────────────
# PhaseSnapshot
# ────────────────────────────────────────────────────────────────────────


class TestPhaseSnapshot:
    def test_percent_zero_when_total_zero(self) -> None:
        snap = PhaseSnapshot(phase="A", status="pending")
        assert snap.percent == 0.0

    def test_percent_happy(self) -> None:
        snap = PhaseSnapshot(phase="A", status="running", total=10, done=3)
        assert snap.percent == 30.0

    def test_percent_capped_at_100(self) -> None:
        snap = PhaseSnapshot(phase="A", status="done", total=10, done=15)
        assert snap.percent == 100.0


# ────────────────────────────────────────────────────────────────────────
# DashboardState init
# ────────────────────────────────────────────────────────────────────────


class TestStateInit:
    def test_initial_state_has_all_phases(self) -> None:
        state = DashboardState()
        assert "A_INSTRUMENTS" in state.phases
        assert "B_EXPIRIES" in state.phases
        assert "C_CONTRACTS" in state.phases
        assert "D_CANDLES" in state.phases

    def test_initial_run_status_is_idle(self) -> None:
        state = DashboardState()
        assert state.run_status == "idle"
        assert state.run_id is None


# ────────────────────────────────────────────────────────────────────────
# Pipeline event handling
# ────────────────────────────────────────────────────────────────────────


def _event(
    kind: str,
    *,
    phase: IngestPhase | None = None,
    **kwargs: Any,
) -> PipelineEvent:
    return PipelineEvent(
        kind=kind,
        at=datetime.now(UTC),
        phase=phase,
        **kwargs,
    )


class TestOnPipelineEvent:
    def test_run_start(self) -> None:
        state = DashboardState()
        state.on_pipeline_event(_event(PipelineEventKind.RUN_START, run_id="run-abc"))
        assert state.run_id == "run-abc"
        assert state.run_status == "running"
        assert state.run_started_at is not None

    def test_phase_start_updates_phase_snapshot(self) -> None:
        state = DashboardState()
        state.on_pipeline_event(
            _event(
                PipelineEventKind.PHASE_START,
                phase=IngestPhase.A_INSTRUMENTS,
                total=223,
            )
        )
        snap = state.phases["A_INSTRUMENTS"]
        assert snap.status == "running"
        assert snap.total == 223
        assert snap.started_at is not None

    def test_phase_progress_updates_counts(self) -> None:
        state = DashboardState()
        state.on_pipeline_event(
            _event(
                PipelineEventKind.PHASE_START,
                phase=IngestPhase.A_INSTRUMENTS,
                total=100,
            )
        )
        state.on_pipeline_event(
            _event(
                PipelineEventKind.PHASE_PROGRESS,
                phase=IngestPhase.A_INSTRUMENTS,
                total=100,
                done=42,
                success=40,
                no_data=1,
                quarantined=1,
            )
        )
        snap = state.phases["A_INSTRUMENTS"]
        assert snap.done == 42
        assert snap.success == 40
        assert snap.no_data == 1
        assert snap.quarantined == 1
        assert snap.percent == 42.0

    def test_phase_complete_marks_done(self) -> None:
        state = DashboardState()
        state.on_pipeline_event(
            _event(
                PipelineEventKind.PHASE_START,
                phase=IngestPhase.A_INSTRUMENTS,
                total=5,
            )
        )
        state.on_pipeline_event(
            _event(
                PipelineEventKind.PHASE_COMPLETE,
                phase=IngestPhase.A_INSTRUMENTS,
                total=5,
                done=5,
                success=5,
                elapsed_seconds=1.23,
            )
        )
        snap = state.phases["A_INSTRUMENTS"]
        assert snap.status == "done"
        assert snap.done == 5
        assert snap.success == 5
        assert snap.elapsed_seconds == 1.23

    def test_phase_progress_without_counts_leaves_snapshot_untouched(self) -> None:
        """A PHASE_PROGRESS event with every count ``None`` changes nothing."""
        state = DashboardState()
        state.on_pipeline_event(
            _event(
                PipelineEventKind.PHASE_START,
                phase=IngestPhase.A_INSTRUMENTS,
                total=7,
            )
        )
        state.on_pipeline_event(
            _event(PipelineEventKind.PHASE_PROGRESS, phase=IngestPhase.A_INSTRUMENTS)
        )
        snap = state.phases["A_INSTRUMENTS"]
        assert snap.total == 7
        assert snap.done == 0
        assert snap.success == 0
        assert snap.no_data == 0
        assert snap.quarantined == 0

    def test_phase_complete_records_no_data_quarantined_retried(self) -> None:
        """PHASE_COMPLETE copies no_data / quarantined / retried; ``None`` total is kept."""
        state = DashboardState()
        state.on_pipeline_event(
            _event(
                PipelineEventKind.PHASE_START,
                phase=IngestPhase.A_INSTRUMENTS,
                total=9,
            )
        )
        state.on_pipeline_event(
            _event(
                PipelineEventKind.PHASE_COMPLETE,
                phase=IngestPhase.A_INSTRUMENTS,
                no_data=2,
                quarantined=1,
                retried=3,
            )
        )
        snap = state.phases["A_INSTRUMENTS"]
        assert snap.status == "done"
        # total / success were None on the event → the started values survive.
        assert snap.total == 9
        assert snap.done == 0
        assert snap.success == 0
        assert snap.no_data == 2
        assert snap.quarantined == 1
        assert snap.retried == 3

    def test_item_complete_with_phase_changes_no_phase_status(self) -> None:
        """A non-lifecycle kind (ITEM_COMPLETE) with a phase set leaves the snapshot as-is."""
        state = DashboardState()
        state.on_pipeline_event(
            _event(
                PipelineEventKind.PHASE_START,
                phase=IngestPhase.A_INSTRUMENTS,
                total=5,
            )
        )
        state.on_pipeline_event(
            _event(
                PipelineEventKind.ITEM_COMPLETE,
                phase=IngestPhase.A_INSTRUMENTS,
                item_id="223|NSE_EQ",
            )
        )
        snap = state.phases["A_INSTRUMENTS"]
        assert snap.status == "running"
        assert snap.total == 5

    def test_phase_halted_marks_halted_and_run(self) -> None:
        state = DashboardState()
        state.on_pipeline_event(
            _event(
                PipelineEventKind.PHASE_START,
                phase=IngestPhase.A_INSTRUMENTS,
                total=5,
            )
        )
        state.on_pipeline_event(
            _event(
                PipelineEventKind.PHASE_HALTED,
                phase=IngestPhase.A_INSTRUMENTS,
                total=5,
                done=3,
                quarantined=1,
                message="bad request from Dhan",
            )
        )
        snap = state.phases["A_INSTRUMENTS"]
        assert snap.status == "halted"
        assert snap.message == "bad request from Dhan"
        assert state.run_status == "halted"

    def test_run_complete(self) -> None:
        state = DashboardState()
        state.on_pipeline_event(_event(PipelineEventKind.RUN_START))
        state.on_pipeline_event(_event(PipelineEventKind.RUN_COMPLETE, elapsed_seconds=42.0))
        assert state.run_status == "complete"
        assert state.run_elapsed_seconds == 42.0

    def test_events_ring_buffer_bounded(self) -> None:
        state = DashboardState()
        for _ in range(600):  # > deque maxlen (500)
            state.on_pipeline_event(_event(PipelineEventKind.PHASE_PROGRESS))
        assert len(state.recent_events) == 500


# ────────────────────────────────────────────────────────────────────────
# Rate-limit + telegram + worker updates
# ────────────────────────────────────────────────────────────────────────


class TestOtherUpdates:
    def test_update_rate_limit(self) -> None:
        state = DashboardState()
        state.update_rate_limit(current_rps=12.5, available_tokens=3.0, recent_429_count=1)
        assert state.rate_limit.current_rps == 12.5
        assert state.rate_limit.available_tokens == 3.0
        assert state.rate_limit.recent_429_count == 1

    def test_update_telegram(self) -> None:
        state = DashboardState()
        state.update_telegram(
            stats={
                "sent": 100,
                "dropped_on_failure": 3,
                "dropped_on_dedupe": 7,
                "dropped_on_queue_full": 0,
                "dropped_on_disabled": 0,
            }
        )
        assert state.telegram.sent == 100
        assert state.telegram.dropped_on_failure == 3
        assert state.telegram.dropped_on_dedupe == 7

    def test_update_worker_upsert(self) -> None:
        state = DashboardState()
        state.update_worker(
            worker_id="w1",
            status="busy",
            current_task="NIFTY:exp",
            elapsed_seconds=2.5,
        )
        assert "w1" in state.workers
        assert state.workers["w1"].status == "busy"
        # Upsert overwrites
        state.update_worker(worker_id="w1", status="idle")
        assert state.workers["w1"].status == "idle"


# ────────────────────────────────────────────────────────────────────────
# Snapshot serialisation
# ────────────────────────────────────────────────────────────────────────


class TestSnapshotSerialization:
    def test_to_dict_shape(self) -> None:
        state = DashboardState()
        state.on_pipeline_event(_event(PipelineEventKind.RUN_START, run_id="r"))
        snapshot = state.to_dict()
        for k in (
            "run_id",
            "run_status",
            "run_started_at",
            "phases",
            "workers",
            "telegram",
            "rate_limit",
            "recent_events",
        ):
            assert k in snapshot
        assert isinstance(snapshot["phases"], list)
        assert len(snapshot["phases"]) == 4  # all four phases initialized

    def test_snapshot_to_json_is_valid(self) -> None:
        state = DashboardState()
        raw = snapshot_to_json(state)
        parsed = json.loads(raw)
        assert parsed["run_status"] == "idle"


# ────────────────────────────────────────────────────────────────────────
# Sweep funnel — DEC-DASHBOARD-LIVE-1
# Per TRACK1_DASHBOARD_DESIGN.md §6.2 / §6.4 — incremental swap-out from
# placeholder MOCKUP numbers to live sweep data.
# ────────────────────────────────────────────────────────────────────────


class TestSweepFunnel:
    def test_default_snapshot_is_zeroed(self) -> None:
        """Empty SweepFunnelSnapshot defaults — used as a sentinel before first sweep."""
        snap = SweepFunnelSnapshot()
        assert snap.step_0_all_candidates == 0
        assert snap.winners_top_n == 0
        assert snap.sweep_id == ""
        assert snap.snapshot_at == ""

    def test_state_sweep_funnel_starts_none(self) -> None:
        """DashboardState.sweep_funnel is None until update_sweep_funnel() is called."""
        state = DashboardState()
        assert state.sweep_funnel is None

    def test_update_sweep_funnel_replaces_snapshot(self) -> None:
        """update_sweep_funnel installs the snapshot under the state lock."""
        state = DashboardState()
        snap = SweepFunnelSnapshot(
            step_0_all_candidates=12_600_000,
            step_1_cheap_prefilter=1_500_000,
            winners_top_n=1,
            sweep_id="sweep-2026-05-24",
            snapshot_at="2026-05-24T10:30:00Z",
        )
        state.update_sweep_funnel(snap)
        # Bind to local for pyright strict narrowing — instance-attribute
        # narrowing across `is`/`is not None` is not guaranteed.
        installed = state.sweep_funnel
        assert installed is snap
        assert installed is not None
        assert installed.step_0_all_candidates == 12_600_000

    def test_to_dict_serializes_sweep_funnel_when_set(self) -> None:
        """to_dict() includes the sweep_funnel dict when populated."""
        state = DashboardState()
        snap = SweepFunnelSnapshot(
            step_0_all_candidates=33_000_000_000,
            sweep_id="sweep-phase10",
            snapshot_at="2026-05-24T10:30:00Z",
        )
        state.update_sweep_funnel(snap)
        out = state.to_dict()
        assert out["sweep_funnel"] is not None
        assert out["sweep_funnel"]["step_0_all_candidates"] == 33_000_000_000
        assert out["sweep_funnel"]["sweep_id"] == "sweep-phase10"

    def test_to_dict_sweep_funnel_is_none_by_default(self) -> None:
        """to_dict() emits sweep_funnel: None until first update — JS poller
        falls back to mockup placeholder values (PREVIEW DATA badge stays).
        """
        state = DashboardState()
        out = state.to_dict()
        assert "sweep_funnel" in out
        assert out["sweep_funnel"] is None
