"""Unit tests for the live pull-rate view projection (DEC-LIVE-RATE-MONITOR-001).

Drives :func:`brutex.dashboard.pull_rate_view.build_pull_rate_view` with explicit
injected values and asserts the honest idle / flowing / stalled projection + the
``to_dict`` JSON shape — full line + branch coverage of the pure module, no clock / no
network / no global state.
"""

from __future__ import annotations

import math

from brutex.dashboard.pull_rate_view import (
    BRIDGE_MAX_AGE_S,
    CEILING_PER_MIN_LIMIT,
    CEILING_RPS_LIMIT,
    FLOW_DONE,
    FLOW_FAILING,
    FLOW_FLOWING,
    FLOW_IDLE,
    FLOW_LOCAL,
    FLOW_STALLED,
    LOCAL_PHASES,
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


# ---------------------------------------------------------------------------
# DEC-RATE-WIDGET-FNO-HONEST-001 — honest achieved-rate + stall verdict across phases.
#
# The SPOT phase wires a real CallMeter so ``effective_rps`` is the MEASURED completed
# rate (``meter_present=True``). The metadata + F&O phases have NO reachable meter, so the
# admission-only sampler pushes ``meter_present=False`` — the view then keeps "achieved" an
# honest 0 (no fabricated completed-rate), bases the stall verdict on the controller's
# admission signal (``recent_minute == 0`` ⇒ stalled), and surfaces the admission per-minute
# average in its OWN ``req_per_min_rate`` field.
# ---------------------------------------------------------------------------


def test_spot_phase_achieved_is_measured_completed_rate() -> None:
    """meter_present=True (spot) → req_per_sec_achieved IS the measured effective_rps.

    Test (i): the achieved figure equals the MEASURED completed-rate, never the admission
    average — and ``req_per_min_rate`` carries the admission average as a SEPARATE field.
    """
    view = build_pull_rate_view(
        effective_rps=2.82,  # the REAL completed rate (e.g. 169 completed/min ≈ 2.82 r/s)
        current_rps=8.0,
        recent_minute=204,  # admissions ran AHEAD of completions (204 admitted/min)
        recent_429_count=0,
        last_update_monotonic=100.0,
        now_monotonic=101.0,
        meter_present=True,
    )
    assert view.req_per_sec_achieved == 2.82  # the MEASURED completed rate, not 204/60 = 3.4
    assert view.flow_state == FLOW_FLOWING
    assert view.stalled is False
    # The admission per-minute average is a distinct field (204 / 60 = 3.4), never the achieved.
    assert view.req_per_min_rate == 204 / 60.0


def test_fno_phase_achieved_is_honest_zero_admission_average_separate() -> None:
    """meter_present=False (F&O) → achieved is an honest 0; admission average in its own field.

    Test (i), the F&O side: there is NO measured completed-rate this phase, so "achieved" reads
    a HONEST 0 (never the old fabricated ``recent_minute / 60`` poured into the achieved slot),
    while the admission per-minute average is surfaced separately in ``req_per_min_rate``.
    """
    view = build_pull_rate_view(
        effective_rps=0.0,  # no meter this phase
        current_rps=8.0,
        recent_minute=204,  # the controller IS admitting calls (204 in the trailing minute)
        recent_429_count=0,
        last_update_monotonic=100.0,
        now_monotonic=101.0,
        meter_present=False,
    )
    # Achieved is honest 0 — no fabricated completed-rate. NOT 204/60 = 3.4 (the over-report bug).
    assert view.req_per_sec_achieved == 0.0
    # Admissions are flowing → the pull reads FLOWING (not a fake-0 "stalled").
    assert view.flow_state == FLOW_FLOWING
    assert view.stalled is False
    # The admission per-minute average lives in its OWN honestly-labelled field.
    assert view.req_per_min_rate == 204 / 60.0


def test_spot_phase_issued_but_failing_reads_stalled() -> None:
    """meter_present=True + admissions high but completed-rate at/below floor → STALLED.

    Test (ii) — the fake-alive regression: in the SPOT phase an issued-but-ALL-FAILING pull
    (the controller is admitting calls — ``recent_minute`` high — but NOTHING is COMPLETING, so
    the measured ``effective_rps`` sits at/below the floor) must read STALLED. The OLD sampler
    fabricated ``effective_rps = recent_minute / 60`` which would have MASKED this as "flowing";
    reading the real measured rate exposes the stall.
    """
    view = build_pull_rate_view(
        effective_rps=0.0,  # NOTHING completing (every fetch failing)
        current_rps=8.0,
        recent_minute=300,  # but the controller is busily ADMITTING calls
        recent_429_count=40,
        last_update_monotonic=100.0,
        now_monotonic=101.0,
        meter_present=True,
    )
    assert view.stalled is True
    assert view.flow_state == FLOW_STALLED
    # The achieved figure is the honest measured 0 — proving the verdict is NOT fooled by the
    # 300 admissions (which the old fabrication would have shown as a healthy 5 r/s "flowing").
    assert view.req_per_sec_achieved == 0.0


def test_fno_phase_no_admissions_reads_stalled() -> None:
    """meter_present=False + zero admissions in the trailing minute → STALLED (idle pull).

    Test (iii): in the F&O phase, with no meter, the verdict falls back to the admission
    signal — ``recent_minute == 0`` means the controller is issuing NOTHING, so the pull is
    effectively dead and reads STALLED (never a fake "flowing" off a fabricated rate).
    """
    view = build_pull_rate_view(
        effective_rps=0.0,
        current_rps=8.0,
        recent_minute=0,  # no admissions at all
        recent_429_count=0,
        last_update_monotonic=100.0,
        now_monotonic=101.0,
        meter_present=False,
    )
    assert view.stalled is True
    assert view.flow_state == FLOW_STALLED
    assert view.req_per_sec_achieved == 0.0
    assert view.req_per_min_rate == 0.0


def test_idle_zeroes_req_per_min_rate() -> None:
    """A stale (idle) panel zeroes req_per_min_rate alongside every other live number."""
    view = build_pull_rate_view(
        effective_rps=8.0,
        current_rps=8.0,
        recent_minute=400,
        recent_429_count=5,
        last_update_monotonic=0.0,
        now_monotonic=100.0,  # well past STALE_AFTER_S → idle
        meter_present=False,
    )
    assert view.flow_state == FLOW_IDLE
    assert view.req_per_min_rate == 0.0
    assert view.req_per_sec_achieved == 0.0


def test_to_dict_carries_req_per_min_rate() -> None:
    """``to_dict`` exposes the new req_per_min_rate field for the widget."""
    view = build_pull_rate_view(
        effective_rps=0.0,
        current_rps=8.0,
        recent_minute=180,
        recent_429_count=0,
        last_update_monotonic=100.0,
        now_monotonic=101.0,
        meter_present=False,
    )
    data = view.to_dict()
    assert data["req_per_min_rate"] == 3.0  # 180 / 60


def test_merged_panel_meter_absent_threads_through() -> None:
    """build_pull_rate_view_merged threads panel_meter_present into the projection.

    The dashboard route reads ``panel.meter_present`` and passes it as ``panel_meter_present``;
    a metadata/F&O-phase panel (meter absent, admissions flowing) must read FLOWING with an
    honest 0 achieved — proving the flag reaches ``build_pull_rate_view`` via the merge.
    """
    merged = build_pull_rate_view_merged(
        panel_effective_rps=0.0,
        panel_current_rps=8.0,
        panel_recent_minute=210,
        panel_recent_429_count=0,
        panel_seconds_since_update=1.0,
        panel_meter_present=False,
    )
    assert merged.flow_state == FLOW_FLOWING
    assert merged.req_per_sec_achieved == 0.0
    assert merged.req_per_min_rate == 210 / 60.0


def test_merged_bridge_meter_present_default_keeps_measured_achieved() -> None:
    """A fresher CLI bridge defaults bridge_meter_present=True → its measured rate is achieved."""
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
        bridge_seconds_since_update=1.0,
    )
    assert merged.req_per_sec_achieved == 4.8
    assert merged.flow_state == FLOW_FLOWING


