# BruteX — System Requirements (v1.0 DRAFT)

**Status:** DRAFT — awaiting user sign-off
**Branch:** `claude/capture-system-requirements-F9IHZ`
**Date:** 2026-04-14
**Scope lock:** Indian F&O (NSE CASH + FNO) via Groww `instruments` + `backtesting` surfaces only. Global-market extensibility deferred to v2+.

> This document is the **single source of truth**. Every future PR must reference an ID from this file (`FR-###`, `NFR-###`, `MG-###`, `ADR-###`). No scope expansion without a new version bump.

---

## Table of Contents

1. Purpose & Scope
2. Hard Constraints (Groww Ground Truth)
3. Finalized Decisions
4. Functional Requirements (FR)
5. Non-Functional Requirements (NFR)
6. Data Model
7. Groww Adapter Contract
8. Ingest Profile & Backfill Strategy
9. Integrity Gates
10. Architecture Layers (L1–L7)
11. Brute-Force Orchestrator Design
12. Execution Simulator (Indian cost model)
13. Risk & Portfolio Book
14. Performance Engineering & O(1) Proofs
15. Observability (Monitoring / Logging / Alerting)
16. Security & Security Hardening
17. 100% Coverage Gates (with mechanical proof)
18. Missing Functionality Register (MG)
19. Risk Register
20. Phase Plan with Exit Criteria
21. Acceptance Test Matrix
22. Architecture Decision Records (ADR)
23. Glossary
24. Appendix A — Verified Groww Facts & Sources
25. Appendix B — Honest Gap List
26. Appendix C — Open Questions Deferred

---

## 1. Purpose & Scope

### 1.1 Mission

Build an **extreme drill-down brute-force backtesting engine** for Indian equities, index, and F&O markets that:

- Supports **all timeframes** dynamically at runtime (no recompile to add a new TF).
- Executes **millions of parameter combinations** per strategy under a defined hardware envelope with a **mechanically-proven** throughput SLA.
- Guarantees **reproducibility, auditability, uniqueness, and deduplication** at the level of every single combo.
- Is **offline-first**: Groww is touched only to ingest data. The brute-force hot path never calls Groww.
- Is **globally extensible**: the same indicators, strategies, and engine will power future adapters (Zerodha, Upstox, IBKR, Binance, Polygon) without touching core layers.

### 1.2 In-Scope (v1)

| Area | v1 scope |
|------|----------|
| Exchanges | NSE only |
| Segments | CASH + FNO |
| Broker adapter | Groww — `instruments` + `backtesting` only |
| Horizon | 2020-01-01 → today |
| Universe | All NSE equities + all indices + all futures + all option contracts (stock + index) |
| Granularity | All timeframes (1m, 3m, 5m, 10m, 15m, 30m, 60m, 240m, 1D, weekly, custom) via runtime resampling from smallest base bar |
| Mode | Offline brute-force backtesting + forward-walking (replay from cache) |
| Overfit guards | Walk-Forward + Combinatorial Purged Cross-Validation (CPCV) + PBO + Deflated Sharpe |

### 1.3 Out-of-Scope (v1)

- Live market data feeds (REST or WebSocket)
- Live order placement, portfolio, margins-live, funds
- Commodity segment (Groww does not support commodity in backtesting)
- BSE cash/derivatives (deferred to v1.1)
- Options Greeks historical API (we compute Greeks from price + Black-76/BS — see FR-GRK)
- Tick data / Level-2 order book history (Groww does not expose)
- Sub-minute backtest fidelity (bounded by the 1-minute minimum bar)
- Multi-user / SaaS deployment (single-operator v1)

### 1.4 Deferred to Later Versions

| Version | Addition |
|---------|----------|
| v1.1 | BSE cash + derivatives, tick data adapter, Greeks cache |
| v1.2 | Live forward-testing via broker WebSocket (paper mode) |
| v2.0 | Global-market adapters (Binance, IBKR, Polygon), multi-tenant |
| v2.1 | Distributed cluster (Ray multi-node), 10M+ combos SLA |

---

## 2. Hard Constraints (Groww Ground Truth)

These are **verified** from Groww's public documentation and are treated as immovable physical constraints. See **Appendix A** for source citations.

| # | Constraint | Value | Impact |
|---|------------|-------|--------|
| HC-01 | Backtest-supported segments | `CASH`, `FNO` only | No commodity backtests via Groww |
| HC-02 | FNO history depth | From **2020** onward only | 5-year max backtest horizon for F&O |
| HC-03 | OHLC rate limit | ~25 req/s (shared with quote bucket) | Must cap at **15 req/s effective** (0.6 safety factor) |
| HC-04 | Minimum granularity | 1-minute candles | Sub-minute fidelity impossible |
| HC-05 | Tick data / L2 | **Not exposed** | No HFT/scalping backtests |
| HC-06 | Options chain history API | **Not provided** as a single call | Must reconstruct per-contract via symbol enumeration |
| HC-07 | Instrument master | Daily CSV snapshot, no history | Temporal master must be built by snapshotting daily |
| HC-08 | Corporate actions | **Not in instrument CSV** | Must be sourced / computed externally |
| HC-09 | Historical option contracts (expired) | Enumerable via `get_expiries(year=N)` + `get_contracts(expiry_date)` — 2020 onwards | See `docs/groww-sdk/backtesting.md §3-§4` |
| HC-10 | SDK method | `get_historical_candles(...)`; `get_historical_candle_data` is **deprecated** | Use only the non-deprecated method |
| HC-11 | Time format | `"YYYY-MM-DD HH:mm:ss"` | IST, no timezone suffix |
| HC-12 | Max duration per candle request | **30 days** (1m/2m/3m/5m), **90 days** (10m/15m/30m), **180 days** (1h/4h/1d/1w/1mo) | Verbatim from `docs/groww-sdk/backtesting.md §6`. Adapter chunker (`FR-ING-006`) splits requests by these limits. |
| HC-13 | Exception surface | Groww SDK raises typed exceptions from `growwapi.groww.exceptions` — `GrowwAPIRateLimitException` / `GrowwAPITimeoutException` / `GrowwAPIAuthentication(Authorisation)Exception` / `GrowwAPIBadRequestException` / `GrowwAPINotFoundException`. Retry-after value availability on rate-limit exception is unverified (OQ-GROWW-429-RETRY-AFTER). Full classification in `docs/groww-sdk/exceptions.md §3`. |
| HC-14 | Auth | API auth token (`GROWW_API_AUTH_TOKEN`) | Data-only scope recommended (lower blast radius) |
| HC-15 | PyPI package | `growwapi` (v1.5.0 as of Dec 2025, Python 3.9–3.13) | Version pinning in lockfile |

---

## 3. Finalized Decisions

Locked in prior discussion with the user. Any change requires explicit user approval and a version bump.

| # | Decision | Value |
|---|----------|-------|
| FD-01 | Granularity | Dynamic — all timeframes, runtime-scalable via resampler |
| FD-02 | Compute framework | Ray actor pool |
| FD-03 | Overfit guard | Both CPCV and Walk-Forward, per-run selectable |
| FD-04 | Hot kernel | Rust (PyO3) primary + Numba fallback (v1, not deferred) |
| FD-05 | Groww surfaces | `instruments` + `backtesting` only |
| FD-06 | Exchanges | NSE only in v1 |
| FD-07 | Segments | CASH + FNO |
| FD-08 | Horizon | 2020-01-01 → today |
| FD-09 | Universe | All NSE equities + indices + futures + options |
| FD-10 | Backfill order | Daily bars for full universe first → 1-minute in background |
| FD-11 | Expired options recovery | Deterministic symbol-format probe + golden test + log the misses |
| FD-12 | Auth modes | Both public-CSV path + token-auth path, runtime-selectable |
| FD-13 | Rate limit | ≤ 15 req/s effective (0.6 × 25 req/s OHLC bucket) |
| FD-14 | Storage stack | Parquet + DuckDB + Redis hot + LMDB result cache, content-addressed |
| FD-15 | Reproducibility hash | `sha256(strategy_id ‖ canonical_params ‖ data_snapshot_hash ‖ code_commit_sha)` |
| FD-16 | SLA proof | `bench_1m_combos` CI test ≤ 300s on reference runner |

---

## 4. Functional Requirements (FR)

Each FR has a stable ID, a testable statement, and a verification mechanism.

### 4.1 Data Ingestion (FR-ING)

| ID | Requirement | Verification |
|----|-------------|--------------|
| FR-ING-001 | The system SHALL fetch the Groww instrument CSV from `https://growwapi-assets.groww.in/instruments/instrument.csv` on demand and on a daily schedule. | Golden-master test against a recorded fixture. |
| FR-ING-002 | The system SHALL persist every daily instrument snapshot in append-only form, keyed by `(snapshot_date, sha256(raw_bytes))`. | Unit test: two identical pulls → one stored blob. |
| FR-ING-003 | The system SHALL build and maintain a temporal instrument master (`instruments_temporal`) supporting `as_of(date)` queries for any past day in the horizon. | Test: `as_of(2023-07-15)` returns contracts active on that date only. |
| FR-ING-004 | The system SHALL fetch historical candles via `get_historical_candles(exchange, segment, groww_symbol, start_time, end_time, candle_interval)`. The deprecated `get_historical_candle_data` SHALL NOT be used. | Static check: grep forbids the deprecated call. |
| FR-ING-005 | The system SHALL support all Groww-published candle intervals, enumerated programmatically via `dir(growwapi)`-introspection and golden-master frozen. | Test asserts frozen set matches SDK. |
| FR-ING-006 | The system SHALL chunk historical fetches by probed `max_bars_per_call` per interval, stored per-interval in a config table. | Integration test: probe runs once, caches result. |
| FR-ING-007 | The system SHALL apply a token-bucket rate limiter capped at `BRUTEX_OHLC_RATE_LIMIT_RPS` (default 15 req/s). | Property test: 1000 calls complete in ≥ (1000/15)s. |
| FR-ING-008 | The system SHALL classify Groww SDK exceptions per `docs/groww-sdk/exceptions.md §3`. RETRY bucket (`GrowwAPIRateLimitException`, `GrowwAPITimeoutException`, generic `GrowwAPIException`/`GrowwBaseException`): exponential backoff `[2s, 4s, 8s, 16s]` with ±20% jitter. FAIL FAST bucket (`GrowwAPIBadRequestException`, `GrowwAPINotFoundException`): skip chunk, continue run. HALT RUN bucket (`GrowwAPIAuthenticationException`, `GrowwAPIAuthorisationException`, exhausted rate-limit retries): stop entire ingest, preserve partial state, exit non-zero. | Unit tests with injected mock exceptions — one per bucket — in `tests/unit/test_groww_retry_classification.py`. |
| FR-ING-009 | All raw Groww responses SHALL be stored content-addressed in `raw/groww/<sha256>.json.zst` before parsing. | Test: corrupted parse still leaves raw blob intact. |
| FR-ING-010 | Every ingest run SHALL be idempotent: re-invoking with the same `(symbol, interval, range)` SHALL skip already-present chunks via content-hash dedup. | Test: second run completes in < 1s with zero API calls. |
| FR-ING-011 | The adapter SHALL support both public-CSV (instruments) and token-auth (candles) modes, runtime-selectable. | Config test with both paths. |
| FR-ING-012 | The ingest CLI SHALL be resumable: killing the process mid-run SHALL leave the catalog consistent; rerun SHALL continue from the last completed chunk. | Integration test: SIGKILL mid-run → rerun completes. |

