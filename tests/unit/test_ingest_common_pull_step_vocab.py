"""Tests for the six-step pull-order vocabulary (DEC-PULL-SIX-STEP-ORDER-001).

Covers the COMMON leaf in :mod:`brutex.adapters.ingest_common`:
``PullStep`` (value == rank 1-6, labels, uniqueness), ``partition_spot_steps``
(only-indices / only-stocks / mixed / reference-only / dual-class / VIX /
empty-group omission / stability), and the observe-only ``StepOrderMonitor``
(begin/observe semantics, rank-regression ERROR, late-write WARN + count,
``raise_on_violation`` test oracle, 8-thread safety). 100% line+branch on the
new surface.
"""

from __future__ import annotations

import threading
from decimal import Decimal

import pytest
from brutex.adapters.ingest_common import (
    PullStep,
    StepOrderError,
    StepOrderMonitor,
    partition_spot_steps,
)
from brutex.models import BruteXError, Exchange, Instrument, InstrumentType, Segment
from structlog.testing import capture_logs


def _instrument(
    symbol: str,
    *,
    kind: InstrumentType = InstrumentType.IDX,
    security_id: int = 1,
) -> Instrument:
    """Build a minimal spot instrument keyed by *symbol* with the given *kind*."""
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=security_id,
        symbol_name=symbol,
        display_name=None,
        instrument=kind,
        segment=Segment.INDEX if kind == InstrumentType.IDX else Segment.CASH,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=0 if kind == InstrumentType.IDX else 500,
        tick_size=Decimal("0") if kind == InstrumentType.IDX else Decimal("0.05"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
    )


def _index(symbol: str) -> Instrument:
    return _instrument(symbol, kind=InstrumentType.IDX)


def _stock(symbol: str) -> Instrument:
    return _instrument(symbol, kind=InstrumentType.EQ)


class TestPullStepEnum:
    """The enum IS the rank — values 1-6 in the decreed order, unique labels."""

    def test_values_are_the_rank_one_to_six(self) -> None:
        assert [int(step) for step in PullStep] == [1, 2, 3, 4, 5, 6]

    def test_decreed_order(self) -> None:
        assert list(PullStep) == [
            PullStep.INDICES_SPOT,
            PullStep.STOCKS_SPOT,
            PullStep.INDICES_FUTURES,
            PullStep.STOCKS_FUTURES,
            PullStep.INDICES_OPTIONS,
            PullStep.STOCKS_OPTIONS,
        ]

    def test_labels(self) -> None:
        assert [step.label for step in PullStep] == [
            "indices-spot",
            "stocks-spot",
            "indices-futures",
            "stocks-futures",
            "indices-options",
            "stocks-options",
        ]

    def test_labels_unique(self) -> None:
        labels = [step.label for step in PullStep]
        assert len(set(labels)) == len(labels) == 6

    def test_rank_comparison_is_integer_order(self) -> None:
        assert PullStep.INDICES_SPOT < PullStep.STOCKS_SPOT < PullStep.INDICES_FUTURES
        assert PullStep.STOCKS_OPTIONS > PullStep.INDICES_OPTIONS


