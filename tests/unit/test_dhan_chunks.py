"""Tests for `brutex.adapters.dhan.chunks` — the Dhan window planner.

Targets 100% line+branch: tiling, clipping, empty range, the positive
window guard, and the two constant-pinned wrappers (90d spot / 30d option).
"""

from __future__ import annotations

from datetime import UTC, datetime
from itertools import pairwise

import pytest
from brutex.adapters.dhan.chunks import (
    DateWindow,
    plan_option_windows,
    plan_spot_intraday_windows,
    plan_windows,
)
from brutex.adapters.dhan.constants import (
    INTRADAY_WINDOW_DAYS,
    ROLLING_OPTION_WINDOW_DAYS,
)


def _dt(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, 9, 15, tzinfo=UTC)


class TestPlanWindows:
    def test_single_window_when_range_under_limit(self) -> None:
        windows = plan_windows(_dt(2021, 1, 1), _dt(2021, 1, 10), window_days=90)
        assert windows == [
            DateWindow(start=_dt(2021, 1, 1).isoformat(), end=_dt(2021, 1, 10).isoformat())
        ]

    def test_exact_multiple_tiles_without_overlap(self) -> None:
        windows = plan_windows(_dt(2021, 1, 1), _dt(2021, 1, 31), window_days=10)
        assert len(windows) == 3
        # Contiguous + non-overlapping: each window's end == next window's start.
        for a, b in pairwise(windows):
            assert a.end == b.start
        assert windows[0].start == _dt(2021, 1, 1).isoformat()
        assert windows[-1].end == _dt(2021, 1, 31).isoformat()

    def test_final_window_clipped_to_end(self) -> None:
        windows = plan_windows(_dt(2021, 1, 1), _dt(2021, 1, 25), window_days=10)
        assert windows[-1].end == _dt(2021, 1, 25).isoformat()
        # Last span is shorter than the limit (clipped).
        assert windows[-1].start == _dt(2021, 1, 21).isoformat()

    def test_empty_when_start_not_before_end(self) -> None:
        assert plan_windows(_dt(2021, 1, 10), _dt(2021, 1, 10), window_days=90) == []
        assert plan_windows(_dt(2021, 1, 20), _dt(2021, 1, 10), window_days=90) == []

    @pytest.mark.parametrize("bad", [0, -1, -90])
    def test_non_positive_window_raises(self, bad: int) -> None:
        with pytest.raises(ValueError, match="window_days must be positive"):
            plan_windows(_dt(2021, 1, 1), _dt(2021, 1, 10), window_days=bad)


class TestConstantPinnedWrappers:
    def test_spot_uses_90_day_window(self) -> None:
        assert INTRADAY_WINDOW_DAYS == 90
        windows = plan_spot_intraday_windows(_dt(2021, 1, 1), _dt(2021, 7, 1))
        manual = plan_windows(_dt(2021, 1, 1), _dt(2021, 7, 1), window_days=90)
        assert windows == manual

    def test_option_uses_30_day_window(self) -> None:
        assert ROLLING_OPTION_WINDOW_DAYS == 30
        windows = plan_option_windows(_dt(2021, 1, 1), _dt(2021, 4, 1))
        manual = plan_windows(_dt(2021, 1, 1), _dt(2021, 4, 1), window_days=30)
        assert windows == manual

    def test_option_window_is_narrower_than_spot(self) -> None:
        span = (_dt(2021, 1, 1), _dt(2021, 6, 1))
        assert len(plan_option_windows(*span)) > len(plan_spot_intraday_windows(*span))
