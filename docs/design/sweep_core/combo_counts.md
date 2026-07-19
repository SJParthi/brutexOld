# combo_counts.md — grid combo counts + post-conversion wall-clock projections

Date 2026-07-08. Derivation re-done from the repo's own vocabulary + grid math
(the audit's `early_estimate_inputs.md` / `W05_*` files are unrecoverable —
see `baseline.md` §0; the coordinator-relayed targets are cross-checked in §5).

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

(A conflict-pruned mask costs only enumeration + `has_conflict` — measured
2.4 µs today, ~tens of ns post-conversion; it never reaches the prefilter or
the walk.)

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

> **SUPERSEDED FOR PROJECTIONS (Verifier A round-1 fold, 2026-07-09).** The §3
> wall-clock table below (and the §5 sanity-check built on it) rests on a
> per-KEPT × per-ENUMERATED unit conflation: the "~0.58 ms/combo" constant is
> a per-KEPT-MASK residual (SPEC §5) multiplied here by ENUMERATED-mask
> populations — arithmetically impossible under DESIGN_FINAL §9's own per-kept
> cap. Every projection row (the "today" day/hour anchors and the
> @50/@30/@10 µs columns) is **WITHDRAWN**; projections are SUPERSEDED by
> DESIGN_FINAL §9's two-term tables + the §9.1 BINDING two-term cost law
> (`cost(mask) = c0 + ≈6.2 ns/mask·bar + ≈4.1 µs/trade`, pinned config; ratio
> band only). This file's §3 is scheduled for the DESIGN §9-mandated bottom-up
> REBUILD from measured s/f_zero/f_trade after the on-branch sub-grid run. The
> COMBINATORIAL counts themselves (§1 conflict-free table, §2 tuple
> multipliers) STAND — only the wall-clock projections are withdrawn.

## 3. Wall-clock table (8 workers, ideal scaling — see caveats §4) — WITHDRAWN (see banner above)

wall = combos × per-combo-cost ÷ 8. "Today" row uses the coordinator-relayed
~0.58 ms/combo [AUDIT-RELAYED; my sandbox re-derivation measured 0.16–0.40
ms/kept-mask at 20k–48k bars — same order, `baseline.md` §2].

| Grid | combos | today @0.58 ms | @50 µs | @30 µs | @10 µs |
|---|---:|---:|---:|---:|---:|
| **k≤5**, B (126 tuples) | 754.1 M | 15.2 h | 1.31 h | 47 min | 16 min |
| **k≤5**, C (×3 anchors) | 2.262 B | 1.90 d | 3.93 h | 2.36 h | 47 min |
| **k≤6**, B (126 tuples) | 7.062 B | 5.93 d | 12.3 h | 7.4 h | 2.45 h |
| **k≤6**, C (×3 anchors) | 21.19 B | 17.8 d | 1.53 d | 22.1 h | 7.4 h |

Formulae: e.g. k≤6/B @30 µs = 7.062e9 × 30e-6 / 8 = 26,483 s ≈ 7.4 h.

## 4. HONEST CAVEATS (must accompany any use of this table)

1. **Parallel efficiency is NOT 8×.** The in-repo `sweep_parallel` honesty
   envelope says the realistic ceiling on the §8 M4 Pro is **~6–8×, not 10×**
   (single writer-actor fsync + reorder + marshalling eat ~20–40%). Multiply
   the table by ~1.15–1.35 for a realistic band.
2. **Per-combo cost is NOT flat across TFs.** Every per-mask operation that
   survives conversion is O(n_bars): even the cheapest prefilter count
   (0.257 ns/bar·mask sandbox) costs ~143 µs/mask at the 1-minute TF's
   ~555k-bar 2020→2026 window, and the walk (1.28 ns/bar) ~710 µs/KEPT-mask
   (sandbox; M4 Pro likely ~2× faster). So a FLAT 10–50 µs/combo is
   optimistic for the 1m (and to a lesser degree 2m/3m/5m) tuples and
   conservative for the 19 higher TFs; the blended average lands in the
   10–50 µs band only if the k=5/6 population is dominated by
   prefilter-dropped masks on mid/high TFs, or if the prefilter is upgraded
   to hit-list intersection (integer-exact, O(min-bit-frequency) — a named
   phase-next lever, DESIGN §10.5). The table is therefore a PROJECTION whose
   1m-TF rows are its weakest cells.
3. **Non-per-combo overheads that do NOT shrink with the per-combo constant**
   (add on top; estimates from `baseline.md` §2 sandbox rates, scenario B):
   - Lake bars load: O(window) per (instrument, TF) — minutes total.
   - Stage-1A `build_bar_bits`: ~14.5 µs/bar → ~55–60 s per instrument-set
     per anchor (amortised by `ResidentKernelCache`).
   - Trade-sim/core construction: O(n_bars) FFI copy per tuple — ~tens of s
     total.
   - **Finalist projection: O(top_k × n_bars) Python-oracle re-simulation per
     tuple** — at the institutional `top=1000` (gap-register #1) on a 1m
     tuple this is ~50–500 ms × 1000 ≈ minutes–hours per grid; the ONE
     per-tuple cost that could rival the converted loop. Flagged in
     open_questions.md Q6 (not in this PR's scope — #1182 non-goal).
   - Store flush + fsync + sidecar + sentinel + 3 audit lines per tuple:
     ~50–100 ms × tuples ≈ seconds–minutes.
   - Checkpoint/writer-actor apply per tuple (parallel driver): small; dedup
     flushes: NONE on the digest path (`baseline.md` §4).
4. **60-vs-58 conditions (gap-register #8):** VWAP bits 52/53 are structurally
   dead on spot (volume=0), so ~10% of enumerated combos (those containing
   bit 52 or 53) can never fire on spot tuples — they are prefilter-dropped
   cheaply but still enumerated+counted. The table counts them as combos
   (matching today's engine); the P1/P2 exclude-vs-flag decision may shrink
   the space by ~10% later.
5. Sandbox→M4-Pro translation is unverified until the bench-m4pro lane runs
   (§8 CLAUDE.md; DEC-BENCH-ABS-BASELINE-001).

## 5. Sanity-check vs the coordinator's targets — WITHDRAWN (rests on the §3 conflation; see the §3 banner)

> This section cross-checks the coordinator-relayed targets against the §3
> table's arithmetic, so it inherits the same per-KEPT × per-ENUMERATED unit
> conflation and is SUPERSEDED for projections by DESIGN_FINAL §9/§9.1
> (Verifier A round-1). Retained verbatim as the historical record of what the
> relayed targets were consistent with UNDER the withdrawn arithmetic.

Relayed targets: "k≤6: 4–14 d → ~0.5–2.5 d; k≤5 → ~1–8 h" at 10–50 µs/combo.
- Today k≤6: scenario B gives 5.9 d, C gives 17.8 d — the 4–14 d band brackets
  B and the low-anchor side of C ✓ (the audit's grid was plausibly B or a
  partial-anchor variant).
- Post k≤6 @10–50 µs: B → 2.5–12.3 h, C → 7.4 h–1.5 d — the "~0.5–2.5 d" band
  matches scenario C and the 50 µs edge of B ✓.
- Post k≤5 @10–50 µs: B → 16 m–1.3 h, C → 47 m–3.9 h — "~1–8 h" matches C ✓.
The targets are internally consistent with a 3-anchor (or SL/TP≈3-point)
full-scope grid; exact grid definition needs the operator/coordinator
confirmation (open_questions.md Q1).
