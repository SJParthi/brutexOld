"""Per-underlying expiry day-of-week registry (SEBI-aware, 2020-2026).

Per `docs/track2/TRACK2_OPTIONS_SPEC.md` §2 — expiry rules changed
multiple times during the 2020-2026 backfill window per NSE/BSE/SEBI
circulars. This module is the per-date lookup. Coverage starts
**2020-01-01**, matching the data-pull floor (``GROWW_HISTORY_FLOOR``;
CLAUDE.md §3, DEC-PULL-FLOOR-2020-001) — DEC-EXPIRY-REGIME-2020-001
extended every table's first row from 2021-01-01 back to 2020-01-01
(a coverage EXTENSION of the same regime, NOT a new transition: two
independent research rounds found NO expiry-weekday regime change
between 2020-01-01 and 2021-01-01 for any of the six series). A date
before 2020-01-01 still raises :class:`ValueError` (coverage floor).

## What it returns

For a given (instrument, date), two questions can be asked:

1. `get_weekly_dow(instrument, date)` → 0..4 (Mon-Fri) or None.
   None means "no weekly expiry available on this date" (e.g.,
   BANKNIFTY after 2024-11-13 when SEBI killed weekly).

2. `get_monthly_dow(instrument, date)` → 0..4. Monthly expiry day-of-week
   is always defined (every instrument has a monthly contract).

3. `next_weekly_expiry(instrument, on_or_after)` → date or None.
   Returns the next weekly expiry date AT OR AFTER the given date.
   Doesn't account for holiday shifts (downstream responsibility —
   when an expiry day is a NSE/BSE holiday, the contract typically
   moves to the prior trading day; that's a Phase 2.5 concern).

4. `next_monthly_expiry(instrument, on_or_after)` → date.
   Returns the "last X of the month" for the current month (if not yet
   passed) or next month.

## Verified history (citation-grounded 2026-05-16; 2020 extension 2026-07-03)

See spec §2 for the per-underlying tables with NSE/BSE circular sources.

2020 extension honesty (Golden Rule #1, DEC-EXPIRY-REGIME-2020-001): the
2020 rows are secondary-corroborated across two independent research
rounds (primary NSE/BSE circulars were unreachable from the research
environment — HTTP 403). NIFTY + BANKNIFTY weekly-THURSDAY /
monthly-last-THURSDAY are CORROBORATED (NIFTY weekly options launched
2019-02-11, BANKNIFTY weekly launched May 2016 with Thursday expiry;
Thursday regime continuous until the 2023 change series — ICICIdirect
revised-expiry-days article, Zerodha bulletin 358889, 5paisa "25-year
Thursday tradition" coverage). SENSEX 2020 Thursday is an
UNVERIFIED-continuity inference (pre-2023-05-15 relaunch BSE derivatives
were thin/illiquid; Business Standard's relaunch coverage says "a new
expiry cycle of Friday from Thursday earlier", implying Thursday before
May 2023, but NO 2020-dated BSE circular could be fetched).

## Withdrawn Monday regime (NIFTY March-April 2025)

NSE announced an all-weekly shift to Monday effective 2025-04-04
(circular early March 2025), then withdrew it 2025-03-27 before any
Monday expiry traded. We DO NOT include the withdrawn-Monday regime in
the history table — NIFTY weekly stayed Thursday through 2025-08-28
and jumped directly to Tuesday on 2025-09-02.

## COMMON · DYNAMIC · SCALABLE · AUTOMATED

- COMMON: same module imported by backtest + future TickVault
- DYNAMIC: add a new SEBI regime change by appending one row per instrument
- SCALABLE: O(log N) per lookup (binary search), N ≤ 6 transitions
- AUTOMATED: pure function, no I/O
"""

from __future__ import annotations

from bisect import bisect_right
from datetime import date, timedelta
from typing import Final

from brutex.options.lot_size_history import OptionsUnderlying

# Day-of-week constants (matches Python's date.weekday() convention).
MONDAY: Final[int] = 0
TUESDAY: Final[int] = 1
WEDNESDAY: Final[int] = 2
THURSDAY: Final[int] = 3
FRIDAY: Final[int] = 4

# Sentinel meaning "no weekly expiry available on this date".
NO_WEEKLY: Final[int] = -1

#: December — last calendar month, used for next-month rollover.
_DECEMBER: Final[int] = 12


# ────── Weekly expiry day-of-week history ──────
# Each tuple = (effective_date, day_of_week).  day_of_week=NO_WEEKLY (-1)
# means weekly was killed.

