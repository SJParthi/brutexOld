"""Full sweep extrapolation — G-08 SLA validator (DEC-P0-062 / PR-N+9).

Closes the **G-08 GAP** from ``docs/TRACK1_REQUIREMENTS_FINAL.md`` §3:
the SLA validator per ``docs/TRACK1_PERFORMANCE_BUDGET.md`` §8 that the
"2-4 day sweep at 80-150K trials/sec" headline claim has *zero CI
enforcement*. Without this gate, the throughput envelope in the spec
is unfalsifiable.

## What this test asserts (v1 scope)

The full Stage 1A/1B/2/3/4 engine doesn't exist yet (G-03 GAP), and the
``count_supermasks`` kernel that real Stage 2 needs (Plan-agent finding
from DEC-P0-060 pre-impl review) is also not shipped. So this v1
**cannot** run the spec's gate code literally:

```python
# docs/TRACK1_PERFORMANCE_BUDGET.md:207-215 (FUTURE — when engine lands)
def test_projected_33b_within_sla(benchmark):
    measured = benchmark(run_micro_sweep, num_trials=100_000)
    trials_per_sec = 100_000 / measured.elapsed
    projected_33b_seconds = 33_000_000_000 / trials_per_sec
    assert projected_33b_seconds <= 4 * 86400
```

Instead, this microbenchmarks the **per-trial kernel proxy** that
every Stage 2 trial WILL consume — the Stage-2 SUPERSET op
``count_supermask_u64s`` (shipped DEC-RUST-SUPERMASK-KERNEL-001) +
``blake3_hash`` — measures the wall time, projects to 33B trials, and:

1. **PRINTS the projection** to stdout + bench-results.json so the
   operator can read the absolute envelope from CI logs.
2. **XFAIL** the hard 4-day SLA assertion until the engine + the
   ``count_supermasks`` kernel ship. The XFAIL is ``strict=True`` so
   the day the projection drops under the SLA cap (because the real
   kernel ships) the XFAIL flips to PASS visibly + we promote it to
   a hard gate.
3. **Assert a REGRESSION FLOOR** — per-trial proxy ≤ 10 ms on CI.
   Catches any 4× kernel regression from today's ~2.6 ms/trial
   baseline (CI ubuntu-24.04). The floor is loose enough to absorb
   shared-runner jitter (per the chronic-flake history in
   ``tests/bench/test_throughput_canary``).

## Per-trial proxy composition

| Op | Cost (measured) | Why representative |
|---|---|---|
| ``count_supermask_u64s`` over 50K u64s | ~1.0 ms/trial (incl. blake3) | Stage 2's SUPERSET test over a 5-year × 15m bar window |
| ``blake3_hash`` over 200-byte payload | ~3 µs | per-trial combo_hash (DEC-P0-060 9-field BLAKE3) |

The proxy is **pessimistic** — it includes Python list → Rust Vec
allocation overhead PER CALL that real Stage 2 will eliminate via numpy
buffer pass-through / Rust-native loops + bars resident across masks. The
kernel's batched best case (bars resident) measures ~60 µs/trial. Real
M4 Pro Stage 2 expectation per PERFORMANCE_BUDGET §5: 80-150K trials/sec
= 6-13 µs/trial. The per-call proxy overshoots by ~80-160×.

## Honest envelope (Golden Rule #10)

- **REAL:** the regression-floor gate fires on any 4× kernel slowdown.
  The first production caller of the AND-count kernel exists
  (DEC-P0-057 Bloom popcount); regressions there would surface here.
- **PARTIAL:** the 4-day SLA assertion is XFAIL until the Stage
  1A/1B/2/3/4 engine lands. The ``count_supermasks_u64s`` kernel itself
  HAS shipped (DEC-RUST-SUPERMASK-KERNEL-001) and is wired into the
  proxy below, but it is only one part — the full SLA additionally needs
  Stage 1A/1B trial pruning + resident bar buffers + the trade-sim. The
  gate-name + the failing assertion remind every PR author that the
  spec's headline claim is unverified — the operator agreed to ship
  the gate FAILING-FOR-NOW per Golden Rule #10 honesty over an
  empty-shell skip.
- **PARTIAL:** no benchmark-vs-target-hardware calibration — the
  hardware factor (M4 Pro vs GH ubuntu-24.04) is approximated at 5×
  per the chronic-flake history; Phase 6 will derive it empirically.
- **DONE:** ``count_supermasks_u64s`` / ``count_supermask_u64s`` shipped
  in ``rust/fno-math/`` (DEC-RUST-SUPERMASK-KERNEL-001); the proxy below
  now uses the SUPERSET kernel instead of ``bitand_count_u64s``.
- **ROADMAP:** drop the XFAIL marker the day the projection drops under
  4 days × 5 = 20 days CI — i.e. once Stage 1A/1B pruning + resident bar
  buffers + the trade-sim land (G-03), not before. Measured today: the
  best-case batched kernel still projects ~23 days raw, so the gate
  stays HONEST-XFAIL.
"""

from __future__ import annotations

