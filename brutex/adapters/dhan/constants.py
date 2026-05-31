"""Dhan v2 verified constants — endpoints, intervals, segments, limits.

Every value here is traceable to ``docs/dhan-ref/`` (Golden Rule #1/#2):

* base + endpoints + response/limit facts — ``docs/dhan-ref/01,05,06``
* auth headers + token URL — ``docs/dhan-ref/02``
* instrument master + segments — ``docs/dhan-ref/09``

Track-1 fetch policy is **1-minute only for spot AND options**, plus
1-day for spot anchors (DEC-DHAN-1M-ONLY-001, MIGRATION #343): the
``5/15/25/60`` intraday values are supported by Dhan but never pulled —
the derived timeframes are resampled from the 1m base.

This module is **pure data** (no I/O, no mutable state) so it is a safe,
O(1) lookup surface importable from anywhere in the control plane.
"""

from __future__ import annotations

from typing import Final

# ── Hosts ────────────────────────────────────────────────────────────────
#: Dhan v2 REST base (``docs/dhan-ref/01-introduction-and-rate-limits.md``).
DHAN_BASE_URL: Final[str] = "https://api.dhan.co/v2/"

#: TOTP access-token issuance host (``docs/dhan-ref/02-authentication.md``).
DHAN_AUTH_TOKEN_URL: Final[str] = "https://auth.dhan.co/app/generateAccessToken"  # noqa: S105  # public URL, not a secret

# ── Data endpoints (relative to DHAN_BASE_URL) ───────────────────────────
#: Spot intraday OHLC — SPOT underlying only (``docs/dhan-ref/05``).
ENDPOINT_INTRADAY: Final[str] = "charts/intraday"

#: Spot daily OHLC (PDH/PDL anchors), to instrument inception.
ENDPOINT_HISTORICAL: Final[str] = "charts/historical"

#: Expired-options rolling OHLC — the ONLY option source (``docs/dhan-ref/06``).
ENDPOINT_ROLLING_OPTION: Final[str] = "charts/rollingoption"

#: Renew an active (web-generated) access token for another 24h (``docs/dhan-ref/02``).
ENDPOINT_RENEW_TOKEN: Final[str] = "RenewToken"  # noqa: S105  # endpoint path, not a secret

# ── Instrument master CSVs (``docs/dhan-ref/09``) ────────────────────────
#: Compact scrip master.
INSTRUMENT_MASTER_COMPACT_URL: Final[str] = "https://images.dhan.co/api-data/api-scrip-master.csv"

#: Detailed scrip master — required for F&O (underlying ids, ISIN, lot/tick).
INSTRUMENT_MASTER_DETAILED_URL: Final[str] = (
    "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"
)

# ── Auth headers (``docs/dhan-ref/02``) ──────────────────────────────────
#: Primary auth header — JWT access token (24h).
HEADER_ACCESS_TOKEN: Final[str] = "access-token"  # noqa: S105  # HTTP header name, not a secret

#: Secondary header — Dhan client id.
HEADER_CLIENT_ID: Final[str] = "dhanClientId"

# ── Intervals ────────────────────────────────────────────────────────────
#: All intraday intervals Dhan supports (minutes, as request strings).
SUPPORTED_INTRADAY_INTERVALS: Final[tuple[str, ...]] = ("1", "5", "15", "25", "60")

#: Track-1 SPOT intraday pull — **1-minute only** (DEC-DHAN-1M-ONLY-001).
TRACK1_SPOT_INTRADAY_INTERVAL: Final[str] = "1"

#: Track-1 OPTIONS pull — **1-minute only** (DEC-DHAN-1M-ONLY-001).
TRACK1_OPTIONS_INTERVAL: Final[str] = "1"

# ── Enums (``docs/dhan-ref/06,09``) ──────────────────────────────────────
#: Exchange-segment enum values used by the Data APIs.
EXCHANGE_SEGMENTS: Final[frozenset[str]] = frozenset({"NSE_EQ", "NSE_FNO", "BSE_EQ", "BSE_FNO"})

#: Instrument-type enum values (Track 1 uses INDEX + EQUITY for spot,
#: OPTIDX/OPTSTK for the lake options surface).
INSTRUMENT_TYPES: Final[frozenset[str]] = frozenset(
    {"INDEX", "EQUITY", "OPTIDX", "OPTSTK", "FUTIDX", "FUTSTK"}
)

#: ``drvOptionType`` values for the rolling-option endpoint.
DRV_OPTION_TYPES: Final[tuple[str, ...]] = ("CALL", "PUT")

#: ``expiryFlag`` values for the rolling-option endpoint.
EXPIRY_FLAGS: Final[tuple[str, ...]] = ("WEEK", "MONTH")

# ── Rate limits + quota (``docs/dhan-ref/01``) ───────────────────────────
#: Verified Data-API ceiling — requests/second (HARD cap, never exceed).
DATA_RATE_LIMIT_RPS: Final[int] = 5

#: Verified Data-API daily quota.
DATA_DAILY_QUOTA: Final[int] = 100_000

# ── Chunk windows (``docs/dhan-ref/05,06``) ──────────────────────────────
#: Max intraday window per call (days) — single Dhan cap (vs Groww's tiering).
INTRADAY_WINDOW_DAYS: Final[int] = 90

#: Max rolling-option window per call (days).
ROLLING_OPTION_WINDOW_DAYS: Final[int] = 30

#: Intraday history depth (years) — daily history goes back to inception.
INTRADAY_HISTORY_YEARS: Final[int] = 5


__all__ = [
    "DATA_DAILY_QUOTA",
    "DATA_RATE_LIMIT_RPS",
    "DHAN_AUTH_TOKEN_URL",
    "DHAN_BASE_URL",
    "DRV_OPTION_TYPES",
    "ENDPOINT_HISTORICAL",
    "ENDPOINT_INTRADAY",
    "ENDPOINT_RENEW_TOKEN",
    "ENDPOINT_ROLLING_OPTION",
    "EXCHANGE_SEGMENTS",
    "EXPIRY_FLAGS",
    "HEADER_ACCESS_TOKEN",
    "HEADER_CLIENT_ID",
    "INSTRUMENT_MASTER_COMPACT_URL",
    "INSTRUMENT_MASTER_DETAILED_URL",
    "INSTRUMENT_TYPES",
    "INTRADAY_HISTORY_YEARS",
    "INTRADAY_WINDOW_DAYS",
    "ROLLING_OPTION_WINDOW_DAYS",
    "SUPPORTED_INTRADAY_INTERVALS",
    "TRACK1_OPTIONS_INTERVAL",
    "TRACK1_SPOT_INTRADAY_INTERVAL",
]
