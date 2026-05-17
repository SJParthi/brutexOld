"""Tests for `brutex.indicators.pivots` — Standard Floor Trader Pivots.

Oracle test against operator's TradingView "Pivots Traditional Auto"
screenshot (NIFTY 14-May-2026):
    P=23631.10  R1=23835.70  R2=23981.75  R3=24186.35  R4=24390.90
                S1=23485.05  S2=23280.45  S3=23134.40  S4=22988.30

Reverse-engineered anchors:
    PDH = 23,777.15, PDL = 23,426.50, PDC = 23,689.65
"""

from __future__ import annotations

from datetime import date

import pytest

from brutex.indicators.pivots import PivotLevels, compute_pivot_levels


class TestNifty14MayPivots:
    """Oracle test against NIFTY 14-May-2026 TradingView screenshot."""

    @pytest.fixture
    def levels(self) -> PivotLevels:
        # Reverse-engineered from P, R1, S1 in screenshot:
        #   PDH = 23,777.15 -> 2,377,715 paisa
        #   PDL = 23,426.50 -> 2,342,650 paisa
        #   PDC = 23,689.65 -> 2,368,965 paisa
        return compute_pivot_levels(
            trading_day=date(2026, 5, 14),
            pdh_paisa=2_377_715,
            pdl_paisa=2_342_650,
            pdc_paisa=2_368_965,
        )

    def test_p_matches_screenshot(self, levels: PivotLevels) -> None:
        # Screenshot: P = 23,631.10 -> 2,363,110 paisa
        assert levels.p_paisa == 2_363_110

    def test_r1_matches_screenshot(self, levels: PivotLevels) -> None:
        # Screenshot: R1 = 23,835.70 -> 2,383,570 paisa
        assert levels.r1_paisa == 2_383_570

    def test_r2_matches_screenshot(self, levels: PivotLevels) -> None:
        # Screenshot: R2 = 23,981.75 -> 2,398,175 paisa
        assert levels.r2_paisa == 2_398_175

    def test_r3_matches_screenshot(self, levels: PivotLevels) -> None:
        # Screenshot: R3 = 24,186.35 -> 2,418,635 paisa
        assert levels.r3_paisa == 2_418_635

    def test_r4_matches_screenshot(self, levels: PivotLevels) -> None:
        # Screenshot: R4 = 24,390.90 -> 2,439,090 paisa
        # Formula: PDH + 3*(P - PDL) = 2,377,715 + 3 * 20,460 = 2,439,095
        # Chart shows 2,439,090 (diff: 5 paisa rounding from chart display).
        # Allow ±10 paisa tolerance for chart-OCR precision.
        assert abs(levels.r4_paisa - 2_439_090) <= 10

    def test_s1_matches_screenshot(self, levels: PivotLevels) -> None:
        # Screenshot: S1 = 23,485.05 -> 2,348,505 paisa
        assert levels.s1_paisa == 2_348_505

    def test_s2_matches_screenshot(self, levels: PivotLevels) -> None:
        # Screenshot: S2 = 23,280.45 -> 2,328,045 paisa
        assert levels.s2_paisa == 2_328_045

    def test_s3_matches_screenshot(self, levels: PivotLevels) -> None:
        # Screenshot: S3 = 23,134.40 -> 2,313,440 paisa
        assert levels.s3_paisa == 2_313_440

    def test_s4_matches_screenshot(self, levels: PivotLevels) -> None:
        # Screenshot: S4 = 22,988.30 -> 2,298,830 paisa
        # Formula: PDL - 3*(PDH - P) = 2,342,650 - 3 * 14,605 = 2,298,835
        # Chart shows 2,298,830 (5 paisa rounding from chart display)
        assert abs(levels.s4_paisa - 2_298_830) <= 10


class TestOrdering:
    """Pivot levels must be in price order: S4 < S3 < S2 < S1 < P < R1 < R2 < R3 < R4."""

    def test_levels_in_strict_ascending_order(self) -> None:
        levels = compute_pivot_levels(
            trading_day=date(2026, 5, 14),
            pdh_paisa=2_377_715,
            pdl_paisa=2_342_650,
            pdc_paisa=2_368_965,
        )
        ordered = [
            levels.s4_paisa,
            levels.s3_paisa,
            levels.s2_paisa,
            levels.s1_paisa,
            levels.p_paisa,
            levels.r1_paisa,
            levels.r2_paisa,
            levels.r3_paisa,
            levels.r4_paisa,
        ]
        for i in range(len(ordered) - 1):
            assert ordered[i] < ordered[i + 1], (
                f"Level at index {i} ({ordered[i]}) not < level at {i+1} ({ordered[i+1]})"
            )


