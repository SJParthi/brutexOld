"""MEASURED before/after bench — numpy reference vs Rust SUPERSET kernel.

DEC-RUST-SUPERMASK-KERNEL-001. The operator's MERGE-READINESS CHARTER
(CLAUDE.md Golden Rule #13 + §11 AUTOMATED) requires a *measured* — never
estimated — before/after for any hot-path change. This bench compares the
numpy reference oracle (``brutex.engine.combo_mask.count_supermasks_reference``)
against the Rust kernel (``fno_math.count_supermasks_u64s``) on a fixed,
realistic workload and prints throughput / per-trial / speedup so the numbers
land in the CI log + bench-results.

## Workload

94 000 bars × 10 000 masks = 940 M mask·bar SUPERSET ops. 94k u64 ≈ the
5-year × 15m intraday bar count for one instrument-TF; 10k masks ≈ a
mid-size Stage-2 mask batch. Both paths produce identical output (asserted)
— Golden Rule #4 bit-exact determinism.

## Honest envelope (Golden Rule #10)

Both timings include the Python ``list`` → Rust/numpy buffer marshalling
that real Stage-2 will eliminate via resident buffers; the speedup is the
*end-to-end facade* speedup a caller sees today, not the raw inner-loop
ratio. Numbers are machine-dependent (recorded on the CI/dev runner);
the assertion is only a coarse correctness + non-regression floor (Rust
must not be SLOWER than numpy), so the test is portable across runners.
"""

from __future__ import annotations

import random
import time

import fno_math
import pytest
from brutex.engine.combo_mask import count_supermasks_reference

_N_BARS: int = 94_000
_N_MASKS: int = 10_000
_SEED: int = 42


def _build_workload() -> tuple[list[int], list[int]]:
    """Deterministic 94k-bar × 10k-mask workload (seeded)."""
    rng = random.Random(_SEED)
    bars = [rng.getrandbits(52) for _ in range(_N_BARS)]
    # Masks: a few required bits each (realistic K=1..6 strategy masks).
    masks = [rng.getrandbits(5) | (1 << rng.randint(0, 51)) for _ in range(_N_MASKS)]
    return bars, masks


@pytest.mark.slow
@pytest.mark.bench
def test_supermask_kernel_numpy_vs_rust() -> None:
    """MEASURED numpy-reference vs Rust-kernel comparison (prints real numbers)."""
    bars, masks = _build_workload()
    work = _N_BARS * _N_MASKS

    # Warm both paths (allocator + cache).
    fno_math.count_supermasks_u64s(bars, masks[:10])
    count_supermasks_reference(bars, masks[:10])

    # numpy reference.
    t0 = time.perf_counter()
    ref = count_supermasks_reference(bars, masks)
    np_t = time.perf_counter() - t0

    # Rust kernel.
    t0 = time.perf_counter()
    rust = list(fno_math.count_supermasks_u64s(bars, masks))
    rust_t = time.perf_counter() - t0

    # Bit-exact parity (Golden Rule #4) — the speedup is meaningless if
    # the kernel disagrees with the oracle.
    assert rust == ref, "Rust kernel disagrees with numpy reference oracle"

    speedup = np_t / rust_t
    print(
        f"\nSUPERSET kernel bench (DEC-RUST-SUPERMASK-KERNEL-001):\n"
        f"  workload          = {_N_BARS:,} bars × {_N_MASKS:,} masks "
        f"= {work:,} mask·bar ops\n"
        f"  numpy reference   = {np_t:.4f}s | {work / np_t / 1e6:.1f} M ops/s | "
        f"{np_t / _N_MASKS * 1e6:.2f} µs/trial\n"
        f"  rust kernel       = {rust_t:.4f}s | {work / rust_t / 1e6:.1f} M ops/s | "
        f"{rust_t / _N_MASKS * 1e6:.2f} µs/trial\n"
        f"  speedup           = {speedup:.2f}×\n"
        f"  rust per bar·mask = {rust_t / work * 1e9:.3f} ns\n"
    )

    # Non-regression floor: Rust must be at least as fast as numpy. Loose
    # so the gate is portable across runners (per the chronic-flake history
    # in tests/bench/test_throughput_canary).
    assert rust_t <= np_t, (
        f"Rust SUPERSET kernel ({rust_t:.4f}s) slower than numpy reference "
        f"({np_t:.4f}s) — the kernel or maturin release profile regressed."
    )
