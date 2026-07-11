"""O(1) benchmark for the rate-bridge cache steady-state read (DEC-O1-BENCH-GAP-CLOSURE-001).

Gates the load-bearing claim of ``brutex/dashboard/rate_bridge_cache.py``
(DEC-PULL-RATE-BRIDGE-NONBLOCK-CACHE-001): a repeat ``GET /api/v1/pull/rate``
poll landing inside the :data:`~brutex.dashboard.rate_bridge_cache.RATE_BRIDGE_CACHE_TTL_S`
TTL window is a **zero-syscall O(1) in-memory hit** — the site-wide widget polls
this route every ~3 s from EVERY dashboard page, so a stalled external lake volume
must never touch it on the hot path.

Two things are pinned here (neither is covered by the module's correctness tests):

1. **Zero syscalls on the TTL hit.** The injected ``probe`` (one ``os.stat``) and
   ``reader`` (the ~300-byte JSON read) are wrapped in call counters. After the
   first (refresh) ``get``, every subsequent ``get`` within the frozen-clock TTL
   window must NOT call either — proving the hot path performs no filesystem I/O
   (the "no wedged read on the event loop" guarantee).
2. **Flat latency vs poll count.** The per-``get`` cost of a TTL hit is measured
   at a small vs a large poll volume and asserted flat under ``ratio_ceiling(5.0)``
   — 5× on the shared GHA runner, tightened to the strict 1.1× M4-Pro tier
   (``tests/bench/_o1_tiers.py``). A regression that re-read the file per poll
   would scale with the poll count and blow past this bound.

Best-of-N-MIN timing (DEC-CANARY-MINTIME-001) keeps the shared-runner ratio
robust: the reported per-call figure is the MIN over several trial loops, which
rejects scheduler-noise spikes that would otherwise inflate the ratio.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from brutex.dashboard.rate_bridge_cache import (
    BRIDGE_SOURCE_CACHED,
    BRIDGE_SOURCE_LIVE,
    RateBridgeCache,
)
from brutex.data.rate_bridge import RateBridgeSnapshot

from tests.bench._o1_tiers import ratio_ceiling

_LAKE = Path("/synthetic/lake")


class _FrozenClock:
    """A monotonic clock stuck at one instant, so every ``get`` stays inside the TTL."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _snapshot() -> RateBridgeSnapshot:
    """A representative decoded bridge snapshot (the value the cache memoises)."""
    return RateBridgeSnapshot(
        schema=1,
        effective_rps=4.0,
        current_rps=5.0,
        recent_minute=240,
        recent_429=0,
        ceiling_per_min=500,
        recent_1s=4,
        recent_600s=100,
        recent_3600s=500,
        wall_time=1000.0,
        monotonic=1000.0,
        pull_active=True,
    )


class _Counters:
    """Probe + reader call counters — a TTL hit must increment neither."""

    def __init__(self) -> None:
        self.probe = 0
        self.reader = 0

    def make(self) -> tuple[Any, Any]:
        snap = _snapshot()

        def probe(_path: Path) -> str:
            self.probe += 1
            return "tok"

        def reader(_path: Path) -> RateBridgeSnapshot:
            self.reader += 1
            return snap

        return probe, reader


def _primed_cache() -> tuple[RateBridgeCache, _Counters]:
    """A cache whose first ``get`` completed a refresh; the clock is frozen inside TTL."""
    counters = _Counters()
    probe, reader = counters.make()
    cache = RateBridgeCache(
        ttl_s=2.0,
        read_deadline_s=0.5,
        clock=_FrozenClock(),
        probe=probe,
        reader=reader,
        guard=lambda fn, _s: fn(),  # inline (thread-free) refresh
    )
    first = cache.get(_LAKE)  # the one refresh: source == live, probe+reader each fire once
    assert first.source == BRIDGE_SOURCE_LIVE
    return cache, counters


class TestTtlHitZeroSyscall:
    """A repeat poll inside the TTL is a zero-syscall in-memory hit."""

    def test_ttl_hit_performs_no_io(self) -> None:
        cache, counters = _primed_cache()
        probe_after_prime = counters.probe
        reader_after_prime = counters.reader
        for _ in range(50_000):
            result = cache.get(_LAKE)
            assert result.source == BRIDGE_SOURCE_CACHED
        # Not one extra probe (os.stat) or reader (file read) on the hot path.
        assert counters.probe == probe_after_prime, "TTL hit issued an os.stat probe"
        assert counters.reader == reader_after_prime, "TTL hit re-read the bridge file"

    def test_ttl_hit_latency_flat_vs_poll_count(self) -> None:
        """The per-hit latency at a large vs small poll volume is flat (< ceiling)."""
        cache, _counters = _primed_cache()

        def _per_call(iterations: int) -> float:
            best = float("inf")
            for _ in range(5):  # best-of-N-MIN (DEC-CANARY-MINTIME-001)
                t0 = time.perf_counter_ns()
                for _ in range(iterations):
                    cache.get(_LAKE)
                best = min(best, (time.perf_counter_ns() - t0) / iterations)
            return best

        _per_call(20_000)  # warm
        small = _per_call(20_000)
        big = _per_call(400_000)
        ratio = big / max(small, 1.0)
        assert ratio < ratio_ceiling(5.0), (
            f"rate-bridge TTL-hit latency ratio {ratio:.2f}× (400k vs 20k polls) exceeds "
            f"the 5× CI ceiling — the steady-state read may have regressed off the "
            f"in-memory path. small={small:.0f}ns big={big:.0f}ns."
        )
