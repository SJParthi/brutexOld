"""Dhan adapter constants — the pure, verified leaf.

DEC-DHAN-SPOT-PULL-001 (umbrella DEC-DHAN-SPOT-FEED-001, ADR-0022). Every value
here is traceable to the VERIFIED DhanHQ-v2 research (the official ``dhanhq``
SDK read verbatim + official-doc snippets); UNVERIFIED live-only facts carry the
``_UNVERIFIED`` honesty suffix (the Groww ``*_UNVERIFIED`` precedent, CLAUDE.md
§6 / Golden Rule #1) and are NEVER read by a decision branch — an AST pin proves
it, so the pull can never gate on a fabricated "verified" flag.

Scope: SPOT indices ONLY — NIFTY / BANKNIFTY / SENSEX (the brute-force ENGINE
surface) + INDIA VIX (REFERENCE_ONLY). All ride the SHARED ``IDX_I`` segment /
``INDEX`` instrument token on ``POST /charts/intraday`` at 1-minute granularity.
NO options, NO expired futures (an operator A/B hold).

Read path (no minting): ``DhanContext(client_id, access_token)`` makes NO network
call at construction; the two credentials are READ from AWS SSM (SecureString for
the 24-h JWT access token) and never PutParameter'd by BruteX.
"""

from __future__ import annotations

from datetime import date
from typing import Final

# ─────────────────────────────────────────────────────────────────────────────
# Endpoint + request shape (VERIFIED-live: SDK ``_historical_data.py`` + ``dhan_http.py``)
# ─────────────────────────────────────────────────────────────────────────────

#: The DhanHQ v2 REST base (SDK ``dhan_http.py`` ``API_BASE_URL``). Not used to build
#: requests here (the vendor ``dhanhq`` SDK owns the transport) — recorded for provenance.
API_BASE: Final[str] = "https://api.dhan.co/v2"

#: The SHARED index exchange-segment for NSE **and** BSE indices (SDK const
#: ``INDEX='IDX_I'``) — there is NO separate BSE-index segment (VERIFIED-live).
SEGMENT_INDEX: Final[str] = "IDX_I"

#: The instrument-type token for an index spot series (SDK README INDEX example).
INSTRUMENT_INDEX: Final[str] = "INDEX"

#: The intraday interval code for 1-minute candles (SDK default ``interval=1``).
DHAN_INTERVAL_1MIN: Final[int] = 1

#: The single canonical interval this SPOT track fetches (1-minute only). The 19
#: derived intraday timeframes + the daily anchor are DERIVED from the stored
#: 1-minute by the prebuild stage, never separately fetched (CLAUDE.md §3).
DHAN_FETCH_INTERVAL: Final[str] = "1minute"

#: The market timezone of Dhan candle timestamps AFTER conversion. Dhan sends Unix
#: EPOCH seconds (UTC POSIX); the lake stores UTC and renders IST by a FIXED +05:30
#: (Asia/Kolkata, no DST) — 09:15 IST ⇔ 03:45 UTC (VERIFIED-live: SDK
#: ``convert_to_date_time`` uses ``timezone(timedelta(hours=5, minutes=30))``).
NATIVE_TIMEZONE: Final[str] = "Asia/Kolkata"

# ─────────────────────────────────────────────────────────────────────────────
# Windowing + rate limits (PARTIAL: official-doc snippets / support pages)
# ─────────────────────────────────────────────────────────────────────────────

#: Per-request window cap for ``/charts/intraday`` — 90 days per call (any interval).
DHAN_INTRADAY_WINDOW_DAYS: Final[int] = 90

#: Data-API per-second ceiling (5 req/s). Injected into the adaptive
#: :class:`~brutex.adapters.rate_common.RateController`; the AIMD ladder still steps
#: DOWN on throttle and recovers up to this ceiling.
DHAN_RATE_LIMIT_RPS: Final[float] = 5.0

#: Data-API daily cap (100,000 req/day). Honestly enforced; a full 3-index
#: June-2022→today 1-minute pull is ≈ 51 requests — trivial vs this cap.
DHAN_DAILY_QUOTA: Final[int] = 100_000

#: Dhan's Data API has NO per-minute / per-hour governor — only 5/s + 100k/day.
#: ``None`` means honestly none (Golden Rule #1), NOT a fabricated cap.
DHAN_RATE_LIMIT_PER_MIN: Final[int | None] = None

# ─────────────────────────────────────────────────────────────────────────────
# Ledger namespace + lake feed id (own namespace → no Groww collision)
# ─────────────────────────────────────────────────────────────────────────────

