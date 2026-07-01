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
    # The expanded-monitor trailing-window real counts default to 0 (idle; no live pull).
    assert panel.recent_1s == 0
    assert panel.recent_600s == 0
    assert panel.recent_3600s == 0
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
            "recent_1s": 7,
            "recent_600s": 4120,
            "recent_3600s": 24720,
        },
        {"effective_rps": 6.4, "latency_ms_p95": 180.0},
    )
    assert state.rate_limit.recent_minute == 412
    assert state.rate_limit.ceiling_per_min == 500
    # The expanded-monitor trailing-window real counts flow through (DEC-RATE-MONITOR-EXPAND-001).
    assert state.rate_limit.recent_1s == 7
    assert state.rate_limit.recent_600s == 4120
    assert state.rate_limit.recent_3600s == 24720
    # A live tick stamped the freshness baseline to a real monotonic reading.
    assert state.rate_limit.last_update_monotonic >= before


def test_observer_defaults_absent_measured_counts_to_zero() -> None:
    """A snapshot from an older controller (no recent_1s/600s/3600s) → honest 0 panel counts."""
    state = DashboardState()
    make_rate_observer(state)({"current_rps": 5.0, "recent_minute": 100}, {})
    assert state.rate_limit.recent_1s == 0
    assert state.rate_limit.recent_600s == 0
    assert state.rate_limit.recent_3600s == 0


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
        # The new measured req/10min + req/hour + req/sec evidence are present + zeroed idle.
        assert body["req_per_10min"] == 0
        assert body["req_per_hour_measured"] == 0
        assert body["req_per_sec_count"] == 0
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
                "recent_1s": 6,
                "recent_600s": 3900,
                "recent_3600s": 23400,
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
        # The NEW measured trailing-window real counts reach the route end to end
        # (DEC-RATE-MONITOR-EXPAND-001): req/10min = trailing-600s count, req/hour MEASURED =
        # trailing-3600s count, req/sec count = trailing-1s evidence.
        assert body["req_per_10min"] == 3900
        assert body["req_per_hour_measured"] == 23400
        assert body["req_per_sec_count"] == 6
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
        # The expanded monitor (DEC-RATE-MONITOR-EXPAND-001): the req/10min metric, the
        # MEASURED req/hour field, the click-to-expand overlay + its idempotent guard, and the
        # raw-evidence count rendering are all present in the served widget.
        assert "req/10min" in js.text
        assert "req_per_10min" in js.text
        assert "req_per_hour_measured" in js.text
        assert "bxRateMonitorOverlay" in js.text
        assert "req_per_sec_count" in js.text
        # Idempotent guard preserved (only one widget ever built).
        assert "document.getElementById(WIDGET_ID)" in js.text
        # DEC-RATE-WIDGET-FNO-MEASURED-001: the widget's own help text must now describe
        # "achieved" as the measured completed rate across spot AND metadata/F&O once the pool
        # is built (not "wired only on the spot phase"), so it never tells the operator the
        # non-zero F&O achieved figure is impossible.
        assert "spot AND metadata/F&O once the pool is built" in js.text
        assert "wired only on the spot phase" not in js.text
        assert "the meter is unreachable" not in js.text
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

    def test_sampler_keeps_route_live_during_fno_phase_without_meter(self) -> None:
        """THE #1000 FIX: the sampler ticks the shared controller → the route stays live in F&O.

        Simulates the F&O phase BEFORE the client pool's meter is registered (or a phase with
        no reachable meter): the SHARED session controller has been paced (its rolling-minute
        ADMISSION count reflects real fetches) but NO meter is registered and NO spot per-window
        observe is running. The sampler — wrapped around the pipeline — pushes that controller
        to the panel, so ``/api/v1/pull/rate`` returns FLOWING (non-idle) DURING the F&O phase.

        DEC-RATE-WIDGET-FNO-HONEST-001 (the meter-absent fallback preserved by
        DEC-RATE-WIDGET-FNO-MEASURED-001): the widget stays LIVE, but ``req_per_sec_achieved``
        is an HONEST 0 this phase (no reachable completed-rate meter — never the old
        ``recent_minute / 60`` over-report poured into the achieved slot); the admission
        per-minute average is surfaced separately in ``req_per_min_rate``.
        """
        from brutex.adapters.rate_common import RateController
        from brutex.dashboard.pull_controller import SessionRateController
        from brutex.dashboard.rate_sampler import RatePanelSampler

        # A SHARED controller that an F&O pull has been pacing: 360 issues in the trailing
        # minute (= 360 ADMITTED calls/min) and a current allowed rate of 8 r/s.
        controller = RateController(ceiling_per_min=500)
        for _ in range(360):
            controller._issues.append(controller._clock())
        session = SessionRateController(factory=lambda: controller)
        session.get()  # the F&O phase built the controller (peek now returns it)
        # NO meter registered → the sampler falls back to the honest meter_present=False path.
        assert session.peek_meter() is None

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
        # HONEST achieved: 0 (no measured completed-rate this phase), NOT the fabricated 6.0.
        assert body["req_per_sec_achieved"] == 0.0
        # The admission per-minute average is in its own labelled field (360 / 60 = 6.0).
        assert body["req_per_min_rate"] == pytest.approx(6.0)
        assert body["req_per_min"] == 360
        assert body["req_per_hour_observed"] == 360 * 60

    def test_sampler_reports_measured_rate_when_meter_registered(self) -> None:
        """DEC-RATE-WIDGET-FNO-MEASURED-001: a registered pool meter → the F&O route reports MEASURED.

        THE FOLLOW-UP FIX (this PR). Once the F&O pull build site has REGISTERED the client
        pool's shared transport :class:`~brutex.adapters.call_meter.CallMeter` into the session
        controller, the sampler reads it via ``peek_meter`` and pushes the meter's REAL measured
        ``snapshot`` — so ``/api/v1/pull/rate``'s ``req_per_sec_achieved`` is the TRUE completed
        rate during the F&O phase (2.82 r/s here), NOT the honest-0 fallback and NOT the
        admission average (360/60 = 6.0). The admission avg is still surfaced separately in
        ``req_per_min_rate``. This is the whole point of the PR: the F&O achieved slot now shows
        a measured completed rate, end to end through the real route.
        """
        from brutex.adapters.call_meter import CallMeter
        from brutex.adapters.rate_common import RateController
        from brutex.dashboard.pull_controller import SessionRateController
        from brutex.dashboard.rate_sampler import RatePanelSampler

        controller = RateController(ceiling_per_min=500)
        for _ in range(360):  # 360 ADMITTED/min — the admission average would be 6.0 r/s
            controller._issues.append(controller._clock())
        session = SessionRateController(factory=lambda: controller)
        session.get()

        # The build site registered the pool's meter; make it report a MEASURED 2.82 r/s
        # completed rate (28 completions in the 10 s achieved window: 28 / 10 = 2.8, rounded).
        meter = CallMeter()
        for _ in range(28):
            meter.start()
            meter.stop(elapsed_ms=180.0, completed=True)
        session.register_meter(meter)
        assert session.peek_meter() is meter

        state = DashboardState()
        sampler = RatePanelSampler(session_rate=session, observer=make_rate_observer(state))
        assert sampler.sample_once() is True

        with _client(state) as c:  # type: ignore[attr-defined]
            body = c.get("/api/v1/pull/rate").json()
        assert body["flow_state"] == "flowing"
        assert body["active"] is True
        # THE MEASURED completed rate reaches the route — NOT the honest-0 fallback (0.0) and
        # NOT the admission average (6.0). The meter measured 28 completions / 10 s = 2.8 r/s.
        assert body["req_per_sec_achieved"] == pytest.approx(2.8)
        # The admission avg is STILL its own separate field, never the achieved slot.
        assert body["req_per_min_rate"] == pytest.approx(6.0)
        assert body["req_per_min"] == 360

    def test_measured_meter_below_floor_reads_stalled_in_fno(self) -> None:
        """A registered meter with a completed-rate at/below the floor → STALLED in F&O.

        DEC-RATE-WIDGET-FNO-MEASURED-001 closes the #1022 gap: with a real meter the F&O phase
        can now detect an issued-but-ALL-FAILING pull (the controller busily ADMITTING — 300/min
        — but the meter measuring ~0 COMPLETIONS). ``meter_present=True`` makes the verdict read
        the MEASURED ``effective_rps``, so a completed-rate at/below the stall floor reads
        STALLED — the fake-alive case #1022 could not catch in F&O is now caught end to end.
        """
        from brutex.adapters.call_meter import CallMeter
        from brutex.adapters.rate_common import RateController
        from brutex.dashboard.pull_controller import SessionRateController
        from brutex.dashboard.rate_sampler import RatePanelSampler

        controller = RateController(ceiling_per_min=500)
        for _ in range(300):  # busily admitting…
            controller._issues.append(controller._clock())
        session = SessionRateController(factory=lambda: controller)
        session.get()
        # …but the meter measured NOTHING completing (a fresh meter reads effective_rps 0.0).
        session.register_meter(CallMeter())

        state = DashboardState()
        sampler = RatePanelSampler(session_rate=session, observer=make_rate_observer(state))
        assert sampler.sample_once() is True

        with _client(state) as c:  # type: ignore[attr-defined]
            body = c.get("/api/v1/pull/rate").json()
        assert body["active"] is True  # a tick landed → the pull is "active"
        assert body["flow_state"] == "stalled"  # measured completed-rate at floor → STALLED
        assert body["req_per_sec_achieved"] == 0.0

    def test_route_reads_idle_when_no_admissions_in_fno_phase(self) -> None:
        """DEC-RATE-WIDGET-FNO-HONEST-001 (test iii): zero admissions in F&O → stalled, not fake.

        A built-but-idle controller (no issues in the trailing minute) pushed by the
        admission-only sampler must read STALLED at the route — the meter-less phase falls back
        to the admission signal (``recent_minute == 0``), never a fabricated "flowing".
        """
        from brutex.adapters.rate_common import RateController
        from brutex.dashboard.pull_controller import SessionRateController
        from brutex.dashboard.rate_sampler import RatePanelSampler

        controller = RateController(ceiling_per_min=500)  # built but never paced → 0 admissions
        session = SessionRateController(factory=lambda: controller)
        session.get()

        state = DashboardState()
        sampler = RatePanelSampler(session_rate=session, observer=make_rate_observer(state))
        assert sampler.sample_once() is True

        with _client(state) as c:  # type: ignore[attr-defined]
            body = c.get("/api/v1/pull/rate").json()
        assert body["active"] is True  # a tick just landed → the pull is "active"
        assert body["flow_state"] == "stalled"  # but issuing NOTHING → honestly stalled
        assert body["req_per_sec_achieved"] == 0.0
        assert body["req_per_min_rate"] == 0.0


