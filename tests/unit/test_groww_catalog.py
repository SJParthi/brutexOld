"""Tests for `brutex.adapters.groww.catalog` — the full-F&O catalog driver."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from brutex.adapters.groww.catalog import run_catalog
from brutex.adapters.groww.client import VendorSdkError
from brutex.adapters.groww.contracts import ContractFetchResult
from brutex.adapters.groww.expiries import ExpiryFetchResult
from brutex.data.instrument_store import ContractRecord, ExpiryRecord
from brutex.models import InstrumentType

_NOW = datetime(2026, 5, 28, tzinfo=UTC)


def _expiry(underlying: str, exchange: str, year: int) -> ExpiryRecord:
    return ExpiryRecord(
        exchange=exchange,
        underlying_symbol=underlying,
        expiry_date=date(year, 1, 30),
        snapshot_date=date(year, 1, 1),
        pulled_year=year,
        fetched_at=_NOW,
        run_id="r",
    )


def _contract(underlying: str, exchange: str) -> ContractRecord:
    return ContractRecord(
        exchange=exchange,
        underlying_symbol=underlying,
        expiry_date=date(2025, 1, 30),
        contract_symbol=f"{exchange}-{underlying}-30Jan25-FUT",
        snapshot_date=date(2025, 1, 1),
        instrument_type=InstrumentType.FUT,
        strike_price=None,
        lot_size=75,
        freeze_quantity=None,
        tick_size=Decimal("0.05"),
        fetched_at=_NOW,
        run_id="r",
    )


class _FakeExpiryFetcher:
    def __init__(self, *, per_call: int = 2, fail_for: frozenset[str] = frozenset()) -> None:
        self.per_call = per_call
        self.fail_for = fail_for

    def fetch(
        self,
        underlying: str,
        exchange: str,
        *,
        year: int,
        month: int | None = None,
        snapshot_date: date | None = None,
    ) -> ExpiryFetchResult:
        if underlying in self.fail_for:
            raise VendorSdkError("expiry boom")
        records = [_expiry(underlying, exchange, year) for _ in range(self.per_call)]
        return ExpiryFetchResult(records=records, skipped_dates=0)


class _FakeContractFetcher:
    def __init__(self, *, per_call: int = 3, fail: bool = False) -> None:
        self.per_call = per_call
        self.fail = fail

    def fetch(
        self,
        underlying: str,
        exchange: str,
        expiry_date: date,
        *,
        snapshot_date: date | None = None,
    ) -> ContractFetchResult:
        if self.fail:
            raise VendorSdkError("contract boom")
        records = [_contract(underlying, exchange) for _ in range(self.per_call)]
        return ContractFetchResult(records=records, skipped_malformed=0, skipped_no_metadata=0)


_UNDERLYINGS = [("NSE", "NIFTY"), ("NSE", "RELIANCE")]


class TestRunCatalog:
    def test_happy_path_aggregates(self) -> None:
        report = run_catalog(
            _FakeExpiryFetcher(per_call=2), _FakeContractFetcher(per_call=3), _UNDERLYINGS, [2025]
        )
        assert report.underlyings == 2
        assert report.expiries == 4  # 2 underlyings × 2 expiries
        assert report.contracts == 12  # 4 expiries × 3 contracts
        assert (report.expiry_failures, report.contract_failures) == (0, 0)

    def test_multiple_years(self) -> None:
        report = run_catalog(
            _FakeExpiryFetcher(per_call=1),
            _FakeContractFetcher(per_call=1),
            [("NSE", "NIFTY")],
            [2024, 2025],
        )
        assert report.expiries == 2  # 1 underlying × 2 years × 1 expiry
        assert report.contracts == 2

    def test_expiry_failure_is_counted_and_skipped(self) -> None:
        report = run_catalog(
            _FakeExpiryFetcher(fail_for=frozenset({"RELIANCE"})),
            _FakeContractFetcher(),
            _UNDERLYINGS,
            [2025],
        )
        assert report.underlyings == 2  # both processed
        assert report.expiry_failures == 1  # RELIANCE's expiry pull failed
        assert report.expiries == 2  # only NIFTY's expiries

    def test_contract_failure_is_counted_and_skipped(self) -> None:
        report = run_catalog(
            _FakeExpiryFetcher(per_call=2),
            _FakeContractFetcher(fail=True),
            [("NSE", "NIFTY")],
            [2025],
        )
        assert report.expiries == 2
        assert report.contracts == 0
        assert report.contract_failures == 2  # both expiries' contract pulls failed

    def test_empty_universe(self) -> None:
        report = run_catalog(_FakeExpiryFetcher(), _FakeContractFetcher(), [], [2025])
        assert report == type(report)(0, 0, 0, 0, 0)
