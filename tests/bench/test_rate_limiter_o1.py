"""O(1) benchmarks for AdaptiveTokenBucket.

Enforces ``NFR-PERF-001`` (hot-path latency) from ``§5.1`` for the
rate limiter's ``acquire`` fast path — the per-call overhead must
be independent of how long the bucket has been running, how many
429 events have been recorded, or how many upshift/downshift
transitions have occurred.

## Strategy

Use a synthetic clock so every ``acquire`` returns instantly (tokens
always available). We then measure the raw Python overhead of the
fast path — lock acquire, refill math, token decrement, lock release.
This isolates the work the limiter does per call from any real
``time.sleep`` cost.

The limiter also stores recent 429 timestamps in a ``deque``. As
old entries age out of the window, they should be popped in O(1).
We verify this by priming the window with thousands of 429 events
and checking that the per-call cost doesn't balloon.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from brutex.orchestrator.rate_limiter import (
    AdaptiveTokenBucket,
)

from tests.bench._o1_tiers import ratio_ceiling

# ────────────────────────────────────────────────────────────────────────
# Fake clock
# ────────────────────────────────────────────────────────────────────────


@dataclass
class FakeClock:
    """Monotonic fake clock that advances on demand."""

    t: float = 1000.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds

    def tick(self, seconds: float) -> None:
        self.t += seconds


# ────────────────────────────────────────────────────────────────────────
# Bucket factories
# ────────────────────────────────────────────────────────────────────────


def _fresh_bucket(clock: FakeClock) -> AdaptiveTokenBucket:
    """A bucket with plenty of capacity — acquire() never blocks.

    The burst is deliberately enormous (1e9). ``pytest-benchmark`` auto-calibrates
    its iteration count to the runner's speed and, for a sub-microsecond op like
    ``acquire``, can invoke ``_call`` *millions* of times within its time budget.
    Because the ``FakeClock`` never advances during those calls, no tokens refill,
    so a merely "large" burst (e.g. 1e6) could be drained on a fast runner — at
    which point ``acquire`` correctly returns a tiny non-zero throttle wait and the
    exact ``== 0.0`` assertions below flake. A 1e9 burst sits ~1000× above any
    feasible benchmark iteration count, keeping the fast path deterministically at
    zero wait.
    """
    return AdaptiveTokenBucket(
        initial_rps=10_000.0,
        downshift_rps=5_000.0,
        burst=1_000_000_000,  # ≫ any pytest-benchmark iteration count → never blocks
        clock=clock.now,
        sleep=clock.sleep,
    )


def _bucket_with_primed_window(clock: FakeClock, hits: int) -> AdaptiveTokenBucket:
    """A bucket with *hits* 429s recorded within the current window."""
    bucket = _fresh_bucket(clock)
    for _ in range(hits):
        bucket.record_rate_limit_hit()
        clock.tick(0.0001)  # tiny tick to keep hits distinguishable
    return bucket


# ────────────────────────────────────────────────────────────────────────
# acquire() fast path
# ────────────────────────────────────────────────────────────────────────


class TestAcquireFastPathIsO1:
    def test_acquire_with_empty_window(self, benchmark) -> None:
        clock = FakeClock()
        bucket = _fresh_bucket(clock)

        def _call():
            return bucket.acquire()

        assert benchmark(_call) == 0.0

    def test_acquire_with_primed_window(self, benchmark) -> None:
        """Even with thousands of 429s in the window, the fast path
        should be constant-time."""
        clock = FakeClock()
        bucket = _bucket_with_primed_window(clock, hits=5_000)

        def _call():
            return bucket.acquire()

        assert benchmark(_call) == 0.0

    def test_acquire_ratio_constant(self) -> None:
        """Empirical check: the per-call overhead must not grow as
        the 429 window fills up."""
        iterations = 50_000

        def _time_acquire(primed_hits: int) -> float:
            clock = FakeClock()
            bucket = _bucket_with_primed_window(clock, hits=primed_hits)
            # Warm up the Python interpreter caches.
            for _ in range(100):
                bucket.acquire()
            t0 = time.perf_counter_ns()
            for _ in range(iterations):
                bucket.acquire()
            t1 = time.perf_counter_ns()
            return (t1 - t0) / iterations

        t_empty = _time_acquire(0)
        t_primed = _time_acquire(5_000)
        ratio = t_primed / max(t_empty, 1.0)

        # Allow generous headroom for CI jitter. O(1) means ratio ≈ 1.0.
        assert ratio < ratio_ceiling(5.0), (
            f"acquire() per-call cost scales with window size: "
            f"ratio {ratio:.2f}× (5000-hit vs 0-hit). "
            f"t_empty={t_empty:.0f}ns, t_primed={t_primed:.0f}ns."
        )


# ────────────────────────────────────────────────────────────────────────
# record_rate_limit_hit — O(1)
# ────────────────────────────────────────────────────────────────────────


class TestRecordHitIsO1:
    def test_record_hit_small_window(self, benchmark) -> None:
        clock = FakeClock()
        bucket = _fresh_bucket(clock)

        def _hit():
            clock.tick(0.001)
            return bucket.record_rate_limit_hit()

        benchmark(_hit)

    def test_record_hit_large_window(self, benchmark) -> None:
        clock = FakeClock()
        bucket = _bucket_with_primed_window(clock, hits=5_000)

        def _hit():
            clock.tick(0.001)
            return bucket.record_rate_limit_hit()

        benchmark(_hit)


# ────────────────────────────────────────────────────────────────────────
# Aging-out 429s should stay O(1) per acquire
# ────────────────────────────────────────────────────────────────────────


class TestWindowAgeOutIsO1:
    """Old 429 entries should be popped off the deque cheaply."""

    def test_aged_out_entries_do_not_slow_acquire(self) -> None:
        clock = FakeClock()
        bucket = _bucket_with_primed_window(clock, hits=1_000)

        # Jump the clock past the downshift window so everything ages out.
        clock.tick(120.0)

        # First acquire should flush the stale window.
        bucket.acquire()

        # Subsequent acquires should be back to the empty-window cost.
        iterations = 10_000
        t0 = time.perf_counter_ns()
        for _ in range(iterations):
            bucket.acquire()
        t1 = time.perf_counter_ns()
        per_call = (t1 - t0) / iterations

        # Should be well under 100µs per call — empty fast path.
        assert per_call < 100_000, (
            f"acquire() took {per_call:.0f}ns on average after window flush — expected <100µs."
        )
