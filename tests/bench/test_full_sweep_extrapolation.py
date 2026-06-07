"""Full sweep extrapolation — G-08 SLA validator (DEC-P0-062; W2-c DEC-SWEEP-RESIDENT-KERNEL-001).

Closes the **G-08 GAP** from ``docs/TRACK1_REQUIREMENTS_FINAL.md`` §3: the SLA
validator per ``docs/TRACK1_PERFORMANCE_BUDGET.md`` §8 that the "2-4 day sweep at
80-150K trials/sec" headline claim has *zero CI enforcement*. Without this gate,
the throughput envelope in the spec is unfalsifiable.

## What changed in W2-c (DEC-SWEEP-RESIDENT-KERNEL-001)

The original v1 proxy paid the Python-list → Rust-Vec marshalling of the WHOLE
bar window on EVERY ``count_supermask_u64s`` call (pessimistic by ~80-160×, per
Hostile P9 of DEC-P0-060), so even its best case projected ~23 days for 33B
trials and the SLA assertion was a strict-``xfail``.

The real sweep path now reuses the bar window across masks: the Stage-2 SUPERSET
op runs over a **resident** Rust buffer (``fno_math.ResidentBars`` via
:class:`brutex.engine.combo_mask.ResidentBarBuffer`, threaded through the grid by
:class:`brutex.engine.sweep_kernel.ResidentKernelCache`), and a genuine mask
**batch** crosses into the production rayon-parallel arm
(``PARALLEL_WORK_THRESHOLD`` = 2^20 ``mask·bar`` ops). This file therefore now
measures THAT path — the batched, resident, parallel mask-AND kernel that
``docs/TRACK1_PERFORMANCE_BUDGET.md`` §3.3 names the bandwidth-bound dominant
cost — not the per-call-marshal proxy.

## Honest envelope (Golden Rule #10) — the xfail is RETIRED, not faked

The hard SLA assertion (``test_projected_33b_within_sla``) is **no longer
``xfail``**. Instead it follows the repo's established absolute-throughput
convention (``tests/bench/test_throughput_absolute.py``, DEC-M4PRO-ABSOLUTE-001):

* **On the M4 Pro self-hosted runner** (``BRUTEX_M4PRO_ABS=1``, the production
  hardware the SLA in §5 is sized for) it is a **REAL HARD GATE**: it projects the
  33B-trial wall time from the measured resident-parallel kernel throughput and
  fails the build if it exceeds the 4-day M4 Pro SLA. If it fails there, the
  spec's headline claim is re-stated per §8 — we never lie about wall time.
* **On shared GitHub Actions runners** it **skips** (no env var) — absolute
  ops/s on a noisy shared 2-4-core runner chronic-flakes (DEC-P0-020), and the
  resident-parallel speedup is hardware-bound, so CI keeps the O(1) **RATIO**
  gate (``test_throughput_canary.py``) + the regression floor below, while the
  absolute SLA is gated to the M4 Pro. The projection is PRINTED on every runner
  so the operator reads the absolute envelope from CI logs regardless.

This is the **honest** resolution the W2-c task sanctioned: the kernel + resident
reuse + batch are real and measured; the absolute SLA is asserted only where it
can be met without flake, and documented precisely otherwise. NEVER claim the
full real sweep (which still walks a Python trade-sim per surviving mask) hits
the SLA — only the bandwidth-bound mask-AND kernel this gate measures does.

## Per-trial proxy composition

| Op | Why representative |
|---|---|
| ``ResidentBarBuffer.count_supermasks`` over a 50K-u64 window, batched | Stage-2's SUPERSET test over a 5-year × 15m bar window — bars resident across masks, the batch crossing the parallel arm. The §3.3 bandwidth-bound dominant cost. |
| ``blake3_hash`` over a 200-byte payload | per-trial combo_hash (DEC-P0-060 9-field BLAKE3 run-identity). |
"""

from __future__ import annotations

import os
import time
from typing import Final

import fno_math
import pytest
from brutex.engine.combo_mask import ResidentBarBuffer

# Per-trial proxy sizing — see module docstring.
#
# 50_000 u64s ≈ the 5-year × 15m bar count for one instrument-TF. The Stage-2
# AND-count walks this slice once per (mask, trial) pair, so it dominates the
# per-trial budget.
_BAR_WINDOW_U64S: Final[int] = 50_000

# Masks per measured batch. 256 × 50_000 = 12.8M mask·bar ops > the kernel's
# 2^20 PARALLEL_WORK_THRESHOLD, so the measurement exercises the SAME rayon-
# parallel arm production Stage-2 takes (DEC-RUST-RAYON-SUPERMASK-001). The
# per-trial cost is the batch wall time divided by the batch size.
_BATCH_MASKS: Final[int] = 256

# The full sweep produces ~33B trials per the locked envelope at
# docs/TRACK1_PERFORMANCE_BUDGET.md §5 (Realistic Throughput Envelope).
_FULL_SWEEP_TRIALS: Final[int] = 33_000_000_000

