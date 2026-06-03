"""Tests for `brutex.money` — the 2-decimal money-display rule (DEC-MONEY-2DP-001).

Money is shown with at most two decimals; statistical metrics keep full
precision (not this module's concern). Internal storage is int64 paisa.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from brutex.money import format_inr, paisa_to_rupees, round_money, rupees_to_paisa


class TestRoundMoney:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("1.005", "1.01"),  # half rounds away from zero
            ("1.004", "1.00"),
            ("1234.5", "1234.50"),
            ("-0.005", "-0.01"),  # negative (a loss) rounds away from zero too
            ("0", "0.00"),
        ],
    )
    def test_quantises_to_two_places(self, raw: str, expected: str) -> None:
        assert round_money(Decimal(raw)) == Decimal(expected)


class TestPaisaToRupees:
    @pytest.mark.parametrize(
        ("paisa", "expected"),
        [
            (1, "0.01"),
            (5, "0.05"),
            (100, "1.00"),
            (123456, "1234.56"),
            (-50, "-0.50"),
        ],
    )
    def test_converts_exactly(self, paisa: int, expected: str) -> None:
        assert paisa_to_rupees(paisa) == Decimal(expected)


class TestRupeesToPaisa:
    @pytest.mark.parametrize(
        ("rupees", "expected"),
        [
            ("0.01", 1),
            ("1.00", 100),
            ("1234.56", 123456),
            ("-0.50", -50),  # a loss
            ("14020.7999", 1402080),  # vendor float-noise snaps up, never truncates
            ("0", 0),
        ],
    )
    def test_converts_exactly(self, rupees: str, expected: int) -> None:
        assert rupees_to_paisa(Decimal(rupees)) == expected

    @pytest.mark.parametrize("paisa", [0, 1, 100, -50, 123456])
    def test_round_trips_with_paisa_to_rupees(self, paisa: int) -> None:
        assert rupees_to_paisa(paisa_to_rupees(paisa)) == paisa


class TestFormatInr:
    @pytest.mark.parametrize(
        ("paisa", "expected"),
        [
            (5, "₹0.05"),
            (123456, "₹1,234.56"),
            (100_000_000, "₹1,000,000.00"),
            (0, "₹0.00"),
        ],
    )
    def test_formats_with_symbol_grouping_two_decimals(self, paisa: int, expected: str) -> None:
        assert format_inr(paisa) == expected
