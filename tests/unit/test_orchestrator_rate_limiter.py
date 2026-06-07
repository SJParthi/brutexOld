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
        assert b.initial_rps == DEFAULT_INITIAL_RPS == 3.0
        assert b.downshift_rps == DEFAULT_DOWNSHIFT_RPS == 1.5
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

    def test_sixth_call_waits(self, bucket: AdaptiveTokenBucket, clock: FakeClock) -> None:
        # Drain burst
        for _ in range(5):
            bucket.acquire()
        # Next call needs to wait 1/15 s
        before = clock.now()
        wait = bucket.acquire()
        after = clock.now()
        assert wait == pytest.approx(1.0 / 15.0)
        assert after - before == pytest.approx(1.0 / 15.0)

    def test_steady_state_throughput(self, bucket: AdaptiveTokenBucket, clock: FakeClock) -> None:
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
    def test_single_429_does_not_downshift(self, bucket: AdaptiveTokenBucket) -> None:
        event = bucket.record_rate_limit_hit()
        assert event is not None
        assert event.kind == "rate_limit_hit"
        assert event.from_rps == 15.0
        assert bucket.current_rps() == 15.0

    def test_two_429s_do_not_downshift(self, bucket: AdaptiveTokenBucket) -> None:
        for _ in range(2):
            event = bucket.record_rate_limit_hit()
            assert event is not None
            assert event.kind == "rate_limit_hit"
        assert bucket.current_rps() == 15.0

    def test_three_429s_trigger_downshift(self, bucket: AdaptiveTokenBucket) -> None:
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
        assert event1 is not None
        assert event1.kind == "rate_limit_hit"
        assert event2 is not None
        assert event2.kind == "rate_limit_hit"
        assert bucket.current_rps() == 15.0

    def test_three_within_window_still_triggers_after_eviction(
        self, bucket: AdaptiveTokenBucket, clock: FakeClock
    ) -> None:
        """Two at t=0, advance 30s (still in window), one more → trigger."""
        bucket.record_rate_limit_hit()
        bucket.record_rate_limit_hit()
        clock.advance(30.0)
        event = bucket.record_rate_limit_hit()
        assert event is not None
        assert event.kind == "downshift"


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
    def test_current_rps_after_construction(self, bucket: AdaptiveTokenBucket) -> None:
        assert bucket.current_rps() == 15.0

    def test_available_tokens_starts_at_burst(self, bucket: AdaptiveTokenBucket) -> None:
        assert bucket.available_tokens() == 5.0

    def test_available_tokens_after_acquire(self, bucket: AdaptiveTokenBucket) -> None:
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

    def test_recent_429_count_starts_at_zero(self, bucket: AdaptiveTokenBucket) -> None:
        assert bucket.recent_429_count() == 0

    def test_recent_429_count_tracks_hits(self, bucket: AdaptiveTokenBucket) -> None:
        bucket.record_rate_limit_hit()
        bucket.record_rate_limit_hit()
        assert bucket.recent_429_count() == 2

    def test_recent_429_count_evicts(self, bucket: AdaptiveTokenBucket, clock: FakeClock) -> None:
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

    def test_acquire_always_returns_non_negative(self, bucket: AdaptiveTokenBucket) -> None:
        for _ in range(20):
            wait = bucket.acquire()
            assert wait >= 0.0

    def test_acquire_after_downshift_still_works(self, bucket: AdaptiveTokenBucket) -> None:
        for _ in range(3):
            bucket.record_rate_limit_hit()
        for _ in range(10):
            wait = bucket.acquire()
            assert wait >= 0.0

    def test_rate_limit_hits_never_raise(self, bucket: AdaptiveTokenBucket) -> None:
        """Recording 429s should never raise, regardless of count."""
        for _ in range(100):
            event = bucket.record_rate_limit_hit()
            assert event is not None  # Always returns an event

    def test_record_success_never_raises(self, bucket: AdaptiveTokenBucket) -> None:
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


# ────────────────────────────────────────────────────────────────────────
# Bucket ↔ DailyQuotaTracker integration (DEC-P0-030, PR #50c)
# ────────────────────────────────────────────────────────────────────────