class TestPartitionSpotSteps:
    """Total, stable, empty-groups-omitted partition keyed on ``is_index``."""

    def test_only_indices_yields_one_group(self) -> None:
        insts = [_index("NIFTY"), _index("BANKNIFTY"), _index("SENSEX")]
        groups = partition_spot_steps(insts)
        assert groups == ((PullStep.INDICES_SPOT, tuple(insts)),)

    def test_only_stocks_yields_one_group(self) -> None:
        insts = [_stock("ADANIENT"), _stock("RELIANCE")]
        groups = partition_spot_steps(insts)
        assert groups == ((PullStep.STOCKS_SPOT, tuple(insts)),)

    def test_mixed_yields_indices_first(self) -> None:
        nifty = _index("NIFTY")
        adani = _stock("ADANIENT")
        sensex = _index("SENSEX")
        rel = _stock("RELIANCE")
        groups = partition_spot_steps([adani, nifty, rel, sensex])
        assert groups == (
            (PullStep.INDICES_SPOT, (nifty, sensex)),
            (PullStep.STOCKS_SPOT, (adani, rel)),
        )

    def test_empty_input_yields_no_groups(self) -> None:
        assert partition_spot_steps([]) == ()

    def test_india_vix_rides_indices(self) -> None:
        """India VIX is IDX (reference-only) — it partitions as an index."""
        vix = _index("INDIAVIX")
        groups = partition_spot_steps([vix, _stock("TCS")])
        assert groups[0] == (PullStep.INDICES_SPOT, (vix,))

    def test_reference_only_stock_still_partitions_as_stock(self) -> None:
        """An NTM reference-only EQ name (never F&O) still rides STOCKS_SPOT."""
        ntm = _stock("SUZLON")
        groups = partition_spot_steps([ntm])
        assert groups == ((PullStep.STOCKS_SPOT, (ntm,)),)

    def test_ntm_plus_fno_dual_class_eq_rides_stocks(self) -> None:
        """A dual-class (NTM constituent AND F&O eligible) EQ name is a stock."""
        dual = _stock("RELIANCE")
        groups = partition_spot_steps([_index("NIFTY"), dual])
        assert groups[1] == (PullStep.STOCKS_SPOT, (dual,))

    def test_within_group_input_order_preserved(self) -> None:
        symbols = ["Z", "A", "M", "B"]
        insts = [_stock(sym) for sym in symbols]
        groups = partition_spot_steps(insts)
        assert [i.symbol_name for i in groups[0][1]] == symbols

    def test_deterministic(self) -> None:
        insts = [_index("NIFTY"), _stock("ADANIENT"), _index("SENSEX")]
        assert partition_spot_steps(insts) == partition_spot_steps(insts)

    def test_every_instrument_in_exactly_one_group(self) -> None:
        insts = [_index("NIFTY"), _stock("ADANIENT"), _index("SENSEX"), _stock("TCS")]
        groups = partition_spot_steps(insts)
        flattened = [inst for _step, members in groups for inst in members]
        assert sorted(i.symbol_name for i in flattened) == sorted(i.symbol_name for i in insts)
        assert len(flattened) == len(insts)


