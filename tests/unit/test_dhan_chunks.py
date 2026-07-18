"""Unit tests for :mod:`brutex.adapters.dhan.chunks` — 100% line+branch."""

from __future__ import annotations

from datetime import datetime

import pytest
from brutex.adapters.dhan import chunks


def test_window_days_intraday() -> None:
    assert chunks.window_days_for_interval("1minute") == 90
    assert chunks.window_days_for_interval("60minute") == 90
    assert chunks.window_days_for_interval("1hour") == 90


def test_window_days_daily_unverified() -> None:
    assert chunks.window_days_for_interval("1day") == 365


def test_window_days_unknown_raises() -> None:
    with pytest.raises(ValueError, match="unknown Dhan candle interval"):
        chunks.window_days_for_interval("2minute")


def test_plan_candle_windows_intraday_tiles_at_90d() -> None:
    windows = chunks.plan_candle_windows(
        datetime(2024, 1, 1), datetime(2024, 7, 1), interval="1minute"
    )
    assert len(windows) == 3  # ~182 days / 90
    assert windows[0].start == datetime(2024, 1, 1).isoformat()
    assert windows[-1].end == datetime(2024, 7, 1).isoformat()


def test_plan_candle_windows_daily_tiles_at_365d() -> None:
    windows = chunks.plan_candle_windows(
        datetime(2020, 1, 1), datetime(2024, 1, 1), interval="1day"
    )
    assert len(windows) == 5  # 4 years → ceil to 5 windows


def test_plan_candle_windows_empty_when_start_ge_end() -> None:
    assert (
        chunks.plan_candle_windows(datetime(2024, 1, 1), datetime(2024, 1, 1), interval="1minute")
        == []
    )


def test_plan_rolling_option_windows_tiles_at_30d() -> None:
    windows = chunks.plan_rolling_option_windows(datetime(2024, 1, 1), datetime(2024, 4, 1))
    assert len(windows) == 4  # ~91 days / 30 → 4 windows
