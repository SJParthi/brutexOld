"""NSE/BSE trading calendar for Indian spot indices, 2020-2026.

Per `docs/TRACK1_BACKTEST_SPEC.md` Phase 1 Validation Gate 1:
- Full holiday list 2020 through 2026
- Muhurat sessions (Diwali ~1 hour; EXCLUDE from Sharpe denominator)
- Pre-open auction 09:00-09:15 (EXCLUDE; first usable bar 09:16-09:17)
- Forced exit time: 15:20 IST
- Tick size: paisa 5 (Rs 0.05) throughout 2020-2026

Source-of-truth: NSE/BSE published calendars + India PIB notifications.
Holidays verified against the published NSE FAOP circulars for each year.

Per-exchange split (DEC-CALENDAR-EXCHANGE-SPLIT-001): the per-year
``HOLIDAYS_20XX`` frozensets are the COMMON core — every source consulted
found the NSE and BSE equity-segment holiday calendars IDENTICAL for all
covered years (2020-2026; secondary-corroborated — the primary exchange
circular pages were unreachable from this environment, HTTP 403).
:data:`NSE_HOLIDAYS` / :data:`BSE_HOLIDAYS` layer (currently EMPTY)
per-exchange divergence sets on top, and every lookup function accepts a
keyword-only ``exchange`` parameter (``None`` → the exchange-agnostic
:data:`ALL_HOLIDAYS` union, byte-identical to the pre-split behaviour).
Muhurat sessions, special weekend sessions, and rebalance events stay SHARED
(both exchanges run the same special sessions).

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
weekdays. The remaining unguarded production consumers were made LOUD by the
calendar-correctness pack: the dashboard pre-open report + the doctor probe-day
walk catch-and-degrade honestly (the pre-open report now rides
:func:`previous_trading_session`'s ONE
``calendar.previous_session_coverage_exhausted`` warning —
DEC-PREOPEN-PREVDAY-COMMON-002; the doctor walk keeps its own
``calendar.coverage_exhausted`` twin),
the engine trade-sim walkers + the bar-metadata stamper refuse an
out-of-coverage weekday via the checked variants, and the synthetic generator
(unguarded by design — it fabricates data) warns once per out-of-coverage call.
"""

from __future__ import annotations

from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from typing import Final
from zoneinfo import ZoneInfo

import structlog

_LOG = structlog.get_logger(__name__)

#: The IANA time-zone KEY for India Standard Time — the ONE canonical string
#: root (DEC-TIMESTAMP-IST-SINGLESOURCE-001) every ``"Asia/Kolkata"`` string
#: consumer derives from (polars ``convert_time_zone`` region strings, DuckDB
#: ``AT TIME ZONE`` SQL literals, the string-family ``_IST`` module mirrors).
IST_TZ_NAME: Final[str] = "Asia/Kolkata"

#: IST is a FIXED +05:30 offset with NO daylight-saving transition (India has
#: observed no DST since 1945). ``ZoneInfo("Asia/Kolkata")`` is therefore a
#: constant-offset zone — every instant maps to UTC+05:30 — which is the
#: deliberate, correct choice over a hand-rolled ``timezone(timedelta(...))``
#: because the IANA database is the authoritative source and a future
#: (hypothetical) policy change would be picked up without a code edit. A
#: ``pytz`` / naive / machine-local construction would be a bug; this is not.
#: The ONE canonical ZoneInfo-family root (DEC-TIMESTAMP-IST-SINGLESOURCE-001).
IST = ZoneInfo(IST_TZ_NAME)

#: The fixed UTC→IST wall-clock offset (+05:30) as a :class:`~datetime.timedelta`
#: — the ONE canonical OFFSET root (DEC-TIMESTAMP-IST-SINGLESOURCE-001). India
#: has observed no DST since 1945, so the offset is exact for every covered
#: instant; every derived representation (:data:`IST_OFFSET_MINUTES`,
#: :data:`IST_FIXED_OFFSET`, the polars-duration helper
#: ``brutex.data.session.ist_offset_duration``, the SQL / Rust / JS mirrors)
#: traces here — never independently re-minted. The ZoneInfo↔offset equality
#: law (``IST.utcoffset(...) == IST_UTC_OFFSET``) is test-pinned.
IST_UTC_OFFSET: Final[timedelta] = timedelta(hours=5, minutes=30)

#: The same fixed offset in whole minutes (330) — DERIVED from
#: :data:`IST_UTC_OFFSET`, never a second literal
#: (DEC-TIMESTAMP-IST-SINGLESOURCE-001). Consumed by the DuckDB
#: ``INTERVAL '{n} minutes'`` interpolation + the cross-language drift pins.
IST_OFFSET_MINUTES: Final[int] = int(IST_UTC_OFFSET.total_seconds()) // 60

#: The fixed-offset REPRESENTATION of IST — ``timezone(IST_UTC_OFFSET)`` — the
#: ONE source for the ``timezone(timedelta(hours=5, minutes=30))`` family
#: (DEC-TIMESTAMP-IST-SINGLESOURCE-001). Deliberately a SEPARATE constant from
#: :data:`IST`: the two families are NOT interchangeable (``tzname()`` /
#: ``repr`` / the DST API differ — ``'UTC+05:30'`` vs ``'Asia/Kolkata'``), so
#: a consumer must never swap one for the other; each family unifies onto its
#: own root, and the same-``utcoffset`` equality of the two is test-pinned.
IST_FIXED_OFFSET: Final[timezone] = timezone(IST_UTC_OFFSET)

#: The regular NSE/BSE session OPEN (09:15 IST) — the ONE canonical root
#: (DEC-TIMESTAMP-IST-SINGLESOURCE-001); ``brutex.data.session.SESSION_OPEN``
#: re-exports it and every plain 09:15 constant mirror derives from it.
SESSION_OPEN_IST = time(9, 15)

#: The regular NSE/BSE session CLOSE (15:30 IST, exclusive) — the ONE canonical
#: root (DEC-TIMESTAMP-IST-SINGLESOURCE-001); ``brutex.data.session.SESSION_CLOSE``
#: re-exports it. NOTE: the F&O expiry-INSTANT convention
#: (``greeks_enrich.EXPIRY_HOUR_IST``/``EXPIRY_MINUTE_IST``) merely EQUALS this
#: wall clock — it is a distinct contract-expiry law, deliberately NOT unified.
SESSION_CLOSE_IST = time(15, 30)
FORCED_EXIT_IST = time(15, 20)

