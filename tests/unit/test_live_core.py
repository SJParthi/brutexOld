"""Unit tests for the Groww live-feed parity CORE (pure modules).

Covers :mod:`brutex.live.tick`, :mod:`~brutex.live.store`,
:mod:`~brutex.live.candle_builder`, :mod:`~brutex.live.parity`,
:mod:`~brutex.live.report`, :mod:`~brutex.live.probe`, and
:mod:`~brutex.live.serde` — every branch, no feed / no network.
"""

from __future__ import annotations

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
    _coerce_number,
    _leaf_price,
    flatten_index_snapshot,
    iter_ticks,
)

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


# ─────────────────────────── store.py ───────────────────────────


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
