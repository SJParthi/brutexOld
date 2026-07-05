"""Rolling-weekly + weekly-static Fibonacci anchor producers (PR-2).

ADR-0012 D2 defines three NIFTY-futures strategies distinguished ONLY by the
Fibonacci anchor. The existing prev-day anchor lives in
``brutex.indicators.prev_day_ohlc`` / ``compute_fib_levels``; this module adds the
other two ANCHOR computations. Each yields a ``(high, low)`` pair the caller feeds
into the SAME ``compute_fib_levels(today, high, low)`` — so there are NO new Fib
bits (the anchor is a swept STRATEGY parameter, ADR-0012 D4), only two new
producers of the existing 15-level ``FibLevels`` struct.

* **rolling-weekly** — anchor = max-High / min-Low over the **trailing 5 completed
  sessions** (strictly before today), recomputed each session.
* **weekly-static** — anchor = High/Low of the **last COMPLETED ISO calendar
  week** (Mon-Sun), frozen for every session of the current week.

## Look-ahead safety (ADR-0012 §4.5 #13, Property P-06)

Both anchors read ONLY prior-day daily candles — exactly like
``prev_day_ohlc.lookup_prev_day_ohlc``. "Last completed week" selects ISO weeks
**strictly before** the current week; the trailing-5 window selects trading days
**strictly before** today. Today's developing intraday bars are NEVER read, so
the anchor for day D never depends on any bar dated >= D. This is the same proof
the PDH/PDL lookup already satisfies (P-06-clean by construction).

## Cold-start (ADR-0012 §4.5 #6) — combo INELIGIBLE until warm

If there are fewer than 5 prior trading sessions (rolling-weekly) or no completed
prior ISO week (weekly-static), the anchor is not warm → the producer returns
``None``. The engine treats a ``None`` anchor as **combo ineligible** for that bar
(no signal), NOT as a degenerate zero — mirroring the EMA/VWAP warm-up contract.
PR-2 supplies the ``None``; PR-4/PR-5 enforce the eligibility gate.

## Muhurat / short weeks (ADR-0012 §4.5 #7)

The Muhurat evening session is EXCLUDED from the weekly High/Low — and this is
AUTOMATIC, not a special case: the NSE calendar does not count a Muhurat date as a
regular trading day (``is_trading_day(muhurat) is False``; it lives only in
``MUHURAT_SESSIONS``), so ``find_prev_trading_day`` never returns it and it can
never enter a weekly window. A holiday-shortened week is still a valid completed
week — whatever regular trading days it had are used. ``is_trading_day`` governs
which dates are sessions.

## O(1) per session

Both anchors recompute once per trading day (like pivots), held constant intraday.
The trailing-5 scan is O(5); the completed-week scan is O(days-in-prior-weeks
within the provided daily dict) = bounded. Held constant intraday → O(1)-amortized
per bar.

## Money representation

int64 paisa throughout per CLAUDE.md §4 (``DailyCandle`` is the prev_day_ohlc one).
"""

from __future__ import annotations

from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from datetime import date

from brutex.calendars.observed_sessions import is_trading_day_checked_observed
from brutex.indicators.prev_day_ohlc import DailyCandle, find_prev_trading_day


@dataclass(frozen=True)
class FibAnchor:
    """A ``(high, low)`` Fibonacci anchor pair + its provenance.

    The caller feeds ``high_paisa`` / ``low_paisa`` into
    ``compute_fib_levels(today, high_paisa, low_paisa)``. ``source_days`` is the
    sorted list of trading days that contributed (for audit / debugging).
    """

    today: date  # the trading day this anchor applies to
    high_paisa: int  # anchor high (max over the window)
    low_paisa: int  # anchor low (min over the window)
    source_days: tuple[date, ...]  # the contributing trading days (sorted)


def _iso_week(d: date) -> tuple[int, int]:
    """Return the ``(iso_year, iso_week)`` key for *d* (stdlib ISO calendar)."""
    iso = d.isocalendar()
    return iso.year, iso.week


