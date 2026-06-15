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


# ───────────────── Bug 2: micro-batch candle re-aggregation ──────────────────
# DEC-LIVE-CANDLE-PRECISION-001 — the ~5s fold writes a candle per slice of a
# minute; the candle must RE-AGGREGATE to the FULL minute (open=first, close=last,
# high=max, low=min, tick_count=true count) and survive a double-fold unchanged.

_MINUTE = datetime(2026, 6, 15, 3, 45, tzinfo=UTC)  # 09:15 IST minute label


def _slice(seq0: int, samples: list[tuple[int, float]]) -> list[TickRecord]:
    """Build a ~5s slice of TickRecords: (second-in-minute, NIFTY price) samples."""
    return [
        TickRecord(
            seq=seq0 + i,
            recv_ts=datetime(2026, 6, 15, 3, 45, sec, tzinfo=UTC),
            snapshot={"NSE:IDX:NIFTY": price},
            recv_ns=1718440510000000000 + (seq0 + i),
        )
        for i, (sec, price) in enumerate(samples)
    ]


def _fold(records: list[TickRecord], *, lake_root, run_id: str = "run1") -> None:
    """Mimic `rolling.fold_segment` order: ticks FIRST, then candles for THIS slice."""
    write_live_ticks(records, lake_root=lake_root, capture_run_id=run_id)
    candles = build_minute_candles(iter_ticks(records))
    write_live_candles(
        candles,
        lake_root=lake_root,
        built_run_id=run_id,
        tick_counts=minute_tick_counts(records),
    )


def test_multi_fold_aggregates_to_full_minute(tmp_path) -> None:
    """Three ~5s slices of one minute fold to the FULL 1-minute candle (Bug 2 fix).

    The plain `keep="first"` dedup kept only the FIRST slice (tick_count=1, truncated
    OHLC). The re-aggregation rebuilds from the deduped tick lake: open = the minute's
    first tick, close = its last, high = max over all slices, low = min, tick_count =
    the true per-minute count — matching a single-shot fold of the same ticks.
    """
    s1 = _slice(0, [(5, 22500.0), (8, 22505.0)])  # open 22500, a high so far
    s2 = _slice(2, [(20, 22490.0), (25, 22520.0)])  # the low and a new high
    s3 = _slice(4, [(50, 22515.0), (58, 22510.0)])  # close 22510
    for seg in (s1, s2, s3):
        _fold(seg, lake_root=tmp_path)

    frame = read_live_candles(tmp_path)
    assert frame.shape[0] == 1  # ONE minute, not three slices
    row = frame.to_dicts()[0]
    assert row["instrument_key"] == "NSE:IDX:NIFTY" and row["timestamp"] == _MINUTE
    assert row["open"] == 22500.0  # FIRST slice's first tick
    assert row["close"] == 22510.0  # LAST slice's last tick
    assert row["high"] == 22520.0 and row["low"] == 22490.0  # max / min across slices
    assert row["tick_count"] == 6  # SUM of the true per-minute deduped tick count

    # The aggregate matches a SINGLE-SHOT fold of the exact same ticks.
    one_shot = tmp_path / "oneshot"
    _fold(s1 + s2 + s3, lake_root=one_shot)
    assert read_live_candles(one_shot).to_dicts()[0] == row


def test_double_fold_same_segment_is_idempotent(tmp_path) -> None:
    """Folding the SAME segment twice does NOT change the candle (no double count).

    A crash-replay re-folds an already-folded segment. The tick lake dedups on its
    (capture_run_id, seq, instrument_key) PK, and the candle is REBUILT from that
    deduped set — so a re-fold reads the identical tick set and rebuilds the identical
    bar. tick_count must NOT double (the idempotence the prompt requires).
    """
    s1 = _slice(0, [(5, 22500.0), (8, 22505.0)])
    s2 = _slice(2, [(20, 22490.0), (25, 22520.0)])
    _fold(s1, lake_root=tmp_path)
    _fold(s2, lake_root=tmp_path)
    once = read_live_candles(tmp_path).to_dicts()

    # Re-fold BOTH segments (the crash-replay) — must be a no-op on the candle.
    _fold(s1, lake_root=tmp_path)
    _fold(s2, lake_root=tmp_path)
    twice = read_live_candles(tmp_path).to_dicts()
    assert twice == once
    assert twice[0]["tick_count"] == 4  # NOT 8 — the deduped count, unchanged


