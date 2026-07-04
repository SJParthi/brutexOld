"""Previous Day OHLC anchors — fetched from yesterday's daily candle.

Per `docs/TRACK1_VOCABULARY.md` Indicator Group 1 (Anchors):
    "PDH (Prev Day High), PDL (Prev Day Low), PDO (Prev Day Open),
     PDC (Prev Day Close)"

## What it computes

For a given (instrument, trading_day), look up the immediately
prior trading day's daily candle and return its OHLC values.

Reference Pine Script (operator-provided 2026-05-16, Pine v6):
```
yHigh  = request.security(syminfo.tickerid, "D", high[1],  lookahead=barmerge.lookahead_on)
yLow   = request.security(syminfo.tickerid, "D", low[1],   lookahead=barmerge.lookahead_on)
yOpen  = request.security(syminfo.tickerid, "D", open[1],  lookahead=barmerge.lookahead_on)
yClose = request.security(syminfo.tickerid, "D", close[1], lookahead=barmerge.lookahead_on)
```

The `[1]` index on the daily timeframe means "the bar BEFORE today" —
i.e., yesterday's completed daily bar.

## Trading-day calendar

Uses the NSE/BSE trading-day calendar from `brutex.calendars.nse` to
find the prior trading day. Weekends and holidays are skipped. Example:

    Today is Monday  → prior trading day = Friday  (skip Sat/Sun)
    Today is Tuesday after a Monday holiday → prior = previous Friday

## Holiday-coverage honesty (DEC-CALENDAR-2020-HONEST-001)

The Track-1 data-pull floor is 2020-01-01 (``GROWW_HISTORY_FLOOR``;
CLAUDE.md §3); the hardcoded NSE/BSE holiday + Muhurat lists span
``[HOLIDAY_COVERAGE_FIRST_YEAR, HOLIDAY_COVERAGE_LAST_YEAR]`` (now 2020-2026
after DEC-CALENDAR-2020-DATA-001). The UNGUARDED ``is_trading_day`` reads an
out-of-coverage weekday holiday (e.g. a 2019 holiday) as a *trading day* — a
SILENT wrong answer — so an out-of-coverage-anchored PDH/PDL lookup could fetch
the holiday's daily candle as the "prior trading day". Per Golden Rule #1 we do
NOT fabricate missing holiday dates; instead every trading-day verdict on this
anchor path goes through the STRICT ``is_trading_day_checked``, which raises
``HolidayDataUnavailableError`` for an out-of-coverage weekday rather than
guess. In-coverage (2020-2026) dates are byte-identical to the unguarded path;
only the previously-silent-wrong out-of-coverage case (now pre-2020) becomes loud.

## "Data wins" walk-back (DEC-CALENDAR-OBSERVED-SESSIONS-001, Session D PR-1)

Previously :func:`lookup_prev_day_ohlc` returned ``None`` whenever the calendar
prior session's daily candle was missing from the provided dict — so BOTH a
calendar-drift day (the vendor observed a holiday the derived calendar lacks)
AND a genuine data gap silently lost the PDH/PDL anchor. Now the lookup walks
BACK within ``lookback_max`` (14) calendar days over the CALENDAR SESSIONS
(minus any *observed* overlay day — see below) to the latest session WITH a
candle, returns that STALE anchor with honest ``prev_day`` provenance, and emits
ONE :data:`PREV_DAY_STALE_ANCHOR_EVENT` WARNING. This is an INTENDED semantic
change that ALSO covers genuine data gaps (stale anchor + warn, previously
``None``) — "data wins", operator-decided, regression-pinned.

The walk domain is deliberately CALENDAR-SESSIONS-ONLY (``is_trading_day_checked``
minus the observed overlay), NOT raw calendar days: a Muhurat / weekend daily bar
that happens to sit in the candle dict can therefore never become the anchor —
preserving the ledgered Muhurat exclusion (ADR-0012 §4.5 #7) the weekly anchors
cite. Because a candle can only win ON a session, a found winner implies the
calendar prior session exists; the two coincide on the silent normal path.

Coverage-floor bound (hostile-review fold): once the calendar prior session is
established, the candle hunt never probes past the published-holiday coverage
floor — a candle-less span reaching pre-2020 ends with the honest no-anchor
WARN + ``None`` (main-parity), NOT a ``HolidayDataUnavailableError`` raised out
of a data gap. The strict raise remains for the genuinely-calendar case (today /
the walk out of coverage before any prior session is found).

## Observed-sessions overlay (remove-only)

Every function takes an optional keyword-only ``observed`` set
(:mod:`brutex.calendars.observed_sessions` — default ``frozenset()``, byte-identical
back-compat). An overlaid date is treated as NOT a session: the walk steps over
it exactly like a holiday. When the overlay explains the drift the walk lands on
the earlier session's candle and the winner EQUALS the calendar prior session —
so a reconciled drift is SILENT (no stale warn); the warn fires only for days the
overlay does not explain.

## No look-ahead

PDH/PDL/PDO/PDC are computed from the COMPLETED daily candle of the
prior trading day. Today's developing bars are never read; the walk-back only
ever visits dates strictly BEFORE today. This satisfies Property P-06
(instrumented bar accessor).

## Money representation

int64 paisa throughout per CLAUDE.md §4.
"""