### 4.2 Data Catalog & Storage (FR-CAT)

| ID | Requirement | Verification |
|----|-------------|--------------|
| FR-CAT-001 | Canonical bars SHALL be stored as Parquet, partitioned by `{exchange}/{segment}/{symbol}/{interval}/{yyyy}/{mm}.parquet`. | Filesystem layout test. |
| FR-CAT-002 | A DuckDB catalog SHALL index every (symbol, interval, range, raw_hash, row_count, first_ts, last_ts, fetched_at). | SQL query test. |
| FR-CAT-003 | The catalog SHALL expose a single `get_bars(symbol, interval, start, end)` API returning an Arrow table with zero-copy. | Benchmark: < 1 ms for 1 year of 1m bars from warm cache. |
| FR-CAT-004 | The catalog SHALL compute and expose `data_snapshot_hash` for any `(universe, interval, range)` tuple — the hash of the concatenated content hashes of every file in the slice. | Property test: stable across invocations. |

### 4.3 Data Integrity (FR-INT)

| ID | Requirement | Verification |
|----|-------------|--------------|
| FR-INT-001 | An integrity pass SHALL run after every ingest chunk and flag: gaps, duplicate timestamps, non-monotonic timestamps, NaN values, negative prices, zero-volume in liquid symbols, bars outside the session window. | Integration test with corrupted fixtures. |
| FR-INT-002 | Any integrity failure SHALL block the affected slice from brute-force consumption until resolved. | Test: corrupted slice → strategy query raises `DataIntegrityError`. |
| FR-INT-003 | A session-window validator SHALL use the NSE trading calendar derived from index candle presence (9:15–15:30 IST, weekends excluded, derived holidays). | Calendar test. |
| FR-INT-004 | A look-ahead bias detector SHALL assert that strategy reads never access a bar with `ts > current_ts`. | Property test runs over all ref strategies. |
| FR-INT-005 | A survivorship-bias guard SHALL refuse strategy universes that exclude delisted symbols when the backtest horizon spans their delisting date. | Test with synthetic delisted symbol. |

### 4.4 Universe & Temporal Master (FR-UNI)

| ID | Requirement | Verification |
|----|-------------|--------------|
| FR-UNI-001 | The temporal master SHALL represent every distinct `groww_symbol` ever seen, with `[valid_from_date, valid_to_date)` ranges. | SQL test: symbol seen on day A and day C but not B yields two intervals. |
| FR-UNI-002 | The master SHALL track lot-size revisions, tick-size revisions, freeze-quantity revisions, and strike/expiry addition/removal. | Diff test across two snapshots. |
| FR-UNI-003 | An `as_of(date).filter(instrument_type='CE', underlying='NIFTY', expiry=date2)` API SHALL return the exact active option chain on that date. | Test against a recorded fixture. |
| FR-UNI-004 | Expired option contracts SHALL be enumerated via `groww.get_expiries(exchange, underlying_symbol, year)` for every year in `[2020, current_year]`, followed by `groww.get_contracts(exchange, underlying_symbol, expiry_date)` for every returned expiry. No symbol-probing fallback is needed. | Integration test against recorded fixture — see `docs/groww-sdk/backtesting.md §3–§4`. |

### 4.5 Indicators & Feature Engine (FR-IND)

| ID | Requirement | Verification |
|----|-------------|--------------|
| FR-IND-001 | The indicator library SHALL provide at minimum: SMA, EMA, WMA, HMA, DEMA, TEMA, RSI, Stochastic, MACD, Bollinger Bands, ATR, ADX, Supertrend, VWAP, Anchored VWAP, Keltner, Donchian, OBV, CMF, MFI, Ichimoku, PSAR, PivotPoints, CPR, OpeningRange, and RealizedVolatility. | Unit tests per indicator with golden values. |
| FR-IND-002 | Every indicator SHALL have a vectorised implementation (Numba/Polars) and SHALL be reachable from the Rust hot kernel via a stable FFI contract. | Cross-impl equivalence test. |
| FR-IND-003 | Indicators SHALL be timeframe-agnostic: a strategy requesting RSI(14) on `15m` triggers on-demand resampling from the base bar. | Test with 1m base → 15m request. |
| FR-IND-004 | Indicator results SHALL be cached by `(symbol, interval, range, indicator_id, params)` in LMDB; cache hit is O(1). | Bench test. |
| FR-IND-005 | Options-specific features (Black-Scholes / Black-76 Greeks, implied vol, moneyness, days-to-expiry, intrinsic/time value) SHALL be available as first-class indicators. | Test against QuantLib reference values within 1e-6 tolerance. |

### 4.6 Strategy Engine (FR-STR)

| ID | Requirement | Verification |
|----|-------------|--------------|
| FR-STR-001 | A strategy SHALL be expressible in **two modes**: (a) vectorised (signal array) for broad sweeps, (b) event-driven (state machine) for top-N deep checks. | Dual-mode base class + conformance test. |
| FR-STR-002 | Strategies SHALL declare their required `symbols`, `timeframes`, `params`, and `lookback_bars` statically so the engine can pre-slice data. | Test: missing declaration → registration error. |
| FR-STR-003 | Strategies SHALL be stateless except for an engine-managed `StrategyState` object, enabling deterministic replay. | Property test: replay(A) == replay(A). |
| FR-STR-004 | A reference library SHALL include at least: MA crossover, RSI mean-revert, Donchian breakout, Supertrend trend-follow, Opening-Range breakout, IronCondor, IronFly, CallSpread, PutSpread, Straddle, Strangle, CoveredCall, Calendar, Diagonal, and a walk-forward momentum ensemble. | Each strategy has an end-to-end test. |
| FR-STR-005 | A strategy SHALL expose an `expected_metrics` contract used by `tests/scenario/` to pin known-good outcomes on fixture data. | Scenario gauntlet test. |

### 4.7 Brute-Force Orchestrator (FR-BF)

| ID | Requirement | Verification |
|----|-------------|--------------|
| FR-BF-001 | The orchestrator SHALL support three sweep strategies: (a) full Cartesian grid, (b) Bayesian (Optuna), (c) Genetic. User-selectable per run. | Config test. |
| FR-BF-002 | A bitmap-prefilter SHALL eliminate combos whose signals are provably equivalent to already-evaluated combos via signal-mask hashing. | Test: 10% duplicate params yield zero duplicate compute. |
| FR-BF-003 | Every combo SHALL be identified by `combo_hash = sha256(strategy_id ‖ canonical_params ‖ data_snapshot_hash ‖ code_commit_sha)`. | Property test: no collision in 10⁷ random params. |
| FR-BF-004 | The orchestrator SHALL skip combos whose `combo_hash` is already in the LMDB result cache (O(1) dedup). | Bench: rerun finishes in < 1s. |
| FR-BF-005 | The orchestrator SHALL support both Walk-Forward and CPCV validation, per-run selectable, with the choice recorded in the run manifest. | Test each path. |
| FR-BF-006 | Every multi-trial run SHALL compute PBO (Probability of Backtest Overfitting) and Deflated Sharpe Ratio, attached to the run report. | Test against Lopez de Prado reference paper values. |
| FR-BF-007 | The orchestrator SHALL persist a run manifest containing: run_id, strategy_id, param grid, data_snapshot_hash, code_commit_sha, validation method, start_ts, end_ts, worker_count, cli_args, env. | Manifest round-trip test. |
| FR-BF-008 | The orchestrator SHALL support graceful cancel (`brutex kill <run_id>`) preserving partial results. | Signal test. |
| FR-BF-009 | The orchestrator SHALL auto-kill a combo that exceeds `p99 × 10` of historical per-combo runtime (hung-task detection). | Test with synthetic infinite loop. |
| FR-BF-010 | A regime segmenter SHALL tag each bar with a regime label (trend-up, trend-down, chop, high-vol, low-vol) and SHALL emit per-regime metrics for every run. | Regime test with synthetic data. |

### 4.8 Execution Simulator (FR-EXEC)

| ID | Requirement | Verification |
|----|-------------|--------------|
| FR-EXEC-001 | Fills SHALL be simulated with configurable slippage models: bar-VWAP ± bps, next-bar-open pessimistic, and square-root-impact. | Per-model test. |
| FR-EXEC-002 | The Indian transaction cost model SHALL apply on every fill: STT (0.1% delivery / 0.025% intraday-equity / 0.0125% FUT sell / 0.05% OPT sell on premium / 0.125% OPT exercise), stamp duty, exchange turnover charge, SEBI turnover fee, GST (18% on brokerage+exch+SEBI), DP charges (₹13.5+GST on delivery sell), user-configurable brokerage. | Golden test against Zerodha calculator values within ±1 paise. |
| FR-EXEC-003 | Circuit-filter rejection SHALL block fills outside configured bands (default 20%/10%/5%). | Test with out-of-band synthetic bar. |
| FR-EXEC-004 | Fills SHALL respect lot size: order quantities not a multiple of lot size SHALL be rejected with `InvalidLotSizeError`. | Unit test. |
| FR-EXEC-005 | Fills SHALL respect freeze quantity at the temporal-master value for the trade date. | Temporal test. |
| FR-EXEC-006 | An optional queue-position penalty SHALL apply when strategies use aggressive limit orders (proxy since no tick data). | Parameterised test. |