class TestStepOrderMonitor:
    """begin/observe semantics — observe-only in production, hard oracle in tests."""

    def test_initial_state(self) -> None:
        monitor = StepOrderMonitor()
        assert monitor.current_step is None
        assert monitor.late_writes == 0

    def test_forward_progress_is_silent(self) -> None:
        monitor = StepOrderMonitor()
        with capture_logs() as logs:
            for step in PullStep:
                monitor.begin_step(step)
        assert logs == []
        assert monitor.current_step is PullStep.STOCKS_OPTIONS

    def test_rebegin_same_step_is_no_violation(self) -> None:
        """An until-clean retry round re-begins its own step — never a violation."""
        monitor = StepOrderMonitor(raise_on_violation=True)
        monitor.begin_step(PullStep.INDICES_FUTURES)
        with capture_logs() as logs:
            monitor.begin_step(PullStep.INDICES_FUTURES)
        assert logs == []
        assert monitor.current_step is PullStep.INDICES_FUTURES

    def test_rank_regression_logs_error(self) -> None:
        monitor = StepOrderMonitor()
        monitor.begin_step(PullStep.STOCKS_SPOT)
        with capture_logs() as logs:
            monitor.begin_step(PullStep.INDICES_SPOT)
        events = [e for e in logs if e["event"] == "pull.step.order_violation"]
        assert len(events) == 1
        assert events[0]["log_level"] == "error"
        assert events[0]["step"] == "indices-spot"
        assert events[0]["current_step"] == "stocks-spot"

    def test_rank_regression_never_rewinds_the_cursor(self) -> None:
        monitor = StepOrderMonitor()
        monitor.begin_step(PullStep.STOCKS_FUTURES)
        monitor.begin_step(PullStep.INDICES_SPOT)  # violation, logged
        assert monitor.current_step is PullStep.STOCKS_FUTURES

    def test_raise_on_violation_raises_step_order_error(self) -> None:
        monitor = StepOrderMonitor(raise_on_violation=True)
        monitor.begin_step(PullStep.INDICES_OPTIONS)
        with pytest.raises(StepOrderError, match="indices-spot.*after.*indices-options"):
            monitor.begin_step(PullStep.INDICES_SPOT)

    def test_step_order_error_is_a_brutex_error(self) -> None:
        assert issubclass(StepOrderError, BruteXError)

    def test_observe_write_current_step_is_silent(self) -> None:
        monitor = StepOrderMonitor()
        monitor.begin_step(PullStep.INDICES_SPOT)
        with capture_logs() as logs:
            monitor.observe_write(PullStep.INDICES_SPOT, symbol="NIFTY")
        assert logs == []
        assert monitor.late_writes == 0

    def test_observe_write_before_any_step_is_silent(self) -> None:
        monitor = StepOrderMonitor()
        with capture_logs() as logs:
            monitor.observe_write(PullStep.STOCKS_OPTIONS)
        assert logs == []
        assert monitor.late_writes == 0

    def test_observe_write_ahead_of_current_is_silent(self) -> None:
        """A write for a FUTURE step is not a LATE write — only below-current warns."""
        monitor = StepOrderMonitor()
        monitor.begin_step(PullStep.INDICES_SPOT)
        with capture_logs() as logs:
            monitor.observe_write(PullStep.STOCKS_OPTIONS)
        assert logs == []
        assert monitor.late_writes == 0

    def test_late_write_warns_and_counts(self) -> None:
        monitor = StepOrderMonitor()
        monitor.begin_step(PullStep.INDICES_SPOT)
        monitor.begin_step(PullStep.STOCKS_SPOT)
        with capture_logs() as logs:
            monitor.observe_write(PullStep.INDICES_SPOT, symbol="NIFTY")
        events = [e for e in logs if e["event"] == "pull.step.late_write"]
        assert len(events) == 1
        assert events[0]["log_level"] == "warning"
        assert events[0]["step"] == "indices-spot"
        assert events[0]["current_step"] == "stocks-spot"
        assert events[0]["symbol"] == "NIFTY"
        assert monitor.late_writes == 1

    def test_late_write_symbol_defaults_none(self) -> None:
        monitor = StepOrderMonitor()
        monitor.begin_step(PullStep.STOCKS_FUTURES)
        with capture_logs() as logs:
            monitor.observe_write(PullStep.INDICES_SPOT)
        events = [e for e in logs if e["event"] == "pull.step.late_write"]
        assert events[0]["symbol"] is None

    def test_late_write_never_raises_even_with_raise_on_violation(self) -> None:
        """observe_write is observe-ONLY — only begin_step is the hard oracle."""
        monitor = StepOrderMonitor(raise_on_violation=True)
        monitor.begin_step(PullStep.STOCKS_SPOT)
        monitor.observe_write(PullStep.INDICES_SPOT)  # must NOT raise
        assert monitor.late_writes == 1

    def test_late_writes_accumulate(self) -> None:
        monitor = StepOrderMonitor()
        monitor.begin_step(PullStep.STOCKS_OPTIONS)
        for _ in range(3):
            monitor.observe_write(PullStep.INDICES_OPTIONS)
        assert monitor.late_writes == 3

    def test_thread_safety_eight_threads(self) -> None:
        """8 concurrent writers never lose a late-write count or corrupt state."""
        monitor = StepOrderMonitor()
        monitor.begin_step(PullStep.STOCKS_OPTIONS)
        per_thread = 250
        barrier = threading.Barrier(8)

        def hammer() -> None:
            barrier.wait()
            for _ in range(per_thread):
                monitor.observe_write(PullStep.INDICES_SPOT)

        threads = [threading.Thread(target=hammer) for _ in range(8)]
        with capture_logs():
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        assert monitor.late_writes == 8 * per_thread
        assert monitor.current_step is PullStep.STOCKS_OPTIONS


