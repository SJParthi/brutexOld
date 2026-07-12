"""Unit tests for the pure ``/movers`` snapshot core (DEC-MOVERS-SNAPSHOT-VIEW-001).

Covers the I/O-free surface of :mod:`brutex.dashboard.movers_snapshot` — the
point-in-time movers logic (session bar-times, per-instrument as-of snapshot,
gainers/losers/volume/value ranking, the markets-screen-style JSON payload, date/time
parsing, the row→bar adapter, and the futures/options "pending" payload). No
lake, no network (the lake-reading orchestration is proven in
``tests/integration/test_dashboard_movers.py``).
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from brutex.dashboard.movers_snapshot import (
    MoverSnapshot,
    ScreenInstrument,
    _cutoff_minute,
    _row_to_bar,
    build_snapshot_payload,
    compute_snapshot,
    interval_minutes,
    is_intraday_interval,
    minute_to_hhmm,
    normalize_min_volume,
    parse_snapshot_date,
    pending_asset_payload,
    session_bar_times,
    value_meets_volume_floor,
)
from brutex.data.models import CandleBar

_NIFTY = ScreenInstrument(symbol="NIFTY", exchange="NSE", segment="INDEX", display_name="Nifty 50")
_BANK = ScreenInstrument(
    symbol="BANKNIFTY", exchange="NSE", segment="INDEX", display_name="Nifty Bank"
)


def _bar(
    *, minute: int, close: str, volume: int, high: str | None = None, low: str | None = None
) -> CandleBar:
    """A valid 1-min bar on 2024-06-13 at 09:<minute> UTC (test fixture).

    *high*/*low* default to *close* (a zero-range bar) so existing callers are
    unchanged; pass them to exercise the HLC3 typical-price turnover.
    """
    c = Decimal(close)
    return CandleBar(
        timestamp=datetime(2024, 6, 13, 9, minute, tzinfo=UTC),
        open=c,
        high=Decimal(high) if high is not None else c,
        low=Decimal(low) if low is not None else c,
        close=c,
        volume=volume,
        open_interest=None,
    )


# ── interval_minutes ──────────────────────────────────────────────────────


def test_interval_minutes_known_and_unknown() -> None:
    assert interval_minutes("1minute") == 1
    assert interval_minutes("5minute") == 5
    assert interval_minutes("1hour") == 60
    assert interval_minutes("1day") == 375
    assert interval_minutes("bogus") is None


# ── session_bar_times ─────────────────────────────────────────────────────


def test_session_bar_times_5minute_spans_0915_to_1525() -> None:
    times = session_bar_times("5minute")
    assert times[0] == "09:15"
    assert times[-1] == "15:25"
    assert len(times) == 75  # 375 min / 5


def test_session_bar_times_1hour() -> None:
    assert session_bar_times("1hour") == [
        "09:15",
        "10:15",
        "11:15",
        "12:15",
        "13:15",
        "14:15",
        "15:15",
    ]


def test_session_bar_times_daily_and_unknown_have_no_intraday_time() -> None:
    assert session_bar_times("1day") == []
    assert session_bar_times("bogus") == []


# ── compute_snapshot ──────────────────────────────────────────────────────


def test_compute_snapshot_full_metrics_vs_prev_close() -> None:
    bars = [_bar(minute=15, close="100", volume=10), _bar(minute=16, close="110", volume=20)]
    snap = compute_snapshot(_NIFTY, bars, prev_close=Decimal("100"))
    assert snap is not None
    assert snap.ltp == Decimal("110.00")
    assert snap.change == Decimal("10.00")
    assert snap.pct_change == Decimal("10")  # vs PREVIOUS-day close
    assert snap.volume == 30  # cumulative fresh session
    # Zero-range bars (high=low=close) → HLC3 typical == close, so the turnover
    # degenerates to 100*10 + 110*20 = 3200 rupees → 320000 paisa.
    assert snap.value_paisa == 320000
    assert snap.bar_count == 2
    assert snap.ltp_inr == "₹110.00"


def test_compute_snapshot_value_uses_hlc3_typical_price() -> None:
    # DEC-MOVERS-VWAP-VALUE-001: turnover is Σ round_money((high+low+close)/3) × volume,
    # NOT close × volume — proven with bars that have a real intrabar range.
    bars = [
        _bar(minute=15, high="102", low="98", close="100", volume=10),
        _bar(minute=16, high="112", low="106", close="110", volume=20),
    ]
    snap = compute_snapshot(_NIFTY, bars, prev_close=Decimal("100"))
    assert snap is not None
    # Bar1 typical=(102+98+100)/3=100.00 → 100.00*10=₹1000.00;
    # Bar2 typical=(112+106+110)/3=109.3333…→round_money 109.33 → 109.33*20=₹2186.60;
    # Σ=₹3186.60 → 318660 paisa (the OLD close×volume formula would give 320000).
    assert snap.value_paisa == 318660
    # The LTP / volume are unaffected by the turnover formula.
    assert snap.ltp == Decimal("110.00") and snap.volume == 30


def test_compute_snapshot_no_previous_day_leaves_change_none() -> None:
    bars = [_bar(minute=15, close="100", volume=10)]
    snap = compute_snapshot(_NIFTY, bars, prev_close=None)
    assert snap is not None
    assert snap.prev_close is None
    assert snap.change is None
    assert snap.pct_change is None
    assert snap.volume == 10


def test_compute_snapshot_empty_bars_is_none() -> None:
    assert compute_snapshot(_NIFTY, [], prev_close=Decimal("100")) is None


def test_value_crore_rendering() -> None:
    # 5,000,000,000 paisa = ₹50,000,000 = 5 crore → "5.00".
    snap = MoverSnapshot(
        instrument=_NIFTY,
        ltp=Decimal("100.00"),
        prev_close=Decimal("100.00"),
        change=Decimal("0.00"),
        pct_change=Decimal("0"),
        volume=10,
        value_paisa=5_000_000_000,
        bar_count=1,
    )
    assert snap.value_crore == "5.00"


# ── build_snapshot_payload (ranking + notes) ──────────────────────────────


def _snap(
    inst: ScreenInstrument, *, pct: str | None, volume: int, value_paisa: int
) -> MoverSnapshot:
    pct_dec = Decimal(pct) if pct is not None else None
    return MoverSnapshot(
        instrument=inst,
        ltp=Decimal("100.00"),
        prev_close=Decimal("100.00") if pct is not None else None,
        change=Decimal("1.00") if pct is not None else None,
        pct_change=pct_dec,
        volume=volume,
        value_paisa=value_paisa,
        bar_count=1,
    )


def test_payload_ranks_gainers_losers_volume() -> None:
    gain = _snap(_NIFTY, pct="10", volume=500, value_paisa=10)
    lose = _snap(_BANK, pct="-5", volume=100, value_paisa=20)
    snaps = [gain, lose]
    payload = build_snapshot_payload(
        snaps, interval="5minute", date="2024-06-13", time="09:20", top_n=10, skipped=0
    )
    assert [r["symbol"] for r in payload["price_gainers"]] == ["NIFTY"]
    assert [r["symbol"] for r in payload["price_losers"]] == ["BANKNIFTY"]
    # Volume ranked desc: NIFTY (500) before BANKNIFTY (100).
    assert [r["symbol"] for r in payload["volume"]] == ["NIFTY", "BANKNIFTY"]
    # Value ranked desc by traded value.
    assert {r["symbol"] for r in payload["value"]} == {"NIFTY", "BANKNIFTY"}
    assert payload["instrument_count"] == 2
    assert payload["price_gainers"][0]["name"] == "Nifty 50"
    assert payload["price_gainers"][0]["pct_change"] == "10"
    assert "reference-only" in payload["note"]


def test_payload_none_pct_excluded_from_pct_lists_but_in_volume() -> None:
    # A no-previous-day snapshot (pct None) is excluded from price gainers/losers,
    # but its volume still ranks — and its row renders change/pct as null.
    nopc = _snap(_NIFTY, pct=None, volume=300, value_paisa=10)
    payload = build_snapshot_payload(
        [nopc], interval="5minute", date="2024-06-13", time="09:20", top_n=10, skipped=0
    )
    assert payload["price_gainers"] == []
    assert payload["price_losers"] == []
    assert [r["symbol"] for r in payload["volume"]] == ["NIFTY"]
    row = payload["volume"][0]
    assert row["change"] is None
    assert row["pct_change"] is None


def test_payload_empty_lake_says_pull_spot_first() -> None:
    payload = build_snapshot_payload(
        [], interval="1day", date="2024-06-13", time=None, top_n=10, skipped=0
    )
    assert payload["instrument_count"] == 0
    assert payload["price_gainers"] == []
    assert "pull spot first" in payload["note"]


def test_payload_data_present_but_no_mover_at_this_bar_is_honest() -> None:
    # An instrument exists (counted) but is flat + zero-volume → no ranked rows;
    # the note distinguishes this from an empty lake (Golden Rule #10).
    flat = _snap(_NIFTY, pct="0", volume=0, value_paisa=0)
    payload = build_snapshot_payload(
        [flat], interval="5minute", date="2024-06-13", time="09:20", top_n=10, skipped=0
    )
    assert payload["instrument_count"] == 1
    assert payload["price_gainers"] == []
    assert payload["price_losers"] == []
    assert payload["volume"] == []
    assert payload["value"] == []
    assert "none had a bar" in payload["note"]


def test_payload_skipped_counts_toward_instrument_count_and_daily_time_label() -> None:
    payload = build_snapshot_payload(
        [], interval="1day", date="2024-06-13", time=None, top_n=10, skipped=3
    )
    assert payload["instrument_count"] == 3
    assert payload["skipped_count"] == 3
    # Daily snapshot has no intraday time → the note reads "(daily)".
    assert "(daily)" in payload["note"]


# ── parse_snapshot_date ───────────────────────────────────────────────────


def test_parse_snapshot_date_valid_and_blank() -> None:
    from datetime import date as date_cls

    assert parse_snapshot_date("2024-06-13") == date_cls(2024, 6, 13)
    # Blank ⇒ today (UTC) — a real date, never raised.
    assert parse_snapshot_date(None) == datetime.now(UTC).date()
    assert parse_snapshot_date("") == datetime.now(UTC).date()


def test_parse_snapshot_date_malformed_raises() -> None:
    with pytest.raises(ValueError, match="malformed date"):
        parse_snapshot_date("13-06-2024")


# ── _cutoff_minute ────────────────────────────────────────────────────────


def test_cutoff_minute_none_and_valid() -> None:
    assert _cutoff_minute(None) is None
    assert _cutoff_minute("") is None
    assert _cutoff_minute("09:15") == 555
    assert _cutoff_minute("15:25") == 925


def test_cutoff_minute_wrong_arity_raises() -> None:
    with pytest.raises(ValueError, match="malformed time"):
        _cutoff_minute("0915")


def test_cutoff_minute_non_integer_raises() -> None:
    with pytest.raises(ValueError, match="malformed time"):
        _cutoff_minute("09:ab")


# ── minute_to_hhmm (DEC-MOVERS-ASOF-LATEST-DEFAULT-001) ────────────────────


def test_minute_to_hhmm_session_endpoints() -> None:
    assert minute_to_hhmm(929) == "15:29"
    assert minute_to_hhmm(555) == "09:15"
    assert minute_to_hhmm(925) == "15:25"


def test_minute_to_hhmm_zero_pads_both_fields() -> None:
    # Single-digit hour AND minute both zero-pad → a valid two-field HH:MM.
    assert minute_to_hhmm(9 * 60 + 5) == "09:05"


def test_minute_to_hhmm_round_trips_through_cutoff_minute() -> None:
    # The formatter output is always a valid `#time` option and its inverse
    # `_cutoff_minute` recovers the original minute exactly (echo == cut in value).
    for minute in (555, 556, 600, 925, 929):
        assert _cutoff_minute(minute_to_hhmm(minute)) == minute


def test_minute_to_hhmm_matches_session_bar_times_grid_strings() -> None:
    # Every string the frontend grid would generate is `minute_to_hhmm` of its
    # minute, so an injected latest minute is always a selectable option.
    for offset, hhmm in enumerate(session_bar_times("1minute")):
        assert hhmm == minute_to_hhmm(555 + offset)


# ── is_intraday_interval (DEC-MOVERS-ASOF-LATEST-DEFAULT-001) ──────────────


def test_is_intraday_interval_true_for_sub_session_timeframes() -> None:
    assert is_intraday_interval("1minute")
    assert is_intraday_interval("3minute")
    assert is_intraday_interval("30minute")
    assert is_intraday_interval("1hour")
    assert is_intraday_interval("3hour")
    assert is_intraday_interval("4hour")  # 240 min < the 375-min session


def test_is_intraday_interval_false_for_daily_and_unknown() -> None:
    assert not is_intraday_interval("1day")  # the bar IS the day
    assert not is_intraday_interval("bogus")  # unknown label → not intraday


def test_is_intraday_interval_agrees_with_session_bar_times() -> None:
    # The predicate is the boolean twin of the non-empty session grid — ONE
    # daily-vs-intraday rule (CLAUDE.md §11 COMMON).
    for interval in ("1minute", "5minute", "3hour", "1day", "bogus"):
        assert is_intraday_interval(interval) == bool(session_bar_times(interval))


# ── _row_to_bar (both tz branches) ────────────────────────────────────────


def test_row_to_bar_tz_aware_passthrough() -> None:
    row = {
        "ts_utc": datetime(2024, 6, 13, 3, 45, tzinfo=UTC),
        "open": 100.0,
        "high": 110.0,
        "low": 99.0,
        "close": 105.0,
        "volume": 42,
    }
    bar = _row_to_bar(row)
    assert bar.timestamp == datetime(2024, 6, 13, 3, 45, tzinfo=UTC)
    assert bar.close == Decimal("105.0")
    assert bar.volume == 42
    assert bar.open_interest is None


def test_row_to_bar_naive_ts_is_stamped_utc() -> None:
    # A resampler that drops tz yields a naive instant — the lake instant is UTC.
    row = {
        "ts_utc": datetime(2024, 6, 13, 3, 45),
        "open": 100.0,
        "high": 100.0,
        "low": 100.0,
        "close": 100.0,
        "volume": 7,
    }
    bar = _row_to_bar(row)
    assert bar.timestamp.tzinfo is UTC


# ── pending_asset_payload (futures / options not yet wired) ───────────────


def test_pending_asset_payload_is_honest_and_empty() -> None:
    payload = pending_asset_payload(
        "futures", interval="1day", date="2024-06-13", time=None, top_n=50
    )
    assert payload["instrument_count"] == 0
    assert payload["skipped_count"] == 0
    assert payload["date"] == "2024-06-13"
    assert payload["interval"] == "1day"
    # Names the asset + states Spot ships first (Golden Rule #10) and carries no rows.
    assert "Futures" in payload["note"]
    assert "Spot" in payload["note"]
    assert "price_gainers" not in payload  # absent category keys → page falls back to []


def test_pending_asset_payload_escapes_reflected_asset() -> None:
    """DEC-DASHBOARD-XSS-HARDENING-001: the raw ``?asset=`` echo is HTML-escaped.

    ``asset`` is reflected verbatim into the note, so a payload like
    ``<img src=x onerror=alert(1)>`` must come back as inert ``&lt;img …`` text,
    never an unescaped live tag — defense-in-depth at the JSON source even though
    the page also sets the note via ``textContent``.
    """
    payload = pending_asset_payload(
        "<img src=x onerror=alert(1)>",
        interval="1day",
        date="2024-06-13",
        time=None,
        top_n=50,
    )
    note = payload["note"]
    # The security invariant: NO literal angle bracket survives, so the browser
    # can never re-form a live `<img …>` tag from the reflected note.
    assert "<" not in note
    assert ">" not in note
    assert "<img" not in note  # no live tag survives
    # The escaped form is present instead (title-cased to "&lt;Img …" by .title()).
    assert "&lt;img" in note.lower()
    assert "&gt;" in note
    # ampersand + quote are escaped as well (html.escape default quote=True).
    payload2 = pending_asset_payload(
        "a&b\"'", interval="1day", date="2024-06-13", time=None, top_n=1
    )
    assert "&amp;" in payload2["note"]
    assert "&quot;" in payload2["note"] or "&#x27;" in payload2["note"]


# ── DEC-MOVERS-MIN-VOLUME-001: the COMMON opt-in Top-Volume floor (spot) ──


class TestNormalizeMinVolume:
    def test_none_is_zero(self) -> None:
        assert normalize_min_volume(None) == 0

    def test_negative_clamps_to_zero(self) -> None:
        assert normalize_min_volume(-7) == 0

    def test_positive_passes_through(self) -> None:
        assert normalize_min_volume(500) == 500

    def test_zero_is_zero(self) -> None:
        assert normalize_min_volume(0) == 0


class TestValueMeetsVolumeFloor:
    def test_zero_volume_excluded_at_default_floor(self) -> None:
        # Byte-identical to the prior `volume > 0` filter when min_volume == 0.
        assert value_meets_volume_floor(0, 0) is False
        assert value_meets_volume_floor(1, 0) is True

    def test_below_floor_excluded(self) -> None:
        assert value_meets_volume_floor(499, 500) is False

    def test_at_or_above_floor_included(self) -> None:
        assert value_meets_volume_floor(500, 500) is True
        assert value_meets_volume_floor(501, 500) is True

    def test_zero_volume_excluded_even_when_floor_is_zero(self) -> None:
        assert value_meets_volume_floor(0, 0) is False


class TestSpotPayloadMinVolume:
    def test_default_floor_byte_identical(self) -> None:
        snaps = [
            _snap(_NIFTY, pct="1", volume=0, value_paisa=10),
            _snap(_BANK, pct="1", volume=300, value_paisa=20),
        ]
        # min_volume omitted == min_volume=0 == prior `volume > 0` behaviour.
        base = build_snapshot_payload(
            snaps, interval="1day", date="2024-06-13", time=None, top_n=10, skipped=0
        )
        zeroed = build_snapshot_payload(
            snaps, interval="1day", date="2024-06-13", time=None, top_n=10, skipped=0, min_volume=0
        )
        assert [r["symbol"] for r in base["volume"]] == ["BANKNIFTY"]
        assert base["volume"] == zeroed["volume"]
        assert base["min_volume"] == 0
        assert "Min volume" not in base["note"]

    def test_positive_floor_drops_thin_and_notes_it(self) -> None:
        snaps = [
            _snap(_NIFTY, pct="1", volume=100, value_paisa=10),
            _snap(_BANK, pct="1", volume=5000, value_paisa=20),
        ]
        payload = build_snapshot_payload(
            snaps,
            interval="1day",
            date="2024-06-13",
            time=None,
            top_n=10,
            skipped=0,
            min_volume=1000,
        )
        assert [r["symbol"] for r in payload["volume"]] == ["BANKNIFTY"]
        assert payload["min_volume"] == 1000
        assert "Min volume ≥ 1,000." in payload["note"]
        # The price rankings are NOT affected by the volume floor.
        assert len(payload["price_gainers"]) == 2

    def test_negative_floor_normalises_in_payload(self) -> None:
        snaps = [_snap(_NIFTY, pct="1", volume=300, value_paisa=10)]
        payload = build_snapshot_payload(
            snaps,
            interval="1day",
            date="2024-06-13",
            time=None,
            top_n=10,
            skipped=0,
            min_volume=-99,
        )
        assert payload["min_volume"] == 0


# ── DEC-MOVERS-STORED-FRESHNESS-001: the stored-freshness guard ───────────
# The day-scoped grid-aware predicate + the capped stale-fallback warner + the
# `_read_intraday_bars` guard arms, all covered from THIS stem file. Lake-needing
# tests guard their deps per-test (`pytest.importorskip`) so the pure suite above
# stays dependency-free.

_GUARD_DAY = date(2024, 6, 13)


def _ist_frame(minutes_of_day: list[int], *, closes: list[str] | None = None) -> Any:
    """A polars ``ts_utc``/OHLCV frame with one row per IST minute-of-day."""
    pl = pytest.importorskip("polars")
    closes = closes or ["100"] * len(minutes_of_day)
    rows = {
        "ts_utc": [
            datetime(
                _GUARD_DAY.year,
                _GUARD_DAY.month,
                _GUARD_DAY.day,
                (m - 330) // 60,
                (m - 330) % 60,
                tzinfo=UTC,
            )
            for m in minutes_of_day
        ],
        "open": [float(c) for c in closes],
        "high": [float(c) + 2 for c in closes],
        "low": [float(c) - 3 for c in closes],
        "close": [float(c) for c in closes],
        "volume": [100] * len(minutes_of_day),
    }
    return pl.DataFrame(rows)


def _lake_bar(*, day: date, hour: int, minute: int, close: str, volume: int) -> CandleBar:
    """A 1-minute bar at ``HH:MM`` IST on *day* (IST = UTC + 05:30, no DST)."""
    c = Decimal(close)
    total = hour * 60 + minute - (5 * 60 + 30)
    return CandleBar(
        timestamp=datetime(day.year, day.month, day.day, total // 60, total % 60, tzinfo=UTC),
        open=c - Decimal("1"),
        high=c + Decimal("2"),
        low=c - Decimal("3"),
        close=c,
        volume=volume,
        open_interest=None,
    )


def _seed_guard_lake(lake: Path, *, base: bool = True, poison_5m: bool = True) -> None:
    """Seed the :351-shaped lake: a 09:15–09:24 1-minute base + a stale 5m poison."""
    pytest.importorskip("polars")
    pytest.importorskip("pyarrow")
    from brutex.data.parquet_writer import ParquetWriter

    writer = ParquetWriter(lake_root=lake)
    if base:
        writer.write(
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            interval="1minute",
            bars=[
                _lake_bar(
                    day=_GUARD_DAY, hour=9, minute=15 + i, close=str(20005 + i * 10), volume=100 + i
                )
                for i in range(10)
            ],
        )
    if poison_5m:
        # ONE stale stored 5-minute bucket at 09:15 (max-open 555) — the fresh
        # base runs to 09:24 (564), so 555 + 5 = 560 ≤ 564 reads STALE.
        writer.write(
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            interval="5minute",
            bars=[_lake_bar(day=_GUARD_DAY, hour=9, minute=15, close="99999", volume=1)],
        )


class TestStoredDayIsFresh:
    def test_stored_day_is_fresh_truth_table(self) -> None:
        """The pure day-scoped grid-aware predicate, boundary-exact.

        Frames carry REAL UTC stamps (the 15:29 IST row is minute 929 — the Int8
        overflow canary through ``_ist_minute_of_day_expr``: 15*60 wraps negative
        without the Int32 cast).
        """
        from brutex.dashboard.movers_snapshot import _max_ist_minute, _stored_day_is_fresh

        # Fresh boundary: base == open + m − 1 (the bucket's own last minute).
        stored = _ist_frame([555, 560])
        assert _stored_day_is_fresh(stored, minutes=5, base_max_min=564) is True
        # Stale: the poison shape (max-open 555, base 564): 560 ≤ 564.
        assert _stored_day_is_fresh(_ist_frame([555]), minutes=5, base_max_min=564) is False
        # Stale boundary: base == open + m (560 + 5 = 565 > 565 is False) — kills
        # a `>` → `>=` mutant.
        assert _stored_day_is_fresh(_ist_frame([555, 560]), minutes=5, base_max_min=565) is False
        # No 1-minute evidence → fail-open fresh (falling back would DROP the row).
        assert _stored_day_is_fresh(_ist_frame([555]), minutes=5, base_max_min=None) is True
        # The Int8 canary: a 15:29 IST stamp reads minute 929, not a wrapped Int8.
        assert _max_ist_minute(_ist_frame([555, 929])) == 929
        assert _stored_day_is_fresh(_ist_frame([925]), minutes=5, base_max_min=929) is True


class TestReadIntradayBarsFreshnessGuard:
    def test_read_intraday_bars_stale_stored_falls_back_to_resample(self, tmp_path: Path) -> None:
        """A stale stored TF partition is bypassed — the 1-minute resample serves.

        Fail-before: on the pre-guard tree the poisoned stored partition is served
        verbatim (``from_stored is True``, close 99999).
        """
        from brutex.dashboard.movers_snapshot import _read_intraday_bars, _read_session_bars

        _seed_guard_lake(tmp_path)
        bars, from_stored = _read_intraday_bars(
            tmp_path,
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            interval="5minute",
            day=_GUARD_DAY,
            minutes=5,
            cutoff_min=None,
        )
        assert from_stored is False
        assert bars == _read_session_bars(
            tmp_path,
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            day=_GUARD_DAY,
            minutes=5,
            cutoff_min=None,
        )
        assert bars and bars[-1].close != Decimal("99999")

    def test_read_intraday_bars_fresh_stored_served_bucket_open_stamp(self, tmp_path: Path) -> None:
        """The naive-predicate canary at unit level: a REAL prebuilt partition is FRESH.

        Stored buckets are bucket-OPEN stamped (max-open 09:20 = 560 < the base's
        09:24 = 564), so a naive ``stored_max >= base_max`` compare would
        false-stale this genuinely fresh lake; the grid-aware ``+ minutes`` reads
        560 + 5 = 565 > 564 = fresh. Keep-green pre AND post fix.
        """
        from brutex.dashboard.movers_snapshot import _read_intraday_bars
        from brutex.data.timeframe_materialize import PrebuildKind, materialize_lake_timeframes

        _seed_guard_lake(tmp_path, poison_5m=False)
        result = materialize_lake_timeframes(
            lake_root=tmp_path,
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            kind=PrebuildKind.SPOT,
            tfs=("5m",),
        )
        assert result.total_bars > 0
        bars, from_stored = _read_intraday_bars(
            tmp_path,
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            interval="5minute",
            day=_GUARD_DAY,
            minutes=5,
            cutoff_min=None,
        )
        assert from_stored is True
        assert len(bars) == 2  # the 09:15 + 09:20 buckets of the 10-minute session

    def test_read_intraday_bars_base_absent_fail_open_serves_stored(self, tmp_path: Path) -> None:
        """No 1-minute base (a TF-only lake) → fail-open serve-stored, never a drop."""
        from brutex.dashboard.movers_snapshot import _read_intraday_bars

        _seed_guard_lake(tmp_path, base=False)
        bars, from_stored = _read_intraday_bars(
            tmp_path,
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            interval="5minute",
            day=_GUARD_DAY,
            minutes=5,
            cutoff_min=None,
        )
        assert from_stored is True
        assert bars  # the stored partition served — falling back would return [].

    def test_read_intraday_bars_1minute_guard_reads_no_base(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The I/O table's teeth: 1minute = identity (ZERO guard reads); fresh TF>1m
        pays exactly ONE guard read (kills the ``minutes == 1`` short-circuit mutants)."""
        from brutex.dashboard import movers_snapshot
        from brutex.data.timeframe_materialize import PrebuildKind, materialize_lake_timeframes

        _seed_guard_lake(tmp_path, poison_5m=False)
        materialize_lake_timeframes(
            lake_root=tmp_path,
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            kind=PrebuildKind.SPOT,
            tfs=("5m",),
        )
        calls: list[str] = []
        real_read = movers_snapshot._read_cut_1m_frame

        def _counting_read(*args: Any, **kwargs: Any) -> Any:
            calls.append("read")
            return real_read(*args, **kwargs)

        monkeypatch.setattr(movers_snapshot, "_read_cut_1m_frame", _counting_read)
        # interval=1minute: the stored 1minute partition IS the base — identity,
        # served from stored with ZERO guard reads.
        bars, from_stored = movers_snapshot._read_intraday_bars(
            tmp_path,
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            interval="1minute",
            day=_GUARD_DAY,
            minutes=1,
            cutoff_min=9 * 60 + 18,
        )
        assert from_stored is True and bars
        assert calls == []
        # Fresh TF>1minute: exactly ONE guard read (the uncut 1-minute day-max).
        bars5, from_stored5 = movers_snapshot._read_intraday_bars(
            tmp_path,
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            interval="5minute",
            day=_GUARD_DAY,
            minutes=5,
            cutoff_min=None,
        )
        assert from_stored5 is True and bars5
        assert calls == ["read"]

    def test_read_intraday_bars_stale_aligned_cutoff_day_scoped(self, tmp_path: Path) -> None:
        """The verdict-2 tripwire: the :351 shape at the ALIGNED 09:19 cutoff is STALE.

        Under a CUT-domain predicate the poison reads fresh (bars ≤ 09:19 keep only
        the 09:15 open: 555 + 5 = 560 > 559) and this test FAILS; only the
        DAY-scoped compare (560 ≤ 564 — the base runs to 09:24) catches it.
        """
        from brutex.dashboard.movers_snapshot import _read_intraday_bars, _read_session_bars

        _seed_guard_lake(tmp_path)
        cutoff = 9 * 60 + 19  # bucket-final ALIGNED — _stored_serves_cutoff admits stored.
        bars, from_stored = _read_intraday_bars(
            tmp_path,
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            interval="5minute",
            day=_GUARD_DAY,
            minutes=5,
            cutoff_min=cutoff,
        )
        assert from_stored is False
        assert bars == _read_session_bars(
            tmp_path,
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            day=_GUARD_DAY,
            minutes=5,
            cutoff_min=cutoff,
        )
        assert bars and bars[-1].close == Decimal("20045")  # the cut resample truth

    def test_read_intraday_bars_stale_warn_state_none_no_log(self, tmp_path: Path) -> None:
        """The default ``stale_warn_state=None`` arm: still falls back, logs nothing."""
        import structlog.testing
        from brutex.dashboard.movers_snapshot import _read_intraday_bars

        _seed_guard_lake(tmp_path)
        with structlog.testing.capture_logs() as logs:
            _, from_stored = _read_intraday_bars(
                tmp_path,
                exchange="NSE",
                segment="INDEX",
                symbol="NIFTY",
                interval="5minute",
                day=_GUARD_DAY,
                minutes=5,
                cutoff_min=None,
            )
        assert from_stored is False
        assert [e for e in logs if e["event"] == "movers.stored_stale_fallback"] == []

    def test_read_intraday_bars_stale_with_warn_state_logs_once(self, tmp_path: Path) -> None:
        """The threaded ``stale_warn_state`` arm: the stale fallback emits ONE WARN.

        Drives the guard's warn call site directly (the ``compute_spot_candidates``
        threading rides the same state) — the event carries the HH:MM day-max pair
        via the COMMON ``minute_to_hhmm`` formatter.
        """
        import structlog.testing
        from brutex.dashboard.movers_snapshot import _read_intraday_bars, _StoredStaleWarnState

        _seed_guard_lake(tmp_path)
        state = _StoredStaleWarnState()
        with structlog.testing.capture_logs() as logs:
            _, from_stored = _read_intraday_bars(
                tmp_path,
                exchange="NSE",
                segment="INDEX",
                symbol="NIFTY",
                interval="5minute",
                day=_GUARD_DAY,
                minutes=5,
                cutoff_min=None,
                stale_warn_state=state,
            )
        assert from_stored is False
        [event] = [e for e in logs if e["event"] == "movers.stored_stale_fallback"]
        assert event["symbol"] == "NIFTY" and event["interval"] == "5minute"
        assert event["stored_max_ist"] == "09:15"  # the poison bucket's open
        assert event["base_max_ist"] == "09:24"  # the fresh base's last minute
        assert state.emitted == 1


class TestStoredStaleWarner:
    def test_stored_stale_warn_capped_deduped_suppressed(self) -> None:
        """Cap 20 / (symbol, interval) dedupe / ONE suppressed summary / reset seam."""
        import structlog.testing
        from brutex.dashboard.movers_snapshot import (
            _STORED_STALE_WARN_CAP,
            _StoredStaleWarnState,
            warn_stored_stale_fallback,
        )

        state = _StoredStaleWarnState()

        def _warn(symbol: str, interval: str = "5minute") -> None:
            warn_stored_stale_fallback(
                state,
                exchange="NSE",
                segment="INDEX",
                symbol=symbol,
                interval=interval,
                snapshot_day=_GUARD_DAY,
                stored_max_ist="09:15",
                base_max_ist="09:24",
            )

        with structlog.testing.capture_logs() as logs:
            for i in range(_STORED_STALE_WARN_CAP):
                _warn(f"SYM{i}")
            # Duplicate key → deduped (no event, no suppressed count).
            _warn("SYM0")
            # The SAME symbol under a DIFFERENT interval is a DISTINCT key — but we
            # are at the cap, so it lands in the suppressed tally.
            _warn("SYM0", interval="15minute")
            _warn("SYM-EXTRA")
        stale = [e for e in logs if e["event"] == "movers.stored_stale_fallback"]
        suppressed = [e for e in logs if e["event"] == "movers.stored_stale_fallback_suppressed"]
        assert len(stale) == _STORED_STALE_WARN_CAP  # a `>=`→`>` cap mutant emits 21
        assert len(suppressed) == 1  # the FIRST past-cap event only
        assert suppressed[0]["cap"] == _STORED_STALE_WARN_CAP
        assert state.emitted == _STORED_STALE_WARN_CAP
        assert state.suppressed == 2
        # The reset seam restores a fresh build's state.
        state.reset()
        assert (state.emitted, state.suppressed, state.seen) == (0, 0, set())
        with structlog.testing.capture_logs() as logs2:
            _warn("SYM0")
        assert [e for e in logs2 if e["event"] == "movers.stored_stale_fallback"] != []

    def test_stored_stale_warn_cap_literal_pinned(self) -> None:
        """The cap LITERAL is the contract — a 20→21 mutant must die HERE.

        The behavioral cap test above asserts RELATIVE to the constant, so it is
        blind to a changed literal; this pin is the independent teeth (the
        `FALLBACK_REL_LOG_CAP == 20` / `_NONCONTIGUOUS_WARN_CAP == 20` idiom).
        """
        from brutex.dashboard.movers_snapshot import _STORED_STALE_WARN_CAP

        assert _STORED_STALE_WARN_CAP == 20