### 4.9 Risk / Portfolio Book (FR-RISK)

| ID | Requirement | Verification |
|----|-------------|--------------|
| FR-RISK-001 | A portfolio book SHALL track per-strategy and aggregate positions, cash, MTM, realized P&L, and unrealized P&L. | Round-trip test. |
| FR-RISK-002 | A SPAN+Exposure margin engine SHALL compute F&O portfolio margin using NSE SPAN parameter files (daily download). | Margin test against NSE examples. |
| FR-RISK-003 | MTM-margin-call / liquidation SHALL be simulated: when equity < maintenance margin, the engine forces liquidation at next-bar open with penalty. | Synthetic drawdown test. |
| FR-RISK-004 | Drawdown guards SHALL halt a strategy when max-DD exceeds a configured threshold. | Halt test. |
| FR-RISK-005 | Kelly and fractional-Kelly position-sizing SHALL be available as a sizing plug-in. | Unit test. |
| FR-RISK-006 | Value-at-Risk (historical and parametric) SHALL be reported per run. | VaR test on fixture. |

### 4.10 Forward-Testing / Replay (FR-FWD)

| ID | Requirement | Verification |
|----|-------------|--------------|
| FR-FWD-001 | A `ReplayFeed` SHALL stream bars from the data lake in chronological order with wall-clock pacing or accelerated pacing. | Unit test. |
| FR-FWD-002 | The same strategy code SHALL run unchanged against both `ReplayFeed` (backtest) and a future `LiveFeed` (v1.2+ paper mode). | Interface conformance test. |
| FR-FWD-003 | A drift-alerter SHALL compare live P&L to backtest expectation and halt at > Nσ divergence. (Deferred to v1.2 when LiveFeed arrives, interface defined now.) | Stub test. |

### 4.11 UI / CLI / API (FR-UI)

| ID | Requirement | Verification |
|----|-------------|--------------|
| FR-UI-001 | A `brutex` CLI SHALL expose commands: `ingest`, `catalog`, `run`, `status`, `kill`, `audit`, `report`. | CLI test per command. |
| FR-UI-002 | `brutex status <run_id>` SHALL return within 100 ms with `{state, combos_done, throughput, workers_alive, errors, eta}`. | Latency test. |
| FR-UI-003 | A FastAPI HTTP server SHALL expose the same operations as the CLI under `/api/v1/`. | Endpoint contract test. |
| FR-UI-004 | A web dashboard SHALL provide drill-down views: per-combo equity curve, trade blotter, margin timeline, drawdown chart, per-regime heatmap, param-sensitivity surface. | Snapshot test (React). |
| FR-UI-005 | Jupyter kernel integration SHALL allow notebook-based research over the data lake. | Notebook smoke test. |

---

## 5. Non-Functional Requirements (NFR)

Every NFR has an **enforcement mechanism**. If there is no test that can fail the build, the NFR is not enforced. No exceptions.

### 5.1 Performance NFRs (the load-bearing ones)

| ID | Statement | Enforcement |
|----|-----------|-------------|
| **NFR-PERF-001** | On the reference runner (16 physical cores, 64 GB RAM, NVMe, Linux x86_64), for a strategy whose per-bar cost ≤ 500 ns and whose parameter space fits the bitmap-prefilter model, BruteX SHALL complete **1,000,000 combinations over 1 year of 1-minute NIFTY bars in ≤ 300 seconds**. | CI test `tests/bench/test_bench_1m_combos.py` — fails build on regression. Green zone target ≤ 150s; hard limit ≤ 300s. |
| **NFR-PERF-002** | Bar lookup by `(symbol, interval, timestamp)` from the warm cache SHALL execute in **O(1)** with median ≤ 500 ns and p99 ≤ 2 µs. | `tests/bench/test_bar_lookup_o1.py` — N=10⁶ lookups, asserts median and p99 bounds. |
| **NFR-PERF-003** | Combo result-cache lookup SHALL be **O(1)** in cache size: ≤ 10% slowdown at 10⁶ entries vs 10³ entries. | `tests/bench/test_dedup_o1.py`. |
| NFR-PERF-004 | Indicator cache hit (vectorised Numba path) SHALL retrieve cached series in ≤ 1 µs per 1000 bars. | `tests/bench/test_indicator_cache.py`. |
| NFR-PERF-005 | First-time 1 year NIFTY 1m ingest (warm disk) SHALL complete in ≤ 30 seconds excluding Groww API time. | Ingest bench test. |
| NFR-PERF-006 | Strategy cold-start (JIT compile + first bar) SHALL be ≤ 500 ms for any library strategy. | Cold-start bench. |
| NFR-PERF-007 | Arrow/mmap shared-memory feed SHALL incur **zero copies** across Ray workers. | Memory-allocator test using `tracemalloc`. |
| NFR-PERF-008 | Polars group-by of 1-year 1m bars SHALL complete in ≤ 50 ms. | Bench test. |

### 5.2 Scalability NFRs

| ID | Statement | Enforcement |
|----|-----------|-------------|
| NFR-SCAL-001 | The system SHALL scale linearly in combo count up to the reference runner's core saturation (16 workers). | Strong-scaling test: 2×, 4×, 8×, 16× workers. |
| NFR-SCAL-002 | The system SHALL support horizontal scaling to a Ray cluster (deferred v2.1) without strategy code changes. | Interface test. |
| NFR-SCAL-003 | The data lake SHALL serve ≥ 10,000 distinct symbol queries per second from the warm Redis cache. | Cache bench. |

### 5.3 Reliability NFRs

| ID | Statement | Enforcement |
|----|-----------|-------------|
| NFR-REL-001 | Every ingest chunk SHALL be idempotent (rerun-safe). | Property test: `ingest(x); ingest(x)` → same state. |
| NFR-REL-002 | Every combo run SHALL be idempotent (rerun-safe, bit-identical outputs). | Property test: `run(x); run(x)` → byte-equal results. |
| NFR-REL-003 | Process crash mid-run SHALL leave the catalog, cache, and audit chain in a consistent state. | Chaos test: SIGKILL at random points. |
| NFR-REL-004 | Every run manifest SHALL be durable before any worker starts executing combos. | WAL test. |
| NFR-REL-005 | No background worker SHALL be allowed to run silently: absence of heartbeat > 5s triggers auto-kill and requeue. | Watchdog integration test. |

### 5.4 Observability NFRs

| ID | Statement | Enforcement |
|----|-----------|-------------|
| **NFR-OBS-001** | Worker heartbeat period ≤ 1 s. Watchdog SHALL kill workers with heartbeat age > 5 s. | `tests/integration/test_watchdog_kills_hung_worker.py`. |
| **NFR-OBS-002** | `brutex status` SHALL return within 100 ms and show `combos_done`, `throughput`, `workers_alive`, `errors`, `eta`. No run SHALL be in a state where these values are unknown. | CLI latency test. |
| NFR-OBS-003 | Every material action (ingest chunk, combo start/end/error, kill) SHALL append one line to a hash-chained JSONL audit log. | `brutex audit verify` must pass end-of-run. |
| NFR-OBS-004 | Prometheus metrics SHALL include: `brutex_combos_total`, `brutex_combos_per_second`, `brutex_workers_alive`, `brutex_worker_heartbeat_age_seconds`, `brutex_combo_duration_seconds`, `brutex_queue_depth`, `brutex_errors_total{type}`, `brutex_cache_hit_ratio`. | Metrics endpoint test. |
| NFR-OBS-005 | OpenTelemetry traces SHALL span: ingest → catalog → feed → indicator → strategy → exec → risk → report. | Trace assertion test. |
| NFR-OBS-006 | Grafana alerts SHALL fire on: `worker_heartbeat_age > 5s`, `combos_per_second == 0 for 30s`, `rate_limit_near_miss > 0`, `data_gap_detected`, `nan_in_pipeline`, `audit_chain_broken`. | Alert rule lint test. |
| NFR-OBS-007 | Structured logs (structlog) SHALL include `run_id`, `combo_hash`, `symbol`, `interval` on every relevant line. | Log schema test. |

### 5.5 Uniqueness & Determinism NFRs

| ID | Statement | Enforcement |
|----|-----------|-------------|
| **NFR-UNIQ-001** | Every combo identity is `sha256(strategy_id ‖ canonical_params ‖ data_snapshot_hash ‖ code_commit_sha)`. Collision probability ≤ 2⁻¹²⁸. | `tests/property/test_hash_no_collision.py` — 10⁷ random param sets, zero collisions asserted. |
| NFR-UNIQ-002 | Canonical params SHALL be serialised via a total-order canonical form (sorted keys, stable float repr, no NaN keys). | Canonicalisation round-trip test. |
| NFR-DET-001 | `PYTHONHASHSEED=0` and all RNGs SHALL accept an explicit seed; no seedless randomness in the hot path. | Grep test forbids `random.random()` without seed. |
| NFR-DET-002 | Same inputs SHALL produce byte-identical outputs across two runs on the same code_commit_sha. | Golden-master test. |

### 5.6 Security NFRs

| ID | Statement | Enforcement |
|----|-----------|-------------|
| NFR-SEC-001 | Groww auth tokens SHALL live only in environment variables or OS keyring; never in files or logs. | Secret-scanning test (bandit + custom grep). |
| NFR-SEC-002 | Workers SHALL run with seccomp profile, no-new-privs, and an egress allowlist restricted to `*.groww.in` (ingest nodes only) and internal services. | Dockerfile audit. |
| NFR-SEC-003 | Dependencies SHALL be scanned by `pip-audit` and `safety` in CI; critical CVEs block merge. | CI step. |
| NFR-SEC-004 | Static security analysis SHALL use `bandit` with zero high-severity findings. | CI step. |
| NFR-SEC-005 | Lockfiles SHALL be generated by `uv` or `poetry` with hashes pinned. | CI checks hash presence. |

