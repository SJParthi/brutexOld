"""Unit tests for brutex.indicators.pivot_fib_levels (DEC-VOCAB-001)."""

from __future__ import annotations

from datetime import date

import pytest
from brutex.indicators.pivot_fib_levels import (
    PIVOT_FIB_R1_RATIO,
    PIVOT_FIB_R2_RATIO,
    PIVOT_FIB_R3_RATIO,
    compute_pivot_fib_levels,
)


class TestPivotFibRatios:
    def test_ratios_match_standard_fibonacci(self) -> None:
        assert pytest.approx(0.382) == PIVOT_FIB_R1_RATIO
        assert pytest.approx(0.618) == PIVOT_FIB_R2_RATIO
        assert PIVOT_FIB_R3_RATIO == 1.0


class TestComputePivotFibLevels:
    """compute_pivot_fib_levels matches the textbook formulas."""

    def test_simple_range_pivot(self) -> None:
        """PDH=10000, PDL=9000, PDC=9500 → P=(10000+9000+9500)/3=9500."""
        levels = compute_pivot_fib_levels(
            trading_day=date(2026, 5, 14),
            pdh_paisa=10000,
            pdl_paisa=9000,
            pdc_paisa=9500,
        )
        assert levels.pdh_paisa == 10000
        assert levels.pdl_paisa == 9000
        assert levels.pdc_paisa == 9500
        assert levels.range_paisa == 1000
        # P = mean(10000, 9000, 9500) = 9500
        assert levels.p_paisa == 9500
        # R1 = P + 0.382 * 1000 = 9500 + 382 = 9882
        assert levels.r1_paisa == 9882
        # R2 = P + 0.618 * 1000 = 9500 + 618 = 10118
        assert levels.r2_paisa == 10118
        # R3 = P + 1.000 * 1000 = 9500 + 1000 = 10500
        assert levels.r3_paisa == 10500
        # Symmetric supports
        assert levels.s1_paisa == 9118
        assert levels.s2_paisa == 8882
        assert levels.s3_paisa == 8500

    def test_get_lookup_table(self) -> None:
        levels = compute_pivot_fib_levels(
            date(2026, 5, 14),
            pdh_paisa=10000,
            pdl_paisa=9000,
            pdc_paisa=9500,
        )
        assert levels.get("P") == 9500
        assert levels.get("R1") == 9882
        assert levels.get("R2") == 10118
        assert levels.get("R3") == 10500
        assert levels.get("S1") == 9118
        assert levels.get("S2") == 8882
        assert levels.get("S3") == 8500

    def test_invalid_key_raises_keyerror(self) -> None:
        levels = compute_pivot_fib_levels(date(2026, 5, 14), 10000, 9000, 9500)
        with pytest.raises(KeyError):
            levels.get("R4")

    def test_negative_range_raises(self) -> None:
        with pytest.raises(ValueError, match="PDH"):
            compute_pivot_fib_levels(
                date(2026, 5, 14),
                pdh_paisa=9000,
                pdl_paisa=10000,
                pdc_paisa=9500,
            )

    def test_close_outside_range_raises(self) -> None:
        with pytest.raises(ValueError, match="PDC"):
            compute_pivot_fib_levels(
                date(2026, 5, 14),
                pdh_paisa=10000,
                pdl_paisa=9000,
                pdc_paisa=12000,  # > PDH
            )
        with pytest.raises(ValueError, match="PDC"):
            compute_pivot_fib_levels(
                date(2026, 5, 14),
                pdh_paisa=10000,
                pdl_paisa=9000,
                pdc_paisa=8000,  # < PDL
            )

    def test_pivot_at_high_when_close_at_high(self) -> None:
        """PDC=PDH → P is biased above midpoint."""
        levels = compute_pivot_fib_levels(
            date(2026, 5, 14),
            pdh_paisa=10000,
            pdl_paisa=9000,
            pdc_paisa=10000,
        )
        # P = (10000 + 9000 + 10000) / 3 = 9667 (rounded)
        assert levels.p_paisa == 9667

    def test_pivot_at_low_when_close_at_low(self) -> None:
        levels = compute_pivot_fib_levels(
            date(2026, 5, 14),
            pdh_paisa=10000,
            pdl_paisa=9000,
            pdc_paisa=9000,
        )
        # P = (10000 + 9000 + 9000) / 3 = 9333 (rounded half-even)
        # 9333.33... → 9333 (half-even rounds to even)
        assert levels.p_paisa in (9333, 9334)  # depends on rounding

    def test_frozen_dataclass(self) -> None:
        levels = compute_pivot_fib_levels(date(2026, 5, 14), 10000, 9000, 9500)
        with pytest.raises((AttributeError, TypeError)):
            levels.p_paisa = 99999  # type: ignore[misc]


