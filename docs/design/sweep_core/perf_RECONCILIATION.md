# RECONCILIATION — the "10× disputed" Rust trade-walk cost, settled by a two-term fit

Date: 2026-07-09 · Host: sandbox x86 container, Intel Xeon @2.80 GHz, 4 vCPU, Python 3.12.3
(**NOT the §8 M4 Pro** — both disputed headline numbers were also produced in this container
class, so container-local reconciliation is the valid comparison).

Capture base: origin/main 665181f3 — `sweep_parallel.py:558` at capture; the pin lives at `:842`
as of 95955a71; semantics unchanged. (Verifier A round-2.) `fit_results_pinned.json` is
deliberately NOT stamped with this header: Verifier A round-2 proved that re-running
`perf_reconcile_raw/fit_and_report3.py` against the committed grids reproduces its bytes
BYTE-IDENTICAL, so its capture-base provenance lives here + in `perf_reconcile_raw/README.md`
instead of inside the frozen JSON.

## 0. TL;DR verdict

**Both sessions' NUMBERS were (approximately) right; both ATTRIBUTIONS were wrong.**
The dispute dissolves into two confounders, only one of which is the hypothesized density term:

1. **THREAD PINNING (the dominant confounder, ~3.8×):** production sweep workers pin
   `RAYON_NUM_THREADS=1` (`brutex/engine/sweep_parallel.py:558`, verified in-repo), but the
   `fno_math.ResidentSweepSim` batch kernels fan a ≥ `1<<20`-cell batch across rayon's global
   pool (`rust/fno-math/src/trade_sim.rs:127,704,742`). My session's ≈1.26–1.28 ns/mask·bar
   was an **UNPINNED, 4-way-parallel WALL rate of the count-only tier** — reproduced here at
   1.53–1.72 ns/bar (unpinned chunk-86) vs **6.1–6.2 ns/mask·bar pinned/serial**. Measured
   unpinned rayon kernel speedup: **3.77× @48.6k, 3.69× @93.9k, 3.51× @300k, 3.00× @604k bars**
   (confirms the counterparty's ~3.97× within shared-vCPU noise). **Never quote the unpinned
   rate for the production sweep.**
2. **TRADE-DENSITY TERM (the hypothesized term — real, and it lives almost entirely in the
   PYTHON wrapper):** the production op (`ResidentTradeSim.simulate_masks_with_exit_indices`)
   pays ≈ **4.1 µs per closed trade** for per-trade `ClosedTrade` + exit-index materialisation
   (the raw Rust record marshal is only ≈0.35 µs/trade; the kernel walk itself is
   density-flat at ≈0.02 µs/trade). At the audit's 1,423 trades/mask that is ~5.8 ms of
   Python per mask on top of a ~3.7 ms pinned walk — the audit's "12.8 ns/mask·bar" is a
   **density-dominated effective rate, not a walk rate**.

## 1. Wheel / build identity

| artifact | version | file | sha256 |
|---|---|---|---|
| fno_math | 0.3.0 | `fno_math-0.3.0-cp312-abi3-manylinux_2_34_x86_64.whl` | `6397ca9d89935397daaf0941ae8094e34ec014a10a9015fb9caf3cc11832363d` |
| trading_core | 0.3.1 | `trading_core-0.3.1-cp312-abi3-manylinux_2_34_x86_64.whl` | `abbd38b648a9e53d96d8cc84cc7c9f5e10cdab829c4d203c8d56c548c86f10ed` |

Built earlier this session from the repo's own `rust/` crates with `maturin build --release`
(recorded in `../baseline.md` §0); venv: `../venv312` (Python 3.12.3, numpy 2.5.1,
polars 1.42.1, pyarrow 24.0.0). The audit session's scratchpad
(`754b4f2b-.../scratchpad/` — FIX-L/FIX-M fixtures, their venv, `env_build.log`,
`measured_costs.json`) is **GONE from this container** (verified `ls`: no such directory),
so their wheel identity could not be byte-matched; same repo crates + same maturin release
profile is the closest available match.

## 2. Fixture identity

Audit FIX-L absent ⇒ **equivalent regenerated fixture**: 604,485 bars of 1-minute-shaped
synthetic data (374 bars/session 09:16–15:29 IST, consecutive weekdays from 2020-01-02,
mean-reverting random walk σ=₹4/min around 22,000, 2dp Decimal, seed 20260709).
`sha256 = 7249c7a388e4ad9fbe8af0a50180d02bc646dd265049d9b84cdf1f89fe1578f9` (over
`ts|o|h|l|c|volume` lines). Grid n_bars = prefixes {48,620 · 93,874 · 299,948 · 604,485}.

Trade density is controlled by SYNTHETIC bar_bits (bit *b* fires every K bars at a phase
offset; 10 phase-offset single-bit masks per density cell; SL ₹8 / TP ₹12 ⇒ holding a few
bars ≪ K, so trades/mask ≈ n_bars/K, verified from the sim output). The kernel cost cannot
depend on how bar_bits were produced; a Stage-1A control cell (SL ₹80/TP ₹120, 43.5
trades/mask) matches the model within ~4% (431 µs measured vs ~415 µs predicted), so the
tight-SL/TP density control does not distort the fit.

## 3. Raw grid — PINNED, `RAYON_NUM_THREADS=1` (THE PRODUCTION CONFIG) — primary numbers

Production op = `prod_wrapper` (production chunk sizes 86/44/13/6 masks/call,
best-of-5, µs per mask). Full JSON: `grid3_pinned_raw.json`.

| n_bars | trades/mask | count (walk) | raw (+FFI marshal) | wrapper (PRODUCTION op) |
|---:|---:|---:|---:|---:|
| 48,620 | 5.0 | 286.0 | 286.6 | 300.6 |
| 48,620 | 43.5 | 288.4 | 299.4 | 431.2 |
| 48,620 | 244.5 | 296.6 | 373.9 | 1,056.7 |
| 48,620 | 683.9 | 318.2 | 540.2 | 2,955.1 |
| 48,620 | 1,401.3 | 348.2 | 834.8 | 6,414.1 |
| 48,620 | 1,514.5 | 341.2 | 889.3 | 6,537.2 |
| 93,874 | 4.9 | 559.8 | 555.1 | 576.0 |
| 93,874 | 43.8 | 555.4 | 576.4 | 690.7 |
| 93,874 | 244.1 | 562.8 | 614.0 | 1,296.2 |
| 93,874 | 680.0 | 579.6 | 835.2 | 3,213.7 |
| 93,874 | 1,359.9 | 618.6 | 1,128.5 | 5,499.4 |
| 93,874 | 1,493.5 | 608.5 | 1,095.5 | 6,646.7 |
| 299,948 | 4.9–1,489 | 1,777–1,833* | 1,773–2,072* | 1,819–6,837* |
| 604,485 | 5.0 | 3,684** | 3,675** | 3,770** |
| 604,485 | 1,361.5 | 3,706.7 | 4,145.5 | 9,057.8 |
| 604,485 | 1,489.1 | 3,780.5 | 4,184.3 | 9,349.2 |

\* full rows in `grid3_pinned_raw.json`; \*\* serial-arm values shown where prod-arm cell
identical within spread. Serial arm (1 mask/call) agrees with prod arm on the kernel tiers
(pinned rayon ⇒ no fan-out); prod-arm wrapper runs slightly hotter at extreme density
(86-mask × ~1,500-trade chunks ⇒ ~130k live `ClosedTrade`s/call — real allocation/GC
pressure the production shape actually pays).

Unpinned control grid (the misleading config): `grid2_unpinned_raw.json` — kernel wall
cost ÷3.0–3.8 vs pinned; e.g. @48,620 bars prod_count = 75.9 µs (1.56 ns/bar) vs pinned
286 µs (5.88 ns/bar).

## 4. Fitted two-term model — PINNED, production arm

`cost(mask) [µs] = c0 + walk_rate·n_bars + trade_cost·n_trades` (global LSQ over all 24
cells; best-of-5 per cell; full residuals in `fit_results_pinned.json`):

| tier | c0 (µs) | walk_rate (ns/mask·bar) | trade_cost (µs/trade) | R² | rms resid |
|---|---:|---:|---:|---:|---:|
| count (kernel walk) | −4.9 | **6.204** | 0.020 | 0.9994 | 2.9% |
| raw (kernel + record marshal) | −4.6 | **6.151** | **0.351** | 0.9990 | 3.4% |
| **wrapper (production op)** | −83.7 | **6.195** | **4.106** | 0.9927 | 7.8% |

Per-n_bars intercepts confirm the walk term is LINEAR (5.9–6.35 ns/bar at every window
size — no cache cliff up to 604k bars, arrays ≈29 MB). c0 is statistically ≈0 (small
negative = slight sublinearity absorbed by the intercept; |c0| ≪ any cell).

**vs the counterparty's fit** (pinned walk 4.82–5.84 ns/mask·bar, B = 3.5–4.2 µs/trade,
R²=0.9996): **trade_cost CONFIRMED inside their band** (4.11 prod-arm / 3.50 serial-arm);
**walk_rate confirmed within +6% of their upper bound** (6.20 vs 5.84; +29% vs their 4.82
lower bound) — cross-container/bar_bits-shape variance on a shared 4-vCPU box; direction,
linearity and magnitude all agree. Model CONFIRMED.