#: The pre-open auction ends AT the session open — DERIVED from
#: :data:`SESSION_OPEN_IST` (the same 09:15 instant by exchange rule), never a
#: second ``time(9, 15)`` literal (DEC-TIMESTAMP-IST-SINGLESOURCE-001).
PRE_OPEN_END_IST = SESSION_OPEN_IST
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

# 2026 list COMPLETED per DEC-CALENDAR-2026-COMPLETE-001 (two independent
# research rounds, 2026-07-03). Source honesty (Golden Rule #1): the official
# NSE 2026 trading-holiday circular is REPORTED as **CMTR71775 dated
# 2025-12-12** by the secondary sources both research rounds consulted (the
# circular NUMBER is itself a secondary claim — the circular was never read);
# its PDF (and every other NSE/BSE primary holiday page probed — 19 fetch
# attempts) returned HTTP 403 from this environment, so NO date below is
# primary-verified.
# Every date IS corroborated by BOTH research rounds AND >= 2 independent
# secondary sources (named inline per date); a date the two rounds disagreed on
# would have been EXCLUDED and tagged UNVERIFIED — none was (the rounds agreed
# in full). This FIXES the prior Ram Navami entry 2026-03-27 (Friday — a normal
# trading day): both rounds independently confirm Ram Navami is Thu 2026-03-26.
# Buddha Purnima is NOT a 2026 trading holiday (single-source noise refuted:
# it falls Fri 2026-05-01, coinciding with the Maharashtra Day closure; the
# "May 12" claim traces to 2025 articles). Weekend-falling published dates are
# kept in the set per the existing convention (earlier years list them too);
# each is tagged "(Sat/Sun — already non-trading)".
#
# HONESTY NOTE (DEC-HARDENING-LEFTOVERS-001, Golden Rule #1) — vendor-observed
# Jan-2026 session-count drift, STILL UNVERIFIED: a live Groww pull served
# **20** distinct Jan-2026 trading sessions while this calendar derives **21**
# (22 Jan-2026 weekdays minus the sole listed January holiday, Republic Day
# 2026-01-26). The now-COMPLETED full-year 2026 list contains NO second January
# holiday (both research rounds confirm: per the secondary reports of the NSE
# circular dated 2025-12-12,
# Republic Day is the ONLY January 2026 trading holiday — Makar Sankranti /
# Pongal are explicitly absent), so the 20-vs-21 drift is now MORE likely a
# vendor data gap than a missing holiday. No date is fabricated to reconcile
# it; the drift is closable ONLY by the official circular PDF or a live vendor
# probe of the Jan-2026 session dates. (A separately-reported claim that the
# 2026-02-01 Sunday Budget session was "missing" is REFUTED: date(2026, 2, 1)
# is already a verified entry in SPECIAL_WEEKEND_TRADING_SESSIONS below, added
# by DEC-CALENDAR-2020-BUDGET-SATURDAY-001.)
# The SANCTIONED operational closure path for such drift is the OBSERVED
# overlay (DEC-CALENDAR-OBSERVED-SESSIONS-001, ``calendars/observed_sessions.py``):
# a REMOVE-ONLY, evidence-gated, ``observed-not-official``-labeled sidecar that
# subtracts a demonstrably-untraded derived-trading-day from the OPT-IN consumer
# surfaces — it never adds a session and never names a holiday, so the
# named-holiday gap here stays closable ONLY by the official circular.
HOLIDAYS_2026 = frozenset(
    [
        date(2026, 1, 26),  # Mon  Republic Day (Zerodha holiday calendar; Kotak Neo)
        date(2026, 2, 15),  # Sun  Mahashivratri (Bajaj AMC; Groww) (Sun — already non-trading)
        date(2026, 3, 3),  # Tue  Holi (Business Standard 2026-03-03 "shut today"; Samco —
        # the festival was celebrated Mar 4 but the EXCHANGE holiday is Mar 3 per the circular)
        date(2026, 3, 21),  # Sat  Id-ul-Fitr (Bajaj AMC; smallcase) (Sat — already non-trading)
        date(2026, 3, 26),  # Thu  Ram Navami (Angel One; Zerodha; Business Standard — FIXES
        # the prior wrong 2026-03-27 entry: Mar 27 is a Friday and a normal trading day)
        date(2026, 3, 31),  # Tue  Mahavir Jayanti (Angel One; Business Standard)
        date(2026, 4, 3),  # Fri  Good Friday (Zerodha; ClearTax)
        date(2026, 4, 14),  # Tue  Dr Ambedkar Jayanti (Business Today 2026-04-14; Kotak Neo)
        date(2026, 5, 1),  # Fri  Maharashtra Day (Groww; Sahi)
        date(2026, 5, 28),  # Thu  Bakri Id / Id-ul-Adha (Groww; ClearTax)
        date(2026, 6, 26),  # Fri  Muharram (IndiaIPO; Groww)
        date(2026, 8, 15),  # Sat  Independence Day (Groww; Swastika) (Sat — already non-trading)
        date(2026, 9, 14),  # Mon  Ganesh Chaturthi (Groww; Kotak Neo)
        date(2026, 10, 2),  # Fri  Mahatma Gandhi Jayanti (ClearTax; Zerodha)
        date(2026, 10, 20),  # Tue  Dussehra / Vijaya Dashami (Kotak Neo; ClearTax)
        date(2026, 11, 8),  # Sun  Diwali Laxmi Pujan (Sahi; Kotak Neo) (Sun — already
        # non-trading; the special Muhurat session is announced for this day — see
        # MUHURAT_SESSIONS below: timings NOT yet notified, so no entry is fabricated)
        date(2026, 11, 10),  # Tue  Diwali Balipratipada (Sahi; Zerodha)
        date(2026, 11, 24),  # Tue  Guru Nanak Jayanti (Sahi; Zerodha)
        date(2026, 12, 25),  # Fri  Christmas (Zerodha; Groww)
    ]
)

#: The COMMON per-year holiday core shared by BOTH exchanges
#: (DEC-CALENDAR-EXCHANGE-SPLIT-001). Every source consulted found the NSE and
#: BSE EQUITY-segment holiday calendars IDENTICAL for every covered year
#: (2020-2026) — secondary-corroborated only: the BSE primary holiday page
#: (bseindia.com listholi.aspx, both URL variants) returned HTTP 403 from this
#: environment, so the identity is NOT primary-verified against a BSE notice.
_COMMON_HOLIDAYS: Final[frozenset[date]] = (
    HOLIDAYS_2020
    | HOLIDAYS_2021
    | HOLIDAYS_2022
    | HOLIDAYS_2023
    | HOLIDAYS_2024
    | HOLIDAYS_2025
    | HOLIDAYS_2026
)

