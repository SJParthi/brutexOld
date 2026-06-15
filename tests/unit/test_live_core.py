"""Unit tests for the Groww live-feed parity CORE (pure modules).

Covers :mod:`brutex.live.tick`, :mod:`~brutex.live.store`,
:mod:`~brutex.live.candle_builder`, :mod:`~brutex.live.parity`,
:mod:`~brutex.live.report`, :mod:`~brutex.live.probe`, and
:mod:`~brutex.live.serde` — every branch, no feed / no network.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from brutex.data.models import CandleBar
from brutex.live.candle_builder import build_minute_candles
from brutex.live.parity import MinuteDiff, ParityReport, compare_candles
from brutex.live.probe import analyze_cadence, probe_connections
from brutex.live.report import render_parity_markdown
from brutex.live.serde import (
    candle_from_dict,
    candle_to_dict,
    candles_from_json,
    candles_to_json,
    read_candles,
    write_candles,
)
from brutex.live.store import TickRecord, TickStore
from brutex.live.tick import (
    LiveTick,
    Quote,
    _coerce_number,
    _leaf_price,
    effective_clock,
    exchange_ts_from_vendor_ms,
    flatten_index_snapshot,
    iter_ticks,
    prune_unchanged,
)


def _bare(price: str) -> Quote:
    """A flat-numeric leaf's Quote (price only — no vendor ts / volume / OI)."""
    return Quote(value=Decimal(price), vendor_ts_ms=None, volume=None, open_interest=None)


# 09:15 IST == 03:45 UTC; 15:29 IST == 09:59 UTC; 15:30 IST == 10:00 UTC.
_OPEN_UTC = datetime(2026, 6, 15, 3, 45, tzinfo=UTC)


def _utc(h: int, m: int, s: int = 0) -> datetime:
    return datetime(2026, 6, 15, h, m, s, tzinfo=UTC)


def _bar(close: str, *, ts: datetime = _OPEN_UTC, **over: object) -> CandleBar:
    price = Decimal(close)
    fields: dict[str, object] = {
        "timestamp": ts,
        "open": price,
        "high": price,
        "low": price,
        "close": price,
        "volume": 0,
        "open_interest": None,
    }
    fields.update(over)
    return CandleBar(**fields)  # type: ignore[arg-type]


# ─────────────────────────── tick.py ────────────────────────────


def test_livetick_rejects_naive_recv_ts() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        LiveTick(recv_ts=datetime(2026, 6, 15, 9, 15), instrument_key="X", value=Decimal("1"))


def test_livetick_rejects_non_finite_value() -> None:
    with pytest.raises(ValueError, match="finite"):
        LiveTick(recv_ts=_OPEN_UTC, instrument_key="X", value=Decimal("NaN"))


def test_coerce_number_branches() -> None:
    assert _coerce_number(True) is None  # bool is rejected before int
    assert _coerce_number(10) == Decimal("10")
    assert _coerce_number(1.5) == Decimal("1.5")
    assert _coerce_number("22500.25") == Decimal("22500.25")
    assert _coerce_number("not-a-number") is None
    assert _coerce_number("Infinity") is None  # parses but not finite
    assert _coerce_number(object()) is None


def test_leaf_price_branches() -> None:
    assert _leaf_price({"value": 100}) == Decimal("100")
    assert _leaf_price({"ltp": "200.5"}) == Decimal("200.5")
    assert _leaf_price({"other": 1}) is None  # no known price key
    assert _leaf_price({"value": "junk"}) is None  # price key but unparseable


def test_leaf_price_equity_uses_ltp_not_value_zero() -> None:
    """A Groww LTP equity leaf reads `ltp`, NOT the `value:0.0` slot (Bug 1 fix).

    The real growwapi equity leaf carries the traded price in `ltp` (e.g. 2396.7)
    while `value` is 0.0 (that field is the day's traded VALUE, not a price). With
    `ltp` before `value` in PRICE_LEAF_KEYS the equity now reads its true price
    instead of 0.0 (DEC-LIVE-CANDLE-PRECISION-001).
    """
    equity = {
        "avgPrice": 0.0,
        "bidQty": 0.0,
        "close": 0.0,
        "high": 0.0,
        "low": 0.0,
        "ltp": 2396.7,
        "open": 0.0,
        "openInterest": 0.0,
        "tsInMillis": 1718440510123.0,
        "value": 0.0,
        "volume": 0.0,
    }
    assert _leaf_price(equity) == Decimal("2396.7")  # NOT 0.0


def test_leaf_price_index_falls_through_to_value() -> None:
    """An INDEX leaf (carries `value`, NO `ltp`) still reads `value` (Bug 1 fix)."""
    index = {"value": 76435.06, "tsInMillis": 1718440510123.0}
    assert _leaf_price(index) == Decimal("76435.06")


def test_leaf_price_untraded_equity_ltp_zero_is_kept() -> None:
    """An un-traded equity with `ltp:0.0` legitimately yields 0 (no trade yet) — kept."""
    untraded = {"ltp": 0.0, "value": 0.0, "tsInMillis": 1718440510123.0}
    assert _leaf_price(untraded) == Decimal("0")  # a real not-yet-traded price, not skipped


