"""Tests for :mod:`brutex.adapters.dhan.chunks` — the flat 90-day tiler."""

from __future__ import annotations

from datetime import datetime

import pytest
from brutex.adapters.dhan.chunks import plan_candle_windows, window_days_for_interval


def test_window_days_for_1minute_is_90() -> None:
    assert window_days_for_interval("1minute") == 90


def test_window_days_for_1day_is_365_defensive_tile() -> None:
    # DEC-DHAN-NATIVE-DAILY-001: the daily endpoint's range cap is
    # UNDOCUMENTED/UNVERIFIED — a DEFENSIVE 365-day tile, never a vendor claim.
    assert window_days_for_interval("1day") == 365


def test_window_days_for_unknown_interval_fails_loud() -> None:
    with pytest.raises(ValueError, match="unsupported Dhan interval"):
        window_days_for_interval("5minute")


def test_plan_tiles_at_90_days() -> None:
    windows = plan_candle_windows(datetime(2022, 6, 1), datetime(2022, 12, 1), interval="1minute")
    # ~183 days → 3 windows (90 + 90 + remainder).
    assert len(windows) == 3
    assert windows[0].start.startswith("2022-06-01")
    assert windows[-1].end.startswith("2022-12-01")


def test_plan_empty_when_start_ge_end() -> None:
    assert plan_candle_windows(datetime(2022, 6, 1), datetime(2022, 6, 1), interval="1minute") == []


def test_plan_single_window_within_cap() -> None:
    windows = plan_candle_windows(datetime(2022, 6, 1), datetime(2022, 6, 20), interval="1minute")
    assert len(windows) == 1


def test_approx_full_pull_request_count() -> None:
    # June-2022 → mid-2026 ≈ 4 years ≈ 17 windows per index; ×3 indices ≈ 51 requests.
    windows = plan_candle_windows(datetime(2022, 6, 1), datetime(2026, 7, 1), interval="1minute")
    assert 15 <= len(windows) <= 18
