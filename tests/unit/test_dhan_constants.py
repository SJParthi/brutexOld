"""Tests for `brutex.adapters.dhan.constants` — verified Dhan v2 facts.

Each assertion pins a value to its `docs/dhan-ref/` source so a silent
drift from the verified docs (Golden Rule #1) fails CI.
"""

from __future__ import annotations

from brutex.adapters.dhan import constants as c


class TestHosts:
    def test_base_url(self) -> None:
        assert c.DHAN_BASE_URL == "https://api.dhan.co/v2/"

    def test_auth_token_url(self) -> None:
        assert c.DHAN_AUTH_TOKEN_URL == "https://auth.dhan.co/app/generateAccessToken"


class TestEndpoints:
    def test_spot_endpoints_are_relative(self) -> None:
        assert c.ENDPOINT_INTRADAY == "charts/intraday"
        assert c.ENDPOINT_HISTORICAL == "charts/historical"

    def test_options_endpoint(self) -> None:
        assert c.ENDPOINT_ROLLING_OPTION == "charts/rollingoption"


class TestInstrumentMaster:
    def test_detailed_master_required_for_fno(self) -> None:
        assert c.INSTRUMENT_MASTER_DETAILED_URL.endswith("api-scrip-master-detailed.csv")
        assert c.INSTRUMENT_MASTER_COMPACT_URL.endswith("api-scrip-master.csv")

    def test_both_served_from_images_host(self) -> None:
        for url in (c.INSTRUMENT_MASTER_COMPACT_URL, c.INSTRUMENT_MASTER_DETAILED_URL):
            assert url.startswith("https://images.dhan.co/api-data/")


class TestAuthHeaders:
    def test_header_names(self) -> None:
        assert c.HEADER_ACCESS_TOKEN == "access-token"
        assert c.HEADER_CLIENT_ID == "dhanClientId"


class TestIntervals:
    def test_supported_intraday_intervals(self) -> None:
        assert c.SUPPORTED_INTRADAY_INTERVALS == ("1", "5", "15", "25", "60")

    def test_track1_pulls_one_minute_only(self) -> None:
        # DEC-DHAN-1M-ONLY-001: spot intraday + options both 1-minute.
        assert c.TRACK1_SPOT_INTRADAY_INTERVAL == "1"
        assert c.TRACK1_OPTIONS_INTERVAL == "1"

    def test_track1_intervals_are_supported(self) -> None:
        assert c.TRACK1_SPOT_INTRADAY_INTERVAL in c.SUPPORTED_INTRADAY_INTERVALS
        assert c.TRACK1_OPTIONS_INTERVAL in c.SUPPORTED_INTRADAY_INTERVALS


class TestEnums:
    def test_exchange_segments(self) -> None:
        assert {"NSE_EQ", "NSE_FNO", "BSE_EQ"} <= c.EXCHANGE_SEGMENTS

    def test_instrument_types(self) -> None:
        assert {"INDEX", "EQUITY", "OPTIDX"} <= c.INSTRUMENT_TYPES

    def test_option_type_and_expiry_flag_values(self) -> None:
        assert c.DRV_OPTION_TYPES == ("CALL", "PUT")
        assert c.EXPIRY_FLAGS == ("WEEK", "MONTH")


class TestLimitsAndWindows:
    def test_data_rate_limit_and_quota(self) -> None:
        assert c.DATA_RATE_LIMIT_RPS == 5
        assert c.DATA_DAILY_QUOTA == 100_000

    def test_chunk_windows(self) -> None:
        assert c.INTRADAY_WINDOW_DAYS == 90
        assert c.ROLLING_OPTION_WINDOW_DAYS == 30

    def test_history_depth(self) -> None:
        assert c.INTRADAY_HISTORY_YEARS == 5


def test_all_exports_are_defined() -> None:
    """Every name in ``__all__`` resolves on the module (no stale exports)."""
    for name in c.__all__:
        assert hasattr(c, name), name