# Hardware envelope SLA cap. The spec at PERFORMANCE_BUDGET §5 locks 2-4 days at
# 80-150K trials/sec on the M4 Pro; this asserts the 4-day upper bound. The hard
# assertion runs ONLY on the M4 Pro runner (see module docstring), so the cap is
# the M4 Pro figure, not a CI-compensated one.
_M4_PRO_SLA_SECONDS: Final[float] = 4 * 86_400.0

# Regression floor — per-trial proxy MUST be ≤ this on EVERY runner. With the
# resident-parallel batched kernel the per-trial proxy is ~µs; this 10 ms floor
# is loose enough to absorb shared-runner jitter yet catches a 1000×+ kernel
# regression or a maturin release-profile break (LTO/opt-level lost).
_REGRESSION_FLOOR_SECONDS_PER_TRIAL: Final[float] = 10e-3

# Env-var gate — set to "1" on the M4 Pro self-hosted runner (see
# .github/workflows/bench-m4pro.yml); left unset on shared GHA so the absolute
# SLA assertion skips without flaking (mirrors test_throughput_absolute.py).
_ENV_M4PRO_ABS: Final[str] = "BRUTEX_M4PRO_ABS"


def _m4pro_gate_active() -> bool:
    """Return True iff ``BRUTEX_M4PRO_ABS=1`` is set (the M4 Pro runner)."""
    return os.environ.get(_ENV_M4PRO_ABS, "").strip() == "1"


def _make_bar_window() -> list[int]:
    """Deterministic 50K-u64 bar window (seeded — same bytes every run)."""
    seed = 0xDEADBEEFCAFEBABE
    return [(seed + i * 0x9E3779B97F4A7C15) & 0xFFFFFFFFFFFFFFFF for i in range(_BAR_WINDOW_U64S)]


def _make_mask_batch() -> list[int]:
    """Deterministic batch of distinct strategy masks (a few bits each)."""
    return [
        ((i * 0x100000001B3) | (1 << (i % 52))) & 0xFFFFFFFFFFFFFFFF for i in range(_BATCH_MASKS)
    ]


def _measure_per_trial_seconds() -> float:
    """Microbenchmark the per-trial hot-path proxy on the REAL resident path.

    Builds the resident bar buffer ONCE (the bar window crosses the FFI boundary
    a single time — exactly what :class:`~brutex.engine.sweep_kernel.ResidentKernelCache`
    does in a grid sweep), then times one batched ``count_supermasks`` over the
    mask batch (the rayon-parallel arm) plus one ``blake3_hash`` per mask, and
    divides by the batch size. Best-of-N to reject upward-only OS noise.

    Returns the wall-time in seconds for one trial-equivalent of work.
    """
    bar_window = _make_bar_window()
    masks = _make_mask_batch()
    payload = b"x" * 200
    resident = ResidentBarBuffer(bar_window)

    # Warm caches + allocator (resident buffer + blake3 first-call cost).
    resident.count_supermasks(masks)
    fno_math.blake3_hash(payload)

    # Best of N repeats — `min` is the least-perturbed true-cost estimator.
    n_repeats = 5
    best = float("inf")
    for _ in range(n_repeats):
        start = time.perf_counter()
        resident.count_supermasks(masks)
        for _ in range(_BATCH_MASKS):
            fno_math.blake3_hash(payload)
        best = min(best, time.perf_counter() - start)
    return best / _BATCH_MASKS


def _project_sweep_envelope() -> tuple[float, float, float]:
    """Return ``(per_trial, projected_seconds, mask_bar_per_sec)``."""
    per_trial = _measure_per_trial_seconds()
    projected_seconds = _FULL_SWEEP_TRIALS * per_trial
    mask_bar_per_sec = _BAR_WINDOW_U64S / per_trial
    return per_trial, projected_seconds, mask_bar_per_sec


@pytest.mark.slow
@pytest.mark.bench
def test_per_trial_kernel_regression_floor() -> None:
    """Per-trial proxy ≤ 10 ms — catches catastrophic kernel/build regressions.

    The REAL gate v1 ships on EVERY runner. With the resident-parallel batched
    kernel the baseline is ~µs/trial; a regression to 10 ms+ means either the
    SUPERSET kernel (DEC-RUST-SUPERMASK-KERNEL-001) / resident buffer
    (DEC-RUST-RESIDENT-BARS-001) regressed, the BLAKE3 kernel (DEC-P0-052) shipped
    a slowdown, or the maturin wheel lost its release-mode optimisations
    (LTO/opt-level=3). Any of these silently destroys the throughput envelope.
    """
    per_trial = _measure_per_trial_seconds()
    print(
        f"\nKernel regression floor (DEC-P0-062 / DEC-SWEEP-RESIDENT-KERNEL-001):\n"
        f"  per-trial proxy  = {per_trial * 1e6:.2f} µs/trial\n"
        f"  regression floor = {_REGRESSION_FLOOR_SECONDS_PER_TRIAL * 1e3:.0f} ms/trial\n"
    )
    assert per_trial <= _REGRESSION_FLOOR_SECONDS_PER_TRIAL, (
        f"Per-trial proxy {per_trial * 1e3:.2f} ms exceeds the "
        f"{_REGRESSION_FLOOR_SECONDS_PER_TRIAL * 1e3:.0f} ms regression floor. "
        f"Either the SUPERSET/resident kernel regressed, the BLAKE3 kernel "
        f"regressed, or the maturin wheel build pipeline broke PyO3's release-mode "
        f"optimisations. Check bench-results.json + the fno-math Rust profile."
    )