# --------------------------------------------------------------------------- #
# The pull-active stall gate (DEC-RATE-STALL-PULL-GATE-001): STALLED is shown
# ONLY while a pull is ACTIVE and its req/sec flatlined — a completed pull reads
# "done" (fresh tick) then "idle", never the red mislabel the operator saw.
# --------------------------------------------------------------------------- #
def _live_kwargs(**overrides: object) -> dict[str, object]:
    """A fresh, healthy mid-pull tick — the shared base for the gate tests."""
    base: dict[str, object] = {
        "effective_rps": 6.4,
        "current_rps": 8.0,
        "recent_minute": 380,
        "recent_429_count": 0,
        "last_update_monotonic": 100.0,
        "now_monotonic": 101.0,
    }
    base.update(overrides)
    return base


def test_pull_finished_fresh_tick_reads_done_never_stalled() -> None:
    """THE operator bug: 80/80 done, tick still fresh, rps at the floor → done."""
    view = build_pull_rate_view(
        **_live_kwargs(effective_rps=0.0),  # type: ignore[arg-type]
        pull_active=False,
    )
    assert view.flow_state == FLOW_DONE
    assert view.stalled is False
    assert view.active is False
    # The last live figures are history, not a current rate — zeroed (Rule #1).
    assert view.req_per_sec_achieved == 0.0
    assert view.req_per_min == 0
    assert view.seconds_since_update == 1.0  # staleness stays real


