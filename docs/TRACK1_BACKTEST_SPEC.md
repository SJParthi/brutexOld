# Track 1 Backtest System — Build Specification

> **Status:** Locked plan (received 2026-05-15). Track 1 = monolithic, hardcoded.
> Track 2 (plugin framework) is a separate later effort (July–Oct 2026).
> Do **not** design Track 1 for plugin extensibility — ship by May 31, 2026.

## Project goal
Build a focused, monolithic backtesting system for NSE/BSE index futures
that finds 3 production-ready intraday strategies for live trading by July 1, 2026.

Note: This is Track 1 (monolithic, hardcoded). A separate Track 2 plugin
framework will be built in July-October. Do NOT design for plugin extensibility
in this build — keep it focused on shipping Track 1 by May 31.

## Hardware target
- MacBook Pro M4 Pro, 14-core CPU (10P + 4E), 20-core GPU, 48 GB RAM
- macOS Tahoe 26.4.1
- 8TB external SSD mounted at /Volumes/QuantData
- Daily apps (Chrome, IntelliJ, Claude Desktop) will be running concurrently
- Keep backtest engine working set under 40GB RAM

## Tech stack (locked)
- Backtest engine: Rust (workspace with multiple crates)
- Concurrency: Rayon for CPU parallelism
- GPU acceleration: Metal compute (Apple Silicon) for indicator precompute only
- Optimizer: Python + Optuna (TPE sampler)
- Glue: PyO3 bindings (Python orchestrator → Rust engine)
- Storage: Parquet with Zstd compression level 9
- Query layer: DuckDB
- Validation: pytest + cargo test + property-based testing

