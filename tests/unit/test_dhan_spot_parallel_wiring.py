"""Unit tests for the parallel spot-ingest wiring (DEC-DHAN-PARALLEL-INGEST-001).

Deterministic, synchronous-fake tests for the OUTER ``(instrument, interval)``
fan-out in :func:`brutex.adapters.dhan.run_ingest.run_dhan_spot_ingest`:

* the injected ``run_parallel`` seam is honoured (every pair is dispatched
  through it, exactly once, in the worklist order a synchronous fake sees);
* ``pull_id`` is re-bound INSIDE each worker so the ``ingest.*`` events emitted
  by ``ingest_spot`` keep the pull's correlation id (the contextvar does not
  cross the ThreadPoolExecutor boundary);
* the per-instrument ``ingest.underlying`` rollup is emitted in a DETERMINISTIC
  order (sorted by ``symbol_name``) regardless of worker completion order;
* the ``workers_to_saturate`` sizing table the dashboard uses to width the pool.

No real threads: a synchronous ``run_parallel`` fake (mirrors the one
:func:`brutex.orchestrator.universe_phases.run_two_phase` is tested with) runs
the fetcher inline so the wiring is provable with zero concurrency.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date
from decimal import Decimal

import pytest
from brutex.adapters.dhan import run_ingest as mod
from brutex.adapters.dhan.errors import DhanNotFoundError
from brutex.adapters.dhan.ingest import IngestReport
from brutex.adapters.dhan.run_ingest import run_dhan_spot_ingest
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from brutex.observability.correlation import current_pull_id, pull_id_scope
from brutex.orchestrator.parallel import MAX_FANOUT, workers_to_saturate
from brutex.orchestrator.universe_phases import ItemResult
from structlog.testing import capture_logs

_START = date(2021, 1, 1)
_END = date(2021, 2, 1)


def _index(symbol: str, *, token: int = 1) -> Instrument:
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=token,
        symbol_name=symbol,
        display_name=None,
        instrument=InstrumentType.IDX,
        segment=Segment.CASH,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=0,
        tick_size=Decimal("0"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
    )


def _sync_run_parallel[ItemT, ResultT](
    fn: Callable[[ItemT], ResultT],
    items: Sequence[ItemT],
) -> list[ItemResult[ItemT, ResultT]]:
    """A synchronous ``RunParallel`` fake: run *fn* inline, capture exceptions."""
    out: list[ItemResult[ItemT, ResultT]] = []
    for item in items:
        try:
            value = fn(item)
        except Exception as exc:  # mirror PoolRunParallel's capture
            out.append(ItemResult(item=item, ok=False, result=None, exc=exc))
        else:
            out.append(ItemResult(item=item, ok=True, result=value, exc=None))
    return out


class TestRunParallelSeam:
    """The injected run_parallel dispatches every pair exactly once."""

    def test_every_pair_dispatched_through_injected_runner(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dispatched: list[tuple[str, str]] = []

        def _fake_ingest(adapter, sink, *, instrument, interval, start, end, **_kw):
            return IngestReport(windows=1, bars_appended=3, transient_failures=0)

        monkeypatch.setattr(mod, "ingest_spot", _fake_ingest)

        def _recording_runner(fn, items):
            for item in items:
                dispatched.append((item[0].symbol_name, item[1]))
            return _sync_run_parallel(fn, items)

        report = run_dhan_spot_ingest(
            object(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            instruments=[_index("13"), _index("25")],
            intervals=["1minute", "1day"],
            start=_START,
            end=_END,
            run_parallel=_recording_runner,
        )
        # 2 instruments × 2 intervals → 4 distinct jobs, each dispatched once, in
        # instrument-major / interval-minor worklist order.
        assert dispatched == [
            ("13", "1minute"),
            ("13", "1day"),
            ("25", "1minute"),
            ("25", "1day"),
        ]
        assert len(report.pairs) == 4
        assert report.bars_appended == 12

    def test_default_runner_is_real_pool_when_omitted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No run_parallel → the real bounded PoolRunParallel runs it (threads).
        def _fake_ingest(adapter, sink, *, instrument, interval, start, end, **_kw):
            return IngestReport(windows=1, bars_appended=1, transient_failures=0)

        monkeypatch.setattr(mod, "ingest_spot", _fake_ingest)
        report = run_dhan_spot_ingest(
            object(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            instruments=[_index("13")],
            intervals=["1minute"],
            start=_START,
            end=_END,
        )
        assert report.bars_appended == 1


class TestPullIdPropagation:
    """pull_id is re-bound inside each worker so ingest.* events keep it."""

    def test_pull_id_propagates_into_workers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The fake ingest reads the contextvar the way the real spine does — if
        # the worker did not re-bind it, this would observe "" and the assert
        # would catch the regression of DEC-DHAN-INGEST-LOGGING-001.
        seen_in_worker: list[str] = []

        def _fake_ingest(adapter, sink, *, instrument, interval, start, end, **_kw):
            # Emit an event the way ingest_spot does, stamping the bound id.
            import structlog

            structlog.get_logger("test.ingest").info(
                "ingest.window", pull_id=current_pull_id(), symbol=instrument.symbol_name
            )
            seen_in_worker.append(current_pull_id())
            return IngestReport(windows=1, bars_appended=1, transient_failures=0)

        monkeypatch.setattr(mod, "ingest_spot", _fake_ingest)

        def _runner(fn, items):
            # Simulate a worker that does NOT inherit the controller contextvar:
            # clear it before running fn, so only the worker's own re-bind can
            # restore it. This makes the test prove the re-bind, not inheritance.
            import contextvars

            results = []
            for item in items:
                ctx = contextvars.Context()  # a fresh context (no bound pull_id)
                results.append(ctx.run(lambda it=item: _one(fn, it)))
            return results

        def _one(fn, item):
            try:
                value = fn(item)
            except Exception as exc:
                return ItemResult(item=item, ok=False, result=None, exc=exc)
            return ItemResult(item=item, ok=True, result=value, exc=None)

        with capture_logs() as logs, pull_id_scope("propagate123"):
            run_dhan_spot_ingest(
                object(),  # type: ignore[arg-type]
                object(),  # type: ignore[arg-type]
                instruments=[_index("13"), _index("25")],
                intervals=["1minute"],
                start=_START,
                end=_END,
                run_parallel=_runner,
            )
        # Every worker observed the bound id (re-bound inside the worker scope).
        assert seen_in_worker == ["propagate123", "propagate123"]
        # And every ingest.* event captured carries it.
        windows = [e for e in logs if e["event"] == "ingest.window"]
        assert len(windows) == 2
        assert all(e["pull_id"] == "propagate123" for e in windows)


class TestDeterministicRollup:
    """ingest.underlying rolls up per instrument in symbol-sorted order."""

    def test_rollup_order_is_sorted_not_completion_order(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _fake_ingest(adapter, sink, *, instrument, interval, start, end, **_kw):
            return IngestReport(windows=2, bars_appended=10, transient_failures=0)

        monkeypatch.setattr(mod, "ingest_spot", _fake_ingest)

        def _reversed_completion_runner(fn, items):
            # Complete in REVERSE worklist order — the rollup must still be
            # emitted in symbol-sorted order (Rule #4 reproducibility).
            return list(reversed(_sync_run_parallel(fn, items)))

        with capture_logs() as logs, pull_id_scope("rollup0001"):
            run_dhan_spot_ingest(
                object(),  # type: ignore[arg-type]
                object(),  # type: ignore[arg-type]
                instruments=[_index("ZEE"), _index("AXIS"), _index("MARUTI")],
                intervals=["1minute"],
                start=_START,
                end=_END,
                run_parallel=_reversed_completion_runner,
            )
        rollups = [e for e in logs if e["event"] == "ingest.underlying"]
        assert [e["symbol"] for e in rollups] == ["AXIS", "MARUTI", "ZEE"]
        assert all(e["pull_id"] == "rollup0001" for e in rollups)
        assert all(e["series_completed"] == 1 for e in rollups)
        assert all(e["bars_appended"] == 10 for e in rollups)

    def test_quarantined_pair_counted_in_its_rollup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _fake_ingest(adapter, sink, *, instrument, interval, start, end, **_kw):
            if instrument.symbol_name == "BAD":
                raise DhanNotFoundError("HTTP 404")
            return IngestReport(windows=1, bars_appended=5, transient_failures=0)

        monkeypatch.setattr(mod, "ingest_spot", _fake_ingest)
        with capture_logs() as logs:
            report = run_dhan_spot_ingest(
                object(),  # type: ignore[arg-type]
                object(),  # type: ignore[arg-type]
                instruments=[_index("GOOD"), _index("BAD", token=9)],
                intervals=["1minute"],
                start=_START,
                end=_END,
                run_parallel=_sync_run_parallel,
            )
        assert report.failed_pairs == 1
        rollups = {e["symbol"]: e for e in logs if e["event"] == "ingest.underlying"}
        assert rollups["BAD"]["series_failed"] == 1
        assert rollups["BAD"]["series_completed"] == 0
        assert rollups["GOOD"]["series_completed"] == 1
        assert rollups["GOOD"]["series_failed"] == 0


class TestWorkersToSaturateSizing:
    """The pool-sizing table the dashboard uses to width the fan-out (Rule #6)."""

    def test_sizing_table(self) -> None:
        # rate × latency, clamped to [1, MAX_FANOUT].
        assert workers_to_saturate(5.0, 3.0) == 15  # 5 r/s × 3 s = 15 in flight
        assert workers_to_saturate(5.0, 1.0) == 5  # 5 r/s × 1 s = 5
        assert workers_to_saturate(5.0, 10.0) == MAX_FANOUT  # 50 → capped at 32
        assert MAX_FANOUT == 32
        assert workers_to_saturate(0.0, 3.0) == 1  # floor: always ≥ 1 worker
        assert workers_to_saturate(5.0, 0.0) == 1  # latency ≤ 0 → floor 1
