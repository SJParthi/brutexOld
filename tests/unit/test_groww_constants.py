"""Tests for `brutex.adapters.groww.constants` — pin verified API facts.

Each assertion pins a value to its `docs/groww/api-reference/` source so a
silent drift from verified docs (Golden Rule #1) fails CI.
"""

from __future__ import annotations

from datetime import date

from brutex.adapters.groww import constants as c


class TestEndpoints:
    def test_base_url(self) -> None:
        assert c.GROWW_BASE_URL == "https://api.groww.in/v1"

    def test_auth_url(self) -> None:
        assert c.GROWW_AUTH_URL == "https://api.groww.in/v1/token/api/access"

    def test_candles_path(self) -> None:
        assert c.CANDLES_PATH == "historical/candles"

    def test_expiries_path(self) -> None:
        assert c.EXPIRIES_PATH == "historical/expiries"

    def test_contracts_path(self) -> None:
        assert c.CONTRACTS_PATH == "historical/contracts"

    def test_instrument_csv_url(self) -> None:
        assert "growwapi-assets.groww.in" in c.INSTRUMENT_CSV_URL
        assert c.INSTRUMENT_CSV_URL.endswith("instrument.csv")


class TestHeaders:
    def test_header_names_present(self) -> None:
        assert c.HEADER_AUTHORIZATION == "Authorization"
        assert c.HEADER_REQUEST_ID == "x-request-id"
        assert c.HEADER_CLIENT_ID == "x-client-id"
        assert c.HEADER_CLIENT_PLATFORM == "x-client-platform"
        assert c.HEADER_CLIENT_PLATFORM_VERSION == "x-client-platform-version"
        assert c.HEADER_API_VERSION == "x-api-version"
        assert c.HEADER_CONTENT_TYPE == "Content-Type"


class TestSsrfGuard:
    def test_only_api_groww_in_allowed(self) -> None:
        assert c.ALLOWED_HOSTS == frozenset({"api.groww.in"})

    def test_auth_url_host_not_in_allowed(self) -> None:
        # Auth URL uses the SAME host — ensure it's covered.
        from urllib.parse import urlparse

        host = urlparse(c.GROWW_AUTH_URL).hostname
        assert host == "api.groww.in"
        assert host in c.ALLOWED_HOSTS


class TestExchangeAndSegment:
    def test_exchange_strings(self) -> None:
        assert c.EXCHANGE_NSE == "NSE"
        assert c.EXCHANGE_BSE == "BSE"

    def test_segment_strings(self) -> None:
        assert c.SEGMENT_CASH == "CASH"
        assert c.SEGMENT_FNO == "FNO"


class TestCandleIntervals:
    def test_all_individual_constants(self) -> None:
        assert c.CANDLE_INTERVAL_1M == "1minute"
        assert c.CANDLE_INTERVAL_2M == "2minute"
        assert c.CANDLE_INTERVAL_3M == "3minute"
        assert c.CANDLE_INTERVAL_5M == "5minute"
        assert c.CANDLE_INTERVAL_10M == "10minute"
        assert c.CANDLE_INTERVAL_15M == "15minute"
        assert c.CANDLE_INTERVAL_30M == "30minute"
        assert c.CANDLE_INTERVAL_1H == "1hour"
        assert c.CANDLE_INTERVAL_4H == "4hour"
        assert c.CANDLE_INTERVAL_1D == "1day"
        assert c.CANDLE_INTERVAL_1W == "1week"
        assert c.CANDLE_INTERVAL_1MO == "1month"

    def test_all_intervals_frozenset_has_12_members(self) -> None:
        assert len(c.ALL_INTERVALS) == 12

    def test_all_intervals_contains_each_constant(self) -> None:
        expected = {
            c.CANDLE_INTERVAL_1M, c.CANDLE_INTERVAL_2M, c.CANDLE_INTERVAL_3M,
            c.CANDLE_INTERVAL_5M, c.CANDLE_INTERVAL_10M, c.CANDLE_INTERVAL_15M,
            c.CANDLE_INTERVAL_30M, c.CANDLE_INTERVAL_1H, c.CANDLE_INTERVAL_4H,
            c.CANDLE_INTERVAL_1D, c.CANDLE_INTERVAL_1W, c.CANDLE_INTERVAL_1MO,
        }
        assert expected == c.ALL_INTERVALS

    def test_track1_spot_interval_is_1m(self) -> None:
        assert c.TRACK1_SPOT_INTERVAL == c.CANDLE_INTERVAL_1M

    def test_track1_daily_interval_is_1d(self) -> None:
        assert c.TRACK1_DAILY_INTERVAL == c.CANDLE_INTERVAL_1D