### 5.7 Portability NFRs

| ID | Statement | Enforcement |
|----|-----------|-------------|
| NFR-PORT-001 | Python 3.12 is the supported runtime. Rust 1.78+ for the hot kernel. | CI matrix. |
| NFR-PORT-002 | BruteX SHALL run on Linux x86_64 primary; macOS ARM as a secondary dev target (no SLA). | CI secondary matrix. |
| NFR-PORT-003 | Numba fallback path SHALL be exercised in CI so the system works on machines without the Rust extension. | Fallback bench test. |

---

## 6. Data Model

### 6.1 Canonical Bar

```
bar {
  symbol:          string            # groww_symbol
  exchange:        enum              # NSE, BSE
  segment:         enum              # CASH, FNO
  instrument_type: enum              # EQ, IDX, FUT, CE, PE (Groww SDK — see docs/groww-sdk/annexures.md)
  interval:        enum              # 1m, 2m, 3m, 5m, 10m, 15m, 30m, 1h, 4h, 1d, 1w, 1mo (12 native Groww intervals)
  ts_open_utc:     int64             # microseconds since epoch (bar open)
  ts_close_utc:    int64             # exclusive close
  open:            float64
  high:            float64
  low:             float64
  close:           float64
  volume:          int64
  oi:              int64 | null      # open interest (F&O only)
  trades:          int32 | null
  vwap:            float64 | null
  adjusted:        bool              # corp-action adjusted?
  source:          string            # "groww:historical/candle/range"
  ingest_ts:       int64
}
```

### 6.2 Temporal Instrument Master

```
instruments_temporal {
  groww_symbol:          string      PK part
  valid_from_date:       date        PK part
  valid_to_date:         date
  exchange:              enum
  segment:               enum
  instrument_type:       enum
  trading_symbol:        string
  name:                  string
  underlying_symbol:     string | null
  expiry_date:           date   | null
  strike_price:          float  | null
  lot_size:              int
  tick_size:             float
  freeze_quantity:       int
  series:                string | null
  isin:                  string | null
  buy_allowed:           bool
  sell_allowed:          bool
  is_intraday:           bool
  first_seen_date:       date
  last_seen_date:        date
  snapshot_hashes:       list<sha256>  -- every daily snapshot that contained this row
}
```

### 6.3 Run Manifest

```
run_manifest {
  run_id:             uuid
  run_label:          string
  strategy_id:        string
  strategy_version:   string
  code_commit_sha:    string
  data_snapshot_hash: sha256
  param_grid:         json            # canonicalised
  sweep_method:       enum            # grid | optuna | genetic
  validation_method:  enum            # walk_forward | cpcv | both
  universe:           list<symbol>
  horizon:            { start, end }
  interval:           string
  worker_count:       int
  rng_seed:           int
  cli_args:           list<string>
  env_snapshot:       map<string,string>
  started_at:         timestamp
  ended_at:           timestamp | null
  state:              enum            # queued | running | done | failed | killed
  combos_total:       int
  combos_done:        int
  combos_skipped:     int
  errors_total:       int
  manifest_sha256:    sha256          # self-hash for tamper-evidence
}
```

### 6.4 Combo Result

```
combo_result {
  combo_hash:    sha256   PK   # = sha256(strategy ‖ params ‖ data_snapshot ‖ code_sha)
  run_id:        uuid
  strategy_id:   string
  params:        json
  metrics: {
    gross_pnl, net_pnl, sharpe, sortino, calmar,
    max_dd, max_dd_duration, cagr, volatility,
    hit_ratio, profit_factor, avg_win, avg_loss,
    trades_count, turnover, exposure_pct,
    deflated_sharpe,       -- multi-trial corrected
    pbo_score,             -- prob of backtest overfit
    per_regime_metrics: {trend_up: ..., trend_down: ..., chop: ..., high_vol: ..., low_vol: ...}
  }
  trade_blotter_uri: string       -- path to parquet
  equity_curve_uri:  string
  elapsed_ms:    int
  worker_id:     string
  computed_at:   timestamp
}
```

### 6.5 Audit Log Entry (hash-chained)

```
audit_entry {
  seq:            int64           # monotonic
  prev_hash:      sha256          # hash of previous entry
  ts_utc:         int64
  actor:          string          # process/user
  action:         enum            # INGEST_CHUNK_START, INGEST_CHUNK_DONE, COMBO_START, COMBO_DONE, COMBO_ERROR, RUN_KILL, DATA_GATE_FAIL, ...
  subject:        string          # e.g., combo_hash, symbol:interval:range
  payload:        json            # action-specific
  entry_hash:     sha256          # sha256(seq ‖ prev_hash ‖ ts_utc ‖ actor ‖ action ‖ subject ‖ canonical(payload))
}
```

`brutex audit verify` recomputes every `entry_hash` and asserts the chain.

---

## 7. Groww Adapter Contract

The Groww adapter is the **only** component allowed to call Groww. Everything else reads from the data lake.

### 7.1 Interface

```python
class GrowwAdapter:
    def pull_instruments_snapshot(self, *, as_of_date: date | None = None) -> SnapshotHandle:
        """Fetch today's instrument CSV, store content-addressed, return handle."""

    def diff_instruments(self, day_a: date, day_b: date) -> InstrumentDiff:
        """Compute added/removed/changed rows between two snapshots."""

    def pull_candles(
        self,
        exchange: Exchange,
        segment: Segment,
        groww_symbol: str,
        interval: CandleInterval,
        start: datetime,
        end: datetime,
    ) -> CandlePullReport:
        """
        Chunked, idempotent, rate-limited candle fetch.
        Returns the set of chunks written + skipped, plus any integrity flags.
        """

    def enumerate_candle_intervals(self) -> frozenset[CandleInterval]:
        """Reflect the SDK's CANDLE_INTERVAL_* constants. Golden-mastered."""

    def health_check(self) -> HealthReport:
        """Probe auth + single-bar pull; never in hot path."""
```

### 7.2 Behavioural Contract

- Token bucket at `BRUTEX_OHLC_RATE_LIMIT_RPS` (default 15) refills at the configured rate with burst = 5.
- On 429: back off 2s → 4s → 8s → 16s with ±20% jitter; after 4 failures escalate to `RateLimitExhausted`.
- On 5xx: same backoff ladder.
- On 4xx (non-429): fail fast, log, mark chunk `permanently_failed`.
- On connection reset / DNS error: treat as 5xx.
- Max bars per call is probed on first run per interval, stored in `adapter_config.max_bars_per_call[interval]`.
- Every call's raw response is persisted to `raw/groww/<sha256>.json.zst` **before** parsing.
- Parse failure leaves raw blob + emits `ParseFailure` audit entry; does not delete raw blob.

### 7.3 Auth Modes

| Mode | Used for | Token required? |
|------|----------|-----------------|
| `public_csv` | Instrument master CSV | No |
| `token_auth` | Historical candles | Yes (`GROWW_API_AUTH_TOKEN`) |

Both modes coexist at runtime. Each method declares the mode it uses; the adapter picks credentials accordingly.

---

## 8. Ingest Profile & Backfill Strategy

### 8.1 Scale Math (honest)

| Bucket | Symbols (est.) | 1m calls per symbol | Total 1m calls |
|--------|---------------:|--------------------:|---------------:|
| NSE equity cash | 2,000 | ~400 | ~800,000 |
| Indices | ~30 | ~400 | ~12,000 |
| Stock futures (2020→today union) | ~36,000 | ~30 | ~1,080,000 |
| Index futures (union) | ~1,100 | ~30 | ~33,000 |
| Stock options (union) | ~720,000 | ~15 | ~10,800,000 |
| Index options (union) | ~250,000 | ~15 | ~3,750,000 |
| **Total 1m** | **~1,000,000** | — | **~16,500,000 calls** |

Wall-clock at 15 req/s effective: 16.5M / 15 ≈ **12.7 days continuous**.

### 8.2 Backfill Profile (locked)

- **Phase A (fast, unblocking):** Daily bars for full universe first. Completes in hours, not days. Unlocks daily-bar backtests immediately.
- **Phase B (background):** 1-minute bars, streamed as a low-priority background job. Progress visible via `brutex status --ingest`.
- **Phase C (opportunistic):** 5m / 15m / 60m resampled on-demand from 1m once Phase B covers the requested slice; else they can be fetched directly if the resample coverage is < 100%.

### 8.3 Historical Expiry Enumeration (revised from earlier draft)

> **Revised 2026-04-14.** Earlier drafts proposed a "deterministic symbol
> probe" fallback to recover pre-snapshot expired options. **That approach
> is now deleted** — it was a workaround for an assumed SDK limitation
> that does not exist. The Groww backtesting SDK exposes `get_expiries`
> and `get_contracts` methods that enumerate historical derivatives
> natively from 2020 onwards.

The authoritative enumeration algorithm for v1 F&O indices:

```
for exchange, underlying in V1_UNDERLYINGS:
    for year in 2020..current_year:
        expiries = groww.get_expiries(
            exchange=exchange,
            underlying_symbol=underlying,
            year=year,
        )["expiries"]
        for expiry in expiries:
            contracts = groww.get_contracts(
                exchange=exchange,
                underlying_symbol=underlying,
                expiry_date=expiry,
            )["contracts"]
            persist_instrument_rows(contracts)
            for contract in contracts:
                for chunk_start, chunk_end in chunk_by_max_duration(...):
                    candles = groww.get_historical_candles(
                        exchange=exchange,
                        segment=groww.SEGMENT_FNO,
                        groww_symbol=contract,
                        start_time=chunk_start,
                        end_time=chunk_end,
                        candle_interval=interval,
                    )
                    persist_candles(contract, interval, candles)
```

