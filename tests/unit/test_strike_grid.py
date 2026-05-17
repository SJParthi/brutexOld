"""Tests for `brutex.options.strike_grid`."""

from __future__ import annotations

import pytest

from brutex.options.lot_size_history import OptionsUnderlying
from brutex.options.strike_grid import (
    STRIKE_STEP,
    get_strike_step,
    grid_around_atm,
    round_to_atm,
    strike_offset_from_atm,
)


class TestStrikeStep:
    def test_nifty_step_50(self) -> None:
        assert get_strike_step(OptionsUnderlying.NIFTY) == 50

    def test_banknifty_step_100(self) -> None:
        assert get_strike_step(OptionsUnderlying.BANKNIFTY) == 100

    def test_sensex_step_100(self) -> None:
        assert get_strike_step(OptionsUnderlying.SENSEX) == 100


class TestRoundToATM:
    @pytest.mark.parametrize(
        "spot,instrument,expected_atm",
        [
            (24_012.0, OptionsUnderlying.NIFTY,     24_000),  # closer to 24,000
            (24_038.0, OptionsUnderlying.NIFTY,     24_050),  # closer to 24,050
            (24_025.0, OptionsUnderlying.NIFTY,     24_050),  # half-step ties up
            (24_024.9, OptionsUnderlying.NIFTY,     24_000),  # just below half
            (50_075.0, OptionsUnderlying.BANKNIFTY, 50_100),
            (50_049.9, OptionsUnderlying.BANKNIFTY, 50_000),
            (80_050.0, OptionsUnderlying.SENSEX,    80_100),  # half-step ties up
            (80_049.0, OptionsUnderlying.SENSEX,    80_000),
        ],
    )
    def test_round_to_atm(
        self, spot: float, instrument: OptionsUnderlying, expected_atm: int,
    ) -> None:
        assert round_to_atm(spot, instrument) == expected_atm


class TestStrikeOffsetFromATM:
    @pytest.mark.parametrize(
        "atm,offset,instrument,expected",
        [
            (24_000,  0, OptionsUnderlying.NIFTY,     24_000),
            (24_000,  3, OptionsUnderlying.NIFTY,     24_150),
            (24_000, -3, OptionsUnderlying.NIFTY,     23_850),
            (24_000, 10, OptionsUnderlying.NIFTY,     24_500),
            (50_000,  2, OptionsUnderlying.BANKNIFTY, 50_200),
            (50_000, -2, OptionsUnderlying.BANKNIFTY, 49_800),
            (80_000,  5, OptionsUnderlying.SENSEX,    80_500),
            (80_000, -5, OptionsUnderlying.SENSEX,    79_500),
        ],
    )
    def test_offset(
        self, atm: int, offset: int, instrument: OptionsUnderlying, expected: int,
    ) -> None:
        assert strike_offset_from_atm(atm, offset, instrument) == expected


class TestGridAroundATM:
    def test_nifty_full_chain(self) -> None:
        grid = grid_around_atm(24_000, OptionsUnderlying.NIFTY, half_width=10)
        assert len(grid) == 21
        assert grid[0] == 23_500   # ATM - 10*50
        assert grid[10] == 24_000  # ATM
        assert grid[20] == 24_500  # ATM + 10*50

    def test_banknifty_full_chain(self) -> None:
        grid = grid_around_atm(50_000, OptionsUnderlying.BANKNIFTY, half_width=10)
        assert len(grid) == 21
        assert grid[0] == 49_000
        assert grid[10] == 50_000
        assert grid[20] == 51_000

    def test_smaller_half_width(self) -> None:
        grid = grid_around_atm(24_000, OptionsUnderlying.NIFTY, half_width=5)
        assert len(grid) == 11
        assert grid[0] == 23_750
        assert grid[5] == 24_000
        assert grid[10] == 24_250

    def test_grid_sorted_ascending(self) -> None:
        grid = grid_around_atm(50_000, OptionsUnderlying.BANKNIFTY)
        for i in range(len(grid) - 1):
            assert grid[i] < grid[i + 1]


class TestConstants:
    def test_strike_step_dict_complete(self) -> None:
        # All 3 underlyings represented
        for inst in OptionsUnderlying:
            assert inst in STRIKE_STEP
            assert STRIKE_STEP[inst] > 0