from __future__ import annotations

from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Final

import structlog

from brutex.calendars.nse import is_holiday_data_available
from brutex.calendars.observed_sessions import is_trading_day_checked_observed

_LOG = structlog.get_logger(__name__)

#: WARNING emitted when the returned anchor is NOT the calendar prior session's
#: candle (the "data wins" walk-back landed on an earlier session). ONE per lookup.
PREV_DAY_STALE_ANCHOR_EVENT: Final[str] = "prev_day_ohlc.stale_anchor"
#: WARNING emitted when NO session within ``lookback_max`` calendar days carries a
#: candle — the only remaining ``None`` path of :func:`lookup_prev_day_ohlc`.
PREV_DAY_NO_ANCHOR_EVENT: Final[str] = "prev_day_ohlc.no_anchor_within_lookback"


@dataclass(frozen=True)
class PrevDayOHLC:
    """Previous trading day's OHLC anchors for one (instrument, today)."""

    today: date  # today (the trading day these anchors apply to)
    prev_day: date  # the trading day from which OHLC was fetched
    pdo_paisa: int  # Previous Day Open
    pdh_paisa: int  # Previous Day High
    pdl_paisa: int  # Previous Day Low
    pdc_paisa: int  # Previous Day Close


@dataclass(frozen=True)
class DailyCandle:
    """One day's OHLC, in int64 paisa.

    Used as input to the PrevDayOHLC lookup. Caller is responsible for
    parsing daily Parquet into these.
    """

    day: date
    open_paisa: int
    high_paisa: int
    low_paisa: int
    close_paisa: int


def find_prev_trading_day(
    today: date,
    lookback_max: int = 14,
    *,
    observed: AbstractSet[date] = frozenset(),
) -> date | None:
    """Find the most recent trading day STRICTLY BEFORE `today`.

    Walks backwards day-by-day, skipping weekends, holidays, and any *observed*
    overlay day (DEC-CALENDAR-OBSERVED-SESSIONS-001 — remove-only: the overlay can
    only step the walk over a day, never add one). Returns None if no trading day
    is found within `lookback_max` calendar days (defensive against extended
    holiday clusters; max NSE holiday cluster is < 14 calendar days). PURE
    calendar semantics — the "data wins" walk-back lives ONLY in
    :func:`lookup_prev_day_ohlc`, the one function holding the candle dict, so
    calendar consumers (weekly anchors, ``data/futures_roll``) are unaffected.

    Examples:
        find_prev_trading_day(Mon) -> previous Fri
        find_prev_trading_day(Tue after long weekend) -> previous Fri

    Raises:
        HolidayDataUnavailableError: if the backward walk reaches an
            out-of-coverage weekday whose holiday status is unknown
            (DEC-CALENDAR-2020-HONEST-001). The strict
            ``is_trading_day_checked`` refuses to guess "trading day" for a
            date outside the published holiday span (e.g. a 2019 weekday)
            rather than silently return it as the prior trading day.
    """
    d = today - timedelta(days=1)
    for _ in range(lookback_max):
        if is_trading_day_checked_observed(d, observed=observed):
            return d
        d -= timedelta(days=1)
    return None


