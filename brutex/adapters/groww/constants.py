"""Groww REST API constants — endpoints, intervals, chunk windows, rate limits.

Every value here is traceable to ``docs/groww/api-reference/`` (Golden Rule
#1/#2). REST endpoints were verified from SDK source inspection on 2026-06-12.

**No growwapi SDK dependency.** All HTTP is plain urllib; only ``pyotp`` is
needed for TOTP generation (DEC-GROWW-REPIVOT-001 / ADR-0009).

This module is **pure data** (no I/O, no mutable state) — safe to import
anywhere in the control plane.
"""

from __future__ import annotations

from datetime import date
from typing import Final

# ── REST base and endpoints ───────────────────────────────────────────────
#: Groww v1 API base.
GROWW_BASE_URL: Final[str] = "https://api.groww.in/v1"

#: TOTP access-token issuance endpoint (POST).
GROWW_AUTH_URL: Final[str] = "https://api.groww.in/v1/token/api/access"

#: Historical candle data endpoint (GET, relative to base).
CANDLES_PATH: Final[str] = "historical/candles"

#: FNO expiry dates endpoint (GET, relative to base).
EXPIRIES_PATH: Final[str] = "historical/expiries"

#: FNO contract symbols endpoint (GET, relative to base).
CONTRACTS_PATH: Final[str] = "historical/contracts"

#: Instrument master CSV — all live tradable instruments.
INSTRUMENT_CSV_URL: Final[str] = (
    "https://growwapi-assets.groww.in/instruments/instrument.csv"
)

# ── Auth header constants (verified from SDK source: _build_headers) ─────
HEADER_AUTHORIZATION: Final[str] = "Authorization"
HEADER_REQUEST_ID: Final[str] = "x-request-id"
HEADER_CLIENT_ID: Final[str] = "x-client-id"
HEADER_CLIENT_PLATFORM: Final[str] = "x-client-platform"
HEADER_CLIENT_PLATFORM_VERSION: Final[str] = "x-client-platform-version"
HEADER_API_VERSION: Final[str] = "x-api-version"
HEADER_CONTENT_TYPE: Final[str] = "Content-Type"

GROWW_CLIENT_ID: Final[str] = "brutex"
GROWW_CLIENT_PLATFORM: Final[str] = "brutex-backtesting"
GROWW_CLIENT_PLATFORM_VERSION: Final[str] = "1.0.0"
GROWW_API_VERSION: Final[str] = "1.0"

# ── SSRF guard ────────────────────────────────────────────────────────────
#: Only api.groww.in is permitted as a request target.
ALLOWED_HOSTS: Final[frozenset[str]] = frozenset({"api.groww.in"})

# ── Auth key type (TOTP flow only — API-key+secret requires daily approval,
#: violating CLAUDE.md §11 AUTOMATED rule) ─────────────────────────────────
AUTH_KEY_TYPE_TOTP: Final[str] = "totp"

# ── Exchange strings (verified: GrowwAPI.EXCHANGE_NSE, EXCHANGE_BSE) ─────
EXCHANGE_NSE: Final[str] = "NSE"
EXCHANGE_BSE: Final[str] = "BSE"

# ── Segment strings (verified: GrowwAPI.SEGMENT_CASH, SEGMENT_FNO) ───────
SEGMENT_CASH: Final[str] = "CASH"
SEGMENT_FNO: Final[str] = "FNO"

# ── Candle intervals (verified from GrowwAPI.CANDLE_INTERVAL_* constants) ─
CANDLE_INTERVAL_1M: Final[str] = "1minute"
CANDLE_INTERVAL_2M: Final[str] = "2minute"
CANDLE_INTERVAL_3M: Final[str] = "3minute"
CANDLE_INTERVAL_5M: Final[str] = "5minute"
CANDLE_INTERVAL_10M: Final[str] = "10minute"
CANDLE_INTERVAL_15M: Final[str] = "15minute"
CANDLE_INTERVAL_30M: Final[str] = "30minute"
CANDLE_INTERVAL_1H: Final[str] = "1hour"
CANDLE_INTERVAL_4H: Final[str] = "4hour"
CANDLE_INTERVAL_1D: Final[str] = "1day"
CANDLE_INTERVAL_1W: Final[str] = "1week"
CANDLE_INTERVAL_1MO: Final[str] = "1month"