## 5. Verdict on the disputed headlines

| headline | claimed | model says (pinned, production op) | attribution verdict |
|---|---|---|---|
| Mine: "walk ≈1.26–1.28 ns/mask·bar" (62 µs @48,620 bars, 43.6 tr) | 62 µs | wrapper **397 µs (8.15 ns/bar)**; even count-only pinned = 298 µs (6.12 ns/bar) | **WRONG ATTRIBUTION, twice**: (a) unpinned 4-way rayon wall rate quoted as the walk rate — production pins to 1 thread (÷3.8 illusion); (b) count-only tier quoted for a path whose production cost includes per-trade materialisation. Reproduced unpinned: 76–83 µs (1.5–1.7 ns/bar) — the 62 µs was real but is NOT a production walk rate. |
| Audit FIX-L: "12.8 ns/mask·bar" (7.7 ms @604,485 bars, 1,423 tr) | 7,700 µs | wrapper **9,504 µs (15.7 ns/bar)** — claim is −19% vs my fit (fine across containers; they report +2.0% vs their own fit) | **NUMBER RIGHT, LABEL WRONG**: at 1,423 trades, ~5.8 ms of it is the 4.1 µs/trade Python term; the true walk is ≈6.2 ns/bar (3.7 ms). Calling the density-dominated effective rate a "walk" rate over-states the walk by ~2×. |
| Audit FIX-M: "4.35 ns/mask·bar" (@93,750 bars, ~250 tr) | ~408 µs | wrapper **1,524 µs (16.2 ns/bar)**; pinned walk ALONE @93.9k = 582 µs > the whole claim | **DOES NOT FIT either config on my container** — 4.35 ns/bar is below even the counterparty's own fitted pinned walk band (4.82–5.84), so this point is internally inconsistent with their fit too; most plausibly an unpinned / partially-parallel measurement. Flagged, unresolved. |

