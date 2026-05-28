"""Property + fuzz tests for `brutex.adapters.groww.contracts.parse_contract_symbol`.

The contract-symbol parser runs over whatever strings Groww returns, so it
must never crash on a malformed symbol — it returns either ``None`` or a
well-typed ``(InstrumentType, strike)`` pair. Plus a structured property:
every synthetically well-formed FUT / CE / PE symbol parses to the right type.
"""

from __future__ import annotations

from decimal import Decimal

from brutex.adapters.groww.contracts import parse_contract_symbol
from brutex.models import InstrumentType
from hypothesis import given
from hypothesis import strategies as st

_symbols = st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=40)
_underlyings = st.sampled_from(["NIFTY", "BANKNIFTY", "RELIANCE", "TCS"])
_strikes = st.integers(min_value=1, max_value=200000)


class TestFuzz:
    @given(_symbols)
    def test_never_crashes_and_well_typed(self, symbol: str) -> None:
        result = parse_contract_symbol(symbol)
        if result is None:
            return
        instrument_type, strike = result
        assert isinstance(instrument_type, InstrumentType)
        assert strike is None or isinstance(strike, Decimal)
        # A None strike only ever pairs with a future; options always carry one.
        if instrument_type is InstrumentType.FUT:
            assert strike is None
        else:
            assert strike is not None


class TestWellFormed:
    @given(_underlyings)
    def test_future_symbol(self, underlying: str) -> None:
        result = parse_contract_symbol(f"NSE-{underlying}-30Jan25-FUT")
        assert result == (InstrumentType.FUT, None)

    @given(_underlyings, _strikes, st.sampled_from(["CE", "PE"]))
    def test_option_symbol(self, underlying: str, strike: int, side: str) -> None:
        result = parse_contract_symbol(f"NSE-{underlying}-30Jan25-{strike}-{side}")
        assert result is not None
        instrument_type, parsed_strike = result
        assert instrument_type is InstrumentType(side)
        assert parsed_strike == Decimal(strike)
