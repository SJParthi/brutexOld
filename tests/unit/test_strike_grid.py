"""Tests for `brutex.options.strike_grid`."""

# Whitebox pins on the private Python oracles (DEC-OPTCOST-TAILS-RUST-001):
# pyright: reportPrivateUsage=none

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
            (24_012.0, OptionsUnderlying.NIFTY, 24_000),  # closer to 24,000
            (24_038.0, OptionsUnderlying.NIFTY, 24_050),  # closer to 24,050
            (24_025.0, OptionsUnderlying.NIFTY, 24_050),  # half-step ties up
            (24_024.9, OptionsUnderlying.NIFTY, 24_000),  # just below half
            (50_075.0, OptionsUnderlying.BANKNIFTY, 50_100),
            (50_049.9, OptionsUnderlying.BANKNIFTY, 50_000),
            (80_050.0, OptionsUnderlying.SENSEX, 80_100),  # half-step ties up
            (80_049.0, OptionsUnderlying.SENSEX, 80_000),
        ],
    )
    def test_round_to_atm(
        self,
        spot: float,
        instrument: OptionsUnderlying,
        expected_atm: int,
    ) -> None:
        assert round_to_atm(spot, instrument) == expected_atm


class TestStrikeOffsetFromATM:
    @pytest.mark.parametrize(
        "atm,offset,instrument,expected",
        [
            (24_000, 0, OptionsUnderlying.NIFTY, 24_000),
            (24_000, 3, OptionsUnderlying.NIFTY, 24_150),
            (24_000, -3, OptionsUnderlying.NIFTY, 23_850),
            (24_000, 10, OptionsUnderlying.NIFTY, 24_500),
            (50_000, 2, OptionsUnderlying.BANKNIFTY, 50_200),
            (50_000, -2, OptionsUnderlying.BANKNIFTY, 49_800),
            (80_000, 5, OptionsUnderlying.SENSEX, 80_500),
            (80_000, -5, OptionsUnderlying.SENSEX, 79_500),
        ],
    )
    def test_offset(
        self,
        atm: int,
        offset: int,
        instrument: OptionsUnderlying,
        expected: int,
    ) -> None:
        assert strike_offset_from_atm(atm, offset, instrument) == expected


class TestGridAroundATM:
    def test_nifty_full_chain(self) -> None:
        grid = grid_around_atm(24_000, OptionsUnderlying.NIFTY, half_width=10)
        assert len(grid) == 21
        assert grid[0] == 23_500  # ATM - 10*50
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


# ── DEC-OPTCOST-TAILS-RUST-001: the flag-ON seam (ON == OFF, float law) ──


class TestOptcostRustSeamOn:
    """The grid functions under `BRUTEX_OPTCOST_TAILS_RUST=1` match the
    Python oracle — `round_to_atm`'s CPython float law included (half-step
    ties, adverse float representations, NaN/inf raise parity)."""

    @pytest.fixture(autouse=True)
    def _seam_on(self, monkeypatch: pytest.MonkeyPatch):
        import brutex.optcost_rust as ocr

        pytest.importorskip("fno_math")
        monkeypatch.setenv(ocr.OPTCOST_TAILS_RUST_ENV, "1")
        ocr.reset_optcost_rust_for_tests()  # burn-in armed — oracle runs beside
        yield
        ocr.reset_optcost_rust_for_tests()

    def test_round_to_atm_matches_the_oracle_on_hostile_floats(self) -> None:
        import brutex.optcost_rust as ocr
        from brutex.options.strike_grid import STRIKE_STEP, _round_to_atm_python

        for spot in (
            24_012.0,
            24_025.0,  # half-step tie
            50_075.0,
            24_024.999999999996,
            81_249.99999999999,
            1e15 + 25,
            5e-324,  # subnormal
            0.0,
            -0.0,
            -24_025.0,
        ):
            for instrument in OptionsUnderlying:
                assert round_to_atm(spot, instrument) == _round_to_atm_python(
                    spot, STRIKE_STEP[instrument]
                )
        assert not ocr.optcost_rust_disabled()

    def test_round_to_atm_nan_and_inf_raise_exactly_as_off(self) -> None:
        # VERIFIED CPython behaviour (never assumed): `inf // step` is NaN in
        # float floor division, so BOTH non-finite spots raise the NaN
        # ValueError — the seam must propagate the oracle's exact bytes.
        for spot in (float("nan"), float("inf"), float("-inf")):
            with pytest.raises(ValueError, match="cannot convert float NaN to integer"):
                round_to_atm(spot, OptionsUnderlying.NIFTY)

    def test_offset_and_grid_match_the_oracle(self) -> None:
        assert strike_offset_from_atm(24_000, 3, OptionsUnderlying.NIFTY) == 24_150
        assert strike_offset_from_atm(50_000, -2, OptionsUnderlying.BANKNIFTY) == 49_800
        assert grid_around_atm(24_000, OptionsUnderlying.NIFTY, 2) == [
            23_900,
            23_950,
            24_000,
            24_050,
            24_100,
        ]
        assert grid_around_atm(24_000, OptionsUnderlying.NIFTY, -1) == []
