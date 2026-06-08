"""Tests for `brutex.adapters.dhan.rate_control.DhanRateController`.

100% line+branch with an injected fake clock (no real sleeping): starts at the
5 r/s ceiling, steps down 5→4→3 on sustained 429s, clamps at the 3 r/s floor
(no halt), ignores a non-sustained burst, evicts the window, recovers 3→4→5 on
a sustained clean window, never exceeds 5, and `pace()` delegates to the bucket.
"""
# pyright: reportPrivateUsage=none

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from brutex.adapters.dhan.rate_control import (
    CEILING_RPS,
    FLOOR_RPS,
    RATE_LIMIT_STORM_FLOOR_RPS,
    STEP_DOWN_HITS,
    STEP_DOWN_TIMEOUT_HITS,
    STEP_DOWN_WINDOW,
    STEP_UP_WINDOW,
    STORM_HITS,
    TIMEOUT_FLOOR_RPS,
    DhanRateController,
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
    assert ctl.snapshot() == {"current_rps": 5.0, "recent_hits": 0, "recent_timeouts": 0}


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


def test_on_success_blocked_by_recent_hit() -> None:
    clock = _Clock()
    ctl = _controller(clock)
    _hammer(ctl, clock, hits=STEP_DOWN_HITS)  # → 4.0 (buffer cleared on the step), last_step ≈ now
    assert ctl.current_rps == 4.0
    clock.advance(STEP_UP_WINDOW)  # the clean-window TIME is satisfied …
    ctl.on_rate_limit()  # … but a FRESH in-window hit lands right before recovery
    ctl.on_success()  # the hit is NOT aged out (>= cutoff) → not evicted → recovery blocked
    assert ctl.current_rps == 4.0  # did NOT step up (a recent in-window hit blocks it)


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
    ctl.on_success()  # evicts the aged hits → not self._hits → recovery 3→4
    assert ctl.current_rps == 4.0
    assert ctl.snapshot()["recent_hits"] == 0  # the aged 429 hits were evicted by on_success


def _hammer_timeouts(
    ctl: DhanRateController, clock: _Clock, *, hits: int, spread: float = 1.0
) -> None:
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

    def test_recent_timeout_blocks_then_clears_recovery(self) -> None:
        clock = _Clock()
        ctl = _controller(clock)
        _hammer_timeouts(ctl, clock, hits=STEP_DOWN_TIMEOUT_HITS)  # → 4.0 (deque cleared)
        assert ctl.current_rps == 4.0
        clock.advance(STEP_UP_WINDOW + 1.0)  # the clean window has elapsed
        ctl.on_timeout()  # but a FRESH stall lands right before recovery
        ctl.on_success()  # blocked: a recent timeout is still in-window
        assert ctl.current_rps == 4.0
        clock.advance(STEP_DOWN_WINDOW + 1.0)  # the stall ages out
        ctl.on_success()  # now genuinely clean → recover 4→5
        assert ctl.current_rps == CEILING_RPS


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

    ctl = DhanRateController(clock=clock, sleep=_sleep)
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

    ctl = DhanRateController(clock=clock, sleep=_sleep)
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

    ctl = DhanRateController(clock=clock, sleep=_sleep)
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
        ctl = DhanRateController(
            clock=_Clock(), sleep=lambda _s: None, quota_tracker=_quota(tmp_path, limit=10, used=4)
        )
        snap = ctl.snapshot()
        assert snap["current_rps"] == 5.0  # base rate fields still present
        assert snap["quota_used"] == 4
        assert snap["quota_remaining"] == 6
        assert snap["quota_limit"] == 10

    def test_snapshot_omits_quota_without_tracker(self) -> None:
        # The default (test) controller has no tracker → snapshot is byte-unchanged.
        assert set(_controller(_Clock()).snapshot()) == {
            "current_rps",
            "recent_hits",
            "recent_timeouts",
        }

    def test_quota_exhausted_true_at_limit(self, tmp_path: Path) -> None:
        ctl = DhanRateController(
            clock=_Clock(), sleep=lambda _s: None, quota_tracker=_quota(tmp_path, limit=2, used=2)
        )
        assert ctl.quota_exhausted() is True

    def test_quota_exhausted_false_with_remaining(self, tmp_path: Path) -> None:
        ctl = DhanRateController(
            clock=_Clock(), sleep=lambda _s: None, quota_tracker=_quota(tmp_path, limit=5, used=1)
        )
        assert ctl.quota_exhausted() is False

    def test_quota_exhausted_false_without_tracker(self) -> None:
        # No tracker wired (the bucket-only mode) → never the limit.
        assert _controller(_Clock()).quota_exhausted() is False
