"""Unit tests for brutex.orchestrator.rate_limiter — adaptive token bucket.

Every test uses an **injected fake clock** so the whole suite runs in
milliseconds (no real ``time.sleep`` ever called). This proves the
determinism guarantee — the limiter's behaviour is purely a function
of the timing the caller reports.
"""

from __future__ import annotations

import pytest

from brutex.orchestrator.rate_limiter import (
    DEFAULT_DOWNSHIFT_RPS,
    DEFAULT_INITIAL_RPS,
    AdaptiveTokenBucket,
    RateLimiterError,
    RateLimiterEvent,
)


# ────────────────────────────────────────────────────────────────────────
# Fake clock & sleep helpers — deterministic test infrastructure
# ────────────────────────────────────────────────────────────────────────


class FakeClock:
    """Monotonic clock controlled by tests.

    Exposes ``.now`` as the injected ``clock`` callable. Calling
    :meth:`advance` moves it forward. Calling ``sleep(s)`` also
    advances by ``s``.
    """

    def __init__(self, start: float = 1000.0) -> None:
        self._t = start

    def now(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds

    def sleep(self, seconds: float) -> None:
        self._t += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def bucket(clock: FakeClock) -> AdaptiveTokenBucket:
    return AdaptiveTokenBucket(
        initial_rps=15.0,
        downshift_rps=10.0,
        downshift_trigger=3,
        downshift_window=60.0,
        upshift_recovery=120.0,
        burst=5,
        clock=clock.now,
        sleep=clock.sleep,
    )


# ────────────────────────────────────────────────────────────────────────
# Construction / validation
# ────────────────────────────────────────────────────────────────────────


class TestConstruction:
    def test_defaults_are_sane(self) -> None:
        b = AdaptiveTokenBucket()
        assert b.initial_rps == DEFAULT_INITIAL_RPS == 15.0
        assert b.downshift_rps == DEFAULT_DOWNSHIFT_RPS == 10.0
        assert b.burst >= 1

    def test_zero_initial_rps_rejected(self) -> None:
        with pytest.raises(RateLimiterError):
            AdaptiveTokenBucket(initial_rps=0.0)

    def test_negative_rps_rejected(self) -> None:
        with pytest.raises(RateLimiterError):
            AdaptiveTokenBucket(initial_rps=-1.0)

    def test_downshift_not_below_initial_rejected(self) -> None:
        with pytest.raises(RateLimiterError, match="must be <"):
            AdaptiveTokenBucket(initial_rps=15.0, downshift_rps=20.0)

    def test_equal_rps_rejected(self) -> None:
        with pytest.raises(RateLimiterError, match="must be <"):
            AdaptiveTokenBucket(initial_rps=15.0, downshift_rps=15.0)

    def test_zero_trigger_rejected(self) -> None:
        with pytest.raises(RateLimiterError):
            AdaptiveTokenBucket(downshift_trigger=0)

    def test_zero_burst_rejected(self) -> None:
        with pytest.raises(RateLimiterError):
            AdaptiveTokenBucket(burst=0)


# ────────────────────────────────────────────────────────────────────────
# Happy-path acquire
# ────────────────────────────────────────────────────────────────────────


class TestAcquireBurst:
    def test_initial_burst_is_free(self, bucket: AdaptiveTokenBucket) -> None:
        """First BURST calls should be instant (bucket starts full)."""
        for _ in range(5):  # burst = 5
            wait = bucket.acquire()
            assert wait == 0.0

    def test_sixth_call_waits(
        self, bucket: AdaptiveTokenBucket, clock: FakeClock
    ) -> None:
        # Drain burst
        for _ in range(5):
            bucket.acquire()
        # Next call needs to wait 1/15 s
        before = clock.now()
        wait = bucket.acquire()
        after = clock.now()
        assert wait == pytest.approx(1.0 / 15.0)
        assert after - before == pytest.approx(1.0 / 15.0)

    def test_steady_state_throughput(
        self, bucket: AdaptiveTokenBucket, clock: FakeClock
    ) -> None:
        """After burst, sustained throughput = initial_rps."""
        # Drain burst
        for _ in range(5):
            bucket.acquire()
        # Next 15 calls should take exactly 1 second total
        start = clock.now()
        for _ in range(15):
            bucket.acquire()
        elapsed = clock.now() - start
        assert elapsed == pytest.approx(1.0, abs=0.05)


# ────────────────────────────────────────────────────────────────────────
# Downshift on consecutive 429s
# ────────────────────────────────────────────────────────────────────────


class TestDownshift:
    def test_single_429_does_not_downshift(
        self, bucket: AdaptiveTokenBucket
    ) -> None:
        event = bucket.record_rate_limit_hit()
        assert event is not None
        assert event.kind == "rate_limit_hit"
        assert event.from_rps == 15.0
        assert bucket.current_rps() == 15.0

    def test_two_429s_do_not_downshift(
        self, bucket: AdaptiveTokenBucket
    ) -> None:
        for _ in range(2):
            event = bucket.record_rate_limit_hit()
            assert event is not None and event.kind == "rate_limit_hit"
        assert bucket.current_rps() == 15.0

    def test_three_429s_trigger_downshift(
        self, bucket: AdaptiveTokenBucket
    ) -> None:
        events = [bucket.record_rate_limit_hit() for _ in range(3)]
        assert events[2] is not None
        assert events[2].kind == "downshift"
        assert events[2].from_rps == 15.0
        assert events[2].to_rps == 10.0
        assert events[2].recent_429_count == 3
        assert bucket.current_rps() == 10.0

    def test_fourth_429_after_downshift_stays_downshifted(
        self, bucket: AdaptiveTokenBucket
    ) -> None:
        for _ in range(3):
            bucket.record_rate_limit_hit()
        assert bucket.current_rps() == 10.0
        # A 4th hit — we're already downshifted, no new downshift event
        event = bucket.record_rate_limit_hit()
        assert event is not None
        assert event.kind == "rate_limit_hit"
        assert bucket.current_rps() == 10.0

    def test_old_429s_evicted_from_window(
        self, bucket: AdaptiveTokenBucket, clock: FakeClock
    ) -> None:
        """Two 429s → wait past the 60s window → two more 429s: no trigger."""
        bucket.record_rate_limit_hit()
        bucket.record_rate_limit_hit()
        clock.advance(61.0)  # Past the window
        # The first two are now out of window
        event1 = bucket.record_rate_limit_hit()
        event2 = bucket.record_rate_limit_hit()
        assert event1 is not None and event1.kind == "rate_limit_hit"
        assert event2 is not None and event2.kind == "rate_limit_hit"
        assert bucket.current_rps() == 15.0

    def test_three_within_window_still_triggers_after_eviction(
        self, bucket: AdaptiveTokenBucket, clock: FakeClock
    ) -> None:
        """Two at t=0, advance 30s (still in window), one more → trigger."""
        bucket.record_rate_limit_hit()
        bucket.record_rate_limit_hit()
        clock.advance(30.0)
        event = bucket.record_rate_limit_hit()
        assert event is not None and event.kind == "downshift"


# ────────────────────────────────────────────────────────────────────────
# Effective rate after downshift
# ────────────────────────────────────────────────────────────────────────


class TestDownshiftedThroughput:
    def test_effective_rate_drops_to_10_rps(
        self, bucket: AdaptiveTokenBucket, clock: FakeClock
    ) -> None:
        # Drain burst first
        for _ in range(5):
            bucket.acquire()
        # Downshift
        for _ in range(3):
            bucket.record_rate_limit_hit()
        assert bucket.current_rps() == 10.0

        # Wait for burst to refill at the new rate
        clock.advance(10.0)  # 10s * 10 rps = 100 tokens, clamped at burst=5
        start = clock.now()
        for _ in range(10):  # Drain the refilled burst + a few steady
            bucket.acquire()
        elapsed = clock.now() - start
        # Burst (5) free + 5 at 10 rps = 0.5s
        assert elapsed == pytest.approx(0.5, abs=0.05)


# ────────────────────────────────────────────────────────────────────────
# Stats — dashboard hooks
# ────────────────────────────────────────────────────────────────────────


class TestStats:
    def test_current_rps_after_construction(
        self, bucket: AdaptiveTokenBucket
    ) -> None:
        assert bucket.current_rps() == 15.0

    def test_available_tokens_starts_at_burst(
        self, bucket: AdaptiveTokenBucket
    ) -> None:
        assert bucket.available_tokens() == 5.0

    def test_available_tokens_after_acquire(
        self, bucket: AdaptiveTokenBucket
    ) -> None:
        bucket.acquire()
        # 5 - 1 = 4 (no time passed, no refill)
        # But the refill helper runs on every check, so a micro refill may occur.
        remaining = bucket.available_tokens()
        assert 3.9 <= remaining <= 5.0

    def test_available_tokens_clamped_to_burst(
        self, bucket: AdaptiveTokenBucket, clock: FakeClock
    ) -> None:
        """Tokens should never exceed burst capacity even with long waits."""
        clock.advance(1000.0)
        assert bucket.available_tokens() == 5.0

    def test_recent_429_count_starts_at_zero(
        self, bucket: AdaptiveTokenBucket
    ) -> None:
        assert bucket.recent_429_count() == 0

    def test_recent_429_count_tracks_hits(
        self, bucket: AdaptiveTokenBucket
    ) -> None:
        bucket.record_rate_limit_hit()
        bucket.record_rate_limit_hit()
        assert bucket.recent_429_count() == 2

    def test_recent_429_count_evicts(
        self, bucket: AdaptiveTokenBucket, clock: FakeClock
    ) -> None:
        bucket.record_rate_limit_hit()
        clock.advance(61.0)
        assert bucket.recent_429_count() == 0


# ────────────────────────────────────────────────────────────────────────
# Never-drop guarantee
# ────────────────────────────────────────────────────────────────────────


class TestNeverDropGuarantee:
    """Per FR-ING-017: the limiter NEVER drops a request silently.

    Every acquire() call eventually returns. The wait time may be
    long under extreme rate limiting but it's never infinite and
    never raises an exception.
    """

    def test_acquire_always_returns_non_negative(
        self, bucket: AdaptiveTokenBucket
    ) -> None:
        for _ in range(20):
            wait = bucket.acquire()
            assert wait >= 0.0

    def test_acquire_after_downshift_still_works(
        self, bucket: AdaptiveTokenBucket
    ) -> None:
        for _ in range(3):
            bucket.record_rate_limit_hit()
        for _ in range(10):
            wait = bucket.acquire()
            assert wait >= 0.0

    def test_rate_limit_hits_never_raise(
        self, bucket: AdaptiveTokenBucket
    ) -> None:
        """Recording 429s should never raise, regardless of count."""
        for _ in range(100):
            event = bucket.record_rate_limit_hit()
            assert event is not None  # Always returns an event

    def test_record_success_never_raises(
        self, bucket: AdaptiveTokenBucket
    ) -> None:
        bucket.record_success()
        bucket.record_success()
        # Still at initial rps
        assert bucket.current_rps() == 15.0


# ────────────────────────────────────────────────────────────────────────
# Thread safety sanity check (no deadlock, correct final state)
# ────────────────────────────────────────────────────────────────────────


class TestThreadSafety:
    def test_concurrent_acquires_serialize(self) -> None:
        """Multiple threads calling acquire() should not deadlock.

        Uses REAL time.monotonic and real sleep here because we want
        to exercise the lock under actual contention.
        """
        import threading
        import time

        bucket = AdaptiveTokenBucket(
            initial_rps=100.0,  # Fast for test
            downshift_rps=50.0,
            burst=10,
        )

        results: list[float] = []

        def worker() -> None:
            for _ in range(3):
                wait = bucket.acquire()
                results.append(wait)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        start = time.monotonic()
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)
        elapsed = time.monotonic() - start

        # Should complete in well under 5 seconds
        assert elapsed < 5.0
        # 15 acquires total, burst=10 free, 5 at 100 rps = ~50ms
        assert len(results) == 15
