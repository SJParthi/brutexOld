# Institutional Architecture Deep Verification — 2026-06-03

> **READ-ONLY HOSTILE AUDIT.** No code and no audited doc was modified to
> produce this report. This file is a new standalone verification artifact
> (the deliverable itself), following the existing `docs/VERIFICATION_*.md`
> convention. Method: 5 parallel hostile agents (CLAUDE.md §9) + an operator
> cross-check against the source-of-truth ledger (`docs/MIGRATION.md`
> entries #410–#415) and `git log`.
>
> **Scope:** the BruteX *institutional brute-force architecture* (the
> Stage 1A → 1B → 2 → 2.5 → 3 → 4 funnel) and its current implementation
> state. Branch `claude/nice-sagan-0V6ou`, HEAD `0caae63`.
>
> Honesty contract: every claim is cited to `file:line`; anything not
> verifiable from a file is marked **UNVERIFIED** per Golden Rule #1; the
> design-vs-built gap is stated plainly per Golden Rule #10.

---

## 0. One-line verdict

The institutional architecture is **well-designed and honestly documented,
but only ~15–20% built**. What ships today is a real, deterministic,
well-tested **single-threaded scalar mask-counter + BLAKE3 run-identity
hash + Dhan data-ingest + dashboard**. The 5-stage funnel that would
deliver "~33 B trials in 2–4 days on an M4 Pro" is **not wired
end-to-end**; Stages 2.5 / 3 / 4 are **design-only**. The redeeming and
important fact: **the repository tells the truth about this itself** — the
33 B SLA is a deliberate strict-`xfail`, and the most aggressive
projections are labelled "NEVER MEASURED". The gap is honesty-preserving,
not deceptive.

### 0.1 Two corrections to the audit inputs (why hostile cross-check matters)

| Input claim | Reality | Evidence |
|---|---|---|
| "Rayon multi-core `DEC-RUST-RAYON-...` recently merged (~#415)" | **FALSE.** No Rayon anywhere in `rust/`; not a dependency. MIGRATION entry #415 is the dashboard pull-beam fix. | `docs/MIGRATION.md:4920`; `rust/fno-math/Cargo.toml:27-59`; `rust/fno-math/src/python.rs:174,219,289` |
| "MIGRATION tops out at #315; the Rust kernel decision IDs don't exist" (an early agent error) | **FALSE** — the file is ~4,930 lines; highest entry is **#415**; the kernel IDs are real. | `docs/MIGRATION.md:4913` (#411), `:4917` (#413) |

---

## (A) The 5-stage funnel — design map vs build state

| Stage | Purpose | Input → Output (reduction) | Design cite | Built? |
|---|---|---|---|---|
| **Total** | Full combinatorial sweep space | **730 B** trials (1.095 T retired by DEC-MODE-002) | `docs/TRACK1_REQUIREMENTS_FINAL.md:26,117,169`; `docs/MIGRATION.md:692` | — |
| **1A** Precompute | Cache 52 conditions/bar as a u64 bitmask matrix | OHLC → bitset (no reduction) | `docs/TRACK1_BACKTEST_SPEC.md:319-324` | 🟡 PARTIAL — only the `combo_mask` primitive exists; no `stage1a` module |
| **1B** Coarse filter | Drop bottom 50% by coarse score | 730 B space → ~50% survivors | `docs/TRACK1_BACKTEST_SPEC.md:326-340` | 🔴 NONE — no `stage1b` module |
| **2** Full backtest | Bar-by-bar SL/TP/15:20 walk on survivors | **~33 B** hot-path trials | `docs/TRACK1_BACKTEST_SPEC.md:29,342`; `docs/TRACK1_PERFORMANCE_BUDGET.md:18` | 🟡 kernel only — mask-count REAL, but no trade-sim, no driver |
| **2.5** Bayesian early-stop | Posterior + SPRT prune of duds in-loop | ~33 B → **~13 B** effective (61% pruned) | `docs/TRACK1_OPTIMIZATIONS.md:26-151` (DEC #100; OPEN #124) | 🔴 NONE |
| **3** CMA-ES refine | BIPOP-CMA-ES on top survivors | **~10 K** masks refined | `docs/TRACK1_BACKTEST_SPEC.md:358-369` | 🔴 NONE |
| **4** Stats gate | 9 filters (DSR/CPCV/SPA/PBO/WFO/lockbox…) | ~1 K → **10²–10⁴** finalists | `docs/TRACK1_BACKTEST_SPEC.md:371-511` | 🔴 NONE — `brutex/metrics/compute.py:13-19` explicitly defers all of them |

**Funnel math is internally consistent** (730 B total / 33 B Stage-2
hot-path / 13 B post-early-stop / 10 K refine / ~10 finalists), with one
honest nuance: **33 B is the "effective hot-path" count, not 730 B × a
clean %** — the raw cartesian product is ~365 B and 33 B is what the spec
defines as actually-computed (`docs/TRACK1_BACKTEST_SPEC.md:29` vs `:342`).
The historical `1.095 T / 33 B / 730 B` inconsistency **was resolved** by
DEC-MODE-002 but is still listed as a live conflict in one stale doc
(drift D-30 below).

---

## (B) Implemented-vs-documented matrix (REAL / STUB / NONE)

| # | Lever / Component | Documented as | Status | Evidence (file:line) |
|---|---|---|---|---|
| a | Stage-2 supermask u64-AND kernel | shipped #411 | ✅ REAL | `rust/fno-math/src/python.rs:155-223`; facade `brutex/engine/combo_mask.py:286-321`; parity `tests/property/test_supermask_kernel_parity.py:48-56`. **Single-threaded**, branchless `(bar & mask) == mask` |
| b | Resident bar buffer | shipped #413 | ✅ REAL | `rust/fno-math/src/python.rs:261-299` (`#[pyclass(frozen)] ResidentBars`); `Vec<u64>` copied once at `new()`; facade `brutex/engine/combo_mask.py:329-419` + numpy fallback |
| c | Rayon multi-core | "shipped ~#415" | 🔴 NONE | Zero `rayon`/`par_iter` in `rust/`; not in `rust/fno-math/Cargo.toml:27-59`. The input claim is false |
| d | Bayesian early-stop / Stage 2.5 | locked #100 | 🔴 NONE | docstring-only `brutex/engine/__init__.py:8`, `brutex/strategies/sweep.py:27`. No SPRT/posterior code |
| e | Memory tiling / cache blocking | designed | 🔴 NONE | All `tile` hits are *network* fetch chunking (`brutex/adapters/dhan/ingest.py:54`). Compute tiling unbuilt |
| f | 8-way SIMD / NEON | "NEON intrinsics" | 🔴 NONE (autovec only) | No `std::simd`/`core::arch`/`aarch64`/`target_feature` in `rust/`. Source says "auto-vectorise at -C opt-level=3" (`rust/fno-math/src/python.rs:149,201`) |
| g | GPU / Metal backend | ADR-0004 design | 🔴 NONE (docs-only) | No `metal`/`cuda`/`wgpu` in any file. ADR ships no code (`docs/MIGRATION.md:4911`; `docs/adr/0004-gpu-acceleration.md`) |
| h | Trade-sim walker (mask-hit → P&L) | "lands alongside" | 🟡 STUB (unwired) | `ExecutionSimulator` is REAL (`brutex/execution/simulator.py:66-348`) but `engine/`+`sweep.py` never import it; `ComboSweep` records a "toy placeholder" `signal_count` (`brutex/strategies/sweep.py:32-36`); `ClosedTrade` has no producer (`brutex/metrics/types.py:13-19`) |
| i | Stage 3 optimizer (CMA-ES) | designed | 🔴 NONE | docstring `brutex/engine/__init__.py:9`; no optuna/pycma/nevergrad dep |
| j | Stage 4 stats — DSR/CPCV/SPA/WFO | 9-filter cascade | 🔴 NONE ×4 | `brutex/metrics/compute.py:13-19` "NOT implemented … NULL". SPA never even specced. (Sharpe/Sortino/Calmar etc. ARE real) |
| — | **End-to-end sweep driver** | "runner — coming" | 🔴 STUB-ONLY | No `stage1a/1b/2/3/4/runner` module exists; `brutex/engine/__init__.py:13-23` admits "Foundation only." `ComboSweep` has zero non-test callers. A user cannot launch a sweep today |
| — | **Determinism (BLAKE3 9-field, GR#4)** | required | ✅ PASS | Genuine BLAKE3 `brutex/strategies/combo.py:180` → `rust/fno-math/src/python.rs:129`; all 9 fields validated; property+unit tested (`tests/unit/test_strategies_combo.py:127`, `tests/property/test_combo_hash_uniqueness.py`). Caveat: only wired caller passes 5/9 fields as literal `"N/A"` (`brutex/strategies/sweep.py:446-450`) |
| — | **O(1) gates (GR#5, ≤1.1×)** | enforced in CI | 🟡 PARTIAL/MISLEADING | Bench tests are REAL and merge-gating (`.github/workflows/ci.yml:2060`), **but enforce 3–10×, not 1.1×** — the 1.1× tier only runs if `BRUTEX_M4PRO_STRICT=1`, which CI never sets (`tests/bench/_o1_tiers.py:36-43`; strict runner offline per `.github/workflows/bench-m4pro.yml:14-22`). "Result append" is skipped (`tests/bench/test_throughput_canary.py:28`). Kernel is explicitly O(N_masks·N_bars), NOT O(1)/mask (`rust/fno-math/src/python.rs:215-223`) |

**Stub census (whole tree):** `todo!()/unimplemented!()` in Rust = **0**
(what exists is real); `NotImplementedError` in `brutex/` = **12** (all
`typing.Protocol` bodies, not feature stubs); `skip/xfail` in tests ≈ **50
across 14 files** (heaviest `tests/property/test_invariants_catalog.py` =
26; `tests/integration/test_python_rust_parity.py` entirely skipped).

**Environment caveat (UNVERIFIED in-tree):** `fno_math` is not built in
this fresh clone (`import fno_math` → `ModuleNotFoundError`); Rust source +
in-crate `#[cfg(test)]` tests are real, but the Python parity/bench tests
cannot execute here. CI-pass status depends on a maturin wheel build that
cannot be verified from this worktree.

---

## (C) Doc-drift list (design docs contradicting shipped code)

### Class 1 — kernel described as "unbuilt/roadmap" though SHIPPED (#312/#314)

| ID | Location | Stale claim |
|---|---|---|
| D-1..D-4 | `docs/TRACK1_REQUIREMENTS_FINAL.md:35,60,134,145,241` | "Rust hot kernel 🔴 empty stub / missing" (doc dated pre-kernel 2026-05-18) |
| D-5,D-6 | `docs/GUARANTEE_CHARTER.md:93,94` | "rust/fno-math is a scaffold; sweep kernel not implemented" |
| D-7 | `docs/adr/0004-gpu-acceleration.md:91,119`; `docs/MIGRATION.md` entry #410 | calls `count_supermasks_u64s` "roadmap / still-to-be-built" — landed the same day |

### Class 2 — SIMD/Rayon/NEON overstatement (shipped kernel is scalar autovec)

| ID | Location | Stale claim |
|---|---|---|
| D-9 | `docs/TRACK1_PERFORMANCE_BUDGET.md:15` | "Rust + std::simd + Rayon parallel" — neither exists |
| D-10,D-11 | `docs/TRACK1_PERFORMANCE_BUDGET.md:89,105,131`; `docs/TRACK1_RUNTIME_ARCHITECTURE.md:270,510` | "aggressive 8-way SIMD … BEATS vectorbt 8-16×" |
| D-12,D-13 | `docs/TRACK1_BACKTEST_SPEC.md:198,208,785`; `docs/GUARANTEE_CHARTER.md:94` | "NEON intrinsics via core::arch::aarch64" / "PyO3 + NEON" |
| D-14,D-15 | `docs/phases/phase_3_brute_force_engine.md:36,154`; `docs/TRACK1_VOCABULARY.md:247` | "NEON on Apple Silicon" / nonexistent `rust/fno-math/src/vocabulary/atoms.rs` |

### Class 3 — Groww→Dhan residue (~1,900 hits / ~110 files; vendor reverted per ADR-0002)

| ID | Location | Issue |
|---|---|---|
| D-17..D-23 | `rust/fno-math/Cargo.toml:7`, `rust/fno-math/src/lib.rs:6`, `rust/fno-math/src/compat/constants.rs:7-100` (149 hits — Groww WebSocket V2 protocol), `compat/segment.rs`, `compat/config.rs`, `compat/sanitize.rs` | Shipped Rust crate still vendor-pinned to the old broker's binary protocol (Dhan is REST) |
| D-24 | `.github/workflows/ci.yml:878-954` | ~10 dead `test_groww_*` CI gates that can never fire (files deleted) |
| D-25,D-26 | `docs/TRACK1_GROWW_ADAPTER.md`, `docs/groww/**`, `docs/phases/phase_2_groww_adapter_vocabulary.md`, `docs/TRACK1_DATA_INGEST_DESIGN.md` | Whole old-vendor specs with no SUPERSEDED banner (unlike ADR-0001 which has one) |
| D-27..D-29 | `brutex/costs/types.py`, `brutex/cli.py`, `scripts/ci/*`, `tests/fixtures/groww_idx_rows_real.csv` | Old vendor name in shipped Python, CI crosswalk, and test fixtures |

### Class 4 — resolved-in-code but still flagged as live conflicts

(single most stale doc = `docs/TRACK1_REQUIREMENTS_FINAL.md`)

| ID | Location | Issue |
|---|---|---|
| D-16 | `docs/TRACK1_REQUIREMENTS_FINAL.md:29,134` | "combo_hash only 4 of 9 fields" — resolved: `brutex/strategies/combo.py:91-103` hashes all 9 via BLAKE3 (#204/#060) |
| D-30 | `docs/TRACK1_REQUIREMENTS_FINAL.md:134` | "trial count inconsistent 33B/730B/1.095T" — resolved by DEC-MODE-002 |
| D-35 | `CLAUDE.md:48,191` | "Active branch `…IpP85`" — actual working branch is `claude/nice-sagan-0V6ou` |
| D-31 | `docs/TRACK1_REQUIREMENTS_FINAL.md:27` | Storage size 50-100GB vs 2.5TB vs 29TB — **genuinely unreconciled (UNVERIFIED)** |

---

## (D) Honest verdict — real vs designed-only + SLA reconciliation

### What is REAL today (works, tested)

Dhan data **ingest** pipeline (`brutex/orchestrator/pipeline.py:610`) ·
u64 **mask primitives** + **resident buffer** (Rust) · **BLAKE3 9-field**
reproducibility hash · **LMDB+Bloom** dedup · hash-chained **audit log** ·
**30+ basic metrics** (Sharpe/Sortino/…) · **dashboard** · isolated
`ExecutionSimulator`.

### What is DESIGNED-ONLY (spec exists, no code)

The **end-to-end sweep driver** · Stage 1B coarse filter · **trade-sim
walker wired to masks** · Stage 2.5 Bayesian early-stop · Stage 3 CMA-ES ·
Stage 4 DSR/CPCV/SPA/WFO · Rayon/SIMD/Metal acceleration.

### SLA honesty (Golden Rule #10) — PASS / exemplary

The 33 B SLA is a strict-`xfail` (`tests/bench/test_full_sweep_extrapolation.py:200-254`)
— never asserted as met; it flips visibly if it ever passes. A **measured
anchor exists** but is honest: shipped kernel ≈ 1.6× over numpy, ≈ 0.6 ns
per bar·mask (`docs/MIGRATION.md:4913`), which **projects to ~22.8 days for
33 B raw** — far over the 2–4 day target.

| Headline | Value | Measured / Estimated | Cite |
|---|---|---|---|
| M4 Pro SLA | 2–4 days / 33 B | ESTIMATED | `CLAUDE.md:372`; `docs/TRACK1_PERFORMANCE_BUDGET.md:22` |
| Sustained throughput | 80–150 K trials/s | ESTIMATED ("NEVER MEASURED") | `docs/MIGRATION.md:191` |
| "Overnight 10–20 h" | 13 B @ Stage2.5+GPU | ESTIMATED (conditional on unbuilt subsystems) | `docs/TRACK1_OPTIMIZATIONS.md:22` |
| Shipped kernel | 1,575 M ops/s, ≈1.6× | MEASURED (microbench) | `docs/MIGRATION.md:4913`; `tests/bench/test_supermask_kernel_bench.py` |
| 33 B projection | ~22.8 days raw | MEASURED→PROJECTED (honestly over SLA) | `docs/MIGRATION.md:4913` |

**One sentence worth fixing (overstatement F-1):**
`docs/TRACK1_OPTIMIZATIONS.md:22` declaratively says the sweep "can finish
in a single overnight session … on M4 Pro alone" — that depends entirely
on the unbuilt Stage 2.5 + GPU.

### On the "O(1) for billions" requirement — the honest physics

Per the repo's own ADR-0004 (`docs/MIGRATION.md:4911`): **"O(1) for
billions is a category error."** Processing 33 B trials is inherently
**O(N)**; what *can* be O(1) is the **per-operation** cost (mask-AND,
dedup, append). Today even that is gated only at 3–10× (not 1.1×), and the
batched kernel is O(N_masks·N_bars). No engine — frontend, backend, or DB
— makes 33 B trials O(1) in wall-time; the honest levers are **pruning**
(Stages 1A/1B/2.5) + **parallelism** (Rayon/GPU), neither of which is built
yet.

---

## (E) Next-phase readiness — Stage 2.5 = NOT-READY (by a wide margin)

**Design:** the algorithm is locked (DEC #100,
`docs/TRACK1_OPTIMIZATIONS.md:26-151`) — Gaussian posterior + Wald-SPRT
gating the 95th-pctile Sharpe, prune at `Pr(Sharpe ≤ τ_low) ≥ 0.95`. But
one decision is **OPEN** (DEC-STAT-002 #124, `docs/MIGRATION.md:239` —
e-value/e-BH vs Wald) and two points are underspecified (the pseudocode at
`:99-106` gates a *mean*, contradicting the "95th-pctile" prose at `:37`;
the empirical-Bayes prior fit at `:39` names no method).

**Ordered prerequisites (all currently missing/partial):**

1. **Reconcile DEC-STAT-002 (#124) vs #100**; pin the percentile-posterior
   estimator + EB-prior fit.
2. **Build the Stage-2 trade-sim walker** (SL/TP/15:20 → `ClosedTrade`) —
   gives Sharpe a producer (`brutex/metrics/types.py` has none).
3. **Build Stage 1B** — its Sharpe distribution *is* the prior
   (`τ_low`/`τ_high`).
4. **Extend the Rust kernel to emit running stats** (Welford + channel) —
   today returns `Vec<u64>` counts only (`rust/fno-math/src/python.rs:216`).
5. **Implement `rust/fno-math/src/stage2_5/early_stop.rs`**
   (`rust/fno-math/src/math/mod.rs` is empty placeholders) + PyO3 bindings
   (the design's `fno-math-py` path is stale — bindings live in
   `rust/fno-math/src/python.rs`).
6. **Build `brutex/orchestrator/stage2_driver.py`** (does not exist).
7. **Wire audit + reproducibility** (`STAGE_2_5_PRUNE/PROMOTE` + L3 replay
   oracle; end-to-end repro hash is 🟡 PARTIAL per `docs/MIGRATION.md:271`).

**Gate:** the phase doc requires "Phase 1 + Phase 2 complete"
(`docs/phases/phase_2_5_bayesian_early_stop.md:17-22`); the master
checklist has Phase 1 = 40%, Phase 3 engine = **0%**
(`docs/phases/00_MASTER_CHECKLIST.md:13-16`).

**Conclusion:** Stage 2.5 cannot be started — the real blocker is the
Stage-2 engine + trade-sim (prerequisites 2–4), not Stage 2.5 itself. The
companion scoping plan (`docs/VERIFICATION_2026-06-03_ENGINE_SCOPING.md`)
addresses exactly that blocker.

---

## Appendix — audit provenance

- 5 parallel agents per CLAUDE.md §9: funnel-map (Explore), levers-matrix,
  driver+determinism+O(1), SLA+doc-drift (general-purpose), Stage-2.5
  readiness (Plan).
- Operator cross-check: `docs/MIGRATION.md` entries #410–#415 read directly;
  `git log` (PRs #312/#313/#314/#315/#317) reconciled against MIGRATION
  entry numbers (#411–#415).
- No file under audit was modified. This report proposes; it does not edit.

