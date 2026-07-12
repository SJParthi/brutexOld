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

# DEC-AWS-SSM-SEAM-COMMON-001 (MIGRATION.md #1754): the region + parameter-path resolvers
# MOVED to the COMMON seam :mod:`brutex.adapters.aws_client` so a second vendor (Dhan) shares
# them. Re-exported here byte-identically so every existing
# ``from brutex.adapters.groww.constants import resolve_token_param_path`` call site is
# unchanged (the Groww suites are the oracle). The GROWW_TOKEN_AWS_REGION_* / AWS_REGION_ENV
# constants below keep their literal definitions (values identical to the seam's) so a test
# that monkeypatches ``os.environ[c.GROWW_TOKEN_AWS_REGION_ENV]`` still drives the moved
# resolver, which reads the SAME env-var name.
from brutex.adapters.aws_client import (  # re-exported for byte-compat call sites (in __all__)
    resolve_token_broker_region,
    resolve_token_param_path,
)

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

#: Bounded mid-pull re-auth budget injected at the PRODUCTION live build sites
#: (``build_live_client`` / ``build_live_pool`` — the DEC-GROWW-AUTH-HALT-001 FP-1
#: hardening). The :class:`~brutex.adapters.groww.client.GrowwClient` DEFAULT stays
#: ``1`` (offline / unit-test back-compat, untouched); production injects ``2`` so
#: the 06:00 IST daily-token rotation (verified from production logs 2026-06-23 —
#: DEC-GROWW-TOKEN-REAUTH-001) cannot FALSE-halt a pull: at the rotation edge a
#: worker's re-read may adopt the Lambda's FRESH token while that token is not yet
#: fleet-valid (a propagation lag — vendor-UNVERIFIED, treated as possible per
#: Golden Rule #1), so the immediate post-refresh retry can 401 once more; with a
#: budget of 1 that second 401 exhausted the budget → a TERMINAL ``GrowwAuthError``
#: → the new pull-wide halt killed the whole ~750-symbol overnight pull where the
#: pre-halt behaviour quarantined one pair. A budget of 2 (paired with the small
#: :data:`~brutex.adapters.groww.client.REAUTH_RETRY_BACKOFF_SECONDS` pause before
#: each post-refresh retry) absorbs exactly that one-rotation lag, while a
#: genuinely-dead credential still exhausts the bounded budget and halts LOUDLY
#: (never an infinite loop — the halt semantics of DEC-GROWW-AUTH-HALT-001 are
#: preserved, just not hair-triggered on the daily rotation).
GROWW_MAX_REAUTHS: Final[int] = 2

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

#: TCP CONNECT timeout (seconds) for every underlying HTTP request the Groww SDK
#: issues on our behalf (DEC-GROWW-HTTP-SOCKET-TIMEOUT-001). The ``growwapi`` SDK
#: builds its requests with module-level ``requests.get``/``post`` and a per-call
#: ``timeout`` defaulting to ``None`` (INFINITE), and BruteX never forwards one — so
#: before this the ONLY bound on a half-open / dead socket was the 45 s wall-clock
#: FREEZE GUARD (:data:`GROWW_PULL_CALL_TIMEOUT_S`), which on a flaky network burns
#: 45 s × the bounded retry ladder (~6 attempts) ≈ 4.5 min PER window before the
#: transient is even classified. A ``requests`` connect timeout fails a dead peer in
#: SECONDS instead. 10 s is comfortably below the 45 s wall-clock cap (so the socket
#: timeout ALWAYS fires first — the wall-clock guard stays only as belt-and-suspenders
#: for a hang that occurs OUTSIDE the socket, e.g. a wedged non-network syscall) yet is
#: generous for a real TCP handshake to Groww. A resulting ``requests.ConnectTimeout``
#: is already classified RETRYABLE (:mod:`~brutex.adapters.groww.errors`), so it flows
#: into the existing retry → defer → re-pull machinery with NO pacing change. Env-tunable
#: via ``BRUTEX_GROWW_HTTP_CONNECT_TIMEOUT_S``. NOT a vendor-documented limit.
GROWW_HTTP_CONNECT_TIMEOUT_S: Final[float] = 10.0