## Market scope (locked)
- Instruments: NIFTY-FUT, BANKNIFTY-FUT, SENSEX-FUT
- Continuous futures: stitch contracts with ratio back-adjustment on roll dates
- Roll convention: switch to next-month contract when next-month volume exceeds current
- Data source: Dhan historical API (https://dhanhq.co/docs/v2/historical-data/)
- Endpoints:
    POST /v2/charts/intraday  (interval=1 only, 90-day chunks)
    POST /v2/charts/historical (daily, for PDH/PDL and back-adjustment)
- Exchange segments: NSE_FNO for NIFTY/BANKNIFTY, BSE_FNO for SENSEX
- Instrument type: FUTIDX
- Data range: 5 years (2021-05-15 to 2026-05-15)
- Trading session: 09:15 - 15:30 IST
- Trading window: 09:16 entry minimum, 15:20 forced exit, intraday only

## Timeframes (locked — 21 total)
Generate ALL of these from 1-min source via local resampling anchored to 09:15 IST:
- Minutes: 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15
- 30 minutes
- Hourly: 60 (1h), 120 (2h), 180 (3h), 240 (4h)
- Daily: 1d

Resampling rules:
- Anchor origin to 09:15 IST each day
- Drop partial bars at session close
- Aggregate: O=first, H=max, L=min, C=last, V=sum
- Store each timeframe as separate Parquet file

## Indicators (locked — all precomputed and cached)
1. EMA 20 (close)
2. EMA 200 (close)
3. VWAP (session-anchored, resets daily at 09:15)
4. Day High (running intraday)
5. Day Low (running intraday)
6. PDH (Previous Day High — from daily data)
7. PDL (Previous Day Low — from daily data)
8. Traditional Daily Pivots: P, R1, R2, R3, S1, S2, S3
   - P = (PDH + PDL + PDC) / 3
   - R1 = 2P - PDL, S1 = 2P - PDH
   - R2 = P + (PDH - PDL), S2 = P - (PDH - PDL)
   - R3 = PDH + 2(P - PDL), S3 = PDL - 2(PDH - P)
9. Static Daily Fibonacci levels (from previous day's H/L range):
   0%, 23.6%, 38.2%, 50%, 61.8%, 78.6%, 100%, 127.2%, 161.8%, 200%, 261.8%, 423.6%
10. Candle metadata: color (bull/bear/doji), body_pct, upper_wick_pct, lower_wick_pct

Cache strategy:
- Compute once per (instrument, timeframe) pair
- Store as additional columns in same Parquet file as OHLCV bars
- Cache key: hash of (data_version, indicator_version)

## Strategy framework (parameter search space)

Entry triggers (8 types):
1. Bull candle bounces off Daily Pivot
2. Bull candle breaks above R1 (target R2)
3. Bear candle breaks below S1 (target S2)
4. Price at VWAP + EMA 20 confluence (bullish: both above; bearish: both below)
5. PDH retest after breakout (price returns to PDH after closing above)
6. PDL retest after breakdown (price returns to PDL after closing below)
7. Price at 61.8% Fib + VWAP confluence
8. Price at 78.6% Fib + EMA 20 confluence

Additional filters per entry:
- Candle color requirement: bull-only / bear-only / either
- Confirmation candles: 1, 2, or 3 consecutive
- Entry timing window: 09:16-10:30, 10:30-14:00, 14:00-15:00, 09:16-15:00 (4 options)

Exit configuration — two modes tested separately:
- Mode A — WITH SL/TP (points-based):
    SL range: 10-300 points (instrument-specific)
    TP range: 30-1500 points (instrument-specific)
    SL types: fixed, trailing
    Asymmetric focus: TP/SL ratio >= 1.5
- Mode B — WITHOUT SL/TP (signal-based exit):
    Exit on: opposite entry trigger fires
    Exit on: candle reversal pattern
    Exit on: forced 15:20 close

Position sizing: 1%, 2%, 3%, 5% of capital
Max trades per day: 1, 2, 3, 5
Min gap between trades: 0, 15, 30, 60 minutes

## Non-negotiable correctness requirements

1. **Money in int64 paisa.** Never floating point. 1 INR = 100 paisa.
2. **Prices in int64 ticks.** Tick size 0.05 INR = 5 paisa. Convert to display rupees only at output.
3. **Timezone: UTC storage, IST display.** Indian session: 03:45-10:00 UTC.
4. **No look-ahead bias.** Signal generated on bar N → entry executes at bar N+1 open price.
5. **Pessimistic fills.** Entry filled at WORST price within entry bar (high for buys, low for sells). Exits at worst price too.
6. **Slippage model.** 1 tick adverse on entry + 1 tick adverse on exit.
7. **Brokerage model.** Match Dhan futures brokerage: ₹20 per executed order or 0.03% whichever is lower.
8. **Indian taxes.** STT 0.0125% on sell side of futures. Exchange transaction charges 0.0019%. GST 18% on (brokerage + transaction charges). Stamp duty 0.002% on buy side.
9. **Forced 15:20 IST exit.** Override SL/TP/signal exits. No exceptions.
10. **Reproducibility hash.** Every result row includes hash of (data_version, params, code_commit_hash).

## The 5-layer build sequence

### Layer 1 — Data Acquisition & Storage (Days 1-2)
- Implement Dhan API client with rate limit handling (1 req/sec, exponential backoff on 429)
- Fetch 1-min data for 3 instruments × 5 years in 90-day chunks (~60 API calls total)
- Fetch daily data for the same (1 call per instrument)
- Validate data integrity:
    * No gaps within trading session
    * No zero volumes during liquid hours
    * Volume present in all bars (futures should always have volume)
    * No extreme outliers (>10 sigma) without news context
- Handle continuous futures stitching:
    * Detect roll dates (when next-month volume exceeds current)
    * Compute back-adjustment ratio
    * Apply ratio to all pre-roll prices
- Store raw Parquet files: /Volumes/QuantData/raw/{instrument}/{1min,daily}.parquet
- Resample 1-min to all 20 other timeframes, store at /Volumes/QuantData/resampled/{instrument}/{tf}.parquet
- Hash every Parquet file. Hashes used in downstream result IDs.

**Validation Gate 1:**
- Pick 50 random bars across instruments and timeframes
- Compare against TradingView display values
- Must match within 0.05% on prices, exactly on volume
- Save validation report to /Volumes/QuantData/validation/layer1_report.json

### Layer 2 — Indicator Precompute (Days 3-4)
- Implement all 10 indicators in Rust with SIMD optimizations where applicable
- Use Metal compute shaders for batch indicator precompute (parallel across timeframes)
- Compute once per (instrument, timeframe) — store as columns in same Parquet
- Indicators that are daily-derived (PDH, PDL, Pivots, Fib) are constant across day for all TFs
- Indicators that are bar-derived (EMA, VWAP, candle metadata) computed per TF

**Validation Gate 2:**
- Pick 10 random timestamps per indicator per instrument
- Compare against TradingView's same indicator
- Prices within 0.5%, indicator values within 1%
- Save validation report to /Volumes/QuantData/validation/layer2_report.json

### Layer 3 — Backtest Engine Core (Days 5-9)
Modules:
- data_loader: memory-map Parquet files for shared worker access
- bar_iterator: sequential bar walking, no look-ahead enforcement
- signal_evaluator: check all 8 entry triggers against current bar + indicators
- position_manager: track open positions, SL/TP state, partial exits
- fill_simulator: pessimistic H/L fill + 1 tick slippage
- cost_calculator: brokerage + STT + GST + stamp duty per trade
- metrics_computer: streaming Sharpe, Sortino, Calmar, MDD, win rate, profit factor
- result_writer: batch 10K rows, write to Parquet with Zstd

Concurrency:
- 12 worker threads (10 P-cores + 2 E-cores, leave 2 for OS)
- Each worker pulls one parameter combination from queue
- No shared mutable state between workers
- Memory-mapped read-only data shared across workers

**Validation Gate 3 — CRITICAL:**
- Build reference test suite of 100 hand-coded scenarios with expected PnL
- Scenarios cover edge cases: gap days, holidays, partial fills, max-trade-cap, forced exits
- Engine must match all 100 within 1 paisa
- Additionally: replay 20 random combinations on TradingView manually
- Backtest results must match TradingView within 0.5%
- DO NOT PROCEED TO LAYER 4 IF THIS GATE FAILS

### Layer 4 — Optuna Bayesian Optimization (Days 10-12)
- Optuna TPE sampler
- 50,000 trials per (strategy, instrument, mode)
- 12 concurrent trials via Optuna's parallelization
- Composite scoring function:
    score = (sharpe × sqrt(num_trades) × profit_factor) / (max_drawdown_pct + 1)
- Hard constraints (trial pruned if violated):
    num_trades >= 100
    max_drawdown_pct <= 30
    profit_factor >= 1.0
    avg_trades_per_day between 1 and 5
- Early-stop trials showing zero edge after 6 months of simulated data
- Persist Optuna study to SQLite at /Volumes/QuantData/optuna_studies/{run_id}.db
- Stream all trial results to Parquet at /Volumes/QuantData/results/{run_id}/trials.parquet

**Validation Gate 4:**
- First 1000 trials inspected — Optuna should be exploring, not stuck in one region
- Trial-per-second rate logged — should be ~10-50 trials/sec on M4 Pro
- Save inspection report

### Layer 5 — Statistical Filters (Days 13-15)
Pipeline (sequential filters, each reads output of previous):

**Filter 5.1 — Top-K composite:** Take top 1,000 trials by composite score from each (strategy, instrument, mode) combination.

**Filter 5.2 — Deflated Sharpe Ratio (López de Prado 2014):**
Compute DSR for each candidate using formula:
    DSR = ((SR - E[max(SR)]) × sqrt(T-1)) / sqrt(1 - γ₃×SR + ((γ₄-1)/4)×SR²)
where E[max(SR)] adjusts for number of trials tested (N=50,000).
Keep candidates with DSR p-value > 0.95.

**Filter 5.3 — Combinatorial Purged Cross-Validation:**
Split 5-year data into 12 groups of ~5 months each.
Test all C(12,3) = 220 combinations of 3-block test sets.
Apply 2-week embargo between train/test to prevent leakage.
Keep candidates with positive Sharpe in >= 80% of test splits.

**Filter 5.4 — Walk-Forward Consistency:**
Sliding window: train 12 months / test 3 months, slide forward 3 months.
~16 walks across 5 years.
Keep candidates with positive Sharpe in >= 12 of 16 walks.

**Filter 5.5 — Lockbox (Out-of-Sample):**
RESERVE last 6 months (Dec 2025 - May 2026) — NEVER seen by any prior filter.
Enforce isolation at code level: any code path that touches lockbox data outside this filter raises exception.
Test top 10 candidates ONCE on lockbox.
Pass criteria:
    Lockbox Sharpe within 30% of training Sharpe
    Lockbox max drawdown within 50% of training max drawdown
    Lockbox win rate within 10% of training win rate

**Validation Gate 5:**
- Verify lockbox isolation: grep codebase for any reference to lockbox data outside Filter 5.5
- Save filter cascade report showing N candidates at each stage

### Layer 6 — Reporting & Final Selection (Day 16)
- Generate per-strategy report card:
    * Equity curve plot
    * Monthly PnL heatmap
    * Trade distribution histogram
    * Drawdown chart
    * All key metrics with confidence intervals
- Export top 3 strategies as JSON configs for live trading:
    /Volumes/QuantData/results/{run_id}/top3_strategies.json
- Each JSON includes complete parameter spec ready for live deployment
- Generate PDF summary report with methodology, results, top 3 configs, caveats
- DuckDB query interface for ad-hoc result exploration

## Result table schema (Parquet)
Every backtest produces one row with these columns:
- combo_id (deterministic hash)
- run_id (Optuna study reference)
- strategy_template (entry trigger type)
- instrument, timeframe, mode (with_sltp or without_sltp)
- All strategy parameters as separate columns
- num_trades, num_wins, num_losses, num_breakeven
- gross_pnl_paisa, net_pnl_paisa, pnl_per_trade_paisa
- win_rate, profit_factor, expectancy
- sharpe_ratio, sortino_ratio, calmar_ratio
- max_drawdown_paisa, max_drawdown_pct, drawdown_duration_days
- avg_trades_per_day, avg_holding_minutes
- monthly_pnl_breakdown (JSON column for walk-forward queries)
- deflated_sharpe, dsr_pvalue (filled in Layer 5.2)
- cpcv_pass_rate (filled in Layer 5.3)
- wfo_pass_rate (filled in Layer 5.4)
- lockbox_sharpe, lockbox_passed (filled in Layer 5.5 for top 10 only)
- data_hash, code_commit_hash, indicator_version_hash

## Cross-cutting requirements

**Reproducibility:**
- Every result row tagged with hash of (code commit, data version, all params)
- Same hash inputs ALWAYS produce same outputs
- Random seeds explicit and logged

**Error handling:**
- Fail loudly. No silent garbage.
- Every layer's output must pass validation gate before next layer runs.
- Engine crash → log full state, save partial results, allow resume from checkpoint

**Logging:**
- Structured JSON logs per worker
- Aggregated to /Volumes/QuantData/logs/{run_id}/
- Progress dashboard: trials/sec, best score, ETA, memory usage

**Checkpointing:**
- Optuna study persisted every 100 trials
- Resume from checkpoint supported (engine crash should not lose >100 trials)

**Memory management:**
- Per-worker working set capped at 500 MB
- Total engine memory under 40 GB (leaves 8 GB for OS + your daily apps)
- Streaming I/O — no "load all results in RAM"
- Memory-mapped Parquet for shared data access

**Testing:**
- Every Rust module: cargo tests covering happy path + edge cases
- Every Python orchestration module: pytest with mock data
- Integration tests at each layer boundary
- The 100-scenario reference suite is the master correctness test

## Build sequence (strict order, no skipping)
1. Layer 1: Data ingestion + validation
2. Validation Gate 1 must pass
3. Layer 2: Indicator precompute
4. Validation Gate 2 must pass
5. Layer 3: Backtest engine + 100-scenario suite + TradingView spot check
6. Validation Gate 3 must pass (CRITICAL — do not proceed without this)
7. Layer 4: Optuna integration
8. Validation Gate 4 must pass
9. Layer 5: All five filters in sequence
10. Validation Gate 5 must pass (lockbox isolation verified)
11. Layer 6: Reporting + JSON export

## Timeline
- Today: May 15, 2026
- Target completion: May 31, 2026 (16 days)
- First sweep: June 1, 2026
- Final candidate selection: June 25, 2026
- Live trading start: July 1, 2026

## Out of scope for Track 1
Do NOT build any of these — they are Track 2 concerns:
- Plugin architecture / hot-loadable indicators
- Pine Script translator
- YAML job definitions
- Multi-broker support
- Multi-asset (equities, options)
- Distributed compute
- Web dashboard
- Live execution integration

## Working style requests
- Show me what you propose to build BEFORE writing code
- Pause after each layer for me to verify validation gate passed
- Ask clarifying questions when requirements are ambiguous
- Report progress after each module
- If you discover a requirement conflict, STOP and ask — do not guess
- Document every non-obvious decision in code comments

## First action
Begin with Layer 1 (data ingestion). Before writing any code, propose:
1. Folder structure for the Rust workspace + Python package
2. Cargo.toml dependencies for Rust crates
3. requirements.txt for Python packages
4. The exact Dhan API request format you'll use (test one call first)
5. How you'll handle the 90-day chunking and rate limits

Wait for my approval on the structure before implementing.
