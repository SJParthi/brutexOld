"""Groww adapter constants — the single pure-data lookup surface.

Every value here is traceable to a verified source (Golden Rule #1/#2):

* ``docs/groww/api-reference/*.md`` — the Groww Trade API SDK reference.
* ``docs/groww/probe/INSTRUMENT_MASTER_FINDINGS.md`` — the operator-verified
  instrument master (India VIX present, 21-column schema, the in-scope
  universe, real lot/tick).

No I/O, no mutable state — mirrors the canonical adapter constants layout. The two
``*_UNVERIFIED`` names carry the honesty requirement (ADR-0009 §3.2) in the
identifier itself: the backtesting rate-limit ceiling is an OPEN Phase-0 probe
gate and MUST NOT be mistaken for a verified fact.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Final

# ── Instrument master (live-downloaded; this snapshot is point-in-time) ──────
#: Live instrument-master CSV (docs/groww/api-reference/02-instruments.md).
GROWW_INSTRUMENT_CSV_URL: Final[str] = "https://growwapi-assets.groww.in/instruments/instrument.csv"
#: SSRF allowlist host for the instrument-master download.
GROWW_CSV_ALLOWED_HOST: Final[str] = "growwapi-assets.groww.in"

#: The verified 21-column header, in exact order (schema-drift guard).
#: Source: docs/groww/probe/INSTRUMENT_MASTER_FINDINGS.md (re-derived from the
#: operator CSV, 160,339 rows, every row exactly 21 fields).
INSTRUMENT_CSV_COLUMNS: Final[tuple[str, ...]] = (
    "exchange",
    "exchange_token",
    "trading_symbol",
    "groww_symbol",
    "name",
    "instrument_type",
    "segment",
    "series",
    "isin",
    "underlying_symbol",
    "underlying_exchange_token",
    "expiry_date",
    "strike_price",
    "lot_size",
    "tick_size",
    "freeze_quantity",
    "is_reserved",
    "buy_allowed",
    "sell_allowed",
    "internal_trading_symbol",
    "is_intraday",
)

# ── Candle intervals (docs/groww/api-reference/03-backtesting.md + 04) ───────
#: BruteX canonical interval → Groww SDK ``CANDLE_INTERVAL_*`` value.
INTERVAL_TO_GROWW: Final[Mapping[str, str]] = {
    "1minute": "1minute",
    "2minute": "2minute",
    "3minute": "3minute",
    "5minute": "5minute",
    "10minute": "10minute",
    "15minute": "15minute",
    "30minute": "30minute",
    "1hour": "1hour",
    "4hour": "4hour",
    "1day": "1day",
    "1week": "1week",
    "1month": "1month",
}
#: Track-1 fetches 1-minute (+ 1-day for PDH/PDL); other TFs are resampled
#: deterministically from 1m, mirroring DEC-DHAN-1M-ONLY-001.
TRACK1_SPOT_INTERVAL: Final[str] = "1minute"
TRACK1_DAILY_INTERVAL: Final[str] = "1day"

# ── Chunk windows per interval tier (03-backtesting.md "Data Limits") ────────
WINDOW_DAYS_30: Final[int] = 30
WINDOW_DAYS_90: Final[int] = 90
WINDOW_DAYS_180: Final[int] = 180
#: 1-5 minute intervals → max 30 days per call.
CHUNK_WINDOW_30D_INTERVALS: Final[frozenset[str]] = frozenset(
    {"1minute", "2minute", "3minute", "5minute"}
)
#: 10/15/30 minute intervals → max 90 days per call.
CHUNK_WINDOW_90D_INTERVALS: Final[frozenset[str]] = frozenset({"10minute", "15minute", "30minute"})
#: 1 hour and coarser → max 180 days per call.
CHUNK_WINDOW_180D_INTERVALS: Final[frozenset[str]] = frozenset(
    {"1hour", "4hour", "1day", "1week", "1month"}
)

# ── Scope (CLAUDE.md §3; COMMODITY/MCX excluded) ────────────────────────────
GROWW_EXCHANGES: Final[frozenset[str]] = frozenset({"NSE", "BSE"})
GROWW_SEGMENTS: Final[frozenset[str]] = frozenset({"CASH", "FNO"})
EXCLUDED_SEGMENTS: Final[frozenset[str]] = frozenset({"COMMODITY"})
GROWW_INSTRUMENT_TYPES: Final[frozenset[str]] = frozenset({"EQ", "IDX", "FUT", "CE", "PE"})

#: Brute-force engine universe (verified present in the instrument master).
BRUTE_FORCE_INDEX_SYMBOLS: Final[frozenset[str]] = frozenset(
    {"NSE-NIFTY", "NSE-BANKNIFTY", "BSE-SENSEX"}
)
#: India VIX — reference-only (buy/sell_allowed=0), never in the 52-vocab.
INDIA_VIX_SYMBOL: Final[str] = "NSE-INDIAVIX"
#: F&O-eligible single-stock count verified in the operator CSV (lake scope).
EXPECTED_FNO_STOCK_COUNT: Final[int] = 212

#: Documented historical depth ("available from 2020", 03-backtesting.md).
#: Whether 1-minute candles truly reach this boundary is an OPEN probe gate.
EARLIEST_HISTORY: Final[date] = date(2020, 1, 1)
#: Groww candle timestamps are native IST (fixed +05:30, no DST).
NATIVE_TIMEZONE: Final[str] = "Asia/Kolkata"

# ── Option lake (DEC-P0-063 — REFERENCE_ONLY, never the brute-force engine) ──
#: The neutral SEGMENT every option/future contract bar is partitioned under in
#: the lake (``bars/<exch>/FNO/<contract_groww_symbol>/<interval>/...`` — see
#: ``brutex/data/partition.py``, whose ``VALID_SEGMENTS`` includes ``FNO`` and
#: whose docstring explicitly supports hyphenated contract symbols like
#: ``NSE-NIFTY-02Jan25-28500-PE``). Groww options are ABSOLUTE-strike OHLCV+OI
#: rows (all listed strikes, 03-backtesting.md:202-236), so they ride the SAME
#: ``BarSink`` / :class:`~brutex.data.models.CandleBar` path as spot — not a
#: relative/rolling option schema (Golden Rule #1 — no invented vendor shape).
OPTION_SEGMENT: Final[str] = "FNO"
#: The coverage-ledger market namespace stamped on every OPTION fetch window
#: (DEC-GROWW-OPTION-INGEST-001), kept DISTINCT from the spot ``"groww"``
#: namespace (:data:`~brutex.adapters.ingest_common.GROWW_COVERAGE_MARKET`) so
#: option windows never collide with spot windows on the shared lake ledger and
#: the dashboard can surface the two coverage surfaces apart.
GROWW_OPTION_COVERAGE_MARKET: Final[str] = "groww-option"
#: The coverage-ledger market namespace stamped on every FUTURE fetch window
#: (DEC-GROWW-FUTURE-INGEST-001), kept DISTINCT from both the spot ``"groww"`` and
#: the option ``"groww-option"`` namespaces so futures windows never collide with
#: spot/option windows on the shared lake ledger and the dashboard can surface the
#: three coverage surfaces apart. FUT contracts ride the SAME ``OPTION_SEGMENT``
#: (``FNO``) BarSink path — only the ledger namespace differs.
GROWW_FUTURE_COVERAGE_MARKET: Final[str] = "groww-future"

# ── UNVERIFIED — OPEN Phase-0 probe gates (ADR-0009 §3.1/§4) ─────────────────
#: Backtesting rate-limit ceiling is UNDOCUMENTED. The "Live Data" 10 r/s figure
#: (01-introduction.md) is NOT confirmed to cover get_historical_candles. The
#: name is suffixed UNVERIFIED so the cutover gate cannot mistake it for fact.
GROWW_RATE_LIMIT_RPS_UNVERIFIED: Final[float] = 10.0
#: Daily quota for backtesting is unknown until probed.
GROWW_DAILY_QUOTA_UNVERIFIED: Final[int | None] = None

#: Per-minute request cap for backtesting `get_historical_candles`, applied as a
#: HARD governor on top of the per-second token bucket (DEC-GROWW-RATE-PERMIN-001).
#: PROVENANCE: operator-confirmed (2026-06-13) — NOT in the SDK reference. The
#: published "Live Data" group is 300/min (01-introduction.md:130-134), but that
#: row is NOT confirmed to cover backtesting; the operator confirms the backtesting
#: endpoint allows 500/min with NO daily quota. The per-second ceiling stays
#: UNVERIFIED (`GROWW_RATE_LIMIT_RPS_UNVERIFIED`) — 500/min (≈8.3 r/s sustained) is
#: the binding limit. Injected into `RateController(ceiling_per_min=...)` so a high
#: per-second ceiling (10 r/s ⇒ 600/min) can never overshoot it.
GROWW_RATE_LIMIT_PER_MIN: Final[int] = 500

__all__ = [
    "BRUTE_FORCE_INDEX_SYMBOLS",
    "CHUNK_WINDOW_30D_INTERVALS",
    "CHUNK_WINDOW_90D_INTERVALS",
    "CHUNK_WINDOW_180D_INTERVALS",
    "EARLIEST_HISTORY",
    "EXCLUDED_SEGMENTS",
    "EXPECTED_FNO_STOCK_COUNT",
    "GROWW_CSV_ALLOWED_HOST",
    "GROWW_DAILY_QUOTA_UNVERIFIED",
    "GROWW_EXCHANGES",
    "GROWW_FUTURE_COVERAGE_MARKET",
    "GROWW_INSTRUMENT_CSV_URL",
    "GROWW_INSTRUMENT_TYPES",
    "GROWW_OPTION_COVERAGE_MARKET",
    "GROWW_RATE_LIMIT_PER_MIN",
    "GROWW_RATE_LIMIT_RPS_UNVERIFIED",
    "GROWW_SEGMENTS",
    "INDIA_VIX_SYMBOL",
    "INSTRUMENT_CSV_COLUMNS",
    "INTERVAL_TO_GROWW",
    "NATIVE_TIMEZONE",
    "OPTION_SEGMENT",
    "TRACK1_DAILY_INTERVAL",
    "TRACK1_SPOT_INTERVAL",
    "WINDOW_DAYS_30",
    "WINDOW_DAYS_90",
    "WINDOW_DAYS_180",
]
