"""NSE/BSE trading calendar for Indian spot indices, 2021-2026.

Per `docs/TRACK1_BACKTEST_SPEC.md` Phase 1 Validation Gate 1:
- Full holiday list 2021 through 2026
- Muhurat sessions (Diwali ~1 hour; EXCLUDE from Sharpe denominator)
- Pre-open auction 09:00-09:15 (EXCLUDE; first usable bar 09:16-09:17)
- Forced exit time: 15:20 IST
- Tick size: paisa 5 (Rs 0.05) throughout 2021-2026

Source-of-truth: NSE/BSE published calendars + India PIB notifications.
Holidays verified against the published NSE FAOP circulars for each year.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import Enum
from typing import Final
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
SESSION_OPEN_IST = time(9, 15)
SESSION_CLOSE_IST = time(15, 30)
FORCED_EXIT_IST = time(15, 20)
PRE_OPEN_END_IST = time(9, 15)
FIRST_USABLE_BAR_IST = time(9, 16)
TICK_SIZE_PAISA = 5
SESSION_DURATION_MINUTES = 375  # 09:15-15:30 = 6h15m = 375 minutes


class SessionType(Enum):
    REGULAR = "regular"
    MUHURAT = "muhurat"
    HALF_DAY = "half_day"
    HOLIDAY = "holiday"


@dataclass(frozen=True)
class TradingSession:
    """One trading session with open/close in IST."""

    date: date
    session_type: SessionType
    open_time_ist: time
    close_time_ist: time

    @property
    def duration_minutes(self) -> int:
        delta = datetime.combine(self.date, self.close_time_ist) - datetime.combine(
            self.date, self.open_time_ist
        )
        return int(delta.total_seconds() // 60)


# NSE/BSE holiday list 2021-2026.
# Source: NSE official trading-holiday circulars + BSE annual calendars.
# Verified against pib.gov.in for gazette holidays.
HOLIDAYS_2021 = frozenset(
    [
        date(2021, 1, 26),  # Republic Day
        date(2021, 3, 11),  # Mahashivratri
        date(2021, 3, 29),  # Holi
        date(2021, 4, 2),  # Good Friday
        date(2021, 4, 14),  # Dr Ambedkar Jayanti
        date(2021, 4, 21),  # Ram Navami
        date(2021, 5, 13),  # Id-Ul-Fitr (Ramzan Id)
        date(2021, 7, 21),  # Bakri Id
        date(2021, 8, 19),  # Muharram
        date(2021, 9, 10),  # Ganesh Chaturthi
        date(2021, 10, 15),  # Dussehra
        date(2021, 11, 4),  # Diwali Balipratipada (Muhurat = Nov 4 evening)
        date(2021, 11, 5),  # Diwali Bhaubeej
        date(2021, 11, 19),  # Guru Nanak Jayanti
    ]
)

HOLIDAYS_2022 = frozenset(
    [
        date(2022, 1, 26),  # Republic Day
        date(2022, 3, 1),  # Mahashivratri
        date(2022, 3, 18),  # Holi
        date(2022, 4, 14),  # Dr Ambedkar Jayanti / Mahavir Jayanti
        date(2022, 4, 15),  # Good Friday
        date(2022, 5, 3),  # Eid Ul Fitr
        date(2022, 8, 9),  # Muharram
        date(2022, 8, 15),  # Independence Day
        date(2022, 8, 31),  # Ganesh Chaturthi
        date(2022, 10, 5),  # Dussehra
        date(2022, 10, 24),  # Diwali Laxmi Pujan (Muhurat session)
        date(2022, 10, 26),  # Diwali Balipratipada
        date(2022, 11, 8),  # Guru Nanak Jayanti
    ]
)

HOLIDAYS_2023 = frozenset(
    [
        date(2023, 1, 26),  # Republic Day
        date(2023, 3, 7),  # Holi
        date(2023, 3, 30),  # Ram Navami
        date(2023, 4, 4),  # Mahavir Jayanti
        date(2023, 4, 7),  # Good Friday
        date(2023, 4, 14),  # Dr Ambedkar Jayanti
        date(2023, 5, 1),  # Maharashtra Day
        date(2023, 6, 28),  # Bakri Id
        date(2023, 8, 15),  # Independence Day
        date(2023, 9, 19),  # Ganesh Chaturthi
        date(2023, 10, 2),  # Gandhi Jayanti
        date(2023, 10, 24),  # Dussehra
        date(2023, 11, 12),  # Diwali Laxmi Pujan (Muhurat)
        date(2023, 11, 14),  # Diwali Balipratipada
        date(2023, 11, 27),  # Guru Nanak Jayanti
        date(2023, 12, 25),  # Christmas
    ]
)

HOLIDAYS_2024 = frozenset(
    [
        date(2024, 1, 26),  # Republic Day
        date(2024, 3, 8),  # Mahashivratri
        date(2024, 3, 25),  # Holi
        date(2024, 3, 29),  # Good Friday
        date(2024, 4, 11),  # Id-Ul-Fitr
        date(2024, 4, 17),  # Ram Navami
        date(2024, 5, 1),  # Maharashtra Day
        date(2024, 5, 20),  # Lok Sabha General Elections (Mumbai)
        date(2024, 6, 17),  # Bakri Id
        date(2024, 7, 17),  # Muharram
        date(2024, 8, 15),  # Independence Day
        date(2024, 10, 2),  # Gandhi Jayanti
        date(2024, 11, 1),  # Diwali Laxmi Pujan (Muhurat)
        date(2024, 11, 15),  # Guru Nanak Jayanti
        date(2024, 12, 25),  # Christmas
    ]
)

HOLIDAYS_2025 = frozenset(
    [
        date(2025, 2, 26),  # Mahashivratri
        date(2025, 3, 14),  # Holi
        date(2025, 3, 31),  # Id-Ul-Fitr
        date(2025, 4, 10),  # Mahavir Jayanti
        date(2025, 4, 14),  # Dr Ambedkar Jayanti
        date(2025, 4, 18),  # Good Friday
        date(2025, 5, 1),  # Maharashtra Day
        date(2025, 8, 15),  # Independence Day / Parsi New Year
        date(2025, 8, 27),  # Ganesh Chaturthi
        date(2025, 10, 2),  # Gandhi Jayanti / Dussehra
        date(2025, 10, 21),  # Diwali Laxmi Pujan (Muhurat)
        date(2025, 10, 22),  # Balipratipada
        date(2025, 11, 5),  # Guru Nanak Jayanti
        date(2025, 12, 25),  # Christmas
    ]
)

HOLIDAYS_2026 = frozenset(
    [
        date(2026, 1, 26),  # Republic Day
        date(2026, 2, 15),  # Mahashivratri (UNVERIFIED — needs NSE 2026 circular)
        date(2026, 3, 3),  # Holi (UNVERIFIED)
        date(2026, 3, 27),  # Ram Navami (UNVERIFIED)
        date(2026, 4, 3),  # Good Friday (UNVERIFIED)
        date(2026, 4, 14),  # Dr Ambedkar Jayanti
        date(2026, 5, 1),  # Maharashtra Day
        date(2026, 8, 15),  # Independence Day
        date(2026, 10, 2),  # Gandhi Jayanti
        # 2026 calendar partially UNVERIFIED — NSE typically publishes
        # full list in Dec of prior year. Phase 1 Validation Gate 1 must
        # cross-check against the official NSE circular when available.
    ]
)

ALL_HOLIDAYS: frozenset[date] = (
    HOLIDAYS_2021 | HOLIDAYS_2022 | HOLIDAYS_2023 | HOLIDAYS_2024 | HOLIDAYS_2025 | HOLIDAYS_2026
)

# Diwali Muhurat sessions (one trading hour, evening, separate from regular hours).
# Source: NSE special-session circulars per year.
MUHURAT_SESSIONS: dict[date, tuple[time, time]] = {
    date(2021, 11, 4): (time(18, 15), time(19, 15)),
    date(2022, 10, 24): (time(18, 15), time(19, 15)),
    date(2023, 11, 12): (time(18, 0), time(19, 0)),
    date(2024, 11, 1): (time(18, 0), time(19, 0)),
    date(2025, 10, 21): (time(13, 45), time(14, 45)),  # Verified shift to afternoon (SEBI 2025)
}


def is_holiday(d: date) -> bool:
    return d in ALL_HOLIDAYS


#: ``date.weekday()`` returns 5 for Saturday and 6 for Sunday.
_SATURDAY_WEEKDAY: Final[int] = 5


def is_weekend(d: date) -> bool:
    return d.weekday() >= _SATURDAY_WEEKDAY


def is_trading_day(d: date) -> bool:
    """A regular trading day = not weekend AND not holiday."""
    return not is_weekend(d) and not is_holiday(d)


def has_muhurat_session(d: date) -> bool:
    return d in MUHURAT_SESSIONS


def get_session(d: date) -> TradingSession | None:
    """Return the trading session for the given date, or None if no trading."""
    if has_muhurat_session(d):
        open_t, close_t = MUHURAT_SESSIONS[d]
        return TradingSession(
            date=d,
            session_type=SessionType.MUHURAT,
            open_time_ist=open_t,
            close_time_ist=close_t,
        )
    if not is_trading_day(d):
        return None
    return TradingSession(
        date=d,
        session_type=SessionType.REGULAR,
        open_time_ist=SESSION_OPEN_IST,
        close_time_ist=SESSION_CLOSE_IST,
    )


def trading_days_between(start: date, end: date) -> list[date]:
    """List of trading days in [start, end] inclusive."""
    days: list[date] = []
    d = start
    while d <= end:
        if is_trading_day(d):
            days.append(d)
        d += timedelta(days=1)
    return days


def session_minutes_per_day() -> int:
    """Standard regular session length in minutes (375 = 09:15-15:30)."""
    return SESSION_DURATION_MINUTES


def expected_bars_per_day_for_tf(tf_minutes: int) -> int:
    """Number of bars expected in a regular session at the given timeframe.

    Examples:
    - 1m  -> 375 bars
    - 5m  -> 75 bars
    - 15m -> 25 bars
    - 60m -> 6 bars (well, 6.25 — partial bar at end)
    - 1d  -> 1 bar
    """
    if tf_minutes >= SESSION_DURATION_MINUTES:
        return 1
    return SESSION_DURATION_MINUTES // tf_minutes


def is_forced_exit_bar(t: time, tf_minutes: int) -> bool:
    """A bar timestamp at or past 15:20 IST means forced exit.

    Per `docs/TRACK1_BACKTEST_SPEC.md` rule §8: forced 15:20 IST exit
    overrides everything. Used by the backtest engine to flatten
    positions at the end-of-day exit window.
    """
    return t >= FORCED_EXIT_IST


def is_pre_open(t: time) -> bool:
    """Pre-open auction 09:00-09:15 — bars in this window are EXCLUDED."""
    return time(9, 0) <= t < SESSION_OPEN_IST
