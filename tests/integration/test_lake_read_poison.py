"""Poisoned-lake read-gate integration tests (DEC-LAKE-READ-UNIQUE-001).

The write side is duplicate-proof since DEC-LAKE-WRITE-UNIQUE-001, but bars
poisoned BEFORE that fix still sit physically duplicated on disk. These tests
write a **physically poisoned** Parquet partition directly (bypassing the
writer, exactly like a poisoned-era lake) and prove every wired reader serves
the SAME bars a clean lake serves — with the ONE aggregated
``lake.read.duplicates_dropped`` WARNING, never silently.

Fails-before discipline (each pinned on unfixed main):

* ``test_get_bars_collapses_poisoned_partition`` — ``Catalog.get_bars``
  returned 5 rows (the duplicate included) instead of 4 and NO warning.
* ``test_replay_feed_streams_unique_bars`` — the feed yielded the duplicate.
* ``test_engine_outputs_identical_clean_vs_poisoned`` — ``build_bar_bits`` /
  hit counts / trades DIFFERED between the clean and poisoned lakes (the
  measured leaderboard-reordering corruption).
* ``test_db_candle_pager_dedups_page_and_total`` /
  ``test_db_option_pager_dedups_page_and_total`` — the /db totals counted the
  physical (inflated) rows and the pages served the duplicate.
* ``test_option_lake_source_keeps_first_bar`` — ``LakeOptionBarSource`` priced
  the leg off the LAST-wins re-statement instead of the first stored bar.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import polars as pl
import pytest
import structlog
from brutex.data.catalog import Catalog
from brutex.data.lake_read import DUPLICATES_DROPPED_EVENT
from brutex.data.models import CatalogEntry
from brutex.feed.replay import ReplayFeed, arrow_table_to_bars
from structlog.typing import EventDict

pytest.importorskip("duckdb")

_EXCHANGE = "NSE"
_SEGMENT = "INDEX"
_SYMBOL = "NSE-NIFTY"
_INTERVAL = "1minute"
_T0 = datetime(2024, 1, 2, 3, 45, tzinfo=UTC)  # 09:15 IST
_START = datetime(2024, 1, 1, tzinfo=UTC)
_END = datetime(2024, 2, 1, tzinfo=UTC)


class _capture_logs:
    """Context manager capturing structlog events into a list (configure/restore)."""

    def __init__(self) -> None:
        self._cap = structlog.testing.LogCapture()
        self._saved: dict[str, object] | None = None

    def __enter__(self) -> list[EventDict]:
        self._saved = structlog.get_config()
        structlog.configure(processors=[self._cap])
        return self._cap.entries

    def __exit__(self, *_exc: object) -> None:
        assert self._saved is not None
        structlog.configure(**self._saved)  # type: ignore[arg-type]


def _dedup_events(logs: list[EventDict]) -> list[EventDict]:
    return [e for e in logs if e["event"] == DUPLICATES_DROPPED_EVENT]


def _clean_rows() -> dict[str, list[Any]]:
    """Four strictly-increasing 1-minute bars (a mini trending session)."""
    minutes = [0, 1, 2, 3]
    closes = [100.0, 101.0, 102.0, 103.0]
    return {
        "timestamp": [_T0 + timedelta(minutes=m) for m in minutes],
        "open": [c - 0.5 for c in closes],
        "high": [c + 1.0 for c in closes],
        "low": [c - 1.0 for c in closes],
        "close": closes,
        "volume": [10, 11, 12, 13],
        "open_interest": [None, None, None, None],
    }


def _poisoned_rows() -> dict[str, list[Any]]:
    """The SAME four bars + an exact dup of minute 1 + a CONFLICTING dup of minute 2.

    The conflict re-statement (a wildly different OHLC) is inserted AFTER the
    real minute-2 bar, exactly like a poisoned-era vendor re-statement appended
    by the old non-unique write path — keep-FIRST must discard it.
    """
    clean = _clean_rows()
    rows = {name: list(values) for name, values in clean.items()}
    # Exact duplicate of minute 1 (position: right after the original).
    for name, values in rows.items():
        values.insert(2, clean[name][1])
    # Conflicting duplicate of minute 2 (now at index 3 → insert after, index 4).
    conflict = {
        "timestamp": _T0 + timedelta(minutes=2),
        "open": 500.0,
        "high": 999.0,
        "low": 1.0,
        "close": 777.0,
        "volume": 999,
        "open_interest": None,
    }
    for name, values in rows.items():
        values.insert(4, conflict[name])
    return rows


def _write_lake(lake_root: Path, rows: dict[str, list[Any]]) -> Path:
    """Write *rows* as the symbol's 2024-01 month partition, bypassing the writer."""
    dest = lake_root / "bars" / _EXCHANGE / _SEGMENT / _SYMBOL / _INTERVAL / "2024" / "01.parquet"
    dest.parent.mkdir(parents=True, exist_ok=True)
    frame = pl.DataFrame(rows).with_columns(pl.col("open_interest").cast(pl.Int64))
    frame.write_parquet(dest)
    return dest


