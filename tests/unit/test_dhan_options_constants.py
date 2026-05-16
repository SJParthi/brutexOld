"""Tests for `brutex.adapters.dhan.options_constants`.

Verifies hardcoded Dhan constants match the spec §2 table.
"""

from __future__ import annotations

from brutex.adapters.dhan.options_constants import (
    ALL_CONTRACTS,
    BANKNIFTY,
    BY_UNDERLYING,
    NIFTY,
    SENSEX,
    get_contract,
)
from brutex.options.lot_size_history import OptionsUnderlying


class TestNifty:
    def test_security_id_13(self) -> None:
        assert NIFTY.spot_security_id == 13

    def test_spot_segment_idx_i(self) -> None:
        assert NIFTY.spot_exchange_segment == "IDX_I"

    def test_options_segment_nse_fno(self) -> None:
        assert NIFTY.options_exchange_segment == "NSE_FNO"

    def test_trading_symbol(self) -> None:
        assert NIFTY.trading_symbol == "NIFTY"


class TestBanknifty:
    def test_security_id_25(self) -> None:
        assert BANKNIFTY.spot_security_id == 25

    def test_options_segment_nse_fno(self) -> None:
        assert BANKNIFTY.options_exchange_segment == "NSE_FNO"

    def test_trading_symbol(self) -> None:
        assert BANKNIFTY.trading_symbol == "BANKNIFTY"


class TestSensex:
    def test_security_id_51(self) -> None:
        assert SENSEX.spot_security_id == 51

    def test_options_segment_bse_fno(self) -> None:
        # SENSEX is on BSE, NOT NSE
        assert SENSEX.options_exchange_segment == "BSE_FNO"

    def test_trading_symbol(self) -> None:
        assert SENSEX.trading_symbol == "SENSEX"


class TestLookup:
    def test_3_contracts_registered(self) -> None:
        assert len(ALL_CONTRACTS) == 3

    def test_get_contract_by_underlying(self) -> None:
        assert get_contract(OptionsUnderlying.NIFTY) == NIFTY
        assert get_contract(OptionsUnderlying.BANKNIFTY) == BANKNIFTY
        assert get_contract(OptionsUnderlying.SENSEX) == SENSEX

    def test_by_underlying_dict_complete(self) -> None:
        for inst in OptionsUnderlying:
            assert inst in BY_UNDERLYING


class TestSpotSegmentSameAcrossAll:
    """All 3 spot indices share IDX_I segment per CLAUDE.md §6 verified table."""

    def test_all_idx_i(self) -> None:
        for c in ALL_CONTRACTS:
            assert c.spot_exchange_segment == "IDX_I"
            assert c.spot_instrument == "INDEX"


class TestOptionsInstrumentOPTIDX:
    """All 3 underlyings use OPTIDX (Index Options) instrument type."""

    def test_all_optidx(self) -> None:
        for c in ALL_CONTRACTS:
            assert c.options_instrument == "OPTIDX"
