"""Bench — the R45 diff-check cost is bounded by K, flat vs mask count.

Pins the D-11 budget law from two sides:

* **Exact work bound** — the number of production/reference simulations a
  ``run_for_tuple`` performs equals ``min(budget, n_masks)`` EXACTLY, at a
  1× and a ~17× mask count (a counting stub — deterministic, unflakable).
* **Timing** — ``run_for_tuple`` wall time at fixed K stays under the tier
  ceiling across a ~17× mask-count growth, and the pure sampler
  (:func:`sample_masks`) is flat 1k → 100k masks.

HONEST envelope (Golden Rule #10): the hook's canonical enumeration
(``iter_masks_k_of_bits`` + conflict prune) is an O(n_masks) integer walk
— deliberately accepted (it is strictly dominated by the tuple's own
production sweep, which SIMULATES all n_masks). The timing pin therefore
proves the K-bounded SIMULATION cost dominates at realistic shapes, and a
separate never-superlinear guard bounds the enumeration term.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final

import pytest
from brutex.data.models import CandleBar
from brutex.engine.sweep_diffcheck import (
    DiffCheckHook,
    DiffCheckMismatch,
    RefTrade,
    reference_drawdown,
    reference_simulate,
    sample_masks,
)

from tests.bench._abs_baseline_registry import measure_best_of_min
from tests.bench._o1_tiers import ratio_ceiling

pytestmark = pytest.mark.bench

#: D-6 traceability — test name → requirement/gap tokens.
TRACEABILITY: dict[str, tuple[str, ...]] = {
    "test_r45_sim_work_is_exactly_min_budget_n_masks": ("R45",),
    "test_r45_sample_masks_flat_vs_n_masks": ("R45",),
    "test_r45_run_for_tuple_bounded_by_k_across_mask_growth": ("R45",),
}

_OPEN_UTC: Final[datetime] = datetime(2024, 1, 2, 3, 45, tzinfo=UTC)
_K: Final[int] = 8

#: Condition-bit pools with MEASURED post-conflict-prune mask counts (the
#: exact counts are asserted below, so any vocabulary drift fails loudly).
_BITS_SMALL: Final[tuple[int, ...]] = (20, 21, 22, 23)  # C(4,2) = 6 masks, 0 conflicts
_BITS_LARGE: Final[tuple[int, ...]] = tuple(range(20, 35))  # C(15,2) = 105 − 3 conflicts = 102


def _bars(n: int = 30) -> list[CandleBar]:
    out: list[CandleBar] = []
    for minute in range(n):
        price = Decimal(10_000 + (minute % 7)) / Decimal(100)
        out.append(
            CandleBar(
                timestamp=_OPEN_UTC + timedelta(minutes=minute),
                open=price,
                high=price + Decimal("0.10"),
                low=price - Decimal("0.10"),
                close=price,
                volume=0,
                open_interest=None,
            )
        )
    return out


@dataclass(frozen=True)
class _TF:
    minutes: int = 1


@dataclass(frozen=True)
class _Dir:
    value: str = "BUY"


@dataclass(frozen=True)
class _Tuple:
    k: int = 2
    sl_paisa: int | None = None
    tp_paisa: int | None = None
    tf: _TF = field(default_factory=_TF)
    direction: _Dir = field(default_factory=_Dir)


@dataclass(frozen=True)
class _Grid:
    condition_bits: tuple[int, ...]
    data_snapshot_hash: str = "snap"
    start_equity_paisa: int = 10_000_000


class _CountingSim:
    """Matching production stub that counts its invocations."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(
        self,
        bars: Sequence[CandleBar],
        entry_signals: Sequence[bool],
        *,
        direction: str,
        tf_minutes: int,
        exec_bars: Sequence[CandleBar],
        stop_loss_paisa: int | None,
        take_profit_paisa: int | None,
    ) -> list[RefTrade]:
        self.calls += 1
        return reference_simulate(
            bars,
            entry_signals,
            direction=direction,
            tf_minutes=tf_minutes,
            exec_bars=exec_bars,
            sl_paisa=stop_loss_paisa,
            tp_paisa=take_profit_paisa,
        )


def _drawdown(
    bars: Sequence[CandleBar],
    trades: Sequence[object],
    *,
    start_equity_paisa: int,
    tf_minutes: int,
) -> tuple[int, float, int, float]:
    ref = [t for t in trades if isinstance(t, RefTrade)]
    return reference_drawdown(ref, bars=bars, start_equity_paisa=start_equity_paisa)


def _sink(mismatch: DiffCheckMismatch) -> None:  # pragma: no cover - matching stubs
    raise AssertionError(f"unexpected mismatch: {mismatch}")