class TestBeginPhase:
    """``begin_phase`` — the legal phase-restart rewind (DEC-PULL-SIX-STEP-ORDER-001).

    The dual-interval defensive spot path runs the ordered steps 1→2 once per
    interval phase; without the rewind the minute phase's ``begin_step(1)``
    after the daily phase's rank-2 cursor was a FALSE ``order_violation``.
    """

    def test_before_any_step_is_a_noop(self) -> None:
        monitor = StepOrderMonitor()
        monitor.begin_phase()
        assert monitor.current_step is None

    def test_rewinds_the_cursor_to_none(self) -> None:
        monitor = StepOrderMonitor()
        monitor.begin_step(PullStep.STOCKS_SPOT)
        monitor.begin_phase()
        assert monitor.current_step is None

    def test_legal_phase_restart_does_not_violate(self) -> None:
        """Steps 1→2, phase restart, steps 1→2 again — silent under the hard oracle."""
        monitor = StepOrderMonitor(raise_on_violation=True)
        monitor.begin_step(PullStep.INDICES_SPOT)
        monitor.begin_step(PullStep.STOCKS_SPOT)
        monitor.begin_phase()
        with capture_logs() as logs:
            monitor.begin_step(PullStep.INDICES_SPOT)  # must NOT raise
            monitor.begin_step(PullStep.STOCKS_SPOT)
        assert [e for e in logs if e["event"] == "pull.step.order_violation"] == []
        assert monitor.current_step is PullStep.STOCKS_SPOT

    def test_legal_phase_restart_logs_no_error_on_observe_only_monitor(self) -> None:
        monitor = StepOrderMonitor()
        monitor.begin_step(PullStep.STOCKS_SPOT)
        monitor.begin_phase()
        with capture_logs() as logs:
            monitor.begin_step(PullStep.INDICES_SPOT)
        assert [e for e in logs if e["event"] == "pull.step.order_violation"] == []

    def test_genuine_regression_after_restart_still_raises(self) -> None:
        """The rewind relaxes only the CROSS-phase cursor — within a phase the law holds."""
        monitor = StepOrderMonitor(raise_on_violation=True)
        monitor.begin_step(PullStep.INDICES_SPOT)
        monitor.begin_step(PullStep.STOCKS_SPOT)
        monitor.begin_phase()
        monitor.begin_step(PullStep.STOCKS_SPOT)
        with pytest.raises(StepOrderError, match="indices-spot .* after stocks-spot"):
            monitor.begin_step(PullStep.INDICES_SPOT)

    def test_genuine_regression_after_restart_logs_error(self) -> None:
        monitor = StepOrderMonitor()
        monitor.begin_phase()
        monitor.begin_step(PullStep.STOCKS_SPOT)
        with capture_logs() as logs:
            monitor.begin_step(PullStep.INDICES_SPOT)
        events = [e for e in logs if e["event"] == "pull.step.order_violation"]
        assert len(events) == 1
        assert events[0]["step"] == "indices-spot"
        assert events[0]["current_step"] == "stocks-spot"

    def test_late_writes_survive_the_rewind(self) -> None:
        """``late_writes`` accumulates ACROSS phases — the rewind never resets it."""
        monitor = StepOrderMonitor()
        monitor.begin_step(PullStep.STOCKS_SPOT)
        with capture_logs():
            monitor.observe_write(PullStep.INDICES_SPOT)
        assert monitor.late_writes == 1
        monitor.begin_phase()
        assert monitor.late_writes == 1

    def test_observe_write_after_rewind_is_silent(self) -> None:
        """A rewound (``None``) cursor observes any step silently — no late count."""
        monitor = StepOrderMonitor()
        monitor.begin_step(PullStep.STOCKS_SPOT)
        monitor.begin_phase()
        with capture_logs() as logs:
            monitor.observe_write(PullStep.INDICES_SPOT)
        assert [e for e in logs if e["event"] == "pull.step.late_write"] == []
        assert monitor.late_writes == 0

    def test_thread_safety_concurrent_restarts(self) -> None:
        """8 threads interleaving begin_phase/begin_step/observe_write never corrupt.

        The observe-only monitor under maximal churn: the final state is one of
        the legal cursor values and the late-write counter is bounded by the
        total observe calls (never negative, never over-counted past them).
        """
        monitor = StepOrderMonitor()
        per_thread = 100
        barrier = threading.Barrier(8)

        def hammer(worker: int) -> None:
            barrier.wait()
            for _ in range(per_thread):
                if worker % 2 == 0:
                    monitor.begin_phase()
                    monitor.begin_step(PullStep.INDICES_SPOT)
                else:
                    monitor.begin_step(PullStep.STOCKS_SPOT)
                    monitor.observe_write(PullStep.INDICES_SPOT)

        threads = [threading.Thread(target=hammer, args=(w,)) for w in range(8)]
        with capture_logs():
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        assert monitor.current_step in {None, PullStep.INDICES_SPOT, PullStep.STOCKS_SPOT}
        assert 0 <= monitor.late_writes <= 4 * per_thread
