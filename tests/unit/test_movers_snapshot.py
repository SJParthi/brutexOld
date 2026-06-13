"""Unit tests for the pure ``/movers`` snapshot core (DEC-MOVERS-SNAPSHOT-VIEW-001).

100% line+branch on :mod:`brutex.dashboard.movers_snapshot` — the I/O-free
point-in-time movers logic (session bar-times, per-instrument as-of snapshot,
gainers/losers/volume ranking, Dhan-style JSON payload). No lake, no network.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from brutex.data.models import CandleBar
from brutex.dashboard.movers_snapshot import (
    MoverSnapshot,
    ScreenInstrument,
    build_snapshot_payload,
    compute_snapshot,
    interval_minutes,
    session_bar_times,
)

_NIFTY = ScreenInstrument(symbol="NIFTY", exchange="NSE", segment="INDEX", display_name="Nifty 50")
_BANK = ScreenInstrument(
    symbol="BANKNIFTY", exchange="NSE", segment="INDEX", display_name="Nifty Bank"
)


def _bar(*, minute: int, close: str, volume: int) -> CandleBar:
    """A valid 1-min bar on 2024-06-13 at 09:<minute> UTC (test fixture)."""
    c = Decimal(close)
    return CandleBar(
        timestamp=datetime(2024, 6, 13, 9, minute, tzinfo=UTC),
        open=c,
        high=c,
        low=c,
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
    # value = 100*10 + 110*20 = 3200 rupees → 320000 paisa.
    assert snap.value_paisa == 320000
    assert snap.bar_count == 2
    assert snap.ltp_inr == "₹110.00"


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


def _snap(inst: ScreenInstrument, *, pct: str | None, volume: int, value_paisa: int) -> MoverSnapshot:
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
    assert [r["symbol"] for r in payload["gainers"]] == ["NIFTY"]
    assert [r["symbol"] for r in payload["losers"]] == ["BANKNIFTY"]
    # Volume movers ranked desc: NIFTY (500) before BANKNIFTY (100).
    assert [r["symbol"] for r in payload["volume_movers"]] == ["NIFTY", "BANKNIFTY"]
    assert payload["instrument_count"] == 2
    assert payload["gainers"][0]["name"] == "Nifty 50"
    assert payload["gainers"][0]["pct_change"] == "10"
    assert "reference-only" in payload["note"]


def test_payload_none_pct_excluded_from_pct_lists_but_in_volume() -> None:
    # A no-previous-day snapshot (pct None) is excluded from gainers/losers,
    # but its volume still ranks — and its row renders change/pct as null.
    nopc = _snap(_NIFTY, pct=None, volume=300, value_paisa=10)
    payload = build_snapshot_payload(
        [nopc], interval="5minute", date="2024-06-13", time="09:20", top_n=10, skipped=0
    )
    assert payload["gainers"] == []
    assert payload["losers"] == []
    assert [r["symbol"] for r in payload["volume_movers"]] == ["NIFTY"]
    row = payload["volume_movers"][0]
    assert row["change"] is None
    assert row["pct_change"] is None


def test_payload_empty_lake_says_pull_spot_first() -> None:
    payload = build_snapshot_payload(
        [], interval="1day", date="2024-06-13", time=None, top_n=10, skipped=0
    )
    assert payload["instrument_count"] == 0
    assert payload["gainers"] == []
    assert "pull spot first" in payload["note"]


def test_payload_data_present_but_no_mover_at_this_bar_is_honest() -> None:
    # An instrument exists (counted) but is flat + zero-volume → no ranked rows;
    # the note distinguishes this from an empty lake (Golden Rule #10).
    flat = _snap(_NIFTY, pct="0", volume=0, value_paisa=0)
    payload = build_snapshot_payload(
        [flat], interval="5minute", date="2024-06-13", time="09:20", top_n=10, skipped=0
    )
    assert payload["instrument_count"] == 1
    assert payload["gainers"] == []
    assert payload["losers"] == []
    assert payload["volume_movers"] == []
    assert "none had a bar" in payload["note"]


def test_payload_skipped_counts_toward_instrument_count_and_daily_time_label() -> None:
    payload = build_snapshot_payload(
        [], interval="1day", date="2024-06-13", time=None, top_n=10, skipped=3
    )
    assert payload["instrument_count"] == 3
    assert payload["skipped_count"] == 3
    # Daily snapshot has no intraday time → the note reads "(daily)".
    assert "(daily)" in payload["note"]
