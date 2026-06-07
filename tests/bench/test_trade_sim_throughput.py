"""Throughput + O(1) bench for the resident Rust trade-sim kernel.

DEC-SWEEP-RUST-TRADESIM-001 (MIGRATION.md #561). Measures the REAL combos/sec of
the fused mask-eval + trade-walk hot loop (``fno_math.ResidentSweepSim``) — the
work the per-combo sweep runs ~33 billion times — over a resident bar window
(loaded ONCE, reused across all masks). Three gates, mirroring the established
bench conventions (``tests/bench/_o1_tiers.py`` + ``test_throughput_absolute.py``):

* **O(1) ratio** (every runner) — per-``(mask·bar)`` cost is flat across bar-window
  sizes (``ratio ≤`` the shared-CI ceiling, or the strict 1.1× on the M4-Pro
  runner). This is the honest claim: O(1) PER operation, NOT O(1) total.
* **Regression floor** (every runner) — per-combo cost ≤ a loose ceiling; catches a
  catastrophic kernel/build regression (lost release-mode optimisation).
* **Absolute combos/s** — PRINTED on every runner (so the operator reads the real
  envelope from logs); the M4-Pro SLA assertion is gated to the self-hosted runner
  (``BRUTEX_M4PRO_ABS=1``) per DEC-M4PRO-ABSOLUTE-001 / DEC-P0-020. NEVER faked.

Honest envelope (Golden Rule #10): the absolute number measured on a shared
x86_64 GitHub runner is NOT the M4 Pro SLA figure; it is printed for transparency
and the per-combo regression floor + O(1) ratio are the active shared-CI gates.
"""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING, Final

import pytest

fno_math = pytest.importorskip("fno_math", reason="resident trade-sim kernel needs the built wheel")

from tests.bench._o1_tiers import ratio_ceiling  # noqa: E402

if TYPE_CHECKING:  # pragma: no cover - typing only (annotations are lazy)
    # `fno_math` above is a runtime variable (importorskip result); pyright forbids
    # it in a type expression. Import the class name for annotations only.
    from fno_math import ResidentSweepSim

# Env-var gate — set to "1" on the M4 Pro self-hosted runner (bench-m4pro.yml).
_ENV_M4PRO_ABS: Final[str] = "BRUTEX_M4PRO_ABS"

# Per-combo regression floor — a single combo (one mask walked over the resident
# window) must cost ≤ this on EVERY runner. Loose enough to absorb shared-runner
# jitter, tight enough to catch a 1000×+ kernel/build regression.
_REGRESSION_FLOOR_SECONDS_PER_COMBO: Final[float] = 5e-3

# O(1) ratio shared-CI ceiling (strict 1.1× on the M4-Pro runner via _o1_tiers).
_RATIO_CEILING: Final[float] = ratio_ceiling(8.0)


def _m4pro_abs_active() -> bool:
    return os.environ.get(_ENV_M4PRO_ABS, "").strip() == "1"


def _make_sim(n_bars: int) -> ResidentSweepSim:
    """Deterministic resident sim: a gently oscillating window, sparse bar_bits."""
    seed = 0x9E3779B97F4A7C15
    base = 1_000_000  # ₹10,000.00 in paisa
    open_p: list[int] = []
    high_p: list[int] = []
    low_p: list[int] = []
    close_p: list[int] = []
    bar_bits: list[int] = []
    is_forced: list[bool] = [False] * n_bars
    for i in range(n_bars):
        wobble = ((seed * (i + 1)) >> 13) % 200 - 100
        o = base + wobble
        open_p.append(o)
        high_p.append(o + 150)
        low_p.append(o - 150)
        close_p.append(o + (wobble % 30))
        # A few condition bits per bar so masks fire intermittently.
        bar_bits.append((seed * (i + 7)) & 0x000F_FFFF)
    return fno_math.ResidentSweepSim(open_p, high_p, low_p, close_p, is_forced, bar_bits)


def _make_masks(n: int) -> list[int]:
    return [((i * 0x100000001B3) | (1 << (i % 20))) & 0x000F_FFFF for i in range(n)]


def _per_combo_seconds(n_bars: int, n_masks: int) -> float:
    """Best-of-N wall time per combo for ``count_trades`` over a resident window."""
    sim = _make_sim(n_bars)
    masks = _make_masks(n_masks)
    sl, tp = 8_000, 12_000  # ₹80 / ₹120 stop/target — trades close, bounded count
    sim.count_trades(masks, True, sl, tp)  # warm allocator + caches
    best = float("inf")
    for _ in range(5):
        start = time.perf_counter()
        sim.count_trades(masks, True, sl, tp)
        best = min(best, time.perf_counter() - start)
    return best / n_masks


