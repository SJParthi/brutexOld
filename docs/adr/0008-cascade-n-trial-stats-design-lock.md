# ADR-0008 — Stage-4 cascade leaderboard trial-stats (the cascade-N gap): design lock (IMPLEMENTABLE-WITH-CONDITIONS)

> **Decision:** DEC-CASCADE-N-DESIGN-LOCK-001 (MIGRATION.md entry #642)
> **Date:** 2026-06-10 · **Status:** ACCEPTED — implementation may begin only
> against the 10 binding conditions below.
> **Method:** Golden-Rule-#9 review-before-code — a facts agent mapped the
> current surface, an architect drafted the option space, and a hostile
> reviewer attacked every option (the ADR-0006/ADR-0007 fan-out pattern).
> **Verdict:** IMPLEMENTABLE-WITH-CONDITIONS for the per-tuple trial-stats
> sidecar + manifest freeze (option A′); the three rival options are
> INFEASIBLE (§3).

## 1. The gap (verified, quantified, anti-conservative)

The Stage-4 cascade's Deflated-Sharpe filter (#2) deflates each candidate
against `n_trials = len(pool)` and `trial_var = pvariance(pool_sharpes)`
over the ENTERING candidate pool (`brutex/metrics/cascade.py:443,460-469`)
— the per-tuple top-K winners, typically 10²–10⁴ rows — while the spec
demands the per-(instrument, TF) leaderboard trial count, N ≤ 10⁷, with
EVERY backtested combo and every Stage-3 refined evaluation counted
(`TRACK1_BACKTEST_SPEC.md:421-423,437-440`). `SR₀ ∝ sqrt(V)·sqrt(2 ln N)`:
at equal V, N = 2×10³ understates the benchmark ~35-55% vs N = 10⁷, and V
is additionally selection-truncated (the pool's Sharpes are top-K order
statistics, not the cross-trial dispersion Bailey-LdP defines). Both errors
point the same way: **the cascade DSR passes data-snooped noise**. The
decisive aggravator: `--overfit-gate` is default OFF, so in the default
pipeline the pool-N cascade DSR is the ONLY multiple-testing control
anywhere. Disclosed as the pre-existing gap in MIGRATION.md #638
(DEC-STAGE3-WIRE-001); this ADR is the design lock that closes it.

The structural cause: the true N exists only at rank time —
`run_persisted_sweep` holds every backtested `(mask, ResultMetrics)` pair
in memory and discards all but the top-K finalists at persistence
(`brutex/ranking/overfit.py:35-49` documents why post-persistence
`N = len(rows)` is statistically dishonest). One leaderboard spans MANY
grid tuples (direction × k × SL × TP axes); per-tuple tallies live only in
audit payloads; nothing persists per-leaderboard N or V.

## 2. The locked design (option A′)

**Per-tuple trial-stats sidecar + manifest freeze.** At rank time each grid
tuple writes a small deterministic JSON sidecar beside its Parquet store
(`<store>.parquet.trials.json`): `n_backtested`, `n_finite` (finite-Sharpe
count), `sum_sharpe`, `sum_sharpe_sq` (both `math.fsum` over finite
Sharpes), the prune tallies (`n_enumerated`, `n_conflict_pruned`,
`n_minhits_dropped` — audit context that makes the N definition flippable
without re-sweeping), and the pairing identity (condition 5). The Stage-3
pass writes the same sidecar per refined cell from the RAW in-memory
metrics. At cascade-build time the launcher merges sidecars per
(instrument, tf) leaderboard — exact union variance from the moments —
and FREEZES the merged numbers into the cascade manifest; both the
AUTOMATIC and the MANUAL cascade paths consume only the frozen numbers.
`run_stage4_cascade` gains an optional per-pool trial-stats input; filter
#2 uses it when present and falls back to today's pool-N LOUDLY when not.

## 3. Rejected options (each with the killing finding)

- **(B) Launcher in-memory accumulation → manifest only: INFEASIBLE.**
  The designed resume paths skip whole tuples without ever producing a
  summary (checkpoint resume + cache skip in `sweep_supervisor.py:563-587`;
  dedup-resume yields zero survivors in `sweep_run.py:721-723`) — a
  killed-and-resumed sweep would freeze a smaller, WRONG (N, V) with no
  way to detect it. Violates Golden Rule #7 structurally.
- **(C) ResultRow/store schema columns: INFEASIBLE as the N/V carrier.**
  A tuple that backtested thousands of combos but ranked zero finalists
  persists NO rows — its trials vanish from N; rows lack (sl, tp,
  proximity) so grouping needs BLAKE3 inversion over the grid product; and
  every field participates in the store's append-equality, so rank-time
  recomputed stats turn benign re-runs into `ResultsStoreError`s and break
  every golden fixture.
- **(D) Recompute N at cascade time + persist only V: INFEASIBLE as
  specified.** The manifest lacks the grid context (condition_bits /
  k_values / axes); min-hits recounts are lake-state-sensitive and
  `has_conflict` is vocabulary-version-dependent — N would DRIFT silently
  instead of failing loudly (a Golden Rule #1 inversion); and it still has
  to persist per-tuple V sums, i.e. it CONTAINS option A's hard part plus
  new failure modes. The deterministic recount survives only as a TEST
  ORACLE cross-checking the sidecar on small fixtures.

## 4. The 10 binding conditions (violating any = red review)

1. **Manifest freeze (F5).** AUTO aggregates sidecars exactly once and
   freezes per-(instrument, tf) `{n_trials, n_finite, sharpe_sum,
   sharpe_sumsq}` into the manifest (additive field — the `stage3` block
   precedent); both cascade paths consume ONLY the frozen numbers;
   sidecars are build-time inputs, never replay-time inputs. Zero-finalist
   tuples' trials are counted (the builder iterates grid tuples, not
   specs).
2. **Write ordering + overwrite policy (F4).** Per tuple: `store.flush()`
   → sidecar write (tempfile + `os.replace`, sorted keys, repr floats,
   `allow_nan=False` — the store's own determinism discipline) →
   `dedup.mark_many()`. The sidecar is recomputed-and-overwritten **iff
   `skipped_dedup == 0`** (a full evaluation), and PRESERVED otherwise —
   merging accumulated scalars is FORBIDDEN (no keyed dedup exists for
   sums; a partial redo would double-count).
3. **N pinned (F7).** N(leaderboard) = Σ over the slice's grid tuples of
   `n_backtested` (post-conflict, post-dedup-novel, post-min-hits — the
   combos that actually ran `metrics_for_mask`, matching the in-sweep
   gate's `n_trials = len(candidates)` precedent) + Σ **unique persisted
   Stage-3 refined rows** of the slice, across BOTH directions (the wire
   pools bull+bear per slice). None-Sharpe trials count in N and are
   excluded from V (the `pool_sharpe_dispersion` convention) — the sidecar
   carries both `n_backtested` and `n_finite` so the None-fraction is
   answerable from data. The same-population invariant comment at
   `cascade.py:457-459` and the spec text are updated in the same PR that
   switches the N source.
4. **Stage-3 contribution (F8).** Accumulated at pass time from each
   `refined_eval.metrics` (the raw `float | None` Sharpe), **deduped by
   `refined_id`** — never from persisted row `sharpe_ratio` (None→0.0
   projection would inject fake zeros into V) and never from
   `evals_total` (it counts per-run cache misses; two survivors sharing a
   mask can both pay a cache-miss for the same quantized point that
   persists as ONE row — `evals_total ≥ unique trials`). The existing
   docstring claims that "`evals_total` IS the DSR-N contribution"
   (`stage3_refine.py:34,199`; `stage3_wire.py` report field docs) are
   corrected in the same PR; the audit keeps both counts.
5. **Sidecar pairing identity (F4c/F5).** Each sidecar carries the tuple
   digest (`_tuple_digest`) + the store content hash it was written
   beside; cascade-build FAILS LOUDLY on mismatch or unexpected absence
   (modulo condition 6's legacy path). Drift is detectable, never silent.
6. **Three-state back-compat + mandatory source stamp (F13).** Manifest
   field ABSENT (old manifest) → pool-N fallback with a structlog warning
   + per-group report stamp `dsr_n_source: "pool"`; present →
   `"leaderboard"` with the frozen N/n_finite/V echoed in the report
   (additive keys at REPORT_VERSION 1 — the DEC-PBO-SIDECAR-001 three-state
   precedent). Old trees (stores without sidecars) mark the leaderboard
   incomplete → all-or-fallback per group, flagged, never a fabricated
   partial N.
7. **Two-DSR labeling (F10).** Post-fix a candidate carries TWO deflated
   numbers: the row's `deflated_sharpe` (per-tuple N, the in-sweep gate)
   and the cascade DSR (leaderboard N). Every surface that shows either
   labels which N it used. Re-deflating at the larger N supersedes (is
   strictly stricter than) the per-tuple gate — no alpha is double-spent.
8. **Amendment-A1 window mixing documented (F9).** Stage-3 trials'
   Sharpes are measured on the lockbox-excluded train slice; grid trials
   on the full window; pooling them into one V mixes estimators of
   different sampling variance. The spec itself mandates the one-pool
   count; the direction is conservative (noisier short-window estimates
   inflate V → larger SR₀); stated, never presented as a clean i.i.d.
   Bailey-LdP V.
9. **Tests (F4/F12).** The AUTO==MANUAL byte-parity golden extended with
   the new manifest field; full-run → resume tree byte-identity INCLUDING
   sidecars; crash-window property tests for all three F4 corners (resumed
   completed tuple preserves the sidecar; flush-then-die redo writes a
   byte-identical sidecar; partial-mark subset never overwrites); the
   moments-vs-`statistics.pvariance` cross-check is APPROXIMATE (pvariance
   uses exact-Fraction summation — last-ulp differences are expected; the
   engine computes V exactly ONE way, from the frozen moments in
   deterministic order); the option-D deterministic recount as a test
   oracle on small fixtures; a monotonicity property (N↑ at fixed V>0 ⇒
   DSR↓ — leaderboard-N is never laxer than pool-N).
10. **Tracked follow-up, not buried (F4b).** The PRE-EXISTING partial-
    `mark_many` redo hole (a crash inside the mark loop lets a re-run
    re-rank a SUBSET: under `--overfit-gate` the subset's differing
    `deflated_sharpe` raises the store's append conflict; with `top_k`
    truncation the subset's top-K can append extras and break tree
    byte-identity) gets its OWN MIGRATION.md decision row and follow-up.
    This design must not silently absorb or mask it — condition 2's
    preserve-on-skips rule keeps the sidecar safe in that window, nothing
    more is claimed.

## 5. Honest envelope (what this fix does NOT do)

- **RC/SPA (#4), PBO (#5), Romano-Wolf (#6), CPCV/WFO/MC stay pool-only.**
  They consume full return SERIES; unpersisted trials cannot enter them
  without re-running the sweep. DSR is the only filter whose multiplicity
  input is computable at full N from scalars. RC's own best-of-N
  understatement is a pre-existing, documented limitation, out of scope.
- **Correlated + adaptive trials.** Grid trials are heavily correlated
  (nested masks, adjacent SL/TP) ⇒ raw-N deflation is conservative;
  Stage-3 CMA-ES trials are adaptive ⇒ the i.i.d. benchmark under-models
  their snooping for that subset. Both stated, neither quantified.
- **Scale wall untouched.** `run_persisted_sweep` already materializes
  every `(mask, metrics)` pair in memory; the sidecar adds a few hundred
  bytes per tuple and an O(tuples) build-time merge — nothing about the
  pre-existing 10⁷-pair memory profile is fixed or claimed.
- **Survivor churn is intended.** With true N, filter #2 rejects more;
  cascade golden fixtures are regenerated with an explicit before/after
  note — `dsr_min` is never loosened to compensate.

## 6. Implementation slices (each its own PR, strict-100% gated)

1. `brutex/engine/trial_stats.py` — the pure core: dataclasses, fsum
   moments, the preserve-vs-overwrite policy, exact union variance,
   deterministic atomic JSON IO, the sidecar path helper. Unit + property
   + golden tests.
2. Sweep write points — `run_persisted_sweep` (flush → sidecar → mark,
   condition 2) + `run_stage3_pass` refined-cell sidecars (condition 4);
   `SweepRunSummary` + `SWEEP_FLUSH` additive fields; the A/B
   byte-identical-tree oracle extended to sidecars; crash-corner tests.
3. Builder + manifest — per-leaderboard aggregation over grid tuples +
   refined cells (zero-finalist tuples counted), the additive
   `leaderboard_trials` manifest block (condition 1), back-compat parse,
   `cmd_sweep_all` wiring.
4. Cascade consumption — `run_stage4_cascade(..., trial_stats=None)`,
   the filter-#2 source switch, `CascadeReport` additive fields + the
   `dsr_n_source` stamp (condition 6), AUTO==MANUAL parity golden,
   fixture regeneration, spec §Stage-4 + `overfit.py` docstring
   reconciliation (conditions 3/7), the condition-10 follow-up row.

*Compiled from the 2026-06-10 three-lane review (facts + architect +
hostile); the full finding-by-finding record (F1-F15 with file:line) is
preserved in MIGRATION.md entry #642.*
