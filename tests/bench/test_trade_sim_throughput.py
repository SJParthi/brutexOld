"""Throughput + O(1) bench for the resident Rust trade-sim kernel.

DEC-SWEEP-RUST-TRADESIM-001 (MIGRATION.md #563). Measures the REAL combos/sec of
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
from typing import TYPE_CHECKING, Final, cast

import pytest

fno_math = pytest.importorskip("fno_math", reason="resident trade-sim kernel needs the built wheel")

from tests.bench._o1_tiers import ratio_ceiling  # noqa: E402

if TYPE_CHECKING:  # pragma: no cover - typing only (annotations are lazy)
    # `fno_math` above is a runtime variable (importorskip result); pyright forbids
    # it in a type expression. Import the class name for annotations only.
    from fno_math import ResidentSweepSim, ResidentSweepSimU128

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


def _tf1_arm_source(n_bars: int) -> list[int]:
    """The contiguous tf==1 fixed-point projection (DEC-FILL-EXEC-GRID-1M-001).

    1m bar ``j`` is the next-1m fill bar of decision bar ``j - 1`` (bar 0 is
    unarmed) — the exact ``ExecGridMap.arm_source`` a contiguous
    decision == exec stream yields at ``tf_minutes == 1``.
    """
    return [-1, *range(n_bars - 1)]


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
    # Single synthetic session (no forced bars either) — session_idx all-0 keeps
    # the bench window shape identical to the pre-session-gate kernel. The
    # dual-feed ctor (DEC-FILL-EXEC-GRID-1M-001) takes the 1m EXECUTION columns
    # + the arm_source projection; this window IS the tf==1 fixed point.
    session_idx: list[int] = [0] * n_bars
    return fno_math.ResidentSweepSim(
        open_p, high_p, low_p, close_p, is_forced, session_idx, _tf1_arm_source(n_bars), bar_bits
    )


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


# ── wired-path speedup gate (DEC-SWEEP-RUST-TRADESIM-WIRE-001) ────────────────
#
# The gates above bench the RAW kernel (`count_trades`, no ClosedTrade
# materialisation) — NOT the surface the production sweep actually calls. This
# gate benches the WIRED surface, `ResidentTradeSim.simulate_masks` (FFI +
# per-trade ClosedTrade construction included), as a RATIO against its own
# forced-Python-fallback arm on the same window — so a wiring regression
# (re-marshalling, per-trade object bloat) fails loudly while shared-runner
# jitter cannot (a ratio of two same-process measurements; DEC-P0-020 honest
# envelope: absolutes are printed, never asserted off the M4 Pro).
# Measured: ~257-266× on this sparse fixture in-sandbox; the blueprint spike
# read 40.5-45.2× at ~81 trades/mask (density-dependent). The floor is 15× —
# far under both measurements (jitter-safe as a same-process ratio) yet high
# enough that a real wiring regression costing an order of magnitude
# (re-marshalling, per-trade object bloat) fails, not merely a near-total
# loss of the kernel (the review's 5×-floor looseness finding).

_WIRED_SPEEDUP_FLOOR: Final[float] = 15.0


def _make_candle_window(n_bars: int) -> tuple[list, list[int]]:
    """Deterministic CandleBar window on real NSE session dates + synthetic bits."""
    from datetime import UTC, datetime, timedelta
    from decimal import Decimal

    from brutex.calendars.nse import IST
    from brutex.data.models import CandleBar

    seed = 0x9E3779B97F4A7C15
    bars: list[CandleBar] = []
    bits: list[int] = []
    per_day = 375  # a full 09:15–15:29 session of 1-minute bars
    day = 0
    minute = 0
    for i in range(n_bars):
        if minute == per_day:
            minute = 0
            day += 1
        ts = (
            datetime(2024, 1, 1, 9, 15, tzinfo=IST) + timedelta(days=day, minutes=minute)
        ).astimezone(UTC)
        minute += 1
        wobble = ((seed * (i + 1)) >> 13) % 200 - 100
        o = Decimal(10_000 + wobble) / 100
        c = o + Decimal((wobble % 30) - 10) / 100
        hi = max(o, c) + Decimal("1.50")
        lo = min(o, c) - Decimal("1.50")
        bars.append(
            CandleBar(
                timestamp=ts, open=o, high=hi, low=lo, close=c, volume=100, open_interest=None
            )
        )
        bits.append((seed * (i + 7)) & 0x000F_FFFF)
    return bars, bits


def _best_of(n: int, fn) -> float:  # type: ignore[no-untyped-def]
    best = float("inf")
    for _ in range(n):
        start = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - start)
    return best


@pytest.mark.slow
@pytest.mark.bench
def test_resident_trade_sim_wired_vs_fallback_speedup() -> None:
    """The wired surface (`simulate_masks`, ClosedTrade included) beats its own
    Python-oracle arm ≥ 15× — the production Phase-A lever, measured not assumed."""
    from collections.abc import Sequence

    from brutex.data.models import CandleBar
    from brutex.engine.rust_trade_sim import ResidentTradeSim
    from brutex.strategies.types import Side

    def _forced_fallback_trade_sim(
        bars: Sequence[CandleBar], bits: Sequence[int], *, tf_minutes: int
    ) -> ResidentTradeSim:
        """Kernel-disabled twin — the same instance-level ``_rust = None``
        factory seam the unit/golden `_forced_fallback_sim` fakes use."""
        # tf==1 fixed point: the same window is decision AND execution stream.
        sim = ResidentTradeSim(bars, bits, tf_minutes=tf_minutes, exec_bars_1m=bars)
        sim._rust = None  # pyright: ignore[reportPrivateUsage] — the DEC-P0-017 friend seam
        return sim

    bars, bits = _make_candle_window(6_000)
    masks = _make_masks(24)
    sl, tp = 8_000, 12_000
    # tf==1 fixed point: the same window is decision AND execution stream.
    sim = ResidentTradeSim(bars, bits, tf_minutes=1, exec_bars_1m=bars)
    assert sim.rust_active, "wheel is built in CI — the wired arm must engage the kernel"
    fallback = _forced_fallback_trade_sim(bars, bits, tf_minutes=1)
    assert not fallback.rust_active

    def _rust_arm() -> None:
        sim.simulate_masks(masks, direction=Side.BUY, stop_loss_paisa=sl, take_profit_paisa=tp)

    def _python_arm() -> None:
        fallback.simulate_masks(masks, direction=Side.BUY, stop_loss_paisa=sl, take_profit_paisa=tp)

    _rust_arm()  # warm allocator + lazy ClosedTrade import on both arms
    _python_arm()
    rust_s = _best_of(3, _rust_arm)
    python_s = _best_of(2, _python_arm)
    ratio = python_s / max(rust_s, 1e-12)
    n_combos = len(masks)
    print(
        f"\nWired ResidentTradeSim.simulate_masks vs Python-oracle arm "
        f"(6,000 bars × {n_combos} masks):\n"
        f"  rust   = {rust_s * 1e3:8.2f} ms  ({n_combos / rust_s:,.0f} combos/s)\n"
        f"  python = {python_s * 1e3:8.2f} ms  ({n_combos / python_s:,.0f} combos/s)\n"
        f"  ratio  = {ratio:.1f}×  (floor {_WIRED_SPEEDUP_FLOOR:.0f}×; density-dependent — "
        f"the blueprint spike read ~40× at ~81 trades/mask, sparser windows read higher)\n"
    )
    assert ratio >= _WIRED_SPEEDUP_FLOOR, (
        f"wired/fallback speedup {ratio:.1f}× fell below the {_WIRED_SPEEDUP_FLOOR:.0f}× floor "
        f"— the Phase-A wiring surface regressed (re-marshalling / object bloat / lost "
        f"release build), or the kernel silently stopped serving."
    )


# ── u128 kernel arm (DEC-TRADE-SIM-U128-KERNEL-001) ───────────────────────────
#
# The wide sibling kernel is MEASURED, never claimed-neutral: same window
# shape as the u64 gates with bar_bits/masks carrying genuine >64-bit words,
# through `ResidentSweepSimU128.count_trades`. The loose per-combo regression
# floor applies (a lost release build / catastrophic generic-core regression
# fails); the absolute number is printed for the operator on every runner.
# The u64 arm's own gates above are UNTOUCHED — they remain the u64
# non-regression pin.


def _make_sim_u128(n_bars: int) -> ResidentSweepSimU128:
    """The `_make_sim` window with each bar's bits widened past bit 64."""
    seed = 0x9E3779B97F4A7C15
    base = 1_000_000
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
        bar_bits.append(((seed * (i + 7)) & 0x000F_FFFF) | (1 << (64 + (i % 50))))
    # The same dual-feed ctor shape as `_make_sim` (tf==1 fixed point).
    session_idx: list[int] = [0] * n_bars
    return cast(
        "ResidentSweepSimU128",
        fno_math.ResidentSweepSimU128(
            open_p,
            high_p,
            low_p,
            close_p,
            is_forced,
            session_idx,
            _tf1_arm_source(n_bars),
            bar_bits,
        ),
    )


