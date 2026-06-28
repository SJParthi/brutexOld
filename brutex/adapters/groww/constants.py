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

import os
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

#: Operator-DIRECTED per-second ceiling for the production Groww backtesting pull
#: (operator-approved 2026-06-24, RAISED 5 → 8). NOT vendor-documented — the
#: per-second backtesting cap stays officially UNVERIFIED (see
#: GROWW_RATE_LIMIT_RPS_UNVERIFIED); 8 is operator-directed, NOT vendor-confirmed.
#: 8 r/s is safe against the HARD per-minute governor because 8×60 = 480/min stays
#: UNDER the operator-confirmed 500/min cap (GROWW_RATE_LIMIT_PER_MIN), so the
#: governor can never be overshot (DEC-GROWW-GOVERNOR-ATOMIC-001).
#:
#: RAISED back to 8 by DEC-GROWW-RATE-CEILING-8-002 (supersedes the 5 r/s ceiling of
#: DEC-GROWW-RATE-CEILING-5-001) once DEC-FETCH-WRITE-OVERLAP-001 Lever 2 (pool 2→4)
#: gave the pull enough concurrent connections to actually GENERATE demand toward the
#: ceiling — at the prior 2 connections the achieved rate capped at ~3.2 r/s so the
#: 5–8 range was pure waste (the #906 finding), but with 4 connections demand can reach
#: the ceiling, making 8 a FAIR test of whether the vendor serves above 5. The change is
#: a CEILING raise, so it only ALLOWS a higher rate — it does NOT force it: the adaptive
#: AIMD ladder still steps the rate DOWN on a 429 burst (soft floor 3, deep floor 1 under
#: sustained pressure) and recovers back UP only as far as the vendor tolerates, so the
#: worst case at 8 is identical to 5 (the controller self-corrects). HONEST (Golden Rule
#: #10): a live-log audit showed the vendor 429ing AT the 5.0 ceiling, so SUSTAINED 8 is
#: UNPROVEN — the controller will settle wherever the vendor allows; revert to 5 is this
#: one constant if 8 only burns 429-retries without a throughput gain (the #906 trap).
GROWW_RATE_LIMIT_RPS_CEILING: Final[float] = 8.0

#: Hard per-call wall-clock deadline (seconds) for a single historical-candles SDK
#: call — the pull FREEZE GUARD (DEC-GROWW-PULL-CALL-DEADLINE-001). The Groww
#: ``get_historical_candles`` call has no inherent timeout, so a stalled connection
#: would hang the whole historical pull forever. The live pull build sites inject
#: this into :class:`~brutex.adapters.groww.client.GrowwClient.call_timeout_seconds`
#: so any call exceeding it raises a retryable timeout (re-issued by the bounded
#: retry loop; a persistently-stalled window FAILS + re-attempts next pull) instead
#: of freezing. 45 s is generous for a single ≤180-day daily window yet bounds a
#: dead socket to seconds, not forever. NOT a vendor-documented limit.
GROWW_PULL_CALL_TIMEOUT_S: Final[float] = 45.0

#: Default number of INDEPENDENT Groww SDK connections the live spot pull fans its worker
#: threads across (DEC-GROWW-CLIENT-POOL-001 — the multi-connection
#: :class:`~brutex.adapters.groww.pool.ClientPool`). 1 = today's exact single-connection
#: behaviour. Raised 2 → 4 by DEC-FETCH-WRITE-OVERLAP-001 (Lever 2) after a fresh-log audit
#: PROVED 2 connections cap the achieved rate at ~3.2 r/s while the shared controller held
#: its 5.0 ceiling for 96% of the run (time-weighted 4.96 r/s, only 65 429s, ~1.76 r/s of
#: UNUSED slack — the bottleneck was DEMAND, our code, not Groww).
#:
#: The earlier "Little's Law → 0.75 concurrent calls reach 5 r/s at ~150 ms latency, so 2 is
#: sufficient" reasoning was WRONG: it used the FETCH latency (~150 ms) alone, but a worker
#: holds its connection for the WHOLE per-contract cycle (~625 ms = ~185 ms fetch + ~440 ms
#: serial non-fetch work, dominated by the lake Parquet append). With W = 0.625 s the real
#: requirement to reach λ = 5 r/s is L = λ·W = 3.1 concurrent calls — so 2 connections cap at
#: 2/0.625 = ~3.2 r/s (EXACTLY the observed achieved rate), and 4 connections give
#: 4/0.625 = 6.4 r/s of demand > 5 so the SHARED rate controller binds at the 5.0 ceiling.
#: Reaching 8 needs Lever 1 (the fetch/write OVERLAP that shrinks the 625 ms cycle), NOT more
#: connections — 4 is the cap here (one shared TOTP token, GROWW_CLIENT_POOL_MAX=4).
#:
#: The SHARED rate controller still caps the AGGREGATE issue rate (CLAUDE.md §2 Rule 6
#: untouched — K connections add concurrency, never extra rate); the achieved-rate meter
#: proves the real number. Override per environment via ``BRUTEX_GROWW_POOL_SIZE`` (clamped
#: to ``[1, GROWW_CLIENT_POOL_MAX]``).
GROWW_CLIENT_POOL_SIZE: Final[int] = 4

