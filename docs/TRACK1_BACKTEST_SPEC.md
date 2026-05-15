# Track 1 — Backtest System Build Specification

> **Status:** Locked spec, 2026-05-15. Supersedes the legacy
> `docs/REQUIREMENTS.md` (Groww v1.0) and the prior `TRACK1_BACKTEST_SPEC.md`
> draft. All decisions below survived a 4-agent hostile-review pass
> (Audit + Verification-Architecture + Hostile-Review + Throughput-Reality).
>
> **Active branch:** `claude/backtest-system-spec-ITI4Y`
>
> Companion documents (read these together):
> - `TRACK1_VERIFICATION_PYRAMID.md` — 9-layer test pyramid + 24 invariants
> - `TRACK1_VOCABULARY.md` — the 52 atomic conditions
> - `TRACK1_PHASE_PLAN.md` — 8-12 week build plan
> - `TRACK1_PERFORMANCE_BUDGET.md` — honest throughput math
> - `TRACK1_MICROSTRUCTURE.md` — Indian-market edge cases
> - `MIGRATION.md` — Groww → Dhan migration log

---

## 1. Project Identity

**BruteX Track 1** is a brute-force backtesting engine that produces
**signal-only research output** on three Indian equity-index **spot**
series:

- **NIFTY 50** (NSE)
- **NIFTY BANK** / `BANKNIFTY` (NSE)
- **BSE SENSEX** / `SENSEX` (BSE)

The pipeline brute-forces ~2.9 million unique strategy patterns
(combinations of 2–5 atomic conditions from a 52-condition vocabulary)
× full parameter sweeps across 15 timeframes × 3 instruments × 2 exit
modes, applies a 4-filter cascade (DSR → CPCV → WFO → Lockbox), and
returns the top 3 production candidates per sweep.

**Out of scope (Track 1):** futures, options, F&O contracts, broker
execution simulation, live feed, WebSocket, multi-tenant SaaS,
commodity segment, BSE cash equity (other than the SENSEX index value
itself).

**Options Track (post-Track-1, future):** translates Track 1 winning
signals into ATM/ITM option fill simulations with IV/theta/spread
modeling — this is when execution realism (brokerage, STT, slippage)
re-enters. NOT to be confused with "Phase 2" of the Track 1 build plan
(see `TRACK1_PHASE_PLAN.md §Phase 2`), which is the Dhan adapter +
52-vocabulary work within Track 1.

---

## 2. Hardware Target

- **MacBook Pro M4 Pro**: 14-core CPU (10 Performance + 4 Efficiency), 20-core GPU, 48 GB RAM
- macOS Tahoe 26.x
- 8 TB external SSD mounted at `/Volumes/QuantData`
- Daily apps (Chrome, IntelliJ, Claude Desktop) run concurrently
- Sweep uses **10 cores (the P-cores)**; 4 E-cores reserved for OS + daily apps
- Working set capped at 40 GB RAM during sweep

**SLA per sweep on this envelope:** **2-4 days wall time** for ~33
billion trials. Faster hardware (64-128 core EPYC / Threadripper)
brings this to 12-24 hours. The spec must restate SLA per hardware
envelope. See `TRACK1_PERFORMANCE_BUDGET.md` for the memory-bandwidth
analysis that bounds these numbers.

---

## 3. Tech Stack (locked)

| Layer | Technology |
|---|---|
| Backtest hot kernel | Rust workspace (extends existing `rust/fno-math/`) |
| Parallelism | Rayon for CPU parallelism |
| SIMD | `std::simd` (portable SIMD; NEON on Apple Silicon) |
| GPU | Metal compute (Apple Silicon) for indicator precompute only |
| Python orchestrator | Python 3.12 |
| Python-Rust FFI | PyO3 + Arrow zero-copy |
| Optimizer (Stage 3) | Optuna (CMA-ES) |
| Storage | Parquet + Zstd-9 (results), LMDB (dedup index + work-item store), DuckDB (catalog + queries) |
| Validation | pytest + cargo test + hypothesis + proptest + pytest-benchmark + criterion + mutmut + cargo-mutants + cargo fuzz |
| Money type | `int64` paisa throughout |

---

## 4. Market Scope (locked)