#: Holidays observed by NSE but NOT BSE. EMPTY by verified fact, not by
#: omission (Golden Rule #1): NO citable NSE-vs-BSE divergent equity-segment
#: holiday exists for 2020-2026 in any source either research round found. Any
#: FUTURE divergent date added here REQUIRES a citable exchange circular in its
#: inline comment; an uncitable divergence is noted UNVERIFIED in comments and
#: NOT encoded.
NSE_ONLY_HOLIDAYS: Final[frozenset[date]] = frozenset()

#: Holidays observed by BSE but NOT NSE. EMPTY by verified fact — same
#: citable-circular requirement as :data:`NSE_ONLY_HOLIDAYS` (Golden Rule #1).
BSE_ONLY_HOLIDAYS: Final[frozenset[date]] = frozenset()

#: The NSE equity-segment holiday set (common core + NSE-only divergences).
NSE_HOLIDAYS: Final[frozenset[date]] = _COMMON_HOLIDAYS | NSE_ONLY_HOLIDAYS

#: The BSE equity-segment holiday set (common core + BSE-only divergences).
BSE_HOLIDAYS: Final[frozenset[date]] = _COMMON_HOLIDAYS | BSE_ONLY_HOLIDAYS

#: The union of both exchanges' holiday sets — the exchange-agnostic default
#: every existing caller keeps (byte-identical to the pre-split ALL_HOLIDAYS:
#: with both `_ONLY` sets empty, ``NSE_HOLIDAYS == BSE_HOLIDAYS ==
#: ALL_HOLIDAYS == _COMMON_HOLIDAYS``).
ALL_HOLIDAYS: frozenset[date] = NSE_HOLIDAYS | BSE_HOLIDAYS

# Diwali Muhurat sessions (one trading hour, evening, separate from regular hours).
# Source: NSE special-session circulars per year. SHARED by both exchanges
# (NSE + BSE conduct the same Muhurat session; DEC-CALENDAR-EXCHANGE-SPLIT-001).
#
# 2026 is deliberately ABSENT (DEC-CALENDAR-2026-COMPLETE-001, Golden Rule #1):
# the 2026 Muhurat session is ANNOUNCED for Sunday 2026-11-08 (Diwali Laxmi
# Pujan, listed in HOLIDAYS_2026 above), but as of 2026-07-03 its TIMINGS are
# NOT yet notified — multiple sources quote the circular language "timings of
# Muhurat Trading shall be notified subsequently through a circular" (typically
# issued in October). ``has_muhurat_session(date(2026, 11, 8))`` therefore
# honestly reads False until the timing circular lands; fabricating an hour
# here would be a guess, not data.
MUHURAT_SESSIONS: dict[date, tuple[time, time]] = {
    date(2020, 11, 14): (time(18, 15), time(19, 15)),  # Sat — Diwali (DEC-CALENDAR-2020-DATA-001)
    date(2021, 11, 4): (time(18, 15), time(19, 15)),
    date(2022, 10, 24): (time(18, 15), time(19, 15)),
    date(2023, 11, 12): (time(18, 0), time(19, 0)),
    date(2024, 11, 1): (time(18, 0), time(19, 0)),
    date(2025, 10, 21): (time(13, 45), time(14, 45)),  # Verified shift to afternoon (SEBI 2025)
}


# DEC-EXTENDED-SESSION-OUTAGE-001 — NSE exchange outage 2021-02-24 (verified 2026-07-11):
# NSE feeds froze 10:08 IST; F&O halted 11:40, cash 11:43 (telecom-link failure); pre-open
# call auction 15:30-15:45; continuous trading 15:45-17:00; close EXTENDED to 17:00 IST at
# NSE, BSE and MSEI (SEBI press releases: sebi.gov.in/media/press-releases/mar-2021/
# trading-halt-at-nse-on-february-24-2021_49603.html + the feb-2021 sibling _49287.html).
# One (open, close) pair models the BSE day exactly and the NSE envelope; the NSE midday
# hole (~11:40 → 15:45, no bars) is census-annotated DATA
# (:mod:`brutex.data.session_density`), deliberately NOT calendar-modeled.
EXTENDED_SESSIONS: Final[dict[date, tuple[time, time]]] = {
    date(2021, 2, 24): (SESSION_OPEN_IST, time(17, 0)),
}

# DEC-HALT-DAY-TREATMENT-001 — market-wide circuit-breaker halt days (verified 2026-07-11):
# 2020-03-13 (10% lower-band halt 09:20 IST, 45-min halt + 15-min pre-open, continuous
# trading resumed 10:20, normal 15:30 close — business-standard.com/article/markets/
# nifty-hits-lower-circuit-amid-global-sell-off-trading-halted-for-45-mins-120031300188_1.html)
# and 2020-03-23 (10% lower-band halt 09:58 IST, resumed after the standard 45+15 sequence
# — ~10:58, DERIVED not press-printed — normal 15:30 close; business-standard.com/article/
# markets/sensex-hits-10-lower-circuit-trading-halted-for-45-minutes-120032300237_1.html).
# ANNOTATION ONLY: both stay full REGULAR 09:15-15:30 sessions with real bars that are
# never dropped; consumed by the session-density census (the known-irregular INFO arm) +
# the >9% daily-range detector (:mod:`brutex.data.spot_daily_resample`).
CIRCUIT_HALT_SESSIONS: Final[frozenset[date]] = frozenset({date(2020, 3, 13), date(2020, 3, 23)})

