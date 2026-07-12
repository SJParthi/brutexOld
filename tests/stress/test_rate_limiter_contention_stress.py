"""Real-clock contention stress for ``AdaptiveTokenBucket`` (DEC-RATE-SLEEP-OUTSIDE-LOCK-001).

Adapted from the round-2 adversarial refuter's harness (MIGRATION.md #1751 —
the quantified fail-open envelope): post-fix the retry sleep runs with the
bucket lock RELEASED, so under sustained demand > rate a woken sleeper can
lose its refilled token to peers ``_ACQUIRE_LOOP_CAP`` consecutive times and
the cap arm force-grants a "phantom" token (fail-OPEN, now COUNTED via
``acquire_fail_open_count()``). The deterministic single-thread pin lives in
``tests/unit/test_orchestrator_rate_limiter.py::TestNoSleepUnderLock``; this
suite proves the MULTITHREADED accounting envelope on the real clock:

* **accounting law** — total issues ≤ ``elapsed × rate + counted fail-opens``
  (every issue is token-backed or counted; the bucket starts empty and the
  rate never changes here, so cumulative refill telescopes to ≤
  ``elapsed × rate``). Timing-robust: ``elapsed`` is measured generously
  (construction → after the last join), never a tight median — safe in the
  bounded per-push ``pytest tests/stress -m 'not soak'`` step too.
* **liveness** — every worker joins (no deadlock, no wedged lock) and a
  raising sleep mid-retry under contention never wedges the bucket.

No minimum fail-open count is asserted (real-clock scheduling cannot force
one deterministically — the unit pin does); the envelope bound is the law.
"""

from __future__ import annotations

import threading
import time

import pytest
from brutex.orchestrator.rate_limiter import AdaptiveTokenBucket

#: All stress tests carry the ``stress`` marker (registered in pyproject.toml)
#: so the nightly ``pytest tests/stress -m stress`` soak selects them.
pytestmark = pytest.mark.stress


class TestContentionAccountingEnvelope:
    """Saturated demand > rate: issues stay inside the counted envelope."""

    def test_issues_bounded_by_refill_plus_counted_fail_opens(self) -> None:
        rps = 100.0
        n_threads = 8
        duration_s = 1.0
        t0 = time.monotonic()
        bucket = AdaptiveTokenBucket(
            initial_rps=rps, downshift_rps=rps / 2, burst=1, start_empty=True
        )
        issues = [0] * n_threads
        errors: list[BaseException] = []
        stop = threading.Event()

        def worker(i: int) -> None:
            while not stop.is_set():
                try:
                    bucket.acquire()
                except BaseException as exc:  # stress harness capture — every failure recorded
                    errors.append(exc)
                    return
                issues[i] += 1

        threads = [
            threading.Thread(target=worker, args=(i,), daemon=True) for i in range(n_threads)
        ]
        for t in threads:
            t.start()
        time.sleep(duration_s)
        stop.set()
        for t in threads:
            t.join(timeout=30.0)
        elapsed = time.monotonic() - t0
        assert not errors, f"worker raised under contention: {errors[0]!r}"
        assert all(not t.is_alive() for t in threads), "worker wedged (lock never released?)"
        total = sum(issues)
        fail_opens = bucket.acquire_fail_open_count()
        # The accounting law: every issue is token-backed (≤ elapsed × rate —
        # start_empty + fixed rate, refills telescope) or a COUNTED fail-open.
        bound = elapsed * rps + fail_opens
        assert total <= bound + 1e-6, (
            f"issues={total} exceeded refill+fail-open bound {bound:.1f} "
            f"(elapsed={elapsed:.3f}s, fail_opens={fail_opens}) — uncounted phantom tokens"
        )

    def test_raising_sleep_under_contention_never_wedges(self) -> None:
        """A flaky sleep raising mid-retry leaves the lock free every time."""
        real_sleep = time.sleep
        counter_lock = threading.Lock()
        n = {"c": 0}

        class _Boom(Exception):
            pass

        def flaky_sleep(seconds: float) -> None:
            with counter_lock:
                n["c"] += 1
                c = n["c"]
            if c % 7 == 0:
                raise _Boom
            real_sleep(min(seconds, 0.002))

        bucket = AdaptiveTokenBucket(
            initial_rps=200.0,
            downshift_rps=100.0,
            burst=1,
            start_empty=True,
            sleep=flaky_sleep,
        )
        booms = [0]
        other: list[BaseException] = []
        stop = threading.Event()

        def worker() -> None:
            while not stop.is_set():
                try:
                    bucket.acquire()
                except _Boom:
                    booms[0] += 1
                except BaseException as exc:  # stress harness capture — every failure recorded
                    other.append(exc)
                    return

        threads = [threading.Thread(target=worker, daemon=True) for _ in range(8)]
        for t in threads:
            t.start()
        time.sleep(0.75)
        stop.set()
        for t in threads:
            t.join(timeout=30.0)
        assert all(not t.is_alive() for t in threads), "worker wedged after a raising sleep"
        assert not other, f"non-Boom error under raising-sleep storm: {other[0]!r}"
        # The bucket is still fully usable after the storm — the lock is free.
        bucket.set_rate(5.0)
        assert bucket.current_rps() == 5.0