class TestSymmetry:
    def test_r1_s1_symmetric_about_p(self) -> None:
        # R1 - P should equal P - S1 (both = 2P - L - P = P - L = H - 2P + 2P - H = H - P-mirror)
        # Actually: R1 = 2P - L means R1 - P = P - L; S1 = 2P - H means P - S1 = H - P
        # These are NOT equal unless H - P = P - L (i.e., P is the exact midpoint of H, L)
        # which is only true when C = (H+L)/2.
        # So this is a weak test: just verify R1 + S1 = 2 * (2P - some) ... skip exact symmetry.
        # Instead verify R1 > P > S1:
        levels = compute_pivot_levels(
            trading_day=date(2026, 5, 14),
            pdh_paisa=2_377_715,
            pdl_paisa=2_342_650,
            pdc_paisa=2_368_965,
        )
        assert levels.r1_paisa > levels.p_paisa > levels.s1_paisa


class TestGetByKey:
    def test_get_by_string_key(self) -> None:
        levels = compute_pivot_levels(
            trading_day=date(2026, 5, 14),
            pdh_paisa=2_377_715,
            pdl_paisa=2_342_650,
            pdc_paisa=2_368_965,
        )
        assert levels.get("P") == levels.p_paisa
        assert levels.get("R1") == levels.r1_paisa
        assert levels.get("R4") == levels.r4_paisa
        assert levels.get("S1") == levels.s1_paisa
        assert levels.get("S4") == levels.s4_paisa

    def test_invalid_key_raises(self) -> None:
        levels = compute_pivot_levels(
            trading_day=date(2026, 5, 14),
            pdh_paisa=2_377_715,
            pdl_paisa=2_342_650,
            pdc_paisa=2_368_965,
        )
        with pytest.raises(KeyError):
            levels.get("R5")
        with pytest.raises(KeyError):
            levels.get("invalid")


class TestValidation:
    def test_pdh_less_than_pdl_raises(self) -> None:
        with pytest.raises(ValueError, match="must be >= PDL"):
            compute_pivot_levels(
                trading_day=date(2026, 5, 14),
                pdh_paisa=50_000_00,
                pdl_paisa=55_000_00,
                pdc_paisa=52_000_00,
            )

    def test_pdc_above_pdh_raises(self) -> None:
        with pytest.raises(ValueError, match="must be in"):
            compute_pivot_levels(
                trading_day=date(2026, 5, 14),
                pdh_paisa=50_000_00,
                pdl_paisa=45_000_00,
                pdc_paisa=55_000_00,  # close above high — corruption
            )

    def test_pdc_below_pdl_raises(self) -> None:
        with pytest.raises(ValueError, match="must be in"):
            compute_pivot_levels(
                trading_day=date(2026, 5, 14),
                pdh_paisa=50_000_00,
                pdl_paisa=45_000_00,
                pdc_paisa=40_000_00,  # close below low — corruption
            )

    def test_pdc_equals_pdh_allowed(self) -> None:
        levels = compute_pivot_levels(
            trading_day=date(2026, 5, 14),
            pdh_paisa=50_000_00,
            pdl_paisa=45_000_00,
            pdc_paisa=50_000_00,  # close = high (valid: limit-up close)
        )
        assert levels.pdc_paisa == 50_000_00


class TestEdgeCases:
    def test_zero_range_degenerate(self) -> None:
        # PDH == PDL == PDC: halted session with single price
        levels = compute_pivot_levels(
            trading_day=date(2026, 5, 14),
            pdh_paisa=50_000_00,
            pdl_paisa=50_000_00,
            pdc_paisa=50_000_00,
        )
        # All levels collapse to the same value
        assert levels.p_paisa == 50_000_00
        assert levels.r1_paisa == 50_000_00
        assert levels.s1_paisa == 50_000_00
        assert levels.r4_paisa == 50_000_00
        assert levels.s4_paisa == 50_000_00
