"""MEASURED before/after bench — resident bar buffer vs per-call re-marshal.

DEC-RUST-RESIDENT-BARS-001 (builds on DEC-RUST-SUPERMASK-KERNEL-001 #411 /
ADR-0004 §5.2). The operator's MERGE-READINESS CHARTER (CLAUDE.md Golden Rule
#13 + §11 AUTOMATED) requires a *measured* — never estimated — before/after for
any hot-path change. This bench compares:

* **Path A (before)** — the free ``fno_math.count_supermasks_u64s(bars, batch)``
  called once per mask batch. The whole ``bars`` (~94k u64) is re-marshalled
  Python→Rust on EVERY batch.
* **Path B (after)** — construct ``ResidentBarBuffer(bars)`` ONCE (timed
  separately), then call ``buf.count_supermasks(batch)`` per batch. ``bars`` is
  marshalled once; thereafter only ``batch`` crosses the FFI boundary.

## Workload

94 000 bars split into R = 100 mask-batches (10 000 masks total). 94k u64 ≈ the
5-year × 15m intraday bar count for one instrument-TF; the R batches model a
Stage-2 sweep that evaluates the SAME bar window against many successive mask
batches — the exact pattern the resident buffer optimizes.

## Honest envelope (Golden Rule #10)

The inner SUPERSET kernel ([`count_one`]) is BYTE-IDENTICAL between the two
paths — the win is purely the eliminated re-marshalling of ``bars`` across R
batches, and it GROWS with R and with N_bars. It is NOT an "O(1) Stage-2"
primitive: each call is still O(N_masks · N_bars) compute. Numbers are
machine-dependent (recorded on the CI/dev runner); the assertion is only a
coarse correctness + non-regression floor (resident must not be SLOWER than the
re-marshal path), so the test is portable across runners.

## De-flake: best-of-N timing

A single ``perf_counter`` sample of a sub-second window is sensitive to a GC
pause / scheduler preemption. We take the **minimum of N repeats** for each
path — ``min`` is the standard, honest microbenchmark estimator of true cost
(least perturbed by upward-only OS noise, never inflated). The floor compares
the two minima; the gap is decisive because the re-marshal cost is real.
"""

from __future__ import annotations

import random
import time

import fno_math
import pytest
from brutex.engine.combo_mask import ResidentBarBuffer, count_supermasks_reference

_N_BARS: int = 94_000
_N_BATCHES: int = 100  # R — the resident buffer is reused across this many
_MASKS_PER_BATCH: int = 100
_SEED: int = 42
_REPEATS: int = 5  # best-of-N: take the min, the noise-robust true-cost estimate


def _build_workload() -> tuple[list[int], list[list[int]]]:
    """Deterministic 94k-bar window + R mask-batches (seeded)."""
    rng = random.Random(_SEED)
    bars = [rng.getrandbits(52) for _ in range(_N_BARS)]
    batches = [
        [rng.getrandbits(5) | (1 << rng.randint(0, 51)) for _ in range(_MASKS_PER_BATCH)]
        for _ in range(_N_BATCHES)
    ]
    return bars, batches


@pytest.mark.slow
@pytest.mark.bench
def test_resident_bar_buffer_vs_remarshal() -> None:
    """MEASURED per-call re-marshal (A) vs resident buffer (B) — prints numbers."""
    bars, batches = _build_workload()
    total_masks = _N_BATCHES * _MASKS_PER_BATCH
    work = _N_BARS * total_masks

    # ── correctness: concatenated B == A == reference, per batch ──
    reference = [count_supermasks_reference(bars, b) for b in batches]
    path_a = [list(fno_math.count_supermasks_u64s(bars, b)) for b in batches]
    resident = ResidentBarBuffer(bars)
    path_b = [resident.count_supermasks(b) for b in batches]
    assert path_a == reference, "Path A (re-marshal) disagrees with reference oracle"
    assert path_b == reference, "Path B (resident) disagrees with reference oracle"

    # ── warm both paths (allocator + cache) ──
    fno_math.count_supermasks_u64s(bars, batches[0])
    ResidentBarBuffer(bars).count_supermasks(batches[0])

    # ── one-time construction cost (Path B pays this ONCE) ──
    ctor_t = float("inf")
    for _ in range(_REPEATS):
        t0 = time.perf_counter()
        _ = ResidentBarBuffer(bars)
        ctor_t = min(ctor_t, time.perf_counter() - t0)

    # ── Path A: re-marshal bars on every batch ──
    per_call_t = float("inf")
    for _ in range(_REPEATS):
        t0 = time.perf_counter()
        for batch in batches:
            fno_math.count_supermasks_u64s(bars, batch)
        per_call_t = min(per_call_t, time.perf_counter() - t0)

    # ── Path B: construct ONCE, then reuse across batches ──
    resident_t = float("inf")
    for _ in range(_REPEATS):
        buf = ResidentBarBuffer(bars)
        t0 = time.perf_counter()
        for batch in batches:
            buf.count_supermasks(batch)
        resident_t = min(resident_t, time.perf_counter() - t0)

    speedup = per_call_t / resident_t
    print(
        f"\nResident bar buffer bench (DEC-RUST-RESIDENT-BARS-001) "
        f"— best of {_REPEATS} runs:\n"
        f"  workload          = {_N_BARS:,} bars × {_N_BATCHES} batches × "
        f"{_MASKS_PER_BATCH} masks = {work:,} mask·bar ops\n"
        f"  one-time ctor     = {ctor_t * 1e3:.3f} ms (paid ONCE for path B)\n"
        f"  path A (remarshal)= {per_call_t:.4f}s | "
        f"{per_call_t / _N_BATCHES * 1e6:.2f} µs/batch | "
        f"{work / per_call_t / 1e6:.1f} M ops/s\n"
        f"  path B (resident) = {resident_t:.4f}s | "
        f"{resident_t / _N_BATCHES * 1e6:.2f} µs/batch | "
        f"{work / resident_t / 1e6:.1f} M ops/s\n"
        f"  speedup A/B       = {speedup:.2f}×\n"
    )

    # Non-regression floor: the resident path must not be SLOWER than the
    # re-marshal path (best-of-N minima). Loose by design / portable — we do
    # NOT assert a hard multiple (the win grows with R and N_bars but the
    # absolute ratio is machine-dependent).
    assert resident_t <= per_call_t, (
        f"Resident buffer (best {resident_t:.4f}s) slower than per-call "
        f"re-marshal (best {per_call_t:.4f}s) — regression in the resident path."
    )