_WEEKLY_NIFTY: Final[tuple[tuple[date, int], ...]] = (
    # 2020 coverage extension (DEC-EXPIRY-REGIME-2020-001): SAME Thursday
    # regime, not a transition — NIFTY weekly options launched 2019-02-11
    # (Thursday) and stayed Thursday continuously until 2025-09-02
    # (ICICIdirect revised-expiry-days; 5paisa "25-year Thursday tradition";
    # two research rounds agree, secondary-corroborated — primary NSE
    # circulars 403'd).
    (date(2020, 1, 1), THURSDAY),
    (date(2025, 9, 2), TUESDAY),  # NSE FAOP/68747 + SEBI standardization May 2025
)

_WEEKLY_BANKNIFTY: Final[tuple[tuple[date, int], ...]] = (
    # 2020 coverage extension (DEC-EXPIRY-REGIME-2020-001): SAME Thursday
    # regime — BANKNIFTY weekly launched May 2016 WITH Thursday expiry and
    # first changed weekday 2023-09-04 (ICICIdirect; Zerodha bulletin
    # 358889; two research rounds agree, secondary-corroborated).
    (date(2020, 1, 1), THURSDAY),
    (date(2023, 9, 4), WEDNESDAY),  # NSE FAOP/57540 — first Wed weekly 2023-09-06
    (date(2024, 11, 14), NO_WEEKLY),  # SEBI killed BN-WEEK on EOD 2024-11-13
)

_WEEKLY_SENSEX: Final[tuple[tuple[date, int], ...]] = (
    # 2020 coverage extension (DEC-EXPIRY-REGIME-2020-001): UNVERIFIED
    # continuity — pre-2023-05-15-relaunch SENSEX weekly derivatives were
    # thin/illiquid and NO citable 2020 BSE circular could be fetched (403).
    # Best evidence is a single-source inference: Business Standard's 2023
    # relaunch coverage ("a new expiry cycle of Friday from Thursday
    # earlier") implies Thursday BEFORE May 2023, consistent with the
    # existing "(thin liquidity)" 2021 row this extends.
    (date(2020, 1, 1), THURSDAY),  # thin liquidity pre-2023-05-15
    (date(2023, 5, 15), FRIDAY),  # BSE Notice 20230327-65
    (date(2025, 1, 1), TUESDAY),  # Business Standard 2024-10-04
    (date(2025, 9, 4), THURSDAY),  # SEBI standardization May 2025
)


# ────── Monthly expiry day-of-week history ──────
# Monthly expiry = "last <day-of-week> of the calendar month".

_MONTHLY_NIFTY: Final[tuple[tuple[date, int], ...]] = (
    # 2020 coverage extension (DEC-EXPIRY-REGIME-2020-001): NSE index
    # monthly expiry was last-Thursday from the early 2000s until the
    # 2023-2025 change series — no monthly-weekday change documented
    # anywhere near 2020 (two research rounds agree).
    (date(2020, 1, 1), THURSDAY),  # last Thursday
    (date(2025, 9, 2), TUESDAY),  # last Tuesday — SEBI standardization
)

_MONTHLY_BANKNIFTY: Final[tuple[tuple[date, int], ...]] = (
    # 2020 coverage extension (DEC-EXPIRY-REGIME-2020-001): same basis as
    # NIFTY monthly — last-Thursday continuous through 2020, first change
    # 2024-03-01 (two research rounds agree, secondary-corroborated).
    (date(2020, 1, 1), THURSDAY),  # last Thursday
    (date(2024, 3, 1), WEDNESDAY),  # NSE FAOP/60011 — first Wed monthly 2024-03-27
    (date(2025, 1, 1), THURSDAY),  # NSE FAOP/65336 — reverted to last Thursday
    (date(2025, 9, 2), TUESDAY),  # SEBI standardization — last Tuesday
)

_MONTHLY_SENSEX: Final[tuple[tuple[date, int], ...]] = (
    # 2020 coverage extension (DEC-EXPIRY-REGIME-2020-001): UNVERIFIED
    # continuity — same single-source Thursday-before-May-2023 inference as
    # _WEEKLY_SENSEX above; no 2020-dated BSE contract spec obtainable.
    (date(2020, 1, 1), THURSDAY),  # last Thursday (thin)
    (date(2023, 5, 15), FRIDAY),  # last Friday
    (date(2025, 1, 1), TUESDAY),  # last Tuesday
    (date(2025, 9, 4), THURSDAY),  # last Thursday — SEBI standardization
)


_WEEKLY_BY_UNDERLYING: Final[dict[OptionsUnderlying, tuple[tuple[date, int], ...]]] = {
    OptionsUnderlying.NIFTY: _WEEKLY_NIFTY,
    OptionsUnderlying.BANKNIFTY: _WEEKLY_BANKNIFTY,
    OptionsUnderlying.SENSEX: _WEEKLY_SENSEX,
}

