"""Unit tests for the live pull-rate view projection (DEC-LIVE-RATE-MONITOR-001).

Drives :func:`brutex.dashboard.pull_rate_view.build_pull_rate_view` with explicit
injected values and asserts the honest idle / flowing / stalled projection + the
``to_dict`` JSON shape — full line + branch coverage of the pure module, no clock / no
network / no global state.
"""

from __future__ import annotations

import math

from brutex.dashboard.pull_rate_view import (
    CEILING_PER_MIN_LIMIT,
    CEILING_RPS_LIMIT,
    FLOW_FLOWING,
    FLOW_IDLE,
    FLOW_STALLED,
    STALE_AFTER_S,
    STALL_RPS_FLOOR,
    PullRateView,
    build_pull_rate_view,
)


def test_flowing_when_recent_tick_above_floor() -> None:
    """A recent tick with achieved r/s above the floor → flowing (green)."""
    view = build_pull_rate_view(
        effective_rps=6.4,
        current_rps=8.0,
        recent_minute=380,
        recent_429_count=2,
        last_update_monotonic=100.0,
        now_monotonic=101.0,
    )
    assert view.flow_state == FLOW_FLOWING
    assert view.active is True
    assert view.stalled is False
    assert view.req_per_sec_achieved == 6.4
    assert view.req_per_sec_allowed == 8.0
    assert view.req_per_min == 380
    # req/hour is the OBSERVED extrapolation = req/min × 60.
    assert view.req_per_hour_observed == 380 * 60
    assert view.recent_429_count == 2
    assert view.seconds_since_update == 1.0
    assert view.ceiling_rps == CEILING_RPS_LIMIT == 8.0
    assert view.ceiling_per_min == CEILING_PER_MIN_LIMIT == 500


def test_stalled_when_active_but_achieved_at_or_below_floor() -> None:
    """An active tick with achieved r/s at/below the floor → stalled (red)."""
    view = build_pull_rate_view(
        effective_rps=STALL_RPS_FLOOR,  # exactly the floor counts as stalled (<=)
        current_rps=8.0,
        recent_minute=3,
        recent_429_count=40,
        last_update_monotonic=50.0,
        now_monotonic=52.0,
    )
    assert view.flow_state == FLOW_STALLED
    assert view.active is True
    assert view.stalled is True
    # The live numbers are still shown (a stalled pull IS active, just barely moving).
    assert view.req_per_sec_achieved == STALL_RPS_FLOOR
    assert view.req_per_min == 3
    assert view.recent_429_count == 40


def test_idle_when_tick_is_stale() -> None:
    """No tick within the staleness window → idle, every live number ZEROED."""
    view = build_pull_rate_view(
        effective_rps=8.0,  # a stale LIVE value that must NOT be shown
        current_rps=8.0,
        recent_minute=400,
        recent_429_count=5,
        last_update_monotonic=10.0,
        now_monotonic=10.0 + STALE_AFTER_S + 0.01,  # just past stale
    )
    assert view.flow_state == FLOW_IDLE
    assert view.active is False
    assert view.stalled is False
    assert view.req_per_sec_achieved == 0.0
    assert view.req_per_sec_allowed == 0.0
    assert view.req_per_min == 0
    assert view.req_per_hour_observed == 0
    assert view.recent_429_count == 0
    # The staleness gauge + limits stay REAL even while idle.
    assert view.seconds_since_update > STALE_AFTER_S
    assert view.ceiling_rps == CEILING_RPS_LIMIT
    assert view.ceiling_per_min == CEILING_PER_MIN_LIMIT


def test_idle_when_never_updated_yields_inf_age() -> None:
    """A never-updated panel (-inf baseline) → idle with an infinite age."""
    view = build_pull_rate_view(
        effective_rps=0.0,
        current_rps=0.0,
        recent_minute=0,
        recent_429_count=0,
        last_update_monotonic=float("-inf"),
        now_monotonic=999.0,
    )
    assert view.flow_state == FLOW_IDLE
    assert view.active is False
    assert math.isinf(view.seconds_since_update)


def test_exactly_at_stale_boundary_is_active() -> None:
    """Age exactly == stale_after_s is still ACTIVE (the boundary is inclusive)."""
    view = build_pull_rate_view(
        effective_rps=5.0,
        current_rps=8.0,
        recent_minute=300,
        recent_429_count=0,
        last_update_monotonic=0.0,
        now_monotonic=STALE_AFTER_S,
    )
    assert view.active is True
    assert view.flow_state == FLOW_FLOWING


def test_negative_age_clamped_to_zero() -> None:
    """A non-advancing monotonic clock can never read as a negative age."""
    view = build_pull_rate_view(
        effective_rps=5.0,
        current_rps=8.0,
        recent_minute=300,
        recent_429_count=0,
        last_update_monotonic=100.0,
        now_monotonic=99.5,  # now < last (clock did not advance / tiny skew)
    )
    assert view.seconds_since_update == 0.0
    assert view.active is True


def test_overridable_thresholds() -> None:
    """Custom stale / stall thresholds are honoured (injectable for tests)."""
    # With a 1 s staleness window, a 2 s-old tick is idle even though it would be
    # active under the default 12 s window.
    view = build_pull_rate_view(
        effective_rps=8.0,
        current_rps=8.0,
        recent_minute=400,
        recent_429_count=0,
        last_update_monotonic=0.0,
        now_monotonic=2.0,
        stale_after_s=1.0,
    )
    assert view.flow_state == FLOW_IDLE
    # With a high stall floor, a healthy-looking 5 r/s reads as stalled.
    stalled = build_pull_rate_view(
        effective_rps=5.0,
        current_rps=8.0,
        recent_minute=300,
        recent_429_count=0,
        last_update_monotonic=0.0,
        now_monotonic=1.0,
        stall_rps_floor=6.0,
    )
    assert stalled.flow_state == FLOW_STALLED


def test_to_dict_active_keeps_finite_age() -> None:
    view = build_pull_rate_view(
        effective_rps=6.4,
        current_rps=8.0,
        recent_minute=380,
        recent_429_count=2,
        last_update_monotonic=100.0,
        now_monotonic=102.5,
    )
    data = view.to_dict()
    assert data["flow_state"] == FLOW_FLOWING
    assert data["seconds_since_update"] == 2.5
    assert data["req_per_hour_observed"] == 380 * 60


def test_to_dict_inf_age_rendered_as_none() -> None:
    """``inf`` staleness is JSON-rendered as ``None`` (inf is not valid JSON)."""
    view = build_pull_rate_view(
        effective_rps=0.0,
        current_rps=0.0,
        recent_minute=0,
        recent_429_count=0,
        last_update_monotonic=float("-inf"),
        now_monotonic=0.0,
    )
    data = view.to_dict()
    assert data["seconds_since_update"] is None
    assert data["flow_state"] == FLOW_IDLE


def test_view_is_frozen() -> None:
    """The view is immutable (frozen dataclass)."""
    import dataclasses

    view = build_pull_rate_view(
        effective_rps=1.0,
        current_rps=1.0,
        recent_minute=1,
        recent_429_count=0,
        last_update_monotonic=0.0,
        now_monotonic=0.0,
    )
    assert isinstance(view, PullRateView)
    try:
        view.flow_state = "hacked"  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        pass
    else:  # pragma: no cover - frozen dataclass always raises
        raise AssertionError("PullRateView should be frozen")
