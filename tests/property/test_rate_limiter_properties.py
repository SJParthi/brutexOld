"""P-18 — the adaptive token bucket never exceeds its rate cap.

CLAUDE.md Golden Rule #6 (rate-limit safety): the Dhan Data API allows at
most 5 r/s, and the adapter paces through :class:`AdaptiveTokenBucket`. The
named invariant **P-18** asserts the limiter physically cannot issue calls
faster than its configured rate (modulo the initial burst). This drives the
bucket through a deterministic fake clock and proves, over arbitrary
rate/burst/call-count combinations, that:

    n_calls  ≤  burst  +  rate × elapsed_seconds

i.e. the achieved throughput never beats the cap. Promoted from the catalog
``Phase 1.5 pending`` stub (DEC-PARSER-FUZZ-001 batch).
"""

from __future__ import annotations

from brutex.orchestrator.rate_limiter import AdaptiveTokenBucket
from hypothesis import given, settings
from hypothesis import strategies as st


class _FakeClock:
    """Monotonic clock advanced only by the bucket's own ``sleep`` calls."""

    def __init__(self, start: float = 1000.0) -> None:
        self._t = start

    def now(self) -> float:
        return self._t

    def sleep(self, seconds: float) -> None:
        # The bucket only ever sleeps non-negative durations.
        assert seconds >= 0.0
        self._t += seconds


@given(
    rate=st.integers(min_value=1, max_value=5),
    burst=st.integers(min_value=1, max_value=5),
    n_calls=st.integers(min_value=1, max_value=60),
)
@settings(max_examples=300)
def test_p_18_bucket_never_exceeds_rate_cap(rate: int, burst: int, n_calls: int) -> None:
    """P-18: over any run of ``acquire`` calls, the number issued never
    exceeds ``burst + rate × elapsed`` — the limiter cannot beat its cap."""
    clock = _FakeClock()
    bucket = AdaptiveTokenBucket(
        initial_rps=float(rate),
        downshift_rps=0.5,  # < initial; never triggered (no 429 path exercised)
        downshift_trigger=3,
        burst=burst,
        clock=clock.now,
        sleep=clock.sleep,
    )

    start = clock.now()
    total_wait = 0.0
    for _ in range(n_calls):
        total_wait += bucket.acquire()
    elapsed = clock.now() - start

    # The fake clock only advances via the bucket's sleep, so the returned
    # wait must reconcile with the clock to the cent of a second.
    assert abs(elapsed - total_wait) < 1e-6

    # Core rate-safety invariant (Golden Rule #6): issued ≤ burst + rate·elapsed.
    eps = 1e-6
    assert n_calls <= burst + rate * elapsed + eps


@given(
    rate=st.integers(min_value=1, max_value=5),
    burst=st.integers(min_value=1, max_value=5),
)
@settings(max_examples=100)
def test_p_18_burst_then_steady_pacing(rate: int, burst: int) -> None:
    """P-18: the first ``burst`` acquisitions are instant (pre-filled bucket),
    and every acquisition past the burst waits ≥ ``1/rate`` seconds."""
    clock = _FakeClock()
    bucket = AdaptiveTokenBucket(
        initial_rps=float(rate),
        downshift_rps=0.5,
        downshift_trigger=3,
        burst=burst,
        clock=clock.now,
        sleep=clock.sleep,
    )

    waits = [bucket.acquire() for _ in range(burst + rate + 1)]

    # The opening burst is free.
    assert all(w == 0.0 for w in waits[:burst])
    # Past the burst, pacing kicks in — at least one paced wait appears and
    # no paced wait undershoots the 1/rate floor (minus float epsilon).
    paced = [w for w in waits[burst:] if w > 0.0]
    assert paced, "expected pacing waits after the burst was spent"
    floor = 1.0 / rate
    assert all(w >= floor - 1e-9 for w in paced)
