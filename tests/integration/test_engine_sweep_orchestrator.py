"""Integration test for the full sweep orchestrator end-to-end.

Drives a real multi-instrument × multi-timeframe × multi-direction grid through
:func:`brutex.engine.sweep_orchestrator.run_sweep_grid` with the real
single-slice runner, a real shared :class:`~brutex.engine.sweep_run.ComboDedup`
(Bloom→exact), real per-tuple :class:`~brutex.engine.results_store.ResultsStore`
Parquet sinks, and a real hash-chained :class:`~brutex.orchestrator.audit.AuditWriter`
— the assembled Wave-2a pipeline (CLAUDE.md §4 7-test-types: integration).

Asserts: every tuple-with-bars produces its own Parquet file under the
deterministic layout, the aggregate tally is internally consistent, the audit
chain verifies end-to-end, and the persisted finalists are re-loadable.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.data.models import CandleBar
from brutex.data.option_dedup import OptionDedupIndex
from brutex.engine.results_store import ResultsStore
from brutex.engine.sweep_orchestrator import (
    SweepGridConfig,
    Timeframe,
    iter_sweep_tuples,
    make_store_for,
    run_sweep_grid,
    tuple_store_path,
)
from brutex.engine.sweep_run import ComboDedup
from brutex.orchestrator.audit import AuditWriter, verify_chain
from brutex.strategies.types import Side

_IST = timezone(timedelta(hours=5, minutes=30))


def _bar(day: int, minute: int, o: float, c: float) -> CandleBar:
    ts = (
        datetime(2026, 1, 5, 9, 16, tzinfo=_IST) + timedelta(days=day, minutes=minute)
    ).astimezone(UTC)
    hi = max(o, c) + 0.5
    lo = min(o, c) - 0.5
    return CandleBar(
        timestamp=ts,
        open=Decimal(str(round(o, 2))),
        high=Decimal(str(round(hi, 2))),
        low=Decimal(str(round(lo, 2))),
        close=Decimal(str(round(c, 2))),
        volume=100,
        open_interest=None,
    )


def _trending_bars(n_days: int = 15, per_day: int = 120) -> list[CandleBar]:
    bars: list[CandleBar] = []
    for d in range(n_days):
        price = 100.0
        for m in range(per_day):
            close = price + 0.6 if m % 3 else price - 0.3
            bars.append(_bar(d, m, price, close))
            price = close
    return bars


def test_full_grid_end_to_end(tmp_path: Path) -> None:
    pytest.importorskip("polars")
    bars = _trending_bars()
    grid = SweepGridConfig(
        instruments=["NIFTY", "BANKNIFTY"],
        timeframes=[Timeframe("1minute", 1), Timeframe("5minute", 5)],
        directions=[Side.BUY, Side.SELL],
        k_values=[1],
        condition_bits=[0, 1, 2, 3, 4],
        stop_losses=[80, None],
        take_profits=[50],
        proximities=[None],
        mode="A",
        data_snapshot_hash="snap0001",
        indicator_version="ind-v1",
        code_commit_sha="deadbeef",
        top_k=5,
    )
    # 2·2·2·1·2·1·1 = 16 tuples, all with bars.
    root = tmp_path / "results"
    audit_path = tmp_path / "grid.jsonl"
    dedup = ComboDedup(OptionDedupIndex(capacity=200_000, fpr_target=1e-7))

    completed: list[int] = []
    with AuditWriter.open(audit_path) as audit:
        summary = run_sweep_grid(
            grid,
            bars_for=lambda _i, _t: bars,
            store_for=make_store_for(root, grid),
            dedup=dedup,
            audit=audit,
            on_tuple=lambda _t, s: completed.append(s.persisted),
        )

    assert summary.tuples_total == 16
    assert summary.tuples_run == 16
    assert summary.tuples_skipped_no_bars == 0
    assert len(completed) == 16
    # Aggregate consistency: persisted finalists never exceed ranked combos.
    assert summary.persisted <= summary.ranked
    assert summary.persisted == sum(completed)

    # Every tuple wrote its own deterministic Parquet file, all distinct.
    paths = [tuple_store_path(root, t, grid) for t in iter_sweep_tuples(grid)]
    assert len(set(paths)) == 16
    for path in paths:
        assert path.exists()
    # The persisted rows are re-loadable, and their count matches the tally.
    total_rows = sum(len(ResultsStore.load(p)) for p in paths)
    assert total_rows == summary.persisted

    # The full audit chain verifies: 2 grid lines + 3 per tuple-that-ran.
    assert verify_chain(audit_path) == 2 + 3 * summary.tuples_run