#: SOCKET-READ timeout (seconds) for every underlying HTTP request the Groww SDK issues
#: on our behalf (DEC-GROWW-HTTP-SOCKET-TIMEOUT-001) — the max idle gap the SDK will wait
#: for the NEXT byte of a response before ``requests`` raises ``ReadTimeout``. This bounds
#: the half-open socket that has connected but stalled mid-transfer (the classic dead-peer
#: symptom the 45 s wall-clock guard used to absorb). 30 s is comfortably below the 45 s
#: wall-clock cap (:data:`GROWW_PULL_CALL_TIMEOUT_S`) so the read timeout fires FIRST, yet
#: is generous for a legitimately slow ≤180-day historical-candle window to stream. A
#: resulting ``requests.ReadTimeout`` is already classified RETRYABLE
#: (:mod:`~brutex.adapters.groww.errors`), so a stalled read flows into the existing
#: retry → defer → re-pull machinery with NO pacing change. Env-tunable via
#: ``BRUTEX_GROWW_HTTP_READ_TIMEOUT_S``. NOT a vendor-documented limit.
GROWW_HTTP_READ_TIMEOUT_S: Final[float] = 30.0

#: Hard per-window wall-clock deadline (seconds) for ONE durable lake write — the
#: DURABLE-WRITE FREEZE GUARD (DEC-FETCH-HUNG-WRITE-DEADLINE-001). The lake
#: read-modify-write ends in ``os.fsync`` + atomic ``os.replace``
#: (:func:`~brutex.data.atomic_io.atomic_write_via`); those syscalls carry NO inherent
#: wall-clock bound, so a WEDGED durable write (a hung ``fsync`` / ``os.replace`` on a
#: stalled external SSD / NFS mount that NEVER raises) would block its fetch-pipeline
#: consumer thread forever, fill its bounded queue, and eventually wedge the PRODUCER (the
#: fetch lane) on back-pressure at 0 r/s while the rate controller still "allows" the
#: ceiling. :func:`~brutex.adapters.groww.write_deadline.append_with_deadline` runs
#: ``sink.append`` under this deadline so a hang raises a TRANSIENT
#: :class:`~brutex.data.errors.WriteDeadlineExceeded` — the ingest spines DEFER that one
#: window (record FAILED → re-pull next round) and the fetch lane is FREED. This deadline
#: wraps the WHOLE ``sink.append`` — the ``flock`` ACQUIRE (bounded by
#: :data:`~brutex.data.partition_lock.DEFAULT_PARTITION_LOCK_TIMEOUT_SECONDS`) PLUS the
#: merge/render/fsync/replace — so the ORDERING INVARIANT
#: (DEC-WRITE-DEADLINE-LOCK-ORDER-001) is: the lock-acquire default MUST stay strictly
#: BELOW this value with real margin (75 s < 90 s, 15 s margin). When the lock bound sat
#: ABOVE this deadline (the prior 120 s default), the lock bound was UNREACHABLE — this
#: deadline abandoned the write thread at 90 s while it still blocked in the ``flock``
#: poll loop, leaking the thread + lock fd instead of taking the clean
#: :class:`~brutex.data.errors.PartitionLockTimeout` defer path. The ordering is pinned
#: by ``tests/unit/test_write_deadline_lock_order.py`` (the two constants deliberately
#: live in their own modules — ``brutex.data`` never imports ``brutex.adapters``); the
#: env overrides (``BRUTEX_GROWW_WRITE_DEADLINE_S`` / ``BRUTEX_PARTITION_LOCK_TIMEOUT_S``)
#: CAN re-invert it at runtime, which ``append_with_deadline`` WARNs about once per
#: process at use time. 90 s is GENEROUS — a merely-SLOW deep-month merge (hundreds of ms
#: typical; pathological deep partitions a few seconds) always completes well within it,
#: but a true hang trips it in bounded time, far under the 600 s pull watchdog. NOT a
#: vendor-documented limit; operator-tunable via
#: :func:`~brutex.adapters.groww.write_deadline.resolve_write_deadline_seconds`
#: (env ``BRUTEX_GROWW_WRITE_DEADLINE_S``).
GROWW_WRITE_DEADLINE_S: Final[float] = 90.0

# ── Vendor-down error-storm breaker + dead-credential escalation ──────────────
# DEC-VENDOR-DOWN-BREAKER-001 (:mod:`brutex.adapters.error_storm_breaker`). Every
# threshold below is an OPERATOR-TUNABLE HEURISTIC over response shapes — NOT a
# vendor-documented limit (Golden Rule #1/#10: Groww documents no outage/credential
# semantics; ``docs/groww/api-reference/01-introduction.md`` covers only rate
# limits). They calibrate "how much sustained, success-free transport failure
# reads as a vendor outage / dead credential", nothing about Groww itself.

