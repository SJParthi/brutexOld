# Trillion-Scale Architecture Evaluation — DB Engine + Frontend Verdicts

> **Decision ID:** DEC-ARCH-EVAL-TRILLION-SCALE-001 · **MIGRATION.md #1723**
> **Date:** 2026-07-11 · **Status:** DECIDED (measured) — hostile-review pass run
> 2026-07-11, FIX-FIRST findings folded (§12).
> **Scope:** research/evaluation decision, DOCS-ONLY. ENGINE UNCHANGED —
> NIFTY/BANKNIFTY/SENSEX; F&O lake stays REFERENCE_ONLY; no scope/vocab/rate change.
>
> Question evaluated: as BruteX scales toward its trillion-frame ambitions
> (~33B trials per full sweep today, Phase-10 max 730B; a 10-year full-universe
> lake), do the DB engine (DuckDB + Parquet + the #1614 stats manifest) and the
> frontend (vanilla JS + createElement/textContent) hold the sub-second
> per-interaction O(1) discipline (Golden Rule #5), or is a migration
> (ClickHouse-class engine / distributed Parquet / React-Svelte / Rust→WASM)
> warranted? Three measurement workstreams answered it: a DB-serving benchmark
> over lakes built with the REAL production writers, a headless-Chromium frontend
> render benchmark mirroring the real screener renderer, and a read-only
> file:line-cited architecture audit. Every number in this document is copied
> verbatim from those three reports (§12 provenance); nothing here is
> estimated-presented-as-measured, and every extrapolation is labelled as such
> with its fitted exponent.

---

## 1. Executive summary + verdicts

### 1.1 The two measured spines (lead with these)

**(a) Paging is FLAT across 49× lake growth — trillion-row-safe BY
ARCHITECTURE.** The control experiment (§4.3): a fresh 6-month symbol written
through the production writer pages in **12.4 ms inside the 49M-row lake vs
12.8 ms inside the 0.98M-row lake** — total lake size is irrelevant to a /db
page; latency tracks only the queried dataset's own depth (rows + month
files), and dataset depth grows with YEARS (bounded, ~12 month files/yr),
never with symbols or lake width. A trillion-row TOTAL lake of bounded-depth
datasets serves pages at the same tens-of-ms as today (extrapolated from the
§4.3 control experiment + the partition-scheme argument; no ≥1B-row lake was
materialized — §11).

**(b) The ~33B trials never reach the serving layer — serving is
O(finalists).** Only top-K finalists per tuple are ever flushed
(`sweep_run.py:1774-1817`); the persisted output of a 33B-trial sweep is
≈ tuples × top_k ≈ 10⁶–10⁷ rows, 3–4 orders of magnitude below the trial
count (§2). The trillion-frame number is compute-time, ~all discarded.

**The ClickHouse arm was really tested** (chdb 4.2.0, embedded ClickHouse,
VERIFIED install — embedded, default settings, NOT a tuned ClickHouse server
deployment; §5, §11), side by side on the same lake (page/count/slice over
one 2.4M-row dataset; the rollup over all 48.9M rows):

| workload | DuckDB (parquet in place) | chdb (parquet in place) | chdb MergeTree copy |
|---|---|---|---|
| page1 (ORDER BY ts DESC LIMIT 100) | **83.8 ms** | 110.9 ms | **5.9 ms** |
| count (DISTINCT ts) | **59.5 ms** | 212.8 ms | 195.5 ms |
| month_slice | **44.9 ms** | 83.3 ms | 9.9 ms |
| full-lake symbol-day rollup (48.9M rows) | 3,592 ms | 2,659 ms | 771 ms |
| one-day movers READ off a PRE-BUILT DuckDB rollup (build cost 3.70 s; §4.6) | **2.7 ms** | — | — |

DuckDB-in-place is FASTER than ClickHouse-in-place on every dashboard-shaped
page/count/slice; the MergeTree copy's headline win — 13× on point-page
lookups (5.9 vs 84 ms) — is against the UNassisted parquet path, while the
production manifest-ON path already holds pages at 12–80 ms and (spine (a))
those costs don't grow with lake size; and its 4.7× from-scratch full-lake
aggregation win (771 ms vs 3.6 s) is answered by pre-aggregation inside the
current stack — the one-day whole-market movers READ off a PRE-BUILT DuckDB
rollup is 2.7 ms (build cost 3.70 s; ~940× vs the 2,524.8 ms raw-parquet
scan). Stated honestly: MergeTree's from-scratch whole-table daily rollup
(771 ms) is itself FASTER than DuckDB's rollup BUILD (3.70 s) — the argument
is "pre-aggregation beats switching engines", NOT "DuckDB is ~285× faster at
this aggregation". The MergeTree copy costs a second store (735 MB vs 213 MB
parquet, 3.4×), a 9.04 s/48.9M-row ingest sync, and a new operational
surface.

### VERDICT A (DB engine): KEEP DuckDB + Parquet + stats-manifest. No ClickHouse / distributed migration.

Two named follow-up fixes carry the O(1) guarantee (neither is in this PR):

1. **/results bounded serving** — the ONE measured sub-second breach in the
   serving stack, O(stores × rows): `load_sweep_scan` cold is 205 ms @10 stores
   → 23.7 s @1000 stores (b≈1.03, crosses 1 s at ≈55 stores), and even the
   revision-cached WARM leaderboard rank is 904 ms per poll @1M finalist rows
   (b≈1.17). Fix: a persisted cross-store leaderboard rollup — top-K
   materialized at sweep write time (the `top100_artifact` pattern generalized)
   — so serving reads O(K), never O(stores).
2. **Materialized rollups for whole-lake aggregations** — measured ~940×:
   2,525 ms raw-parquet whole-market movers scan → a 2.7 ms one-day movers
   READ off a PRE-BUILT daily rollup (build cost 3.70 s; 48.9M rows →
   130,440 daily rows, 1.8 MB).

### VERDICT B (frontend): KEEP vanilla JS. The data-shipping contract is the lever, not the language.

