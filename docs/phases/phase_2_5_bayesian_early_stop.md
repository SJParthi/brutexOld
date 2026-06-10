# Phase 2.5 — Stage 2.5 Bayesian Early-Stop

> **🚫 SUPERSEDED + ABANDONED (2026-06-08 verdict; banner added 2026-06-10, DEC-SPRT-SLA-RETIRE-001).** Stage-2.5 SPRT early-stop is ABANDONED as a sweep accelerator (ADR-0006 Final Verdict / DEC-SPRT-INFEASIBLE-001): a provably leaderboard-preserving kill is infeasible (pool-coupled z-scores + DSR trial-count). This phase doc describes the rejected inter-backtest `DashMap` design (ADR-0006 B3) and a "61% savings" SLA retired per Golden Rule #10. Retained for history only — do NOT build. Canonical record: `TRACK1_BACKTEST_SPEC.md` §"Stage 2.5" + `docs/adr/0006-stage2.5-sprt-design-block.md`. Leaderboard-safe speedups: the exact Stage-1B prefilter (in tree) + the Phase-3.5 bit-exact GPU path.

## Status: 🚫 ABANDONED (ADR-0006 / DEC-SPRT-INFEASIBLE-001)

## Effort: 1 week
## Owner: Claude (entirely)

## Goal

Implement the adaptive Bayesian early-stop layer inside Stage 2.
Prunes (combo × inst × TF) tuples that are clearly dud after the
first ~10% of their parameter sweep, reducing total Stage 2 trials
from 33B → ~13B (61% expected savings).

## Prerequisites

- [x] Phase 0 complete
- [ ] Phase 1 complete (verification scaffolding shipped)
- [ ] Phase 2 complete (Groww adapter + 52-vocabulary live)
- [ ] Read `docs/TRACK1_OPTIMIZATIONS.md §2` in full
- [ ] User confirms "start Phase 2.5"

## Checklist

### Days 1-2 — Rust kernel

- [ ] `rust/fno-math/src/stage2_5/early_stop.rs`:
  - [ ] `PosteriorSharpe` struct (prior_mean, prior_var, obs_mean, obs_var via Welford, n_obs)
  - [ ] `EarlyStopDecision` enum (Continue / Prune / Promote)
  - [ ] `EarlyStopConfig` (min_trials=50K, batch_size=25K, alpha_prune=0.95, alpha_promote=0.99)
  - [ ] `should_stop_early(...)` function per pseudocode in OPTIMIZATIONS.md §2.4
  - [ ] `normal_cdf(x, μ, σ)` helper (or use libm)
  - [ ] Empirical Bayes prior fit from Stage 1B output
- [ ] PyO3 bindings: `fno_math_py.stage2_5.should_stop_early(...)`
- [ ] Unit tests:
  - [ ] Returns Continue if `trials_so_far < min_trials`
  - [ ] Returns Prune when posterior matches dud case
  - [ ] Returns Promote when posterior matches winner case

### Days 3-4 — Python driver integration

- [ ] `brutex/orchestrator/stage2_driver.py`:
  - [ ] `Arc<DashMap<ComboId, EarlyStopState>>` (or Python equivalent)
  - [ ] Background Rayon worker drains channel of (combo_id, sharpe_running) tuples
  - [ ] Calls Rust `should_stop_early` per BATCH_SIZE trials
  - [ ] Writes decision back to shared state
  - [ ] Workers poll before pulling next batch from work queue
- [ ] Audit log integration:
  - [ ] Emit `STAGE_2_5_PRUNE` event per pruned combo
  - [ ] Emit `STAGE_2_5_PROMOTE` event per promoted combo
  - [ ] Replay-oracle test passes (per P-26)

### Day 5 — Verification + property tests

