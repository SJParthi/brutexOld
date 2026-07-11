# combo_counts.md — grid combo counts + measured wall-clock rebuild

Original derivation 2026-07-08 (repo vocabulary + grid math; the audit's
`early_estimate_inputs.md` / `W05_*` files are unrecoverable — see
`baseline.md` §0; coordinator-relayed targets cross-checked in §5). **§3
REBUILT 2026-07-11** on the Slice-1 branch (`claude/rust-sweep-enum-slice1`,
DEC-SWEEP-ENUM-RUST-001, MIGRATION.md #1738) per the DESIGN_FINAL §9 mandate:
bottom-up from the round-5 SPLIT cost law with on-branch measured constants.
§1 (conflict-free counts) and §2 (tuple multipliers) stand verbatim from the
2026-07-08 derivation.

## 1. Per-tuple mask-space (exact, from the repo)

Vocabulary: 60 implemented conditions (`default_condition_bits()` →
`[0..59]`, `cli.py:109`; `IMPLEMENTED_MASK = (1<<60)-1`). Per tuple the sweep
enumerates every k-subset: `C(60, k)` masks (`iter_masks_k_of_bits`).
Conflict-free counts computed EXACTLY from the live
`brutex/vocabulary/conflicts.py::CONFLICT_PAIRS` (26 pairs) by
independent-set counting over the conflict graph (script in session log;
components brute-forced, polynomial product):

| k | C(60,k) enumerated | conflict-free (reach Stage-1B) | pruned |
|---|---:|---:|---:|
| 1 | 60 | 60 | 0.0% |
| 2 | 1,770 | 1,744 | 1.5% |
| 3 | 34,220 | 32,733 | 4.3% |
| 4 | 487,635 | 446,137 | 8.5% |
| 5 | 5,461,512 | 4,708,180 | 13.8% |
| 6 | 50,063,860 | 40,057,386 | 20.0% |
| **Σ k≤5** | **5,985,197** | **5,188,854** | 13.3% |
| **Σ k≤6** | **56,049,057** | **45,246,240** | 19.3% |

(A conflict-pruned mask costs only enumeration + the pair test — measured
2.66 µs today on the Python spine, ~57 ns in the Slice-1 ranged kernel; it
never reaches the prefilter count or the walk.)

## 2. Tuple multiplier

tuples = instruments × TFs × directions × fib_anchors × |SL| × |TP|.
Scenarios (the audit's exact grid definition is unrecoverable — flagged in
open_questions.md Q1):

| Scenario | tuples | Σk≤5 combos | Σk≤6 combos |
|---|---:|---:|---:|
| A. CLI default shape (2 sym × 3 TFs × 2 dir × 1 anchor × 1 SL × 1 TP) | 12 | 71.8 M | 672.6 M |
| B. Full-scope core (3 instruments × 21 TFs × 2 dir × 1 anchor) | 126 | 754.1 M | 7.062 B |
| C. B × 3 fib anchors | 378 | 2.262 B | 21.19 B |

("combos" here = ENUMERATED masks = tuples × Σ C(60,k); the honest trial
count for Bailey-LdP purposes is the conflict-free/backtested population,
13–19% smaller — both given above.)

> **§3 REBUILT (2026-07-11, Slice-1 branch).** The original §3 wall-clock
> table was WITHDRAWN by the Verifier-A round-1 fold (a per-KEPT ×
> per-ENUMERATED unit conflation — the "~0.58 ms/combo" constant was a
> per-KEPT-MASK residual multiplied by ENUMERATED-mask populations). The §3
> below is the DESIGN_FINAL §9-mandated bottom-up REBUILD from the round-5
> SPLIT cost law with constants MEASURED on this branch's shipped kernel.
> **VENUE (Golden Rule #10): every number in §3 was measured on a SHARED x86
> SANDBOX over SYNTHETIC bars (the deterministic golden-fixture generator) —
> NOT the §8 M4 Pro and NOT the real lake.** The REAL-DATA twin run
> (`scripts/ci/rust_enum_twin_run.sh` / the `rust-enum-twin-run.yml`
> dispatch job) and the M4-Pro re-measurement are named OPERATOR-BOX
> obligations and remain the merge gate for real-lake byte-identity
> (coordinator ruling 2026-07-11). The COMBINATORIAL counts (§1, §2) stand
> verbatim — they are exact repo math, venue-independent.

## 3. Wall-clock rebuild — measured bottom-up (Slice-1 branch, 2026-07-11)

### 3.1 The round-5 SPLIT cost law

Per-tuple wall cost decomposes dimension-honestly (the conflation the
withdrawn table committed is structurally impossible here — each term is
multiplied by its OWN population):

```
T_tuple ≈ C_fixed
        + N_enum · c                                  (enumerate + conflict-prune)
        + N_cf   · (coeff_count · n_bars) · [min_hits > 0]   (fused hits count)
        + N_kept · (f_zero · PK_zero + f_trade · PK_trade)   (Stage-1B survivors)
```

per-ENUMERATED-mask form: `T ≈ c + s·(coeff_count × n_bars)·[min_hits>0] +
s·k_keep·(f_zero·PK_zero + f_trade·PK_trade)` where `s = N_cf/N_enum`
(conflict-free fraction, §1), `k_keep = N_kept/N_cf` (min-hits keep rate),
and `f_zero`/`f_trade` split the KEPT population into zero-trade vs
trade-bearing masks. The `[min_hits>0]` gate is the kernel's SHORT-CIRCUIT
LAW (design §1.3): a `min_hits==0` tuple pays ZERO per-mask counts —
measured directly, 0.001 s vs 0.238 s for the identical 34,220-mask range
(§3.2 row 3).

### 3.2 Measured constants (shared x86 sandbox, synthetic bars)

| # | constant | measured value | how |
|---|---|---|---|
| 1 | `c` — Python spine, enumerate+prune per enumerated mask | **2.66 µs** | C(60,5) = 5,461,512 masks: 14.527 s (`iter_masks_k_of_bits` + `has_conflict`) |
| 2 | `c` — ranged kernel, steady state (shadow burned in) | **56.8 ns** (**46.9×**) | same segment: 0.310 s through `enum_prefilter_via_kernel` |
| 3 | short-circuit law teeth | min_hits=0: **0.001 s**; min_hits=1: 0.238 s | k=3, 34,220 masks, n_bars=93,750 — the =0 arm provably pays no counts |
| 4 | `coeff_count` — fused per-mask hits count, k=3 | **0.077 ns/(mask·bar)** | (min_hits=1 − min_hits=0) ÷ (32,733 cf × 93,750 bars) |
| 5 | `coeff_count` — k=5 | **0.125 ns/(mask·bar)** | C(16,5) subset, 2,984 cf × 93,750 bars — ≈ 0.025 ns per plane·bar, scaling with k |
| 6 | `s` — conflict-free fraction | 0.9667 (k=2) / 0.6832 (k=5) on the 16-bit sub-grid; §1 table for the 60-bit vocab | exact counts |
| 7 | `k_keep` at the production Stage-1B floor (min_hits=30) | 34/116 (k=2) / 15/2,984 (k=5) on the sub-grid fixture | measured via `count_supermasks` |
| 8 | `f_zero` / `f_trade` of KEPT | **0.00 / 1.00** at min_hits=30 on this fixture | kept masks re-simulated (`ResidentTradeSim.simulate_masks`); the min-hits floor removed every zero-trade mask here. The earlier profiled fixture at min_hits=0 read ≈ 66/34 zero/trade — `f_zero` is grid-dependent, NOT a universal constant |
| 9 | `PK_trade` bundle — per KEPT-FINALIST whole-pipeline cost | **≈ 2.5–4.3 µs/(bar·kept)** → 0.24–0.40 s/kept @93,750 bars; 16–19 ms @6,250 | sub-grid walls ÷ kept, both TFs × both k; every kept mask was a finalist (kept ≤ top), so this bundles walk + metrics + the O(top×n_bars) finalist Python-oracle re-sim + store flush — an UPPER bound on the pure per-kept walk |
| 10 | `PK_zero` | **NOT measured on-branch** (`f_zero=0` at the floor — no zero-trade kept mask existed to time) | design two-term law: metrics-on-empty, `c`-class constant |

The design pack's carried 0.257 ns/(mask·bar) `count_supermasks` constant is
the BATCH-FFI path's coefficient; the fused in-kernel count measured
0.077–0.125 (rows 4–5) because it skips per-batch marshalling. Projections
below quote the band [measured, carried] where the count term matters.

### 3.3 Sub-grid twin measurements (both arms, both tops — the §9 mandate)

16-bit condition subset (Python arm sandbox-feasible), min_hits=30, k∈{2,5},
TF∈{1minute-shaped 93,750 bars, 15minute-shaped 6,250 bars}, `--top` default
(20) AND the production `--top=1000`, through the REAL
`run_persisted_sweep`; `store_content_hash` compared per cell
(`scripts/dev_measure_enum_subgrid.py`):

| TF (n_bars) | k | top | OFF (s) | ON (s) | ratio | store hash |
|---|---|---:|---:|---:|---:|---|
| 1minute (93,750) | 2 | 20 | 14.021 | 13.156 | 1.07× | MATCH |
| 1minute (93,750) | 2 | 1000 | 13.592 | 13.568 | 1.00× | MATCH |
| 1minute (93,750) | 5 | 20 | 3.490 | 3.560 | 0.98× | MATCH |
| 1minute (93,750) | 5 | 1000 | 3.541 | 3.490 | 1.01× | MATCH |
| 15minute (6,250) | 2 | 20 | 0.626 | 0.655 | 0.96× | MATCH |
| 15minute (6,250) | 2 | 1000 | 0.634 | 0.707 | 0.90× | MATCH |
| 15minute (6,250) | 5 | 20 | 0.251 | 0.236 | 1.06× | MATCH |
| 15minute (6,250) | 5 | 1000 | 0.238 | 0.238 | 1.00× | MATCH |

All 8 cells byte-identical stores. The end-to-end ratios sit at 0.90–1.07×
because at n=16 bits the enumeration term is microscopic (≤4,368 masks)
against the kept-mask trade/finalist work — the HONEST reading is
"byte-identity + no regression at sub-grid scale", NOT an end-to-end
speedup claim. The enumeration lever itself is row 2's 46.9×, which becomes
material only at full-vocabulary mask counts (§3.4).

### 3.4 Bottom-up projections (8 workers, ideal split — §4 caveat 1 applies)

**Term-1 deltas (what Slice 1 changes).** The kernel replaces ONLY the
enumerate+prune term on the digest-path flat-enumeration arm (apriori
precedence unchanged — a tuple the apriori arm serves never flat-enumerates;
these rows describe the non-apriori arm the kernel serves):

| Grid (from §2) | enumerated | term 1 Python @2.66 µs | term 1 kernel @56.8 ns |
|---|---:|---:|---:|
| B, k≤5 (126 tuples) | 754.1 M | 4.2 min | 5.4 s |
| B, k≤6 | 7.062 B | 39 min | 50 s |
| C, k≤5 (378 tuples) | 2.262 B | 12.5 min | 16 s |
| C, k≤6 | 21.19 B | 2.0 h | 2.5 min |

**Term-2 dominance (unchanged by Slice 1 in magnitude; re-homed in-kernel).**
At `min_hits>0` every conflict-free mask pays `coeff_count × n_bars`. Per
SINGLE tuple at k≤5 (5,188,854 cf masks, k-weighted coefficient dominated by
k=5's 0.125 ns). A tuple runs on ONE worker (`sweep_parallel` splits across
TUPLES), so the per-tuple column is single-worker; the grid-wall column is
its ÷8 amortisation across a many-tuple grid:

| tuple TF | n_bars (2020→2026) | measured coeff (per tuple / grid-wall share) | carried 0.257 (per tuple / grid-wall share) |
|---|---:|---:|---:|
| 1minute | ~555,000 | ≈ 98 h / ≈ 12 h | ≈ 206 h / ≈ 26 h |
| 15minute | ~37,000 | ≈ 6.5 h / ≈ 49 min | ≈ 13.7 h / ≈ 1.7 h |

The 1-minute k≤5 full-vocabulary tuple is COUNT-TERM dominated under EITHER
arm (today's Python spine pays the same O(cf × n_bars) via batched
`count_supermasks` PLUS 2.66 µs/mask enumeration on top) — this is exactly
the design §10.5 hit-list-intersection lever's target, and the ~10%
bit-52/53 dead-condition shrink (§4 caveat 4) helps linearly. Slice 1
strictly improves this arm (term 1 removed, count re-homed at a measured
2–3× cheaper coefficient) but does NOT change its asymptotic shape.

**Term-3 cap.** Per tuple the finalist Python-oracle re-simulation is capped
at `top_k` finalists: at `--top=1000` on a 1-minute tuple, ≤1000 ×
~0.24–0.40 s ≈ **≤ 4–7 min/tuple** (row 9's bundle; the gap-register #1
`top=1000` cost, measured not estimated). Zero-trade kept masks (`f_zero`)
skip the walk — on min_hits≥30 grids this fixture measured `f_zero=0`, so
budget with `f_trade≈1` unless the grid drops the floor.

### 3.5 Not measured here (named operator-box obligations)

1. **Real-lake byte-identity twin** — `scripts/ci/rust_enum_twin_run.sh`
   (or the `rust-enum-twin-run.yml` M4-Pro dispatch job); a MERGE GATE
   (coordinator ruling 2026-07-11).
2. **M4-Pro re-measurement of rows 1–5, 9** (`bench-m4pro` lane;
   DEC-BENCH-ABS-BASELINE-001 governance) — sandbox→M4-Pro translation is
   unverified (§4 caveat 5).
3. Real-bar densities (`s` is exact math, but `k_keep`/`f_zero`/`f_trade`
   on REAL 2020→2026 bars are fixture-dependent — row 8's caveat).

## 4. HONEST CAVEATS (must accompany any use of §3)

1. **Parallel efficiency is NOT 8×.** The in-repo `sweep_parallel` honesty
   envelope says the realistic ceiling on the §8 M4 Pro is **~6–8×, not 10×**
   (single writer-actor fsync + reorder + marshalling eat ~20–40%). Multiply
   the table by ~1.15–1.35 for a realistic band.
2. **Per-combo cost is NOT flat across TFs.** Every per-mask operation that
   survives conversion is O(n_bars): the fused prefilter count costs
   `coeff_count × n_bars` per conflict-free mask (measured 0.077–0.125
   ns/(mask·bar) on this branch's kernel, §3.2 rows 4–5; the batch-FFI path
   carried 0.257) — ~43–69 µs/mask at the 1-minute TF's ~555k-bar 2020→2026
   window — and the walk ~1.28 ns/bar per KEPT mask. A flat per-combo
   constant is therefore optimistic for 1m/2m/3m/5m tuples and conservative
   for the higher TFs; §3.4's per-TF rows replace the withdrawn flat table.
3. **Non-per-combo overheads that do NOT shrink with the per-combo constant**
   (add on top; estimates from `baseline.md` §2 sandbox rates, scenario B):
   - Lake bars load: O(window) per (instrument, TF) — minutes total.
   - Stage-1A `build_bar_bits`: ~14.5 µs/bar → ~55–60 s per instrument-set
     per anchor (amortised by `ResidentKernelCache`).
   - Trade-sim/core construction: O(n_bars) FFI copy per tuple — ~tens of s
     total.
   - **Finalist projection: O(top_k × n_bars) Python-oracle re-simulation per
     tuple** — now MEASURED at §3.4's term-3 cap (≤ 4–7 min/tuple at
     `top=1000` on a 1m tuple, sandbox) instead of estimated.
   - Store flush + fsync + sidecar + sentinel + 3 audit lines per tuple:
     ~50–100 ms × tuples ≈ seconds–minutes.
   - Checkpoint/writer-actor apply per tuple (parallel driver): small; dedup
     flushes: NONE on the digest path (`baseline.md` §4).
4. **60-vs-58 conditions (gap-register #8):** VWAP bits 52/53 are structurally
   dead on spot (volume=0), so ~10% of enumerated combos (those containing
   bit 52 or 53) can never fire on spot tuples — they are prefilter-dropped
   cheaply but still enumerated+counted. The tables count them as combos
   (matching today's engine); the P1/P2 exclude-vs-flag decision may shrink
   the space by ~10% later.
5. Sandbox→M4-Pro translation is unverified until the bench-m4pro lane runs
   (§8 CLAUDE.md; DEC-BENCH-ABS-BASELINE-001).

## 5. Sanity-check vs the coordinator's targets — HISTORICAL (rested on the SUPERSEDED pre-rebuild §3)

> This section cross-checked the coordinator-relayed targets against the
> WITHDRAWN flat-µs §3 table's arithmetic, so it inherited the per-KEPT ×
> per-ENUMERATED unit conflation (Verifier A round-1). Retained verbatim as
> the historical record of what the relayed targets were consistent with
> UNDER the withdrawn arithmetic; the measured replacement is §3.4.

Relayed targets: "k≤6: 4–14 d → ~0.5–2.5 d; k≤5 → ~1–8 h" at 10–50 µs/combo.
- Today k≤6: scenario B gave 5.9 d, C 17.8 d — the 4–14 d band brackets
  B and the low-anchor side of C ✓ (the audit's grid was plausibly B or a
  partial-anchor variant).
- Post k≤6 @10–50 µs: B → 2.5–12.3 h, C → 7.4 h–1.5 d — the "~0.5–2.5 d" band
  matches scenario C and the 50 µs edge of B ✓.
- Post k≤5 @10–50 µs: B → 16 m–1.3 h, C → 47 m–3.9 h — "~1–8 h" matches C ✓.
The targets are internally consistent with a 3-anchor (or SL/TP≈3-point)
full-scope grid; exact grid definition needs the operator/coordinator
confirmation (open_questions.md Q1).
