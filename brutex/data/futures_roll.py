"""Near-month NIFTY futures contract roll resolution (ADR-0012 D11).

Per ADR-0012 D11 (``DEC-NIFTY-ROLL-METADATA-001``, builds on
``DEC-NIFTY-DYNAMIC-STRATEGY-001``) the brute-force engine runs on a
**per-contract** NIFTY futures series — NOT a back-adjusted continuous
contract. Back-adjustment shifts every absolute price level and would corrupt
pivots / Fibonacci / VWAP versus what a trader actually saw. Each monthly
contract is its own series; this module resolves *which* contract a trade
entering on a given IST date would use.

## The roll policy (D11, locked)

* The active contract on an entry date is the **near month** — the monthly
  future whose (holiday-shifted) expiry is **at or after** the entry date.
* On **expiry day itself** an intraday trade stays on the **expiring** contract
  and force-exits at 15:20 IST (per D5 — nothing held overnight, no overnight
  roll). So :func:`resolve_active_future` on the expiry date STILL returns the
  expiring contract — the trade lives entirely inside that contract's final
  session.
* The **next** trading session opens on the **new** near month (the expiring
  contract is gone), so the front→next-month basis gap (typically 20-60 pts) is
  **never inside a trade** and is never treated as a price move. There is no
  cross-roll level comparison — every session/level indicator recomputes
  per-contract (the roll boundary is a hard reset, mirroring the VWAP
  ``session_key`` reset of D12).

## Holiday-shifted expiry (NSE rule)

The expiry calendar (:mod:`brutex.options.expiry_calendar`) returns the
*calendar* monthly expiry (e.g. "last Thursday of the month") and explicitly
does NOT apply holiday shifts. NSE rolls a contract whose calendar expiry falls
on a trading holiday to the **prior trading day**. :func:`resolve_active_future`
applies that shift here via :func:`brutex.indicators.prev_day_ohlc.find_prev_trading_day`
(the one COMMON prior-trading-day walker), so the resolved expiry is the date the
contract ACTUALLY last traded.

## Why Python, not Rust

This is **pull / orchestration** logic (a date → contract mapping evaluated once
per trading session, NOT once per bar in the mask-AND sweep hot loop). It does
no numeric kernel work, so the COMMON·DYNAMIC·SCALABLE rules are met by a pure,
deterministic Python function — there is no hot-path justification for the Rust
``trading-core`` crate (the per-bar stamping in :mod:`brutex.feed.bar_metadata`
is the O(1) part, and even that is a trivial integer/date stamp, not a kernel).

## COMMON · DYNAMIC · SCALABLE · AUTOMATED

- COMMON: one source of truth for the futures near-month roll; reuses the
  shared expiry calendar + the shared prior-trading-day walker (no copy).
- DYNAMIC: a new SEBI monthly-expiry regime is one row in
  :mod:`brutex.options.expiry_calendar`; this module inherits it for free.
- SCALABLE: O(1) per resolution (one expiry lookup + a bounded holiday walk).
- AUTOMATED: pure function, no I/O, fully deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from brutex.indicators.prev_day_ohlc import find_prev_trading_day
from brutex.options.expiry_calendar import next_monthly_expiry
from brutex.options.lot_size_history import OptionsUnderlying

__all__ = [
    "FuturesContract",
    "holiday_shifted_expiry",
    "resolve_active_future",
    "resolve_active_future_expiry",
]


@dataclass(frozen=True, slots=True)
class FuturesContract:
    """One monthly NIFTY futures contract (a per-contract series).

    Parameters
    ----------
    underlying
        The futures underlying (NIFTY for this track; the three Track-2
        underlyings are all addressable for COMMON reuse).
    expiry
        The contract's ACTUAL last-trading date (the holiday-SHIFTED monthly
        expiry — see :func:`holiday_shifted_expiry`). This is the per-contract
        series identity: two trades on the same calendar day resolve to the same
        ``expiry`` iff they are the same contract.
    """

    underlying: OptionsUnderlying
    expiry: date

    @property
    def series_key(self) -> str:
        """A stable, hashable identity for the per-contract series.

        Used as the engine ``session_key`` reset token (D11/D12): a change of
        this key is a roll boundary, so VWAP / pivots / Fib never span two
        contracts. Format: ``"<UNDERLYING>-FUT-<YYYY-MM-DD expiry>"``.
        """
        return f"{self.underlying.value}-FUT-{self.expiry.isoformat()}"


def holiday_shifted_expiry(underlying: OptionsUnderlying, calendar_expiry: date) -> date:
    """Shift a *calendar* monthly expiry to the contract's ACTUAL last trading day.

    The expiry calendar returns the calendar expiry ("last Thursday of the
    month") without holiday handling. NSE rolls a contract whose calendar expiry
    is a trading holiday (or a weekend, defensively) to the **prior trading
    day**. This applies that shift.

    Parameters
    ----------
    underlying
        Unused today (the NSE prior-trading-day rule is exchange-wide, not
        per-underlying) but kept in the signature so a future per-underlying
        nuance is a non-breaking change — DYNAMIC.
    calendar_expiry
        The calendar monthly expiry from
        :func:`brutex.options.expiry_calendar.next_monthly_expiry`.

    Returns
    -------
    date
        ``calendar_expiry`` itself if it is a trading day, otherwise the most
        recent trading day strictly before it.

    Raises
    ------
    ValueError
        If no trading day is found within the holiday-walk lookback (a defensive
        guard against an impossibly long holiday cluster — never expected for a
        real NSE month).
    """
    from brutex.calendars.nse import is_trading_day

    del underlying  # reserved for a future per-underlying nuance (see docstring)
    if is_trading_day(calendar_expiry):
        return calendar_expiry
    shifted = find_prev_trading_day(calendar_expiry)
    if shifted is None:
        raise ValueError(
            f"no trading day found before calendar expiry {calendar_expiry} "
            "within the holiday-walk lookback"
        )
    return shifted


def resolve_active_future_expiry(underlying: OptionsUnderlying, on_date: date) -> date:
    """Resolve the active near-month futures EXPIRY a trade entering on *on_date* uses.

    Per D11 the near month is the monthly future whose holiday-shifted expiry is
    **at or after** *on_date*. On the expiry date itself this returns the
    EXPIRING contract's expiry (the intraday trade stays on it and force-exits at
    15:20 — no overnight roll); the next session resolves the new near month.

    The calendar's monthly expiry is computed AT OR AFTER *on_date*, then
    holiday-shifted. If the holiday shift pulls the shifted expiry to a date
    **before** *on_date* (i.e. *on_date* sits in the 1-2 day gap between a
    holiday-shifted expiry and the original calendar last-<dow>), the contract
    has already expired, so the NEXT month's contract is resolved instead — the
    correct near month for that gap.

    Parameters
    ----------
    underlying
        The futures underlying.
    on_date
        The trade's entry date (IST trading day).

    Returns
    -------
    date
        The active contract's ACTUAL (holiday-shifted) expiry date,
        ``>= on_date``.
    """
    calendar_expiry = next_monthly_expiry(underlying, on_date)
    shifted = holiday_shifted_expiry(underlying, calendar_expiry)
    if shifted >= on_date:
        return shifted
    # The holiday shift moved the expiry before on_date: on_date is in the gap
    # between the shifted expiry and the calendar last-<dow>. The near contract
    # has already expired — resolve next month from the day after the calendar
    # expiry (re-evaluates the dow + holiday shift for the rolled month).
    from datetime import timedelta

    next_calendar = next_monthly_expiry(underlying, calendar_expiry + timedelta(days=1))
    return holiday_shifted_expiry(underlying, next_calendar)


def resolve_active_future(underlying: OptionsUnderlying, on_date: date) -> FuturesContract:
    """Resolve the active near-month :class:`FuturesContract` for an entry date.

    Thin wrapper over :func:`resolve_active_future_expiry` returning the full
    per-contract identity (the ``series_key`` the engine uses as its roll-reset
    ``session_key``). See :func:`resolve_active_future_expiry` for the policy.

    Parameters
    ----------
    underlying
        The futures underlying.
    on_date
        The trade's entry date (IST trading day).

    Returns
    -------
    FuturesContract
        The active near-month contract a trade entering on *on_date* uses.
    """
    return FuturesContract(
        underlying=underlying,
        expiry=resolve_active_future_expiry(underlying, on_date),
    )