- [ ] Property test **P-25** (decision #100): Posterior(prune) monotone non-decreasing in n_obs when x̄ ≤ τ_low
- [ ] Property test **P-26** (decision #100): Audit replay reproduces every decision byte-identically
- [ ] Both green with 500 hypothesis examples

### Day 6 — A/B oracle test

- [ ] `tests/integration/test_stage2_5_no_winner_loss.py`:
  - [ ] Setup: 100 random combos from real Stage 1B output, frozen RNG seed
  - [ ] Run baseline (no early-stop) → top-10 winners
  - [ ] Run with-early-stop → top-10 winners
  - [ ] Assert top-3 overlap ≥ 100% (no winner missed)
  - [ ] Assert top-10 overlap ≥ 95% (recall budget)
  - [ ] Promoted combos must all reach baseline's top-1K
- [ ] Test runs in CI (nightly only — too expensive per-PR)
- [ ] Add rolling 30d top-3-miss-rate audit metric

### Day 7 — Sign-off

- [ ] Spec amendment to `TRACK1_BACKTEST_SPEC.md §12` (Stage 2.5 description)
- [ ] Update §2.1 contingency SLA matrix — Stage 2.5 enabled rows
- [ ] **Spawn 4-agent hostile review** on Phase 2.5
- [ ] Hostile review findings documented + fixed
- [ ] MIGRATION decisions added (#126-#130 estimated)
- [ ] **User signs off** → ready for Phase 3

## Verification gates

- [ ] P-25 + P-26 green with 500 examples
- [ ] A/B oracle: top-3 recall = 100%, top-10 recall ≥ 95%
- [ ] Audit replay byte-identical with Stage 2.5 enabled
- [ ] Unit tests: 100% coverage on `stage2_5/` module
- [ ] CI 22 gates green
- [ ] 4-agent review report

## Exit criteria

- [ ] `should_stop_early` Rust kernel shipped
- [ ] Python driver actor + DashMap coordination
- [ ] P-25, P-26 property invariants green
- [ ] A/B oracle test green
- [ ] Spec §12 amended with Stage 2.5 description
- [ ] MIGRATION rows added
- [ ] 4-agent hostile review pass

## Files to create

```
rust/fno-math/src/stage2_5/early_stop.rs           (new, ~300 lines)
rust/fno-math/src/stage2_5/mod.rs                  (new)
rust/fno-math-py/src/lib.rs                        (extend with stage2_5 module)
brutex/orchestrator/stage2_driver.py               (new)
tests/integration/test_stage2_5_no_winner_loss.py  (full impl)
tests/property/test_invariants.py                  (add P-25, P-26 real bodies)
docs/TRACK1_BACKTEST_SPEC.md                       (amend §12, §2.1)
```

## Risk register

| Risk | Mitigation |
|---|---|
| Kills a true winner (false negative) | Hard 10% floor + α_prune=0.95 + A/B oracle test |
| Prior misspecified | Empirical Bayes re-fit per sweep (never carried) |
| Late-bloomer pattern (only one good param) | Posterior on 95th-percentile (not mean) — sees tail early |
| Property test P-25/P-26 brittle | Use deterministic RNG seed; hypothesis derandomised in CI |

## Decisions to lock (#126-#130 estimated)

- #126: Stage 2.5 Rust kernel shipped
- #127: Python driver actor integration
- #128: Audit events STAGE_2_5_PRUNE / STAGE_2_5_PROMOTE emitted + replay-verified
- #129: A/B oracle test green (top-3 recall = 100%)
- #130: Phase 2.5 4-agent hostile review pass

## Phase 2.5 entry / exit summary

| | Before | After |
|---|---|---|
| Stage 2 effective trials | 33B (all combos × all params) | ~13B (with adaptive early-stop) |
| Stage 2 wall time (CPU only) | 2-4 days | 1.0-1.6 days |
| Stage 2 wall time (CPU + Phase 3.5 GPU) | n/a (not built yet) | n/a until Phase 3.5 |
| Property invariants | 24 | 26 (P-25, P-26 added) |
| Oracle tests | 3 | 4 (A/B oracle added) |
| MIGRATION decisions | ~125 | ~130 |
