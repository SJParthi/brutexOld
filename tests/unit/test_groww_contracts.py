"""Unit tests for ``brutex.adapters.groww.contracts`` (DEC-GROWW-OPTION-INGEST-001).

100% line+branch: the right-anchored contract-symbol parser (CE/PE/FUT + every
malformed-input raise), the ``get_expiries`` / ``get_contracts`` resolvers (parse,
dedup, lenient-empty, format-drift raises, year/month kwarg omission), the CE/PE
filter, and the lazy multi-year enumerator (expiry dedup across years). Driven by
a FAKE SDK behind a REAL :class:`GrowwClient` — no ``growwapi``, no network.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date

import pytest
from brutex.adapters.groww.client import GrowwClient
from brutex.adapters.groww.contracts import (
    enumerate_contracts,
    option_contracts,
    parse_contract_symbol,
    resolve_contracts,
    resolve_expiries,
)
from brutex.adapters.protocol import DerivativeContract

_EXPIRY = date(2025, 1, 2)


class _FakeSDK:
    """Records every kwarg; returns whatever the injected callables produce."""

    def __init__(
        self,
        *,
        expiries: Callable[[Mapping[str, object]], Mapping[str, object]] | None = None,
        contracts: Callable[[Mapping[str, object]], Mapping[str, object]] | None = None,
    ) -> None:
        self._expiries = expiries or (lambda _kw: {"expiries": []})
        self._contracts = contracts or (lambda _kw: {"contracts": []})
        self.expiry_calls: list[dict[str, object]] = []
        self.contract_calls: list[dict[str, object]] = []

    def get_historical_candles(self, **_kw: object) -> Mapping[str, object]:  # pragma: no cover
        raise AssertionError("candles are not fetched by the resolution layer")

    def get_expiries(self, **kw: object) -> Mapping[str, object]:
        self.expiry_calls.append(dict(kw))
        return self._expiries(kw)

    def get_contracts(self, **kw: object) -> Mapping[str, object]:
        self.contract_calls.append(dict(kw))
        return self._contracts(kw)


def _client(sdk: _FakeSDK) -> GrowwClient:
    return GrowwClient(sdk=sdk, sleep=lambda _s: None)


# ─────────────────────────────────────────────────────────────────────────
# parse_contract_symbol
# ─────────────────────────────────────────────────────────────────────────
class TestParseContractSymbol:
    def test_call_option(self) -> None:
        c = parse_contract_symbol(
            "NSE-NIFTY-30Sep25-24650-CE", underlying_symbol="NIFTY", expiry=_EXPIRY
        )
        assert c.contract_type == "CE"
        assert c.strike == 24650.0
        assert c.symbol == "NSE-NIFTY-30Sep25-24650-CE"
        assert c.underlying_symbol == "NIFTY"
        assert c.expiry == _EXPIRY

    def test_put_option(self) -> None:
        c = parse_contract_symbol(
            "BSE-SENSEX-25Sep25-79500-PE", underlying_symbol="SENSEX", expiry=_EXPIRY
        )
        assert c.contract_type == "PE"
        assert c.strike == 79500.0

    def test_future_has_no_strike(self) -> None:
        c = parse_contract_symbol(
            "NSE-NIFTY-30Sep25-FUT", underlying_symbol="NIFTY", expiry=_EXPIRY
        )
        assert c.contract_type == "FUT"
        assert c.strike is None

    def test_fractional_strike_is_decimal_exact(self) -> None:
        c = parse_contract_symbol(
            "NSE-WIDGET-30Sep25-123.5-CE", underlying_symbol="WIDGET", expiry=_EXPIRY
        )
        assert c.strike == 123.5

    def test_too_few_tokens_raises(self) -> None:
        with pytest.raises(ValueError, match="not a Groww derivative symbol"):
            parse_contract_symbol("NSE-NIFTY", underlying_symbol="NIFTY", expiry=_EXPIRY)

    def test_option_missing_strike_token_raises(self) -> None:
        with pytest.raises(ValueError, match="missing strike token"):
            parse_contract_symbol("NSE-NIFTY-30Sep25-CE", underlying_symbol="NIFTY", expiry=_EXPIRY)

    def test_unknown_contract_type_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown contract type"):
            parse_contract_symbol(
                "NSE-NIFTY-30Sep25-24650-ZZ", underlying_symbol="NIFTY", expiry=_EXPIRY
            )

    def test_unparseable_strike_raises(self) -> None:
        with pytest.raises(ValueError, match="unparseable strike"):
            parse_contract_symbol(
                "NSE-NIFTY-30Sep25-ABC-CE", underlying_symbol="NIFTY", expiry=_EXPIRY
            )


# ─────────────────────────────────────────────────────────────────────────
# resolve_expiries
# ─────────────────────────────────────────────────────────────────────────
class TestResolveExpiries:
    def test_parses_sorts_dedups(self) -> None:
        sdk = _FakeSDK(
            expiries=lambda _kw: {"expiries": ["2024-01-31", "2024-01-25", "2024-01-25"]}
        )
        out = resolve_expiries(_client(sdk), exchange="NSE", underlying_symbol="NIFTY", year=2024)
        assert out == [date(2024, 1, 25), date(2024, 1, 31)]

    def test_missing_key_is_empty(self) -> None:
        sdk = _FakeSDK(expiries=lambda _kw: {})
        assert resolve_expiries(_client(sdk), exchange="NSE", underlying_symbol="X") == []

    def test_non_array_raises(self) -> None:
        sdk = _FakeSDK(expiries=lambda _kw: {"expiries": "nope"})
        with pytest.raises(ValueError, match="'expiries' must be an array"):
            resolve_expiries(_client(sdk), exchange="NSE", underlying_symbol="X")

    def test_non_string_item_raises(self) -> None:
        sdk = _FakeSDK(expiries=lambda _kw: {"expiries": [20240125]})
        with pytest.raises(ValueError, match="expiry must be a string"):
            resolve_expiries(_client(sdk), exchange="NSE", underlying_symbol="X")

    def test_unparseable_date_raises(self) -> None:
        sdk = _FakeSDK(expiries=lambda _kw: {"expiries": ["not-a-date"]})
        with pytest.raises(ValueError, match="unparseable Groww expiry"):
            resolve_expiries(_client(sdk), exchange="NSE", underlying_symbol="X")

    def test_year_and_month_omitted_when_none(self) -> None:
        sdk = _FakeSDK()
        resolve_expiries(_client(sdk), exchange="NSE", underlying_symbol="NIFTY")
        kw = sdk.expiry_calls[0]
        assert "year" not in kw and "month" not in kw
        assert kw == {"exchange": "NSE", "underlying_symbol": "NIFTY"}

    def test_year_forwarded_month_omitted(self) -> None:
        sdk = _FakeSDK()
        resolve_expiries(_client(sdk), exchange="NSE", underlying_symbol="NIFTY", year=2024)
        kw = sdk.expiry_calls[0]
        assert kw["year"] == 2024 and "month" not in kw

    def test_year_and_month_forwarded(self) -> None:
        sdk = _FakeSDK()
        resolve_expiries(
            _client(sdk), exchange="NSE", underlying_symbol="NIFTY", year=2024, month=1
        )
        kw = sdk.expiry_calls[0]
        assert kw["year"] == 2024 and kw["month"] == 1


# ─────────────────────────────────────────────────────────────────────────
# resolve_contracts
# ─────────────────────────────────────────────────────────────────────────
class TestResolveContracts:
    def test_parses_each_contract(self) -> None:
        sdk = _FakeSDK(
            contracts=lambda _kw: {
                "contracts": ["NSE-NIFTY-02Jan25-28500-PE", "NSE-NIFTY-02Jan25-28150-CE"]
            }
        )
        out = resolve_contracts(
            _client(sdk), exchange="NSE", underlying_symbol="NIFTY", expiry=_EXPIRY
        )
        assert [c.contract_type for c in out] == ["PE", "CE"]
        assert all(c.expiry == _EXPIRY for c in out)
        # the call passes the authoritative expiry as an ISO date
        assert sdk.contract_calls[0]["expiry_date"] == "2025-01-02"

    def test_dedups_duplicate_symbols_first_wins(self) -> None:
        sdk = _FakeSDK(
            contracts=lambda _kw: {
                "contracts": ["NSE-NIFTY-02Jan25-28150-CE", "NSE-NIFTY-02Jan25-28150-CE"]
            }
        )
        out = resolve_contracts(
            _client(sdk), exchange="NSE", underlying_symbol="NIFTY", expiry=_EXPIRY
        )
        assert len(out) == 1

    def test_missing_key_is_empty(self) -> None:
        sdk = _FakeSDK(contracts=lambda _kw: {})
        assert (
            resolve_contracts(_client(sdk), exchange="NSE", underlying_symbol="X", expiry=_EXPIRY)
            == []
        )

    def test_non_array_raises(self) -> None:
        sdk = _FakeSDK(contracts=lambda _kw: {"contracts": 5})
        with pytest.raises(ValueError, match="'contracts' must be an array"):
            resolve_contracts(_client(sdk), exchange="NSE", underlying_symbol="X", expiry=_EXPIRY)

    def test_non_string_item_raises(self) -> None:
        sdk = _FakeSDK(contracts=lambda _kw: {"contracts": [123]})
        with pytest.raises(ValueError, match="contract must be a string"):
            resolve_contracts(_client(sdk), exchange="NSE", underlying_symbol="X", expiry=_EXPIRY)

    def test_malformed_symbol_propagates(self) -> None:
        sdk = _FakeSDK(contracts=lambda _kw: {"contracts": ["GARBAGE"]})
        with pytest.raises(ValueError, match="not a Groww derivative symbol"):
            resolve_contracts(_client(sdk), exchange="NSE", underlying_symbol="X", expiry=_EXPIRY)


# ─────────────────────────────────────────────────────────────────────────
# option_contracts + enumerate_contracts
# ─────────────────────────────────────────────────────────────────────────
def _contract(symbol: str, ctype: str, strike: float | None) -> DerivativeContract:
    return DerivativeContract(
        underlying_symbol="NIFTY", expiry=_EXPIRY, symbol=symbol, contract_type=ctype, strike=strike
    )


class TestOptionContractsFilter:
    def test_keeps_only_ce_pe(self) -> None:
        chain = [
            _contract("NSE-NIFTY-02Jan25-FUT", "FUT", None),
            _contract("NSE-NIFTY-02Jan25-100-CE", "CE", 100.0),
            _contract("NSE-NIFTY-02Jan25-100-PE", "PE", 100.0),
        ]
        kept = option_contracts(chain)
        assert [c.contract_type for c in kept] == ["CE", "PE"]


class TestEnumerateContracts:
    def test_yields_contracts_per_year(self) -> None:
        # object keys: the fake routes on the SDK kwargs, which are typed object.
        exp_by_year: dict[object, list[str]] = {2023: ["2023-12-28"], 2024: ["2024-01-25"]}
        con_by_expiry: dict[object, list[str]] = {
            "2023-12-28": ["NSE-NIFTY-28Dec23-100-CE"],
            "2024-01-25": ["NSE-NIFTY-25Jan24-200-PE"],
        }
        sdk = _FakeSDK(
            expiries=lambda kw: {"expiries": exp_by_year.get(kw.get("year"), [])},
            contracts=lambda kw: {"contracts": con_by_expiry.get(kw["expiry_date"], [])},
        )
        out = list(
            enumerate_contracts(
                _client(sdk), exchange="NSE", underlying_symbol="NIFTY", years=[2023, 2024]
            )
        )
        assert {c.symbol for c in out} == {
            "NSE-NIFTY-28Dec23-100-CE",
            "NSE-NIFTY-25Jan24-200-PE",
        }
        # one get_expiries call per year
        assert len(sdk.expiry_calls) == 2

    def test_dedups_expiry_seen_in_two_years(self) -> None:
        # The same expiry surfaces in BOTH year queries → contracts fetched ONCE.
        sdk = _FakeSDK(
            expiries=lambda _kw: {"expiries": ["2024-01-25"]},
            contracts=lambda _kw: {"contracts": ["NSE-NIFTY-25Jan24-100-CE"]},
        )
        out = list(
            enumerate_contracts(
                _client(sdk), exchange="NSE", underlying_symbol="NIFTY", years=[2023, 2024]
            )
        )
        assert len(out) == 1
        assert len(sdk.contract_calls) == 1  # the duplicate expiry was skipped

    def test_no_years_yields_nothing(self) -> None:
        sdk = _FakeSDK()
        assert (
            list(
                enumerate_contracts(
                    _client(sdk), exchange="NSE", underlying_symbol="NIFTY", years=[]
                )
            )
            == []
        )
        assert sdk.expiry_calls == []