def test_candle_rebuilt_from_deduped_ticks_not_passed_partial(tmp_path) -> None:
    """When the tick lake has the minute, the rebuild OVERRIDES a partial passed candle.

    Even if `write_live_candles` is handed a candle for only ONE slice, the affected
    minute is rebuilt from the FULL deduped tick lake — so a late-arriving slice that
    was folded into ticks but NOT into the passed candle still lands in the bar.
    """
    full = _slice(0, [(5, 100.0), (30, 90.0), (58, 110.0)])
    write_live_ticks(full, lake_root=tmp_path, capture_run_id="run1")
    # Pass a candle built from ONLY the first tick (a partial ~5s slice view).
    partial = build_minute_candles(iter_ticks(full[:1]))
    write_live_candles(
        partial,
        lake_root=tmp_path,
        built_run_id="run1",
        tick_counts=minute_tick_counts(full[:1]),
    )
    row = read_live_candles(tmp_path).to_dicts()[0]
    assert row["open"] == 100.0 and row["close"] == 110.0  # full minute
    assert row["high"] == 110.0 and row["low"] == 90.0
    assert row["tick_count"] == 3  # the deduped full-minute count, not 1


def test_rebuild_keeps_only_affected_instruments(tmp_path) -> None:
    """An instrument in the tick lake but NOT in this fold's candle is fully dropped.

    The tick lake holds BANKNIFTY (a whole separate instrument) plus NIFTY, but THIS
    candle write only touches NIFTY. The rebuild folds every lake instrument, then for
    BANKNIFTY finds NONE of its bars are in `affected` → `in_scope` is empty → the
    instrument is skipped entirely (the `if in_scope:` FALSE branch). It is written
    only when its own fold arrives.
    """
    nifty = _slice(0, [(5, 100.0), (8, 101.0)])
    other = [  # BANKNIFTY — present in the tick lake, NOT in the NIFTY-only candle write
        TickRecord(
            seq=10,
            recv_ts=datetime(2026, 6, 15, 3, 45, 6, tzinfo=UTC),
            snapshot={"NSE:IDX:BANKNIFTY": 48000.0},
            recv_ns=1718440560000000010,
        )
    ]
    write_live_ticks(nifty + other, lake_root=tmp_path, capture_run_id="run1")
    # Write candles for NIFTY ONLY (the rebuild SEES BANKNIFTY in the lake but skips it).
    write_live_candles(
        build_minute_candles(iter_ticks(nifty)),
        lake_root=tmp_path,
        built_run_id="run1",
        tick_counts=minute_tick_counts(nifty),
    )
    frame = read_live_candles(tmp_path)
    assert frame["instrument_key"].to_list() == ["NSE:IDX:NIFTY"]  # BANKNIFTY skipped


def test_candles_without_tick_lake_fall_back_to_passed_frame(tmp_path) -> None:
    """With NO ticks persisted, the passed candle is written as-is (batch/test path).

    The pure-candle path (no `write_live_ticks` first) has nothing to rebuild from, so
    `_merge_candles` falls back to the candle as handed in — preserving the existing
    `candles_to_frame`-only contract used by the older tests.
    """
    candles = build_minute_candles(iter_ticks(_records()))
    counts = minute_tick_counts(_records())
    write_live_candles(candles, lake_root=tmp_path, built_run_id="run1", tick_counts=counts)
    frame = read_live_candles(tmp_path)
    assert frame.shape[0] == 2  # NIFTY + BANKNIFTY, the passed candles verbatim
    nifty = frame.filter(frame["instrument_key"] == "NSE:IDX:NIFTY").to_dicts()[0]
    assert nifty["open"] == 22500.50 and nifty["close"] == 22510.0 and nifty["tick_count"] == 2
