"""Tests for `brutex.adapters.groww.contracts` — contract discovery (Surface D)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from unittest import mock

import pytest
from brutex.adapters.groww.client import VendorSdkError
from brutex.adapters.groww.contracts import (
    ContractFetcher,
    ContractMeta,
    parse_contract_symbol,
    store_metadata_lookup,
    underlying_symbol_of,
)
from brutex.data.instrument_store import ContractRecord
from brutex.models import InstrumentType


class BaseGrowwException(Exception): ...


class GrowwAPIException(BaseGrowwException): ...


class GrowwAPIRateLimitException(GrowwAPIException): ...


class _FakeSDK:
    EXCHANGE_NSE = "EX_NSE"

    def __init__(self, result: object = None, raises: BaseException | None = None) -> None:
        self._result = result
        self._raises = raises
        self.kwargs: dict[str, object] | None = None

    def get_contracts(self, **kwargs: object) -> object:
        self.kwargs = kwargs
        if self._raises is not None:
            raise self._raises
        return self._result


_META = ContractMeta(lot_size=75, tick_size=Decimal("0.05"), freeze_quantity=1800)


def _fetcher(sdk: _FakeSDK, *, lookup: Any = None) -> tuple[ContractFetcher, mock.Mock, list[Any]]:
    bucket = mock.Mock()
    upserted: list[Any] = []

    class _Store:
        def upsert_contracts(self, contracts: Any) -> int:
            upserted.append(list(contracts))
            return len(upserted[-1])

    fetcher = ContractFetcher(
        sdk=sdk,
        bucket=bucket,
        base_exception=BaseGrowwException,
        store=_Store(),
        metadata_lookup=lookup if lookup is not None else (lambda _sym: _META),
        run_id_factory=lambda: "run-1",
        clock=lambda: datetime(2026, 5, 28, 9, 0, tzinfo=UTC),
    )
    return fetcher, bucket, upserted


class TestParseContractSymbol:
    def test_future(self) -> None:
        assert parse_contract_symbol("NSE-NIFTY-30Jan25-FUT") == (InstrumentType.FUT, None)

    def test_call_option(self) -> None:
        itype, strike = parse_contract_symbol("NSE-NIFTY-30Jan25-24650-CE")  # type: ignore[misc]
        assert itype == InstrumentType.CE
        assert strike == Decimal("24650")

    def test_put_option(self) -> None:
        itype, _strike = parse_contract_symbol("NSE-NIFTY-30Jan25-24650-PE")  # type: ignore[misc]
        assert itype == InstrumentType.PE

    def test_too_few_tokens_is_none(self) -> None:
        assert parse_contract_symbol("NSE-NIFTY-FUT") is None

    def test_unknown_suffix_is_none(self) -> None:
        assert parse_contract_symbol("NSE-NIFTY-30Jan25-24650-XX") is None

    def test_bad_strike_is_none(self) -> None:
        assert parse_contract_symbol("NSE-NIFTY-30Jan25-abc-CE") is None


class TestFetch:
    def test_parses_enriches_and_upserts(self) -> None:
        sdk = _FakeSDK(
            result={"contracts": ["NSE-NIFTY-30Jan25-24650-CE", "NSE-NIFTY-30Jan25-FUT"]}
        )
        fetcher, bucket, upserted = _fetcher(sdk)
        result = fetcher.fetch("NIFTY", "NSE", date(2025, 1, 30))
        assert len(result.records) == 2
        assert (result.skipped_malformed, result.skipped_no_metadata) == (0, 0)
        ce = result.records[0]
        assert isinstance(ce, ContractRecord)
        assert ce.instrument_type == InstrumentType.CE
        assert ce.strike_price == Decimal("24650")
        assert ce.lot_size == 75 and ce.tick_size == Decimal("0.05")
        bucket.record_success.assert_called_once()
        assert sdk.kwargs is not None and sdk.kwargs["expiry_date"] == "2025-01-30"
        assert upserted[0] == result.records

    def test_malformed_symbol_skipped(self) -> None:
        sdk = _FakeSDK(result={"contracts": ["NSE-NIFTY-30Jan25-24650-CE", "GARBAGE"]})
        fetcher, _, _ = _fetcher(sdk)
        result = fetcher.fetch("NIFTY", "NSE", date(2025, 1, 30))
        assert len(result.records) == 1
        assert result.skipped_malformed == 1

    def test_missing_metadata_skipped(self) -> None:
        sdk = _FakeSDK(result={"contracts": ["NSE-NIFTY-30Jan25-24650-CE"]})
        fetcher, _, _ = _fetcher(sdk, lookup=lambda _sym: None)  # no metadata available
        result = fetcher.fetch("NIFTY", "NSE", date(2025, 1, 30))
        assert result.records == []
        assert result.skipped_no_metadata == 1

    def test_empty_contracts(self) -> None:
        fetcher, _, upserted = _fetcher(_FakeSDK(result={"contracts": []}))
        result = fetcher.fetch("NIFTY", "NSE", date(2025, 1, 30))
        assert result.records == []
        assert upserted[0] == []


class TestGuardsAndErrors:
    def test_unknown_exchange_is_bad_request(self) -> None:
        fetcher, bucket, _ = _fetcher(_FakeSDK(result={"contracts": []}))
        with pytest.raises(VendorSdkError) as ei:
            fetcher.fetch("NIFTY", "MCX", date(2025, 1, 30))
        assert type(ei.value).__name__ == "VendorAPIBadRequestException"
        bucket.acquire.assert_not_called()

    def test_malformed_response_is_bad_request(self) -> None:
        fetcher, _, _ = _fetcher(_FakeSDK(result={"wrong": []}))
        with pytest.raises(VendorSdkError) as ei:
            fetcher.fetch("NIFTY", "NSE", date(2025, 1, 30))
        assert type(ei.value).__name__ == "VendorAPIBadRequestException"

    def test_rate_limit_records_hit(self) -> None:
        fetcher, bucket, _ = _fetcher(_FakeSDK(raises=GrowwAPIRateLimitException("429")))
        with pytest.raises(VendorSdkError) as ei:
            fetcher.fetch("NIFTY", "NSE", date(2025, 1, 30))
        assert type(ei.value).__name__ == "VendorAPIRateLimitException"
        bucket.record_rate_limit_hit.assert_called_once()

    def test_non_rate_limit_translates_without_hit(self) -> None:
        fetcher, bucket, _ = _fetcher(_FakeSDK(raises=GrowwAPIException("boom")))
        with pytest.raises(VendorSdkError) as ei:
            fetcher.fetch("NIFTY", "NSE", date(2025, 1, 30))
        assert type(ei.value).__name__ == "VendorAPIException"
        bucket.record_rate_limit_hit.assert_not_called()


class TestUnderlyingSymbolOf:
    """Recover the underlying vendor_symbol prefix from a contract symbol."""

    def test_option_symbol(self) -> None:
        assert underlying_symbol_of("NSE-NIFTY-30Jan25-24650-CE") == "NSE-NIFTY"

    def test_future_symbol(self) -> None:
        assert underlying_symbol_of("NSE-RELIANCE-30Jan25-FUT") == "NSE-RELIANCE"

    def test_bse_symbol(self) -> None:
        assert underlying_symbol_of("BSE-SENSEX-30Jan25-FUT") == "BSE-SENSEX"

    def test_too_few_tokens_is_none(self) -> None:
        assert underlying_symbol_of("NIFTY") is None


class _UnderlyingRow:
    """Minimal stand-in for an instrument-master row's metadata fields."""

    def __init__(self, lot_size: int, tick_size: Decimal, freeze_quantity: int | None) -> None:
        self.lot_size = lot_size
        self.tick_size = tick_size
        self.freeze_quantity = freeze_quantity