class TestMeasuredCountsAreRealEndToEnd:
    """PROOF the displayed req/10min + req/hour are REAL tracking, not fabricated.

    DEC-RATE-MONITOR-EXPAND-001 (MIGRATION.md #1004). Drive a REAL ``RateController`` through
    ``pace()`` at known clock instants, push its real ``snapshot()`` through the production
    ``make_rate_observer`` onto the panel, and assert the ``/api/v1/pull/rate`` route reports
    the EXACT measured trailing-window counts the controller derived from actual issue
    timestamps — so the numbers the operator sees provably come from real per-request
    tracking, end to end.
    """

    def test_route_reports_controllers_real_window_counts(self) -> None:
        from brutex.adapters.rate_common import RateController

        # A plain ``def`` clock closure over a mutable holder — NOT a callable CLASS.
        # CodeQL's points-to analysis cannot resolve ``__call__`` through the
        # ``clock: Callable[[], float]`` cross-function flow, so a callable-class fixture
        # would make it wrongly report the production ``clock()`` call sites in
        # ``rate_common.py`` as ``py/call-to-non-callable``; a plain ``def`` is
        # unambiguously callable, so the analysis stays correct (same time-control behaviour).
        clock_state = {"t": 1000.0}

        def clock() -> float:
            return clock_state["t"]

        def advance(seconds: float) -> None:
            clock_state["t"] += seconds

        # A real controller with a governor (production shape). No sleeping in tests.
        ctl = RateController(clock=clock, sleep=lambda _s: None, ceiling_per_min=500)
        # Issue 5 real paces over 5 s — every issue is timestamped by pace() itself.
        for _ in range(5):
            ctl.pace()
            advance(1.0)
        # Now (t=1005): all 5 issues are within the last 60s / 600s / 3600s; only the most
        # recent (t=1004, 1s before now) is within the trailing 1s window.
        snap = ctl.snapshot()
        # Sanity: the controller's own real counts.
        assert snap["recent_60s"] == 5
        assert snap["recent_600s"] == 5
        assert snap["recent_3600s"] == 5

        state = DashboardState()
        make_rate_observer(state)(snap, {"effective_rps": 5.0})
        with _client(state) as c:  # type: ignore[attr-defined]
            body = c.get("/api/v1/pull/rate").json()
        # The route's measured metrics EQUAL the controller's real windowed issue counts —
        # provably real tracking, not a per-second figure multiplied up.
        assert body["req_per_min"] == 5
        assert body["req_per_10min"] == 5
        assert body["req_per_hour_measured"] == 5
        # The req/sec evidence count is the controller's trailing-1s count (a real subset).
        assert body["req_per_sec_count"] == snap["recent_1s"]