**Does the two-term fit reconcile 1.26 ns@43.6-tr with 12.8 ns@1,423-tr?** Not by density
alone — density explains the audit side (12.8 ns ≈ walk 6.2 + 1,423×4.1 µs ⁄ 604,485 bars
≈ 15.7 ns/bar, same regime), but my 1.26 ns headline additionally needed the **rayon
pinning confounder** (unpinned ≈ pinned ÷ 3.8) and the count-only tier. With BOTH
corrections applied, every measurement lands on the one fitted model.

## 6. Campaign implication (pinned coefficients, honest band)

Assumptions (stated, not measured from a real lake): FIX-L shape 604,485 bars; real
kept-mask densities per the coordinator: k=2 median ≈1,532 trades/mask, 7.7% zero-trade;
Python per-combo residual deleted by the port = 0.40–0.58 ms (my 48k-bar sandbox marginal
/ the audit's figure). Floor after the port: `raw` tier if the per-trade marshal moves to
Rust too (aggressive), `wrapper` tier if `ClosedTrade` materialisation stays Python
(conservative).

| density @604,485 bars | wrapper (now) | raw floor | speedup (floor=raw) | speedup (floor=wrapper) |
|---|---:|---:|---:|---:|
| zero-trade (7.7% of masks) | 3.66 ms | 3.71 ms | 1.09–1.14× | 1.11–1.16× |
| sparse 220 tr | 4.56 ms | 3.79 ms | 1.31–1.36× | 1.09–1.13× |
| mid 950 tr | 7.56 ms | 4.05 ms | 1.97–2.01× | 1.05–1.08× |
| **k=2 median 1,532 tr** | 9.95 ms | 4.25 ms | **2.44–2.48×** | 1.04–1.06× |

Density-mix (7.7% zero + 92.3% @1,532): **≈2.3–2.4×** for the FIX-L campaign shape.
Window-size sensitivity: at 48,620-bar windows the walk floor is only ~0.3 ms so the
deleted residual dominates → up to ≈**3.1×** there; at 604k-bar sparse masks it bottoms at
≈1.3×. **Honest campaign band: ≈1.3–3.1× shape-dependent; ≈2.3–2.5× at the measured FIX-L
median density** — overlapping the counterparty's 1.7–2.2× (measured density) / ~2.7×
(sparse-short-window) / ~3× ceiling within cross-container spread, with one disagreement
in shape: under MY fit the >2× gains require either high trade density (per-trade Python
deleted) or short windows (residual dominates); a sparse mask on the FULL 604k window gains
the least. **The kernel walk (≈6.2 ns/mask·bar·core pinned, this container) is the floor no
port removes.** All speedups are single-worker; the 8-worker process pool multiplies
throughput orthogonally.

## 7. Artifacts

- `grid3_pinned_raw.json` — PRIMARY: pinned (production-config) grid, 24 cells + control
- `grid2_unpinned_raw.json` — unpinned control grid (the misleading config; serial arm within it is pin-equivalent)
- `grid_raw.json` — v1 grid (10-mask batches; mixed serial/parallel arms — superseded, kept for provenance)
- `fit_results_pinned.json` — fitted coefficients, residuals, disputed-point predictions, campaign rows
- `measure_grid.py` / `measure_grid2.py` / `grid3_pinned.py` / `fit_and_report3.py` / `probe_walk_discrepancy.py`
- probe transcript (rayon confounder discovery): task outputs `b4ayfz98a` (probe A–E: earlier-shape repro 83.4 µs unpinned; zero-match mask serial 271–285 µs vs chunk-86 76 µs)
