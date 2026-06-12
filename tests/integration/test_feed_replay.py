"""Integration tests for :class:`brutex.feed.replay.ReplayFeed`.

Exercises the real write → read → replay path: bars are written via
:class:`ParquetBarSink`, then streamed back chronologically through
:class:`ReplayFeed`. Skipped automatically if the P2 ``[data]``
extras (``polars`` / ``duckdb`` / ``pyarrow``) aren't installed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.data.catalog import Catalog
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink
from brutex.feed.errors import FeedError, LookAheadError
from brutex.feed.replay import FeedEvent, ReplayFeed

pytest.importorskip("polars")
pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")


# ────────────────────────────────────────────────────────────────────────
# Helpers
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


def _seed_lake(
    tmp_path: Path,
    *,
    symbol: str,
    bars: list[CandleBar],
) -> tuple[Path, Path]:
    """Persist *bars* for *symbol* and return ``(lake_root, catalog_path)``."""
    lake_root = tmp_path / "lake"
    catalog_path = tmp_path / "catalog.duckdb"
    writer = ParquetWriter(lake_root=lake_root)
    with Catalog.open(catalog_path) as cat:
        sink = ParquetBarSink(writer=writer, catalog=cat)
        sink.append(
            exchange="NSE",
            segment="FNO",
            symbol=symbol,
            interval="1minute",
            bars=bars,
        )
    return lake_root, catalog_path


# ────────────────────────────────────────────────────────────────────────
# Construction — input validation
# ────────────────────────────────────────────────────────────────────────


class TestReplayFeedConstruction:
    def test_empty_symbols_raises(self, tmp_path: Path) -> None:
        with (
            Catalog.open(tmp_path / "catalog.duckdb") as cat,
            pytest.raises(FeedError, match="at least one"),
        ):
            ReplayFeed(
                catalog=cat,
                lake_root=tmp_path / "lake",
                exchange="NSE",
                segment="FNO",
                symbols=[],
                interval="1minute",
                start=datetime(2024, 1, 1, tzinfo=UTC),
                end=datetime(2024, 1, 2, tzinfo=UTC),
            )

    def test_naive_window_raises(self, tmp_path: Path) -> None:
        with (
            Catalog.open(tmp_path / "catalog.duckdb") as cat,
            pytest.raises(FeedError, match="timezone-aware"),
        ):
            ReplayFeed(
                catalog=cat,
                lake_root=tmp_path / "lake",
                exchange="NSE",
                segment="FNO",
                symbols=["X"],
                interval="1minute",
                start=datetime(2024, 1, 1),  # naive
                end=datetime(2024, 1, 2, tzinfo=UTC),
            )

    def test_inverted_window_raises(self, tmp_path: Path) -> None:
        with (
            Catalog.open(tmp_path / "catalog.duckdb") as cat,
            pytest.raises(FeedError, match=">="),
        ):
            ReplayFeed(
                catalog=cat,
                lake_root=tmp_path / "lake",
                exchange="NSE",
                segment="FNO",
                symbols=["X"],
                interval="1minute",
                start=datetime(2024, 12, 31, tzinfo=UTC),
                end=datetime(2024, 1, 1, tzinfo=UTC),
            )

    def test_symbols_and_interval_properties(self, tmp_path: Path) -> None:
        """The configured symbol tuple + interval are exposed read-only.

        DEC-COV-001: pins the ``symbols`` / ``interval`` accessor
        properties (the symbol tuple is a defensive copy — an immutable
        tuple, not the internal list).
        """
        with Catalog.open(tmp_path / "catalog.duckdb") as cat:
            feed = ReplayFeed(
                catalog=cat,
                lake_root=tmp_path / "lake",
                exchange="NSE",
                segment="FNO",
                symbols=["NIFTY", "BANKNIFTY"],
                interval="1minute",
                start=datetime(2024, 1, 1, tzinfo=UTC),
                end=datetime(2024, 1, 2, tzinfo=UTC),
            )
            assert feed.symbols == ("NIFTY", "BANKNIFTY")
            assert isinstance(feed.symbols, tuple)
            assert feed.interval == "1minute"


# ────────────────────────────────────────────────────────────────────────
# Chronological single-symbol replay
# ────────────────────────────────────────────────────────────────────────


class TestSingleSymbolReplay:
    def test_empty_window_yields_nothing(self, tmp_path: Path) -> None:
        catalog_path = tmp_path / "catalog.duckdb"
        with Catalog.open(catalog_path) as cat:
            feed = ReplayFeed(
                catalog=cat,
                lake_root=tmp_path / "lake",
                exchange="NSE",
                segment="FNO",
                symbols=["NOPE"],
                interval="1minute",
                start=datetime(2024, 1, 1, tzinfo=UTC),
                end=datetime(2024, 1, 31, tzinfo=UTC),
            )
            events = list(feed)
        assert events == []
        assert feed.current_ts is None

    def test_single_symbol_chronological_order(self, tmp_path: Path) -> None:
        bars = [_bar(day=1, minute=15 + i) for i in range(5)]
        lake_root, catalog_path = _seed_lake(tmp_path, symbol="NSE-A", bars=bars)
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
            events = list(feed)
        assert len(events) == 5
        ts_list = [e.timestamp for e in events]
        assert ts_list == sorted(ts_list)
        # current_ts tracks the last yielded bar.
        assert feed.current_ts == events[-1].timestamp

    def test_filters_to_exact_window(self, tmp_path: Path) -> None:
        bars = [_bar(day=d) for d in (1, 5, 10, 15, 20)]
        lake_root, catalog_path = _seed_lake(tmp_path, symbol="NSE-A", bars=bars)
        with Catalog.open(catalog_path) as cat:
            feed = ReplayFeed(
                catalog=cat,
                lake_root=lake_root,
                exchange="NSE",
                segment="FNO",
                symbols=["NSE-A"],
                interval="1minute",
                start=datetime(2024, 1, 5, tzinfo=UTC),
                end=datetime(2024, 1, 15, 23, 59, tzinfo=UTC),
            )
            events = list(feed)
        # Days 5, 10, 15 fall inside the window.
        assert len(events) == 3


# ────────────────────────────────────────────────────────────────────────
# Multi-symbol k-way merge
# ────────────────────────────────────────────────────────────────────────


class TestMultiSymbolMerge:
    def _seed_two_symbols(self, tmp_path: Path) -> tuple[Path, Path]:
        lake_root = tmp_path / "lake"
        catalog_path = tmp_path / "catalog.duckdb"
        writer = ParquetWriter(lake_root=lake_root)
        with Catalog.open(catalog_path) as cat:
            sink = ParquetBarSink(writer=writer, catalog=cat)
            # Symbol A: bars at minute 15, 17, 19
            sink.append(
                exchange="NSE",
                segment="FNO",
                symbol="NSE-A",
                interval="1minute",
                bars=[_bar(1, minute=m, open_price=100.0) for m in (15, 17, 19)],
            )
            # Symbol B: bars at minute 16, 18, 20
            sink.append(
                exchange="NSE",
                segment="FNO",
                symbol="NSE-B",
                interval="1minute",
                bars=[_bar(1, minute=m, open_price=200.0) for m in (16, 18, 20)],
            )
        return lake_root, catalog_path

    def test_interleaved_chronological_order(self, tmp_path: Path) -> None:
        lake_root, catalog_path = self._seed_two_symbols(tmp_path)
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
            events = list(feed)
        assert len(events) == 6
        # Interleaved A, B, A, B, A, B
        assert [e.symbol for e in events] == [
            "NSE-A",
            "NSE-B",
            "NSE-A",
            "NSE-B",
            "NSE-A",
            "NSE-B",
        ]
        ts_list = [e.timestamp for e in events]
        assert ts_list == sorted(ts_list)

    def test_ties_break_by_symbol_order(self, tmp_path: Path) -> None:
        """Two bars sharing a timestamp must emit in the order passed to ``symbols``."""
        lake_root = tmp_path / "lake"
        catalog_path = tmp_path / "catalog.duckdb"
        writer = ParquetWriter(lake_root=lake_root)
        with Catalog.open(catalog_path) as cat:
            sink = ParquetBarSink(writer=writer, catalog=cat)
            for sym in ("NSE-A", "NSE-B"):
                sink.append(
                    exchange="NSE",
                    segment="FNO",
                    symbol=sym,
                    interval="1minute",
                    bars=[_bar(1, minute=15)],
                )
        with Catalog.open(catalog_path) as cat:
            # B before A → B should yield first.
            feed_b_first = ReplayFeed(
                catalog=cat,
                lake_root=lake_root,
                exchange="NSE",
                segment="FNO",
                symbols=["NSE-B", "NSE-A"],
                interval="1minute",
                start=datetime(2024, 1, 1, tzinfo=UTC),
                end=datetime(2024, 1, 31, tzinfo=UTC),
            )
            events = list(feed_b_first)
        assert [e.symbol for e in events] == ["NSE-B", "NSE-A"]


# ────────────────────────────────────────────────────────────────────────
# Look-ahead guard
# ────────────────────────────────────────────────────────────────────────


class TestLookAheadGuard:
    def test_ensure_no_lookahead_before_iteration_raises(self, tmp_path: Path) -> None:
        with Catalog.open(tmp_path / "catalog.duckdb") as cat:
            feed = ReplayFeed(
                catalog=cat,
                lake_root=tmp_path / "lake",
                exchange="NSE",
                segment="FNO",
                symbols=["X"],
                interval="1minute",
                start=datetime(2024, 1, 1, tzinfo=UTC),
                end=datetime(2024, 1, 31, tzinfo=UTC),
            )
            with pytest.raises(LookAheadError, match="not yielded"):
                feed.ensure_no_lookahead(datetime(2024, 1, 15, tzinfo=UTC))

    def test_ensure_no_lookahead_at_current_is_ok(self, tmp_path: Path) -> None:
        bars = [_bar(day=1, minute=15 + i) for i in range(3)]
        lake_root, catalog_path = _seed_lake(tmp_path, symbol="NSE-A", bars=bars)
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
            iterator = iter(feed)
            first = next(iterator)
            # Read at current_ts is legal.
            feed.ensure_no_lookahead(first.timestamp)
            # Read at ts <= current_ts is legal.
            feed.ensure_no_lookahead(first.timestamp - timedelta(minutes=1))

    def test_ensure_no_lookahead_future_ts_raises(self, tmp_path: Path) -> None:
        bars = [_bar(day=1, minute=15 + i) for i in range(3)]
        lake_root, catalog_path = _seed_lake(tmp_path, symbol="NSE-A", bars=bars)
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
            iterator = iter(feed)
            first = next(iterator)
            future_ts = first.timestamp + timedelta(hours=1)
            with pytest.raises(LookAheadError, match="FR-INT-004"):
                feed.ensure_no_lookahead(future_ts)

    def test_ensure_no_lookahead_naive_ts_raises(self, tmp_path: Path) -> None:
        with Catalog.open(tmp_path / "catalog.duckdb") as cat:
            feed = ReplayFeed(
                catalog=cat,
                lake_root=tmp_path / "lake",
                exchange="NSE",
                segment="FNO",
                symbols=["X"],
                interval="1minute",
                start=datetime(2024, 1, 1, tzinfo=UTC),
                end=datetime(2024, 1, 31, tzinfo=UTC),
            )
            with pytest.raises(LookAheadError, match="tz-aware"):
                feed.ensure_no_lookahead(datetime(2024, 1, 15))


# ────────────────────────────────────────────────────────────────────────
# Pacer
# ────────────────────────────────────────────────────────────────────────


class TestPacer:
    def test_pacer_receives_gap_between_bars(self, tmp_path: Path) -> None:
        """Every yielded bar after the first invokes the pacer with the prev gap."""
        bars = [_bar(day=1, minute=15 + i) for i in range(4)]
        lake_root, catalog_path = _seed_lake(tmp_path, symbol="NSE-A", bars=bars)

        gaps: list[timedelta] = []

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
                pacer=gaps.append,
            )
            list(feed)

        # 4 bars → 3 gaps. Every gap is exactly 1 minute.
        assert gaps == [timedelta(minutes=1)] * 3


# ────────────────────────────────────────────────────────────────────────
# FeedEvent dataclass
# ────────────────────────────────────────────────────────────────────────


class TestFeedEvent:
    def test_timestamp_delegates_to_bar(self) -> None:
        bar = _bar(day=1)
        event = FeedEvent(symbol="NSE-A", bar=bar)
        assert event.timestamp == bar.timestamp
        assert event.symbol == "NSE-A"
