# ADR-0007 — Stage-3 BIPOP-CMA-ES refinement: design lock (IMPLEMENTABLE-WITH-CONDITIONS)

> **Decision:** DEC-STAGE3-DESIGN-LOCK-001 (MIGRATION.md entry #623)
> **Date:** 2026-06-10 · **Status:** ACCEPTED — implementation may begin only
> against the 9 locked conditions below.
> **Method:** Golden-Rule-#9 review-before-code — a facts agent mapped the
> current surface (no prior verdict blocks Stage-3; the Stage-2 output the
> stage consumes is built) and a hostile reviewer attacked reproducibility,
> statistical validity, look-ahead, SLA honesty, and dependency risk.
> **Verdict:** IMPLEMENTABLE-WITH-CONDITIONS — unlike Stage-2.5 SPRT
> (ADR-0006: structurally infeasible, kills needed the very data they
> skipped), Stage-3 has no closed circle: it ADDS exactly-measured
> candidates and deletes nothing unmeasured.

## 1. What Stage-3 is (as locked here)

A deterministic refinement pass over the Stage-2 survivor set: for each of
the top-K survivor masks (per instrument × TF × mode leaderboard), an inner
BIPOP-CMA-ES (pycma) searches the continuous (SL, TP) plane — normalized
[0,1]², denormalized and **snapped to the int-paisa grid before every
evaluation** — each evaluation being one full standalone backtest through
the unchanged engine path. Refined candidates merge into ONE pool with the
grid survivors, deduped by the existing 9-field combo hash, ranked once,
and flow into Stage 4 unchanged.

## 2. The 9 locked conditions (each binds implementation; violating any = red review)

1. **Optuna is DROPPED (D0).** The spec's outer "Optuna TPE over the top
   10,000 masks" with a budget of 10,000 × 200 selects nothing — every mask
   gets an inner run, so TPE is a dead dependency plus an extra RNG; and TPE
   over a 10,000-way unordered categorical with ≤1 observation per category
   degenerates to random sampling. The outer layer is a **deterministic loop
   over the configured top-K masks in leaderboard order**. (Also removes the
   alembic/sqlalchemy/colorlog/tqdm/PyYAML supply-chain surface — X2.)
2. **The per-eval objective is the pool-independent scalar `sharpe_ratio`
   (D1)**, computed by the existing engine. The 8-term composite is
   pool-z-scored and undefined for a single evaluation. The divergence is
   disclosed: CMA-ES may converge to Sharpe-optimal (SL,TP) that is
   composite-suboptimal; this is acceptable because Stage-3 only ADDS
   candidates — the merged pool is re-ranked by the composite as always.
3. **Quantize before hashing (R2) + fixed denorm bounds (L2).** [0,1]² maps
   onto the spec's Mode-A grid bounds (SL ∈ [5,200] paisa, TP ∈ [10,500]
   paisa — constants, never data-derived) and snaps to the 5/10-paisa
   ticks BEFORE `combo_hash`; identity for a refined candidate is exactly
   the existing 9-field hash over the quantized int params. Floats never
   reach the hash.
4. **Refinement-run identity + value cache (R3).** The resumable/dedup unit
   is the WHOLE per-mask refinement run, identified by
   `blake3(mask_run_id ‖ optimizer-config ‖ seed ‖ budget ‖
   pycma/numpy versions)`. Per-eval results live in a run-id-keyed VALUE
   cache (the optimizer needs f(x), not membership); the global mark-seen
   happens only at refinement-run completion (redo-not-skip preserved).