#: Machine reason token for a :data:`CIRCUIT_HALT_SESSIONS` date
#: (:func:`known_irregular_reason`).
CIRCUIT_HALT_REASON: Final[str] = "market_wide_circuit_halt"
#: Machine reason token for an :data:`EXTENDED_SESSIONS` date
#: (:func:`known_irregular_reason`).
EXTENDED_SESSION_REASON: Final[str] = "exchange_outage_extended_close"


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
#: (DEC-CALENDAR-2020-HONEST-001); the once-deferred unguarded callers
#: (``dashboard/preopen_report.py``, ``doctor_live.py``, the engine trade-sim
#: walkers, ``feed/bar_metadata.py``) are now loud too (the
#: calendar-correctness pack). Per
#: Golden Rule #1 we do NOT hallucinate holiday dates — the 2 standing-annual
#: 2020 November dates that could not be pinned to the raw circular are tagged
#: ``# UNVERIFIED`` rather than presented as confirmed.
#:
#: MAINTENANCE — the post-2026 hard horizon (hostile-review operational note,
#: calendar-correctness pack): the strict consumers (the engine trade-sim
#: walkers ``engine/trade_sim.py`` / ``engine/rust_trade_sim.py``,
#: ``feed/bar_metadata.py``, ``dashboard/preopen_report.py``) now RAISE
#: :class:`HolidayDataUnavailableError` for any weekday bar dated outside this
#: span — loud-by-design (a sweep surfaces it as ``TUPLE_FAILED`` per
#: DEC-ENGINE-TUPLE-SKIP-RESILIENT-001), never a silently-fabricated
#: 09:15/15:20 session. Consequence: ``HOLIDAYS_2027`` MUST be added (from the
#: official NSE 2027 circular, once published) BEFORE any 2027-dated bar is
#: pulled or swept, or every 2027 tuple fails loudly.
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


def holidays_for_exchange(exchange: str) -> frozenset[date]:
    """Return the holiday set for one exchange (DEC-CALENDAR-EXCHANGE-SPLIT-001).

    Exact-match dispatch: ``"NSE"`` → :data:`NSE_HOLIDAYS`, ``"BSE"`` →
    :data:`BSE_HOLIDAYS`. The two sets are IDENTICAL for every covered year
    (2020-2026) per every secondary source consulted (the primary exchange
    circulars were unreachable from this environment — HTTP 403), so today this
    is a divergence MECHANISM, not a divergence: the first citable divergent
    date lands in :data:`NSE_ONLY_HOLIDAYS` / :data:`BSE_ONLY_HOLIDAYS` and
    every ``exchange=``-threaded caller picks it up with no further change.
    O(1).

    Parameters
    ----------
    exchange
        ``"NSE"`` or ``"BSE"`` (exact match — no case folding).

    Raises
    ------
    ValueError
        If *exchange* is anything other than ``"NSE"`` or ``"BSE"``.
    """
    if exchange == "NSE":
        return NSE_HOLIDAYS
    if exchange == "BSE":
        return BSE_HOLIDAYS
    raise ValueError(f"unknown exchange {exchange!r}: valid values are 'NSE' and 'BSE'")


def _resolve_holiday_set(exchange: str | None) -> frozenset[date]:
    """Map an optional exchange to its holiday set — the ONE resolution helper.

    ``None`` (the default every pre-split caller implicitly passes) resolves to
    the exchange-agnostic :data:`ALL_HOLIDAYS` union — byte-identical to the
    pre-split behaviour; ``"NSE"`` / ``"BSE"`` resolve via
    :func:`holidays_for_exchange` (anything else raises :class:`ValueError`
    there). O(1).
    """
    if exchange is None:
        return ALL_HOLIDAYS
    return holidays_for_exchange(exchange)


def is_holiday(d: date, *, exchange: str | None = None) -> bool:
    """Return True if ``d`` is in the published holiday list.

    The list covers ``[HOLIDAY_COVERAGE_FIRST_YEAR, HOLIDAY_COVERAGE_LAST_YEAR]``
    inclusive (currently 2020-2026). For a date OUTSIDE that span this returns
    ``False`` because the date is not in the (incomplete) set — that ``False``
    means "not a known holiday", NOT "verified trading day". A caller that
    needs the distinction must gate on :func:`is_holiday_data_available` or use
    the strict :func:`is_holiday_checked`. O(1) ``frozenset`` membership.

    Parameters
    ----------
    d
        The calendar date to test.
    exchange
        ``None`` (default) tests the exchange-agnostic :data:`ALL_HOLIDAYS`
        union — byte-identical to the pre-split behaviour; ``"NSE"`` / ``"BSE"``
        test that exchange's own set (DEC-CALENDAR-EXCHANGE-SPLIT-001; the sets
        are identical for 2020-2026 per secondary sources — primary circulars
        unreachable from this environment).

    Raises
    ------
    ValueError
        If *exchange* is neither ``None`` nor ``"NSE"`` / ``"BSE"``.
    """
    return d in _resolve_holiday_set(exchange)


def is_holiday_checked(d: date, *, exchange: str | None = None) -> bool:
    """Strict :func:`is_holiday` — raise on an out-of-coverage date.

    Identical to :func:`is_holiday` for dates within the covered span, but
    raises :class:`HolidayDataUnavailableError` (after emitting a structlog
    warning) when ``d`` lies outside ``[HOLIDAY_COVERAGE_FIRST_YEAR,
    HOLIDAY_COVERAGE_LAST_YEAR]``. A caller that uses THIS variant never
    silently mistakes an unknown date for a confirmed non-holiday; callers that
    keep using the unguarded :func:`is_holiday` retain its back-compat ``False``.
    The coverage span is shared by both exchanges (one common per-year core).
    O(1).

    Parameters
    ----------
    d
        The calendar date to test.
    exchange
        ``None`` (default) / ``"NSE"`` / ``"BSE"`` — see :func:`is_holiday`.

    Raises
    ------
    HolidayDataUnavailableError
        If ``d.year`` is outside the covered holiday-list span.
    ValueError
        If *exchange* is neither ``None`` nor ``"NSE"`` / ``"BSE"`` (raised
        before the coverage check — an invalid exchange never resolves).
    """
    holidays = _resolve_holiday_set(exchange)
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
    return d in holidays


#: ``date.weekday()`` returns 5 for Saturday and 6 for Sunday.
_SATURDAY_WEEKDAY: Final[int] = 5


def is_weekend(d: date) -> bool:
    """Return True if ``d`` is Saturday or Sunday."""
    return d.weekday() >= _SATURDAY_WEEKDAY


def is_trading_day(d: date, *, exchange: str | None = None) -> bool:
    """Return True if ``d`` is a trading day.

    A trading day is either a regular weekday (Mon-Fri) that is not a
    listed holiday, OR a special weekend session (e.g. Budget Day on a
    Saturday / Sunday — see :data:`SPECIAL_WEEKEND_TRADING_SESSIONS`).
    Special weekend sessions are SHARED by both exchanges (NSE + BSE run the
    same Budget-Day sessions), so only the holiday set is exchange-scoped.

    Parameters
    ----------
    d
        The calendar date to test.
    exchange
        ``None`` (default) / ``"NSE"`` / ``"BSE"`` — selects the holiday set;
        see :func:`is_holiday` (DEC-CALENDAR-EXCHANGE-SPLIT-001).

    Raises
    ------
    ValueError
        If *exchange* is neither ``None`` nor ``"NSE"`` / ``"BSE"`` (validated
        eagerly, before any date-path short-circuit).
    """
    _resolve_holiday_set(exchange)  # eager validation: invalid exchange always raises
    if is_special_weekend_session(d):
        return True
    return not is_weekend(d) and not is_holiday(d, exchange=exchange)