#: All supported candle interval strings.
ALL_INTERVALS: Final[frozenset[str]] = frozenset(
    {
        CANDLE_INTERVAL_1M,
        CANDLE_INTERVAL_2M,
        CANDLE_INTERVAL_3M,
        CANDLE_INTERVAL_5M,
        CANDLE_INTERVAL_10M,
        CANDLE_INTERVAL_15M,
        CANDLE_INTERVAL_30M,
        CANDLE_INTERVAL_1H,
        CANDLE_INTERVAL_4H,
        CANDLE_INTERVAL_1D,
        CANDLE_INTERVAL_1W,
        CANDLE_INTERVAL_1MO,
    }
)

#: Track-1 spot intraday interval — **1-minute only** (same policy as Dhan;
#: other TFs are resampled from the 1m base, never fetched).
TRACK1_SPOT_INTERVAL: Final[str] = CANDLE_INTERVAL_1M

#: Track-1 daily interval used for PDH/PDL anchors.
TRACK1_DAILY_INTERVAL: Final[str] = CANDLE_INTERVAL_1D

# ── Chunk windows (docs/groww/api-reference/03-backtesting.md) ───────────
#: Short-window intervals (1m/2m/3m/5m): 30 calendar days per API call.
CHUNK_WINDOW_SHORT_DAYS: Final[int] = 30

#: Medium-window intervals (10m/15m/30m): 90 calendar days per API call.
CHUNK_WINDOW_MED_DAYS: Final[int] = 90

#: Long-window intervals (1h/4h/1d/1w/1mo): 180 calendar days per API call.
CHUNK_WINDOW_LONG_DAYS: Final[int] = 180

#: Interval → chunk window (calendar days, inclusive per call).
INTERVAL_CHUNK_WINDOW: Final[dict[str, int]] = {
    CANDLE_INTERVAL_1M: CHUNK_WINDOW_SHORT_DAYS,
    CANDLE_INTERVAL_2M: CHUNK_WINDOW_SHORT_DAYS,
    CANDLE_INTERVAL_3M: CHUNK_WINDOW_SHORT_DAYS,
    CANDLE_INTERVAL_5M: CHUNK_WINDOW_SHORT_DAYS,
    CANDLE_INTERVAL_10M: CHUNK_WINDOW_MED_DAYS,
    CANDLE_INTERVAL_15M: CHUNK_WINDOW_MED_DAYS,
    CANDLE_INTERVAL_30M: CHUNK_WINDOW_MED_DAYS,
    CANDLE_INTERVAL_1H: CHUNK_WINDOW_LONG_DAYS,
    CANDLE_INTERVAL_4H: CHUNK_WINDOW_LONG_DAYS,
    CANDLE_INTERVAL_1D: CHUNK_WINDOW_LONG_DAYS,
    CANDLE_INTERVAL_1W: CHUNK_WINDOW_LONG_DAYS,
    CANDLE_INTERVAL_1MO: CHUNK_WINDOW_LONG_DAYS,
}

# ── Historical depth ──────────────────────────────────────────────────────
#: Earliest date Groww serves data for (equities, indices, FNO).
#: Source: docs/groww/api-reference/03-backtesting.md ("available from 2020").
HISTORY_START: Final[date] = date(2020, 1, 1)

# ── Rate limit (adaptive range) ───────────────────────────────────────────
#: Documented Live-Data bucket: 10 r/s. Backtesting bucket: UNVERIFIED.
#: Adaptive limiter starts at MAX and steps down on 429s (ADR-0009).
RATE_LIMIT_MIN_RPS: Final[float] = 4.0
RATE_LIMIT_MAX_RPS: Final[float] = 8.0
RATE_LIMIT_INITIAL_RPS: Final[float] = 8.0
RATE_LIMIT_STEP_DOWN_HITS: Final[int] = 3
RATE_LIMIT_STEP_DOWN_WINDOW_SEC: Final[float] = 60.0
RATE_LIMIT_STEP_UP_CLEAN_SEC: Final[float] = 300.0

# ── Market hours (IST) ────────────────────────────────────────────────────
#: Intraday fetch start time (IST string, appended to date for API calls).
MARKET_OPEN_IST: Final[str] = "09:15:00"

#: Intraday fetch end time (IST string, appended to date for API calls).
MARKET_CLOSE_IST: Final[str] = "15:30:00"