def test_flatten_equity_leaf_uses_ltp_for_ticks_and_candles() -> None:
    """An equity leaf flattens to its `ltp` (2396.7), not the `value:0.0` (Bug 1)."""
    from brutex.live.tick import extract_quotes

    snap = {"NSE": {"CASH": {"RELIANCE": {"value": 0.0, "ltp": 2396.7, "tsInMillis": 1.0}}}}
    assert flatten_index_snapshot(snap) == {"NSE:CASH:RELIANCE": Decimal("2396.7")}
    assert extract_quotes(snap)["NSE:CASH:RELIANCE"].value == Decimal("2396.7")


def test_flatten_index_leaf_uses_value_when_no_ltp() -> None:
    """An index leaf (no `ltp`) still flattens to its `value` (76435.06) (Bug 1)."""
    from brutex.live.tick import extract_quotes

    snap = {"BSE": {"IDX": {"SENSEX": {"value": 76435.06, "tsInMillis": 1.0}}}}
    assert flatten_index_snapshot(snap) == {"BSE:IDX:SENSEX": Decimal("76435.06")}
    assert extract_quotes(snap)["BSE:IDX:SENSEX"].value == Decimal("76435.06")


def test_flatten_flat_nested_and_leafdict() -> None:
    flat = flatten_index_snapshot({"NSE_NIFTY": 22500.5, "skip": "x"})
    assert flat == {"NSE_NIFTY": Decimal("22500.5")}

    nested = flatten_index_snapshot({"NSE": {"IDX": {"NIFTY": 22500.5}}})
    assert nested == {"NSE:IDX:NIFTY": Decimal("22500.5")}

    leaf = flatten_index_snapshot({"NSE": {"IDX": {"NIFTY": {"value": 22500.5, "ts": 1}}}})
    assert leaf == {"NSE:IDX:NIFTY": Decimal("22500.5")}


def test_iter_ticks_expands_and_skips_empty() -> None:
    records = [
        TickRecord(seq=0, recv_ts=_OPEN_UTC, snapshot={"NIFTY": 100, "BANKNIFTY": 200}),
        TickRecord(seq=1, recv_ts=_utc(3, 46), snapshot={}),
    ]
    ticks = list(iter_ticks(records))
    assert [(t.instrument_key, t.value) for t in ticks] == [
        ("NIFTY", Decimal("100")),
        ("BANKNIFTY", Decimal("200")),
    ]


def test_extract_quotes_all_shapes_and_vendor_ts() -> None:
    from brutex.live.tick import Quote, extract_quotes

    # flat numeric leaf → value only.
    assert extract_quotes({"NIFTY": 100.0})["NIFTY"] == Quote(
        value=Decimal("100.0"), vendor_ts_ms=None, volume=None, open_interest=None
    )
    # nested index leaf → UNTAMPERED tsInMillis, no volume.
    idx = extract_quotes({"NSE": {"IDX": {"NIFTY": {"tsInMillis": 123.0, "value": 22500.5}}}})
    q = idx["NSE:IDX:NIFTY"]
    assert q.value == Decimal("22500.5") and q.vendor_ts_ms == 123.0 and q.volume is None
    # LTP leaf → volume/OI coerced from the proto's doubles; ms kept.
    ltp = extract_quotes({"X": {"tsInMillis": 5, "value": 10, "volume": 7.0, "openInterest": 3.0}})[
        "X"
    ]
    assert ltp.vendor_ts_ms == 5.0 and ltp.volume == 7 and ltp.open_interest == 3
    # price leaf WITHOUT a vendor timestamp → vendor_ts_ms None.
    assert extract_quotes({"Z": {"value": 5}})["Z"].vendor_ts_ms is None
    # a bool is never a timestamp.
    assert extract_quotes({"Y": {"value": 1, "tsInMillis": True}})["Y"].vendor_ts_ms is None
    # a non-price mapping recurses to nothing; a non-numeric leaf is skipped.
    assert extract_quotes({"a": {"foo": "bar"}}) == {}


def test_negative_price_is_skipped() -> None:
    """A negative index/LTP (vendor sign-flip) is dropped, never a candle (L1)."""
    from brutex.live.tick import extract_quotes, flatten_index_snapshot

    assert flatten_index_snapshot({"NIFTY": -5}) == {}
    assert extract_quotes({"NIFTY": -5}) == {}
    assert flatten_index_snapshot({"X": {"value": -1}}) == {}  # negative inside a leaf dict


def test_schema_drift_leaf_is_skipped_not_recursed() -> None:
    """A leaf with `tsInMillis` but an UNKNOWN price key emits NO phantom keys (H-a)."""
    from brutex.live.tick import extract_quotes, flatten_index_snapshot

    # `lastTradePrice` is not in PRICE_LEAF_KEYS → schema drift; the leaf is
    # skipped (not recursed), so `…:tsInMillis` / `…:x` never become instruments.
    snap = {"NSE": {"IDX": {"NIFTY": {"tsInMillis": 1.0, "lastTradePrice": 22500.5, "x": 9}}}}
    assert flatten_index_snapshot(snap) == {}
    assert extract_quotes(snap) == {}


# ─────────────── exchange_ts derivation (DEC-LIVE-EXCHANGE-TS-IST-001) ───────────────


# 1718440510123 ms == 2024-06-15 08:35:10.123 UTC == 14:05:10.123 IST.
_VENDOR_MS = 1718440510123.0
_EXCHANGE_UTC = datetime(2024, 6, 15, 8, 35, 10, 123000, tzinfo=UTC)