def test_pull_finished_then_tick_ages_out_settles_to_idle() -> None:
    """done → idle: once the last tick ages past the window the label settles."""
    view = build_pull_rate_view(
        **_live_kwargs(now_monotonic=100.0 + STALE_AFTER_S + 1.0),  # type: ignore[arg-type]
        pull_active=False,
    )
    assert view.flow_state == FLOW_IDLE
    assert view.stalled is False
    assert view.active is False


def test_pull_finished_healthy_fresh_tick_is_done_not_flowing() -> None:
    """No pull running ⇒ even a healthy-looking fresh tick is not 'flowing'."""
    view = build_pull_rate_view(
        **_live_kwargs(),  # type: ignore[arg-type]
        pull_active=False,
    )
    assert view.flow_state == FLOW_DONE
    assert view.active is False
    assert view.req_per_sec_achieved == 0.0


def test_active_pull_fresh_tick_keeps_measured_verdicts() -> None:
    """pull_active=True + fresh tick: the measured flowing/stalled verdict rules."""
    flowing = build_pull_rate_view(
        **_live_kwargs(),  # type: ignore[arg-type]
        pull_active=True,
    )
    assert flowing.flow_state == FLOW_FLOWING
    assert flowing.active is True
    stalled = build_pull_rate_view(
        **_live_kwargs(effective_rps=STALL_RPS_FLOOR),  # type: ignore[arg-type]
        pull_active=True,
    )
    assert stalled.flow_state == FLOW_STALLED
    assert stalled.stalled is True


def test_active_pull_with_no_ticks_reads_stalled() -> None:
    """pull_active=True + NO tick in the window = a flatlined pull → red stalled."""
    view = build_pull_rate_view(
        **_live_kwargs(now_monotonic=100.0 + STALE_AFTER_S + 5.0),  # type: ignore[arg-type]
        pull_active=True,
    )
    assert view.flow_state == FLOW_STALLED
    assert view.stalled is True
    assert view.active is False  # no live numbers to show — zeroed
    assert view.req_per_sec_achieved == 0.0
    assert view.req_per_min == 0


def test_no_signal_keeps_legacy_tick_freshness_behaviour() -> None:
    """pull_active=None (the CLI bridge) is byte-identical to the pre-gate view."""
    legacy_flowing = build_pull_rate_view(**_live_kwargs())  # type: ignore[arg-type]
    explicit_none = build_pull_rate_view(
        **_live_kwargs(),  # type: ignore[arg-type]
        pull_active=None,
    )
    assert explicit_none == legacy_flowing
    stale = build_pull_rate_view(
        **_live_kwargs(now_monotonic=100.0 + STALE_AFTER_S + 5.0),  # type: ignore[arg-type]
        pull_active=None,
    )
    assert stale.flow_state == FLOW_IDLE
    assert stale.stalled is False


def test_full_lifecycle_transition_sequence() -> None:
    """flowing (active) → stalled (active flatline) → done (finished) → idle."""
    states = [
        build_pull_rate_view(
            **_live_kwargs(),  # type: ignore[arg-type]
            pull_active=True,
        ).flow_state,
        build_pull_rate_view(
            **_live_kwargs(effective_rps=0.0),  # type: ignore[arg-type]
            pull_active=True,
        ).flow_state,
        build_pull_rate_view(
            **_live_kwargs(effective_rps=0.0),  # type: ignore[arg-type]
            pull_active=False,
        ).flow_state,
        build_pull_rate_view(
            **_live_kwargs(now_monotonic=100.0 + STALE_AFTER_S + 60.0),  # type: ignore[arg-type]
            pull_active=False,
        ).flow_state,
    ]
    assert states == [FLOW_FLOWING, FLOW_STALLED, FLOW_DONE, FLOW_IDLE]


def test_merged_panel_pull_active_threads_through() -> None:
    """The merged builder threads panel_pull_active into the winning panel view."""
    view = build_pull_rate_view_merged(
        panel_effective_rps=0.0,
        panel_current_rps=8.0,
        panel_recent_minute=12,
        panel_recent_429_count=0,
        panel_seconds_since_update=1.0,
        panel_pull_active=False,
    )
    assert view.flow_state == FLOW_DONE
    assert view.stalled is False