#: Minimum DISTINCT symbols the current zero-success transport-failure storm must
#: span before the vendor-down breaker may trip. ONE symbol failing every window
#: (a delisted / mis-addressed name) is a per-symbol problem — the quarantine
#: handles it; a real vendor outage fails EVERY in-flight symbol alike, so 3 keeps
#: a single-symbol blip from ever halting a pull. The F&O spine feeds the
#: contract's UNDERLYING symbol (not the per-strike contract symbol), so one
#: underlying's whole multi-contract chain failing alone can never satisfy this
#: floor either — the guarantee is per underlying on both spines. Heuristic;
#: env-overridable (:data:`GROWW_STORM_MIN_SYMBOLS_ENV`).
GROWW_STORM_MIN_DISTINCT_SYMBOLS: Final[int] = 3

#: Minimum DISTINCT ladder-exhausted, zero-success transport-failure windows
#: before the vendor-down breaker may trip. Each counted window means one window's
#: WHOLE bounded retry ladder was spent (the client's 6-attempt fast-retry — every
#: attempt failing), and each window counts at most ONCE per pull (dedupe on the
#: ledger window identity — a retry pass / until-clean round / resume stream
#: re-observing the SAME failed window never re-counts it, so a few sticky
#: windows can never fake a storm), so 25 distinct all-failed windows with zero
#: successes ≈ 150+ failed vendor calls — far past any blip the AIMD ladder
#: absorbs. TIMING honesty (Golden Rule #10): reached within seconds under a
#: FAST-FAIL outage shape (connection refused / instant 5xx) at the production
#: ceiling; a read-timeout-shaped outage burns the full client timeout ladder per
#: window, so the trip can take tens of minutes there. ANY success resets the
#: count, so a mixed pull can never trip. Heuristic; env-overridable
#: (:data:`GROWW_STORM_MIN_FAILURES_ENV`).
GROWW_STORM_MIN_FAILED_WINDOWS: Final[int] = 25

#: Minimum CONSECUTIVE :class:`~brutex.adapters.groww.errors.GrowwTokenStillStaleError`
#: ladder-exhausted outcomes (zero adopted-token successes in between) before the
#: dead-credential escalation may trip. A healthy 06:00-IST rotation surfaces a
#: handful of StillStale deferrals until the Lambda's ~06:05 mint lands; 8
#: consecutive with no adopted-token success is past that shape. Heuristic;
#: env-overridable (:data:`GROWW_CREDENTIAL_DEAD_STREAK_ENV`).
GROWW_CREDENTIAL_DEAD_STREAK: Final[int] = 8

#: Minimum MINUTES the StillStale streak must span (first → latest observation,
#: measured on an injected monotonic clock) before the dead-credential escalation
#: may trip — the time gate that separates "the Lambda mints a few minutes late"
#: (self-heals) from "the broker/credential is DEAD" (operator action). 10 min is
#: ~2× the observed healthy 06:00→~06:05 mint lag. Heuristic; env-overridable
#: (:data:`GROWW_CREDENTIAL_DEAD_MINUTES_ENV`).
GROWW_CREDENTIAL_DEAD_MIN_MINUTES: Final[float] = 10.0

#: Env override for :data:`GROWW_STORM_MIN_DISTINCT_SYMBOLS` (int; ``<= 0`` → any).
GROWW_STORM_MIN_SYMBOLS_ENV: Final[str] = "BRUTEX_STORM_BREAKER_MIN_SYMBOLS"
#: Env override for :data:`GROWW_STORM_MIN_FAILED_WINDOWS` (int; ``<= 0`` disables
#: the vendor-down rule).
GROWW_STORM_MIN_FAILURES_ENV: Final[str] = "BRUTEX_STORM_BREAKER_MIN_FAILURES"
#: Env override for :data:`GROWW_CREDENTIAL_DEAD_STREAK` (int; ``<= 0`` disables
#: the dead-credential rule).
GROWW_CREDENTIAL_DEAD_STREAK_ENV: Final[str] = "BRUTEX_CREDENTIAL_DEAD_STREAK"
#: Env override for :data:`GROWW_CREDENTIAL_DEAD_MIN_MINUTES` (float minutes;
#: ``0`` removes the time gate — pure streak; negative/invalid falls back).
GROWW_CREDENTIAL_DEAD_MINUTES_ENV: Final[str] = "BRUTEX_CREDENTIAL_DEAD_MINUTES"
#: Kill switch: ``BRUTEX_ERROR_STORM_BREAKER=off|0|false|no`` disables BOTH
#: mechanisms entirely (observations no-op, never trips). Default ON.
GROWW_ERROR_STORM_ENABLED_ENV: Final[str] = "BRUTEX_ERROR_STORM_BREAKER"

