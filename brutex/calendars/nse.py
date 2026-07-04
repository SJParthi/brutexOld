"""NSE/BSE trading calendar for Indian spot indices, 2020-2026.

Per `docs/TRACK1_BACKTEST_SPEC.md` Phase 1 Validation Gate 1:
- Full holiday list 2020 through 2026
- Muhurat sessions (Diwali ~1 hour; EXCLUDE from Sharpe denominator)
- Pre-open auction 09:00-09:15 (EXCLUDE; first usable bar 09:16-09:17)
- Forced exit time: 15:20 IST
- Tick size: paisa 5 (Rs 0.05) throughout 2020-2026

Source-of-truth: NSE/BSE published calendars + India PIB notifications.
Holidays verified against the published NSE FAOP circulars for each year.

Coverage honesty (DEC-CALENDAR-HARDEN-001): the hardcoded holiday list spans
:data:`HOLIDAY_COVERAGE_FIRST_YEAR`-:data:`HOLIDAY_COVERAGE_LAST_YEAR`
(inclusive). For a date outside that span :func:`is_holiday` returns ``False``
because the date is simply not in the (incomplete) set — that is an ABSENCE of
information, not a verified "trading day". Callers that need the distinction
(e.g. a pre-2020 date that predates the published list start) must gate on
:func:`is_holiday_data_available` or use the strict
:func:`is_holiday_checked` / :func:`is_trading_day_checked`, which raise
:class:`HolidayDataUnavailableError` rather than guess. The unguarded
:func:`is_holiday` / :func:`is_trading_day` keep their byte-identical
back-compat behaviour (an out-of-coverage date reads as not-a-holiday), so this
is a DETECT-and-opt-in surface, not a behaviour change.
Per Golden Rule #1 we do NOT hallucinate the missing-year holiday dates; we
surface the gap honestly.

The spot-anchor consumers ``indicators/prev_day_ohlc.py`` +
``indicators/weekly_anchors.py`` are WIRED to the strict
:func:`is_trading_day_checked` (DEC-CALENDAR-2020-HONEST-001), so an
out-of-coverage-anchored PDH/PDL / weekly-Fib lookup raises
:class:`HolidayDataUnavailableError` for an out-of-coverage weekday instead of
silently treating an unknown holiday as a trading day. The 2020 holiday list is
now ADDED (DEC-CALENDAR-2020-DATA-001) so 2020 — the data-pull floor year — is
fully covered and the loud-guard now applies to pre-2020 (2019 and earlier)
weekdays. Other unguarded :func:`is_trading_day` callers (e.g. the dashboard
pre-open report) remain a deferred opt-in follow-up.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import Enum
from typing import Final
from zoneinfo import ZoneInfo

import structlog

_LOG = structlog.get_logger(__name__)

#: IST is a FIXED +05:30 offset with NO daylight-saving transition (India has
#: observed no DST since 1945). ``ZoneInfo("Asia/Kolkata")`` is therefore a
#: constant-offset zone — every instant maps to UTC+05:30 — which is the
#: deliberate, correct choice over a hand-rolled ``timezone(timedelta(...))``
#: because the IANA database is the authoritative source and a future
#: (hypothetical) policy change would be picked up without a code edit. A
#: ``pytz`` / naive / machine-local construction would be a bug; this is not.
IST = ZoneInfo("Asia/Kolkata")
SESSION_OPEN_IST = time(9, 15)
SESSION_CLOSE_IST = time(15, 30)
FORCED_EXIT_IST = time(15, 20)
PRE_OPEN_END_IST = time(9, 15)
FIRST_USABLE_BAR_IST = time(9, 16)
TICK_SIZE_PAISA = 5
SESSION_DURATION_MINUTES = 375  # 09:15-15:30 = 6h15m = 375 minutes

#: Seconds-per-unit helpers for timeframe-aware time-of-day arithmetic
#: (:func:`is_forced_exit_bar`). Kept as named constants so the conversion
#: reads as a sentence rather than magic ``3600`` / ``60`` literals.
_SECONDS_PER_HOUR: Final[int] = 3600
_SECONDS_PER_MINUTE: Final[int] = 60

#: 15:20 IST as seconds-since-midnight — the forced-exit boundary, precomputed
#: once (loop-invariant) so :func:`is_forced_exit_bar` stays a few int ops.
_FORCED_EXIT_SECONDS: Final[int] = (
    FORCED_EXIT_IST.hour * _SECONDS_PER_HOUR + FORCED_EXIT_IST.minute * _SECONDS_PER_MINUTE
)

#: Track-1 forced-exit margin before the session close (10 minutes). For the
#: standard 15:30 close this yields the canonical 15:20 forced exit
#: (== :data:`FORCED_EXIT_IST`); for a short / half-day session it scales with
#: the actual close (docs/TRACK1_MICROSTRUCTURE.md §1.2 + §2). One COMMON rule.
_FORCED_EXIT_MARGIN: Final[timedelta] = timedelta(minutes=10)


class SessionType(Enum):
    """Trading session classification: REGULAR / MUHURAT / HALF_DAY / HOLIDAY."""

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
        """Session length in whole minutes (close - open in IST)."""
        delta = datetime.combine(self.date, self.close_time_ist) - datetime.combine(
            self.date, self.open_time_ist
        )
        return int(delta.total_seconds() // 60)


# NSE/BSE holiday list 2020-2026.
# Source: NSE official trading-holiday circulars + BSE annual calendars.
# Verified against pib.gov.in for gazette holidays.
#
# 2020 list added per DEC-CALENDAR-2020-DATA-001 to close the 2020 loud-guard
# gap (the data-pull floor is 2020-01-01 — ``GROWW_HISTORY_FLOOR``; CLAUDE.md §3
# — so a 2020 weekday previously hit ``HolidayDataUnavailableError`` and any
# sweep over 2020 refused to run). The 10 CONFIRMED dates are cross-checked
# across ≥2 public NSE/BSE holiday sources + weekday arithmetic; the 2 dates
# tagged ``# UNVERIFIED`` are standing annual NSE holidays observed every year
# with weekday-correct 2020 dates that could NOT be pinned to the raw 2020 NSE
# circular (it 403-blocked at verification time) — mirrors the 2026 UNVERIFIED
# precedent below; per Golden Rule #1 we tag the gap honestly rather than fake
# confirmation.
HOLIDAYS_2020 = frozenset(
    [
        date(2020, 2, 21),  # Fri  Mahashivratri
        date(2020, 3, 10),  # Tue  Holi
        date(2020, 4, 2),  # Thu  Ram Navami
        date(2020, 4, 6),  # Mon  Mahavir Jayanti
        date(2020, 4, 10),  # Fri  Good Friday
        date(2020, 4, 14),  # Tue  Dr. Baba Saheb Ambedkar Jayanti
        date(2020, 5, 1),  # Fri  Maharashtra Day
        date(2020, 5, 25),  # Mon  Id-Ul-Fitr (Ramzan Id)
        date(2020, 10, 2),  # Fri  Mahatma Gandhi Jayanti
        date(2020, 12, 25),  # Fri  Christmas
        date(2020, 11, 16),  # Mon  Diwali Balipratipada (UNVERIFIED — raw 2020 circular 403'd)
        date(2020, 11, 30),  # Mon  Guru Nanak Jayanti (UNVERIFIED — raw 2020 circular 403'd)
    ]
)

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

# HONESTY NOTE (DEC-HARDENING-LEFTOVERS-001, Golden Rule #1) — vendor-observed
# Jan-2026 session-count drift, UNVERIFIED: a live Groww pull served **20**
# distinct Jan-2026 trading sessions while this calendar derives **21**
# (22 Jan-2026 weekdays minus the sole listed January holiday, Republic Day
# 2026-01-26) — implying EXACTLY ONE additional Jan-2026 NSE holiday this list
# lacks. No source in this repo names that date, so it is deliberately NOT
# fabricated here (no behavioral change); the gap is closable ONLY by the
# official NSE 2026 trading-holiday circular or a live vendor probe of the
# Jan-2026 session dates. (A separately-reported claim that the 2026-02-01
# Sunday Budget session was "missing" is REFUTED: date(2026, 2, 1) is already a
# verified entry in SPECIAL_WEEKEND_TRADING_SESSIONS below, added by
# DEC-CALENDAR-2020-BUDGET-SATURDAY-001.)
# The SANCTIONED operational closure path for such drift is now the OBSERVED
# overlay (DEC-CALENDAR-OBSERVED-SESSIONS-001, ``calendars/observed_sessions.py``):
# a REMOVE-ONLY, evidence-gated, ``observed-not-official``-labeled sidecar that
# subtracts a demonstrably-untraded derived-trading-day from the OPT-IN consumer
# surfaces — it never adds a session and never names a holiday, so the
# named-holiday gap here stays closable ONLY by the official circular.
HOLIDAYS_2026 = frozenset(
    [
        date(2026, 1, 26),  # Republic Day
        # Mahashivratri 2026: the previously-listed 2026-02-15 is a SUNDAY —
        # ``date(2026, 2, 15).weekday() == 6`` — so it was an arithmetic NO-OP in
        # ``is_trading_day`` (a weekend is already non-trading; no special-weekend
        # entry exists for it) AND per-line UNVERIFIED. Removed
        # (DEC-CALENDAR-OBSERVED-SESSIONS-001): keeping an unverified
        # movable-festival entry that changes nothing conveys false confidence.
        # The actual NSE 2026 observance date is UNKNOWN pending the official
        # circular — deliberately NOT replaced with a guessed date (Golden Rule #1).
        date(2026, 3, 3),  # Holi (UNVERIFIED)
        date(2026, 3, 27),  # Ram Navami (UNVERIFIED)
        date(2026, 4, 3),  # Good Friday (UNVERIFIED)
        # The five fixed-DATE national observances below (Republic Day above,
        # Dr Ambedkar Jayanti, Maharashtra Day, Independence Day, Gandhi Jayanti):
        # the DATES are calendar-fixed, but their NSE 2026 trading-holiday
        # OBSERVANCE is still circular-UNVERIFIED like the rest of this set
        # (DEC-CALENDAR-OBSERVED-SESSIONS-001 annotation — previously implied,
        # now stated). 2026-08-15 is a Saturday (weekend no-op) — retained because
        # the date itself is fixed, unlike the movable 2026-02-15 case above.
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
    HOLIDAYS_2020
    | HOLIDAYS_2021
    | HOLIDAYS_2022
    | HOLIDAYS_2023
    | HOLIDAYS_2024
    | HOLIDAYS_2025
    | HOLIDAYS_2026
)

# Diwali Muhurat sessions (one trading hour, evening, separate from regular hours).
# Source: NSE special-session circulars per year.
MUHURAT_SESSIONS: dict[date, tuple[time, time]] = {
    date(2020, 11, 14): (time(18, 15), time(19, 15)),  # Sat — Diwali (DEC-CALENDAR-2020-DATA-001)
    date(2021, 11, 4): (time(18, 15), time(19, 15)),
    date(2022, 10, 24): (time(18, 15), time(19, 15)),
    date(2023, 11, 12): (time(18, 0), time(19, 0)),
    date(2024, 11, 1): (time(18, 0), time(19, 0)),
    date(2025, 10, 21): (time(13, 45), time(14, 45)),  # Verified shift to afternoon (SEBI 2025)
}


#: The INCLUSIVE calendar-year span the hardcoded NSE/BSE holiday list above
#: actually covers. Derived from the per-year frozensets so the two can never
#: silently drift: the first/last keys ARE the smallest/largest holiday year.
#:
#: Why this matters (DEC-CALENDAR-HARDEN-001): the Track-1 data pull floor is
#: 2020-01-01 (``GROWW_HISTORY_FLOOR``; CLAUDE.md §3, MIGRATION.md #722). The
#: holiday list now starts in 2020 (DEC-CALENDAR-2020-DATA-001) and ends in
#: 2026, so the entire pull range is covered; the loud-guard now bites only for
#: PRE-2020 (2019 and earlier) and post-2026 dates. For ANY date outside this
#: span :func:`is_holiday` returns ``False`` — which is the HONEST answer for
#: "is this date IN the published list" but, if a caller treats it as "is this a
#: trading day", an out-of-coverage weekday holiday reads as a trading day via
#: the UNGUARDED :func:`is_trading_day`. That unguarded behaviour is UNCHANGED
#: by this module (back-compat); the OPT-IN detection surface — the coverage
#: range + :func:`is_holiday_data_available` / :func:`is_holiday_checked` /
#: :func:`is_trading_day_checked` — lets a caller DETECT the out-of-coverage
#: case and refuse to guess, instead of trusting a silent ``False``. The
#: spot-anchor consumers (``indicators/prev_day_ohlc.py``,
#: ``indicators/weekly_anchors.py``) are WIRED to the strict variants
#: (DEC-CALENDAR-2020-HONEST-001); other unguarded callers
#: (``dashboard/preopen_report.py``) remain a deferred opt-in follow-up. Per
#: Golden Rule #1 we do NOT hallucinate holiday dates — the 2 standing-annual
#: 2020 November dates that could not be pinned to the raw circular are tagged
#: ``# UNVERIFIED`` rather than presented as confirmed.
HOLIDAY_COVERAGE_FIRST_YEAR: Final[int] = min(h.year for h in ALL_HOLIDAYS)
HOLIDAY_COVERAGE_LAST_YEAR: Final[int] = max(h.year for h in ALL_HOLIDAYS)


class HolidayDataUnavailableError(LookupError):
    """Raised when a holiday lookup is requested for an out-of-coverage date.

    The hardcoded NSE/BSE holiday list covers
    ``[HOLIDAY_COVERAGE_FIRST_YEAR, HOLIDAY_COVERAGE_LAST_YEAR]`` inclusive
    (currently 2020-2026). A date outside that span has NO published-holiday
    information, so :func:`is_holiday` would silently return ``False`` — which
    a strict caller can refuse via :func:`is_holiday_checked` to avoid treating
    an unknown date as a guaranteed non-holiday (Golden Rule #1).
    """


def is_holiday_data_available(d: date) -> bool:
    """Return True iff ``d`` falls within the covered holiday-list year span.

    O(1) — a single integer-range test against
    ``[HOLIDAY_COVERAGE_FIRST_YEAR, HOLIDAY_COVERAGE_LAST_YEAR]``. When this is
    ``False`` the result of :func:`is_holiday` is an absence of information, not
    a verified "not a holiday".
    """
    return HOLIDAY_COVERAGE_FIRST_YEAR <= d.year <= HOLIDAY_COVERAGE_LAST_YEAR


def is_holiday(d: date) -> bool:
    """Return True if ``d`` is in the NSE/BSE published holiday list.

    The list covers ``[HOLIDAY_COVERAGE_FIRST_YEAR, HOLIDAY_COVERAGE_LAST_YEAR]``
    inclusive (currently 2020-2026). For a date OUTSIDE that span this returns
    ``False`` because the date is not in the (incomplete) set — that ``False``
    means "not a known holiday", NOT "verified trading day". A caller that
    needs the distinction must gate on :func:`is_holiday_data_available` or use
    the strict :func:`is_holiday_checked`. O(1) ``frozenset`` membership.
    """
    return d in ALL_HOLIDAYS


def is_holiday_checked(d: date) -> bool:
    """Strict :func:`is_holiday` — raise on an out-of-coverage date.

    Identical to :func:`is_holiday` for dates within the covered span, but
    raises :class:`HolidayDataUnavailableError` (after emitting a structlog
    warning) when ``d`` lies outside ``[HOLIDAY_COVERAGE_FIRST_YEAR,
    HOLIDAY_COVERAGE_LAST_YEAR]``. A caller that uses THIS variant never
    silently mistakes an unknown date for a confirmed non-holiday; callers that
    keep using the unguarded :func:`is_holiday` retain its back-compat ``False``.
    O(1).

    Raises
    ------
    HolidayDataUnavailableError
        If ``d.year`` is outside the covered holiday-list span.
    """
    if not is_holiday_data_available(d):
        _LOG.warning(
            "calendar.holiday_data_unavailable",
            requested_date=d.isoformat(),
            covered_first_year=HOLIDAY_COVERAGE_FIRST_YEAR,
            covered_last_year=HOLIDAY_COVERAGE_LAST_YEAR,
        )
        raise HolidayDataUnavailableError(
            f"no published NSE/BSE holiday data for {d.isoformat()}: covered span is "
            f"{HOLIDAY_COVERAGE_FIRST_YEAR}-{HOLIDAY_COVERAGE_LAST_YEAR} (inclusive)"
        )
    return d in ALL_HOLIDAYS


#: ``date.weekday()`` returns 5 for Saturday and 6 for Sunday.
_SATURDAY_WEEKDAY: Final[int] = 5


def is_weekend(d: date) -> bool:
    """Return True if ``d`` is Saturday or Sunday."""
    return d.weekday() >= _SATURDAY_WEEKDAY


def is_trading_day(d: date) -> bool:
    """Return True if ``d`` is a trading day.

    A trading day is either a regular weekday (Mon-Fri) that is not a
    listed holiday, OR a special weekend session (e.g. Budget Day on a
    Saturday / Sunday — see :data:`SPECIAL_WEEKEND_TRADING_SESSIONS`).
    """
    if is_special_weekend_session(d):
        return True
    return not is_weekend(d) and not is_holiday(d)


def is_trading_day_checked(d: date) -> bool:
    """Strict :func:`is_trading_day` — raise on an out-of-coverage date.

    Identical to :func:`is_trading_day` within the covered holiday span. For a
    date whose year is OUTSIDE ``[HOLIDAY_COVERAGE_FIRST_YEAR,
    HOLIDAY_COVERAGE_LAST_YEAR]`` it raises :class:`HolidayDataUnavailableError`
    **only when the verdict would actually depend on the missing holiday
    list** — i.e. a weekday. Two cases resolve WITHOUT raising because their
    answer is data-complete regardless of the holiday list:

    * a special-weekend session (data the package explicitly holds) → ``True``;
    * a plain weekend with no special session → ``False`` (a weekend is never a
      trading day; the holiday list cannot change that).

    A weekday outside the span is the genuinely-ambiguous case: it would be a
    trading day UNLESS it is an (unknown) holiday, so the strict variant
    refuses rather than silently assume a trading day. O(1).

    Raises
    ------
    HolidayDataUnavailableError
        If ``d`` is an out-of-coverage WEEKDAY that is not a special session.
    """
    if is_special_weekend_session(d):
        return True
    if is_weekend(d):
        # A weekend is non-trading independent of the holiday list — no
        # missing data is consulted, so this answer is honest even out of span.
        return False
    # A weekday: the verdict turns on the holiday list. ``is_holiday_checked``
    # raises for an out-of-coverage date rather than guess "not a holiday".
    return not is_holiday_checked(d)


def has_muhurat_session(d: date) -> bool:
    """Return True if ``d`` has a Diwali muhurat session (~1 hour, excluded from Sharpe denominator)."""
    return d in MUHURAT_SESSIONS


def get_session(d: date) -> TradingSession | None:
    """Return the trading session for the given date, or None if no trading.

    Resolution order:

    1. Muhurat session (Diwali ~1 hr) takes precedence over the holiday
       flag on the same calendar day.
    2. Special weekend session (e.g. Budget Day on Saturday / Sunday) —
       full 09:15-15:30 hours, classified as ``REGULAR``.
    3. Regular weekday session if :func:`is_trading_day` is True.
    4. ``None`` otherwise (weekend / holiday).
    """
    if has_muhurat_session(d):
        open_t, close_t = MUHURAT_SESSIONS[d]
        return TradingSession(
            date=d,
            session_type=SessionType.MUHURAT,
            open_time_ist=open_t,
            close_time_ist=close_t,
        )
    if is_special_weekend_session(d):
        open_t, close_t = SPECIAL_WEEKEND_TRADING_SESSIONS[d]
        return TradingSession(
            date=d,
            session_type=SessionType.REGULAR,
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


def get_session_checked(d: date) -> TradingSession | None:
    """Strict :func:`get_session` — raise on an out-of-coverage date.

    Identical to :func:`get_session` within the covered holiday span. For a date
    whose year is OUTSIDE ``[HOLIDAY_COVERAGE_FIRST_YEAR,
    HOLIDAY_COVERAGE_LAST_YEAR]`` it raises :class:`HolidayDataUnavailableError`
    **only when the verdict would actually depend on the missing holiday
    list** — i.e. a plain weekday — instead of silently FABRICATING a regular
    09:15-15:30 session for an unknown weekday holiday. Two cases resolve
    WITHOUT raising because their answer is data-complete regardless of the
    holiday list, mirroring :func:`is_trading_day_checked`:

    * a muhurat session or a special-weekend session (data the package
      explicitly holds) → that session;
    * a plain weekend with no special/muhurat session → ``None`` (a weekend is
      never a trading day; the holiday list cannot change that).

    A weekday outside the span is the genuinely-ambiguous case: it would carry a
    regular session UNLESS it is an (unknown) holiday, so the strict variant
    refuses (via :func:`is_trading_day_checked`) rather than guess a REGULAR
    session. The unguarded :func:`get_session` keeps its byte-identical
    back-compat behaviour (an out-of-coverage weekday reads as a REGULAR
    session). O(1).

    Raises
    ------
    HolidayDataUnavailableError
        If ``d`` is an out-of-coverage WEEKDAY that is neither a muhurat nor a
        special-weekend session.
    """
    if has_muhurat_session(d):
        open_t, close_t = MUHURAT_SESSIONS[d]
        return TradingSession(
            date=d,
            session_type=SessionType.MUHURAT,
            open_time_ist=open_t,
            close_time_ist=close_t,
        )
    if is_special_weekend_session(d):
        open_t, close_t = SPECIAL_WEEKEND_TRADING_SESSIONS[d]
        return TradingSession(
            date=d,
            session_type=SessionType.REGULAR,
            open_time_ist=open_t,
            close_time_ist=close_t,
        )
    if not is_trading_day_checked(d):
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


def trading_days_between_checked(start: date, end: date) -> list[date]:
    """Strict :func:`trading_days_between` — raise on an out-of-coverage weekday.

    Identical to :func:`trading_days_between` when every date in ``[start,
    end]`` falls within the covered holiday span. If ANY date in the range is an
    out-of-coverage WEEKDAY that is not a special session, the verdict for that
    day turns on the missing holiday list, so this variant raises
    :class:`HolidayDataUnavailableError` (via :func:`is_trading_day_checked`)
    rather than silently INCLUDE an unknown weekday holiday as a trading day.
    Out-of-coverage weekends resolve without raising (a weekend is never a
    trading day, holiday list or not). The unguarded
    :func:`trading_days_between` keeps its byte-identical back-compat behaviour.
    O(days).

    Raises
    ------
    HolidayDataUnavailableError
        If any date in ``[start, end]`` is an out-of-coverage WEEKDAY that is
        not a special session.
    """
    days: list[date] = []
    d = start
    while d <= end:
        if is_trading_day_checked(d):
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

    Raises
    ------
    ValueError
        If ``tf_minutes < 1``. Mirrors :func:`is_forced_exit_bar`; a zero or
        negative timeframe is meaningless and previously raised
        :class:`ZeroDivisionError` (tf == 0) or returned a negative bar count
        (tf < 0).
    """
    if tf_minutes < 1:
        raise ValueError(f"expected_bars_per_day_for_tf.tf_minutes must be >= 1, got {tf_minutes}")
    if tf_minutes >= SESSION_DURATION_MINUTES:
        return 1
    return SESSION_DURATION_MINUTES // tf_minutes


def is_forced_exit_bar(t: time, tf_minutes: int, forced_exit_seconds: int | None = None) -> bool:
    """Return True if a bar opening at IST time ``t`` reaches the forced exit.

    Per `docs/TRACK1_BACKTEST_SPEC.md` rule §8 the forced IST exit overrides
    everything. A bar covers the half-open window ``[t, t + tf_minutes)``; it is
    a forced-exit bar when that window **reaches or passes** the forced-exit
    instant — i.e. its close ``t + tf_minutes`` is strictly after it.

    For 1-minute bars this is identical to the old tf-blind ``t >= boundary``
    test, but for coarser timeframes it additionally catches the bar whose
    *span* straddles the boundary (e.g. a 15-minute bar opening at 15:15 IST
    runs to 15:30 and must force-exit) — which the previous implementation
    silently missed because it ignored ``tf_minutes`` (DEC-TRADESIM-001; open
    question Q4 from `docs/VERIFICATION_2026-06-03_ENGINE_SCOPING.md` §5).

    The boundary defaults to the standard 15:20 IST (regular 09:15-15:30
    session). A SESSION-AWARE caller passes ``forced_exit_seconds`` — the
    session's own close − 10min (e.g. an evening Diwali Muhurat or an early
    half-day close) via :func:`forced_exit_seconds_for_session` — so a
    non-regular session force-exits at its real close − 10min rather than the
    wrong, hardcoded 15:20 (DEC-SESSION-FORCED-EXIT-001).

    Parameters
    ----------
    t
        The bar's OPEN time of day, in IST.
    tf_minutes
        The bar's timeframe in whole minutes (``>= 1``): 1 for the 1-minute
        base, 5/15/25/60/... for the resampled intraday timeframes.
    forced_exit_seconds
        The forced-exit boundary as seconds-since-midnight IST, or ``None`` to
        use the standard 15:20 IST boundary (:data:`_FORCED_EXIT_SECONDS`).

    Returns
    -------
    bool
        True iff the bar's ``[open, open + tf)`` window reaches the boundary.

    Raises
    ------
    ValueError
        If ``tf_minutes < 1``.
    """
    if tf_minutes < 1:
        raise ValueError(f"is_forced_exit_bar.tf_minutes must be >= 1, got {tf_minutes}")
    boundary = _FORCED_EXIT_SECONDS if forced_exit_seconds is None else forced_exit_seconds
    open_seconds = t.hour * _SECONDS_PER_HOUR + t.minute * _SECONDS_PER_MINUTE + t.second
    close_seconds = open_seconds + tf_minutes * _SECONDS_PER_MINUTE
    return close_seconds > boundary


def forced_exit_time_for_session(session: TradingSession) -> time:
    """Return the IST forced-exit time-of-day for ``session``.

    Track 1 force-closes all open positions :data:`_FORCED_EXIT_MARGIN`
    (10 minutes) before the session's close (`docs/TRACK1_MICROSTRUCTURE.md`
    §1.2 + §2). For the standard 09:15-15:30 session this is exactly
    15:20 IST (== :data:`FORCED_EXIT_IST`); for a short / half-day session
    (e.g. a Diwali Muhurat hour) the exit scales with the actual close, so a
    single COMMON rule replaces a hardcoded 15:20.

    Assumes the session close is at or after 00:10 IST (always true for every
    NSE/BSE session — the earliest in-scope close is the 14:45 Muhurat); below
    that the 10-minute subtraction would roll back into the previous day.
    """
    close_dt = datetime.combine(session.date, session.close_time_ist)
    return (close_dt - _FORCED_EXIT_MARGIN).time()


def forced_exit_seconds_for_session(session: TradingSession) -> int:
    """Return ``session``'s forced-exit time-of-day as seconds-since-midnight IST.

    The integer twin of :func:`forced_exit_time_for_session`, in the same
    seconds-since-midnight unit :func:`is_forced_exit_bar` compares against, so
    a session-aware caller resolves the boundary once and feeds it straight to
    the tf-aware forced-exit test without any time→seconds re-derivation at the
    call site (one COMMON conversion; DEC-SESSION-FORCED-EXIT-001).

    For the standard 09:15-15:30 session this equals :data:`_FORCED_EXIT_SECONDS`
    (15:20 IST); for a short / half-day / Muhurat session it scales with the
    actual close − 10min.
    """
    forced = forced_exit_time_for_session(session)
    return forced.hour * _SECONDS_PER_HOUR + forced.minute * _SECONDS_PER_MINUTE + forced.second


def is_pre_open(t: time) -> bool:
    """Pre-open auction 09:00-09:15 — bars in this window are EXCLUDED."""
    return time(9, 0) <= t < SESSION_OPEN_IST


# ────────────────────────────────────────────────────────────────────────
# NIFTY 50 semi-annual rebalance events (DEC-P0-046, Q-09 lock)
# ────────────────────────────────────────────────────────────────────────
#
# Methodology (verified 2026-05-18 against niftyindices.com + NSE FAQ):
#   • Semi-annual review with cut-off dates 31 January + 31 July each year.
#   • Effective date = first working day after F&O expiry of March / Sept
#     (historically last Thursday; post-SEBI Aug-2024 reform may shift).
#   • Four weeks' prior public notice via NSE Indices press release.
#
# Sources cited per event are NSE press archives + financial news outlets
# that quote the NSE Indices circular for that specific event. The
# `announcement_date` is the NSE Indices press-release date; the
# `effective_date` is the first session on which the new constituent list
# is reflected in the index.
#
# Why this matters for Track 1 backtests:
#   • A backtest spanning a rebalance must re-evaluate index-level
#     statistics (PDH/PDL, ATR, etc.) on the FROZEN constituent set for
#     each side of the boundary OR explicitly stamp the rebalance event
#     in the audit log so post-hoc analysis can attribute regime breaks.
#   • The backtest engine consumes this list via `is_nifty_rebalance_day`
#     and stamps a flag on bars dated on / after each effective date.
#
# Honest envelope (Golden Rule #1 + #10):
#   • 9 events VERIFIED (March 31, 2021 → September 30, 2025) via
#     news-outlet cross-cite with NSE press circulars.
#   • 1 event UNVERIFIED (September 30, 2021) — pattern-derived from the
#     semi-annual cadence but no direct cite found. Marked explicitly.
#   • Post-2025 events not yet published by NSE as of 2026-05-18.


@dataclass(frozen=True)
class NiftyRebalanceEvent:
    """One semi-annual NIFTY 50 constituent reconstitution.

    ``effective_date`` is the first session the new constituent list
    applies to. ``announcement_date`` is the NSE Indices press-release
    date (typically ~4 weeks earlier). ``added`` / ``removed`` are
    tuples of NSE ticker symbols.

    Note
    ----
    The ticker tuples are recorded for audit-trail completeness; the
    backtest engine only needs the date. A subset of stocks recorded
    here is treated as authoritative — see :data:`NIFTY50_REBALANCE_EVENTS`
    docstring for the per-event verification level.
    """

    effective_date: date
    announcement_date: date | None
    added: tuple[str, ...]
    removed: tuple[str, ...]
    verified: bool


#: NIFTY 50 semi-annual rebalance events 2021 → 2025 (Q-09 lock).
#:
#: Source pattern: "first working day after F&O expiry of March / September"
#: per NSE Indices methodology document; effective dates cross-cited with
#: Business Standard / Business Today / ICICIdirect coverage of each event.
#:
#: ``verified=True`` means the effective date has at least one independent
#: financial-news source quoting the NSE Indices circular. ``verified=False``
#: marks pattern-derived dates that need to be cross-checked when the
#: NSE press archive becomes accessible (Phase 1 Validation Gate 1).
NIFTY50_REBALANCE_EVENTS: Final[tuple[NiftyRebalanceEvent, ...]] = (
    NiftyRebalanceEvent(
        effective_date=date(2021, 3, 31),
        announcement_date=None,
        added=("TATACONSUM",),
        removed=("GAIL",),
        verified=True,
    ),
    NiftyRebalanceEvent(
        # UNVERIFIED — pattern-derived from semi-annual cadence. NSE's
        # F&O expiry was Thu 2021-09-30; first working day after = Fri
        # 2021-10-01 (no holiday). The published methodology supports
        # either effective date depending on which side of expiry the
        # press release falls; needs niftyindices.com circular cross-check.
        effective_date=date(2021, 9, 30),
        announcement_date=None,
        added=(),
        removed=(),
        verified=False,
    ),
    NiftyRebalanceEvent(
        effective_date=date(2022, 3, 31),
        announcement_date=date(2022, 2, 25),
        added=("APOLLOHOSP",),
        removed=("IOC",),
        verified=True,
    ),
    NiftyRebalanceEvent(
        effective_date=date(2022, 9, 30),
        announcement_date=date(2022, 9, 1),
        added=("ADANIENT",),
        removed=("SHREECEM",),
        verified=True,
    ),
    NiftyRebalanceEvent(
        # Note: per ICICIdirect + enrichmoney coverage, "ACC + Ambuja +
        # BoB out / Titan + ADANIPORTS + IOC in" was the headline ticker
        # set spanning Nifty Next 50 + Nifty 50 indices. Track 1 only
        # records the effective date; constituent-level attribution
        # deferred to Phase 1 Validation Gate 1 cross-check.
        effective_date=date(2023, 3, 31),
        announcement_date=None,
        added=(),
        removed=(),
        verified=True,
    ),
    NiftyRebalanceEvent(
        # Semi-annual review cadence date; constituent attribution
        # deferred (see March 2023 note above).
        effective_date=date(2023, 9, 29),
        announcement_date=None,
        added=(),
        removed=(),
        verified=True,
    ),
    NiftyRebalanceEvent(
        effective_date=date(2024, 3, 28),
        announcement_date=date(2024, 2, 28),
        added=("SHRIRAMFIN",),
        removed=("UPL",),
        verified=True,
    ),
    NiftyRebalanceEvent(
        effective_date=date(2024, 9, 30),
        announcement_date=None,
        added=("TRENT", "BEL"),
        removed=("DIVISLAB", "LTIM"),
        verified=True,
    ),
    NiftyRebalanceEvent(
        effective_date=date(2025, 3, 28),
        announcement_date=None,
        added=("ETERNAL", "JIOFIN"),  # Zomato → ETERNAL post-rename
        removed=("BPCL", "BRITANNIA"),
        verified=True,
    ),
    NiftyRebalanceEvent(
        effective_date=date(2025, 9, 30),
        announcement_date=None,
        added=("INDIGO", "MAXHEALTH"),
        removed=("HEROMOTOCO", "INDUSINDBK"),
        verified=True,
    ),
)


#: O(1) ``date`` -> event map for :func:`is_nifty_rebalance_day` /
#: :func:`nifty_rebalance_event_on`. Built once from the authoritative
#: :data:`NIFTY50_REBALANCE_EVENTS` tuple (the single source of truth);
#: effective dates are unique (asserted by tests) so there is no key
#: collision. Replaces the previous O(n) linear scan with a true O(1)
#: dict lookup (Golden Rule #5: O(1) discipline).
_REBALANCE_BY_DATE: Final[dict[date, NiftyRebalanceEvent]] = {
    event.effective_date: event for event in NIFTY50_REBALANCE_EVENTS
}


def is_nifty_rebalance_day(d: date) -> bool:
    """Return True if ``d`` is a NIFTY 50 semi-annual rebalance effective date.

    Used by the backtest engine to stamp a `rebalance_flag` on bars on
    or after each effective date. The audit log records the event so
    post-hoc regime-break analysis is possible. O(1) dict lookup.
    """
    return d in _REBALANCE_BY_DATE


def nifty_rebalance_event_on(d: date) -> NiftyRebalanceEvent | None:
    """Return the :class:`NiftyRebalanceEvent` for ``d`` if any, else None.

    O(1) dict lookup against :data:`_REBALANCE_BY_DATE`.
    """
    return _REBALANCE_BY_DATE.get(d)


# ────────────────────────────────────────────────────────────────────────
# Special weekend trading sessions (DEC-P0-046, Q-09 lock)
# ────────────────────────────────────────────────────────────────────────
#
# NSE occasionally runs a FULL trading session on a weekend day for
# operational reasons. The classic case is Union Budget Day when Feb 1
# falls on a Saturday or Sunday — NSE + BSE remain open for normal hours
# so the live tape can absorb the budget announcement.
#
# These are NOT "half-day" sessions (the regular 09:15-15:30 hours apply);
# they are SPECIAL full-day sessions on otherwise-weekend dates. The
# `SessionType.HALF_DAY` enum is kept for forward-compat (no qualifying
# half-day session exists in 2020-2026 outside Muhurat — see honest
# envelope below).
#
# Honest envelope (Golden Rule #10):
#   • NSE does NOT run pre-festival "half-day" sessions in modern era
#     (post-2010 trading-hours unification). Muhurat (Diwali ~1 hr
#     evening, since 2025 ~1 hr afternoon) is the only short session
#     and is handled by :data:`MUHURAT_SESSIONS` above.
#   • Special weekend trading sessions for Budget Day on Feb-1-Saturday
#     occurred 2020-02-01 + 2025-02-01. All three in-scope Budget weekend
#     sessions are modelled below. The 2020-02-01 Budget Saturday session
#     (India's FY2020-21 Union Budget, presented Sat 1 Feb 2020 with a live
#     NSE + BSE full-day session) is INSIDE the 2020-01-01 data-pull floor
#     (GROWW_HISTORY_FLOOR) and is now added by
#     DEC-CALENDAR-2020-BUDGET-SATURDAY-001 — it was previously omitted by
#     DEC-CALENDAR-2020-DATA-001 (which scoped in only the 2020 full-day
#     holiday list + the 2020 Diwali Muhurat session), so an in-scope full
#     trading session was being scored as non-trading, corrupting PDH/PDL
#     anchors + per-session stats on/around that day. The 2026-02-01
#     (Sunday) session is announced but is the future-facing edge case;
#     included with verified=True.
#   • Feb-1 fell on a weekend only in 2020 (Sat), 2025 (Sat) and 2026 (Sun)
#     across the 2020→today floor; 2021-2024 Feb-1 were weekdays. So these
#     three entries are the COMPLETE set of Budget weekend sessions in scope.
#
# Source: NSE special-trading-session circulars + Business Standard /
# Good Returns coverage of Budget-Day Saturday/Sunday sessions
# (DEC-CALENDAR-2020-BUDGET-SATURDAY-001).

#: Special trading sessions on dates that would otherwise be non-trading
#: (Saturday / Sunday). Keyed by date; value is (open, close) IST tuple.
#: Sessions held during regular trading hours (09:15-15:30) unless the
#: circular announces otherwise.
SPECIAL_WEEKEND_TRADING_SESSIONS: Final[dict[date, tuple[time, time]]] = {
    # Union Budget 2020-21 — Saturday session (NSE + BSE special live
    # session; India's FY2020-21 Budget presented Sat 2020-02-01). Inside
    # the 2020-01-01 data-pull floor; DEC-CALENDAR-2020-BUDGET-SATURDAY-001.
    date(2020, 2, 1): (SESSION_OPEN_IST, SESSION_CLOSE_IST),
    # Union Budget 2025-26 — Saturday session (NSE circular Jan 2025).
    date(2025, 2, 1): (SESSION_OPEN_IST, SESSION_CLOSE_IST),
    # Union Budget 2026-27 — Sunday session (announced; verified by
    # Business Standard + Good Returns coverage 2026-01-30).
    date(2026, 2, 1): (SESSION_OPEN_IST, SESSION_CLOSE_IST),
}


def is_special_weekend_session(d: date) -> bool:
    """Return True if ``d`` has a special trading session on a weekend.

    Used by the backtest engine to flip the "is_trading_day" decision
    for these specific weekend dates so that Budget Day bars are
    ingested + included in the data lake. Without this hook, the
    weekend filter in :func:`is_trading_day` would silently drop those
    bars.
    """
    return d in SPECIAL_WEEKEND_TRADING_SESSIONS
