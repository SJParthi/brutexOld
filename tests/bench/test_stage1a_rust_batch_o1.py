"""O(1)-per-bar bench + A/B measurement for the batch Rust Stage-1A arm.

DEC-STAGE1A-RUST-BATCH-001 §6.7. Two probes:

* **Engaged-arm flatness** — the flag-ON build's per-bar cost at ``N_big``
  stays within the shared-CI ceiling of ``N_small`` (the same two-tier
  ``_o1_tiers`` discipline as ``test_engine_stage1a_o1``; the prepass, the
  batch FFI call and the Rust loop are each O(N), so per-bar cost is flat).
* **A/B ON-vs-OFF wall clock** — MEASURED and printed for the PR body's
  honest-envelope table; deliberately NO gate floor (Golden Rule #10: no
  speedup is asserted until real M4-Pro numbers exist — the flag ships OFF).

NO O-class claim: the batch arm is a constant-factor change; ``build``
remains O(N_bars) on both arms.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from brutex.data.models import CandleBar
from brutex.engine.rust_stage1a import (
    STAGE1A_RUST_ENV,
    reset_observability_memo_for_tests,
    reset_self_check_for_tests,
)
from brutex.engine.stage1a import build_bar_bits

from tests.bench._o1_tiers import ratio_ceiling

_N_SMALL: int = 1_000
_N_BIG: int = 20_000
_BARS_PER_DAY: int = 375
_OPEN_UTC = datetime(2024, 1, 1, 3, 45, tzinfo=UTC)
_REPEATS: int = 3  # best-of-N: the min is the honest true-cost estimate


def _make_bars(n: int) -> list[CandleBar]:
    """Deterministic ``n``-bar stream at a fixed intraday density."""
    bars: list[CandleBar] = []
    for i in range(n):
        day, minute = divmod(i, _BARS_PER_DAY)
        ts = _OPEN_UTC + timedelta(days=day, minutes=minute)
        close = 1000 + (i % 7)
        bars.append(
            CandleBar(
                timestamp=ts,
                open=Decimal(1000),
                high=Decimal(1010),
                low=Decimal(990),
                close=Decimal(close),
                volume=100,
                open_interest=None,
            )
        )
    return bars


def _per_bar_seconds(bars: list[CandleBar]) -> float:
    """Best-of-N minimum mean per-bar ``build_bar_bits`` latency (seconds)."""
    n = len(bars)
    best = float("inf")
    for _ in range(_REPEATS):
        t0 = time.perf_counter()
        build_bar_bits(bars)
        best = min(best, (time.perf_counter() - t0) / n)
    return best


@pytest.mark.slow
@pytest.mark.bench
def test_engaged_arm_per_bar_cost_is_flat(monkeypatch: pytest.MonkeyPatch) -> None:
    """Flag-ON per-bar cost at ``N_big`` within the CI ceiling of ``N_small``."""
    monkeypatch.setenv(STAGE1A_RUST_ENV, "1")
    reset_self_check_for_tests(remaining=0)  # steady state: no burn-in oracle
    reset_observability_memo_for_tests()
    try:
        small = _make_bars(_N_SMALL)
        big = _make_bars(_N_BIG)
        build_bar_bits(small)  # warm caches / allocator per size
        build_bar_bits(big)
        per_bar_small = _per_bar_seconds(small)
        per_bar_big = _per_bar_seconds(big)
    finally:
        reset_self_check_for_tests()
        reset_observability_memo_for_tests()
    ratio = per_bar_big / max(per_bar_small, 1e-12)
    ceiling = ratio_ceiling(5.0)
    print(
        f"\nBatch-Rust Stage-1A engaged-arm O(1) bench (DEC-STAGE1A-RUST-BATCH-001) "
        f"— best of {_REPEATS} runs:\n"
        f"  N_small = {_N_SMALL:,} bars | {per_bar_small * 1e9:.1f} ns/bar\n"
        f"  N_big   = {_N_BIG:,} bars | {per_bar_big * 1e9:.1f} ns/bar\n"
        f"  ratio   = {ratio:.2f}x (ceiling {ceiling}x)\n"
    )
    assert ratio < ceiling, (
        f"flag-ON per-bar build cost ratio {ratio:.2f}x (N_big vs N_small) exceeds "
        f"the {ceiling}x CI ceiling — the batch arm may have regressed to "
        f"super-linear. per_bar_small={per_bar_small * 1e9:.1f}ns, "
        f"per_bar_big={per_bar_big * 1e9:.1f}ns."
    )


@pytest.mark.slow
@pytest.mark.bench
def test_ab_on_vs_off_wall_clock_measured(monkeypatch: pytest.MonkeyPatch) -> None:
    """MEASURE (never gate) the ON-vs-OFF wall clock for the PR-body table.

    Golden Rule #10: the printed figures are the MEASURED evidence for any
    per-build speedup claim; no floor is asserted here (shared-CI noise +
    the flag ships default-OFF — the real-tier calibration is the flip DEC's
    burden). The one hard assertion is CORRECTNESS: both arms produced the
    identical matrix.
    """
    bars = _make_bars(_N_BIG)
    reset_self_check_for_tests(remaining=0)
    reset_observability_memo_for_tests()
    try:
        # Wave flip: unset resolves default-ON, so a bare delenv would time the
        # RUST walk here too — a vacuous Rust-vs-Rust A/B (~1.0x) AND a
        # Rust-vs-Rust equality. "0" is the kill switch: the genuine Python
        # walk the "OFF (Python walk)" figure below claims to measure.
        monkeypatch.setenv(STAGE1A_RUST_ENV, "0")
        build_bar_bits(bars)  # warm
        t0 = time.perf_counter()
        off = build_bar_bits(bars)
        t_off = time.perf_counter() - t0

        monkeypatch.setenv(STAGE1A_RUST_ENV, "1")
        build_bar_bits(bars)  # warm
        t0 = time.perf_counter()
        on = build_bar_bits(bars)
        t_on = time.perf_counter() - t0
    finally:
        reset_self_check_for_tests()
        reset_observability_memo_for_tests()
    assert on == off
    print(
        f"\nBatch-Rust Stage-1A A/B (MEASURED, {_N_BIG:,} bars, single run each):\n"
        f"  OFF (Python walk): {t_off * 1e3:.1f} ms  "
        f"({t_off / _N_BIG * 1e6:.2f} us/bar)\n"
        f"  ON  (batch Rust) : {t_on * 1e3:.1f} ms  "
        f"({t_on / _N_BIG * 1e6:.2f} us/bar)\n"
        f"  per-build ratio  : {t_off / max(t_on, 1e-12):.2f}x "
        f"(MEASURED on this runner — not a gated claim)\n"
    )
