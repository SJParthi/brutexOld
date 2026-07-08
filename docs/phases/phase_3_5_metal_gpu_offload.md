# Phase 3.5 — Apple Metal GPU Offload

> **SUPERSEDED (2026-07-06, DEC-GPU-GATE-RECONCILE-001):** this
> pre-ADR-0004 Metal phase never started and is superseded by ADR-0004
> (DEC-GPU-ACCELERATION-ADR-001, MIGRATION.md #410 — CPU/Rust-first; §5.2
> rejects GPU on the M4 Pro; §5 item 4 prefers MLX over raw Metal). Any
> future GPU work is gated by ADR-0004 §6's retained fail-closed
> precondition + `tests/unit/test_gpu_gate_drift_pin.py`. The status line
> below stays — it is factually true; the body is preserved as the
> historical design sketch.

## Status: ☐ Not started

## Effort: 3-4 weeks
## Owner: Claude

## Goal

Add Apple Metal compute kernels for the bitmask AND + indicator precompute
hot paths. Cuts wall time from 2-4 days → 1-2 days. Apple Silicon GPU
only — no CUDA, no AWS, no cloud.

If GPU works → blended ~2× speedup; 33B-equivalent sweep finishes in 1
overnight (10-20 hours). If GPU breaks → automatic fallback to CPU-only
path (zero quality impact, just slower).

## Prerequisites

- [x] Phase 0 complete
- [ ] Phase 1 complete
- [ ] Phase 2 complete
- [ ] Phase 2.5 complete
- [ ] Phase 3 complete (CPU-only baseline measured + canary green)
- [ ] Read `docs/TRACK1_OPTIMIZATIONS.md §3` in full
- [ ] User confirms "start Phase 3.5"

## Checklist

### Week 1 — Rust ↔ Metal FFI POC

- [ ] `rust/fno-math/Cargo.toml`:
  - [ ] Add `metal = "0.27"` behind `[target.'cfg(all(target_os = "macos", target_arch = "aarch64"))'.dependencies]`
  - [ ] Add `objc2 = "0.5"` behind same cfg gate
- [ ] `rust/fno-math/src/lib.rs`: register `#[cfg(...)] pub mod gpu;`
- [ ] `rust/fno-math/src/gpu/mod.rs`: skeleton with `Dispatcher` actor
- [ ] `rust/fno-math/src/gpu/shaders/vec_add.metal`: trivial f32 add kernel (POC)
- [ ] `tests/bench/test_metal_vec_add.py`:
  - [ ] GPU result ≡ CPU result bit-identical
  - [ ] Round-trip dispatch ≤ 2 ms
- [ ] Verify on real M4 Pro hardware
- [ ] POC POC PR opened against feature branch

### Week 2 — Bitmask AND kernel

- [ ] `rust/fno-math/src/gpu/shaders/bitmask_and.metal`:
  - [ ] `kernel void bitmask_and_sweep(...)` per `OPTIMIZATIONS.md §3.4`
  - [ ] Threadgroup (32, 8) = 256 threads
  - [ ] Threadgroup memory `bar_cache[32]` for 8× DRAM reduction
- [ ] `rust/fno-math/src/gpu/bitmask.rs`:
  - [ ] Rust dispatcher allocating buffers + command queue
  - [ ] `dispatch_bitmask_and(bar_masks, strat_masks)` → `Vec<u32>` hit counts
- [ ] PyO3: `fno_math_py.gpu.run_bitmask_sweep(...)`
- [ ] **`tests/bench/test_metal_kernel_speedup.py`**:
  - [ ] Asserts `gpu_time / cpu_time ≤ 0.5` over 5 warm runs
  - [ ] Skipped on non-Apple-Silicon CI runners

### Week 3 — Indicator precompute kernels

- [ ] `rust/fno-math/src/gpu/shaders/ema_scan.metal`:
  - [ ] Hillis-Steele inclusive prefix-scan for EMA recursion
  - [ ] One dispatch per (inst, TF) pair (~84 dispatches per sweep)
- [ ] `rust/fno-math/src/gpu/shaders/bar_mask_pack.metal`:
  - [ ] Per-bar 52-condition pack
  - [ ] Reads indicators + daily anchors
- [ ] Wire into Phase 2 indicator pipeline behind `BRUTEX_GPU_OFFLOAD=1` flag
- [ ] Bench: indicator precompute wall reduces from ~120 sec to ~15-25 sec

### Week 4 — End-to-end mixed CPU+GPU + scheduler integration

- [ ] `brutex/orchestrator/scheduler.py`:
  - [ ] Day mode: GPU OFF
  - [ ] Night mode (22:00-07:00 IST): GPU ON
  - [ ] `caffeinate -i` wrapper to prevent macOS power suspend
  - [ ] Detect via `time` + `pgrep idea / Docker / Chrome`
