"""Tests for `brutex.indicators.day_extremes` — running Day Hi/Lo.

Verifies the running-max/running-min logic matches the operator's
Pine v3 source (provided 2026-05-16):

    if is_first and barstate.isnew:
        day_high := high
        day_low  := low
    else:
        day_high := day_high[1]
        day_low  := day_low[1]

    if high > day_high: day_high := high
    if low  < day_low : day_low  := low
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from brutex.indicators.day_extremes import compute_day_extremes

IST = ZoneInfo("Asia/Kolkata")


def _t(d: int, h: int, m: int) -> datetime:
    """Build a 13-May-2026 IST datetime for brevity in fixtures."""
    return datetime(2026, 5, d, h, m, tzinfo=IST)


class TestSingleDay:
    def test_single_bar(self) -> None:
        bars = [(_t(13, 9, 15), 5400000, 5395000)]
        result = compute_day_extremes(bars)
        assert len(result) == 1
        r = result[0]
        assert r.day_high_paisa == 5400000
        assert r.day_low_paisa == 5395000

    def test_running_max_min_within_day(self) -> None:
        bars = [
            (_t(13, 9, 15), 5400000, 5395000),  # initial: H=5400000, L=5395000
            (_t(13, 9, 16), 5410000, 5398000),  # expand H to 5410000
            (_t(13, 9, 17), 5405000, 5390000),  # expand L to 5390000
            (_t(13, 9, 18), 5408000, 5392000),  # no change to running
        ]
        result = compute_day_extremes(bars)
        assert result[0].day_high_paisa == 5400000
        assert result[0].day_low_paisa == 5395000
        assert result[1].day_high_paisa == 5410000  # new high
        assert result[1].day_low_paisa == 5395000  # unchanged
        assert result[2].day_high_paisa == 5410000  # unchanged
        assert result[2].day_low_paisa == 5390000  # new low
        assert result[3].day_high_paisa == 5410000  # unchanged
        assert result[3].day_low_paisa == 5390000  # unchanged

    def test_running_high_only_increases(self) -> None:
        # Strictly increasing highs: running_high tracks each bar
        bars = [
            (_t(13, 9, 15), 5400000, 5400000),
            (_t(13, 9, 16), 5410000, 5410000),
            (_t(13, 9, 17), 5420000, 5420000),
        ]
        result = compute_day_extremes(bars)
        assert [r.day_high_paisa for r in result] == [5400000, 5410000, 5420000]

    def test_running_low_only_decreases(self) -> None:
        # Strictly decreasing lows: running_low tracks each bar
        bars = [
            (_t(13, 9, 15), 5400000, 5400000),
            (_t(13, 9, 16), 5390000, 5390000),
            (_t(13, 9, 17), 5380000, 5380000),
        ]
        result = compute_day_extremes(bars)
        assert [r.day_low_paisa for r in result] == [5400000, 5390000, 5380000]


class TestDayBoundary:
    def test_new_day_resets(self) -> None:
        bars = [
            # Day 1: H=5410000, L=5390000
            (_t(13, 9, 15), 5400000, 5395000),
            (_t(13, 9, 16), 5410000, 5398000),
            (_t(13, 15, 30), 5405000, 5390000),
            # Day 2 (next trading day): RESET
            (_t(14, 9, 15), 5350000, 5340000),  # new day low
            (_t(14, 9, 16), 5360000, 5345000),
        ]
        result = compute_day_extremes(bars)

        # End of day 1
        assert result[2].day_high_paisa == 5410000
        assert result[2].day_low_paisa == 5390000

        # First bar of day 2: RESET to current bar's H/L
        assert result[3].day_high_paisa == 5350000
        assert result[3].day_low_paisa == 5340000

        # Second bar of day 2: expand high
        assert result[4].day_high_paisa == 5360000
        assert result[4].day_low_paisa == 5340000

    def test_three_consecutive_days(self) -> None:
        bars = [
            (_t(13, 9, 15), 1000, 500),
            (_t(14, 9, 15), 2000, 1500),  # new day
            (_t(15, 9, 15), 3000, 2500),  # new day
        ]
        result = compute_day_extremes(bars)
        # Each day starts fresh
        assert result[0].day_high_paisa == 1000
        assert result[1].day_high_paisa == 2000
        assert result[2].day_high_paisa == 3000


class TestEdgeCases:
    def test_empty_input(self) -> None:
        assert compute_day_extremes([]) == []

    def test_doji_bar_high_equals_low(self) -> None:
        # Single bar where high == low (e.g., circuit / halt)
        bars = [(_t(13, 9, 15), 5400000, 5400000)]
        result = compute_day_extremes(bars)
        assert result[0].day_high_paisa == 5400000
        assert result[0].day_low_paisa == 5400000


class TestNoLookAhead:
    """Property P-06: day_high/day_low at bar N must only depend on bars 0..N."""

    def test_truncating_input_gives_same_prefix(self) -> None:
        bars = [
            (_t(13, 9, 15), 5400000, 5395000),
            (_t(13, 9, 16), 5410000, 5398000),
            (_t(13, 9, 17), 5405000, 5390000),
            (_t(13, 9, 18), 5408000, 5392000),
        ]
        full = compute_day_extremes(bars)
        # Truncating to first 2 bars should give same result for those 2 bars
        for n in range(1, len(bars) + 1):
            truncated = compute_day_extremes(bars[:n])
            assert len(truncated) == n
            for i in range(n):
                assert truncated[i].day_high_paisa == full[i].day_high_paisa
                assert truncated[i].day_low_paisa == full[i].day_low_paisa
