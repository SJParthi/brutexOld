"""O() pins for the Rust-hosted minute-path index (DEC-RUST-CASCADE-RESULTS-001).

The `MinuteIndex.span_slice` lookup is O(log N) bisect over the crate-side
timestamp array — a repeat lookup must stay FLAT as the index deepens 100×
(1k vs 100k bars). Honest envelope (Golden Rule #10): building the index is
O(N_bars) — one price parse per bar, once per (instrument, window) — and is
deliberately NOT pinned flat; only the per-lookup cost is the O(1)-class
claim, gated at the shared-CI robust tier (`ratio_ceiling`).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from brutex.data.models import CandleBar
from brutex.engine.minute_path import MinuteIndex

from ._o1_tiers import ratio_ceiling

_T0 = datetime(2024, 1, 2, 3, 46, tzinfo=UTC)
_SMALL = 1_000
_LARGE = 100_000


def _index(n: int) -> MinuteIndex:
    bars = [
        CandleBar(
            timestamp=_T0 + timedelta(minutes=i),
            open=Decimal("100.00"),
            high=Decimal("101.00"),
            low=Decimal("99.00"),
            close=Decimal("100.50"),
            volume=1,
            open_interest=None,
        )
        for i in range(n)
    ]
    return MinuteIndex.from_bars(bars)


def _time_lookup(index: MinuteIndex, n: int, *, iterations: int = 2_000) -> float:
    start = _T0 + timedelta(minutes=n // 2)
    end = start + timedelta(minutes=3)
    for _ in range(50):  # warm the branch predictor + FFI path
        _ = index.span_slice(start, end)
    t0 = time.perf_counter_ns()
    for _ in range(iterations):
        _ = index.span_slice(start, end)
    t1 = time.perf_counter_ns()
    return (t1 - t0) / iterations


class TestSpanSliceO1Class:
    def test_lookup_ratio_flat_vs_index_depth(self) -> None:
        """A span lookup at 100k bars must stay within the robust O(1)-class
        ceiling of the 1k-bar cost (an O(log N) bisect moves ~1.7× in theory;
        an O(N) regression would blow the tier ceiling immediately)."""
        small = _index(_SMALL)
        large = _index(_LARGE)
        t_small = _time_lookup(small, _SMALL)
        t_large = _time_lookup(large, _LARGE)
        ratio = t_large / max(t_small, 1.0)
        assert ratio < ratio_ceiling(5.0), (
            f"MinuteIndex.span_slice ratio {ratio:.2f}× (100k vs 1k bars) exceeds the "
            f"tier ceiling — the lookup may have regressed from O(log N) to O(N). "
            f"t_1k={t_small:.0f}ns, t_100k={t_large:.0f}ns."
        )
