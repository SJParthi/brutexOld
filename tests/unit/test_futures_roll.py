"""Unit + property tests for the near-month NIFTY futures roll resolver (PR-3).

Covers the ADR-0012 D11 contract (``DEC-NIFTY-ROLL-METADATA-001``): per-contract
near-month resolution, holiday-shifted expiry (NSE rolls to the prior trading
day), the expiry-day "stay on the expiring contract" rule, the roll-boundary
``series_key`` reset token, and the gap-day rollover. 100% line + branch on
``brutex.data.futures_roll``.

Synthetic-fixture / calendar-driven — proves the LOGIC; the real-data run is on
the operator's machine.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from brutex.calendars.nse import is_trading_day
from brutex.data.futures_roll import (
    FuturesContract,
    holiday_shifted_expiry,
    resolve_active_future,
    resolve_active_future_expiry,
)
from brutex.options.expiry_calendar import next_monthly_expiry
from brutex.options.lot_size_history import OptionsUnderlying
from hypothesis import given
from hypothesis import strategies as st

NIFTY = OptionsUnderlying.NIFTY
BANKNIFTY = OptionsUnderlying.BANKNIFTY
SENSEX = OptionsUnderlying.SENSEX

#: A known clean monthly expiry: June 2025 last Thursday = 2025-06-26 (a trading day).
JUN_2025_EXPIRY = date(2025, 6, 26)
#: A known holiday-shifted expiry: Jan 2023 last Thursday = 2025-01-26 Republic Day
#: (holiday) → shifted to Wed 2023-01-25.
JAN_2023_CALENDAR = date(2023, 1, 26)
JAN_2023_SHIFTED = date(2023, 1, 25)


# ────── FuturesContract ──────


def test_series_key_is_stable_and_identifying() -> None:
    c = FuturesContract(underlying=NIFTY, expiry=JUN_2025_EXPIRY)
    assert c.series_key == "NIFTY-FUT-2025-06-26"
    # Same underlying + expiry → same key (per-contract identity).
    assert FuturesContract(underlying=NIFTY, expiry=JUN_2025_EXPIRY).series_key == c.series_key
    # Different expiry → different key (a roll boundary).
    other = FuturesContract(underlying=NIFTY, expiry=date(2025, 7, 31))
    assert other.series_key != c.series_key


def test_series_key_distinguishes_underlyings() -> None:
    assert (
        FuturesContract(NIFTY, JUN_2025_EXPIRY).series_key
        != FuturesContract(BANKNIFTY, JUN_2025_EXPIRY).series_key
    )


# ────── holiday_shifted_expiry ──────


def test_holiday_shift_noop_on_a_trading_day() -> None:
    assert is_trading_day(JUN_2025_EXPIRY)
    assert holiday_shifted_expiry(NIFTY, JUN_2025_EXPIRY) == JUN_2025_EXPIRY


def test_holiday_shift_to_prior_trading_day() -> None:
    # Jan 2023 last Thursday is Republic Day (holiday) → prior trading day Wed.
    assert not is_trading_day(JAN_2023_CALENDAR)
    assert holiday_shifted_expiry(NIFTY, JAN_2023_CALENDAR) == JAN_2023_SHIFTED


def test_holiday_shift_raises_when_no_trading_day_within_lookback(monkeypatch) -> None:
    # Force the prior-trading-day walk to find nothing (impossible holiday cluster).
    import brutex.data.futures_roll as mod

    monkeypatch.setattr(mod, "find_prev_trading_day", lambda _d: None)
    # Use a Sunday so the calendar_expiry is itself not a trading day.
    sunday = date(2025, 6, 22)
    assert not is_trading_day(sunday)
    with pytest.raises(ValueError, match="no trading day found before"):
        holiday_shifted_expiry(NIFTY, sunday)


# ────── resolve_active_future_expiry ──────


def test_near_month_before_expiry() -> None:
    # Early June → the June contract (last-Thursday 2025-06-26).
    assert resolve_active_future_expiry(NIFTY, date(2025, 6, 10)) == JUN_2025_EXPIRY


def test_on_expiry_day_returns_the_expiring_contract() -> None:
    # On expiry day itself the intraday trade STAYS on the expiring contract
    # (force-exit 15:20, no overnight roll) — NOT the next month.
    assert resolve_active_future_expiry(NIFTY, JUN_2025_EXPIRY) == JUN_2025_EXPIRY


def test_day_after_expiry_rolls_to_next_month() -> None:
    # The next session opens on the new near month.
    nxt = resolve_active_future_expiry(NIFTY, JUN_2025_EXPIRY + timedelta(days=1))
    assert nxt > JUN_2025_EXPIRY
    assert nxt.month == 7  # July contract


def test_holiday_shifted_near_month_is_returned() -> None:
    # Mid-Jan 2023 → the holiday-shifted Jan contract (Wed 2023-01-25).
    assert resolve_active_future_expiry(NIFTY, date(2023, 1, 10)) == JAN_2023_SHIFTED


def test_entry_on_shifted_expiry_day_returns_that_contract() -> None:
    # Entering on the actual (shifted) expiry → still that contract.
    assert resolve_active_future_expiry(NIFTY, JAN_2023_SHIFTED) == JAN_2023_SHIFTED


def test_gap_day_between_shifted_expiry_and_calendar_rolls_to_next(monkeypatch) -> None:
    # Construct the gap branch: on_date sits strictly AFTER the shifted expiry
    # but on/before the calendar last-<dow>. Force a 2-day holiday shift so the
    # gap contains a synthetic trading day we can query.
    import brutex.data.futures_roll as mod

    calendar = next_monthly_expiry(NIFTY, date(2025, 6, 1))  # 2025-06-26 (Thu)
    forced_shift = calendar - timedelta(days=3)  # pretend the contract last traded 3d earlier

    def fake_shift(underlying: OptionsUnderlying, ce: date) -> date:
        # Shift ONLY the June calendar expiry; pass others through honestly.
        if ce == calendar:
            return forced_shift
        return date(ce.year, ce.month, ce.day) if is_trading_day(ce) else ce

    monkeypatch.setattr(mod, "holiday_shifted_expiry", fake_shift)
    # on_date is in (forced_shift, calendar]: the June contract has "expired"
    # (shifted < on_date), so it must roll to July.
    on_date = calendar - timedelta(days=1)
    assert forced_shift < on_date <= calendar
    rolled = resolve_active_future_expiry(NIFTY, on_date)
    assert rolled.month == 7


# ────── resolve_active_future (wrapper) ──────


def test_resolve_active_future_returns_full_contract() -> None:
    c = resolve_active_future(NIFTY, date(2025, 6, 10))
    assert isinstance(c, FuturesContract)
    assert c.underlying is NIFTY
    assert c.expiry == JUN_2025_EXPIRY
    assert c.series_key == "NIFTY-FUT-2025-06-26"


# ────── Property invariants ──────

_DATE_MIN = date(2022, 1, 1)
_DATE_MAX = date(2026, 6, 30)


@given(st.dates(min_value=_DATE_MIN, max_value=_DATE_MAX))
def test_property_expiry_is_at_or_after_a_trading_entry(d: date) -> None:
    # For any TRADING entry date, the resolved expiry is >= the entry date
    # (the near month never resolves to an already-finished contract).
    if not is_trading_day(d):
        return
    assert resolve_active_future_expiry(NIFTY, d) >= d


@given(st.dates(min_value=_DATE_MIN, max_value=_DATE_MAX))
def test_property_resolved_expiry_is_always_a_trading_day(d: date) -> None:
    # The holiday-shifted expiry is the contract's ACTUAL last-trading date — so
    # it must itself be a trading day.
    if not is_trading_day(d):
        return
    assert is_trading_day(resolve_active_future_expiry(NIFTY, d))


@given(
    st.sampled_from([NIFTY, BANKNIFTY, SENSEX]),
    st.dates(min_value=_DATE_MIN, max_value=_DATE_MAX),
)
def test_property_idempotent(underlying: OptionsUnderlying, d: date) -> None:
    # Same inputs → same output, bit-for-bit (Golden Rule #7).
    if not is_trading_day(d):
        return
    assert resolve_active_future_expiry(underlying, d) == resolve_active_future_expiry(
        underlying, d
    )