def _hook(sim: _CountingSim) -> DiffCheckHook:
    return DiffCheckHook(
        sweep_id="bench-sweep",
        tuple_digest_of=lambda tuple_, grid: "bench-digest",
        production_simulate=sim,
        production_drawdown=_drawdown,
        on_mismatch=_sink,
    )


class TestDiffcheckBoundedByK:
    def test_r45_sim_work_is_exactly_min_budget_n_masks(self) -> None:
        """Sim invocations == min(budget, n_masks) at BOTH mask counts (exact)."""
        bars = _bars()
        bar_bits = [(1 << 60) - 1 if m % 2 == 0 else 0 for m in range(len(bars))]
        for bits, n_masks in ((_BITS_SMALL, 6), (_BITS_LARGE, 102)):
            sim = _CountingSim()
            report = _hook(sim).run_for_tuple(
                bars=bars,
                bar_bits=bar_bits,
                exec_bars=bars,
                tuple_=_Tuple(),
                grid=_Grid(condition_bits=bits),
                budget_left=_K,
            )
            expected = min(_K, n_masks)
            assert report.checked == expected
            assert sim.calls == expected  # the K bound, not one sim per mask
            assert report.mismatches == 0
        # A budget larger than the pool checks the WHOLE pool, never more.
        sim = _CountingSim()
        report = _hook(sim).run_for_tuple(
            bars=bars,
            bar_bits=bar_bits,
            exec_bars=bars,
            tuple_=_Tuple(),
            grid=_Grid(condition_bits=_BITS_SMALL),
            budget_left=10_000,
        )
        assert report.checked == 6
        assert sim.calls == 6

    def test_r45_sample_masks_flat_vs_n_masks(self) -> None:
        """The pure sampler is O(K): flat 1k → 100k masks (tier ceiling)."""
        small_masks = tuple(range(1_000))
        large_masks = tuple(range(100_000))

        def timed(masks: tuple[int, ...]) -> float:
            def op() -> None:
                for _ in range(50):
                    picked = sample_masks(
                        sweep_id="s",
                        tuple_digest="d",
                        data_snapshot_hash="h",
                        masks=masks,
                        k=200,
                    )
                    assert len(picked) == 200  # anti-vacuous

            best_ns, _samples, _spread = measure_best_of_min(
                op, ops_per_round=50, rounds=7, warmup=2
            )
            return best_ns

        small_ns = timed(small_masks)
        large_ns = timed(large_masks)
        ratio = large_ns / max(small_ns, 1.0)
        ceiling = ratio_ceiling(5.0)
        assert ratio < ceiling, (
            f"sample_masks degraded with mask count: {small_ns:.0f} ns @ 1k vs "
            f"{large_ns:.0f} ns @ 100k (ratio {ratio:.2f} >= ceiling {ceiling}) — "
            "suspected O(n_masks) materialisation inside the sampler"
        )

    def test_r45_run_for_tuple_bounded_by_k_across_mask_growth(self) -> None:
        """Fixed K=8: wall time under the tier ceiling across ~17× more masks.

        The enumeration term is HONESTLY O(n_masks); this pin proves the
        K-bounded simulation cost dominates at realistic shapes (6 vs 102
        masks over 30 bars), i.e. the diff-check never re-simulates the
        whole grid. A gross regression (per-mask simulation — 6 vs 102
        sims ≈ 17× — or a superlinear enumeration) blows the ceiling.
        """
        bars = _bars()
        bar_bits = [(1 << 60) - 1 if m % 2 == 0 else 0 for m in range(len(bars))]

        def timed(bits: tuple[int, ...], expected_checked: int) -> float:
            sim = _CountingSim()
            hook = _hook(sim)

            def op() -> None:
                report = hook.run_for_tuple(
                    bars=bars,
                    bar_bits=bar_bits,
                    exec_bars=bars,
                    tuple_=_Tuple(),
                    grid=_Grid(condition_bits=bits),
                    budget_left=_K,
                )
                assert report.checked == expected_checked  # anti-vacuous

            best_ns, _samples, _spread = measure_best_of_min(
                op, ops_per_round=1, rounds=9, warmup=2
            )
            return best_ns

        small_ns = timed(_BITS_SMALL, min(_K, 6))
        large_ns = timed(_BITS_LARGE, min(_K, 102))
        ratio = large_ns / max(small_ns, 1.0)
        ceiling = ratio_ceiling(5.0)
        assert ratio < ceiling, (
            f"run_for_tuple degraded with mask count at fixed K: {small_ns:.0f} ns @ "
            f"6 masks vs {large_ns:.0f} ns @ 105 masks (ratio {ratio:.2f} >= ceiling "
            f"{ceiling}) — suspected per-mask simulation instead of the K bound"
        )