def test_exchange_ts_from_vendor_ms_is_millisecond_precise() -> None:
    """tsInMillis → tz-aware UTC datetime preserving the .mmm (the us column keeps it)."""
    dt = exchange_ts_from_vendor_ms(_VENDOR_MS)
    assert dt == _EXCHANGE_UTC
    assert dt.tzinfo is UTC
    assert dt.microsecond == 123000  # the millisecond survived (no truncation)


def test_exchange_ts_displays_as_ist_utc_plus_530() -> None:
    """The derived exchange instant reads as IST = UTC+05:30 (CLAUDE.md §6, no DST)."""
    from brutex.calendars.nse import IST

    ist = exchange_ts_from_vendor_ms(_VENDOR_MS).astimezone(IST)
    assert ist.isoformat() == "2024-06-15T14:05:10.123000+05:30"
    assert (ist.utcoffset().total_seconds()) == 5.5 * 3600  # fixed +05:30


def test_exchange_ts_none_when_vendor_ts_absent() -> None:
    """A leaf without tsInMillis (None) derives no exchange_ts → recv_ts fallback."""
    assert exchange_ts_from_vendor_ms(None) is None


@pytest.mark.parametrize(
    "bad_ms",
    [
        -1.0,  # negative — vendor sign-flip glitch
        0.0,  # epoch 1970 — pre-2015 (a zero/uninitialised stamp)
        1_400_000_000_000.0,  # 2014-05 — just before the 2015 floor
        99_999_999_999_999.0,  # ~year 5138 — far future
        float("nan"),  # _coerce_ms does NOT reject nan → must not crash fromtimestamp
        float("inf"),  # +inf — far-future via the > now branch
        float("-inf"),  # -inf — caught by the non-finite guard, never the < floor compare
    ],
)
def test_exchange_ts_none_for_implausible_vendor_ms(bad_ms: float) -> None:
    """Implausible tsInMillis (non-finite / negative / pre-2015 / far-future) → None.

    Dropped, NEVER raised — a ``nan`` slips past both the ``<`` floor and ``>`` ceiling
    comparisons (every nan comparison is False) and WOULD crash
    ``datetime.fromtimestamp`` without the explicit finiteness guard.
    """
    assert exchange_ts_from_vendor_ms(bad_ms) is None


def test_exchange_ts_floor_boundary_is_plausible() -> None:
    """The exact 2015-01-01 floor instant is admitted (the >= boundary, not dropped)."""
    floor_ms = datetime(2015, 1, 1, tzinfo=UTC).timestamp() * 1000.0
    assert exchange_ts_from_vendor_ms(floor_ms) == datetime(2015, 1, 1, tzinfo=UTC)


def test_effective_clock_prefers_exchange_ts_else_recv_ts() -> None:
    """effective_clock returns exchange_ts when present, recv_ts when None."""
    with_exch = LiveTick(
        recv_ts=_utc(3, 46), instrument_key="N", value=Decimal("1"), exchange_ts=_utc(3, 45)
    )
    assert effective_clock(with_exch) == _utc(3, 45)
    without = LiveTick(recv_ts=_utc(3, 46), instrument_key="N", value=Decimal("1"))
    assert effective_clock(without) == _utc(3, 46)  # falls back to recv_ts


def test_livetick_rejects_naive_exchange_ts() -> None:
    """A naive exchange_ts is refused (mirrors the recv_ts tz-aware invariant)."""
    with pytest.raises(ValueError, match="exchange_ts must be timezone-aware"):
        LiveTick(
            recv_ts=_OPEN_UTC,
            instrument_key="N",
            value=Decimal("1"),
            exchange_ts=datetime(2026, 6, 15, 9, 15),  # naive
        )


def test_iter_ticks_carries_exchange_ts_from_tsinmillis() -> None:
    """iter_ticks derives exchange_ts from a leaf's tsInMillis; a flat leaf has None."""
    rec = TickRecord(
        seq=0,
        recv_ts=_utc(3, 46),
        snapshot={
            "NSE": {"IDX": {"NIFTY": {"value": 22500.5, "tsInMillis": _VENDOR_MS}}},
            "FLAT": 100.0,  # flat numeric leaf — no tsInMillis
        },
    )
    by_key = {t.instrument_key: t for t in iter_ticks([rec])}
    assert by_key["NSE:IDX:NIFTY"].exchange_ts == _EXCHANGE_UTC
    assert by_key["FLAT"].exchange_ts is None  # fallback path


# ───────────────────── prune_unchanged (delta) ──────────────────


def test_prune_first_snapshot_keeps_all() -> None:
    """The first snapshot (empty baseline) keeps every instrument (DEC-LIVE-DELTA)."""
    snap = {"NIFTY": 100.0, "BANKNIFTY": 200.0}
    pruned, updated = prune_unchanged(snap, {})
    assert pruned == snap
    assert updated == {"NIFTY": _bare("100.0"), "BANKNIFTY": _bare("200.0")}


def test_prune_identical_snapshot_is_empty() -> None:
    """An identical second snapshot prunes to nothing (write NOTHING)."""
    snap = {"NIFTY": 100.0, "BANKNIFTY": 200.0}
    _first, baseline = prune_unchanged(snap, {})
    pruned, updated = prune_unchanged(snap, baseline)
    assert pruned == {}
    assert updated == baseline  # baseline unchanged when nothing moved


