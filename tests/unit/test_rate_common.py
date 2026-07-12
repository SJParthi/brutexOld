"""Tests for `brutex.adapters.rate_common.RateController`.

100% line+branch with an injected fake clock (no real sleeping): starts at the
5 r/s ceiling, steps down 5→4→3 on sustained 429s, rests at the SOFT 3 r/s floor
on a brief burst but eases 3→2→1 under a SUSTAINED storm (DEC-DHAN-RATE-STORM-
FLOOR-001), ignores a non-sustained burst, evicts the window, recovers up on a
sustained clean window (incl. after a storm aged out — the on_success hits-evict
fix), never exceeds 5, and `pace()` delegates to the bucket.
"""
# pyright: reportPrivateUsage=none

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime
from itertools import pairwise
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from brutex.adapters import rate_common
from brutex.adapters.rate_common import (
    _GOVERNOR_LOOP_CAP,
    _SMOOTH_LOOP_CAP,
    CEILING_RPS,
    CLOCK_GAP_THRESHOLD_S,
    FLOOR_RPS,
    LOG_WINDOW_1S,
    LOG_WINDOW_60S,
    LOG_WINDOW_600S,
    LOG_WINDOW_3600S,
    PER_MIN_WINDOW,
    RATE_LIMIT_STORM_FLOOR_RPS,
    SMOOTH_PACING_ADAPTIVE,
    SMOOTH_PACING_OFF,
    SMOOTH_PACING_ON,
    SNAPSHOT_LOG_INTERVAL_S,
    STEP_DOWN_HITS,
    STEP_DOWN_TIMEOUT_HITS,
    STEP_DOWN_WINDOW,
    STEP_RPS,
    STEP_UP_WINDOW,
    STORM_HITS,
    TIMEOUT_FLOOR_RPS,
    TRAILING_LOG_BUCKETS,
    TRAILING_LOG_WINDOW_S,
    RateController,
)
from brutex.orchestrator.quota_tracker import DailyQuotaTracker
from structlog.testing import capture_logs

_IST = ZoneInfo("Asia/Kolkata")


def _quota(tmp_path: Path, *, limit: int, used: int) -> DailyQuotaTracker:
    """Build a real quota tracker (fixed IST clock) pre-advanced to *used* calls."""
    q = DailyQuotaTracker(
        daily_limit=limit,
        store_path=tmp_path / "q.lmdb",
        clock=lambda: datetime(2026, 6, 8, 12, 0, tzinfo=_IST),
    )
    for _ in range(used):
        q.acquire_or_raise()
    return q


