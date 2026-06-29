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


def test_widget_referenced_on_every_dashboard_template() -> None:
    """EVERY dashboard page loads the widget — incl. the pull-watching pages (the bug).

    DEC-PULL-RATE-WIDGET-VISIBILITY-001: the widget previously loaded ONLY on v29 +
    universe, so it was ABSENT from the pages a pull is actually watched on
    (coverage / spot_coverage / fetch_windows / completeness / db / movers / …). It is
    now loaded site-wide (the widget is self-contained + idempotent, so this is safe).
    """
    templates = Path(__file__).resolve().parents[2] / "brutex" / "dashboard" / "templates"
    html_files = sorted(templates.glob("*.html"))
    assert html_files, "expected dashboard templates to exist"
    # The pull-watching pages the bug report names MUST be present + carry the widget.
    must_have = {
        "coverage.html",
        "spot_coverage.html",
        "fetch_windows.html",
        "completeness.html",
        "db.html",
        "movers.html",
        "results.html",
        "reports.html",
        "expired_options.html",
    }
    present = {p.name for p in html_files}
    assert must_have <= present, f"missing pull-watching templates: {must_have - present}"
    for path in html_files:
        text = path.read_text(encoding="utf-8")
        assert "/static/bx-rate-monitor.js" in text, path.name


class TestFnoPhaseLiveness:
    """The live panel stays fed through the metadata + F&O phases, not just spot.

    DEC-PULL-RATE-WIDGET-VISIBILITY-001 — the BACKEND half of the fix. The bug: only the
    spot phase fed the panel, so ~12 s after the spot phase ended the route reported
    ``idle`` for the entire ~38-minute F&O phase. The fix: a
    :class:`~brutex.dashboard.rate_sampler.RatePanelSampler` ticks the SHARED controller
    into the panel through the WHOLE pipeline, so the widget stays live across F&O.
    """

    def test_route_goes_idle_after_spot_without_the_sampler(self) -> None:
        """REGRESSION (the bug): a spot-only tick that has aged out → the route reads idle.

        Mirrors the broken state: the spot phase pushed the last live tick, then the long
        F&O phase fed NOTHING, so once the panel's last update is older than the staleness
        window the route honestly flips to idle (the blank widget the operator saw).
        """
        from brutex.dashboard.pull_rate_view import STALE_AFTER_S

        state = DashboardState()
        # A real spot-phase tick happened…
        make_rate_observer(state)(
            {"current_rps": 8.0, "recent_minute": 300, "ceiling_per_min": 500},
            {"effective_rps": 5.0},
        )
        # …but then the F&O phase fed NOTHING, so the tick ages out past the staleness window.
        with state._lock:  # type: ignore[attr-defined]
            state.rate_limit.last_update_monotonic = time.monotonic() - (STALE_AFTER_S + 5.0)
        with _client(state) as c:  # type: ignore[attr-defined]
            body = c.get("/api/v1/pull/rate").json()
        assert body["flow_state"] == "idle"
        assert body["active"] is False
        assert body["req_per_min"] == 0  # zeroed — the blank widget

    def test_sampler_keeps_route_live_during_fno_phase(self) -> None:
        """THE FIX: the sampler ticks the shared controller → the route stays live in F&O.

        Simulates the F&O phase: the SHARED session controller has been paced (its
        rolling-minute issue count reflects real fetches) but NO spot per-window observe is
        running. The sampler — wrapped around the pipeline — pushes that controller to the
        panel, so ``/api/v1/pull/rate`` returns FLOWING (non-idle) DURING the F&O phase.
        """
        from brutex.adapters.rate_common import RateController
        from brutex.dashboard.pull_controller import SessionRateController
        from brutex.dashboard.rate_sampler import RatePanelSampler

        # A SHARED controller that an F&O pull has been pacing: 360 issues in the trailing
        # minute (= a real 6 r/s achieved) and a current allowed rate of 8 r/s.
        controller = RateController(ceiling_per_min=500)
        for _ in range(360):
            controller._issues.append(controller._clock())
        session = SessionRateController(factory=lambda: controller)
        session.get()  # the F&O phase built the controller (peek now returns it)

        state = DashboardState()
        sampler = RatePanelSampler(session_rate=session, observer=make_rate_observer(state))
        # The pipeline wraps the metadata + F&O phases in the sampler; here we drive ONE
        # tick deterministically (what the background loop does on its cadence).
        assert sampler.sample_once() is True

        with _client(state) as c:  # type: ignore[attr-defined]
            body = c.get("/api/v1/pull/rate").json()
        # LIVE during the F&O phase — never the blank/idle widget the bug produced.
        assert body["flow_state"] == "flowing"
        assert body["active"] is True
        assert body["req_per_sec_achieved"] == pytest.approx(6.0)  # 360 / 60
        assert body["req_per_min"] == 360
        assert body["req_per_hour_observed"] == 360 * 60