def test_prune_partial_change_keeps_only_changed() -> None:
    """Only the instrument(s) whose price moved are kept; others dropped."""
    _first, baseline = prune_unchanged({"NIFTY": 100.0, "BANKNIFTY": 200.0}, {})
    pruned, updated = prune_unchanged({"NIFTY": 100.0, "BANKNIFTY": 201.0}, baseline)
    assert pruned == {"BANKNIFTY": 201.0}  # NIFTY unchanged → dropped
    assert updated == {"NIFTY": _bare("100.0"), "BANKNIFTY": _bare("201.0")}


def test_prune_same_price_new_vendor_ts_is_kept() -> None:
    """Same price but a NEW vendor ``tsInMillis`` is a NEW tick — KEPT, not dropped.

    A liquid equity prints many trades at the same price, each carrying a fresh
    ``tsInMillis`` (and a rising cumulative volume). A price-only delta would drop
    them (lost ticks); the full-leaf :class:`Quote` signature keeps every distinct
    vendor observation — the operator's "save ALL the ticks" rule
    (DEC-LIVE-DELTA-CAPTURE-001).
    """
    leaf1 = {"value": 100.0, "tsInMillis": 1000.0, "volume": 5.0}
    leaf2 = {"value": 100.0, "tsInMillis": 2000.0, "volume": 9.0}
    _first, baseline = prune_unchanged({"NSE": {"FNO": {"RELIANCE": leaf1}}}, {})
    pruned, updated = prune_unchanged({"NSE": {"FNO": {"RELIANCE": leaf2}}}, baseline)
    assert pruned == {"NSE": {"FNO": {"RELIANCE": leaf2}}}  # same price, new ts → kept
    assert updated["NSE:FNO:RELIANCE"].vendor_ts_ms == 2000.0
    assert updated["NSE:FNO:RELIANCE"].volume == 9


def test_prune_kept_leaf_preserves_volume_oi_and_ts() -> None:
    """A kept leaf is copied VERBATIM — tsInMillis / volume / openInterest survive."""
    leaf = {"value": 100.0, "tsInMillis": 1234.0, "volume": 7.0, "openInterest": 3.0}
    snap = {"NSE": {"FNO": {"RELIANCE": leaf}}}
    pruned, _updated = prune_unchanged(snap, {})
    assert pruned == snap  # the full raw leaf, untouched
    assert pruned["NSE"]["FNO"]["RELIANCE"] == leaf


def test_prune_identical_leaf_dict_is_dropped() -> None:
    """A byte-identical leaf-dict re-read (same price/ts/volume/OI) prunes to empty.

    Exercises the :func:`Quote` equality FALSE path: previous observation equals
    the new one, so nothing is written (the redundancy the delta exists to strip).
    """
    leaf = {"value": 100.0, "tsInMillis": 1234.0, "volume": 7.0, "openInterest": 3.0}
    snap = {"NSE": {"FNO": {"RELIANCE": leaf}}}
    _first, baseline = prune_unchanged(snap, {})
    pruned, updated = prune_unchanged(snap, baseline)
    assert pruned == {}  # identical leaf → nothing written
    assert updated == baseline


def test_prune_nested_only_changed_branch_survives() -> None:
    """In a nested snapshot, only the sub-tree holding a changed leaf is rebuilt."""
    snap1 = {"NSE": {"IDX": {"NIFTY": {"value": 1.0}, "BANKNIFTY": {"value": 2.0}}}}
    _first, baseline = prune_unchanged(snap1, {})
    snap2 = {"NSE": {"IDX": {"NIFTY": {"value": 1.0}, "BANKNIFTY": {"value": 9.0}}}}
    pruned, updated = prune_unchanged(snap2, baseline)
    # NIFTY unchanged is dropped; only BANKNIFTY's leaf rides up the same nesting.
    assert pruned == {"NSE": {"IDX": {"BANKNIFTY": {"value": 9.0}}}}
    assert updated["NSE:IDX:NIFTY"] == _bare("1.0")
    assert updated["NSE:IDX:BANKNIFTY"] == _bare("9.0")


def test_prune_empty_nested_branch_is_dropped() -> None:
    """A nested sub-tree that prunes to nothing is omitted, and the loop continues.

    Exercises the `if child:` FALSE branch — the first branch (`NSE`) recurses to
    empty (its only leaf is unchanged) while a later top-level key still changes.
    """
    snap = {"NSE": {"IDX": {"NIFTY": {"value": 1.0}}}, "BSE": {"SENSEX": {"value": 5.0}}}
    _first, baseline = prune_unchanged(snap, {})
    pruned, _updated = prune_unchanged(
        {"NSE": {"IDX": {"NIFTY": {"value": 1.0}}}, "BSE": {"SENSEX": {"value": 6.0}}},
        baseline,
    )
    # NSE sub-tree is all-unchanged → dropped entirely; only the BSE change rides.
    assert pruned == {"BSE": {"SENSEX": {"value": 6.0}}}


def test_prune_skips_drift_and_non_numeric_leaves() -> None:
    """A drifted leaf (tsInMillis, no price) and a junk leaf are pruned out."""
    snap = {"good": 5.0, "junk": "x", "drift": {"tsInMillis": 1.0, "lastTradePrice": 9.0}}
    pruned, updated = prune_unchanged(snap, {})
    assert pruned == {"good": 5.0}  # junk + drift never kept
    assert updated == {"good": _bare("5.0")}


