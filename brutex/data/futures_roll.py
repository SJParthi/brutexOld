"""Near-month NIFTY futures contract roll resolution (ADR-0012 D11).

Per ADR-0012 D11 (``DEC-NIFTY-ROLL-METADATA-001``, builds on
``DEC-NIFTY-DYNAMIC-STRATEGY-001``) the brute-force engine runs on a
**per-contract** NIFTY futures series — NOT a back-adjusted continuous
contract. Back-adjustment shifts every absolute price level and would corrupt
pivots / Fibonacci / every price-anchored level versus what a trader actually
saw. Each monthly
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
  per-contract (the roll boundary is a hard reset — the Stage-1A
  ``session_key`` roll-reset mechanism of D11/D12, which outlived its
  original ``VwapState`` consumer, retired with the bits-52/53 tombstone).

## Holiday-shifted expiry (NSE rule)

The expiry calendar (:mod:`brutex.options.expiry_calendar`) returns the
*calendar* monthly expiry (e.g. "last Thursday of the month") and explicitly
does NOT apply holiday shifts. NSE rolls a contract whose calendar expiry falls
on a trading holiday to the **prior trading day**. :func:`resolve_active_future`
applies that shift here via :func:`brutex.indicators.prev_day_ohlc.find_prev_trading_day`
(the one COMMON prior-trading-day walker), so the resolved expiry is the date the
contract ACTUALLY last traded.

## Rust-vs-Python split (the near-month DECISION is RUST)

Per ``DEC-NIFTY-ROLL-METADATA-001`` TickVault trades the near-month future LIVE,
so the at-or-after near-month roll **DECISION** must be shared, byte-identical,
between the backtest and the live engine. That decision —
"is the candidate (holiday-shifted) expiry at-or-after the entry date (so it is
the active near month, including on the expiry date itself), or has the holiday
shift pulled it before the entry (so roll to next month)?" — lives in the shared
Rust ``trading-core`` crate (``rust/trading-core/src/indicators/metadata.rs``,
exposed via PyO3 as ``trading_core.resolve_near_month``). THIS module is the
thin **delegating wrapper**: it computes the CANDIDATE + next-month
holiday-shifted expiries from the BruteX-resident NSE calendar (the holiday list,
the prior-trading-day walk, the monthly-expiry rule are DATA, not a kernel) and
hands their day-ordinals to the Rust decision.

What stays Python: the NSE/BSE holiday list + the prior-trading-day walk
(:func:`brutex.indicators.prev_day_ohlc.find_prev_trading_day`) + the
monthly-expiry calendar (:func:`brutex.options.expiry_calendar.next_monthly_expiry`)
— all BruteX-resident data the Rust decision is parameterised by.

## COMMON · DYNAMIC · SCALABLE · AUTOMATED

- COMMON: ONE source of truth for the near-month DECISION — the Rust
  ``trading-core`` crate, shared with TickVault; reuses the shared expiry
  calendar + the shared prior-trading-day walker for the candidate (no copy).
- DYNAMIC: a new SEBI monthly-expiry regime is one row in
  :mod:`brutex.options.expiry_calendar`; this module inherits it for free.
- SCALABLE: O(1) per resolution (one expiry lookup + a bounded holiday walk +
  one Rust FFI call).
- AUTOMATED: pure function, no I/O, fully deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import trading_core

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
        this key is a roll boundary, so no session/level indicator (pivots /
        Fib / EMA / market structure — historically the since-retired VWAP
        too) ever spans two contracts.
        Format: ``"<UNDERLYING>-FUT-<YYYY-MM-DD expiry>"``.
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
    brutex.calendars.nse.HolidayDataUnavailableError
        If *calendar_expiry* falls in an out-of-coverage year (2027+) on a
        weekday whose trading-day verdict depends on the (missing) holiday list.
        The strict :func:`is_trading_day_checked` REFUSES to guess rather than
        silently treat an unknown holiday as a trading day and return the expiry
        unshifted — symmetric with the PDH/PDL anchor path, which already uses
        the checked variant.
    """
    from brutex.calendars.nse import is_trading_day_checked

    del underlying  # reserved for a future per-underlying nuance (see docstring)
    if is_trading_day_checked(calendar_expiry):
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
    holiday-shifted. The roll-to-next-month branch (the holiday shift pulling the
    shifted expiry to a date **before** *on_date*) is **defensive-only**: under
    the monthly last-``<dow>`` rule the calendar expiry is always computed at or
    after *on_date*, so a single-day prior-trading-day shift can never land
    strictly before it — the gap branch has zero real occurrences. It is RETAINED
    for a future weekly / Monday-expiry regime, where a multi-day shift across a
    holiday cluster could open such a gap; the Rust decision then resolves the
    NEXT month's contract — the correct near month for that gap.

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
    candidate_shifted = holiday_shifted_expiry(underlying, calendar_expiry)
    # Compute the NEXT month's holiday-shifted expiry eagerly (cheap) so the Rust
    # decision is a pure function with no callback: it is used only when the
    # candidate has already expired (the gap-day branch). The next month is
    # resolved from the day after the calendar expiry (re-evaluates the dow +
    # holiday shift for the rolled month).
    next_calendar = next_monthly_expiry(underlying, calendar_expiry + timedelta(days=1))
    next_shifted = holiday_shifted_expiry(underlying, next_calendar)
    # Delegate the at-or-after near-month DECISION to the shared Rust core so it
    # is byte-identical to the TickVault-native live path
    # (DEC-NIFTY-ROLL-METADATA-001). Day-ordinals in, day-ordinal out.
    active_ordinal = int(
        trading_core.resolve_near_month(
            on_date.toordinal(),
            candidate_shifted.toordinal(),
            next_shifted.toordinal(),
        )
    )
    return date.fromordinal(active_ordinal)


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