class TestChunkWindows:
    def test_short_window(self) -> None:
        assert c.CHUNK_WINDOW_SHORT_DAYS == 30

    def test_med_window(self) -> None:
        assert c.CHUNK_WINDOW_MED_DAYS == 90

    def test_long_window(self) -> None:
        assert c.CHUNK_WINDOW_LONG_DAYS == 180

    def test_interval_chunk_window_covers_all_intervals(self) -> None:
        assert set(c.INTERVAL_CHUNK_WINDOW.keys()) == c.ALL_INTERVALS

    def test_short_window_intervals(self) -> None:
        for iv in (c.CANDLE_INTERVAL_1M, c.CANDLE_INTERVAL_2M,
                   c.CANDLE_INTERVAL_3M, c.CANDLE_INTERVAL_5M):
            assert c.INTERVAL_CHUNK_WINDOW[iv] == c.CHUNK_WINDOW_SHORT_DAYS

    def test_med_window_intervals(self) -> None:
        for iv in (c.CANDLE_INTERVAL_10M, c.CANDLE_INTERVAL_15M, c.CANDLE_INTERVAL_30M):
            assert c.INTERVAL_CHUNK_WINDOW[iv] == c.CHUNK_WINDOW_MED_DAYS

    def test_long_window_intervals(self) -> None:
        for iv in (c.CANDLE_INTERVAL_1H, c.CANDLE_INTERVAL_4H,
                   c.CANDLE_INTERVAL_1D, c.CANDLE_INTERVAL_1W, c.CANDLE_INTERVAL_1MO):
            assert c.INTERVAL_CHUNK_WINDOW[iv] == c.CHUNK_WINDOW_LONG_DAYS


class TestHistoryStart:
    def test_history_starts_2020(self) -> None:
        assert c.HISTORY_START == date(2020, 1, 1)


class TestRateLimit:
    def test_initial_equals_max(self) -> None:
        assert c.RATE_LIMIT_INITIAL_RPS == c.RATE_LIMIT_MAX_RPS

    def test_min_less_than_max(self) -> None:
        assert c.RATE_LIMIT_MIN_RPS < c.RATE_LIMIT_MAX_RPS

    def test_step_down_hits_positive(self) -> None:
        assert c.RATE_LIMIT_STEP_DOWN_HITS > 0


class TestMarketHours:
    def test_market_open(self) -> None:
        assert c.MARKET_OPEN_IST == "09:15:00"

    def test_market_close(self) -> None:
        assert c.MARKET_CLOSE_IST == "15:30:00"


class TestBruteForceUniverse:
    def test_four_instruments(self) -> None:
        assert len(c.BRUTE_FORCE_UNIVERSE) == 4

    def test_nifty_entry(self) -> None:
        sym, exch, seg = c.BRUTE_FORCE_UNIVERSE["NIFTY"]
        assert sym == "NSE-NIFTY"
        assert exch == "NSE"
        assert seg == "CASH"

    def test_banknifty_entry(self) -> None:
        sym, exch, seg = c.BRUTE_FORCE_UNIVERSE["BANKNIFTY"]
        assert sym == "NSE-BANKNIFTY"
        assert exch == "NSE"
        assert seg == "CASH"

    def test_sensex_entry(self) -> None:
        sym, exch, seg = c.BRUTE_FORCE_UNIVERSE["SENSEX"]
        assert sym == "BSE-SENSEX"
        assert exch == "BSE"
        assert seg == "CASH"

    def test_indiavix_entry(self) -> None:
        sym, exch, seg = c.BRUTE_FORCE_UNIVERSE["INDIAVIX"]
        assert sym == "NSE-INDIAVIX"
        assert exch == "NSE"
        assert seg == "CASH"


class TestEnvVars:
    def test_api_key_env_name(self) -> None:
        assert c.ENV_API_KEY == "BRUTEX_GROWW_API_KEY"

    def test_totp_secret_env_name(self) -> None:
        assert c.ENV_TOTP_SECRET == "BRUTEX_GROWW_TOTP_SECRET"

    def test_token_path_env_name(self) -> None:
        assert c.ENV_TOKEN_PATH == "BRUTEX_GROWW_TOKEN_PATH"

    def test_smoke_env_name(self) -> None:
        assert c.ENV_SMOKE == "BRUTEX_GROWW_SMOKE"

    def test_default_token_path_in_home(self) -> None:
        assert c.DEFAULT_TOKEN_PATH.startswith("~")
        assert "groww" in c.DEFAULT_TOKEN_PATH


class TestTimeout:
    def test_timeout_positive(self) -> None:
        assert c.DEFAULT_TIMEOUT_SEC > 0
