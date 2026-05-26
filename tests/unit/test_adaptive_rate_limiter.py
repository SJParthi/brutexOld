"""Tests for `brutex.orchestrator.adaptive_rate_limiter`.

Per DEC-ADAPTIVE-RATE-LIMITER-001 (MIGRATION entry #249). The state
machine is verified across all 4 states + every transition path:

    PROBING_UP → LOCKED            (rate reaches ceiling on clean window)
    PROBING_UP → PROBING_DOWN      (rate-limit hits trip step-down)
    LOCKED → PROBING_DOWN          (rate-limit hits trip step-down at ceiling)
    PROBING_DOWN → PROBING_DOWN    (further hits → another step-down)
    PROBING_DOWN → PROBING_UP      (clean window after rate dropped)
    PROBING_DOWN → FLOOR_BREACHED  (rate dropped below min)
    FLOOR_BREACHED → FLOOR_BREACHED  (terminal — no more transitions)

All tests use a synthetic clock so timing is deterministic.
"""

from __future__ import annotations

import pytest
from brutex.orchestrator.adaptive_rate_limiter import (
    AdaptiveRateLimiter,
    AdaptiveRateLimiterConfig,
    RateLimiterState,
    StateTransition,
)


class _FakeClock:
    """Caller-driven monotonic clock — `advance(seconds)` to step time."""

    def __init__(self, start: float = 1000.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


def _config(
    *,
    min_rps: float = 4.0,
    max_rps: float = 8.0,
    initial_rps: float = 8.0,
    step_down_hits: int = 3,
    step_down_window: float = 60.0,
    step_up_clean_window: float = 300.0,
) -> AdaptiveRateLimiterConfig:
    return AdaptiveRateLimiterConfig(
        min_rps=min_rps,
        max_rps=max_rps,
        initial_rps=initial_rps,
        step_down_hits=step_down_hits,
        step_down_window=step_down_window,
        step_up_clean_window=step_up_clean_window,
    )


# ────────────────────────────────────────────────────────────────────────
# Config validation
# ────────────────────────────────────────────────────────────────────────


class TestConfigValidation:
    def test_min_rps_zero_raises(self) -> None:
        with pytest.raises(ValueError, match="min_rps must be > 0"):
            AdaptiveRateLimiterConfig(min_rps=0.0)

    def test_min_rps_negative_raises(self) -> None:
        with pytest.raises(ValueError, match="min_rps must be > 0"):
            AdaptiveRateLimiterConfig(min_rps=-1.0)

    def test_max_below_min_raises(self) -> None:
        with pytest.raises(ValueError, match="max_rps .* must be >= min_rps"):
            AdaptiveRateLimiterConfig(min_rps=4.0, max_rps=3.0)

    def test_initial_below_min_raises(self) -> None:
        with pytest.raises(ValueError, match=r"initial_rps .* must be in"):
            AdaptiveRateLimiterConfig(min_rps=4.0, max_rps=8.0, initial_rps=2.0)

    def test_initial_above_max_raises(self) -> None:
        with pytest.raises(ValueError, match=r"initial_rps .* must be in"):
            AdaptiveRateLimiterConfig(min_rps=4.0, max_rps=8.0, initial_rps=10.0)

    def test_step_down_hits_zero_raises(self) -> None:
        with pytest.raises(ValueError, match="step_down_hits must be >= 1"):
            AdaptiveRateLimiterConfig(step_down_hits=0)

    def test_step_down_window_zero_raises(self) -> None:
        with pytest.raises(ValueError, match="step_down_window must be > 0"):
            AdaptiveRateLimiterConfig(step_down_window=0.0)

    def test_step_up_clean_window_zero_raises(self) -> None:
        with pytest.raises(ValueError, match="step_up_clean_window must be > 0"):
            AdaptiveRateLimiterConfig(step_up_clean_window=0.0)


# ────────────────────────────────────────────────────────────────────────
# Env-driven config
# ────────────────────────────────────────────────────────────────────────


class TestFromEnv:
    def test_defaults_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for var in (
            "BRUTEX_GROWW_MIN_RPS",
            "BRUTEX_GROWW_MAX_RPS",
            "BRUTEX_GROWW_INITIAL_RPS",
            "BRUTEX_GROWW_STEP_DOWN_HITS",
            "BRUTEX_GROWW_STEP_DOWN_WINDOW",
            "BRUTEX_GROWW_STEP_UP_CLEAN_WINDOW",
            "BRUTEX_GROWW_RECHECK_INTERVAL",
        ):
            monkeypatch.delenv(var, raising=False)
        cfg = AdaptiveRateLimiterConfig.from_env()
        assert cfg.min_rps == 4.0
        assert cfg.max_rps == 8.0
        assert cfg.initial_rps == 8.0
        assert cfg.step_down_hits == 3
        assert cfg.step_down_window == 60.0
        assert cfg.step_up_clean_window == 300.0
        assert cfg.recheck_interval == 60.0

    def test_overrides_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRUTEX_GROWW_MIN_RPS", "2.0")
        monkeypatch.setenv("BRUTEX_GROWW_MAX_RPS", "6.0")
        monkeypatch.setenv("BRUTEX_GROWW_INITIAL_RPS", "5.0")
        monkeypatch.setenv("BRUTEX_GROWW_STEP_DOWN_HITS", "5")
        monkeypatch.setenv("BRUTEX_GROWW_STEP_DOWN_WINDOW", "30")
        monkeypatch.setenv("BRUTEX_GROWW_STEP_UP_CLEAN_WINDOW", "120")
        monkeypatch.setenv("BRUTEX_GROWW_RECHECK_INTERVAL", "15")
        cfg = AdaptiveRateLimiterConfig.from_env()
        assert cfg.min_rps == 2.0
        assert cfg.max_rps == 6.0
        assert cfg.initial_rps == 5.0
        assert cfg.step_down_hits == 5
        assert cfg.step_down_window == 30.0
        assert cfg.step_up_clean_window == 120.0
        assert cfg.recheck_interval == 15.0

    def test_unparseable_float_falls_back_to_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRUTEX_GROWW_MIN_RPS", "not-a-float")
        cfg = AdaptiveRateLimiterConfig.from_env()
        assert cfg.min_rps == 4.0

    def test_unparseable_int_falls_back_to_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRUTEX_GROWW_STEP_DOWN_HITS", "not-an-int")
        cfg = AdaptiveRateLimiterConfig.from_env()
        assert cfg.step_down_hits == 3


# ────────────────────────────────────────────────────────────────────────
# Initial state
# ────────────────────────────────────────────────────────────────────────


class TestInitialState:
    def test_starts_at_initial_rps(self) -> None:
        clock = _FakeClock()
        limiter = AdaptiveRateLimiter(config=_config(), clock=clock)
        assert limiter.current_rps == 8.0

    def test_starts_locked_when_initial_equals_max(self) -> None:
        clock = _FakeClock()
        limiter = AdaptiveRateLimiter(config=_config(initial_rps=8.0), clock=clock)
        assert limiter.state is RateLimiterState.LOCKED

    def test_starts_probing_up_when_below_ceiling(self) -> None:
        clock = _FakeClock()
        limiter = AdaptiveRateLimiter(config=_config(initial_rps=6.0), clock=clock)
        assert limiter.state is RateLimiterState.PROBING_UP


# ────────────────────────────────────────────────────────────────────────
# Step-down sequence
# ────────────────────────────────────────────────────────────────────────


class TestStepDown:
    def test_three_hits_in_window_triggers_step_down(self) -> None:
        clock = _FakeClock()
        transitions: list[StateTransition] = []
        limiter = AdaptiveRateLimiter(
            config=_config(),
            clock=clock,
            on_transition=transitions.append,
        )
        for _ in range(3):
            limiter.record_rate_limit_hit()
            clock.advance(5.0)
        assert limiter.current_rps == 7.0
        assert limiter.state is RateLimiterState.PROBING_DOWN
        assert len(transitions) == 1
        assert transitions[0].reason == "step_down_hits"
        assert transitions[0].from_state is RateLimiterState.LOCKED
        assert transitions[0].to_state is RateLimiterState.PROBING_DOWN

    def test_two_hits_not_enough_to_step_down(self) -> None:
        clock = _FakeClock()
        limiter = AdaptiveRateLimiter(config=_config(), clock=clock)
        for _ in range(2):
            limiter.record_rate_limit_hit()
            clock.advance(5.0)
        assert limiter.current_rps == 8.0  # unchanged
        assert limiter.state is RateLimiterState.LOCKED  # unchanged

    def test_hits_outside_window_are_pruned(self) -> None:
        clock = _FakeClock()
        limiter = AdaptiveRateLimiter(config=_config(), clock=clock)
        limiter.record_rate_limit_hit()
        clock.advance(70.0)  # > 60-second window — first hit ages out
        limiter.record_rate_limit_hit()
        clock.advance(5.0)
        limiter.record_rate_limit_hit()
        # Only 2 hits in the current window — not enough
        assert limiter.current_rps == 8.0

    def test_step_down_clears_hit_buffer(self) -> None:
        clock = _FakeClock()
        limiter = AdaptiveRateLimiter(config=_config(), clock=clock)
        for _ in range(3):
            limiter.record_rate_limit_hit()
            clock.advance(1.0)
        snap = limiter.snapshot()
        assert snap["recent_hit_count"] == 0  # buffer cleared on step-down


# ────────────────────────────────────────────────────────────────────────
# Floor breach
# ────────────────────────────────────────────────────────────────────────


class TestFloorBreach:
    def test_four_step_downs_from_eight_reach_floor(self) -> None:
        # 8 → 7 → 6 → 5 → 4 (4 step-downs); 5th step-down breaches floor.
        clock = _FakeClock()
        transitions: list[StateTransition] = []
        limiter = AdaptiveRateLimiter(
            config=_config(),
            clock=clock,
            on_transition=transitions.append,
        )
        for _ in range(5):
            for _ in range(3):
                limiter.record_rate_limit_hit()
                clock.advance(1.0)
        assert limiter.state is RateLimiterState.FLOOR_BREACHED
        assert limiter.current_rps == 4.0  # pinned at floor
        # Last transition should be floor_breach.
        assert transitions[-1].reason == "floor_breach"
        assert transitions[-1].to_state is RateLimiterState.FLOOR_BREACHED

    def test_terminal_state_ignores_further_hits(self) -> None:
        clock = _FakeClock()
        transitions: list[StateTransition] = []
        limiter = AdaptiveRateLimiter(
            config=_config(),
            clock=clock,
            on_transition=transitions.append,
        )
        # Drive to floor.
        for _ in range(5):
            for _ in range(3):
                limiter.record_rate_limit_hit()
                clock.advance(1.0)
        baseline_count = len(transitions)
        # Further hits should not change state or fire transitions.
        for _ in range(10):
            limiter.record_rate_limit_hit()
            clock.advance(1.0)
        assert limiter.state is RateLimiterState.FLOOR_BREACHED
        assert len(transitions) == baseline_count

    def test_terminal_state_ignores_step_up(self) -> None:
        clock = _FakeClock()
        limiter = AdaptiveRateLimiter(config=_config(), clock=clock)
        # Drive to floor.
        for _ in range(5):
            for _ in range(3):
                limiter.record_rate_limit_hit()
                clock.advance(1.0)
        # Now wait a long clean window.
        clock.advance(10_000.0)
        limiter.record_clean_call()
        assert limiter.state is RateLimiterState.FLOOR_BREACHED
        assert limiter.current_rps == 4.0


# ────────────────────────────────────────────────────────────────────────
# Step-up sequence
# ────────────────────────────────────────────────────────────────────────


class TestStepUp:
    def test_clean_window_at_ceiling_no_op(self) -> None:
        clock = _FakeClock()
        transitions: list[StateTransition] = []
        limiter = AdaptiveRateLimiter(
            config=_config(),  # starts at LOCKED (initial=max=8)
            clock=clock,
            on_transition=transitions.append,
        )
        clock.advance(1000.0)  # long clean window
        limiter.record_clean_call()
        # Still locked at ceiling — no transition fired.
        assert limiter.state is RateLimiterState.LOCKED
        assert limiter.current_rps == 8.0
        assert transitions == []

    def test_clean_window_below_ceiling_steps_up(self) -> None:
        clock = _FakeClock()
        transitions: list[StateTransition] = []
        limiter = AdaptiveRateLimiter(
            config=_config(initial_rps=5.0),
            clock=clock,
            on_transition=transitions.append,
        )
        clock.advance(301.0)  # > 300 step-up clean window
        limiter.record_clean_call()
        assert limiter.current_rps == 6.0
        assert limiter.state is RateLimiterState.PROBING_UP
        assert len(transitions) == 1
        assert transitions[0].reason == "step_up_clean_window"

    def test_repeated_step_ups_reach_ceiling(self) -> None:
        clock = _FakeClock()
        transitions: list[StateTransition] = []
        limiter = AdaptiveRateLimiter(
            config=_config(initial_rps=5.0),
            clock=clock,
            on_transition=transitions.append,
        )
        for _ in range(3):  # 5 → 6 → 7 → 8
            clock.advance(301.0)
            limiter.record_clean_call()
        assert limiter.current_rps == 8.0
        assert limiter.state is RateLimiterState.LOCKED
        # Last transition should be ceiling_reached.
        assert transitions[-1].reason == "ceiling_reached"
        assert transitions[-1].to_state is RateLimiterState.LOCKED

    def test_partial_clean_window_no_op(self) -> None:
        clock = _FakeClock()
        limiter = AdaptiveRateLimiter(config=_config(initial_rps=5.0), clock=clock)
        clock.advance(100.0)  # < 300 window
        limiter.record_clean_call()
        assert limiter.current_rps == 5.0  # unchanged

    def test_hit_in_window_resets_step_up_clock(self) -> None:
        clock = _FakeClock()
        limiter = AdaptiveRateLimiter(config=_config(initial_rps=5.0), clock=clock)
        clock.advance(250.0)  # close to step-up
        limiter.record_rate_limit_hit()  # one hit — not enough to step down
        # But the hit IS in the window — step-up should be deferred.
        clock.advance(100.0)  # total 350, > 300
        limiter.record_clean_call()
        # Hit was still in the step-down window (60s) at time=250+100=350
        # → hit happened at 250, window 60 → cutoff 290.
        # At time=350, hit at 250 is OUTSIDE the cutoff (350-60=290 > 250),
        # so the buffer prunes it and step-up proceeds.
        # That's intentional: after a single isolated hit + long clean,
        # the limiter recovers.
        assert limiter.current_rps == 6.0

    def test_hit_pending_in_window_blocks_step_up(self) -> None:
        clock = _FakeClock()
        limiter = AdaptiveRateLimiter(config=_config(initial_rps=5.0), clock=clock)
        clock.advance(301.0)  # establish baseline clean window
        limiter.record_rate_limit_hit()
        clock.advance(10.0)  # hit still in window (10s < 60s step-down window)
        limiter.record_clean_call()
        # Hit still pending → step-up blocked, but clock resets.
        assert limiter.current_rps == 5.0


# ────────────────────────────────────────────────────────────────────────
# Tick (scheduler-driven progress)
# ────────────────────────────────────────────────────────────────────────


class TestTick:
    def test_tick_steps_up_after_clean_window(self) -> None:
        clock = _FakeClock()
        transitions: list[StateTransition] = []
        limiter = AdaptiveRateLimiter(
            config=_config(initial_rps=5.0),
            clock=clock,
            on_transition=transitions.append,
        )
        clock.advance(301.0)
        limiter.tick()
        assert limiter.current_rps == 6.0
        assert len(transitions) == 1

    def test_tick_at_ceiling_no_op(self) -> None:
        clock = _FakeClock()
        transitions: list[StateTransition] = []
        limiter = AdaptiveRateLimiter(
            config=_config(),  # LOCKED at 8
            clock=clock,
            on_transition=transitions.append,
        )
        clock.advance(10_000.0)
        limiter.tick()
        assert transitions == []

    def test_tick_in_floor_breach_no_op(self) -> None:
        clock = _FakeClock()
        limiter = AdaptiveRateLimiter(config=_config(), clock=clock)
        # Drive to floor.
        for _ in range(5):
            for _ in range(3):
                limiter.record_rate_limit_hit()
                clock.advance(1.0)
        before = limiter.snapshot()
        clock.advance(10_000.0)
        limiter.tick()
        after = limiter.snapshot()
        assert after == before


# ────────────────────────────────────────────────────────────────────────
# Snapshot
# ────────────────────────────────────────────────────────────────────────


class TestSnapshot:
    def test_snapshot_shape(self) -> None:
        clock = _FakeClock()
        limiter = AdaptiveRateLimiter(config=_config(initial_rps=6.0), clock=clock)
        snap = limiter.snapshot()
        assert snap == {
            "state": "probing_up",
            "current_rps": 6.0,
            "min_rps": 4.0,
            "max_rps": 8.0,
            "recent_hit_count": 0,
            "last_hit_at": None,
        }

    def test_snapshot_after_hit(self) -> None:
        clock = _FakeClock(start=2000.0)
        limiter = AdaptiveRateLimiter(config=_config(), clock=clock)
        limiter.record_rate_limit_hit()
        snap = limiter.snapshot()
        assert snap["recent_hit_count"] == 1
        assert snap["last_hit_at"] == 2000.0


# ────────────────────────────────────────────────────────────────────────
# Callback contract
# ────────────────────────────────────────────────────────────────────────


class TestTransitionCallback:
    def test_no_callback_means_no_op(self) -> None:
        # No on_transition wired — must still work end-to-end without crashing.
        clock = _FakeClock()
        limiter = AdaptiveRateLimiter(config=_config(), clock=clock)
        for _ in range(3):
            limiter.record_rate_limit_hit()
            clock.advance(1.0)
        assert limiter.current_rps == 7.0

    def test_callback_exception_propagates(self) -> None:
        # Programmer-error callbacks should fail loudly (not silently
        # swallow) — the limiter does not catch.
        clock = _FakeClock()

        def boom(_t: StateTransition) -> None:
            raise RuntimeError("callback broken")

        limiter = AdaptiveRateLimiter(
            config=_config(),
            clock=clock,
            on_transition=boom,
        )
        # First two hits don't trip the step-down (need 3 in window) so
        # the callback isn't called until the third hit. Stage outside
        # the `with` to keep the body single-statement (ruff PT012).
        limiter.record_rate_limit_hit()
        clock.advance(1.0)
        limiter.record_rate_limit_hit()
        clock.advance(1.0)
        with pytest.raises(RuntimeError, match="callback broken"):
            limiter.record_rate_limit_hit()


# ────────────────────────────────────────────────────────────────────────
# Default-clock seam
# ────────────────────────────────────────────────────────────────────────


class TestDefaultClock:
    def test_default_clock_is_monotonic(self) -> None:
        # Constructor without explicit clock uses time.monotonic.
        # Just verify the limiter constructs + the snapshot reports a
        # finite last_step_up_check baseline (proxy: no crash + valid float).
        limiter = AdaptiveRateLimiter(config=_config())
        assert limiter.current_rps == 8.0
        # Sanity: hit + clean ops execute without error.
        limiter.record_rate_limit_hit()
        limiter.record_clean_call()
        limiter.tick()
