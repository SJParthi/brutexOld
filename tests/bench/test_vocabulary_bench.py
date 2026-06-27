"""O(1) bench for the 52-condition vocabulary hot paths (CLAUDE.md §2 r5 / §12).

The brute-force engine evaluates the 52-condition mask, prunes conflict masks,
and decodes winning combos billions of times across a sweep, so each of these
operations MUST be O(1) in the size of the work already done — independent of how
many bars/masks preceded it. This bench proves that for the five public hot
paths:

* :func:`brutex.vocabulary.evaluator.evaluate_atoms` — one cache-resident Rust
  call per bar; latency must not grow with the number of bars already evaluated.
* :func:`brutex.vocabulary.conflicts.has_conflict` /
  :func:`~brutex.vocabulary.conflicts.count_conflicts` — a bounded scan of the
  fixed 26-pair set; independent of how many masks were checked before.
* :func:`brutex.vocabulary.describe.describe_mask` /
  :func:`~brutex.vocabulary.describe.matched_condition_names` — a bounded scan of
  the 52 condition bits; independent of how many masks were decoded before.

Honest envelope (Golden Rule #10): the "N" varied here is the number of PRIOR
identical calls in a tight loop (a fixed-size single-call working set repeated N
times), NOT the input size — every one of these ops is O(1) in its input BY
CONSTRUCTION (a single bar, a 52-bit mask). What this gate catches is an
accidental hidden accumulation (a growing cache/list/dict on a hot path) that
would make the per-call latency drift upward as the loop runs. The ratio is
routed through the shared :func:`tests.bench._o1_tiers.ratio_ceiling`, so the
strict 1.1× decree applies on the quiet M4-Pro runner and a robust default holds
on the shared GitHub runners (zero added flakiness).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from brutex.indicators.fib_levels import compute_fib_levels
from brutex.indicators.pivots import compute_pivot_levels
from brutex.vocabulary import (
    IndicatorSnapshot,
    PriorBarHistory,
    ProximityParams,
    describe_mask,
    evaluate_atoms,
    matched_condition_names,
)
from brutex.vocabulary.conflicts import count_conflicts, has_conflict

from tests.bench._o1_tiers import ratio_ceiling

pytestmark = pytest.mark.bench

_IST = ZoneInfo("Asia/Kolkata")
_PROX = ProximityParams(pivot_proximity_bps=10, pdh_pdl_proximity_bps=10, fib_proximity_bps=10)


def _snapshot() -> IndicatorSnapshot:
    """A representative non-degenerate bar that lights several condition bits."""
    ts = datetime(2026, 5, 13, 10, 30, tzinfo=_IST)
    day = ts.date()
    return IndicatorSnapshot(
        timestamp_ist=ts,
        open_paisa=24_000_00,
        high_paisa=24_010_00,
        low_paisa=23_990_00,
        close_paisa=24_005_00,
        ema20_paisa=24_000_00,
        ema200_paisa=23_900_00,
        pivots=compute_pivot_levels(day, 24_050_00, 23_950_00, 24_000_00),
        fib=compute_fib_levels(day, 24_050_00, 23_950_00),
        day_high_paisa=24_010_00,
        day_low_paisa=23_980_00,
        open_of_day_paisa=24_000_00,
    )


def _representative_mask() -> int:
    """A mask with bits across several families (not 0, not the full mask)."""
    return evaluate_atoms(_snapshot(), _PROX, PriorBarHistory(prior_n=3, flags=[True, False, True]))


def _ratio_under_ceiling(call: Callable[[], object], *, small: int, big: int, warmup: int) -> float:
    """Return per-call latency ratio big/small for a zero-argument ``call``."""
    fn = call  # local bind avoids repeated attribute lookups in the loop

    def _time(iterations: int) -> float:
        for _ in range(warmup):
            fn()
        t0 = time.perf_counter_ns()
        for _ in range(iterations):
            fn()
        t1 = time.perf_counter_ns()
        return (t1 - t0) / iterations

    t_small = _time(small)
    t_big = _time(big)
    return t_big / max(t_small, 1.0)


class TestEvaluateAtomsIsO1:
    """``evaluate_atoms`` latency does not drift with the number of prior calls."""

    def test_evaluate_atoms_single_call(self, benchmark: Any) -> None:
        snap = _snapshot()
        prior = PriorBarHistory(prior_n=3, flags=[True, False, True])
        result = benchmark(lambda: evaluate_atoms(snap, _PROX, prior))
        assert isinstance(result, int)

    def test_evaluate_atoms_ratio_below_ceiling(self) -> None:
        snap = _snapshot()
        prior = PriorBarHistory(prior_n=3, flags=[True, False, True])
        ratio = _ratio_under_ceiling(
            lambda: evaluate_atoms(snap, _PROX, prior),
            small=2_000,
            big=20_000,
            warmup=500,
        )
        assert ratio < ratio_ceiling(8.0), (
            f"evaluate_atoms per-call latency ratio {ratio:.2f}× (20k vs 2k calls) "
            f"exceeds ceiling — a hidden accumulation broke O(1) per bar."
        )


class TestConflictChecksAreO1:
    """``has_conflict`` / ``count_conflicts`` scan the fixed 26-pair set, O(1)."""

    def test_has_conflict_ratio_below_ceiling(self) -> None:
        mask = _representative_mask()
        ratio = _ratio_under_ceiling(
            lambda: has_conflict(mask), small=20_000, big=200_000, warmup=2_000
        )
        assert ratio < ratio_ceiling(5.0), (
            f"has_conflict latency ratio {ratio:.2f}× exceeds ceiling — the "
            f"26-pair scan must be O(1) per mask."
        )

    def test_count_conflicts_ratio_below_ceiling(self) -> None:
        mask = _representative_mask()
        ratio = _ratio_under_ceiling(
            lambda: count_conflicts(mask), small=20_000, big=200_000, warmup=2_000
        )
        assert ratio < ratio_ceiling(5.0), (
            f"count_conflicts latency ratio {ratio:.2f}× exceeds ceiling — the "
            f"26-pair scan must be O(1) per mask."
        )


class TestDescribeIsO1:
    """``describe_mask`` / ``matched_condition_names`` scan the 52 bits, O(1)."""

    def test_describe_mask_ratio_below_ceiling(self) -> None:
        mask = _representative_mask()
        ratio = _ratio_under_ceiling(
            lambda: describe_mask(mask), small=20_000, big=200_000, warmup=2_000
        )
        assert ratio < ratio_ceiling(5.0), (
            f"describe_mask latency ratio {ratio:.2f}× exceeds ceiling — the "
            f"52-bit decode must be O(1) per mask."
        )

    def test_matched_condition_names_ratio_below_ceiling(self) -> None:
        mask = _representative_mask()
        ratio = _ratio_under_ceiling(
            lambda: matched_condition_names(mask), small=20_000, big=200_000, warmup=2_000
        )
        assert ratio < ratio_ceiling(5.0), (
            f"matched_condition_names latency ratio {ratio:.2f}× exceeds ceiling "
            f"— the 52-bit decode must be O(1) per mask."
        )
