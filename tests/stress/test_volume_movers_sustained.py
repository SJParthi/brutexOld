"""Sustained-load stress test for the movers screener (DEC-MOVERS-VOLUME-001).

Screens a LARGE universe (thousands of instruments, some empty) to prove the
ranker stays correct + deterministic at scale: no crash on a wide varied pool,
the rankings are descending subsets of the metrics, empties are skipped (never
fabricated), and the whole report is byte-stable across repeated runs
(Golden Rule #4 / #7).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import pytest
from brutex.data.models import CandleBar
from brutex.screeners.volume_movers import (
    MoversReport,
    ScreenTarget,
    rank_movers,
)

from tests.fakes.movers_fakes import make_candle

pytestmark = pytest.mark.stress

_POOL_SIZE = 5_000
_TOP_N = 25
_RECENT_BARS = 1
_START = datetime(2025, 1, 2, tzinfo=UTC)
_END = datetime(2025, 1, 3, tzinfo=UTC)


def _series() -> dict[ScreenTarget, Sequence[CandleBar]]:
    """A deterministic, varied universe — every 50th instrument is empty."""
    series: dict[ScreenTarget, Sequence[CandleBar]] = {}
    for index in range(_POOL_SIZE):
        target = ScreenTarget("NSE", "INDEX", f"SYM{index:05d}", "1minute")
        if index % 50 == 0:
            series[target] = []  # empty → skipped
            continue
        base_volume = (index % 40) + 1
        recent_volume = base_volume * ((index % 6) + 1)
        half_range = (index % 12) + 1
        series[target] = [
            make_candle(high="101", low="99", close="100", volume=base_volume, minute=0),
            make_candle(
                high=str(100 + half_range),
                low=str(100 - half_range),
                close="100",
                volume=recent_volume,
                minute=1,
            ),
        ]
    return series


def _assert_descending(ranked: Sequence[object], attr: str) -> None:
    values = [getattr(metric, attr) for metric in ranked]
    assert all(value is not None for value in values)
    assert values == sorted(values, reverse=True)


def test_large_universe_screen_is_correct_and_deterministic() -> None:
    report = rank_movers(
        _series(),
        window_start=_START,
        window_end=_END,
        recent_bars=_RECENT_BARS,
        top_n=_TOP_N,
    )
    assert isinstance(report, MoversReport)

    skipped_count = len([i for i in range(_POOL_SIZE) if i % 50 == 0])
    assert len(report.skipped) == skipped_count
    assert len(report.metrics) == _POOL_SIZE - skipped_count

    # Both rankings are descending subsets of the metrics, capped at top_n.
    metric_set = set(report.metrics)
    assert set(report.top_volume_surge) <= metric_set
    assert set(report.top_realised_volatility) <= metric_set
    assert len(report.top_volume_surge) == _TOP_N
    assert len(report.top_realised_volatility) == _TOP_N
    _assert_descending(report.top_volume_surge, "volume_surge_ratio")
    _assert_descending(report.top_realised_volatility, "realised_range_volatility")

    # Byte-stable across a re-run on freshly built data.
    rerun = rank_movers(
        _series(),
        window_start=_START,
        window_end=_END,
        recent_bars=_RECENT_BARS,
        top_n=_TOP_N,
    )
    assert rerun == report