def test_merged_bridge_pull_active_threads_through() -> None:
    """A fresher bridge with an explicit pull_active=False reads done, not stalled."""
    view = build_pull_rate_view_merged(
        panel_effective_rps=0.0,
        panel_current_rps=0.0,
        panel_recent_minute=0,
        panel_recent_429_count=0,
        panel_seconds_since_update=500.0,
        bridge_effective_rps=0.0,
        bridge_current_rps=8.0,
        bridge_recent_minute=3,
        bridge_recent_429_count=0,
        bridge_seconds_since_update=1.0,
        bridge_pull_active=False,
    )
    assert view.flow_state == FLOW_DONE


def test_merged_bridge_default_keeps_tick_freshness_fallback() -> None:
    """The CLI bridge carries no active flag — its default stays the legacy verdict."""
    view = build_pull_rate_view_merged(
        panel_effective_rps=0.0,
        panel_current_rps=0.0,
        panel_recent_minute=0,
        panel_recent_429_count=0,
        panel_seconds_since_update=500.0,
        bridge_effective_rps=0.0,
        bridge_current_rps=8.0,
        bridge_recent_minute=3,
        bridge_recent_429_count=0,
        bridge_seconds_since_update=1.0,
    )
    assert view.flow_state == FLOW_STALLED  # unchanged pre-gate bridge behaviour


# ---------------------------------------------------------------------------
# DEC-PIPELINE-PHASE-UI-HONESTY-001 — the amber FLOW_LOCAL verdict.
# The pipeline's prebuild + movers-warm stages are LOCAL processing (ZERO vendor
# calls by design — DEC-PIPELINE-AUTOPREBUILD-001), so the vendor-rate heuristics
# (measured completed-rate at/below the floor · zero admissions · a stale tick)
# read a perfectly healthy local stage as a red "stalled". FLOW_LOCAL keys
# STRICTLY on ``pull_active is True ∧ phase ∈ LOCAL_PHASES`` — never on meter
# absence / zero admissions — so every DEC-RATE-WIDGET-FNO-HONEST-001 stall pin
# stays green unmodified.
# ---------------------------------------------------------------------------


def test_local_phases_are_exactly_the_zero_vendor_call_stages() -> None:
    """LOCAL_PHASES = {prebuild, movers} — the two post-fetch local pipeline stages."""
    assert frozenset({"prebuild", "movers"}) == LOCAL_PHASES


def test_prebuild_phase_reads_local_amber_not_stalled() -> None:
    """Regression (fails before the fix): a healthy prebuild used to read red.

    During the prebuild the sampler keeps ticking with a ~0 completed rate, so the
    pre-fix verdict (``meter_present=True ∧ effective_rps <= floor``) was STALLED —
    a dishonest red alarm on a healthy local stage. With the phase threaded it now
    reads the amber ``local``.
    """
    before = build_pull_rate_view(
        **_live_kwargs(effective_rps=0.0),  # type: ignore[arg-type]
        pull_active=True,
    )
    assert before.flow_state == FLOW_STALLED  # the pre-fix (phase-blind) verdict
    after = build_pull_rate_view(
        **_live_kwargs(effective_rps=0.0),  # type: ignore[arg-type]
        pull_active=True,
        phase="prebuild",
    )
    assert after.flow_state == FLOW_LOCAL
    assert after.stalled is False
    assert after.active is False  # zero vendor traffic — live numbers zeroed
    assert after.req_per_sec_achieved == 0.0
    assert after.req_per_min == 0


def test_movers_warm_phase_reads_local_amber_not_stalled() -> None:
    """The movers-warm stage is the second LOCAL phase — same amber verdict."""
    view = build_pull_rate_view(
        **_live_kwargs(effective_rps=0.0, recent_minute=0),  # type: ignore[arg-type]
        meter_present=False,
        pull_active=True,
        phase="movers",
    )
    assert view.flow_state == FLOW_LOCAL
    assert view.stalled is False


def test_local_verdict_carries_flow_detail_naming_the_phase() -> None:
    """flow_detail = "local processing — <phase> (no vendor calls)" for the widget."""
    view = build_pull_rate_view(
        **_live_kwargs(effective_rps=0.0),  # type: ignore[arg-type]
        pull_active=True,
        phase="prebuild",
    )
    assert view.flow_detail == "local processing — prebuild (no vendor calls)"
    data = view.to_dict()
    assert data["flow_detail"] == "local processing — prebuild (no vendor calls)"
    assert data["flow_state"] == FLOW_LOCAL