| Aspect | Value |
|---|---|
| Instruments | NIFTY 50, BANKNIFTY, SENSEX (spot indexes only) |
| Tradability | None (Track 1 is signal-only) |
| Volume column | Zero / null (indexes don't trade) |
| Continuous-series stitching | N/A (spot is naturally continuous) |
| Range | 2021-05-15 → 2026-05-15 (5 years) |
| Trading session | 09:15 - 15:30 IST |
| Pre-open exclusion | Bars before 09:15:00 IST dropped at ingest |
| Forced exit | 15:20 IST (Track 1 convention) |
| Muhurat sessions | Excluded by hardcoded date list (see `TRACK1_MICROSTRUCTURE.md`) |
| Holiday calendar | NSE official 2021-2026 (resolved at ingest) |
| Index rebalance dates | Flagged in audit; data series is exchange-published (chain-linked) |

---

## 5. Data Acquisition (Dhan)

### 5.1 Endpoints

| Endpoint | Use |
|---|---|
| `POST /v2/charts/intraday` | 1-minute OHLC; 90-day chunks |
| `POST /v2/charts/historical` | Daily OHLC (for PDH/PDL anchors, calendar) |

### 5.2 Native granularity

**Dhan provides 1, 5, 15, 25, 60 minute and 1-day intraday natively.**
Track 1 fetches **1-minute only** and deterministically resamples to
all other timeframes from the 1m base, anchored to 09:15 IST. The 25m
and 60m natives are NOT used — we resample 1m for full parity across
all TFs and a single reproducibility hash.

### 5.3 Resampling rules

- Anchor origin to 09:15 IST each day
- Drop partial bars at session close (15:30 cutoff)
- Aggregate: `open = first`, `high = max`, `low = min`, `close = last`,
  `volume = sum` (always 0 for spot)
- Each TF stored as separate Parquet file partitioned by (instrument, date)

### 5.4 securityId resolution

`securityId` and `exchangeSegment` are **resolved at runtime** from
Dhan's instrument master at
`https://images.dhan.co/api-data/api-scrip-master-detailed.csv`. The
adapter caches the snapshot hash. **Never hardcode** — IDs can change
on master CSV revisions.

Expected (verify at first call):
- NIFTY 50: segment `IDX_I`, symbol `NIFTY 50`
- BANKNIFTY: segment `IDX_I`, symbol `NIFTY BANK`
- SENSEX: segment `BSE_I` (BSE indices segment — verify code), symbol `SENSEX`

### 5.5 Rate limiting

Dhan rate limit is **UNVERIFIED**. Until the first calls confirm the
actual throttle, the adapter caps at **10 req/s effective** with the
standard 0.6 safety factor. Once verified, the constant moves to a
single source in `brutex/adapters/dhan/constants.py` and the cap is
revisited.

---

## 6. Timeframes (15 derived)

All resampled from 1m base, anchored to 09:15 IST:

| Group | Timeframes |
|---|---|
| Minutes | 1m, 2m, 3m, 5m, 10m, 15m, 20m, 30m, 45m |
| Hours | 60m (1h), 90m, 120m (2h), 180m (3h), 240m (4h) |
| Daily | 1d |

**15 total** — corrected from the earlier "21 timeframes" claim which
double-counted, included a non-native 25m, and used inconsistent
groupings. The hostile review caught this.

---

## 7. Indicators (9 precomputed)

| # | Indicator | Notes |
|---|---|---|
| 1 | EMA 20 (close) | per-TF |
| 2 | EMA 200 (close) | per-TF |
| 3 | Day High | running intraday; `day_high[t] = max(day_high[t-1], high[t])` — no resample leak |
| 4 | Day Low | symmetric to Day High |
| 5 | PDH (Previous Day High) | from daily data; **previous trading day** (skips holidays) |
| 6 | PDL (Previous Day Low) | symmetric to PDH |
| 7 | Daily Pivots (P, R1, R2, R3, S1, S2, S3) | computed from PDH/PDL/PDC, pure function — no intraday leak |
| 8 | Static Daily Fib levels (12 computed, 11 in vocabulary) | anchor: previous-day H→L; extensions in trade direction; computed levels: 0, 23.6, 38.2, 50, 61.8, 78.6, 100, 127.2, 161.8, 200, 261.8, 423.6. The 423.6% extension is computed but reserved for Options Track (rarely hits on 5y spot data) — see `TRACK1_VOCABULARY.md §5`. |
| 9 | Candle metadata | color (bull/bear/doji defined by close vs open), body_pct, upper_wick_pct, lower_wick_pct |

**VWAP intentionally dropped** — indexes have no volume.

All indicators are precomputed once per (instrument, timeframe) and
stored as additional columns in the same Parquet file as OHLCV bars.
Cache key: `sha256(data_version ‖ indicator_version)`.

---

## 8. Strategy Vocabulary (52 atomic conditions)

The strategy space is a **brute force over condition combinations**,
not a hand-picked strategy list. The 52 atomic conditions are organised
into 9 groups (EMA position 6, pivot proximity 7, PDH/PDL 6, Fib
proximity 11, candle shape 7, prior bar pattern 3, day structure 4,
time of day 4, day type 4).

**A "strategy" = pick K conditions (K ∈ {2,3,4,5}) and AND them.**

- AND only (no OR / mixed boolean)
- Per-instrument direction parameter (bull/bear) — same condition set
  evaluated in both directions
- Mutually-exclusive combinations (e.g., `close > EMA20 AND
  close < EMA20`) pruned at generation time via a conflict graph
- Effective pattern count after pruning: ~1.2-1.5M (down from raw 2.9M)

### Tolerance / proximity parameters

All "near" conditions use **basis points (bps) relative to current
close**, NOT absolute points. This is critical: a 10-point tolerance
on NIFTY ≈ 4 bps but on SENSEX ≈ 1.3 bps — same number is a different
signal.

- `fib_proximity_bps`: 5, 10, 25, 50 bps
- `pivot_proximity_bps`: 5, 10, 25, 50 bps
- `ema_proximity_bps`: 5, 10, 25, 50 bps
- `pdh_retest_dip_bps`: 0, 5, 10, 25 bps
- `pdh_retest_close_above_bps`: 5, 10, 25 bps

See `TRACK1_VOCABULARY.md` for the full enumeration with pseudocode
for each of the 52 atomic conditions.

---

## 9. Exit Modes (both brute-forced in parallel)

### Mode A — SL/TP-based (~2.76M params per pattern)

| Param | Values |
|---|---|
| SL points (Stage 2 coarse) | 10-300 step 25 (12 values) |
| TP points (Stage 2 coarse) | 30-1500 step 50 (30 values) |
| SL refine (Stage 3) | 1-pt grid around winners |
| TP refine (Stage 3) | 1-pt grid around winners |
| SL type | fixed / trailing (2 values) |
| Confirm candles | 1, 2, 3 (3 values) |
| Entry window | early / mid / midday / afternoon / all (5 values) |
| Proximity tolerance | 4 values (see above) |
| Position size | 1, 2, 3, 5% (4 values) |
| Max trades/day | 1, 2, 3, 5 (4 values) |
| Min gap | 0, 15, 30, 60 min (4 values) |

Exit triggers: SL hit, TP hit, or 15:20 forced exit (whichever fires first).

### Mode B — signal-based exit (~500K params per pattern)

Exit vocabulary (1 or 2 exit conditions, ORed):

1. Opposite entry condition fires
2. 1-bar candle color reversal
3. 2-bar candle reversal
4. EMA 20 cross opposite
5. EMA 200 cross opposite
6. Pivot break opposite (R1/S1)
7. PDH/PDL break opposite
8. Fib level break opposite
9. Time-based: N bars elapsed (N ∈ {10, 30, 60, 120, 240})
10. **Forced 15:20 IST close (always active)**

---

## 10. Entry Rule (carved in stone)

```
ALWAYS on bar CLOSE.
NEVER on bar touch / mid-bar.
Signal at bar N → enter at bar (N+1) OPEN.
```

**Definitions:**

- "Bar is bullish" = `close > open` (not `close > prev_close`)
- "Prior N bars bullish" = N parametrised (N ∈ {1, 2, 3, 4})
- "Near level X within K bps" = `abs(bar.close - X) ≤ X × K / 10000`

---

## 11. Fill Model (signal-only)

| Aspect | Value |
|---|---|
| Fill price | bar OPEN of (signal_bar + 1) |
| Slippage | ZERO |
| Pessimistic fill | NONE |
| Brokerage | **NONE** — spot is not tradable |
| STT | **NONE** |
| GST | **NONE** |
| Stamp duty | **NONE** |
| PnL column | `paper_pnl_paisa` (single column, signal-only honest paper) |
| Position size unit | % of notional capital; lot-rounded reserved for the Options Track |

Execution realism (brokerage, STT, slippage, spread, IV decay, theta)
re-enters in the **Options Track** (post-Track-1) when Track 1 winners
are translated to option contracts. Distinct from Track 1's own
"Phase 2" build phase (Dhan adapter + vocabulary; see `TRACK1_PHASE_PLAN.md`).

---

## 12. Search Pipeline (5 stages)

```
STAGE 1A — Pattern discovery (NIFTY 15m only)
   ~5.8M trials, ~90 sec wall
   → top 50,000 patterns

STAGE 1B — Pattern expansion across all 45 (inst × TF) combos
   Top 50K × 45 inst-TF × default params
   ~2.25M trials, ~40 sec wall
   → top 10,000 (pattern × inst × TF × direction) combos

STAGE 2 — Full parameter brute force
   Top 10K × Mode A (2.76M params) = 27.6B trials
   Top 10K × Mode B (500K params) = 5.0B trials
   ~33 BILLION trials total
   ~2-4 DAYS wall on M4 Pro at 80-150K trials/sec sustained
   (memory-bandwidth bound; see TRACK1_PERFORMANCE_BUDGET.md)
   → top 1,000 combos

STAGE 3 — Bayesian fine refinement
   Top 1K × CMA-ES with 1-pt SL/TP precision
   ~50K trials, ~15-20 min wall
   → top 100 combos

STAGE 4 — 4-filter cascade
   Top 100 → DSR (effective-N, cluster-adjusted) → CPCV → WFO → Lockbox
   ~30-45 min wall
   → FINAL 3 selected
```

**Effective N for DSR** uses Bailey & López de Prado correlation
clustering: patterns that share ≥ 3 of K conditions are clustered, and
the cluster counts as 1 trial. Typical effective N: 10K-100K, not 33B.
This makes the DSR threshold achievable for genuine-edge strategies.

---

## 13. O(1) Guarantees (proven in CI)

Every hot path is provably O(1). Bench tests at N ∈ {1K, 10K, 100K,
1M} with hard assertion `p99(N=1M) / p99(N=1K) ≤ 1.1`.

| Operation | Mechanism |
|---|---|
| Bar lookup by ts | Pre-sorted u64 index in mmap |
| Indicator value lookup | Column array index |
| Strategy mask evaluation | Bitwise AND on u64 |
| Combo dedup pre-check | Bloom filter (~5ns) |
| Combo dedup confirm | LMDB B+tree get (~50µs amortised) |
| Result Parquet append | Buffered streaming writer (~100ns amortised) |

See `TRACK1_VERIFICATION_PYRAMID.md §5` for the full bench-test design.

---

## 14. Dedup & Uniqueness

```
combo_hash = SHA256(
    strategy_mask_u64     ‖  // which of 52 conditions
    direction_bit         ‖  // bull/bear
    instrument_id         ‖  // NIFTY/BANKNIFTY/SENSEX
    timeframe_id          ‖  // 1m to 1d (one of 15)
    mode_byte             ‖  // A or B
    canonical_params_json ‖  // sorted keys, NaN-rejected
    data_version_hash     ‖  // SHA of input Parquet files
    indicator_version     ‖  // bump on indicator code change
    code_commit_hash         // current git HEAD
)
```

- Bloom filter (~5GB at 0.1% FPR, ~14 bits/element) for fast negative
  membership check
- LMDB (~30 GB at 33B keys, but in practice we only store **survivors**
  above a result-threshold, dropping it to ~250 MB)
- Lookup path: Bloom check → LMDB confirm (both O(1))
- Timeframe is part of the hash: same pattern + params on 5m vs 15m
  are **different combos** with different rows

---

## 15. Hash-Chained Audit Log

Every material event (ingest chunk start/end, stage start/end, error,
quarantine, kill) appends ONE line to `audit/audit.jsonl`:

```
{
  "ts": "...",
  "kind": "...",
  "data": {...},
  "prev_sha256": "<hash of prior line bytes>",
  "this_sha256": "<hash of this line w/o this_sha256 field>"
}
```

Replay test (oracle): re-running the audit log through the orchestrator
MUST produce a byte-identical Parquet output. SHA256 mismatch fails CI.

Selective logging: at 33B trials per sweep, NOT every trial logs to
audit. Only material events (pattern_discovered, stage_complete,
top_1k_winners, filter_decisions, errors). The full per-trial result
goes to Parquet, not audit.

---

## 16. Storage Strategy

| Item | Size |
|---|---|
| Raw 1m OHLC (5y × 3 inst) | ~20 MB compressed |
| Resampled 15 TFs | ~80 MB compressed |
| 9 indicators precomputed | ~80 MB compressed |
| **Result Parquet (threshold-filtered)** | **~50-100 GB per sweep** |
| `failures.parquet` (audit completeness) | ~500 MB per sweep |
| LMDB dedup index | ~250 MB |
| Optuna SQLite (Stage 3 only) | ~500 MB |
| Audit JSONL (selective) | ~50 MB |
| **Per-sweep total** | **~50-100 GB** |
| 8 TB SSD capacity | ~80-160 sweeps |

Threshold filter (drops 95% of result rows that scored too poorly to
ever survive the filter cascade) keeps daily iteration tractable.

---

## 17. Indian Microstructure Handling

Documented in detail in `TRACK1_MICROSTRUCTURE.md`. Highlights:

- Pre-open 09:00-09:15 bars dropped at ingest
- 15:20 forced exit (Track 1 convention)
- Muhurat dates excluded by hardcoded list (Samvat 2078-2083)
- NIFTY 50 reconstitution dates flagged in audit
- Circuit breaker halts: drop the full day; do not carry signals
- Half-day sessions (Ram Navami etc.) honoured by calendar

---

## 18. Verification (the path to 99.9% assurance)

See `TRACK1_VERIFICATION_PYRAMID.md` for the full 9-layer pyramid:

1. Unit tests (100% line + branch on Python, 100% line + 95% branch on Rust)
2. Property tests (24 invariants, hypothesis + proptest, 500 examples each)
3. Golden tests (byte-equal SHA256 against fixtures)
4. Integration tests (audit-log replay, Python-Rust parity, 52-condition spec table)
5. Benchmark tests (O(1) ratio ≤ 1.1 at all hot paths)
6. Stress tests (24h sustained, RSS ±5%)
7. Smoke tests (<30s pre-push)
8. Fuzz tests (cargo fuzz + atheris, 10 min/target nightly)
9. Mutation tests (mutmut + cargo-mutants ≥ 85%)

**The 3 oracle tests** that catch impl bugs:

- **Audit-log replay** → byte-identical Parquet (proves reproducibility)
- **Python-Rust parity** → naive ref engine ≡ Rust engine on 1-day/50-combo input (proves correctness)
- **52-condition spec table** → engine returns exactly the hardcoded boolean for each atomic condition (proves vocabulary semantics)

---

## 19. Phase Plan

Detailed in `TRACK1_PHASE_PLAN.md`. Summary:

| Phase | Duration | Deliverable |
|---|---|---|
| 0 | This week | Documentation, REQUIREMENTS deprecation, wipe |
| 1 | 2-3 weeks | Verification scaffolding (9-layer pyramid, 24 invariants, 3 oracle tests) |
| 2 | 3-4 weeks | Dhan adapter + spot-index ingest + 52-vocabulary |
| 3 | 2-3 weeks | Brute-force engine (Stage 1A/1B/2/3) |
| 4 | 1-2 weeks | Filter cascade (Stage 4) + first production sweep |

**Total: 8-12 weeks** to ship Track 1 with 99.9% verification.

---

## 20. Final Locked Decisions (one-line ledger)

The full decision list (70 items) is mirrored in `MIGRATION.md`.
Highlights:

1. Spot indexes only, signal-only research, no broker theatre
2. Dhan API; 1m fetched, 14 additional TFs resampled (15 total inc. 1m)
3. 9 indicators, no VWAP
4. 52 atomic conditions, K=2..5 ANDed, conflict-graph pruned
5. ~1.2-1.5M unique patterns (post-prune) × 2 directions
6. Bull/bear paired as a direction parameter
7. AND only; no OR
8. Two modes: A (SL/TP), B (signal-exit) — parallel sweep
9. Entry: bar (N+1) open after candle close signal
10. PnL: `paper_pnl_paisa` only; brokerage/STT/GST/stamp REMOVED
11. Tolerances in basis points (not absolute points)
12. DSR with effective-N (cluster-adjusted, Bailey-LdP)
13. 5-stage smart-staged search (1A → 1B → 2 → 3 → 4)
14. ~33B trials per sweep; **2-4 days wall time honestly stated**
15. 10 P-cores used (4 E-cores reserved for OS + daily apps); multi-day execution
16. SHA256+Bloom+LMDB dedup with TF as part of hash
17. Hash-chained audit JSONL with selective material-event logging
18. Threshold-filtered Parquet (~50-100GB per sweep)
19. 9-layer test pyramid, 24 invariants, 3 oracles
20. 8-12 week build phase plan
