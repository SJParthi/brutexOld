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

from datetime import datetime
from itertools import pairwise
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
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
        with pytest.raises(ValueError, match="unknown smooth_pacing_mode"):
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