def test_flow_detail_is_empty_for_every_non_local_state() -> None:
    """flow_detail is an ADDITIVE field — empty everywhere but FLOW_LOCAL."""
    flowing = build_pull_rate_view(**_live_kwargs(), pull_active=True)  # type: ignore[arg-type]
    stalled = build_pull_rate_view(
        **_live_kwargs(effective_rps=0.0),  # type: ignore[arg-type]
        pull_active=True,
    )
    done = build_pull_rate_view(**_live_kwargs(), pull_active=False)  # type: ignore[arg-type]
    idle = build_pull_rate_view(
        **_live_kwargs(now_monotonic=100.0 + STALE_AFTER_S + 60.0),  # type: ignore[arg-type]
    )
    for view in (flowing, stalled, done, idle):
        assert view.flow_detail == ""
        assert view.to_dict()["flow_detail"] == ""


def test_zero_admission_historical_phase_still_reads_stalled() -> None:
    """THE MANDATED PIN: FLOW_LOCAL keys on the PHASE, never on zero admissions.

    A zero-admission HISTORICAL (fetch) phase must STILL read STALLED — the
    DEC-RATE-WIDGET-FNO-HONEST-001 fake-alive guard is untouched: ``historical``
    is a VENDOR-fetch phase, so a dead pull there is a real stall, not local work.
    """
    view = build_pull_rate_view(
        **_live_kwargs(effective_rps=0.0, recent_minute=0),  # type: ignore[arg-type]
        meter_present=False,
        pull_active=True,
        phase="historical",
    )
    assert view.flow_state == FLOW_STALLED
    assert view.stalled is True
    # And the spot/metadata fetch phases behave identically (never local).
    for fetch_phase in ("spot", "metadata"):
        fetch_view = build_pull_rate_view(
            **_live_kwargs(effective_rps=0.0, recent_minute=0),  # type: ignore[arg-type]
            meter_present=False,
            pull_active=True,
            phase=fetch_phase,
        )
        assert fetch_view.flow_state == FLOW_STALLED


def test_healthy_fetch_phase_with_phase_label_keeps_measured_verdict() -> None:
    """A flowing spot phase with its label threaded stays the measured FLOWING."""
    view = build_pull_rate_view(
        **_live_kwargs(),  # type: ignore[arg-type]
        pull_active=True,
        phase="spot",
    )
    assert view.flow_state == FLOW_FLOWING
    assert view.req_per_sec_achieved == 6.4  # the _live_kwargs measured rate


def test_local_requires_pull_active_true() -> None:
    """The amber verdict needs the AUTHORITATIVE active signal — never fabricated.

    ``pull_active=False`` (pull finished) outranks a lingering local phase label
    (→ done); ``pull_active=None`` (the CLI bridge — which never has a pipeline
    phase anyway) keeps the byte-identical legacy tick-freshness verdict.
    """
    finished = build_pull_rate_view(
        **_live_kwargs(effective_rps=0.0),  # type: ignore[arg-type]
        pull_active=False,
        phase="prebuild",
    )
    assert finished.flow_state == FLOW_DONE
    no_signal = build_pull_rate_view(
        **_live_kwargs(effective_rps=0.0),  # type: ignore[arg-type]
        pull_active=None,
        phase="prebuild",
    )
    assert no_signal.flow_state == FLOW_STALLED  # legacy phase-blind verdict


def test_local_with_stale_tick_still_reads_local_not_stalled() -> None:
    """A between-tick gap during a local stage can never read red either.

    The local check runs BEFORE the tick-freshness stall (an active pull whose
    ticks paused mid-prebuild is doing local work, not a vendor stall).
    """
    view = build_pull_rate_view(
        **_live_kwargs(now_monotonic=100.0 + STALE_AFTER_S + 5.0),  # type: ignore[arg-type]
        pull_active=True,
        phase="prebuild",
    )
    assert view.flow_state == FLOW_LOCAL
    assert view.stalled is False


def test_empty_or_unknown_phase_never_reads_local() -> None:
    """phase="" (cleared) / an unknown label / None → the pre-phase verdicts."""
    for phase in ("", "unknown-future-phase", None):
        view = build_pull_rate_view(
            **_live_kwargs(effective_rps=0.0),  # type: ignore[arg-type]
            pull_active=True,
            phase=phase,
        )
        assert view.flow_state == FLOW_STALLED


