"""Per-bit unit tests for `brutex.vocabulary.evaluator`.

Tests every ACTIVE atomic condition in isolation (62 active of the
66-declared layout — the RETIRED VWAP tombstones 52/53 and the declared-only
SuperTrend bits 64/65 are pinned inert instead):
- the manifest's indicator groups (``bits.GROUP_RANGES``)
- Each bit verified to fire / not-fire on hand-built scenarios
- Look-ahead invariant
- Conflict-pair generator pruning
- Integration test combining all 5 indicators

Per `docs/TRACK1_VOCABULARY.md`. Each test uses a synthetic
IndicatorSnapshot — no real data required.
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar
from zoneinfo import ZoneInfo

import pytest
from brutex.indicators.fib_levels import FibLevels, compute_fib_levels
from brutex.indicators.pivots import PivotLevels, compute_pivot_levels
from brutex.vocabulary import (
    DEFAULT_SWING_PROXIMITY_BPS,
    IndicatorSnapshot,
    PriorBarHistory,
    ProximityParams,
    evaluate_atoms,
    swing_params_fragment,
)
from brutex.vocabulary import bits as B
from brutex.vocabulary.conflicts import CONFLICT_PAIRS, count_conflicts, has_conflict

IST = ZoneInfo("Asia/Kolkata")


def _make_snapshot(
    *,
    timestamp_ist: datetime | None = None,
    open_paisa: int = 24_000_00,
    high_paisa: int = 24_010_00,
    low_paisa: int = 23_990_00,
    close_paisa: int = 24_005_00,
    ema20_paisa: int | None = 24_000_00,
    ema200_paisa: int | None = 23_900_00,
    pdh_paisa: int = 24_050_00,
    pdl_paisa: int = 23_950_00,
    pdc_paisa: int = 24_000_00,
    day_high_paisa: int = 24_010_00,
    day_low_paisa: int = 23_980_00,
    open_of_day_paisa: int = 24_000_00,
) -> IndicatorSnapshot:
    """Build a default IndicatorSnapshot for testing. Override fields as needed."""
    if timestamp_ist is None:
        timestamp_ist = datetime(2026, 5, 13, 10, 30, tzinfo=IST)
    today = timestamp_ist.date()
    return IndicatorSnapshot(
        timestamp_ist=timestamp_ist,
        open_paisa=open_paisa,
        high_paisa=high_paisa,
        low_paisa=low_paisa,
        close_paisa=close_paisa,
        ema20_paisa=ema20_paisa,
        ema200_paisa=ema200_paisa,
        pivots=compute_pivot_levels(today, pdh_paisa, pdl_paisa, pdc_paisa),
        fib=compute_fib_levels(today, pdh_paisa, pdl_paisa),
        day_high_paisa=day_high_paisa,
        day_low_paisa=day_low_paisa,
        open_of_day_paisa=open_of_day_paisa,
    )


def _default_proximity() -> ProximityParams:
    return ProximityParams(
        pivot_proximity_bps=10,
        pdh_pdl_proximity_bps=10,
        fib_proximity_bps=10,
    )


def _empty_history(prior_n: int = 3) -> PriorBarHistory:
    return PriorBarHistory(prior_n=prior_n, flags=[])


def _bit(mask: int, position: int) -> bool:
    """Helper: test if `position` bit is set in `mask`."""
    return bool(mask & (1 << position))


# ────── Group 1: EMA Position (bits 0-5) ──────


class TestGroup1_EMA:
    def test_close_above_ema20(self) -> None:
        s = _make_snapshot(close_paisa=24_010_00, ema20_paisa=24_000_00)
        m = evaluate_atoms(s, _default_proximity(), _empty_history())
        assert _bit(m, B.BIT_CLOSE_ABOVE_EMA20)
        assert not _bit(m, B.BIT_CLOSE_BELOW_EMA20)

    def test_close_below_ema20(self) -> None:
        s = _make_snapshot(close_paisa=23_990_00, ema20_paisa=24_000_00)
        m = evaluate_atoms(s, _default_proximity(), _empty_history())
        assert _bit(m, B.BIT_CLOSE_BELOW_EMA20)
        assert not _bit(m, B.BIT_CLOSE_ABOVE_EMA20)

    def test_close_equals_ema20_no_bit_set(self) -> None:
        # Strict inequality only — equal = neither bit
        s = _make_snapshot(close_paisa=24_000_00, ema20_paisa=24_000_00)
        m = evaluate_atoms(s, _default_proximity(), _empty_history())
        assert not _bit(m, B.BIT_CLOSE_ABOVE_EMA20)
        assert not _bit(m, B.BIT_CLOSE_BELOW_EMA20)

    def test_close_above_ema200(self) -> None:
        s = _make_snapshot(close_paisa=24_000_00, ema200_paisa=23_900_00)
        m = evaluate_atoms(s, _default_proximity(), _empty_history())
        assert _bit(m, B.BIT_CLOSE_ABOVE_EMA200)
        assert not _bit(m, B.BIT_CLOSE_BELOW_EMA200)

    def test_close_below_ema200(self) -> None:
        s = _make_snapshot(close_paisa=23_800_00, ema200_paisa=23_900_00)
        m = evaluate_atoms(s, _default_proximity(), _empty_history())
        assert _bit(m, B.BIT_CLOSE_BELOW_EMA200)
        assert not _bit(m, B.BIT_CLOSE_ABOVE_EMA200)

    def test_ema20_above_ema200_uptrend(self) -> None:
        s = _make_snapshot(ema20_paisa=24_000_00, ema200_paisa=23_900_00)
        m = evaluate_atoms(s, _default_proximity(), _empty_history())
        assert _bit(m, B.BIT_EMA20_ABOVE_EMA200)
        assert not _bit(m, B.BIT_EMA20_BELOW_EMA200)

    def test_ema20_below_ema200_downtrend(self) -> None:
        s = _make_snapshot(ema20_paisa=23_800_00, ema200_paisa=23_900_00)
        m = evaluate_atoms(s, _default_proximity(), _empty_history())
        assert _bit(m, B.BIT_EMA20_BELOW_EMA200)
        assert not _bit(m, B.BIT_EMA20_ABOVE_EMA200)

    def test_ema_warmup_no_bits_set(self) -> None:
        # EMA still warming up (None) -> no EMA bits fire
        s = _make_snapshot(ema20_paisa=None, ema200_paisa=None)
        m = evaluate_atoms(s, _default_proximity(), _empty_history())
        for bit in [
            B.BIT_CLOSE_ABOVE_EMA20,
            B.BIT_CLOSE_BELOW_EMA20,
            B.BIT_CLOSE_ABOVE_EMA200,
            B.BIT_CLOSE_BELOW_EMA200,
            B.BIT_EMA20_ABOVE_EMA200,
            B.BIT_EMA20_BELOW_EMA200,
        ]:
            assert not _bit(m, bit), f"bit {bit} should not be set during warm-up"


# ────── Group 2: Pivot Proximity (bits 6-12) ──────


class TestGroup2_Pivots:
    def test_close_at_pivot_p(self) -> None:
        # PDH=24,050  PDL=23,950  PDC=24,000  -> P = 24,000.00
        # close right at P -> near_pivot_p fires
        s = _make_snapshot(
            close_paisa=24_000_00, pdh_paisa=24_050_00, pdl_paisa=23_950_00, pdc_paisa=24_000_00
        )
        m = evaluate_atoms(s, _default_proximity(), _empty_history())
        assert _bit(m, B.BIT_NEAR_PIVOT_P)

    def test_close_far_from_all_pivots(self) -> None:
        # Move close very far away from all pivots
        s = _make_snapshot(
            close_paisa=20_000_00, pdh_paisa=24_050_00, pdl_paisa=23_950_00, pdc_paisa=24_000_00
        )
        m = evaluate_atoms(s, _default_proximity(), _empty_history())
        for bit in [
            B.BIT_NEAR_PIVOT_P,
            B.BIT_NEAR_PIVOT_R1,
            B.BIT_NEAR_PIVOT_R2,
            B.BIT_NEAR_PIVOT_R3,
            B.BIT_NEAR_PIVOT_S1,
            B.BIT_NEAR_PIVOT_S2,
            B.BIT_NEAR_PIVOT_S3,
        ]:
            assert not _bit(m, bit), f"bit {bit} should not fire when far from pivots"


# ────── Group 3: PDH/PDL (bits 13-18) ──────


class TestGroup3_PDH_PDL:
    def test_close_above_pdh(self) -> None:
        s = _make_snapshot(close_paisa=24_100_00, pdh_paisa=24_050_00, pdl_paisa=23_950_00)
        m = evaluate_atoms(s, _default_proximity(), _empty_history())
        assert _bit(m, B.BIT_CLOSE_ABOVE_PDH)
        assert _bit(m, B.BIT_CLOSE_ABOVE_PDL)  # also above PDL by transitivity
        assert not _bit(m, B.BIT_CLOSE_BELOW_PDH)

    def test_close_below_pdl(self) -> None:
        s = _make_snapshot(close_paisa=23_900_00, pdh_paisa=24_050_00, pdl_paisa=23_950_00)
        m = evaluate_atoms(s, _default_proximity(), _empty_history())
        assert _bit(m, B.BIT_CLOSE_BELOW_PDL)
        assert _bit(m, B.BIT_CLOSE_BELOW_PDH)
        assert not _bit(m, B.BIT_CLOSE_ABOVE_PDL)

    def test_close_between_pdl_pdh(self) -> None:
        # Between PDL and PDH -> above PDL AND below PDH
        s = _make_snapshot(close_paisa=24_000_00, pdh_paisa=24_050_00, pdl_paisa=23_950_00)
        m = evaluate_atoms(s, _default_proximity(), _empty_history())
        assert _bit(m, B.BIT_CLOSE_ABOVE_PDL)
        assert _bit(m, B.BIT_CLOSE_BELOW_PDH)
        assert not _bit(m, B.BIT_CLOSE_ABOVE_PDH)
        assert not _bit(m, B.BIT_CLOSE_BELOW_PDL)

    def test_near_pdh(self) -> None:
        s = _make_snapshot(close_paisa=24_050_00, pdh_paisa=24_050_00, pdl_paisa=23_950_00)
        m = evaluate_atoms(s, _default_proximity(), _empty_history())
        assert _bit(m, B.BIT_NEAR_PDH)

    def test_near_pdl(self) -> None:
        s = _make_snapshot(close_paisa=23_950_00, pdh_paisa=24_050_00, pdl_paisa=23_950_00)
        m = evaluate_atoms(s, _default_proximity(), _empty_history())
        assert _bit(m, B.BIT_NEAR_PDL)


# ────── Group 5: Candle Shape (bits 30-36) ──────


class TestGroup5_CandleShape:
    def test_bar_bullish(self) -> None:
        s = _make_snapshot(
            open_paisa=24_000_00, close_paisa=24_005_00, high_paisa=24_006_00, low_paisa=23_999_00
        )
        m = evaluate_atoms(s, _default_proximity(), _empty_history())
        assert _bit(m, B.BIT_BAR_BULLISH)
        assert not _bit(m, B.BIT_BAR_BEARISH)

    def test_bar_bearish(self) -> None:
        s = _make_snapshot(
            open_paisa=24_005_00, close_paisa=24_000_00, high_paisa=24_006_00, low_paisa=23_999_00
        )
        m = evaluate_atoms(s, _default_proximity(), _empty_history())
        assert _bit(m, B.BIT_BAR_BEARISH)
        assert not _bit(m, B.BIT_BAR_BULLISH)

    def test_bar_doji_tiny_body(self) -> None:
        # Body = 0.5; Range = 10 → 5% body → doji (< 10%)
        s = _make_snapshot(
            open_paisa=24_000_00, close_paisa=24_000_50, high_paisa=24_005_00, low_paisa=23_995_00
        )
        m = evaluate_atoms(s, _default_proximity(), _empty_history())
        assert _bit(m, B.BIT_BAR_DOJI)
        # Note: doji also fires bullish since close > open (a "doji-bull")

    def test_large_body(self) -> None:
        # body = 8, range = 10 → 80% body → large
        s = _make_snapshot(
            open_paisa=24_000_00, close_paisa=24_008_00, high_paisa=24_009_00, low_paisa=23_999_00
        )
        m = evaluate_atoms(s, _default_proximity(), _empty_history())
        assert _bit(m, B.BIT_BAR_LARGE_BODY)
        assert not _bit(m, B.BIT_BAR_SMALL_BODY)

    def test_small_body(self) -> None:
        # body = 2, range = 10 → 20% body → small
        s = _make_snapshot(
            open_paisa=24_000_00, close_paisa=24_002_00, high_paisa=24_005_00, low_paisa=23_995_00
        )
        m = evaluate_atoms(s, _default_proximity(), _empty_history())
        assert _bit(m, B.BIT_BAR_SMALL_BODY)
        assert not _bit(m, B.BIT_BAR_LARGE_BODY)

    def test_long_upper_wick(self) -> None:
        # open=close=24,000, high=24,008, low=23,999 -> upper wick = 8 (range = 9) → 88% > 50%
        s = _make_snapshot(
            open_paisa=24_000_00, close_paisa=24_000_00, high_paisa=24_008_00, low_paisa=23_999_00
        )
        m = evaluate_atoms(s, _default_proximity(), _empty_history())
        assert _bit(m, B.BIT_LONG_UPPER_WICK)

    def test_long_lower_wick(self) -> None:
        # open=close=24,000, high=24,001, low=23,992 -> lower wick = 8 (range = 9) → 88% > 50%
        s = _make_snapshot(
            open_paisa=24_000_00, close_paisa=24_000_00, high_paisa=24_001_00, low_paisa=23_992_00
        )
        m = evaluate_atoms(s, _default_proximity(), _empty_history())
        assert _bit(m, B.BIT_LONG_LOWER_WICK)


# ────── Group 6: Prior Bar Pattern (bits 37-39) ──────


class TestGroup6_PriorBars:
    def test_prior_3_bullish(self) -> None:
        history = PriorBarHistory(prior_n=3, flags=[True, True, True])
        s = _make_snapshot()
        m = evaluate_atoms(s, _default_proximity(), history)
        assert _bit(m, B.BIT_PRIOR_N_BULLISH)
        assert not _bit(m, B.BIT_PRIOR_N_BEARISH)
        assert not _bit(m, B.BIT_PRIOR_ALTERNATING)

    def test_prior_3_bearish(self) -> None:
        history = PriorBarHistory(prior_n=3, flags=[False, False, False])
        s = _make_snapshot()
        m = evaluate_atoms(s, _default_proximity(), history)
        assert _bit(m, B.BIT_PRIOR_N_BEARISH)
        assert not _bit(m, B.BIT_PRIOR_N_BULLISH)

    def test_prior_alternating(self) -> None:
        history = PriorBarHistory(prior_n=3, flags=[True, False, True])
        s = _make_snapshot()
        m = evaluate_atoms(s, _default_proximity(), history)
        assert _bit(m, B.BIT_PRIOR_ALTERNATING)
        assert not _bit(m, B.BIT_PRIOR_N_BULLISH)
        assert not _bit(m, B.BIT_PRIOR_N_BEARISH)

    def test_prior_warmup_no_bits(self) -> None:
        # Only 1 bar in history (need 3) -> no bits fire
        history = PriorBarHistory(prior_n=3, flags=[True])
        s = _make_snapshot()
        m = evaluate_atoms(s, _default_proximity(), history)
        assert not _bit(m, B.BIT_PRIOR_N_BULLISH)
        assert not _bit(m, B.BIT_PRIOR_N_BEARISH)
        assert not _bit(m, B.BIT_PRIOR_ALTERNATING)


# ────── Group 7: Day Structure (bits 40-43) ──────


class TestGroup7_DayStructure:
    def test_close_at_day_high(self) -> None:
        s = _make_snapshot(close_paisa=24_010_00, day_high_paisa=24_010_00, day_low_paisa=23_980_00)
        m = evaluate_atoms(s, _default_proximity(), _empty_history())
        assert _bit(m, B.BIT_CLOSE_AT_DAY_HIGH)

    def test_close_at_day_low(self) -> None:
        s = _make_snapshot(close_paisa=23_980_00, day_high_paisa=24_010_00, day_low_paisa=23_980_00)
        m = evaluate_atoms(s, _default_proximity(), _empty_history())
        assert _bit(m, B.BIT_CLOSE_AT_DAY_LOW)

    def test_close_in_upper_third(self) -> None:
        # Range = 24,010 - 23,980 = 30. Upper third starts at 23,980 + 20 = 24,000.
        # close = 24,005 → in upper third
        s = _make_snapshot(close_paisa=24_005_00, day_high_paisa=24_010_00, day_low_paisa=23_980_00)
        m = evaluate_atoms(s, _default_proximity(), _empty_history())
        assert _bit(m, B.BIT_CLOSE_IN_UPPER_THIRD)
        assert not _bit(m, B.BIT_CLOSE_IN_LOWER_THIRD)

    def test_close_in_lower_third(self) -> None:
        # Lower third < 23,980 + 10 = 23,990. close = 23,985 → lower third.
        s = _make_snapshot(close_paisa=23_985_00, day_high_paisa=24_010_00, day_low_paisa=23_980_00)
        m = evaluate_atoms(s, _default_proximity(), _empty_history())
        assert _bit(m, B.BIT_CLOSE_IN_LOWER_THIRD)
        assert not _bit(m, B.BIT_CLOSE_IN_UPPER_THIRD)


# ────── Group 8: Time of Day (bits 44-47) ──────


class TestGroup8_TimeOfDay:
    @pytest.mark.parametrize(
        "hour,minute,expected_bit",
        [
            (9, 16, B.BIT_EARLY_MORNING),
            (9, 30, B.BIT_EARLY_MORNING),
            (9, 59, B.BIT_EARLY_MORNING),
            (10, 0, B.BIT_MID_MORNING),
            (10, 30, B.BIT_MID_MORNING),
            (11, 29, B.BIT_MID_MORNING),
            (11, 30, B.BIT_MIDDAY),
            (12, 30, B.BIT_MIDDAY),
            (12, 59, B.BIT_MIDDAY),
            (13, 0, B.BIT_AFTERNOON),
            (14, 0, B.BIT_AFTERNOON),
            (14, 59, B.BIT_AFTERNOON),
        ],
    )
    def test_time_of_day_buckets(self, hour: int, minute: int, expected_bit: int) -> None:
        s = _make_snapshot(timestamp_ist=datetime(2026, 5, 13, hour, minute, tzinfo=IST))
        m = evaluate_atoms(s, _default_proximity(), _empty_history())
        assert _bit(m, expected_bit), (
            f"Bar at {hour:02d}:{minute:02d} should set bit {expected_bit}"
        )
        # Verify ONLY that bit fires (mutual exclusion)
        for other in [B.BIT_EARLY_MORNING, B.BIT_MID_MORNING, B.BIT_MIDDAY, B.BIT_AFTERNOON]:
            if other != expected_bit:
                assert not _bit(m, other)

    def test_before_market_no_bit(self) -> None:
        s = _make_snapshot(timestamp_ist=datetime(2026, 5, 13, 8, 30, tzinfo=IST))
        m = evaluate_atoms(s, _default_proximity(), _empty_history())
        for bit in [B.BIT_EARLY_MORNING, B.BIT_MID_MORNING, B.BIT_MIDDAY, B.BIT_AFTERNOON]:
            assert not _bit(m, bit)

    def test_after_market_no_bit(self) -> None:
        s = _make_snapshot(timestamp_ist=datetime(2026, 5, 13, 15, 30, tzinfo=IST))
        m = evaluate_atoms(s, _default_proximity(), _empty_history())
        for bit in [B.BIT_EARLY_MORNING, B.BIT_MID_MORNING, B.BIT_MIDDAY, B.BIT_AFTERNOON]:
            assert not _bit(m, bit)


# ────── Group 9: Day Type (bits 48-51) ──────


class TestGroup9_DayType:
    def test_gap_up_day(self) -> None:
        # PDC = 24,000. Gap up > 0.5% means open > 24,120.
        s = _make_snapshot(open_of_day_paisa=24_150_00, pdc_paisa=24_000_00)
        m = evaluate_atoms(s, _default_proximity(), _empty_history())
        assert _bit(m, B.BIT_GAP_UP_DAY)
        assert not _bit(m, B.BIT_GAP_DOWN_DAY)

    def test_gap_down_day(self) -> None:
        # PDC = 24,000. Gap down > 0.5% means open < 23,880.
        s = _make_snapshot(open_of_day_paisa=23_850_00, pdc_paisa=24_000_00)
        m = evaluate_atoms(s, _default_proximity(), _empty_history())
        assert _bit(m, B.BIT_GAP_DOWN_DAY)
        assert not _bit(m, B.BIT_GAP_UP_DAY)

    def test_no_gap_normal_day(self) -> None:
        # open within 0.5% of PDC -> no gap
        s = _make_snapshot(open_of_day_paisa=24_010_00, pdc_paisa=24_000_00)
        m = evaluate_atoms(s, _default_proximity(), _empty_history())
        assert not _bit(m, B.BIT_GAP_UP_DAY)
        assert not _bit(m, B.BIT_GAP_DOWN_DAY)

    def test_inside_day(self) -> None:
        # Today's range strictly inside prev day's range
        s = _make_snapshot(
            day_high_paisa=24_040_00,
            day_low_paisa=23_960_00,
            pdh_paisa=24_050_00,
            pdl_paisa=23_950_00,
        )
        m = evaluate_atoms(s, _default_proximity(), _empty_history())
        assert _bit(m, B.BIT_INSIDE_DAY)
        assert not _bit(m, B.BIT_OUTSIDE_DAY)

    def test_outside_day(self) -> None:
        # Today's range strictly outside prev day's range
        s = _make_snapshot(
            day_high_paisa=24_100_00,
            day_low_paisa=23_900_00,
            pdh_paisa=24_050_00,
            pdl_paisa=23_950_00,
        )
        m = evaluate_atoms(s, _default_proximity(), _empty_history())
        assert _bit(m, B.BIT_OUTSIDE_DAY)
        assert not _bit(m, B.BIT_INSIDE_DAY)


# ────── Conflict pairs ──────


class TestConflicts:
    def test_no_conflict_empty_mask(self) -> None:
        assert not has_conflict(0)
        assert count_conflicts(0) == 0

    def test_no_conflict_single_bit(self) -> None:
        # Only bit 0 set -> no conflict
        assert not has_conflict(1)

    def test_direct_opposition_above_below_ema20(self) -> None:
        # Bits 0 (above EMA20) + 1 (below EMA20) = direct conflict
        mask = (1 << B.BIT_CLOSE_ABOVE_EMA20) | (1 << B.BIT_CLOSE_BELOW_EMA20)
        assert has_conflict(mask)
        assert count_conflicts(mask) == 1

    def test_candle_bullish_and_bearish_conflict(self) -> None:
        mask = (1 << B.BIT_BAR_BULLISH) | (1 << B.BIT_BAR_BEARISH)
        assert has_conflict(mask)

    def test_time_of_day_mutual_exclusion(self) -> None:
        # Early morning + afternoon = conflict
        mask = (1 << B.BIT_EARLY_MORNING) | (1 << B.BIT_AFTERNOON)
        assert has_conflict(mask)

    def test_orthogonal_bits_no_conflict(self) -> None:
        # EMA position + PDH position (independent indicator groups)
        mask = (1 << B.BIT_CLOSE_ABOVE_EMA20) | (1 << B.BIT_CLOSE_ABOVE_PDH)
        assert not has_conflict(mask)

    def test_all_conflict_pairs_documented(self) -> None:
        # 24 original − 2 buggy (bullish/bearish-doji, removed in
        # DEC-STAGE1B-CONFLICT-FIX-001) + 4 verified candle conflicts +
        # 3 CPR partition pairs (the 60→66 reshape — bits 60/61/62 are an
        # exact partition, evaluator.rs:422-431) = 29.
        assert len(CONFLICT_PAIRS) == 29

    def test_cpr_partition_pairs_present(self) -> None:
        # The three CPR position bits are pairwise mutually exclusive.
        assert (B.BIT_ABOVE_CPR_TC, B.BIT_BELOW_CPR_BC) in CONFLICT_PAIRS
        assert (B.BIT_ABOVE_CPR_TC, B.BIT_INSIDE_CPR) in CONFLICT_PAIRS
        assert (B.BIT_BELOW_CPR_BC, B.BIT_INSIDE_CPR) in CONFLICT_PAIRS


# ────── Look-ahead invariant ──────


class TestLookAhead:
    def test_evaluator_is_pure_function(self) -> None:
        """Calling evaluate_atoms twice with the same input produces the same output."""
        s = _make_snapshot()
        m1 = evaluate_atoms(s, _default_proximity(), _empty_history())
        m2 = evaluate_atoms(s, _default_proximity(), _empty_history())
        assert m1 == m2

    def test_evaluator_independent_of_future_bars(self) -> None:
        """The evaluator uses ONLY the snapshot + prior_history + proximity.
        It has no access to any future bar data.
        """
        s = _make_snapshot()
        # The function signature itself proves this — we can only pass current
        # bar's snapshot. This test is structural; it verifies no surprise
        # globals or class state.
        h1 = PriorBarHistory(prior_n=3, flags=[True, True, True])
        h2 = PriorBarHistory(prior_n=3, flags=[False, False, False])
        m1 = evaluate_atoms(s, _default_proximity(), h1)
        m2 = evaluate_atoms(s, _default_proximity(), h2)
        # Only the prior-history-dependent bits should differ
        differ = m1 ^ m2
        # Bits 37, 38 should differ (prior_n_bullish vs prior_n_bearish)
        # bit 39 (alternating) might also differ depending on flags
        for changed_bit in [B.BIT_PRIOR_N_BULLISH, B.BIT_PRIOR_N_BEARISH]:
            assert differ & (1 << changed_bit), (
                f"bit {changed_bit} should change between bullish/bearish history"
            )


# ────── Integration: realistic snapshot, multiple bits fire ──────


class TestIntegration:
    def test_uptrend_at_pdh_bullish_candle(self) -> None:
        """
        Realistic intraday scenario:
        - Strong uptrend: close > EMA20 > EMA200, EMA20 > EMA200
        - Price right at PDH (breakout candidate)
        - Bullish candle with large body
        - In mid-morning (10:30 IST)
        - Gap-up day (open 1% > PDC)

        Expected bits set: 0, 2, 4, 13, 17, 30, 33, 42 (upper third), 45, 48
        """
        # PDH=24,050, PDL=23,950, PDC=23,960 (in [PDL,PDH] as required by
        # pivots validator). Gap-up trigger: open_of_day > PDC * 1.005.
        # PDC * 1.005 = 24,079.8, so open_of_day=24,100 (1.0%+ gap up) qualifies.
        s = _make_snapshot(
            timestamp_ist=datetime(2026, 5, 13, 10, 30, tzinfo=IST),
            open_paisa=24_040_00,
            high_paisa=24_055_00,
            low_paisa=24_039_00,
            close_paisa=24_050_00,
            ema20_paisa=24_030_00,
            ema200_paisa=23_900_00,
            pdh_paisa=24_050_00,
            pdl_paisa=23_950_00,
            pdc_paisa=23_960_00,  # within [PDL, PDH]
            day_high_paisa=24_055_00,
            day_low_paisa=24_020_00,
            open_of_day_paisa=24_100_00,  # >0.5% gap up from PDC=23,960
        )
        m = evaluate_atoms(s, _default_proximity(), _empty_history())

        # Expected to fire
        assert _bit(m, B.BIT_CLOSE_ABOVE_EMA20)
        assert _bit(m, B.BIT_CLOSE_ABOVE_EMA200)
        assert _bit(m, B.BIT_EMA20_ABOVE_EMA200)
        assert _bit(m, B.BIT_CLOSE_ABOVE_PDL)
        assert _bit(m, B.BIT_NEAR_PDH)
        assert _bit(m, B.BIT_BAR_BULLISH)
        assert _bit(m, B.BIT_MID_MORNING)
        assert _bit(m, B.BIT_GAP_UP_DAY)

        # Expected NOT to fire
        assert not _bit(m, B.BIT_BAR_BEARISH)
        assert not _bit(m, B.BIT_GAP_DOWN_DAY)
        assert not _bit(m, B.BIT_CLOSE_BELOW_EMA20)
        assert not _bit(m, B.BIT_EARLY_MORNING)
        assert not _bit(m, B.BIT_AFTERNOON)

    def test_no_bits_set_in_pre_market(self) -> None:
        """Before 09:16 IST, time-of-day bits should not fire."""
        s = _make_snapshot(timestamp_ist=datetime(2026, 5, 13, 9, 0, tzinfo=IST))
        m = evaluate_atoms(s, _default_proximity(), _empty_history())
        for bit in [B.BIT_EARLY_MORNING, B.BIT_MID_MORNING, B.BIT_MIDDAY, B.BIT_AFTERNOON]:
            assert not _bit(m, bit)


# ────── PriorBarHistory state machine ──────


class TestPriorBarHistory:
    def test_update_keeps_last_n(self) -> None:
        h = PriorBarHistory(prior_n=3)
        for f in [True, False, True, False, True]:
            h.update(f)
        # Should have only last 3
        assert len(h.flags) == 3
        assert h.flags == [True, False, True]

    def test_all_bullish_requires_full_window(self) -> None:
        # The all-bullish/alternating logic now lives in the Rust core
        # (DEC-SHARED-CORE-001 #441); assert the BIT through evaluate_atoms.
        h = PriorBarHistory(prior_n=3)
        h.update(True)
        h.update(True)
        s = _make_snapshot()
        # Only 2 bars in a 3-bar window → not full → bit stays clear.
        assert not _bit(evaluate_atoms(s, _default_proximity(), h), B.BIT_PRIOR_N_BULLISH)
        h.update(True)
        assert _bit(evaluate_atoms(s, _default_proximity(), h), B.BIT_PRIOR_N_BULLISH)

    def test_alternating_for_n_eq_1(self) -> None:
        # Single-bar history trivially "alternates" (empty pairwise check).
        h = PriorBarHistory(prior_n=1)
        h.update(True)
        s = _make_snapshot()
        assert _bit(evaluate_atoms(s, _default_proximity(), h), B.BIT_PRIOR_ALTERNATING)

    def test_alternating_pattern(self) -> None:
        h = PriorBarHistory(prior_n=4)
        for f in [True, False, True, False]:
            h.update(f)
        s = _make_snapshot()
        m = evaluate_atoms(s, _default_proximity(), h)
        assert _bit(m, B.BIT_PRIOR_ALTERNATING)
        assert not _bit(m, B.BIT_PRIOR_N_BULLISH)
        assert not _bit(m, B.BIT_PRIOR_N_BEARISH)

    def test_not_alternating_two_same_in_a_row(self) -> None:
        h = PriorBarHistory(prior_n=4)
        for f in [True, False, True, True]:  # last two same
            h.update(f)
        s = _make_snapshot()
        assert not _bit(evaluate_atoms(s, _default_proximity(), h), B.BIT_PRIOR_ALTERNATING)


# ────── Strict-100% coverage backfill (DEC-INDICATOR-COVERAGE-001 follow-up) ──────


class TestBitsInvariants:
    """Coverage for `brutex.vocabulary.bits._check_invariants`.

    The two `raise AssertionError` branches are unreachable from the
    real `NAME_TO_BIT` constant, so coverage needs a direct call with
    tampered inputs.
    """

    def test_correct_inputs_return_none(self) -> None:
        # The real constants pass — already executed at import, but exercise
        # the happy path explicitly for branch coverage symmetry. The function
        # signature is `-> None`; mypy strict rejects an `is None` assertion
        # on a guaranteed-None return, so we just confirm it doesn't raise.
        B._check_invariants(B.NAME_TO_BIT, B.TOTAL_CONDITIONS)

    def test_wrong_count_raises(self) -> None:
        bad = {"only_one": 0}
        with pytest.raises(
            AssertionError,
            match=rf"Expected {B.TOTAL_CONDITIONS} conditions, got 1",
        ):
            B._check_invariants(bad, B.TOTAL_CONDITIONS)

    def test_bit_position_gap_raises(self) -> None:
        # Right count but bit 0 missing — uses 1..TOTAL instead.
        bad = {f"cond_{i}": i for i in range(1, B.TOTAL_CONDITIONS + 1)}
        with pytest.raises(
            AssertionError,
            match=rf"Bit positions must be 0\.\.{B.TOTAL_CONDITIONS - 1} with no gaps",
        ):
            B._check_invariants(bad, B.TOTAL_CONDITIONS)


class TestEvaluatorEdgeCases:
    """Edge-case coverage routed through the Rust core via the shim.

    The branch logic now lives in `rust/trading-core/src/evaluator.rs`; the
    shim (`evaluate_atoms`) marshals to it, so the same hand-built snapshots
    still produce the documented masks (DEC-SHARED-CORE-001 #441):

    - `is_near` guard for `close <= 0` → no near_* bit ever fires.
    - Pivot R3 / S3 proximity hits.
    - `total_range == 0` skip in Group 5 (candle shape).
    - `day_range == 0` skip in Group 7 (position-in-day).
    - `pdc <= 0` skip in Group 9 (the gap-day path Option B preserves).
    """

    def test_close_zero_returns_no_near_bits(self) -> None:
        # close=0 trips the Rust `is_near` close<=0 guard → all near_* bits stay 0.
        s = _make_snapshot(close_paisa=0)
        m = evaluate_atoms(s, _default_proximity(), _empty_history())
        for bit in (
            B.BIT_NEAR_PIVOT_P,
            B.BIT_NEAR_PIVOT_R1,
            B.BIT_NEAR_PIVOT_R2,
            B.BIT_NEAR_PIVOT_R3,
            B.BIT_NEAR_PIVOT_S1,
            B.BIT_NEAR_PIVOT_S2,
            B.BIT_NEAR_PIVOT_S3,
            B.BIT_NEAR_PDH,
            B.BIT_NEAR_PDL,
            B.BIT_NEAR_FIB_50,
            B.BIT_NEAR_FIB_618,
        ):
            assert not _bit(m, bit), f"Bit {bit} should not fire with close=0"

    def test_near_pivot_r3_fires(self) -> None:
        # Standard pivot math: PDH=24_050, PDL=23_950, PDC=24_000 →
        # P=24_000, range=100 → R3 = PDH + 2*(P-PDL) = 24_050 + 2*50 = 24_150.
        s = _make_snapshot(close_paisa=24_150_00)
        m = evaluate_atoms(s, _default_proximity(), _empty_history())
        assert _bit(m, B.BIT_NEAR_PIVOT_R3)

    def test_near_pivot_s3_fires(self) -> None:
        # S3 = PDL - 2*(PDH-P) = 23_950 - 2*50 = 23_850.
        s = _make_snapshot(close_paisa=23_850_00)
        m = evaluate_atoms(s, _default_proximity(), _empty_history())
        assert _bit(m, B.BIT_NEAR_PIVOT_S3)

    def test_zero_range_bar_skips_candle_shape_branches(self) -> None:
        # open == high == low == close → total_range = 0 → skip Group 5
        # body / wick checks. Doji also requires total_range > 0, so it must
        # NOT fire either.
        s = _make_snapshot(
            open_paisa=24_000_00,
            high_paisa=24_000_00,
            low_paisa=24_000_00,
            close_paisa=24_000_00,
        )
        m = evaluate_atoms(s, _default_proximity(), _empty_history())
        for bit in (
            B.BIT_BAR_BULLISH,
            B.BIT_BAR_BEARISH,
            B.BIT_BAR_DOJI,
            B.BIT_BAR_LARGE_BODY,
            B.BIT_BAR_SMALL_BODY,
            B.BIT_LONG_UPPER_WICK,
            B.BIT_LONG_LOWER_WICK,
        ):
            assert not _bit(m, bit), f"Bit {bit} should not fire when total_range=0"

    def test_zero_day_range_skips_position_in_day(self) -> None:
        # day_high == day_low → day_range = 0 → skip Group 7 upper/lower
        # third bits. The at-high / at-low bits still fire because
        # close == day_high == day_low.
        s = _make_snapshot(
            close_paisa=24_000_00,
            day_high_paisa=24_000_00,
            day_low_paisa=24_000_00,
        )
        m = evaluate_atoms(s, _default_proximity(), _empty_history())
        assert _bit(m, B.BIT_CLOSE_AT_DAY_HIGH)
        assert _bit(m, B.BIT_CLOSE_AT_DAY_LOW)
        assert not _bit(m, B.BIT_CLOSE_IN_UPPER_THIRD)
        assert not _bit(m, B.BIT_CLOSE_IN_LOWER_THIRD)

    def test_zero_pdc_skips_gap_bits(self) -> None:
        # pdc=0 → the Rust evaluator's `pdc > 0` guard skips the Group 9
        # gap-up / gap-down branch. `compute_pivot_levels` rejects pdc=0
        # (data-corruption guard), so build the PivotLevels manually with
        # pdc_paisa=0; the shim passes the levels through verbatim (Option B),
        # which is exactly why the gap-day path survives the Rust delegation.
        from datetime import date

        from brutex.indicators.pivots import PivotLevels

        pdh, pdl = 24_050_00, 23_950_00
        zero_pdc_pivots = PivotLevels(
            trading_day=date(2026, 5, 13),
            pdh_paisa=pdh,
            pdl_paisa=pdl,
            pdc_paisa=0,
            r5_paisa=0,
            r4_paisa=0,
            r3_paisa=0,
            r2_paisa=0,
            r1_paisa=0,
            p_paisa=0,
            s1_paisa=0,
            s2_paisa=0,
            s3_paisa=0,
            s4_paisa=0,
            s5_paisa=0,
        )
        base = _make_snapshot()
        s = IndicatorSnapshot(
            timestamp_ist=base.timestamp_ist,
            open_paisa=base.open_paisa,
            high_paisa=base.high_paisa,
            low_paisa=base.low_paisa,
            close_paisa=base.close_paisa,
            ema20_paisa=base.ema20_paisa,
            ema200_paisa=base.ema200_paisa,
            pivots=zero_pdc_pivots,
            fib=base.fib,
            day_high_paisa=base.day_high_paisa,
            day_low_paisa=base.day_low_paisa,
            open_of_day_paisa=24_000_00,
        )
        m = evaluate_atoms(s, _default_proximity(), _empty_history())
        assert not _bit(m, B.BIT_GAP_UP_DAY)
        assert not _bit(m, B.BIT_GAP_DOWN_DAY)


class TestTodSecondsRustSeam:
    """DEC-TOD-SECONDS-RUST-SEAM-001: ONE shared tod derivation, no inline copies.

    The evaluator's Group-8 ``tod_seconds`` (and the Stage-1A VWAP feed + the
    bar-metadata stamp) now come from ``trading_core.seconds_of_day`` — the
    SAME Rust arithmetic ``derive_bar_metadata`` uses — instead of three
    independent Python ``h*3600 + m*60 + s`` copies.
    """

    def test_seconds_of_day_matches_independent_arithmetic(self) -> None:
        # The independent test-side inline expression is the ORACLE — do NOT
        # "clean it up" onto the binding (its independence is the point).
        import trading_core

        for hour, minute, second in [(0, 0, 0), (9, 15, 0), (15, 20, 30), (23, 59, 59)]:
            assert int(trading_core.seconds_of_day(hour, minute, second)) == (
                hour * 3600 + minute * 60 + second
            )
            meta_tod = trading_core.derive_bar_metadata(
                739_000, hour, minute, second, 33_300, 739_001
            )[0]
            assert int(meta_tod) == hour * 3600 + minute * 60 + second

    @pytest.mark.parametrize(
        ("hour", "minute", "second", "field"),
        [
            (24, 0, 0, "hour"),
            (2_000_000, 0, 0, "hour"),
            (0, 60, 0, "minute"),
            (0, 0, 60, "second"),
        ],
    )
    def test_out_of_range_component_raises_value_error(
        self, hour: int, minute: int, second: int, field: str
    ) -> None:
        # The FFI boundary guard: garbage civil components raise a clean
        # ValueError (never an aborting Rust panic under panic="abort").
        import trading_core

        with pytest.raises(ValueError, match=field):
            trading_core.seconds_of_day(hour, minute, second)

    def test_no_inline_tod_arithmetic_on_the_eval_path(self) -> None:
        # Source-scan guard, NARROWLY scoped to the three eval-path feeds the
        # seam replaced (calendars/nse.py keeps its calendar-construction copy
        # DELIBERATELY — out of the eval path, a named exclusion).
        from pathlib import Path

        import brutex.engine.stage1a
        import brutex.feed.bar_metadata
        import brutex.vocabulary.evaluator

        for module in (
            brutex.vocabulary.evaluator,
            brutex.engine.stage1a,
            brutex.feed.bar_metadata,
        ):
            source = Path(str(module.__file__)).read_text(encoding="utf-8")
            assert "* 3600" not in source, (
                f"inline tod arithmetic reintroduced in {module.__name__}"
            )

    def test_evaluator_group8_bits_unchanged_via_seam(self) -> None:
        # Bit-identity of the swap: a 09:30 IST bar still fires EARLY_MORNING
        # through the seam-derived tod (byte-identical to the old inline path).
        snapshot = _make_snapshot(
            timestamp_ist=datetime(2026, 6, 29, 9, 30, tzinfo=ZoneInfo("Asia/Kolkata"))
        )
        mask = evaluate_atoms(snapshot, _default_proximity(), _empty_history())
        assert _bit(mask, B.BIT_EARLY_MORNING)


def test_applies_to_1day_mask_clears_exactly_tod_and_vwap_bits() -> None:
    """Pin the EXACT intraday-only bit set masked out on 1day (ADR-0012 D12).

    ``IMPLEMENTED_MASK & ~APPLIES_TO_1DAY_MASK`` must equal exactly the four
    Time-of-Day bits 44-47 — built from the named BIT_* constants, never magic
    numbers — so the bits.py comment can never drift from the constant again
    (DEC-CORP-ACTION-GATE-001 doc-drift fold). The tombstoned VWAP bits 52-53
    left IMPLEMENTED_MASK with the 66-bit reshape (they stay outside
    APPLIES_TO_1DAY_MASK too, pinned below), so the implemented∖1day residue
    is the ToD quartet alone.
    """
    expected_cleared = (
        (1 << B.BIT_EARLY_MORNING)
        | (1 << B.BIT_MID_MORNING)
        | (1 << B.BIT_MIDDAY)
        | (1 << B.BIT_AFTERNOON)
    )
    assert expected_cleared == B.IMPLEMENTED_MASK & ~B.APPLIES_TO_1DAY_MASK
    # The tombstoned VWAP pair sits outside BOTH masks (never 1day-enabled).
    assert B.APPLIES_TO_1DAY_MASK & (1 << B.BIT_CLOSE_ABOVE_VWAP) == 0
    assert B.APPLIES_TO_1DAY_MASK & (1 << B.BIT_CLOSE_BELOW_VWAP) == 0
    # The ONLY 1day-applicable bits the evaluator does not implement are the
    # two declared-only Supertrend positions (their AND-mask membership is
    # inert until the math lands — the evaluator never sets them) plus the
    # three DEC-FIB-TEMPLATE-EXPRESS-001 gap placeholders (bits 66-68,
    # reserved=true — dropped at the re-stack onto the gap-midpoint branch).
    assert B.APPLIES_TO_1DAY_MASK & ~B.IMPLEMENTED_MASK == (
        (1 << B.BIT_CLOSE_ABOVE_SUPERTREND) | (1 << B.BIT_CLOSE_BELOW_SUPERTREND) | (0b111 << 66)
    )


# ────────────────────────────────────────────────────────────────────────
# Frozen-ABI level projections (DEC-STAGE1A-RUST-BATCH-001)
# ────────────────────────────────────────────────────────────────────────
#
# `pivot_levels_list` / `fib_levels_list` are the extracted, SHARED marshal
# projections `evaluate_atoms` delegates its inline builds to — the SAME code
# the batch prepass's day tables use. These pin (a) the frozen order,
# field-for-field, and (b) the delegation equality (evaluate_atoms is
# byte-identical to the pre-extraction inline build by construction — the
# frozen 400-case golden re-pins it end-to-end).

import dataclasses  # noqa: E402
from datetime import date as _pl_date  # noqa: E402

import trading_core as _tc  # noqa: E402
from brutex.vocabulary.evaluator import fib_levels_list, pivot_levels_list  # noqa: E402


def test_pivot_levels_list_frozen_order_and_length() -> None:
    pv = compute_pivot_levels(_pl_date(2026, 5, 13), 24_050_00, 23_950_00, 24_000_00)
    levels = pivot_levels_list(pv)
    assert len(levels) == _tc.PIVOT_LEVELS_LEN == 12
    assert levels == [
        pv.p_paisa,
        pv.r1_paisa,
        pv.r2_paisa,
        pv.r3_paisa,
        pv.s1_paisa,
        pv.s2_paisa,
        pv.s3_paisa,
        pv.pdh_paisa,
        pv.pdl_paisa,
        pv.pdc_paisa,
        pv.r5_paisa,
        pv.s5_paisa,
    ]


def test_fib_levels_list_frozen_order_and_length() -> None:
    fib = compute_fib_levels(_pl_date(2026, 5, 13), 24_050_00, 23_950_00)
    levels = fib_levels_list(fib)
    assert len(levels) == _tc.FIB_LEVELS_LEN == 19
    assert levels == [
        fib.retrace_level_at_pdh_paisa,
        fib.retrace_23_paisa,
        fib.retrace_38_paisa,
        fib.retrace_50_paisa,
        fib.retrace_62_paisa,
        fib.retrace_78_paisa,
        fib.retrace_level_at_pdl_paisa,
        fib.ext_up_127_paisa,
        fib.ext_up_162_paisa,
        fib.ext_up_200_paisa,
        fib.ext_up_262_paisa,
        fib.ext_down_127_paisa,
        fib.ext_down_162_paisa,
        fib.ext_down_200_paisa,
        fib.ext_down_262_paisa,
        # DEC-FIB-TEMPLATE-EXPRESS-001 — the four appended-last slots read by
        # bits 69-71 (bull23 → 69, bull78 → 70, up424/dn424 both → 71). The
        # original 15 slots above are byte-identical to the pre-express order.
        fib.retrace_bull_23_paisa,
        fib.retrace_bull_78_paisa,
        fib.ext_up_424_paisa,
        fib.ext_down_424_paisa,
    ]
    # §4.2 constant-pun discipline: FIB_COMPUTE_LIST_LEN (the compute
    # projection) is ALSO 19 now, but the two projections stay DISTINCT —
    # this ABI list carries the 424 pair while the compute projection carries
    # bull_38/bull_62 instead, so equal lengths never imply equal lists
    # (never merge). bull_23 vs bull_78 asymmetry: for a non-degenerate range
    # the two appended retrace slots differ (0.55·range apart).
    assert fib.retrace_bull_23_paisa != fib.retrace_bull_78_paisa
    assert levels[15:] == [
        fib.retrace_bull_23_paisa,
        fib.retrace_bull_78_paisa,
        fib.ext_up_424_paisa,
        fib.ext_down_424_paisa,
    ]


def test_evaluate_atoms_delegates_to_the_shared_projections() -> None:
    # Delegation equality: hand-marshalling through the projections into the
    # raw FFI reproduces evaluate_atoms exactly (same code path — the pin
    # that the extraction changed nothing).
    s = _make_snapshot()
    prox = _default_proximity()
    prior = PriorBarHistory(prior_n=3, flags=[True, False, True])
    via_shim = evaluate_atoms(s, prox, prior)
    ts = s.timestamp_ist
    via_raw = _tc.evaluate_with_levels(
        int(_tc.seconds_of_day(ts.hour, ts.minute, ts.second)),
        s.open_paisa,
        s.high_paisa,
        s.low_paisa,
        s.close_paisa,
        s.ema20_paisa,
        s.ema200_paisa,
        pivot_levels_list(s.pivots),
        fib_levels_list(s.fib),
        s.day_high_paisa,
        s.day_low_paisa,
        s.open_of_day_paisa,
        prox.pivot_proximity_bps,
        prox.pdh_pdl_proximity_bps,
        prox.fib_proximity_bps,
        prior.prior_n,
        prior.flags,
        s.market_structure.bos_bullish,
        s.market_structure.bos_bearish,
        s.market_structure.choch_bullish,
        s.market_structure.choch_bearish,
    )
    assert via_shim == via_raw


# ────── Group 13: Central Pivot Range (bits 60-63) — the hand-derived battery ──────
#
# G-CPRCOR / the 60→66 reshape: every expected TC/BC below is computed BY HAND
# with literal integers in the comments — NEVER by calling the production
# helper (`pivots.rs::compute_cpr_levels` has no Python binding by design; the
# arithmetic is restated so a shared bug cannot self-certify). Semantics under
# pin: BC = round_half_even((pdh+pdl)/2); TC = 2·P − BC; canonicalised
# tc = max, bc = min; bit 60 strict `close > tc`; bit 61 strict `close < bc`;
# bit 62 inclusive `bc <= close <= tc`; bit 63 (`narrow_cpr_day`) iff
# `p > 0` and `(tc − bc) · 10_000 <= p · NARROW_CPR_WIDTH_BPS` — the LOCKED
# **P-relative** denominator (never close-relative).


def _pivots_with_literals(*, p: int, pdh: int, pdl: int, pdc: int) -> PivotLevels:
    """A PivotLevels with hand-set P/PDH/PDL literals (CPR reads only those).

    The R/S rungs are parked far away (±10⁹ paisa) so no `near_*` bit can
    interfere with the asserted CPR nibble.
    """
    far = 1_000_000_000
    return PivotLevels(
        trading_day=datetime(2026, 5, 13, tzinfo=IST).date(),
        pdh_paisa=pdh,
        pdl_paisa=pdl,
        pdc_paisa=pdc,
        r5_paisa=far + 50,
        r4_paisa=far + 40,
        r3_paisa=far + 30,
        r2_paisa=far + 20,
        r1_paisa=far + 10,
        p_paisa=p,
        s1_paisa=-far - 10,
        s2_paisa=-far - 20,
        s3_paisa=-far - 30,
        s4_paisa=-far - 40,
        s5_paisa=-far - 50,
    )


def _cpr_nibble(*, p: int, pdh: int, pdl: int, pdc: int, close: int) -> int:
    """Evaluate one bar against hand-set pivot literals; return bits 60-63."""
    ts = datetime(2026, 5, 13, 10, 30, tzinfo=IST)
    snap = IndicatorSnapshot(
        timestamp_ist=ts,
        open_paisa=close,
        high_paisa=close,
        low_paisa=close,
        close_paisa=close,
        ema20_paisa=None,
        ema200_paisa=None,
        pivots=_pivots_with_literals(p=p, pdh=pdh, pdl=pdl, pdc=pdc),
        fib=compute_fib_levels(ts.date(), max(pdh, pdl + 1), pdl),
        day_high_paisa=close,
        day_low_paisa=close,
        open_of_day_paisa=close,
    )
    mask = evaluate_atoms(snap, _default_proximity(), _empty_history())
    return (mask >> 60) & 0xF


_ABOVE = 0b0001  # bit 60
_BELOW = 0b0010  # bit 61
_INSIDE = 0b0100  # bit 62
_NARROW = 0b1000  # bit 63


class TestGroup13_CPRHandBattery:
    """Hand-computed CPR boundary cases (exact literal arithmetic per case)."""

    # Base anchors: P = 1_000_000; pdh = 1_040_000, pdl = 940_000
    #   BC = rhe((1_040_000 + 940_000) / 2) = rhe(990_000.0) = 990_000
    #   TC = 2·1_000_000 − 990_000 = 1_010_000  (tc > bc — not inverted)
    #   width = 20_000; 20_000·10_000 = 200_000_000 > 1_000_000·30 = 30_000_000
    #   → NOT narrow.
    _BASE: ClassVar[dict[str, int]] = {
        "p": 1_000_000,
        "pdh": 1_040_000,
        "pdl": 940_000,
        "pdc": 1_020_000,
    }

    def test_above_tc_by_one_paisa(self) -> None:
        """close = 1_010_001 = TC + 1 → above only (strict >)."""
        assert _cpr_nibble(**self._BASE, close=1_010_001) == _ABOVE

    def test_exactly_at_tc_is_inside_not_above(self) -> None:
        """close = 1_010_000 = TC exactly → inside (inclusive), never above."""
        assert _cpr_nibble(**self._BASE, close=1_010_000) == _INSIDE

    def test_below_bc_by_one_paisa(self) -> None:
        """close = 989_999 = BC − 1 → below only (strict <)."""
        assert _cpr_nibble(**self._BASE, close=989_999) == _BELOW

    def test_exactly_at_bc_is_inside_not_below(self) -> None:
        """close = 990_000 = BC exactly → inside (inclusive), never below."""
        assert _cpr_nibble(**self._BASE, close=990_000) == _INSIDE

    def test_inverted_raw_pair_is_canonicalised(self) -> None:
        """Raw TC < BC day: the pair canonicalises tc = max, bc = min.

        P = 1_000_000; pdh = 1_060_000, pdl = 1_000_000:
          BC_raw = rhe(2_060_000 / 2) = 1_030_000
          TC_raw = 2_000_000 − 1_030_000 = 970_000  → INVERTED (tc_raw < bc_raw)
          canonical tc = 1_030_000, bc = 970_000; width = 60_000 (not narrow:
          600_000_000 > 30_000_000).
        The three probes pin the CANONICAL boundaries specifically — under an
        un-canonicalised implementation `close > 970_000` would read "above".
        """
        anchors = {"p": 1_000_000, "pdh": 1_060_000, "pdl": 1_000_000, "pdc": 940_000}
        assert _cpr_nibble(**anchors, close=1_000_000) == _INSIDE  # between raw pair
        assert _cpr_nibble(**anchors, close=1_030_001) == _ABOVE  # > canonical tc (= raw BC)
        assert _cpr_nibble(**anchors, close=969_999) == _BELOW  # < canonical bc (= raw TC)

    def test_narrow_exactly_at_30_bps_fires(self) -> None:
        """The <= tie: width·10_000 == p·30 exactly → narrow FIRES.

        P = 1_000_000; pdh = 1_048_500, pdl = 948_500:
          BC = rhe(1_997_000 / 2) = 998_500; TC = 2_000_000 − 998_500 = 1_001_500
          width = 3_000; 3_000·10_000 = 30_000_000 == 1_000_000·30 → fires.
        P-relative denominator law: the compare is against P (1_000_000), never
        the close.
        """
        anchors = {"p": 1_000_000, "pdh": 1_048_500, "pdl": 948_500, "pdc": 1_003_000}
        assert _cpr_nibble(**anchors, close=1_000_000) == _INSIDE | _NARROW

    def test_narrow_just_over_30_bps_clears(self) -> None:
        """The minimal reachable just-over width → narrow CLEAR.

        width = TC − BC = 2·(P − BC) is structurally EVEN, so "30 bps + 1
        paisa" is unreachable; the minimal just-over is +2.
        P = 1_000_000; pdh = 1_048_499, pdl = 948_499:
          BC = rhe(1_996_998 / 2) = 998_499; TC = 2_000_000 − 998_499 = 1_001_501
          width = 3_002; 3_002·10_000 = 30_020_000 > 30_000_000 → clear.
        """
        anchors = {"p": 1_000_000, "pdh": 1_048_499, "pdl": 948_499, "pdc": 1_003_002}
        assert _cpr_nibble(**anchors, close=1_000_000) == _INSIDE

    def test_p_zero_degenerate_clears_narrow_only(self) -> None:
        """p == 0 (all-zero placeholder anchors): the p > 0 guard clears ONLY
        narrow; the position comparisons still fire against the zero levels
        (close = 50 > tc = 0 → above) — exactly why the CPR bits ride the
        Stage-1A anchor-dependent clear upstream (the cold-anchor regression
        in test_engine_stage1a.py pins that clear end-to-end)."""
        assert _cpr_nibble(p=0, pdh=0, pdl=0, pdc=0, close=50) == _ABOVE

    def test_flat_prev_day_zero_width_is_narrow_by_design(self) -> None:
        """G-CPRCOR-05: a flat prev day (H == L == C) → width 0 → narrow fires
        trivially (0 <= threshold) — pinned as INTENDED (a zero-width coil IS
        maximally narrow; noted for the DEC record).

        pdh = pdl = pdc = 1_000_000 → P = 1_000_000, BC = 1_000_000,
        TC = 1_000_000, width = 0. close = 1_000_000 → inside + narrow.
        """
        anchors = {"p": 1_000_000, "pdh": 1_000_000, "pdl": 1_000_000, "pdc": 1_000_000}
        assert _cpr_nibble(**anchors, close=1_000_000) == _INSIDE | _NARROW

    def test_bc_half_tie_rounds_half_even(self) -> None:
        """G-CPRCOR-06: an odd pdh+pdl makes BC a .5 tie — round HALF EVEN.

        pdh = 1_000_000, pdl = 999_997 → sum = 1_999_997; /2 = 999_998.5 →
        RHE → 999_998 (the even neighbour; HALF-UP would give 999_999).
        P: pdc = 1_000_003 → (1_999_997 + 1_000_003)/3 = 1_000_000 exact.
        TC = 2_000_000 − 999_998 = 1_000_002; width = 4 → narrow (40_000 <=
        30_000_000). The two probes discriminate RHE from HALF-UP: under
        HALF-UP (BC = 999_999) the close = 999_998 probe would read BELOW.
        """
        anchors = {"p": 1_000_000, "pdh": 1_000_000, "pdl": 999_997, "pdc": 1_000_003}
        assert _cpr_nibble(**anchors, close=999_998) == _INSIDE | _NARROW  # == BC (RHE)
        assert _cpr_nibble(**anchors, close=999_997) == _BELOW | _NARROW  # BC − 1

    def test_real_nifty_scale_anchor(self) -> None:
        """A real NIFTY-scale prev day (₹24,000s) — full hand derivation.

        pdh = 2_430_000, pdl = 2_400_000, pdc = 2_412_000:
          P  = rhe((2_430_000 + 2_400_000 + 2_412_000) / 3)
             = rhe(7_242_000 / 3) = rhe(2_414_000.0) = 2_414_000
          BC_raw = rhe(4_830_000 / 2) = 2_415_000
          TC_raw = 2·2_414_000 − 2_415_000 = 2_413_000 → INVERTED
          canonical tc = 2_415_000, bc = 2_413_000; width = 2_000
          narrow: 2_000·10_000 = 20_000_000 <= 2_414_000·30 = 72_420_000 → fires.
        """
        anchors = {"p": 2_414_000, "pdh": 2_430_000, "pdl": 2_400_000, "pdc": 2_412_000}
        assert _cpr_nibble(**anchors, close=2_414_000) == _INSIDE | _NARROW
        assert _cpr_nibble(**anchors, close=2_415_001) == _ABOVE | _NARROW
        assert _cpr_nibble(**anchors, close=2_412_999) == _BELOW | _NARROW

    def test_position_bits_partition_every_warm_case(self) -> None:
        """Exactly ONE of above/below/inside per evaluation (the conflicts-table
        justification for the (60,61)/(60,62)/(61,62) pairs)."""
        for close in (989_999, 990_000, 1_000_000, 1_010_000, 1_010_001):
            nibble = _cpr_nibble(**self._BASE, close=close)
            position = nibble & (_ABOVE | _BELOW | _INSIDE)
            assert bin(position).count("1") == 1


def test_narrow_cpr_width_bps_cross_pin() -> None:
    """DRIFT PIN: ``trading_core.NARROW_CPR_WIDTH_BPS`` is LOCKED at 30.

    The authoritative narrow-CPR threshold (supersedes an earlier 50 lock).
    The constant sits inside ``indicator_version``-governed behaviour — it
    moves bit 63 on every bar, hence run identity — so a silent change is
    FORBIDDEN: any re-lock is a deliberate, decision-ID'd act that must
    update this pin, the hand battery above and the golden fixtures together.
    """
    import trading_core

    assert trading_core.NARROW_CPR_WIDTH_BPS == 30


# ────── Fib-Template Express: Fibonacci Extended (69-71) + Swing (72-73) ──────
#
# DEC-FIB-TEMPLATE-EXPRESS-001. Every case drives the REAL evaluate_atoms
# (the Rust core via the shim). Fails-before: none of these bits exist at the
# pre-express merge-base (TOTAL_CONDITIONS was 66) — the entire class fails
# to import BIT_NEAR_FIB_BULL_236 there.


class TestExpressFibExtended:
    """Bits 69-71: bull-236/bull-786 retracements + the two-sided 424."""

    def _fib(self) -> FibLevels:
        return compute_fib_levels(_pl_date(2026, 5, 13), 24_050_00, 23_950_00)

    def test_near_fib_bull_236_fires_on_exact_level(self) -> None:
        fib = self._fib()
        s = _make_snapshot(close_paisa=fib.retrace_bull_23_paisa)
        m = evaluate_atoms(s, _default_proximity(), _empty_history())
        assert _bit(m, B.BIT_NEAR_FIB_BULL_236)

    def test_near_fib_bull_236_clear_just_outside_band(self) -> None:
        # close < level by MORE than the close-anchored tolerance: the gap is
        # level*bps//10⁴ + 2 while tol(close) <= level*bps//10⁴ (close < level).
        fib = self._fib()
        level = fib.retrace_bull_23_paisa
        close = level - (level * 10) // 10_000 - 2
        s = _make_snapshot(close_paisa=close)
        m = evaluate_atoms(s, _default_proximity(), _empty_history())
        assert not _bit(m, B.BIT_NEAR_FIB_BULL_236)

    def test_near_fib_bull_786_fires_and_is_not_bull_236(self) -> None:
        # Asymmetric-ladder fixture: the two bull slots differ by 0.55·range,
        # so a close snapped onto bull_78 fires 70 and NOT 69 (kills the
        # retrace_bull_23 ↔ retrace_bull_78 field-swap mutant).
        fib = self._fib()
        assert fib.retrace_bull_23_paisa != fib.retrace_bull_78_paisa
        s = _make_snapshot(close_paisa=fib.retrace_bull_78_paisa)
        m = evaluate_atoms(s, _default_proximity(), _empty_history())
        assert _bit(m, B.BIT_NEAR_FIB_BULL_786)
        assert not _bit(m, B.BIT_NEAR_FIB_BULL_236)

    def test_near_fib_424_fires_on_up_variant_only(self) -> None:
        # Near ONLY ext_up_424 (far from ext_down_424) — kills the OR→AND
        # mutant on the two-sided bit-71 predicate.
        fib = self._fib()
        assert abs(fib.ext_up_424_paisa - fib.ext_down_424_paisa) > 100_00
        s = _make_snapshot(close_paisa=fib.ext_up_424_paisa)
        m = evaluate_atoms(s, _default_proximity(), _empty_history())
        assert _bit(m, B.BIT_NEAR_FIB_424)

    def test_near_fib_424_fires_on_down_variant_only(self) -> None:
        fib = self._fib()
        s = _make_snapshot(close_paisa=fib.ext_down_424_paisa)
        m = evaluate_atoms(s, _default_proximity(), _empty_history())
        assert _bit(m, B.BIT_NEAR_FIB_424)

    def test_near_fib_424_clear_between_variants(self) -> None:
        # Midway between the up/down 424 extensions — near NEITHER.
        fib = self._fib()
        mid = (fib.ext_up_424_paisa + fib.ext_down_424_paisa) // 2
        s = _make_snapshot(close_paisa=mid)
        m = evaluate_atoms(s, _default_proximity(), _empty_history())
        assert not _bit(m, B.BIT_NEAR_FIB_424)

    def test_bull_bits_use_the_fib_band_not_the_swing_band(self) -> None:
        # Distinct band values: at fib_bps=50 the offset close classifies
        # near bull_23; at fib_bps=5 it does not — swing_bps huge either way
        # (kills a fib_bps → swing_proximity_bps band-field swap on 69-71).
        fib = self._fib()
        level = fib.retrace_bull_23_paisa
        close = level + (level * 30) // 10_000  # ~30 bps away
        wide = ProximityParams(fib_proximity_bps=50, swing_proximity_bps=5000)
        narrow = ProximityParams(fib_proximity_bps=5, swing_proximity_bps=5000)
        m_wide = evaluate_atoms(_make_snapshot(close_paisa=close), wide, _empty_history())
        m_narrow = evaluate_atoms(_make_snapshot(close_paisa=close), narrow, _empty_history())
        assert _bit(m_wide, B.BIT_NEAR_FIB_BULL_236)
        assert not _bit(m_narrow, B.BIT_NEAR_FIB_BULL_236)


class TestExpressSwingProximity:
    """Bits 72/73: last-confirmed-swing proximity (None-abstain law)."""

    _CLOSE = 24_000_00
    _TOL_10BPS = (24_000_00 * 10) // 10_000  # 24_000 paisa at the default band

    def test_bits_never_fire_on_none_swings(self) -> None:
        # fire-on-None mutant killer: no confirmed swing ⇒ bits 72/73 CLEAR
        # even though close would be trivially "near" any fabricated level.
        s = _make_snapshot(close_paisa=self._CLOSE)
        assert s.last_swing_high_paisa is None and s.last_swing_low_paisa is None
        m = evaluate_atoms(s, _default_proximity(), _empty_history())
        assert not _bit(m, B.BIT_NEAR_SWING_HIGH)
        assert not _bit(m, B.BIT_NEAR_SWING_LOW)

    def test_snapshot_defaults_are_none_and_mask_is_byte_identical(self) -> None:
        # Snapshot-defaults pin: omitting the swing kwargs ≡ explicit None/None
        # — the whole mask (every other bit included) byte-identical.
        s_default = _make_snapshot(close_paisa=self._CLOSE)
        s_explicit = dataclasses.replace(
            s_default, last_swing_high_paisa=None, last_swing_low_paisa=None
        )
        m1 = evaluate_atoms(s_default, _default_proximity(), _empty_history())
        m2 = evaluate_atoms(s_explicit, _default_proximity(), _empty_history())
        assert m1 == m2

    def test_near_swing_high_exact_band_edge_in_and_out(self) -> None:
        # The exact tolerance law: tol = close*bps//10⁴ with close FIXED, the
        # free swing level placed AT the edge (fires) and one paisa past (clear).
        close = self._CLOSE
        tol = self._TOL_10BPS
        s_in = _make_snapshot(close_paisa=close)
        s_in = dataclasses.replace(s_in, last_swing_high_paisa=close + tol)
        m_in = evaluate_atoms(s_in, _default_proximity(), _empty_history())
        assert _bit(m_in, B.BIT_NEAR_SWING_HIGH)
        s_out = dataclasses.replace(s_in, last_swing_high_paisa=close + tol + 1)
        m_out = evaluate_atoms(s_out, _default_proximity(), _empty_history())
        assert not _bit(m_out, B.BIT_NEAR_SWING_HIGH)

    def test_near_swing_low_exact_band_edge_in_and_out(self) -> None:
        close = self._CLOSE
        tol = self._TOL_10BPS
        base = _make_snapshot(close_paisa=close)
        s_in = dataclasses.replace(base, last_swing_low_paisa=close - tol)
        m_in = evaluate_atoms(s_in, _default_proximity(), _empty_history())
        assert _bit(m_in, B.BIT_NEAR_SWING_LOW)
        s_out = dataclasses.replace(base, last_swing_low_paisa=close - tol - 1)
        m_out = evaluate_atoms(s_out, _default_proximity(), _empty_history())
        assert not _bit(m_out, B.BIT_NEAR_SWING_LOW)

    def test_swing_high_and_low_are_not_swapped(self) -> None:
        # Asymmetric fixture: ONLY the high is near ⇒ ONLY bit 72; then ONLY
        # the low is near ⇒ ONLY bit 73 (kills the hi↔lo swap mutant).
        base = _make_snapshot(close_paisa=self._CLOSE)
        s_high = dataclasses.replace(
            base, last_swing_high_paisa=self._CLOSE + 5_00, last_swing_low_paisa=20_000_00
        )
        m_high = evaluate_atoms(s_high, _default_proximity(), _empty_history())
        assert _bit(m_high, B.BIT_NEAR_SWING_HIGH)
        assert not _bit(m_high, B.BIT_NEAR_SWING_LOW)
        s_low = dataclasses.replace(
            base, last_swing_high_paisa=28_000_00, last_swing_low_paisa=self._CLOSE - 5_00
        )
        m_low = evaluate_atoms(s_low, _default_proximity(), _empty_history())
        assert _bit(m_low, B.BIT_NEAR_SWING_LOW)
        assert not _bit(m_low, B.BIT_NEAR_SWING_HIGH)

    def test_swing_bits_use_the_swing_band_not_the_fib_band(self) -> None:
        # Distinct band values (band off-by-one/field-swap killer): the level
        # sits ~30 bps off close — inside swing_bps=50, outside swing_bps=5 —
        # with fib_bps pinned OPPOSITE each time.
        close = self._CLOSE
        level = close + (close * 30) // 10_000
        base = _make_snapshot(close_paisa=close)
        s = dataclasses.replace(base, last_swing_high_paisa=level)
        wide = ProximityParams(fib_proximity_bps=5, swing_proximity_bps=50)
        narrow = ProximityParams(fib_proximity_bps=50, swing_proximity_bps=5)
        assert _bit(evaluate_atoms(s, wide, _empty_history()), B.BIT_NEAR_SWING_HIGH)
        assert not _bit(evaluate_atoms(s, narrow, _empty_history()), B.BIT_NEAR_SWING_HIGH)

    def test_default_swing_band_constant_cross_pin(self) -> None:
        assert DEFAULT_SWING_PROXIMITY_BPS == 10
        assert ProximityParams().swing_proximity_bps == 10


def test_abi_and_compute_fib_projections_are_distinct() -> None:
    """§4.2 constant-pun discipline: 19 == 19 yet the two lists DIFFER.

    ``fib_levels_list`` (the evaluator ABI, FIB_LEVELS_LEN) appends
    bull23/bull78/up424/dn424; the ``compute_fib_levels`` FFI projection
    (FIB_COMPUTE_LIST_LEN) carries bull_38/bull_62 instead of the 424 pair —
    equal lengths never imply mergeable lists.
    """
    fib = compute_fib_levels(_pl_date(2026, 5, 13), 24_050_00, 23_950_00)
    abi = fib_levels_list(fib)
    compute_projection = list(_tc.compute_fib_levels(24_050_00, 23_950_00))
    assert len(abi) == len(compute_projection) == 19
    assert abi != compute_projection
    # The pdh/pdl retrace anchors are ABI-only slots (the compute projection
    # starts at retrace_23 and derives the anchors caller-side); slot 0 alone
    # proves the orders can never pun (bull_38 vs bearish-62 is an IEEE
    # coincidence, so field-level membership is deliberately NOT the probe).
    assert abi[0] == fib.retrace_level_at_pdh_paisa
    assert fib.retrace_level_at_pdh_paisa not in compute_projection
    assert abi[0] != compute_projection[0]


class TestSwingParamsFragment:
    """The ONE COMMON default-omit identity fragment (DEC-FIB-TEMPLATE-EXPRESS-001).

    Direct unit coverage of ``vocabulary.swing_params_fragment`` — the emit law
    every identity builder (canonical_trade_params / _grid_descriptor /
    _tuple_cache_params / _tuple_digest) delegates to.  The builder-side pins
    live in the engine suites; this covers the fragment's own three arms so the
    ci.yml ``brutex.vocabulary.evaluator`` 100% gate holds from this file alone.
    """

    def test_none_proximity_emits_nothing(self) -> None:
        assert swing_params_fragment(None) == {}

    def test_default_band_emits_nothing(self) -> None:
        prox = ProximityParams(swing_proximity_bps=DEFAULT_SWING_PROXIMITY_BPS)
        assert swing_params_fragment(prox) == {}

    def test_non_default_band_emits_exactly_one_key(self) -> None:
        prox = ProximityParams(swing_proximity_bps=25)
        assert swing_params_fragment(prox) == {"swing_proximity_bps": 25}
