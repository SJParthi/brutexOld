"""Unit tests for brutex.engine.session_lens (DEC-SWEEP-RESULTS-DEPTH-001 WP2).

Covers: the look-ahead-clean trend fold (exact labels + prefix stability +
hypothesis walks), the sorted-input contract (shuffled/duplicate rejection),
every verified event flag on its known calendar date (the 2025 locked-flags
golden of the design's test plan), the expiry abstain paths (unmapped
instrument / pre-history / out-of-holiday-coverage), the IST conversion
boundary, and the pinned VIX band table.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from itertools import pairwise

import pytest
from brutex.calendars.nse import IST
from brutex.data.models import CandleBar
from brutex.engine.session_lens import (
    EVENT_BUDGET_WEEKEND,
    EVENT_EXPIRY,
    EVENT_FLAGS,
    EVENT_MUHURAT,
    EVENT_NIFTY_REBALANCE,
    VIX_BANDS,
    SessionLens,
    SessionLensError,
    TrendRegime,
    build_session_lens,
    entry_hour_ist,
    ist_trade_date,
    vix_band_label,
)
from hypothesis import given, settings
from hypothesis import strategies as st


def _daily_bar(d: date, o: str, h: str, lo: str, c: str) -> CandleBar:
    """One derived-1day bar: midnight-IST timestamp, stored UTC (the lake law)."""
    ts = datetime(d.year, d.month, d.day, tzinfo=IST).astimezone(UTC)
    return CandleBar(
        timestamp=ts,
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(lo),
        close=Decimal(c),
        volume=0,
        open_interest=None,
    )


def _flat_bars(days: list[date]) -> list[CandleBar]:
    return [_daily_bar(d, "100", "101", "99", "100.50") for d in days]


# ── Trend fold ───────────────────────────────────────────────────────────────

#: A hand-designed daily walk (k=2 fractal): bars 0-4 warm up and confirm the
#: bar-2 swing HIGH (105) at bar 4; bar 5 closes above it → BOS bullish → UP;
#: bar 6 confirms the bar-4 swing LOW (91) and holds above it → still UP;
#: bar 7 closes below the confirmed swing low while trending UP → CHoCH → DOWN.
_TREND_WALK = [
    ("100", "90", "95"),
    ("101", "91", "96"),
    ("105", "95", "100"),
    ("101", "92", "97"),
    ("100", "91", "96"),
    ("106", "96", "105.50"),
    ("104", "98", "100"),
    ("99", "85", "86"),
]
_TREND_DAYS = [date(2024, 6, 3) + timedelta(days=i) for i in range(len(_TREND_WALK))]
_TREND_BARS = [
    _daily_bar(d, c, h, lo, c) for d, (h, lo, c) in zip(_TREND_DAYS, _TREND_WALK, strict=True)
]


class TestTrendFold:
    def test_exact_labels_on_designed_walk(self) -> None:
        lens = build_session_lens(_TREND_BARS, instrument="NIFTY")
        expected = [
            TrendRegime.SIDEWAYS,  # warm-up
            TrendRegime.SIDEWAYS,
            TrendRegime.SIDEWAYS,
            TrendRegime.SIDEWAYS,
            TrendRegime.SIDEWAYS,  # swing high confirmed, no break yet
            TrendRegime.UP,  # BOS bullish
            TrendRegime.UP,  # holds above the confirmed swing low
            TrendRegime.DOWN,  # CHoCH bearish
        ]
        assert [lens.trend_by_date[d] for d in _TREND_DAYS] == expected

    def test_look_ahead_clean_prefix_stability(self) -> None:
        """Appending future sessions never changes an already-labelled date."""
        full = build_session_lens(_TREND_BARS, instrument="NIFTY")
        for cut in range(1, len(_TREND_BARS)):
            prefix = build_session_lens(_TREND_BARS[:cut], instrument="NIFTY")
            for d in _TREND_DAYS[:cut]:
                assert prefix.trend_by_date[d] == full.trend_by_date[d]

    @given(
        closes=st.lists(st.integers(min_value=9_000, max_value=11_000), min_size=1, max_size=30),
        cut=st.integers(min_value=1, max_value=30),
    )
    @settings(max_examples=50, deadline=None)
    def test_look_ahead_clean_on_random_walks(self, closes: list[int], cut: int) -> None:
        base = date(2024, 1, 1)
        bars = [
            _daily_bar(
                base + timedelta(days=i),
                str(Decimal(c) / 100),
                str(Decimal(c + 50) / 100),
                str(Decimal(c - 50) / 100),
                str(Decimal(c) / 100),
            )
            for i, c in enumerate(closes)
        ]
        cut = min(cut, len(bars))
        full = build_session_lens(bars, instrument="NIFTY")
        prefix = build_session_lens(bars[:cut], instrument="NIFTY")
        for i in range(cut):
            d = base + timedelta(days=i)
            assert prefix.trend_by_date[d] == full.trend_by_date[d]


# ── Input contract ───────────────────────────────────────────────────────────


class TestBuildValidation:
    def test_shuffled_input_rejected(self) -> None:
        bars = list(reversed(_TREND_BARS))
        with pytest.raises(SessionLensError, match="strictly increasing"):
            build_session_lens(bars, instrument="NIFTY")

    def test_duplicate_session_date_rejected(self) -> None:
        d = date(2024, 6, 3)
        first = _daily_bar(d, "100", "101", "99", "100")
        second = CandleBar(
            timestamp=first.timestamp + timedelta(hours=1),
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100"),
            volume=0,
            open_interest=None,
        )
        with pytest.raises(SessionLensError, match="share IST session date"):
            build_session_lens([first, second], instrument="NIFTY")

    def test_empty_bars_yield_empty_lens(self) -> None:
        lens = build_session_lens([], instrument="NOT-A-REAL-UNDERLYING")
        assert lens.trend_by_date == {}
        assert lens.event_flags_by_date == {}

    def test_lens_is_frozen(self) -> None:
        lens = SessionLens(trend_by_date={}, event_flags_by_date={})
        with pytest.raises(AttributeError):
            lens.trend_by_date = {}  # type: ignore[misc]


# ── Event flags (the 2025 locked-flags golden) ───────────────────────────────

#: Known 2025 calendar spot dates, each exercising one verified flag source:
#: 2025-01-02 Thu = NIFTY weekly expiry · 2025-02-01 Sat = Budget special
#: weekend session · 2025-03-28 Fri = NIFTY 50 rebalance effective date ·
#: 2025-04-30 Wed = ACTUAL weekly expiry (calendar Thu 2025-05-01 is the
#: Maharashtra Day holiday → NSE prior-trading-day shift) · 2025-05-02 Fri =
#: plain session · 2025-10-20 Mon = ACTUAL weekly expiry (calendar Tue
#: 2025-10-21 is the Diwali holiday) · 2025-10-21 = Muhurat session.
_GOLDEN_2025_DAYS = [
    date(2025, 1, 2),
    date(2025, 2, 1),
    date(2025, 3, 28),
    date(2025, 4, 30),
    date(2025, 5, 2),
    date(2025, 10, 20),
    date(2025, 10, 21),
]
_GOLDEN_2025_FLAGS: dict[date, tuple[str, ...]] = {
    date(2025, 1, 2): (EVENT_EXPIRY,),
    date(2025, 2, 1): (EVENT_BUDGET_WEEKEND,),
    date(2025, 3, 28): (EVENT_NIFTY_REBALANCE,),
    date(2025, 4, 30): (EVENT_EXPIRY,),
    date(2025, 10, 20): (EVENT_EXPIRY,),
    date(2025, 10, 21): (EVENT_MUHURAT,),
}


class TestEventFlags:
    def test_locked_2025_flags_golden(self) -> None:
        lens = build_session_lens(_flat_bars(_GOLDEN_2025_DAYS), instrument="NIFTY")
        assert dict(lens.event_flags_by_date) == _GOLDEN_2025_FLAGS

    def test_flag_tokens_are_the_pinned_vocabulary(self) -> None:
        assert EVENT_FLAGS == ("expiry", "muhurat", "budget_weekend", "nifty_rebalance")

    def test_banknifty_monthly_expiry_with_weekly_killed(self) -> None:
        """SEBI killed BANKNIFTY weeklies (2024-11-14) — monthly still flags."""
        days = [date(2025, 6, 24), date(2025, 6, 25), date(2025, 6, 26)]
        lens = build_session_lens(_flat_bars(days), instrument="BANKNIFTY")
        assert lens.event_flags_by_date == {date(2025, 6, 26): (EVENT_EXPIRY,)}

    def test_unmapped_instrument_abstains_expiry_but_keeps_other_flags(self) -> None:
        days = [date(2025, 1, 2), date(2025, 3, 28)]
        lens = build_session_lens(_flat_bars(days), instrument="INDIAVIX")
        # 2025-01-02 IS a NIFTY expiry but INDIAVIX has no expiry calendar —
        # the flag abstains; the rebalance market-event flag still applies.
        assert lens.event_flags_by_date == {date(2025, 3, 28): (EVENT_NIFTY_REBALANCE,)}

    def test_pre_history_dates_abstain_expiry(self) -> None:
        """Two pre-2020 dates: the expiry regime history cannot answer."""
        days = [date(2019, 12, 30), date(2019, 12, 31)]
        lens = build_session_lens(_flat_bars(days), instrument="NIFTY")
        assert lens.event_flags_by_date == {}
        assert set(lens.trend_by_date) == set(days)

    def test_out_of_holiday_coverage_abstains_expiry(self) -> None:
        """BANKNIFTY late-Dec-2026: monthly rolls into the uncovered 2027."""
        lens = build_session_lens(_flat_bars([date(2026, 12, 30)]), instrument="BANKNIFTY")
        assert lens.event_flags_by_date == {}

    def test_out_of_coverage_weekly_probe_abstains(self) -> None:
        """NIFTY 2027 date: the weekly shift needs the missing 2027 holidays."""
        lens = build_session_lens(_flat_bars([date(2027, 1, 4)]), instrument="NIFTY")
        assert lens.event_flags_by_date == {}


# ── IST conversion boundary ──────────────────────────────────────────────────


class TestIstBoundary:
    def test_entry_hour_ist_converts_utc(self) -> None:
        # 03:46 UTC == 09:16 IST.
        assert entry_hour_ist(datetime(2024, 1, 2, 3, 46, tzinfo=UTC)) == 9

    def test_entry_hour_ist_rejects_naive(self) -> None:
        with pytest.raises(SessionLensError, match="naive datetime"):
            entry_hour_ist(datetime(2024, 1, 2, 9, 16))

    def test_ist_trade_date_crosses_midnight(self) -> None:
        # 20:00 UTC on Jan 2 == 01:30 IST on Jan 3.
        assert ist_trade_date(datetime(2024, 1, 2, 20, 0, tzinfo=UTC)) == date(2024, 1, 3)

    def test_ist_trade_date_same_day(self) -> None:
        assert ist_trade_date(datetime(2024, 1, 2, 5, 0, tzinfo=UTC)) == date(2024, 1, 2)

    def test_ist_trade_date_rejects_naive(self) -> None:
        with pytest.raises(SessionLensError, match="naive datetime"):
            ist_trade_date(datetime(2024, 1, 2, 9, 16))


# ── VIX bands ────────────────────────────────────────────────────────────────


class TestVixBands:
    @pytest.mark.parametrize(
        ("vix", "label"),
        [
            ("0", "<12"),
            ("11.99", "<12"),
            ("12", "12-15"),
            ("14.999", "12-15"),
            ("15", "15-20"),
            ("20", "20-25"),
            ("25", "25-30"),
            ("29.9999", "25-30"),
            ("30", ">30"),
            ("1000000000", ">30"),
        ],
    )
    def test_band_labels(self, vix: str, label: str) -> None:
        assert vix_band_label(Decimal(vix)) == label

    def test_bands_partition_the_nonnegative_line(self) -> None:
        assert VIX_BANDS[0][1] == 0.0
        for prev, cur in pairwise(VIX_BANDS):
            assert prev[2] == cur[1]

    def test_negative_rejected(self) -> None:
        with pytest.raises(SessionLensError, match="finite and >= 0"):
            vix_band_label(Decimal("-0.01"))

    def test_non_finite_rejected(self) -> None:
        with pytest.raises(SessionLensError, match="finite and >= 0"):
            vix_band_label(Decimal("NaN"))
