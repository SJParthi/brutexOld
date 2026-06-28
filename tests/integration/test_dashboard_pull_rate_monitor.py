"""Integration tests for the live pull-rate monitor (DEC-LIVE-RATE-MONITOR-001).

Covers the end-to-end wiring: the rate panel's new ``recent_minute`` /
``ceiling_per_min`` / ``last_update_monotonic`` fields, ``make_rate_observer`` mapping
the controller's ``recent_minute`` / ``ceiling_per_min`` onto the panel, the
``/api/v1/pull/rate`` route returning REAL panel-derived values, the honest IDLE state
on a fresh (never-pulled) dashboard, the stall verdict, the back-compat ``/api/v1/rate``
route stripping the non-JSON monotonic baseline, and the widget script being served +
referenced site-wide.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from brutex.dashboard.app import DashboardState, RateLimitPanel, make_rate_observer
from brutex.dashboard.pull_rate_view import CEILING_PER_MIN_LIMIT, CEILING_RPS_LIMIT


def test_panel_defaults_include_live_rate_monitor_fields() -> None:
    panel = RateLimitPanel()
    assert panel.recent_minute == 0
    assert panel.ceiling_per_min == -1
    # The freshness baseline starts "never updated".
    assert panel.last_update_monotonic == float("-inf")


def test_observer_maps_recent_minute_and_per_min_cap() -> None:
    """The controller snapshot's recent_minute + ceiling_per_min flow to the panel."""
    state = DashboardState()
    before = time.monotonic()
    make_rate_observer(state)(
        {
            "current_rps": 8.0,
            "recent_hits": 1,
            "recent_minute": 412,
            "ceiling_per_min": 500,
        },
        {"effective_rps": 6.4, "latency_ms_p95": 180.0},
    )
    assert state.rate_limit.recent_minute == 412
    assert state.rate_limit.ceiling_per_min == 500
    # A live tick stamped the freshness baseline to a real monotonic reading.
    assert state.rate_limit.last_update_monotonic >= before


def test_observer_maps_absent_per_min_cap_to_sentinel() -> None:
    """A snapshot with no ceiling_per_min (governor disabled / None) → the -1 sentinel."""
    state = DashboardState()
    make_rate_observer(state)({"current_rps": 5.0, "ceiling_per_min": None}, {})
    assert state.rate_limit.ceiling_per_min == -1
    state2 = DashboardState()
    make_rate_observer(state2)({"current_rps": 5.0}, {})  # key absent entirely
    assert state2.rate_limit.ceiling_per_min == -1


def test_update_throughput_stamps_freshness() -> None:
    """The measured-throughput half of a tick also stamps the freshness baseline."""
    state = DashboardState()
    assert state.rate_limit.last_update_monotonic == float("-inf")
    state.update_throughput(effective_rps=4.0, latency_ms_p95=120.0)
    assert state.rate_limit.last_update_monotonic > float("-inf")


def _client(state: DashboardState) -> object:
    """Build a FastAPI ``TestClient`` over the dashboard app bound to *state*."""
    pytest.importorskip("fastapi")
    from brutex.dashboard.app import build_app
    from fastapi.testclient import TestClient

    return TestClient(build_app(state))


class TestPullRateRoute:
    def test_idle_state_when_no_pull_has_run(self) -> None:
        """A fresh dashboard (no pull) → honest idle, ZEROED live numbers, null age."""
        state = DashboardState()
        with _client(state) as c:  # type: ignore[attr-defined]
            resp = c.get("/api/v1/pull/rate")
        assert resp.status_code == 200
        body = resp.json()
        assert body["flow_state"] == "idle"
        assert body["active"] is False
        assert body["req_per_sec_achieved"] == 0.0
        assert body["req_per_min"] == 0
        assert body["req_per_hour_observed"] == 0
        assert body["seconds_since_update"] is None  # never updated
        # The limits are real even while idle.
        assert body["ceiling_rps"] == CEILING_RPS_LIMIT
        assert body["ceiling_per_min"] == CEILING_PER_MIN_LIMIT

    def test_flowing_state_reflects_real_panel_values(self) -> None:
        """A just-pushed live tick → flowing, with the REAL controller/meter numbers."""
        state = DashboardState()
        make_rate_observer(state)(
            {
                "current_rps": 8.0,
                "recent_hits": 3,
                "recent_minute": 390,
                "ceiling_per_min": 500,
            },
            {"effective_rps": 6.4, "latency_ms_p95": 200.0},
        )
        with _client(state) as c:  # type: ignore[attr-defined]
            resp = c.get("/api/v1/pull/rate")
        body = resp.json()
        assert body["flow_state"] == "flowing"
        assert body["active"] is True
        assert body["req_per_sec_achieved"] == 6.4
        assert body["req_per_sec_allowed"] == 8.0
        assert body["req_per_min"] == 390
        assert body["req_per_hour_observed"] == 390 * 60  # observed extrapolation
        assert body["recent_429_count"] == 3
        assert body["seconds_since_update"] is not None
        assert body["seconds_since_update"] >= 0.0

    def test_stalled_state_when_achieved_near_zero(self) -> None:
        """A live tick with near-zero achieved r/s → the red stalled verdict."""
        state = DashboardState()
        make_rate_observer(state)(
            {"current_rps": 8.0, "recent_hits": 50, "recent_minute": 2, "ceiling_per_min": 500},
            {"effective_rps": 0.0, "latency_ms_p95": 9999.0},
        )
        with _client(state) as c:  # type: ignore[attr-defined]
            resp = c.get("/api/v1/pull/rate")
        body = resp.json()
        assert body["flow_state"] == "stalled"
        assert body["stalled"] is True

    def test_legacy_rate_route_strips_monotonic_baseline(self) -> None:
        """/api/v1/rate must not leak the (non-JSON, -inf) monotonic baseline."""
        state = DashboardState()
        with _client(state) as c:  # type: ignore[attr-defined]
            resp = c.get("/api/v1/rate")
        assert resp.status_code == 200
        body = resp.json()
        assert "last_update_monotonic" not in body
        # The new panel fields ARE serialised (they are plain ints).
        assert body["recent_minute"] == 0
        assert body["ceiling_per_min"] == -1

    def test_widget_script_served_and_referenced(self) -> None:
        """The widget JS is served from /static and referenced by /universe."""
        state = DashboardState()
        with _client(state) as c:  # type: ignore[attr-defined]
            js = c.get("/static/bx-rate-monitor.js")
            uni = c.get("/universe")
        assert js.status_code == 200
        assert "/api/v1/pull/rate" in js.text
        assert uni.status_code == 200
        assert "/static/bx-rate-monitor.js" in uni.text


def test_widget_referenced_on_operator_and_universe_templates() -> None:
    """Both the operator (v29) and /universe templates load the widget site-wide."""
    templates = Path(__file__).resolve().parents[2] / "brutex" / "dashboard" / "templates"
    for name in ("v29.html", "universe.html"):
        text = (templates / name).read_text(encoding="utf-8")
        assert "/static/bx-rate-monitor.js" in text, name
