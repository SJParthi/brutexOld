"""Integration tests for :class:`brutex.strategies.runner.StrategyRunner`.

Wires a real :class:`Catalog` + :class:`ParquetWriter` + :class:`ReplayFeed`
and drives a trivial :class:`Strategy` through it to exercise the full
FR-STR-001 → FR-STR-003 → FR-INT-004 path.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.data.catalog import Catalog
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink
from brutex.feed.replay import FeedEvent, ReplayFeed
from brutex.strategies.errors import StrategyContractError
from brutex.strategies.protocol import StrategyMeta
from brutex.strategies.runner import StrategyRunner, StrategyRunResult
from brutex.strategies.types import Side, Signal, StrategyState

pytest.importorskip("polars")
pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")


# ────────────────────────────────────────────────────────────────────────
# Fixtures / helpers
# ────────────────────────────────────────────────────────────────────────


def _bar(day: int, minute: int = 15, *, open_price: float = 100.0) -> CandleBar:
    return CandleBar(
        timestamp=datetime(2024, 1, day, 9, minute, 0, tzinfo=UTC),
        open=Decimal(str(open_price)),
        high=Decimal(str(open_price + 1)),
        low=Decimal(str(open_price - 1)),
        close=Decimal(str(open_price + 0.5)),
        volume=10,
        open_interest=500,
    )


def _seed_single_symbol(
    tmp_path: Path,
    *,
    symbol: str = "NSE-A",
    n_bars: int = 5,
) -> tuple[Path, Path]:
    lake_root = tmp_path / "lake"
    catalog_path = tmp_path / "catalog.duckdb"
    writer = ParquetWriter(lake_root=lake_root)
    with Catalog.open(catalog_path) as cat:
        sink = ParquetBarSink(writer=writer, catalog=cat)
        bars = [_bar(1, minute=15 + i) for i in range(n_bars)]
        sink.append(
            exchange="NSE",
            segment="FNO",
            symbol=symbol,
            interval="1minute",
            bars=bars,
        )
    return lake_root, catalog_path


# ────────────────────────────────────────────────────────────────────────
# Test strategies
# ────────────────────────────────────────────────────────────────────────


class _AlwaysBuyStrategy:
    """Emits a BUY signal on every bar. No indicators, no lookback."""

    META = StrategyMeta(
        strategy_id="always_buy",
        version="0.1.0",
        symbols=("NSE-A",),
        interval="1minute",
    )

    def on_event(
        self,
        event: FeedEvent,
        state: StrategyState,
    ) -> Sequence[Signal]:
        del state
        return [
            Signal(
                symbol=event.symbol,
                side=Side.BUY,
                timestamp=event.timestamp,
                strength=0.5,
                reason="always_buy",
            )
        ]


class _LookbackThreeStrategy:
    """Emits one BUY every time bar_count is exactly 4 or 5."""

    META = StrategyMeta(
        strategy_id="lookback_three",
        version="0.1.0",
        symbols=("NSE-A",),
        interval="1minute",
        lookback_bars=3,
    )

    def on_event(
        self,
        event: FeedEvent,
        state: StrategyState,
    ) -> Sequence[Signal]:
        del state
        return [
            Signal(
                symbol=event.symbol,
                side=Side.BUY,
                timestamp=event.timestamp,
            )
        ]


class _QuietStrategy:
    """Returns an empty sequence on every bar (no signals)."""

    META = StrategyMeta(
        strategy_id="quiet",
        version="0.1.0",
        symbols=("NSE-A",),
        interval="1minute",
    )

    def on_event(
        self,
        event: FeedEvent,
        state: StrategyState,
    ) -> Sequence[Signal]:
        del event, state
        return []


class _BadTypeStrategy:
    """Emits a non-Signal value. The runner must reject it."""

    META = StrategyMeta(
        strategy_id="bad_type",
        version="0.1.0",
        symbols=("NSE-A",),
        interval="1minute",
    )

    def on_event(
        self,
        event: FeedEvent,
        state: StrategyState,
    ) -> Sequence[object]:
        del event, state
        return ["not a signal"]


class _OtherSymbolStrategy:
    """Emits a signal for a symbol outside its declared META.symbols."""

    META = StrategyMeta(
        strategy_id="other_symbol",
        version="0.1.0",
        symbols=("NSE-A",),
        interval="1minute",
    )

    def on_event(
        self,
        event: FeedEvent,
        state: StrategyState,
    ) -> Sequence[Signal]:
        del state
        return [
            Signal(
                symbol="NSE-B",  # not in META.symbols
                side=Side.BUY,
                timestamp=event.timestamp,
            )
        ]


class _FutureSignalStrategy:
    """Emits a signal dated one minute in the future."""

    META = StrategyMeta(
        strategy_id="future_signal",
        version="0.1.0",
        symbols=("NSE-A",),
        interval="1minute",
    )

    def on_event(
        self,
        event: FeedEvent,
        state: StrategyState,
    ) -> Sequence[Signal]:
        del state
        return [
            Signal(
                symbol=event.symbol,
                side=Side.BUY,
                timestamp=event.timestamp + timedelta(minutes=1),
            )
        ]


# ────────────────────────────────────────────────────────────────────────
# Happy path
# ────────────────────────────────────────────────────────────────────────


class TestRunnerHappyPath:
    def test_always_buy_emits_one_signal_per_bar(self, tmp_path: Path) -> None:
        lake_root, catalog_path = _seed_single_symbol(tmp_path, n_bars=5)
        with Catalog.open(catalog_path) as cat:
            feed = ReplayFeed(
                catalog=cat,
                lake_root=lake_root,
                exchange="NSE",
                segment="FNO",
                symbols=["NSE-A"],
                interval="1minute",
                start=datetime(2024, 1, 1, tzinfo=UTC),
                end=datetime(2024, 1, 31, tzinfo=UTC),
            )
            runner = StrategyRunner(strategy=_AlwaysBuyStrategy(), feed=feed)
            result = runner.run()

        assert isinstance(result, StrategyRunResult)
        assert result.strategy_id == "always_buy"
        assert result.bars_observed == 5
        assert result.bars_skipped_lookback == 0
        assert len(result.signals) == 5
        for signal in result.signals:
            assert signal.side == Side.BUY
            assert signal.symbol == "NSE-A"

    def test_quiet_strategy_emits_nothing(self, tmp_path: Path) -> None:
        lake_root, catalog_path = _seed_single_symbol(tmp_path, n_bars=5)
        with Catalog.open(catalog_path) as cat:
            feed = ReplayFeed(
                catalog=cat,
                lake_root=lake_root,
                exchange="NSE",
                segment="FNO",
                symbols=["NSE-A"],
                interval="1minute",
                start=datetime(2024, 1, 1, tzinfo=UTC),
                end=datetime(2024, 1, 31, tzinfo=UTC),
            )
            runner = StrategyRunner(strategy=_QuietStrategy(), feed=feed)
            result = runner.run()
        assert result.signals == ()
        assert result.bars_observed == 5


# ────────────────────────────────────────────────────────────────────────
# Lookback gating — FR-STR-002
# ────────────────────────────────────────────────────────────────────────


class TestLookbackGate:
    def test_strategy_does_not_see_lookback_bars(self, tmp_path: Path) -> None:
        lake_root, catalog_path = _seed_single_symbol(tmp_path, n_bars=5)
        with Catalog.open(catalog_path) as cat:
            feed = ReplayFeed(
                catalog=cat,
                lake_root=lake_root,
                exchange="NSE",
                segment="FNO",
                symbols=["NSE-A"],
                interval="1minute",
                start=datetime(2024, 1, 1, tzinfo=UTC),
                end=datetime(2024, 1, 31, tzinfo=UTC),
            )
            runner = StrategyRunner(strategy=_LookbackThreeStrategy(), feed=feed)
            result = runner.run()

        # 5 bars - 3 lookback = 2 warm-up skipped + 2 emitted (bars 4, 5)
        assert result.bars_observed == 5
        assert result.bars_skipped_lookback == 3
        assert len(result.signals) == 2


# ────────────────────────────────────────────────────────────────────────
# Determinism — FR-STR-003
# ────────────────────────────────────────────────────────────────────────


class TestDeterministicReplay:
    def test_back_to_back_runs_produce_identical_results(self, tmp_path: Path) -> None:
        """FR-STR-003 property: ``replay(A) == replay(A)``."""
        lake_root, catalog_path = _seed_single_symbol(tmp_path, n_bars=5)
        with Catalog.open(catalog_path) as cat:
            feed1 = ReplayFeed(
                catalog=cat,
                lake_root=lake_root,
                exchange="NSE",
                segment="FNO",
                symbols=["NSE-A"],
                interval="1minute",
                start=datetime(2024, 1, 1, tzinfo=UTC),
                end=datetime(2024, 1, 31, tzinfo=UTC),
            )
            run1 = StrategyRunner(strategy=_AlwaysBuyStrategy(), feed=feed1).run()

            feed2 = ReplayFeed(
                catalog=cat,
                lake_root=lake_root,
                exchange="NSE",
                segment="FNO",
                symbols=["NSE-A"],
                interval="1minute",
                start=datetime(2024, 1, 1, tzinfo=UTC),
                end=datetime(2024, 1, 31, tzinfo=UTC),
            )
            run2 = StrategyRunner(strategy=_AlwaysBuyStrategy(), feed=feed2).run()

        assert run1 == run2


# ────────────────────────────────────────────────────────────────────────
# Contract enforcement
# ────────────────────────────────────────────────────────────────────────


class TestContractEnforcement:
    def _make_runner(
        self,
        tmp_path: Path,
        strategy: object,
    ) -> tuple[StrategyRunner, Catalog]:
        lake_root, catalog_path = _seed_single_symbol(tmp_path, n_bars=3)
        cat = Catalog.open(catalog_path)
        feed = ReplayFeed(
            catalog=cat,
            lake_root=lake_root,
            exchange="NSE",
            segment="FNO",
            symbols=["NSE-A"],
            interval="1minute",
            start=datetime(2024, 1, 1, tzinfo=UTC),
            end=datetime(2024, 1, 31, tzinfo=UTC),
        )
        return StrategyRunner(strategy=strategy, feed=feed), cat  # type: ignore[arg-type]

    def test_interval_mismatch_raises(self, tmp_path: Path) -> None:
        lake_root, catalog_path = _seed_single_symbol(tmp_path, n_bars=1)
        with Catalog.open(catalog_path) as cat:
            feed = ReplayFeed(
                catalog=cat,
                lake_root=lake_root,
                exchange="NSE",
                segment="FNO",
                symbols=["NSE-A"],
                interval="5minute",  # mismatches strategy's 1minute
                start=datetime(2024, 1, 1, tzinfo=UTC),
                end=datetime(2024, 1, 31, tzinfo=UTC),
            )
            with pytest.raises(StrategyContractError, match="interval"):
                StrategyRunner(strategy=_AlwaysBuyStrategy(), feed=feed)

    def test_non_signal_emission_raises(self, tmp_path: Path) -> None:
        runner, cat = self._make_runner(tmp_path, _BadTypeStrategy())
        try:
            with pytest.raises(StrategyContractError, match="expected"):
                runner.run()
        finally:
            cat.close()

    def test_symbol_outside_meta_raises(self, tmp_path: Path) -> None:
        runner, cat = self._make_runner(tmp_path, _OtherSymbolStrategy())
        try:
            with pytest.raises(StrategyContractError, match="not in its declared"):
                runner.run()
        finally:
            cat.close()

    def test_future_signal_timestamp_raises(self, tmp_path: Path) -> None:
        runner, cat = self._make_runner(tmp_path, _FutureSignalStrategy())
        try:
            with pytest.raises(StrategyContractError, match="look-ahead"):
                runner.run()
        finally:
            cat.close()


# ────────────────────────────────────────────────────────────────────────
# Symbol filtering
# ────────────────────────────────────────────────────────────────────────


class TestSymbolFiltering:
    def test_events_for_out_of_scope_symbols_skipped(self, tmp_path: Path) -> None:
        """Feed yields A+B events; strategy only declares A → B skipped."""
        lake_root = tmp_path / "lake"
        catalog_path = tmp_path / "catalog.duckdb"
        writer = ParquetWriter(lake_root=lake_root)
        with Catalog.open(catalog_path) as cat:
            sink = ParquetBarSink(writer=writer, catalog=cat)
            for symbol in ("NSE-A", "NSE-B"):
                sink.append(
                    exchange="NSE",
                    segment="FNO",
                    symbol=symbol,
                    interval="1minute",
                    bars=[_bar(1, minute=15 + i) for i in range(3)],
                )

        with Catalog.open(catalog_path) as cat:
            feed = ReplayFeed(
                catalog=cat,
                lake_root=lake_root,
                exchange="NSE",
                segment="FNO",
                symbols=["NSE-A", "NSE-B"],
                interval="1minute",
                start=datetime(2024, 1, 1, tzinfo=UTC),
                end=datetime(2024, 1, 31, tzinfo=UTC),
            )
            # Strategy only consumes NSE-A
            runner = StrategyRunner(strategy=_AlwaysBuyStrategy(), feed=feed)
            result = runner.run()

        assert result.bars_observed == 3  # only NSE-A bars
        assert len(result.signals) == 3
        assert all(s.symbol == "NSE-A" for s in result.signals)
