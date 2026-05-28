"""Tests for `brutex.adapters.groww.expiries` — expiry discovery (Surface C)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from unittest import mock

import pytest
from brutex.adapters.groww.client import VendorSdkError
from brutex.adapters.groww.expiries import ExpiryFetcher
from brutex.data.instrument_store import ExpiryRecord


class BaseGrowwException(Exception): ...


class GrowwAPIException(BaseGrowwException): ...


class GrowwAPIRateLimitException(GrowwAPIException): ...


class GrowwAPINotFoundException(GrowwAPIException): ...


class _FakeSDK:
    EXCHANGE_NSE = "EX_NSE"

    def __init__(self, result: object = None, raises: BaseException | None = None) -> None:
        self._result = result
        self._raises = raises
        self.kwargs: dict[str, object] | None = None

    def get_expiries(self, **kwargs: object) -> object:
        self.kwargs = kwargs
        if self._raises is not None:
            raise self._raises
        return self._result


def _fetcher(sdk: _FakeSDK) -> tuple[ExpiryFetcher, mock.Mock, list[Any]]:
    bucket = mock.Mock()
    upserted: list[Any] = []

    class _Store:
        def upsert_expiries(self, expiries: Any) -> int:
            upserted.append(list(expiries))
            return len(upserted[-1])

    fetcher = ExpiryFetcher(
        sdk=sdk,
        bucket=bucket,
        base_exception=BaseGrowwException,
        store=_Store(),
        run_id_factory=lambda: "run-1",
        clock=lambda: datetime(2026, 5, 28, 9, 0, tzinfo=UTC),
    )
    return fetcher, bucket, upserted


class TestHappyPath:
    def test_parses_and_upserts(self) -> None:
        sdk = _FakeSDK(result={"expiries": ["2025-01-30", "2025-02-27"]})
        fetcher, bucket, upserted = _fetcher(sdk)
        result = fetcher.fetch("NIFTY", "NSE", year=2025)
        assert len(result.records) == 2
        assert result.skipped_dates == 0
        assert all(isinstance(r, ExpiryRecord) for r in result.records)
        assert result.records[0].expiry_date == date(2025, 1, 30)
        assert result.records[0].pulled_year == 2025
        bucket.acquire.assert_called_once()
        bucket.record_success.assert_called_once()
        assert upserted[0] == result.records

    def test_month_and_snapshot_passed_through(self) -> None:
        sdk = _FakeSDK(result={"expiries": ["2025-01-30"]})
        fetcher, _, _ = _fetcher(sdk)
        result = fetcher.fetch("NIFTY", "NSE", year=2025, month=1, snapshot_date=date(2025, 1, 1))
        assert sdk.kwargs is not None
        assert sdk.kwargs["month"] == 1
        assert sdk.kwargs["underlying_symbol"] == "NIFTY"
        assert sdk.kwargs["exchange"] == "EX_NSE"
        assert result.records[0].snapshot_date == date(2025, 1, 1)

    def test_default_snapshot_is_clock_date(self) -> None:
        sdk = _FakeSDK(result={"expiries": ["2025-01-30"]})
        fetcher, _, _ = _fetcher(sdk)
        result = fetcher.fetch("NIFTY", "NSE", year=2025)
        assert result.records[0].snapshot_date == date(2026, 5, 28)

    def test_malformed_date_skipped(self) -> None:
        sdk = _FakeSDK(result={"expiries": ["2025-01-30", "not-a-date", "2025-02-27"]})
        fetcher, _, _ = _fetcher(sdk)
        result = fetcher.fetch("NIFTY", "NSE", year=2025)
        assert len(result.records) == 2
        assert result.skipped_dates == 1


class TestGuards:
    def test_year_below_min_is_bad_request(self) -> None:
        fetcher, bucket, _ = _fetcher(_FakeSDK(result={"expiries": []}))
        with pytest.raises(VendorSdkError) as ei:
            fetcher.fetch("NIFTY", "NSE", year=2019)
        assert type(ei.value).__name__ == "VendorAPIBadRequestException"
        bucket.acquire.assert_not_called()

    def test_year_above_max_is_bad_request(self) -> None:
        fetcher, _, _ = _fetcher(_FakeSDK(result={"expiries": []}))
        with pytest.raises(VendorSdkError):
            fetcher.fetch("NIFTY", "NSE", year=2099)  # clock year 2026 → max 2027

    def test_unknown_exchange_is_bad_request(self) -> None:
        fetcher, bucket, _ = _fetcher(_FakeSDK(result={"expiries": []}))
        with pytest.raises(VendorSdkError) as ei:
            fetcher.fetch("NIFTY", "MCX", year=2025)
        assert type(ei.value).__name__ == "VendorAPIBadRequestException"
        bucket.acquire.assert_not_called()

    def test_malformed_response_is_bad_request(self) -> None:
        fetcher, _, _ = _fetcher(_FakeSDK(result={"wrong": []}))
        with pytest.raises(VendorSdkError) as ei:
            fetcher.fetch("NIFTY", "NSE", year=2025)
        assert type(ei.value).__name__ == "VendorAPIBadRequestException"


class TestExceptionTranslation:
    def test_rate_limit_records_hit(self) -> None:
        fetcher, bucket, _ = _fetcher(_FakeSDK(raises=GrowwAPIRateLimitException("429")))
        with pytest.raises(VendorSdkError) as ei:
            fetcher.fetch("NIFTY", "NSE", year=2025)
        assert type(ei.value).__name__ == "VendorAPIRateLimitException"
        bucket.record_rate_limit_hit.assert_called_once()

    def test_not_found_translates_without_hit(self) -> None:
        fetcher, bucket, _ = _fetcher(_FakeSDK(raises=GrowwAPINotFoundException("404")))
        with pytest.raises(VendorSdkError) as ei:
            fetcher.fetch("NIFTY", "NSE", year=2025)
        assert type(ei.value).__name__ == "VendorAPINotFoundException"
        bucket.record_rate_limit_hit.assert_not_called()


class TestDefaults:
    def test_default_clock_and_run_id(self) -> None:
        # No clock/run_id_factory injected → exercises the module defaults.
        sdk = _FakeSDK(result={"expiries": [f"{datetime.now(UTC).year}-01-30"]})

        class _Store:
            def upsert_expiries(self, expiries: Any) -> int:
                return len(list(expiries))

        fetcher = ExpiryFetcher(
            sdk=sdk, bucket=mock.Mock(), base_exception=BaseGrowwException, store=_Store()
        )
        result = fetcher.fetch("NIFTY", "NSE", year=datetime.now(UTC).year)
        assert len(result.records) == 1
        assert result.records[0].run_id.startswith("expiries-")
