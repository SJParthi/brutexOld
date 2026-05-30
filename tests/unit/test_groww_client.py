"""Tests for `brutex.adapters.groww.client` — the real Groww candle fetcher.

A fake ``growwapi`` SDK (constants + ``get_historical_candles``) and a fake
``Groww*`` exception hierarchy let us prove — with no SDK installed — that the
client paces via the bucket, maps ChunkKey → SDK kwargs (exchange / segment /
interval / epoch), and translates every SDK exception to its neutral ``Vendor*``
twin. Kept free of any ``brutex.orchestrator`` import so the module stays
standalone-coverage-gateable (the loop/classifier round-trip is proven in
``test_groww_client_integration.py``).
"""

from __future__ import annotations

from typing import Any
from unittest import mock

import pytest
from brutex.adapters.groww.client import (
    GrowwCandleFetcher,
    GrowwFetchError,
    VendorSdkError,
)
from brutex.data.watermark import ChunkKey


# ── Fake growwapi exception hierarchy (names mirror the real SDK) ──────────
class BaseGrowwException(Exception): ...


class GrowwAPIException(BaseGrowwException): ...


class GrowwAPIRateLimitException(GrowwAPIException): ...


class GrowwAPIAuthenticationException(GrowwAPIException): ...


class GrowwAPINotFoundException(GrowwAPIException): ...


class GrowwAPIRateLimitBurstException(GrowwAPIRateLimitException):
    """A subclass NOT in the name table — must resolve via its parent."""


# ── Fake SDK ────────────────────────────────────────────────────────────
class _FakeSDK:
    """Exposes the constants the client reads + a scriptable candle call."""

    EXCHANGE_NSE = "EX_NSE"
    EXCHANGE_BSE = "EX_BSE"
    SEGMENT_CASH = "SEG_CASH"
    SEGMENT_FNO = "SEG_FNO"
    CANDLE_INTERVAL_MIN_1 = "CI_MIN_1"
    CANDLE_INTERVAL_DAY = "CI_DAY"

    def __init__(self, result: object = None, raises: BaseException | None = None) -> None:
        self._result = result
        self._raises = raises
        self.last_kwargs: dict[str, object] | None = None

    def get_historical_candles(self, **kwargs: object) -> object:
        self.last_kwargs = kwargs
        if self._raises is not None:
            raise self._raises
        return self._result


def _key(
    symbol: str = "NSE-NIFTY",
    interval: str = "1m",
    start: str = "2021-01-04T09:15:00+00:00",
    end: str = "2021-02-03T09:15:00+00:00",
) -> ChunkKey:
    return ChunkKey(symbol, interval, start, end)


def _fetcher(sdk: _FakeSDK, **kw: Any) -> tuple[GrowwCandleFetcher, mock.Mock]:
    bucket = mock.Mock()
    fetcher = GrowwCandleFetcher(sdk=sdk, bucket=bucket, base_exception=BaseGrowwException, **kw)
    return fetcher, bucket


class TestSuccess:
    def test_returns_rows_and_records_success(self) -> None:
        rows = [["2021-01-04T09:15:00", 1, 2, 0, 1, 10, None]]
        fetcher, bucket = _fetcher(_FakeSDK(result={"candles": rows}))
        out = fetcher.fetch_chunk(_key())
        assert out == rows
        bucket.acquire.assert_called_once()
        bucket.record_success.assert_called_once()
        bucket.record_rate_limit_hit.assert_not_called()

    def test_empty_candles_returns_empty(self) -> None:
        fetcher, bucket = _fetcher(_FakeSDK(result={"candles": []}))
        assert fetcher.fetch_chunk(_key()) == []
        bucket.record_success.assert_called_once()


class TestRequestMapping:
    def test_index_maps_to_cash_nse_and_ist_time(self) -> None:
        sdk = _FakeSDK(result={"candles": []})
        fetcher, _ = _fetcher(sdk)
        fetcher.fetch_chunk(_key("NSE-NIFTY", "1m"))
        kw = sdk.last_kwargs
        assert kw is not None
        assert kw["exchange"] == "EX_NSE"
        assert kw["segment"] == "SEG_CASH"
        assert kw["groww_symbol"] == "NSE-NIFTY"
        assert kw["candle_interval"] == "CI_MIN_1"
        # The SDK documents start/end as "yyyy-MM-dd HH:mm:ss" IST strings —
        # NOT an integer epoch. 2021-01-04 09:15 UTC == 14:45 IST.
        assert kw["start_time"] == "2021-01-04 14:45:00"
        assert kw["end_time"] == "2021-02-03 14:45:00"

    def test_sensex_maps_to_bse(self) -> None:
        sdk = _FakeSDK(result={"candles": []})
        fetcher, _ = _fetcher(sdk)
        fetcher.fetch_chunk(_key("BSE-SENSEX", "1d"))
        assert sdk.last_kwargs is not None
        assert sdk.last_kwargs["exchange"] == "EX_BSE"
        assert sdk.last_kwargs["segment"] == "SEG_CASH"
        assert sdk.last_kwargs["candle_interval"] == "CI_DAY"

    @pytest.mark.parametrize(
        "symbol",
        [
            "NSE-NIFTY-30Sep25-FUT",
            "NSE-NIFTY-30Sep25-24650-CE",
            "NSE-NIFTY-30Sep25-24650-PE",
        ],
    )
    def test_derivatives_map_to_fno(self, symbol: str) -> None:
        sdk = _FakeSDK(result={"candles": []})
        fetcher, _ = _fetcher(sdk)
        fetcher.fetch_chunk(_key(symbol, "1m"))
        assert sdk.last_kwargs is not None
        assert sdk.last_kwargs["segment"] == "SEG_FNO"