def test_merged_panel_phase_threads_through_to_local() -> None:
    """build_pull_rate_view_merged threads panel_phase into the panel projection."""
    view = build_pull_rate_view_merged(
        panel_effective_rps=0.0,
        panel_current_rps=8.0,
        panel_recent_minute=0,
        panel_recent_429_count=0,
        panel_seconds_since_update=1.0,
        panel_pull_active=True,
        panel_phase="prebuild",
    )
    assert view.flow_state == FLOW_LOCAL
    assert view.flow_detail == "local processing — prebuild (no vendor calls)"


def test_merged_bridge_never_receives_a_phase() -> None:
    """A fresher CLI bridge keeps its byte-identical verdict — local is panel-only.

    The bridge is a separate ``brutex fetch`` process with NO pipeline phase, so
    even with the panel side sitting in a local phase the bridge projection can
    never read amber.
    """
    view = build_pull_rate_view_merged(
        panel_effective_rps=0.0,
        panel_current_rps=8.0,
        panel_recent_minute=0,
        panel_recent_429_count=0,
        panel_seconds_since_update=500.0,
        panel_pull_active=True,
        panel_phase="prebuild",
        bridge_effective_rps=0.0,
        bridge_current_rps=8.0,
        bridge_recent_minute=3,
        bridge_recent_429_count=0,
        bridge_seconds_since_update=1.0,
    )
    assert view.flow_state == FLOW_STALLED  # the bridge's unchanged legacy verdict


# ═══════════════════════════════════════════════════════════════════════════
# failed_rps → the distinct FAILING verdict (DEC-PULL-TELEMETRY-TRIAD-001 —
# closing the #1022 flagged follow-up: "issued-but-all-failing needs the
# meter's failed rate"). Meter-fed path only; the default is byte-identical.
# ═══════════════════════════════════════════════════════════════════════════


def test_failed_rps_default_is_byte_identical() -> None:
    """PARITY PIN (Session-B contract): omitting ``failed_rps`` and passing the 0.0
    default produce the SAME view — and the verdict matrix is unchanged at 0.0."""
    kwargs: dict[str, float | int] = {
        "effective_rps": 0.0,
        "current_rps": 8.0,
        "recent_minute": 240,
        "recent_429_count": 0,
        "last_update_monotonic": 100.0,
        "now_monotonic": 101.0,
    }
    omitted = build_pull_rate_view(**kwargs)  # type: ignore[arg-type]
    explicit = build_pull_rate_view(failed_rps=0.0, **kwargs)  # type: ignore[arg-type]
    assert omitted == explicit
    assert omitted.flow_state == FLOW_STALLED  # the pre-decision verdict, unchanged
    assert omitted.req_per_sec_failed == 0.0


def test_issuing_but_all_failing_reads_failing_with_evidence() -> None:
    """Meter-fed + completed-rate at/below the floor + failures burning → the DISTINCT
    ``failing`` verdict (stalled stays True for red-light consumers) with the measured
    failed-calls rate carried as evidence."""
    view = build_pull_rate_view(
        effective_rps=0.0,  # nothing completing…
        current_rps=8.0,
        recent_minute=240,  # …while calls ARE being issued…
        recent_429_count=0,
        last_update_monotonic=100.0,
        now_monotonic=101.0,
        meter_present=True,
        failed_rps=2.5,  # …and the meter MEASURES them failing
    )
    assert view.flow_state == FLOW_FAILING
    assert view.stalled is True  # failing IS a degraded/red state
    assert view.active is True
    assert view.req_per_sec_achieved == 0.0
    assert view.req_per_sec_failed == 2.5


def test_flowing_with_some_failures_stays_flowing() -> None:
    """A healthy completed-rate above the floor stays FLOWING even with failures —
    ``failing`` is reserved for the all-failing (at/below-floor) case."""
    view = build_pull_rate_view(
        effective_rps=5.0,
        current_rps=8.0,
        recent_minute=300,
        recent_429_count=1,
        last_update_monotonic=100.0,
        now_monotonic=101.0,
        failed_rps=1.0,
    )
    assert view.flow_state == FLOW_FLOWING
    assert view.stalled is False
    assert view.req_per_sec_failed == 1.0


def test_meter_absent_never_fabricates_failing() -> None:
    """meter_present=False ⇒ no measured failure rate exists — the verdict keeps the
    admission fallback and the carried failed rate is an honest 0.0 (never fabricated),
    even if a stray nonzero ``failed_rps`` is passed."""
    view = build_pull_rate_view(
        effective_rps=0.0,
        current_rps=8.0,
        recent_minute=240,  # admissions happening → the fallback reads flowing
        recent_429_count=0,
        last_update_monotonic=100.0,
        now_monotonic=101.0,
        meter_present=False,
        failed_rps=3.0,  # must be IGNORED — there is no meter to have measured it
    )
    assert view.flow_state == FLOW_FLOWING  # the pre-decision meter-less fallback verdict
    assert view.req_per_sec_failed == 0.0


