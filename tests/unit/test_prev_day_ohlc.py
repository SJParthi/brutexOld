"""Tests for `brutex.indicators.prev_day_ohlc` — previous day OHLC anchors.

Verifies the calendar-aware lookup matches the operator's Pine v6 source:
    yHigh = request.security(ticker, "D", high[1], lookahead=barmerge.lookahead_on)
    (and similarly for yLow / yOpen / yClose)

The `[1]` index on the daily TF means "yesterday's completed daily bar."
We use the NSE calendar to skip weekends and holidays correctly.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date

import pytest
from brutex.calendars.nse import HolidayDataUnavailableError
from brutex.indicators.prev_day_ohlc import (
    PREV_DAY_NO_ANCHOR_EVENT,
    PREV_DAY_STALE_ANCHOR_EVENT,
    DailyCandle,
    find_prev_trading_day,
    lookup_prev_day_ohlc,
    lookup_prev_day_ohlc_series,
)
from structlog.testing import capture_logs


class TestFindPrevTradingDay:
    def test_tuesday_prev_is_monday(self) -> None:
        # Tue 2026-05-12 -> Mon 2026-05-11 (no holiday)
        result = find_prev_trading_day(date(2026, 5, 12))
        assert result == date(2026, 5, 11)

    def test_monday_prev_is_friday_skipping_weekend(self) -> None:
        # Mon 2026-05-11 -> Fri 2026-05-08 (skip Sat 9, Sun 10)
        result = find_prev_trading_day(date(2026, 5, 11))
        assert result == date(2026, 5, 8)

    def test_skips_known_holiday(self) -> None:
        # 2024-01-26 is Republic Day (NSE holiday).
        # 2024-01-27 is a Saturday.
        # 2024-01-28 is a Sunday.
        # 2024-01-29 is Monday -> prev should be Thu 2024-01-25
        result = find_prev_trading_day(date(2024, 1, 29))
        assert result == date(2024, 1, 25)

    def test_returns_none_when_lookback_exhausted(self) -> None:
        # Mon 2026-05-11 with lookback_max=1: only Sun 2026-05-10 is checked,
        # which is not a trading day -> loop ends without a hit -> None.
        result = find_prev_trading_day(date(2026, 5, 11), lookback_max=1)
        assert result is None

    def test_returns_none_when_lookback_zero(self) -> None:
        # lookback_max=0 skips the loop entirely.
        result = find_prev_trading_day(date(2026, 5, 12), lookback_max=0)
        assert result is None


class TestLookupPrevDayOHLC:
    @pytest.fixture
    def daily_dict(self) -> dict[date, DailyCandle]:
        return {
            date(2026, 5, 8): DailyCandle(
                day=date(2026, 5, 8),
                open_paisa=5_350_000,
                high_paisa=5_400_000,
                low_paisa=5_340_000,
                close_paisa=5_390_000,
            ),
            date(2026, 5, 11): DailyCandle(
                day=date(2026, 5, 11),
                open_paisa=5_390_000,
                high_paisa=5_420_000,
                low_paisa=5_385_000,
                close_paisa=5_410_000,
            ),
            date(2026, 5, 12): DailyCandle(
                day=date(2026, 5, 12),
                open_paisa=5_410_000,
                high_paisa=5_430_000,
                low_paisa=5_395_000,
                close_paisa=5_400_000,
            ),
        }

    def test_lookup_tuesday_returns_monday(self, daily_dict: dict[date, DailyCandle]) -> None:
        result = lookup_prev_day_ohlc(date(2026, 5, 12), daily_dict)
        assert result is not None
        assert result.today == date(2026, 5, 12)
        assert result.prev_day == date(2026, 5, 11)
        assert result.pdh_paisa == 5_420_000
        assert result.pdl_paisa == 5_385_000
        assert result.pdo_paisa == 5_390_000
        assert result.pdc_paisa == 5_410_000

    def test_lookup_monday_returns_friday(self, daily_dict: dict[date, DailyCandle]) -> None:
        result = lookup_prev_day_ohlc(date(2026, 5, 11), daily_dict)
        assert result is not None
        assert result.prev_day == date(2026, 5, 8)
        assert result.pdh_paisa == 5_400_000
        assert result.pdl_paisa == 5_340_000

    def test_today_is_holiday_raises(self, daily_dict: dict[date, DailyCandle]) -> None:
        # 2024-01-26 is Republic Day
        with pytest.raises(ValueError, match="not a trading day"):
            lookup_prev_day_ohlc(date(2024, 1, 26), daily_dict)

    def test_missing_daily_candle_returns_none(self) -> None:
        # Empty dict -> no prev_day lookup possible
        result = lookup_prev_day_ohlc(date(2026, 5, 12), {})
        assert result is None

    def test_returns_none_when_no_candle_in_lookback(self) -> None:
        # The lookup's own bounded walk (no candle anywhere within 14 calendar
        # days) is the sole remaining None path — logged, never silent
        # (DEC-CALENDAR-OBSERVED-SESSIONS-001 "data wins" walk-back).
        with capture_logs() as logs:
            result = lookup_prev_day_ohlc(date(2026, 5, 12), {})
        assert result is None
        assert [e for e in logs if e["event"] == PREV_DAY_NO_ANCHOR_EVENT]


class TestLookupSeries:
    def test_bulk_lookup_skips_missing(self) -> None:
        daily_dict = {
            date(2026, 5, 8): DailyCandle(
                day=date(2026, 5, 8),
                open_paisa=100_00,
                high_paisa=110_00,
                low_paisa=90_00,
                close_paisa=105_00,
            ),
        }
        # DATA-WINS semantic change (DEC-CALENDAR-OBSERVED-SESSIONS-001): before
        # the walk-back, 2026-05-12 (calendar prev 2026-05-11 NOT in dict) was a
        # silent miss; NOW it resolves to the latest earlier session WITH a
        # candle (2026-05-08) — a STALE anchor with honest provenance + a warn.
        trading_days = [date(2026, 5, 11), date(2026, 5, 12)]
        with capture_logs() as logs:
            result = lookup_prev_day_ohlc_series(trading_days, daily_dict)
        assert date(2026, 5, 11) in result
        assert result[date(2026, 5, 11)].prev_day == date(2026, 5, 8)
        assert date(2026, 5, 12) in result
        assert result[date(2026, 5, 12)].prev_day == date(2026, 5, 8)
        warns = [e for e in logs if e["event"] == PREV_DAY_STALE_ANCHOR_EVENT]
        assert len(warns) == 1  # one warn for the one gapped day, not per series

    def test_bulk_lookup_all_present(self) -> None:
        daily_dict = {
            date(2026, 5, 8): DailyCandle(
                day=date(2026, 5, 8),
                open_paisa=100_00,
                high_paisa=110_00,
                low_paisa=90_00,
                close_paisa=105_00,
            ),
            date(2026, 5, 11): DailyCandle(
                day=date(2026, 5, 11),
                open_paisa=105_00,
                high_paisa=115_00,
                low_paisa=95_00,
                close_paisa=110_00,
            ),
        }
        trading_days = [date(2026, 5, 11), date(2026, 5, 12)]
        result = lookup_prev_day_ohlc_series(trading_days, daily_dict)
        assert len(result) == 2


class TestNoLookAhead:
    """Property P-06: PDH/PDL must only depend on days BEFORE today."""

    def test_today_data_not_used(self) -> None:
        # Even if today's daily candle is in the dict, PDH must be from
        # yesterday's candle, never today's.
        daily_dict = {
            date(2026, 5, 11): DailyCandle(
                day=date(2026, 5, 11),
                open_paisa=100_00,
                high_paisa=110_00,
                low_paisa=90_00,
                close_paisa=105_00,
            ),
            date(2026, 5, 12): DailyCandle(  # today — must NOT leak
                day=date(2026, 5, 12),
                open_paisa=999_99,
                high_paisa=999_99,
                low_paisa=999_99,
                close_paisa=999_99,
            ),
        }
        result = lookup_prev_day_ohlc(date(2026, 5, 12), daily_dict)
        assert result is not None
        # Must be from 2026-05-11, NOT 2026-05-12
        assert result.prev_day == date(2026, 5, 11)
        assert result.pdh_paisa == 110_00
        assert result.pdh_paisa != 999_99  # explicit: today's data is not leaked


def _candle(d: date, base: int = 100_00) -> DailyCandle:
    return DailyCandle(
        day=d,
        open_paisa=base,
        high_paisa=base + 10_00,
        low_paisa=base - 10_00,
        close_paisa=base + 5_00,
    )


def _stale_warns(logs: Sequence[Mapping[str, object]]) -> list[Mapping[str, object]]:
    return [e for e in logs if e["event"] == PREV_DAY_STALE_ANCHOR_EVENT]


class TestDataWinsWalkBack:
    """DEC-CALENDAR-OBSERVED-SESSIONS-001 — the ITEM-2 "data wins" walk-back.

    INTENDED semantic change, pinned here: a calendar prior session whose daily
    candle is missing no longer yields ``None`` — the lookup walks back (over
    calendar sessions ONLY) to the latest session WITH a candle and returns the
    STALE anchor with ONE warning. This deliberately ALSO changes the GENUINE
    data-gap behaviour (stale anchor + warn instead of None). The change lives on
    the DEFINITIONS surface (``resolve_fib_anchor_levels`` / ``resolve_fib_levels``)
    — the brute-force engine's PREV_DAY fib path is stream-based (stage1a computes
    ``prev_ohlc`` off the bar stream, never via this lookup), so sweep output is
    byte-identical (refuter finding R3).
    """

    def test_walkback_stale_anchor_on_missing_candle(self) -> None:
        # FAIL-BEFORE/PASS-AFTER: calendar prev (Mon 2026-05-11) absent from the
        # dict, an older session (Fri 2026-05-08) present -> previously None, now
        # the stale Friday anchor + exactly ONE stale_anchor warn.
        daily = {date(2026, 5, 8): _candle(date(2026, 5, 8))}
        with capture_logs() as logs:
            result = lookup_prev_day_ohlc(date(2026, 5, 12), daily)
        assert result is not None
        assert result.prev_day == date(2026, 5, 8)  # honest provenance
        warns = _stale_warns(logs)
        assert len(warns) == 1
        assert warns[0]["today"] == "2026-05-12"
        assert warns[0]["calendar_prev_day"] == "2026-05-11"
        assert warns[0]["used_prev_day"] == "2026-05-08"
        assert warns[0]["gap_calendar_days"] == 3
        assert warns[0]["overlaid"] is False

    def test_walkback_genuine_gap_semantic_change_pinned(self) -> None:
        # The INTENDED behaviour-change regression (DEC-CALENDAR-OBSERVED-SESSIONS-001):
        # a PURE data gap (no calendar drift, no overlay) now yields a stale
        # anchor + warn where it previously yielded None — "data wins".
        daily = {
            date(2026, 5, 6): _candle(date(2026, 5, 6)),  # Wed
            # Thu 2026-05-07 missing — a genuine vendor gap
        }
        with capture_logs() as logs:
            result = lookup_prev_day_ohlc(date(2026, 5, 8), daily)  # Fri
        assert result is not None
        assert result.prev_day == date(2026, 5, 6)
        assert len(_stale_warns(logs)) == 1

    def test_walkback_none_after_14_days(self) -> None:
        with capture_logs() as logs:
            result = lookup_prev_day_ohlc(date(2026, 5, 12), {})
        assert result is None
        no_anchor = [e for e in logs if e["event"] == PREV_DAY_NO_ANCHOR_EVENT]
        assert len(no_anchor) == 1
        assert no_anchor[0]["lookback_max"] == 14

    def test_normal_path_no_warn_byte_identical(self) -> None:
        daily = {date(2026, 5, 11): _candle(date(2026, 5, 11))}
        with capture_logs() as logs:
            result = lookup_prev_day_ohlc(date(2026, 5, 12), daily)
        assert result is not None
        assert result.prev_day == date(2026, 5, 11)
        assert result.pdh_paisa == 110_00
        assert logs == []  # ZERO log events on the normal path

    def test_walkback_overlaid_prev_day_no_warn(self) -> None:
        # The overlay EXPLAINS the drift: calendar prev (2026-05-12) is overlaid
        # and the day before it has the candle -> anchor returned SILENTLY.
        daily = {date(2026, 5, 11): _candle(date(2026, 5, 11))}
        with capture_logs() as logs:
            result = lookup_prev_day_ohlc(
                date(2026, 5, 13), daily, observed=frozenset({date(2026, 5, 12)})
            )
        assert result is not None
        assert result.prev_day == date(2026, 5, 11)
        assert _stale_warns(logs) == []

    def test_walkback_overlaid_flag_in_warn_payload(self) -> None:
        # An overlaid day between the used anchor and today, with the calendar
        # prev's candle ALSO missing -> stale warn carries overlaid=True.
        daily = {date(2026, 5, 8): _candle(date(2026, 5, 8))}  # Fri
        with capture_logs() as logs:
            result = lookup_prev_day_ohlc(
                date(2026, 5, 13), daily, observed=frozenset({date(2026, 5, 12)})
            )
        assert result is not None
        assert result.prev_day == date(2026, 5, 8)
        warns = _stale_warns(logs)
        assert len(warns) == 1
        assert warns[0]["calendar_prev_day"] == "2026-05-11"
        assert warns[0]["overlaid"] is True

    def test_muhurat_candle_never_wins(self) -> None:
        # ADR-0012 §4.5 #7 (refuter finding R4): the 2025-10-21 Muhurat session is
        # NOT a regular trading day, so its daily candle — even when present in a
        # lake-fed dict — can NEVER become the prev-day anchor. The walk lands on
        # the prior REGULAR session with a candle (Fri 2025-10-17) instead.
        daily = {
            date(2025, 10, 21): _candle(date(2025, 10, 21), base=999_00),  # Muhurat
            date(2025, 10, 17): _candle(date(2025, 10, 17)),  # Fri (regular)
            # Mon 2025-10-20 (the calendar prev of Thu 10-23) deliberately missing
        }
        with capture_logs() as logs:
            result = lookup_prev_day_ohlc(date(2025, 10, 23), daily)
        assert result is not None
        assert result.prev_day == date(2025, 10, 17)
        assert result.pdh_paisa != 999_00 + 10_00  # the Muhurat bar never leaked
        assert len(_stale_warns(logs)) == 1

    def test_muhurat_calendar_prev_unchanged_when_candle_present(self) -> None:
        # COMPLETE (non-gapped) data around Diwali behaves byte-identically to the
        # pre-change code: the calendar prev (Mon 2025-10-20) has its candle, so
        # the walk stops there silently — the Muhurat candle is never consulted.
        daily = {
            date(2025, 10, 21): _candle(date(2025, 10, 21), base=999_00),  # Muhurat
            date(2025, 10, 20): _candle(date(2025, 10, 20)),
        }
        with capture_logs() as logs:
            result = lookup_prev_day_ohlc(date(2025, 10, 23), daily)
        assert result is not None
        assert result.prev_day == date(2025, 10, 20)
        assert logs == []

    def test_today_overlaid_raises_valueerror(self) -> None:
        with pytest.raises(ValueError, match="not a trading day"):
            lookup_prev_day_ohlc(date(2026, 5, 12), {}, observed=frozenset({date(2026, 5, 12)}))

    def test_find_prev_trading_day_observed_subtraction(self) -> None:
        # Overlaid Monday 2026-05-11 -> the prior session is Friday 2026-05-08.
        result = find_prev_trading_day(date(2026, 5, 12), observed=frozenset({date(2026, 5, 11)}))
        assert result == date(2026, 5, 8)

    def test_series_skips_overlaid_member(self) -> None:
        # Refuter fold R5: an overlay-unaware trading_days list containing an
        # overlaid day must NOT crash the series — the overlaid member is skipped
        # (it is simply not a session), mirroring the skip-None contract.
        daily = {
            date(2026, 5, 8): _candle(date(2026, 5, 8)),
            date(2026, 5, 11): _candle(date(2026, 5, 11)),
        }
        observed = frozenset({date(2026, 5, 12)})
        trading_days = [date(2026, 5, 11), date(2026, 5, 12), date(2026, 5, 13)]
        with capture_logs() as logs:
            result = lookup_prev_day_ohlc_series(trading_days, daily, observed=observed)
        assert date(2026, 5, 12) not in result  # overlaid member skipped, no raise
        assert result[date(2026, 5, 11)].prev_day == date(2026, 5, 8)
        # 2026-05-13's calendar prev (the overlaid 05-12) is stepped over -> the
        # 05-11 candle anchors it SILENTLY (overlay-reconciled path).
        assert result[date(2026, 5, 13)].prev_day == date(2026, 5, 11)
        assert _stale_warns(logs) == []

    def test_coverage_floor_gap_returns_none_not_raise(self) -> None:
        # Hostile-review fold (main-parity regression): a data gap near the 2020
        # coverage floor. today = Fri 2020-01-10 with an EMPTY candle dict — the
        # calendar prior session (Thu 2020-01-09) is established but candle-less,
        # and the candle hunt reaches the pre-2020 out-of-coverage span. The walk
        # must ABSTAIN (no-anchor WARN + None); the review proved the pre-fold
        # walk RAISED HolidayDataUnavailableError here where origin/main
        # returned None.
        with capture_logs() as logs:
            result = lookup_prev_day_ohlc(date(2020, 1, 10), {})
        assert result is None
        no_anchor = [e for e in logs if e["event"] == PREV_DAY_NO_ANCHOR_EVENT]
        assert len(no_anchor) == 1

    def test_coverage_floor_calendar_question_still_raises(self) -> None:
        # The strict raise is PRESERVED for the genuinely-CALENDAR case: today is
        # the first covered session (Wed 2020-01-01), so the walk reaches
        # 2019-12-31 BEFORE any calendar prior session exists — refusing to guess
        # a pre-coverage trading-day verdict stays LOUD
        # (DEC-CALENDAR-2020-HONEST-001; byte-identical to origin/main).
        with pytest.raises(HolidayDataUnavailableError):
            lookup_prev_day_ohlc(date(2020, 1, 1), {})

    def test_coverage_floor_candle_present_normal_silent_path(self) -> None:
        # Near the floor WITH data: the calendar prior session carries its candle
        # -> normal silent path; the floor guard never fires.
        daily = {date(2020, 1, 9): _candle(date(2020, 1, 9))}
        with capture_logs() as logs:
            result = lookup_prev_day_ohlc(date(2020, 1, 10), daily)
        assert result is not None
        assert result.prev_day == date(2020, 1, 9)
        assert logs == []

    def test_series_skips_day_with_no_anchor_at_all(self) -> None:
        # A `today` whose ENTIRE 14-day lookback has no candle stays absent from
        # the series output (the surviving skip-None contract).
        with capture_logs() as logs:
            result = lookup_prev_day_ohlc_series([date(2026, 5, 11)], {})
        assert result == {}
        assert [e for e in logs if e["event"] == PREV_DAY_NO_ANCHOR_EVENT]