def is_trading_day_checked(d: date, *, exchange: str | None = None) -> bool:
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

    Parameters
    ----------
    d
        The calendar date to test.
    exchange
        ``None`` (default) / ``"NSE"`` / ``"BSE"`` — selects the holiday set;
        see :func:`is_holiday` (DEC-CALENDAR-EXCHANGE-SPLIT-001). Special
        weekend sessions stay SHARED (both exchanges).

    Raises
    ------
    HolidayDataUnavailableError
        If ``d`` is an out-of-coverage WEEKDAY that is not a special session.
    ValueError
        If *exchange* is neither ``None`` nor ``"NSE"`` / ``"BSE"`` (validated
        eagerly, before any date-path short-circuit).
    """
    _resolve_holiday_set(exchange)  # eager validation: invalid exchange always raises
    if is_special_weekend_session(d):
        return True
    if is_weekend(d):
        # A weekend is non-trading independent of the holiday list — no
        # missing data is consulted, so this answer is honest even out of span.
        return False
    # A weekday: the verdict turns on the holiday list. ``is_holiday_checked``
    # raises for an out-of-coverage date rather than guess "not a holiday".
    return not is_holiday_checked(d, exchange=exchange)


def has_muhurat_session(d: date) -> bool:
    """Return True if ``d`` has a Diwali muhurat session (~1 hour, excluded from Sharpe denominator)."""
    return d in MUHURAT_SESSIONS


def has_extended_session(d: date) -> bool:
    """Return True iff ``d`` has an EXTENDED (post-15:30 close) trading session.

    DEC-EXTENDED-SESSION-OUTAGE-001 — membership in :data:`EXTENDED_SESSIONS`
    (2021-02-24, the NSE exchange-outage day whose close was extended to
    17:00 IST at NSE, BSE and MSEI per the SEBI press releases; verified
    2026-07-11). O(1).
    """
    return d in EXTENDED_SESSIONS


def is_circuit_halt_session(d: date) -> bool:
    """Return True iff ``d`` is an annotated market-wide circuit-breaker halt day.

    DEC-HALT-DAY-TREATMENT-001 — membership in :data:`CIRCUIT_HALT_SESSIONS`
    (2020-03-13 + 2020-03-23, verified 2026-07-11). ANNOTATION only: the day
    stays a full REGULAR 09:15-15:30 session; no bar is ever dropped. O(1).
    """
    return d in CIRCUIT_HALT_SESSIONS


def is_known_irregular_session(d: date) -> bool:
    """Return True iff ``d`` is a KNOWN-irregular session (halt day OR extended).

    The union predicate the session-density census
    (:mod:`brutex.data.session_density`) + the >9% daily-range detector
    (:mod:`brutex.data.spot_daily_resample`) key their INFO (expected-irregular)
    arms on — a deficit / extreme range on one of these annotated dates is a
    documented market fact, never an unexplained lake defect. O(1).
    """
    return is_circuit_halt_session(d) or has_extended_session(d)


def known_irregular_reason(d: date) -> str | None:
    """Return the machine reason token for a known-irregular session, else ``None``.

    * :data:`CIRCUIT_HALT_REASON` (``"market_wide_circuit_halt"``) for a
      :data:`CIRCUIT_HALT_SESSIONS` date (DEC-HALT-DAY-TREATMENT-001);
    * :data:`EXTENDED_SESSION_REASON` (``"exchange_outage_extended_close"``)
      for an :data:`EXTENDED_SESSIONS` date (DEC-EXTENDED-SESSION-OUTAGE-001);
    * ``None`` for every other date. O(1).
    """
    if is_circuit_halt_session(d):
        return CIRCUIT_HALT_REASON
    if has_extended_session(d):
        return EXTENDED_SESSION_REASON
    return None


def get_session(d: date, *, exchange: str | None = None) -> TradingSession | None:
    """Return the trading session for the given date, or None if no trading.

    Resolution order:

    1. Muhurat session (Diwali ~1 hr) takes precedence over the holiday
       flag on the same calendar day.
    2. Special weekend session (e.g. Budget Day on Saturday / Sunday) —
       full 09:15-15:30 hours, classified as ``REGULAR``.
    3. Extended session (:data:`EXTENDED_SESSIONS` — the 2021-02-24 NSE
       exchange-outage day, close extended to 17:00 IST at NSE, BSE and MSEI;
       DEC-EXTENDED-SESSION-OUTAGE-001) — classified as ``REGULAR`` (the
       special-weekend precedent; identity is :func:`has_extended_session`).
    4. Regular weekday session if :func:`is_trading_day` is True.
    5. ``None`` otherwise (weekend / holiday).

    Muhurat + special weekend + extended sessions are SHARED by both exchanges
    (NSE + BSE ran the same sessions — the 2021-02-24 17:00 extension applied
    to NSE, BSE and MSEI alike); only the holiday set is exchange-scoped.

    Parameters
    ----------
    d
        The calendar date to resolve.
    exchange
        ``None`` (default) / ``"NSE"`` / ``"BSE"`` — selects the holiday set;
        see :func:`is_holiday` (DEC-CALENDAR-EXCHANGE-SPLIT-001).

    Raises
    ------
    ValueError
        If *exchange* is neither ``None`` nor ``"NSE"`` / ``"BSE"`` (validated
        eagerly, before any date-path short-circuit).
    """
    _resolve_holiday_set(exchange)  # eager validation: invalid exchange always raises
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
    if d in EXTENDED_SESSIONS:
        open_t, close_t = EXTENDED_SESSIONS[d]
        return TradingSession(
            date=d,
            session_type=SessionType.REGULAR,
            open_time_ist=open_t,
            close_time_ist=close_t,
        )
    if not is_trading_day(d, exchange=exchange):
        return None
    return TradingSession(
        date=d,
        session_type=SessionType.REGULAR,
        open_time_ist=SESSION_OPEN_IST,
        close_time_ist=SESSION_CLOSE_IST,
    )


def get_session_checked(d: date, *, exchange: str | None = None) -> TradingSession | None:
    """Strict :func:`get_session` — raise on an out-of-coverage date.

    Identical to :func:`get_session` within the covered holiday span. For a date
    whose year is OUTSIDE ``[HOLIDAY_COVERAGE_FIRST_YEAR,
    HOLIDAY_COVERAGE_LAST_YEAR]`` it raises :class:`HolidayDataUnavailableError`
    **only when the verdict would actually depend on the missing holiday
    list** — i.e. a plain weekday — instead of silently FABRICATING a regular
    09:15-15:30 session for an unknown weekday holiday. Two cases resolve
    WITHOUT raising because their answer is data-complete regardless of the
    holiday list, mirroring :func:`is_trading_day_checked`:

    * a muhurat session, a special-weekend session or an extended session
      (:data:`EXTENDED_SESSIONS` — data the package explicitly holds;
      DEC-EXTENDED-SESSION-OUTAGE-001) → that session;
    * a plain weekend with no special/muhurat session → ``None`` (a weekend is
      never a trading day; the holiday list cannot change that).

    Resolution order mirrors :func:`get_session` exactly: muhurat → special
    weekend → extended → regular (strict) → ``None``. A weekday outside the
    span is the genuinely-ambiguous case: it would carry a
    regular session UNLESS it is an (unknown) holiday, so the strict variant
    refuses (via :func:`is_trading_day_checked`) rather than guess a REGULAR
    session. The unguarded :func:`get_session` keeps its byte-identical
    back-compat behaviour (an out-of-coverage weekday reads as a REGULAR
    session). Muhurat + special weekend + extended sessions are SHARED by both
    exchanges; only the holiday set is exchange-scoped. O(1).

    Parameters
    ----------
    d
        The calendar date to resolve.
    exchange
        ``None`` (default) / ``"NSE"`` / ``"BSE"`` — selects the holiday set;
        see :func:`is_holiday` (DEC-CALENDAR-EXCHANGE-SPLIT-001).

    Raises
    ------
    HolidayDataUnavailableError
        If ``d`` is an out-of-coverage WEEKDAY that is neither a muhurat nor a
        special-weekend session.
    ValueError
        If *exchange* is neither ``None`` nor ``"NSE"`` / ``"BSE"`` (validated
        eagerly, before any date-path short-circuit).
    """
    _resolve_holiday_set(exchange)  # eager validation: invalid exchange always raises
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
    if d in EXTENDED_SESSIONS:
        open_t, close_t = EXTENDED_SESSIONS[d]
        return TradingSession(
            date=d,
            session_type=SessionType.REGULAR,
            open_time_ist=open_t,
            close_time_ist=close_t,
        )
    if not is_trading_day_checked(d, exchange=exchange):
        return None
    return TradingSession(
        date=d,
        session_type=SessionType.REGULAR,
        open_time_ist=SESSION_OPEN_IST,
        close_time_ist=SESSION_CLOSE_IST,
    )


def daily_derive_cutoff(now_ist: datetime) -> date:
    """The session-close-aware forming-session cutoff for the derived ``1day``.

    DEC-PREBUILD-SESSION-CLOSE-CUTOFF-001 — the ONE COMMON resolver of the
    ``before`` date the daily derive drops sessions at (``ist_date >= before``
    in :func:`brutex.data.spot_daily_resample.resample_1m_to_daily`). It
    supersedes the whole-day-granular "``before`` = today IST" default (the
    DEC-DOCTOR-KIT-HONESTY-001 fix-4 clause "the code cannot cheaply prove the
    session closed" — it now CAN, from the calendar):

    * **today has NO session** (weekend / holiday) → ``today + 1 day``: there
      is no forming session to protect, so yesterday's (and any stored)
      completed sessions all derive.
    * **today's session is provably OVER** (``now_ist.time()`` strictly after
      :attr:`TradingSession.close_time_ist` — :func:`get_session` already
      resolves the per-date close for regular days,
      :data:`SPECIAL_WEEKEND_TRADING_SESSIONS` Budget weekends AND the
      :data:`MUHURAT_SESSIONS` evening hours) → ``today + 1 day``: a post-close
      prebuild writes the run day's own daily bar instead of deferring it to
      the next pipeline run.
    * **still forming / pre-close** (incl. exactly AT the close instant —
      strict ``>``, conservative) → ``today``: the no-look-ahead exclusion is
      unchanged. The at-close ``>`` → ``>=`` "boundary tightening" follow-up
      is CLOSED-AS-WONTFIX (DEC-SESSION-CLOSE-BOUNDARY-PIN-001): tightening
      would only shave a one-minute self-healing window while making the
      verdict clock-race-dependent (at exactly 15:30:00 the 15:29 bar may not
      yet be pulled/filtered/written — finalizing risks a partial daily bar),
      and the grounded arm below covers the real hazard. The pull-side
      :func:`brutex.adapters.ingest_common.clamp_window_end` DELIBERATELY
      holds the OPPOSITE at-close verdict (``>=`` — exactly 15:30:00 counts
      as closed): a request-side earliest-fetch of a finished session is safe
      (no bar at/after 15:30 can exist under the half-open ingest filter),
      while the finalize-side here must never write a partial bar. The pair
      is frozen by the cross-module drift-pin test
      (``tests/unit/test_session_close_boundary_pin.py``) — any future flip
      is a conscious, test-breaking act.

    HONEST envelope (Golden Rule #10): clock-based proof, not data-grounded —
    the data-grounded last-bar-vs-close variant is
    :func:`daily_derive_cutoff_grounded` (DEC-DAILY-CUTOFF-GROUNDED-001, the
    shipped follow-up; DEFER-only, it delegates here for the clock verdict).
    Uses the unguarded :func:`get_session`, which FABRICATES a regular session
    for an out-of-coverage weekday (an unknown future holiday): both
    mis-verdicts are harmless here — pre-"close" keeps ``today`` (nothing to
    derive on a holiday), post-"close" includes a bar-less date (derives
    nothing) — so the boot-safe unguarded variant is deliberate. Pure O(1);
    *now_ist* must be the current IST wall-clock instant.
    """
    today = now_ist.date()
    session = get_session(today)
    if session is None or now_ist.time() > session.close_time_ist:
        return today + timedelta(days=1)
    return today


def now_ist() -> datetime:
    """The current IST wall-clock instant — the ONE production IST clock leaf.

    DEC-DAILY-CUTOFF-GROUNDED-001: kills the inline
    ``datetime.now(UTC).astimezone(IST)`` copies at the prebuild cutoff pin
    sites (CLAUDE.md §11 COMMON) — callers that need "now, in IST" import
    THIS. The adapters-layer sibling
    :func:`brutex.adapters.ingest_common.now_ist` predates it and stays (the
    calendars package must never import adapters); both return the identical
    ``Asia/Kolkata``-aware instant. Tests freeze their local injectable clock
    seam (e.g. ``timeframe_materialize._now_ist``), never this leaf.
    """
    return datetime.now(tz=IST)


def session_last_bar_complete(session: TradingSession, last_bar_ist: datetime) -> bool:
    """``True`` iff *last_bar_ist* proves *session* ran to its calendar close.

    The ONE data-grounded completeness predicate
    (DEC-DAILY-CUTOFF-GROUNDED-001): ``last_bar + 1 minute >= close``,
    compared on full datetimes (robust against any midnight-adjacent close).
    The ``+ 1 minute`` is LOAD-BEARING: the ingest session filter is HALF-OPEN
    ``[open, close)`` (:func:`brutex.data.session.filter_to_trading_session`,
    pinned close-exclusive), so a complete regular session's LAST stored
    1-minute bar is labelled 15:29:00 — a naive ``last_bar >= close`` would
    NEVER fire and would defer every completed session forever. The close is
    the session's OWN per-date close (regular 15:30, Budget special-weekend,
    Muhurat afternoon/evening — resolved by :func:`get_session`), never a
    hardcoded 15:30. Grounding proves the CLOSE EDGE only, never session
    density (interior gaps stay the pull-side detectors' job). Pure O(1).
    """
    close_dt = datetime.combine(session.date, session.close_time_ist, tzinfo=last_bar_ist.tzinfo)
    return last_bar_ist + timedelta(minutes=1) >= close_dt


def daily_derive_cutoff_grounded(now_ist: datetime, *, last_bar_ist: datetime | None) -> date:
    """The data-grounded forming-session cutoff — DEFER-only over the clock arm.

    DEC-DAILY-CUTOFF-GROUNDED-001, the shipped data-grounded follow-up of
    :func:`daily_derive_cutoff` (the operator's ``run_day_session_closed``
    sketch): the clock arm proves "the session SHOULD be over"; this arm
    additionally demands the stored data AGREES before finalizing the RUN
    DAY's own daily bar. It can only ever DEFER relative to the clock verdict
    (``result ∈ {clock, run_day}`` and ``result <= clock`` — zero look-ahead
    risk, never a promotion past the clock), and it is RUN-DAY-ONLY (it never
    re-opens a session strictly before the run day — yesterday finalizes
    exactly as today; no permanent-deferral spiral).

    Resolution (every abstention falls back to the clock verdict — honest
    unknown, never a fabricated defer):

    * clock verdict already ``<= today`` (mid-session / pre-close / at-close)
      → clock: the run day is already excluded, nothing to ground (this also
      makes every mid-session run byte-identical to the clock arm by
      construction);
    * *last_bar_ist* is ``None`` (empty frame / non-datetime max) → clock;
    * the last bar is NOT dated the run day (no run-day bars: deriving writes
      no run-day bar under either verdict; a FUTURE-dated last bar also lands
      here — the caller WARNs) → clock;
    * no session on the run day (bars on a non-trading date —
      filter-impossible, defensive) → clock;
    * the run-day session is PROVABLY complete
      (:func:`session_last_bar_complete`) → clock (finalize the run day);
    * otherwise → ``today``: the run-day session is provably INCOMPLETE (a
      pull died mid-day / a stale mid-day frame re-run) — DEFER it rather
      than finalize a partial daily bar; latest-wins on the derived ``1day``
      heals it once the data completes.

    Inherits the clock arm's deliberately-unguarded :func:`get_session`
    stance (out-of-coverage fabrication is proven harmless there — restated,
    not re-guarded). Pure O(1); *now_ist* is the run's pinned IST instant,
    *last_bar_ist* the frame's last-bar IST timestamp.
    """
    clock = daily_derive_cutoff(now_ist)
    today = now_ist.date()
    if clock <= today:
        return clock
    if last_bar_ist is None or last_bar_ist.date() != today:
        return clock
    session = get_session(today)
    if session is None:
        return clock
    if session_last_bar_complete(session, last_bar_ist):
        return clock
    return today


def trading_days_between(start: date, end: date, *, exchange: str | None = None) -> list[date]:
    """List of trading days in [start, end] inclusive.

    Parameters
    ----------
    start, end
        The inclusive date range to scan.
    exchange
        ``None`` (default) / ``"NSE"`` / ``"BSE"`` — selects the holiday set;
        see :func:`is_holiday` (DEC-CALENDAR-EXCHANGE-SPLIT-001). Special
        weekend sessions stay SHARED (both exchanges).

    Raises
    ------
    ValueError
        If *exchange* is neither ``None`` nor ``"NSE"`` / ``"BSE"`` (validated
        eagerly, even for an empty range).
    """
    _resolve_holiday_set(exchange)  # eager validation: invalid exchange always raises
    days: list[date] = []
    d = start
    while d <= end:
        if is_trading_day(d, exchange=exchange):
            days.append(d)
        d += timedelta(days=1)
    return days


def trading_days_between_checked(
    start: date, end: date, *, exchange: str | None = None
) -> list[date]:
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

    Parameters
    ----------
    start, end
        The inclusive date range to scan.
    exchange
        ``None`` (default) / ``"NSE"`` / ``"BSE"`` — selects the holiday set;
        see :func:`is_holiday` (DEC-CALENDAR-EXCHANGE-SPLIT-001).

    Raises
    ------
    HolidayDataUnavailableError
        If any date in ``[start, end]`` is an out-of-coverage WEEKDAY that is
        not a special session.
    ValueError
        If *exchange* is neither ``None`` nor ``"NSE"`` / ``"BSE"`` (validated
        eagerly, even for an empty range).
    """
    _resolve_holiday_set(exchange)  # eager validation: invalid exchange always raises
    days: list[date] = []
    d = start
    while d <= end:
        if is_trading_day_checked(d, exchange=exchange):
            days.append(d)
        d += timedelta(days=1)
    return days


def previous_trading_session(
    d: date, *, exchange: str | None = None, observed: AbstractSet[date] = frozenset()
) -> date | None:
    """Return the trading session immediately preceding *d*, or ``None`` (abstain).

    DEC-MOVERS-PREVCLOSE-CONTIGUITY-001 — the ONE COMMON "which session should a
    previous-day baseline come from?" primitive (CLAUDE.md §11 COMMON). Walks
    backwards from ``d − 1 day`` and returns the first date the STRICT
    :func:`is_trading_day_checked` confirms as a trading day — stepping over
    weekends and every listed holiday, and honouring the special-weekend Budget
    sessions (:data:`SPECIAL_WEEKEND_TRADING_SESSIONS`).

    *observed* (DEC-PREOPEN-PREVDAY-COMMON-002) is the REMOVE-ONLY
    observed-sessions overlay (DEC-CALENDAR-OBSERVED-SESSIONS-001): the walk
    predicate is ``is_trading_day_checked(probe) AND probe not in observed``,
    so an overlay member can only flip a session verdict ``True -> False``
    (an observably-untraded day is stepped over exactly like a holiday) —
    never ``False -> True``. The overlay is plain DATA (a set of dates), so
    this module never imports the overlay loader (no package cycle). The
    default ``frozenset()`` keeps every existing caller byte-identical
    (parity-pinned). Termination is unaffected: the overlay is a FINITE set
    (detector-capped at 8 days/year; a hand-edited sidecar still loads a
    finite set), and the checked variant's raise at the coverage floor still
    bounds the walk.

    ABSTAIN, never guess (Golden Rule #1): when the back-walk crosses the
    published-holiday coverage floor before finding a session (the strict
    variant raises :class:`HolidayDataUnavailableError` on an out-of-coverage
    weekday), ONE ``calendar.previous_session_coverage_exhausted`` warning is
    emitted and ``None`` is returned — a caller must treat that as "unknown",
    not as "no prior session". The checked variant's raise on the first
    out-of-coverage weekday GUARANTEES termination (the walk is bounded by the
    coverage floor; within coverage the longest gap is a holiday cluster of a
    few days).

    Honest limit (Golden Rule #10): a Diwali **Muhurat** day is a NON-session
    here — :func:`is_trading_day_checked` scores the Muhurat date a holiday
    (the ~1-hour evening/afternoon session is deliberately outside the
    regular-session vocabulary), so a baseline actually carrying Muhurat bars
    will read as NOT the immediately-preceding session. Accepted + documented,
    not papered over.

    Parameters
    ----------
    d
        The reference date; the returned session is strictly before it.
    exchange
        ``None`` (default) / ``"NSE"`` / ``"BSE"`` — selects the holiday set;
        see :func:`is_holiday` (DEC-CALENDAR-EXCHANGE-SPLIT-001). The two sets
        are identical for every covered year, so today the argument is inert
        parity plumbing.
    observed
        Dates the observed-sessions overlay flags as NOT traded (remove-only;
        default empty = the plain derived calendar, byte-identical back-compat).

    Raises
    ------
    ValueError
        If *exchange* is neither ``None`` nor ``"NSE"`` / ``"BSE"`` (validated
        eagerly, before any date-path work).
    """
    _resolve_holiday_set(exchange)  # eager validation: invalid exchange always raises
    probe = d - timedelta(days=1)
    while True:
        try:
            if is_trading_day_checked(probe, exchange=exchange) and probe not in observed:
                return probe
        except HolidayDataUnavailableError:
            _LOG.warning(
                "calendar.previous_session_coverage_exhausted",
                requested_date=d.isoformat(),
                probe_date=probe.isoformat(),
                covered_first_year=HOLIDAY_COVERAGE_FIRST_YEAR,
                covered_last_year=HOLIDAY_COVERAGE_LAST_YEAR,
            )
            return None
        probe -= timedelta(days=1)


def session_minutes_per_day() -> int:
    """Standard regular session length in minutes (375 = 09:15-15:30)."""
    return SESSION_DURATION_MINUTES


def expected_bars_per_day_for_tf(tf_minutes: int) -> int:
    """Number of bars expected in a regular session at the given timeframe.

    CEILING division over the 375-minute session
    (DEC-CALENDAR-BARS-CEIL-001): the resampler oracle
    :func:`brutex.data.tf_resampler.resample_1m_to_tf` emits
    ``ceil(375 / tf)`` bars per regular session — session-anchored
    left-labeled buckets INCLUDING the truncated final bucket (pinned by
    ``tests/unit/test_tf_resampler_session_bounds.py``). The previous FLOOR
    division under-counted every timeframe that does not divide 375 (e.g.
    60m read 6 while the lake actually stores 7 bars, the last labeled
    15:15 IST covering only 15:15-15:29).

    Examples:
    - 1m  -> 375 bars
    - 5m  -> 75 bars
    - 15m -> 25 bars
    - 30m -> 13 bars (12 full + the truncated 15:00-15:29 final bucket)
    - 60m -> 7 bars (6 full + the truncated 15:15-15:29 final bucket)
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
    return -(-SESSION_DURATION_MINUTES // tf_minutes)


def expected_bars_for_session(session: TradingSession, tf_minutes: int) -> int:
    """Number of bars expected in ``session`` at the given timeframe — session-AWARE.

    DEC-SESSION-DENSITY-CENSUS-001 — the session-aware twin of the date-blind
    :func:`expected_bars_per_day_for_tf`: the SAME ceiling-division law
    (DEC-CALENDAR-BARS-CEIL-001 — session-anchored left-labeled buckets
    INCLUDING the truncated final bucket) applied to the session's OWN
    :attr:`TradingSession.duration_minutes` instead of the hardcoded
    375-minute regular constant, so a Muhurat hour (60 min), the 2021-02-24
    extended 09:15-17:00 outage day (465 min — DEC-EXTENDED-SESSION-OUTAGE-001)
    and any future short/long session all predict honestly. For every REGULAR
    375-minute session the two functions are equal (property-pinned);
    :func:`expected_bars_per_day_for_tf` itself is byte-UNTOUCHED.

    Examples
    --------
    >>> from datetime import date
    >>> regular = get_session(date(2024, 1, 2))  # a regular Tuesday
    >>> expected_bars_for_session(regular, 1)
    375
    >>> expected_bars_for_session(regular, 30)
    13
    >>> extended = get_session(date(2021, 2, 24))  # 09:15-17:00 outage day
    >>> expected_bars_for_session(extended, 1)
    465

    Raises
    ------
    ValueError
        If ``tf_minutes < 1`` (mirrors :func:`expected_bars_per_day_for_tf`).
    """
    if tf_minutes < 1:
        raise ValueError(f"expected_bars_for_session.tf_minutes must be >= 1, got {tf_minutes}")
    duration = session.duration_minutes
    if tf_minutes >= duration:
        return 1
    return -(-duration // tf_minutes)


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
