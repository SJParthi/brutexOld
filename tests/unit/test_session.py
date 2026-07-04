"""Tests for `brutex.data.session` — NSE/BSE session filter (09:15–15:30 IST).

100% line+branch: the open-inclusive / close-exclusive boundaries, the legacy
date-blind intraday filter (keeps in-session, drops pre-open + post-close), and
the calendar-aware :func:`filter_to_trading_session`
(DEC-INGEST-BOUNDARY-INTEGRITY-001): the regular-day golden parity vs the legacy
filter, the muhurat-evening KEEP (a sanctioned behavior change), the
special-weekend KEEP, the non-trading-date DROP, and the out-of-coverage
fixed-window abstention.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from brutex.calendars.nse import get_session
from brutex.data.models import CandleBar
from brutex.data.session import (
    ALL_FILTERED,
    SessionFilterResult,
    filter_intraday_session,
    filter_to_trading_session,
    format_ist_clock,
    in_session,
    is_session_exempt,
)

_IST = ZoneInfo("Asia/Kolkata")


def _bar(hh: int, mm: int) -> CandleBar:
    """A bar whose UTC timestamp equals ``hh:mm`` UTC on 2021-01-04."""
    return CandleBar(
        timestamp=datetime(2021, 1, 4, hh, mm, tzinfo=UTC),
        open=Decimal("100"),
        high=Decimal("100"),
        low=Decimal("100"),
        close=Decimal("100"),
        volume=0,
        open_interest=None,
    )


def _ist_bar(year: int, month: int, day: int, hh: int, mm: int) -> CandleBar:
    """A bar whose timestamp is the given IST wall clock (stored UTC, as the lake does)."""
    ts = datetime(year, month, day, hh, mm, tzinfo=_IST).astimezone(UTC)
    return CandleBar(
        timestamp=ts,
        open=Decimal("100"),
        high=Decimal("100"),
        low=Decimal("100"),
        close=Decimal("100"),
        volume=0,
        open_interest=None,
    )


# IST = UTC + 5:30 → 09:15 IST = 03:45 UTC; 15:30 IST = 10:00 UTC.
class TestInSession:
    @pytest.mark.parametrize(
        ("utc_h", "utc_m", "expected"),
        [
            (3, 45, True),  # 09:15 IST — open, inclusive
            (3, 44, False),  # 09:14 IST — pre-open
            (3, 30, False),  # 09:00 IST — pre-open auction
            (6, 0, True),  # 11:30 IST — mid-session
            (9, 59, True),  # 15:29 IST — last intraday minute
            (10, 0, False),  # 15:30 IST — close, exclusive
            (10, 1, False),  # 15:31 IST — post-close
        ],
    )
    def test_boundaries(self, utc_h: int, utc_m: int, expected: bool) -> None:
        assert in_session(_bar(utc_h, utc_m).timestamp) is expected


class TestFilterIntradaySession:
    def test_keeps_in_session_drops_out(self) -> None:
        bars = [_bar(3, 30), _bar(3, 45), _bar(6, 0), _bar(9, 59), _bar(10, 0)]
        kept = filter_intraday_session(bars)
        assert [b.timestamp.hour * 60 + b.timestamp.minute for b in kept] == [
            3 * 60 + 45,
            6 * 60,
            9 * 60 + 59,
        ]

    def test_empty(self) -> None:
        assert filter_intraday_session([]) == []


class TestFilterToTradingSession:
    """Calendar-aware boundary filter (DEC-INGEST-BOUNDARY-INTEGRITY-001)."""

    def test_golden_regular_day_parity_with_legacy_filter(self) -> None:
        # GOLDEN: on a REGULAR trading day (2024-01-02, Tuesday) the calendar-aware
        # kept-set is byte-identical to the legacy fixed-window filter's kept-set —
        # every minute 08:00–16:59 IST, pinning the same [09:15, 15:30) inclusivity.
        bars = [_ist_bar(2024, 1, 2, h, m) for h in range(8, 17) for m in range(60)]
        verdict = filter_to_trading_session(bars)
        legacy = filter_intraday_session(bars)
        assert verdict.kept == legacy
        assert len(verdict.kept) == 375  # the full 09:15–15:29 session
        assert verdict.dropped_non_trading_date == 0
        assert verdict.dropped_off_session == len(bars) - 375

    def test_muhurat_evening_bar_is_kept(self) -> None:
        # SANCTIONED BEHAVIOR CHANGE: an evening Diwali-muhurat bar (2024-11-01
        # session 18:00–19:00 IST) is now KEPT — the date-blind filter wrongly
        # dropped it.
        evening = _ist_bar(2024, 11, 1, 18, 30)
        assert filter_intraday_session([evening]) == []  # the old defect, pinned
        verdict = filter_to_trading_session([evening])
        assert verdict.kept == [evening]
        assert verdict.dropped == 0

    def test_muhurat_session_boundaries_open_inclusive_close_exclusive(self) -> None:
        at_open = _ist_bar(2024, 11, 1, 18, 0)
        at_close = _ist_bar(2024, 11, 1, 19, 0)
        verdict = filter_to_trading_session([at_open, at_close])
        assert verdict.kept == [at_open]  # open inclusive, close exclusive
        assert verdict.dropped_off_session == 1

    def test_regular_hours_bar_on_a_muhurat_date_is_dropped(self) -> None:
        # On Diwali the exchange trades ONLY the muhurat hour — a 10:00 IST bar on
        # that date is vendor junk and is dropped (counted off-session).
        verdict = filter_to_trading_session([_ist_bar(2024, 11, 1, 10, 0)])
        assert verdict.kept == []
        assert verdict.dropped_off_session == 1
        assert verdict.all_filtered is True

    def test_special_weekend_session_bar_is_kept(self) -> None:
        # 2025-02-01 (Saturday) was a Budget special session 09:15–15:30 — KEPT.
        saturday_budget = _ist_bar(2025, 2, 1, 10, 0)
        verdict = filter_to_trading_session([saturday_budget])
        assert verdict.kept == [saturday_budget]
        assert verdict.dropped == 0

    def test_pre_open_bar_is_dropped_and_counted(self) -> None:
        verdict = filter_to_trading_session([_ist_bar(2024, 1, 2, 9, 0)])
        assert verdict.kept == []
        assert verdict.dropped_off_session == 1
        assert verdict.dropped == 1

    def test_holiday_bar_is_dropped_as_non_trading_date(self) -> None:
        # 2024-01-26 (Republic Day, a Friday) — in-window wall clock, NO session:
        # the date-blind filter wrongly KEPT it; now dropped + counted.
        holiday_bar = _ist_bar(2024, 1, 26, 10, 0)
        assert filter_intraday_session([holiday_bar]) == [holiday_bar]  # old defect
        verdict = filter_to_trading_session([holiday_bar])
        assert verdict.kept == []
        assert verdict.dropped_non_trading_date == 1
        assert verdict.all_filtered is True

    def test_weekend_bar_is_dropped_as_non_trading_date(self) -> None:
        verdict = filter_to_trading_session([_ist_bar(2024, 1, 6, 10, 0)])  # Saturday
        assert verdict.kept == []
        assert verdict.dropped_non_trading_date == 1

    def test_out_of_coverage_date_abstains_to_fixed_window(self) -> None:
        # 2027 is past the published holiday-list span — the filter ABSTAINS from
        # the calendar and falls back to the legacy fixed [09:15, 15:30) window,
        # byte-identical to the old filter for those dates (Golden Rule #1).
        in_window = _ist_bar(2027, 1, 4, 10, 0)  # Monday, in-window
        out_window = _ist_bar(2027, 1, 4, 17, 30)
        verdict = filter_to_trading_session([in_window, out_window])
        assert verdict.kept == [in_window]
        assert verdict.dropped_off_session == 1
        assert verdict.dropped_non_trading_date == 0

    def test_session_lookup_is_cached_per_unique_date(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # O(bars + unique_dates): many bars on ONE date resolve the session ONCE.
        # The real function is taken from its home module (brutex.calendars.nse —
        # the identical object brutex.data.session binds); the patch targets the
        # session module's binding by dotted path, so this file imports the module
        # in exactly ONE style (CodeQL py/import-and-import-from, alert #534).
        calls = {"n": 0}

        def _counting(d: object) -> object:
            calls["n"] += 1
            return get_session(d)  # type: ignore[arg-type]

        monkeypatch.setattr("brutex.data.session.get_session", _counting)
        bars = [_ist_bar(2024, 1, 2, 10, m) for m in range(30)]
        verdict = filter_to_trading_session(bars)
        assert len(verdict.kept) == 30
        assert calls["n"] == 1  # one lookup for the one unique date

    def test_empty_input(self) -> None:
        verdict = filter_to_trading_session([])
        assert verdict.kept == []
        assert verdict.dropped == 0
        assert verdict.all_filtered is False  # nothing came in — NOT all-filtered

    def test_all_filtered_constant_and_result_shape(self) -> None:
        assert ALL_FILTERED == "ALL_FILTERED"
        kept_bar = _ist_bar(2024, 1, 2, 10, 0)
        verdict = SessionFilterResult(kept=[kept_bar], dropped_off_session=2)
        assert verdict.dropped == 2
        assert verdict.all_filtered is False  # a survivor → never all-filtered


class TestSessionExempt:
    """GIFT Nifty is exempt from the NSE 09:15–15:30 filter (DEC-DHAN-GIFTNIFTY-SESSION-001)."""

    @pytest.mark.parametrize(
        ("symbol", "expected"),
        [
            ("GIFTNIFTY", True),
            ("giftnifty", True),  # case-insensitive
            ("  GIFTNIFTY  ", True),  # whitespace-tolerant
            ("NIFTY", False),
            ("BANKNIFTY", False),
            ("SENSEX", False),
            ("", False),
            (None, False),
        ],
    )
    def test_is_session_exempt(self, symbol: str | None, expected: bool) -> None:
        assert is_session_exempt(symbol) is expected


class TestFormatIstClock:
    """`format_ist_clock` — the COMMON UTC→clean-IST display rule (DEC-WINDOW-LEDGER-001)."""

    def test_utc_iso_string_converts_to_clean_ist(self) -> None:
        # 08:35:53 UTC + 5:30 = 14:05:53 IST; the +05:30 offset is dropped.
        assert format_ist_clock("2026-06-09T08:35:53.763513+00:00") == "2026-06-09 14:05:53"

    def test_utc_datetime_converts_to_clean_ist(self) -> None:
        assert (
            format_ist_clock(datetime(2026, 6, 9, 8, 35, 53, tzinfo=UTC)) == "2026-06-09 14:05:53"
        )

    def test_none_passes_through(self) -> None:
        assert format_ist_clock(None) is None

    def test_naive_datetime_assumed_ist(self) -> None:
        # No tzinfo → assumed already-IST, just formatted (no shift).
        assert format_ist_clock(datetime(2026, 6, 9, 14, 5, 53)) == "2026-06-09 14:05:53"

    def test_unparseable_string_returned_unchanged(self) -> None:
        # A free-text note (e.g. last_error) is not a timestamp — left as-is.
        assert format_ist_clock("DhanRateLimitError") == "DhanRateLimitError"
