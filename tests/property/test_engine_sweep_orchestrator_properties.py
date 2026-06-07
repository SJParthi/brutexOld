"""Property invariants for the sweep orchestrator (`brutex.engine.sweep_orchestrator`).

Hypothesis-driven invariants (CLAUDE.md §4 7-test-types / Golden Rule #4):

* **Count == iteration length.** :func:`count_sweep_tuples` (the O(1) product)
  always equals the number of tuples :func:`iter_sweep_tuples` actually yields.
* **Enumeration is deterministic.** Two iterations of the same grid yield the
  identical tuple sequence.
* **Paths are injective per distinct tuple.** Distinct tuples of one grid map to
  distinct :func:`tuple_store_path` files (the silent-overwrite guard); the same
  tuple always maps to the same path.
* **Axis de-dup is idempotent.** Re-wrapping an already-deduped axis is a no-op,
  and a repeated axis value never inflates the tuple count.
"""

from __future__ import annotations

from pathlib import Path

from brutex.engine.sweep_orchestrator import (
    SweepGridConfig,
    Timeframe,
    count_sweep_tuples,
    iter_sweep_tuples,
    tuple_store_path,
)
from brutex.strategies.types import Side
from hypothesis import given
from hypothesis import strategies as st

_INSTRUMENTS = ["NIFTY", "BANKNIFTY", "SENSEX"]
_TFS = [Timeframe("1minute", 1), Timeframe("5minute", 5), Timeframe("15minute", 15)]
_DIRECTIONS = [Side.BUY, Side.SELL]


def _grid_strategy() -> st.SearchStrategy[SweepGridConfig]:
    return st.builds(
        SweepGridConfig,
        instruments=st.lists(st.sampled_from(_INSTRUMENTS), min_size=1, max_size=3),
        timeframes=st.lists(st.sampled_from(_TFS), min_size=1, max_size=3),
        directions=st.lists(st.sampled_from(_DIRECTIONS), min_size=1, max_size=2),
        k_values=st.lists(st.integers(min_value=0, max_value=3), min_size=1, max_size=3),
        condition_bits=st.just([0, 1, 2, 3]),
        stop_losses=st.lists(
            st.one_of(st.none(), st.integers(min_value=1, max_value=200)),
            min_size=1,
            max_size=2,
        ),
        take_profits=st.lists(
            st.one_of(st.none(), st.integers(min_value=1, max_value=200)),
            min_size=1,
            max_size=2,
        ),
        proximities=st.just([None]),
        mode=st.just("A"),
        data_snapshot_hash=st.just("snap0001"),
        indicator_version=st.just("ind-v1"),
        code_commit_sha=st.just("deadbeef"),
    )


@given(grid=_grid_strategy())
def test_count_equals_iteration_length(grid: SweepGridConfig) -> None:
    assert count_sweep_tuples(grid) == len(list(iter_sweep_tuples(grid)))


@given(grid=_grid_strategy())
def test_enumeration_is_deterministic(grid: SweepGridConfig) -> None:
    assert list(iter_sweep_tuples(grid)) == list(iter_sweep_tuples(grid))


@given(grid=_grid_strategy())
def test_paths_are_injective_per_distinct_tuple(grid: SweepGridConfig) -> None:
    root = Path("/tmp/brutex-prop-paths")
    tuples = list(iter_sweep_tuples(grid))
    paths = [tuple_store_path(root, t, grid) for t in tuples]
    # Distinct tuples → distinct paths (no two grid points share a file).
    assert len(set(paths)) == len(tuples)
    # And the mapping is stable.
    assert [tuple_store_path(root, t, grid) for t in tuples] == paths


@given(
    instruments=st.lists(st.sampled_from(_INSTRUMENTS), min_size=1, max_size=6),
    k_values=st.lists(st.integers(min_value=0, max_value=3), min_size=1, max_size=6),
)
def test_axis_dedup_collapses_repeats(instruments: list[str], k_values: list[int]) -> None:
    grid = SweepGridConfig(
        instruments=instruments,
        timeframes=[Timeframe("1minute", 1)],
        directions=[Side.BUY],
        k_values=k_values,
        condition_bits=[0, 1, 2, 3],
        stop_losses=[80],
        take_profits=[50],
        proximities=[None],
        mode="A",
        data_snapshot_hash="snap0001",
        indicator_version="ind-v1",
        code_commit_sha="deadbeef",
    )
    # Deduped axes carry no repeats.
    assert len(grid.instruments) == len(set(grid.instruments))
    assert len(grid.k_values) == len(set(grid.k_values))
    # Count reflects the deduped cardinalities.
    assert count_sweep_tuples(grid) == len(set(instruments)) * len(set(k_values))