- [ ] End-to-end micro-sweep test: 100K trials with `--gpu` flag
- [ ] **Re-measure throughput canary** with GPU enabled
  - [ ] Target: ≥ 300K t/s sustained → SLA band 1 ("1-2 days for 33B")
- [ ] **Update `brutex doctor` C11** to read `.brutex/bench/canary_gpu.json`
- [ ] **Spec amendment** to `TRACK1_BACKTEST_SPEC.md §2.1` — measured GPU band confirmed
- [ ] MIGRATION row with measured numbers

### Phase 3.5 sign-off

- [ ] Replay oracle byte-identical WITH GPU path enabled
- [ ] Numerical parity `assert_array_equal(cpu_hit_counts, gpu_hit_counts)`
- [ ] Audit-replay reproduces same `data_snapshot_hash`, `combo_hash`
- [ ] `BRUTEX_GPU_OFFLOAD=0` fallback path works (CPU-only mode unchanged)
- [ ] `brutex doctor` C11 GREEN
- [ ] **Spawn 4-agent hostile review** on Phase 3.5
- [ ] **User signs off** → ready for Phase 4

## Verification gates

- [ ] `test_metal_kernel_speedup.py`: gpu/cpu ratio ≤ 0.5
- [ ] Numerical parity: bit-exact CPU↔GPU
- [ ] Replay oracle: byte-identical with GPU enabled
- [ ] All previous phase invariants still green
- [ ] CI 22 gates green (skip Metal tests on non-Apple-Silicon)
- [ ] 4-agent hostile review report

## Exit criteria

- [ ] Bitmask AND kernel ≥ 2× CPU baseline
- [ ] Indicator precompute kernel ≥ 5× CPU baseline
- [ ] End-to-end mixed CPU+GPU sweep produces same `results.parquet` as CPU-only
- [ ] Throughput canary measured ≥ 300K t/s (or SLA band amended if lower)
- [ ] `BRUTEX_GPU_OFFLOAD=0` fallback path verified
- [ ] `brutex doctor` C11 GREEN with GPU canary
- [ ] User signs off

## Files to create / modify

```
rust/fno-math/Cargo.toml                              (add metal + objc2 cfg-gated)
rust/fno-math/src/lib.rs                              (register #[cfg(...)] gpu module)
rust/fno-math/src/gpu/mod.rs                          (new, Dispatcher actor)
rust/fno-math/src/gpu/bitmask.rs                      (new)
rust/fno-math/src/gpu/indicators.rs                   (new)
rust/fno-math/src/gpu/shaders/vec_add.metal           (POC)
rust/fno-math/src/gpu/shaders/bitmask_and.metal       (main kernel)
rust/fno-math/src/gpu/shaders/ema_scan.metal          (indicator)
rust/fno-math/src/gpu/shaders/bar_mask_pack.metal     (indicator)
rust/fno-math-py/src/lib.rs                           (expose gpu submodule)
brutex/orchestrator/scheduler.py                      (day/night auto-throttle)
tests/bench/test_metal_kernel_speedup.py              (full impl)
tests/bench/test_metal_vec_add.py                     (POC test)
.brutex/bench/canary_gpu.json                         (artifact, written by bench)
```

## Risk register

| Risk | Mitigation |
|---|---|
| GPU thermal throttling | Run only at night per scheduler |
| CPU+GPU bandwidth contention | Different access patterns; profile with `powermetrics` |
| Metal API breakage on macOS upgrade | `metal = "0.27"` pinned; integration test in CI |
| `MTLCommandBuffer` hang | 30-sec timeout per dispatch; fallback to CPU |
| FP precision drift (GPU vs CPU) | int64 paisa throughout (no float in hot path); bitmask AND is bit-exact |
| Race condition / non-determinism | Deterministic reduction; P-24 property test asserts |

## Decisions to lock (#141-#150 estimated)

- #141: `metal` crate v0.27 + `objc2` v0.5 deps added (cfg-gated)
- #142: GPU module structure (Dispatcher actor + shaders)
- #143: bitmask_and.metal kernel shipped
- #144: ema_scan.metal kernel shipped
- #145: bar_mask_pack.metal kernel shipped
- #146: scheduler.py day/night auto-throttle
- #147: Measured GPU throughput (specific value)
- #148: SLA band confirmed/amended in spec §2.1
- #149: BRUTEX_GPU_OFFLOAD=0 fallback verified
- #150: Phase 3.5 4-agent hostile review pass

## Phase 3.5 entry / exit summary

| | Before | After |
|---|---|---|
| Engine throughput | 80-150K t/s (CPU only) | 160-400K t/s (CPU + GPU) |
| 33B wall (Stage 2.5 ABANDONED — DEC-SPRT-INFEASIBLE-001) | 2-4 days (CPU baseline) | projection pending measurement |
| GPU code paths | absent | shipped + fallback flag |
| Apple Silicon-specific tests | absent | added (skip on Linux/x86) |
| MIGRATION decisions | ~140 | ~150 |
| Phase 4 readiness | n/a | READY |