#: The window-ledger market namespace for Dhan spot windows (own namespace so a
#: Dhan window can never collide with a Groww window on the shared lake ledger).
DHAN_COVERAGE_MARKET: Final[str] = "dhan"

#: The feed id under which the Dhan lake nests: ``<lake_root>/feeds/dhan/`` (the
#: :mod:`brutex.data.feed_root` nesting law; path-structural attribution, no column).
DHAN_FEED_ID: Final[str] = "dhan"

# ─────────────────────────────────────────────────────────────────────────────
# securityId resolution — hardcoded verified-pending map (probe kit is the ID cross-check)
# ─────────────────────────────────────────────────────────────────────────────

#: The three ENGINE index symbols (brute-force scope) + INDIA VIX (REFERENCE_ONLY).
#: The canonical BruteX symbol keys; the hardcoded map resolves each to its Dhan
#: ``securityId`` on the shared ``IDX_I`` segment.
NIFTY_SYMBOL: Final[str] = "NIFTY"
BANKNIFTY_SYMBOL: Final[str] = "BANKNIFTY"
SENSEX_SYMBOL: Final[str] = "SENSEX"
INDIAVIX_SYMBOL: Final[str] = "INDIAVIX"

#: The brute-force ENGINE spot index symbols (NIFTY / BANKNIFTY / SENSEX). INDIA VIX
#: is deliberately EXCLUDED — it is REFERENCE_ONLY, never in the vocabulary / run
#: identity (CLAUDE.md §3).
DHAN_ENGINE_INDEX_SYMBOLS: Final[tuple[str, ...]] = (
    NIFTY_SYMBOL,
    BANKNIFTY_SYMBOL,
    SENSEX_SYMBOL,
)

#: Every index this SPOT track may pull (ENGINE indices + the reference-only VIX).
DHAN_SPOT_INDEX_SYMBOLS: Final[tuple[str, ...]] = (
    *DHAN_ENGINE_INDEX_SYMBOLS,
    INDIAVIX_SYMBOL,
)

#: The hardcoded verified-PENDING ``{symbol → securityId}`` map — the SINGLE source of
#: truth (no runtime live-CSV loader; coordinator ruling). NIFTY 13 is official (SDK
#: README ``security_id=13``); the others are in-repo-verified (MIGRATION #87/#72 +
#: TickVault CSV-pins): BANKNIFTY 25, SENSEX 51, INDIA VIX 21 (NOT 26 — a known
#: third-party mirror error). The read-only probe kit
#: (``docs/runbooks/feed-probe/probe_feeds.py``) is the ONE-TIME cross-check that
#: confirms each id against the public compact scrip-master CSV. A symbol ABSENT from
#: this map is loud-skipped (never a blind id, Golden Rule #1).
DHAN_INDEX_SECURITY_IDS: Final[dict[str, int]] = {
    NIFTY_SYMBOL: 13,
    BANKNIFTY_SYMBOL: 25,
    SENSEX_SYMBOL: 51,
    INDIAVIX_SYMBOL: 21,
}

# ─────────────────────────────────────────────────────────────────────────────
# History floor + per-index servability — UNVERIFIED until a probe transcript lands
# ─────────────────────────────────────────────────────────────────────────────

#: The earliest 1-minute date Dhan is CONFIRMED to serve. ``None`` = UNVERIFIED
#: (Golden Rule #1): Dhan's official policy is a ROLLING "last 5 years" window, so
#: June-2022 is reachable TODAY but the true floor + rolling edge are UNVERIFIED
#: (all ``*.dhan.co`` hosts are egress-blocked in-sandbox). The credentialed probe
#: (docs/runbooks §6 gate G1) flips this to a real ``date`` in a ONE-line data
#: change; until then the pull NEVER fabricates a floor — it plans from the operator
#: date range and records honest empty windows for a too-old span.
DHAN_HISTORY_FLOOR: Final[date | None] = None

#: Per-index intraday SERVABILITY — UNVERIFIED until a credentialed probe transcript
#: lands (Golden Rule #1). These are HONESTY MARKERS ONLY: no decision branch reads
#: them (AST-pinned in ``tests/unit/test_dhan_constants.py``), so the pull can never
#: gate on a fabricated "verified" flag — an index either resolves a securityId
#: (attempted; honest no-data on empty arrays) or is loud-skipped. Flipping a marker
#: to a probe transcript is the one-line data change of §6 gates G1 (depth) / G2
#: (VIX) / G3 (SENSEX). ``None`` = "no probe transcript yet".
NIFTY_SERVABILITY_UNVERIFIED: Final[None] = None
BANKNIFTY_SERVABILITY_UNVERIFIED: Final[None] = None
SENSEX_SERVABILITY_UNVERIFIED: Final[None] = None
INDIAVIX_SERVABILITY_UNVERIFIED: Final[None] = None