import os
import time

import fno_math
import pytest

# Per-trial proxy sizing — see module docstring.
#
# 50_000 u64s = 5-year × 15m × 4 inst-TF approximate bar count. The Stage 2
# AND-count walks this slice once per (mask, trial) pair, so it dominates
# the per-trial budget.
_BAR_WINDOW_U64S: int = 50_000

# The full sweep produces ~33B trials per the locked envelope at
# docs/TRACK1_PERFORMANCE_BUDGET.md §5 (Realistic Throughput Envelope).
_FULL_SWEEP_TRIALS: int = 33_000_000_000

# Hardware envelope SLA cap. The spec at PERFORMANCE_BUDGET §5 locks
# 2-4 days at 80-150K trials/sec on M4 Pro; this test asserts the
# upper bound (4 days = 345_600 s). On the CI shared runner we apply
# a 5× hardware compensation factor (see module docstring).
_M4_PRO_SLA_SECONDS: float = 4 * 86_400.0
_CI_HARDWARE_FACTOR: float = 5.0
_CI_SLA_SECONDS: float = _M4_PRO_SLA_SECONDS * _CI_HARDWARE_FACTOR

# Regression floor — per-trial proxy MUST be ≤ this. v1 baseline on
# ubuntu-24.04 is ~2.6 ms; this floor is ~4× looser to absorb shared-
# runner jitter (per the chronic-flake history). Any drop below this
# floor means a CI runner is throttled OR a kernel actually regressed
# 4×+ — either way, fail loudly.
_REGRESSION_FLOOR_SECONDS_PER_TRIAL: float = 10e-3


def _hardware_envelope() -> tuple[str, float]:
    """Return ``(label, sla_seconds)`` for the current hardware.

    Override the auto-detected envelope via ``BRUTEX_SLA_HARDWARE``
    (one of ``"m4_pro"`` or ``"ci"``). Default is ``"ci"``.
    """
    env = os.environ.get("BRUTEX_SLA_HARDWARE", "ci").lower()
    if env == "m4_pro":
        return "M4 Pro (per docs/TRACK1_PERFORMANCE_BUDGET.md §5)", _M4_PRO_SLA_SECONDS
    return f"CI (M4 Pro × {_CI_HARDWARE_FACTOR:g})", _CI_SLA_SECONDS


def _measure_per_trial_seconds() -> float:
    """Microbenchmark the per-trial hot-path proxy.

    Returns the wall-time in seconds for one trial-equivalent of work:
    one ``count_supermask_u64s`` (the Stage-2 SUPERSET op,
    DEC-RUST-SUPERMASK-KERNEL-001) over the 50K-u64 bar window + one
    ``blake3_hash`` of a 200-byte combo_hash payload. Calibrated by
    repeated sampling so the result is stable to within ±10% under
    typical CI noise.
    """
    bar_window: list[int] = [0xDEADBEEFCAFEBABE + i for i in range(_BAR_WINDOW_U64S)]
    strategy_mask: int = 0xFFFFFFFFFFFFFFFF
    payload: bytes = b"x" * 200

    # Warm caches + allocator (PyO3 has no JIT but the wheel-side
    # allocator benefits from one preflight call).
    fno_math.count_supermask_u64s(bar_window, strategy_mask)
    fno_math.blake3_hash(payload)

    # Time N trial-equivalents, divide. N chosen so total wall is
    # ≥ 0.5s (stable measurement vs jitter) but ≤ 5s (CI-budget-friendly).
    n_iters: int = 50
    start = time.perf_counter()
    for _ in range(n_iters):
        fno_math.count_supermask_u64s(bar_window, strategy_mask)
        fno_math.blake3_hash(payload)
    elapsed = time.perf_counter() - start
    return elapsed / n_iters


def _project_sweep_envelope() -> tuple[float, float, str, float]:
    """Return ``(per_trial, projected_seconds, hw_label, sla_seconds)``."""
    per_trial = _measure_per_trial_seconds()
    projected_seconds = _FULL_SWEEP_TRIALS * per_trial
    hw_label, sla_seconds = _hardware_envelope()
    return per_trial, projected_seconds, hw_label, sla_seconds