Full reference: `docs/groww-sdk/backtesting.md §3–§5, §10`.

### 8.4 Revised Call Budget — v1 F&O indices universe (2020 → today)

Based on the **real** per-request time limits documented in
`docs/groww-sdk/backtesting.md §6` (30 / 90 / 180 days by interval group),
NOT the earlier bar-count guess:

| Component | Calls |
|---|---:|
| `get_expiries` (8 underlyings × 5 years) | ~40 |
| `get_contracts` (~2,500 distinct historical expiries) | ~2,500 |
| Spot indices (8 × 60 × 1m) | 480 |
| Index futures (~1,440 contracts × ~3 chunks each) | ~4,320 |
| Index options (~207,000 contracts × ~1 chunk each — each option lives < 30 days) | ~207,000 |
| **Total calls for 1-minute full v1 universe** | **~214,340** |

At 15 req/s effective (BruteX safety cap, `FR-ING-007`):

```
214,340 / 15 ≈ 14,290 seconds ≈ 3.97 hours
```

**v1 full 1-minute ingest from scratch completes in ~4 hours on a single operator.**

For each additional interval (5m, 15m, 1h, 1d), add another ~4 hours.

| Passes | Wall-clock |
|---|---|
| 1m only | ~4 hours (one morning) |
| 1m + 5m + 15m + 1h + 1d | ~20 hours (overnight + morning) |
| All 12 native intervals | ~48 hours (one weekend) |

This is a **95% reduction** from my earlier (wrong) estimate of 12.7 days
for the all-NSE-equities plan, and a ~75% reduction from the intermediate
~16-hour estimate that used a conservative 1000-bars-per-call guess.

---

## 9. Integrity Gates

Before any slice is usable by strategies, it must pass **all** of these gates:

| Gate | Check | Failure action |
|------|-------|----------------|
| G-GAP | Expected bar count vs actual, per session | Mark slice `GAP_DETECTED`, block brute-force consumption |
| G-DUP | Duplicate `ts_open_utc` | Reject slice; preserve raw |
| G-MONO | Timestamps strictly increasing | Reject slice |
| G-NAN | No NaN in O/H/L/C/V | Reject slice |
| G-NEG | No negative prices; volume ≥ 0 | Reject slice |
| G-ZERO-VOL | Zero-volume bars in known-liquid symbols flagged | Warn, do not block |
| G-SESSION | Bars within `09:15–15:30 IST` for NSE equities | Reject out-of-session bars |
| G-ADJUST | Corp-action adjustment applied if event in range | Re-adjust on-the-fly |
| G-LOT | Lot-size consistent with temporal master on that date | Flag |
| G-FREEZE | Freeze-qty consistent with temporal master | Flag |
| G-LOOKAHEAD | Strategy reads never access `ts > current_ts` | Assert in engine, raise `LookAheadError` |
| G-SURVIVOR | Delisted symbols included in universes that span their delisting | Warn, require explicit override |

`brutex catalog gate <slice>` runs all gates and emits a signed report.

---

## 10. Architecture Layers (L1–L7)

```
┌──────────────────────────────────────────────────────────────┐
│ L7  UI / CLI / API                                           │
│     FastAPI • React dashboard • CLI • Jupyter kernel         │
├──────────────────────────────────────────────────────────────┤
│ L6  Brute-Force Orchestrator                                 │
│     param-sweep DAG • Optuna • genetic • walk-forward • CPCV │
│     PBO • Deflated Sharpe • bitmap prefilter • LMDB dedup    │
├──────────────────────────────────────────────────────────────┤
│ L5  Strategy Engine Core                                     │
│     vectorised path + event-driven path (dual-mode)          │
│     Rust/PyO3 hot kernel • Numba fallback • indicator cache  │
├──────────────────────────────────────────────────────────────┤
│ L4  Execution Simulator   │   Risk / Portfolio Book          │
│     slippage • impact     │   SPAN+Exposure • MTM • Kelly    │
│     Indian txn costs      │   VaR • drawdown guards          │
├──────────────────────────────────────────────────────────────┤
│ L3  Virtual Market Feed                                      │
│     Arrow/mmap zero-copy • O(1) bar lookup                   │
│     on-demand resampler • corp-action • continuous futures   │
│     session calendar • look-ahead guard                      │
├──────────────────────────────────────────────────────────────┤
│ L2  Data Lake (canonical store)                              │
│     Parquet + DuckDB + Redis hot + LMDB result cache         │
│     content-addressed • partition-pruned • temporal master   │
├──────────────────────────────────────────────────────────────┤
│ L1  Ingestion / Broker Adapter (pluggable)                   │
│     GrowwAdapter (v1) • IBrokerDataSource interface          │
│     token bucket • backoff • idempotent • integrity pass     │
└──────────────────────────────────────────────────────────────┘
```

### 10.1 Layer Responsibilities

- **L1 GrowwAdapter:** only component that talks to Groww. All rate-limit logic lives here. Writes to L2.
- **L2 Data Lake:** canonical storage. Parquet for cold, DuckDB for query, Redis for hot bars, LMDB for result cache. All content-addressed.
- **L3 Virtual Feed:** reads L2, materialises an in-memory Arrow view shared across Ray workers via mmap. Owns the `as_of(ts)` contract and the look-ahead guard.
- **L4 Exec+Risk:** pure functions given (bar, order, book) → (fill, updated_book). No I/O.
- **L5 Strategy Engine:** dual-mode (vectorised + event-driven). Hot kernel is Rust; Python/Numba is fallback. Indicator cache is transparent.
- **L6 Orchestrator:** DAGs the sweep, manages Ray actors, applies CPCV / walk-forward, computes PBO / Deflated Sharpe, writes results to LMDB.
- **L7 UI/CLI/API:** thin clients. No business logic. Read-only from L2/L6 state.

### 10.2 Dependency Rule

**Layers depend downward only.** L5 can read L3; L3 cannot know about L5. Enforced by an `import-linter` contract in CI.

---

## 11. Brute-Force Orchestrator Design

### 11.1 Two-Phase Funnel

1. **Phase 1 — vectorised broad sweep.**
   All combos run through the Rust/Numba vectorised path. Cheap (sub-ms per combo for simple strategies). Produces preliminary metrics.
2. **Phase 2 — event-driven deep check.**
   Top-N survivors (default top 0.5%) re-run under the event-driven engine with realistic fills, partial fills, slippage, margin calls. Produces final metrics.

This funnel is **mandatory** — it's how we hit the 1M/5min SLA without lying about fidelity.

### 11.2 Sweep Methods

| Method | When | Implementation |
|--------|------|----------------|
| Full Cartesian grid | Small-dim, exhaustive needed | `itertools.product` + Ray map |
| Bayesian (TPE/CMA-ES) | High-dim, cost-sensitive | Optuna |
| Genetic | Discrete + non-convex | DEAP or custom |
| Random | Baseline sanity | NumPy seeded |

### 11.3 Validation Methods (per FR-BF-005)

| Method | Purpose | Parameters |
|--------|---------|------------|
| Walk-Forward | Anchored or rolling train/test windows | `train_len`, `test_len`, `step`, `mode` |
| CPCV (Combinatorial Purged Cross-Validation) | Lopez de Prado — kills overfit on time-series | `N`, `k`, `embargo_bars`, `purge_bars` |

Both methods record their splits in the run manifest so results are reproducible.

### 11.4 Overfit Metrics (always emitted)

- **PBO (Probability of Backtest Overfitting)** — Lopez de Prado 2014.
- **Deflated Sharpe Ratio** — Bailey & Lopez de Prado 2014, adjusts Sharpe for multiple testing.
- **Haircut Sharpe Ratio** — Harvey & Liu 2015, multiple-testing correction.

Any run where `deflated_sharpe < 0` for the top combo emits a prominent `OVERFIT_WARNING` in the report.

### 11.5 Bitmap Prefilter

Pre-computed signal bitmaps per bar: each bar is a bit in a 64-bit word. A combo's entry/exit signal is a bitwise expression over these words. Two combos whose signal bitmaps hash to the same value are **provably equivalent**, and one can be skipped.

This cuts the effective combo count by 10–40% on typical grids for free.

### 11.6 Dedup Cache (LMDB)

- Key: `combo_hash` (32 bytes)
- Value: serialised `combo_result` (mean ~2 KB)
- O(1) lookup by B-tree page cache.
- `test_dedup_o1.py` asserts flat lookup time across 10³ → 10⁶ entries.

### 11.7 Supervisor & Watchdog

- One supervisor Ray actor per run.
- Heartbeats every 1 s from each worker to the supervisor.
- Supervisor kills workers with heartbeat age > 5 s; requeues their combos.
- Hung-task detection: if a combo exceeds `p99 × 10` of historical runtime, the supervisor kills the worker and requeues the combo.
- Run wall-clock budget = `2 × ETA`; exceeded → supervisor kills the run and writes partial results.

### 11.8 CLI

```
brutex ingest instruments
brutex ingest candles --universe nifty50 --interval 1m --start 2024-01-01 --end 2024-12-31
brutex run <strategy_id> --params grid.yaml --validation cpcv --workers 16
brutex status <run_id>
brutex kill <run_id>
brutex audit tail -f
brutex audit verify
brutex report <run_id> --format html|json|parquet
```

---

## 12. Execution Simulator (Indian Cost Model)

### 12.1 Slippage Models

| Model | Formula | Notes |
|-------|---------|-------|
| `bar_vwap_bps` | `fill_px = vwap ± bps` | Default |
| `next_bar_open_pessimistic` | `fill_px = next_bar.open ± bps` | Conservative |
| `sqrt_impact` | `fill_px = mid + k·σ·√(order_qty / adv)` | Size-dependent |

### 12.2 Indian Transaction Costs

Applied on **every** fill. Formulas below reflect SEBI/Exchange schedules as of 2025; constants live in a versioned config so regulatory changes don't silently break backtests.

