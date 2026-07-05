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


# ────────────────────────────────────────────────────────────────────────
# Corrupt stored bars are refused LOUDLY (DEC-STORED-DATA-VALIDATION-001)
# ────────────────────────────────────────────────────────────────────────


class TestCorruptStoredBarRefused:
    """The engine bar-load read boundary rejects a corrupt stored bar.

    Fails-before (round-2 audit, live-reproduced): a legacy pre-#912
    partition holding zero/negative but internally-ORDERED prices passed
    ``arrow_table_to_bars`` silently — a day_low of -1000 flowed into
    Stage-1A + run identity. The #912 ``has_no_price`` screen is
    ingest-parse-time only and is bypassed when READING stored bars.
    """

    @staticmethod
    def _poisoned_table(*, open_: float, high: float, low: float, close: float) -> object:
        import pyarrow as pa

        t0 = datetime(2024, 1, 2, 3, 45, tzinfo=UTC)
        return pa.table(
            {
                "timestamp": pa.array(
                    [t0, t0 + timedelta(minutes=1)], type=pa.timestamp("us", tz="UTC")
                ),
                "open": pa.array([100.0, open_], type=pa.float64()),
                "high": pa.array([101.0, high], type=pa.float64()),
                "low": pa.array([99.0, low], type=pa.float64()),
                "close": pa.array([100.5, close], type=pa.float64()),
                "volume": pa.array([10, 11], type=pa.int64()),
                "open_interest": pa.array([None, None], type=pa.int64()),
            }
        )

    def test_negative_ordered_bar_raises_naming_source_and_ts(self) -> None:
        from brutex.feed.replay import arrow_table_to_bars

        table = self._poisoned_table(open_=-5.0, high=-1.0, low=-10.0, close=-2.0)
        with pytest.raises(ValueError, match="corrupt stored bar") as excinfo:
            arrow_table_to_bars(table, source="NSE/INDEX/NSE-NIFTY/1minute")  # type: ignore[arg-type]
        message = str(excinfo.value)
        assert "NSE/INDEX/NSE-NIFTY/1minute" in message
        assert "2024, 1, 2, 3, 46" in message  # the offending bar's timestamp
        assert "positive price" in message  # the per-bar defect, chained

    def test_without_source_still_raises_loudly(self) -> None:
        from brutex.feed.replay import arrow_table_to_bars

        table = self._poisoned_table(open_=0.0, high=0.0, low=0.0, close=0.0)
        with pytest.raises(ValueError, match="corrupt stored bar at"):
            arrow_table_to_bars(table)  # type: ignore[arg-type]

    def test_all_positive_ordering_violation_raises_via_constructor(self) -> None:
        # All prices POSITIVE but high < low → the positivity loop passes and
        # the CONSTRUCTOR's ordering guard fires, re-wrapped as a corrupt-bar
        # error (covers the try/except branch).
        from brutex.feed.replay import arrow_table_to_bars

        table = self._poisoned_table(open_=100.0, high=50.0, low=99.0, close=100.0)
        with pytest.raises(ValueError, match="corrupt stored bar") as excinfo:
            arrow_table_to_bars(table, source="NSE/INDEX/NSE-NIFTY/1minute")  # type: ignore[arg-type]
        message = str(excinfo.value)
        assert "NSE/INDEX/NSE-NIFTY/1minute" in message
        assert "positive price" not in message  # NOT a positivity defect
        assert "high" in message  # the ordering defect, chained from the ctor

    def test_ordering_violation_without_source_raises(self) -> None:
        from brutex.feed.replay import arrow_table_to_bars

        table = self._poisoned_table(open_=100.0, high=50.0, low=99.0, close=100.0)
        with pytest.raises(ValueError, match="corrupt stored bar at"):
            arrow_table_to_bars(table)  # type: ignore[arg-type]

    def test_empty_table_returns_empty_list(self) -> None:
        import pyarrow as pa
        from brutex.feed.replay import arrow_table_to_bars

        empty = pa.table(
            {
                "timestamp": pa.array([], type=pa.timestamp("us", tz="UTC")),
                "open": pa.array([], type=pa.float64()),
                "high": pa.array([], type=pa.float64()),
                "low": pa.array([], type=pa.float64()),
                "close": pa.array([], type=pa.float64()),
                "volume": pa.array([], type=pa.int64()),
                "open_interest": pa.array([], type=pa.int64()),
            }
        )
        assert arrow_table_to_bars(empty) == []  # type: ignore[arg-type]

    def test_clean_table_round_trips_unchanged(self) -> None:
        from brutex.feed.replay import arrow_table_to_bars

        table = self._poisoned_table(open_=100.0, high=102.0, low=99.5, close=101.0)
        bars = arrow_table_to_bars(table, source="NSE/INDEX/NSE-NIFTY/1minute")  # type: ignore[arg-type]
        assert [b.close for b in bars] == [Decimal("100.5"), Decimal("101.0")]

    def test_replay_feed_names_the_partition_on_poisoned_lake(self, tmp_path: Path) -> None:
        # Poison a REAL lake partition (raw pyarrow — the writer refuses
        # corrupt bars, exactly like a legacy pre-#912 lake), then prove the
        # feed's load raises naming the series it was reading.
        import pyarrow as pa
        import pyarrow.parquet as pq

        symbol = "NSE-NIFTY"
        lake_root = tmp_path / "lake"
        dest = lake_root / "bars" / "NSE" / "INDEX" / symbol / "1minute" / "2024" / "01.parquet"
        dest.parent.mkdir(parents=True, exist_ok=True)
        t0 = datetime(2024, 1, 2, 3, 45, tzinfo=UTC)
        pq.write_table(
            pa.table(
                {
                    "timestamp": pa.array([t0], type=pa.timestamp("us", tz="UTC")),
                    "open": pa.array([-5.0], type=pa.float64()),
                    "high": pa.array([-1.0], type=pa.float64()),
                    "low": pa.array([-1000.0], type=pa.float64()),
                    "close": pa.array([-2.0], type=pa.float64()),
                    "volume": pa.array([10], type=pa.int64()),
                    "open_interest": pa.array([None], type=pa.int64()),
                }
            ),
            dest,
        )
        catalog_path = tmp_path / "catalog.duckdb"
        with Catalog.open(catalog_path) as cat:
            from brutex.data.models import CatalogEntry

            cat.upsert(
                CatalogEntry(
                    exchange="NSE",
                    segment="INDEX",
                    symbol=symbol,
                    interval="1minute",
                    year=2024,
                    month=1,
                    raw_hash="a" * 64,
                    row_count=1,
                    first_ts=t0,
                    last_ts=t0,
                    fetched_at=t0,
                )
            )
            feed = ReplayFeed(
                catalog=cat,
                lake_root=lake_root,
                exchange="NSE",
                segment="INDEX",
                symbols=[symbol],
                interval="1minute",
                start=datetime(2024, 1, 1, tzinfo=UTC),
                end=datetime(2024, 2, 1, tzinfo=UTC),
            )
            with pytest.raises(ValueError, match="NSE/INDEX/NSE-NIFTY/1minute"):
                list(feed)