Measured: uncapped rendering is O(rows) in BOTH vanilla JS (paint ≈ N^0.94;
100k rows = 29.3 s of main-thread block) and React 18 (paint ≈ N^1.09; 50k rows
= 14.6 s) — same failure class, constants differ ~1.4×; a framework swap moves
the constant, never the asymptote. Capped rendering (the repo's existing
SHOW_ALL_CAP=1000) is measured flat (~190–265 ms regardless of a 100k-row
payload); windowed rendering is measured flat at 0.7–0.9 ms sync from 100 to
500,000 rows (5000× range, zero longtasks). No client compute at the repo's
actual caps exceeds 15 ms (sort-5k 6.7 ms, filter 0.3 ms, cumsum 0.1 ms,
85k-cell format 15.0 ms), so a Rust→WASM kernel has nothing to accelerate —
the JS↔WASM boundary crossing is of the same order as the work itself. The
repo already encodes the winning architecture: server pagination
(TOP100_ROW_CAP=100) → bounded input (5000) → bounded render (200/1000), every
truncation named on the count line.

---

## 2. The architectural spine — why per-query cost stays flat in lake size at trillion scale

The honest physics (Golden Rule #10): O(1)-per-query at trillions is achieved
by **never touching most of the data**. This is an ARCHITECTURE property —
pruning + manifests + rollups + finalists-only persistence — not a
language/engine property. Concretely, with the audit's file:line citations:

- **The ~33B sweep TRIALS never reach the serving layer.** Per-mask
  `ResultMetrics` live only in memory — the bounded arm keeps ~80–105 B/kept of
  ranking columns and DROPS the ResultMetrics (`brutex/engine/sweep_run.py:1727-1736`),
  ranks, truncates to `top_k` finalists (`sweep_run.py:1774-1793`), and only the
  finalists are persisted (`sweep_run.py:1795-1817`). Even the finalists' inner
  trade streams are discarded at that stage (`sweep_run.py:1740-1756`). One
  `ResultsStore` Parquet per tuple, explicitly "Sized for the funnel's final
  10²–10⁴ finalist rows" (`brutex/engine/results_store.py:537-543`). At
  `--top 1000` the entire persisted output of a 33B-trial sweep is
  ≈ tuples × 1000 rows ≈ 10⁶–10⁷ rows — 3–4 orders of magnitude below the trial
  count. **Serving grows O(tuples × top_k) = O(finalists), never O(trials).**
- **The top100 artifact is hard-capped at 100** (`TOP100_LIMIT = 100`,
  `brutex/engine/top100_artifact.py:156`; `load_top100` is O(1) in stored-winner
  count by construction, `top100_artifact.py:66-77`; the display reader re-caps
  at `TOP100_ROW_CAP = 100`, `brutex/dashboard/oneclick_artifacts.py:198,1346-1356`).
  Measured 1.9 ms median regardless of grid size (§4.5).
- **The LAKE at 10 years × full universe is ~1.5–2B rows, not a trillion**
  (audit §5 arithmetic: ~750 underlyings × 10y × 93,750 1-minute bars/symbol-yr
  ≈ 703M spot 1m rows, ×~2–3 with derived TFs; ~3.6M files) — and /db serving
  is per-DATASET-depth-bounded: the control experiment (§4.3) shows a fresh
  6-month symbol pages in 12.4 ms inside a 49M-row lake, identical to 12.8 ms
  in a 0.98M-row lake. Total lake size is irrelevant to a /db page.
- **The trillion-class number is the trial space** (~33B/sweep,
  `docs/TRACK1_PERFORMANCE_BUDGET.md:17-18`; Phase-10 730B), which is a COMPUTE
  problem (the O(1)-per-trial hot kernel + Bloom/LMDB dedup + the Stage 1A→4
  funnel, memory-bandwidth-bound). No serving engine helps that layer; a
  ClickHouse-class engine is irrelevant to it.

The two layers need different tools, and both already have them: the trials
layer has the funnel; the serving layer has bounded artifacts + stat-token
revision caches + the two write-time manifests (#1480 discovery, #1614
per-file stats). The residual O(total) terms (§8's follow-ups) are file-count
problems, not row-count problems, and all removable with small file-based
rollups at the existing partition-locked write chokepoints — without an
external query engine.

---

## 3. Method

- **Real writers, real readers.** Candle lakes were written through the
  production `brutex.data.parquet_writer.ParquetWriter.write` (one call per
  (symbol, month) partition — the production grouping law; measured
  ≈113k rows/s single-threaded) and served through the real
  `db_view.query_candles`, `sweep_results_view.load_sweep_scan` /
  `build_sweep_leaderboard`, `ResultsStore`, and the engine `write_top100` →
  display `load_top100` chain. Result stores were built with the real
  `ResultsStore.flush` (validated `ResultRow`s, 64-hex run_ids, u128 masks) at
  the production layout, 1,000 finalist rows per store (the Ruling-1 top-1000
  storage law).
- **Datasets** (1-minute bars, 375/day, Mon–Fri, 20 symbols/lake, NSE/INDEX,
  deterministic 2dp-grid prices; depth per symbol grows with lake size — the
  realistic growth axis):

  | scale point | rows | month files | rows/dataset | files/dataset | on-disk | build time |
  |---|---|---|---|---|---|---|
  | 1M  (20 sym × 6 mo)   | 975,000    | 120   | 48,750    | 6   | 4.5 MB | 8.6 s |
  | 10M (20 sym × 60 mo)  | 9,787,500  | 1,200 | 489,375   | 60  | 43 MB  | 86.9 s |
  | 50M (20 sym × 300 mo) | 48,915,000 | 6,000 | 2,445,750 | 300 | 213 MB | 430.3 s |

  100M was skipped: it adds no new information to a 3-point log-log fit
  (1M→50M already spans 50×) and the 50M point alone took 7.2 min of writer
  time. Compression ≈ 4.4–4.6 bytes/row (zstd-3, synthetic prices; real vendor
  bars will be a few × larger but same order).
- **Statistics:** medians + p99 over 20 reps warm / 10 reps cold (DB);
  medians of ≥3–5 reps as stated per cell (frontend); log-log least-squares
  exponent fits `t = a·N^b` over the 3 scale points.
- **PROMINENT caveat: the sandbox is NOT the production M4 Pro.** All
  measurements ran on 4 × Intel Xeon @ 2.10 GHz vCPU, 15 GB RAM, cloud block
  storage (duckdb 1.5.4, polars 1.42.1, pyarrow 25.0.0, chdb 4.2.0, Python
  3.12.3; Rust wheels built in-session). Absolute milliseconds are
  environment-specific; the deliverable is the **scaling curves and fitted
  exponents**, which transfer across hardware far better than absolute
  numbers. "Cold" = fresh DuckDB connection + fresh generation-stamped cache
  per rep; the OS page cache stays warm in both modes (true cold-storage
  first-touch would be slower on all arms equally).
- **Honesty detail:** an earlier DB measurement pass ran concurrently with the
  50M lake build and was DISCARDED as contaminated; every DB number below is
  from the clean re-run on an otherwise idle box.
- **Manifest seeding note:** stats-manifest shards are read-side seeded — the
  FIRST manifest-ON read per dataset pays one O(files) seed, after which reads
  validate the shard per request. All manifest-ON numbers are post-seed (the
  steady production state).

---

## 4. Measured DB serving curves

### 4.1 /db candle serving (`db_view.query_candles`), manifest ON vs OFF

All queries target ONE symbol dataset (that is what the /db pager does; its
cost is per-DATASET depth — §4.3). page = limit 100 descending; deep page =
offset total/2; month_slice = one-month window (limit 50k, engine-feed-shaped);
count = the page `total` path. Median / p99 ms.

| interaction | mode | 1M med/p99 | 10M med/p99 | 50M med/p99 |
|---|---|---|---|---|
| page1        | warm, ON  | 12.2 / 13.6 | 24.7 / 29.5 | 80.0 / 91.1 |
| page1        | warm, OFF | 17.2 / 20.7 | 66.3 / 72.2 | 332.8 / 430.5 |
| page1        | cold, ON  | 28.8 / 35.3 | 43.1 / 49.0 | 109.1 / 158.4 |
| page1        | cold, OFF | 39.5 / 48.8 | 119.0 / 140.4 | 522.3 / 626.7 |
| deep_page    | warm, ON  | 11.4 / 14.3 | 22.6 / 25.2 | 70.6 / 73.8 |
| deep_page    | warm, OFF | 18.4 / 22.9 | 98.2 / 106.8 | 517.9 / 621.3 |
| month_slice  | warm, ON  | 22.0 / 24.4 | 32.0 / 34.1 | 83.9 / 102.6 |
| month_slice  | warm, OFF | 19.2 / 23.9 | 59.0 / 70.8 | 235.0 / 252.7 |
| count total  | warm, ON  | 10.1 / 10.9 | 18.4 / 24.5 | 58.9 / 91.3 |
| count total  | warm, OFF | 13.5 / 16.4 | 61.0 / 76.0 | 307.4 / 388.2 |
| count total  | cold, ON  | 26.3 / 27.1 | 35.3 / 37.0 | 83.6 / 92.0 |
| count total  | cold, OFF | 37.4 / 43.6 | 106.7 / 111.7 | 491.8 / 506.6 |

### 4.2 Fitted exponents (t = a·N^b; N = total lake rows in the harness)

`@1B`/`@1T` are EXTRAPOLATIONS from the fit, under the harness's pessimistic
assumption that dataset depth + file count keep growing proportionally with N
(§4.3 explains why production does not do this).

| interaction | manifest | mode | b | @1M (ms) | @1B extrap | @1T extrap |
|---|---|---|---|---|---|---|
| page1       | ON  | warm | **0.47** | 11.1 | 282 ms | 7.2 s |
| page1       | OFF | warm | **0.75** | 15.8 | 2.7 s | 462 s |
| page1       | ON  | cold | 0.33 | 26.1 | 254 ms | 2.5 s |
| page1       | OFF | cold | 0.65 | 35.8 | 3.1 s | 274 s |
| deep_page   | ON  | warm | 0.46 | 10.4 | 240 ms | 5.6 s |
| deep_page   | OFF | warm | **0.84** | 17.4 | 5.9 s | 1991 s |
| month_slice | ON  | warm | 0.33 | 19.9 | 193 ms | 1.9 s |
| month_slice | OFF | warm | 0.63 | 17.7 | 1.4 s | 106 s |
| count total | ON  | warm | 0.44 | 9.1 | 186 ms | 3.8 s |
| count total | OFF | warm | 0.79 | 12.6 | 2.9 s | 672 s |
| count total | ON  | cold | 0.28 | 23.9 | 169 ms | 1.2 s |
| count total | OFF | cold | 0.64 | 33.5 | 2.9 s | 244 s |

Honest reading (Golden Rule #10):

- **No /db interaction is truly flat (b≈0).** The manifest does NOT make
  serving O(1) — it replaces the O(dataset-rows) scan term with an
  O(dataset-FILES) term (revalidation stat sweep + shard parse + pruned-file
  query), exactly as the #1614 module's own honest envelope states
  ("O(page-files) work + an O(D) per-consult revalidation sweep — NOT literal
  O(1)"). With D ∝ N in this harness, that reads as b≈0.33–0.47. **Manifest-ON
  /db is O(month-files-per-dataset)** — the b≈0.33–0.47 vs whole-lake growth
  appears in the harness only because the harness grew months-per-symbol; the
  control (§4.3) isolates the truth: per-dataset depth, not lake width.
  Dataset depth grows with YEARS (bounded, ~12 files/yr), not with symbols or
  lake width.
- The manifest is worth **4–7× at 50M** on every paging/count interaction and
  its advantage grows with depth (b drops by ~0.3 across the board);
  deep_page is where it matters most: 71 ms vs 518 ms at 50M. Manifest-OFF
  crosses 1 s at ≈260M rows of proportional growth (b=0.75 fit) — the kill
  switch is a real cliff at scale, not a benign toggle. Keep it ON.
- Where the growth lives (cProfile of a warm manifest-ON hit at 10M): ~12 ms
  in the pruned DuckDB page query itself (per-query planning/execution floor
  at this vCPU class), ~4 ms in `load_valid_dataset_stats` (O(D) stat sweep +
  shard JSON parse: ~150 `posix.stat`s per request at D=60). Both terms grow
  with month-file count, neither with total lake size.

### 4.3 Per-dataset vs total-lake scaling (the control experiment — the key result)

A FRESH 6-month symbol (`NSE-CTRL099`, 48,750 rows / 6 files) was written
through the production writer INTO the 49M-row lake, and its page1 compared
against the identically-shaped dataset in the 0.98M-row lake:

| query target | median / p99 ms |
|---|---|
| 6-month dataset inside the **0.98M-row** lake | 12.8 / 15.0 |
| 6-month dataset inside the **49M-row** lake | **12.4 / 14.0** |
| 300-month dataset inside the same 49M-row lake | 78.8 / 86.7 |

**/db serving latency is a function of the QUERIED DATASET's depth (rows +
month files) only — total lake size is irrelevant.** The §4.2 exponents are
therefore per-dataset-depth exponents. A trillion-row lake built of hundreds
of thousands of bounded-depth contract datasets serves pages at the SAME
tens-of-ms as today; the only way a /db page crosses 1 s is a SINGLE dataset
growing to ~1,800 month files (≈150 years of 1-minute bars, extrapolated at
b=0.47) — structurally impossible for the partition scheme.

### 4.4 Sweep-results serving (`ResultsStore` → /results) — the ONE breach

Stores: real `ResultsStore.flush`, 1,000 finalist rows per store.

| stores | rows total | on-disk | build time |
|---|---|---|---|
| 10 | 10,000 | 0.9 MB | 0.3 s |
| 100 | 100,000 | 8.7 MB | 2.4 s |
| 1000 | 1,000,000 | 86 MB | 23.6 s |

| interaction | 10 stores | 100 stores | 1000 stores | scaling |
|---|---|---|---|---|
| `load_sweep_scan` cold (store discovery + full row load) | 205 / 230 | 2,142 / 2,397 | 23,719 / 23,896 | **b ≈ 1.03 — strictly O(stores × rows)** |
| `build_sweep_leaderboard` warm (scan revision-cached) | 4.2 / 7.0 | 48.9 / 70.3 | 904 / 969 | **b ≈ 1.17 — O(rows) per poll** |
| `build_sweep_leaderboard` cold (scan=None) | 202 / 225 | 2,291 / 2,405 | 24,413 / 25,415 | b ≈ 1.04 |

Serving is **O(stores × finalist rows) — never O(trials)** (the 33B-trial
sweep only ever lands ≤1000 finalists per tuple store). But it IS linear in
the finalist pool: the cold scan crosses **1 s at ≈55 stores** (55k rows) in
this environment and takes 24 s at 1000 stores — paid once per revision token,
then cached; the WARM path (filter+rank over the cached `SweepScan.rows` tuple
in pure Python) crosses **1 s at ≈1000 stores / 1M finalist rows per poll** —
the first genuine sub-second-at-scale breach in the serving stack, because it
is paid on EVERY leaderboard interaction, not once per revision. Fix = §8(a).

### 4.5 One-click top100 artifact — flat by construction

`build_top100` → engine `write_top100` (real writer, 100 entries,
200,700 bytes) → `oneclick_artifacts.load_top100`: **median 1.9 ms /
p99 2.4 ms** (30 reps). O(1) by construction (≤100 entries regardless of grid
size) — confirmed flat and trivially sub-second at any sweep scale. Already
trillion-safe.

### 4.6 DuckDB materialized-rollup probe (50M lake)

Per symbol-day OHLCV rollup (`arg_min`/`arg_max`/`max`/`min`/`sum` over the
raw 1-minute parquet, GROUP BY symbol+day) materialized into a DuckDB
database file:

| metric | value |
|---|---|
| build time (48.9M rows → 130,440 daily rows) | **3.70 s** (one-shot) |
| rollup DB size | 1.8 MB |
| 90-day daily series, one symbol — vs rollup | **32.8 ms** med / 36.7 p99 |
| same series computed from raw 1-minute parquet | 136.3 ms med / 143.8 p99 (4.2×) |
| whole-market one-day movers scan — vs rollup | **2.7 ms** med / 3.5 p99 |
| same movers scan from raw parquet | 2,524.8 ms med / 2,568.2 p99 (**~940×**) |

The rollup converts the worst dashboard aggregation (whole-lake cross-symbol
scan) from 2.5 s to 2.7 ms for a 3.7 s build cost per refresh — and the build
itself is O(lake) but incremental-izable per month partition. Push rollups,
don't switch engines.

---

## 5. ClickHouse A/B (VERIFIED, chdb 4.2.0)

`pip install chdb` succeeded (chdb 4.2.0, embedded ClickHouse) — the
comparison is MEASURED, not analytical. Raw output: `chdb_all.jsonl` (§12).

### 5.1 Same parquet lake, in place (dashboard-shaped queries, one 2.4M-row dataset)

| interaction | DuckDB read_parquet med/p99 | chdb file(Parquet) med/p99 |
|---|---|---|
| page1 (ORDER BY ts DESC LIMIT 100) | **83.8 / 85.9** | 110.9 / 117.5 |
| count (DISTINCT ts) | **59.5 / 61.2** | 212.8 / 226.4 (uniqExact) |
| month_slice | **44.9 / 56.2** | 83.3 / 91.8 |
| full-lake per symbol-day OHLCV rollup (48.9M rows) | 3,592 / 3,658 | **2,659 / 2,692** |

Over parquet-in-place, ClickHouse's engine buys NOTHING for the page-serving
workload (it is slightly slower than DuckDB on pages/counts/slices and ~1.35×
faster only on the full-lake aggregation). The sparse-index advantage requires
the MergeTree copy.

### 5.2 Real MergeTree (chdb stateful session, `ORDER BY (symbol, ts)`)

| metric | value |
|---|---|
| ingest 48,915,000 rows from the parquet lake | **9.04 s** (one INSERT…SELECT) |
| MergeTree on-disk size | **735 MB** (vs 213 MB parquet — 3.4×; default settings, DateTime64(6) key, parts as left by the insert) |
| mt_page1 — symbol ORDER BY ts DESC LIMIT 100 | **5.9 / 12.3 ms** |
| mt_month_slice — symbol + ts-range | **9.9 / 10.5 ms** |
| mt_count_sym — uniqExact(ts) one symbol | 195.5 / 231.7 ms |
| mt_daily_rollup — whole-table symbol-day OHLCV GROUP BY | **771 / 789 ms** |

### 5.3 What MergeTree measurably buys — and what it would cost

- (i) **Page serving: 5.9 ms vs 80–84 ms** for DuckDB-over-parquet on the same
  2.4M-row dataset (≈13×) — the sparse primary index reads only the granules
  the page needs, and this stays ~flat in dataset depth. BUT: the production
  manifest-ON path already holds pages at 12–80 ms for realistic dataset
  depths, and the control (§4.3) shows those costs don't grow with lake size.
- (ii) **Ad-hoc full-lake aggregation: 771 ms vs 3.6 s** DuckDB-over-parquet
  (4.7×) at 50M rows — both linear in lake size, so at 1B rows this workload
  is ~16 s vs ~73 s (extrapolated, b≈1). Neither is sub-second at 1B+ without
  pre-aggregation — which the 3.7 s DuckDB rollup (§4.6) solves at
  2.7 ms/query.
- Both measured wins are on workloads the dashboard either doesn't do (ad-hoc
  whole-lake scans) or that the rollup fix wins harder on (2.7 ms).
- **Costs:** a SECOND copy of the data (3.4× parquet size here) or a
  dual-write pipeline (a 9 s/50M-row ingest sync — ≈3 min/1B rows extrapolated
  linearly), a server/daemon in the single-M4-Pro zero-services envelope, and
  the migration-cost dimensions from the audit: byte-identical Parquet
  rendering is golden-pinned and idempotence-keystoned
  (`brutex/data/parquet_writer.py:158-161,1865-1868`;
  `results_store.py:140-144,798-855`, re-flush byte-identity
  `results_store.py:64-77`) and a MergeTree part is NOT byte-deterministic —
  the files must remain the source of truth and any engine a derived read
  replica; the flock/`LakeWriteHalt` concurrency + failure semantics
  (`brutex/data/partition_lock.py:344`; `brutex/data/errors.py:50`) have no
  external-server equivalent; the hash-chained audit JSONL (Golden Rule #8,
  `brutex/orchestrator/audit.py:12-46`) must stay a file artifact —
  dual-write, never replace; and `data_snapshot_hash` run identity
  (`brutex/engine/stage3_wire.py:172-224`) means any migration that changes
  what bars a sweep loads is a silent re-key event — a serving-only migration
  is safe iff the sweep continues to read bars via Catalog/ReplayFeed over the
  same parquet.

---

## 6. Measured frontend curves

Environment: headless Chromium 141.0.7390.37 (Playwright 1.61.0), viewport
1400×900. The benchmark page's row renderer is a line-for-line mirror of the
repo's screener render (createElement + textContent per cell, the real
17-column `BX_SCREENER_COLUMNS`, the real `bxFixed2`/money/cellText pipeline).
`paint` = performance.now() bracket through a double-requestAnimationFrame
(**~33 ms floor** — two 60 Hz frame quanta; a "paint ≈ 33 ms" cell means
"render cost below one frame"); `sync` = pure JS+DOM-build time; `longtask` =
PerformanceObserver('longtask') main-thread blocking. Medians of ≥3–5 reps as
noted.

### 6.1 UNCAPPED (render all N rows) — the failure mode

| N | reps | sync ms | paint ms | longtask ms |
|---:|---:|---:|---:|---:|
| 100 | 5 | 2.3 | 32.3 | 0 |
| 1,000 | 5 | 25.5 | 391 | 331 |
| 5,000 | 5 | 120.8 | 2,079.7 | 2,043 |
| 10,000 | 5 | 259.0 | 2,989.3 | 2,751 |
| 50,000 | 3 | 1,705.6 | 12,615.1 | 12,608 |
| 100,000 | 3 | 3,211.8 | 29,303.1 | 29,160 |

**Fitted exponent (log-log, 1k→100k): paint ≈ N^0.94, sync ≈ N^1.05 —
O(rows), essentially linear.** At 100k rows the tab's main thread is blocked
~29 s in one longtask. This independently reproduces (and exceeds) the repo's
own ledgered 10k=4.8 s / 20k=14.1 s tab-freeze measurement
(`bx-screener.js:34-38` records why the caps exist).

### 6.2 CAPPED at 1000 (current repo behavior: render min(N, SHOW_ALL_CAP))

| N (payload) | reps | sync ms | paint ms | longtask ms |
|---:|---:|---:|---:|---:|
| 100 | 5 | 2.3 | 35.1 | 0 |
| 1,000 | 5 | 29.4 | 265.1 | 243 |
| 5,000 | 5 | 27.1 | 229.2 | 214 |
| 10,000 | 5 | 19.0 | 197.8 | 190 |
| 50,000 | 5 | 18.3 | 186.7 | 171 |
| 100,000 | 5 | 19.4 | 206.5 | 196 |

Flat in N once the cap binds (~190–265 ms paint for the 1000-row table
regardless of payload size). The cap converts O(N) into O(cap) — but a
1000-row render still costs a ~200 ms longtask; the DEFAULT 200-row view
(RENDER_CAP=200) is proportionally ~5× cheaper still.

### 6.3 WINDOWED / virtual scroll (40-row absolute-positioned window, same per-cell discipline)

| N | reps | sync ms | paint ms | longtask ms |
|---:|---:|---:|---:|---:|
| 100 | 5 | 0.8 | 32.8 | 0 |
| 1,000 | 5 | 0.8 | 32.8 | 0 |
| 5,000 | 5 | 0.8 | 32.8 | 0 |
| 10,000 | 5 | 0.8 | 32.8 | 0 |
| 50,000 | 5 | 0.9 | 32.7 | 0 |
| 100,000 | 5 | 0.7 | 32.4 | 0 |
| 500,000 | 5 | 0.8 | 32.9 | 0 |

**Flat. 5000× data-size range (100 → 500,000 rows), sync ratio ≈ 1.0×, paint
pinned at the two-frame rAF floor (~33 ms), zero longtasks — measured
O(visible).** (Caveat: the in-page ARRAY still holds N rows — 500k rows of
this shape is ~100s of MB of JS heap; the true unbounded lever is server
pagination so the client never holds N, exactly the repo's existing wire
contract.)

Scroll responsiveness (60 sequential page-jumps): per-step re-render sync
1.2 ms median / 1.8 max @10k · 1.4 / 1.6 @100k · 1.4 / 1.9 @500k — flat vs N
and two orders of magnitude under a 60 Hz frame budget. Windowing does not
trade away interactivity.

### 6.4 Framework comparison — React 18.3.1 (production UMD, flushSync render of the identical 17-col table; MEASURED, not analytical)

| N | reps | React sync ms | React paint ms | vs vanilla sync | vs vanilla paint |
|---:|---:|---:|---:|---:|---:|
| 100 | 5 | 4.2 | 33.1 | 1.8× slower | ~same (frame floor) |
| 1,000 | 5 | 28.8 | 207.7 | 1.1× slower | 0.5× (faster)* |
| 5,000 | 5 | 165.6 | 1,071.6 | 1.4× slower | 0.5× (faster)* |
| 10,000 | 3 | 354.3 | 2,265.0 | 1.4× slower | 0.8× |
| 50,000 | 3 | 2,489.2 | 14,582.0 | 1.5× slower | 1.2× slower |

**Fitted exponent (1k→50k): React paint ≈ N^1.09 — also O(rows).** React's JS
(sync) cost is consistently ~1.4–1.5× the direct createElement build; total
paint-complete times land in the same order of magnitude either way (*the
sub-1× paint cells at 5k–10k are layout/paint variance — both are still
seconds-scale and linear; the claim is "same asymptote", not "React always
slower"). A framework swap moves the CONSTANT by tens of percent in either
direction; it does not change the asymptote, which is the thing that kills
the tab.

### 6.5 Client-compute audit (repo-shaped worst cases) — nothing for WASM to accelerate

The caps, found in source: `TOP100_ROW_CAP = 100` (server wire bound,
`brutex/dashboard/oneclick_artifacts.py:198`, enforced at `:1350-1356`);
`BX_SCREENER_RENDER_CAP = 200` (default view, `bx-screener.js:43`);
`BX_SCREENER_SHOW_ALL_CAP = 1000` (`bx-screener.js:45`);
`BX_SCREENER_INPUT_CAP = 5000` (`bx-screener.js:47`, applied in `bxSortRows`
`:248` and `bxApplyFilters` `:340`); trades 100/page (server
`PAGE_LIMIT_MAX=500`, `strategy.html:260`, `oneclick_artifacts.py:2095,2135`);
equity series ≤5000 points (`app.py:7405`); candle chart fetch 500 bars
(`strategy.html:382`); timeline event ring 5000 records (`bx-oneclick.js:40`).

| Compute | Input | median ms | max ms | reps |
|---|---|---:|---:|---:|
| Sort, numeric key (real bxCompare comparator) | 5,000 rows (INPUT_CAP) | **6.7** | 10.7 | 15 |
| Filter (bxFilterRow + VIX overlap) | 5,000 rows | **0.3** | 0.7 | 15 |
| Equity cumsum (bxEquitySeries, int paisa) | 5,000 points (equity-route max) | **0.1** | 0.7 | 15 |
| Full cell-text/format pipeline | 5,000 rows × 17 cells (85k cells) | **15.0** | 16.3 | 10 |
| Sort headroom (beyond every current cap) | 100,000 rows | **228.7** | 250.2 | 5 |

At today's ACTUAL caps every compute path is ≤15 ms — under any
human-perceptible threshold and under the 50 ms longtask bar. Even the
deliberately-out-of-contract 100k-row sort is 229 ms, and it only exists if
someone first removes the server's 100-row wire cap AND the 5000-row input
cap. A Rust→WASM kernel would be accelerating 0.1–15 ms of work per
interaction; the JS↔WASM boundary-crossing and (de)serialization overhead is
of the same order as the work itself.

**Verdict B argument, restated on the numbers:** both frameworks are measured
O(rows) (0.94–1.09 exponents); capped rendering is measured flat; windowed
rendering is measured flat to 500k rows; no capped compute exceeds 15 ms. The
lever is the data-shipping contract the repo already enforces — bounded wire
(100) → bounded input (5000) → bounded render (200/1000), each truncation
named on the count line — not the language or framework.

---

## 7. Extrapolation to trillion scale — explicit, with assumptions

For each serving interaction: the fitted exponent, what N actually grows in
production, the extrapolated latency at the trillion-frame scale, and which
follow-up fix (if any) it depends on. Every extrapolation is from the §4/§6
fits — labelled, never presented as a measurement.

| interaction | fitted exponent (measured) | what N grows in production | extrapolated at trillion-frame scale | depends on |
|---|---|---|---|---|
| /db page1 / deep_page / count (manifest ON, warm) | b = 0.47 / 0.46 / 0.44 in per-DATASET depth (§4.3 control: independent of lake width) | YEARS per dataset (~12 month files/yr); never symbols or total lake rows | tens of ms at any TOTAL lake size (12.4 ms fresh symbol inside 49M rows, measured); 1 s needs ~1,800 month files ≈ 150 years of 1-minute bars — structurally impossible for the partition scheme | nothing (manifest stays ON) |
| /db with manifest OFF | b = 0.75 (page1 warm) | proportional dataset growth | crosses 1 s at ≈260M rows of proportional growth — the kill switch is a cliff, not a benign toggle | keep manifest ON |
| /results `load_sweep_scan` cold | b ≈ 1.03 — O(stores × rows) | accumulated tuple stores across ALL runs (the sweeps tree is never partitioned by run; audit gap 3) | measured 23.7 s @1000 stores; 100 full-grid runs × 10⁴ tuples × 1000 rows = 10⁹ rows loaded into one process dict per cold revision — unserviceable | **fix (a)** — persisted leaderboard rollup ⇒ O(K) |
| /results warm leaderboard rank | b ≈ 1.17 — O(rows) per poll | finalist pool (stores × top_k) | measured 904 ms @1M finalist rows, paid on EVERY interaction; linear beyond | **fix (a)** |
| one-click top100 | flat by construction (≤100 entries) | nothing | 1.9 ms at any grid size (measured) | nothing |
| whole-lake aggregation (movers-style raw scan) | b ≈ 1 | lake rows (years × symbols): ~1.5–2B rows at 10y × full universe (audit §5) | 2.5 s @50M measured → minutes at 1B+ (extrapolated) — vs **2.7 ms** off the materialized rollup (measured) | **fix (b)** — materialized rollups |
| runs-listing poll (`runs_revision` stat walk) | O(run dirs) stats per poll (deliberately uncapped — the F5 ruling, `oneclick_artifacts.py:886-891`) | accumulated sweep runs (never pruned) | a stat is ~µs — bites only at O(10⁴⁺) runs | **fix (c)** — runs index / retention |
| maintenance walks (prebuild / heal L3 / Greeks scan / summary refresh) | O(files) once per pull/content-token (amortized, write-side) | datasets × months — measured 2,355,002 files today (`lake_manifest.py:6-9`), ≈3.6M at 10y × full universe | walk time roughly doubles by 10y; read-side share already removed by the #1480/#1614 manifests | **fix (c)** — partition-level catalog / rollups |
| frontend uncapped render | paint ≈ N^0.94 (vanilla), N^1.09 (React) | payload rows, IF a surface ever bypasses the wire caps | 29.3 s @100k rows (measured) — the tab dies | **fix (d)** — the bounded contract on every new surface |
| frontend capped render (SHOW_ALL_CAP=1000) | flat O(cap) | nothing (cap binds) | ~190–265 ms regardless of payload (measured) | existing caps |
| frontend windowed render | flat O(visible) | nothing | 0.7–0.9 ms sync, zero longtasks, measured to 500,000 rows | **fix (d)** for any >1000-row surface |

**Stated assumptions:** (i) I/O locality — the benches ran with a warm OS page
cache on cloud block storage; production is a local SSD on the M4 Pro, and
absolute ms are environment-specific either way (§3); (ii) the stats manifest
is present and seeded (the steady production state); (iii) rollups are
materialized for the aggregation surfaces (fix (b)) — the raw-scan row is what
happens without them; (iv) finalists-only persistence is unchanged (the top_k
store law, `results_store.py:537-543` — serving never sees trials); (v) the
partition scheme keeps datasets bounded-depth (one month file per
(symbol, interval, month)).

---

## 8. Recommendation + migration plan sketch (follow-ups, NOT in this PR)

Each with a one-line cost estimate in complexity terms (never time):

- **(a) /results persisted leaderboard rollup** — bounded O(K) serving,
  written at sweep flush time (the #1614 write-hook seam / the
  `top100_artifact` precedent: build once at write time, serve a hard-capped
  artifact). Removes both the b≈1.03 cold scan and the b≈1.17 per-poll rank.
  *Cost: one flush-time rollup writer + one reader repoint + goldens — the
  top100_artifact pattern generalized to the /results surface.*
- **(b) Generalized materialized rollups** for any whole-lake aggregation
  surface (movers-style): the measured 2.7 ms one-day movers READ off a
  PRE-BUILT rollup that cost 3.70 s to build (~940× vs the raw scan),
  incremental-izable per month partition at the prebuild/write funnel.
  *Cost: one rollup builder at the existing partition-locked write hooks +
  per-surface reader repoints with the byte-identical legacy-scan degrade.*
- **(c) Close the audit's other named O(total) gaps** with file rollups:
  the `runs_revision` uncapped per-poll stat walk (a tiny append-only runs
  index rewritten by the run-end pass, or run-dir retention); the O(files)
  maintenance walks (prebuild / heal L3 / Greeks scan / summary refresh) via
  a partition-level catalog — which the #1614 stats-manifest shards
  effectively already are. *Cost: small file-based indexes at existing write
  chokepoints; no new engine, no new service.*
- **(d) Frontend: enforce the viewport-bounded contract on any new surface** —
  server pagination + named caps by default; the ~60-line windowed renderer
  (measured flat to 500k rows) for anything that must expose >1000 rows.
  *Cost: a shared windowed-render primitive + a review-time rule; zero
  framework change.*

Explicitly NOT recommended: ClickHouse/distributed migration (§5.3 costs vs
§4 measured needs), a frontend framework swap, a WASM client kernel (§6.5) —
each carries real cost against a measured non-problem. Revisit triggers in
§10.

---

## 9. Reconciling the Rust-everywhere directive with the KEEP verdict

There is an apparent tension between the operator's Rust-everywhere directive
and "KEEP DuckDB". Stated plainly and resolved:

- **DuckDB is a C++ ENGINE, not Python.** The KEEP verdict keeps a native
  columnar engine that this evaluation measured as the best fit for the
  serving contract; it does not keep any Python hot path. The Python in the
  serving layer is the glue AROUND the engine (FastAPI routes, payload
  builders, cache keying), not the scan itself.
- **The code AROUND the engine can convert to Rust per the directive.** The
  `duckdb` Rust crate + arrow-rs serve DuckDB natively — serving routes and
  payload builders rewritten in Rust would keep issuing the SAME SQL over the
  SAME parquet through the same engine. The Rust conversion and the
  KEEP-DuckDB verdict are **orthogonal**: one chooses the implementation
  language of the glue, the other chooses the scan engine underneath it.
- **A Rust rewrite that preserves the same scan shapes preserves the same
  measured breaches.** The /results warm rank is O(rows)-per-poll (904 ms @1M
  finalist rows) because it ranks the whole finalist pool on every
  interaction — a Rust rank over the same 1M rows is faster by a constant
  factor but still linear, and the cold O(stores × rows) load is
  file-I/O-and-parse-bound, not interpreter-bound. **The two named rollup
  follow-ups (§8 a–b) are the latency fixes regardless of serving language.**
  The frontend measurement (§6.4) is the same lesson one layer up: language
  and framework move constants; the asymptote is set by what the code touches.
- Where Rust genuinely carries the guarantee is where the repo already uses
  it: the trials/compute layer (the `fno_math` + `trading_core` kernels — the
  O(1)-per-trial hot path, Golden Rule #5) — and constant-factor wins in glue
  are welcome under the directive, just never a substitute for the bounded
  scan shapes.

---

## 10. Steelman of the rejected options

Written to survive hostile review — each option's STRONGEST case, why it
loses today, and the explicit revisit trigger.

1. **ClickHouse-class engine (MergeTree).** Strongest case: ad-hoc
   interactive analytics over raw billions of rows — the measured 13×
   point-page lookup (5.9 vs 84 ms) and 4.7× full-lake aggregation
   (771 ms vs 3.6 s @50M) are real, and MergeTree page cost stays ~flat in
   dataset depth. Why not today: the product's serving contract is paged +
   pre-aggregatable — manifest-ON pages are already 12–110 ms and
   lake-size-independent (§4.3), and the one aggregation workload MergeTree
   wins is won ~940× harder by a 2.7 ms materialized rollup (§4.6); the price
   is a second data copy (3.4×), an ingest-sync pipeline, a daemon in the
   zero-services envelope, and the §5.3 dual-write/byte-determinism/audit
   costs. **Revisit trigger: interactive whole-lake ad-hoc analytics (not
   pre-aggregatable) becomes a product requirement.**
2. **Distributed Parquet + scale-out query engine.** Strongest case: the lake
   outgrows a single box. Why not today: the 10-year full-universe lake is
   ~1.5–2B rows ≈ O(100 GB–1 TB) at measured parquet density — comfortably
   inside the 8 TB SSD / single-M4-Pro envelope (§8 of CLAUDE.md; audit §5) —
   and the trillion-scale number is the discarded trial stream, which no
   serving engine touches. **Revisit trigger: the lake outgrows one SSD or
   one box's memory bandwidth.**
3. **React/Svelte (framework swap).** Strongest case: developer ergonomics at
   high UI complexity — components, state management, ecosystem. Explicitly
   NOT a latency lever, measured: React 18 renders the identical table at the
   same O(rows) asymptote (paint ≈ N^1.09 vs vanilla N^0.94) with ~1.4–1.5×
   the JS cost. A future adoption would be an ergonomics decision judged on
   its own merits against the repo's XSS/textContent discipline — never a
   performance justification. **Revisit trigger: UI complexity (not latency)
   makes the vanilla discipline the bottleneck.**
4. **Rust→WASM client kernel.** Strongest case: a heavy client compute path.
   Today's measured maximum at capped input is 15.0 ms (the 85k-cell format
   pipeline); sort-5k is 6.7 ms; the boundary-crossing overhead is of the
   same order as the work. **Revisit trigger: a client compute path >100 ms
   at its capped input appears.**

---

## 11. Honest limits / what was NOT measured

- **No literal 1B+/trillion-row lake was materialized.** The largest built
  lake is 48,915,000 rows (the 3-point fit spans 50×, 1M→50M); everything
  beyond is extrapolation via the fitted exponents, labelled as such
  throughout. The 100M point was deliberately skipped (no new information for
  the fit; the 50M point alone took 7.2 min of writer time).
- **Sandbox absolutes ≠ the production M4 Pro** (4-vCPU Xeon @ 2.10 GHz,
  15 GB RAM, cloud block storage). The curves and exponents are the evidence;
  absolute ms will differ on the M4 Pro (likely faster on every arm).
- **/results warm rank 904 ms was measured at 1M finalist rows in-sandbox** —
  the M4 Pro is likely faster, but the exponent (b≈1.17 per poll) is the
  problem and is hardware-independent.
- **chdb ≠ a tuned ClickHouse server deployment** (embedded, default
  settings, parts as left by one INSERT…SELECT). A tuned server could shift
  the constants; it does not remove the second-copy/dual-write/ops costs
  (§5.3), which are the decision-carrying terms.
- **The frontend was measured on a synthetic-but-shape-identical page**
  (a line-for-line mirror of the screener renderer + the real column/format
  pipeline), not the live dashboard; paint medians carry a ~33 ms double-rAF
  frame-quantum floor; the 50k/100k uncapped and 10k/50k React cells used
  3 reps (runtime bound), everything else 5–15.
- The DB benches ran with a warm OS page cache in both modes (the lakes were
  just written); true cold-storage first-touch would be slower on all arms
  equally. An earlier contaminated measurement pass was discarded and
  re-run cleanly (§3).
- Synthetic 2dp-grid prices compress better than vendor bars (≈4.4–4.6
  bytes/row; real bars a few × larger, same order).

---

## 12. Provenance

- **Benchmark scripts + raw outputs** (session scratchpad of the 2026-07-11
  evaluation session): `db_bench_build.py`, `db_bench_candles.py`,
  `db_bench_fit.py`, `db_bench_results.py`, `db_bench_top100.py`,
  `db_bench_rollup.py`, `db_bench_chdb.py`; raw JSONL `candles_all.jsonl`,
  `results_all.jsonl`, `chdb_all.jsonl`, `control_all.jsonl`;
  `frontend_bench_page.html`, `frontend_bench_runner.py`,
  `frontend_bench_results.json`.
- **The three source reports** (summarized here verbatim-numbered):
  `db_bench_report.md` (DB serving curves, manifest ON/OFF exponents, the
  control experiment, /results breach, top100, VERIFIED chdb A/B, rollup
  probe), `frontend_bench_report.md` (Chromium render curves, React 18
  comparison, client-compute audit), `arch_audit_report.md` (file:line-cited
  serving-path audit, cache/manifest inventory, O(total) gaps,
  migration-cost dimensions, lake-vs-trials framing).
- **Decision ID:** DEC-ARCH-EVAL-TRILLION-SCALE-001 (MIGRATION.md #1723).
- **Hostile review (Golden Rule #9):** independent hostile-review pass run
  2026-07-11 — verdict FIX-FIRST (1 MAJOR: the exec-summary framing conflated
  a pre-built one-day rollup READ with a from-scratch full-lake aggregation;
  3 MINOR: caveat placement on the flat-paging claim, the §2 header's bare
  "O(1)", and the chdb "really tested" wording; 1 NIT: the 0.98M-row label
  harmonization — ALL folded in the same-branch fix commit). Number fidelity,
  15 spot-checked repo citations, ledger format, and steelman quality all
  verified CLEAR.