# ── Track-1 brute-force universe (instrument_name → (groww_symbol, exchange, segment))
#: India VIX is reference-only (CLAUDE.md §2 Rule 3) — fetched + stamped on
#: every trade but NEVER part of brute-force pattern combinations.
BRUTE_FORCE_UNIVERSE: Final[dict[str, tuple[str, str, str]]] = {
    "NIFTY": ("NSE-NIFTY", EXCHANGE_NSE, SEGMENT_CASH),
    "BANKNIFTY": ("NSE-BANKNIFTY", EXCHANGE_NSE, SEGMENT_CASH),
    "SENSEX": ("BSE-SENSEX", EXCHANGE_BSE, SEGMENT_CASH),
    "INDIAVIX": ("NSE-INDIAVIX", EXCHANGE_NSE, SEGMENT_CASH),
}

# ── Auth env vars ─────────────────────────────────────────────────────────
ENV_API_KEY: Final[str] = "BRUTEX_GROWW_API_KEY"
ENV_TOTP_SECRET: Final[str] = "BRUTEX_GROWW_TOTP_SECRET"  # noqa: S105
ENV_TOKEN_PATH: Final[str] = "BRUTEX_GROWW_TOKEN_PATH"  # noqa: S105
ENV_SMOKE: Final[str] = "BRUTEX_GROWW_SMOKE"
DEFAULT_TOKEN_PATH: Final[str] = "~/.brutex/groww/access_token.json"  # noqa: S105

# ── Request timeout ───────────────────────────────────────────────────────
#: Default per-call timeout seconds (generous for bulk historical pulls).
DEFAULT_TIMEOUT_SEC: Final[int] = 30

__all__ = [
    "ALLOWED_HOSTS",
    "ALL_INTERVALS",
    "AUTH_KEY_TYPE_TOTP",
    "BRUTE_FORCE_UNIVERSE",
    "CANDLES_PATH",
    "CANDLE_INTERVAL_1D",
    "CANDLE_INTERVAL_1H",
    "CANDLE_INTERVAL_1M",
    "CANDLE_INTERVAL_1MO",
    "CANDLE_INTERVAL_1W",
    "CANDLE_INTERVAL_2M",
    "CANDLE_INTERVAL_3M",
    "CANDLE_INTERVAL_4H",
    "CANDLE_INTERVAL_5M",
    "CANDLE_INTERVAL_10M",
    "CANDLE_INTERVAL_15M",
    "CANDLE_INTERVAL_30M",
    "CHUNK_WINDOW_LONG_DAYS",
    "CHUNK_WINDOW_MED_DAYS",
    "CHUNK_WINDOW_SHORT_DAYS",
    "CONTRACTS_PATH",
    "DEFAULT_TIMEOUT_SEC",
    "DEFAULT_TOKEN_PATH",
    "ENV_API_KEY",
    "ENV_SMOKE",
    "ENV_TOKEN_PATH",
    "ENV_TOTP_SECRET",
    "EXCHANGE_BSE",
    "EXCHANGE_NSE",
    "EXPIRIES_PATH",
    "GROWW_API_VERSION",
    "GROWW_AUTH_URL",
    "GROWW_BASE_URL",
    "GROWW_CLIENT_ID",
    "GROWW_CLIENT_PLATFORM",
    "GROWW_CLIENT_PLATFORM_VERSION",
    "HEADER_API_VERSION",
    "HEADER_AUTHORIZATION",
    "HEADER_CLIENT_ID",
    "HEADER_CLIENT_PLATFORM",
    "HEADER_CLIENT_PLATFORM_VERSION",
    "HEADER_CONTENT_TYPE",
    "HEADER_REQUEST_ID",
    "HISTORY_START",
    "INSTRUMENT_CSV_URL",
    "INTERVAL_CHUNK_WINDOW",
    "MARKET_CLOSE_IST",
    "MARKET_OPEN_IST",
    "RATE_LIMIT_INITIAL_RPS",
    "RATE_LIMIT_MAX_RPS",
    "RATE_LIMIT_MIN_RPS",
    "RATE_LIMIT_STEP_DOWN_HITS",
    "RATE_LIMIT_STEP_DOWN_WINDOW_SEC",
    "RATE_LIMIT_STEP_UP_CLEAN_SEC",
    "SEGMENT_CASH",
    "SEGMENT_FNO",
    "TRACK1_DAILY_INTERVAL",
    "TRACK1_SPOT_INTERVAL",
]