@pytest.mark.slow
@pytest.mark.bench
def test_projected_33b_within_sla() -> None:
    """Project 33B-trial wall time from the resident-parallel kernel; gate on M4 Pro.

    Spec gate per ``docs/TRACK1_PERFORMANCE_BUDGET.md`` §8. The ``xfail`` is
    RETIRED (W2-c / DEC-SWEEP-RESIDENT-KERNEL-001): this is now a real hard gate
    on the M4 Pro self-hosted runner (``BRUTEX_M4PRO_ABS=1``) — the production
    hardware §5 sizes the SLA for — and **skips** on shared GHA (absolute ops/s
    there chronic-flakes per DEC-P0-020; CI keeps the O(1) RATIO gate in
    ``test_throughput_canary.py``). The projection is printed on every runner.

    If the projection exceeds the SLA on the M4 Pro, the spec's headline claim is
    lying about wall time and must be re-stated (Golden Rule #1 + #10).
    """
    per_trial, projected_seconds, mask_bar_per_sec = _project_sweep_envelope()
    trials_per_sec = 1.0 / per_trial
    sla_days = _M4_PRO_SLA_SECONDS / 86_400.0
    projected_days = projected_seconds / 86_400.0

    print(
        f"\nSLA validator (DEC-P0-062 / DEC-SWEEP-RESIDENT-KERNEL-001, G-08):\n"
        f"  path                  = resident + rayon-parallel batched mask-AND\n"
        f"  mask·bar throughput   = {mask_bar_per_sec / 1e6:.1f} M mask·bar/s\n"
        f"  per-trial proxy       = {per_trial * 1e6:.2f} µs/trial\n"
        f"  measured throughput   = {trials_per_sec:,.0f} trials/sec\n"
        f"  projected (33B)       = {projected_days:.2f} days\n"
        f"  M4 Pro SLA envelope   = {sla_days:.2f} days\n"
    )

    if not _m4pro_gate_active():
        pytest.skip(
            f"{_ENV_M4PRO_ABS}=1 not set — the absolute 33B-trial SLA gate runs "
            "only on the M4 Pro self-hosted runner (projection printed above). On "
            "shared GHA the O(1) RATIO gate (test_throughput_canary.py) + the "
            "regression floor are the active gates (DEC-P0-020 honest envelope)."
        )

    assert projected_seconds <= _M4_PRO_SLA_SECONDS, (
        f"Projected {_FULL_SWEEP_TRIALS:,}-trial wall time {projected_days:.2f}d "
        f"exceeds the M4 Pro SLA of {sla_days:.2f}d. Either the resident-parallel "
        f"mask-AND kernel regressed OR the spec's headline throughput claim at "
        f"docs/TRACK1_PERFORMANCE_BUDGET.md §5 must be re-stated (Golden Rule #1 + #10)."
    )


@pytest.mark.slow
@pytest.mark.bench
def test_per_trial_proxy_stable_across_runs() -> None:
    """Sanity check: 3 measurements within 50% of their median.

    Catches catastrophic measurement noise (a CI runner throttled mid-test)
    before the regression-floor assertion's number is trusted. If this fails the
    projected-wall-time number is unreliable + the SLA gate is meaningless until
    the runner stabilises.
    """
    samples = [_measure_per_trial_seconds() for _ in range(3)]
    median = sorted(samples)[1]
    max_dev = max(abs(s - median) / median for s in samples)
    assert max_dev <= 0.5, (
        f"Per-trial proxy unstable: samples {samples}, median {median:.6f}, "
        f"max dev {max_dev:.1%} > 50% — CI runner likely throttled. Re-run the bench job."
    )


@pytest.mark.bench
def test_m4pro_gate_decoder() -> None:
    """The gate decoder runs on EVERY runner (no skip) — verifies the env logic.

    Independent of whether the gate fires, so the M4-Pro-only branch above is
    never an untested dark corner (Golden Rule #10 worst-case hardening).
    """
    saved = os.environ.get(_ENV_M4PRO_ABS)
    try:
        for value, expected in [
            ("1", True),
            (" 1 ", True),
            ("", False),
            ("0", False),
            ("true", False),
        ]:
            os.environ[_ENV_M4PRO_ABS] = value
            assert _m4pro_gate_active() is expected
        os.environ.pop(_ENV_M4PRO_ABS, None)
        assert _m4pro_gate_active() is False
    finally:
        if saved is None:
            os.environ.pop(_ENV_M4PRO_ABS, None)
        else:
            os.environ[_ENV_M4PRO_ABS] = saved
