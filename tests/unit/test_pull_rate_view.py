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
    build_pull_rate_view_merged,
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
        recent_1s=7,
        recent_600s=3800,
        recent_3600s=22800,
    )
    assert view.flow_state == FLOW_FLOWING
    assert view.active is True
    assert view.stalled is False
    assert view.req_per_sec_achieved == 6.4
    assert view.req_per_sec_allowed == 8.0
    assert view.req_per_min == 380
    # req/hour is the OBSERVED extrapolation = req/min × 60.
    assert view.req_per_hour_observed == 380 * 60
    # MEASURED trailing-window real counts (DEC-RATE-MONITOR-EXPAND-001) — the new metrics +
    # evidence. req/10min is the real trailing-600s count, req/hour MEASURED is the real
    # trailing-3600s count, and the req/sec count is the trailing-1s evidence.
    assert view.req_per_sec_count == 7
    assert view.req_per_10min == 3800
    assert view.req_per_hour_measured == 22800
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
        recent_1s=8,  # stale measured counts that must ALSO be zeroed when idle
        recent_600s=4800,
        recent_3600s=28800,
    )
    assert view.flow_state == FLOW_IDLE
    assert view.active is False
    assert view.stalled is False
    assert view.req_per_sec_achieved == 0.0
    assert view.req_per_sec_allowed == 0.0
    assert view.req_per_min == 0
    assert view.req_per_hour_observed == 0
    # The measured trailing-window counts are ALSO zeroed when idle (never a stale "live"
    # count — Golden Rule #1).
    assert view.req_per_sec_count == 0
    assert view.req_per_10min == 0
    assert view.req_per_hour_measured == 0
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


# ---------------------------------------------------------------------------
# build_pull_rate_view_merged — the cross-process bridge merge (DEC-CLI-RATE-BRIDGE-001).
# ---------------------------------------------------------------------------


def test_merged_no_bridge_projects_panel_byte_identically() -> None:
    """No bridge file (every bridge arg None) → the panel alone, identical to the legacy view."""
    merged = build_pull_rate_view_merged(
        panel_effective_rps=6.4,
        panel_current_rps=8.0,
        panel_recent_minute=380,
        panel_recent_429_count=2,
        panel_seconds_since_update=1.0,
    )
    legacy = build_pull_rate_view(
        effective_rps=6.4,
        current_rps=8.0,
        recent_minute=380,
        recent_429_count=2,
        last_update_monotonic=0.0,
        now_monotonic=1.0,
    )
    assert merged == legacy
    assert merged.flow_state == FLOW_FLOWING


def test_merged_no_bridge_never_updated_panel_is_idle_inf() -> None:
    """No bridge + a never-updated panel (inf age) → idle with an infinite age (the -inf path)."""
    merged = build_pull_rate_view_merged(
        panel_effective_rps=0.0,
        panel_current_rps=0.0,
        panel_recent_minute=0,
        panel_recent_429_count=0,
        panel_seconds_since_update=math.inf,
    )
    assert merged.flow_state == FLOW_IDLE
    assert math.isinf(merged.seconds_since_update)


def test_merged_bridge_fresher_surfaces_cli_numbers() -> None:
    """A bridge fresher than the panel → the CLI bridge numbers win (flowing)."""
    merged = build_pull_rate_view_merged(
        panel_effective_rps=0.0,
        panel_current_rps=0.0,
        panel_recent_minute=0,
        panel_recent_429_count=0,
        panel_seconds_since_update=math.inf,  # dashboard never pulled
        bridge_effective_rps=4.8,
        bridge_current_rps=5.0,
        bridge_recent_minute=290,
        bridge_recent_429_count=3,
        bridge_seconds_since_update=1.5,  # CLI ticked 1.5 s ago
    )
    assert merged.flow_state == FLOW_FLOWING
    assert merged.req_per_sec_achieved == 4.8
    assert merged.req_per_sec_allowed == 5.0
    assert merged.req_per_min == 290
    assert merged.recent_429_count == 3
    assert merged.seconds_since_update == 1.5


def test_merged_panel_fresher_keeps_dashboard_numbers() -> None:
    """A panel fresher than the bridge → the dashboard numbers win (the bridge is older)."""
    merged = build_pull_rate_view_merged(
        panel_effective_rps=7.0,
        panel_current_rps=8.0,
        panel_recent_minute=400,
        panel_recent_429_count=1,
        panel_seconds_since_update=0.5,  # dashboard ticked 0.5 s ago
        bridge_effective_rps=4.8,
        bridge_current_rps=5.0,
        bridge_recent_minute=290,
        bridge_recent_429_count=3,
        bridge_seconds_since_update=2.0,  # CLI bridge is older
    )
    assert merged.req_per_sec_achieved == 7.0
    assert merged.req_per_min == 400
    assert merged.seconds_since_update == 0.5


def test_merged_equal_age_prefers_panel() -> None:
    """Equal ages → the panel wins (the strict `<` guard means a tie is NOT the bridge)."""
    merged = build_pull_rate_view_merged(
        panel_effective_rps=7.0,
        panel_current_rps=8.0,
        panel_recent_minute=400,
        panel_recent_429_count=0,
        panel_seconds_since_update=2.0,
        bridge_effective_rps=4.8,
        bridge_current_rps=5.0,
        bridge_recent_minute=290,
        bridge_recent_429_count=0,
        bridge_seconds_since_update=2.0,
    )
    assert merged.req_per_sec_achieved == 7.0


