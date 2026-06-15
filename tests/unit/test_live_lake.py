"""Tests for the live Parquet lake writer/reader (:mod:`brutex.live.lake`)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from brutex.data.models import CandleBar
from brutex.live.candle_builder import build_minute_candles
from brutex.live.lake import (
    CANDLE_SCHEMA,
    TICK_SCHEMA,
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
from brutex.live.tick import iter_ticks, payload_hash

# 1718440510123 ms == 2024-06-15 08:35:10.123 UTC == 14:05:10.123 IST.
_VENDOR_MS = 1718440510123.0
_EXCHANGE_UTC = datetime(2024, 6, 15, 8, 35, 10, 123000, tzinfo=UTC)

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


def test_ticks_to_frame_price_2dp_snapped_and_recv_ns() -> None:
    frame = ticks_to_frame(_records(), capture_run_id="run1", feed_kind="index")
    assert frame.shape[0] == 3
    nifty = frame.filter(frame["instrument_key"] == "NSE:IDX:NIFTY").sort("seq")
    # Price column is snapped to 2dp, ROUND_HALF_UP (22500.499 → 22500.50) — the
    # Dhan/Groww/NSE standard (DEC-SPOT-PRICE-2DP-001). The EXACT raw value is kept
    # in raw_json for audit/re-parse, so nothing is lost.
    assert nifty["value"].to_list() == [22500.5, 22510.0]
    assert "22500.499" in frame["raw_json"].to_list()[0]  # exact raw preserved
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


# ─────────── exchange-time bucketing (DEC-LIVE-EXCHANGE/BUCKET-TS-IST-001) ───────────


def _leaf_rec(seq: int, recv: datetime, price: float, *, ts_ms: float | None) -> TickRecord:
    """A NIFTY index leaf record: optional vendor tsInMillis → derives exchange_ts."""
    leaf: dict[str, object] = {"value": price}
    if ts_ms is not None:
        leaf["tsInMillis"] = ts_ms
    return TickRecord(
        seq=seq,
        recv_ts=recv,
        snapshot={"NSE": {"IDX": {"NIFTY": leaf}}},
        recv_ns=1718440510000000000 + seq,
    )


def test_ticks_frame_has_exchange_ts_column_derived_and_null() -> None:
    """The lake stamps exchange_ts (ms-precise) for a vendor leaf, null for a flat one."""
    records = [
        _leaf_rec(0, _T0, 22500.5, ts_ms=_VENDOR_MS),  # derives exchange_ts
        TickRecord(seq=1, recv_ts=_T1, snapshot={"FLAT": 100.0}),  # flat → null
    ]
    frame = ticks_to_frame(records, capture_run_id="r", feed_kind="index")
    assert "exchange_ts" in frame.columns
    nifty = frame.filter(frame["instrument_key"] == "NSE:IDX:NIFTY").to_dicts()[0]
    assert nifty["exchange_ts"] == _EXCHANGE_UTC  # ms preserved
    assert nifty["vendor_ts_ms"] == _VENDOR_MS  # raw untampered alongside
    flat = frame.filter(frame["instrument_key"] == "FLAT").to_dicts()[0]
    assert flat["exchange_ts"] is None  # no tsInMillis → null, recv_ts fallback


def test_minute_tick_counts_use_exchange_minute() -> None:
    """minute_tick_counts buckets by the exchange minute, not OUR recv minute.

    Both leaves are stamped by the exchange inside the 09:15 IST minute
    (03:45 UTC) but received across the 03:45 / 03:46 wall-clock boundary; the
    count must collapse them into the single 03:45 exchange minute.
    """
    # 09:15:30 IST == 03:45:30 UTC; 09:15:55 IST == 03:45:55 UTC.
    e30 = datetime(2026, 6, 15, 3, 45, 30, tzinfo=UTC).timestamp() * 1000.0
    e55 = datetime(2026, 6, 15, 3, 45, 55, tzinfo=UTC).timestamp() * 1000.0
    records = [
        _leaf_rec(0, datetime(2026, 6, 15, 3, 45, 31, tzinfo=UTC), 100.0, ts_ms=e30),
        _leaf_rec(1, datetime(2026, 6, 15, 3, 46, 2, tzinfo=UTC), 110.0, ts_ms=e55),
    ]
    counts = minute_tick_counts(records)
    minute = datetime(2026, 6, 15, 3, 45, tzinfo=UTC)
    assert counts[("NSE:IDX:NIFTY", minute)] == 2  # one 03:45 bucket, not two
    assert (datetime(2026, 6, 15, 3, 46, tzinfo=UTC),) not in {(k[1],) for k in counts}


def _ms(recv: datetime) -> float:
    """Vendor tsInMillis equal to *recv* (so exchange minute == recv minute here)."""
    return recv.timestamp() * 1000.0


def _exch_slice(seq0: int, samples: list[tuple[int, float]]) -> list[TickRecord]:
    """A ~5s slice of NIFTY index leaves carrying a tsInMillis == recv_ts."""
    out: list[TickRecord] = []
    for i, (sec, price) in enumerate(samples):
        recv = datetime(2026, 6, 15, 3, 45, sec, tzinfo=UTC)
        out.append(_leaf_rec(seq0 + i, recv, price, ts_ms=_ms(recv)))
    return out


def _fold_exch(records: list[TickRecord], *, lake_root, run_id: str = "run1") -> None:
    write_live_ticks(records, lake_root=lake_root, capture_run_id=run_id)
    candles = build_minute_candles(iter_ticks(records))
    write_live_candles(
        candles, lake_root=lake_root, built_run_id=run_id, tick_counts=minute_tick_counts(records)
    )


def test_micro_batch_fold_equals_single_shot_under_exchange_clock(tmp_path) -> None:
    """Multi-slice micro-batch fold == single-shot fold under the exchange clock."""
    s1 = _exch_slice(0, [(5, 22500.0), (8, 22505.0)])
    s2 = _exch_slice(2, [(20, 22490.0), (25, 22520.0)])
    s3 = _exch_slice(4, [(50, 22515.0), (58, 22510.0)])
    for seg in (s1, s2, s3):
        _fold_exch(seg, lake_root=tmp_path)
    multi = read_live_candles(tmp_path).to_dicts()

    one_shot = tmp_path / "oneshot"
    _fold_exch(s1 + s2 + s3, lake_root=one_shot)
    single = read_live_candles(one_shot).to_dicts()
    assert multi == single
    assert len(multi) == 1 and multi[0]["tick_count"] == 6
    assert multi[0]["open"] == 22500.0 and multi[0]["close"] == 22510.0


def test_double_fold_idempotent_under_exchange_clock(tmp_path) -> None:
    """A re-fold under exchange-ts bucketing is byte-identical (tick_count not doubled)."""
    s1 = _exch_slice(0, [(5, 22500.0), (8, 22505.0)])
    s2 = _exch_slice(2, [(20, 22490.0), (25, 22520.0)])
    _fold_exch(s1, lake_root=tmp_path)
    _fold_exch(s2, lake_root=tmp_path)
    once = read_live_candles(tmp_path).to_dicts()
    _fold_exch(s1, lake_root=tmp_path)  # crash-replay
    _fold_exch(s2, lake_root=tmp_path)
    twice = read_live_candles(tmp_path).to_dicts()
    assert twice == once
    assert twice[0]["tick_count"] == 4  # NOT 8


def test_rebuild_buckets_late_recv_tick_by_exchange_minute(tmp_path) -> None:
    """A tick the exchange stamped at 09:15:58 but we received at 09:16 lands in 09:15.

    The whole lake path (write ticks → rebuild candle from the deduped lake) must
    bucket by exchange_ts read back from the tick lake, so a snapshot-cadence delay
    never spills a 09:15 trade into a phantom 09:16 bar (DEC-LIVE-CANDLE-BUCKET-IST-001).
    """
    e_open = datetime(2026, 6, 15, 3, 45, 5, tzinfo=UTC).timestamp() * 1000.0
    e_late = datetime(2026, 6, 15, 3, 45, 58, tzinfo=UTC).timestamp() * 1000.0  # 09:15:58 IST
    records = [
        _leaf_rec(0, datetime(2026, 6, 15, 3, 45, 6, tzinfo=UTC), 100.0, ts_ms=e_open),
        _leaf_rec(1, datetime(2026, 6, 15, 3, 46, 1, tzinfo=UTC), 110.0, ts_ms=e_late),
    ]
    _fold_exch(records, lake_root=tmp_path)
    frame = read_live_candles(tmp_path)
    assert frame.shape[0] == 1  # ONE bar — both ticks in the 09:15 exchange minute
    row = frame.to_dicts()[0]
    assert row["timestamp"] == datetime(2026, 6, 15, 3, 45, tzinfo=UTC)
    assert row["open"] == 100.0 and row["close"] == 110.0 and row["tick_count"] == 2


def test_index_leaf_buckets_by_exchange_a_no_ts_leaf_by_recv(tmp_path) -> None:
    """An index leaf (tsInMillis) buckets by exchange minute; a leaf without it by recv."""
    e_ms = datetime(2026, 6, 15, 3, 45, 40, tzinfo=UTC).timestamp() * 1000.0  # 09:15:40 IST
    records = [
        # NIFTY: exchange 09:15:40, received in the 09:16 wall-clock minute.
        _leaf_rec(0, datetime(2026, 6, 15, 3, 46, 3, tzinfo=UTC), 22500.0, ts_ms=e_ms),
        # A flat leaf with NO tsInMillis, received at 09:16:03 → recv-minute bucket.
        TickRecord(
            seq=1,
            recv_ts=datetime(2026, 6, 15, 3, 46, 3, tzinfo=UTC),
            snapshot={"NSE:IDX:BANKNIFTY": 48000.0},
            recv_ns=1718440560000000001,
        ),
    ]
    _fold_exch(records, lake_root=tmp_path)
    frame = read_live_candles(tmp_path).sort("instrument_key")
    rows = {r["instrument_key"]: r["timestamp"] for r in frame.to_dicts()}
    assert rows["NSE:IDX:NIFTY"] == datetime(2026, 6, 15, 3, 45, tzinfo=UTC)  # exchange minute
    assert rows["NSE:IDX:BANKNIFTY"] == datetime(2026, 6, 15, 3, 46, tzinfo=UTC)  # recv fallback


# ─── tick parity fields: avg_price + payload_hash (DEC-LIVE-TICK-FIELDS-PARITY-001) ───


def _equity_rec(seq: int, recv: datetime, *, ltp: float, avg: float | None) -> TickRecord:
    """A Groww LTP equity leaf record, optionally carrying an avgPrice."""
    leaf: dict[str, object] = {"ltp": ltp, "tsInMillis": 1718440510123.0, "volume": 7.0}
    if avg is not None:
        leaf["avgPrice"] = avg
    return TickRecord(
        seq=seq, recv_ts=recv, snapshot={"NSE": {"CASH": {"RELIANCE": leaf}}}, recv_ns=seq + 1
    )


def test_tick_schema_has_avg_price_and_payload_hash_columns() -> None:
    """The additive columns exist in TICK_SCHEMA without disturbing the prior columns."""
    assert "avg_price" in TICK_SCHEMA and "payload_hash" in TICK_SCHEMA
    # Prior columns untouched (a sanity subset of the #756 baseline).
    for col in ("value", "vendor_ts_ms", "exchange_ts", "volume", "open_interest", "raw_json"):
        assert col in TICK_SCHEMA


def test_ticks_to_frame_avg_price_2dp_and_payload_hash() -> None:
    """A leaf with avgPrice → 2dp-snapped avg_price + a blake3 payload_hash."""
    rec = _equity_rec(0, _T0, ltp=2396.7, avg=2400.005)  # avg snaps 2400.005 → 2400.01
    row = ticks_to_frame([rec], capture_run_id="r", feed_kind="ltp").to_dicts()[0]
    assert row["avg_price"] == 2400.01  # ROUND_HALF_UP money snap (DEC-SPOT-PRICE-2DP)
    leaf = {"ltp": 2396.7, "tsInMillis": 1718440510123.0, "volume": 7.0, "avgPrice": 2400.005}
    assert row["payload_hash"] == payload_hash(leaf)


def test_ticks_to_frame_avg_price_null_when_absent() -> None:
    """An index leaf / a leaf with no avgPrice → null avg_price (the None ternary branch)."""
    rec = _equity_rec(0, _T0, ltp=2396.7, avg=None)
    row = ticks_to_frame([rec], capture_run_id="r", feed_kind="ltp").to_dicts()[0]
    assert row["avg_price"] is None  # honest null, never fabricated
    assert row["payload_hash"] is not None  # the leaf still hashes


def test_ticks_to_frame_flat_scalar_has_null_avg_and_hash() -> None:
    """A flat numeric snapshot leaf → null avg_price AND null payload_hash."""
    rec = TickRecord(seq=0, recv_ts=_T0, snapshot={"NSE:IDX:NIFTY": 22500.5}, recv_ns=1)
    row = ticks_to_frame([rec], capture_run_id="r", feed_kind="index").to_dicts()[0]
    assert row["avg_price"] is None and row["payload_hash"] is None


# ─── candle DERIVED percentages (DEC-LIVE-CANDLE-DERIVED-PCT-001) ───


def _pct_bar(*, open_: str, close: str) -> CandleBar:
    """A 1-minute CandleBar with a given open/close (high/low immaterial here)."""
    return CandleBar(
        timestamp=_MINUTE,
        open=Decimal(open_),
        high=Decimal(close),
        low=Decimal(open_),
        close=Decimal(close),
        volume=0,
        open_interest=None,
    )


def test_candle_schema_has_derived_pct_columns() -> None:
    assert "pct_from_open" in CANDLE_SCHEMA and "pct_change" in CANDLE_SCHEMA


def test_candles_to_frame_derived_pcts_full_precision() -> None:
    """pct_from_open + pct_change are FULL precision (never 2dp) — statistics, not money."""
    bar = _pct_bar(open_="100", close="103")  # +3% from open
    frame = candles_to_frame(
        {"NSE:IDX:NIFTY": [bar]},
        built_run_id="r",
        prev_closes={"NSE:IDX:NIFTY": Decimal("99")},  # (103-99)/99*100 = 4.0404...
    )
    row = frame.to_dicts()[0]
    assert row["pct_from_open"] == 3.0
    # FULL precision — NOT snapped to 2dp (would be 4.04); the statistic keeps its tail.
    assert abs(row["pct_change"] - (4.0 / 99.0 * 100 + 0.0)) < 1e-9
    assert row["pct_change"] != round(row["pct_change"], 2)  # genuinely un-rounded


def test_candles_to_frame_pct_change_null_without_prev_close() -> None:
    """No prev_close for the instrument → pct_change null (honest); pct_from_open still set."""
    bar = _pct_bar(open_="100", close="110")
    # prev_closes omitted entirely (None map) AND a present-but-other-instrument map.
    none_map = candles_to_frame({"NSE:IDX:NIFTY": [bar]}, built_run_id="r").to_dicts()[0]
    assert none_map["pct_change"] is None and none_map["pct_from_open"] == 10.0
    other = candles_to_frame(
        {"NSE:IDX:NIFTY": [bar]}, built_run_id="r", prev_closes={"OTHER": Decimal("5")}
    ).to_dicts()[0]
    assert other["pct_change"] is None


def test_candles_to_frame_pct_divide_by_zero_guards() -> None:
    """A 0-rupee open → null pct_from_open; a 0-rupee prev_close → null pct_change."""
    zero_open = _pct_bar(open_="0", close="10")
    row = candles_to_frame(
        {"NSE:IDX:NIFTY": [zero_open]},
        built_run_id="r",
        prev_closes={"NSE:IDX:NIFTY": Decimal("0")},  # zero prev_close
    ).to_dicts()[0]
    assert row["pct_from_open"] is None  # no divide-by-zero, no inf
    assert row["pct_change"] is None


def test_write_live_candles_threads_prev_close_through_tick_rebuild(tmp_path) -> None:
    """pct_change is computed on the tick-lake REBUILD path, idempotent under a double-fold."""
    full = _slice(0, [(5, 100.0), (58, 110.0)])  # open 100, close 110 in the 09:15 minute
    write_live_ticks(full, lake_root=tmp_path, capture_run_id="run1")
    candles = build_minute_candles(iter_ticks(full))
    prevs = {"NSE:IDX:NIFTY": Decimal("100")}  # (110-100)/100*100 = 10.0 ; (100/100..)=0 open
    write_live_candles(
        candles,
        lake_root=tmp_path,
        built_run_id="run1",
        tick_counts=minute_tick_counts(full),
        prev_closes=prevs,
    )
    row = read_live_candles(tmp_path).to_dicts()[0]
    assert row["pct_from_open"] == 10.0  # (110-100)/100*100
    assert row["pct_change"] == 10.0  # (110-100)/100*100 vs prev_close
    # Re-fold (crash replay) with the SAME prev_closes → byte-identical (idempotent).
    write_live_candles(
        candles,
        lake_root=tmp_path,
        built_run_id="run1",
        tick_counts=minute_tick_counts(full),
        prev_closes=prevs,
    )
    assert read_live_candles(tmp_path).to_dicts()[0] == row


def test_pct_columns_populated_on_fallback_no_tick_lake_path(tmp_path) -> None:
    """The no-tick-lake FALLBACK path (passed candle written as-is) still carries pcts.

    `_merge_candles` falls back to the passed `frame` (built by `candles_to_frame`
    WITH prev_closes), so a pure-candle write (no `write_live_ticks` first) still
    persists the derived percentages (DEC-LIVE-CANDLE-DERIVED-PCT-001).
    """
    bar = _pct_bar(open_="100", close="105")  # +5% from open
    write_live_candles(
        {"NSE:IDX:NIFTY": [bar]},
        lake_root=tmp_path,
        built_run_id="run1",
        prev_closes={"NSE:IDX:NIFTY": Decimal("100")},  # (105-100)/100*100 = 5.0
    )
    row = read_live_candles(tmp_path).to_dicts()[0]
    assert row["pct_from_open"] == 5.0 and row["pct_change"] == 5.0
