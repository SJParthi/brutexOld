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
    """Return True if ``d`` is in the NSE/BSE published holiday list (2021-2026)."""
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


def is_forced_exit_bar(t: time, tf_minutes: int) -> bool:
    """Return True if a bar opening at IST time ``t`` reaches the 15:20 forced exit.

    Per `docs/TRACK1_BACKTEST_SPEC.md` rule §8 the forced 15:20 IST exit
    overrides everything. A bar covers the half-open window
    ``[t, t + tf_minutes)``; it is a forced-exit bar when that window **reaches
    or passes** 15:20 IST — i.e. its close ``t + tf_minutes`` is strictly after
    15:20.

    For 1-minute bars this is identical to the old tf-blind ``t >= 15:20``
    test, but for coarser timeframes it additionally catches the bar whose
    *span* straddles 15:20 (e.g. a 15-minute bar opening at 15:15 IST runs to
    15:30 and must force-exit) — which the previous implementation silently
    missed because it ignored ``tf_minutes`` (DEC-TRADESIM-001; open question
    Q4 from `docs/VERIFICATION_2026-06-03_ENGINE_SCOPING.md` §5).

    Parameters
    ----------
    t
        The bar's OPEN time of day, in IST.
    tf_minutes
        The bar's timeframe in whole minutes (``>= 1``): 1 for the 1-minute
        base, 5/15/25/60/... for the resampled intraday timeframes.

    Returns
    -------
    bool
        True iff the bar's ``[open, open + tf)`` window reaches 15:20 IST.

    Raises
    ------
    ValueError
        If ``tf_minutes < 1``.
    """
    if tf_minutes < 1:
        raise ValueError(f"is_forced_exit_bar.tf_minutes must be >= 1, got {tf_minutes}")
    open_seconds = t.hour * _SECONDS_PER_HOUR + t.minute * _SECONDS_PER_MINUTE + t.second
    close_seconds = open_seconds + tf_minutes * _SECONDS_PER_MINUTE
    return close_seconds > _FORCED_EXIT_SECONDS


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
# half-day session exists in 2021-2026 outside Muhurat — see honest
# envelope below).
#
# Honest envelope (Golden Rule #10):
#   • NSE does NOT run pre-festival "half-day" sessions in modern era
#     (post-2010 trading-hours unification). Muhurat (Diwali ~1 hr
#     evening, since 2025 ~1 hr afternoon) is the only short session
#     and is handled by :data:`MUHURAT_SESSIONS` above.
#   • Special weekend trading sessions for Budget Day on Feb-1-Saturday
#     last occurred 2020-02-01 (out of Track 1 scope) + 2025-02-01.
#     The 2026-02-01 (Sunday) session is announced but is the future-
#     facing edge case; included with verified=True.
#
# Source: NSE special-trading-session circulars + Business Standard /
# Good Returns coverage of Budget-Day Saturday/Sunday sessions.

#: Special trading sessions on dates that would otherwise be non-trading
#: (Saturday / Sunday). Keyed by date; value is (open, close) IST tuple.
#: Sessions held during regular trading hours (09:15-15:30) unless the
#: circular announces otherwise.
SPECIAL_WEEKEND_TRADING_SESSIONS: Final[dict[date, tuple[time, time]]] = {
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
