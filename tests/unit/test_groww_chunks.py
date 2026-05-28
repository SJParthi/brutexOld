"""Tests for `brutex.adapters.groww.chunks` — the window-splitting planner."""

from __future__ import annotations

from datetime import UTC, datetime
from itertools import pairwise

import pytest
from brutex.adapters.groww.chunks import (
    ALL_GROWW_INTERVALS,
    GROWW_INTERVAL_TO_PARTITION,
    WINDOW_DAYS,
    plan_chunks,
)
from brutex.data.partition import VALID_INTERVALS
from brutex.data.watermark import ChunkKey


def _dt(month: int, day: int) -> datetime:
    return datetime(2021, month, day, 9, 15, tzinfo=UTC)


class TestWindowLimits:
    def test_minute_intervals_are_30_days(self) -> None:
        for iv in ("1m", "2m", "3m", "5m"):
            assert WINDOW_DAYS[iv] == 30

    def test_mid_intervals_are_90_days(self) -> None:
        for iv in ("10m", "15m", "30m"):
            assert WINDOW_DAYS[iv] == 90

    def test_long_intervals_are_180_days(self) -> None:
        for iv in ("1h", "4h", "1d", "1w", "1mo"):
            assert WINDOW_DAYS[iv] == 180


class TestPlanChunks:
    def test_single_window_when_range_under_limit(self) -> None:
        chunks = plan_chunks("NSE-NIFTY", "1m", _dt(1, 1), _dt(1, 10))
        assert chunks == [
            ChunkKey("NSE-NIFTY", "1m", _dt(1, 1).isoformat(), _dt(1, 10).isoformat())
        ]

    def test_multiple_windows_last_clipped_to_end(self) -> None:
        # 1m → 30-day windows; Jan 1 → Mar 1 (59 days) = 30 + 29
        chunks = plan_chunks("NSE-NIFTY", "1m", _dt(1, 1), _dt(3, 1))
        assert len(chunks) == 2
        assert chunks[0].start == _dt(1, 1).isoformat()
        assert chunks[0].end == _dt(1, 31).isoformat()  # +30 days
        assert chunks[1].start == _dt(1, 31).isoformat()  # contiguous
        assert chunks[1].end == _dt(3, 1).isoformat()  # clipped to end

    def test_contiguous_no_overlap_no_gap(self) -> None:
        chunks = plan_chunks("NSE-NIFTY", "1m", _dt(1, 1), _dt(4, 1))
        for earlier, later in pairwise(chunks):
            assert earlier.end == later.start  # tile exactly

    def test_empty_when_start_not_before_end(self) -> None:
        assert plan_chunks("NSE-NIFTY", "1m", _dt(2, 1), _dt(2, 1)) == []
        assert plan_chunks("NSE-NIFTY", "1m", _dt(3, 1), _dt(2, 1)) == []

    def test_window_days_override(self) -> None:
        chunks = plan_chunks("NSE-NIFTY", "1m", _dt(1, 1), _dt(1, 11), window_days=5)
        assert len(chunks) == 2  # 5 + 5
        assert chunks[0].end == _dt(1, 6).isoformat()

    def test_unknown_interval_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown interval"):
            plan_chunks("NSE-NIFTY", "7m", _dt(1, 1), _dt(2, 1))

    def test_unknown_interval_ok_with_override(self) -> None:
        chunks = plan_chunks("NSE-NIFTY", "7m", _dt(1, 1), _dt(1, 5), window_days=10)
        assert len(chunks) == 1

    def test_non_positive_window_raises(self) -> None:
        with pytest.raises(ValueError, match="must be positive"):
            plan_chunks("NSE-NIFTY", "1m", _dt(1, 1), _dt(2, 1), window_days=0)


class TestIntervalVocabulary:
    def test_partition_mapping_matches_lake_exactly(self) -> None:
        # The short→long map's targets MUST equal the lake's partition vocab,
        # or persisted bars would land under an interval the lake rejects.
        assert set(GROWW_INTERVAL_TO_PARTITION.values()) == VALID_INTERVALS

    def test_mapping_keys_cover_every_window_interval(self) -> None:
        assert set(GROWW_INTERVAL_TO_PARTITION) == set(WINDOW_DAYS)

    def test_all_intervals_is_the_full_native_set(self) -> None:
        assert set(ALL_GROWW_INTERVALS) == set(WINDOW_DAYS)
        assert len(ALL_GROWW_INTERVALS) == 12