def test_merged_bridge_fresher_but_stale_is_idle() -> None:
    """The bridge is the fresher source but itself stale → idle (every live number zeroed)."""
    merged = build_pull_rate_view_merged(
        panel_effective_rps=0.0,
        panel_current_rps=0.0,
        panel_recent_minute=0,
        panel_recent_429_count=0,
        panel_seconds_since_update=math.inf,
        bridge_effective_rps=4.8,
        bridge_current_rps=5.0,
        bridge_recent_minute=290,
        bridge_recent_429_count=3,
        bridge_seconds_since_update=STALE_AFTER_S + 1.0,  # CLI ended a while ago
    )
    assert merged.flow_state == FLOW_IDLE
    assert merged.req_per_sec_achieved == 0.0
    assert merged.req_per_min == 0


def test_merged_bridge_age_present_but_fields_none_defaults_safely() -> None:
    """A fresher bridge age with None fields defaults each to 0 (the defensive else branches)."""
    merged = build_pull_rate_view_merged(
        panel_effective_rps=9.0,
        panel_current_rps=8.0,
        panel_recent_minute=400,
        panel_recent_429_count=9,
        panel_seconds_since_update=math.inf,
        # The bridge is fresher (a finite age beats inf) but its value fields are absent.
        bridge_effective_rps=None,
        bridge_current_rps=None,
        bridge_recent_minute=None,
        bridge_recent_429_count=None,
        bridge_seconds_since_update=1.0,
    )
    # All bridge fields defaulted to 0 → achieved 0.0 ≤ floor while ACTIVE ⇒ stalled.
    assert merged.flow_state == FLOW_STALLED
    assert merged.req_per_sec_achieved == 0.0
    assert merged.req_per_sec_allowed == 0.0
    assert merged.req_per_min == 0
    assert merged.recent_429_count == 0
    assert merged.seconds_since_update == 1.0


def test_to_dict_carries_measured_window_fields() -> None:
    """The JSON payload exposes the new measured req/10min + req/hour + req/sec evidence."""
    view = build_pull_rate_view(
        effective_rps=6.4,
        current_rps=8.0,
        recent_minute=380,
        recent_429_count=2,
        last_update_monotonic=100.0,
        now_monotonic=101.0,
        recent_1s=7,
        recent_600s=3800,
        recent_3600s=22800,
    )
    data = view.to_dict()
    assert data["req_per_sec_count"] == 7
    assert data["req_per_10min"] == 3800
    assert data["req_per_hour_measured"] == 22800
    # The older observed extrapolation is RETAINED alongside the measured count.
    assert data["req_per_hour_observed"] == 380 * 60


def test_merged_panel_measured_counts_flow_through() -> None:
    """The in-memory panel's measured trailing-window counts reach the view (panel path)."""
    merged = build_pull_rate_view_merged(
        panel_effective_rps=7.0,
        panel_current_rps=8.0,
        panel_recent_minute=400,
        panel_recent_429_count=1,
        panel_seconds_since_update=0.5,
        panel_recent_1s=7,
        panel_recent_600s=4000,
        panel_recent_3600s=24000,
    )
    assert merged.req_per_sec_count == 7
    assert merged.req_per_10min == 4000
    assert merged.req_per_hour_measured == 24000


def test_merged_bridge_measured_counts_flow_through() -> None:
    """A fresher bridge's measured trailing-window counts reach the view (bridge path)."""
    merged = build_pull_rate_view_merged(
        panel_effective_rps=0.0,
        panel_current_rps=0.0,
        panel_recent_minute=0,
        panel_recent_429_count=0,
        panel_seconds_since_update=math.inf,  # dashboard never pulled
        bridge_effective_rps=4.8,
        bridge_current_rps=5.0,
        bridge_recent_minute=290,
        bridge_recent_429_count=3,
        bridge_seconds_since_update=1.5,
        bridge_recent_1s=5,
        bridge_recent_600s=2900,
        bridge_recent_3600s=17400,
    )
    assert merged.req_per_sec_count == 5
    assert merged.req_per_10min == 2900
    assert merged.req_per_hour_measured == 17400


def test_merged_bridge_measured_counts_none_default_to_zero() -> None:
    """A fresher bridge with None measured counts defaults each to 0 (the defensive else)."""
    merged = build_pull_rate_view_merged(
        panel_effective_rps=9.0,
        panel_current_rps=8.0,
        panel_recent_minute=400,
        panel_recent_429_count=9,
        panel_seconds_since_update=math.inf,
        bridge_effective_rps=4.8,
        bridge_current_rps=5.0,
        bridge_recent_minute=290,
        bridge_recent_429_count=3,
        bridge_seconds_since_update=1.0,
        bridge_recent_1s=None,
        bridge_recent_600s=None,
        bridge_recent_3600s=None,
    )
    assert merged.req_per_sec_count == 0
    assert merged.req_per_10min == 0
    assert merged.req_per_hour_measured == 0