#: Minimum DISTINCT symbols with a TERMINAL vendor AUTHORISATION rejection
#: (:class:`~brutex.adapters.groww.errors.GrowwAuthorisationError` — the SDK's
#: ``GrowwAPIAuthorisationException`` class) — with ZERO transport successes since
#: the storm began — before the breaker escalates to the fleet-wide
#: :class:`~brutex.adapters.groww.errors.GrowwAuthorisationStormError` pull HALT
#: (DEC-GROWW-AUTHZ-QUARANTINE-001). BELOW the threshold each authorisation error is
#: a PER-SYMBOL quarantine (one entitlement-restricted instrument never stops a
#: ~750-symbol pull); AT/ABOVE it the honest verdict is an account-level suspension
#: (which hits every symbol), so the old pull-wide HALT semantics are restored.
#: Heuristic, not a vendor fact; env-overridable
#: (:data:`GROWW_AUTHZ_STORM_MIN_SYMBOLS_ENV`); ``<= 0`` disables the arm.
GROWW_AUTHZ_STORM_MIN_SYMBOLS: Final[int] = 5
#: Env override for :data:`GROWW_AUTHZ_STORM_MIN_SYMBOLS` (int; ``<= 0`` disables
#: the authorisation-storm arm — every authorisation error stays a quarantine).
GROWW_AUTHZ_STORM_MIN_SYMBOLS_ENV: Final[str] = "BRUTEX_GROWW_AUTHZ_STORM_MIN_SYMBOLS"

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

#: The IST hour at which the Groww daily access token resets (the session boundary
#: for the on-disk token cache, DEC-GROWW-TOKEN-DISK-CACHE-001). A token minted BEFORE
#: this hour belongs to the PREVIOUS trading session; on/after it, the current session.
#: HONEST (Golden Rule #10): 06:00 IST is asserted in CLAUDE.md §6 / ADR-0009 / the
#: production logs (the token died at exactly 06:00 IST), NOT in the SDK reference —
#: which in fact labels the TOTP flow "No Expiry" (docs/groww/api-reference/\
#: 01-introduction.md:64). It is exposed as a CONSTANT so a future verified probe can
#: correct it without a code change; a wrong boundary is SELF-CORRECTING on the token's
#: first real use (the rejected token drives the existing in-pull forced re-mint, which
#: overwrites the cache) at the cost of at most one extra mint that day, never silently wrong.
GROWW_TOKEN_RESET_HOUR_IST: Final[int] = 6
#: Env override for :data:`GROWW_TOKEN_RESET_HOUR_IST` (operator escape hatch, no code change).
GROWW_TOKEN_RESET_HOUR_ENV: Final[str] = "BRUTEX_GROWW_TOKEN_RESET_HOUR_IST"  # noqa: S105  # env var name, not a secret

#: On-disk path for the cached daily Groww access token (DEC-GROWW-TOKEN-DISK-CACHE-001).
#: Default ``~/.brutex/cache/groww_access_token.json`` (mode 0600), mirroring the other
#: per-user cache state (``~/.brutex/cache/ntm_constituents.csv``). Override via
#: :data:`GROWW_TOKEN_CACHE_PATH_ENV`. The cache lets a pull mint AT MOST once per
#: session per machine and reuse the token across the 3 pipeline phases + cold starts,
#: eliminating the self-inflicted 3×-per-run mint storm that tripped the vendor's
#: token-issuance limit (operator-reported fresh-clone pull, MIGRATION.md entry #1041).
GROWW_TOKEN_CACHE_PATH_ENV: Final[str] = "BRUTEX_GROWW_TOKEN_CACHE_PATH"  # noqa: S105  # env var name, not a secret
#: Cache-file basename under ``~/.brutex/cache/`` (kept matched to the ``.gitignore``
#: ``**/groww_access_token*`` glob so a redirected cache can never be committed).
GROWW_TOKEN_CACHE_FILENAME: Final[str] = "groww_access_token.json"  # noqa: S105  # filename, not a secret
#: JSON schema version stamped in the cache file; a mismatch ⇒ cache-miss → re-mint.
GROWW_TOKEN_CACHE_SCHEMA: Final[int] = 1

