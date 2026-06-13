"""O(1)-per-instrument bench for the gainers/losers screener (DEC-MOVERS-GAINERS-001).

The per-instrument reduction :func:`brutex.screeners.gainers_losers._build_mover`
is **endpoint-only**: it reads the first bar's open, the last bar's close, and
the bar count — it does NOT scan the window. So reducing a 100,000-bar window
must cost the same as reducing a 1,000-bar window: O(1) in the number of bars.

Honest envelope (Golden Rule #10): this gates the PER-INSTRUMENT reduction
cost only. A whole screen is inherently ``O(M·log M)`` over ``M`` instruments
(the rank sort) plus one ``O(window)`` lake read each — NOT O(1) total, and the
module never claims otherwise. The lake read itself is bounded by the
overlapping partitions, never the whole lake.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from brutex.data.models import CandleBar
from brutex.screeners.gainers_losers import InstrumentRef, _build_mover

from tests.bench._o1_tiers import ratio_ceiling

_SMALL = 1_000
_LARGE = 100_000

_T0 = datetime(2024, 1, 2, 4, 0, 0, tzinfo=UTC)
_REF = InstrumentRef(symbol="NIFTY", exchange="NSE", segment="INDEX")


def _window(n: int) -> list[CandleBar]:
    """A monotone ``n``-bar window (each bar valid, open == prior close)."""
    bars: list[CandleBar] = []
    for i in range(n):
        price = Decimal(100 + i)
        bars.append(
            CandleBar(
                timestamp=_T0 + timedelta(minutes=i),
                open=price,
                high=price + 1,
                low=price,
                close=price + 1,
                volume=0,
                open_interest=None,
            )
        )
    return bars


def test_build_mover_small(benchmark: object) -> None:
    bars = _window(_SMALL)
    result = benchmark(lambda: _build_mover(_REF, bars))  # type: ignore[operator]
    assert result is not None


def test_build_mover_large(benchmark: object) -> None:
    bars = _window(_LARGE)
    result = benchmark(lambda: _build_mover(_REF, bars))  # type: ignore[operator]
    assert result is not None


def test_build_mover_ratio_is_o1() -> None:
    """``_build_mover`` at 100k vs 1k bars must stay O(1) (< ceiling)."""

    def _time(n: int, *, iterations: int = 20_000) -> float:
        bars = _window(n)
        for _ in range(2_000):  # warm-up
            _build_mover(_REF, bars)
        start = time.perf_counter_ns()
        for _ in range(iterations):
            _build_mover(_REF, bars)
        return (time.perf_counter_ns() - start) / iterations

    t_small = _time(_SMALL)
    t_large = _time(_LARGE)
    ratio = t_large / max(t_small, 1.0)
    assert ratio < ratio_ceiling(5.0), (
        f"_build_mover ratio {ratio:.2f}× (100k vs 1k bars) exceeds ceiling — the "
        f"reduction must be endpoint-only, not a window scan. "
        f"t_1k={t_small:.0f}ns, t_100k={t_large:.0f}ns."
    )
