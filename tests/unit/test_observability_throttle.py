"""Tests for `brutex.observability.throttle` — the generic throttled callback.

100% line+branch via an injected fake clock (deterministic, no real sleeping):
the first call always fires, calls within the window are dropped, the first call
past the window fires again, and a non-positive interval disables the throttle
(DEC-OPTIONS-LIVE-STATUS-001).
"""

from __future__ import annotations

from brutex.observability.throttle import DEFAULT_THROTTLE_INTERVAL_S, make_throttled


class _Clock:
    """A controllable monotonic clock — returns whatever ``now`` is set to."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_first_call_always_fires() -> None:
    """With no prior run recorded, the very first call runs the callback."""
    runs: list[int] = []
    clock = _Clock()
    throttled = make_throttled(lambda: runs.append(1), min_interval_s=2.0, clock=clock)

    throttled()

    assert runs == [1]


def test_call_within_window_is_dropped() -> None:
    """A second call before *min_interval_s* has elapsed is a no-op."""
    runs: list[int] = []
    clock = _Clock()
    throttled = make_throttled(lambda: runs.append(1), min_interval_s=2.0, clock=clock)

    throttled()  # fires at t=0
    clock.now = 1.5  # < 2.0 since the last run
    throttled()  # dropped

    assert runs == [1]


def test_call_past_window_fires_again() -> None:
    """The first call at/after *min_interval_s* runs the callback again and resets."""
    runs: list[int] = []
    clock = _Clock()
    throttled = make_throttled(lambda: runs.append(1), min_interval_s=2.0, clock=clock)

    throttled()  # fires at t=0
    clock.now = 2.0  # exactly the interval — no longer < min_interval_s
    throttled()  # fires
    clock.now = 3.0  # < 2.0 since the t=2.0 run → dropped
    throttled()

    assert runs == [1, 1]


def test_non_positive_interval_fires_every_call() -> None:
    """A non-positive interval means no throttle — every call fires."""
    runs: list[int] = []
    clock = _Clock()
    throttled = make_throttled(lambda: runs.append(1), min_interval_s=0.0, clock=clock)

    throttled()
    throttled()  # same instant, but interval 0 ⇒ never < 0 ⇒ always fires
    throttled()

    assert runs == [1, 1, 1]


def test_default_interval_is_two_seconds() -> None:
    """The module default matches the summary-cache TTL floor (2 s)."""
    assert DEFAULT_THROTTLE_INTERVAL_S == 2.0


def test_default_clock_is_used_when_not_injected() -> None:
    """Omitting *clock* uses the real monotonic clock — the first call still fires."""
    runs: list[int] = []
    throttled = make_throttled(lambda: runs.append(1))

    throttled()

    assert runs == [1]
