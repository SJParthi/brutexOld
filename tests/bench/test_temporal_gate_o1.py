"""Temporal-gate cost pins (DEC-SWEEP-TEMPORAL-ROBUSTNESS-001).

The honest cost envelope of the rank-time year fold, machine-checked:

* **B1 — the per-mask fold is O(trades), independent of N_bars**: the sink's
  fast arm is two array indexings + one int add per trade against the
  pre-built ``year_of_bar`` table — a 10× deeper bar window must not move the
  per-call cost past the shared-CI tier ceiling
  (``tests/bench/_o1_tiers.ratio_ceiling``; the strict 1.1× tier applies on
  the M4-Pro runner).
* **B2 — gate-ON vs gate-OFF whole-sweep ratio**: the fold + gate ride a
  metrics tail already O(trades)/mask with heavier work, so the A/B wall ratio
  on the frozen 3-year fixture stays inside the tier ceiling ("flat ≠ free" —
  the fold is REAL work, bounded, never hidden). Measured 2026-07-09
  (sandbox): B1 ratio ≈ 1.0×; B2 ratio ≈ 1.02×.

The gate-OFF ZERO-work proof (builder/sink/predicate never invoked) is the
mechanical spy in ``tests/unit/test_engine_sweep_run_temporal.py``; the exact
17 B/kept ``owned_nbytes`` accounting teeth (B3) live in
``tests/unit/test_engine_sweep_stream_temporal.py`` — deterministic pins, not
timing, so they run in the unit tier.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from pathlib import Path

import pytest
from brutex.data.models import CandleBar
from brutex.data.option_dedup import OptionDedupIndex
from brutex.engine.results_store import ResultsStore
from brutex.engine.sweep_run import (
    ComboDedup,
    _build_temporal_runtime,  # pyright: ignore[reportPrivateUsage]
    run_persisted_sweep,
)
from brutex.metrics.types import ClosedTrade
from brutex.orchestrator.audit import AuditWriter
from brutex.strategies.types import Side

from tests.bench._o1_tiers import ratio_ceiling
from tests.golden.test_sweep_temporal_gate_golden import (
    GOLDEN_TEMPORAL,
    bars_3y,
    golden_config,
)

pytestmark = pytest.mark.usefixtures("_polars")


@pytest.fixture(name="_polars")
def _polars_fixture() -> None:
    pytest.importorskip("polars")


def _best_of(n: int, fn: object) -> float:
    """House best-of-N MIN timing idiom (DEC-CANARY-MINTIME-001)."""
    best = float("inf")
    for _ in range(n):
        start = time.perf_counter()
        fn()  # type: ignore[operator]
        best = min(best, time.perf_counter() - start)
    return best


def _trades_at(bars: Sequence[CandleBar], indices: list[int]) -> list[ClosedTrade]:
    return [
        ClosedTrade(
            pnl_paisa=100 if idx % 2 else -100,
            mfe_paisa=100,
            mae_paisa=100,
            holding_bars=1,
            entry_ts=bars[idx].timestamp,
            exit_ts=bars[idx].timestamp,
            direction=Side.BUY,
        )
        for idx in indices
    ]


def test_b1_sink_fold_is_flat_vs_bar_window_depth() -> None:
    """Per-mask fold cost at FIXED trades/mask must not scale with N_bars."""
    small_bars = bars_3y()
    big_bars = bars_3y() * 10  # 10× the window depth (timestamps reused — the
    # census only feeds judged years; the fold indexes year_of_bar directly)
    small_runtime = _build_temporal_runtime(small_bars, GOLDEN_TEMPORAL)
    big_runtime = _build_temporal_runtime(big_bars, GOLDEN_TEMPORAL)
    n_trades = 200
    small_indices = [i % len(small_bars) for i in range(n_trades)]
    big_indices = [i % len(small_bars) for i in range(n_trades)]  # same targets
    small_trades = _trades_at(small_bars, small_indices)
    big_trades = _trades_at(big_bars, big_indices)

    def _small() -> None:
        for _ in range(50):
            small_runtime.sink(1, small_trades, small_indices)
            small_runtime.take_last(1)

    def _big() -> None:
        for _ in range(50):
            big_runtime.sink(1, big_trades, big_indices)
            big_runtime.take_last(1)

    small_t = _best_of(5, _small)
    big_t = _best_of(5, _big)
    assert big_t / small_t <= ratio_ceiling(3.0), (small_t, big_t)


def test_b2_gate_on_vs_off_whole_sweep_ratio(tmp_path: Path) -> None:
    """The gate-ON sweep (fold + predicates + capture) stays within the tier
    ceiling of the gate-OFF sweep on the frozen fixture — bounded, disclosed
    cost; never claimed free."""
    bars = bars_3y()
    sequence = iter(range(1_000))

    def _sweep(tag: str, temporal: object) -> None:
        # Unique artifact paths per invocation (the audit chain refuses to
        # overwrite an existing file — deliberately, Golden Rule #8).
        unique = f"{tag}{next(sequence)}"
        store = ResultsStore(path=tmp_path / f"{unique}.parquet")
        dedup = ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6))
        with AuditWriter.open(tmp_path / f"{unique}.jsonl") as audit:
            run_persisted_sweep(
                bars,
                golden_config(temporal=temporal),  # type: ignore[arg-type]
                store=store,
                dedup=dedup,
                audit=audit,
            )

    off_t = _best_of(3, lambda: _sweep("off", None))
    on_t = _best_of(3, lambda: _sweep("on", GOLDEN_TEMPORAL))
    assert on_t / off_t <= ratio_ceiling(3.0), (off_t, on_t)


def _display_row(seed: int) -> object:
    """A minimal temporal-columned ResultRow for the B4 drill bench."""
    from decimal import Decimal

    from brutex.engine.period_stats import encode_judged_years, encode_period_stats
    from brutex.engine.results_store import ResultRow

    return ResultRow(
        run_id=f"{seed:064x}",
        instrument="NIFTY",
        tf="5m",
        direction="bull",
        mode="A",
        mask_bits=1,
        net_pnl_paisa=300,
        gross_profit_paisa=500,
        gross_loss_paisa=200,
        max_drawdown_paisa=300,
        expectancy_paisa=100,
        total_trades=3,
        winning_trades=1,
        losing_trades=2,
        sharpe_ratio=1.0,
        sortino_ratio=1.2,
        calmar_ratio=0.9,
        profit_factor=2.5,
        win_rate=0.33,
        max_drawdown_pct=0.05,
        avg_entry_price=Decimal("100.00"),
        avg_exit_price=Decimal("101.00"),
        stats_by_year_json=encode_period_stats({"2021": (3, 300, 1, 300)}),
        stats_by_quarter_json=encode_period_stats(
            {"2021Q1": (2, 500, 1, 100), "2021Q3": (1, -200, 0, 200)}
        ),
        stats_by_month_json=encode_period_stats(
            {"2021-01": (2, 500, 1, 100), "2021-07": (1, -200, 0, 200)}
        ),
        temporal_judged_years_json=encode_judged_years([2021]),
    )


def test_b4_stored_drill_is_flat_vs_store_row_count() -> None:
    """B4 — one quarter/month drill click is FLAT vs store row count.

    The endpoint's per-click work (after the per-revision scan/index cache) is
    ONE dict lookup + ONE row's decode — independent of how many finalist rows
    the store holds. Benched at 200 vs 2,000 indexed rows (10×); the
    per-revision index BUILD is deliberately OUTSIDE the timed region (paid
    once per revision — the disclosed envelope, never per click).
    Measured 2026-07-09 (sandbox): small ≈ 15.6 µs, big ≈ 15.5 µs — ratio ≈ 0.99.
    """
    from brutex.dashboard.sweep_temporal_view import stored_horizon_payload

    small_index = {row.run_id: row for row in (_display_row(s) for s in range(200))}  # type: ignore[attr-defined]
    big_index = {row.run_id: row for row in (_display_row(s) for s in range(2_000))}  # type: ignore[attr-defined]
    target = f"{150:064x}"

    def _click(index: dict[str, object]) -> None:
        for horizon in ("quarter", "month"):
            payload = stored_horizon_payload(
                index.get(target),  # type: ignore[arg-type]
                run_id=target,
                horizon=horizon,
            )
            assert payload["status"] == "ok"

    small_t = _best_of(7, lambda: _click(small_index))
    big_t = _best_of(7, lambda: _click(big_index))
    assert big_t / small_t <= ratio_ceiling(3.0), (small_t, big_t)