# ─────────────────────────────────────────────────────────────────────────────
# Known vendor error codes — the full documented table (VERIFIED-docs(mirror):
# the DhanHQ v2 annexure, mirror 20-annexure.md ⇐ dhanhq.co/docs/v2/annexure/).
# Trading-side codes are the DH-9xx strings; the Data-API additionally returns
# NUMERIC codes (800-814). The SDK's failure envelope spells the remark keys
# snake_case (``error_code``/``error_type``/``error_message`` — SDK
# ``dhan_http.py`` ``_parse_response``); the docs' raw-HTTP shape is camelCase —
# :func:`brutex.adapters.dhan.errors.classify_dhan_failure` accepts BOTH.
# ─────────────────────────────────────────────────────────────────────────────

#: DH-901 ``Invalid_Authentication`` — a bad / expired 24-h JWT. Handled by RE-READING
#: SSM (never a mint) — the read-only reauth self-heal. [mirror 20-annexure.md]
DHAN_ERR_INVALID_AUTH: Final[str] = "DH-901"

#: DH-902 — "User has not subscribed to Data APIs or does not have access to
#: Trading APIs" — the SUBSCRIPTION / authorization class. TERMINAL: activate the
#: Data-API subscription (₹499/mo or the 25-trades-in-30d waiver), not a retry.
#: [mirror 20-annexure.md] RELABEL (DEC-DHAN-ROLLING-FLOOR-001 review, A-MINOR-2):
#: this constant previously carried ``DH-905``, which the annexure documents as the
#: INPUT-exception class (:data:`DHAN_ERR_INPUT_EXCEPTION`), not subscription.
DHAN_ERR_AUTHORIZATION: Final[str] = "DH-902"

#: DH-904 — "Too many requests on server from single user breaching rate limits"
#: (the trading-side rate-limit twin of the Data-API numeric 805). RETRYABLE +
#: rate-limit (feeds the AIMD ladder). [mirror 20-annexure.md]
DHAN_ERR_RATE_LIMIT: Final[str] = "DH-904"

#: DH-905 — "input exception" (missing / malformed fields). TERMINAL bad request —
#: fix the request, never a retry. [mirror 20-annexure.md]
DHAN_ERR_INPUT_EXCEPTION: Final[str] = "DH-905"

#: DH-907 — "data-fetch error (bad params)". TERMINAL bad request. [mirror 20-annexure.md]
DHAN_ERR_DATA_FETCH: Final[str] = "DH-907"

#: DH-908 — internal server error. RETRYABLE (the Groww 5xx posture — rides the
#: gentle timeout ladder, never the rate ladder). [mirror 20-annexure.md]
DHAN_ERR_INTERNAL_SERVER: Final[str] = "DH-908"

#: DH-909 — network error. RETRYABLE (server-stress class). [mirror 20-annexure.md]
DHAN_ERR_NETWORK: Final[str] = "DH-909"

#: The Data-API NUMERIC codes (mirror 20-annexure.md; numeric codes may arrive as
#: ``int`` — the classifier normalises ``str(code).strip().upper()``).
DHAN_ERR_NUM_INTERNAL: Final[str] = "800"  # Internal Server Error → retryable
DHAN_ERR_NUM_INSTRUMENT_LIMIT: Final[str] = "804"  # instruments-per-request limit → bad request
DHAN_ERR_NUM_RATE_LIMIT: Final[str] = "805"  # too many requests/connections → throttle
DHAN_ERR_NUM_NOT_SUBSCRIBED: Final[str] = "806"  # Data APIs not subscribed → subscription
DHAN_ERR_NUM_TOKEN_EXPIRED: Final[str] = "807"  # noqa: S105 — error code, not a secret; access token expired → auth (re-read)
DHAN_ERR_NUM_AUTH_FAILED: Final[str] = "808"  # authentication failed → auth (re-read)
DHAN_ERR_NUM_TOKEN_INVALID: Final[str] = "809"  # noqa: S105 — error code, not a secret; access token invalid → auth (re-read)
DHAN_ERR_NUM_CLIENT_ID_INVALID: Final[str] = "810"  # client id invalid → auth (re-read)
DHAN_ERR_NUM_INVALID_EXPIRY: Final[str] = "811"  # invalid expiry date → bad request
DHAN_ERR_NUM_INVALID_DATE: Final[str] = "812"  # invalid date format → bad request
DHAN_ERR_NUM_INVALID_SECURITY_ID: Final[str] = "813"  # invalid SecurityId → bad request
DHAN_ERR_NUM_INVALID_REQUEST: Final[str] = "814"  # invalid request → bad request