#: Bounded exponential-back-off budget for a token-issuance THROTTLE on the COLD-START
#: mint (DEC-GROWW-TOKEN-DISK-CACHE-001). When the FIRST mint of a fresh clone is itself
#: issuance-throttled (no cache to fall back to), the cold-start path re-checks the cache
#: (a racing phase/process may have just written one) then sleeps + retries up to this
#: many ATTEMPTS before re-raising a RETRYABLE throttle (the pull quarantines the window +
#: re-pulls across pulls — never a whole-pull crash). The back-off runs OUTSIDE any pool
#: lock (it is on the cold-start build path, never inside :meth:`SdkRefresher.reauth`'s
#: locked block), so it can never freeze a pool leaf (hostile-review M2 fold).
GROWW_TOKEN_ISSUANCE_RETRY_ATTEMPTS: Final[int] = 3
#: Base seconds for the cold-start issuance back-off (``base × 2**attempt``, attempt 0-based).
#: Deliberately SMALL — it rides out the concurrent storm we ourselves caused (already
#: eliminated in steady state by the cache), NOT a long vendor outage. Capped per attempt
#: by :data:`GROWW_TOKEN_ISSUANCE_RETRY_CAP_S` so the total stays well under a pull deadline.
GROWW_TOKEN_ISSUANCE_RETRY_BASE_S: Final[float] = 1.0
#: Per-attempt cap (seconds) for the cold-start issuance back-off sleep.
GROWW_TOKEN_ISSUANCE_RETRY_CAP_S: Final[float] = 4.0

# ── Token-stale fetch-lane PAUSE (DEC-TOKEN-STALE-PAUSE-001) ──────────────────
#: Wall-clock bound (seconds) the fetch lane WAITS for the read-only token broker
#: to hold a fresh token before a :class:`~brutex.adapters.groww.errors.\
#: GrowwTokenStillStaleError` is allowed to propagate. Forensically calibrated:
#: the operator's real 2026-07-04 overnight run measured the 06:00-IST reset →
#: external-Lambda mint lag at 5 m 31 s (331 s) — during which the F&O window
#: ladder burned all its attempts on 111 windows (339 failed calls) that the
#: until-clean rounds then had to rescue. 420 s (7 min) covers that observed
#: worst case with margin while staying WELL under the 600 s pull watchdog; the
#: wait additionally beats the progress-beat seam so the watchdog can never
#: false-fire during it. A dead broker still fails LOUDLY: once the (shared,
#: per-generation) deadline expires the error propagates exactly as before —
#: the window defers FAILED and the dead-credential escalation
#: (DEC-VENDOR-DOWN-BREAKER-001) still applies. HONEST trade (Golden Rule
#: #10): because the whole pool spends AT MOST ONE such pause per stale
#: generation, a GENUINELY dead broker's ``GrowwCredentialDeadError``
#: escalation is DELAYED by at most one :data:`GROWW_TOKEN_STALE_WAIT_S`
#: (~420 s — the worst-case time-to-halt moves from the breaker's ~600 s
#: streak-span floor to ~1020 s wall-clock) — deliberate, bounded, and
#: disclosed, bought so a HEALTHY 06:00 rotation never burns window
#: attempts. Cosmetic side-effect: a worker sleeping in the pause still
#: holds its in-flight call-meter slot (``meter.start`` fired before the
#: auth error surfaced), so the ``in_flight`` gauge reads elevated during a
#: pause; the achieved-rps completed-rate is NOT inflated (a paused attempt
#: never records ``completed``). NOT a vendor limit — an operator-tunable
#: pause bound (env :data:`GROWW_TOKEN_STALE_WAIT_ENV`).
GROWW_TOKEN_STALE_WAIT_S: Final[float] = 420.0
#: Env override for :data:`GROWW_TOKEN_STALE_WAIT_S` (float seconds; ``0``
#: explicitly DISABLES the pause — byte-identical to the pre-gate behaviour;
#: blank / non-numeric / non-finite / negative falls back to the default).
GROWW_TOKEN_STALE_WAIT_ENV: Final[str] = "BRUTEX_GROWW_TOKEN_STALE_WAIT_S"  # noqa: S105  # env var name, not a secret
#: Poll cadence (seconds) between fresh-token checks during the stale-wait.
#: ONE waiter per pool (the leader) does ONE broker re-read per tick — never a
#: per-thread SSM stampede; the others only poll the in-process generation.
GROWW_TOKEN_STALE_POLL_S: Final[float] = 5.0
#: Bounded structlog cadence (seconds) for the ``groww.token.stale_wait`` event
#: — ONE event per pool per interval, however many threads are waiting.
GROWW_TOKEN_STALE_LOG_EVERY_S: Final[float] = 30.0

