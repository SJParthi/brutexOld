# Track 1 — Backtest System Build Specification

> **Status:** Locked spec, 2026-05-15. Supersedes the legacy
> `docs/REQUIREMENTS.md` (v1.0 (legacy)) and the prior `TRACK1_BACKTEST_SPEC.md`
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
> - `MIGRATION.md` — prior-broker → Groww migration log

---

## 1. Project Identity

**BruteX Track 1** is a brute-force backtesting engine that produces
**signal-only research output** on three Indian equity-index spot series,
with India VIX added as a **reference-only** context signal:

- **NIFTY 50** (NSE spot) — brute-forced
- **NIFTY BANK** / `BANKNIFTY` (NSE spot) — brute-forced
- **BSE SENSEX** / `SENSEX` (BSE spot) — brute-forced
- **India VIX** (NSE volatility index) — **reference only** (see §4.1)

The pipeline brute-forces ~2.9 million unique strategy patterns
(combinations of 2–5 atomic conditions from a 52-condition vocabulary)
× full parameter sweeps across **21 timeframes × 3 brute-force instruments**
× 2 exit modes, applies a 4-filter cascade (DSR → CPCV → WFO → Lockbox),
and returns the top 3 production candidates per sweep. India VIX is
fetched + stored exactly like the 3 spot indexes but is NEVER part of
brute-force combinations or the 52-condition vocabulary. Every trade
entry/exit captures `vix_at_entry` / `vix_at_exit` as reference columns
on the result row, enabling downstream "did this strategy work better
when VIX was high?" slicing.

Data ingest pipeline specified in `TRACK1_DATA_INGEST_DESIGN.md`.

**Out of scope (Track 1):** futures, options, F&O contracts, broker
execution simulation, live feed, WebSocket, multi-tenant SaaS,
commodity segment, BSE cash equity (other than the SENSEX index value
itself).

**Options Track (post-Track-1, future):** translates Track 1 winning
signals into ATM/ITM option fill simulations with IV/theta/spread
modeling — this is when execution realism (brokerage, STT, slippage)
re-enters. NOT to be confused with "Phase 2" of the Track 1 build plan
(see `TRACK1_PHASE_PLAN.md §Phase 2`), which is the Groww adapter +
52-vocabulary work within Track 1.

---

## 2. Hardware Target

- **MacBook Pro M4 Pro**: 14-core CPU (10 Performance + 4 Efficiency), 20-core GPU, 48 GB RAM
- macOS Tahoe 26.x
- 8 TB external SSD mounted at `/Volumes/QuantData`
- Daily apps (Chrome, IntelliJ, Claude Desktop) run concurrently
- Sweep uses **10 cores (the P-cores)**; 4 E-cores reserved for OS + daily apps
- Working set capped at 40 GB RAM during sweep

**SLA per sweep on this envelope:** **target 2-4 days wall time** for
~33 billion trials. Faster hardware (64-128 core EPYC / Threadripper)
brings this to 12-24 hours. The spec must restate SLA per hardware
envelope. See `TRACK1_PERFORMANCE_BUDGET.md` for the memory-bandwidth
analysis that bounds these numbers.

### 2.1 Honest Disclaimer (added 2026-05-15 evening, post-throughput-audit)

**The 2-4 day figure is a TARGET, not a guarantee.** It is predicated on
sustained 80-150K trials/sec, which has **never been measured** on a
real M4 Pro running BruteX code. The throughput is memory-bandwidth
bound with **zero margin** (per `TRACK1_PERFORMANCE_BUDGET.md §3.3`).

The Phase 3 bench gate `tests/bench/test_full_sweep_extrapolation.py`
is the SOLE validator: it runs 100K real trials end-to-end and refuses
CI merge if the projected 33B wall exceeds the published SLA band.

**Contingency SLA matrix (derived from worst-case throughput audit):**

| Phase 3 canary measures | Published SLA (33B) | Recommended action |
|---|---|---|
| ≥ 300K t/s (CPU + GPU) | **1-2 days** | proceed with Phase 3.5 Metal GPU offload |
| ≥ 150K t/s (CPU only) | **2-3 days** | proceed with current plan |
| 80-149K t/s | **3-5 days** | proceed; enable Stage 2 incremental checkpoint every 6h |
| 30-79K t/s | **5-12 days** | escalate: Phase 3.5 Metal GPU is mandatory; consider tighter Stage 1B funnel from 10K → 3K survivors (cuts 33B → ~10B) |
| < 30K t/s | **12-30 days — HALT** | re-architect: sweep sharding across N machines OR shrink universe |