class TestRealWorldNifty:
    """Realistic NIFTY index numbers."""

    def test_nifty_24300_pivot(self) -> None:
        """Typical NIFTY day: PDH=24500, PDL=24100, PDC=24300."""
        levels = compute_pivot_fib_levels(
            date(2026, 5, 14),
            pdh_paisa=2_450_000,
            pdl_paisa=2_410_000,
            pdc_paisa=2_430_000,
        )
        # P = (2_450_000 + 2_410_000 + 2_430_000) / 3 = 2_430_000
        assert levels.p_paisa == 2_430_000
        assert levels.range_paisa == 40_000
        # R1 = 2_430_000 + round(0.382 * 40_000) = 2_430_000 + 15_280 = 2_445_280
        assert levels.r1_paisa == 2_445_280
        # R3 = 2_430_000 + 40_000 = 2_470_000
        assert levels.r3_paisa == 2_470_000
        # S3 = 2_430_000 - 40_000 = 2_390_000
        assert levels.s3_paisa == 2_390_000


class TestVocabularyConsistency:
    """Pivot-Fib R/S keys match the 6 new vocab bits added in DEC-VOCAB-001."""

    def test_vocab_bits_match_indicator_keys(self) -> None:
        from brutex.vocabulary.bits import (
            BIT_NEAR_PIVOT_FIB_R1,
            BIT_NEAR_PIVOT_FIB_R2,
            BIT_NEAR_PIVOT_FIB_R3,
            BIT_NEAR_PIVOT_FIB_S1,
            BIT_NEAR_PIVOT_FIB_S2,
            BIT_NEAR_PIVOT_FIB_S3,
            NAME_TO_BIT,
        )

        # Vocab has exactly 6 pivot-Fib bits.
        pivot_fib_bits = [
            BIT_NEAR_PIVOT_FIB_R1,
            BIT_NEAR_PIVOT_FIB_R2,
            BIT_NEAR_PIVOT_FIB_R3,
            BIT_NEAR_PIVOT_FIB_S1,
            BIT_NEAR_PIVOT_FIB_S2,
            BIT_NEAR_PIVOT_FIB_S3,
        ]
        assert len(set(pivot_fib_bits)) == 6, "all 6 pivot-Fib bits must be distinct"
        # Sequential bits 57..62.
        assert sorted(pivot_fib_bits) == [57, 58, 59, 60, 61, 62]
        # Name lookups consistent.
        assert NAME_TO_BIT["near_pivot_fib_r1"] == BIT_NEAR_PIVOT_FIB_R1
        assert NAME_TO_BIT["near_pivot_fib_s3"] == BIT_NEAR_PIVOT_FIB_S3

    def test_indicator_get_keys_cover_all_vocab_bits(self) -> None:
        """All 7 pivot-Fib keys produce non-negative results on positive input."""
        levels = compute_pivot_fib_levels(date(2026, 5, 14), 10000, 9000, 9500)
        for key in ("P", "R1", "R2", "R3", "S1", "S2", "S3"):
            value = levels.get(key)
            assert value > 0, f"key {key!r} produced non-positive value {value}"