5. **Determinism (R1/R4).** Seeds derive from the mask's BLAKE3 run
   identity (low 64 bits); the inner optimizer runs single-threaded.
   Reproducibility is claimed for same commit + same lockfile + same
   platform ONLY — CMA-ES eigendecomposition is ULP-sensitive to BLAS
   backends and neither pycma nor numpy guarantees cross-version seed-stream
   stability (Golden Rule #10).
6. **Every evaluation is a Bailey-LdP trial (S1/S3).** All eval Sharpes
   enter the per-leaderboard DSR trial count N and cross-trial variance V,
   and the SPA best-of-N pool — never just the per-mask optima. The honest
   envelope states that adaptive (non-independent) trials keep even
   correct-N DSR somewhat anti-conservative. One merged pool, ranked once.
7. **Train-window only (S2).** The Stage-3 objective is evaluated strictly
   on the lockbox-excluded training window, code-enforced exactly like the
   Stage-4.8 lockbox guard; the lockbox remains the uncontaminated arbiter.
8. **Honest SLA (P1/P2).** The "~1-2 hours" figure is RETIRED until
   measured: it implied ~280-560 full backtests/s while the production
   sweep is the Python `metrics_for_mask` loop and the Rust resident sim is
   unwired. The BIPOP budget contradiction is fixed: 200 MaxFEvals per
   restart × a hard per-mask cap of 3 restarts (≤600 evals/mask, ≤6M total
   at K=10,000 — the previous "2M" assumed zero restarts from a restart
   strategy). TolX/TolFun on a paisa-quantized piecewise-constant objective
   cause flat-fitness restart churn — the cap bounds it; tuning is an
   implementation concern measured at bench time.
9. **Dependency policy (X1).** `pycma` (numpy-only) enters a new
   `optimize` extra with a dedicated decision row; it is carved out of the
   Renovate "always latest" automerge (lockstep-pinned like the
   polars/duckdb/pyarrow rule) because optimizer version bumps silently
   change search trajectories. Optuna is not adopted (condition 1).

## 3. Spec corrections carried with this ADR

- `TRACK1_BACKTEST_SPEC.md` §Stage 3 rewritten to the locked design
  (deterministic outer loop, quantize-before-hash, seed policy, train-window
  scope, honest budget/SLA); the Optuna row leaves the toolchain table.
- MIGRATION.md decision row #37 ("refine top 100, ~15-20 min") amended
  in place: Stage-3 consumes the top-K (default 10,000) survivors and
  emits the merged re-ranked pool; "top 100" was the post-Stage-3 trim
  consumed by Stage 4, and the wall-time figure is retired with the SLA.

## 4. What Stage-3 must emit (observability)

`STAGE_3_REFINE_START / STAGE_3_REFINE_FLUSH / STAGE_3_REFINE_COMPLETE`
hash-chained audit lines per refinement run (mirroring the sweep's three-line
O(1) pattern), carrying the refinement-run identity, seed, budget consumed,
best quantized (SL,TP), and the eval count contributed to N.

## 5. Implementation slices (each its own PR, strict-100% gated)

1. `optimize` extra + pinned pycma + Renovate carve-out (deps ADR row).
2. Top-K survivor query over the persisted leaderboard (the audit found it
   is a stub today).
3. Pure normalization/quantization module (the [0,1]² ↔ paisa bijection on
   the grid) + property tests (round-trip, bounds, tick-snap).
4. The refinement runner (loop + CMA-ES + value cache + audit) behind a
   default-OFF config flag, with the A/B merged-pool determinism oracle.
5. CLI/orchestrator wiring + cascade handoff.

*Compiled from the 2026-06-10 hostile review; the full finding-by-finding
record (BLOCKER/RISK/OK with file:line) is preserved in MIGRATION.md entry
#623.*

## 6. Amendment A1 (2026-06-10, DEC-STAGE3-WIRE-001) — condition 3 vs condition 7 reconciliation

The slice-5 hostile review (MIGRATION.md entry for DEC-STAGE3-WIRE-001, F1)
found conditions 3 and 7 mutually unsatisfiable as written: Stage 2 evaluates
the FULL CLI window (the lockbox is enforced later, as cascade filter #8 over
the same series' tail), so a refined candidate evaluated per condition 7 on
the lockbox-excluded training slice produces DIFFERENT metrics under the SAME
9-field run identity as a full-window grid cell — the identity's
`data_snapshot_hash` field did not distinguish the windows, so condition 3's
"dedup against grid survivors by the existing hash" would either collide
(same id, different row → the store's run-identity conflict raise) or force a
condition-7 violation.

**Resolution (binding):** Stage-3 refined rows are stamped with the
**content-derived `data_snapshot_hash` of the lockbox-excluded training
slice** they were evaluated on (the same `_data_snapshot_hash` derivation the
launcher applies to its slices). The 9-field hash then honestly encodes the
evaluated data: a refined eval landing on a grid cell's (SL, TP) is a
DISTINCT identity from the full-window grid row — both legitimately coexist
in the merged pool, the cascade re-simulates each at its own coordinates, and
no cross-window dedup is claimed or wanted. Condition 3's intra-Stage-3 dedup
(the run-scoped value cache + identical refined evals across reruns) is
unaffected.

## 7. Amendment A2 (2026-07-03, DEC-STATS-GATE-INTEGRITY-001) — filter #8 training-reference decontamination + trade-count evidence

The audit-round-2 findings R2-SR-F1/F2 (statistics/ranking gate integrity)
found a divergence between condition 7's intent and the cascade wire's
actual filter-#8 inputs:

**Pre-change divergence (R2-SR-F1):** `brutex.engine.cascade_wire.
build_candidate_input` fed the IDENTICAL full per-bar series as BOTH the
training reference (`training_sharpe` / `training_max_drawdown` were
computed over `per_bar` in full) AND the lockbox series
(`lockbox_returns = per_bar`). Filter #8's degradation floor therefore
compared the holdout tail against a reference that CONTAINED the tail —
anti-conservative: a tail-degraded candidate's full-series reference was
dragged down by the very tail being judged, softening the floor. §6's
"as cascade filter #8 over the same series tail" wording described this
(now historical) same-series convention.

**Pre-change divergence (R2-SR-F2):** the wire's lockbox series is per-BAR,
so `lockbox_evaluation`'s `>= min_trades` count gate counted BARS (a 182-day
1-minute window holds ~68k bars) — the DEC-LOCK-001 500-**trade** floor
could never bind in production.

**Resolution (binding):** ONE shared pure resolver —
`brutex.metrics.lockbox.resolve_lockbox_window` — is the single source of
truth for the filter-#8 train/holdout boundary. The wire (a) computes each
candidate's TRUE per-trade exit-day IST ordinals and threads them as
`evidence_days` (the count gate + adaptive window extension are now
trade-driven), and (b) computes `training_sharpe`/`training_max_drawdown`
over the per-bar TRAIN COMPLEMENT (`day < window_start`) resolved by the
SAME resolver with the SAME `CascadeConfig` lockbox params filter #8 gates
with. Degenerate complement (< 2 bars or zero variance — e.g. every trade
exits inside the window): the reference falls back to the legacy
full-series pair AS ONE UNIT, logged loudly
(`cascade_wire.training_ref_fallback`) — decontamination is NOT claimed for
fallback candidates.

**Standing honest envelope (unchanged by this amendment):** selection-stage
lockbox EXPOSURE remains by design — Stage-1A/2 selection evaluates the full
CLI window, lockbox days included; the sweep window is deliberately NOT
changed here. The uncontaminated arbiter is the cascade's filter #8 (whose
training reference now genuinely excludes the holdout tail), plus Stage-3's
static `train_slice_bars` exclusion (condition 7). The Stage-3 residual also
stands: an EXTENDED per-candidate lockbox window can overlap Stage-3's
static 182-day exclusion (documented at `stage3_wire.train_slice_bars`).
