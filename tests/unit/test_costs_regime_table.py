"""Tests for the COMMON date-regime lookup mechanism (R2-OC-3).

Covers ``brutex.costs.constants.rate_for_date`` — the ONE shared regime
mechanism the STT options / STT exercise / NSE ETC / BSE ETC lookups ride —
plus the honesty contract of the ETC tables: only the CURRENT (effective
2024-10-01) rates are verified, so the tables deliberately return the same
current rate for EVERY date via an explicit UNVERIFIED placeholder row
(behaviour byte-identical to the previous date-blind lookup).

The synthetic-table tests below exercise the MECHANISM with fabricated rates
— they are mechanism-tests ONLY, never a claim about any real historical
NSE/BSE rate (Golden Rule #1).
"""

from __future__ import annotations

from datetime import date

import pytest
from brutex.costs import constants
from hypothesis import given, settings
from hypothesis import strategies as st

# ── rate_for_date mechanism (synthetic tables — NOT real rates) ──────────────

#: Fabricated 2-regime table proving the mechanism switches on the boundary.
#: The 111/222 rates are SYNTHETIC — a mechanism fixture, not a market fact.
_SYNTHETIC_REGIMES: tuple[tuple[date, int], ...] = (
    (date(2021, 1, 1), 111),
    (date(2024, 10, 1), 222),
)


def test_mechanism_picks_first_regime_before_boundary() -> None:
    assert constants.rate_for_date(_SYNTHETIC_REGIMES, date(2024, 9, 30)) == 111


def test_mechanism_switches_exactly_on_boundary() -> None:
    assert constants.rate_for_date(_SYNTHETIC_REGIMES, date(2024, 10, 1)) == 222


def test_mechanism_holds_after_boundary() -> None:
    assert constants.rate_for_date(_SYNTHETIC_REGIMES, date(2030, 1, 1)) == 222


def test_mechanism_first_row_start_is_inclusive() -> None:
    assert constants.rate_for_date(_SYNTHETIC_REGIMES, date(2021, 1, 1)) == 111


def test_mechanism_rejects_date_before_history() -> None:
    """No rate is ever fabricated for a date outside the recorded history."""
    with pytest.raises(ValueError, match="precedes the first recorded regime"):
        constants.rate_for_date(_SYNTHETIC_REGIMES, date(2020, 12, 31))


def test_mechanism_rejects_empty_table() -> None:
    with pytest.raises(ValueError, match="empty regime table"):
        constants.rate_for_date((), date(2024, 1, 1))


# ── STT refactor byte-identity (the COMMON mechanism must not move STT) ─────


@pytest.mark.parametrize(
    ("on_date", "expected"),
    [
        (date(2020, 1, 1), constants.STT_OPTIONS_PRE_OCT_2024_BPS_X100),
        (date(2024, 9, 30), constants.STT_OPTIONS_PRE_OCT_2024_BPS_X100),
        (date(2024, 10, 1), constants.STT_OPTIONS_OCT_2024_TO_MAR_2026_BPS_X100),
        (date(2026, 3, 31), constants.STT_OPTIONS_OCT_2024_TO_MAR_2026_BPS_X100),
        (date(2026, 4, 1), constants.STT_OPTIONS_POST_APR_2026_BPS_X100),
        (date(2030, 12, 31), constants.STT_OPTIONS_POST_APR_2026_BPS_X100),
    ],
)
def test_stt_options_regimes_byte_identical_to_pre_refactor(on_date: date, expected: int) -> None:
    assert constants.stt_options_rate_bps_x100(on_date) == expected


@pytest.mark.parametrize(
    ("on_date", "expected"),
    [
        (date(2020, 1, 1), constants.STT_EXERCISE_PRE_APR_2026_BPS_X100),
        (date(2026, 3, 31), constants.STT_EXERCISE_PRE_APR_2026_BPS_X100),
        (date(2026, 4, 1), constants.STT_EXERCISE_POST_APR_2026_BPS_X100),
    ],
)
def test_stt_exercise_regimes_byte_identical_to_pre_refactor(on_date: date, expected: int) -> None:
    assert constants.stt_exercise_rate_bps_x100(on_date) == expected


# ── ETC honesty contract: same verified rate for ANY date (today) ────────────


@pytest.mark.property
@settings(max_examples=200, deadline=None)
@given(
    on_date=st.dates(min_value=date(2020, 1, 1), max_value=date(2030, 12, 31)),
)
def test_etc_returns_current_verified_rate_for_any_date(on_date: date) -> None:
    """Pre-boundary ETC rates are UNVERIFIED, so the placeholder row keeps the
    CURRENT rate for every date — behaviour byte-identical to the previous
    date-blind lookup. This property test pins that contract; it FAILS the day
    a verified historical rate lands (forcing a conscious update here)."""
    assert (
        constants.exchange_charge_bps_x100("NIFTY", on_date) == constants.NSE_ETC_OPTIONS_BPS_X100
    )
    assert (
        constants.exchange_charge_bps_x100("BANKNIFTY", on_date)
        == constants.NSE_ETC_OPTIONS_BPS_X100
    )
    assert (
        constants.exchange_charge_bps_x100("SENSEX", on_date) == constants.BSE_ETC_OPTIONS_BPS_X100
    )


def test_etc_boundary_date_constant_is_oct_2024() -> None:
    """The verified effective date of the CURRENT rates (NSE FA64232 / BSE
    notice 27-Sep-2024, per COSTS_VERIFIED §2/§15)."""
    assert date(2024, 10, 1) == constants.ETC_REGIME_OCT_2024_START
