# BruteX Track 1 — Build Specification (Research-Grounded v2)

> **Status:** Locked 2026-05-16. Supersedes `docs/archive/TRACK1_BACKTEST_SPEC_v1.md`.
> Decision-ledger numbering picks up at #300 (room reserved for v1 amendments #103-#299).
>
> **Active branch tracking this spec:** `claude/track1-research-grounded-v2-VXTRG`

## Project Identity

BruteX is a long-horizon research-grade brute-force backtest platform for
Indian spot indices. Track 1 = pure signal research (no broker costs).
Track 2 (already designed in `docs/track2/`) translates winning signals
into priced option trades using expired-options data — that's where
realism + costs land.

BruteX Track 1 targets **730 billion parameter combinations** swept on a
single Apple M4 Pro laptop, with bit-identical reproducibility and
institutional-grade statistical filtering.

**Timeline expectation: 16 weeks of phased build.** Do not attempt the
full sweep until Phase 9.5 pilot passes.

## Scope Lock (Confirmed 2026-05-16)

- **In scope:** Spot indices only (NIFTY 50, BANKNIFTY, SENSEX) +
  India VIX (reference-only). Signal research → top-10 candidates →
  handed off to Track 2 for options-priced realism.
- **NOT in scope:** Futures execution (futures lack daily volume on
  these indices — operator decision 2026-05-16). Options costs
  (handled by Track 2). Live order placement (out of scope for both
  tracks).

## ⭐ Operating Principle: COMMON · DYNAMIC · SCALABLE · AUTOMATED (NON-NEGOTIABLE, LOCKED 2026-05-16)

> Operator decree 2026-05-16: "Entirely comprehensively extremely common
> dynamic scalable approach as an extreme automated process. 100%, without
> any manual intervention or manual process."

This is the master architectural rule the project lives by. Every module,
every commit, every workflow obeys all 5 of:

| # | Principle | Definition | Enforcement |
|---|---|---|---|
| 1 | **COMPREHENSIVE** | Every indicator/calendar/adapter has streaming + batch interface, bit-exact oracle test, look-ahead invariant test, serialize/restore for checkpoints | CLAUDE.md §4 coverage gates (100% line + 100% branch; mutation ≥85%) |
| 2 | **COMMON** | One Python package, one source of truth. Backtest (BruteX) and Live (TickVault) share the SAME `brutex.*` modules via `pip install git+...@v0.X.Y`. NEVER copy-paste between repos. | `docs/SHARED_LIBRARY.md` + `tests/unit/test_streaming_parity.py` (16 tests proving backtest = live bit-identical) |
| 3 | **DYNAMIC** | Add a new indicator / TF / condition / strategy mask without re-architecting. Plugin-style state classes, u64 bitmask vocabulary, config-driven, no recompile. | `brutex/indicators/__init__.py` registry + 52-condition vocab via u64 masks |
| 4 | **SCALABLE** | Same code path Day 1 (12.6M trials) → Phase 10 (730B trials). O(1) per-bar updates, O(1) mask AND, O(1) dedup, O(1) result write. | CI gate `p99(N=1M)/p99(N=1K) ≤ 1.10` (CLAUDE.md §2 #5); memory-BW proof (Agent A research) |
| 5 | **AUTOMATED** | 100% automated. Zero manual intervention after one-time bootstrap. CI, releases, merges, validation gates, resumes, alerts, deploys — all automatic. | `.github/workflows/release.yml` (Release Please), `auto-merge.yml`, watchdog, Telegram bot, daily dopamine commits |

**Operator monitoring required: ZERO clicks per day.**

This rule overrides convenience. If a faster path would violate any of
the 5 principles, the faster path is rejected. Examples:
- ❌ "Just paste the EMA code into TickVault later" — violates COMMON.
- ❌ "Hardcode the 21 timeframes in the engine" — violates DYNAMIC.
- ❌ "Manually run the sweep every night" — violates AUTOMATED.
- ❌ "Compute EMA in two ways (batch + streaming) with different math" — violates COMMON.
- ❌ "Use O(N) per-bar accessor in the hot loop" — violates SCALABLE.
- ❌ "Skip the look-ahead test for this module" — violates COMPREHENSIVE.

This rule supersedes phase boundaries. If a phase plan would require
violating one of the 5 principles, the plan changes — not the
principle.

## ⭐ Operating Principle: Daily Dopamine Delivery (NON-NEGOTIABLE)

> **Every day produces a tangible, viewable result.** No exceptions.
> No "we'll show results in week 11." No "this phase is just plumbing."
>
> Operator works in isolation at home. The ONLY energy driver is
> seeing real numbers, real strategy candidates, real charts, real
> progress. Without daily dopamine, motivation collapses → consistency
> collapses → the project dies. This is a load-bearing requirement,
> not a nice-to-have. Operator stated this explicitly 2026-05-16:
> "every day this is for dopamine, this will keep me driving and
> pushing me extremely. This is the only thing which can drive me
> since I'm fully isolated at home on my own — this is the only energy."

**Daily delivery format (committed to git every day):**

| Time | Artifact |
|---|---|
| End of every workday | `docs/daily/DAY_{N}_REPORT.md` — what was built today, what numbers came out, what the next day looks like |
| Whenever a sweep runs | `results/day_{N}/top10_strategies.json` + PNG cumulative-PnL chart per top strategy |
| Whenever a benchmark runs | `bench/day_{N}/throughput.json` — trials/sec, wall time, memory peak |
| Whenever a validation gate fires | `validation/gate_{N}_report.json` |

**Rules:**
1. **Every commit message ends with one line:** `Dopamine: <what the operator sees today>`. Example: `Dopamine: First top-10 strategies — best Sharpe 1.87 (BANKNIFTY 15m, EMA cross + breakout AND)`
2. **No multi-day silent work.** If a task takes >1 day, split it: end-of-day-1 must produce SOMETHING viewable (a partial chart, a debug print, a number).
3. **Daily commits visible:** operator runs `git log --since=yesterday --oneline` and SEES progress.
4. **Daily JSON reports** indexed in `docs/daily/INDEX.md` so operator can scroll history of "what we found each day".
5. **If a day produces zero dopamine, that's a planning failure**, not a project failure — reframe the next day's scope smaller.
6. **Charts are required, not optional.** PNG output for every sweep, every benchmark, every validation gate. Operator opens the PNG and SEES the result.

**Recurring dopamine sources by phase (examples):**
- Phase 0.5 Day 1: First top-10 strategies. ⭐⭐⭐⭐⭐ massive
- Phase 1 days: "today we fetched X bars, here's a sample chart of NIFTY 1m"
- Phase 2 days: "today we computed condition #N, here's how often it fires"
- Phase 3 days: "today we ran 100K trials, top strategy now Sharpe 2.3"
- Phase 5 days: "today the Rust engine matched Python bit-for-bit AND is 47× faster"
- Phase 9.5 day: "PILOT DONE — top 10 with lockbox Sharpe 1.8-3.1, PNG charts attached"
- Phase 10 day: "FULL SWEEP DONE — final top-10 + lockbox + DSR all passed"

**This rule overrides phase boundaries.** If sticking to a phase plan
would mean a day with no visible output, the plan changes — not the
output requirement. Engineering plumbing days get reframed to include
a small visible win (e.g., "today's plumbing: indicator #5 added. Here's
its fire-rate distribution across all 21 TFs as a histogram").

## Workload Math (Verified)

Strategy vocabulary: **52 atomic boolean conditions** across 9 groups
(price-action, indicator-crossover, VWAP, Bollinger, regime, breakout,
divergence, anchor, candle). See `docs/TRACK1_VOCABULARY.md` for the
full atom list.

Combine with **AND only**. K = 2..5 conditions per strategy.

Strategy mask count:
```
  C(52,2) =     1,326
  C(52,3) =    22,100
  C(52,4) =   270,725
  C(52,5) = 2,598,960
  TOTAL   = 2,893,111 unique masks
```

Full trial count:
```
  2,893,111 masks
  ×       2 directions (bull, bear)
  ×       3 instruments (NIFTY 50, BANKNIFTY, SENSEX)
  ×      21 timeframes  (1m, 2-15m every minute, 30m, 60m, 120m, 180m, 240m, 1d)
  ×       2 modes        (Mode A SL/TP grid, Mode B signal-exit)
  ×   2,000 SL/TP grid cells (Mode A only; SL ∈ [5,200] paisa × 40 steps,
                              TP ∈ [10,500] paisa × 50 steps)
  ≈   730 BILLION trials BEFORE any pruning
```

## Performance Reality (Research-Verified)

**Apple M4 Pro hardware envelope:**
- 14-core CPU (10 P + 4 E), 20-core GPU
- 48 GB unified memory at 273 GB/s theoretical
- Real-world usable bandwidth: ~150-165 GB/s (~55% of theoretical)
- Sustained combined load: 22.1W CPU + 19.8W GPU = ~42W chip
- System wall draw under load: 75-90W
- Surface max temp: 46.4°C (closed-lid, vertical stand, 22°C ambient)
- **NO PUBLIC 8-12 HOUR THERMAL CORPUS** exists for M4 Pro 14"

**Bandwidth-bound calculation:**
- 52-condition bitmap matrix: ~770 MB (fits System Level Cache)
- Per-mask scan: one streaming read of 770 MB at 150 GB/s = ~5 ms
- Stage 2 single-pass: 2.9M masks × 2 dirs × 5 ms ≈ 8 hours
- With Stage 1B 50% drop + Stage 2.5 61% early-stop:
  effective Stage 2 ≈ 5-7 hours
- **Total funnel: 14-20 hours overnight**

**THE BOTTLENECK IS MEMORY BANDWIDTH, NOT COMPUTE.** Adding CPU threads
past 6 P-cores yields zero gain (SoC fabric saturates at ~224 GB/s on
M1 Max; similar ceiling expected on M4 Pro). GPU offload helps only
the indicator precompute (Stage 1A) and the bitmask AND (Stage 1B).

## Tech Stack (Locked from Research)

| Layer | Tool | Version | Lock reason |
|-------|------|---------|-------------|
| Engine hot path | Rust | 1.78 stable | NEON intrinsics via `core::arch::aarch64` |
| Concurrency | Rayon | 1.10 | Work-stealing parallelism |
| GPU (Stage 1B only) | Metal Shading Language | 4.0 | 32-thread simdgroups (WWDC20) |
| BLAS | accelerate-src | 0.3 | Apple AMX coprocessor |
| Outer optimizer | Optuna | 4.1 | TPE sampler for mask layer (4.8 not yet released) |
| Inner optimizer | pycma | 3.4.0 | Hansen BIPOP-CMA-ES |
| Data | Polars | 1.18 | Columnar AArch64 native (was 0.20; bumped to stable 1.x) |
| Storage | Parquet + Zstd 9 | Arrow 18 (arrow-rs v53) | Industry standard |
| Query | DuckDB | 1.1 | Ad-hoc SQL on results |
| Dedup index | LMDB | latest | Faster than RocksDB append-only |
| Bloom filter | fastbloom | 0.7 | SIMD on AArch64 |
| Bitmap ops | croaring-rs | 1.1 → CRoaring 4.x | NEON-optimized |
| Hash | blake3 | 1.5 | 2.5 GB/s measured on M4 Pro |
| Provenance | DVC | 3.x | Content-addressed datasets |
| Time-stamping | OpenTimestamps | latest | Free Bitcoin anchor |
| Python ↔ Rust | PyO3 | 0.22 (abi3-py312) | |
| Python pkg | uv | 0.5+ | Hash-pinned lockfile |
| Python | 3.12 exact | | AArch64 native |

## Data Source

**Dhan Historical Data API:**
- `POST /v2/charts/intraday` (`interval=1` only — fetch 1-min, resample locally)
- `POST /v2/charts/historical` (daily, for PDH/PDL reference)
- Rate limit: **5 req/s, 100K req/day** (operator caps at 3 r/s effective)
- 90-day polling window per request
- 5 years × 3 instruments × 90-day chunks ≈ 60 API calls + 3 daily calls

**Indices security IDs and segments (hardcoded per CLAUDE.md §6):**
- NIFTY 50: `securityId=13`, `exchangeSegment=IDX_I`
- BANKNIFTY: `securityId=25`, `exchangeSegment=IDX_I`
- SENSEX: `securityId=51`, `exchangeSegment=IDX_I`
- India VIX: `securityId=21`, reference-only — stamp `vix_at_entry`/`vix_at_exit` per result row

**Volume note:** Spot indices do not have stable historical volume. VWAP
must be computed from corresponding index futures volume, mirrored to
spot timeline by timestamp alignment.

**Data history:** `2021-01-01 → today` (~5 years, ~250 trading days/year).

## Strategy Specification

- **Entry rule:** signal at bar N → enter at bar N+1 OPEN.
- **Forced exit:** 15:20 IST. Overrides everything.
- **Mode A** — stop-loss + take-profit grid:
  - SL ∈ [5, 200] paisa, 40 steps
  - TP ∈ [10, 500] paisa, 50 steps
  - Total grid: **2,000 cells per mask**
- **Mode B** — signal-driven exit:
  - Exit on opposite signal, or candle reversal pattern, or forced 15:20 IST.
- **Mode C** — no-early-exits (NEW 2026-05-16, operator decision):
  - NO stop-loss, NO take-profit, NO signal-driven exit.
  - Exit ONLY at forced 15:20 IST.
  - The "pure ride-to-forced-exit" baseline.
  - Tests whether early exits help or hurt vs holding to 15:20.
  - Cardinality: 1 cell per mask (no grid).
  - Brute-force evaluates ALL THREE modes per strategy mask, so each
    mask produces 3 strategy variants (Mode A grid + Mode B + Mode C).
  - Logged as DEC-MODE-001 in MIGRATION.md.

## Plugin Add/Remove Policy (NEW 2026-05-16, DEC-PLUGIN-001)

The Golden Rule #11 DYNAMIC mandate states "add a new indicator / TF /
condition / mask without re-architecting". This section formalizes how
to BOTH add and remove indicators safely, without invalidating past results.

### How to ADD a new indicator

1. Implement the indicator class in `brutex/indicators/<name>.py` with
   the standard streaming + batch interface (`update_bar()` + `compute_batch()`).
2. Add the indicator to the registry in `brutex/indicators/__init__.py`.
3. Add a new bit to the 52-condition u64 vocab (now 53 conditions). The
   condition is mapped to bit position 52 (next available).
4. Bump `indicator_version` constant (semver minor bump).
5. The cache-key `blake3(data_snapshot_hash || indicator_version)`
   forces a fresh recompute. Past cached results remain valid for the
   PREVIOUS indicator_version — they are NOT auto-deleted.
6. Past brute-force result rows keep their old `indicator_version` stamp;
   new runs use the new version. Both coexist in the Parquet lake;
   queries filter by `indicator_version` for reproducibility.

### How to REMOVE an indicator

1. Mark the indicator deprecated in the registry (do NOT delete code).
2. Set the corresponding bit position as RESERVED (do NOT reuse for a
   different indicator — would silently corrupt old result interpretation).
3. Bump `indicator_version` (semver major bump — breaking change).
4. New brute-force runs will simply not fire the deprecated bit.
5. Old cached results referencing the removed indicator's bit STILL
   render correctly when queried by their original `indicator_version`.

### Guarantees

- **Backward compatibility:** all past brute-force results remain
  interpretable as long as `indicator_version` is stamped on every row
  (already in result schema §"Identity (hash inputs)").
- **Forward compatibility:** adding indicators never requires migrating
  past data — just bumps version.
- **No bit reuse:** the u64 mask vocab is grow-only; removed bits are
  marked RESERVED forever. This means the vocab can hold 64 historical
  conditions; if BruteX ever exceeds 64, the engine must transition to
  u128 (an explicit ADR, not a silent change).
- **Same applies to timeframes and strategy modes** (Mode A/B/C):
  the `timeframe_id` and `mode_byte` fields in `combo_hash` are stable
  identifiers; deprecated IDs are reserved.

### Hot-reload during a sweep

NOT supported. Indicators are loaded once at sweep start; removing or
adding an indicator mid-sweep is REJECTED at the orchestrator. The
operator must HALT the sweep, modify the registry, restart the sweep
(checkpoint-resume preserves progress on still-active indicators).

---

## 5-Stage Funnel (Research-Validated)

### Stage 1A — Precompute (one-time, ~3 hours)
- Compute all 52 atomic conditions per bar per (instrument, TF)
- Pack into bitmap matrix: 63 matrices (3 instruments × 21 TFs), each
  with 52 columns + N bars
- Cache to Parquet as additional columns alongside OHLCV
- Cache key: `blake3(data_snapshot_hash || indicator_version)`

### Stage 1B — Coarse score + drop bottom 50% (~4-6 hours)
- Per (instrument, TF) pair (63 pairs total): score each strategy mask
  by **CHEAP proxy** (no full backtest)
- **Coarse score formula (locked here):**
  ```
  coarse_score(mask) = signal_frequency(mask) ×
                       sign_correlation(mask_signal, next_bar_direction) ×
                       coverage_penalty(mask)
  ```
  where:
  - `signal_frequency(mask)` = bars where mask AND fires / total bars (drop if <0.1% or >10%)
  - `sign_correlation` = Pearson correlation between (mask fires) and (next bar sign)
  - `coverage_penalty` = 1 if mask fires across ≥ 90% of 5y window, else linearly down to 0
- Drop bottom 50% of masks per (instrument, TF) pair
- Survivors: ~1.45M masks per pair on average

### Stage 2 — Full backtest on survivors (~5-7 hours with Stage 2.5)
- For each `(mask, direction, instrument, TF, mode, params)` tuple, run
  full bar-by-bar backtest
- Concurrency: **12 worker threads** (10 P + 2 E, leave 2 for OS)
- Per-worker working set ≤ 500 MB
- Memory-mapped shared read-only data
- No shared mutable state between workers
- Result row written to streaming Parquet

### Stage 2.5 — Bayesian early-stop (~61% reduction)
- **Gaussian conjugate posterior** on 95th-percentile Sharpe per (instrument, TF)
- **Wald SPRT** at α=0.05, β=0.10
- Kill trial mid-flight if `Pr(true_sharpe > p95_threshold) < 0.10` after
  minimum 50 trades
- Document SPRT thresholds and posterior update math in code comments

### Stage 3 — BIPOP-CMA-ES refinement (~1-2 hours)
- **Outer:** Optuna TPE over top 10,000 survivor masks from Stage 2.5
- **Inner:** pycma 3.4 BIPOP-CMA-ES per mask over (SL, TP) normalized [0,1]²
  - Dimension d = 2
  - Population λ = 6 (4 + ⌊3 ln 2⌋)
  - Parents μ = 3
  - Initial step σ₀ = 0.3
  - TolX = TolFun = 1e-6
  - MaxFEvals per restart = 200
  - Restart strategy: BIPOP (Hansen GECCO 2009)
- Total inner budget: 10K masks × 200 evals = 2M backtest calls
- **CMA-ES must NOT cross discrete dimensions** — mask layer is OUTER only

### Stage 4 — Statistical filters (pipeline order matters)

1. **Top-K composite:** keep top 1,000 per (strategy_template, instrument, mode)
   from Stage 3
2. **Deflated Sharpe Ratio** (Bailey & López de Prado, JPM 2014):
   COMPUTE PER (instrument, TF) LEADERBOARD with N ≤ 10⁷, **NOT pool-wide**.
   Pool-wide N=10¹² gives threshold ≈ 7.4σ ≈ Sharpe 23 (impossible).
   Per-leaderboard N=10⁷ gives threshold ≈ 5.3σ ≈ Sharpe 2-3 (realistic).
3. **CPCV** (López de Prado, Adv ML in Finance 2018, Ch 12):
   S = 16 sub-periods, C(16,8) = 12,870 splits
   Embargo = max indicator lookback bars (NOT 1% of timeline)
4. **PBO via CSCV** (Bailey-Borwein-LdP-Zhu, JCF 2017)
5. **Romano-Wolf step-down** (Econometrica 73(4) 2005):
   USE THIS, NOT Bonferroni. At N=10K, Bonferroni gives p ≈ 5×10⁻⁶ →
   rejects everything real.
6. **Walk-Forward rolling** (NOT anchored):
   2-year train, 6-month test, 3-month slide
   ~16 walks across 5 years
   Keep candidates with positive Sharpe in ≥12 of 16 walks
7. **Lockbox:** reserve last 6 months (Dec 2025 - May 2026), NEVER seen
   by any prior stage. Code-enforced isolation: any access path outside
   Stage 4.7 raises exception. Top 10 candidates tested ONCE.
   Pass: lockbox Sharpe within 30% of training, MDD within 50% of training.

## Result Row Schema (Every Single Trial — 30+ Metrics)

**Sharpe alone is a noob trap.** Real institutional strategy evaluation
ranks on a composite of 12-15 metrics, with hard rejection gates BEFORE
composite scoring. TradingView's Strategy Report (the standard) shows
all of these — we compute every one of them per trial.

Every result row written to `stage2_results.parquet` carries the
following columns (paisa-int64 where money, float64 where ratio):

### Identity (hash inputs)
- `result_id` (blake3-low-128 of canonical params)
- `strategy_mask` (u64 bitmask)
- `direction` (enum: bull, bear)
- `instrument` (enum: NIFTY, BANKNIFTY, SENSEX)
- `tf` (enum: 1m..1d, 21 values)
- `mode` (enum: A, B)
- `sl_paisa`, `tp_paisa` (Mode A only, else NULL)
- `data_snapshot_hash`, `indicator_version`, `code_commit_hash`

### Return metrics
- `net_pnl_paisa` (int64)
- `total_return_pct` (float64)
- `cagr_pct` (float64, annualized)
- `gross_profit_paisa` (int64)
- `gross_loss_paisa` (int64, absolute value)

### Risk-adjusted return
- `sharpe_ratio` (float64)
- `sortino_ratio` (float64) — downside-only volatility
- `calmar_ratio` (float64) — CAGR / MaxDD
- `mar_ratio` (float64) — Calmar alternative
- `information_ratio` (float64) — excess vs buy-and-hold
- `omega_ratio` (float64) — at 0% threshold

### Drawdown
- `max_drawdown_pct` (float64)
- `max_drawdown_paisa` (int64)
- `max_drawdown_duration_bars` (int64)
- `recovery_factor` (float64) — net profit / MDD
- `ulcer_index` (float64) — time-weighted drawdown
- `largest_losing_trade_paisa` (int64)
- `max_consecutive_losses` (int32)
- `max_consecutive_wins` (int32)

### Trade quality
- `total_trades` (int64)
- `winning_trades` (int64)
- `losing_trades` (int64)
- `win_rate` (float64, fraction)
- `profit_factor` (float64) — gross_profit / gross_loss
- `avg_winning_trade_paisa` (int64)
- `avg_losing_trade_paisa` (int64, absolute)
- `win_loss_ratio` (float64) — avg_win / avg_loss
- `expectancy_paisa` (int64) — money per trade
- `avg_mfe_paisa` (int64) — Maximum Favorable Excursion
- `avg_mae_paisa` (int64) — Maximum Adverse Excursion

### Trade timing
- `avg_holding_time_bars` (float64)
- `max_holding_time_bars` (int64)
- `time_in_market_pct` (float64)

### Distribution robustness
- `tail_ratio` (float64) — p95 / |p5|
- `skewness` (float64)
- `kurtosis` (float64)
- `returns_std_dev` (float64)

### Statistical validity (Stage 4 only, NULL before then)
- `deflated_sharpe_ratio` (float64) — Bailey & LdP 2014
- `pbo_probability` (float64) — Bailey 2017
- `cpcv_pass_rate` (float64) — fraction of 12,870 splits with positive Sharpe
- `wfo_pass_rate` (float64) — fraction of 16 walks with positive Sharpe
- `lockbox_sharpe` (float64) — out-of-sample Sharpe (Dec 2025 – May 2026)
- `lockbox_mdd_pct` (float64)

### Hard rejection gates (applied BEFORE Stage 4 composite scoring)

A result is REJECTED (excluded from top-10) if ANY of:

| Gate | Threshold | Rationale |
|---|---|---|
| `total_trades` | < 30 | Insufficient sample (statistical noise) |
| `profit_factor` | < 1.2 | No real edge |
| `max_drawdown_pct` | > 30% | Uninvestable capital risk |
| `win_rate` < 25% AND `win_loss_ratio` < 2.0 | both true | Psychologically uninvestable |
| `max_consecutive_losses` | > 10 | Account-blower risk |
| `lockbox_sharpe / sharpe_ratio` (training) | < 0.5 | Overfit (out-of-sample collapse) |

### Composite ranking score (Stage 4)

After gates pass, rank by composite score:

```
composite_score(s) =
    0.25 × normalize(s.sharpe_ratio)
  + 0.20 × normalize(s.sortino_ratio)
  + 0.15 × normalize(s.calmar_ratio)
  + 0.15 × normalize(s.profit_factor − 1.0)
  + 0.10 × normalize(s.recovery_factor)
  + 0.10 × normalize(s.win_rate × s.win_loss_ratio)
  + 0.05 × normalize(s.lockbox_sharpe)
```

Where `normalize(x)` is per-(instrument, TF) leaderboard z-score
clamped to [-3, +3] (prevents single-metric outliers from dominating).

### Day 1 (Phase 0.5) computes a SUBSET

To fit ~30 metrics into Day 1's 16-20h budget, Day 1 computes:
- All Return metrics
- All Risk-adjusted return metrics (Sharpe, Sortino, Calmar)
- All Drawdown metrics (except Ulcer Index — added Day 7)
- All Trade quality metrics
- Hard rejection gates 1-5 (lockbox gate added Day 14+ when CPCV+lockbox lands)
- Simplified composite score (no lockbox term, weights normalized to sum to 1)

Day 1 output: top-10 strategies by Day-1-simplified composite, with
ALL 30+ metrics displayed in the JSON report and PNG charts.

## Non-Negotiable Correctness Requirements

1. **Money in int64 paisa.** NO floats in hot paths.
2. **Prices in int64 ticks** (₹0.05 = 5 paisa unit).
3. **UTC storage internal, IST display only.** Session: 03:45-10:00 UTC.
4. **No look-ahead:** bar N signal → bar N+1 open entry. Instrumented
   bar accessor catches violations at runtime (debug build only).
5. **Precise fills (corrected 2026-05-16):** entry at bar N+1 OPEN
   (exact value); exit at bar M+1 OPEN (exact value). NO pessimistic
   fill on spot. Operator clarified: spot indices (NIFTY/BANKNIFTY/
   SENSEX) are computed averages and are NOT directly tradable — there
   is no order book to slip against, so pessimistic fill is fiction.
   Pessimistic-fill convention applies ONLY to Track 2 options (where
   real order books exist).
6. **NO slippage on spot (corrected 2026-05-16):** spot indices have
   no executable venue, so any slippage adjustment is meaningless.
   Track 1 backtest uses the exact bar OPEN price at entry and exit.
   Slippage applies ONLY in Track 2 options per
   `docs/track2/TRACK2_OPTIONS_SPEC.md` §9 (delta-bucketed × ToD ×
   VIX). At 1-minute granularity we never have tick-level access,
   so spot's "1-tick adverse" rule (from v1 spec, now retired) is
   doubly inappropriate: no venue + no tick data.
7. **NO brokerage / NO STT / NO costs.** Track 1 is signal research only
   (operator confirmed 2026-05-16). Track 2 handles realism via expired
   options data (see `docs/track2/`).
8. **Forced 15:20 IST exit.** NO overrides ever.
9. **Every result row hashed:**
   ```
   blake3(strategy_mask ‖ direction ‖ instrument ‖ tf ‖ mode ‖
          canonical_params ‖ data_snapshot_hash ‖ indicator_version ‖
          code_commit_hash)
   ```
10. **Audit chain:** hash-chained JSONL log, BLAKE3 verifies end-to-end.
11. **FPCR rounding mode** pinned to round-nearest-even at process start
    in both Rust and Python paths. Don't touch FPCR afterwards.
12. **Subnormal flush-to-zero (FZ bit)** explicitly DISABLED for
    scalar/vector consistency.
13. **Debug builds:** enable `feenableexcept(FE_DIVBYZERO | FE_INVALID |
    FE_OVERFLOW)` to trap on NaN.
14. **Reproducibility:** same inputs → bit-identical outputs across
    rebuilds of same toolchain version.

## Indian Market Calendar (Phase 1 Deliverable)

Build `brutex/calendars/nse.py` as first-class ~500-line module:
- Full holiday list 2021 through 2026
- Muhurat sessions (Diwali ~1 hour; **EXCLUDE from Sharpe denominator**)
- Pre-open auction 09:00-09:15 (EXCLUDE; first usable bar 09:16-09:17)
- Semi-annual rebalance dates (March, September)
- Half-day flags
- Forced exit time: 15:20 IST
- Tick size: ₹0.05 (5 paisa) throughout 2021-2026 — no discontinuity

## Phased Build Plan (16 weeks + Day 1 Sprint)

> **Phase 0.5 — Dopamine Day** runs FIRST (operator instruction 2026-05-16:
> "results give me dopamine and confidence — that's my only energy").
> Day 1 produces real top-10 strategies on real data via a fast
> Python+Polars implementation. The 16-week plan (Phase 1-10) then
> rebuilds the same pipeline properly in Rust with full statistical
> rigor — but Day 1 is the smell test that proves the project is alive.
>
> **Phase 1 (Week 1-2) covers ALL 3 instruments × ALL 21 timeframes
> from day one** (operator instruction 2026-05-16). The pilot in
> Phase 9.5 is also full-coverage on instruments and TFs (only the
> mask vocab and SL/TP grid are reduced).

### **Phase 0.5: DOPAMINE DAY (Day 1, 16-20h focused work)** ⭐⭐⭐

Maximum-aggressive Day-1 sprint to produce real strategy results in
one day. Trade-off: Python+Polars (slower) instead of Rust (faster
to write); crude rigor (raw Sharpe ranking, no DSR/CPCV/PBO/RW); no
lockbox isolation barrier (just visual holdout). The 16-week plan
rebuilds all of this properly.

**Day 1 scope:**

| Dimension | Day 1 | Phase 9.5 pilot | Full Phase 10 |
|---|---|---|---|
| Instruments | 3 (all) | 3 | 3 |
| Timeframes | 21 (all) | 21 | 21 |
| Masks | **1,000** random K=2..4 | 294K | 2.9M |
| SL/TP grid | **100 cells** (10×10) | 700 | 2,000 |
| Directions | 2 | 2 | 2 |
| Modes | **Mode A only** | A + B | A + B |
| Total trials | **~12.6M** | ~52B | 730B |
| Wall time | **~30-60 min sweep + 16h setup** | 1-2h | 14-20h |

**Day 1 hour-by-hour:**

```
Hour 0-2   ──  Python skeleton + Dhan token wired + first 1m batch fetched
Hour 2-4   ──  Full 5y data for all 3 instruments (5 min API time after token works)
               Stored as Parquet + ZSTD
Hour 4-6   ──  Resample 1m → all 21 TFs (Polars groupby + agg)
               Build calendar module (holidays, Muhurat, pre-open)
Hour 6-10  ──  Implement 52 atomic conditions in Polars vectorized
               Precompute bitmap matrix for each (inst, TF) — 63 matrices
Hour 10-14 ──  Backtest engine in Python (Polars-vectorized, no per-bar loop)
               Test on 10 hand-coded scenarios → confirm correctness
Hour 14-16 ──  Run sweep: ~12.6M trials, ~30-60 min wall
Hour 16-18 ──  Compute Sharpe / MDD / hit-rate per result, rank top 10
Hour 18-20 ──  Generate PnL curves (matplotlib), output JSON + 10 PNG charts
```

**Day 1 deliverable:**
- `/Volumes/QuantData/results/day1/top10_strategies.json`
- 10 PNG cumulative-PnL charts at `/Volumes/QuantData/results/day1/charts/`
- A real table: rank | mask | inst | TF | Sharpe | MDD | win rate | sample size
- Operator confidence: "the engine works, the data is good, patterns exist"

**Day 1 VALIDATION GATE (relaxed — this is a smell test):**
- Sweep completes without crash
- No NaN/inf in result rows
- Top 10 each have ≥ 30 trades over 5y (statistical floor)
- Top-1 Sharpe > 0.5 (anything less means the vocab is broken or data is bad)
- Save report to `gate0_5_dopamine_report.json`

**What Day 1 explicitly skips (added on Day 2+):**
- Rust hot-path (Python is 100× slower but 100× faster to write)
- DSR / CPCV / PBO / Romano-Wolf (statistical rigor, Day 14+)
- Lockbox isolation barrier (Day 21+ — Day 1 uses last 6 months visual-only)
- BIPOP-CMA-ES refinement (Day 14+)
- Audit chain + blake3 + DVC + OpenTimestamps (Day 7+)
- Full 2.9M masks (Day 1 = 1K random sample)
- Full 2,000 SL/TP cells (Day 1 = 100-cell coarse grid)
- Mode B signal-exit (Day 1 = Mode A only)
- 100% test coverage gates (Day 1 = sanity tests on 10 hand-coded scenarios)

**Prerequisites the operator must provide before Phase 0.5 starts:**
- ⬜ `DHAN_ACCESS_TOKEN` environment variable set (Dhan account + token issued)
- ⬜ `/Volumes/QuantData` mounted (or alternative path specified)

If both green → Day 1 sprint executes in one continuous session.

---

### Phase 1: Foundation (Week 1-2)
- Rust workspace skeleton + Python package via PyO3
- Dhan API client with 5 req/s rate limit, exponential backoff on 429
- Fetch 5-year 1-minute data for **NIFTY, BANKNIFTY, SENSEX** (all 3)
- Resample to **all 21 timeframes** anchored to 09:15 IST
- Store as Parquet+Zstd9 at `/Volumes/QuantData/raw/{instrument}/`
- Build `brutex/calendars/nse.py`

**VALIDATION GATE 1:**
- 50 random bars match TradingView within 0.05% on prices
- All 21 timeframes generated, no partial bars
- NSE holiday list complete 2021-2026
- Save report to `/Volumes/QuantData/validation/gate1_report.json`

### Phase 2: Indicator Precompute (Week 3-4)
- Implement 52 atomic conditions in Rust with NEON intrinsics
- Pack as bitmap matrix per (instrument, TF) — 63 matrices
- Stage 1A cache: Parquet columns alongside OHLCV
- Daily-anchored indicators (PDH, PDL, Pivots, Fib) computed from daily
  Parquet, constant across day for all TFs

**VALIDATION GATE 2:**
- 10 random timestamps per condition match manually computed values
  exactly (zero tolerance on boolean conditions)
- Save report to `gate2_report.json`

### Phase 3: Single-Backtest Engine (Week 5-6)
Modules: `data_loader`, `bar_iterator`, `signal_evaluator`,
`position_manager`, `fill_simulator`, `cost_calculator` (zero-cost in
Track 1), `metrics_computer`, `result_writer`.
- 12-worker concurrency model
- Build reference test suite: 100 hand-coded scenarios with expected PnL

**VALIDATION GATE 3:**
- All 100 reference scenarios match expected PnL within 1 paisa
- Memory-mapped data shared across workers
- Per-worker peak memory ≤ 500 MB
- Save report to `gate3_report.json`

### Phase 4: TradingView Reference Validation (Week 7)
- Build TradingView replay tool: 20 random combinations
- Manually replay each on TradingView, record entry/exit/PnL
- Engine must match within 0.5%

**CRITICAL VALIDATION GATE 4:**
- All 20 TradingView replays match within 0.5%
- **DO NOT PROCEED TO PHASE 5 IF ANY FAILS**
- Save report to `gate4_report.json`

### Phase 5: Single-Stage Sweep Benchmark (Week 8-10)
- Run Stage 2 monolithically on SMALL slice:
  1 instrument × 1 TF × 10,000 random masks × Mode A only
- Measure: trials/sec, memory peak, per-mask scan time
- Compare to theoretical bandwidth-bound estimate (~5 ms/scan)

**VALIDATION GATE 5:**
- Measured throughput within 30% of theoretical estimate
- No NaN/inf in any result row
- Reproducible bit-for-bit on rerun
- Save benchmark to `gate5_report.json`

### Phase 6: 6-Hour Calibration Sweep (Week 11)
- 1 instrument × 3 TFs × 50,000 masks × Mode A
- Continuous 6-hour run under powermetrics logging:
  `powermetrics --samplers smc,cpu_power,gpu_power -i 60000`
- Open lid, vertical stand, 22°C ambient, Macs Fan Control aggressive
  (3500-5500 RPM)
- Document M4 Pro sustained-load behavior

**VALIDATION GATE 6:**
- No thermal throttle observed (package temp < 95°C sustained)
- No crash, no resource leak (memory steady-state)
- Checkpoint resume tested mid-sweep, results bit-identical
- Save thermal log + benchmark to `gate6_report.json`
- **THIS IS YOUR M4 PRO 6-12 HOUR CORPUS — NO PUBLIC EQUIVALENT EXISTS**

### Phase 7: Statistical Filters (Week 12-13)
- Deflated Sharpe per (instrument, TF) leaderboard
- CPCV with S=16, embargo = max indicator lookback
- PBO via CSCV
- Romano-Wolf step-down
- Walk-Forward rolling 2y/6m/3m slide
- Lockbox isolation enforced at code level (assert exception)

**VALIDATION GATE 7:**
- Known-overfit synthetic strategy correctly rejected by DSR + PBO
- Known-real synthetic strategy correctly accepted
- Lockbox access from outside Stage 4.7 raises Exception
- Save report to `gate7_report.json`

### Phase 8: Stage 3 Optimizer (Week 14)
- Optuna TPE outer over masks
- pycma 3.4 BIPOP-CMA-ES inner over (SL, TP)
- Hyperparameters as locked above

**VALIDATION GATE 8:**
- Synthetic 2D Rosenbrock function optimized to f(x) < 1e-4 in <100 evals
- Convergence reproducible across reruns
- Save report to `gate8_report.json`

### Phase 9: Full Funnel Integration (Week 15)
- End-to-end: Phase 2 indicators → Phase 5 sweep → Phase 7 filters →
  Phase 8 refinement → top 10 strategies
- Resumable checkpoints every 100M trials
- Hash-chained JSONL audit log
- Daily Parquet seal: BLAKE3 root + OpenTimestamps
- DVC pipeline with content-addressed data versioning
- GPG-signed git commits for all code state

**VALIDATION GATE 9:**
- Full pipeline runs end-to-end on small slice (1 inst × 3 TF × 100K masks)
- Deterministic: rerun produces identical result hashes
- Audit chain verifies end-to-end via blake3
- Save report to `gate9_report.json`

### **Phase 9.5: PILOT SWEEP (~52B trials, ~1-2h) — NEW** ⭐

> Operator instruction 2026-05-16: do a smaller pilot before committing
> to the full 730B Phase 10 sweep. Pilot must cover **all 3 instruments
> × all 21 timeframes** from day one — only mask vocab and SL/TP grid
> are reduced.

**Pilot configuration:**
| Dimension | Pilot value | Phase 10 (full) value | Reduction |
|---|---|---|---|
| Instruments | **3 (all)** | 3 | 1× (full coverage) |
| Timeframes | **21 (all)** | 21 | 1× (full coverage) |
| Mask K range | **K=2..4** (294K masks) | K=2..5 (2.9M masks) | 10× |
| SL/TP grid | **700 cells** (25 SL × 28 TP) | 2,000 cells (40 × 50) | ~2.9× |
| Directions | 2 (bull, bear) | 2 | 1× |
| Modes | 2 (A, B) | 2 | 1× |
| **Total trials** | **~52B** | 730B | **14×** |
| Wall time | ~1-2h | 14-20h | ~10× |

**VALIDATION GATE 9.5 (PILOT):**
- Sweep completes without crash
- Measured throughput within 30% of bandwidth-bound projection
- Top 10 pilot candidates pass lockbox (Sharpe within 30% of training, MDD within 50%)
- Audit chain verifies end-to-end via blake3
- Wall time matches 1-2h projection (±50%)
- No NaN/inf in any result row
- Save report to `gate9_5_pilot_report.json`

**Decision branches after Phase 9.5:**
- **Pilot finds ≥3 candidates with lockbox Sharpe > 1.5** → consider stopping;
  hand off top-10 to Track 2 for options realism. Skip Phase 10 unless
  operator demands wider coverage.
- **Pilot finds 1-2 candidates** → consider Phase 10 only for the
  surviving (instrument, TF) regions, not the full grid.
- **Pilot finds 0 candidates** → diagnose (vocab insufficient? data wrong?
  filters too strict?). Do NOT auto-proceed to Phase 10. Operator review.

### Phase 10: Full 730B Sweep (Week 16, CONDITIONAL on Phase 9.5)
- Full 730B trials with 5-stage funnel — only if Phase 9.5 outcome
  justifies it
- Target wall time: 14-20 hours
- Run under: `caffeinate -d -i -m -s -u brutex-sweep --config production.yaml`
- powermetrics logging continuous to JSONL
- Auto-pause triggers:
  - Package temp > 95°C sustained 5 min → pause, drop to 4 cores
  - System wall draw > 100W sustained 10 min → pause
  - Battery discharging (`pmset -g batt`) → immediate pause
- Save sweep state every 100M trials to 8TB SSD
- Top 10 strategies → JSON config for Track 2 options realism leg

**VALIDATION GATE 10:**
- Sweep completes without crash
- Top 10 strategies all pass lockbox test
- Audit chain verifies
- Total wall time within 14-24 hour band
- Save final report to `gate10_final.json`

## Safety + Kill Switch

```rust
struct StageBudget {
    stage: Stage,
    max_wall_sec: u64,
    max_memory_gb: f32,
    max_trials: u64,
    checkpoint_every_n_trials: u64,
}

const STAGE_1B: StageBudget = StageBudget {
    stage: Stage::CoarseScore,
    max_wall_sec: 30 * 60,
    max_memory_gb: 8.0,
    max_trials: 200_000_000,
    checkpoint_every_n_trials: 10_000_000,
};
```

**Watchdog (`brutex doctor` adjacent):**
- Main writes heartbeat JSON to `/tmp/brutex_heartbeat.json` every 30s
- `brutex doctor` polls every 60s
- Stale > 5 min → SIGTERM
- After 30s → SIGKILL
- `shutdown_clean()` flushes Parquet, closes LMDB, BLAKE3-chains partial results

**NaN / overflow detection:**
- int64: `checked_add/sub/mul` → log + skip + overflow_counter on `None`
- f64: `is_finite()` on every accumulation; non-finite → NaN bucket dispatch
- AArch64 FPCR: enable IXE trap in debug builds only

## Storage Layout (8TB SSD)

```
/Volumes/QuantData/
├── raw/
│   ├── NIFTY/
│   │   ├── 1min_2021_2026.parquet
│   │   └── daily_2021_2026.parquet
│   ├── BANKNIFTY/...
│   └── SENSEX/...
├── resampled/
│   └── {instrument}/{tf}.parquet           # 21 TFs × 3 instruments = 63 files
├── indicators/
│   └── {instrument}_{tf}_indicators.parquet # 52 conditions packed
├── stages/
│   ├── stage1b_survivors.parquet
│   ├── stage2_results.parquet              # streaming append
│   ├── stage2_5_pruned.parquet
│   ├── stage3_refined.parquet
│   └── stage4_lockbox/                     # access-controlled
├── audit/
│   └── {run_id}/chain.jsonl                # hash-chained, BLAKE3-sealed
├── checkpoints/
│   └── {run_id}/chunk_{N}.json
├── validation/
│   └── gate{N}_report.json                  # 1..10 + 9_5_pilot
├── results/
│   ├── pilot_top10_strategies.json
│   └── final_top10_strategies.json
└── dvc-cache/                              # content-addressed datasets
```

## Reproducibility Trail

Every result row tagged with:
```
blake3(strategy_mask ‖ direction ‖ instrument ‖ tf ‖ mode ‖
       canonical_params ‖ data_snapshot_hash ‖ indicator_version ‖
       code_commit_hash)
```

Where:
- `data_snapshot_hash` = blake3 of input Parquet bytes (DVC-tracked)
- `indicator_version` = semver from `pyproject.toml`
- `code_commit_hash` = git SHA, GPG-signed

**Daily seal:** blake3 of all run Parquet files → OpenTimestamps commit.

## Working Style

1. After EACH phase, pause for operator to verify the validation gate.
2. Do not proceed to next phase if validation gate fails.
3. Document every non-obvious decision in code comments with research citation.
4. Run benchmarks on the actual M4 Pro and report measured numbers, not
   theoretical claims.
5. Flag any deviation from research recommendations explicitly with reason.
6. Build incrementally — do not skip phases.
7. If a requirement conflict is discovered, STOP and ask. Do not guess.

## Hand-off to Track 2

Phase 9.5 (or Phase 10, if executed) produces a top-10 list. That list
is the input to Track 2's Phase 4 sweep (`docs/track2/phases/phase_4_first_sweep.md`).
Track 2 takes each (mask, direction, instrument, TF, mode, params) tuple
and translates it into priced option trades using expired-options data,
applying realistic brokerage / STT / STO / GST / slippage. Track 2 does
NOT modify the spot signal — only adds the realism leg.

## Decision Ledger Linkage

This spec adds the following decision rows to `docs/MIGRATION.md`
(numbering starts at #300 to leave room for v1 amendments #103-#299):

- #300 — Track 1 v2 spec adopted (this doc); v1 archived to `docs/archive/`
- #301 — Track 1 = signal-only, NO futures, NO costs (operator 2026-05-16)
- #302 — Phase 9.5 pilot inserted (~52B, all 3 inst × all 21 TFs, K=2..4, 700 SL/TP cells)
- #303 — Tech stack version locks (Rust 1.78, Polars 1.18, Optuna 4.1, pycma 3.4, etc.)
- #304 — Stage 1B coarse-score formula locked
- #305 — DSR threshold per (instrument, TF) leaderboard, N≤10⁷ (not pool-wide)
- #306 — BIPOP-CMA-ES hyperparameters locked (λ=6, μ=3, σ₀=0.3)
- #307 — Lockbox window = Dec 2025 – May 2026
- #308 — `/Volumes/QuantData` 8TB SSD partition layout
- #309 — FPCR pinning + FZ bit DISABLED + feenableexcept on debug
- #310 — Audit chain via blake3 + OpenTimestamps daily seal + DVC content-addressed

## First Action (Before Writing Any Code)

Operator approved 2026-05-16 ("go ahead"). Implementation proceeds with:

1. ✅ Folder structure (per Phase 0 PR proposal)
2. ✅ Cargo.toml + pyproject.toml + uv.lock approach (per Phase 0 PR proposal)
3. ✅ Dhan API test call (per `scripts/dhan_test_call.sh`)
4. ✅ 90-day chunking + rate limit handler (per design above + `TRACK1_DATA_INGEST_DESIGN.md`)
5. ✅ 8TB SSD partition plan (per `Storage Layout` section)
6. ✅ Validation harness skeleton for Phase 1 (per `brutex/validation/gate1.py` design)

Phase 1 implementation begins on branch `claude/track1-phase-1-foundation-VXTP1`
after this spec PR merges. Phase 1 exits on Validation Gate 1 sign-off.