def test_prune_updates_baseline_in_place_returns_same_object() -> None:
    """`prune_unchanged` mutates *last_obs* IN PLACE and returns the SAME dict.

    The O(1)-space contract (no per-callback full-dict copy, Golden Rule #5): a
    changed leaf is written into the passed-in baseline and that very object is
    returned, so the caller's `self._last_obs = updated` is a no-op adoption.
    """
    baseline = {"NIFTY": _bare("100.0")}
    pruned, updated = prune_unchanged({"NIFTY": 101.0}, baseline)
    assert updated is baseline  # SAME object — no copy was made
    assert baseline == {"NIFTY": _bare("101.0")}  # mutated in place to the new price
    assert pruned == {"NIFTY": 101.0}


def test_prune_idle_callback_leaves_baseline_byte_untouched() -> None:
    """An all-unchanged callback keeps nothing → mutates nothing (idle path O(1))."""
    baseline = {"NIFTY": _bare("100.0")}
    before = dict(baseline)
    pruned, updated = prune_unchanged({"NIFTY": 100.0}, baseline)
    assert pruned == {}  # nothing changed → nothing written
    assert updated is baseline and baseline == before  # baseline untouched


# ─────────────────────────── store.py ───────────────────────────


def test_store_bytes_written_matches_file_size_including_non_ascii(tmp_path) -> None:
    """`bytes_written` is the EXACT on-disk byte size — the rotation oracle.

    The rolling sink decides a size-based rotation from this in-process counter
    instead of a per-append ``stat`` syscall, so it MUST equal the file's real
    byte growth — including a non-ASCII snapshot (a ``₹`` rupee sign / Unicode
    name) whose UTF-8 byte length differs from its character length, AND across a
    resume that re-seeds the counter from the existing file size.
    """
    path = tmp_path / "bytes.jsonl"
    with TickStore.open(path) as store:
        assert store.bytes_written == 0
        store.append({"NSE:IDX:NIFTY": 22500.5}, recv_ts=_OPEN_UTC)
        assert store.bytes_written == path.stat().st_size
        store.append({"₹-NIFTY-Ünïçödé": 1.0}, recv_ts=_OPEN_UTC)  # multibyte UTF-8
        assert store.bytes_written == path.stat().st_size
    # Resume re-seeds the counter from the existing file, then keeps tracking exactly.
    with TickStore.open(path, resume=True) as resumed:
        assert resumed.bytes_written == path.stat().st_size
        resumed.append({"X": 2.0}, recv_ts=_OPEN_UTC)
        assert resumed.bytes_written == path.stat().st_size


def test_store_append_read_roundtrip_and_resume(tmp_path) -> None:
    path = tmp_path / "ticks.jsonl"
    with TickStore.open(path) as store:
        assert store.next_seq == 0
        rec = store.append({"NIFTY": 100.0}, recv_ts=_OPEN_UTC)
        assert rec.seq == 0 and store.next_seq == 1
    # resume continues the sequence after the existing line.
    with TickStore.open(path, resume=True) as store:
        assert store.next_seq == 1
        store.append({"NIFTY": 101.0}, recv_ts=_utc(3, 46))
    records = TickStore.read(path)
    assert [r.seq for r in records] == [0, 1]
    assert records[0].snapshot == {"NIFTY": 100.0}
    assert records[0].recv_ts == _OPEN_UTC


def test_store_resume_false_restarts_seq(tmp_path) -> None:
    path = tmp_path / "t.jsonl"
    with TickStore.open(path) as seed:
        seed.append({"A": 1}, recv_ts=_OPEN_UTC)
    with TickStore.open(path, resume=False) as store:
        assert store.next_seq == 0


def test_store_append_naive_and_closed_raise(tmp_path) -> None:
    store = TickStore.open(tmp_path / "t.jsonl")
    with pytest.raises(ValueError, match="timezone-aware"):
        store.append({"A": 1}, recv_ts=datetime(2026, 6, 15, 9, 15))
    store.close()
    store.close()  # idempotent
    with pytest.raises(RuntimeError, match="closed"):
        store.append({"A": 1}, recv_ts=_OPEN_UTC)


def test_store_read_missing_is_empty_and_skips_blank_lines(tmp_path) -> None:
    assert TickStore.read(tmp_path / "nope.jsonl") == []
    path = tmp_path / "blanks.jsonl"
    path.write_text('{"seq":0,"recv_ts":"2026-06-15T03:45:00+00:00","snapshot":{}}\n\n')
    assert [r.seq for r in TickStore.read(path)] == [0]


def test_store_resume_counts_past_blank_line(tmp_path) -> None:
    """``_count_lines`` (resume cursor) skips blank lines but counts records."""
    path = tmp_path / "blank.jsonl"
    path.write_text('{"seq":0,"recv_ts":"2026-06-15T03:45:00+00:00","snapshot":{}}\n\n')
    with TickStore.open(path, resume=True) as store:
        assert store.next_seq == 1  # the blank line is not counted


_REC0 = '{"recv_ns":1,"recv_ts":"2026-06-15T03:45:00+00:00","seq":0,"snapshot":{}}\n'
_REC1 = '{"recv_ns":2,"recv_ts":"2026-06-15T03:46:00+00:00","seq":1,"snapshot":{}}\n'


