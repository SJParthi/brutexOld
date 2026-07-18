"""Dhan Data API v2 constants — every value traces to a verified source or is ``_UNVERIFIED``.

DEC-DHAN-ADAPTER-001. This is the SECOND vendor adapter (after Groww), built
**LAKE / REFERENCE-ONLY and GATED ON the ₹499/mo Data-API subscription** — fully
READY but INERT (no live calls until the operator provisions credentials). Every
fact below is either **VERIFIED** against the official DhanHQ v2 documentation (a
provenance docstring names it) or stamped with an ``_UNVERIFIED`` suffix in its
identifier + a docstring naming exactly what a live probe must confirm (Golden
Rule #1 — no hallucinated vendor fact).

This module is a **pure, I/O-free LEAF** (stdlib only) — no client / network / AWS
import — so a future ``dhan_constants_rust.py`` facade could wrap the resolvers 1:1
(DEC-DHAN-RUST-FACADE-001, the deferred uniformity-only Rust seam).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import date
from typing import Final

# ─────────────────────────────────────────────────────────────────────────────
# Transport — base URL + endpoint paths (VERIFIED — DhanHQ v2 REST docs)
# ─────────────────────────────────────────────────────────────────────────────

#: DhanHQ v2 REST base URL — every endpoint below is a path under it.
#: VERIFIED — DhanHQ v2 docs (all REST calls target ``https://api.dhan.co``).
DHAN_BASE_URL: Final[str] = "https://api.dhan.co"

#: Historical DAILY OHLC endpoint (``POST``). Body: ``securityId``,
#: ``exchangeSegment``, ``instrument``, optional ``expiryCode``, ``oi``,
#: ``fromDate`` (``"YYYY-MM-DD"``), ``toDate``. VERIFIED — DhanHQ v2 docs.
DHAN_ENDPOINT_HISTORICAL: Final[str] = "/v2/charts/historical"

#: Intraday OHLC endpoint (``POST``). Body adds ``interval`` (1/5/15/25/60) and
#: uses ``"YYYY-MM-DD HH:MM:SS"`` bounds. VERIFIED — DhanHQ v2 docs.
DHAN_ENDPOINT_INTRADAY: Final[str] = "/v2/charts/intraday"

#: Option-chain endpoint (``POST``). Body ``{UnderlyingScrip, UnderlyingSeg,
#: Expiry}``; requires the ``client-id`` header. VERIFIED — DhanHQ v2 docs.
DHAN_ENDPOINT_OPTION_CHAIN: Final[str] = "/v2/optionchain"

#: Expiry-list endpoint (``POST``). Body ``{UnderlyingScrip, UnderlyingSeg}`` →
#: ``{data:[...], status:"success"}``. VERIFIED — DhanHQ v2 docs.
DHAN_ENDPOINT_EXPIRY_LIST: Final[str] = "/v2/optionchain/expirylist"

#: Expired-options rolling/ATM-relative history endpoint (``POST``). VERIFIED —
#: DhanHQ v2 docs (``/v2/charts/rollingoption``; ATM-relative macro addressing,
#: NO dated-contract security_id resolution).
DHAN_ENDPOINT_ROLLING_OPTION: Final[str] = "/v2/charts/rollingoption"

#: Token renewal endpoint (``POST``). Refreshes an ACTIVE access token for another
#: 24 h; renewing an EXPIRED token errors (must re-mint). VERIFIED — DhanHQ v2 docs.
DHAN_ENDPOINT_RENEW_TOKEN: Final[str] = "/v2/RenewToken"  # noqa: S105  # endpoint path, not a secret

#: Token-validation endpoint (``GET``) — ``GET /v2/profile`` validates a token.
#: VERIFIED — DhanHQ v2 docs.
DHAN_ENDPOINT_PROFILE: Final[str] = "/v2/profile"

# ─────────────────────────────────────────────────────────────────────────────
# Auth header names (VERIFIED — DhanHQ v2 docs)
# ─────────────────────────────────────────────────────────────────────────────

#: Access-token header sent on every request (JWT). VERIFIED — DhanHQ v2 docs.
DHAN_HEADER_ACCESS_TOKEN: Final[str] = "access-token"  # noqa: S105  # header name, not a secret

#: Client-id header — required by the option-chain endpoint (and ``RenewToken``
#: via ``dhanClientId``). VERIFIED — DhanHQ v2 docs.
DHAN_HEADER_CLIENT_ID: Final[str] = "client-id"

#: ``dhanClientId`` header name for ``RenewToken``. VERIFIED — DhanHQ v2 docs.
DHAN_HEADER_DHAN_CLIENT_ID: Final[str] = "dhanClientId"

#: Access-token JWT lifetime (hours). VERIFIED — DhanHQ v2 docs (24-hour token).
DHAN_TOKEN_LIFETIME_HOURS: Final[int] = 24

# ─────────────────────────────────────────────────────────────────────────────
# Rate limits (VERIFIED — DhanHQ v2 rate-limit table)
# ─────────────────────────────────────────────────────────────────────────────

#: Data-API request rate ceiling (requests/second). VERIFIED — DhanHQ v2 docs
#: ("Data API 5/s + 100,000/day"). The historical minute + hourly endpoints carry
#: no separate per-minute rate limit; the 5/s applies to the Data-API request rate.
DHAN_DATA_RATE_LIMIT_RPS: Final[int] = 5

#: Data-API daily quota (requests/day). VERIFIED — DhanHQ v2 docs.
DHAN_DATA_DAILY_QUOTA: Final[int] = 100_000

#: Option-chain SPECIAL minimum interval between requests (seconds) — 1 request /
#: 3 seconds. VERIFIED — DhanHQ v2 docs (option-chain special rate limit).
DHAN_OPTION_CHAIN_MIN_INTERVAL_S: Final[float] = 3.0

#: Quote-API request rate ceiling (requests/second). VERIFIED — DhanHQ v2 docs
#: ("Quote 1/s"). A data-only adapter does not call quote, recorded for parity.
DHAN_QUOTE_RATE_LIMIT_RPS: Final[int] = 1

#: Non-trading request rate ceiling (requests/second). VERIFIED — DhanHQ v2 docs
#: ("Non-Trading 20/s").
DHAN_NON_TRADING_RATE_LIMIT_RPS: Final[int] = 20

#: Order/Trading rate-limit table (VERIFIED — DhanHQ v2 docs). Recorded for
#: completeness; a data-only adapter never places orders. Keys are the documented
#: window labels, values the limits.
DHAN_ORDER_RATE_LIMITS: Final[Mapping[str, int]] = {
    "per_second": 10,
    "per_minute": 250,
    "per_hour": 1000,
    "per_day": 7000,
}

#: PRODUCTION per-second ceiling injected into the rate controller at a (future)
#: live build site — clamped to the verified Data-API 5/s. Because the ceiling
#: only ever ALLOWS a rate (the AIMD ladder still steps down on 429s), never a
#: silent raise (CLAUDE.md §2 Rule 6 discipline, mirrored for Dhan).
DHAN_RATE_LIMIT_RPS_CEILING: Final[float] = 5.0

# ─────────────────────────────────────────────────────────────────────────────
# Candle intervals + chunk windows
# ─────────────────────────────────────────────────────────────────────────────

#: BruteX canonical interval → Dhan INTRADAY ``interval`` code. The Dhan intraday
#: endpoint accepts exactly 1/5/15/25/60 minutes. VERIFIED — DhanHQ v2 docs.
#: ``"1hour"`` maps to the 60-minute code (60 minutes == 1 hour).
INTRADAY_INTERVAL_TO_DHAN: Final[Mapping[str, str]] = {
    "1minute": "1",
    "5minute": "5",
    "15minute": "15",
    "25minute": "25",
    "60minute": "60",
    "1hour": "60",
}

#: BruteX canonical DAILY interval — served by the historical endpoint (no
#: ``interval`` field), NOT the intraday endpoint.
DHAN_DAILY_INTERVAL: Final[str] = "1day"

#: Every BruteX canonical interval the Dhan adapter can serve (intraday keys plus
#: the daily interval). Used by :data:`DHAN_CAPABILITIES.supported_intervals`.
DHAN_SUPPORTED_INTERVALS: Final[frozenset[str]] = frozenset(INTRADAY_INTERVAL_TO_DHAN) | {
    DHAN_DAILY_INTERVAL
}

#: Intraday max range per call (days). VERIFIED — DhanHQ v2 docs (90 days/call).
DHAN_WINDOW_DAYS_INTRADAY: Final[int] = 90

#: Rolling-option (expired) max range per call (days). VERIFIED — DhanHQ v2 docs
#: (up to 30 days per call).
DHAN_WINDOW_DAYS_ROLLING_OPTION: Final[int] = 30

#: DAILY max range per call (days). **UNVERIFIED** — the DhanHQ v2 daily endpoint
#: serves "from inception" but does not document a per-call range cap. A
#: conservative 365-day tiling is used pending the live probe; the ``_UNVERIFIED``
#: suffix carries the honesty (a probe may reveal a smaller cap — over-tiling is
#: always safe, so this can only be too CAUTIOUS, never wrong data).
DHAN_WINDOW_DAYS_DAILY_UNVERIFIED: Final[int] = 365

# ─────────────────────────────────────────────────────────────────────────────
# Segments + instrument types (VERIFIED — DhanHQ v2 docs)
# ─────────────────────────────────────────────────────────────────────────────

#: Exchange-segment tokens accepted by the Dhan endpoints. VERIFIED — DhanHQ v2
#: docs. ``IDX_I`` is the cross-exchange index segment (the option-chain
#: ``UnderlyingSeg`` for index options).
DHAN_SEGMENTS: Final[frozenset[str]] = frozenset(
    {
        "NSE_EQ",
        "BSE_EQ",
        "NSE_FNO",
        "BSE_FNO",
        "NSE_CURRENCY",
        "BSE_CURRENCY",
        "MCX_COMM",
        "IDX_I",
    }
)

#: Instrument-type tokens. VERIFIED — DhanHQ v2 docs.
DHAN_INSTRUMENT_TYPES: Final[frozenset[str]] = frozenset(
    {
        "INDEX",
        "EQUITY",
        "FUTIDX",
        "OPTIDX",
        "FUTSTK",
        "OPTSTK",
        "FUTCOM",
        "OPTFUT",
        "FUTCUR",
        "OPTCUR",
    }
)

#: ``(EXCH_ID, SEGMENT)`` → Dhan ``exchangeSegment`` token. VERIFIED — DhanHQ v2
#: docs (scrip-master segment derivation). Keyed on the two raw scrip-master
#: columns so the header-driven instrument parser resolves a row's segment.
DHAN_EXCH_SEGMENT_MAP: Final[Mapping[tuple[str, str], str]] = {
    ("NSE", "E"): "NSE_EQ",
    ("NSE", "D"): "NSE_FNO",
    ("NSE", "I"): "IDX_I",
    ("NSE", "C"): "NSE_CURRENCY",
    ("BSE", "E"): "BSE_EQ",
    ("BSE", "D"): "BSE_FNO",
    ("BSE", "I"): "IDX_I",
    ("BSE", "C"): "BSE_CURRENCY",
    ("MCX", "M"): "MCX_COMM",
}

# ─────────────────────────────────────────────────────────────────────────────
# Instrument master (VERIFIED existence; exact columns UNVERIFIED)
# ─────────────────────────────────────────────────────────────────────────────

#: Compact scrip-master CSV URL. VERIFIED — DhanHQ v2 docs (existence).
DHAN_SCRIP_MASTER_URL: Final[str] = "https://images.dhan.co/api-data/api-scrip-master.csv"

#: Detailed scrip-master CSV URL. VERIFIED — DhanHQ v2 docs (existence).
DHAN_SCRIP_MASTER_DETAILED_URL: Final[str] = (
    "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"
)

#: SSRF host allowlist for the scrip-master download — the scrip-master + base API
#: hosts only. A future real downloader must refuse any other host (Golden Rule #1).
DHAN_ALLOWED_HOSTS: Final[frozenset[str]] = frozenset({"images.dhan.co", "api.dhan.co"})

#: Scrip-master header column names the instrument parser depends on. **UNVERIFIED**
#: — the ``SEM_``-prefixed names + column ORDER are model-inferred; the header-driven
#: parser reads BY NAME and tolerates order, and this drift-guard tuple pins the
#: exact names a live scrip-master download must carry (a rename fails loudly, never
#: silently mis-parses). Every name here is ``_UNVERIFIED`` until a real header lands.
DHAN_SCRIP_HEADERS_UNVERIFIED: Final[Mapping[str, str]] = {
    "security_id": "SEM_SMST_SECURITY_ID",
    "trading_symbol": "SEM_TRADING_SYMBOL",
    "series": "SEM_SERIES",
    "instrument_type": "SEM_EXCH_INSTRUMENT_TYPE",
    "exch_id": "SEM_EXM_EXCH_ID",
    "segment": "SEM_SEGMENT",
    "expiry_date": "SEM_EXPIRY_DATE",
    "strike_price": "SEM_STRIKE_PRICE",
    "option_type": "SEM_OPTION_TYPE",
    "lot_size": "SEM_LOT_UNITS",
    "tick_size": "SEM_TICK_SIZE",
}

# ─────────────────────────────────────────────────────────────────────────────
# Timezone + history
# ─────────────────────────────────────────────────────────────────────────────

#: Market native timezone — NSE/BSE report IST. VERIFIED — the NSE/BSE market is
#: the same for every vendor.
NATIVE_TIMEZONE: Final[str] = "Asia/Kolkata"

#: Earliest history date used to clamp Phase-D requests. **UNVERIFIED** — daily is
#: served "from inception" (exact earliest UNVERIFIED) and intraday is a ROLLING
#: last-5-years window, so no single fixed earliest date exists in the docs. A
#: conservative ``2020-01-01`` placeholder is used pending the live probe; the
#: ``_UNVERIFIED`` suffix carries the honesty (a too-early clamp never fabricates
#: data — the vendor simply returns nothing before its real floor).
EARLIEST_HISTORY_UNVERIFIED: Final[date] = date(2020, 1, 1)

#: Rolling-option (expired-options) coverage depth — last 5 years, minute-level, up
#: to 10 strikes above/below ATM, Index + Stock options. VERIFIED — DhanHQ v2 docs.
DHAN_ROLLING_OPTION_COVERAGE_YEARS: Final[int] = 5

# ─────────────────────────────────────────────────────────────────────────────
# Reauth budget
# ─────────────────────────────────────────────────────────────────────────────

#: Bounded token-refresh budget — a stale/dead token is re-read/renewed AT MOST
#: this many times per call then raises terminally (never an infinite loop),
#: mirroring the Groww ``max_reauths`` discipline.
DHAN_MAX_REAUTHS: Final[int] = 1

# ─────────────────────────────────────────────────────────────────────────────
# AWS Parameter-Store credential + token paths (mirrors DEC-AWS-ONLY-CREDS-001)
# ─────────────────────────────────────────────────────────────────────────────

#: SSM region env override name, then the shared ``AWS_REGION`` fallback name.
DHAN_TOKEN_AWS_REGION_ENV: Final[str] = "BRUTEX_DHAN_TOKEN_AWS_REGION"  # noqa: S105  # env var name
AWS_REGION_ENV: Final[str] = "AWS_REGION"  # env var name, not a secret
#: SSM region where the Dhan SecureStrings live (mirrors the Groww ap-south-1 default).
DHAN_TOKEN_AWS_REGION_DEFAULT: Final[str] = "ap-south-1"  # noqa: S105  # AWS region, not a secret

#: Shared daily access-token parameter path + its env override.
DHAN_TOKEN_PARAM_ACCESS_TOKEN_ENV: Final[str] = "BRUTEX_DHAN_TOKEN_PARAM_ACCESS_TOKEN"  # noqa: S105  # env var name, not a secret
DHAN_TOKEN_PARAM_ACCESS_TOKEN_DEFAULT: Final[str] = "/tickvault/prod/dhan/access-token"  # noqa: S105  # parameter path, not a secret

#: Client-id parameter path + its env override (a Dhan client-id, not a secret
#: value, but read from SSM alongside the token for provisioning uniformity).
DHAN_TOKEN_PARAM_CLIENT_ID_ENV: Final[str] = "BRUTEX_DHAN_TOKEN_PARAM_CLIENT_ID"  # noqa: S105  # env var name, not a secret
DHAN_TOKEN_PARAM_CLIENT_ID_DEFAULT: Final[str] = "/tickvault/prod/dhan/client-id"  # noqa: S105  # parameter path, not a secret

#: TOTP-based automated-generation credential paths (the 1-year api-key/secret
#: flow). Present for the automated daily regeneration path; a data-only inert
#: adapter never mints, so these are provisioning pointers only.
DHAN_TOKEN_PARAM_API_KEY_ENV: Final[str] = "BRUTEX_DHAN_TOKEN_PARAM_API_KEY"  # noqa: S105  # env var name, not a secret
DHAN_TOKEN_PARAM_API_KEY_DEFAULT: Final[str] = "/tickvault/prod/dhan/api-key"  # noqa: S105  # parameter path, not a secret
DHAN_TOKEN_PARAM_TOTP_SECRET_ENV: Final[str] = "BRUTEX_DHAN_TOKEN_PARAM_TOTP_SECRET"  # noqa: S105  # env var name, not a secret
DHAN_TOKEN_PARAM_TOTP_SECRET_DEFAULT: Final[str] = "/tickvault/prod/dhan/totp-secret"  # noqa: S105  # parameter path, not a secret

# ─────────────────────────────────────────────────────────────────────────────
# Ledger namespaces — DISTINCT from Groww so windows never collide on the shared lake
# ─────────────────────────────────────────────────────────────────────────────

#: Window-ledger market namespace for Dhan spot candles (distinct from Groww's
#: ``groww``/``groww-option``/``groww-future`` — Dhan windows can never collide
#: with Groww windows on the shared lake ledger). HONEST NOTE: a PRE-ADR-0009
#: legacy lake could carry rows in this same namespace from the deleted v1-era
#: Dhan adapter; the live-wiring DEC must decide the coexistence law before any
#: production write (this inert adapter writes no ledger rows — nothing collides
#: today).
DHAN_COVERAGE_MARKET: Final[str] = "dhan"

#: Window-ledger market namespace for Dhan option candles.
DHAN_OPTION_COVERAGE_MARKET: Final[str] = "dhan-option"

#: Window-ledger market namespace for Dhan future candles.
DHAN_FUTURE_COVERAGE_MARKET: Final[str] = "dhan-future"


def resolve_token_broker_region() -> str:
    """Resolve the SSM region: ``BRUTEX_DHAN_TOKEN_AWS_REGION`` → ``AWS_REGION`` → default.

    A non-blank env override wins in order; else the ap-south-1 default (never
    ``None`` — a mis-set profile default cannot silently read the wrong region).
    O(1). Mirrors the Groww resolver (CLAUDE.md §11 COMMON pattern, Dhan-scoped
    env names).
    """
    for env_name in (DHAN_TOKEN_AWS_REGION_ENV, AWS_REGION_ENV):
        raw = os.environ.get(env_name)
        if raw is not None and raw.strip():
            return raw.strip()
    return DHAN_TOKEN_AWS_REGION_DEFAULT


def resolve_token_param_path(env_name: str, default: str) -> str:
    """Return the Parameter-Store path from *env_name*, or *default* when unset/blank.

    A thin COMMON resolver shared by the Dhan secret paths so each honours its own
    env override with the fixed ``/tickvault/prod/dhan/…`` default. O(1).
    """
    raw = os.environ.get(env_name)
    if raw is not None and raw.strip():
        return raw.strip()
    return default


__all__ = [
    "AWS_REGION_ENV",
    "DHAN_ALLOWED_HOSTS",
    "DHAN_BASE_URL",
    "DHAN_COVERAGE_MARKET",
    "DHAN_DAILY_INTERVAL",
    "DHAN_DATA_DAILY_QUOTA",
    "DHAN_DATA_RATE_LIMIT_RPS",
    "DHAN_ENDPOINT_EXPIRY_LIST",
    "DHAN_ENDPOINT_HISTORICAL",
    "DHAN_ENDPOINT_INTRADAY",
    "DHAN_ENDPOINT_OPTION_CHAIN",
    "DHAN_ENDPOINT_PROFILE",
    "DHAN_ENDPOINT_RENEW_TOKEN",
    "DHAN_ENDPOINT_ROLLING_OPTION",
    "DHAN_EXCH_SEGMENT_MAP",
    "DHAN_FUTURE_COVERAGE_MARKET",
    "DHAN_HEADER_ACCESS_TOKEN",
    "DHAN_HEADER_CLIENT_ID",
    "DHAN_HEADER_DHAN_CLIENT_ID",
    "DHAN_INSTRUMENT_TYPES",
    "DHAN_MAX_REAUTHS",
    "DHAN_NON_TRADING_RATE_LIMIT_RPS",
    "DHAN_OPTION_CHAIN_MIN_INTERVAL_S",
    "DHAN_OPTION_COVERAGE_MARKET",
    "DHAN_ORDER_RATE_LIMITS",
    "DHAN_QUOTE_RATE_LIMIT_RPS",
    "DHAN_RATE_LIMIT_RPS_CEILING",
    "DHAN_ROLLING_OPTION_COVERAGE_YEARS",
    "DHAN_SCRIP_HEADERS_UNVERIFIED",
    "DHAN_SCRIP_MASTER_DETAILED_URL",
    "DHAN_SCRIP_MASTER_URL",
    "DHAN_SEGMENTS",
    "DHAN_SUPPORTED_INTERVALS",
    "DHAN_TOKEN_AWS_REGION_DEFAULT",
    "DHAN_TOKEN_AWS_REGION_ENV",
    "DHAN_TOKEN_LIFETIME_HOURS",
    "DHAN_TOKEN_PARAM_ACCESS_TOKEN_DEFAULT",
    "DHAN_TOKEN_PARAM_ACCESS_TOKEN_ENV",
    "DHAN_TOKEN_PARAM_API_KEY_DEFAULT",
    "DHAN_TOKEN_PARAM_API_KEY_ENV",
    "DHAN_TOKEN_PARAM_CLIENT_ID_DEFAULT",
    "DHAN_TOKEN_PARAM_CLIENT_ID_ENV",
    "DHAN_TOKEN_PARAM_TOTP_SECRET_DEFAULT",
    "DHAN_TOKEN_PARAM_TOTP_SECRET_ENV",
    "DHAN_WINDOW_DAYS_DAILY_UNVERIFIED",
    "DHAN_WINDOW_DAYS_INTRADAY",
    "DHAN_WINDOW_DAYS_ROLLING_OPTION",
    "EARLIEST_HISTORY_UNVERIFIED",
    "INTRADAY_INTERVAL_TO_DHAN",
    "NATIVE_TIMEZONE",
    "resolve_token_broker_region",
    "resolve_token_param_path",
]
