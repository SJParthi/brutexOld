"""MEASURED multi-core scaling bench — Rayon-parallel Stage-2 supermask scan.

DEC-RUST-RAYON-SUPERMASK-001 (MIGRATION.md #417; builds on
DEC-RUST-SUPERMASK-KERNEL-001 #411 / DEC-RUST-RESIDENT-BARS-001 #413 /
ADR-0004 §5.2). The masks-outer scan is embarrassingly parallel — each mask is
an independent integer SUPERSET count over the shared, read-only ``bar_bits``.
``fno_math.count_supermasks_u64s_nthreads(bars, masks, t)`` runs that scan on a
SCOPED rayon pool of exactly ``t`` workers (production uses the DEFAULT global
pool via ``count_supermasks_u64s``; the scoped shim exists only so this bench +
the parity tests can pin a worker count without touching the global pool).

## Workload

94 000 bars × 10 000 masks = 940 M mask·bar SUPERSET ops (the
``test_supermask_kernel_bench.py`` workload). 94 k u64 ≈ the 5-year × 15 m
intraday bar count for one instrument-TF; 10 k masks ≈ a mid-size Stage-2 mask
batch.

## What is asserted vs printed

* **PARITY (correctness — runs EVERYWHERE):** at every thread count the output
  is bit-identical to the numpy reference oracle. Order-preserving collect +
  pure integer counts ⇒ determinism (Golden Rule #4). A single off-by-thread
  would silently invalidate the blake3 run-identity hash.
* **SCALING numbers (printed everywhere):** throughput, speedup vs t=1, the
  effective GB/s, and the % of the M4 Pro ~224 GB/s memory wall — so the
  numbers land in the CI log.
* **SCALING-FLOOR assertion (gated):** a LOOSE non-regression floor (multi-
  thread not slower than 1-thread, on the best-of-N min) is asserted ONLY when
  ``tests.bench._o1_tiers.m4pro_strict_active()`` (the quiet self-hosted M4-Pro
  runner). On the shared 2-4-core CI runner and this 4-core dev container the
  floor is NOT gated on, because a noisy oversubscribed shared box can make a
  parallel run momentarily slower than serial — only parity runs there.

## Honest envelope (Golden Rule #10)

The kernel is **memory-bandwidth-bound, not compute-bound** (ADR-0004): the
inner op is a branchless AND + CMP, so throughput is governed by how fast
``bar_bits`` streams from cache/RAM, not the ALU. On the M4 Pro's 10 P-core /
~224 GB/s shared pool the realistic plateau is **~2-4×, NOT linear 10×**
(``docs/TRACK1_PERFORMANCE_BUDGET.md``). The numbers printed by THIS bench are
recorded on the **dev/CI container (4 cores)** — NOT the M4 Pro — and are
machine-dependent; they are a directional signal, never an SLA.
"""

from __future__ import annotations

import random
import time

import fno_math
import pytest
from brutex.engine.combo_mask import count_supermasks_reference

from tests.bench._o1_tiers import m4pro_strict_active

_N_BARS: int = 94_000
_N_MASKS: int = 10_000
_SEED: int = 42
_REPEATS: int = 5  # best-of-N: the min is the noise-robust true-cost estimate
_THREAD_COUNTS: tuple[int, ...] = (1, 2, 4, 6, 10)
_MEM_WALL_GBPS: float = 224.0  # M4 Pro shared-pool memory wall (TRACK1_PERFORMANCE_BUDGET)


def _build_workload() -> tuple[list[int], list[int]]:
    """Deterministic 94k-bar × 10k-mask workload (seeded — matches kernel bench)."""
    rng = random.Random(_SEED)
    bars = [rng.getrandbits(52) for _ in range(_N_BARS)]
    masks = [rng.getrandbits(5) | (1 << rng.randint(0, 51)) for _ in range(_N_MASKS)]
    return bars, masks


def _best_of_n(bars: list[int], masks: list[int], n_threads: int) -> tuple[float, list[int]]:
    """Best-of-N (min) wall time + result for the t-worker scoped-pool kernel."""
    best = float("inf")
    out: list[int] | None = None
    for _ in range(_REPEATS):
        t0 = time.perf_counter()
        out = list(fno_math.count_supermasks_u64s_nthreads(bars, masks, n_threads))
        best = min(best, time.perf_counter() - t0)
    assert out is not None
    return best, out


@pytest.mark.slow
@pytest.mark.bench
def test_supermask_scaling_multicore() -> None:
    """MEASURED multi-core scaling of the rayon Stage-2 scan (prints real numbers).

    Parity is asserted at EVERY thread count (correctness, runs everywhere). The
    scaling-floor assertion is gated behind the M4-Pro strict tier so the shared
    CI runner / 4-core dev container only run parity + print numbers.
    """
    bars, masks = _build_workload()
    work = _N_BARS * _N_MASKS

    # Bit-exact reference oracle (Golden Rule #4) — the scaling numbers are
    # meaningless if the kernel disagrees with the oracle.
    reference = count_supermasks_reference(bars, masks)

    # Warm the allocator + cache + default global pool.
    fno_math.count_supermasks_u64s_nthreads(bars, masks[:10], 4)

    results: dict[int, tuple[float, list[int]]] = {}
    for t in _THREAD_COUNTS:
        results[t] = _best_of_n(bars, masks, t)

    # PARITY at every thread count — correctness, asserted EVERYWHERE.
    for t, (_, out) in results.items():
        assert out == reference, f"rayon kernel (t={t}) disagrees with numpy reference oracle"

    t1_best = results[1][0]
    lines = [
        f"\nSUPERSET multi-core scaling bench (DEC-RUST-RAYON-SUPERMASK-001) "
        f"— best of {_REPEATS} runs — dev/CI container (4 cores), NOT M4 Pro:",
        f"  workload = {_N_BARS:,} bars × {_N_MASKS:,} masks = {work:,} mask·bar ops",
        "  threads |   best(s) |   M ops/s | speedup | eff GB/s | % of 224 GB/s wall",
    ]
    for t in _THREAD_COUNTS:
        best, _ = results[t]
        mops = work / best / 1e6
        speedup = t1_best / best
        eff_gbps = _N_BARS * 8 * _N_MASKS / best / 1e9
        pct_wall = eff_gbps / _MEM_WALL_GBPS * 100
        lines.append(
            f"  {t:7d} | {best:9.4f} | {mops:9.1f} | {speedup:6.2f}× | "
            f"{eff_gbps:8.1f} | {pct_wall:6.1f}%"
        )
    lines.append(
        "  NOTE: bandwidth-bound; M4 Pro 10-P-core plateau is ~2-4× (NOT linear 10×) "
        "per docs/TRACK1_PERFORMANCE_BUDGET.md — Golden Rule #10."
    )
    print("\n".join(lines))

    # SCALING-FLOOR (gated): only on the quiet self-hosted M4-Pro runner do we
    # assert a LOOSE non-regression floor — multi-thread must not be slower than
    # 1-thread on the best-of-N min. On the shared 2-4-core CI runner / this
    # 4-core dev container an oversubscribed box can momentarily invert that, so
    # we only run parity + print numbers there (Golden Rule #10 — no hard
    # multiple is EVER asserted; this is a coarse non-regression floor only).
    if m4pro_strict_active():
        for t in (2, 4):
            best_t = results[t][0]
            assert best_t <= t1_best * 1.05, (
                f"rayon scan at t={t} (best {best_t:.4f}s) is slower than t=1 "
                f"(best {t1_best:.4f}s) beyond the 5% non-regression floor — "
                f"the parallel dispatch or release profile regressed."
            )