def rolling_weekly_anchor(
    today: date,
    daily_candles_by_date: dict[date, DailyCandle],
    *,
    sessions: int = 5,
    observed: AbstractSet[date] = frozenset(),
) -> FibAnchor | None:
    """Anchor over the trailing *sessions* completed trading days before *today*.

    Walks backwards via the trading-day calendar to collect the last *sessions*
    trading days strictly before ``today`` that are present in
    ``daily_candles_by_date``; the anchor high = max of their daily highs, low =
    min of their daily lows. The Muhurat evening session is excluded.

    Args:
        today: The trading day the anchor applies to.
        daily_candles_by_date: ``date -> DailyCandle`` (caller-provided, the
            prev_day_ohlc daily dict).
        sessions: The trailing-session window (default 5 = one trading week).
        observed: Observed-holiday overlay days (DEC-CALENDAR-OBSERVED-SESSIONS-001,
            remove-only; default empty = byte-identical back-compat). An overlaid
            date is treated as NOT a session, so the trailing window extends one
            session earlier instead of folding an observably-untraded day in.

    Returns:
        A :class:`FibAnchor`, or ``None`` if fewer than *sessions* prior trading
        days with a daily candle are available (cold-start → combo ineligible).

    Raises:
        ValueError: if ``today`` is not a trading day OR ``sessions`` < 1.
        HolidayDataUnavailableError: if ``today`` (or a date the backward
            walk visits) lies outside the published holiday span so its
            trading-day verdict is unknown (DEC-CALENDAR-2020-HONEST-001) —
            the strict variant refuses to guess rather than fold an
            out-of-coverage 2020 holiday into the weekly window.
    """
    if not is_trading_day_checked_observed(today, observed=observed):
        raise ValueError(f"today ({today}) is not a trading day — rolling-weekly anchor invalid")
    if sessions < 1:
        raise ValueError(f"sessions must be >= 1, got {sessions}")

    collected: list[DailyCandle] = []
    cursor = today
    # Bound the backward walk generously: even with holiday clusters, `sessions`
    # trading days lie within a few weeks. `find_prev_trading_day` returns only
    # `is_trading_day` dates, and the Muhurat evening session is NOT a regular
    # trading day (`is_trading_day(muhurat) is False` — it lives only in
    # MUHURAT_SESSIONS), so it is excluded automatically (ADR-0012 §4.5 #7). We
    # skip dates absent from the dict, stopping once we have `sessions` candles or
    # run out of history.
    guard = 0
    max_steps = sessions * 14  # ample slack for holiday-dense spans
    while len(collected) < sessions and guard < max_steps:
        guard += 1
        prev = find_prev_trading_day(cursor, observed=observed)
        if prev is None:
            break
        cursor = prev
        candle = daily_candles_by_date.get(prev)
        if candle is None:
            continue
        collected.append(candle)

    if len(collected) < sessions:
        return None  # cold-start: not enough prior sessions → ineligible

    days = tuple(sorted(c.day for c in collected))
    return FibAnchor(
        today=today,
        high_paisa=max(c.high_paisa for c in collected),
        low_paisa=min(c.low_paisa for c in collected),
        source_days=days,
    )


def weekly_static_anchor(
    today: date,
    daily_candles_by_date: dict[date, DailyCandle],
    *,
    observed: AbstractSet[date] = frozenset(),
) -> FibAnchor | None:
    """Anchor = High/Low of the last COMPLETED ISO week before *today*'s week.

    Collects every daily candle whose ISO week is strictly before ``today``'s ISO
    week, takes the MOST RECENT such completed week, and returns its High/Low.
    Frozen for every session of the current week (recompute only when the ISO week
    advances). The Muhurat evening session is excluded.

    Args:
        today: The trading day the anchor applies to.
        daily_candles_by_date: ``date -> DailyCandle`` (caller-provided).
        observed: Observed-holiday overlay days (DEC-CALENDAR-OBSERVED-SESSIONS-001,
            remove-only; default empty = byte-identical back-compat). An overlaid
            date is stepped over exactly like a holiday, so it can never
            contribute to the completed week's High/Low.

    Returns:
        A :class:`FibAnchor` for the last completed week, or ``None`` if no daily
        candle falls in any prior ISO week (cold-start → combo ineligible).

    Raises:
        ValueError: if ``today`` is not a trading day.
        HolidayDataUnavailableError: if ``today`` (or a date the backward
            walk visits) lies outside the published holiday span so its
            trading-day verdict is unknown (DEC-CALENDAR-2020-HONEST-001).
    """
    if not is_trading_day_checked_observed(today, observed=observed):
        raise ValueError(f"today ({today}) is not a trading day — weekly-static anchor invalid")

    current_week = _iso_week(today)
    # Walk BACKWARDS from today (never enumerating the dict, so a future-dated key
    # is never even touched — the strict P-06 look-ahead proof) until we land in
    # the FIRST ISO week strictly before the current one, then collect that whole
    # completed week's sessions. The Muhurat evening session is excluded.
    cursor = today
    target_week: tuple[int, int] | None = None
    collected: list[DailyCandle] = []
    guard = 0
    max_steps = 60  # ample slack to step out of the current week + scan one week
    while guard < max_steps:
        guard += 1
        prev = find_prev_trading_day(cursor, observed=observed)
        if prev is None:
            break
        cursor = prev
        prev_week = _iso_week(prev)
        if prev_week >= current_week:
            continue  # still inside (a tail of) the current ISO week — skip
        if target_week is None:
            target_week = prev_week  # the most recent completed week
        if prev_week != target_week:
            break  # walked past the target completed week → done
        # `find_prev_trading_day` returns only regular trading days; the Muhurat
        # evening session is NOT one (`is_trading_day(muhurat) is False`), so it is
        # excluded from the weekly H/L automatically (ADR-0012 §4.5 #7).
        candle = daily_candles_by_date.get(prev)
        if candle is not None:
            collected.append(candle)

    if not collected:
        return None  # cold-start: no completed prior week (or no candles) → ineligible

    days = tuple(sorted(c.day for c in collected))
    return FibAnchor(
        today=today,
        high_paisa=max(c.high_paisa for c in collected),
        low_paisa=min(c.low_paisa for c in collected),
        source_days=days,
    )