def test_quiet_views_zero_the_failed_rate() -> None:
    """Idle / done / stale views zero ``req_per_sec_failed`` like every live number."""
    idle = build_pull_rate_view(
        effective_rps=4.0,
        current_rps=8.0,
        recent_minute=200,
        recent_429_count=0,
        last_update_monotonic=0.0,
        now_monotonic=STALE_AFTER_S + 100.0,
        failed_rps=9.9,
    )
    assert idle.flow_state == FLOW_IDLE
    assert idle.req_per_sec_failed == 0.0
    done = build_pull_rate_view(
        effective_rps=4.0,
        current_rps=8.0,
        recent_minute=200,
        recent_429_count=0,
        last_update_monotonic=100.0,
        now_monotonic=101.0,
        failed_rps=9.9,
        pull_active=False,
    )
    assert done.flow_state == FLOW_DONE
    assert done.req_per_sec_failed == 0.0


def test_to_dict_carries_req_per_sec_failed() -> None:
    """The JSON payload exposes the additive ``req_per_sec_failed`` key for the widget."""
    view = build_pull_rate_view(
        effective_rps=0.0,
        current_rps=8.0,
        recent_minute=100,
        recent_429_count=0,
        last_update_monotonic=100.0,
        now_monotonic=101.0,
        failed_rps=1.7,
    )
    data = view.to_dict()
    assert data["req_per_sec_failed"] == 1.7
    assert data["flow_state"] == FLOW_FAILING


def test_merged_threads_panel_failed_rps() -> None:
    """The merged builder threads ``panel_failed_rps`` into the panel-side projection
    (default 0.0 — byte-identical) and defaults an absent bridge field to 0.0."""
    view = build_pull_rate_view_merged(
        panel_effective_rps=0.0,
        panel_current_rps=8.0,
        panel_recent_minute=120,
        panel_recent_429_count=0,
        panel_seconds_since_update=1.0,
        panel_failed_rps=2.0,
    )
    assert view.flow_state == FLOW_FAILING
    assert view.req_per_sec_failed == 2.0


def test_merged_bridge_failed_rps_threaded_and_none_defaults_zero() -> None:
    """A fresher bridge wins with ITS failed rate; a ``None`` (older bridge file)
    projects an honest 0.0 → the plain stalled verdict, never a fabricated failing."""
    fresher_bridge = build_pull_rate_view_merged(
        panel_effective_rps=5.0,
        panel_current_rps=8.0,
        panel_recent_minute=300,
        panel_recent_429_count=0,
        panel_seconds_since_update=50.0,
        bridge_effective_rps=0.0,
        bridge_current_rps=8.0,
        bridge_recent_minute=100,
        bridge_recent_429_count=0,
        bridge_seconds_since_update=1.0,
        bridge_failed_rps=4.0,
    )
    assert fresher_bridge.flow_state == FLOW_FAILING
    assert fresher_bridge.req_per_sec_failed == 4.0
    legacy_bridge = build_pull_rate_view_merged(
        panel_effective_rps=5.0,
        panel_current_rps=8.0,
        panel_recent_minute=300,
        panel_recent_429_count=0,
        panel_seconds_since_update=50.0,
        bridge_effective_rps=0.0,
        bridge_current_rps=8.0,
        bridge_recent_minute=100,
        bridge_recent_429_count=0,
        bridge_seconds_since_update=1.0,
        bridge_failed_rps=None,
    )
    assert legacy_bridge.flow_state == FLOW_STALLED  # no failure measurement → plain stalled
    assert legacy_bridge.req_per_sec_failed == 0.0


# ---------------------------------------------------------------------------
# The bridge max-age TTL (DEC-RATE-METER-LIFECYCLE-001, audit C3): a DEAD CLI's
# stuck pull_active=True file must never fabricate a PERMANENT red "stalled".
# ---------------------------------------------------------------------------