class _FakeMetaStore:
    """Records get() calls + returns a canned underlying row (or None)."""

    def __init__(self, row: _UnderlyingRow | None) -> None:
        self._row = row
        self.calls: list[tuple[str, object]] = []

    def get(self, vendor_symbol: str, *, as_of: object = None) -> _UnderlyingRow | None:
        self.calls.append((vendor_symbol, as_of))
        return self._row


class TestStoreMetadataLookup:
    """The contract metadata_lookup that inherits lot/tick/freeze from the underlying."""

    def test_inherits_from_underlying(self) -> None:
        store = _FakeMetaStore(_UnderlyingRow(75, Decimal("0.05"), 1800))
        lookup = store_metadata_lookup(store)
        meta = lookup("NSE-NIFTY-30Jan25-24650-CE")
        assert meta == ContractMeta(lot_size=75, tick_size=Decimal("0.05"), freeze_quantity=1800)
        # Resolved the underlying vendor_symbol, not the leg symbol.
        assert store.calls == [("NSE-NIFTY", None)]

    def test_unresolvable_symbol_is_none(self) -> None:
        store = _FakeMetaStore(_UnderlyingRow(75, Decimal("0.05"), 1800))
        lookup = store_metadata_lookup(store)
        assert lookup("NIFTY") is None  # too few tokens → no underlying
        assert store.calls == []  # short-circuits before any store hit

    def test_missing_underlying_is_none(self) -> None:
        store = _FakeMetaStore(None)  # underlying absent from the master
        lookup = store_metadata_lookup(store)
        assert lookup("NSE-NIFTY-30Jan25-24650-CE") is None
        assert store.calls == [("NSE-NIFTY", None)]

    def test_as_of_is_forwarded(self) -> None:
        store = _FakeMetaStore(_UnderlyingRow(50, Decimal("0.10"), None))
        lookup = store_metadata_lookup(store, as_of=date(2025, 1, 1))
        meta = lookup("NSE-BANKNIFTY-30Jan25-FUT")
        assert meta is not None and meta.freeze_quantity is None
        assert store.calls == [("NSE-BANKNIFTY", date(2025, 1, 1))]

    def test_drives_a_real_fetch(self) -> None:
        # End-to-end: the inherited lookup feeds the ContractFetcher and the
        # written record carries the underlying's economics.
        store = _FakeMetaStore(_UnderlyingRow(250, Decimal("0.05"), 5000))
        sdk = _FakeSDK(result={"contracts": ["NSE-RELIANCE-30Jan25-1200-CE"]})
        fetcher, _bucket, upserted = _fetcher(sdk, lookup=store_metadata_lookup(store))
        result = fetcher.fetch("RELIANCE", "NSE", date(2025, 1, 30))
        assert len(result.records) == 1
        assert result.records[0].lot_size == 250
        assert result.records[0].tick_size == Decimal("0.05")
        assert upserted[0] == result.records


class TestDefaults:
    def test_default_clock_and_run_id(self) -> None:
        # No clock/run_id_factory injected → exercises the module defaults.
        sdk = _FakeSDK(result={"contracts": ["NSE-NIFTY-30Jan25-FUT"]})

        class _Store:
            def upsert_contracts(self, contracts: Any) -> int:
                return len(list(contracts))

        fetcher = ContractFetcher(
            sdk=sdk,
            bucket=mock.Mock(),
            base_exception=BaseGrowwException,
            store=_Store(),
            metadata_lookup=lambda _sym: _META,
        )
        result = fetcher.fetch("NIFTY", "NSE", date(2025, 1, 30))
        assert result.records[0].run_id.startswith("contracts-")