| Charge | Rate | Applies to |
|--------|------|------------|
| STT delivery equity | 0.1% on buy+sell | Delivery |
| STT intraday equity | 0.025% on sell | Intraday |
| STT futures | 0.0125% on sell | F&O FUT |
| STT options | 0.05% on sell (on premium) | F&O OPT |
| STT option exercise | 0.125% on intrinsic | OPT exercise |
| Exchange txn charge | ~0.00297% (NSE equity), 0.00173% (NSE FUT), 0.03503% (NSE OPT on premium) | — |
| SEBI turnover fee | ₹10 per crore (0.0001%) | All |
| Stamp duty | 0.015% (delivery), 0.003% (intraday), 0.002% (FUT), 0.003% (OPT) | Buy-side only |
| GST | 18% on (brokerage + exchange + SEBI) | All |
| DP charges | ₹13.5 + GST per ISIN per day on delivery sell | Delivery sell |
| Brokerage | User-configurable (default: ₹20 flat per executed order, capped) | All |

A golden test `tests/golden/test_indian_costs.py` pins computed costs to known-good values cross-checked against Zerodha/Groww calculators within ±₹0.01.

### 12.3 Match Engine

- Time-priority at bar granularity (no tick queue, acknowledged).
- Partial fills allowed when order size > configured `max_fill_per_bar(volume)`.
- Circuit-filter rejection on out-of-band fills.
- Lot-size and freeze-quantity validation.

---

## 13. Risk / Portfolio Book

### 13.1 SPAN Margin Engine

- Consumes NSE SPAN parameter files (daily download, out-of-band dependency).
- Computes `SPAN_margin` for a portfolio of F&O legs.
- Adds `Exposure_margin` per SEBI schedule.
- Portfolio margin benefit applied for hedges (calendar, vertical, iron condor, etc.).
- Golden tests against NSE's own worked examples.

### 13.2 MTM & Liquidation

- EOD MTM per position.
- Intraday MTM every bar.
- When `equity < maintenance_margin` → forced liquidation at next-bar open with penalty slippage.
- Audit log entry `FORCED_LIQUIDATION` with reason.

### 13.3 Guards

| Guard | Action |
|-------|--------|
| Max-drawdown | Halt strategy on breach |
| Max-position-count | Reject new orders |
| Max-notional | Reject new orders |
| Max-single-name-concentration | Reject new orders |
| Circuit-rejection | Reject fills outside bands |

### 13.4 Position Sizing Plug-ins

- Fixed notional
- Fixed fractional
- Kelly
- Fractional Kelly
- Volatility-targeting
- Risk-parity (portfolio level)

---

## 14. Performance Engineering & O(1) Proofs

### 14.1 Latency Budget per Combo

| Complexity | Target budget | Impl |
|------------|--------------:|------|
| Simple (1 indicator, 1 threshold) | 0.3 ms | Rust SIMD |
| Moderate (3 indicators, stop/target) | 1.0 ms | Rust SIMD |
| Complex (multi-leg options, Greeks) | 10 ms | Rust + QuantLib FFI |
| Event-driven deep check | 100 ms | Python+Numba |

### 14.2 Throughput Math (for NFR-PERF-001)

`1,000,000 combos / 300 s = 3,333 combos/s` required.
On 16 cores → 208 combos/s per worker.
At 1 ms/combo (Rust), one worker does 1,000/s → 5× headroom. ✅

### 14.3 O(1) Proofs (test file per proof)

| Claim | Proof test | Assertion |
|-------|-----------|-----------|
| Bar lookup O(1) | `tests/bench/test_bar_lookup_o1.py` | median ≤ 500 ns; p99 ≤ 2 µs; N=10⁶ |
| Combo dedup O(1) | `tests/bench/test_dedup_o1.py` | t(10⁶) ≤ 1.1 × t(10³) |
| Indicator cache O(1) | `tests/bench/test_indicator_cache_o1.py` | median ≤ 1 µs |
| Result cache O(1) | `tests/bench/test_result_cache_o1.py` | t(10⁶) ≤ 1.1 × t(10³) |
| 1M combos SLA | `tests/bench/test_bench_1m_combos.py` | wall-clock ≤ 300 s |

### 14.4 Techniques in Use

| Technique | Benefit |
|-----------|---------|
| mmap + Arrow IPC | Zero-copy across Ray workers |
| Polars instead of Pandas | 5–30× faster group-by |
| Numba AOT | 50–200× vs pure Python |
| Rust/PyO3 hot kernel | 3–5× vs Numba |
| Bitmap signal masks | 10–40% combo elimination |
| Pre-sorted ts indexes | No binary search in hot path |
| LMDB result cache | O(1) skip on rerun |
| Content-addressed dedup | Idempotence + reproducibility |

---

## 15. Observability (Monitoring / Logging / Alerting)

### 15.1 Metrics (Prometheus)

| Metric | Type | Purpose |
|--------|------|---------|
| `brutex_combos_total` | counter | total combos processed |
| `brutex_combos_per_second` | gauge | live throughput |
| `brutex_workers_alive` | gauge | live worker count |
| `brutex_worker_heartbeat_age_seconds` | histogram | liveness distribution |
| `brutex_combo_duration_seconds` | histogram | per-combo latency |
| `brutex_queue_depth` | gauge | pending combos |
| `brutex_errors_total{type}` | counter | error taxonomy |
| `brutex_cache_hit_ratio` | gauge | LMDB hit ratio |
| `brutex_ingest_rate_limit_near_misses_total` | counter | rate-limit pressure |
| `brutex_audit_chain_broken` | gauge | 0/1 alarm |

### 15.2 Tracing (OpenTelemetry)

Spans: `ingest.chunk → catalog.write → feed.materialise → indicator.compute → strategy.evaluate → exec.fill → risk.update → report.write`.

### 15.3 Logging (structlog)

- JSON structured, no `print` anywhere (enforced by lint).
- Mandatory fields: `run_id`, `combo_hash`, `symbol`, `interval`, `component`, `level`.
- Log level gated by `BRUTEX_LOG_LEVEL` env.
- PII filter (no tokens, no auth headers, no user-specific paths).

### 15.4 Alerts (Grafana)

| Alert | Condition | Severity |
|-------|-----------|----------|
| WorkerHeartbeatStale | `max(heartbeat_age) > 5s` | critical |
| ThroughputStalled | `combos_per_second == 0 for 30s` | critical |
| RateLimitNearMiss | `rate(near_misses) > 0` | warn |
| DataGapDetected | `data_gate_fail{gate="G-GAP"} > 0` | warn |
| NaNInPipeline | `data_gate_fail{gate="G-NAN"} > 0` | critical |
| AuditChainBroken | `audit_chain_broken == 1` | critical |
| MemoryHigh | `rss_bytes > 90% limit` | warn |
| DiskHigh | `disk_used > 85%` | warn |

### 15.5 Audit Chain

- Hash-chained JSONL (`data/audit/YYYY-MM-DD.jsonl`).
- Every entry's `entry_hash` is `sha256(seq ‖ prev_hash ‖ ts ‖ actor ‖ action ‖ subject ‖ payload)`.
- `brutex audit verify` recomputes the chain; any mismatch exits non-zero.
- CI integration test re-verifies after every pipeline run.

---

## 16. Security & Security Hardening

| Control | Mechanism |
|---------|-----------|
| Secrets | Env vars + OS keyring; never in files or logs |
| Token scope | Data-only (no orders); enforced by the chosen auth mode |
| Dependency scanning | `pip-audit`, `safety`, `cargo audit` in CI |
| Static analysis | `bandit` (python), `cargo-clippy -D warnings`, `semgrep` rules |
| SBOM | Generated per release via `cyclonedx-python` |
| Container | Distroless base, non-root, read-only root-fs, no-new-privs |
| Seccomp | Restrictive profile; deny `ptrace`, `mount`, `kexec`, `bpf` |
| Egress allowlist | Ingest nodes: `*.groww.in`; worker nodes: internal only |
| Lockfiles | `uv.lock` with hashes; CI refuses unpinned deps |
| Git signing | `gpgsign = true`; tags signed; hooks never bypassed |
| Input validation | Pydantic v2 on all CLI / API / config inputs |
| Path safety | No string-concatenated paths; `pathlib.Path` only; `resolve().is_relative_to(root)` check on every user-supplied path |

---

## 17. 100% Coverage Gates (with mechanical proof)

Each claim has a **pass/fail mechanism**. No test → no guarantee.

| # | Claim | Mechanism | Pass threshold |
|---|-------|-----------|----------------|
| CG-01 | 100% line + branch coverage | `pytest --cov=brutex --cov-branch --cov-fail-under=100` | 100.00% |
| CG-02 | Mutation score | `mutmut run && mutmut results` | ≥ 85% killed |
| CG-03 | Type coverage | `mypy --strict brutex` + `pyright` | 0 errors |
| CG-04 | Lint cleanliness | `ruff check brutex` + `pylint -E brutex` | 0 warnings |
| CG-05 | Security scan | `bandit -r brutex` + `pip-audit` | 0 high / 0 critical |
| CG-06 | Performance regression | `pytest-benchmark compare --fail-on-regression` | ≤ 5% slowdown vs baseline |
| CG-07 | SLA bench | `test_bench_1m_combos.py` | ≤ 300 s wall clock |
| CG-08 | O(1) proofs | All `test_*_o1.py` | assertions hold |
| CG-09 | Audit-chain integrity | `brutex audit verify` in CI | chain intact |
| CG-10 | Reproducibility | Golden-master byte-compare | byte-identical |
| CG-11 | Scenario gauntlet | `tests/scenario/test_gauntlet.py` (trend/chop/gap/expiry/circuit/holiday/corp-action/illiquid) | all pass |
| CG-12 | Property tests | `pytest tests/property -x` (hypothesis) | 0 falsifications |
| CG-13 | Fuzz parser | `tests/fuzz/test_groww_parser.py` (atheris) | 0 crashes in 60 s |
| CG-14 | Import graph | `import-linter` contract | no upward deps |
| CG-15 | Doc presence | `interrogate brutex --fail-under=100` | 100% public symbols |
| CG-16 | Canonical-params stability | Property test | serialise→parse→serialise idempotent |
| CG-17 | Rate-limit compliance | Bench test | never > 15 req/s |
| CG-18 | Integrity-gate coverage | All gates in §9 have a test | all present |
| CG-19 | Review gate | GitHub branch protection: 2 reviewers + CI green | enforced |
| CG-20 | License compliance | `pip-licenses --fail-on=UNKNOWN` | no unknown |

