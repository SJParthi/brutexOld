"""O(1)-per-bar bench for the movers screener (DEC-MOVERS-VOLUME-001).

:func:`brutex.screeners.volume_movers.compute_mover_metrics` does a fixed amount
of exact :class:`~decimal.Decimal` work **per bar** (one range division for
volatility, two volume sums for the surge split). So the *per-bar* cost at a
1,000,000-bar window must match the per-bar cost at a 1,000-bar window: O(1) per
bar.

Honest envelope (Golden Rule #10): this gates the PER-BAR cost only. Computing a
whole window is inherently O(n) over its bars and ranking a universe is
``O(m log m)`` over ``m`` instruments — neither is O(1)-total, which would be
physically impossible and is never claimed. The ceiling routes through the shared
:func:`tests.bench._o1_tiers.ratio_ceiling` (5× on shared CI, strict 1.1× under
``BRUTEX_M4PRO_STRICT=1``).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from brutex.data.models import CandleBar
from brutex.screeners.volume_movers import ScreenTarget, compute_mover_metrics

from tests.bench._o1_tiers import ratio_ceiling

pytestmark = pytest.mark.bench

_TARGET = ScreenTarget("NSE", "INDEX", "NSE-NIFTY", "1minute")

#: One reusable bar (a non-trivial range + volume so the per-bar Decimal work is
#: real). Reused by reference so a 1,000,000-bar window is memory-cheap.
_SAMPLE = CandleBar(
    timestamp=datetime(2025, 1, 2, 3, 45, tzinfo=UTC),
    open=Decimal("100"),
    high=Decimal("101.5"),
    low=Decimal("98.5"),
    close=Decimal("100"),
    volume=7,
    open_interest=None,
)


def _bars(count: int) -> list[CandleBar]:
    return [_SAMPLE] * count


def _per_bar_ns(count: int, *, iterations: int) -> float:
    bars = _bars(count)
    compute_mover_metrics(_TARGET, bars, recent_bars=10)  # warm-up
    start = time.perf_counter_ns()
    for _ in range(iterations):
        compute_mover_metrics(_TARGET, bars, recent_bars=10)
    return (time.perf_counter_ns() - start) / iterations / count


def test_compute_bench_small(benchmark: object) -> None:
    bars = _bars(1_000)
    result = benchmark(lambda: compute_mover_metrics(_TARGET, bars, recent_bars=10))  # type: ignore[operator]
    assert result.bar_count == 1_000


def test_compute_is_o1_per_bar() -> None:
    """Per-bar cost at N=1M vs N=1K must stay O(1) (< ceiling)."""
    small = _per_bar_ns(1_000, iterations=50)
    large = _per_bar_ns(1_000_000, iterations=3)
    ratio = large / max(small, 1e-3)
    assert ratio < ratio_ceiling(5.0), (
        f"per-bar ratio {ratio:.2f}× exceeds ceiling — compute must be O(1) per bar "
        f"(per_bar_1k={small:.2f}ns, per_bar_1M={large:.2f}ns)"
    )