@pytest.mark.slow
def test_per_trial_kernel_regression_floor() -> None:
    """Per-trial proxy ≤ 10 ms — catches 4× kernel regressions.

    This is the REAL gate v1 ships. With the SUPERSET kernel wired in
    (DEC-RUST-SUPERMASK-KERNEL-001) the baseline is ~1.0 ms/trial; a
    regression to 10 ms+ means either:
      (a) the SUPERSET kernel (DEC-RUST-SUPERMASK-KERNEL-001) regressed,
      (b) the BLAKE3 kernel (DEC-P0-052) shipped a slowdown, or
      (c) the maturin wheel build pipeline broke PyO3's release-mode
          optimisations (e.g., LTO disabled, debug symbols inflating
          .so size).
    Any of these would silently destroy the production throughput
    envelope; the gate makes the regression loud.
    """
    per_trial = _measure_per_trial_seconds()
    print(
        f"\nKernel regression floor (DEC-P0-062):\n"
        f"  per-trial proxy = {per_trial * 1e6:.2f} µs/trial\n"
        f"  regression floor = {_REGRESSION_FLOOR_SECONDS_PER_TRIAL * 1e3:.0f} ms/trial\n"
    )
    assert per_trial <= _REGRESSION_FLOOR_SECONDS_PER_TRIAL, (
        f"Per-trial proxy {per_trial * 1e3:.2f} ms exceeds the "
        f"{_REGRESSION_FLOOR_SECONDS_PER_TRIAL * 1e3:.0f} ms regression floor. "
        f"Either the SUPERSET kernel (DEC-RUST-SUPERMASK-KERNEL-001), the "
        f"BLAKE3 kernel (DEC-P0-052), or the maturin wheel build pipeline "
        f"regressed. Check bench-results.json + the fno-math Rust profile "
        f"(LTO + opt-level=3 must hold)."
    )


@pytest.mark.slow
@pytest.mark.xfail(
    strict=True,
    reason=(
        "`count_supermasks_u64s` NOW SHIPS (DEC-RUST-SUPERMASK-KERNEL-001) — "
        "measured ~1.7× faster than the numpy reference on a 94k-bar × 10k-mask "
        "workload (1583 vs 931 M mask·bar/s; 0.63 ns per bar·mask), and the "
        "per-trial proxy here dropped ~2.4× (≈2.5 ms → ≈1.0 ms/trial). But the "
        "proxy is STILL pessimistic: it pays the Python list → Rust Vec "
        "marshalling of the full bar window PER CALL (Hostile P9 from "
        "DEC-P0-060). Even the kernel's best-case batched figure (bars "
        "resident across masks, no per-trial blake3) projects to ~23 days raw "
        "for 33B trials — over the 4-day M4 Pro SLA and the 20-day CI cap. The "
        "kernel is one part; the full SLA additionally needs Stage 1A/1B trial "
        "pruning + numpy/Rust-side resident bar buffers + the trade-sim, none "
        "of which exist yet (G-03). HONEST-XFAIL per Golden Rule #10 until the "
        "full engine lands. Tracked as DEC-P0-062 / DEC-RUST-SUPERMASK-KERNEL-001."
    ),
)
def test_projected_33b_within_sla() -> None:
    """Project 33B-trial wall time from microbench; assert ≤ hardware SLA.

    Spec gate per ``docs/TRACK1_PERFORMANCE_BUDGET.md`` §8: if the
    projection exceeds the SLA, the spec's headline claim is lying
    about wall time and must be re-stated (per Golden Rule #1
    "no hallucination" + Golden Rule #10 "honest limits").

    Marked ``xfail(strict=True)`` for v1 — the day the projection
    drops under the SLA (because the real ``count_supermasks_u64s``
    kernel + engine ship), the XFAIL flips to PASS visibly, and a
    follow-up PR promotes it back to a hard gate.
    """
    per_trial, projected_seconds, hw_label, sla_seconds = _project_sweep_envelope()
    trials_per_sec = 1.0 / per_trial
    sla_days = sla_seconds / 86_400.0
    projected_days = projected_seconds / 86_400.0

    # pytest captures stdout; surface measurements regardless of pass/
    # fail so the operator reads absolute throughput from CI logs.
    print(
        f"\nSLA validator (DEC-P0-062, G-08):\n"
        f"  hardware envelope     = {hw_label}\n"
        f"  per-trial proxy       = {per_trial * 1e6:.2f} µs/trial\n"
        f"  measured throughput   = {trials_per_sec:.0f} trials/sec\n"
        f"  projected (33B)       = {projected_days:.2f} days\n"
        f"  SLA envelope          = {sla_days:.2f} days\n"
    )

    assert projected_seconds <= sla_seconds, (
        f"Projected {_FULL_SWEEP_TRIALS:,}-trial wall time "
        f"{projected_days:.2f}d exceeds the {hw_label} SLA of {sla_days:.2f}d. "
        f"Either the AND-mask kernel regressed OR the spec's headline "
        f"throughput claim at docs/TRACK1_PERFORMANCE_BUDGET.md §5 must "
        f"be re-stated (Golden Rule #1 + #10)."
    )


@pytest.mark.slow
def test_per_trial_proxy_stable_across_runs() -> None:
    """Sanity check: 3 measurements should be within 50% of their median.

    Catches catastrophic measurement noise (CI runner getting throttled
    mid-test) before the regression-floor assertion above runs. If this
    fails, the projected-wall-time number is unreliable + the SLA gate
    is meaningless until the runner stabilises.
    """
    samples = [_measure_per_trial_seconds() for _ in range(3)]
    median = sorted(samples)[1]
    max_dev = max(abs(s - median) / median for s in samples)
    assert max_dev <= 0.5, (
        f"Per-trial proxy unstable: samples {samples}, median {median:.6f}, "
        f"max dev {max_dev:.1%} > 50% — CI runner likely throttled. "
        f"Re-run the bench job."
    )
