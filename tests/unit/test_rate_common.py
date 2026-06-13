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
from pathlib import Path
from zoneinfo import ZoneInfo

from brutex.adapters.rate_common import (
    _GOVERNOR_LOOP_CAP,
    CEILING_RPS,
    FLOOR_RPS,
    PER_MIN_WINDOW,
    RATE_LIMIT_STORM_FLOOR_RPS,
    STEP_DOWN_HITS,
    STEP_DOWN_TIMEOUT_HITS,
    STEP_DOWN_WINDOW,
    STEP_UP_WINDOW,
    STORM_HITS,
    TIMEOUT_FLOOR_RPS,
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
    # Governor disabled by default → recent_minute 0, ceiling_per_min None.
    assert ctl.snapshot() == {
        "current_rps": 5.0,
        "recent_hits": 0,
        "recent_timeouts": 0,
        "recent_minute": 0,
        "ceiling_per_min": None,
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
        # fields + the always-present governor gauges are the full set.
        assert set(_controller(_Clock()).snapshot()) == {
            "current_rps",
            "recent_hits",
            "recent_timeouts",
            "recent_minute",
            "ceiling_per_min",
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
        # oldest issue ages out (59.8 s), then admits exactly one — never overshooting.
        ctl, _clock, slept = _governed(2)
        ctl.pace()  # issue @1000.2 (bucket 0.2)
        ctl.pace()  # issue @1000.4 (bucket 0.2)
        assert slept == [0.2, 0.2]
        assert ctl.snapshot()["recent_minute"] == 2
        ctl.pace()  # window full → governor waits 0.2+60-0.4 = 59.8, then frees one
        assert any(abs(s - 59.8) < 1e-6 for s in slept)  # the governor block fired
        assert ctl.snapshot()["recent_minute"] == 2  # cap held, no overshoot

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
            assert ctl.snapshot()["recent_minute"] <= 3
