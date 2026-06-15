"""O(1) benchmark for the in-RAM ring append hot path (:mod:`brutex.live.ring`).

Enforces the per-tick hot-path O(1) NFR (CLAUDE.md §2 Rule 5 / Golden Rule #11
SCALABLE; ``docs/STANDARDS_100PCT.md`` §1 row 13) for DEC-LIVE-RAM-RING-001 (#797):
:meth:`~brutex.live.ring.TickRing.append` — the call the Groww feed daemon thread
makes on EVERY callback — must cost the same whether the ring already holds 1,000
items or 1,000,000. A ``collections.deque`` append is amortised O(1); this test
proves the WHOLE method (lock + append + capacity compare) stays flat, so a future
regression to a linear structure (e.g. a list ``insert(0, ...)`` or a per-append
scan) blows the ratio and fails CI.

The guarantee: append latency at N=1,000,000 / append latency at N=1,000 must be
below the CI ratio ceiling (5× on the shared GHA runner, tightened to the strict
1.1× M4-Pro tier via ``tests/bench/_o1_tiers.py::ratio_ceiling``,
DEC-O1-STRICT-TIER-001). Pure + in-memory — no ``growwapi``, no network, no disk.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from brutex.live.ring import RingItem, TickRing

from tests.bench._o1_tiers import ratio_ceiling

_RECV_TS = datetime(2026, 6, 15, 3, 45, 10, tzinfo=UTC)  # 09:15:10 IST


def _item(i: int) -> RingItem:
    """A deterministic ring item keyed on *i* (snapshot value carries the index)."""
    return RingItem(snapshot={"NSE:IDX:NIFTY": float(i)}, recv_ts=_RECV_TS, recv_ns=i)


def _time_append_after_prefill(prefill: int, *, iterations: int = 2_000) -> float:
    """Mean ns/append for a fresh tick AFTER the ring already holds *prefill* items.

    Capacity is set far above ``prefill + iterations`` so no spill/eviction occurs —
    the measurement is the pure append cost at the given occupancy.
    """
    ring = TickRing(capacity=prefill + iterations + 1)
    for i in range(prefill):
        ring.append(_item(i))
    sample = _item(prefill)
    # Warm up the bound-method dispatch + the lock fast path.
    for _ in range(50):
        ring.append(sample)
    t0 = time.perf_counter_ns()
    for _ in range(iterations):
        ring.append(sample)
    t1 = time.perf_counter_ns()
    return (t1 - t0) / iterations


class TestRingAppendIsO1:
    """Assert that :meth:`TickRing.append` latency does not scale with depth."""

    def test_append_at_1k_scale(self, benchmark: Any) -> None:
        ring = TickRing(capacity=10_000)
        for i in range(1_000):
            ring.append(_item(i))
        sample = _item(1_000)

        def _do() -> bool:
            return ring.append(sample)

        # Below capacity the append always reports "not full" (False).
        assert benchmark(_do) is False

    def test_append_ratio_below_ceiling(self) -> None:
        """Append latency ratio at 1M / 1K occupancy must stay under the CI ceiling.

        A flat ratio is the empirical O(1) proof for the per-tick hot path; a
        linear regression would blow it past ``ratio_ceiling(5.0)`` and fail.
        """
        t_1k = _time_append_after_prefill(1_000)
        t_1m = _time_append_after_prefill(1_000_000)
        ratio = t_1m / max(t_1k, 1.0)
        assert ratio < ratio_ceiling(5.0), (
            f"TickRing.append latency ratio {ratio:.2f}× (1M vs 1K depth) exceeds the "
            f"5× CI ceiling — the per-tick hot path may have regressed from O(1). "
            f"t_1k={t_1k:.0f}ns, t_1m={t_1m:.0f}ns."
        )
