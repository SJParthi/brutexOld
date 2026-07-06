"""Tests for the COMMON date-regime lookup mechanism (R2-OC-3 + DEC-ETC-REGIME-HISTORY-001).

Covers ``brutex.costs.constants.rate_for_date`` — the ONE shared regime
mechanism the STT options / STT exercise / NSE ETC / BSE ETC lookups ride —
plus the honesty contract of the ETC tables: only the CURRENT (effective
2024-10-01) rates are verified, so every pre-boundary date lands on an
explicit rate-less REFUSAL row and raises ``UnverifiedCostRegimeError``
(DEC-ETC-REGIME-HISTORY-001, MIGRATION.md #1524) — never a fabricated or
silently-current rate.

The synthetic-table tests below exercise the MECHANISM with fabricated rates
— they are mechanism-tests ONLY, never a claim about any real historical
NSE/BSE rate (Golden Rule #1).
"""
# Whitebox pins on the shipped private tables:
# pyright: reportPrivateUsage=none

from __future__ import annotations

from datetime import date

import pytest
from brutex.costs import constants
from brutex.costs.constants import RegimeRow, validate_regime_table
from brutex.costs.types import UnverifiedCostRegimeError
from hypothesis import given, settings
from hypothesis import strategies as st

# ── rate_for_date mechanism (synthetic tables — NOT real rates) ──────────────

#: Fabricated 2-regime table proving the mechanism switches on the boundary.
#: The 111/222 rates are SYNTHETIC — a mechanism fixture, not a market fact.
_SYNTHETIC_REGIMES: tuple[RegimeRow, ...] = (
    RegimeRow(date(2021, 1, 1), 111, "synthetic fixture row 1"),
    RegimeRow(date(2024, 10, 1), 222, "synthetic fixture row 2"),
)

#: Synthetic table with a MIDDLE refusal row (None-rate) followed by a rated
#: row — exercises the verified_from = next-row-start branch.
_SYNTHETIC_REFUSAL_MIDDLE: tuple[RegimeRow, ...] = (
    RegimeRow(date(2020, 1, 1), 111, "synthetic verified era"),
    RegimeRow(date(2022, 1, 1), None, "synthetic UNVERIFIED gap — no circular retrieved"),
    RegimeRow(date(2024, 10, 1), 222, "synthetic verified current"),
)

