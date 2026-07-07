"""Per-session classification lenses for the results-depth pass (R46/R51).

DEC-SWEEP-RESULTS-DEPTH-001 (WP2). One :class:`SessionLens` is built per sweep
tuple from the tuple's OWN daily bars and consumed by
:mod:`brutex.engine.combo_aggregates` to attribute every captured trade to

* a **trend regime** (R46 "trending-vs-sideways") — a fold of the existing
  look-ahead-clean :class:`~brutex.indicators.market_structure.MarketStructureState`
  (``k = 2``, the production 5-bar fractal) over the daily closes: after
  feeding day ``D``'s bar the machine's :class:`~brutex.indicators.market_structure.Trend`
  is the label for ``D``, and by the module's own confirmation law that state
  used ONLY bars ``<= D`` — appending future sessions can never change an
  already-labelled date (property-pinned);
* **event-day flags** (R51) — ONLY what the repo's calendars verifiably know
  (Golden Rule #1): derivative **expiry** day for the tuple's instrument
  (:mod:`brutex.options.expiry_calendar` regimes + the holiday shift of
  :func:`brutex.data.futures_roll.holiday_shifted_expiry`), the **muhurat**
  session (:func:`brutex.calendars.nse.has_muhurat_session`), the **Budget
  special weekend session** (:func:`brutex.calendars.nse.is_special_weekend_session`)
  and the **NIFTY 50 rebalance** effective date
  (:func:`brutex.calendars.nse.is_nifty_rebalance_day` — an index-composition
  market event, flagged for every instrument). Weekday Budget days and
  election days are tagged NOWHERE in the repo and are honestly OMITTED —
  no invented event lists (R51's "elections" is out until a verified
  calendar lands);
* an **entry hour** (IST wall clock) via :func:`entry_hour_ist` — the ONE
  UTC→IST conversion boundary for the lens surface — and a trade date via
  :func:`ist_trade_date`;
* a **VIX band** label via the pinned :data:`VIX_BANDS` table (R31/R46).

## Honesty envelope (Golden Rule #1 / #10)

* The trend label is a designed HEURISTIC (a market-structure fold over daily
  closes), deterministic + look-ahead-clean, used for display/aggregation
  ONLY — never a ranking gate (design F.8).
* Expiry flags ABSTAIN (flag simply absent) when the calendar cannot answer:
  an instrument outside the 3 Track-2 underlyings, a date before the encoded
  2020 regime history, or an out-of-holiday-coverage year — counted and
  logged once per build (``session_lens.expiry_abstained``), never guessed.
* Every abstention leaves the OTHER flags intact (they are independent
  set lookups covering any date).

## O() envelope

``build_session_lens`` is one O(N_sessions) pass (the market-structure update
is O(1)/bar; each expiry probe is O(log regimes) + an O(holiday-walk-bounded)
shift); every lens lookup thereafter is an O(1) dict hit. Pure and
deterministic: no I/O, no wall clock, no RNG.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from math import inf, isfinite
from typing import TYPE_CHECKING, Final

import structlog

from brutex.calendars.nse import (
    IST,
    HolidayDataUnavailableError,
    has_muhurat_session,
    is_nifty_rebalance_day,
    is_special_weekend_session,
)
from brutex.engine.exit_index import bars_strictly_increasing
from brutex.indicators.market_structure import MarketStructureState, Trend
from brutex.models import BruteXError
from brutex.money import rupees_to_paisa
from brutex.options.expiry_calendar import next_monthly_expiry, next_weekly_expiry
from brutex.options.lot_size_history import OptionsUnderlying

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from collections.abc import Mapping, Sequence
    from datetime import date
    from decimal import Decimal

    from brutex.data.models import CandleBar

_LOG = structlog.get_logger("brutex.engine.session_lens")

__all__ = [
    "EVENT_BUDGET_WEEKEND",
    "EVENT_EXPIRY",
    "EVENT_FLAGS",
    "EVENT_MUHURAT",
    "EVENT_NIFTY_REBALANCE",
    "VIX_BANDS",
    "SessionLens",
    "SessionLensError",
    "TrendRegime",
    "build_session_lens",
    "entry_hour_ist",
    "ist_trade_date",
    "vix_band_label",
]


class SessionLensError(BruteXError):
    """Raised on malformed lens input (unsorted bars, naive timestamps, bad VIX)."""


class TrendRegime(Enum):
    """The per-session trend label (R46's "trending-vs-sideways" axis).

    Mapped 1:1 from the market-structure machine's
    :class:`~brutex.indicators.market_structure.Trend`: ``UP``/``DOWN`` are its
    confirmed trends; ``SIDEWAYS`` is its ``NONE`` (no confirmed structure yet
    — warm-up sessions therefore read SIDEWAYS, honestly).
    """

    UP = "up"
    DOWN = "down"
    SIDEWAYS = "sideways"


#: Verified event-day flag tokens (R51) — the COMPLETE set; pinned order is the
#: per-date emission order. Sources: expiry = the options expiry-calendar
#: regimes + NSE holiday shift; muhurat = ``MUHURAT_SESSIONS``; budget_weekend
#: = ``SPECIAL_WEEKEND_TRADING_SESSIONS``; nifty_rebalance =
#: ``NIFTY50_REBALANCE_EVENTS``. Elections/weekday-Budget: NOT tagged anywhere
#: in the repo — honestly omitted.
EVENT_EXPIRY: Final[str] = "expiry"
EVENT_MUHURAT: Final[str] = "muhurat"
EVENT_BUDGET_WEEKEND: Final[str] = "budget_weekend"
EVENT_NIFTY_REBALANCE: Final[str] = "nifty_rebalance"
EVENT_FLAGS: Final[tuple[str, ...]] = (
    EVENT_EXPIRY,
    EVENT_MUHURAT,
    EVENT_BUDGET_WEEKEND,
    EVENT_NIFTY_REBALANCE,
)

#: The pinned VIX regime bands (R31/R46): ``(label, lo, hi)`` half-open
#: ``[lo, hi)`` intervals covering ``[0, +inf)`` — every finite non-negative
#: VIX level lands in exactly one band. Labels are display tokens (P4 renders
#: them verbatim); edges are index levels, NOT money (never rounded).
VIX_BANDS: Final[tuple[tuple[str, float, float], ...]] = (
    ("<12", 0.0, 12.0),
    ("12-15", 12.0, 15.0),
    ("15-20", 15.0, 20.0),
    ("20-25", 20.0, 25.0),
    ("25-30", 25.0, 30.0),
    (">30", 30.0, inf),
)

#: The production fractal half-window for the daily trend fold — the same
#: ``k = 2`` (5-bar fractal) the vocabulary's BOS/CHoCH bits use.
_TREND_FRACTAL_K: Final[int] = 2

#: Trend-enum → regime-label map (module-level so the fold is a pure lookup).
_TREND_TO_REGIME: Final[dict[Trend, TrendRegime]] = {
    Trend.NONE: TrendRegime.SIDEWAYS,
    Trend.UP: TrendRegime.UP,
    Trend.DOWN: TrendRegime.DOWN,
}


@dataclass(frozen=True, slots=True)
class SessionLens:
    """Per-session-date classification for one tuple's window.

    ``trend_by_date`` labels every session that had a daily bar;
    ``event_flags_by_date`` carries, per date, the subset of
    :data:`EVENT_FLAGS` that verifiably applies (in :data:`EVENT_FLAGS` order;
    dates with zero flags are ABSENT from the map — a lookup miss means "no
    verified event", never an error).
    """

    trend_by_date: Mapping[date, TrendRegime]
    event_flags_by_date: Mapping[date, tuple[str, ...]]


def ist_trade_date(ts_utc: datetime) -> date:
    """Return the IST calendar date of a tz-aware instant — the ONE session key.

    The lake stores UTC; every per-trade date attribution (year buckets, trend
    lookup, event flags) goes through this single conversion boundary
    (CLAUDE.md §11 COMMON — the ``data/session.py`` display-boundary law).

    Raises
    ------
    SessionLensError
        If *ts_utc* is naive (the models-layer tz-aware law, re-asserted here
        because a naive datetime would silently shift the session date).
    """
    if ts_utc.tzinfo is None:
        raise SessionLensError(f"ist_trade_date: naive datetime {ts_utc!r} (tz-aware required)")
    return ts_utc.astimezone(IST).date()


def entry_hour_ist(ts_utc: datetime) -> int:
    """Return the IST wall-clock HOUR of a tz-aware instant (R51 time-of-day lens).

    ``0..23``; a regular NSE session yields ``9..15``. Shares
    :func:`ist_trade_date`'s single-conversion-boundary law.

    Raises
    ------
    SessionLensError
        If *ts_utc* is naive.
    """
    if ts_utc.tzinfo is None:
        raise SessionLensError(f"entry_hour_ist: naive datetime {ts_utc!r} (tz-aware required)")
    return ts_utc.astimezone(IST).hour


def vix_band_label(vix: Decimal) -> str:
    """Map a stamped India-VIX level to its pinned :data:`VIX_BANDS` label.

    Pure O(bands) lookup over the frozen table — a label, never generated
    text (the R33 no-hallucination mechanism). The input is the trade's
    full-precision ``vix_entry`` stamp (Decimal, never rounded).

    Raises
    ------
    SessionLensError
        If *vix* is negative or non-finite (a VIX level is a non-negative
        index value; anything else is corrupt input, refused loudly).
    """
    level = float(vix)
    if not isfinite(level) or level < 0.0:
        raise SessionLensError(f"vix_band_label: VIX level must be finite and >= 0, got {vix!r}")
    for label, lo_edge, hi_edge in VIX_BANDS[:-1]:
        if lo_edge <= level < hi_edge:
            return label
    return VIX_BANDS[-1][0]


def _expiry_underlying(instrument: str) -> OptionsUnderlying | None:
    """Resolve the instrument token to its expiry-calendar underlying, or ``None``.

    Exact, case-sensitive match against the 3 Track-2 underlyings (the sweep's
    canonical uppercase instrument tokens). Anything else — INDIAVIX, a stock,
    a futures series key — has NO encoded expiry calendar here, so the expiry
    flag ABSTAINS for the whole lens (honest omission, logged once).
    """
    try:
        return OptionsUnderlying(instrument)
    except ValueError:
        return None


def _is_expiry_day(underlying: OptionsUnderlying, day: date) -> bool | None:
    """Return whether *day* is the ACTUAL (holiday-shifted) expiry day, or abstain.

    A date is an expiry day iff the underlying's next WEEKLY calendar expiry
    (when the regime has one — SEBI killed some) or next MONTHLY calendar
    expiry, holiday-shifted per the NSE prior-trading-day rule, lands exactly
    on *day*. Computing the calendar expiry AT-OR-AFTER *day* and shifting
    BACK covers the "calendar Thursday is a holiday → actual expiry is this
    Wednesday" case exactly.

    Returns ``None`` (ABSTAIN — the caller omits the flag and counts it) when
    the calendars cannot answer: a pre-2020 date (before the encoded expiry
    regime history — ``ValueError``) or an out-of-holiday-coverage year
    (``HolidayDataUnavailableError`` from the checked trading-day probe;
    Golden Rule #1 — never guess a holiday).
    """
    # Lazy import: futures_roll imports the trading_core wheel at module level;
    # deferring keeps this module import-light (the repo's PLC0415-sanctioned
    # lazy-import idiom) — the wheel is only needed once a lens is BUILT for a
    # calendar-covered instrument.
    from brutex.data.futures_roll import holiday_shifted_expiry

    try:
        weekly = next_weekly_expiry(underlying, day)
        if weekly is not None and holiday_shifted_expiry(underlying, weekly) == day:
            return True
        monthly = next_monthly_expiry(underlying, day)
    except (HolidayDataUnavailableError, ValueError):
        return None
    try:
        return holiday_shifted_expiry(underlying, monthly) == day
    except (HolidayDataUnavailableError, ValueError):
        return None


def _event_flags(day: date, underlying: OptionsUnderlying | None) -> tuple[tuple[str, ...], bool]:
    """Resolve *day*'s verified event flags (in :data:`EVENT_FLAGS` order).

    Returns ``(flags, expiry_abstained)`` — the second element is ``True``
    when the expiry probe abstained for this date (counted by the builder).
    """
    flags: list[str] = []
    abstained = False
    if underlying is not None:
        verdict = _is_expiry_day(underlying, day)
        if verdict is None:
            abstained = True
        elif verdict:
            flags.append(EVENT_EXPIRY)
    if has_muhurat_session(day):
        flags.append(EVENT_MUHURAT)
    if is_special_weekend_session(day):
        flags.append(EVENT_BUDGET_WEEKEND)
    if is_nifty_rebalance_day(day):
        flags.append(EVENT_NIFTY_REBALANCE)
    return tuple(flags), abstained


def build_session_lens(daily_bars: Sequence[CandleBar], *, instrument: str) -> SessionLens:
    """Build the per-session lens from a tuple's OWN daily bars (R46/R51).

    Parameters
    ----------
    daily_bars
        The instrument's derived-1day bars for the sweep window,
        chronologically ordered (strictly increasing timestamps — the lake
        read contract; shuffled input is REJECTED, not silently re-sorted:
        the trend fold is order-sensitive and a silent fix would hide poison)
        with exactly one bar per IST session. Empty is first-class: an empty
        lens (every downstream lookup misses → "unknown" trend, no flags).
    instrument
        The tuple's instrument token (e.g. ``"NIFTY"``). Drives ONLY the
        expiry flag; an instrument outside the 3 Track-2 underlyings makes
        the expiry flag abstain lens-wide (logged once), all other flags and
        the trend fold apply to any instrument.

    Returns
    -------
    SessionLens
        Trend label + event flags per session date.

    Raises
    ------
    SessionLensError
        On non-strictly-increasing timestamps or two bars sharing one IST
        session date (not a one-bar-per-session daily series).
    """
    if not bars_strictly_increasing(daily_bars):
        raise SessionLensError(
            "build_session_lens: daily bars must be strictly increasing in timestamp "
            "(the lake read contract — never silently re-sorted)"
        )
    underlying = _expiry_underlying(instrument)
    if underlying is None and len(daily_bars) > 0:
        _LOG.info(
            "session_lens.expiry_unmapped_instrument",
            instrument=instrument,
            note="no encoded expiry calendar — expiry flag abstains lens-wide",
        )
    state = MarketStructureState(k=_TREND_FRACTAL_K)
    trend_by_date: dict[date, TrendRegime] = {}
    event_flags_by_date: dict[date, tuple[str, ...]] = {}
    expiry_abstained = 0
    first_abstained: date | None = None
    for bar in daily_bars:
        day = ist_trade_date(bar.timestamp)
        if day in trend_by_date:
            raise SessionLensError(
                f"build_session_lens: two daily bars share IST session date {day.isoformat()} "
                "(a daily series carries exactly one bar per session)"
            )
        state.update(
            rupees_to_paisa(bar.high), rupees_to_paisa(bar.low), rupees_to_paisa(bar.close)
        )
        trend_by_date[day] = _TREND_TO_REGIME[state.trend]
        flags, abstained = _event_flags(day, underlying)
        if abstained:
            expiry_abstained += 1
            if first_abstained is None:
                first_abstained = day
        if flags:
            event_flags_by_date[day] = flags
    if expiry_abstained:
        _LOG.info(
            "session_lens.expiry_abstained",
            instrument=instrument,
            sessions=expiry_abstained,
            first_date=None if first_abstained is None else first_abstained.isoformat(),
            note="expiry calendar/holiday coverage cannot answer — flag omitted, never guessed",
        )
    return SessionLens(trend_by_date=trend_by_date, event_flags_by_date=event_flags_by_date)