def _catalog_with_entry(lake_root: Path, *, row_count: int) -> Catalog:
    cat = Catalog.open(lake_root / "catalog.duckdb")
    cat.upsert(
        CatalogEntry(
            exchange=_EXCHANGE,
            segment=_SEGMENT,
            symbol=_SYMBOL,
            interval=_INTERVAL,
            year=2024,
            month=1,
            raw_hash="a" * 64,
            row_count=row_count,
            first_ts=_T0,
            last_ts=_T0 + timedelta(minutes=3),
            fetched_at=_T0,
        )
    )
    return cat


def _get_bars(cat: Catalog, lake_root: Path) -> Any:
    return cat.get_bars(
        lake_root=lake_root,
        exchange=_EXCHANGE,
        segment=_SEGMENT,
        symbol=_SYMBOL,
        interval=_INTERVAL,
        start=_START,
        end=_END,
    )


# ────────────────────────────────────────────────────────────────────────
# Catalog.get_bars — the engine's input mouth
# ────────────────────────────────────────────────────────────────────────


class TestCatalogGetBarsDedup:
    def test_get_bars_collapses_poisoned_partition(self, tmp_path: Path) -> None:
        _write_lake(tmp_path, _poisoned_rows())
        with _catalog_with_entry(tmp_path, row_count=6) as cat, _capture_logs() as logs:
            table = _get_bars(cat, tmp_path)
        timestamps = table.column("timestamp").to_pylist()
        assert len(timestamps) == 4
        assert len(set(timestamps)) == 4
        # Keep-FIRST: the conflicting minute-2 re-statement (close 777.0) lost.
        closes = table.column("close").to_pylist()
        assert closes == [100.0, 101.0, 102.0, 103.0]
        events = _dedup_events(logs)
        assert len(events) == 1
        assert events[0]["dropped"] == 2
        assert events[0]["conflict_count"] == 1
        assert events[0]["source"] == f"catalog:{_EXCHANGE}/{_SEGMENT}/{_SYMBOL}/{_INTERVAL}"

    def test_clean_partition_is_byte_identical_and_silent(self, tmp_path: Path) -> None:
        _write_lake(tmp_path, _clean_rows())
        with _catalog_with_entry(tmp_path, row_count=4) as cat, _capture_logs() as logs:
            table = _get_bars(cat, tmp_path)
        assert table.column("close").to_pylist() == [100.0, 101.0, 102.0, 103.0]
        assert _dedup_events(logs) == []

    def test_replay_feed_streams_unique_bars(self, tmp_path: Path) -> None:
        _write_lake(tmp_path, _poisoned_rows())
        with _catalog_with_entry(tmp_path, row_count=6) as cat:
            feed = ReplayFeed(
                catalog=cat,
                lake_root=tmp_path,
                exchange=_EXCHANGE,
                segment=_SEGMENT,
                symbols=[_SYMBOL],
                interval=_INTERVAL,
                start=_START,
                end=_END,
            )
            events = list(feed)
        timestamps = [e.timestamp for e in events]
        assert len(timestamps) == 4
        assert timestamps == sorted(timestamps)
        assert len(set(timestamps)) == 4


# ────────────────────────────────────────────────────────────────────────
# Engine end-to-end — the audit's corruption becomes impossible
# ────────────────────────────────────────────────────────────────────────


