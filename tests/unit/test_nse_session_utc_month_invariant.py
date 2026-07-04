"""The NSE-session ↔ UTC-month invariant pin (DEC-TF-REBUILD-MONTH-BOUND-001).

The month-bounded intraday-TF rebuild
(:func:`brutex.data.timeframe_materialize.month_partition_files` /
``materialize_lake_timeframes(months=…)``) is CORRECT only if a trading
session's bars can never land outside the UTC month of the session's own date
— i.e. every session's OPEN **and** CLOSE instants, converted IST → UTC, fall
on the SAME UTC calendar date as the session date. This suite pins that
invariant EXHAUSTIVELY over the real calendar's whole covered span — every
regular weekday session, every special-weekend Budget session
(:data:`SPECIAL_WEEKEND_TRADING_SESSIONS`) and every Diwali Muhurat evening
session (:data:`MUHURAT_SESSIONS`, which is a HOLIDAY-day session
``trading_days_between`` would skip, so it is iterated explicitly).

Why the invariant holds structurally: IST is UTC+05:30, so an instant's UTC
date differs from its IST date only between 00:00 and 05:30 IST — and no NSE
session (regular 09:15–15:30, Budget-weekend 09:15–15:30, Muhurat evening
~17:00–19:15) touches that band. The derived ``1day`` does NOT share the
invariant (midnight-IST stamping = 18:30 UTC of the PRIOR day), which is
exactly why ``months= + include_daily=True`` is refused.
"""

from __future__ import annotations

from datetime import date, datetime, time

from brutex.calendars.nse import (
    HOLIDAY_COVERAGE_FIRST_YEAR,
    HOLIDAY_COVERAGE_LAST_YEAR,
    IST,
    MUHURAT_SESSIONS,
    SPECIAL_WEEKEND_TRADING_SESSIONS,
    get_session,
    trading_days_between,
)


def _utc_date(session_date: date, instant: time) -> date:
    """The UTC calendar date of *instant* (IST) on *session_date*."""
    from datetime import UTC

    return datetime.combine(session_date, instant, tzinfo=IST).astimezone(UTC).date()


def test_every_covered_session_opens_and_closes_in_its_own_utc_month() -> None:
    """EXHAUSTIVE: every trading session over the covered span stays in-month.

    Covers every regular weekday session AND the three special-weekend Budget
    sessions (``trading_days_between`` includes them via
    ``is_special_weekend_session``) across the full holiday-coverage span.
    """
    start = date(HOLIDAY_COVERAGE_FIRST_YEAR, 1, 1)
    end = date(HOLIDAY_COVERAGE_LAST_YEAR, 12, 31)
    days = trading_days_between(start, end)
    assert len(days) > 1000  # a real multi-year sweep, not a degenerate range
    for d in days:
        session = get_session(d)
        assert session is not None, d
        assert _utc_date(d, session.open_time_ist) == d, d
        assert _utc_date(d, session.close_time_ist) == d, d


def test_special_weekend_sessions_are_in_the_sweep_and_in_month() -> None:
    """The Budget Saturday/Sunday sessions are explicitly asserted (not just swept)."""
    assert len(SPECIAL_WEEKEND_TRADING_SESSIONS) >= 3  # 2020/2025/2026 Budget days
    for d, (open_t, close_t) in SPECIAL_WEEKEND_TRADING_SESSIONS.items():
        assert _utc_date(d, open_t) == d
        assert _utc_date(d, close_t) == d


def test_muhurat_evening_sessions_stay_in_month() -> None:
    """Muhurat runs on a HOLIDAY (skipped by ``trading_days_between``) — pin it
    explicitly: even the latest evening close maps to the session's own UTC date."""
    assert MUHURAT_SESSIONS  # the calendar carries real Diwali sessions
    for d, (open_t, close_t) in MUHURAT_SESSIONS.items():
        session = get_session(d)  # Muhurat takes precedence over the holiday flag
        assert session is not None and session.open_time_ist == open_t
        assert _utc_date(d, open_t) == d
        assert _utc_date(d, close_t) == d
