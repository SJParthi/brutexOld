"""Unit tests for ``brutex.adapters.groww.chunks`` (DEC-GROWW-DEPS-REINSTATE-001)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from brutex.adapters.groww import chunks


@pytest.mark.parametrize(
    ("interval", "expected_days"),
    [
        ("1minute", 30),
        ("5minute", 30),
        ("10minute", 90),
        ("30minute", 90),
        ("1hour", 180),
        ("1day", 180),
        ("1month", 180),
    ],
)
def test_window_days_for_interval(interval: str, expected_days: int) -> None:
    assert chunks.window_days_for_interval(interval) == expected_days


def test_window_days_for_unknown_interval_raises() -> None:
    with pytest.raises(ValueError, match="unknown Groww backtesting interval"):
        chunks.window_days_for_interval("7minute")


def test_plan_candle_windows_1minute_tiles_at_30_days() -> None:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = datetime(2024, 3, 16, tzinfo=UTC)  # 75 days
    windows = chunks.plan_candle_windows(start, end, interval="1minute")
    assert len(windows) == 3  # 30 + 30 + 15
    # contiguous + non-overlapping
    assert windows[0].start == start.isoformat()
    assert windows[0].end == windows[1].start
    assert windows[1].end == windows[2].start
    assert windows[-1].end == end.isoformat()  # final window clipped to end


def test_plan_candle_windows_1day_tiles_at_180_days() -> None:
    start = datetime(2021, 1, 1, tzinfo=UTC)
    end = datetime(2022, 1, 1, tzinfo=UTC)  # 365 days
    windows = chunks.plan_candle_windows(start, end, interval="1day")
    assert len(windows) == 3  # 180 + 180 + 5


def test_plan_candle_windows_empty_when_start_not_before_end() -> None:
    moment = datetime(2024, 1, 1, tzinfo=UTC)
    assert chunks.plan_candle_windows(moment, moment, interval="1minute") == []
