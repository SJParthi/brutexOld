"""Tests for the live Parquet lake writer/reader (:mod:`brutex.live.lake`)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from brutex.data.models import CandleBar
from brutex.live.candle_builder import build_minute_candles
from brutex.live.lake import (
    CANDLE_SCHEMA,
    candles_to_frame,
    minute_tick_counts,
    read_live_candles,
    read_live_ticks,
    split_instrument_key,
    ticks_to_frame,
    write_live_candles,
    write_live_ticks,
)
from brutex.live.store import TickRecord
from brutex.live.tick import iter_ticks

_T0 = datetime(2026, 6, 15, 3, 45, 10, tzinfo=UTC)  # 09:15:10 IST
_T1 = datetime(2026, 6, 15, 3, 45, 50, tzinfo=UTC)  # 09:15:50 IST


def _records() -> list[TickRecord]:
    return [
        TickRecord(
            seq=0,
            recv_ts=_T0,
            snapshot={"NSE:IDX:NIFTY": 22500.499, "NSE:IDX:BANKNIFTY": 48000.0},
            recv_ns=1718440510000000001,
        ),
        TickRecord(
            seq=1, recv_ts=_T1, snapshot={"NSE:IDX:NIFTY": 22510.0}, recv_ns=1718440550000000002
        ),
    ]


def test_split_instrument_key_branches() -> None:
    assert split_instrument_key("NSE:IDX:NIFTY") == ("NSE", "IDX", "NIFTY")
    assert split_instrument_key("A:B:C:D") == ("A", "B", "C:D")
    assert split_instrument_key("NIFTY") == ("", "", "NIFTY")


def test_ticks_to_frame_full_precision_and_recv_ns() -> None:
    frame = ticks_to_frame(_records(), capture_run_id="run1", feed_kind="index")
    assert frame.shape[0] == 3
    nifty = frame.filter(frame["instrument_key"] == "NSE:IDX:NIFTY").sort("seq")
    assert nifty["value"].to_list() == [22500.499, 22510.0]  # RAW — ticks are not rounded
    assert nifty["recv_ns"].to_list()[0] == 1718440510000000001  # OURS — nanoseconds
    assert nifty["vendor_ts_ms"].to_list()[0] is None  # flat snapshot has no tsInMillis
    assert nifty["groww_symbol"].to_list()[0] == "NSE-NIFTY"
    assert str(nifty["session_date"].to_list()[0]) == "2026-06-15"
    assert "NSE:IDX:NIFTY" in frame["raw_json"].to_list()[0]


def test_ticks_to_frame_vendor_ts_untampered_and_volume_oi() -> None:
    """A real leaf carries the UNTAMPERED tsInMillis + volume/OI (LTP feed)."""
    rec = TickRecord(
        seq=0,
        recv_ts=_T0,
        recv_ns=7,
        snapshot={
            "NSE": {
                "FNO": {
                    "NIFTY": {
                        "tsInMillis": 1718440510123.0,
                        "ltp": 22500.5,
                        "value": 22500.5,
                        "volume": 1234.0,
                        "openInterest": 56.0,
                    }
                }
            }
        },
    )
    row = ticks_to_frame([rec], capture_run_id="r", feed_kind="ltp").to_dicts()[0]
    assert row["instrument_key"] == "NSE:FNO:NIFTY"
    assert row["vendor_ts_ms"] == 1718440510123.0  # milliseconds, NEVER edited
    assert row["value"] == 22500.5 and row["volume"] == 1234 and row["open_interest"] == 56
    assert row["exchange"] == "NSE" and row["segment"] == "FNO" and row["recv_ns"] == 7


def test_minute_tick_counts() -> None:
    counts = minute_tick_counts(_records())
    minute = datetime(2026, 6, 15, 3, 45, tzinfo=UTC)
    assert counts[("NSE:IDX:NIFTY", minute)] == 2
    assert counts[("NSE:IDX:BANKNIFTY", minute)] == 1


def test_candles_to_frame_with_and_without_counts() -> None:
    candles = build_minute_candles(iter_ticks(_records()))
    counts = minute_tick_counts(_records())
    frame = candles_to_frame(candles, built_run_id="run1", tick_counts=counts)
    nifty = frame.filter(frame["instrument_key"] == "NSE:IDX:NIFTY")
    row = nifty.to_dicts()[0]
    assert row["open"] == 22500.50 and row["close"] == 22510.0
    assert row["tick_count"] == 2 and row["source"] == "live" and row["volume"] == 0
    assert row["open_interest"] is None
    # without counts → defaults to 0
    bare = candles_to_frame(candles, built_run_id="run1")
    assert bare.filter(bare["instrument_key"] == "NSE:IDX:NIFTY").to_dicts()[0]["tick_count"] == 0


def test_write_read_ticks_roundtrip_and_idempotent(tmp_path) -> None:
    records = _records()
    paths = write_live_ticks(records, lake_root=tmp_path, capture_run_id="run1")
    assert len(paths) == 1 and paths[0].exists()
    first = read_live_ticks(tmp_path)
    assert first.shape[0] == 3
    # re-writing the SAME run is idempotent (dedup on the PK).
    write_live_ticks(records, lake_root=tmp_path, capture_run_id="run1")
    assert read_live_ticks(tmp_path).shape[0] == 3
    # a DIFFERENT run adds rows (distinct capture_run_id → distinct PK).
    write_live_ticks(records, lake_root=tmp_path, capture_run_id="run2")
    assert read_live_ticks(tmp_path).shape[0] == 6


def test_write_read_candles_roundtrip(tmp_path) -> None:
    candles = build_minute_candles(iter_ticks(_records()))
    counts = minute_tick_counts(_records())
    paths = write_live_candles(candles, lake_root=tmp_path, built_run_id="run1", tick_counts=counts)
    assert len(paths) == 1
    frame = read_live_candles(tmp_path)
    assert frame.shape[0] == 2  # NIFTY + BANKNIFTY, one minute each
    assert set(frame["instrument_key"].to_list()) == {"NSE:IDX:NIFTY", "NSE:IDX:BANKNIFTY"}


def test_read_empty_lake_returns_typed_empty_frame(tmp_path) -> None:
    ticks = read_live_ticks(tmp_path)
    candles = read_live_candles(tmp_path)
    assert ticks.shape[0] == 0 and candles.shape[0] == 0
    assert set(candles.columns) == set(CANDLE_SCHEMA)


def test_multi_day_partitions(tmp_path) -> None:
    day2 = datetime(2026, 6, 16, 3, 45, 10, tzinfo=UTC)
    records = [
        TickRecord(seq=0, recv_ts=_T0, snapshot={"NSE:IDX:NIFTY": 100.0}),
        TickRecord(seq=1, recv_ts=day2, snapshot={"NSE:IDX:NIFTY": 200.0}),
    ]
    paths = write_live_ticks(records, lake_root=tmp_path, capture_run_id="r")
    assert len(paths) == 2  # one parquet per IST session date
    assert read_live_ticks(tmp_path).shape[0] == 2


def test_candle_from_decimal_bar(tmp_path) -> None:
    bar = CandleBar(
        timestamp=datetime(2026, 6, 15, 3, 45, tzinfo=UTC),
        open=Decimal("100.005"),
        high=Decimal("100.005"),
        low=Decimal("100.005"),
        close=Decimal("100.005"),
        volume=0,
        open_interest=None,
    )
    frame = candles_to_frame({"NSE:IDX:NIFTY": [bar]}, built_run_id="r")
    assert frame.to_dicts()[0]["open"] == 100.01  # ROUND_HALF_UP at the lake boundary
