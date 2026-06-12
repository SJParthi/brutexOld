"""Tests for `brutex.adapters.groww.chunks` — date-range chunker.

100% line+branch: single-chunk range, exact-multiple tiling, final-chunk
clipping, end < start error, unknown interval error, chunk_count helper,
and the three window-tier wrappers via chunk_window().
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from brutex.adapters.groww.chunks import chunk_count, chunk_window, iter_chunks
from brutex.adapters.groww.constants import (
    CANDLE_INTERVAL_10M,
    CANDLE_INTERVAL_1D,
    CANDLE_INTERVAL_1H,
    CANDLE_INTERVAL_1M,
    CANDLE_INTERVAL_30M,
    CHUNK_WINDOW_LONG_DAYS,
    CHUNK_WINDOW_MED_DAYS,
    CHUNK_WINDOW_SHORT_DAYS,
)


def _chunks(start: date, end: date, interval: str = CANDLE_INTERVAL_1M) -> list[tuple[date, date]]:
    return list(iter_chunks(start, end, interval))


class TestChunkWindow:
    def test_1m_is_short(self) -> None:
        assert chunk_window(CANDLE_INTERVAL_1M) == CHUNK_WINDOW_SHORT_DAYS

    def test_10m_is_med(self) -> None:
        assert chunk_window(CANDLE_INTERVAL_10M) == CHUNK_WINDOW_MED_DAYS

    def test_1d_is_long(self) -> None:
        assert chunk_window(CANDLE_INTERVAL_1D) == CHUNK_WINDOW_LONG_DAYS

    def test_unknown_interval_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown interval"):
            chunk_window("badinterval")


class TestIterChunks:
    def test_single_day_yields_one_chunk(self) -> None:
        d = date(2024, 6, 3)
        chunks = _chunks(d, d)
        assert chunks == [(d, d)]

    def test_same_start_end_yields_one_chunk(self) -> None:
        d = date(2024, 1, 1)
        chunks = _chunks(d, d)
        assert len(chunks) == 1
        assert chunks[0] == (d, d)

    def test_range_within_window_is_single_chunk(self) -> None:
        start = date(2024, 6, 1)
        end = date(2024, 6, 20)  # 19 days < 30-day window
        chunks = _chunks(start, end)
        assert len(chunks) == 1
        assert chunks[0] == (start, end)

    def test_exact_window_size_is_single_chunk(self) -> None:
        start = date(2024, 1, 1)
        end = start + timedelta(days=CHUNK_WINDOW_SHORT_DAYS - 1)
        chunks = _chunks(start, end)
        assert len(chunks) == 1
        assert chunks[0] == (start, end)

    def test_range_spanning_two_windows(self) -> None:
        start = date(2024, 1, 1)
        # 31 days > 30-day window → 2 chunks
        end = start + timedelta(days=30)
        chunks = _chunks(start, end)
        assert len(chunks) == 2

    def test_chunks_are_non_overlapping(self) -> None:
        start = date(2024, 1, 1)
        end = date(2024, 12, 31)
        chunks = _chunks(start, end)
        for i in range(len(chunks) - 1):
            # chunk_start[i+1] = chunk_end[i] + 1 day
            assert chunks[i + 1][0] == chunks[i][1] + timedelta(days=1)

    def test_first_chunk_starts_at_start(self) -> None:
        start = date(2024, 1, 1)
        end = date(2024, 12, 31)
        chunks = _chunks(start, end)
        assert chunks[0][0] == start

    def test_last_chunk_ends_at_end(self) -> None:
        start = date(2024, 1, 1)
        end = date(2024, 12, 31)
        chunks = _chunks(start, end)
        assert chunks[-1][1] == end

    def test_final_chunk_clipped_below_window(self) -> None:
        start = date(2024, 1, 1)
        # 45 days → chunk 1 covers days 0-29, chunk 2 covers days 30-44
        end = start + timedelta(days=44)
        chunks = _chunks(start, end)
        assert len(chunks) == 2
        c2_start, c2_end = chunks[1]
        assert (c2_end - c2_start).days < CHUNK_WINDOW_SHORT_DAYS - 1

    def test_end_before_start_raises(self) -> None:
        with pytest.raises(ValueError, match="end.*≥.*start"):
            _chunks(date(2024, 6, 10), date(2024, 6, 1))

    def test_unknown_interval_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown interval"):
            _chunks(date(2024, 1, 1), date(2024, 2, 1), interval="badinterval")

    def test_med_window_produces_fewer_chunks_than_short(self) -> None:
        start = date(2024, 1, 1)
        end = date(2024, 6, 30)
        short_n = len(_chunks(start, end, CANDLE_INTERVAL_1M))
        med_n = len(_chunks(start, end, CANDLE_INTERVAL_10M))
        assert med_n < short_n

    def test_long_window_produces_fewer_chunks_than_med(self) -> None:
        start = date(2024, 1, 1)
        end = date(2024, 12, 31)
        med_n = len(_chunks(start, end, CANDLE_INTERVAL_30M))
        long_n = len(_chunks(start, end, CANDLE_INTERVAL_1H))
        assert long_n < med_n


class TestChunkCount:
    def test_single_day_is_one(self) -> None:
        d = date(2024, 6, 3)
        assert chunk_count(d, d, CANDLE_INTERVAL_1M) == 1

    def test_matches_len_of_iter_chunks(self) -> None:
        start = date(2024, 1, 1)
        end = date(2024, 12, 31)
        assert chunk_count(start, end, CANDLE_INTERVAL_1M) == len(
            list(iter_chunks(start, end, CANDLE_INTERVAL_1M))
        )

    def test_full_year_1m_more_than_12_chunks(self) -> None:
        # 365 / 30 ≈ 12.2 → 13 chunks
        start = date(2024, 1, 1)
        end = date(2024, 12, 31)
        assert chunk_count(start, end, CANDLE_INTERVAL_1M) == 13