class TestEngineIdenticalCleanVsPoisoned:
    def test_engine_outputs_identical_clean_vs_poisoned(self, tmp_path: Path) -> None:
        """Poisoned lake → catalog gate → bar_bits / hit counts / trades IDENTICAL.

        The forensic engine audit measured one duplicate bar corrupting the
        condition masks of every subsequent bar, flipping hit counts and
        changing trades — this pins that a poisoned partition now produces the
        EXACT engine outputs of the clean lake, bit-for-bit.
        """
        from brutex.engine.combo_mask import mask_to_entry_signals
        from brutex.engine.stage1a import build_bar_bits
        from brutex.engine.trade_sim import simulate_trades
        from brutex.strategies.types import Side

        outputs: dict[str, tuple[list[int], list[bool], Any]] = {}
        for label, rows in (("clean", _clean_rows()), ("poisoned", _poisoned_rows())):
            lake_root = tmp_path / label
            _write_lake(lake_root, rows)
            with _catalog_with_entry(lake_root, row_count=len(rows["timestamp"])) as cat:
                bars = arrow_table_to_bars(_get_bars(cat, lake_root))
            bar_bits = build_bar_bits(bars)
            entry_signals = mask_to_entry_signals(bar_bits, 0)
            # tf==1 fixed point (DEC-FILL-EXEC-GRID-1M-001): the deduped 1m
            # read IS both the decision and the execution stream.
            trades = simulate_trades(
                bars, entry_signals, direction=Side.BUY, tf_minutes=1, exec_bars_1m=bars
            )
            outputs[label] = (bar_bits, entry_signals, trades)

        clean_bits, clean_signals, clean_trades = outputs["clean"]
        poisoned_bits, poisoned_signals, poisoned_trades = outputs["poisoned"]
        assert poisoned_bits == clean_bits
        assert poisoned_signals == clean_signals
        assert poisoned_trades == clean_trades
        # Hit counts (per-bit totals over the matrix) identical too.
        clean_hits = [sum((bits >> bit) & 1 for bits in clean_bits) for bit in range(60)]
        poisoned_hits = [sum((bits >> bit) & 1 for bits in poisoned_bits) for bit in range(60)]
        assert poisoned_hits == clean_hits


# ────────────────────────────────────────────────────────────────────────
# /db pagers — totals equal distinct counts, pages dedup in-scan
# ────────────────────────────────────────────────────────────────────────


class TestDbPagersDedup:
    def test_db_candle_pager_dedups_page_and_total(self, tmp_path: Path) -> None:
        import duckdb
        from brutex.dashboard.db_view import query_candles

        _write_lake(tmp_path, _poisoned_rows())
        conn = duckdb.connect()
        page = query_candles(
            conn,
            tmp_path,
            exchange=_EXCHANGE,
            segment=_SEGMENT,
            symbol=_SYMBOL,
            interval=_INTERVAL,
            limit=50,
        )
        assert page["total"] == 4  # DISTINCT timestamps, not the 6 physical rows
        assert len(page["rows"]) == 4
        close_idx = page["columns"].index("close")
        assert [row[close_idx] for row in page["rows"]] == [100.0, 101.0, 102.0, 103.0]

    def test_db_option_pager_dedups_page_and_total(self, tmp_path: Path) -> None:
        import duckdb
        from brutex.dashboard.db_view import query_option_candles

        series = "NSE_FNO-NIFTY-WEEK-1-ATM_P0-CALL"
        dest = tmp_path / "options" / series / "2024-01.parquet"
        dest.parent.mkdir(parents=True, exist_ok=True)
        pl.DataFrame(
            {
                "underlying_symbol": ["NIFTY"] * 4,
                "exch_id": ["NSE"] * 4,
                "expiry_flag": ["WEEK"] * 4,
                "expiry_code": [1] * 4,
                "strike_offset": ["ATM+0"] * 4,
                "side": ["CALL", "CALL", "CALL", "PUT"],
                "strike": [21000.0] * 4,
                # (CALL, T0) is duplicated with a CONFLICTING close; (PUT, T0)
                # shares the timestamp but is a DISTINCT composite identity.
                "timestamp": [_T0, _T0, _T0 + timedelta(minutes=1), _T0],
                "open": [10.0, 90.0, 11.0, 12.0],
                "high": [10.5, 99.0, 11.5, 12.5],
                "low": [9.5, 1.0, 10.5, 11.5],
                "close": [10.2, 77.0, 11.2, 12.2],
                "volume": [100, 999, 110, 120],
                "open_interest": [1000, 9999, 1100, 1200],
                "spot": [21000.0] * 4,
                "iv": [0.12, 0.99, 0.13, 0.14],
            }
        ).write_parquet(dest)
        conn = duckdb.connect()
        page = query_option_candles(conn, tmp_path, series=series, limit=50)
        assert page["total"] == 3  # DISTINCT (side, timestamp), not 4 physical rows
        assert len(page["rows"]) == 3
        close_idx = page["columns"].index("close")
        side_idx = page["columns"].index("side")
        served = {(row[side_idx], row[close_idx]) for row in page["rows"]}
        # Keep-FIRST: the conflicting (CALL, T0) re-statement (close 77.0) lost.
        assert served == {("CALL", 10.2), ("CALL", 11.2), ("PUT", 12.2)}