#: Hard upper bound on the connection-pool size (a safety clamp on
#: ``BRUTEX_GROWW_POOL_SIZE``). Concurrent use of one TOTP token across many SDK connections
#: is vendor-UNVERIFIED (ADR-0009 §3) and the vendor 429s above ~5 r/s anyway (live logs),
#: so more than 4 connections only manufactures 429s — never raise this without ADR sign-off.
GROWW_CLIENT_POOL_MAX: Final[int] = 4

#: Environment override for the pool size (operator escape hatch, no code change).
GROWW_POOL_SIZE_ENV: Final[str] = "BRUTEX_GROWW_POOL_SIZE"

#: Environment toggle for the OPTIONAL anti-burst smooth pacing
#: (DEC-GROWW-SMOOTH-PACING-001). The token bucket already caps the AVERAGE rate at
#: ``current_rps``, but its ``1/rate`` cadence still lets sub-second micro-bursts
#: through (the operator's ``rps_1s_max`` spiking to 8–9 below the AIMD ceiling), which
#: can trip a vendor 429. When this toggle is truthy, the live pull build site injects
#: ``RateController(smooth_pacing=True)`` so issuance is spread evenly with a minimum
#: ``1/current_rps`` interval between consecutive issues — the per-second issue count
#: can then never exceed the current AIMD rate. DEFAULT OFF (byte-identical to today):
#: the smoothing is provably SAFE (caps + AIMD ladder UNCHANGED) but its live
#: 429-reduction is UNVERIFIED pending a live probe, so it ships behind a flag.
GROWW_SMOOTH_PACING_ENV: Final[str] = "BRUTEX_GROWW_SMOOTH_PACING"


def resolve_smooth_pacing() -> bool:
    """Resolve the smooth-pacing toggle from ``BRUTEX_GROWW_SMOOTH_PACING`` — default OFF.

    Reads :data:`GROWW_SMOOTH_PACING_ENV`. Returns ``True`` only for an explicit truthy
    value (``"1"`` / ``"true"`` / ``"yes"`` / ``"on"``, case-insensitive); ANY other
    value — unset, blank, ``"0"``, ``"false"``, or unrecognised — returns ``False``, so
    the safe default-OFF, byte-identical behaviour can never be enabled by accident
    (CLAUDE.md §2 Rule 6 — never silently change a rate gate). O(1).
    """
    raw = os.environ.get(GROWW_SMOOTH_PACING_ENV)
    if raw is None:
        return False
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def resolve_pool_size() -> int:
    """Resolve the live-pull connection-pool size: env override clamped to ``[1, MAX]``.

    Reads ``BRUTEX_GROWW_POOL_SIZE`` (:data:`GROWW_POOL_SIZE_ENV`); falls back to
    :data:`GROWW_CLIENT_POOL_SIZE` when unset, blank, non-integer, or out of range — a
    bad value can never crash a pull or silently exceed the :data:`GROWW_CLIENT_POOL_MAX`
    safety clamp (fail-safe to the audited default, CLAUDE.md §2 Rule 1 / Rule 6). O(1).
    """
    raw = os.environ.get(GROWW_POOL_SIZE_ENV)
    if raw is None or not raw.strip():
        return GROWW_CLIENT_POOL_SIZE
    try:
        value = int(raw)
    except ValueError:
        return GROWW_CLIENT_POOL_SIZE
    if value < 1 or value > GROWW_CLIENT_POOL_MAX:
        return GROWW_CLIENT_POOL_SIZE
    return value


__all__ = [
    "BRUTE_FORCE_INDEX_SYMBOLS",
    "CHUNK_WINDOW_30D_INTERVALS",
    "CHUNK_WINDOW_90D_INTERVALS",
    "CHUNK_WINDOW_180D_INTERVALS",
    "EARLIEST_HISTORY",
    "EXCLUDED_SEGMENTS",
    "EXPECTED_FNO_STOCK_COUNT",
    "GROWW_CLIENT_POOL_MAX",
    "GROWW_CLIENT_POOL_SIZE",
    "GROWW_CSV_ALLOWED_HOST",
    "GROWW_DAILY_QUOTA_UNVERIFIED",
    "GROWW_EXCHANGES",
    "GROWW_FUTURE_COVERAGE_MARKET",
    "GROWW_INSTRUMENT_CSV_URL",
    "GROWW_INSTRUMENT_TYPES",
    "GROWW_OPTION_COVERAGE_MARKET",
    "GROWW_POOL_SIZE_ENV",
    "GROWW_PULL_CALL_TIMEOUT_S",
    "GROWW_RATE_LIMIT_PER_MIN",
    "GROWW_RATE_LIMIT_RPS_CEILING",
    "GROWW_RATE_LIMIT_RPS_UNVERIFIED",
    "GROWW_SEGMENTS",
    "GROWW_SMOOTH_PACING_ENV",
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
    "resolve_pool_size",
    "resolve_smooth_pacing",
]
