# Track 1 — Optimization Designs (Phase 2.5 + Phase 3.5)

> **Status:** Designs locked 2026-05-15 evening from 2 parallel Plan-agent audits
> (Phase 3.5 Metal GPU + Stage 2.5 Bayesian early-stop). Both designed as
> additive accelerators that preserve all locked Track 1 guarantees:
> reproducibility, dedup, audit chain, 100% test coverage targets.
>
> Companion to `TRACK1_BACKTEST_SPEC.md`, `TRACK1_PHASE_PLAN.md`,
> `TRACK1_PERFORMANCE_BUDGET.md`, and `TRACK1_RUNTIME_ARCHITECTURE.md`.

---

## 1. TL;DR — Combined Impact

| Configuration | Effective trials | Throughput | Wall time |
|---|---|---|---|
| Baseline (CPU only, no early-stop) | 33 B | 80-150 K t/s | 2-4 days |
| ~~+ Stage 2.5 only (CPU + Bayesian early-stop)~~ | 🚫 retired — Stage-2.5 ABANDONED (ADR-0006 / DEC-SPRT-INFEASIBLE-001) | — | — |
| + Phase 3.5 only (CPU + Metal GPU) | 33 B | 160-400 K t/s | **0.95-2 days** |
| ~~+ Both Stage 2.5 AND Phase 3.5~~ | 🚫 retired — Stage-2.5 ABANDONED (ADR-0006 / DEC-SPRT-INFEASIBLE-001) | — | — |

