"""Property tests for `brutex.adapters.groww.chunks.plan_chunks`.

The chunk planner is the "no missing data" boundary — if its windows had a
gap or overlap, candles would be skipped or double-pulled. These properties
prove, over random ranges + every interval, that the windows TILE the
requested range exactly: contiguous, non-overlapping, each within the
interval's limit, the first starting at ``start`` and the last ending at ``end``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import pairwise

from brutex.adapters.groww.chunks import WINDOW_DAYS, plan_chunks
from hypothesis import given
from hypothesis import strategies as st

_starts = st.datetimes(
    min_value=datetime(2020, 1, 1, 0, 0),
    max_value=datetime(2030, 1, 1, 0, 0),
    timezones=st.just(UTC),
)
_intervals = st.sampled_from(sorted(WINDOW_DAYS))
_day_spans = st.integers(min_value=-5, max_value=2000)


class TestTiling:
    @given(_starts, _intervals, _day_spans)
    def test_windows_tile_the_range_exactly(
        self, start: datetime, interval: str, span: int
    ) -> None:
        end = start + timedelta(days=span)
        chunks = plan_chunks("NSE-NIFTY", interval, start, end)

        if span <= 0:
            assert chunks == []
            return

        limit = timedelta(days=WINDOW_DAYS[interval])
        # First starts at start; last ends at end (full coverage, clipped).
        assert chunks[0].start == start.isoformat()
        assert chunks[-1].end == end.isoformat()
        for chunk in chunks:
            lo = datetime.fromisoformat(chunk.start)
            hi = datetime.fromisoformat(chunk.end)
            assert lo < hi  # every window is non-empty
            assert hi - lo <= limit  # never wider than the Groww limit
        # Contiguous + non-overlapping: each window's end is the next's start.
        for earlier, later in pairwise(chunks):
            assert earlier.end == later.start

    @given(_starts, _intervals, _day_spans)
    def test_deterministic(self, start: datetime, interval: str, span: int) -> None:
        end = start + timedelta(days=span)
        assert plan_chunks("NSE-NIFTY", interval, start, end) == plan_chunks(
            "NSE-NIFTY", interval, start, end
        )

    @given(_starts, _intervals, _day_spans)
    def test_every_chunk_carries_symbol_and_interval(
        self, start: datetime, interval: str, span: int
    ) -> None:
        end = start + timedelta(days=span)
        for chunk in plan_chunks("NSE-NIFTY", interval, start, end):
            assert chunk.groww_symbol == "NSE-NIFTY"
            assert chunk.interval == interval