# ────────────────────────────────────────────────────────────────────────
# Direct option-lake reader (executor seam) — keep-FIRST leg pricing
# ────────────────────────────────────────────────────────────────────────


class TestOptionLakeSourceKeepFirst:
    def test_option_lake_source_keeps_first_bar(self, tmp_path: Path) -> None:
        from brutex.options.lake_source import LakeOptionBarSource
        from brutex.options.lot_size_history import OptionsUnderlying

        symbol = "NSE-NIFTY-25Jan24-21000-CE"
        dest = tmp_path / "bars" / "NSE" / "FNO" / symbol / "1minute" / "2024" / "01.parquet"
        dest.parent.mkdir(parents=True, exist_ok=True)
        entry_ts = _T0
        exit_ts = _T0 + timedelta(minutes=1)
        pl.DataFrame(
            {
                # The entry instant is duplicated with a CONFLICTING re-statement
                # stored AFTER the real bar — the OLD reader was last-wins here.
                "timestamp": [entry_ts, entry_ts, exit_ts],
                "open": [50.0, 500.0, 51.0],
                "high": [50.5, 999.0, 51.5],
                "low": [49.5, 1.0, 50.5],
                "close": [50.2, 777.0, 51.2],
                "volume": [10, 999, 11],
                "open_interest": [100, 9999, 110],
            }
        ).with_columns(pl.col("open_interest").cast(pl.Int64)).write_parquet(dest)

        source = LakeOptionBarSource(tmp_path)
        with _capture_logs() as logs:
            pair = source.load_pair(
                instrument=OptionsUnderlying.NIFTY,
                expiry_date=datetime(2024, 1, 25, tzinfo=UTC).date(),
                strike=21000,
                option_type="CE",
                entry_ts=entry_ts,
                exit_ts=exit_ts,
            )
        assert pair is not None
        # Keep-FIRST: the real first stored bar priced the leg, not the 777 re-statement.
        assert pair.entry_bar.close == Decimal("50.2")
        assert pair.exit_bar.close == Decimal("51.2")
        events = _dedup_events(logs)
        assert len(events) == 1
        assert events[0]["dropped"] == 1
        assert events[0]["conflict_count"] == 1


# ────────────────────────────────────────────────────────────────────────
# Remaining wired readers — spot join / greeks read / movers daily / coverage
# ────────────────────────────────────────────────────────────────────────