# ── Shared token broker (DEC-TOKEN-BROKER-AWS-001 / DEC-AWS-ONLY-CREDS-001) ───
# The operator's FINAL design: the Groww api_key, TOTP secret AND the daily minted
# access token ALL live in ONE shared place — AWS Systems Manager Parameter Store
# (region ``ap-south-1``) — read by BOTH TickVault (live) and BruteX (backtesting).
# A SINGLE dedicated Lambda is the ONLY writer/minter of the shared access token;
# BruteX and TickVault are strictly READ-ONLY consumers. Because there is exactly ONE
# writer, there is NO compare-and-swap / DynamoDB need — a plain SecureString parameter
# suffices (a read is a ``GetParameter WithDecryption=True`` that KMS-decrypts the
# SecureString; AWS SSM docs "Working with SecureString parameters", ``GetParameter`` API
# reference). AWS is now the DEFAULT AND ONLY credential/token source
# (DEC-AWS-ONLY-CREDS-001): the ``BRUTEX_TOKEN_BROKER_READONLY`` opt-in toggle + the
# local-mint fallback were REMOVED, so the broker read is unconditional + fail-fast.
#
#: AWS region for the token-broker SSM client. Unset/blank ⇒ the operator default
#: :data:`GROWW_TOKEN_AWS_REGION_DEFAULT` (``ap-south-1`` — where the SecureStrings
#: already live). ``AWS_REGION`` is honoured as a secondary override before the default.
GROWW_TOKEN_AWS_REGION_ENV: Final[str] = "BRUTEX_TOKEN_AWS_REGION"  # noqa: S105  # env var name, not a secret
#: Standard AWS SDK region env var, honoured as a secondary override.
AWS_REGION_ENV: Final[str] = "AWS_REGION"
#: The operator-confirmed default region (the SecureStrings live in ap-south-1).
GROWW_TOKEN_AWS_REGION_DEFAULT: Final[str] = "ap-south-1"  # noqa: S105  # AWS region, not a secret

#: Full Parameter-Store path of the SHARED daily access token (the Lambda writes it;
#: BruteX reads it). Env-overridable; default is the operator's fixed path.
GROWW_TOKEN_PARAM_ACCESS_TOKEN_ENV: Final[str] = "BRUTEX_TOKEN_PARAM_ACCESS_TOKEN"  # noqa: S105  # env var name, not a secret
GROWW_TOKEN_PARAM_ACCESS_TOKEN_DEFAULT: Final[str] = "/tickvault/prod/groww/access-token"  # noqa: S105  # parameter path, not a secret
#: Full Parameter-Store path of the shared Groww api_key SecureString. Env-overridable.
GROWW_TOKEN_PARAM_API_KEY_ENV: Final[str] = "BRUTEX_TOKEN_PARAM_API_KEY"  # noqa: S105  # env var name, not a secret
GROWW_TOKEN_PARAM_API_KEY_DEFAULT: Final[str] = "/tickvault/prod/groww/api-key"  # noqa: S105  # parameter path, not a secret
#: Full Parameter-Store path of the shared Groww TOTP-secret SecureString. Env-overridable.
GROWW_TOKEN_PARAM_TOTP_SECRET_ENV: Final[str] = "BRUTEX_TOKEN_PARAM_TOTP_SECRET"  # noqa: S105  # env var name, not a secret
GROWW_TOKEN_PARAM_TOTP_SECRET_DEFAULT: Final[str] = "/tickvault/prod/groww/totp-secret"  # noqa: S105  # parameter path, not a secret