@pytest.mark.slow
@pytest.mark.bench
def test_trade_sim_per_combo_regression_floor() -> None:
    """Per-combo cost ≤ the regression floor on every runner."""
    # 64 masks × 20_000 bars = 1.28M mask·bar ops > the kernel's 2^20 parallel
    # threshold, so this exercises the production rayon-parallel arm.
    per_combo = _per_combo_seconds(n_bars=20_000, n_masks=64)
    print(
        f"\nRust trade-sim kernel (DEC-SWEEP-RUST-TRADESIM-001):\n"
        f"  per-combo (20k bars) = {per_combo * 1e6:.2f} µs/combo\n"
        f"  combos/sec           = {1.0 / per_combo:,.0f}\n"
        f"  regression floor     = {_REGRESSION_FLOOR_SECONDS_PER_COMBO * 1e3:.0f} ms/combo\n"
    )
    assert per_combo <= _REGRESSION_FLOOR_SECONDS_PER_COMBO, (
        f"Per-combo {per_combo * 1e3:.2f} ms exceeds the "
        f"{_REGRESSION_FLOOR_SECONDS_PER_COMBO * 1e3:.0f} ms floor — the resident "
        f"trade-sim kernel regressed or the maturin wheel lost its release-mode build."
    )


@pytest.mark.slow
@pytest.mark.bench
def test_trade_sim_o1_per_bar_ratio() -> None:
    """Per-``(mask·bar)`` cost is flat across bar-window sizes (O(1) per op).

    Both windows cross the 2^20 parallel threshold (same production arm), so the
    ratio measures per-op scaling, not an arm switch. NOT an O(1)-total claim — the
    sweep is inherently O(N_masks · N_bars) (Golden Rule #10).
    """
    n_masks = 64
    small_bars = 20_000
    large_bars = 80_000
    small_per_op = _per_combo_seconds(small_bars, n_masks) / small_bars
    large_per_op = _per_combo_seconds(large_bars, n_masks) / large_bars
    ratio = large_per_op / max(small_per_op, 1e-12)
    print(
        f"\nRust trade-sim O(1)-per-op ratio:\n"
        f"  per-op @ {small_bars} bars = {small_per_op * 1e9:.3f} ns\n"
        f"  per-op @ {large_bars} bars = {large_per_op * 1e9:.3f} ns\n"
        f"  ratio                  = {ratio:.2f}× (ceiling {_RATIO_CEILING:.2f}×)\n"
    )
    assert ratio <= _RATIO_CEILING, (
        f"per-(mask·bar) cost ratio {ratio:.2f}× exceeds {_RATIO_CEILING:.2f}× — "
        f"possible super-linear (non-O(1)) regression in the trade-sim kernel."
    )


@pytest.mark.slow
@pytest.mark.bench
def test_trade_sim_absolute_combos_per_sec_printed() -> None:
    """Print the measured combos/sec; assert the M4-Pro SLA only on that runner.

    On shared GitHub runners absolute ops/s chronic-flakes (DEC-P0-020), so the
    absolute assertion skips (the number is still printed for the operator) and the
    O(1) ratio + regression floor are the active gates. The M4-Pro target is the
    ≥ 50 M mask·bar/s envelope from STANDARDS_100PCT.md §1 row 14 — measured, never
    fabricated.
    """
    n_bars, n_masks = 20_000, 64
    per_combo = _per_combo_seconds(n_bars, n_masks)
    combos_per_sec = 1.0 / per_combo
    mask_bar_per_sec = n_bars / per_combo
    print(
        f"\nRust trade-sim absolute throughput (printed on every runner):\n"
        f"  combos/sec         = {combos_per_sec:,.0f}\n"
        f"  mask·bar/sec       = {mask_bar_per_sec / 1e6:.1f} M\n"
    )
    if not _m4pro_abs_active():
        pytest.skip(
            f"{_ENV_M4PRO_ABS}=1 not set — the absolute combos/s SLA gate runs only "
            "on the M4 Pro self-hosted runner (number printed above). Shared CI keeps "
            "the O(1) ratio + regression-floor gates (DEC-P0-020 honest envelope)."
        )
    # M4 Pro target: ≥ 50 M mask·bar/s for the brute-force hot path.
    assert mask_bar_per_sec >= 50_000_000, (
        f"M4 Pro mask·bar/s {mask_bar_per_sec / 1e6:.1f} M below the 50 M target "
        f"(STANDARDS_100PCT.md §1 row 14) — the kernel or its SLA must be re-stated."
    )
