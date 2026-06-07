"""O(1) benchmark for the sweep supervisor (`brutex.engine.sweep_supervisor`).

Per-operation invariant (CLAUDE.md §2 r5 / §12 / Golden Rule #10):

* :func:`tuple_cache_key` — the supervisor's per-tuple content-addressed dedup
  key — is O(1) in the GRID SIZE: building one tuple's key costs the same whether
  the grid enumerates ~1K or ~1M tuples (the key depends only on the single tuple
  plus the grid's fixed-width scalar context + the bounded ≤52 condition-bit set,
  never on how many tuples the product holds).

Routed through the single-source-of-truth :func:`tests.bench._o1_tiers.ratio_ceiling`
(5× on shared CI, the strict 1.1× tier on the M4-Pro runner), mirroring
``tests/bench/test_engine_sweep_orchestrator_o1.py``. Honest envelope (Golden
Rule #10): this benches the PER-OP cost only — a supervised grid is inherently
O(T) tuples × O(N) masks plus O(T) flushes/fsyncs, never O(1) total.
"""

from __future__ import annotations

import time

from brutex.engine.sweep_orchestrator import SweepGridConfig, Timeframe, iter_sweep_tuples
from brutex.engine.sweep_supervisor import tuple_cache_key
from brutex.strategies.types import Side

from tests.bench._o1_tiers import ratio_ceiling

_SMALL_TUPLES = 1_000
_LARGE_TUPLES = 1_000_000


def _grid(n_instruments: int) -> SweepGridConfig:
    """A grid whose tuple count == n_instruments (all other axes are singletons)."""
    return SweepGridConfig(
        instruments=[f"SYM{i}" for i in range(n_instruments)],
        timeframes=[Timeframe("1minute", 1)],
        directions=[Side.BUY],
        k_values=[1],
        condition_bits=[0, 1, 2, 3, 4],
        stop_losses=[80],
        take_profits=[50],
        proximities=[None],
        mode="A",
        data_snapshot_hash="snap0001",
        indicator_version="ind-v1",
        code_commit_sha="deadbeef",
        max_tuples=2_000_000,
    )


class TestTupleCacheKeyIsO1:
    def test_key_small(self, benchmark) -> None:  # type: ignore[no-untyped-def]
        grid = _grid(_SMALL_TUPLES)
        first = next(iter_sweep_tuples(grid))
        assert len(benchmark(lambda: tuple_cache_key(first, grid))) == 32

    def test_key_large(self, benchmark) -> None:  # type: ignore[no-untyped-def]
        grid = _grid(_LARGE_TUPLES)
        first = next(iter_sweep_tuples(grid))
        assert len(benchmark(lambda: tuple_cache_key(first, grid))) == 32

    def test_key_ratio_is_o1(self) -> None:
        """tuple_cache_key cost at a 1M-grid vs a 1K-grid must stay O(1) (< ceiling)."""

        def _time(n: int, *, iterations: int = 20_000) -> float:
            grid = _grid(n)
            first = next(iter_sweep_tuples(grid))
            for _ in range(100):  # warm
                tuple_cache_key(first, grid)
            t0 = time.perf_counter_ns()
            for _ in range(iterations):
                tuple_cache_key(first, grid)
            t1 = time.perf_counter_ns()
            return (t1 - t0) / iterations

        t_small = _time(_SMALL_TUPLES)
        t_large = _time(_LARGE_TUPLES)
        ratio = t_large / max(t_small, 1.0)
        assert ratio < ratio_ceiling(5.0), (
            f"tuple_cache_key ratio {ratio:.2f}× (1M vs 1K grid) exceeds 5× — the "
            f"per-tuple key must be O(1) in grid size. t_1k={t_small:.0f}ns, t_1m={t_large:.0f}ns."
        )