#: Environment toggle for the anti-burst smooth pacing
#: (DEC-GROWW-SMOOTH-PACING-001 + DEC-SMOOTH-PACING-AUTOLATCH-001). The token bucket
#: already caps the AVERAGE rate at ``current_rps``, but its ``1/rate`` cadence still
#: lets sub-second micro-bursts through (the operator's ``rps_1s_max`` spiking to 8–9
#: below the AIMD ceiling), which can trip a vendor 429. THREE modes
#: (:func:`resolve_smooth_pacing_mode`): a truthy value forces smooth pacing ON from
#: construction; an explicit falsy value (``"0"`` / ``"false"`` / ``"no"`` / ``"off"``)
#: disables it AND the auto-latch entirely; unset/blank (the DEFAULT) is ADAPTIVE —
#: smoothing starts OFF (byte-identical pacing) and ONE-WAY LATCHES ON the first time a
#: 429 lands with measured per-1s burst evidence above the then-current allowed rate.
#: The latch only ever ADDS spacing (caps + AIMD ladder UNCHANGED — CLAUDE.md §2 Rule
#: 6); its live 429-reduction remains UNVERIFIED pending a live probe (Golden Rule #10).
GROWW_SMOOTH_PACING_ENV: Final[str] = "BRUTEX_GROWW_SMOOTH_PACING"

#: Explicit-falsy env values that select the OFF smooth-pacing mode (latch disabled
#: entirely). Anything truthy selects ON; unset/blank/unrecognised selects ADAPTIVE.
_SMOOTH_PACING_OFF_VALUES: Final[frozenset[str]] = frozenset({"0", "false", "no", "off"})
#: Truthy env values that select the ON smooth-pacing mode (latched from construction).
_SMOOTH_PACING_ON_VALUES: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})


def resolve_smooth_pacing_mode() -> str:
    """Resolve the TRI-STATE smooth-pacing mode from ``BRUTEX_GROWW_SMOOTH_PACING``.

    DEC-SMOOTH-PACING-AUTOLATCH-001. Returns one of the vendor-neutral
    :mod:`brutex.adapters.rate_common` mode tokens:

    * **ADAPTIVE** (:data:`~brutex.adapters.rate_common.SMOOTH_PACING_ADAPTIVE`) —
      the env var is UNSET / blank / unrecognised. Smoothing starts OFF
      (byte-identical pacing) and the controller ONE-WAY latches it ON when a 429
      arrives with measured per-1s burst evidence above the then-current allowed
      rate. The new DEFAULT: evidence-gated, never raises any cap, so a bad env
      value degrades to the safe adaptive default rather than silently forcing
      smoothing ON (CLAUDE.md §2 Rule 6 — a rate gate is never FORCED by accident;
      the latch only ADDS spacing on measured evidence).
    * **ON** (:data:`~brutex.adapters.rate_common.SMOOTH_PACING_ON`) — an explicit
      truthy value (``"1"`` / ``"true"`` / ``"yes"`` / ``"on"``, case-insensitive):
      latched from construction, the prior ``smooth_pacing=True`` behaviour.
    * **OFF** (:data:`~brutex.adapters.rate_common.SMOOTH_PACING_OFF`) — an explicit
      falsy value (``"0"`` / ``"false"`` / ``"no"`` / ``"off"``): smoothing AND the
      auto-latch are disabled entirely (the pre-latch escape hatch).

    O(1). The rate_common import is lazy so this constants module stays
    import-light (the repo's deliberate lazy-import pattern).
    """
    from brutex.adapters.rate_common import (
        SMOOTH_PACING_ADAPTIVE,
        SMOOTH_PACING_OFF,
        SMOOTH_PACING_ON,
    )

    raw = os.environ.get(GROWW_SMOOTH_PACING_ENV)
    if raw is None:
        return SMOOTH_PACING_ADAPTIVE
    token = raw.strip().lower()
    if token in _SMOOTH_PACING_ON_VALUES:
        return SMOOTH_PACING_ON
    if token in _SMOOTH_PACING_OFF_VALUES:
        return SMOOTH_PACING_OFF
    return SMOOTH_PACING_ADAPTIVE


