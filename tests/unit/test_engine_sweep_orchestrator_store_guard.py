"""Unit suite for the ``TupleStoreProvider`` refactor (DEC-SWEEP-RESUME-STORE-GUARD-001).

T13 of the fix matrix — the ``make_store_for`` back-compat law: the returned
provider still satisfies ``Callable[[SweepTuple], ResultsStore]`` byte-identically
(the load-or-create semantics of the bare lambda it replaces), and additionally
exposes the ``store_path`` seam whose answer equals :func:`tuple_store_path` —
the auto-activation keystone for the supervisor's resume store guard.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from brutex.engine.results_store import ResultRow, ResultsStore
from brutex.engine.sweep_orchestrator import (
    SweepGridConfig,
    Timeframe,
    TupleStoreProvider,
    iter_sweep_tuples,
    make_store_for,
    tuple_store_path,
)
from brutex.strategies.types import Side

if TYPE_CHECKING:
    from pathlib import Path


def _grid() -> SweepGridConfig:
    return SweepGridConfig(
        instruments=["NIFTY"],
        timeframes=[Timeframe("1minute", 1)],
        directions=[Side.BUY],
        k_values=[1],
        condition_bits=[0, 1, 2],
        stop_losses=[80],
        take_profits=[50],
        proximities=[None],
        mode="A",
        data_snapshot_hash="snap0001",
        indicator_version="ind-v1",
        code_commit_sha="deadbeef",
    )


def _row(run_id: str) -> ResultRow:
    return ResultRow(
        run_id=run_id,
        instrument="NIFTY",
        tf="1minute",
        direction="bull",
        mode="A",
        mask_bits=1,
        net_pnl_paisa=100,
        gross_profit_paisa=150,
        gross_loss_paisa=50,
        max_drawdown_paisa=10,
        expectancy_paisa=5,
        total_trades=20,
        winning_trades=12,
        losing_trades=8,
        sharpe_ratio=1.0,
        sortino_ratio=1.0,
        calmar_ratio=1.0,
        profit_factor=3.0,
        win_rate=0.6,
        max_drawdown_pct=0.01,
        avg_entry_price=Decimal("100.00"),
        avg_exit_price=Decimal("101.00"),
    )


class TestTupleStoreProvider:
    def test_make_store_for_returns_a_provider_that_is_still_callable(self, tmp_path: Path) -> None:
        grid = _grid()
        provider = make_store_for(tmp_path, grid)
        assert isinstance(provider, TupleStoreProvider)
        assert callable(provider)

    def test_store_path_equals_tuple_store_path(self, tmp_path: Path) -> None:
        grid = _grid()
        provider = TupleStoreProvider(root=tmp_path, grid=grid)
        tuple_ = next(iter_sweep_tuples(grid))
        assert provider.store_path(tuple_) == tuple_store_path(tmp_path, tuple_, grid)

    def test_call_creates_a_fresh_store_at_the_deterministic_path(self, tmp_path: Path) -> None:
        grid = _grid()
        provider = make_store_for(tmp_path, grid)
        tuple_ = next(iter_sweep_tuples(grid))
        store = provider(tuple_)
        assert isinstance(store, ResultsStore)
        assert store.path == tuple_store_path(tmp_path, tuple_, grid)

    def test_call_loads_an_existing_store_the_lambda_semantics(self, tmp_path: Path) -> None:
        """Load-or-create byte-identical to the replaced lambda: an existing
        Parquet is LOADED (rows preserved), never clobbered by a fresh store."""
        grid = _grid()
        provider = make_store_for(tmp_path, grid)
        tuple_ = next(iter_sweep_tuples(grid))
        first = provider(tuple_)
        first.append(_row("a" * 64))
        first.flush()
        again = provider(tuple_)
        assert len(again) == 1  # loaded, not re-created empty
