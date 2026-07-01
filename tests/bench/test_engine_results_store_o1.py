"""O(1) benchmarks for the results store (`brutex.engine.results_store`).

Enforces the O(1) decree (CLAUDE.md §2 r5 / §12) for the store's two hot paths:

* :meth:`ResultsStore.get` — run-identity lookup must be latency-independent of
  how many rows are stored (a dict probe, not a scan);
* :meth:`ResultsStore.append` — per-row insert must be amortised O(1)
  (dict membership + insert), independent of the current row count.

Mirrors ``tests/bench/test_lmdb_membership_o1.py``: empirical ratio at 10k vs 1k
with CI-jitter headroom (5× on shared CI). ``get`` (a dict probe on an
already-resident store) keeps the raw strict 1.1× via :func:`ratio_ceiling`.
``append`` routes its M4-Pro strict ceiling through
:func:`tests.bench._o1_tiers.cache_tier_ceiling` (the DEC-O1-CACHE-TIER-001
measured 3× cache allowance): inserting fresh rows into a 10k-deep store touches
a larger allocated working set than a 1k-deep one, so the amortised insert
crosses a CPU cache tier into DRAM (~1.3–2.5× measured, a cache-cliff not an
O(n) regression) — the same allowance the throughput canary routes its 1M-elem
paths through (DEC-O1-BENCH-GATE-COMPLETE-001). A scan regression would blow the
ratio well past 3× and fail.
"""

from __future__ import annotations

import time
from decimal import Decimal
from pathlib import Path

from brutex.engine.results_store import ResultRow, ResultsStore

from tests.bench._o1_tiers import cache_tier_ceiling, ratio_ceiling

_SMALL = 1_000
_LARGE = 10_000


def _rid(seed: int) -> str:
    return f"{seed:064x}"


def _row(seed: int) -> ResultRow:
    return ResultRow(
        run_id=_rid(seed),
        instrument="NIFTY",
        tf="5m",
        direction="bull",
        mode="A",
        mask_bits=seed % (1 << 52),
        net_pnl_paisa=seed,
        gross_profit_paisa=seed,
        gross_loss_paisa=0,
        max_drawdown_paisa=0,
        expectancy_paisa=0,
        total_trades=1,
        winning_trades=1,
        losing_trades=0,
        sharpe_ratio=float(seed % 7),
        sortino_ratio=0.0,
        calmar_ratio=0.0,
        profit_factor=1.0,
        win_rate=1.0,
        max_drawdown_pct=0.0,
        avg_entry_price=Decimal("1.00"),
        avg_exit_price=Decimal("2.00"),
    )


def _filled(n: int) -> ResultsStore:
    store = ResultsStore(path=Path("unused.parquet"))
    store.extend(_row(i) for i in range(n))
    return store


class TestGetIsO1:
    def test_get_at_small_scale(self, benchmark) -> None:  # type: ignore[no-untyped-def]
        store = _filled(_SMALL)
        probe = _rid(_SMALL // 2)
        result = benchmark(lambda: store.get(probe))
        assert result is not None

    def test_get_at_large_scale(self, benchmark) -> None:  # type: ignore[no-untyped-def]
        store = _filled(_LARGE)
        probe = _rid(_LARGE // 2)
        result = benchmark(lambda: store.get(probe))
        assert result is not None

    def test_get_ratio_is_o1(self) -> None:
        """`get` latency at 10k / 1k rows must stay O(1) (< ceiling)."""

        def _time(n: int, *, iterations: int = 5_000) -> float:
            store = _filled(n)
            probe = _rid(n // 2)
            for _ in range(50):  # warm
                store.get(probe)
            t0 = time.perf_counter_ns()
            for _ in range(iterations):
                store.get(probe)
            t1 = time.perf_counter_ns()
            return (t1 - t0) / iterations

        t_small = _time(_SMALL)
        t_large = _time(_LARGE)
        ratio = t_large / max(t_small, 1.0)
        assert ratio < ratio_ceiling(5.0), (
            f"ResultsStore.get ratio {ratio:.2f}× (10k vs 1k) exceeds 5× — "
            f"lookup may have regressed to a scan. "
            f"t_1k={t_small:.0f}ns, t_10k={t_large:.0f}ns."
        )


class TestAppendIsO1:
    def test_append_dup_noop_at_small_scale(self, benchmark) -> None:  # type: ignore[no-untyped-def]
        store = _filled(_SMALL)
        existing = _row(_SMALL // 2)
        result = benchmark(lambda: store.append(existing))
        assert result is False

    def test_append_dup_noop_at_large_scale(self, benchmark) -> None:  # type: ignore[no-untyped-def]
        store = _filled(_LARGE)
        existing = _row(_LARGE // 2)
        result = benchmark(lambda: store.append(existing))
        assert result is False

    def test_append_ratio_is_o1(self) -> None:
        """Inserting fresh rows costs the same at 10k base as at 1k base (O(1))."""

        def _time(n: int, *, iterations: int = 2_000) -> float:
            store = _filled(n)
            # Pre-build fresh rows (ids beyond n) so we time append, not validation.
            fresh = [_row(n + i) for i in range(iterations)]
            t0 = time.perf_counter_ns()
            for row in fresh:
                store.append(row)
            t1 = time.perf_counter_ns()
            return (t1 - t0) / iterations

        t_small = _time(_SMALL)
        t_large = _time(_LARGE)
        ratio = t_large / max(t_small, 1.0)
        assert ratio < cache_tier_ceiling(5.0), (
            f"ResultsStore.append ratio {ratio:.2f}× (10k vs 1k base) exceeds 5× — "
            f"insert may have regressed from amortised O(1). "
            f"t_1k={t_small:.0f}ns, t_10k={t_large:.0f}ns."
        )