_MONTHLY_BY_UNDERLYING: Final[dict[OptionsUnderlying, tuple[tuple[date, int], ...]]] = {
    OptionsUnderlying.NIFTY: _MONTHLY_NIFTY,
    OptionsUnderlying.BANKNIFTY: _MONTHLY_BANKNIFTY,
    OptionsUnderlying.SENSEX: _MONTHLY_SENSEX,
}


# ────── Helpers ──────


def _lookup_in_history(
    history: tuple[tuple[date, int], ...],
    on_date: date,
) -> int:
    """Binary search the history table for the value in effect on `on_date`."""
    if on_date < history[0][0]:
        raise ValueError(f"on_date {on_date} is before recorded history start ({history[0][0]})")
    dates_only = [t[0] for t in history]
    idx = bisect_right(dates_only, on_date) - 1
    return history[idx][1]


def get_weekly_dow(
    instrument: OptionsUnderlying,
    on_date: date,
) -> int | None:
    """Day of week for weekly expiry on `on_date`, or None if no weekly available.

    Returns:
        0..4 (Mon-Fri) if weekly is available; None if SEBI has killed weekly
        for this instrument on this date (currently only BANKNIFTY post-2024-11-13).
    """
    dow = _lookup_in_history(_WEEKLY_BY_UNDERLYING[instrument], on_date)
    return None if dow == NO_WEEKLY else dow


def get_monthly_dow(
    instrument: OptionsUnderlying,
    on_date: date,
) -> int:
    """Day of week for monthly expiry on `on_date`.

    Returns 0..4 (Mon-Fri). Monthly expiry is always defined for our 3 instruments.
    """
    return _lookup_in_history(_MONTHLY_BY_UNDERLYING[instrument], on_date)


def has_weekly(instrument: OptionsUnderlying, on_date: date) -> bool:
    """Convenience: True iff weekly expiry is available on this date."""
    return get_weekly_dow(instrument, on_date) is not None


def next_weekly_expiry(
    instrument: OptionsUnderlying,
    on_or_after: date,
) -> date | None:
    """Find the next weekly expiry AT OR AFTER `on_or_after`.

    Returns:
        Next weekly expiry date, or None if no weekly is available for the
        instrument on `on_or_after`. Doesn't account for holiday shifts.
    """
    target_dow = get_weekly_dow(instrument, on_or_after)
    if target_dow is None:
        return None
    days_until = (target_dow - on_or_after.weekday()) % 7
    return on_or_after + timedelta(days=days_until)


def next_monthly_expiry(
    instrument: OptionsUnderlying,
    on_or_after: date,
) -> date:
    """Find the next monthly expiry AT OR AFTER `on_or_after`.

    Monthly expiry = "last <day-of-week> of the calendar month."

    If the last <dow> of `on_or_after`'s month is already past, rolls to
    the next month — and RE-EVALUATES the dow for that month (handles
    regime transitions across the year boundary, e.g., BANKNIFTY Dec
    2024 = last Wednesday but Jan 2025 = last Thursday).

    Returns:
        Date of the next monthly expiry (no None — monthly is always defined).

    Doesn't account for NSE/BSE holiday shifts (downstream concern).
    """
    # Use this month's regime first
    this_month_dow = get_monthly_dow(instrument, on_or_after)
    candidate = _last_dow_of_month(on_or_after.year, on_or_after.month, this_month_dow)
    if candidate >= on_or_after:
        return candidate
    # This month's expiry already past — roll to next month and re-evaluate dow
    if on_or_after.month == _DECEMBER:
        next_year, next_month = on_or_after.year + 1, 1
    else:
        next_year, next_month = on_or_after.year, on_or_after.month + 1
    # Use mid-month of the rolled month as the dow-lookup reference (avoids
    # 1st-of-month overlapping with a transition that takes effect on the 1st)
    next_month_ref = date(next_year, next_month, 15)
    next_month_dow = get_monthly_dow(instrument, next_month_ref)
    return _last_dow_of_month(next_year, next_month, next_month_dow)


def _last_dow_of_month(year: int, month: int, target_dow: int) -> date:
    """Return the last <target_dow> of (year, month)."""
    # Find last day of month
    next_month_first = date(year + 1, 1, 1) if month == _DECEMBER else date(year, month + 1, 1)
    last_day = next_month_first - timedelta(days=1)
    # Walk back from last_day until we hit target_dow
    days_back = (last_day.weekday() - target_dow) % 7
    return last_day - timedelta(days=days_back)