def lookup_prev_day_ohlc(
    today: date,
    daily_candles_by_date: dict[date, DailyCandle],
    *,
    observed: AbstractSet[date] = frozenset(),
) -> PrevDayOHLC | None:
    """Look up the prior session's OHLC for *today* — "DATA WINS" walk-back.

    DEC-CALENDAR-OBSERVED-SESSIONS-001 (Session D PR-1): walks back up to 14
    calendar days over the CALENDAR SESSIONS (``is_trading_day_checked`` minus
    *observed*) strictly before *today*; the FIRST session carrying a candle in
    *daily_candles_by_date* wins. When that winner IS the calendar prior session
    the path is silent and byte-identical to the pre-change behaviour; when it is
    an EARLIER session the STALE anchor is returned (honest ``prev_day``
    provenance) with ONE :data:`PREV_DAY_STALE_ANCHOR_EVENT` WARNING — an
    INTENDED semantic change that ALSO covers genuine data gaps (previously
    ``None``). The session-restricted domain preserves the ADR-0012 §4.5 #7
    Muhurat exclusion: a Muhurat / weekend candle in the dict can never win.

    Args:
        today: The trading day for which we want the prior day's anchors.
        daily_candles_by_date: Dict mapping `date -> DailyCandle`. Caller
            provides this — typically loaded from a daily-bars Parquet.
        observed: Observed-holiday overlay days (remove-only; default empty =
            byte-identical back-compat). An overlaid calendar prior session is
            stepped over; when its candle is genuinely absent the walk lands on
            the earlier session SILENTLY (the drift is reconciled, not noisy).

    Returns:
        PrevDayOHLC from the latest prior session WITH a candle, or None when NO
        session within the 14-day lookback carries one (logged
        :data:`PREV_DAY_NO_ANCHOR_EVENT`). Because a candle can only win ON a
        session, a non-None result implies the calendar prior session exists —
        the "calendar-prev is None yet a candle exists" case is structurally
        impossible under the session-restricted walk.

    Raises:
        ValueError: if `today` itself is not a trading day — overlaid days
            included (likely programmer error — should not be asking for PDH on
            a holiday / an observably-untraded day).
        HolidayDataUnavailableError: if `today` lies outside the published
            holiday span, or the backward walk reaches an out-of-coverage
            date BEFORE the calendar prior session is established
            (DEC-CALENDAR-2020-HONEST-001). Once the calendar prior session
            IS established, an out-of-coverage date ends the walk with the
            no-anchor WARN + ``None`` instead — a data gap near the 2020
            coverage floor is a CANDLE question, not a calendar one
            (main-parity, regression-pinned; hostile-review fold).
    """
    if not is_trading_day_checked_observed(today, observed=observed):
        raise ValueError(f"today ({today}) is not a trading day — PDH/PDL lookup invalid")

    lookback_max = 14
    calendar_prev: date | None = None
    winner: tuple[date, DailyCandle] | None = None
    d = today - timedelta(days=1)
    for _ in range(lookback_max):
        if calendar_prev is not None and not is_holiday_data_available(d):
            # Coverage-floor abstain (hostile-review fold): the calendar prior
            # session is already established — probing further back past the
            # published-holiday coverage floor (pre-2020) is only a CANDLE hunt,
            # not a calendar question, so a data gap near the 2020 floor yields
            # the honest no-anchor WARN + None (main-parity: this case returned
            # None before the walk-back) instead of the strict walk raising
            # HolidayDataUnavailableError out of a candle hunt.
            break
        if is_trading_day_checked_observed(d, observed=observed):
            if calendar_prev is None:
                calendar_prev = d
            candle = daily_candles_by_date.get(d)
            if candle is not None:
                winner = (d, candle)
                break
        d -= timedelta(days=1)

    if winner is None:
        _LOG.warning(
            PREV_DAY_NO_ANCHOR_EVENT,
            today=today.isoformat(),
            lookback_max=lookback_max,
        )
        return None

    used_day, used_candle = winner
    # ``calendar_prev`` is never None once a winner exists: the walk inspects only
    # sessions, so a winning candle implies the FIRST session (== calendar_prev)
    # was seen. The not-None term exists for type-narrowing, not as a guard.
    if calendar_prev is not None and used_day != calendar_prev:
        overlaid = any(
            (used_day + timedelta(days=offset)) in observed
            for offset in range(1, (today - used_day).days)
        )
        _LOG.warning(
            PREV_DAY_STALE_ANCHOR_EVENT,
            today=today.isoformat(),
            calendar_prev_day=calendar_prev.isoformat(),
            used_prev_day=used_day.isoformat(),
            gap_calendar_days=(calendar_prev - used_day).days,
            overlaid=overlaid,
        )

    return PrevDayOHLC(
        today=today,
        prev_day=used_day,
        pdo_paisa=used_candle.open_paisa,
        pdh_paisa=used_candle.high_paisa,
        pdl_paisa=used_candle.low_paisa,
        pdc_paisa=used_candle.close_paisa,
    )


def lookup_prev_day_ohlc_series(
    trading_days: list[date],
    daily_candles_by_date: dict[date, DailyCandle],
    *,
    observed: AbstractSet[date] = frozenset(),
) -> dict[date, PrevDayOHLC]:
    """Bulk variant — compute PrevDayOHLC for a series of trading days.

    Returns dict mapping today -> PrevDayOHLC. Skips days where lookup
    returns None (e.g., the very first day of the available history
    has no prior day in the dict) AND days the *observed* overlay removes —
    a caller feeding an overlay-unaware ``trading_days_between`` list must not
    crash mid-series on an overlaid member (it is simply not a session), so the
    skip mirrors the existing skip-None contract. (Honest note: this bulk
    variant currently has NO production consumers — it is a public convenience
    surface; the blast radius of the walk-back lives in
    :func:`lookup_prev_day_ohlc`.)
    """
    out: dict[date, PrevDayOHLC] = {}
    for today in trading_days:
        if today in observed:
            continue
        result = lookup_prev_day_ohlc(today, daily_candles_by_date, observed=observed)
        if result is not None:
            out[today] = result
    return out