#: Synthetic table whose LAST row is a refusal row — open-ended window.
_SYNTHETIC_REFUSAL_LAST: tuple[RegimeRow, ...] = (
    RegimeRow(date(2020, 1, 1), 111, "synthetic verified era"),
    RegimeRow(date(2024, 10, 1), None, "synthetic UNVERIFIED trailing window"),
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
    """No rate is ever fabricated for a date outside the recorded history.

    Reachable only for a caller-supplied table without a ``date.min``
    anchor — all four shipped tables anchor ``date.min`` (structural pin
    below).
    """
    with pytest.raises(ValueError, match="precedes the first recorded regime"):
        constants.rate_for_date(_SYNTHETIC_REGIMES, date(2020, 12, 31))


def test_mechanism_rejects_empty_table() -> None:
    with pytest.raises(ValueError, match="empty regime table"):
        constants.rate_for_date((), date(2024, 1, 1))


# ── RegimeRow validation (DEC-ETC-REGIME-HISTORY-001) ────────────────────────


def test_regime_row_rejects_negative_rate() -> None:
    with pytest.raises(ValueError, match="rate_bps_x100 -1 must be >= 0 or None"):
        RegimeRow(date(2024, 10, 1), -1, "a source")


def test_regime_row_rejects_empty_source() -> None:
    with pytest.raises(ValueError, match="source must carry its citation"):
        RegimeRow(date(2024, 10, 1), 100, "")


def test_regime_row_rejects_whitespace_source() -> None:
    with pytest.raises(ValueError, match="source must carry its citation"):
        RegimeRow(date(2024, 10, 1), 100, "   \t")


def test_regime_row_verified_property() -> None:
    """``verified`` is DERIVED from ``rate_bps_x100`` — it can never drift."""
    assert RegimeRow(date(2024, 10, 1), 100, "cited").verified is True
    assert RegimeRow(date(2024, 10, 1), 0, "cited zero rate").verified is True
    assert RegimeRow(date(2024, 10, 1), None, "refusal window").verified is False


# ── validate_regime_table (import-time structural validation) ────────────────


def test_validate_regime_table_rejects_empty() -> None:
    with pytest.raises(ValueError, match=r"regime table 'empty fixture' is empty"):
        validate_regime_table((), label="empty fixture")


def test_validate_regime_table_rejects_unsorted() -> None:
    unsorted = (
        RegimeRow(date(2024, 10, 1), 222, "later row first"),
        RegimeRow(date(2021, 1, 1), 111, "earlier row second"),
    )
    with pytest.raises(ValueError, match="strictly\\s+ascending"):
        validate_regime_table(unsorted, label="unsorted fixture")


def test_validate_regime_table_rejects_duplicate_effective_date() -> None:
    duplicated = (
        RegimeRow(date(2024, 10, 1), 111, "first of the twins"),
        RegimeRow(date(2024, 10, 1), 222, "second of the twins"),
    )
    with pytest.raises(ValueError, match="strictly\\s+ascending"):
        validate_regime_table(duplicated, label="duplicate fixture")


def test_validate_regime_table_returns_table_unchanged() -> None:
    assert validate_regime_table(_SYNTHETIC_REGIMES, label="ok") is _SYNTHETIC_REGIMES


# ── Refusal rows (UnverifiedCostRegimeError) ─────────────────────────────────


def test_refusal_row_middle_raises_with_verified_from() -> None:
    """A date on a middle refusal row refuses with ``verified_from`` = the
    NEXT row's start — every structured attribute asserted individually
    (mutation-kill for the message formatter)."""
    with pytest.raises(UnverifiedCostRegimeError) as exc_info:
        constants.rate_for_date(
            _SYNTHETIC_REFUSAL_MIDDLE,
            date(2023, 6, 15),
            charge="synthetic charge",
            exchange="NSE",
        )
    err = exc_info.value
    assert err.charge == "synthetic charge"
    assert err.exchange == "NSE"
    assert err.on_date == date(2023, 6, 15)
    assert err.window_start == date(2022, 1, 1)
    assert err.verified_from == date(2024, 10, 1)
    assert err.source == "synthetic UNVERIFIED gap — no circular retrieved"
    assert "add ONE dated" in err.remediation
    assert "MIGRATION.md" in err.remediation


def test_refusal_row_last_raises_open_ended() -> None:
    """A trailing (last-row) refusal window has NO verified successor:
    ``verified_from is None`` and the message reads 'open'."""
    with pytest.raises(UnverifiedCostRegimeError) as exc_info:
        constants.rate_for_date(_SYNTHETIC_REFUSAL_LAST, date(2025, 1, 1))
    err = exc_info.value
    assert err.verified_from is None
    assert err.window_start == date(2024, 10, 1)
    assert "open" in str(err)


def test_refusal_error_is_value_error() -> None:
    """The ``UnsupportedTradeOutcomeError`` precedent: catch-ValueError
    callers keep working; a caller can pin the exact type."""
    assert issubclass(UnverifiedCostRegimeError, ValueError)


# ── Structural pins on the four SHIPPED tables ───────────────────────────────


def test_all_shipped_tables_anchor_date_min() -> None:
    """Every shipped table's first row starts ``date.min`` — refusal/rate
    coverage is TOTAL, so ``rate_for_date``'s pre-history branch is
    unreachable for shipped tables."""
    for table in (
        constants._STT_OPTIONS_REGIMES,
        constants._STT_EXERCISE_REGIMES,
        constants._NSE_ETC_OPTIONS_REGIMES,
        constants._BSE_ETC_OPTIONS_REGIMES,
    ):
        assert table[0].start == date.min


def test_etc_tables_carry_exactly_two_rows_one_refusal_one_verified() -> None:
    """DEC-ETC-REGIME-HISTORY-001 hard line: exactly TWO rate-carrying ETC
    rows exist repo-wide (NSE 3_503 / BSE 3_250, both eff. 2024-10-01); the
    pre-boundary window is a rate-less refusal row on each table."""
    for table, current in (
        (constants._NSE_ETC_OPTIONS_REGIMES, constants.NSE_ETC_OPTIONS_BPS_X100),
        (constants._BSE_ETC_OPTIONS_REGIMES, constants.BSE_ETC_OPTIONS_BPS_X100),
    ):
        assert len(table) == 2
        assert table[0].verified is False
        assert table[0].rate_bps_x100 is None
        assert "UNVERIFIED" in table[0].source
        assert table[1].start == constants.ETC_REGIME_OCT_2024_START
        assert table[1].verified is True
        assert table[1].rate_bps_x100 == current


# ── exchange_for_instrument (the COMMON exchange resolver) ───────────────────


def test_exchange_for_instrument_three_way() -> None:
    assert constants.exchange_for_instrument("NIFTY") == "NSE"
    assert constants.exchange_for_instrument("BANKNIFTY") == "NSE"
    assert constants.exchange_for_instrument("SENSEX") == "BSE"


def test_exchange_for_instrument_unknown_instrument_raises() -> None:
    """The previous silent-NSE default is DEAD: an unknown instrument fails
    loud, naming the allowlist (DEC-ETC-REGIME-HISTORY-001)."""
    with pytest.raises(ValueError, match=r"unknown instrument 'GARBAGE'.*BANKNIFTY.*NIFTY.*SENSEX"):
        constants.exchange_for_instrument("GARBAGE")


def test_exchange_charge_unknown_instrument_raises() -> None:
    with pytest.raises(ValueError, match="unknown instrument"):
        constants.exchange_charge_bps_x100("GARBAGE", date(2026, 5, 15))


def test_ipft_delegates_to_exchange_for_instrument() -> None:
    """IPFT rides the SAME resolver (the copy-paste kill regression): NSE
    names keep 50, SENSEX keeps 0, and an unknown instrument now raises."""
    assert constants.ipft_bps_x100("NIFTY") == constants.NSE_IPFT_BPS_X100
    assert constants.ipft_bps_x100("BANKNIFTY") == constants.NSE_IPFT_BPS_X100
    assert constants.ipft_bps_x100("SENSEX") == constants.BSE_IPFT_BPS_X100
    with pytest.raises(ValueError, match="unknown instrument"):
        constants.ipft_bps_x100("GARBAGE")


# ── unverified_windows / etc_unverified_windows (data-derived spans) ─────────


def test_unverified_windows_shipped_tables() -> None:
    """Both STT tables have ZERO refusal windows; both ETC tables have
    exactly the ``[date.min → 2024-10-01)`` window."""
    assert constants.unverified_windows(constants._STT_OPTIONS_REGIMES) == ()
    assert constants.unverified_windows(constants._STT_EXERCISE_REGIMES) == ()
    expected = ((date.min, date(2024, 10, 1)),)
    assert constants.unverified_windows(constants._NSE_ETC_OPTIONS_REGIMES) == expected
    assert constants.unverified_windows(constants._BSE_ETC_OPTIONS_REGIMES) == expected


def test_unverified_windows_synthetic_multi_window_derivation() -> None:
    """Middle windows close at the next row's start; a trailing window is
    open-ended (``None``)."""
    assert constants.unverified_windows(_SYNTHETIC_REFUSAL_MIDDLE) == (
        (date(2022, 1, 1), date(2024, 10, 1)),
    )
    assert constants.unverified_windows(_SYNTHETIC_REFUSAL_LAST) == ((date(2024, 10, 1), None),)


def test_etc_unverified_windows_accessor() -> None:
    got = constants.etc_unverified_windows()
    assert set(got) == {"NSE", "BSE"}
    assert got["NSE"] == ((date.min, date(2024, 10, 1)),)
    assert got["BSE"] == ((date.min, date(2024, 10, 1)),)


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


# ── ETC honesty contract (DEC-ETC-REGIME-HISTORY-001) ────────────────────────


@pytest.mark.property
@settings(max_examples=200, deadline=None)
@given(
    on_date=st.dates(min_value=date(2024, 10, 1), max_value=date(2030, 12, 31)),
)
def test_etc_post_boundary_returns_verified_rates(on_date: date) -> None:
    """Every post-2024-10-01 date returns the VERIFIED current rates.

    FORCING FUNCTION: this property FAILS the day the encoded post-boundary
    regime changes — e.g. a verified NSE FA73061 row (eff. 2026-03-01) lands
    — forcing a conscious update of this pin alongside the new row.
    """
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


@pytest.mark.property
@settings(max_examples=200, deadline=None)
@given(
    on_date=st.dates(min_value=date(2020, 1, 1), max_value=date(2024, 9, 30)),
)
def test_etc_pre_boundary_always_refuses(on_date: date) -> None:
    """Every pre-2024-10-01 date REFUSES with ``UnverifiedCostRegimeError``
    for all three instruments — never an int, never a fabricated rate.

    FORCING FUNCTION (preserves the discipline of the deleted R2-OC-3
    placeholder pin, which self-declared "FAILS the day a verified
    historical rate lands"): this property FAILS the day a verified
    historical RegimeRow lands inside the window, forcing a conscious
    re-pin of exactly which dates remain refused.
    """
    for instrument in ("NIFTY", "BANKNIFTY", "SENSEX"):
        with pytest.raises(UnverifiedCostRegimeError):
            constants.exchange_charge_bps_x100(instrument, on_date)


@pytest.mark.property
@settings(max_examples=200, deadline=None)
@given(
    starts=st.lists(
        st.dates(min_value=date(2015, 1, 1), max_value=date(2030, 1, 1)),
        min_size=1,
        max_size=6,
        unique=True,
    ),
    rates=st.lists(st.integers(min_value=0, max_value=99_999), min_size=6, max_size=6),
    on_date=st.dates(min_value=date(2015, 1, 1), max_value=date(2031, 1, 1)),
)
def test_rate_for_date_matches_linear_scan_oracle(
    starts: list[date], rates: list[int], on_date: date
) -> None:
    """Mechanism bit-exactness beyond boundary pins: the bisect lookup must
    equal a naive linear scan over synthetic all-rated tables (kills
    bisect_right→bisect_left and index-arithmetic mutants)."""
    table = tuple(
        RegimeRow(start, rate, f"synthetic oracle row {i}")
        for i, (start, rate) in enumerate(zip(sorted(starts), rates, strict=False))
    )
    # Naive oracle: the LAST row whose start <= on_date.
    expected: int | None = None
    for row in table:
        if row.start <= on_date:
            expected = row.rate_bps_x100
    if expected is None:
        with pytest.raises(ValueError, match="precedes the first recorded regime"):
            constants.rate_for_date(table, on_date)
    else:
        assert constants.rate_for_date(table, on_date) == expected


def test_etc_boundary_date_constant_is_oct_2024() -> None:
    """The verified effective date of the CURRENT rates (NSE FA64232 / BSE
    notice 27-Sep-2024, per COSTS_VERIFIED §2/§15)."""
    assert date(2024, 10, 1) == constants.ETC_REGIME_OCT_2024_START