def _per_combo_seconds_u128(n_bars: int, n_masks: int) -> float:
    """Best-of-N wall time per combo for the u128 kernel's ``count_trades``."""
    sim = _make_sim_u128(n_bars)
    masks = [
        (((i * 0x100000001B3) | (1 << (i % 20))) & 0x000F_FFFF) | (1 << 64) for i in range(n_masks)
    ]
    sl, tp = 8_000, 12_000
    sim.count_trades(masks, True, sl, tp)  # warm allocator + caches
    best = float("inf")
    for _ in range(5):
        start = time.perf_counter()
        sim.count_trades(masks, True, sl, tp)
        best = min(best, time.perf_counter() - start)
    return best / n_masks


@pytest.mark.slow
@pytest.mark.bench
def test_trade_sim_u128_kernel_per_combo_measured() -> None:
    """The u128 arm is MEASURED (printed) and floor-gated on every runner.

    Honest envelope (Golden Rule #10): since the SuperTrend implemented-flip
    (DEC-SUPERTREND-CONDITIONS-001 — bits 64/65 LIVE) warm production windows
    are genuinely >u64, so this arm is the PRODUCTION regime, no longer
    future headroom (the same-window flip-shape ratio lives in
    ``test_vocab66_wide_grid.py``) — the measurement documents the wide
    kernel's real cost (the 128-bit AND is a two-limb compare; expect the same
    order of magnitude as the u64 arm, never asserted equal).
    """
    per_combo_u128 = _per_combo_seconds_u128(n_bars=20_000, n_masks=64)
    per_combo_u64 = _per_combo_seconds(n_bars=20_000, n_masks=64)
    print(
        f"\nRust trade-sim u128 kernel (DEC-TRADE-SIM-U128-KERNEL-001):\n"
        f"  u128 per-combo (20k bars) = {per_combo_u128 * 1e6:.2f} µs/combo"
        f"  ({1.0 / per_combo_u128:,.0f} combos/s)\n"
        f"  u64  per-combo (20k bars) = {per_combo_u64 * 1e6:.2f} µs/combo"
        f"  ({1.0 / per_combo_u64:,.0f} combos/s)\n"
        f"  u128/u64 ratio            = {per_combo_u128 / per_combo_u64:.2f}×"
        f"  (measured, informational — never asserted equal)\n"
    )
    assert per_combo_u128 <= _REGRESSION_FLOOR_SECONDS_PER_COMBO, (
        f"u128 per-combo {per_combo_u128 * 1e3:.2f} ms exceeds the "
        f"{_REGRESSION_FLOOR_SECONDS_PER_COMBO * 1e3:.0f} ms floor — the wide trade-sim "
        f"kernel regressed or the maturin wheel lost its release-mode build."
    )