class TestRequestErrors:
    def test_unknown_interval_is_bad_request(self) -> None:
        fetcher, bucket = _fetcher(_FakeSDK(result={"candles": []}))
        with pytest.raises(VendorSdkError) as ei:
            fetcher.fetch_chunk(_key("NSE-NIFTY", "7m"))
        assert type(ei.value).__name__ == "VendorAPIBadRequestException"
        bucket.acquire.assert_not_called()  # failed before pacing

    def test_unparseable_timestamp_is_bad_request(self) -> None:
        fetcher, _ = _fetcher(_FakeSDK(result={"candles": []}))
        with pytest.raises(VendorSdkError) as ei:
            fetcher.fetch_chunk(_key(start="not-a-date"))
        assert type(ei.value).__name__ == "VendorAPIBadRequestException"

    def test_missing_sdk_constant_is_bad_request(self) -> None:
        # An exchange the fake SDK has no EXCHANGE_* constant for.
        fetcher, _ = _fetcher(_FakeSDK(result={"candles": []}))
        with pytest.raises(VendorSdkError) as ei:
            fetcher.fetch_chunk(_key("MCX-GOLD", "1m"))
        assert type(ei.value).__name__ == "VendorAPIBadRequestException"


class TestResponseErrors:
    def test_missing_candles_key_is_api_error(self) -> None:
        fetcher, bucket = _fetcher(_FakeSDK(result={"wrong": []}))
        with pytest.raises(VendorSdkError) as ei:
            fetcher.fetch_chunk(_key())
        assert type(ei.value).__name__ == "VendorAPIException"
        bucket.record_success.assert_not_called()

    def test_non_dict_response_is_api_error(self) -> None:
        fetcher, _ = _fetcher(_FakeSDK(result=None))
        with pytest.raises(VendorSdkError) as ei:
            fetcher.fetch_chunk(_key())
        assert type(ei.value).__name__ == "VendorAPIException"


class TestExceptionTranslation:
    def test_rate_limit_translates_and_records_hit(self) -> None:
        fetcher, bucket = _fetcher(_FakeSDK(raises=GrowwAPIRateLimitException("429")))
        with pytest.raises(VendorSdkError) as ei:
            fetcher.fetch_chunk(_key())
        assert type(ei.value).__name__ == "VendorAPIRateLimitException"
        bucket.record_rate_limit_hit.assert_called_once()
        bucket.record_success.assert_not_called()

    def test_auth_translates_without_recording_hit(self) -> None:
        fetcher, bucket = _fetcher(_FakeSDK(raises=GrowwAPIAuthenticationException("401")))
        with pytest.raises(VendorSdkError) as ei:
            fetcher.fetch_chunk(_key())
        assert type(ei.value).__name__ == "VendorAPIAuthenticationException"
        bucket.record_rate_limit_hit.assert_not_called()

    def test_not_found_translates(self) -> None:
        fetcher, _ = _fetcher(_FakeSDK(raises=GrowwAPINotFoundException("404")))
        with pytest.raises(VendorSdkError) as ei:
            fetcher.fetch_chunk(_key())
        assert type(ei.value).__name__ == "VendorAPINotFoundException"

    def test_subclass_resolves_via_parent_in_mro(self) -> None:
        # Name not in the table → walk MRO → matches GrowwAPIRateLimitException.
        fetcher, bucket = _fetcher(_FakeSDK(raises=GrowwAPIRateLimitBurstException("burst")))
        with pytest.raises(VendorSdkError) as ei:
            fetcher.fetch_chunk(_key())
        assert type(ei.value).__name__ == "VendorAPIRateLimitException"
        bucket.record_rate_limit_hit.assert_called_once()

    def test_base_exception_translates_to_vendor_base(self) -> None:
        fetcher, _ = _fetcher(_FakeSDK(raises=BaseGrowwException("root")))
        with pytest.raises(VendorSdkError) as ei:
            fetcher.fetch_chunk(_key())
        assert type(ei.value).__name__ == "VendorBaseException"


class TestReauth:
    def test_reauth_swaps_sdk(self) -> None:
        new_sdk = _FakeSDK(result={"candles": [["x"]]})
        fetcher, _ = _fetcher(_FakeSDK(result={"candles": []}), reauth_fn=lambda: new_sdk)
        fetcher.reauth()
        assert fetcher.sdk is new_sdk

    def test_reauth_without_callback_raises(self) -> None:
        fetcher, _ = _fetcher(_FakeSDK(result={"candles": []}), reauth_fn=None)
        with pytest.raises(GrowwFetchError, match="no reauth_fn"):
            fetcher.reauth()


class TestToGrowwTime:
    """`_to_groww_time` formats the chunk boundary as Groww's documented IST
    "yyyy-MM-dd HH:mm:ss" string (DEC-PULL-CANDLE-TIME-FORMAT-001)."""

    def test_tz_aware_utc_to_ist_string(self) -> None:
        from brutex.adapters.groww.client import _to_groww_time

        # 09:15 UTC + 5:30 == 14:45 IST.
        assert _to_groww_time("2021-01-04T09:15:00+00:00") == "2021-01-04 14:45:00"

    def test_naive_is_treated_as_utc(self) -> None:
        from brutex.adapters.groww.client import _to_groww_time

        # A naive ISO string is assumed UTC, then converted to IST.
        assert _to_groww_time("2021-01-04T09:15:00") == "2021-01-04 14:45:00"
