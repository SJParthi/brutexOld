"""Tests for `brutex.adapters.dhan.rate_control.DhanRateController`.

100% line+branch with an injected fake clock (no real sleeping): starts at the
5 r/s ceiling, steps down 5→4→3 on sustained 429s, clamps at the 3 r/s floor
(no halt), ignores a non-sustained burst, evicts the window, recovers 3→4→5 on
a sustained clean window, never exceeds 5, and `pace()` delegates to the bucket.
"""

from __future__ import annotations

from brutex.adapters.dhan.rate_control import (
    CEILING_RPS,
    FLOOR_RPS,
    STEP_DOWN_HITS,
    STEP_DOWN_WINDOW,
    STEP_UP_WINDOW,
    DhanRateController,
)
from structlog.testing import capture_logs


class _Clock:
    """A controllable monotonic clock."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _controller(clock: _Clock) -> DhanRateController:
    return DhanRateController(clock=clock, sleep=lambda _s: None)


def _hammer(ctl: DhanRateController, clock: _Clock, *, hits: int, spread: float = 1.0) -> None:
    """Feed *hits* rate-limit hits, advancing the clock *spread* s between each."""
    for _ in range(hits):
        ctl.on_rate_limit()
        clock.advance(spread)


def test_starts_at_ceiling() -> None:
    ctl = _controller(_Clock())
    assert ctl.current_rps == CEILING_RPS  # 5.0
    assert ctl.snapshot() == {"current_rps": 5.0, "recent_hits": 0}


def test_step_down_5_to_4_to_3_on_sustained_429s() -> None:
    clock = _Clock()
    ctl = _controller(clock)
    _hammer(ctl, clock, hits=STEP_DOWN_HITS)  # 3 hits within 60s → 5→4
    assert ctl.current_rps == 4.0
    _hammer(ctl, clock, hits=STEP_DOWN_HITS)  # → 4→3
    assert ctl.current_rps == 3.0


def test_clamps_at_floor_never_below_3_no_halt() -> None:
    clock = _Clock()
    ctl = _controller(clock)
    _hammer(ctl, clock, hits=STEP_DOWN_HITS * 4)  # way more than enough to overshoot
    assert ctl.current_rps == FLOOR_RPS  # 3.0 — clamped, not 2 or a halt
    # A further sustained burst at the floor stays at 3.0 (no exception).
    _hammer(ctl, clock, hits=STEP_DOWN_HITS)
    assert ctl.current_rps == 3.0


def test_non_sustained_burst_does_not_step() -> None:
    clock = _Clock()
    ctl = _controller(clock)
    ctl.on_rate_limit()
    ctl.on_rate_limit()  # only 2 (< STEP_DOWN_HITS) → no step
    assert ctl.current_rps == 5.0
    assert ctl.snapshot()["recent_hits"] == 2


def test_window_eviction_old_hits_dont_count() -> None:
    clock = _Clock()
    ctl = _controller(clock)
    ctl.on_rate_limit()
    ctl.on_rate_limit()  # 2 hits
    clock.advance(STEP_DOWN_WINDOW + 1)  # both fall out of the window
    ctl.on_rate_limit()
    ctl.on_rate_limit()  # 2 fresh hits → still only 2 in-window → no step
    assert ctl.current_rps == 5.0


def test_step_up_3_to_4_to_5_on_recovery() -> None:
    clock = _Clock()
    ctl = _controller(clock)
    _hammer(ctl, clock, hits=STEP_DOWN_HITS * 2)  # down to 3.0
    assert ctl.current_rps == 3.0
    # A sustained clean window steps up 1 r/s each time.
    clock.advance(STEP_UP_WINDOW)
    ctl.on_success()
    assert ctl.current_rps == 4.0
    clock.advance(STEP_UP_WINDOW)
    ctl.on_success()
    assert ctl.current_rps == 5.0


def test_never_exceeds_ceiling() -> None:
    clock = _Clock()
    ctl = _controller(clock)
    # Already at 5.0; a clean window must not push past the ceiling.
    clock.advance(STEP_UP_WINDOW * 2)
    ctl.on_success()
    assert ctl.current_rps == 5.0


def test_on_success_blocked_by_recent_hit() -> None:
    clock = _Clock()
    ctl = _controller(clock)
    _hammer(ctl, clock, hits=STEP_DOWN_HITS)  # → 4.0 (and the buffer was cleared on the step)
    assert ctl.current_rps == 4.0
    ctl.on_rate_limit()  # a fresh in-window hit → recovery is blocked
    clock.advance(STEP_UP_WINDOW)
    ctl.on_success()
    assert ctl.current_rps == 4.0  # did NOT step up (recent hit present)


def test_on_success_blocked_before_clean_window() -> None:
    clock = _Clock()
    ctl = _controller(clock)
    for _ in range(STEP_DOWN_HITS):  # 3 same-instant hits → step to 4.0, last_step = now
        ctl.on_rate_limit()
    assert ctl.current_rps == 4.0
    clock.advance(STEP_UP_WINDOW - 1)  # just under the clean window
    ctl.on_success()
    assert ctl.current_rps == 4.0  # too soon → no step up


class TestThrottleLogging:
    """The throttle must be VISIBLE — every 429, every step down, every recovery."""

    def test_every_429_logs_rate_429(self) -> None:
        clock = _Clock()
        ctl = _controller(clock)
        with capture_logs() as logs:
            ctl.on_rate_limit()
        hit = [e for e in logs if e["event"] == "rate.429"]
        assert len(hit) == 1
        assert hit[0]["current_rps"] == 5.0
        assert hit[0]["recent_hits"] == 1

    def test_step_down_logs_from_and_to_rps(self) -> None:
        clock = _Clock()
        ctl = _controller(clock)
        with capture_logs() as logs:
            _hammer(ctl, clock, hits=STEP_DOWN_HITS)  # 5 → 4
        down = [e for e in logs if e["event"] == "rate.stepdown"]
        assert len(down) == 1
        assert down[0]["from_rps"] == 5.0
        assert down[0]["to_rps"] == 4.0
        assert down[0]["floor_rps"] == FLOOR_RPS

    def test_step_up_logs_recovery(self) -> None:
        clock = _Clock()
        ctl = _controller(clock)
        _hammer(ctl, clock, hits=STEP_DOWN_HITS)  # 5 → 4 (silent here)
        clock.advance(STEP_UP_WINDOW)
        with capture_logs() as logs:
            ctl.on_success()  # clean window → 4 → 5
        up = [e for e in logs if e["event"] == "rate.stepup"]
        assert len(up) == 1
        assert up[0]["from_rps"] == 4.0
        assert up[0]["to_rps"] == 5.0
        assert up[0]["ceiling_rps"] == CEILING_RPS


def test_pace_delegates_without_error() -> None:
    clock = _Clock()
    slept: list[float] = []
    ctl = DhanRateController(clock=clock, sleep=slept.append)
    ctl.pace()  # a token is available (burst) → no blocking sleep
    assert slept == []
