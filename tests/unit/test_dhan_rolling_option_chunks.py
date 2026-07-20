"""Tests for the Dhan rolling-ATM option chunk planner (30-day tiling).

DEC-DHAN-ROLLING-OPTIONS-001.
"""

from __future__ import annotations

import itertools
from datetime import datetime

import pytest
from brutex.adapters.dhan.rolling_option_chunks import (
    plan_rolling_option_windows,
    window_days_for_rolling_interval,
)


def test_window_days_is_30_for_1minute() -> None:
    assert window_days_for_rolling_interval("1minute") == 30


def test_window_days_unknown_interval_fails_loud() -> None:
    with pytest.raises(ValueError, match="unsupported Dhan rolling-option interval"):
        window_days_for_rolling_interval("5minute")


def test_tiling_is_30_day_contiguous_no_gap_no_overlap() -> None:
    start = datetime(2024, 1, 1)
    end = datetime(2024, 4, 1)  # 91 days → 4 windows (30+30+30+1)
    windows = plan_rolling_option_windows(start, end, interval="1minute")
    assert len(windows) == 4
    # Contiguous: each window's end == the next window's start (no gap, no overlap).
    for prev, nxt in itertools.pairwise(windows):
        assert prev.end == nxt.start
    # Bounds honoured.
    assert windows[0].start[:10] == "2024-01-01"
    assert windows[-1].end[:10] == "2024-04-01"
    # Each span is <= 30 days.
    for window in windows:
        span = datetime.fromisoformat(window.end) - datetime.fromisoformat(window.start)
        assert span.days <= 30


def test_empty_range_yields_no_windows() -> None:
    d = datetime(2024, 1, 1)
    assert plan_rolling_option_windows(d, d, interval="1minute") == []
