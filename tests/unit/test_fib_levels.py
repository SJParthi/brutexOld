"""Tests for `brutex.indicators.fib_levels` — Static Daily Fibonacci.

Verifies the formula against the ES Trading Academy AmiBroker v11
screenshots provided by operator on 2026-05-16:
- BANKNIFTY 13-May-2026: PDH=54,000.00, PDL=53,594.40
  Expected 23% = 53,904.28 (the chart shows this exact value)

This test is the ground-truth oracle for the Fib indicator.
"""

from __future__ import annotations

from datetime import date

import pytest

from brutex.indicators.fib_levels import (
    ALL_FIB_LEVEL_KEYS,
    FIB_RATIOS_EXTENSION,
    FIB_RATIOS_INTRA,
    FibLevels,
    compute_fib_levels,
)


class TestBankniftyScreenshotOracle:
    """Exact-match oracle test against operator's AmiBroker v11 screenshot.

    BANKNIFTY 13-May-2026:
    PDH = 54,000.00 (5,400,000 paisa)
    PDL = 53,594.40 (5,359,440 paisa)
    Expected 23% level = 53,904.28 (5,390,428 paisa)
    """

    @pytest.fixture
    def banknifty_13may(self) -> FibLevels:
        return compute_fib_levels(
            trading_day=date(2026, 5, 13),
            pdh_paisa=5_400_000,  # 54,000.00
            pdl_paisa=5_359_440,  # 53,594.40
        )

    def test_range_is_correct(self, banknifty_13may: FibLevels) -> None:
        # 54,000.00 - 53,594.40 = 405.60 -> 40,560 paisa
        assert banknifty_13may.range_paisa == 40_560

    def test_level_0_equals_pdh(self, banknifty_13may: FibLevels) -> None:
        assert banknifty_13may.level_0_paisa == 5_400_000

    def test_level_100_equals_pdl(self, banknifty_13may: FibLevels) -> None:
        assert banknifty_13may.level_100_paisa == 5_359_440

    def test_level_23_matches_screenshot_exact(self, banknifty_13may: FibLevels) -> None:
        # Screenshot shows 53,904.28 -> 5,390,428 paisa
        # Formula: 5,400,000 - 0.236 * 40,560 = 5,400,000 - 9,572.16 = 5,390,427.84
        # Banker's rounding -> 5,390,428
        assert banknifty_13may.level_23_paisa == 5_390_428

    def test_level_50_is_midpoint(self, banknifty_13may: FibLevels) -> None:
        # Midpoint: (5,400,000 + 5,359,440) / 2 = 5,379,720
        # Screenshot shows 53,797.20 -> 5,379,720 paisa ✓
        assert banknifty_13may.level_50_paisa == 5_379_720

    def test_level_38_correct(self, banknifty_13may: FibLevels) -> None:
        # 5,400,000 - 0.382 * 40,560 = 5,384,506.08 -> 5,384,506
        # Screenshot shows 53,845.06 ✓
        assert banknifty_13may.level_38_paisa == 5_384_506

    def test_level_62_correct(self, banknifty_13may: FibLevels) -> None:
        # 5,400,000 - 0.618 * 40,560 = 5,374,933.92 -> 5,374,934
        # Screenshot shows 53,749.34 ✓
        assert banknifty_13may.level_62_paisa == 5_374_934

    def test_level_78_correct(self, banknifty_13may: FibLevels) -> None:
        # 5,400,000 - 0.786 * 40,560 = 5,368,119.84 -> 5,368,120
        # Screenshot shows 53,681.20 ✓
        assert banknifty_13may.level_78_paisa == 5_368_120

    def test_extensions_below_pdl(self, banknifty_13may: FibLevels) -> None:
        # 200% = one full range below PDL
        # 5,359,440 - 40,560 = 5,318,880
        assert banknifty_13may.level_200_paisa == 5_318_880


class TestNiftyScreenshotOracle:
    """Second oracle test from operator's NIFTY screenshot 13-May-2026.

    NIFTY 13-May-2026: 100% (= PDL) = 23,594.40
    23% = 23,904.28 (visible on chart)
    """

    def test_nifty_levels_internally_consistent(self) -> None:
        # Inferred PDL = 23,594.40 -> 2,359,440 paisa
        # Inferred 23% level = 23,904.28 -> 2,390,428 paisa
        # Formula: 23% = PDH - 0.236 * range
        #         2,390,428 = PDH - 0.236 * (PDH - 2,359,440)
        # Solve: 2,390,428 = PDH * (1 - 0.236) + 0.236 * 2,359,440
        #        2,390,428 = 0.764 * PDH + 556,827.84
        #        0.764 * PDH = 1,833,600.16
        #        PDH = 2,400,000 (approx 24,000)
        # So PDH = 2,400,000 paisa = Rs 24,000 (a round number — likely correct)
        levels = compute_fib_levels(
            trading_day=date(2026, 5, 13),
            pdh_paisa=2_400_000,  # 24,000.00 inferred
            pdl_paisa=2_359_440,  # 23,594.40
        )
        # Verify 23% matches screenshot's 23,904.28
        assert levels.level_23_paisa == 2_390_428


class TestValidation:
    def test_pdh_less_than_pdl_raises(self) -> None:
        with pytest.raises(ValueError, match="must be >= PDL"):
            compute_fib_levels(
                trading_day=date(2024, 5, 13),
                pdh_paisa=50_000_00,
                pdl_paisa=55_000_00,  # PDL > PDH = corruption
            )

    def test_zero_range_is_allowed(self) -> None:
        # PDH == PDL is degenerate but valid (e.g., halted session with single bar)
        levels = compute_fib_levels(
            trading_day=date(2024, 5, 13),
            pdh_paisa=50_000_00,
            pdl_paisa=50_000_00,
        )
        assert levels.range_paisa == 0
        assert levels.level_0_paisa == levels.level_100_paisa
        assert levels.level_50_paisa == 50_000_00


class TestStructure:
    def test_all_12_level_keys_present(self) -> None:
        assert len(ALL_FIB_LEVEL_KEYS) == 12

    def test_intra_ratios_count(self) -> None:
        # 7 retracements: 0, 23, 38, 50, 62, 78, 100
        assert len(FIB_RATIOS_INTRA) == 7

    def test_extension_ratios_count(self) -> None:
        # 5 extensions: 127, 162, 200, 262, 424
        assert len(FIB_RATIOS_EXTENSION) == 5

    def test_get_by_key(self) -> None:
        levels = compute_fib_levels(
            trading_day=date(2024, 5, 13),
            pdh_paisa=5_400_000,
            pdl_paisa=5_359_440,
        )
        # Lookup by key matches direct field access
        assert levels.get("0") == levels.level_0_paisa
        assert levels.get("50") == levels.level_50_paisa
        assert levels.get("100") == levels.level_100_paisa
        assert levels.get("424") == levels.level_424_paisa

    def test_get_invalid_key_raises(self) -> None:
        levels = compute_fib_levels(
            trading_day=date(2024, 5, 13),
            pdh_paisa=5_400_000,
            pdl_paisa=5_359_440,
        )
        with pytest.raises(KeyError):
            levels.get("999")
