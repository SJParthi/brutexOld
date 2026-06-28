"""End-to-end test for :class:`brutex.execution.driver.BacktestDriver`.

Wires a real Parquet lake + DuckDB catalog + :class:`ReplayFeed` +
:class:`ExecutionSimulator` and drives a concrete strategy
through the full backtest loop. Verifies that the returned
:class:`BacktestResult` has consistent fills, positions, equity
curve, and cash accounting.

Skipped when the ``[data]`` extras are absent.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.data.catalog import Catalog
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink
from brutex.execution.driver import BacktestDriver
from brutex.execution.simulator import ExecutionSimulator
from brutex.execution.types import InstrumentMeta
from brutex.feed.replay import ReplayFeed
from brutex.models import InstrumentType, Segment
from brutex.strategies.library import BuyAndHold, ClosePreviousMomentum
from brutex.strategies.types import Side

pytest.importorskip("polars")
pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")


# ────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────


def _bar(day: int, minute: int, *, close: float) -> CandleBar:
    return CandleBar(
        timestamp=datetime(2024, 1, day, 9, minute, 0, tzinfo=UTC),
        open=Decimal(str(close)),
        high=Decimal(str(close + 1)),
        low=Decimal(str(close - 1)),
        close=Decimal(str(close)),
        volume=100,
        open_interest=0,
    )


def _seed_lake(tmp_path: Path, closes: Sequence[float]) -> tuple[Path, Path]:
    lake_root = tmp_path / "lake"
    catalog_path = tmp_path / "catalog.duckdb"
    writer = ParquetWriter(lake_root=lake_root)
    bars = [_bar(1, 15 + i, close=c) for i, c in enumerate(closes)]
    with Catalog.open(catalog_path) as cat:
        sink = ParquetBarSink(writer=writer, catalog=cat)
        sink.append(
            exchange="NSE",
            segment="FNO",
            symbol="NSE-NIFTY",
            interval="1minute",
            bars=bars,
        )
    return lake_root, catalog_path


def _nifty_meta() -> InstrumentMeta:
    # Treat NSE-NIFTY as a cash-equivalent index for this test \u2014 we
    # don't need the full FNO lot-size / freeze plumbing here and the
    # concrete strategies trade "NSE-NIFTY" as their symbol.
    return InstrumentMeta(
        symbol="NSE-NIFTY",
        segment=Segment.CASH,
        instrument_type=InstrumentType.IDX,
        lot_size=1,
        freeze_quantity=10_000,
    )


def _make_feed(catalog: Catalog, lake_root: Path) -> ReplayFeed:
    return ReplayFeed(
        catalog=catalog,
        lake_root=lake_root,
        exchange="NSE",
        segment="FNO",
        symbols=["NSE-NIFTY"],
        interval="1minute",
        start=datetime(2024, 1, 1, tzinfo=UTC),
        end=datetime(2024, 1, 31, tzinfo=UTC),
    )


# ────────────────────────────────────────────────────────────────────────
# Happy-path backtest
# ────────────────────────────────────────────────────────────────────────


class TestBacktestHappyPath:
    def test_buy_and_hold_produces_one_fill_and_positive_pnl(
        self,
        tmp_path: Path,
    ) -> None:
        """BuyAndHold on a rising market \u2014 one BUY fill, long position,
        positive unrealized P&L at the end."""
        closes = [100, 102, 104, 106, 108]
        lake_root, catalog_path = _seed_lake(tmp_path, closes)
        with Catalog.open(catalog_path) as cat:
            feed = _make_feed(cat, lake_root)
            sim = ExecutionSimulator(
                initial_cash=Decimal("10000"),
                instruments={"NSE-NIFTY": _nifty_meta()},
                default_qty=10,
                slippage_bps=Decimal("0"),
            )
            driver = BacktestDriver(
                strategy=BuyAndHold(),
                feed=feed,
                simulator=sim,
            )
            result = driver.run()

        assert len(result.fills) == 1
        assert result.fills[0].side == Side.BUY
        assert result.fills[0].qty == 10

        # Long at ~100 (plus costs), marked to 108 \u2192 unrealized ~80.
        nifty = result.final_positions["NSE-NIFTY"]
        assert nifty.qty == 10
        assert nifty.avg_price == Decimal("100")
        assert nifty.unrealized_pnl == Decimal("80")
        # Equity curve has one point per in-scope bar.
        assert len(result.equity_curve) == 5

    def test_equity_curve_tracks_positions_value(self, tmp_path: Path) -> None:
        """Final equity point reflects cash plus marked position value."""
        closes = [100, 110]
        lake_root, catalog_path = _seed_lake(tmp_path, closes)
        with Catalog.open(catalog_path) as cat:
            feed = _make_feed(cat, lake_root)
            sim = ExecutionSimulator(
                initial_cash=Decimal("10000"),
                instruments={"NSE-NIFTY": _nifty_meta()},
                default_qty=1,
                slippage_bps=Decimal("0"),
            )
            driver = BacktestDriver(
                strategy=BuyAndHold(),
                feed=feed,
                simulator=sim,
            )
            result = driver.run()

        last = result.equity_curve[-1]
        assert last.positions_value == Decimal("110")  # 1 share @ 110
        assert last.total_equity == last.cash + last.positions_value

    def test_total_costs_matches_sum_of_fill_costs(self, tmp_path: Path) -> None:
        closes = [100, 101, 102, 103]
        lake_root, catalog_path = _seed_lake(tmp_path, closes)
        with Catalog.open(catalog_path) as cat:
            feed = _make_feed(cat, lake_root)
            sim = ExecutionSimulator(
                initial_cash=Decimal("10000"),
                instruments={"NSE-NIFTY": _nifty_meta()},
                default_qty=1,
                slippage_bps=Decimal("0"),
            )
            driver = BacktestDriver(
                strategy=ClosePreviousMomentum({"k": 2}),
                feed=feed,
                simulator=sim,
            )
            result = driver.run()

        manual = Decimal("0")
        for fill in result.fills:
            manual += fill.costs.total
        assert result.total_costs == manual


# ────────────────────────────────────────────────────────────────────────
# Determinism: running twice yields identical results
# ────────────────────────────────────────────────────────────────────────


class TestBacktestDeterminism:
    def test_two_runs_produce_identical_results(self, tmp_path: Path) -> None:
        """FR-STR-003 end-to-end: two backtests on the same input must match."""
        closes = [100, 105, 95, 110, 90]
        lake_root, catalog_path = _seed_lake(tmp_path, closes)

        def run_once() -> tuple:
            with Catalog.open(catalog_path) as cat:
                feed = _make_feed(cat, lake_root)
                sim = ExecutionSimulator(
                    initial_cash=Decimal("10000"),
                    instruments={"NSE-NIFTY": _nifty_meta()},
                    default_qty=1,
                    slippage_bps=Decimal("0"),
                )
                driver = BacktestDriver(
                    strategy=ClosePreviousMomentum({"k": 1}),
                    feed=feed,
                    simulator=sim,
                )
                result = driver.run()
                return (
                    result.final_cash,
                    result.final_equity,
                    result.total_costs,
                    tuple((f.side, f.qty, f.fill_price) for f in result.fills),
                )

        assert run_once() == run_once()

    def test_same_driver_two_runs_are_equal(self, tmp_path: Path) -> None:
        """Re-running the SAME :class:`BacktestDriver` is reproducible.

        Regression for DEC-EXEC-REPRO-RESILIENCE-001 (the
        ``reset()``-in-``run()`` same-driver reproducibility BLOCKER fix).
        Unlike :meth:`test_two_runs_produce_identical_results`, which builds a
        FRESH :class:`ExecutionSimulator` per run, this directly drives ONE
        driver (hence ONE simulator instance) twice and asserts the second
        run neither diverges nor ACCUMULATES the first run's fills/cash. On the
        pre-fix code the simulator carried prior state into the second
        ``run()``, so ``result2`` doubled the fills and shifted cash.
        """
        closes = [100, 105, 95, 110, 90]
        lake_root, catalog_path = _seed_lake(tmp_path, closes)
        with Catalog.open(catalog_path) as cat:
            feed = _make_feed(cat, lake_root)
            sim = ExecutionSimulator(
                initial_cash=Decimal("10000"),
                instruments={"NSE-NIFTY": _nifty_meta()},
                default_qty=1,
                slippage_bps=Decimal("0"),
            )
            driver = BacktestDriver(
                strategy=ClosePreviousMomentum({"k": 1}),
                feed=feed,
                simulator=sim,
            )
            result1 = driver.run()
            result2 = driver.run()

        assert result2 == result1
        # The second run must NOT accumulate the first run's state.
        assert result2.total_fills == result1.total_fills
        assert result2.final_cash == result1.final_cash


# ────────────────────────────────────────────────────────────────────────
# Accounting identity: cash delta matches fills + cost sum
# ────────────────────────────────────────────────────────────────────────


class TestCashAccountingIdentity:
    def test_initial_cash_minus_final_equals_net_outlay_plus_unrealized(
        self,
        tmp_path: Path,
    ) -> None:
        """Running the loop should conserve equity modulo realized + unrealized P&L.

        Specifically:
            final_equity = initial_cash + total_realized_pnl
                          + sum(unrealized per open position)
                          - total_costs
        """
        closes = [100, 105, 110]
        lake_root, catalog_path = _seed_lake(tmp_path, closes)
        initial = Decimal("10000")
        with Catalog.open(catalog_path) as cat:
            feed = _make_feed(cat, lake_root)
            sim = ExecutionSimulator(
                initial_cash=initial,
                instruments={"NSE-NIFTY": _nifty_meta()},
                default_qty=1,
                slippage_bps=Decimal("0"),
            )
            driver = BacktestDriver(
                strategy=BuyAndHold(),
                feed=feed,
                simulator=sim,
            )
            result = driver.run()

        unrealized = sum(
            (p.unrealized_pnl for p in result.final_positions.values()),
            start=Decimal("0"),
        )
        expected = initial + result.total_realized_pnl + unrealized - result.total_costs
        assert result.final_equity == expected