class TestOtherWiredReaders:
    def test_spot_join_keeps_first_close(self, tmp_path: Path) -> None:
        """spot_close_by_ts_window was LAST-wins on a duplicated instant (fails-before)."""
        from brutex.data.spot_join import spot_close_by_ts_window

        base = tmp_path / "bars" / "NSE" / "INDEX" / "NIFTY" / "1minute" / "2024" / "01"
        base.parent.mkdir(parents=True, exist_ok=True)
        pl.DataFrame(
            {
                "timestamp": [_T0, _T0, _T0 + timedelta(minutes=1)],
                "close": [21500.0, 99999.0, 21501.0],
            }
        ).write_parquet(base.parent / "01.parquet")
        join = spot_close_by_ts_window(
            tmp_path,
            exchange="NSE",
            underlying="NIFTY",
            interval="1minute",
            start=_T0.date(),
            end=(_T0 + timedelta(days=1)).date(),
        )
        assert join.close_by_ts[_T0] == Decimal("21500.0")  # the FIRST stored close

    def test_greeks_read_option_rows_dedups_merged_frame(self, tmp_path: Path) -> None:
        from brutex.data.greeks_enrich import GreeksEnrichTarget, read_option_rows

        symbol = "NSE-NIFTY-25Jan24-21000-CE"
        dest = tmp_path / "bars" / "NSE" / "FNO" / symbol / "1minute" / "2024" / "01.parquet"
        dest.parent.mkdir(parents=True, exist_ok=True)
        pl.DataFrame(
            {
                "timestamp": [_T0, _T0, _T0 + timedelta(minutes=1)],
                "open": [50.0, 500.0, 51.0],
                "high": [50.5, 999.0, 51.5],
                "low": [49.5, 1.0, 50.5],
                "close": [50.2, 777.0, 51.2],
                "volume": [10, 999, 11],
                "open_interest": [100, 9999, 110],
            }
        ).with_columns(pl.col("open_interest").cast(pl.Int64)).write_parquet(dest)
        target = GreeksEnrichTarget(
            exchange="NSE",
            symbol=symbol,
            start=_T0.date(),
            end=(_T0 + timedelta(days=1)).date(),
        )
        rows = read_option_rows(tmp_path, target)
        assert len(rows) == 2  # the duplicate collapsed
        assert [float(str(r["close"])) for r in rows] == [50.2, 51.2]  # keep-FIRST

    def test_movers_daily_partition_dedups(self, tmp_path: Path) -> None:
        from brutex.dashboard.movers_snapshot import _read_daily_partition

        day_ts = datetime(2024, 1, 2, 18, 30, tzinfo=UTC)  # midnight IST 2024-01-03
        dest = tmp_path / "bars" / "NSE" / "INDEX" / "NSE-NIFTY" / "1day" / "2024" / "01.parquet"
        dest.parent.mkdir(parents=True, exist_ok=True)
        pl.DataFrame(
            {
                "timestamp": [day_ts, day_ts],
                "open": [100.0, 500.0],
                "high": [101.0, 999.0],
                "low": [99.0, 1.0],
                "close": [100.5, 777.0],
                "volume": [10, 999],
                "open_interest": [None, None],
            }
        ).with_columns(pl.col("open_interest").cast(pl.Int64)).write_parquet(dest)
        frame = _read_daily_partition(tmp_path, exchange="NSE", segment="INDEX", symbol="NSE-NIFTY")
        assert frame is not None
        assert frame.height == 1
        assert frame.get_column("close").to_list() == [100.5]  # keep-FIRST

    def test_movers_futures_prev_day_oi_keeps_first(self, tmp_path: Path) -> None:
        from brutex.dashboard.movers_futures import _prev_day_oi

        day_ts = datetime(2024, 1, 2, 18, 30, tzinfo=UTC)
        dest = tmp_path / "bars" / "NSE" / "FNO" / "NSE-NIFTY-FUT" / "1day" / "2024" / "01.parquet"
        dest.parent.mkdir(parents=True, exist_ok=True)
        pl.DataFrame(
            {
                "timestamp": [day_ts, day_ts],
                "open": [100.0, 500.0],
                "high": [101.0, 999.0],
                "low": [99.0, 1.0],
                "close": [100.5, 777.0],
                "volume": [10, 999],
                "open_interest": [1234, 9999],
            }
        ).with_columns(pl.col("open_interest").cast(pl.Int64)).write_parquet(dest)
        oi = _prev_day_oi(
            tmp_path,
            exchange="NSE",
            segment="FNO",
            symbol="NSE-NIFTY-FUT",
            day=datetime(2024, 1, 10, tzinfo=UTC).date(),
        )
        assert oi == 1234  # the FIRST stored bar's OI, not the 9999 re-statement

    def test_completeness_bar_count_is_distinct(self, tmp_path: Path) -> None:
        """summarize_spot_datasets counts DISTINCT timestamps (fails-before: 6)."""
        import duckdb
        from brutex.dashboard.db_view import summarize_spot_datasets

        _write_lake(tmp_path, _poisoned_rows())
        conn = duckdb.connect()
        rows = summarize_spot_datasets(conn, tmp_path)
        assert len(rows) == 1
        assert rows[0]["bars"] == 4  # not the 6 physical rows