### 17.1 CI Pipeline Order

1. `ruff format --check` + `ruff check`
2. `mypy --strict` + `pyright`
3. `bandit` + `pip-audit`
4. `pytest -x tests/unit`
5. `pytest -x tests/property`
6. `pytest -x tests/golden`
7. `pytest -x tests/scenario`
8. `pytest --cov --cov-branch --cov-fail-under=100 tests/`
9. `mutmut run` (nightly; score reported on PR)
10. `pytest-benchmark` gate
11. `brutex audit verify` against CI-recorded audit
12. Docs build + `interrogate`
13. Container build + vulnerability scan

Any failure blocks merge. No `--no-verify`, no bypass.

---

## 18. Missing Functionality Register (MG)

Every known gap tracked explicitly. Status: `accepted` = will build; `deferred` = v1.1+; `out-of-scope` = not BruteX's job.

| ID | Gap | Status | Mitigation |
|----|-----|--------|------------|
| MG-001 | Tick data / L2 order book | out-of-scope (v1) | Bar-granularity disclosed as fidelity ceiling; revisit in v1.1 |
| MG-002 | Historical option Greeks API | accepted | Compute via Black-76/BS from underlying + rate curve |
| MG-003 | Historical IV surface | accepted | Compute from option chain snapshots |
| MG-004 | Corporate actions (splits/bonus/div) | accepted | Apply user-supplied or detect via price gap heuristic; store in `corporate_actions` table |
| MG-005 | Expiry-day lot-size revisions history | accepted | Snapshot daily from instrument CSV |
| MG-006 | Circuit-breaker history | accepted | Derive from bar-limit detection |
| MG-007 | SGX/GIFT-Nifty overnight signal | deferred | Not available via Groww |
| MG-008 | Holiday calendar | accepted | Derive from index-bar absence pattern |
| MG-009 | Continuous-futures rolled series | accepted | Panama / ratio / OI-weighted stitcher |
| MG-010 | VIX / PCR / max-pain history | accepted | Compute from raw chain data |
| MG-011 | Slippage / impact model | accepted | Three configurable models (§12.1) |
| MG-012 | STT/stamp/GST/exch cost model | accepted | §12.2 |
| MG-013 | SPAN margin files | accepted | Daily fetch from NSE (out-of-band) |
| MG-014 | MTM margin call / liquidation sim | accepted | §13.2 |
| MG-015 | Borrow / short-unavailability | deferred | v1.1 |
| MG-016 | Overnight gap handling | accepted | Gap detector + gap-trade-through model |
| MG-017 | Pre-open / auction session | deferred | v1.1 |
| MG-018 | Block-deal handling | out-of-scope | Not in Groww data |
| MG-019 | Walk-forward / purged-KFold / CPCV | accepted | §11.3 |
| MG-020 | Multi-testing correction (PBO / Deflated Sharpe) | accepted | §11.4 |
| MG-021 | Regime segmentation | accepted | FR-BF-010 |
| MG-022 | Strategy genealogy / lineage DAG | deferred | v1.1 nice-to-have |
| MG-023 | Reproducibility hash | accepted | FD-15 + NFR-UNIQ-001 |
| MG-024 | Look-ahead detector | accepted | FR-INT-004 |
| MG-025 | Survivorship guard | accepted | FR-INT-005 |
| MG-026 | Timestamp alignment validator | accepted | G-MONO |
| MG-027 | NaN / gap detector | accepted | G-NAN / G-GAP |
| MG-028 | Idempotency guard | accepted | NFR-REL-001/002 |
| MG-029 | Deterministic replay log | accepted | NFR-DET-002 + audit chain |
| MG-030 | Pre-2026 expired options recovery | **RESOLVED** — `get_expiries`/`get_contracts` enumerate natively from 2020 | see `docs/groww-sdk/backtesting.md §3-§4` |
| MG-031 | BSE cash / derivatives | deferred | v1.1 |
| MG-032 | Distributed multi-node Ray | deferred | v2.1 |
| MG-033 | Live forward-test (paper) | deferred | v1.2 |
| MG-034 | Drift alerter (live vs backtest) | deferred | v1.2 |
| MG-035 | Global-market adapters | deferred | v2.0 |
| MG-036 | Queue-position HFT modelling | out-of-scope | Impossible without tick data |

---

## 19. Risk Register

| ID | Risk | Likelihood | Impact | Mitigation |
|----|------|-----------|--------|------------|
| R-01 | Groww changes SDK method signature | Med | High | Adapter interface + golden-master fixture; CI probes daily |
| R-02 | Groww tightens rate limit | Med | High | Safety factor 0.6; configurable cap; alerting on near-misses |
| R-03 | Groww deprecates `backtesting` endpoint | Low | Critical | Abstract `IBrokerDataSource`; alt adapter (Zerodha/Upstox) designed |
| R-04 | Token leak | Low | Critical | Env-only + keyring; secret scanning; rotation procedure |
| R-05 | SLA fails on reference runner | Med | High | Two-phase funnel + Rust kernel + bitmap prefilter; CI gate |
| R-06 | `get_expiries` year filter upper/lower bound behaviour differs from docs | Low | Med | Golden-master test on a known 2020 expiry; empirically verify edge cases in P1c |
| R-07 | SPAN files unavailable / format change | Med | High | Parser versioned; manual override path |
| R-08 | Data corruption mid-ingest | Low | High | Content-addressed raw store; integrity gates; resumable |
| R-09 | Silent look-ahead bias | Med | Critical | Detector (FR-INT-004) + property test on every strategy |
| R-10 | Overfit during brute-force | High | Critical | CPCV + PBO + Deflated Sharpe + prominent warnings |
| R-11 | Numba fallback drifts from Rust kernel | Med | High | Cross-impl equivalence test in CI |
| R-12 | Mutable global state introduced accidentally | Low | High | Lint rule + architecture test |
| R-13 | Storage blows past disk budget | Med | Med | Zstd compression; old-slice eviction policy; disk alert |
| R-14 | Reproducibility broken by non-deterministic deps | Med | High | `PYTHONHASHSEED=0`; fixed RNG; deterministic lockfiles |
| R-15 | Audit chain corruption | Low | Critical | Hash-chain + CI verifier; append-only fs perms |

---

## 20. Phase Plan with Exit Criteria

| Phase | Name | Deliverable | Exit criterion |
|-------|------|-------------|----------------|
| **P0** | Requirements (now) | `docs/REQUIREMENTS.md` | User sign-off written in this file |
| **P1a** | Groww instruments | `pull_instruments_snapshot()` + raw parquet landing + daily runner | One day's CSV ingested, **19 columns** verified against `docs/groww-sdk/instruments.md` |
| **P1b** | Temporal master | `instruments_temporal` builder | `as_of(date)` returns correct universe for any past day |
| **P1c** | Candle adapter | `pull_candles()` with rate-limit bucket, chunker, dedup, integrity pass | 1 year NIFTY 1m ingested, zero gaps reported |
| **P1d** | Interval enumerator | Golden-master of `CANDLE_INTERVAL_*` | CI has frozen snapshot |
| **P2** | Data lake + feed | Parquet+DuckDB+Redis+LMDB, L3 feed, resampler, corp-action, continuous futures, look-ahead guard, golden-master tests | Deterministic replay matches fixture byte-for-byte |
| **P3** | Engine core | L4 exec sim (Indian costs + SPAN) + L5 Numba path + Rust kernel + bitmap prefilter + O(1) dedup LMDB | Single backtest reproduces a known-good equity curve within ε |
| **P4** | Orchestrator | L6 Ray + grid + Optuna + walk-forward + CPCV + PBO + Deflated Sharpe | `bench_1m_combos` passes 300s SLA |
| **P5** | UI/API | L7 FastAPI + dashboard + drill-down | Click any combo, see forensic view |
| **P6** | Paper mode (deferred v1.2) | `LiveFeed` swap + drift alerter | Backtest vs forward-test reconcile within Nσ |
| **P7** | Ops hardening | Prom/Graf/OTel + audit chain + security hardening + 100% coverage gate | CI green, mutation ≥ 85%, audit verified |
| **P8** | Global adapters (v2.0) | `BinanceAdapter` scaffolding | Same strategy runs unchanged on crypto data |

---

## 21. Acceptance Test Matrix

Each row is a **scenario × gate**. Green = test exists and passes.

| Scenario | FR covered | Gate test file |
|----------|-----------|----------------|
| Trend day (strong up) | FR-STR, FR-IND | `tests/scenario/test_trend_up.py` |
| Trend day (strong down) | FR-STR, FR-IND | `tests/scenario/test_trend_down.py` |
| Chop / range-bound | FR-STR, FR-IND | `tests/scenario/test_chop.py` |
| Gap-up open | FR-EXEC, FR-RISK | `tests/scenario/test_gap_up.py` |
| Gap-down open | FR-EXEC, FR-RISK | `tests/scenario/test_gap_down.py` |
| Expiry-day weekly options | FR-UNI, FR-STR | `tests/scenario/test_expiry_weekly.py` |
| Expiry-day monthly | FR-UNI, FR-STR | `tests/scenario/test_expiry_monthly.py` |
| Circuit filter triggered | FR-EXEC-003 | `tests/scenario/test_circuit.py` |
| Holiday week | FR-INT-003 | `tests/scenario/test_holiday.py` |
| Corp-action (split) | MG-004 | `tests/scenario/test_split.py` |
| Corp-action (bonus) | MG-004 | `tests/scenario/test_bonus.py` |
| Corp-action (dividend) | MG-004 | `tests/scenario/test_dividend.py` |
| Illiquid symbol | G-ZERO-VOL | `tests/scenario/test_illiquid.py` |
| Delisted symbol | FR-INT-005 | `tests/scenario/test_delisted.py` |
| Iron condor (4-leg) | FR-STR-004, FR-RISK | `tests/scenario/test_iron_condor.py` |
| Forced liquidation | FR-RISK-003 | `tests/scenario/test_liquidation.py` |
| Rate-limit near miss | FR-ING-007 | `tests/scenario/test_rate_limit.py` |
| Resume after SIGKILL | FR-ING-012 | `tests/scenario/test_resume.py` |
| Dedup hit on rerun | FR-BF-004 | `tests/scenario/test_dedup_rerun.py` |
| CPCV validation | FR-BF-005 | `tests/scenario/test_cpcv.py` |
| Walk-forward validation | FR-BF-005 | `tests/scenario/test_walk_forward.py` |
| PBO computed | FR-BF-006 | `tests/scenario/test_pbo.py` |
| Audit chain intact | NFR-OBS-003 | `tests/scenario/test_audit_chain.py` |
| Watchdog kills hung worker | NFR-OBS-001 | `tests/integration/test_watchdog.py` |
| 1M combos SLA | NFR-PERF-001 | `tests/bench/test_bench_1m_combos.py` |