# ─────────────────────────────────────────────────────────────────────────────
# AWS SSM parameter paths for the READ-ONLY Dhan credentials (own params)
# ─────────────────────────────────────────────────────────────────────────────

#: The (public) Dhan client id — SSM ``String`` (not a secret; still read from SSM
#: so a rotation propagates to every consumer). Env-overridable path name.
DHAN_TOKEN_PARAM_CLIENT_ID_ENV: Final[str] = "BRUTEX_TOKEN_PARAM_DHAN_CLIENT_ID"  # noqa: S105 — env var name, not a secret
DHAN_TOKEN_PARAM_CLIENT_ID_DEFAULT: Final[str] = "/tickvault/prod/dhan/client-id"  # noqa: S105 — parameter path, not a secret

#: The Dhan 24-h JWT access token — SSM ``SecureString`` (a secret). Env-overridable
#: path name. Minted by the Lambda / TickVault; BruteX only ``GetParameter``s it.
DHAN_TOKEN_PARAM_ACCESS_TOKEN_ENV: Final[str] = "BRUTEX_TOKEN_PARAM_DHAN_ACCESS_TOKEN"  # noqa: S105 — env var name, not a secret
DHAN_TOKEN_PARAM_ACCESS_TOKEN_DEFAULT: Final[str] = "/tickvault/prod/dhan/access-token"  # noqa: S105 — parameter path, not a secret


__all__ = [
    "API_BASE",
    "BANKNIFTY_SERVABILITY_UNVERIFIED",
    "BANKNIFTY_SYMBOL",
    "DHAN_COVERAGE_MARKET",
    "DHAN_DAILY_QUOTA",
    "DHAN_ENGINE_INDEX_SYMBOLS",
    "DHAN_ERR_AUTHORIZATION",
    "DHAN_ERR_DATA_FETCH",
    "DHAN_ERR_INPUT_EXCEPTION",
    "DHAN_ERR_INTERNAL_SERVER",
    "DHAN_ERR_INVALID_AUTH",
    "DHAN_ERR_NETWORK",
    "DHAN_ERR_NUM_AUTH_FAILED",
    "DHAN_ERR_NUM_CLIENT_ID_INVALID",
    "DHAN_ERR_NUM_INSTRUMENT_LIMIT",
    "DHAN_ERR_NUM_INTERNAL",
    "DHAN_ERR_NUM_INVALID_DATE",
    "DHAN_ERR_NUM_INVALID_EXPIRY",
    "DHAN_ERR_NUM_INVALID_REQUEST",
    "DHAN_ERR_NUM_INVALID_SECURITY_ID",
    "DHAN_ERR_NUM_NOT_SUBSCRIBED",
    "DHAN_ERR_NUM_RATE_LIMIT",
    "DHAN_ERR_NUM_TOKEN_EXPIRED",
    "DHAN_ERR_NUM_TOKEN_INVALID",
    "DHAN_ERR_RATE_LIMIT",
    "DHAN_FEED_ID",
    "DHAN_FETCH_INTERVAL",
    "DHAN_HISTORY_FLOOR",
    "DHAN_INDEX_SECURITY_IDS",
    "DHAN_INTERVAL_1MIN",
    "DHAN_INTRADAY_WINDOW_DAYS",
    "DHAN_RATE_LIMIT_PER_MIN",
    "DHAN_RATE_LIMIT_RPS",
    "DHAN_SPOT_INDEX_SYMBOLS",
    "DHAN_TOKEN_PARAM_ACCESS_TOKEN_DEFAULT",
    "DHAN_TOKEN_PARAM_ACCESS_TOKEN_ENV",
    "DHAN_TOKEN_PARAM_CLIENT_ID_DEFAULT",
    "DHAN_TOKEN_PARAM_CLIENT_ID_ENV",
    "INDIAVIX_SERVABILITY_UNVERIFIED",
    "INDIAVIX_SYMBOL",
    "INSTRUMENT_INDEX",
    "NATIVE_TIMEZONE",
    "NIFTY_SERVABILITY_UNVERIFIED",
    "NIFTY_SYMBOL",
    "SEGMENT_INDEX",
    "SENSEX_SERVABILITY_UNVERIFIED",
    "SENSEX_SYMBOL",
]
