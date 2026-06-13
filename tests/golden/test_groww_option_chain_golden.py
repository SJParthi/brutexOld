"""Golden byte-freeze for Groww option-chain resolution (DEC-GROWW-OPTION-INGEST-001).

Where ``tests/property/test_groww_contract_symbol_properties.py`` proves the parser
invariants generatively, this PINS the exact resolved chain for a hand-picked
``get_contracts`` payload — so any drift in the canonicalisation (field order,
type discrimination, strike parsing, expiry stamping, first-wins dedup) is caught
byte-for-byte. The payload mixes CE / PE / FUT plus a duplicate symbol.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date

from brutex.adapters.groww.client import GrowwClient
from brutex.adapters.groww.contracts import option_contracts, resolve_contracts

_EXPIRY = date(2025, 1, 2)

#: The frozen vendor payload (a representative NIFTY 02Jan25 leg-set + a duplicate).
_CONTRACTS_PAYLOAD: Mapping[str, object] = {
    "contracts": [
        "NSE-NIFTY-02Jan25-28500-PE",
        "NSE-NIFTY-02Jan25-24000-PE",
        "NSE-NIFTY-02Jan25-28150-CE",
        "NSE-NIFTY-02Jan25-FUT",
        "NSE-NIFTY-02Jan25-28150-CE",  # duplicate — first-wins dedup drops it
    ]
}

#: The frozen resolved chain: ``(symbol, contract_type, strike)`` in input order.
_GOLDEN_CHAIN: tuple[tuple[str, str, float | None], ...] = (
    ("NSE-NIFTY-02Jan25-28500-PE", "PE", 28500.0),
    ("NSE-NIFTY-02Jan25-24000-PE", "PE", 24000.0),
    ("NSE-NIFTY-02Jan25-28150-CE", "CE", 28150.0),
    ("NSE-NIFTY-02Jan25-FUT", "FUT", None),
)

#: The frozen CE/PE-only subset (futures filtered out).
_GOLDEN_OPTIONS: tuple[str, ...] = (
    "NSE-NIFTY-02Jan25-28500-PE",
    "NSE-NIFTY-02Jan25-24000-PE",
    "NSE-NIFTY-02Jan25-28150-CE",
)


class _FrozenSDK:
    def get_historical_candles(self, **_kw: object) -> Mapping[str, object]:  # pragma: no cover
        raise AssertionError("unused")

    def get_expiries(self, **_kw: object) -> Mapping[str, object]:  # pragma: no cover
        raise AssertionError("unused")

    def get_contracts(self, **_kw: object) -> Mapping[str, object]:
        return _CONTRACTS_PAYLOAD


def test_resolved_chain_is_byte_frozen() -> None:
    client = GrowwClient(sdk=_FrozenSDK(), sleep=lambda _s: None)
    chain = resolve_contracts(client, exchange="NSE", underlying_symbol="NIFTY", expiry=_EXPIRY)
    frozen = tuple((c.symbol, c.contract_type, c.strike) for c in chain)
    assert frozen == _GOLDEN_CHAIN
    assert all(c.expiry == _EXPIRY and c.underlying_symbol == "NIFTY" for c in chain)


def test_option_only_subset_is_byte_frozen() -> None:
    client = GrowwClient(sdk=_FrozenSDK(), sleep=lambda _s: None)
    chain = resolve_contracts(client, exchange="NSE", underlying_symbol="NIFTY", expiry=_EXPIRY)
    options = tuple(c.symbol for c in option_contracts(chain))
    assert options == _GOLDEN_OPTIONS
