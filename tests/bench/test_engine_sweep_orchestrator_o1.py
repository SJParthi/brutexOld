"""O(1) benchmarks for the sweep orchestrator (`brutex.engine.sweep_orchestrator`).

Two per-operation invariants (CLAUDE.md §2 r5 / §12 / Golden Rule #10):

* :func:`count_sweep_tuples` is O(1) — a pure product of the 7 axis cardinalities,
  independent of how many tuples the product represents. A grid that enumerates
  ~1M tuples costs the same 7 multiplications as one enumerating ~1K.
* :func:`iter_sweep_tuples`'s per-step ``next`` is O(1) — an
  :func:`itertools.product` step, independent of the grid size.

Routed through the single-source-of-truth :func:`tests.bench._o1_tiers.ratio_ceiling`
(5× on shared CI, the strict 1.1× tier on the M4-Pro runner), mirroring
``tests/bench/test_engine_sweep_run_o1.py``. Honest envelope (Golden Rule #10):
this benches the PER-OP cost only — running a whole grid is inherently O(T) tuples
× O(N) masks, never O(1) total.
"""

from __future__ import annotations

import time

from brutex.engine.sweep_orchestrator import (
    SweepGridConfig,
    Timeframe,
    count_sweep_tuples,
    iter_sweep_tuples,
)
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
        # Synthetic SYM{i} names by test intent (an O(1) bench needs a wide
        # instrument axis) — opt out of the Layer-1 engine-scope invariant
        # (DEC-ENGINE-SCOPE-GUARD-001).
        unsafe_allow_out_of_scope_instruments=True,
    )


class TestCountIsO1:
    def test_count_small(self, benchmark) -> None:  # type: ignore[no-untyped-def]
        grid = _grid(_SMALL_TUPLES)
        assert benchmark(lambda: count_sweep_tuples(grid)) == _SMALL_TUPLES

    def test_count_large(self, benchmark) -> None:  # type: ignore[no-untyped-def]
        grid = _grid(_LARGE_TUPLES)
        assert benchmark(lambda: count_sweep_tuples(grid)) == _LARGE_TUPLES

    def test_count_ratio_is_o1(self) -> None:
        """count cost at 1M vs 1K tuples must stay O(1) (< ceiling)."""

        def _time(n: int, *, iterations: int = 20_000) -> float:
            grid = _grid(n)
            for _ in range(100):  # warm
                count_sweep_tuples(grid)
            t0 = time.perf_counter_ns()
            for _ in range(iterations):
                count_sweep_tuples(grid)
            t1 = time.perf_counter_ns()
            return (t1 - t0) / iterations

        t_small = _time(_SMALL_TUPLES)
        t_large = _time(_LARGE_TUPLES)
        ratio = t_large / max(t_small, 1.0)
        assert ratio < ratio_ceiling(5.0), (
            f"count_sweep_tuples ratio {ratio:.2f}× (1M vs 1K tuples) exceeds 5× — "
            f"it must be a pure O(1) product. t_1k={t_small:.0f}ns, t_1m={t_large:.0f}ns."
        )


class TestIterStepIsO1:
    def test_iter_step_ratio_is_o1(self) -> None:
        """Per-tuple `next()` latency at a 1M-grid vs a 1K-grid must stay O(1)."""

        def _time_step(n: int, *, steps: int = 500) -> float:
            it = iter_sweep_tuples(_grid(n))
            for _ in range(50):  # warm (50 + 500 < the 1K small-grid size)
                next(it)
            t0 = time.perf_counter_ns()
            for _ in range(steps):
                next(it)
            t1 = time.perf_counter_ns()
            return (t1 - t0) / steps

        t_small = _time_step(_SMALL_TUPLES)
        t_large = _time_step(_LARGE_TUPLES)
        ratio = t_large / max(t_small, 1.0)
        assert ratio < ratio_ceiling(5.0), (
            f"iter_sweep_tuples per-step ratio {ratio:.2f}× (1M vs 1K grid) exceeds 5× — "
            f"a product step must be O(1). t_1k={t_small:.0f}ns, t_1m={t_large:.0f}ns."
        )