---

## 22. Architecture Decision Records (ADR)

ADRs live in `docs/adr/NNNN-title.md`. Initial set (one-line summary here; full docs later):

| ADR | Title | Decision |
|-----|-------|---------|
| ADR-0001 | Scope lock to Groww `instruments` + `backtesting` | Accepted |
| ADR-0002 | Python 3.12 + Rust (PyO3) hot kernel | Accepted |
| ADR-0003 | Parquet + DuckDB + Arrow + Redis + LMDB storage stack | Accepted |
| ADR-0004 | Ray actor pool for brute-force orchestration | Accepted |
| ADR-0005 | CPCV + Walk-Forward + PBO + Deflated Sharpe as overfit guards | Accepted |
| ADR-0006 | Content-addressed reproducibility hash | Accepted |
| ADR-0007 | Two-phase funnel (vectorised → event-driven) | Accepted |
| ADR-0008 | Numba fallback alongside Rust kernel | Accepted |
| ADR-0009 | Hash-chained JSONL audit log | Accepted |
| ADR-0010 | Daily instrument CSV snapshotting for temporal master | Accepted |
| ADR-0011 | Rate-limit cap at 0.6 × Groww bucket (15 req/s effective) | Accepted |
| ADR-0012 | No `pandas` in hot paths (polars/numpy/numba/rust only) | Accepted |
| ADR-0013 | Dual-mode strategies (vectorised + event-driven) | Accepted |
| ADR-0014 | Runtime-dynamic timeframe resampler | Accepted |
| ADR-0015 | `import-linter` enforced layer dependencies | Accepted |

---

## 23. Glossary

| Term | Meaning |
|------|---------|
| **BruteX** | This product |
| **CPCV** | Combinatorial Purged Cross-Validation (Lopez de Prado) |
| **PBO** | Probability of Backtest Overfitting |
| **Deflated Sharpe** | Sharpe ratio adjusted for multiple-testing (Bailey & Lopez de Prado) |
| **SPAN** | Standard Portfolio Analysis of Risk — NSE margin model |
| **STT** | Securities Transaction Tax (India) |
| **MTM** | Mark-to-Market |
| **LTP** | Last Traded Price |
| **VWAP** | Volume-Weighted Average Price |
| **Freeze Quantity** | Max order quantity per exchange rules |
| **Lot Size** | Contract multiplier for F&O |
| **Circuit Filter** | Exchange price band (20%/10%/5%/2% depending on the symbol) |
| **Strike Chain** | Set of option strikes for a given expiry |
| **groww_symbol** | Groww's canonical instrument identifier |
| **Temporal Master** | Instrument table with validity ranges per row |
| **Data Snapshot Hash** | sha256 over the concatenated content hashes of every bar file in a universe/range |
| **Combo Hash** | sha256 over (strategy_id ‖ params ‖ data_snapshot ‖ code_sha) |
| **LMDB** | Lightning Memory-Mapped Database — our result cache |
| **Arrow IPC** | Apache Arrow inter-process columnar format |
| **Ray** | Distributed task framework |
| **Polars** | Rust-backed DataFrame library |
| **Numba** | Python JIT compiler |
| **PyO3** | Rust↔Python FFI bindings |

---

## 24. Appendix A — Verified Groww Facts & Sources

| Fact | Value | Source |
|------|-------|--------|
| Instrument CSV URL | `https://growwapi-assets.groww.in/instruments/instrument.csv` | `groww.in/trade-api/docs/python-sdk/instruments` |
| Columns in CSV | **19** (see §6.2 and `docs/groww-sdk/instruments.md`) | Groww docs |
| Backtest segments | `CASH`, `FNO` only | `groww.in/trade-api/docs/python-sdk/backtesting` |
| FNO history depth | From 2020 onward | Groww docs |
| Historical endpoint | `GET /v1/historical/candle/range` | Groww cURL docs |
| SDK method | `get_historical_candles(exchange, segment, groww_symbol, start_time, end_time, candle_interval)` | python-sdk/historical-data |
| Deprecated SDK method | `get_historical_candle_data` — DO NOT USE | python-sdk/historical-data |
| OHLC rate limit | ~25 req/s shared with quote | marketcalls / stratzy community docs |
| Time format | `"YYYY-MM-DD HH:mm:ss"` | Groww docs |
| PyPI package | `growwapi` 1.5.0, Python 3.9–3.13 | PyPI (Dec 2025) |
| Auth env var | `GROWW_API_AUTH_TOKEN` | Groww docs |

## 25. Appendix B — Honest Gap List

Things **we cannot get from Groww** and must source elsewhere or compute:

1. Tick data / order-book depth → **not available**, hard cap on sub-minute fidelity.
2. Historical options Greeks / IV surface → **must compute** from price + rate curve.
3. SPAN daily parameter files → **out-of-band** download from NSE.
4. Corporate actions → **out-of-band** (BSE/NSE corporate action feeds or user supply).
5. Historical instrument master (pre-first-snapshot) → **daily snapshotting** going forward for stable identifiers; historical F&O contracts (including expired ones) enumerated natively via `get_expiries` + `get_contracts` from 2020 onwards.
6. Holiday calendar → **derived** from index-bar absence pattern.
7. Continuous-futures series → **built** by stitcher (Panama / ratio / OI-weighted).
8. Historical circuit-filter bands → **derived** from bar-limit detection.
9. NSE bhavcopy cross-check data → **optional** for golden-master integrity tests.
10. Regulatory cost schedule changes → **versioned config**, hand-updated with citations.

## 26. Appendix C — Open Questions Deferred

These do **not** block P0 sign-off but must be resolved before the relevant phase:

| # | Question | Resolve by |
|---|----------|-----------|
| OQ-01 | Exact `groww_symbol` format for historical expired options | P1b |
| OQ-02 | ~~Max bars per call per interval~~ — **RESOLVED**: documented as 30/90/180 days by interval group in `docs/groww-sdk/backtesting.md §6` | — |
| OQ-03 | Whether Groww's OHLC bucket is per-token or per-IP | P1c |
| OQ-04 | Exact retry-after semantics on 429 (SDK raises? headers? silent?) | P1c |
| ~~OQ-GROWW-CANDLE-NAMES~~ | **RESOLVED 2026-04-14** — bare names (`CANDLE_INTERVAL_DAY`/`WEEK`/`MONTH`) are canonical, matches annexures page. See `docs/groww-sdk/backtesting.md §9` and `docs/groww-sdk/annexures.md §4`. Golden-master `test_groww_sdk_constants_frozen` pins all 12 names. | — |
| OQ-GROWW-INTERNAL-TRADING-SYMBOL | `get_all_instruments()` DataFrame has a 20th column `internal_trading_symbol` that is NOT in the documented CSV-columns table. Appears identical to `trading_symbol` in samples. Verify via CI assertion and flag any divergence. | P1a |
| OQ-GROWW-TICK-SIZE-UNIT | `tick_size` shows as `0.05` (float rupees) in CSV but as `5` (integer, presumably paise) in dict output. Normalise to `Decimal` rupees in adapter; assert crossed value matches expected NSE/BSE tick sizes. | P1a |
| OQ-GROWW-RATE-LIMIT-BUCKET | Is the OHLC rate bucket per-token, per-IP, or per-account? Probe by running from 2 machines simultaneously with the same token. | P1c |
| OQ-GROWW-429-RETRY-AFTER | Does `GrowwAPIRateLimitException.code` contain a Retry-After value, or just a generic error code? Probe by triggering a deliberate rate limit. | P1c |
| OQ-GROWW-YEAR-BOUNDS | Does `get_expiries(year=2020)` include expiries whose last_trade_date is in Jan 2020 but first_trade_date is in Dec 2019? Edge case around the boundary. | P1c |
| OQ-GROWW-TIMEZONE | Groww docs say "timestamps are in IST" but candle response uses `yyyy-MM-ddTHH:mm:ss` with no explicit timezone. Confirm the raw wire format is naive-IST (no Z suffix, no offset). | P1c |
| OQ-05 | Whether Greeks should be precomputed and cached, or computed on-read | P2 |
| OQ-06 | Default CPCV parameters (N, k, embargo, purge) for Indian F&O | P4 |
| OQ-07 | Dashboard stack: Streamlit MVP vs React/Next.js from day 1 | P5 |
| OQ-08 | Single-box vs cluster deployment for NFR-PERF-001 re-validation | P4 |
| OQ-09 | Whether to store Greeks per bar or per trade | P2 |
| OQ-10 | Brokerage defaults (Groww vs Zerodha vs flat) in cost model | P3 |

---

## 27. Sign-Off

This document requires explicit written sign-off from the product owner before any code in Phase P1+ is written. Sign-off captured here:

- [ ] Product owner: _________________  date: ________
- [ ] Scope locked as §1.2 / §1.3
- [ ] Finalized decisions accepted as §3
- [ ] Gap register acknowledged as §18
- [ ] Risk register acknowledged as §19
- [ ] Phase plan approved as §20

Once signed, this file is frozen. Changes require a new version and a changelog entry.

---

_End of REQUIREMENTS.md v1.0 DRAFT_