def resolve_smooth_pacing() -> bool:
    """Legacy BOOLEAN smooth-pacing view — ``True`` iff the resolved mode is ON.

    Kept as a thin compat delegate over :func:`resolve_smooth_pacing_mode`
    (DEC-SMOOTH-PACING-AUTOLATCH-001): it is an exported public symbol with
    existing pins, and its ORIGINAL contract — ``True`` only for an explicit
    truthy ``BRUTEX_GROWW_SMOOTH_PACING`` (DEC-GROWW-SMOOTH-PACING-001) — is
    preserved exactly (ADAPTIVE and OFF both read ``False``). The production
    build sites now pass the tri-state mode instead; use
    :func:`resolve_smooth_pacing_mode` in new code. O(1).
    """
    from brutex.adapters.rate_common import SMOOTH_PACING_ON

    return resolve_smooth_pacing_mode() == SMOOTH_PACING_ON


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
    "AWS_REGION_ENV",
    "BRUTE_FORCE_INDEX_SYMBOLS",
    "CHUNK_WINDOW_30D_INTERVALS",
    "CHUNK_WINDOW_90D_INTERVALS",
    "CHUNK_WINDOW_180D_INTERVALS",
    "EARLIEST_HISTORY",
    "EXCLUDED_SEGMENTS",
    "EXPECTED_FNO_STOCK_COUNT",
    "GROWW_AUTHZ_STORM_MIN_SYMBOLS",
    "GROWW_AUTHZ_STORM_MIN_SYMBOLS_ENV",
    "GROWW_CLIENT_POOL_MAX",
    "GROWW_CLIENT_POOL_SIZE",
    "GROWW_CREDENTIAL_DEAD_MINUTES_ENV",
    "GROWW_CREDENTIAL_DEAD_MIN_MINUTES",
    "GROWW_CREDENTIAL_DEAD_STREAK",
    "GROWW_CREDENTIAL_DEAD_STREAK_ENV",
    "GROWW_CSV_ALLOWED_HOST",
    "GROWW_DAILY_QUOTA_UNVERIFIED",
    "GROWW_ERROR_STORM_ENABLED_ENV",
    "GROWW_EXCHANGES",
    "GROWW_FUTURE_COVERAGE_MARKET",
    "GROWW_HTTP_CONNECT_TIMEOUT_S",
    "GROWW_HTTP_READ_TIMEOUT_S",
    "GROWW_INSTRUMENT_CSV_URL",
    "GROWW_INSTRUMENT_TYPES",
    "GROWW_MAX_REAUTHS",
    "GROWW_OPTION_COVERAGE_MARKET",
    "GROWW_POOL_SIZE_ENV",
    "GROWW_PULL_CALL_TIMEOUT_S",
    "GROWW_RATE_LIMIT_PER_MIN",
    "GROWW_RATE_LIMIT_RPS_CEILING",
    "GROWW_RATE_LIMIT_RPS_UNVERIFIED",
    "GROWW_SEGMENTS",
    "GROWW_SMOOTH_PACING_ENV",
    "GROWW_STORM_MIN_DISTINCT_SYMBOLS",
    "GROWW_STORM_MIN_FAILED_WINDOWS",
    "GROWW_STORM_MIN_FAILURES_ENV",
    "GROWW_STORM_MIN_SYMBOLS_ENV",
    "GROWW_TOKEN_AWS_REGION_DEFAULT",
    "GROWW_TOKEN_AWS_REGION_ENV",
    "GROWW_TOKEN_CACHE_FILENAME",
    "GROWW_TOKEN_CACHE_PATH_ENV",
    "GROWW_TOKEN_CACHE_SCHEMA",
    "GROWW_TOKEN_ISSUANCE_RETRY_ATTEMPTS",
    "GROWW_TOKEN_ISSUANCE_RETRY_BASE_S",
    "GROWW_TOKEN_ISSUANCE_RETRY_CAP_S",
    "GROWW_TOKEN_PARAM_ACCESS_TOKEN_DEFAULT",
    "GROWW_TOKEN_PARAM_ACCESS_TOKEN_ENV",
    "GROWW_TOKEN_PARAM_API_KEY_DEFAULT",
    "GROWW_TOKEN_PARAM_API_KEY_ENV",
    "GROWW_TOKEN_PARAM_TOTP_SECRET_DEFAULT",
    "GROWW_TOKEN_PARAM_TOTP_SECRET_ENV",
    "GROWW_TOKEN_RESET_HOUR_ENV",
    "GROWW_TOKEN_RESET_HOUR_IST",
    "GROWW_TOKEN_STALE_LOG_EVERY_S",
    "GROWW_TOKEN_STALE_POLL_S",
    "GROWW_TOKEN_STALE_WAIT_ENV",
    "GROWW_TOKEN_STALE_WAIT_S",
    "GROWW_WRITE_DEADLINE_S",
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
    "resolve_smooth_pacing_mode",
    "resolve_token_broker_region",
    "resolve_token_param_path",
]
