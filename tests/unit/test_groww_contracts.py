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
