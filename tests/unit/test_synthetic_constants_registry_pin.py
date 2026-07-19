"""Synthetic INTERVAL_TO_MINUTES registry-derivation pin (DEC-TF-REGISTRY-DYNAMIC-001)."""

from __future__ import annotations

from brutex.adapters.synthetic.constants import (
    INTERVAL_TO_MINUTES,
    SUPPORTED_INTERVALS,
    SYMBOL_SPECS,
)
from brutex.models import InstrumentType

_EXPECTED = {
    "1minute": 1,
    "5minute": 5,
    "15minute": 15,
    "1hour": 60,
    "1day": 375,
}


def test_interval_to_minutes_exact_literal_values() -> None:
    assert dict(INTERVAL_TO_MINUTES) == _EXPECTED


def test_supported_intervals_unchanged() -> None:
    assert frozenset(_EXPECTED) == SUPPORTED_INTERVALS


def test_spec_is_index_reflects_instrument_type() -> None:
    """Direct per-module coverage of ``SyntheticSymbolSpec.is_index``."""
    for spec in SYMBOL_SPECS:
        assert spec.is_index == (spec.instrument_type == InstrumentType.IDX)


def test_spec_market_symbol_is_exchange_dash_symbol() -> None:
    """Direct per-module coverage of ``SyntheticSymbolSpec.market_symbol``."""
    for spec in SYMBOL_SPECS:
        assert spec.market_symbol == f"{spec.exchange.value}-{spec.symbol}"