def test_store_resume_truncates_torn_tail(tmp_path) -> None:
    """A crash-torn partial last line is truncated on resume; seq stays exact (B4)."""
    path = tmp_path / "torn.jsonl"
    path.write_text(_REC0 + _REC1 + '{"seq":2,"recv_ts":"2026-06-15T03:4', encoding="utf-8")
    with TickStore.open(path, resume=True) as store:
        assert store.next_seq == 2  # the torn tail is not counted
        store.append({"N": 9}, recv_ts=_utc(3, 47))  # appends cleanly after the repair
    assert [r.seq for r in TickStore.read(path)] == [0, 1, 2]


def test_store_repair_truncates_lone_partial(tmp_path) -> None:
    """A file that is ONLY a torn partial line repairs to empty (rfind → -1 branch)."""
    path = tmp_path / "lone.jsonl"
    path.write_text('{"seq":0,"recv', encoding="utf-8")
    with TickStore.open(path, resume=True) as store:
        assert store.next_seq == 0
    assert TickStore.read(path) == []


def test_store_resume_empty_file_is_zero(tmp_path) -> None:
    """An existing but empty file resumes at seq 0 (the `not data` branch)."""
    path = tmp_path / "empty.jsonl"
    path.touch()
    with TickStore.open(path, resume=True) as store:
        assert store.next_seq == 0


def test_store_read_skips_torn_tail(tmp_path) -> None:
    """read() tolerates a torn final line (no newline) but keeps the good records (B3)."""
    path = tmp_path / "tail.jsonl"
    path.write_text(_REC0 + '{"seq":1,"recv_ts":"2026-06-15', encoding="utf-8")
    assert [r.seq for r in TickStore.read(path)] == [0]