**LOCAL-ONLY constraint (user decision 2026-05-15 evening):** AWS /
cloud offload is **explicitly out of scope** for Track 1. The only
hardware escalation lever is the Apple Metal GPU (20-core, already
present in the M4 Pro). All SLAs above assume single-machine execution.

If the canary lands in a slower band, the published SLA is amended
**and a new `Decision #` row is added to `docs/MIGRATION.md`** before
any sweep proceeds. Per `CLAUDE.md §2 rule 10`, no "guarantee"
language ships ahead of the bench gate. The 2026-05-15 4-agent review
caught a 10× over-optimistic throughput claim; this clause exists to
prevent that pattern from repeating.

**Public benchmark gap (honest):** there is **no public benchmark for
"trials/sec on M4 Pro for a Python+Rust quantitative backtest"**. The
closest analog is vectorbt's "50K simulations/sec on M1" claim
(hardware unverified, work-equivalence unclear). NEON benchmarks
(Erio-Harrison) verify 2.5-9× scalar speedups for byte-scan and
range-check kernels but no direct measurement for u64 bitmask AND
exists. The honest implication: the 80-150K target is a defensible
projection but requires Phase 3 validation.

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
| Brute-force instruments | NIFTY 50, BANKNIFTY, SENSEX (spot) — **3** |
| Reference-only instrument | India VIX (volatility index) — **1**, see §4.1 |
| Tradability | None (Track 1 is signal-only) |
| Volume column | Zero / null (indexes don't trade) |
| Continuous-series stitching | N/A (indexes are naturally continuous) |
| Range | Rolling: `2021-01-01 → today` |
| Trading session | 09:15 - 15:30 IST |
| Pre-open exclusion | Bars before 09:15:00 IST dropped at ingest |
| Forced exit | 15:20 IST (Track 1 convention) |
| Muhurat sessions | Excluded by hardcoded date list (see `TRACK1_MICROSTRUCTURE.md`) |
| Holiday calendar | NSE official 2021-2026 (resolved at ingest) |
| Index rebalance dates | Flagged in audit; data series is exchange-published (chain-linked) |

### 4.1 India VIX — Reference-Only Role

India VIX is **fetched and stored** like the 3 spot indexes — same
Groww endpoints, same 1m + 1d intervals, same 21 derived timeframes,
same audit guarantees — but is **never** an input to the brute-force
search. Specifically:

| What VIX participates in | Yes / No |
|---|---|
| Fetched from Groww (1m + 1d) | ✅ YES |
| Stored in Parquet lake (all 21 TFs) | ✅ YES |
| Watermark tracking, audit chain | ✅ YES |
| Brute-force pattern combinations | ❌ NO — never |
| 52-condition vocabulary atoms | ❌ NO — never |
| `vix_at_entry` column on trade result | ✅ YES — captured at signal bar close |
| `vix_at_exit` column on trade result | ✅ YES — captured at exit bar close |
| Stage 4 post-filter input | ✅ optional (e.g., "slice winners where vix_at_entry > 18") |

**Why not put VIX in the vocabulary?**
1. VIX updates every 15 seconds, not tick — its 1m OHLC has ~4 underlying samples (low intra-bar resolution)
2. Cross-instrument signals dramatically inflate the brute-force surface area
3. VIX-as-context is a known design pattern in volatility research — it's a regime indicator, not a price-action pattern
4. Future filter additions can use VIX without retroactively expanding the locked vocabulary

**Implementation:** `brutex/universe.py` exports `BRUTE_FORCE_INDICES`
(3 spot indexes, used by the engine) and `REFERENCE_ONLY_INDICES`
(India VIX, used only by the ingest + context-snapshot path). Property
P-25 (Phase 1) asserts no pattern in the generated set ever references
the VIX symbol.

---

## 5. Data Acquisition (Groww)

### 5.1 Endpoints

| Endpoint | Use |
|---|---|
| `POST /v2/charts/intraday` | 1-minute OHLC; 90-day chunks |
| `POST /v2/charts/historical` | Daily OHLC (for PDH/PDL anchors, calendar) |

### 5.2 Native granularity

**Groww provides 1, 5, 15, 25, 60 minute and 1-day intraday natively.**
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

### 5.4 securityId resolution — Static (hardcoded for Track 1)

Track 1's universe is permanently locked to 4 instruments. All
identifiers are **hardcoded** in `brutex/adapters/groww/constants.py`
(Phase 2 deliverable). The previous "runtime CSV lookup + never
hardcode" rule is relaxed for this narrow scope — verified once,
locked forever, quarterly smoke-tested.

**The 4 verified tuples (2026-05-15, user-confirmed via
`web.groww.co/Charts?exch=IDX&seg=I&secid=X`):**

| # | Trading Symbol | securityId | exchangeSegment | instrument | Use |
|---|---|---|---|---|---|
| 1 | `NIFTY 50` | `13` | `IDX_I` | `INDEX` | brute-force |
| 2 | `NIFTY BANK` (BANKNIFTY) | `25` | `IDX_I` | `INDEX` | brute-force |
| 3 | `SENSEX` | `51` | `IDX_I` | `INDEX` | brute-force |
| 4 | `INDIA VIX` | `21` | `IDX_I` | `INDEX` | reference-only |

**Why hardcode:**
- 4-tuple universe is locked in MIGRATION decisions #71 and #87
- Adding a 5th instrument requires a code change (intentional friction)
- Saves ~100k-row master CSV download per adapter startup
- Saves a network call + snapshot hash tracking on every run
- Deterministic — no drift from upstream CSV revisions

**Verification (quarterly smoke test, `tests/smoke/test_groww_secid_alive.py`):**

For each of the 4 securityIds, GET `web.groww.co/Charts?exch=IDX&seg=I&secid=X`
and assert the response is HTTP 200 with the expected symbol in the
page title. If a securityId is ever invalidated by Groww (extremely
unlikely for index spot), CI nightly catches it.

**The master CSV approach is preserved as a fallback** only — if all 4
hardcoded values fail simultaneously (catastrophic Groww re-numbering),
operators can manually fetch the CSV at
`https://images.groww.co/api-data/api-scrip-master-detailed.csv` and
update the constants module.

### 5.5 Rate limiting

**Verified 2026-05-15 from groww.in/trade-api/docs/docs/v2 Rate Limit table:**

| Tier | per-second | per-day |
|---|---|---|
| Data APIs (what Track 1 uses) | **5 req/s** | **100,000 / day** |
| Order APIs (not used in Track 1) | 10 | 7,000 |
| Quote APIs (not used in Track 1) | 1 | unlimited |

**Adapter caps:**
- Per-second: 5 × 0.6 safety factor = **3 r/s effective**
- Per-day: 100,000 hard ceiling — track in audit log, refuse to start a run that projects above 80% utilisation

**Track 1 typical utilisation:**
- First run (5-year backfill): 4 instruments × (22 × 1m chunks + 1 daily chunk) = 92 requests
- Daily incremental: 4 instruments × 1 chunk = 4 requests
- **Both are < 0.1% of the daily quota** — quota is not the binding constraint.

Constants live in `brutex/adapters/groww/constants.py` (Phase 2 deliverable).

### 5.6 Authentication (manual paste + auto-renew)

Track 1 uses Groww's **individual trader** access token model, NOT the
TOTP / API-key flow.

**First-time setup (manual, once per user):**

1. User logs into `web.groww.co`
2. Navigates: My Profile → Access Groww APIs → Generate Access Token
3. Token has 24-hour validity
4. User copies the token + their Client ID into a JSON file at
   `~/.brutex/groww/access_token.json`:

```json
{
  "client_id": "1000000401",
  "access_token": "eyJ0eXAi…",
  "expiry_time": "2026-05-16T15:00:00.000Z",
  "last_renewed_at": "2026-05-15T15:00:00Z"
}
```

The JSON file path is gitignored (`**/groww_token*`, `**/access_token*`)
and never committed.

**Automatic renewal:**

- At adapter startup AND every ~22 hours, BruteX calls
  `POST https://api.groww.co/v2/RenewToken` with headers
  `access-token: <current>` + `growwClientId: <client_id>`.
- On success: write new token + new expiry to the JSON file
  atomically (tempfile + rename).
- On failure (e.g., 24h elapsed, token already expired): structlog
  `CRITICAL auth.token_expired` event, FSM → HALTED, user must
  manually regenerate.

**Why this design:**

- Simplest first-run experience (one copy-paste)
- Auto-renewal keeps Track 1 running indefinitely as long as someone
  refreshes once per ~6 months (or whenever they want)
- TOTP flow is reserved for an optional `brutex auth setup --totp`
  command later (out of Track 1 minimum)

Detailed flow + pseudocode in `TRACK1_DATA_INGEST_DESIGN.md §5A`.

---

## 6. Timeframes (21 total)

### 6.0 Single-Timeframe Invariant (no multi-TF strategies)

Every brute-forced strategy is evaluated on bars of **exactly one**
chosen intraday timeframe (the run's `timeframe_id`). The 52-condition
vocabulary contains **no cross-intraday-TF condition**:

- Indicators EMA20/EMA200, day_high/day_low, candle shape, prior-N-bar
  patterns, time-of-day are all computed on the chosen TF.
- PDH, PDL, PDC, daily pivots (P/R1-R3/S1-S3), daily Fib anchors, and
  prev_close are **scalar constants** derived once per trading day from
  the daily series and broadcast across that day's intraday bars —
  they are NOT a parallel bar stream and do not constitute multi-TF logic.

This is industry-standard "daily-anchor on intraday" behavior
(TradingView, NinjaTrader, etc.). Each (instrument, timeframe) tuple
runs as an entirely independent backtest over the full 5-year range.
Property test P-25 (Phase 1) asserts the bar-mask evaluator only reads
the chosen-TF Parquet plus the daily-anchor scalar table.

### 6.1 Timeframes themselves

All resampled from 1m base, anchored to 09:15 IST. The 1m base + 1d
daily are fetched directly from Groww (`/v2/charts/intraday` and
`/v2/charts/historical` respectively); the 19 intraday derived TFs are
resampled in a single-pass cascade.

| Group | Timeframes | Count |
|---|---|---|
| Native fetched | 1m (intraday), 1d (daily) | 2 |
| Derived — 1-min increments through 15m | 2m, 3m, 4m, 5m, 6m, 7m, 8m, 9m, 10m, 11m, 12m, 13m, 14m, 15m | 14 |
| Derived — longer windows | 30m, 60m, 120m, 180m, 240m | 5 |
| **TOTAL** | | **21** |

**Updated 2026-05-15** (from 15 → 21 TFs per user scope expansion).
The 1-minute increment series 2m through 15m provides finer
intermediate granularity for short-horizon strategies; longer
windows (30m up to 4h) cover swing setups.

The resampling pipeline is specified in detail in
`TRACK1_DATA_INGEST_DESIGN.md §3` and `§11.2`. Property test P-08
asserts associativity: `resample(resample(x,2),3) ≡ resample(x,6)`.

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
| **VIX context columns** | `vix_at_entry` (NIFTY VIX close at signal bar) + `vix_at_exit` (NIFTY VIX close at exit bar) — captured but NOT used in entry/exit decision |
| Position size unit | % of notional capital; lot-rounded reserved for the Options Track |

Execution realism (brokerage, STT, slippage, spread, IV decay, theta)
re-enters in the **Options Track** (post-Track-1) when Track 1 winners
are translated to option contracts. Distinct from Track 1's own
"Phase 2" build phase (Groww adapter + vocabulary; see `TRACK1_PHASE_PLAN.md`).

---

## 12. Search Pipeline (5 stages)

```
STAGE 1A — Pattern discovery (NIFTY 15m only)
   ~5.8M trials, ~90 sec wall
   → top 50,000 patterns

STAGE 1B — Pattern expansion across all 63 (inst × TF) combos
   Top 50K × 63 inst-TF × default params
   ~3.15M trials, ~56 sec wall
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
    timeframe_id          ‖  // 1m to 1d (one of 21)
    mode_byte             ‖  // A or B
    canonical_params_json ‖  // sorted keys, NaN-rejected
    data_version_hash     ‖  // SHA of input Parquet files
    indicator_version     ‖  // bump on indicator code change
    code_commit_hash         // current git HEAD
)
```

- Bloom filter (~5GB at ~0.1% FPR for the **survivors-only** working set
  of 100M-1B post-threshold-filter combos, NOT for all 33B raw trials).
  Math: 5 GiB = 40e9 bits; at 1B survivors → 40 bits/elem → FPR ≈ 1e-12;
  at 150M survivors → 267 bits/elem → FPR ≈ 0. The L3 LMDB B+tree is
  the source of truth on Bloom collision. **Math sanity-check 2026-05-15
  evening:** if Bloom were sized for all 33B raw trials (not survivors)
  the same 5 GB would give only 1.21 bits/elem → FPR ≈ 56% (broken).
  Track 1 sizes Bloom for survivors-only because raw trials are
  threshold-filtered out of LMDB at write time (see §16).
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
| 2 | 3-4 weeks | Groww adapter + spot-index ingest + 52-vocabulary |
| 3 | 2-3 weeks | Brute-force engine (Stage 1A/1B/2/3) |
| 4 | 1-2 weeks | Filter cascade (Stage 4) + first production sweep |

**Total: 8-12 weeks** to ship Track 1 with 99.9% verification.

---

## 20. Final Locked Decisions (one-line ledger)

The full decision list (85 items, last amended 2026-05-15 evening) is
mirrored in `MIGRATION.md`. Highlights:

1. Spot indexes + India VIX, signal-only research, no broker theatre
2. Groww API; 1m + 1d fetched, 19 additional intraday TFs resampled (21 total)
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