class TestQuotaTrackerIntegration:
    """Verify the optional ``quota_tracker`` field composes correctly.

    Closes Agent 1 §G None-safety, Agent 3 A1/A2/B1/B2 ordering, Agent 2
    §B no-silent-bypass (via explicit test that None is the test-only
    legacy path).
    """

    def test_quota_tracker_none_is_legacy_path(self) -> None:
        """When quota_tracker is None, bucket behaves identically to
        pre-PR-#50c (no quota call). This is the test-fixture path.
        """
        clock = FakeClock()
        sleep = clock.sleep
        bucket = AdaptiveTokenBucket(
            initial_rps=10.0,
            downshift_rps=5.0,
            burst=1,
            clock=clock.now,
            sleep=sleep,
            # quota_tracker omitted — defaults to None
        )
        assert bucket.quota_tracker is None
        wait = bucket.acquire()
        assert wait == 0.0  # first token from burst, no wait

    def test_quota_tracker_set_increments_on_acquire(self, tmp_path: object) -> None:
        """When quota_tracker is set, every successful bucket acquire
        triggers exactly one quota increment.
        """
        from datetime import datetime as _datetime
        from pathlib import Path
        from zoneinfo import ZoneInfo

        from brutex.orchestrator.quota_tracker import DailyQuotaTracker

        clock = FakeClock()
        sleep = clock.sleep

        ist = ZoneInfo("Asia/Kolkata")
        ist_now = _datetime(2026, 5, 18, 9, 15, tzinfo=ist)

        def ist_clock() -> _datetime:
            return ist_now

        tracker = DailyQuotaTracker(
            daily_limit=10,
            store_path=Path(str(tmp_path)) / "quota.lmdb",
            clock=ist_clock,
        )
        try:
            bucket = AdaptiveTokenBucket(
                initial_rps=10.0,
                downshift_rps=5.0,
                burst=5,
                clock=clock.now,
                sleep=sleep,
                quota_tracker=tracker,
            )
            assert tracker.current_count() == 0
            bucket.acquire()
            assert tracker.current_count() == 1
            bucket.acquire()
            bucket.acquire()
            assert tracker.current_count() == 3
        finally:
            tracker.close()

    def test_quota_exceeded_bubbles_up_from_acquire(self, tmp_path: object) -> None:
        """When the quota tracker raises DailyQuotaExceeded, it bubbles
        up from bucket.acquire() — the adapter maps this to Outcome.FATAL
        (no auto-retry).
        """
        from datetime import datetime as _datetime
        from pathlib import Path
        from zoneinfo import ZoneInfo

        from brutex.orchestrator.quota_tracker import (
            DailyQuotaExceeded,
            DailyQuotaTracker,
        )

        clock = FakeClock()
        sleep = clock.sleep

        ist = ZoneInfo("Asia/Kolkata")
        ist_now = _datetime(2026, 5, 18, 9, 15, tzinfo=ist)

        def ist_clock() -> _datetime:
            return ist_now

        tracker = DailyQuotaTracker(
            daily_limit=3,
            store_path=Path(str(tmp_path)) / "quota.lmdb",
            clock=ist_clock,
        )
        try:
            bucket = AdaptiveTokenBucket(
                initial_rps=10.0,
                downshift_rps=5.0,
                burst=5,
                clock=clock.now,
                sleep=sleep,
                quota_tracker=tracker,
            )
            bucket.acquire()
            bucket.acquire()
            bucket.acquire()
            with pytest.raises(DailyQuotaExceeded):
                bucket.acquire()
        finally:
            tracker.close()

    def test_record_success_unchanged_on_quota_raise(self, tmp_path: object) -> None:
        """Agent 3 A2/I1: bucket's _last_success_at MUST remain
        unchanged when quota raises. The adapter caller controls
        record_success(); the bucket itself does not call it inside
        acquire().
        """
        from datetime import datetime as _datetime
        from pathlib import Path
        from zoneinfo import ZoneInfo

        from brutex.orchestrator.quota_tracker import (
            DailyQuotaExceeded,
            DailyQuotaTracker,
        )

        clock = FakeClock()
        sleep = clock.sleep

        ist = ZoneInfo("Asia/Kolkata")
        ist_now = _datetime(2026, 5, 18, 9, 15, tzinfo=ist)

        def ist_clock() -> _datetime:
            return ist_now

        tracker = DailyQuotaTracker(
            daily_limit=1,
            store_path=Path(str(tmp_path)) / "quota.lmdb",
            clock=ist_clock,
        )
        try:
            bucket = AdaptiveTokenBucket(
                initial_rps=10.0,
                downshift_rps=5.0,
                burst=5,
                clock=clock.now,
                sleep=sleep,
                quota_tracker=tracker,
            )
            bucket.acquire()
            success_at_before = bucket._last_success_at
            with pytest.raises(DailyQuotaExceeded):
                bucket.acquire()
            # _last_success_at MUST be unchanged — bucket.acquire() does not
            # update it on quota failure (adapter caller controls success
            # recording via the separate record_success() method).
            assert bucket._last_success_at == success_at_before
        finally:
            tracker.close()


class TestStartEmpty:
    """``start_empty`` ease-in (DEC-DHAN-RATE-HARD-CAP-001): the bucket holds 0
    tokens at t=0 instead of ``burst``, so the FIRST acquire waits one full
    ``1/rate`` interval — no opening burst over the rate."""

    def test_default_starts_full_first_call_immediate(self, clock: FakeClock) -> None:
        """Legacy default (start_empty=False): a full bucket → first token is free."""
        b = AdaptiveTokenBucket(
            initial_rps=5.0,
            downshift_rps=2.5,
            burst=1,
            clock=clock.now,
            sleep=clock.sleep,
        )
        assert b.acquire() == 0.0

    def test_start_empty_first_call_waits_one_interval(self, clock: FakeClock) -> None:
        """start_empty=True: a 0-token bucket → the first call is paced at 1/rate."""
        b = AdaptiveTokenBucket(
            initial_rps=5.0,
            downshift_rps=2.5,
            burst=1,
            start_empty=True,
            clock=clock.now,
            sleep=clock.sleep,
        )
        assert b.acquire() == 1.0 / 5.0