class _Clock:
    """A controllable monotonic clock."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _controller(clock: _Clock) -> RateController:
    return RateController(clock=clock, sleep=lambda _s: None)


def _hammer(ctl: RateController, clock: _Clock, *, hits: int, spread: float = 1.0) -> None:
    """Feed *hits* rate-limit hits, advancing the clock *spread* s between each."""
    for _ in range(hits):
        ctl.on_rate_limit()
        clock.advance(spread)


def test_starts_at_ceiling() -> None:
    ctl = _controller(_Clock())
    assert ctl.current_rps == CEILING_RPS  # 5.0
    # Governor disabled by default → recent_minute 0, ceiling_per_min None. The expanded-
    # monitor trailing-window real counts are 0 on a fresh controller (nothing issued yet;
    # DEC-RATE-MONITOR-EXPAND-001).
    assert ctl.snapshot() == {
        "current_rps": 5.0,
        "recent_hits": 0,
        "recent_timeouts": 0,
        "recent_minute": 0,
        "ceiling_per_min": None,
        "recent_1s": 0,
        "recent_60s": 0,
        "recent_600s": 0,
        "recent_3600s": 0,
    }


def test_step_down_5_to_4_to_3_on_sustained_429s() -> None:
    clock = _Clock()
    ctl = _controller(clock)
    _hammer(ctl, clock, hits=STEP_DOWN_HITS)  # 3 hits within 60s → 5→4
    assert ctl.current_rps == 4.0
    _hammer(ctl, clock, hits=STEP_DOWN_HITS)  # → 4→3
    assert ctl.current_rps == 3.0


def test_brief_429_burst_clamps_at_soft_floor_3() -> None:
    # A BRIEF burst reaches the SOFT floor (3) quickly and rests there — a
    # sub-STORM_HITS burst never crawls below 3 (a quota nudge is no reason to crawl).
    clock = _Clock()
    ctl = _controller(clock)
    _hammer(ctl, clock, hits=STEP_DOWN_HITS * 4)  # 5→4→3, then a few more (< STORM_HITS)
    assert ctl.current_rps == FLOOR_RPS  # 3.0 — the soft floor, not 2
    _hammer(ctl, clock, hits=STEP_DOWN_HITS)  # still a brief burst at the floor → stays 3
    assert ctl.current_rps == FLOOR_RPS


def test_sustained_429_storm_eases_below_soft_floor_to_storm_floor() -> None:
    # A RELENTLESS 429 storm — every call rejected even at the soft floor (3) —
    # eases BELOW it to RATE_LIMIT_STORM_FLOOR_RPS (1) on the higher STORM_HITS
    # evidence threshold (DEC-DHAN-RATE-STORM-FLOOR-001), so the controller can
    # finally back off instead of spinning pinned at 3 getting 100%-rejected.
    clock = _Clock()
    ctl = _controller(clock)
    _hammer(ctl, clock, hits=STEP_DOWN_HITS * 2)  # 5→4→3 (the soft floor)
    assert ctl.current_rps == FLOOR_RPS
    _hammer(ctl, clock, hits=STORM_HITS)  # sustained at the floor → 3→2
    assert ctl.current_rps == 2.0
    _hammer(ctl, clock, hits=STORM_HITS)  # → 2→1
    assert ctl.current_rps == RATE_LIMIT_STORM_FLOOR_RPS  # 1.0
    assert RATE_LIMIT_STORM_FLOOR_RPS < FLOOR_RPS  # genuinely deeper than the soft floor


def test_429_clamps_at_storm_floor_never_below_1() -> None:
    clock = _Clock()
    ctl = _controller(clock)
    _hammer(ctl, clock, hits=STEP_DOWN_HITS * 2)  # → 3
    _hammer(ctl, clock, hits=STORM_HITS)  # → 2
    _hammer(ctl, clock, hits=STORM_HITS)  # → 1 (the storm floor)
    assert ctl.current_rps == RATE_LIMIT_STORM_FLOOR_RPS
    _hammer(ctl, clock, hits=STORM_HITS)  # at the storm floor: rps > floor is False → no step
    assert ctl.current_rps == RATE_LIMIT_STORM_FLOOR_RPS  # 1.0 — clamped, no halt


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


def test_mild_trickle_does_not_block_recovery() -> None:
    # DEC-DHAN-RATE-RECOVERY-TRICKLE-001 (the trickle-trap fix): a CHRONIC mild 429
    # drip BELOW the step-down trigger must NOT pin the rate forever. A single fresh
    # in-window hit — which previously blocked recovery indefinitely (`not
    # self._hits`) — now recovers, because one hit is nowhere near step-down-level
    # pressure (STEP_DOWN_HITS). This is the residual the #539 age-out fix left open
    # (it only recovered when the window fully cleared to ZERO).
    clock = _Clock()
    ctl = _controller(clock)
    _hammer(ctl, clock, hits=STEP_DOWN_HITS)  # → 4.0 (buffer cleared on the step)
    assert ctl.current_rps == 4.0
    clock.advance(STEP_UP_WINDOW)  # the clean-window TIME is satisfied
    ctl.on_rate_limit()  # a single fresh in-window hit — a mild drip, NOT a storm
    assert ctl.snapshot()["recent_hits"] == 1  # 1 < STEP_DOWN_HITS → not blocking
    ctl.on_success()  # recovery proceeds DESPITE the lone in-window drip
    assert ctl.current_rps == 5.0  # stepped up — the trickle-trap is closed


def test_step_down_pressure_at_floor_blocks_recovery() -> None:
    # The False arm of the hit gate (DEC-DHAN-RATE-RECOVERY-TRICKLE-001): at/below
    # the floor — where on_rate_limit no longer clears _hits on every step (it needs
    # STORM_HITS there) — a drip that REACHES the step-down trigger
    # (>= STEP_DOWN_HITS in the rolling window) is genuine pressure and HOLDS the
    # rate down. Recovery does NOT fire even though the STEP_UP_WINDOW timer is met.
    clock = _Clock()
    ctl = _controller(clock)
    _hammer(ctl, clock, hits=STEP_DOWN_HITS * 2)  # → 3.0 (the soft floor), last_step = now
    assert ctl.current_rps == FLOOR_RPS
    clock.advance(STEP_UP_WINDOW)  # timer satisfied AND the setup hits age out of the window
    for _ in range(STEP_DOWN_HITS):  # a FRESH step-down-level drip, in-window; at 3 r/s → no step
        ctl.on_rate_limit()
    assert ctl.snapshot()["recent_hits"] == STEP_DOWN_HITS  # genuine pressure sitting in-window
    ctl.on_success()  # timer met, but len(_hits) >= STEP_DOWN_HITS → recovery HELD
    assert ctl.current_rps == FLOOR_RPS  # did NOT step up — real pressure holds it down


def test_timeout_pressure_at_floor_blocks_recovery() -> None:
    # The False arm of the TIMEOUT gate (DEC-DHAN-RATE-RECOVERY-TRICKLE-001):
    # symmetric to the 429 case — a sustained timeout drip at the timeout floor (1)
    # that reaches STEP_DOWN_TIMEOUT_HITS in-window holds the rate down, even with
    # zero 429s and the STEP_UP_WINDOW timer met.
    clock = _Clock()
    ctl = _controller(clock)
    _hammer_timeouts(ctl, clock, hits=STEP_DOWN_TIMEOUT_HITS * 4)  # ease to the timeout floor (1.0)
    assert ctl.current_rps == TIMEOUT_FLOOR_RPS
    clock.advance(STEP_UP_WINDOW)  # timer satisfied AND the setup timeouts age out
    for _ in range(STEP_DOWN_TIMEOUT_HITS):  # fresh timeout drip, in-window; at 1 r/s → no step
        ctl.on_timeout()
    assert ctl.snapshot()["recent_timeouts"] == STEP_DOWN_TIMEOUT_HITS
    ctl.on_success()  # timer met, but len(_timeouts) >= STEP_DOWN_TIMEOUT_HITS → recovery HELD
    assert ctl.current_rps == TIMEOUT_FLOOR_RPS  # held at the timeout floor by the drip


def test_on_success_blocked_before_clean_window() -> None:
    clock = _Clock()
    ctl = _controller(clock)
    for _ in range(STEP_DOWN_HITS):  # 3 same-instant hits → step to 4.0, last_step = now
        ctl.on_rate_limit()
    assert ctl.current_rps == 4.0
    clock.advance(STEP_UP_WINDOW - 1)  # just under the clean window
    ctl.on_success()
    assert ctl.current_rps == 4.0  # too soon → no step up


def test_recovery_unblocked_after_storm_hits_age_out() -> None:
    # The recovery-bug fix (DEC-DHAN-RATE-STORM-FLOOR-001): a 429 storm pins _hits
    # at the floor; on_success must EVICT the aged hits so recovery proceeds —
    # previously on_success evicted only _timeouts, so _hits stayed pinned and the
    # rate was STUCK at the floor for the rest of the pull, never recovering to 5.
    clock = _Clock()
    ctl = _controller(clock)
    _hammer(ctl, clock, hits=STEP_DOWN_HITS * 2)  # → 3.0 (soft floor; _hits cleared on the step)
    # A sub-STORM_HITS burst at the floor pins _hits (no further step, never cleared).
    _hammer(ctl, clock, hits=5)
    assert ctl.snapshot()["recent_hits"] == 5  # pinned — would block recovery forever pre-fix
    clock.advance(
        STEP_UP_WINDOW
    )  # storm clears: the 5 hits age out (> STEP_DOWN_WINDOW) + clean window
    ctl.on_success()  # evicts the aged hits → len(_hits) < STEP_DOWN_HITS → recovery 3→4
    assert ctl.current_rps == 4.0
    assert ctl.snapshot()["recent_hits"] == 0  # the aged 429 hits were evicted by on_success


def _hammer_timeouts(ctl: RateController, clock: _Clock, *, hits: int, spread: float = 1.0) -> None:
    """Feed *hits* server-stress stalls, advancing the clock *spread* s between each."""
    for _ in range(hits):
        ctl.on_timeout()
        clock.advance(spread)


class TestTimeoutLadder:
    """The gentle adaptive TIMEOUT step-down (DEC-DHAN-TIMEOUT-BACKOFF-001).

    A sustained timeout storm eases 5→4→3→2→1 r/s (separate, more tolerant
    threshold than the 429 ladder) — down to the DEEPER timeout floor
    (DEC-DHAN-OPTION-TIMEOUT-STORM-001), below the 429 floor — ignores a one-off /
    non-sustained blip, evicts its window, and lets :meth:`on_success` recover
    only once the storm has cleared.
    """

    def test_step_down_5_to_4_to_3_on_sustained_timeouts(self) -> None:
        clock = _Clock()
        ctl = _controller(clock)
        _hammer_timeouts(ctl, clock, hits=STEP_DOWN_TIMEOUT_HITS)  # 5 within 60s → 5→4
        assert ctl.current_rps == 4.0
        _hammer_timeouts(ctl, clock, hits=STEP_DOWN_TIMEOUT_HITS)  # → 4→3
        assert ctl.current_rps == 3.0

    def test_eases_below_429_floor_to_timeout_floor(self) -> None:
        # A relentless server-stress storm eases PAST the 429 floor (3) down to the
        # deeper TIMEOUT_FLOOR_RPS (1), so a struggling endpoint can recover
        # (DEC-DHAN-OPTION-TIMEOUT-STORM-001).
        clock = _Clock()
        ctl = _controller(clock)
        for expected in (4.0, 3.0, 2.0, 1.0):
            _hammer_timeouts(ctl, clock, hits=STEP_DOWN_TIMEOUT_HITS)
            assert ctl.current_rps == expected
        assert TIMEOUT_FLOOR_RPS < FLOOR_RPS  # the timeout floor is genuinely deeper

    def test_clamps_at_timeout_floor_never_below(self) -> None:
        clock = _Clock()
        ctl = _controller(clock)
        _hammer_timeouts(ctl, clock, hits=STEP_DOWN_TIMEOUT_HITS * 4)  # overshoot → 1.0
        assert ctl.current_rps == TIMEOUT_FLOOR_RPS  # 1.0 — clamped, not 0 or a halt
        _hammer_timeouts(ctl, clock, hits=STEP_DOWN_TIMEOUT_HITS)  # already floor → no halt
        assert ctl.current_rps == TIMEOUT_FLOOR_RPS

    def test_429_after_timeout_floor_does_not_step_below(self) -> None:
        # Worst case: a timeout storm already eased to 1.0, then a 429 storm lands.
        # The 429 ladder's floor is the HIGHER 3.0, so it must not step further —
        # no zero/negative rate, no halt (the two ladders share one _rps cleanly).
        clock = _Clock()
        ctl = _controller(clock)
        _hammer_timeouts(ctl, clock, hits=STEP_DOWN_TIMEOUT_HITS * 4)  # → 1.0
        assert ctl.current_rps == TIMEOUT_FLOOR_RPS
        _hammer(ctl, clock, hits=STEP_DOWN_HITS * 2)  # a 429 storm at the deep floor
        assert ctl.current_rps == TIMEOUT_FLOOR_RPS  # unchanged — 429 floor (3) is higher

    def test_non_sustained_blip_does_not_step(self) -> None:
        clock = _Clock()
        ctl = _controller(clock)
        ctl.on_timeout()
        ctl.on_timeout()  # only 2 (< STEP_DOWN_TIMEOUT_HITS) → no step (no speed loss)
        assert ctl.current_rps == CEILING_RPS
        assert ctl.snapshot()["recent_timeouts"] == 2

    def test_window_eviction_old_stalls_dont_count(self) -> None:
        clock = _Clock()
        ctl = _controller(clock)
        ctl.on_timeout()
        ctl.on_timeout()  # 2 stalls at t0
        clock.advance(STEP_DOWN_WINDOW + 2.0)  # both age strictly out of the window
        for _ in range(STEP_DOWN_TIMEOUT_HITS - 1):  # 4 fresh, same instant
            ctl.on_timeout()
        # the 2 stale were evicted → only 4 in-window (< 5) → no step
        assert ctl.current_rps == CEILING_RPS
        assert ctl.snapshot()["recent_timeouts"] == STEP_DOWN_TIMEOUT_HITS - 1

    def test_mild_timeout_drip_does_not_block_recovery(self) -> None:
        # DEC-DHAN-RATE-RECOVERY-TRICKLE-001 (timeout side): a single fresh stall —
        # a mild drip far below STEP_DOWN_TIMEOUT_HITS — must NOT pin the rate. It
        # previously blocked recovery indefinitely (`not self._timeouts`); now
        # recovery proceeds because one stall is nowhere near step-down pressure.
        # (Genuine step-down-level timeout pressure is held by
        # test_timeout_pressure_at_floor_blocks_recovery.)
        clock = _Clock()
        ctl = _controller(clock)
        _hammer_timeouts(ctl, clock, hits=STEP_DOWN_TIMEOUT_HITS)  # → 4.0 (deque cleared)
        assert ctl.current_rps == 4.0
        clock.advance(STEP_UP_WINDOW + 1.0)  # the clean window has elapsed
        ctl.on_timeout()  # a single fresh stall — a mild drip, not a storm
        assert ctl.snapshot()["recent_timeouts"] == 1  # 1 < STEP_DOWN_TIMEOUT_HITS
        ctl.on_success()  # recovery proceeds DESPITE the lone in-window stall
        assert ctl.current_rps == CEILING_RPS  # stepped 4→5 — the trickle-trap is closed

    def test_recovery_unblocked_after_timeout_drip_ages_out(self) -> None:
        # Symmetric to the 429 age-out (test_recovery_unblocked_after_storm_hits_age_out):
        # a sub-threshold timeout drip pins _timeouts at the rate; once the stalls
        # age out of the rolling window, on_success EVICTS them and recovery proceeds
        # (DEC-DHAN-RATE-RECOVERY-TRICKLE-001 + DEC-DHAN-RATE-STORM-FLOOR-001).
        clock = _Clock()
        ctl = _controller(clock)
        _hammer_timeouts(
            ctl, clock, hits=STEP_DOWN_TIMEOUT_HITS
        )  # → 4.0 (deque cleared on the step)
        _hammer_timeouts(
            ctl, clock, hits=STEP_DOWN_TIMEOUT_HITS - 1
        )  # 4 sub-threshold stalls pin it
        assert ctl.snapshot()["recent_timeouts"] == STEP_DOWN_TIMEOUT_HITS - 1
        clock.advance(STEP_UP_WINDOW)  # the stalls age out (> STEP_DOWN_WINDOW) + clean window
        ctl.on_success()  # evicts the aged timeouts → recovery 4→5
        assert ctl.current_rps == CEILING_RPS
        assert ctl.snapshot()["recent_timeouts"] == 0  # the aged stalls were evicted


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

    def test_storm_step_below_soft_floor_logs_deep_storm_floor(self) -> None:
        # A sustained-storm step BELOW the soft floor (3→2) must log the EFFECTIVE
        # floor for that rung — the deep RATE_LIMIT_STORM_FLOOR_RPS (1), not the
        # soft FLOOR_RPS (3) — so the operator sees the controller is now easing
        # into the storm regime (DEC-DHAN-RATE-STORM-FLOOR-001).
        clock = _Clock()
        ctl = _controller(clock)
        _hammer(ctl, clock, hits=STEP_DOWN_HITS * 2)  # 5→4→3 (now AT the soft floor)
        assert ctl.current_rps == FLOOR_RPS
        with capture_logs() as logs:
            _hammer(ctl, clock, hits=STORM_HITS)  # sustained storm → 3→2
        down = [e for e in logs if e["event"] == "rate.stepdown"]
        assert len(down) == 1
        assert down[0]["from_rps"] == 3.0
        assert down[0]["to_rps"] == 2.0
        assert down[0]["floor_rps"] == RATE_LIMIT_STORM_FLOOR_RPS  # deep floor, not 3

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

    def test_every_timeout_logs_rate_timeout(self) -> None:
        clock = _Clock()
        ctl = _controller(clock)
        with capture_logs() as logs:
            ctl.on_timeout()
        hit = [e for e in logs if e["event"] == "rate.timeout"]
        assert len(hit) == 1
        assert hit[0]["current_rps"] == 5.0
        assert hit[0]["recent_timeouts"] == 1

    def test_timeout_step_down_logs_cause_timeout(self) -> None:
        clock = _Clock()
        ctl = _controller(clock)
        with capture_logs() as logs:
            _hammer_timeouts(ctl, clock, hits=STEP_DOWN_TIMEOUT_HITS)  # 5 → 4
        down = [e for e in logs if e["event"] == "rate.stepdown"]
        assert len(down) == 1
        assert down[0]["from_rps"] == 5.0
        assert down[0]["to_rps"] == 4.0
        assert down[0]["cause"] == "timeout"


def test_pace_delegates_without_error() -> None:
    # HARD-CAP (DEC-DHAN-RATE-HARD-CAP-001): the session bucket starts EMPTY, so the
    # very first paced call waits one 1/5 s interval — no free opening token that
    # would push the physical rate over the 5 r/s ceiling.
    clock = _Clock()
    slept: list[float] = []

    def _sleep(seconds: float) -> None:
        slept.append(seconds)
        clock.advance(seconds)  # sleeping advances time → the bucket refills

    ctl = RateController(clock=clock, sleep=_sleep)
    ctl.pace()
    assert slept == [1.0 / CEILING_RPS]  # first call paced, not a free burst


def test_eases_in_no_opening_burst() -> None:
    # HARD-CAP (DEC-DHAN-RATE-HARD-CAP-001) hardens EASE-IN (DEC-DHAN-EASE-IN-001):
    # the bucket now starts EMPTY, so NEITHER the opening call NOR the next one fires
    # free — both are paced at 1/5 s, so the PHYSICAL rate never exceeds 5 r/s even
    # in the opening burst that previously triggered the 429 storm.
    clock = _Clock()
    slept: list[float] = []

    def _sleep(seconds: float) -> None:
        slept.append(seconds)
        clock.advance(seconds)  # sleeping advances time → the bucket refills

    ctl = RateController(clock=clock, sleep=_sleep)
    ctl.pace()  # 1st: empty bucket → paced
    ctl.pace()  # 2nd: still paced at the ceiling
    assert slept == [1.0 / CEILING_RPS, 1.0 / CEILING_RPS]


def test_stepdown_drains_to_empty_no_free_burst() -> None:
    # HARD-CAP (DEC-DHAN-RATE-HARD-CAP-001): a 5→4 stepdown changes the bucket's
    # rate IN PLACE (DEC-DHAN-RATE-SINGLE-BUCKET-001) and drains it to empty, so the
    # stepdown can't release a free burst token at the lower rate. The first paced
    # call after the step waits a full 1/4 s (0 elapsed since the change) — the
    # physical rate stays strictly ≤ the new rate.
    clock = _Clock()
    slept: list[float] = []

    def _sleep(seconds: float) -> None:
        slept.append(seconds)
        clock.advance(seconds)

    ctl = RateController(clock=clock, sleep=_sleep)
    for _ in range(STEP_DOWN_HITS):  # 3 hits at the same instant → 5→4 (in place)
        ctl.on_rate_limit()
    assert ctl.current_rps == 4.0
    ctl.pace()  # drained bucket + 0 elapsed → waits a full 1/4 s
    assert slept == [1.0 / 4.0]


def test_steps_preserve_single_bucket_identity() -> None:
    # DEC-DHAN-RATE-SINGLE-BUCKET-001 — the ROOT-CAUSE regression for the operator's
    # ``rate.breach rps_1s=11`` opening transient. A per-step bucket REBUILD let
    # in-flight ``pace`` workers (on the OLD bucket) and new workers (on the NEW
    # bucket) run two independent token streams that stacked POSTs over the cap.
    # The fix mutates ONE persistent bucket in place, so its object identity is
    # STABLE across every 429 stepdown, timeout stepdown AND recovery stepup —
    # there is therefore only ever a single ≤ current_rps token stream.
    clock = _Clock()
    ctl = _controller(clock)
    original = ctl._bucket

    _hammer(ctl, clock, hits=STEP_DOWN_HITS)  # 5→4 (429 ladder)
    assert ctl.current_rps == 4.0
    assert ctl._bucket is original

    _hammer_timeouts(ctl, clock, hits=STEP_DOWN_TIMEOUT_HITS)  # 4→3 (timeout ladder)
    assert ctl.current_rps == 3.0
    assert ctl._bucket is original

    clock.advance(STEP_UP_WINDOW)
    ctl.on_success()  # 3→4 recovery stepup
    assert ctl.current_rps == 4.0
    assert ctl._bucket is original

    # The single shared bucket now paces at the recovered rate (proof the same
    # object carries the new rate, not a stale clone).
    assert ctl._bucket.current_rps() == 4.0


class TestQuotaWiring:
    """The daily-quota gauge + brake (DEC-AUTO-RESUME-QUOTA-001)."""

    def test_snapshot_includes_quota_when_tracker_wired(self, tmp_path: Path) -> None:
        ctl = RateController(
            clock=_Clock(), sleep=lambda _s: None, quota_tracker=_quota(tmp_path, limit=10, used=4)
        )
        snap = ctl.snapshot()
        assert snap["current_rps"] == 5.0  # base rate fields still present
        assert snap["quota_used"] == 4
        assert snap["quota_remaining"] == 6
        assert snap["quota_limit"] == 10

    def test_snapshot_omits_quota_without_tracker(self) -> None:
        # The default (test) controller has no tracker → no quota keys; the base rate
        # fields + the always-present governor gauges + the expanded-monitor trailing-window
        # real counts (DEC-RATE-MONITOR-EXPAND-001) are the full set.
        assert set(_controller(_Clock()).snapshot()) == {
            "current_rps",
            "recent_hits",
            "recent_timeouts",
            "recent_minute",
            "ceiling_per_min",
            "recent_1s",
            "recent_60s",
            "recent_600s",
            "recent_3600s",
        }

    def test_quota_exhausted_true_at_limit(self, tmp_path: Path) -> None:
        ctl = RateController(
            clock=_Clock(), sleep=lambda _s: None, quota_tracker=_quota(tmp_path, limit=2, used=2)
        )
        assert ctl.quota_exhausted() is True

    def test_quota_exhausted_false_with_remaining(self, tmp_path: Path) -> None:
        ctl = RateController(
            clock=_Clock(), sleep=lambda _s: None, quota_tracker=_quota(tmp_path, limit=5, used=1)
        )
        assert ctl.quota_exhausted() is False

    def test_quota_exhausted_false_without_tracker(self) -> None:
        # No tracker wired (the bucket-only mode) → never the limit.
        assert _controller(_Clock()).quota_exhausted() is False


#: A high per-second ceiling (10 r/s ⇒ 600/min) used to prove the per-minute cap is
#: the binding gate. Mirrors the UNVERIFIED Groww backtesting r/s without importing
#: the vendor constant into this vendor-neutral controller's tests.
GROWW_CEILING: float = 10.0


def _governed(
    cap: int | None, *, ceiling_rps: float = CEILING_RPS
) -> tuple[RateController, _Clock, list[float]]:
    """Build a governed controller + its clock + a sleep-log that advances the clock."""
    clock = _Clock()
    slept: list[float] = []

    def _sleep(seconds: float) -> None:
        slept.append(seconds)
        clock.advance(seconds)

    ctl = RateController(clock=clock, sleep=_sleep, ceiling_rps=ceiling_rps, ceiling_per_min=cap)
    return ctl, clock, slept


class TestPerMinuteGovernor:
    """The per-minute sliding-window governor (DEC-GROWW-RATE-PERMIN-001)."""

    def test_disabled_skips_governor(self) -> None:
        # Default controller: cap None → pace() touches neither governor phase, the
        # issue deque stays empty, and the snapshot gauges report the disabled state.
        ctl, _clock, slept = _governed(None)
        ctl.pace()
        ctl.pace()
        assert slept == [1.0 / CEILING_RPS, 1.0 / CEILING_RPS]  # only the bucket paced
        assert len(ctl._issues) == 0
        snap = ctl.snapshot()
        assert snap["recent_minute"] == 0
        assert snap["ceiling_per_min"] is None

    def test_wait_disabled_returns_zero(self) -> None:
        # The helper is safe to call regardless of cap: None ⇒ always 0.0 (no governor).
        ctl = _controller(_Clock())  # cap None
        assert ctl._governor_wait_locked(ctl._clock()) == 0.0

    def test_empty_window_has_room(self) -> None:
        # cap=3, fresh window: the first pace sees 0 governor wait (empty deque), so it
        # only pays the bucket's opening 1/rps; recent_minute + ceiling_per_min surface.
        ctl, _clock, slept = _governed(3)
        ctl.pace()
        assert slept == [1.0 / CEILING_RPS]  # governor added no wait
        snap = ctl.snapshot()
        assert snap["recent_minute"] == 1
        assert snap["ceiling_per_min"] == 3

    def test_blocks_at_cap_then_frees(self) -> None:
        # cap=2 at 5 r/s: the 3rd pace fills the window, so the governor BLOCKS until the
        # oldest issue ages out, then admits exactly one — never overshooting. The issue
        # instant is RESERVED at admission (DEC-GROWW-GOVERNOR-ATOMIC-001), so the two
        # issues are stamped @1000.0 + @1000.2 (the pre-bucket clock), and the 3rd pace
        # waits (1000.0+60)-1000.4 = 59.6 (the oldest's age-out).
        ctl, _clock, slept = _governed(2)
        ctl.pace()  # issue reserved @1000.0 (admission), then bucket 0.2 → clock 1000.2
        ctl.pace()  # issue reserved @1000.2 (admission), then bucket 0.2 → clock 1000.4
        assert slept == [0.2, 0.2]
        assert ctl.snapshot()["recent_minute"] == 2
        ctl.pace()  # window full → governor waits (1000.0+60)-1000.4 = 59.6, then frees one
        assert any(abs(s - 59.6) < 1e-6 for s in slept)  # the governor block fired
        assert ctl.snapshot()["recent_minute"] == 2  # cap held, no overshoot

    def test_concurrent_admission_never_overshoots_cap(self) -> None:
        # DEC-GROWW-GOVERNOR-ATOMIC-001 regression: N workers contending for a governed
        # window must NEVER overshoot the cap. Pre-fix, the free-slot CHECK and the issue
        # APPEND were in two separate lock acquisitions (with bucket.acquire between), so
        # concurrent workers could all pass one free slot and each append → cap + (N-1).
        # The fix reserves the slot in the SAME lock that sees room, so the reservation is
        # atomic. This probes the invariant under REAL threads: the deque length captured
        # the instant each reservation lands (under the controller lock) is never > cap.
        import threading
        from collections import deque as _deque

        cap = 4
        n_workers = 16
        cl = threading.Lock()
        t = [1000.0]

        def clock() -> float:
            with cl:
                return t[0]

        def sleep(s: float) -> None:
            with cl:  # the shared test clock advances on every sleep (thread-safe)
                t[0] += s

        ctl = RateController(clock=clock, sleep=sleep, ceiling_rps=1000.0, ceiling_per_min=cap)
        # Swap in a deque that records the post-append length AT the reservation instant —
        # the append fires inside the controller's lock, so this captures the true
        # concurrent count (deque.append can't be monkeypatched on an instance; subclass).
        max_seen = [0]
        rec_lock = threading.Lock()

        class _RecordingDeque(_deque):  # type: ignore[type-arg]
            def append(self, item: object) -> None:
                super().append(item)
                with rec_lock:
                    max_seen[0] = max(max_seen[0], len(self))

        ctl._issues = _RecordingDeque()  # type: ignore[assignment]

        barrier = threading.Barrier(n_workers)

        def worker() -> None:
            barrier.wait()  # release all workers together to maximise contention
            ctl.pace()

        threads = [threading.Thread(target=worker) for _ in range(n_workers)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=10.0)
        assert all(not th.is_alive() for th in threads)  # none hung
        assert max_seen[0] <= cap  # the cap is NEVER overshot at any reservation instant

    def test_window_roll_evicts_all(self) -> None:
        # cap=2 filled, then >60 s passes with no issues → the next pace evicts BOTH
        # aged issues in one sweep (the while-loop runs twice) and admits freely.
        ctl, clock, _slept = _governed(2)
        ctl.pace()
        ctl.pace()
        assert ctl.snapshot()["recent_minute"] == 2
        clock.advance(PER_MIN_WINDOW + 1.0)  # whole window ages out
        ctl.pace()
        assert ctl.snapshot()["recent_minute"] == 1  # both old evicted, one fresh

    def test_loop_cap_fails_open(self) -> None:
        # A pathological clock that NEVER advances (sleep is a no-op): the governor's
        # re-check loop must not spin forever — it breaks after _GOVERNOR_LOOP_CAP and
        # fails OPEN (admits the call), mirroring the bucket's acquire cap.
        clock = _Clock()
        slept: list[float] = []
        ctl = RateController(
            clock=clock, sleep=slept.append, ceiling_rps=CEILING_RPS, ceiling_per_min=1
        )
        ctl.pace()  # records one issue @1000.0 (bucket also fails open on the frozen clock)
        slept.clear()
        ctl.pace()  # window full + frozen clock → governor spins to the cap, then fails open
        # Each governor iteration computes wait == PER_MIN_WINDOW (oldest+60-now, now frozen).
        assert slept.count(PER_MIN_WINDOW) == _GOVERNOR_LOOP_CAP
        assert ctl.snapshot()["recent_minute"] == 2  # fail-open admitted the extra call

    def test_orthogonal_to_stepdown(self) -> None:
        # The per-second AIMD ladder and the per-minute cap share no mutable state: a
        # 429 storm steps _rps down but leaves ceiling_per_min + the issue deque intact.
        ctl, clock, _slept = _governed(300, ceiling_rps=GROWW_CEILING)
        ctl.pace()
        before = ctl.snapshot()["recent_minute"]
        _hammer(ctl, clock, hits=STEP_DOWN_HITS)  # storm → step down
        assert ctl.current_rps < GROWW_CEILING  # ladder moved
        snap = ctl.snapshot()
        assert snap["ceiling_per_min"] == 300  # cap untouched by the ladder
        assert snap["recent_minute"] == before  # on_rate_limit never touched _issues

    def test_snapshot_evicts_aged_issues(self) -> None:
        # snapshot() evicts aged issues before reporting recent_minute so the gauge is
        # an accurate rolling-60s count even with no intervening pace().
        ctl, clock, _slept = _governed(5)
        ctl.pace()
        ctl.pace()
        assert ctl.snapshot()["recent_minute"] == 2
        clock.advance(PER_MIN_WINDOW + 1.0)
        assert ctl.snapshot()["recent_minute"] == 0  # both aged out on read

    def test_cap_binds_over_high_rps(self) -> None:
        # cap=3 with a 10 r/s ceiling (would otherwise allow 600/min): the governor is
        # the binding gate — recent_minute never exceeds the cap across many paces.
        ctl, _clock, _slept = _governed(3, ceiling_rps=GROWW_CEILING)
        for _ in range(12):
            ctl.pace()
            recent = ctl.snapshot()["recent_minute"]
            assert isinstance(recent, int) and recent <= 3


# ── DEC-GROWW-RATE-SNAPSHOT-LOG-001 — periodic rate.snapshot telemetry ───────────
def test_pace_emits_throttled_rate_snapshot() -> None:
    """`pace` logs a `rate.snapshot` at most once per SNAPSHOT_LOG_INTERVAL_S.

    The visibility gap (DEC-GROWW-RATE-SNAPSHOT-LOG-001): rate.* events fire only on a
    rung change, so a HOLDING rate logs nothing. The first pace (warm-up grace, < the
    interval since construction) emits no snapshot; after the interval elapses the next
    pace emits exactly one, carrying the dashboard-panel gauges.
    """
    clock = _Clock()
    ctl = _controller(clock)
    with capture_logs() as logs1:
        ctl.pace()  # t == construction time → within the interval → no snapshot
    assert [e for e in logs1 if e["event"] == "rate.snapshot"] == []
    clock.advance(SNAPSHOT_LOG_INTERVAL_S)  # bucket refills + the throttle window elapses
    with capture_logs() as logs2:
        ctl.pace()
    snaps = [e for e in logs2 if e["event"] == "rate.snapshot"]
    assert len(snaps) == 1
    assert snaps[0]["current_rps"] == CEILING_RPS
    assert snaps[0]["recent_hits"] == 0
    assert snaps[0]["recent_timeouts"] == 0
    assert snaps[0]["recent_minute"] == 0
    assert snaps[0]["ceiling_per_min"] is None


# ── DEC-GROWW-SMOOTH-PACING-001 — optional anti-burst smooth pacing ──────────────
def _smooth(
    *, ceiling_rps: float = CEILING_RPS, ceiling_per_min: int | None = None
) -> tuple[RateController, _Clock, list[float]]:
    """Build a smooth-paced controller + its clock + a clock-advancing sleep-log."""
    clock = _Clock()
    slept: list[float] = []

    def _sleep(seconds: float) -> None:
        slept.append(seconds)
        clock.advance(seconds)

    ctl = RateController(
        clock=clock,
        sleep=_sleep,
        ceiling_rps=ceiling_rps,
        ceiling_per_min=ceiling_per_min,
        smooth_pacing=True,
    )
    return ctl, clock, slept


class TestSmoothPacing:
    """The OPTIONAL anti-burst smooth-pacing gate (DEC-GROWW-SMOOTH-PACING-001)."""

    def test_default_off_is_byte_identical(self) -> None:
        # Default controller: smooth OFF → the smooth gate is skipped entirely, the
        # spacing baseline is never stamped, and pace() pays ONLY the bucket's 1/rps —
        # byte-for-byte identical to the prior two-gate behaviour.
        clock = _Clock()
        slept: list[float] = []

        def _sleep(seconds: float) -> None:
            slept.append(seconds)
            clock.advance(seconds)

        ctl = RateController(clock=clock, sleep=_sleep)  # smooth OFF (default)
        ctl.pace()
        ctl.pace()
        assert slept == [1.0 / CEILING_RPS, 1.0 / CEILING_RPS]  # only the bucket paced
        assert ctl._last_issue_ts == float("-inf")  # smooth baseline never touched
        assert ctl._smooth_pacing is False

    def test_smooth_wait_disabled_branch_via_first_issue(self) -> None:
        # The FIRST smooth-paced issue never waits: _last_issue_ts starts -inf so the
        # spacing gate computes a non-positive wait → 0.0 (the <= 0 branch) and stamps.
        ctl, _clock, _slept = _smooth(ceiling_rps=8.0)
        assert ctl._smooth_wait_locked(ctl._clock()) == 0.0  # -inf baseline → no wait
        ctl.pace()
        assert ctl._last_issue_ts == 1000.0  # stamped at admission

    def test_issues_evenly_spaced_at_min_interval(self) -> None:
        # Smooth ON at 8 r/s: consecutive issues are stamped exactly 1/8 = 0.125 s apart,
        # so the per-second issue count can never exceed the rate (no sub-second burst).
        ctl, _clock, _slept = _smooth(ceiling_rps=8.0)
        stamps: list[float] = []
        for _ in range(20):
            ctl.pace()
            stamps.append(ctl._last_issue_ts)
        gaps = [round(b - a, 6) for a, b in pairwise(stamps)]
        assert gaps == [0.125] * 19  # uniform spacing at the min interval
        # max issues in ANY rolling 1-second window <= the AIMD rate (8).
        worst = max(sum(1 for s in stamps if t - 1.0 < s <= t) for t in stamps)
        assert worst <= 8

    def test_per_second_count_never_exceeds_rate_16_threads(self) -> None:
        # Anti-burst guarantee under REAL contention: 16 workers smooth-paced at 8 r/s
        # must spread issuance so NO 1-second window ever holds more than 8 issues —
        # the rps_1s_max <= current_rps property (no 9-burst), proven concurrently.
        import threading

        cl = threading.Lock()
        t = [1000.0]

        def clock() -> float:
            with cl:
                return t[0]

        def sleep(s: float) -> None:
            with cl:
                t[0] += s

        ctl = RateController(
            clock=clock,
            sleep=sleep,
            ceiling_rps=8.0,
            ceiling_per_min=500,
            smooth_pacing=True,
        )
        stamps: list[float] = []
        stamp_lock = threading.Lock()

        def worker() -> None:
            for _ in range(4):
                ctl.pace()
                with stamp_lock:
                    stamps.append(ctl._last_issue_ts)

        threads = [threading.Thread(target=worker) for _ in range(16)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=10.0)
        assert all(not th.is_alive() for th in threads)  # none hung
        # Every issue is at least 1/8 s after the previous → at most 8 per 1 s window.
        worst = max(sum(1 for s in stamps if t0 - 1.0 < s <= t0) for t0 in stamps)
        assert worst <= 8  # rps_1s never exceeds the current AIMD rate

    def test_caps_still_hold_under_smooth_governor(self) -> None:
        # Smooth ADDS spacing but never bypasses the per-minute governor: cap=4 at 8 r/s
        # → recent_minute never exceeds 4 across many paces (the HARD 500-style cap holds).
        ctl, _clock, _slept = _smooth(ceiling_rps=8.0, ceiling_per_min=4)
        for _ in range(12):
            ctl.pace()
            recent = ctl.snapshot()["recent_minute"]
            assert isinstance(recent, int) and recent <= 4

    def test_aimd_step_down_widens_spacing(self) -> None:
        # AIMD ladder intact under smooth mode: a sustained 429 storm steps 5→4 r/s, and
        # the smooth interval immediately tracks the LIVE rate (1/4 s after the step).
        ctl, clock, _slept = _smooth(ceiling_rps=CEILING_RPS)
        ctl.pace()  # stamp @1000.0 at 5 r/s
        _hammer(ctl, clock, hits=STEP_DOWN_HITS)  # 5→4 (same ladder, unchanged)
        assert ctl.current_rps == 4.0
        # With the rate now 4, the min interval is 1/4 = 0.25 (was 1/5 = 0.20).
        assert ctl._smooth_wait_locked(ctl._last_issue_ts) == 0.25

    def test_aimd_step_up_narrows_spacing(self) -> None:
        # AIMD recovery intact under smooth mode: after a clean STEP_UP_WINDOW the rate
        # recovers 3→4, and the smooth interval narrows to track the higher live rate.
        ctl, clock, _slept = _smooth(ceiling_rps=CEILING_RPS)
        ctl.pace()  # stamp _last_issue_ts at the live clock (5 r/s)
        _hammer(ctl, clock, hits=STEP_DOWN_HITS)  # 5→4
        _hammer(ctl, clock, hits=STEP_DOWN_HITS)  # 4→3
        assert ctl.current_rps == 3.0
        clock.advance(STEP_UP_WINDOW)
        ctl.on_success()  # 3→4 recovery (same ladder)
        assert ctl.current_rps == 4.0
        # Evaluate the spacing AT the last issue instant: the wait to the next issue is
        # the full 1/4 = 0.25 s interval (narrowed from 1/3 at the recovered rate).
        assert ctl._smooth_wait_locked(ctl._last_issue_ts) == 0.25  # 1/4, narrowed from 1/3

    def test_loop_cap_fails_open_on_frozen_clock(self) -> None:
        # A pathological clock that NEVER advances (sleep is a no-op): the smooth gate's
        # re-test loop must not spin forever — it breaks after _SMOOTH_LOOP_CAP and
        # fails OPEN (stamps + admits), mirroring the governor's loop cap. The smooth
        # gate's wait is the EXACT float ``(baseline + interval) − now`` it computes; the
        # bucket's later acquire-loop wait is a different float, so we count the smooth
        # gate's leading run by exact-value equality.
        clock = _Clock()
        slept: list[float] = []
        ctl = RateController(
            clock=clock, sleep=slept.append, ceiling_rps=CEILING_RPS, smooth_pacing=True
        )
        ctl.pace()  # first issue stamps @1000.0 (no wait — -inf baseline)
        smooth_wait = (1000.0 + 1.0 / CEILING_RPS) - 1000.0  # the smooth gate's exact wait
        slept.clear()
        ctl.pace()  # baseline 1000.0, frozen clock → every pass waits 1/5, spins to the cap
        assert slept.count(smooth_wait) == _SMOOTH_LOOP_CAP  # smooth gate spun to its cap
        assert ctl._last_issue_ts == 1000.0  # fail-open stamped at the frozen now


def test_rate_snapshot_throttles_after_a_log() -> None:
    """A second call inside the interval after a log is suppressed (the throttle resets)."""
    clock = _Clock()
    ctl = _controller(clock)
    clock.advance(SNAPSHOT_LOG_INTERVAL_S)
    with capture_logs() as first:
        ctl._maybe_log_snapshot()
    assert len([e for e in first if e["event"] == "rate.snapshot"]) == 1
    clock.advance(SNAPSHOT_LOG_INTERVAL_S - 0.1)  # just under the interval
    with capture_logs() as second:
        ctl._maybe_log_snapshot()
    assert [e for e in second if e["event"] == "rate.snapshot"] == []


def test_rate_snapshot_reflects_a_stepped_down_holding_rate() -> None:
    """The whole point: a stepped-down, HOLDING rate is now visible in the snapshot.

    Drive the controller to the soft floor (the operator's ~3 r/s), let the clock cross
    the interval with NO rung change, and assert the snapshot reports the floored rate +
    the recent-429 pressure — the exact data the live log was missing.
    """
    clock = _Clock()
    ctl = _controller(clock)
    _hammer(ctl, clock, hits=STEP_DOWN_HITS * 2)  # 5→4→3 (the soft floor)
    assert ctl.current_rps == FLOOR_RPS
    ctl.on_rate_limit()  # one fresh hit at the floor (no further step) → visible pressure
    assert ctl.current_rps == FLOOR_RPS
    clock.advance(SNAPSHOT_LOG_INTERVAL_S)
    with capture_logs() as logs:
        ctl._maybe_log_snapshot()
    snap = next(e for e in logs if e["event"] == "rate.snapshot")
    assert snap["current_rps"] == FLOOR_RPS  # the HOLDING floored rate, now in /logs
    assert snap["recent_hits"] >= 1  # the pressure keeping it there is visible


def test_recovers_from_floor_up_to_the_injected_8_ceiling() -> None:
    """DEC-GROWW-RATE-RECOVER-8-001: the INCREMENTAL ladder climbs the soft floor back to
    WHATEVER ceiling is injected — one rung per clean STEP_UP_WINDOW — and clamps there.

    This is a generic AIMD-ladder mechanism test: 8.0 is an arbitrary INJECTED fixture
    (deliberately decoupled from the production ``GROWW_RATE_LIMIT_RPS_CEILING``, which the
    operator lowered 8 → 5 on 2026-06-22), proving the climb tracks the injected ceiling.
    Before the STEP_UP fix the 300 s/rung recovery meant a stepped-down pull never reached
    the ceiling within its wall time (stuck at ~3). With STEP_UP_WINDOW == STEP_DOWN_WINDOW
    (60 s) the climb 3→8 takes ~5 clean windows.
    """
    clock = _Clock()
    ctl = RateController(clock=clock, sleep=lambda _s: None, ceiling_rps=8.0)
    assert ctl.current_rps == 8.0  # starts at the injected ceiling
    _hammer(ctl, clock, hits=STEP_DOWN_HITS * 5)  # 8→7→6→5→4→3 (the soft floor)
    assert ctl.current_rps == FLOOR_RPS
    # Five clean windows recover 3→4→5→6→7→8 (the DECREMENTAL ladder is untouched).
    for expected in (4.0, 5.0, 6.0, 7.0, 8.0):
        clock.advance(STEP_UP_WINDOW)
        ctl.on_success()
        assert ctl.current_rps == expected
    # At the ceiling a further clean window must NOT exceed 8 (clamped).
    clock.advance(STEP_UP_WINDOW)
    ctl.on_success()
    assert ctl.current_rps == 8.0


def test_step_up_window_matches_the_step_down_window() -> None:
    """The incremental window is symmetric with the rolling step-down window (60 s)."""
    assert STEP_UP_WINDOW == STEP_DOWN_WINDOW == 60.0


class TestClockGapSleepWake:
    """DEC-RATE-CLOCK-SLEEPWAKE-001 — laptop sleep/wake monotonic-gap safety.

    When the operator's MacBook sleeps overnight and wakes, ``time.monotonic()``
    jumps forward by a large delta. Two failure modes the controller must prevent:
    (a) a stale rolling-60s issue history that lets a post-wake burst overshoot the
    per-second OR per-minute cap, and (b) mis-timed AIMD recovery/backoff after the
    gap. A single delta larger than :data:`PER_MIN_WINDOW` (~60 s) is treated as a
    sleep/wake event: the rolling histories are pruned + every time-anchored marker
    is re-based to ``now`` so issuance starts fresh — never bursting past the cap,
    never stalling.
    """

    def test_governor_no_burst_overshoot_across_sleep(self) -> None:
        # cap=3 at a HIGH ceiling so the per-second bucket is never the binding gate:
        # only the per-minute governor governs. Fill the window, then jump the clock
        # well past PER_MIN_WINDOW (sleep/wake) and confirm the next pace cannot let
        # the rolling count exceed the cap.
        clock = _Clock()
        slept: list[float] = []

        def _sleep(seconds: float) -> None:  # never reached in this happy path
            slept.append(seconds)
            clock.advance(seconds)

        ctl = RateController(clock=clock, sleep=_sleep, ceiling_rps=1000.0, ceiling_per_min=3)
        for _ in range(3):
            ctl.pace()
        assert ctl.snapshot()["recent_minute"] == 3  # window full at the cap
        slept.clear()  # ignore the bucket's negligible 0.001 s opening pacing
        # Simulate an 8-hour laptop sleep: one giant monotonic delta.
        clock.advance(8 * 3600.0)
        # The gap is detected → the stale issue history is reset to ``now``; the next
        # pace admits exactly ONE fresh issue and the rolling count is the post-reset
        # count, never the cap+pre-sleep stale entries.
        ctl.pace()
        recent = ctl.snapshot()["recent_minute"]
        assert isinstance(recent, int)
        assert recent <= 3, f"per-minute cap overshot after sleep/wake: {recent}"
        # No GOVERNOR block fired: any governor block would be tens of seconds (one
        # rolling window); the only sleep should be the bucket's tiny 1/1000 s pacing.
        assert all(s < 1.0 for s in slept), f"governor blocked after sleep/wake: {slept}"

    def test_no_stall_after_backward_clock_glitch(self) -> None:
        # A small BACKWARD clock glitch on wake must not wedge the governor (mode b):
        # a tiny backward step inside the window is tolerated normally, but a large
        # backward jump (> PER_MIN_WINDOW) is treated as a gap → reset, so the
        # controller resumes immediately instead of waiting on a future-stamped issue.
        clock = _Clock()
        ctl = RateController(
            clock=clock, sleep=lambda _s: None, ceiling_rps=1000.0, ceiling_per_min=2
        )
        ctl.pace()
        ctl.pace()
        assert ctl.snapshot()["recent_minute"] == 2  # full
        clock.advance(-(2 * CLOCK_GAP_THRESHOLD_S))  # large backward glitch (> threshold)
        # Without gap handling the deque holds FUTURE-stamped issues that never age out
        # → the governor would wait (or fail-open after spinning). With the reset the
        # next pace starts fresh and admits without a spin.
        ctl.pace()
        recent = ctl.snapshot()["recent_minute"]
        assert isinstance(recent, int)
        assert recent <= 2

    def test_smooth_pacing_no_burst_across_sleep(self) -> None:
        # With smooth pacing ON, ``_last_issue_ts`` holds a real instant; a post-wake
        # gap re-bases it so the first post-wake issue is not throttled against a stale
        # baseline, yet the per-second spacing still binds afterward (no burst).
        clock = _Clock()
        slept: list[float] = []
        ctl = RateController(
            clock=clock,
            sleep=slept.append,
            ceiling_rps=5.0,
            smooth_pacing=True,
        )
        ctl.pace()  # first issue (no wait — _last_issue_ts started -inf)
        slept.clear()  # ignore the bucket's 1/5 s opening pacing
        clock.advance(8 * 3600.0)  # sleep/wake
        ctl.pace()  # gap re-based; first post-wake issue should not wait on the gate-0 spacing
        # The smooth gate adds NO wait (a stale baseline 8 h in the past is way past
        # the 1/rps interval anyway, but the re-base makes the intent explicit); the
        # only sleep is the bucket's own 1/5 s pacing, never a multi-second block.
        assert all(s < 1.0 for s in slept), f"smooth gate blocked after sleep/wake: {slept}"

    def test_aimd_anchors_rebased_after_gap(self) -> None:
        # After a sleep/wake gap, ``_last_step`` is re-based so a single clean
        # on_success cannot interpret the whole sleep as "many recovery windows":
        # recovery is still ONE rung per genuine post-wake clean STEP_UP_WINDOW.
        clock = _Clock()
        ctl = RateController(clock=clock, sleep=lambda _s: None, ceiling_rps=5.0)
        _hammer(ctl, clock, hits=STEP_DOWN_HITS * 2)  # 5→4→3 (soft floor)
        assert ctl.current_rps == FLOOR_RPS
        clock.advance(8 * 3600.0)  # sleep/wake (huge gap)
        ctl.on_success()  # gap re-bases _last_step → NOT yet a clean window post-wake
        assert ctl.current_rps == FLOOR_RPS  # no instant multi-rung jump from the gap
        # A genuine clean window after the wake recovers one rung.
        clock.advance(STEP_UP_WINDOW)
        ctl.on_success()
        assert ctl.current_rps == FLOOR_RPS + STEP_RPS

    def test_sub_threshold_delta_does_not_trip_gap(self) -> None:
        # A normal small advance (≤ CLOCK_GAP_THRESHOLD_S) must NOT be treated as a
        # sleep/wake event: the rolling 429 history is preserved across it so the
        # step-down ladder still works (the gap handler's false branch). Hammer 3 hits
        # spread by a sub-threshold amount and confirm the step-down still fires.
        clock = _Clock()
        ctl = RateController(clock=clock, sleep=lambda _s: None, ceiling_rps=5.0)
        # Spread under BOTH the rolling step-down window (so the hits co-exist) AND the
        # gap threshold (so no phantom reset). 1 s satisfies both.
        small = 1.0
        assert small < STEP_DOWN_WINDOW and small <= CLOCK_GAP_THRESHOLD_S
        for _ in range(STEP_DOWN_HITS):
            ctl.on_rate_limit()
            clock.advance(small)
        # The hits were NOT cleared by a phantom gap → the ladder stepped down.
        assert ctl.current_rps == CEILING_RPS - STEP_RPS

    def test_default_controller_rebases_via_snapshot_on_wake(self) -> None:
        # A DEFAULT controller (no governor, no smooth pacing) reads the clock under
        # lock only in _maybe_log_snapshot at the end of pace(). A sleep/wake gap there
        # must re-base _last_step so a subsequent on_success times recovery from the
        # wake, not the pre-sleep step. Drive the rate to the floor, then pace across a
        # huge gap and confirm a single immediate on_success does NOT step up.
        clock = _Clock()
        ctl = RateController(clock=clock, sleep=lambda _s: None, ceiling_rps=5.0)
        _hammer(ctl, clock, hits=STEP_DOWN_HITS * 2)  # → FLOOR
        assert ctl.current_rps == FLOOR_RPS
        clock.advance(8 * 3600.0)  # sleep/wake
        ctl.pace()  # the only clock-under-lock read (snapshot) re-bases _last_step
        ctl.on_success()  # 0 s since the re-based step → no recovery from the gap alone
        assert ctl.current_rps == FLOOR_RPS

    def test_smooth_gate_gap_capture_prevents_stale_baseline_stall(self) -> None:
        """Kills the gate-0 ``gap_evt = self._handle_clock_gap_locked(now)`` → ``= None`` mutant.

        The mutant DELETES the smooth gate's own gap handling (full-run
        survivor 188): after a large BACKWARD clock glitch the gate then
        computes its wait from the FUTURE-stamped ``_last_issue_ts`` baseline
        and sleeps ~600 s before admitting — the exact stall the gate-seam gap
        capture exists to prevent. The correct path detects the gap FIRST
        (re-bases the baseline to ``-inf``) and admits with only sub-second
        bucket pacing. It survived before because ``_maybe_log_snapshot``
        re-emits ``rate.clock_gap`` LATER, satisfying any event-presence
        assertion — the SLEEP behaviour is the discriminating observable
        (DEC-MUTATION-RUNTIME-003 fixer round).
        """
        ctl, clock, slept = _smooth(ceiling_rps=CEILING_RPS)
        ctl.pace()  # stamps _last_issue_ts = 1000.0; bucket → clock 1000.2
        slept.clear()
        clock.advance(-(2 * CLOCK_GAP_THRESHOLD_S))  # backward glitch → 400.2
        ctl.pace()
        # Correct: gate 0 gap-resets (baseline → -inf) → zero smooth wait; the
        # only sleeps are the bucket's ≤ 1/rps pacing (bounded by its acquire
        # cap on the backward clock). Mutant: one ~600 s smooth-gate sleep.
        assert all(s < 1.0 for s in slept), f"smooth gate slept on a stale baseline: {slept}"

    def test_governor_gate_gap_capture_prevents_stale_window_stall(self) -> None:
        """Kills the gate-1 ``gov_gap_evt = self._handle_clock_gap_locked(now)`` → ``= None`` mutant.

        The governor-seam twin (full-run survivor 209): the mutant deletes the
        governor loop's own gap handling, so after a large backward glitch the
        full rolling window holds FUTURE-stamped issues that never age out and
        the governor sleeps ~660 s before a slot frees. The correct path
        gap-resets the window at the seam and admits immediately
        (DEC-MUTATION-RUNTIME-003 fixer round).
        """
        ctl, clock, slept = _governed(2)
        ctl.pace()  # issue reserved @1000.0
        ctl.pace()  # issue reserved @1000.2 → window FULL at the cap
        slept.clear()
        clock.advance(-(2 * CLOCK_GAP_THRESHOLD_S))  # backward glitch → 400.4
        ctl.pace()
        assert all(s < 1.0 for s in slept), f"governor slept on a stale window: {slept}"
        # The gap reset cleared the stale window; the fresh issue is the only one.
        assert list(ctl._issues) == [pytest.approx(400.4)]

    def test_threshold_is_above_every_operational_window(self) -> None:
        # The gap threshold sits comfortably ABOVE every normal operational gap (the
        # longest of which is one AIMD recovery window) so genuine pacing never
        # false-trips, while a multi-minute laptop sleep always does.
        assert CLOCK_GAP_THRESHOLD_S == 5.0 * PER_MIN_WINDOW == 300.0
        assert CLOCK_GAP_THRESHOLD_S > STEP_UP_WINDOW
        assert CLOCK_GAP_THRESHOLD_S > STEP_DOWN_WINDOW


class TestTrailingWindowIssueLog:
    """The OBSERVE-ONLY trailing-hour issue log feeds REAL windowed counts.

    DEC-RATE-MONITOR-EXPAND-001 (MIGRATION.md #1004). These are the TRUST/TRANSPARENCY
    tests the operator asked for: feed KNOWN issue timestamps and assert the snapshot's
    ``recent_1s`` / ``recent_60s`` / ``recent_600s`` / ``recent_3600s`` equal the EXACT
    count of issues in each trailing window — proving the displayed req/sec·min·10min·hour
    are genuine measured counts, not a per-second figure multiplied up.
    """

    def test_window_constants_are_the_documented_seconds(self) -> None:
        # 1 s / 60 s / 600 s / 3600 s — the four trailing windows the monitor reports.
        assert LOG_WINDOW_1S == 1.0
        assert LOG_WINDOW_60S == 60.0 == PER_MIN_WINDOW
        assert LOG_WINDOW_600S == 600.0
        assert LOG_WINDOW_3600S == 3600.0 == TRAILING_LOG_WINDOW_S

    def test_counts_equal_real_issues_in_each_window(self) -> None:
        # Feed issue instants at KNOWN times relative to ``now`` and assert each window's
        # count is EXACTLY the number of issues inside it (the arithmetic the operator can
        # verify by eye). ``now`` = 5000.0; we stamp the log at hand-chosen offsets.
        clock = _Clock()
        ctl = _controller(clock)
        now = 5000.0
        # offsets (seconds BEFORE now) → expected membership:
        #   0.5  -> in 1s, 60s, 600s, 3600s
        #   0.9  -> in 1s, 60s, 600s, 3600s
        #   30   -> in 60s, 600s, 3600s
        #   120  -> in 600s, 3600s
        #   900  -> in 3600s
        #   4000 -> in NONE (older than the hour; pruned)
        for offset in (4000.0, 900.0, 120.0, 30.0, 0.9, 0.5):
            ctl._record_issue_locked(now - offset)
        clock.t = now
        snap = ctl.snapshot()
        assert snap["recent_1s"] == 2  # the 0.5 + 0.9 instants
        assert snap["recent_60s"] == 3  # + the 30 s instant
        assert snap["recent_600s"] == 4  # + the 120 s instant
        assert snap["recent_3600s"] == 5  # + the 900 s instant (the 4000 s one was pruned)
        # The req/sec·min·10min·hour the widget shows ARE these counts (1 s window ⇒
        # rps == recent_1s exactly): the transparency contract. No governor wired here, so
        # recent_minute stays 0 while the observe-only log carries the real trailing-60 s count.
        assert snap["recent_minute"] == 0

    def test_snapshot_rolls_ring_forward_when_no_newer_push(self) -> None:
        # The ring advances to ``now`` at snapshot time (separate from the push advance), so an
        # issue that has aged past the hour when NO newer pace has pushed since reads 0 — the
        # reported counts never include a > 1 h-old issue even on an idle-then-polled controller.
        clock = _Clock()
        ctl = _controller(clock)
        ctl._record_issue_locked(1000.0)  # counted in the second 1000 bucket
        clock.t = 1000.0 + TRAILING_LOG_WINDOW_S + 5.0  # poll an hour-plus later, no new push
        snap = ctl.snapshot()
        assert snap["recent_3600s"] == 0  # the stale second aged out as the ring rolled forward
        assert snap["recent_60s"] == 0
        assert ctl._issue_newest_sec == int(clock.t)  # the ring head followed ``now``

    def test_advance_on_unseeded_ring_is_a_noop(self) -> None:
        # The defensive guard: advancing the ring before any issue is recorded (newest is None)
        # is a no-op — the ring stays unseeded + all-zero (both production callers seed/guard
        # first, but the guard keeps the method total).
        clock = _Clock()
        ctl = _controller(clock)
        ctl._advance_issue_ring_locked(1234)
        assert ctl._issue_newest_sec is None
        assert sum(ctl._issue_buckets) == 0

    def test_ring_is_fixed_size_regardless_of_rate(self) -> None:
        # The per-second ring is O(1) MEMORY — a FIXED TRAILING_LOG_BUCKETS ints regardless of
        # how many issues are recorded (the SCALABLE replacement for a per-issue deque, the
        # DEC-MEMLEAK-GATE-001 ceiling). Recording many issues never grows the structure.
        clock = _Clock()
        ctl = _controller(clock)
        assert len(ctl._issue_buckets) == TRAILING_LOG_BUCKETS
        for i in range(5000):  # far more than the ring size
            ctl._record_issue_locked(2000.0 + i * 0.1)
        assert len(ctl._issue_buckets) == TRAILING_LOG_BUCKETS  # still fixed — no growth

    def test_ring_zeroes_whole_window_on_a_long_jump(self) -> None:
        # A forward jump of ≥ the ring size zeroes the WHOLE ring exactly once (every old count
        # has aged out) — bounded advance, never an unbounded loop.
        clock = _Clock()
        ctl = _controller(clock)
        ctl._record_issue_locked(0.0)  # second 0
        ctl._record_issue_locked(10_000.0)  # ≫ 3600 s later ⇒ the whole ring zeroes once
        # Only the new second holds a count; the trailing-hour total is exactly 1.
        clock.t = 10_000.0
        assert ctl.snapshot()["recent_3600s"] == 1

    def test_recent_60s_cross_checks_recent_minute_under_governor(self) -> None:
        # With a governor wired, the issue-ring ``recent_60s`` and the governor's own
        # ``recent_minute`` are TWO INDEPENDENT counts of the same trailing-60 s window
        # (one from _issue_buckets, one from the governor's _issues deque) — so they agree, a
        # built-in cross-check the operator can trust.
        clock = _Clock()
        ctl = RateController(clock=clock, sleep=lambda _s: None, ceiling_per_min=500)
        # Three real paces through the full gate stack (governor + bucket both stamp).
        for _ in range(3):
            ctl.pace()
            clock.advance(0.5)
        snap = ctl.snapshot()
        assert snap["recent_60s"] == snap["recent_minute"] == 3

    def test_recent_60s_cross_checks_recent_minute_at_fractional_boundary(self) -> None:
        # DEC-ADAPTER-ROBUSTNESS-001 (H10) regression: the issue-ring ``recent_60s`` and the
        # governor's ``recent_minute`` must still AGREE when an issue sits in the boundary second
        # (within ``now - 60`` of a FRACTIONAL ``now``). The old ``ceil(now - window)`` oldest-sec
        # binning dropped that boundary bucket, so the ring undercounted (recent_60s = 0) while the
        # float-instant deque kept it (recent_minute = 1) — the documented cross-check then read
        # falsely UNEQUAL. With ``floor(now - window)`` the ring keeps the boundary second, so the
        # two windows agree (to within one boundary second). FAILS under the old ``ceil``.
        clock = _Clock()
        ctl = RateController(clock=clock, sleep=lambda _s: None, ceiling_per_min=500)
        now = 5000.5  # FRACTIONAL ``now`` ⇒ ``now - 60 == 4940.5`` is non-integer
        boundary_ts = 4940.7  # in (4940.5, 5000.5] ⇒ kept by the deque; bucket = floor = 4940
        ctl._record_issue_locked(boundary_ts)  # into the per-second issue ring
        ctl._issues.append(boundary_ts)  # into the governor deque (same exact instant)
        clock.t = now
        snap = ctl.snapshot()
        # The boundary-second issue is counted by BOTH sources, so the cross-check holds.
        assert snap["recent_60s"] == 1
        assert snap["recent_minute"] == 1
        assert snap["recent_60s"] == snap["recent_minute"]

    def test_count_in_window_keeps_the_boundary_second_bucket(self) -> None:
        # DEC-ADAPTER-ROBUSTNESS-001 (H10): the trailing-window count includes the bucket that
        # HOLDS ``now - window`` (the boundary second), because that bucket's sub-range
        # ``(now - window, floor(now - window) + 1)`` IS inside the trailing window. Probes the
        # primitive directly at a fractional ``now``: an issue at second 4940 is inside the
        # trailing 60 s of ``now = 5000.5`` (4940 ≥ floor(4940.5)) and must be counted as 1.
        # Under the old ``ceil(4940.5) = 4941`` oldest-sec it was DROPPED (counted 0).
        clock = _Clock()
        ctl = _controller(clock)
        ctl._record_issue_locked(4940.0)  # the boundary-second bucket (second 4940)
        clock.t = 5000.5
        assert ctl._count_in_window_locked(5000.5, LOG_WINDOW_60S) == 1

    def test_pace_records_an_issue_without_a_governor(self) -> None:
        # Governor OFF: _issues stays empty (recent_minute 0), but the OBSERVE-ONLY issue
        # ring STILL counts every admitted issue, so req/sec·min·10min·hour are real even
        # without a per-minute governor wired.
        clock = _Clock()
        ctl = _controller(clock)  # no governor
        for _ in range(4):
            ctl.pace()
            clock.advance(0.1)
        snap = ctl.snapshot()
        assert snap["recent_minute"] == 0  # no governor deque
        assert snap["recent_60s"] == 4  # the issue ring counted all 4
        assert snap["recent_3600s"] == 4

    def test_clock_gap_clears_the_issue_ring(self) -> None:
        # A laptop sleep/wake clock jump must forget pre-sleep counts so a post-wake snapshot
        # does not count stale issues into the windows (DEC-RATE-CLOCK-SLEEPWAKE-001 extended to
        # the issue ring). A jump > CLOCK_GAP_THRESHOLD_S triggers the reset.
        clock = _Clock()
        ctl = _controller(clock)
        ctl.pace()  # one real issue counted at t=1000
        assert ctl._issue_newest_sec is not None
        assert sum(ctl._issue_buckets) == 1
        clock.advance(CLOCK_GAP_THRESHOLD_S + 1.0)  # the machine "slept"
        ctl.on_success()  # any critical section runs the clock-gap handler first
        assert ctl._issue_newest_sec is None  # ring reset
        assert sum(ctl._issue_buckets) == 0


# ── DEC-SMOOTH-PACING-AUTOLATCH-001 — tri-state mode + one-way auto-latch ────────


def _adaptive(*, ceiling_rps: float = 8.0) -> tuple[RateController, _Clock, list[float]]:
    """Build an ADAPTIVE-mode controller with the fake clock + sleep-log harness."""
    clock = _Clock()
    slept: list[float] = []

    def _sleep(seconds: float) -> None:
        slept.append(seconds)
        clock.advance(seconds)

    ctl = RateController(
        clock=clock,
        sleep=_sleep,
        ceiling_rps=ceiling_rps,
        smooth_pacing_mode=SMOOTH_PACING_ADAPTIVE,
    )
    return ctl, clock, slept


def _seed_burst(ctl: RateController, *, second: int, count: int) -> None:
    """Record *count* issues into the observe-only ring's bucket for *second*.

    Drives the EXISTING per-second issue accounting directly (the latch reads
    that ring; ``pace()`` cannot legally out-issue the token bucket, so the
    burst shape the operator observed — ``rps_1s_max`` above the AIMD rate —
    is synthesised at the ring, exactly the evidence surface the latch scans).
    """
    with ctl._lock:
        for _ in range(count):
            ctl._record_issue_locked(float(second))


class TestSmoothPacingModeResolution:
    def test_default_derives_off_from_the_legacy_bool(self) -> None:
        ctl = RateController()
        assert ctl.smooth_pacing_mode == SMOOTH_PACING_OFF
        assert ctl.smooth_pacing_active is False

    def test_legacy_bool_true_derives_on(self) -> None:
        ctl = RateController(smooth_pacing=True)
        assert ctl.smooth_pacing_mode == SMOOTH_PACING_ON
        assert ctl.smooth_pacing_active is True  # latched from construction

    def test_explicit_mode_wins_over_the_legacy_bool(self) -> None:
        ctl = RateController(smooth_pacing=True, smooth_pacing_mode=SMOOTH_PACING_OFF)
        assert ctl.smooth_pacing_mode == SMOOTH_PACING_OFF
        assert ctl.smooth_pacing_active is False

    def test_adaptive_starts_inactive(self) -> None:
        ctl, _clock, _slept = _adaptive()
        assert ctl.smooth_pacing_mode == SMOOTH_PACING_ADAPTIVE
        assert ctl.smooth_pacing_active is False

    def test_unknown_mode_fails_loud(self) -> None:
        # ``match=`` is re.search, so the anchor pins the message HEAD byte-exact —
        # an XX-wrapped message mutant no longer matches (DEC-MUTATION-RUNTIME-003;
        # the compute.py mutant-215 idiom, CLAUDE.md §11 COMMON).
        with pytest.raises(ValueError, match=r"^unknown smooth_pacing_mode"):
            RateController(smooth_pacing_mode="banana")


class TestSmoothPacingAutoLatch:
    """The one-way ADAPTIVE latch (fail-before: the latch did not exist — a 429
    burst left ``_smooth_pacing`` False forever on a default-built controller)."""

    def test_latch_fires_on_a_burst_above_current_rps(self) -> None:
        ctl, clock, _slept = _adaptive(ceiling_rps=8.0)
        _seed_burst(ctl, second=int(clock.t), count=9)  # 9 issues in one second > 8.0
        with capture_logs() as logs:
            ctl.on_rate_limit()
        assert ctl.smooth_pacing_active is True
        latched = [e for e in logs if e["event"] == "rate.smooth_pacing_latched"]
        assert len(latched) == 1
        assert latched[0]["current_rps"] == 8.0
        assert max(latched[0]["bucket_counts"]) == 9  # the evidence is carried

    def test_latch_does_not_fire_at_or_below_current_rps(self) -> None:
        ctl, clock, _slept = _adaptive(ceiling_rps=8.0)
        _seed_burst(ctl, second=int(clock.t), count=8)  # AT the allowed rate — compliant
        with capture_logs() as logs:
            ctl.on_rate_limit()
        assert ctl.smooth_pacing_active is False
        assert not any(e["event"] == "rate.smooth_pacing_latched" for e in logs)

    def test_latch_scans_the_trailing_buckets_not_just_the_current_second(self) -> None:
        """A 429 lands AFTER request latency — the burst sits 2 s behind and still latches."""
        ctl, clock, _slept = _adaptive(ceiling_rps=8.0)
        burst_second = int(clock.t)
        _seed_burst(ctl, second=burst_second, count=9)
        clock.advance(2.0)  # the 429 arrives two seconds after the burst
        ctl.on_rate_limit()
        assert ctl.smooth_pacing_active is True

    def test_burst_outside_the_scan_window_never_latches(self) -> None:
        ctl, clock, _slept = _adaptive(ceiling_rps=8.0)
        _seed_burst(ctl, second=int(clock.t), count=9)
        clock.advance(10.0)  # far past the 3 s scan window
        ctl.on_rate_limit()
        assert ctl.smooth_pacing_active is False

    def test_latch_is_one_way_for_the_controller_lifetime(self) -> None:
        ctl, clock, _slept = _adaptive(ceiling_rps=8.0)
        _seed_burst(ctl, second=int(clock.t), count=9)
        with capture_logs() as logs:
            ctl.on_rate_limit()
            # Further 429s (no burst evidence at all) never UNLATCH — and never re-log.
            ctl.on_rate_limit()
            ctl.on_rate_limit()
        assert ctl.smooth_pacing_active is True
        assert sum(1 for e in logs if e["event"] == "rate.smooth_pacing_latched") == 1

    def test_off_mode_disables_the_latch_entirely(self) -> None:
        clock = _Clock()
        ctl = RateController(clock=clock, sleep=lambda _s: None, ceiling_rps=8.0)  # OFF
        _seed_burst(ctl, second=int(clock.t), count=9)
        ctl.on_rate_limit()
        assert ctl.smooth_pacing_active is False  # OFF can never latch

    def test_on_mode_is_already_latched_no_relog(self) -> None:
        clock = _Clock()
        ctl = RateController(
            clock=clock, sleep=lambda _s: None, ceiling_rps=8.0, smooth_pacing_mode=SMOOTH_PACING_ON
        )
        _seed_burst(ctl, second=int(clock.t), count=9)
        with capture_logs() as logs:
            ctl.on_rate_limit()
        assert ctl.smooth_pacing_active is True
        assert not any(e["event"] == "rate.smooth_pacing_latched" for e in logs)

    def test_unseeded_ring_never_latches(self) -> None:
        """A 429 before ANY issue was recorded has no burst evidence — no latch."""
        ctl, _clock, _slept = _adaptive(ceiling_rps=8.0)
        ctl.on_rate_limit()
        assert ctl.smooth_pacing_active is False

    def test_latch_rebases_the_spacing_baseline(self) -> None:
        """The first POST-latch issue admits immediately (the -inf construction baseline)."""
        ctl, clock, _slept = _adaptive(ceiling_rps=8.0)
        _seed_burst(ctl, second=int(clock.t), count=9)
        ctl.on_rate_limit()
        assert ctl._last_issue_ts == float("-inf")
        assert ctl._smooth_wait_locked(ctl._clock()) == 0.0

    def test_recent_bucket_counts_zero_span_is_empty(self) -> None:
        """Degenerate scan spans (0 / negative seconds) read as no evidence."""
        ctl, clock, _slept = _adaptive(ceiling_rps=8.0)
        _seed_burst(ctl, second=int(clock.t), count=3)
        with ctl._lock:
            assert ctl._recent_bucket_counts_locked(ctl._clock(), 0) == []

    def test_aimd_ladder_and_caps_unchanged_by_the_latch(self) -> None:
        """The latch only flips the smooth gate — the 429 step-down maths is untouched."""
        ctl, clock, _slept = _adaptive(ceiling_rps=CEILING_RPS)
        _seed_burst(ctl, second=int(clock.t), count=int(CEILING_RPS) + 1)
        for _ in range(STEP_DOWN_HITS):
            ctl.on_rate_limit()
        assert ctl.smooth_pacing_active is True
        assert ctl.current_rps == CEILING_RPS - STEP_RPS  # the ladder stepped exactly once


# ── DEC-RATE-LOG-OUTSIDE-LOCK-001: no potentially-blocking I/O under _lock ──────


class _WedgedSink:
    """A deliberately-BLOCKING log sink: every emission wedges until released.

    ``entered`` is set the instant an emission begins; ``release`` unwedges it.
    The wait is BOUNDED (30 s) so the test itself can never hang CI even if the
    release event is never set (a red assertion fires long before).
    """

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.events: list[tuple[str, str, dict[str, object]]] = []

    def _emit(self, level: str, event: str, fields: dict[str, object]) -> None:
        self.entered.set()
        self.release.wait(timeout=30.0)
        self.events.append((level, event, fields))

    def warning(self, event: str, **fields: object) -> None:
        self._emit("warning", event, dict(fields))

    def info(self, event: str, **fields: object) -> None:
        self._emit("info", event, dict(fields))


class _LockFreeProbe:
    """Asserts the controller lock is NOT held at every emission; records events.

    ``acquire(blocking=False)`` succeeding at emit time proves the emitter
    released ``_lock`` before calling into the (potentially blocking) sink —
    the permanent "no emission under _lock anywhere in this module" audit pin.
    """

    def __init__(self, ctl: RateController) -> None:
        self._ctl = ctl
        self.records: list[tuple[str, str, bool]] = []

    def _record(self, level: str, event: str) -> None:
        acquired = self._ctl._lock.acquire(blocking=False)
        if acquired:
            self._ctl._lock.release()
        self.records.append((level, event, acquired))

    def warning(self, event: str, **fields: object) -> None:
        self._record("warning", event)

    def info(self, event: str, **fields: object) -> None:
        self._record("info", event)

    def assert_lock_free(self, *expected_events: str) -> None:
        assert all(free for (_lvl, _evt, free) in self.records), (
            f"an emission ran while _lock was held: {self.records}"
        )
        seen = [evt for (_lvl, evt, _free) in self.records]
        for event in expected_events:
            assert event in seen, f"expected {event} in {seen}"


class _RaisingSink:
    """A log sink that RAISES on every emission (the failure-injection twin)."""

    def warning(self, event: str, **fields: object) -> None:
        raise RuntimeError(f"sink failed on {event}")

    def info(self, event: str, **fields: object) -> None:
        raise RuntimeError(f"sink failed on {event}")


class TestLogEmissionOutsideLock:
    """DEC-RATE-LOG-OUTSIDE-LOCK-001 — every rate.* emission runs OUTSIDE ``_lock``.

    Pre-fix, ``rate.429`` / ``rate.stepdown`` / ``rate.timeout`` / ``rate.stepup``
    / ``rate.clock_gap`` / ``rate.smooth_pacing_latched`` were emitted WHILE
    HOLDING ``RateController._lock``, so one wedged/slow log sink froze every
    worker's ``pace()`` and the dashboard sampler's ``snapshot()`` read path —
    stalling the whole pull's pacing. The fix captures each event's fields under
    the lock (at the original emission program point) and emits after release
    via ``_flush_rate_logs``. Pacing DECISIONS are byte-identical — pinned here
    and by every pre-existing suite in this file passing unmodified.
    """

    # ── T1: the REQUIRED blocking-sink regression (deadlocks pre-fix) ──────────

    def test_pace_and_snapshot_never_block_on_a_wedged_log_sink(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # FROZEN fake clock: the probe thread emits nothing itself (the snapshot
        # throttle sits inside its warm-up grace; zero delta → no clock gap).
        clock = _Clock()
        ctl = RateController(clock=clock, sleep=lambda _s: None)
        sink = _WedgedSink()
        monkeypatch.setattr(rate_common, "_LOG", sink)

        probe_result: dict[str, object] = {}
        done = threading.Event()

        def _wedge() -> None:
            ctl.on_rate_limit()  # emits rate.429 → wedges inside the sink

        def _probe() -> None:
            ctl.pace()
            probe_result["current_rps"] = ctl.current_rps
            probe_result["smooth_active"] = ctl.smooth_pacing_active
            probe_result["snapshot"] = ctl.snapshot()
            ctl.on_success()  # at the ceiling + no gap → nothing to emit
            done.set()

        wedged = threading.Thread(target=_wedge, daemon=True)
        probe = threading.Thread(target=_probe, daemon=True)
        try:
            wedged.start()
            assert sink.entered.wait(5.0), "the sink was never entered"
            probe.start()
            # PRE-FIX: the wedged thread holds _lock inside the rate.429 emission,
            # so the probe blocks on its FIRST lock acquisition → this times out RED.
            # POST-FIX: the lock was released before the emission → the probe's
            # pace() + property reads + snapshot() + on_success() complete in ms.
            assert done.wait(5.0), "pace()/snapshot() blocked behind the wedged log sink"
            snap = probe_result["snapshot"]
            assert isinstance(snap, dict)
            # The 429 DECISION committed under the lock BEFORE the wedged telemetry.
            assert snap["recent_hits"] == 1
            assert probe_result["current_rps"] == CEILING_RPS
            assert not ctl._lock.locked(), "a thread is parked holding the pacing lock"
        finally:
            sink.release.set()
            wedged.join(timeout=5.0)
            probe.join(timeout=5.0)
        assert not wedged.is_alive() and not probe.is_alive()
        # Nothing lost: the wedged event emitted exactly once, fields captured at
        # the decision point (current_rps/recent_hits as of the 429, not later).
        assert sink.events == [("warning", "rate.429", {"current_rps": 5.0, "recent_hits": 1})]

    # ── T2: the scripted-scenario byte-identity golden ─────────────────────────

    def test_deferred_emit_scenario_pacing_and_log_order(self) -> None:
        # One deterministic fake-clock run: 429-burst stepdown 5→4 → pace →
        # timeout storm 4→3 → clean 60 s recovery 3→4 → mid-run 400 s clock gap.
        # The expected sleep list, _rps trajectory, and ordered (event, fields)
        # list are HARD-CODED from pre-change semantics — the golden that pins
        # pacing decisions AND telemetry byte-identical across the refactor.
        clock = _Clock()
        slept: list[float] = []

        def _sleep(seconds: float) -> None:
            slept.append(seconds)
            clock.advance(seconds)

        ctl = RateController(clock=clock, sleep=_sleep)
        with capture_logs() as logs:
            for _ in range(STEP_DOWN_HITS):  # 429 burst at one instant → 5→4
                ctl.on_rate_limit()
            assert ctl.current_rps == 4.0
            ctl.pace()  # drained bucket at the new rate → exactly one 1/4 s sleep
            for _ in range(STEP_DOWN_TIMEOUT_HITS):  # timeout storm → 4→3
                ctl.on_timeout()
            assert ctl.current_rps == 3.0
            clock.advance(STEP_UP_WINDOW)  # one clean recovery window
            ctl.on_success()  # → 3→4
            assert ctl.current_rps == 4.0
            clock.advance(400.0)  # mid-run sleep/wake clock gap (> 300 s threshold)
            ctl.on_success()  # gap re-bases anchors → no stepup, one rate.clock_gap
            assert ctl.current_rps == 4.0
        assert slept == [1.0 / 4.0]  # the EXACT pre-change sleep sequence
        actual = [
            (e["event"], {k: v for k, v in e.items() if k not in ("event", "log_level")})
            for e in logs
        ]
        expected: list[tuple[str, dict[str, object]]] = [
            ("rate.429", {"current_rps": 5.0, "recent_hits": 1}),
            ("rate.429", {"current_rps": 5.0, "recent_hits": 2}),
            ("rate.429", {"current_rps": 5.0, "recent_hits": 3}),
            ("rate.stepdown", {"from_rps": 5.0, "to_rps": 4.0, "floor_rps": FLOOR_RPS}),
            ("rate.timeout", {"current_rps": 4.0, "recent_timeouts": 1}),
            ("rate.timeout", {"current_rps": 4.0, "recent_timeouts": 2}),
            ("rate.timeout", {"current_rps": 4.0, "recent_timeouts": 3}),
            ("rate.timeout", {"current_rps": 4.0, "recent_timeouts": 4}),
            ("rate.timeout", {"current_rps": 4.0, "recent_timeouts": 5}),
            (
                "rate.stepdown",
                {
                    "from_rps": 4.0,
                    "to_rps": 3.0,
                    "floor_rps": TIMEOUT_FLOOR_RPS,
                    "cause": "timeout",
                },
            ),
            ("rate.stepup", {"from_rps": 3.0, "to_rps": 4.0, "ceiling_rps": CEILING_RPS}),
            ("rate.clock_gap", {"gap_s": 400.0, "current_rps": 4.0}),
        ]
        assert actual == expected

    # ── T3: the all-sites lock-free-emission audit pin ─────────────────────────

    def test_every_emission_site_runs_lock_free(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Drive EVERY rate.* emission site in the module and assert the emitter
        # does NOT hold _lock at emit time. Pre-fix this fails at every site;
        # post-fix it is the permanent AUTOMATED "no emission under _lock
        # anywhere in this file" audit — any future in-lock emission fails it.
        def _advancing(clock: _Clock) -> Callable[[float], None]:
            def _sleep(seconds: float) -> None:
                clock.advance(seconds)

            return _sleep

        # (1) on_rate_limit: rate.429 ×3 + rate.stepdown.
        clock = _Clock()
        ctl = _controller(clock)
        probe = _LockFreeProbe(ctl)
        monkeypatch.setattr(rate_common, "_LOG", probe)
        for _ in range(STEP_DOWN_HITS):
            ctl.on_rate_limit()
        probe.assert_lock_free("rate.429", "rate.stepdown")

        # (2) the adaptive latch: rate.smooth_pacing_latched.
        ctl2, clock2, _slept2 = _adaptive(ceiling_rps=8.0)
        probe2 = _LockFreeProbe(ctl2)
        monkeypatch.setattr(rate_common, "_LOG", probe2)
        _seed_burst(ctl2, second=int(clock2.t), count=9)
        ctl2.on_rate_limit()
        probe2.assert_lock_free("rate.429", "rate.smooth_pacing_latched")

        # (3) on_timeout: rate.timeout ×5 + rate.stepdown (cause=timeout).
        clock3 = _Clock()
        ctl3 = _controller(clock3)
        probe3 = _LockFreeProbe(ctl3)
        monkeypatch.setattr(rate_common, "_LOG", probe3)
        for _ in range(STEP_DOWN_TIMEOUT_HITS):
            ctl3.on_timeout()
        probe3.assert_lock_free("rate.timeout", "rate.stepdown")

        # (4) on_success: rate.stepup.
        clock4 = _Clock()
        ctl4 = _controller(clock4)
        probe4 = _LockFreeProbe(ctl4)
        monkeypatch.setattr(rate_common, "_LOG", probe4)
        _hammer(ctl4, clock4, hits=STEP_DOWN_HITS)  # 5→4
        clock4.advance(STEP_UP_WINDOW)
        ctl4.on_success()
        probe4.assert_lock_free("rate.stepup")

        # (5) pace(): the throttled rate.snapshot.
        clock5 = _Clock()
        ctl5 = RateController(clock=clock5, sleep=_advancing(clock5))
        probe5 = _LockFreeProbe(ctl5)
        monkeypatch.setattr(rate_common, "_LOG", probe5)
        ctl5.pace()  # warm-up grace — no snapshot yet
        clock5.advance(SNAPSHOT_LOG_INTERVAL_S + 1.0)
        ctl5.pace()  # past the interval → rate.snapshot
        probe5.assert_lock_free("rate.snapshot")

        # (6) rate.clock_gap from ALL SIX call sites.
        gap = CLOCK_GAP_THRESHOLD_S + 100.0
        # (6a) on_rate_limit / (6b) on_timeout / (6c) on_success.
        for method in ("on_rate_limit", "on_timeout", "on_success"):
            clock6 = _Clock()
            ctl6 = _controller(clock6)
            probe6 = _LockFreeProbe(ctl6)
            monkeypatch.setattr(rate_common, "_LOG", probe6)
            clock6.advance(gap)
            getattr(ctl6, method)()
            probe6.assert_lock_free("rate.clock_gap")
        # (6d) pace() gate 0 — the smooth-pacing loop.
        clock7 = _Clock()
        ctl7 = RateController(clock=clock7, sleep=_advancing(clock7), smooth_pacing=True)
        probe7 = _LockFreeProbe(ctl7)
        monkeypatch.setattr(rate_common, "_LOG", probe7)
        clock7.advance(gap)
        ctl7.pace()
        probe7.assert_lock_free("rate.clock_gap")
        # (6e) pace() gate 1 — the per-minute governor loop.
        clock8 = _Clock()
        ctl8 = RateController(clock=clock8, sleep=_advancing(clock8), ceiling_per_min=500)
        probe8 = _LockFreeProbe(ctl8)
        monkeypatch.setattr(rate_common, "_LOG", probe8)
        clock8.advance(gap)
        ctl8.pace()
        probe8.assert_lock_free("rate.clock_gap")
        # (6f) pace() on a DEFAULT controller — the _maybe_log_snapshot site.
        clock9 = _Clock()
        ctl9 = RateController(clock=clock9, sleep=_advancing(clock9))
        probe9 = _LockFreeProbe(ctl9)
        monkeypatch.setattr(rate_common, "_LOG", probe9)
        clock9.advance(gap)
        ctl9.pace()
        probe9.assert_lock_free("rate.clock_gap")

    # ── T4: capture-at-the-decision-point field pins (kill the lazy-read mutant) ─

    def test_429_fields_captured_before_stepdown_mutation(self) -> None:
        # The 3rd 429 at the ceiling steps 5→4 and CLEARS _hits in the same
        # critical section — yet the emitted rate.429 must carry the values AT
        # its original program point (current_rps=5.0, recent_hits=3). A lazy
        # re-read-at-flush mutant would emit 4.0/0 and die here.
        clock = _Clock()
        ctl = _controller(clock)
        _hammer(ctl, clock, hits=STEP_DOWN_HITS - 1)
        with capture_logs() as logs:
            ctl.on_rate_limit()  # 3rd hit → stepdown in the SAME call
        assert [e["event"] for e in logs] == ["rate.429", "rate.stepdown"]
        assert logs[0]["current_rps"] == 5.0  # pre-stepdown value
        assert logs[0]["recent_hits"] == 3  # pre-clear count
        assert logs[0]["log_level"] == "warning"
        assert logs[1]["log_level"] == "warning"
        assert ctl.current_rps == 4.0  # the decision itself committed

    def test_clock_gap_fields_captured_before_baseline_overwrite(self) -> None:
        # gap_s must be the REAL observed jump (400.0) — a post-overwrite read of
        # _last_seen would emit 0.0. current_rps is the pre-gap rate (3.0).
        clock = _Clock()
        ctl = _controller(clock)
        _hammer(ctl, clock, hits=STEP_DOWN_HITS * 2)  # 5→4→3
        assert ctl.current_rps == FLOOR_RPS
        ctl.on_success()  # syncs _last_seen to now (1 s since the step → no stepup)
        clock.advance(400.0)
        with capture_logs() as logs:
            ctl.on_success()
        gap_events = [e for e in logs if e["event"] == "rate.clock_gap"]
        assert len(gap_events) == 1
        assert gap_events[0]["gap_s"] == 400.0  # not 0.0 — captured pre-overwrite
        assert gap_events[0]["current_rps"] == FLOOR_RPS
        assert gap_events[0]["log_level"] == "warning"

    def test_stepup_is_emitted_at_info_level(self) -> None:
        clock = _Clock()
        ctl = _controller(clock)
        _hammer(ctl, clock, hits=STEP_DOWN_HITS)  # 5→4
        clock.advance(STEP_UP_WINDOW)
        with capture_logs() as logs:
            ctl.on_success()
        up = [e for e in logs if e["event"] == "rate.stepup"]
        assert len(up) == 1
        assert up[0]["log_level"] == "info"  # _flush_rate_logs' info branch

    # ── T5: a RAISING sink can no longer abort rate decisions ──────────────────

    def test_raising_sink_commits_decisions_and_releases_the_lock(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The ONE sanctioned behavioral delta: pre-fix a raise from the rate.429
        # emission ABORTED the latch + stepdown (they ran after the in-lock
        # emit); post-fix decisions commit under the lock and telemetry raises
        # afterward, outside it. Decisions-before-telemetry is strictly safer.
        clock = _Clock()
        ctl = _controller(clock)
        monkeypatch.setattr(rate_common, "_LOG", _RaisingSink())
        for _ in range(STEP_DOWN_HITS):
            with pytest.raises(RuntimeError, match="sink failed on rate.429"):
                ctl.on_rate_limit()
        assert ctl.current_rps == 4.0  # the stepdown committed despite the raise
        assert not ctl._lock.locked()  # the raise happened OUTSIDE the lock
        # snapshot() emits nothing — safe under the raising sink — and shows the
        # stepdown's _hits.clear() committed too.
        assert ctl.snapshot()["recent_hits"] == 0

    # ── T6: multi-event same-call ordering with the latch ──────────────────────

    def test_multi_event_same_call_order_with_latch(self) -> None:
        # One on_rate_limit call firing all three events preserves the pre-change
        # in-lock emission order: rate.429 → rate.smooth_pacing_latched →
        # rate.stepdown (per-thread order only; cross-thread interleaving is
        # deliberately unpinned — observability, not state).
        ctl, clock, _slept = _adaptive(ceiling_rps=8.0)
        ctl.on_rate_limit()  # hits=1, no burst evidence yet → no latch, no step
        ctl.on_rate_limit()  # hits=2
        _seed_burst(ctl, second=int(clock.t), count=9)  # 9 > 8.0 — burst evidence
        with capture_logs() as logs:
            ctl.on_rate_limit()  # hits=3 → 429 + latch + stepdown in ONE call
        assert [e["event"] for e in logs] == [
            "rate.429",
            "rate.smooth_pacing_latched",
            "rate.stepdown",
        ]
        assert logs[1]["current_rps"] == 8.0  # latch captured PRE-stepdown
        assert logs[2]["from_rps"] == 8.0
        assert logs[2]["to_rps"] == 7.0
        assert ctl.current_rps == 7.0
        assert ctl.smooth_pacing_active is True


# ── DEC-MUTATION-RUNTIME-003 (MIGRATION.md #1597) — mutmut survivor kills ────────
# Boundary + literal pins for the survivors of the rate_common mutation probe
# (read_rate.md §3; mutant ids cite that probe — matched by DIFF CONTENT on the
# current tree, since DEC-RATE-LOG-OUTSIDE-LOCK-001 renumbered mutants). Every
# test pins the CURRENT brake-EARLY / never-exceed direction of the rate-safety
# invariants (CLAUDE.md §2 Rule 6) — no ceiling/cap/threshold value is changed
# or loosened anywhere.


class TestMutationBoundaryPins:
    """Literal-boundary kills: thresholds, window-age evictions, exact-zero waits.

    The existing suite fed ``hits=STEP_DOWN_HITS`` etc. SYMBOLICALLY, so a
    threshold+1 mutant shifted the test and the threshold together and
    survived; these tests feed LITERAL counts/instants so the boundary itself
    is pinned (DEC-MUTATION-RUNTIME-003).
    """

    def test_step_down_after_exactly_three_literal_hits(self) -> None:
        """Kills mutant 11 (``STEP_DOWN_HITS: 3 → 4``): 3 LITERAL hits DO step 5→4."""
        ctl = _controller(_Clock())
        for _ in range(3):  # literal 3 — NOT range(STEP_DOWN_HITS)
            ctl.on_rate_limit()
        assert ctl.current_rps == 4.0

    def test_storm_steps_below_soft_floor_after_exactly_thirty_hits(self) -> None:
        """Kills mutant 29 (``STORM_HITS: 30 → 31``): 30 LITERAL same-window hits ease 3→2."""
        ctl = _controller(_Clock())
        for _ in range(6):  # two 3-hit windows: 5→4→3 (each step clears _hits)
            ctl.on_rate_limit()
        assert ctl.current_rps == FLOOR_RPS
        for _ in range(30):  # literal 30 — the storm threshold at/below the soft floor
            ctl.on_rate_limit()
        assert ctl.current_rps == FLOOR_RPS - STEP_RPS  # 2.0 — the storm step fired

    def test_timeout_ladder_steps_after_exactly_five_literal_timeouts(self) -> None:
        """Kills mutant 31 (``STEP_DOWN_TIMEOUT_HITS: 5 → 6``): 5 LITERAL stalls step 5→4."""
        ctl = _controller(_Clock())
        for _ in range(5):  # literal 5
            ctl.on_timeout()
        assert ctl.current_rps == 4.0

    def test_adaptive_latch_scan_excludes_offset_three_seconds(self) -> None:
        """Kills mutant 47 (``SMOOTH_LATCH_SCAN_SECONDS: 3 → 4``).

        A burst exactly 3 s behind the 429 is OUTSIDE the 3-bucket trailing scan
        (offsets 0/1/2) — the mutant's 4-bucket scan would latch on it. The
        existing advance-2.0 test pins the 3→2 direction.
        """
        ctl, clock, _slept = _adaptive(ceiling_rps=8.0)
        _seed_burst(ctl, second=int(clock.t), count=9)  # 9 > 8.0 — burst evidence
        clock.advance(3.0)  # the burst bucket now sits at trailing offset 3
        ctl.on_rate_limit()
        assert ctl.smooth_pacing_active is False  # offset 3 is outside a 3-s scan

    def test_clock_gap_exact_threshold_is_not_a_gap(self) -> None:
        """Kills mutant 93 (gap check ``> CLOCK_GAP_THRESHOLD_S`` → ``>=``).

        An EXACTLY-300 s delta is NOT a sleep/wake gap (strict ``>``), so the
        observe-only issue ring is KEPT — the mutant resets it. The RING is the
        observable (60 s hit-eviction would mask a ``recent_hits`` check).
        """
        clock = _Clock()
        ctl = _controller(clock)
        ctl.pace()  # seeds the issue ring with one real issue at t=1000.0
        clock.advance(CLOCK_GAP_THRESHOLD_S)  # exactly the threshold — not a gap
        ctl.on_success()  # runs the gap handler
        recent = ctl.snapshot()["recent_3600s"]
        assert recent == 1  # ring kept; the >= mutant zeroes it → 0

    def test_smooth_exact_zero_wait_admits_without_spin(self) -> None:
        """Kills mutant 190 (pace gate-0 ``wait <= 0.0`` → ``< 0.0``, BOTH sites).

        At ``ceiling_rps=8.0`` the 0.125 s interval is float-EXACT, so the second
        pace computes ``wait == 0.0`` precisely: the stamp site's mutant skips the
        atomic stamp (pinned via ``_last_issue_ts``); the break site's mutant
        burns ``sleep(0.0)`` re-check spins (pinned via the sleep log).
        """
        ctl, _clock, slept = _smooth(ceiling_rps=8.0)
        ctl.pace()  # stamps @1000.0; bucket sleeps 0.125 → clock 1000.125
        ctl.pace()  # gate-0 wait == 0.0 exactly → stamp + admit with NO smooth sleep
        assert slept == [0.125, 0.125]  # only the bucket paced — no gate-0 spin
        assert 0.0 not in slept  # the break-site mutant sleeps 0.0 before re-checking
        assert ctl._last_issue_ts == 1000.125  # the stamp-site mutant never re-stamps

    def test_smooth_fractional_wait_never_stamps_before_admission(self) -> None:
        """Kills the gate-0 STAMP-site ``wait <= 0.0`` → ``<= 1.0`` mutant (full-run survivor 191).

        With a FRACTIONAL wait (0 < wait ≤ 1) the gate must sleep once and
        stamp ONLY at admission. The mutant stamps on every re-check pass
        (moving the spacing baseline while still waiting), so the wait never
        reaches ≤ 0 and the loop burns to ``_SMOOTH_LOOP_CAP`` — dozens of
        extra sleeps. Pinning the exact sleep sequence + the admission stamp
        kills it (and the ``<= 0.0`` → ``< 0.0`` no-stamp flip at the same
        site, which would break WITHOUT stamping).
        """
        # Dyadic values throughout (1/8 s interval) so every float below is
        # EXACT and the sleep-advance lands precisely on the admission instant.
        ctl, clock, slept = _smooth(ceiling_rps=8.0)
        ctl.pace()  # stamps @1000.0; bucket sleeps 0.125 → clock 1000.125
        slept.clear()
        clock.t = 1000.0625  # a jitter re-entry → smooth wait = 0.0625 (fractional)
        ctl.pace()
        # Exactly ONE 0.0625 s smooth sleep (to the admission instant 1000.125),
        # then the bucket's own 0.125 s token wait — nothing else.
        assert slept == [0.0625, 0.125]
        assert ctl._last_issue_ts == 1000.125  # stamped at admission only

    def test_governor_fractional_wait_reserves_only_at_admission(self) -> None:
        """Kills the gate-1 BREAK-site ``gov_wait <= 0.0`` → ``<= 1.0`` mutant (full-run survivor 216).

        A rate-safety boundary: with a fractional governor wait (0 < wait ≤ 1)
        the mutant BREAKS out of the governor loop WITHOUT reserving the slot —
        the issue proceeds unrecorded, so the rolling window under-counts and a
        later caller can overshoot the HARD per-minute cap. The correct path
        sleeps the fractional wait, evicts the aged slot, and reserves at
        admission. (The RESERVE-site twin at the same boundary would reserve
        into a FULL window — overshooting — and then still sleep a whole
        window; the exact sleep-sequence pin kills both.)
        """
        ctl, clock, slept = _governed(1)
        ctl.pace()  # slot reserved @1000.0; bucket → clock 1000.2
        slept.clear()
        # Dyadic offset (0.25) so the sleep-advance lands EXACTLY on the
        # age-out instant 1060.0 — every float below is exact.
        clock.t = 1000.0 + PER_MIN_WINDOW - 0.25  # 1059.75 → gov_wait = 0.25
        ctl.pace()
        # Exactly ONE 0.25 s governor sleep (to the age-out instant 1060.0); the
        # bucket has long since refilled, so no token wait follows.
        assert slept == [0.25]
        # The stale slot aged out and the fresh issue was RESERVED at admission.
        assert list(ctl._issues) == [1060.0]

    def test_hit_at_exact_window_age_still_counts_toward_stepdown(self) -> None:
        """Kills mutant 224 (``on_rate_limit`` evict ``<`` → ``<=``).

        A hit EXACTLY ``STEP_DOWN_WINDOW`` old is still in-window evidence and
        still brakes (the SAFE direction — the mutant evicts it → no step).
        """
        clock = _Clock()
        ctl = _controller(clock)
        ctl.on_rate_limit()  # hit at t=1000.0
        clock.advance(STEP_DOWN_WINDOW)  # exactly 60 s — boundary age
        ctl.on_rate_limit()
        ctl.on_rate_limit()  # 3rd in-window hit → step 5→4
        assert ctl.current_rps == 4.0

    def test_timeout_at_exact_window_age_still_counts(self) -> None:
        """Kills mutant 242 (``on_timeout`` evict ``<`` → ``<=``) — the stall twin."""
        clock = _Clock()
        ctl = _controller(clock)
        ctl.on_timeout()  # stall at t=1000.0
        clock.advance(STEP_DOWN_WINDOW)  # exactly 60 s
        for _ in range(4):
            ctl.on_timeout()  # 5th in-window stall → step 5→4
        assert ctl.current_rps == 4.0

    def test_recovery_blocked_by_hits_exactly_window_old(self) -> None:
        """Kills mutant 258 (``on_success`` hits-evict ``<`` → ``<=``).

        429 evidence EXACTLY ``STEP_UP_WINDOW`` old still counts as step-down-level
        pressure and blocks recovery (the mutant evicts it → an EARLY step-up =
        the less-safe direction).
        """
        clock = _Clock()
        ctl = _controller(clock)
        for _ in range(6):
            ctl.on_rate_limit()  # 5→4→3 (the soft floor); _hits cleared each step
        assert ctl.current_rps == FLOOR_RPS
        for _ in range(STEP_DOWN_HITS):
            ctl.on_rate_limit()  # 3 hits AT the floor (storm threshold → no step)
        assert ctl.current_rps == FLOOR_RPS
        clock.advance(STEP_UP_WINDOW)  # the hits are now EXACTLY window-old
        ctl.on_success()
        assert ctl.current_rps == FLOOR_RPS  # recovery held — boundary hits still count

    def test_recovery_blocked_by_timeouts_exactly_window_old(self) -> None:
        """Kills mutant 261 (``on_success`` timeouts-evict ``<`` → ``<=``) — the twin."""
        clock = _Clock()
        ctl = _controller(clock)
        for _ in range(20):
            ctl.on_timeout()  # 5→4→3→2→1 (the deep timeout floor)
        assert ctl.current_rps == TIMEOUT_FLOOR_RPS
        for _ in range(STEP_DOWN_TIMEOUT_HITS):
            ctl.on_timeout()  # 5 stalls AT the floor (rps not > floor → no step)
        assert ctl.current_rps == TIMEOUT_FLOOR_RPS
        clock.advance(STEP_UP_WINDOW)  # the stalls are now EXACTLY window-old
        ctl.on_success()
        assert ctl.current_rps == TIMEOUT_FLOOR_RPS  # recovery held

    def test_snapshot_recent_minute_evicts_at_exact_boundary(self) -> None:
        """Kills mutant 279 (snapshot governor evict ``<= 0.0`` → ``< 0.0``).

        An issue EXACTLY ``PER_MIN_WINDOW`` old has aged out of the rolling-60 s
        gauge — the mutant keeps it (an inflated ``recent_minute``).
        """
        ctl, clock, _slept = _governed(5)
        ctl.pace()  # issue reserved @1000.0 (admission instant, pre-bucket)
        assert ctl.snapshot()["recent_minute"] == 1
        clock.t = 1000.0 + PER_MIN_WINDOW  # exactly 60 s after the reserved instant
        assert ctl.snapshot()["recent_minute"] == 0  # boundary-age issue evicted

    def test_quota_exhausted_false_at_remaining_exactly_one(self, tmp_path: Path) -> None:
        """Kills mutant 301 (``quota_exhausted`` ``remaining() <= 0`` → ``<= 1``).

        One remaining call is NOT exhaustion — the loop may still spend it; the
        mutant would stop the pull a call early (a silent under-use, still a
        defect: the gate must fire at EXACTLY zero).
        """
        q = _quota(tmp_path, limit=2, used=1)
        ctl = RateController(quota_tracker=q, clock=_Clock(), sleep=lambda _s: None)
        assert q.remaining() == 1
        assert ctl.quota_exhausted() is False

    def test_issue_ring_advances_on_one_second_step(self) -> None:
        """Kills every mutant of the rephrased ``steps < 1`` ring-advance guard.

        A ONE-second step MUST advance the ring head (``<= 1`` / ``< 2`` mutants
        return early → the 1001.0 issue lands in the 1000 bucket and the trailing
        1 s count over (1001.5, 1002.5] reads 0, not 1). DEC-MUTATION-RUNTIME-003.
        """
        ctl = _controller(_Clock())
        ctl._record_issue_locked(1000.0)
        ctl._record_issue_locked(1001.0)
        assert ctl._count_in_window_locked(1002.5, 1.0) == 1

    def test_count_in_window_span_excludes_extra_bucket(self) -> None:
        """Kills mutant 155 (``raw_span``'s ``+ 1`` → ``+ 2``).

        With one issue at second 1000 only, the trailing 1 s of ``now=1002.5``
        spans seconds 1001-1002 → 0; the mutant's extra bucket reaches second
        1000 and reads a phantom 1.
        """
        ctl = _controller(_Clock())
        ctl._record_issue_locked(1000.0)
        assert ctl._count_in_window_locked(1002.5, 1.0) == 0

    def test_recent_bucket_counts_cover_every_trailing_offset(self) -> None:
        """Kills mutant 174 (``range(span - 1, -1, -1)`` step ``-1`` → ``-2``).

        Distinct per-second counts at offsets 0/1/2 pin the EXACT oldest→newest
        list — a ``-2`` step skips the middle bucket, a start/stop flip reshapes
        the list.
        """
        ctl = _controller(_Clock())
        _seed_burst(ctl, second=1000, count=1)
        _seed_burst(ctl, second=1001, count=2)
        _seed_burst(ctl, second=1002, count=3)
        with ctl._lock:
            counts = ctl._recent_bucket_counts_locked(1002.0, 3)
        assert counts == [1, 2, 3]


class TestMutationLiteralPins:
    """Cheap exact-value pins: public tokens, telemetry fields, constants, anchors.

    DEC-MUTATION-RUNTIME-003 — each pin holds a contract value callers may rely
    on as a raw literal (mode tokens, log event names, documented constants).
    """

    def test_smooth_mode_tokens_are_public_contract(self) -> None:
        """Kills mutants 40/42/44 (the mode-token strings XX-wrapped)."""
        assert (SMOOTH_PACING_ON, SMOOTH_PACING_OFF, SMOOTH_PACING_ADAPTIVE) == (
            "on",
            "off",
            "adaptive",
        )

    def test_clock_gap_logs_event_name_and_gap_seconds_exact(self) -> None:
        """Kills mutants 103/104 (``rate.clock_gap`` event name; ``gap_s`` arithmetic)."""
        clock = _Clock()
        ctl = _controller(clock)
        clock.advance(CLOCK_GAP_THRESHOLD_S + 1.0)  # a crafted 301.0 s gap
        with capture_logs() as logs:
            ctl.on_success()
        gap_events = [e for e in logs if e["event"] == "rate.clock_gap"]
        assert len(gap_events) == 1
        assert gap_events[0]["gap_s"] == CLOCK_GAP_THRESHOLD_S + 1.0

    def test_pacing_constants_pinned_exact(self) -> None:
        """Kills mutants 19/36/38/52 (ring size, both loop caps, snapshot throttle)."""
        assert TRAILING_LOG_BUCKETS == 3600
        assert _GOVERNOR_LOOP_CAP == 64 == _SMOOTH_LOOP_CAP
        assert SNAPSHOT_LOG_INTERVAL_S == 15.0

    def test_last_step_anchored_to_construction_clock(self) -> None:
        """Kills mutant 81 (``self._last_step = clock()`` → ``= None``)."""
        ctl = _controller(_Clock())
        assert ctl._last_step == 1000.0

    def test_bucket_downshift_pinned_at_half_the_start_rate(self) -> None:
        """Kills the ``downshift_rps=self._rps / 2.0`` value mutant (``/ 3.0``).

        The value IS directly observable on the constructed bucket, so the
        prior "unobservable by design" pragma was unnecessary AND its proof
        false — the ``* 2.0`` operator half already dies at construction
        (``AdaptiveTokenBucket.__post_init__`` requires downshift < initial);
        this pin closes the value half (DEC-MUTATION-RUNTIME-003 fixer round).
        """
        ctl = _controller(_Clock())
        assert ctl._bucket.initial_rps == CEILING_RPS
        assert ctl._bucket.downshift_rps == CEILING_RPS / 2.0

    def test_issue_ring_head_starts_and_gap_resets_at_slot_zero(self) -> None:
        """Kills the ``self._issue_head = 0`` → ``= 1`` mutants at BOTH sites.

        The head slot is behaviourally arbitrary while the ring is all-zero,
        but the documented "starts at slot 0 / gap-reset re-bases to slot 0"
        contract is pinned so the mutants are observable instead of pragma'd
        (the #624 observable-over-pragma idiom; DEC-MUTATION-RUNTIME-003
        fixer round — the crash-class ``= None`` halves die elsewhere).
        """
        clock = _Clock()
        ctl = _controller(clock)
        assert ctl._issue_head == 0  # construction site
        with ctl._lock:
            ctl._record_issue_locked(1000.0)  # seed the ring at second 1000
            ctl._record_issue_locked(1005.0)  # advance the head 5 slots
        assert ctl._issue_head == 5
        clock.advance(CLOCK_GAP_THRESHOLD_S + 1.0)
        ctl.on_success()  # trips the sleep/wake gap handler → full ring reset
        assert ctl._issue_head == 0  # gap-reset site
        assert ctl._issue_newest_sec is None

    def test_smooth_wait_overdue_is_exactly_zero(self) -> None:
        """Kills the rephrased ``max(wait, 0.0)`` line's ``0.0 → 1.0`` mutant.

        A long-overdue previous issue clamps the smooth wait to EXACTLY 0.0
        (mutant 126's tie-equivalent conditional is gone; the replacement mutant
        returns 1.0 here). DEC-MUTATION-RUNTIME-003.
        """
        ctl, clock, _slept = _smooth(ceiling_rps=CEILING_RPS)
        ctl.pace()  # stamps _last_issue_ts at 1000.0
        clock.advance(100.0)  # far past the 1/5 s interval
        assert ctl._smooth_wait_locked(ctl._clock()) == 0.0


class TestPendingLogLevelSlotPins:
    """T8 re-probe kills (DEC-MUTATION-RUNTIME-003, MIGRATION.md #1597).

    The scoped mutmut re-run on THIS base enumerated never-probed #1202
    deferred-log mutants. The level SLOT of a pending tuple is invisible to a
    log-sink observer — ``_flush_rate_logs`` dispatches ``level == "info"`` vs
    ELSE-warning, so a mutated ``"warning"`` (``"XXwarningXX"``) emits
    identically — the ONLY kill surface is the raw tuple itself. These tests
    pin every pending-tuple level slot byte-exact by capturing the tuples
    BEFORE dispatch (monkeypatched ``_flush_rate_logs``) or from a
    ``*_locked`` helper's return value, plus the ``_PendingLog`` alias object
    (whose ``= None`` mutant is unobservable through PEP-563 annotations).
    """

    def test_pending_log_alias_is_the_exact_triple_shape(self) -> None:
        """Kills the ``_PendingLog = tuple[...] → None`` alias mutant.

        Under PEP 563 the alias is never evaluated inside an annotation, so
        the module-level alias OBJECT is pinned directly instead. The slot
        alias ``_PendingLogSlot`` (DEC-MUTATION-RUNTIME-003 fixer round) is
        pinned the same way: its ``= None`` mutant never evaluates through a
        PEP-563 annotation, and its ``|`` → ``&`` mutant already dies at
        import (``TypeError`` — a ``GenericAlias`` does not support ``&``).
        """
        assert rate_common._PendingLog == tuple[str, str, dict[str, object]]
        assert rate_common._PendingLogSlot == (rate_common._PendingLog | None)

    def test_clock_gap_pending_tuple_is_byte_exact_including_level(self) -> None:
        """Kills the clock-gap ``"warning" → "XXwarningXX"`` level-slot mutant."""
        clock = _Clock()
        ctl = _controller(clock)
        clock.advance(400.0)  # > CLOCK_GAP_THRESHOLD_S — a real sleep/wake gap
        with ctl._lock:
            pending = ctl._handle_clock_gap_locked(clock.t)
        assert pending == (
            "warning",
            "rate.clock_gap",
            {"gap_s": 400.0, "current_rps": CEILING_RPS},
        )

    def test_smooth_latch_pending_tuple_level_slot_is_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Kills the latch ``"warning" → "XXwarningXX"`` level-slot mutant.

        The tuple is pinned BYTE-EXACT — including the full fields dict — so
        every field KEY mutant dies too (the full-run survivor 183 was the
        ``"scan_seconds"`` key XX-wrapped: the old loose per-field asserts
        never read that key — DEC-MUTATION-RUNTIME-003 fixer round).
        """
        captured: list[tuple[str, str, dict[str, object]]] = []

        def _capture(*events: tuple[str, str, dict[str, object]] | None) -> None:
            captured.extend(e for e in events if e is not None)

        monkeypatch.setattr(rate_common, "_flush_rate_logs", _capture)
        ctl, clock, _slept = _adaptive(ceiling_rps=8.0)
        _seed_burst(ctl, second=int(clock.t), count=9)  # 9 > 8.0 — burst evidence
        ctl.on_rate_limit()  # latches → latch_evt rides the same flush
        latch = [e for e in captured if e[1] == "rate.smooth_pacing_latched"]
        assert latch == [
            (
                "warning",
                "rate.smooth_pacing_latched",
                {
                    "bucket_counts": [0, 0, 9],  # trailing 3 s, oldest → newest
                    "current_rps": 8.0,
                    "scan_seconds": 3,
                },
            )
        ]

    def test_flush_fed_pending_tuples_are_byte_exact_including_levels(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Kills the 429/stepdown/timeout/stepup level-slot mutants.

        Mirrors the T2 deferred-emit golden's scenario but captures the RAW
        (level, event, fields) tuples before dispatch, so the level slot of
        EVERY flush-fed emission site is pinned byte-exact — including the
        ``"info"`` stepup slot, whose mutation WOULD flip the emitted level
        yet was never level-pinned (the existing goldens drop ``log_level``).
        """
        captured: list[tuple[str, str, dict[str, object]]] = []

        def _capture(*events: tuple[str, str, dict[str, object]] | None) -> None:
            captured.extend(e for e in events if e is not None)

        monkeypatch.setattr(rate_common, "_flush_rate_logs", _capture)
        clock = _Clock()
        ctl = _controller(clock)
        for _ in range(STEP_DOWN_HITS):  # 429 burst at one instant → 5→4
            ctl.on_rate_limit()
        for _ in range(STEP_DOWN_TIMEOUT_HITS):  # timeout storm → 4→3
            ctl.on_timeout()
        clock.advance(STEP_UP_WINDOW)  # one clean recovery window
        ctl.on_success()  # → 3→4 (rate.stepup, the one "info" site)
        expected: list[tuple[str, str, dict[str, object]]] = [
            ("warning", "rate.429", {"current_rps": 5.0, "recent_hits": 1}),
            ("warning", "rate.429", {"current_rps": 5.0, "recent_hits": 2}),
            ("warning", "rate.429", {"current_rps": 5.0, "recent_hits": 3}),
            (
                "warning",
                "rate.stepdown",
                {"from_rps": 5.0, "to_rps": 4.0, "floor_rps": FLOOR_RPS},
            ),
            ("warning", "rate.timeout", {"current_rps": 4.0, "recent_timeouts": 1}),
            ("warning", "rate.timeout", {"current_rps": 4.0, "recent_timeouts": 2}),
            ("warning", "rate.timeout", {"current_rps": 4.0, "recent_timeouts": 3}),
            ("warning", "rate.timeout", {"current_rps": 4.0, "recent_timeouts": 4}),
            ("warning", "rate.timeout", {"current_rps": 4.0, "recent_timeouts": 5}),
            (
                "warning",
                "rate.stepdown",
                {
                    "from_rps": 4.0,
                    "to_rps": 3.0,
                    "floor_rps": TIMEOUT_FLOOR_RPS,
                    "cause": "timeout",
                },
            ),
            (
                "info",
                "rate.stepup",
                {"from_rps": 3.0, "to_rps": 4.0, "ceiling_rps": CEILING_RPS},
            ),
        ]
        assert captured == expected


class _RaisingClock:
    """A clock that reads normally until :meth:`arm`, then always raises.

    The sanctioned injected-clock seam (``RateController(clock=...)``) used to
    FORCE the in-lock exception path: ``now = self._clock()`` is the first
    in-lock statement of ``pace``'s two gates and of every ``on_*`` method, so
    once armed the raise fires with every pending-log slot still at its
    ``None`` initializer — the exception-safety path those initializers exist
    for. Construction stays benign (``RateController.__init__`` itself reads
    the clock to seed ``_last_step`` and friends).
    """

    def __init__(self) -> None:
        self.t = 1000.0
        self.armed = False

    def __call__(self) -> float:
        if self.armed:
            raise RuntimeError("forced in-lock failure")
        return self.t

    def arm(self) -> None:
        self.armed = True


class TestInLockExceptionSlotSafety:
    """The documented ``_flush_rate_logs`` exception-safety contract, forced.

    DEC-MUTATION-RUNTIME-003 fixer round 3 (F2): the ``_PendingLogSlot``
    ``None`` initializers on the unconditionally-overwritten slots are LIVE on
    the in-lock-exception path — each method's ``finally`` reads them — and
    that path is forceable through the injected-clock seam (or, for
    ``_maybe_log_snapshot``, whose clock read sits OUTSIDE the try, a raising
    ``_handle_clock_gap_locked``). Each test pins: (a) the injected error
    propagates UNMASKED (an ``= ""`` initializer mutant instead crashes the
    finally's flush — ``"" is not None`` passes the guard and the 3-tuple
    unpack raises ValueError, replacing the RuntimeError); (b) the lock is
    provably released; (c) NO partial rate.* event is emitted. This suite is
    what licenses dropping the 9 line-scoped slot pragmas — the ``""`` mutants
    are now suite-killed, not argued equivalent.
    """

    @staticmethod
    def _assert_lock_released(ctl: RateController) -> None:
        assert ctl._lock.acquire(blocking=False), "controller lock leaked"
        ctl._lock.release()

    def _assert_propagates_clean(self, ctl: RateController, call: Callable[[], None]) -> None:
        with (
            capture_logs() as logs,
            pytest.raises(RuntimeError, match="forced in-lock failure"),
        ):
            call()
        assert logs == []  # no partial event emitted from initializer slots
        self._assert_lock_released(ctl)

    @staticmethod
    def _armed_controller(
        *, smooth_pacing: bool = False, ceiling_per_min: int | None = None
    ) -> RateController:
        clock = _RaisingClock()
        ctl = RateController(
            clock=clock,
            sleep=lambda _s: None,
            smooth_pacing=smooth_pacing,
            ceiling_per_min=ceiling_per_min,
        )
        clock.arm()
        return ctl

    def test_on_rate_limit_in_lock_failure_propagates_unmasked(self) -> None:
        """Kills the ``gap_evt``/``hit_evt``/``latch_evt`` ``None → ""`` mutants."""
        ctl = self._armed_controller()
        self._assert_propagates_clean(ctl, ctl.on_rate_limit)

    def test_on_timeout_in_lock_failure_propagates_unmasked(self) -> None:
        """Kills the ``gap_evt``/``to_evt`` ``None → ""`` mutants."""
        ctl = self._armed_controller()
        self._assert_propagates_clean(ctl, ctl.on_timeout)

    def test_on_success_in_lock_failure_propagates_unmasked(self) -> None:
        """Kills the ``gap_evt`` ``None → ""`` mutant in ``on_success``."""
        ctl = self._armed_controller()
        self._assert_propagates_clean(ctl, ctl.on_success)

    def test_pace_smooth_gate_in_lock_failure_propagates_unmasked(self) -> None:
        """Kills the gate-0 ``gap_evt`` ``None → ""`` mutant in ``pace``."""
        ctl = self._armed_controller(smooth_pacing=True)
        self._assert_propagates_clean(ctl, ctl.pace)

    def test_pace_governor_gate_in_lock_failure_propagates_unmasked(self) -> None:
        """Kills the gate-1 ``gov_gap_evt`` ``None → ""`` mutant in ``pace``.

        Smooth pacing stays OFF (the default) so gate 0 is skipped entirely and
        the FIRST clock read is gate 1's — the raise lands with ``gov_gap_evt``
        at its initializer.
        """
        ctl = self._armed_controller(ceiling_per_min=500)
        self._assert_propagates_clean(ctl, ctl.pace)

    def test_snapshot_log_in_lock_failure_propagates_unmasked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Kills the ``gap_evt`` ``None → ""`` mutant in ``_maybe_log_snapshot``.

        Its ``now = self._clock()`` read sits OUTSIDE the try, so the in-lock
        exception is forced one statement deeper: the first in-lock statement
        is ``self._handle_clock_gap_locked(now)`` — patched to raise.
        """
        ctl = _controller(_Clock())

        def _raise(now: float) -> tuple[str, str, dict[str, object]] | None:
            raise RuntimeError("forced in-lock failure")

        monkeypatch.setattr(ctl, "_handle_clock_gap_locked", _raise)
        self._assert_propagates_clean(ctl, ctl._maybe_log_snapshot)


# ────────────────────────────────────────────────────────────────────────
# DEC-RATE-SLEEP-OUTSIDE-LOCK-001 companion — the transitive controller
# thaw: pre-fix, a worker parked in the BUCKET's retry sleep held the
# bucket lock, so a step-down's `set_rate` (called UNDER the controller
# lock) blocked — freezing every other worker's pace()/snapshot() too.
# ────────────────────────────────────────────────────────────────────────


class _GateSleep:
    """Injected sleep (twin of the rate_limiter suite's): call #1 (the
    bucket's first outside-lock sleep) returns instantly; call #2 (the
    retry-loop sleep) parks on an Event and signals arrival; calls #3+
    return instantly (drains the bounded cap loop fast)."""

    def __init__(self) -> None:
        self.in_retry_sleep = threading.Event()
        self.release = threading.Event()
        self._calls = 0

    def __call__(self, _seconds: float) -> None:
        self._calls += 1
        if self._calls != 2:
            return
        self.in_retry_sleep.set()
        assert self.release.wait(5.0), "test bug: retry sleeper never released"


def test_snapshot_not_blocked_by_bucket_retry_sleep() -> None:
    """snapshot() completes while a paced worker sleeps in the bucket retry loop.

    The production-critical transitive-freeze pin: worker 1's ``pace()`` parks
    in the bucket's retry-loop sleep; worker 2 fires ``on_rate_limit()``
    ``STEP_DOWN_HITS`` times — the step-down's ``set_rate`` blocks on the
    BUCKET lock pre-fix WHILE HOLDING the controller lock, so worker 3's
    ``snapshot()`` (controller lock) froze too. Post-fix both complete in
    microseconds while the sleeper stays parked. Deterministic both ways:
    pre-fix the sleeper provably holds the bucket lock until released.
    """
    gate = _GateSleep()
    ctl = RateController(clock=lambda: 1000.0, sleep=gate, ceiling_rps=5.0)

    worker_done = threading.Event()

    def _pace() -> None:
        ctl.pace()
        worker_done.set()

    worker = threading.Thread(target=_pace, daemon=True)
    worker.start()
    assert gate.in_retry_sleep.wait(5.0)

    stepper_done = threading.Event()

    def _step_down() -> None:
        for _ in range(STEP_DOWN_HITS):
            ctl.on_rate_limit()
        stepper_done.set()

    stepper = threading.Thread(target=_step_down, daemon=True)
    stepper.start()
    stepped = stepper_done.wait(0.5)

    snap_done = threading.Event()

    def _snapshot() -> None:
        ctl.snapshot()
        snap_done.set()

    snapper = threading.Thread(target=_snapshot, daemon=True)
    snapper.start()
    snapped = snap_done.wait(0.5)

    gate.release.set()
    worker.join(5.0)
    stepper.join(5.0)
    snapper.join(5.0)
    assert not worker.is_alive()  # the parked pace() terminated (bounded cap loop)
    assert worker_done.is_set()
    assert stepped, "on_rate_limit() step-down blocked behind the bucket retry sleep"
    assert snapped, "snapshot() blocked behind on_rate_limit behind the retry sleeper"


class TestGovernorLoopCapClockSafety:
    """guarantee-audit BUG-6 (test-only pins): the per-minute governor's fail-OPEN
    loop cap (_GOVERNOR_LOOP_CAP=64, pace() Gate 1) is a deliberate never-deadlock
    posture mirroring the bucket's _ACQUIRE_LOOP_CAP — UNREACHABLE under any clock
    that advances during sleep (production: time.monotonic + time.sleep), and
    reachable ONLY under a pathological non-advancing injected clock + no-op sleep.
    Both arms pinned; the governor code + every VALUE byte-untouched."""

    def test_advancing_clock_never_exceeds_per_min_cap(self) -> None:
        # (a) production-safe arm: deterministic advancing clock whose sleep
        # advances it (the suite's standard idiom). Stress well past the cap:
        # 35 admissions at ceiling_per_min=10 — the rolling-60s law holds on
        # EVERY admission and the fail-open path is never needed.
        clock = {"t": 1000.0}

        def now() -> float:
            return clock["t"]

        def sleep(s: float) -> None:
            clock["t"] += s

        ctl = RateController(clock=now, sleep=sleep, ceiling_rps=1000.0, ceiling_per_min=10)
        stamps: list[float] = []
        for _ in range(35):
            ctl.pace()
            recent = ctl.snapshot()["recent_minute"]
            assert isinstance(recent, int)
            assert recent <= 10  # never beyond the cap
            stamps.append(now())
        for i in range(len(stamps) - 10):  # belt: any 11 admissions
            assert stamps[i + 10] - stamps[i] >= 60.0 - 0.01  # span >= one window

    def test_fail_open_reachable_only_under_frozen_clock_noop_sleep(self) -> None:
        # (b) pin the EXISTING fail-open behavior explicitly as the bounded
        # never-deadlock posture under the production-unreachable clock shape:
        # frozen clock + no-op sleep, cap=3 → paces 4..5 spin exactly
        # _GOVERNOR_LOOP_CAP futile sleeps then fail OPEN and append — pace()
        # TERMINATES (never spins forever) and the deque honestly exceeds the cap.
        sleeps = {"n": 0}

        def noop_sleep(_s: float) -> None:
            sleeps["n"] += 1

        ctl = RateController(
            clock=lambda: 1000.0, sleep=noop_sleep, ceiling_rps=1000.0, ceiling_per_min=3
        )
        for _ in range(5):
            ctl.pace()  # terminates via BOTH fail-opens
        snap = ctl.snapshot()
        assert snap["recent_minute"] == 5  # 2 fail-open admissions past cap=3
        assert snap["ceiling_per_min"] == 3
        # the cap bounded the spin: each fail-open pace slept the governor loop
        # exactly _GOVERNOR_LOOP_CAP times (+ the bucket's own bounded retries) —
        # assert traversal happened AND stayed bounded, without coupling to
        # bucket internals:
        assert sleeps["n"] >= _GOVERNOR_LOOP_CAP  # the cap loop ran
        assert sleeps["n"] <= 5 * (_GOVERNOR_LOOP_CAP + 25)  # and was bounded