**Honest SLA (Golden Rule #10, DEC-SPRT-SLA-RETIRE-001):** the former "single-overnight" claim assumed Stage-2.5, which is ABANDONED (DEC-SPRT-INFEASIBLE-001). Baseline remains **2-4 days for ~33 B trials on the M4 Pro envelope** (CLAUDE.md §8); the Phase-3.5 GPU row is a projection — absolute wall-time claims only after measurement.

---

## 2. Stage 2.5 — Bayesian Adaptive Early-Stop

> 🚫 **SUPERSEDED (DEC-SPRT-SPEC-RECONCILE-001, 2026-06-08).** The
> **inter-backtest** design in this section (prune a param-tuple after ≥50,000
> param-trials via a shared `Arc<DashMap>` polled by Rayon workers) was **rejected**
> by the 4-agent hostile review (ADR-0006): the `DashMap`/worker-poll prune is
> **thread-scheduling-dependent → non-deterministic → breaks the audit-replay
> oracle** (blocker B3). The LOCKED canonical algorithm is the **intra-backtest**
> definition in `TRACK1_BACKTEST_SPEC.md §"Stage 2.5"` (kill ONE backtest after
> ≥50 closed trades). This §2 is retained for historical context only — do NOT
> implement the DashMap architecture (§2.3) or the 50,000-param-trial floor (§2.2).

Insert as a **modifier inside Stage 2** (not a new sequential stage). The
5-stage funnel name preserved; engine gets one new in-loop component.

### 2.1 Framework

**Hybrid Bayesian posterior + Wald-style SPRT decision rule.**

| Component | Choice | Reference |
|---|---|---|
| Posterior on | **95th-percentile Sharpe** (NOT mean) | own design — addresses late-bloomer failure mode |
| Update | Gaussian conjugate | Gelman et al. BDA3 Ch. 2.5, CRC 2013, ISBN 978-1439840955 |
| Prior | Empirical Bayes from Stage 1B output | Efron & Hastie 2016 Ch. 6, ISBN 978-1107149892 |
| Decision rule | Wald-style likelihood ratio threshold | Wald 1947 *Sequential Analysis*, Dover |

**Why 95th percentile not mean:** Stage 3 (CMA-ES refine) only cares about
the best Sharpe in a combo's parameter neighborhood. Modeling the 95th
percentile is correct; modeling the mean misclassifies high-variance
combos whose single-best-param tail is what we actually want.

### 2.2 Decision thresholds

| Parameter | Value |
|---|---|
| `MIN_TRIALS` before any decision | **50,000** (≈1.5% of 3.26M param surface) |
| `BATCH_SIZE` re-evaluation cadence | **25,000** trials |
| `τ_low` (dud threshold) | Stage 1B Sharpe **median** |
| `τ_high` (early-promote threshold) | Stage 1B Sharpe **99th percentile** |
| `α_prune` (prune confidence) | **0.95** |
| `α_promote` (promote confidence) | **0.99** |
| Cool-down before re-eval if borderline | 2 × BATCH_SIZE trials |
| Hard cap (always sweep at least) | MAX(MIN_TRIALS, 10% of param surface) |

### 2.3 Implementation architecture

```
   ┌────────────────────────────────────────────────────────────────┐
   │  Stage 2 hot loop (Rayon worker pool)                          │
   │  ─────────────────────────────────                             │
   │  per trial: bar-mask eval, SL/TP sim, append result            │
   │       │                                                        │
   │       ▼ every BATCH_SIZE (25K) trials                          │
   │  ┌────────────────────────────────────────────────────────┐    │
   │  │ crossbeam::channel::bounded(1024)                      │    │
   │  │ → (combo_id, sharpe_running) tuples                    │    │
   │  └─────────────────────┬──────────────────────────────────┘    │
   │                        ▼                                       │
   │  ┌────────────────────────────────────────────────────────┐    │
   │  │ Early-stop evaluator (single Rayon worker)             │    │
   │  │   - Welford online posterior update                    │    │
   │  │   - normal_cdf at τ_low, τ_high                        │    │
   │  │   - returns EarlyStopDecision                          │    │
   │  └─────────────────────┬──────────────────────────────────┘    │
   │                        ▼                                       │
   │  Arc<DashMap<ComboId, ComboState>>  ◀── workers poll           │
   │                                                                │
   └────────────────────────────────────────────────────────────────┘
```

### 2.4 Pseudocode

```rust
pub fn should_stop_early(
    combo_id: ComboId,
    trials_so_far: u64,
    sharpe_dist: &PosteriorSharpe,
    tau_low: f64,
    tau_high: f64,
    cfg: &EarlyStopConfig,
) -> EarlyStopDecision {
    if trials_so_far < cfg.min_trials || trials_so_far < cfg.max_trials_cap_min {
        return EarlyStopDecision::Continue;
    }
    let post_mean = sharpe_dist.posterior_mean();
    let post_std  = sharpe_dist.posterior_std();
    let p_dud    = normal_cdf(tau_low,  post_mean, post_std);
    let p_winner = 1.0 - normal_cdf(tau_high, post_mean, post_std);
    if p_dud    >= cfg.alpha_prune   { return EarlyStopDecision::Prune { ... }; }
    if p_winner >= cfg.alpha_promote { return EarlyStopDecision::Promote { ... }; }
    EarlyStopDecision::Continue
}
```

### 2.5 Audit log integration

Each Prune / Promote decision emits one hash-chained audit line:

```json
{ "action": "STAGE_2_5_PRUNE",
  "subject": "combo:<combo_hash>",
  "payload": { "trials_done": 280000,
               "posterior_prob_dud": 0.967,
               "posterior_mean_sharpe": 0.34,
               "posterior_std": 0.18,
               "tau_low": 0.52,
               "rng_seed": 42 } }
```

Replay-oracle test produces **byte-identical Parquet WITH Stage 2.5 enabled**.

### 2.6 Expected savings

| Scenario | % combos pruned | Avg prune point | Effective trials | Savings |
|---|---|---|---|---|
| Pessimistic | 50% | 25% | 18.0 B | **45%** |
| Expected | 70% | 15% | 12.8 B | **61%** |
| Optimistic | 85% | 10% | 7.8 B | **76%** |

Expected case: 33B → ~13B → wall time 2-4 days → **1.0-1.6 days at 100K t/s**.

### 2.7 Verification (Property tests + Oracle)

| Test | Purpose |
|---|---|
| **P-25** (new property invariant) | Posterior(prune) monotone non-decreasing in n_obs when x̄ ≤ τ_low |
| **P-26** (new property invariant) | Audit replay reproduces every Prune/Promote decision byte-identically |
| **`tests/integration/test_stage2_5_no_winner_loss.py`** (new oracle) | A/B: top-3 winners under early-stop match unrestricted-sweep top-3 with ≥ 95% recall |
| Rolling 30-day `top-3-miss-rate` audit | If > 5%, auto-raise `α_prune` from 0.95 → 0.98 (sacrifice wall time for safety) |

### 2.8 Implementation files (Phase 2.5 deliverable, ~1 week)

- `rust/fno-math/src/stage2_5/early_stop.rs` (NEW — posterior + decision)
- `brutex/orchestrator/stage2_driver.py` (NEW — owns DashMap, calls Rust via PyO3)
- `tests/integration/test_stage2_5_no_winner_loss.py` (NEW — A/B oracle)

---

## 3. Phase 3.5 — Metal GPU Compute Kernel

**Apple Metal only.** No CUDA, no OpenCL, no AWS — local M4 Pro 20-core
Apple Silicon GPU + unified 273 GB/s DRAM.

### 3.1 Workload split

| Workload | Verdict | Speedup |
|---|---|---|
| **A. Bitmask AND** `(strategy_mask & bar_mask) == strategy_mask` | **GPU** | **3-5×** projected |
| **B. Indicator precompute** (EMA scan, day_high/low, Fib anchors) | **GPU** | **5-10×** projected |
| **C. Trade simulation** (SL/TP/15:20 forced exit) — branchy | **CPU stays** | n/a |

Blended sweep speedup: **~2×** (because trade sim remains CPU-bound).

### 3.2 Memory architecture — Apple unified memory advantage

`MTLResourceStorageModeShared` = same virtual address visible to CPU and
GPU. **Zero PCIe copy.** Per developer.apple.com Metal Best Practices.

```
   48 GB unified DRAM (273 GB/s shared)
       │
       ├─ Parquet mmap (read-only Arrow columns, no copy)
       │
       └─ MTLBuffer (StorageModeShared)
              │
              ├─ Rayon worker threads (CPU side: SL/TP sim)
              └─ Metal command queue   (GPU side: bitmask AND)
                     │
                     └─ MTLCommandBuffer.addCompletedHandler
                        → crossbeam_channel::Sender back to scheduler
```

**Hazard tracking:** `MTLResourceHazardTrackingModeUntracked` (Track 1
owns synchronisation; Metal's auto-tracker adds 5-15% overhead).

### 3.3 Rust ↔ Metal integration

| Option | Verdict |
|---|---|
| `metal` crate (gfx-rs/metal-rs) v0.27 | **PICK** — idiomatic Rust, Apache-2.0/MIT, used by wgpu |
| `objc2` + `objc2-metal` v0.5 | Fallback for any missing API |
| C-FFI bridge (Objective-C++) | Rejected — two build systems |

**Crate gating:**
```toml
[target.'cfg(all(target_os = "macos", target_arch = "aarch64"))'.dependencies]
metal = "0.27"
objc2 = "0.5"
```

### 3.4 Bitmask kernel design

```msl
kernel void bitmask_and_sweep(
    device const uint64_t*  bar_masks      [[ buffer(0) ]],  // [n_bars]
    device const uint64_t*  strat_masks    [[ buffer(1) ]],  // [n_strats]
    device atomic_uint*     hit_counts     [[ buffer(2) ]],  // [n_strats]
    constant uint&          n_bars         [[ buffer(3) ]],
    uint2                   gid            [[ thread_position_in_grid ]]);
```

| Detail | Value |
|---|---|
| Grid | (n_bars, n_strats) = e.g. (94,000, 10,000) |
| Threadgroup | (32, 8) = 256 threads/group (1 SIMD-group × 8 strats) |
| Threads/SIMD-group | 32 (Apple GPU verified) |
| Batch | 8192 strategies × 94K bars per command buffer |
| Buffer size | ~850 KB per batch (fits L2 easily) |
| Command buffers per Stage 2 | ~11,600 (across all 63 inst×TF) |
| Launch overhead total | ~12 sec out of ~3-day sweep (0.005%) |

Estimated occupancy: **70-90%** for branchless integer kernel on M4 Pro
20-core GPU. Effective ~3.0-3.6 TFLOPS-equivalent integer ops.

### 3.5 Verification (preserves all locked guarantees)

| Question | Answer |
|---|---|
| Does GPU change `data_snapshot_hash`? | NO — same input data |
| Does GPU change `indicator_version`? | NO — must produce bit-identical indicator output |
| Does GPU change `combo_hash`? | NO — same 9 inputs, same SHA256 |
| Could GPU produce different result? | If bitmask AND or popcount differs → **CI replay oracle FAILS** → blocks merge |
| Worst case if GPU fails? | Auto-fallback to CPU-only path (`BRUTEX_GPU_OFFLOAD=0`) |

### 3.6 CI gates (new for Phase 3.5)

| Gate | Assertion |
|---|---|
| `tests/bench/test_metal_kernel_speedup.py` | `gpu_time / cpu_time ≤ 0.5` (must be ≥ 2× faster), 5 warm runs, 1 sigma |
| Numerical parity | `assert_array_equal(cpu_hit_counts, gpu_hit_counts)` — bit-exact (bitmask AND has no FP fuzz) |
| Replay oracle | Audit-replay produces byte-identical Parquet WITH GPU path enabled |
| Skip condition | `@pytest.mark.skipif(platform.machine() != "arm64" or platform.system() != "Darwin")` |
| `brutex doctor` C11 | Extended to read `.brutex/bench/canary_gpu.json` |

### 3.7 Implementation timeline (Phase 3.5, ~4 weeks)

| Week | Deliverable | Exit gate |
|---|---|---|
| 1 | Rust↔Metal FFI POC: trivial vec-add kernel | Compiles, runs, GPU result = CPU result; round-trip ≤ 2 ms |
| 2 | `bitmask_and_sweep` kernel + 8192-strat batching | Kernel ≥ 2× CPU baseline |
| 3 | EMA + indicator precompute kernels + integration | Stage 2 pass uses GPU end-to-end; Parquet bit-identical to CPU-only |
| 4 | End-to-end mixed CPU+GPU sweep + scheduler hookup | Projected 33B wall ≤ 2 days; C11 canary GREEN at ≥ 300K t/s |
| Sign-off | 4-agent hostile review | Decision row in MIGRATION.md; spec §2.1 updated; CI green |

### 3.8 Implementation files (Phase 3.5 deliverable, ~3-4 weeks)

- `rust/fno-math/Cargo.toml` (add metal + objc2 deps behind cfg gate)
- `rust/fno-math/src/lib.rs` (register `pub mod gpu;` module)
- `rust/fno-math/src/gpu/mod.rs` (NEW — Dispatcher actor, public API)
- `rust/fno-math/src/gpu/shaders/bitmask_and.metal` (NEW — MSL source)
- `rust/fno-math-py/src/lib.rs` (expose PyO3 functions)
- `tests/bench/test_metal_kernel_speedup.py` (NEW — CI gate)

---

## 4. Combined Roadmap (Phase 2.5 + Phase 3.5)

```
   Phase 0 ✅  Documentation (shipped on main, PR #12 + #13 + #14)
        │
        ▼
   Phase 1     Verification scaffolding (2-3 weeks)
        │     - 9-layer test pyramid as empty shells
        │     - 24 property invariants stubbed
        │     - 3 oracle test skeletons
        ▼
   Phase 2     Groww adapter + Vocabulary (3-4 weeks)
        │
        ▼
   Phase 2.5   STAGE 2.5 EARLY-STOP (1 week, NEW)
        │     - rust/fno-math/src/stage2_5/early_stop.rs
        │     - test_stage2_5_no_winner_loss.py A/B oracle
        ▼
   Phase 3     Brute-force engine (2-3 weeks)
        │     - Stage 1A + 1B + 2 + 3 implementation
        │     - Throughput canary bench (Phase 3 SLA gate)
        ▼
   Phase 3.5   METAL GPU OFFLOAD (3-4 weeks, NEW)
        │     - rust/fno-math/src/gpu/
        │     - test_metal_kernel_speedup.py
        ▼
   Phase 4     Filter cascade + first production sweep (1-2 weeks)
        │     - DSR + CPCV + WFO + Lockbox
        │     - First production 33B sweep (baseline 2-4 days; GPU-path
        │       projection pending measurement — DEC-SPRT-SLA-RETIRE-001)
        ▼
   🚢 SHIP — top 3 production candidates
```

**Total: 10-14 weeks** (vs original 8-12 weeks — adds 2 weeks for the
two optimization phases).

---

## 5. Decisions Locked Here

Additions to `MIGRATION.md` decision ledger:

| # | Decision |
|---|---|
| 100 | **Stage 2.5 — Bayesian adaptive early-stop locked.** Hybrid Bayesian posterior (Gaussian conjugate on 95th-percentile Sharpe) + Wald-style decision rule. Empirical-Bayes prior fit from Stage 1B Sharpe distribution. Prune if Pr(true Sharpe ≤ Stage-1B median) ≥ 0.95; promote if Pr(true Sharpe ≥ Stage-1B 99th pctile) ≥ 0.99. Hard floor 50K trials before decision. Expected 61% Stage-2 wall reduction (33B → ~13B effective). Verified by P-25 + P-26 property invariants AND `tests/integration/test_stage2_5_no_winner_loss.py` A/B oracle (top-3 recall ≥ 95%). References: Wald 1947 Sequential Analysis (Dover); Gelman BDA3 Ch. 2.5 (CRC 2013); Efron-Hastie 2016 Ch. 6 (Cambridge UP). Phase 2.5 implementation, 1 week effort, sits between Phase 2 and Phase 3 in build plan.
| 101 | **Phase 3.5 — Apple Metal GPU offload locked.** Local M4 Pro 20-core GPU only (NO CUDA / NO OpenCL / NO AWS). Workload split: bitmask AND + indicator precompute on GPU (3-10× speedup projected); trade simulation stays on CPU (branchy). Apple unified memory (`MTLResourceStorageModeShared`) = zero PCIe copy. Rust↔Metal via `metal` crate v0.27 + `objc2` v0.5, gated `#[cfg(target_os="macos", target_arch="aarch64")]`. Bitmask kernel threadgroup (32, 8) = 256 threads, batch 8192 strats × 94K bars. CI gate `tests/bench/test_metal_kernel_speedup.py` asserts ≥ 2× CPU baseline + bit-exact numerical parity + audit-replay byte-identical Parquet. Fallback `BRUTEX_GPU_OFFLOAD=0` env flag → CPU-only path. Projected blended speedup ~2× → 33B wall 1-2 days CPU+GPU vs 2-4 days CPU-only. Phase 3.5 implementation, 3-4 weeks effort, sits after Phase 3 in build plan. **🚫 Amended 2026-07-06 by ADR-0004 (DEC-GPU-ACCELERATION-ADR-001 #410) + DEC-GPU-GATE-RECONCILE-001:** GPU-on-M4-Pro rejected (ADR-0004 §5.2); the §6 tri-platform benchmark is a dormant fail-closed precondition (never run — any future GPU PR must land it first); no GPU code exists in `brutex/` or `rust/` (mechanically pinned by `tests/unit/test_gpu_gate_drift_pin.py`). No numeric claim here is changed — the projections stay projections.
| 102 | **Combined optimization impact projected**: Stage 2.5 + Phase 3.5 stack additively. Effective trials drop 33B → ~13B (Stage 2.5 prune); throughput rises 100K → 200-300K t/s (Phase 3.5 GPU); combined 33B-equivalent wall time drops to **~10-20 hours = 1 overnight session (22:00-07:00 IST) on M4 Pro alone, no cloud**. Subject to Phase 3 + 3.5 + 2.5 canary validation; SLA bands in spec §2.1 self-amend if measurements differ. **🚫 Amended 2026-06-10 (DEC-SPRT-SLA-RETIRE-001):** rows #100 and #102 are RETIRED — Stage-2.5 abandoned per ADR-0006 Final Verdict (DEC-SPRT-INFEASIBLE-001); the combined-impact projection is void.

---

## 6. Risks + Mitigations Summary

| Risk | Phase | Mitigation |
|---|---|---|
| Stage 2.5 kills a true winner (false negative) | 2.5 | Hard 10% floor + α_prune = 0.95 + A/B oracle test |
| Stage 2.5 prior misspecified | 2.5 | Empirical Bayes re-fit per sweep — never carried across runs |
| GPU produces wrong result | 3.5 | Replay oracle + numerical parity test + CPU fallback |
| GPU thermal throttling | 3.5 | Run only at night per spec §5A; auto-throttle scheduler |
| CPU+GPU bandwidth contention | 3.5 | Different access patterns (GPU: bitmask scan, CPU: branchy sim) |
| Metal API breakage on macOS upgrade | 3.5 | Version pinning via `metal = "0.27"`; integration test in CI |
| Optimization regression hides Phase 3 issues | both | Both add canary CI gates; cannot ship without measurement |

---

## 7. Verification — How We Prove Both Optimizations Work

| Gate | Stage 2.5 | Phase 3.5 |
|---|---|---|
| Property invariants | P-25, P-26 | (existing P-19, P-20 cover GPU determinism) |
| Oracle tests | `test_stage2_5_no_winner_loss.py` (A/B) | `test_metal_kernel_speedup.py` (speedup + parity) + replay oracle |
| Audit log | Prune/Promote events hash-chained | All GPU events go through same audit chain |
| `brutex doctor` | (existing C-checks cover) | C11 `throughput.canary_fresh` extended |
| 4-agent hostile review | required at Phase 2.5 sign-off | required at Phase 3.5 sign-off |
| MIGRATION decision row | required (#100) | required (#101) |
| Spec amendment | §12 Stage 2 modifier wording | §2.1 contingency band update |

---

## 8. What This Means for the Final SLA

Updated contingency SLA matrix in `TRACK1_BACKTEST_SPEC.md §2.1`:

> **🚫 Note (2026-06-10, DEC-SPRT-SLA-RETIRE-001):** every "Phase 2.5 active = YES" row below is VOID — Stage-2.5 is abandoned (DEC-SPRT-INFEASIBLE-001). Only the CPU-baseline and GPU-only rows can ever publish, and only after measurement.

| Phase 3 canary | Phase 2.5 active? | Phase 3.5 active? | Published SLA (33B) |
|---|---|---|---|
| ≥ 300K t/s | YES | YES (GPU) | **~10-20 hours** (1 overnight) ⚡ |
| ≥ 150K t/s | YES | NO | **1.0-1.6 days** |
| ≥ 150K t/s | NO | YES (GPU) | **0.95-2 days** |
| ≥ 150K t/s | NO | NO | **2-3 days** |
| 80-149K t/s | YES | NO | **1.5-3 days** |
| 30-79K t/s | YES | YES | **2-5 days** (was 5-12) |
| < 30K t/s | — | — | HALT, re-architect |

The combination buys **3 full SLA tiers without any hardware escalation.**