def test_store_read_raises_on_midfile_corruption(tmp_path) -> None:
    """A malformed NON-final line is real corruption — never hidden (B3)."""
    path = tmp_path / "corrupt.jsonl"
    path.write_text("not-json\n" + _REC1, encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        TickStore.read(path)


def test_store_read_raises_on_terminated_malformed_last_line(tmp_path) -> None:
    """A newline-terminated malformed LAST line is corruption, not a torn tail (B3)."""
    path = tmp_path / "badlast.jsonl"
    path.write_text(_REC0 + "garbage\n", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        TickStore.read(path)


def test_store_json_default_stringifies_decimal(tmp_path) -> None:
    path = tmp_path / "dec.jsonl"
    with TickStore.open(path) as store:
        store.append({"NIFTY": Decimal("100.5")}, recv_ts=_OPEN_UTC)
    assert TickStore.read(path)[0].snapshot == {"NIFTY": "100.5"}


# ───────────────────────── candle_builder.py ─────────────────────


def test_build_window_filter_and_ohlc() -> None:
    ticks = [
        LiveTick(recv_ts=_utc(3, 44, 50), instrument_key="N", value=Decimal("1")),  # 09:14 — out
        LiveTick(recv_ts=_utc(3, 45, 10), instrument_key="N", value=Decimal("100")),  # open
        LiveTick(recv_ts=_utc(3, 45, 30), instrument_key="N", value=Decimal("105")),  # high
        LiveTick(recv_ts=_utc(3, 45, 50), instrument_key="N", value=Decimal("99")),  # low/close
        LiveTick(recv_ts=_utc(9, 59, 30), instrument_key="N", value=Decimal("200")),  # 15:29 — in
        LiveTick(recv_ts=_utc(10, 0, 5), instrument_key="N", value=Decimal("9")),  # 15:30 — out
    ]
    candles = build_minute_candles(ticks)
    bars = candles["N"]
    assert [b.timestamp for b in bars] == [_utc(3, 45), _utc(9, 59)]
    first = bars[0]
    assert (first.open, first.high, first.low, first.close) == (
        Decimal("100"),
        Decimal("105"),
        Decimal("99"),
        Decimal("99"),
    )
    assert first.volume == 0 and first.open_interest is None


def test_build_session_date_filter() -> None:
    from datetime import date

    # 03:45 UTC == 09:15 IST on 2026-06-15, so that IST date keeps the tick.
    tick = LiveTick(recv_ts=_utc(3, 45, 10), instrument_key="N", value=Decimal("1"))
    assert build_minute_candles([tick], session_date=date(2026, 6, 15))["N"]
    assert build_minute_candles([tick], session_date=date(2020, 1, 1)) == {}


def test_build_buckets_by_exchange_minute_even_when_recv_crosses_minute() -> None:
    """A 09:15:30 and a 09:15:59 EXCHANGE tick land in the SAME 09:15 bar.

    Both ticks are stamped by the exchange inside the 09:15 IST minute, but OUR
    ``recv_ts`` for the second already crossed into the 09:16 wall-clock minute
    (a snapshot-cadence / GC pause delay). Bucketing by ``exchange_ts`` keeps both
    in the single 09:15 exchange minute — schema- AND semantics-identical to the
    backtest bar (DEC-LIVE-CANDLE-BUCKET-IST-001).
    """
    # 09:15:30 IST == 03:45:30 UTC; 09:15:59 IST == 03:45:59 UTC.
    exch_30 = datetime(2026, 6, 15, 3, 45, 30, tzinfo=UTC)
    exch_59 = datetime(2026, 6, 15, 3, 45, 59, tzinfo=UTC)
    ticks = [
        LiveTick(
            recv_ts=_utc(3, 45, 31),  # received still inside 09:15
            instrument_key="N",
            value=Decimal("100"),
            exchange_ts=exch_30,
        ),
        LiveTick(
            recv_ts=_utc(3, 46, 2),  # received in the NEXT wall-clock minute (09:16)
            instrument_key="N",
            value=Decimal("110"),
            exchange_ts=exch_59,
        ),
    ]
    candles = build_minute_candles(ticks)
    bars = candles["N"]
    assert [b.timestamp for b in bars] == [_utc(3, 45)]  # ONE bar at the 09:15 label
    assert bars[0].open == Decimal("100") and bars[0].close == Decimal("110")


def test_build_falls_back_to_recv_ts_when_no_exchange_ts() -> None:
    """A tick without exchange_ts buckets by recv_ts (the per-tick fallback branch)."""
    ticks = [LiveTick(recv_ts=_utc(3, 45, 10), instrument_key="N", value=Decimal("7"))]
    bars = build_minute_candles(ticks)["N"]
    assert [b.timestamp for b in bars] == [_utc(3, 45)] and bars[0].close == Decimal("7")


def test_build_mixed_clock_index_vs_no_ts_leaf() -> None:
    """An index leaf (tsInMillis) buckets by exchange minute; a leaf without it by recv_ts.

    NIFTY's exchange clock says 09:15 even though we received it in the 09:16
    wall-clock minute; a flat leaf with no tsInMillis falls back to its 09:16
    recv_ts — proving the two clocks coexist per-tick in one fold.
    """
    nifty = LiveTick(
        recv_ts=_utc(3, 46, 5),
        instrument_key="NSE:IDX:NIFTY",
        value=Decimal("22500"),
        exchange_ts=datetime(2026, 6, 15, 3, 45, 40, tzinfo=UTC),  # 09:15:40 IST
    )
    flat = LiveTick(recv_ts=_utc(3, 46, 5), instrument_key="FLAT", value=Decimal("9"))
    candles = build_minute_candles([nifty, flat])
    assert [b.timestamp for b in candles["NSE:IDX:NIFTY"]] == [_utc(3, 45)]  # exchange minute
    assert [b.timestamp for b in candles["FLAT"]] == [_utc(3, 46)]  # recv fallback


# ───────────────────────────── parity.py ─────────────────────────


def test_compare_match_mismatch_and_gaps() -> None:
    live = [_bar("100", ts=_utc(3, 45)), _bar("101", ts=_utc(3, 46)), _bar("9", ts=_utc(3, 47))]
    backtest = [
        _bar("100", ts=_utc(3, 45)),
        _bar("102", ts=_utc(3, 46)),
        _bar("5", ts=_utc(3, 48)),
    ]
    report = compare_candles(live, backtest, instrument_key="N")
    assert report.matched == 1
    assert report.mismatched == 1
    assert report.live_only == 1  # 03:47 only live
    assert report.backtest_only == 1  # 03:48 only backtest
    assert report.total_minutes == 4
    assert not report.is_identical
    reasons = {d.reason for d in report.diffs}
    assert reasons == {"mismatch", "live_only", "backtest_only"}
    assert "DIVERGES" in report.summary()


def test_compare_identical_and_volume_flag() -> None:
    live = [_bar("100", ts=_utc(3, 45), volume=0)]
    backtest = [_bar("100", ts=_utc(3, 45), volume=7)]
    assert compare_candles(live, backtest, instrument_key="N").is_identical
    vol = compare_candles(live, backtest, instrument_key="N", compare_volume=True)
    assert vol.mismatched == 1
    assert "IDENTICAL" in compare_candles(live, live, instrument_key="N").summary()


def test_minutediff_dataclass_fields() -> None:
    diff = MinuteDiff(timestamp=_utc(3, 45), live=None, backtest=_bar("1"), reason="backtest_only")
    assert diff.reason == "backtest_only" and diff.live is None


# ───────────────────────────── report.py ─────────────────────────


def test_render_identical_report() -> None:
    report = ParityReport(
        instrument_key="NIFTY",
        total_minutes=2,
        matched=2,
        mismatched=0,
        live_only=0,
        backtest_only=0,
        diffs=(),
    )
    md = render_parity_markdown([report])
    assert "✅ IDENTICAL" in md
    assert "ALL IDENTICAL" in md
    assert "Divergences" not in md


def test_render_divergences_all_reasons_and_truncation() -> None:
    diffs = (
        MinuteDiff(_utc(3, 45), _bar("100"), _bar("101"), "mismatch"),
        MinuteDiff(_utc(3, 46), _bar("1"), None, "live_only"),
        MinuteDiff(_utc(3, 47), None, _bar("2"), "backtest_only"),
    )
    report = ParityReport("NIFTY", 3, 0, 1, 1, 1, diffs)
    full = render_parity_markdown([report])
    assert "❌ DIVERGES" in full and "DIVERGENCES FOUND" in full
    assert "MISSING" in full  # the live_only / backtest_only gap rows
    # a mismatch on _bar("100") vs _bar("101") fills all 4 fields, so 2 rows truncate.
    assert "more diverging field" in render_parity_markdown([report], max_diff_rows=2)


def test_render_empty_reports() -> None:
    assert "NO INSTRUMENTS" in render_parity_markdown([])


def test_render_diff_skips_equal_fields() -> None:
    """A mismatch on close-only emits ONE row (open/high/low are equal → skipped)."""
    live = CandleBar(
        timestamp=_utc(3, 45),
        open=Decimal("100"),
        high=Decimal("105"),
        low=Decimal("99"),
        close=Decimal("100"),
        volume=0,
        open_interest=None,
    )
    backtest = CandleBar(
        timestamp=_utc(3, 45),
        open=Decimal("100"),
        high=Decimal("105"),
        low=Decimal("99"),
        close=Decimal("101"),
        volume=0,
        open_interest=None,
    )
    report = ParityReport(
        "N", 1, 0, 1, 0, 0, (MinuteDiff(_utc(3, 45), live, backtest, "mismatch"),)
    )
    md = render_parity_markdown([report])
    assert md.count("| close |") == 1
    assert "| high |" not in md  # equal field is skipped


# ───────────────────────────── probe.py ──────────────────────────


def test_probe_connections_all_succeed() -> None:
    opened: list[int] = []
    result = probe_connections(open_connection=opened.append, max_conns=3)
    assert result.max_held == 3 and not result.reached_ceiling
    assert result.failed_at is None and opened == [1, 2, 3]


def test_probe_connections_fails_at_n() -> None:
    def opener(ordinal: int) -> None:
        if ordinal == 3:
            raise RuntimeError("max connections")

    result = probe_connections(open_connection=opener, max_conns=10, between=lambda: None)
    assert result.max_held == 2 and result.failed_at == 3
    assert result.reached_ceiling
    assert result.failure == "RuntimeError: max connections"


def test_probe_holds_then_closes_each_connection() -> None:
    """Every opened connection is RETAINED for the ramp, then released via close."""
    opened: list[object] = []
    closed: list[object] = []

    def opener(_ordinal: int) -> object:
        conn = object()
        opened.append(conn)
        return conn

    result = probe_connections(open_connection=opener, max_conns=3, close=closed.append)
    assert result.max_held == 3
    assert len(opened) == 3 and closed == opened  # all held concurrently, then all closed


def test_probe_close_errors_are_swallowed() -> None:
    """A close failure is logged + swallowed — it never masks the probe result."""

    def bad_close(_conn: object) -> None:
        raise RuntimeError("close failed")

    result = probe_connections(open_connection=lambda _o: object(), max_conns=2, close=bad_close)
    assert result.max_held == 2 and result.failed_at is None


def test_probe_fails_at_first_connection() -> None:
    """A refusal on the very first open holds nothing (the empty-cleanup path)."""

    def opener(_ordinal: int) -> object:
        raise RuntimeError("refused")

    result = probe_connections(open_connection=opener, max_conns=5, close=lambda _c: None)
    assert result.max_held == 0 and result.failed_at == 1 and result.reached_ceiling


def test_analyze_cadence_insufficient() -> None:
    stats = analyze_cadence([])
    assert stats.samples == 0 and stats.median_interval_ms is None
    assert "insufficient" in stats.classification


@pytest.mark.parametrize(
    ("gap_ms", "expect"),
    [
        (50, "tick-by-tick"),
        (300, "sub-second"),
        (1000, "1 per second"),
        (3000, "multi-second"),
    ],
)
def test_analyze_cadence_classifications(gap_ms: int, expect: str) -> None:
    base = _OPEN_UTC
    records = [
        TickRecord(seq=i, recv_ts=base + timedelta(milliseconds=gap_ms * i), snapshot={"N": i})
        for i in range(4)
    ]
    # feed them out of order to exercise the defensive sort.
    stats = analyze_cadence(list(reversed(records)))
    assert stats.samples == 4
    assert expect in stats.classification
    assert stats.min_interval_ms == pytest.approx(gap_ms)


def test_analyze_cadence_prefers_recv_ns() -> None:
    """When every record carries recv_ns, gaps come from the ns oracle (M4)."""
    base_ns = 1_700_000_000_000_000_000
    records = [
        TickRecord(seq=i, recv_ts=_OPEN_UTC, snapshot={"N": i}, recv_ns=base_ns + i * 50_000_000)
        for i in range(4)
    ]
    # recv_ts is identical for all → a recv_ts-based cadence would read 0ms; the
    # 50ms gaps prove recv_ns drove the calculation.
    stats = analyze_cadence(list(reversed(records)))
    assert stats.samples == 4
    assert stats.min_interval_ms == pytest.approx(50.0)
    assert stats.duration_seconds == pytest.approx(0.15)
    assert "tick-by-tick" in stats.classification


# ───────────────────────────── serde.py ──────────────────────────


def test_candle_dict_roundtrip_oi_none_and_int() -> None:
    bar = _bar("100.25")
    assert candle_from_dict(candle_to_dict(bar)) == bar
    fno = _bar("100", open_interest=42, volume=5)
    back = candle_from_dict(candle_to_dict(fno))
    assert back.open_interest == 42 and back.volume == 5


def test_candles_json_roundtrip_and_files(tmp_path) -> None:
    candles = {"NIFTY": [_bar("100", ts=_utc(3, 45)), _bar("101", ts=_utc(3, 46))]}
    assert candles_from_json(candles_to_json(candles)) == candles
    path = tmp_path / "c" / "candles.json"
    write_candles(path, candles)
    assert read_candles(path) == candles
