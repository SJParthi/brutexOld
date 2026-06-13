"""Property tests for the movers screener (DEC-MOVERS-VOLUME-001).

Hypothesis invariants for the pure metric computation + the rankings: volatility
is a non-negative range mean over the positive-close bars, an equal-volume window
has unit surge, and a ranking is always a descending, deduplicated subset whose
ties break stably by target — reproducibly across re-runs (Golden Rule #4 / #7).
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from brutex.screeners.volume_movers import (
    MoverMetrics,
    ScreenTarget,
    compute_mover_metrics,
    rank_by_volume_surge,
)
from hypothesis import given, settings
from hypothesis import strategies as st

from tests.fakes.movers_fakes import make_candle

pytestmark = pytest.mark.property

_TARGET = ScreenTarget("NSE", "INDEX", "NSE-NIFTY", "1minute")
#: A pool of distinct targets for ranking properties (order-significant tiebreak).
_TARGETS = tuple(ScreenTarget("NSE", "INDEX", f"SYM{i:02d}", "1minute") for i in range(12))


@st.composite
def _candles(draw: st.DrawFn, *, min_size: int, max_size: int) -> list[object]:
    """A list of valid candles with integer rupee prices and ``close > 0``."""
    size = draw(st.integers(min_value=min_size, max_value=max_size))
    bars: list[object] = []
    for index in range(size):
        low = draw(st.integers(min_value=1, max_value=1_000))
        span = draw(st.integers(min_value=0, max_value=500))
        high = low + span
        close = draw(st.integers(min_value=low, max_value=high))
        volume = draw(st.integers(min_value=0, max_value=10_000))
        bars.append(make_candle(high=high, low=low, close=close, volume=volume, minute=index))
    return bars


@settings(derandomize=True, max_examples=200)
@given(bars=_candles(min_size=1, max_size=8), recent=st.integers(min_value=1, max_value=8))
def test_volatility_nonnegative_over_all_positive_close_bars(
    bars: list[object], recent: int
) -> None:
    metric = compute_mover_metrics(_TARGET, bars, recent_bars=recent)  # type: ignore[arg-type]
    assert metric.volatility_bar_count == len(bars)
    assert metric.realised_range_volatility is not None
    assert metric.realised_range_volatility >= Decimal(0)


@settings(derandomize=True, max_examples=200)
@given(bars=_candles(min_size=1, max_size=8), recent=st.integers(min_value=1, max_value=8))
def test_surge_when_defined_is_nonnegative(bars: list[object], recent: int) -> None:
    metric = compute_mover_metrics(_TARGET, bars, recent_bars=recent)  # type: ignore[arg-type]
    if metric.volume_surge_ratio is not None:
        assert metric.volume_surge_ratio >= Decimal(0)
        # Defined surge requires a baseline window AND a positive baseline volume.
        assert len(bars) > recent
        assert metric.baseline_avg_volume is not None
        assert metric.baseline_avg_volume > Decimal(0)


@settings(derandomize=True, max_examples=200)
@given(
    size=st.integers(min_value=2, max_value=8),
    recent=st.integers(min_value=1, max_value=7),
    volume=st.integers(min_value=1, max_value=10_000),
)
def test_equal_volume_window_has_unit_surge(size: int, recent: int, volume: int) -> None:
    # A flat-volume window: recent mean == baseline mean ⇒ surge is exactly 1.
    recent = min(recent, size - 1)
    bars = [
        make_candle(high="101", low="99", close="100", volume=volume, minute=i) for i in range(size)
    ]
    metric = compute_mover_metrics(_TARGET, bars, recent_bars=recent)
    assert metric.volume_surge_ratio == Decimal(1)


@settings(derandomize=True, max_examples=200)
@given(size=st.integers(min_value=1, max_value=8))
def test_flat_bars_have_zero_volatility(size: int) -> None:
    bars = [make_candle(high="50", low="50", close="50", volume=3, minute=i) for i in range(size)]
    metric = compute_mover_metrics(_TARGET, bars, recent_bars=1)
    assert metric.realised_range_volatility == Decimal(0)


def _surge_metric(target: ScreenTarget, surge: Decimal | None) -> MoverMetrics:
    """A :class:`MoverMetrics` carrying just the surge under test."""
    return MoverMetrics(
        target=target,
        bar_count=5,
        recent_avg_volume=None if surge is None else Decimal(10),
        baseline_avg_volume=None if surge is None else Decimal(10),
        volume_surge_ratio=surge,
        volatility_bar_count=5,
        realised_range_volatility=Decimal("0.1"),
        last_close_paisa=10_000,
    )


@st.composite
def _surge_metrics(draw: st.DrawFn) -> list[MoverMetrics]:
    """A list of distinct-target metrics with ties + undefined surges allowed."""
    count = draw(st.integers(min_value=0, max_value=len(_TARGETS)))
    surge_strategy = st.one_of(st.none(), st.integers(min_value=0, max_value=8).map(Decimal))
    return [_surge_metric(_TARGETS[i], draw(surge_strategy)) for i in range(count)]


@settings(derandomize=True, max_examples=300)
@given(metrics=_surge_metrics(), top_n=st.integers(min_value=1, max_value=20))
def test_ranking_is_descending_deduped_subset(metrics: list[MoverMetrics], top_n: int) -> None:
    ranked = rank_by_volume_surge(metrics, top_n=top_n)
    defined = [m for m in metrics if m.volume_surge_ratio is not None]
    # Length is min(top_n, eligible); every survivor has a defined surge.
    assert len(ranked) == min(top_n, len(defined))
    assert all(m.volume_surge_ratio is not None for m in ranked)
    # A subset of the input (same identities, no fabrication).
    assert set(ranked) <= set(metrics)
    # Descending by surge, with the target key as the ascending stable tiebreak.
    keys = [
        (-m.volume_surge_ratio, m.target.symbol)  # type: ignore[operator]
        for m in ranked
    ]
    assert keys == sorted(keys)
    # Reproducible across a re-run.
    assert rank_by_volume_surge(metrics, top_n=top_n) == ranked