class TestBridgeMaxAge:
    """`build_pull_rate_view_merged`'s bridge TTL — dead evidence ≡ absent."""

    def test_dead_bridge_beyond_ttl_reads_idle(self) -> None:
        """The C3 repro pin (FAILS on pre-#1482 HEAD — it read FLOW_STALLED forever).

        MUTANT KILLED: the ``<= bridge_max_age_s`` conjunct severed → the 2-day-old
        SIGKILLed CLI file out-freshes the never-ticked panel and fabricates a
        permanent site-wide red ``stalled``.
        """
        merged = build_pull_rate_view_merged(
            panel_effective_rps=0.0,
            panel_current_rps=0.0,
            panel_recent_minute=0,
            panel_recent_429_count=0,
            panel_seconds_since_update=math.inf,  # freshly-restarted, never-pulled dashboard
            bridge_effective_rps=4.8,
            bridge_current_rps=5.0,
            bridge_recent_minute=290,
            bridge_recent_429_count=0,
            bridge_seconds_since_update=172_800.0,  # a 2-day-old dead CLI file
            bridge_pull_active=True,  # the stuck stamp (the finally never ran — SIGKILL)
        )
        assert merged.flow_state == FLOW_IDLE
        assert merged.stalled is False
        assert merged.active is False

    def test_bridge_at_ttl_boundary_still_trusted(self) -> None:
        """AT the bound the bridge stays trusted — the died-mid-run red alert is KEPT.

        MUTANT KILLED: ``<=`` degraded to ``<`` (the boundary file would flip idle,
        shaving the documented 15-minute alert window).
        """
        merged = build_pull_rate_view_merged(
            panel_effective_rps=0.0,
            panel_current_rps=0.0,
            panel_recent_minute=0,
            panel_recent_429_count=0,
            panel_seconds_since_update=math.inf,
            bridge_effective_rps=4.8,
            bridge_current_rps=5.0,
            bridge_recent_minute=290,
            bridge_recent_429_count=0,
            bridge_seconds_since_update=BRIDGE_MAX_AGE_S,  # exactly AT the bound
            bridge_pull_active=True,
        )
        assert merged.flow_state == FLOW_STALLED
        assert merged.stalled is True

    def test_fresh_bridge_projection_byte_identical(self) -> None:
        """A fresh bridge projects field-for-field IDENTICALLY to the pre-TTL pick.

        MUTANTS KILLED: swapped operands / an inverted gate (the fresh CLI numbers
        would vanish); a silently-changed constant (the 900.0 literal is pinned —
        a policy revision must consciously retune the boundary tests too).
        """
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
            bridge_seconds_since_update=3.0,  # a live, flowing CLI
            bridge_recent_1s=5,
            bridge_recent_600s=2_800,
            bridge_recent_3600s=16_500,
            bridge_pull_active=True,
        )
        expected = build_pull_rate_view(
            effective_rps=4.8,
            current_rps=5.0,
            recent_minute=290,
            recent_429_count=3,
            recent_1s=5,
            recent_600s=2_800,
            recent_3600s=16_500,
            pull_active=True,
            last_update_monotonic=0.0,
            now_monotonic=3.0,
        )
        assert merged == expected
        assert merged.flow_state == FLOW_FLOWING
        assert BRIDGE_MAX_AGE_S == 900.0  # the pinned policy literal (Golden Rule #10)

    def test_non_finite_bridge_age_ignored(self) -> None:
        """A NaN/inf bridge age takes the panel path — the isfinite CONTRACT pin.

        MUTANT KILLED: the ``math.isfinite`` conjunct severed → the NaN behaviour
        would silently depend on float-comparison ordering (``nan <= x`` is False
        for every comparison, but the contract must not hinge on that accident).
        """
        for bad_age in (float("nan"), math.inf):
            merged = build_pull_rate_view_merged(
                panel_effective_rps=0.0,
                panel_current_rps=0.0,
                panel_recent_minute=0,
                panel_recent_429_count=0,
                panel_seconds_since_update=math.inf,
                bridge_effective_rps=4.8,
                bridge_current_rps=5.0,
                bridge_recent_minute=290,
                bridge_recent_429_count=0,
                bridge_seconds_since_update=bad_age,
                bridge_pull_active=True,
            )
            assert merged.flow_state == FLOW_IDLE
            assert merged.stalled is False

    def test_stale_but_within_ttl_bridge_still_reads_stalled(self) -> None:
        """The 12 s < age ≤ 900 s alert window is PRESERVED end-to-end at the projection:
        a CLI that died 60 s ago still reads honestly red (died mid-run)."""
        merged = build_pull_rate_view_merged(
            panel_effective_rps=0.0,
            panel_current_rps=0.0,
            panel_recent_minute=0,
            panel_recent_429_count=0,
            panel_seconds_since_update=math.inf,
            bridge_effective_rps=4.8,
            bridge_current_rps=5.0,
            bridge_recent_minute=290,
            bridge_recent_429_count=0,
            bridge_seconds_since_update=60.0,
            bridge_pull_active=True,
        )
        assert merged.flow_state == FLOW_STALLED
        assert merged.stalled is True
