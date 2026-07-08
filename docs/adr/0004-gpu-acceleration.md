# ADR-0004 — GPU acceleration for the brute-force sweep

**Status:** ACCEPTED (2026-07-06, `DEC-GPU-GATE-RECONCILE-001`; was
PROPOSED since 2026-06-02) — the CPU-first arm is EXECUTED + MEASURED
(DEC-RUST-SUPERMASK-KERNEL-001 #411: 964 → 1,575 M ops·s⁻¹ bit-exact;
DEC-RUST-RESIDENT-BARS-001 #413; DEC-RUST-RAYON-SUPERMASK-001 #417; the
33B SLA is now a hard M4-Pro gate,
`tests/bench/test_full_sweep_extrapolation.py`). GPU arms DORMANT: the §6
tri-platform micro-benchmark has NEVER RUN (verified 2026-07-06 — no
MLX/CuPy code, no CI job; `verify-m4pro.yml:77-80` is informational only)
and is RETIRED as scheduled work while RETAINED as the fail-closed
PRECONDITION any future GPU PR must satisfy first (**no GPU code lands
without the empirical micro-benchmark gate in §6**; mechanically pinned by
`tests/unit/test_gpu_gate_drift_pin.py`). Operator-requested 2026-06-02.
**Decision ID:** `DEC-GPU-ACCELERATION-ADR-001` (MIGRATION.md #410);
reconciled by `DEC-GPU-GATE-RECONCILE-001` (MIGRATION.md).
**Method:** CLAUDE.md §9 hostile multi-agent review — a hot-path-fitness
agent + a GPU-tech-landscape/determinism web-research agent, synthesized
here. Every number is cited (repo `file:line` or external URL) or flagged
as an **estimate pending measurement** (Golden Rule #10 — no illusion).

---

## 1. Context — the question

The operator asked: *"implement GPU to make it O(1) always, extremely
ultra-fast, to achieve the billions of brute-force with precision."*

The Track 1 sweep explores **730 B candidate / ~33 B Stage-2 trials**
(`TRACK1_BACKTEST_SPEC.md:28-29`). The dominant hot operation is, per the
code, a **branchless integer u64 bitwise-AND of a strategy mask against a
precomputed per-bar 52-condition bitset, then `== mask` / popcount**
(`brutex/engine/combo_mask.py:189-221`; Rust `bitand_count_u64s`
`rust/fno-math/src/python.rs:84-99`). It is **memory-bandwidth-bound**, not
compute-bound (`TRACK1_PERFORMANCE_BUDGET.md:73,114`;
`TRACK1_BACKTEST_SPEC.md:189-191` — *"THE BOTTLENECK IS MEMORY BANDWIDTH,
NOT COMPUTE"*). The operator's dev machine is an **Apple M4 Pro
(unified memory ≈ 273 GB/s, shared CPU+GPU)**; production may add cloud
NVIDIA.

---

## 2. First honest correction — "O(1) for billions" is a category error

You **cannot** make *processing 33 billion trials* O(1) — that is
inherently **O(N) in the trial count**. What IS O(1) (and is already
implemented + CI-gated) is **per-operation** cost: bar lookup, the u64
mask-AND, Bloom+LMDB dedup, option-series dedup, result append
(`combo_mask.py:13-18`, `tests/bench/test_throughput_canary.py`). The
billions-scale sweep is **O(N) work × massive parallelism × a vectorized
kernel** — GPU is a *throughput multiplier*, never an O(1) maker. Anyone
claiming "billions in O(1)" is selling an illusion. This ADR is about
**throughput**, with O(1) per-op preserved.

---

## 3. Determinism — GPU is SAFE here (the key enabler)

The reproducibility contract is `blake3(strategy_mask ‖ … ‖
code_commit_hash)`, bit-for-bit (CLAUDE.md §2 Rule 4;
`TRACK1_BACKTEST_SPEC.md:659-664`). **Integer** AND / compare / popcount
(`__popcll` on CUDA, `cuda.popc` on Numba, native `popcount` in MSL) are
**bit-identical across devices** — no rounding, no reduction-order freedom
([CUDA integer intrinsics](https://docs.nvidia.com/cuda/cuda-math-api/cuda_math_api/group__CUDA__MATH__INTRINSIC__INT.html)).
The only GPU non-determinism is **float reductions** (summing for
Sharpe/DSR) ([Hawkeye, arXiv](https://arxiv.org/pdf/2603.20421)) — and
those run on the **≤10⁴ Stage-4 survivors on CPU**, never on GPU. So a GPU
integer kernel **preserves the reproducibility hash**. This is what makes
GPU viable at all.

---

## 4. The decisive physics — and why the M4 Pro answer differs from cloud

For a bandwidth-bound integer scan, speedup ≈ **(GPU reachable BW) /
(CPU reachable BW)** — arithmetic intensity is ~0, so this ratio is the
ceiling.

| Hardware | Memory BW | vs CPU on same/typical box | Honest speedup for THIS kernel |
|---|---|---|---|
| **M4 Pro unified** (shared CPU+GPU pool) | ≈ 273 GB/s total ([Apple](https://www.apple.com/newsroom/2024/10/apple-introduces-m4-pro-and-m4-max/)) | CPU can't saturate the bus; GPU can | **~1.2–2.3×** *(estimate)* — the pool is SHARED, so GPU offload barely out-runs a tuned multi-core CPU ([arXiv Apple-Si HPC](https://arxiv.org/html/2502.05317v1)) |
| **Discrete NVIDIA RTX 4090** | ≈ 1,008 GB/s GDDR6X ([runpod](https://www.runpod.io/articles/guides/nvidia-rtx-4090)) | vs server CPU ~100–400 GB/s | **~3–8×** *(estimate)* — separate VRAM; 770 MB bitset resident → PCIe is one-time |
| **NVIDIA H100** | ≈ 3.35 TB/s HBM3 ([runpod](https://www.runpod.io/articles/guides/nvidia-h100)) | " | **~8–10×+** *(estimate)* — different hardware + $$ |

**The blunt truth:** on the M4 Pro, unified memory means a GPU port of a
BW-bound kernel buys only ~1.2–2.3× over the Rust multi-core CPU kernel
*[was "(still-to-be-built) … NEON": since built + measured the same day
this ADR landed — 964 → 1,575 M ops·s⁻¹ single-core, bit-exact (#411,
scalar-autovec, not hand-NEON), resident buffers (#413), Rayon multi-core
(#417); the D-7 stale wording flagged by
`VERIFICATION_2026-06-03_INSTITUTIONAL_ARCH.md:106`, reconciled 2026-07-06
(DEC-GPU-GATE-RECONCILE-001)]* — for a whole extra toolchain, a second
code path to keep bit-identical, and CI cost. The real 3–10× GPU win lives **only on
discrete-VRAM NVIDIA** (cloud), which is *different hardware*. Also note:
the blended end-to-end sweep speedup is lower than the kernel speedup
because the **branchy SL/TP/15:20 trade simulation stays on CPU**
(`TRACK1_OPTIMIZATIONS.md:166,316` — honest blended ≈ 2× even for the
kernel slice).

---

## 5. Decision

1. **O(1) per-operation stays the contract** (already gated); GPU is a
   throughput multiplier for the O(N) sweep, not an O(1) mechanism.
2. **Do NOT add GPU on the M4 Pro dev box.** The first real speed unlock
   is the **Rust `count_supermasks_u64s` PyO3 kernel** (multi-threaded,
   tiled to fit the ~770 MB bitset in SLC) — that is what flips the
   33B-trial SLA from xfail (~1014-day proxy) toward the 2–4-day M4-Pro
   target (`TRACK1_PERFORMANCE_BUDGET.md`). **CPU Rust first.**
   *[Reconciled 2026-07-06 (DEC-GPU-GATE-RECONCILE-001): this arm was
   EXECUTED the same day the ADR landed — the original "roadmap … NEON
   popcount" wording was stale (D-7,
   `VERIFICATION_2026-06-03_INSTITUTIONAL_ARCH.md:106`). Measured:
   964 → 1,575 M ops·s⁻¹ bit-exact (#411, scalar-autovec — not hand-NEON);
   resident bar buffers (#413); Rayon multi-core (#417); the 33B SLA is
   now a REAL M4-Pro hard gate
   (`tests/bench/test_full_sweep_extrapolation.py`).]*
3. **For discrete-NVIDIA production, adopt CuPy `RawKernel`** for the
   integer AND+popcount stage (fallback Numba-CUDA), behind a
   `BRUTEX_GPU_OFFLOAD` env flag with a **hard CPU↔GPU `assert_array_equal`
   bit-exact parity gate** in CI (integer ops → no FP fuzz;
   `TRACK1_OPTIMIZATIONS.md:233-245`). Float stats remain CPU.
4. **If an Apple-GPU path is ever wanted, use MLX** (`bitwise_and` on
   uint64, popcount via the SWAR bit-trick — no native popcount in MLX or
   WGSL); fall back to raw Metal/MSL (native `popcount`, 64-bit) only if
   SWAR is measured too slow.
5. **Reject:** wgpu/WGSL (**no 64-bit ints** — [WGSL spec](https://www.w3.org/TR/WGSL/),
   [gpuweb#5152](https://github.com/gpuweb/gpuweb/issues/5152)), OpenCL
   (deprecated on Apple), PyTorch/Triton (no clean popcount, heavyweight)
   as the primary kernel host.
6. **Scope to the right stages only:** GPU may accelerate Stage-1A
   indicator precompute + Stage-1B/2 mask-AND scan; the DSR/CPCV/SPA float
   filters, the audit hash-chain, and the Parquet writes **stay CPU**.

---

## 6. Gate before any GPU code (non-negotiable — Golden Rule #10)

> **AMENDED 2026-07-06 (DEC-GPU-GATE-RECONCILE-001):** this tri-platform
> micro-benchmark has **NEVER RUN** (verified 2026-07-06 — no MLX/CuPy
> code anywhere in `brutex/` or `rust/`, no CI job;
> `verify-m4pro.yml:77-80` reports the GPU device as informational only).
> It is **RETIRED as scheduled work** — 2 of its 3 legs are unexecutable
> on current facts (the MLX leg is contingent on an Apple-GPU path §5.2
> itself rejects for now; the CuPy leg requires cloud NVIDIA hardware
> outside the CLAUDE.md §8 envelope, none provisioned; ADR-0011 reaffirms
> the engine is intentionally CPU/Rust-first) — and **RETAINED as a
> dormant FAIL-CLOSED PRECONDITION**: any future GPU-offload PR must land
> this benchmark FIRST. The precondition is mechanically pinned by
> `tests/unit/test_gpu_gate_drift_pin.py` (no `mlx`/`cupy` import and no
> `BRUTEX_GPU_OFFLOAD` read may appear in `brutex/` or `rust/` source).
> The CuPy leg stays DORMANT, not deleted — outright deletion of a
> possibly-operator-contemplated cloud path needs operator direction.
> Per-bullet status below.

No GPU implementation merges until an **empirical micro-benchmark** lands
that measures, on the ~770 MB bitset (itself a spec SIZING ESTIMATE,
`TRACK1_BACKTEST_SPEC.md:182` — the benchmark must first measure the
actual artifact; the pre-amendment wording "the real ~770 MB bitset"
mislabeled the estimate as a measured artifact):
- the Rust CPU kernel (multi-threaded) — the baseline — **kernel BUILT +
  measured at MICROBENCH scale, NOT yet at this leg's ~770 MB scale**
  *(round-2 review relabel over the earlier "SATISFIED with measured
  figures" wording, which conflated the two)*: numpy reference
  964 M ops·s⁻¹ → Rust kernel 1,575 M ops·s⁻¹, bit-exact parity asserted,
  was measured SINGLE-core on the dev/CI runner over a 94,000-bar bitset
  (94,000 × 8 B ≈ 752 KB — cache-resident, not the DRAM-streaming
  envelope that decides a memory-bandwidth-bound kernel; #411,
  `tests/bench/test_supermask_kernel_bench.py`; scalar-autovec, not
  hand-NEON); resident buffers (#413); multi-core scaling measured on a
  4-core dev/CI CONTAINER, not the M4 Pro (#417). The ~770 MB-bitset
  MULTI-THREADED baseline measurement this leg defines remains OWED BY
  the future benchmark itself,
- MLX on the M4 Pro GPU — **NEVER RUN — DORMANT**: contingent on an
  operator-requested Apple-GPU path §5.2 itself rejects for now,
- CuPy RawKernel on a cloud NVIDIA box — **NEVER RUN — DORMANT**: requires
  cloud NVIDIA hardware outside the CLAUDE.md §8 envelope (none
  provisioned),

reporting **measured** throughput (bars·masks/s), wall-clock for a fixed
trial slice, and a CPU↔GPU bit-exact parity check. The ~1.2–2.3× /
~3–10× figures above are **BW-ratio estimates, not measurements** — the
benchmark replaces them before any commitment. The Current/Proposed/Δ
comparison table the operator required was NEVER produced; it is a
CONDITIONAL deliverable, produced if-and-only-if a GPU port is proposed —
at which point this §6 benchmark (all three legs, measured throughput +
bit-exact CPU↔GPU parity) MUST land BEFORE any GPU code merges (unchanged,
fail-closed; mechanically pinned by
`tests/unit/test_gpu_gate_drift_pin.py`).

---

## 7. Consequences

- **Near term:** zero GPU work; invest in the Rust supermask CPU kernel
  (the honest unlock on the operator's actual hardware). No new toolchain,
  no second code path, reproducibility untouched.
- **Later (cloud scale):** a gated, bit-exact CuPy integer kernel gives the
  real 3–10× — opt-in, fallback-safe, CI-parity-gated.
- **Reproducibility (Golden Rule #4):** preserved on every path (integer
  kernel is deterministic; float stats stay CPU).
- **Honest envelope:** GPU is **not** a silver bullet on M4 Pro and is
  **not** O(1); it is a cloud-NVIDIA throughput option behind a measured
  gate. This ADR records the decision; it ships **no** GPU code.

---

## 8. Sources

Repo: `brutex/engine/combo_mask.py:189-221` · `rust/fno-math/src/python.rs:55-99`
· `docs/TRACK1_PERFORMANCE_BUDGET.md:72-118` · `docs/TRACK1_BACKTEST_SPEC.md:28-30,182-192,659-673`
· `docs/TRACK1_OPTIMIZATIONS.md:154-267,316` · `tests/bench/test_throughput_canary.py`.
External: Apple M4 Pro newsroom · arXiv 2502.05317 (Apple-Silicon HPC) ·
runpod RTX 4090 / H100 BW · CUDA integer intrinsics · CuPy user-defined
kernels · Numba CUDA · MLX `bitwise_and` · WGSL spec + gpuweb#5152 ·
Hawkeye arXiv 2603.20421 (GPU FP non-determinism). Full URLs in the
2026-06-02 research transcripts.
