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
from typing import Any

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


def _client(state: DashboardState, *, pull_active: bool | None = None) -> object:
    """Build a FastAPI ``TestClient`` over the dashboard app bound to *state*.

    ``pull_active`` injects the ``/api/v1/pull/rate`` stall gate's pull-run
    probe (DEC-RATE-STALL-PULL-GATE-001): ``None`` (default) keeps the
    PRODUCTION wiring — the app's own ``BackgroundPullJob.running`` (False on a
    fresh test app, i.e. "no pull running"); a bool pins the signal so the
    mid-pull / just-finished transitions are driven deterministically.
    """
    pytest.importorskip("fastapi")
    from brutex.dashboard.app import build_app
    from fastapi.testclient import TestClient

    probe = None if pull_active is None else (lambda: pull_active)
    return TestClient(build_app(state, pull_active_probe=probe))


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
        with _client(state, pull_active=True) as c:  # type: ignore[attr-defined]
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
        with _client(state, pull_active=True) as c:  # type: ignore[attr-defined]
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

        with _client(state, pull_active=True) as c:  # type: ignore[attr-defined]
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

        with _client(state, pull_active=True) as c:  # type: ignore[attr-defined]
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

        with _client(state, pull_active=True) as c:  # type: ignore[attr-defined]
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

        with _client(state, pull_active=True) as c:  # type: ignore[attr-defined]
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
        with _client(state, pull_active=True) as c:  # type: ignore[attr-defined]
            body = c.get("/api/v1/pull/rate").json()
        # The route's measured metrics EQUAL the controller's real windowed issue counts —
        # provably real tracking, not a per-second figure multiplied up.
        assert body["req_per_min"] == 5
        assert body["req_per_10min"] == 5
        assert body["req_per_hour_measured"] == 5
        # The req/sec evidence count is the controller's trailing-1s count (a real subset).
        assert body["req_per_sec_count"] == snap["recent_1s"]


class TestStallGateOnPullCompletion:
    """DEC-RATE-STALL-PULL-GATE-001 — the operator's mislabel, pinned end to end.

    A pull that just COMPLETED (fresh final tick, near-zero measured rate, the
    background job no longer running) must read ``done`` — NEVER the red
    ``PULL RATE stalled`` the widget showed right after an 80/80-complete pull.
    """

    def _completed_pull_state(self) -> DashboardState:
        """A panel exactly as a just-finished pull leaves it: fresh tick, rate ~0."""
        state = DashboardState()
        make_rate_observer(state)(
            {"current_rps": 8.0, "recent_hits": 0, "recent_minute": 4, "ceiling_per_min": 500},
            {"effective_rps": 0.0, "latency_ms_p95": 250.0},
        )
        return state

    def test_production_default_reads_done_after_completion(self) -> None:
        """The PRODUCTION wiring (no injected probe): job not running → done."""
        state = self._completed_pull_state()
        with _client(state) as c:  # type: ignore[attr-defined]
            body = c.get("/api/v1/pull/rate").json()
        assert body["flow_state"] == "done"
        assert body["stalled"] is False
        assert body["active"] is False
        # History, not a live rate — zeroed (Golden Rule #1); staleness stays real.
        assert body["req_per_sec_achieved"] == 0.0
        assert body["req_per_min"] == 0
        assert body["seconds_since_update"] is not None

    def test_explicit_no_pull_probe_reads_done(self) -> None:
        """An injected pull_active=False probe pins the gate deterministically."""
        state = self._completed_pull_state()
        with _client(state, pull_active=False) as c:  # type: ignore[attr-defined]
            body = c.get("/api/v1/pull/rate").json()
        assert body["flow_state"] == "done"
        assert body["stalled"] is False

    def test_same_panel_mid_pull_still_reads_stalled(self) -> None:
        """The SAME near-zero panel DURING an active pull keeps the honest red."""
        state = self._completed_pull_state()
        with _client(state, pull_active=True) as c:  # type: ignore[attr-defined]
            body = c.get("/api/v1/pull/rate").json()
        assert body["flow_state"] == "stalled"
        assert body["stalled"] is True

    def test_fresh_dashboard_stays_plain_idle(self) -> None:
        """No tick ever + no pull → plain idle (done is reserved for a fresh tick)."""
        state = DashboardState()
        with _client(state) as c:  # type: ignore[attr-defined]
            body = c.get("/api/v1/pull/rate").json()
        assert body["flow_state"] == "idle"
        assert body["stalled"] is False


class TestLocalPhaseAmber:
    """Amber 'local' during the zero-vendor-call pipeline stages
    (DEC-PIPELINE-PHASE-UI-HONESTY-001).

    The route threads the pipeline's coarse phase (the injectable
    ``pull_phase_probe`` seam; production default = the app's own
    ``BackgroundPullJob.snapshot().phase``) into the PANEL-side projection, so a
    healthy prebuild/movers-warm stage reads ``local`` — never the red
    ``stalled`` the vendor-rate heuristics read off its correctly-flatlined
    fetch rate. Strictly phase-keyed: the historical fetch phase still stalls.
    """

    @staticmethod
    def _fresh_zero_rate_panel() -> DashboardState:
        """A state whose panel just ticked with a ~0 completed rate (local stage)."""
        state = DashboardState()
        make_rate_observer(state)(
            {"current_rps": 8.0, "recent_hits": 0, "recent_minute": 0, "ceiling_per_min": 500},
            {"effective_rps": 0.0, "latency_ms_p95": 5.0},
        )
        return state

    def _phase_client(self, state: DashboardState, *, phase: str) -> object:
        pytest.importorskip("fastapi")
        from brutex.dashboard.app import build_app
        from fastapi.testclient import TestClient

        return TestClient(
            build_app(
                state,
                pull_active_probe=lambda: True,
                pull_phase_probe=lambda: phase,
            )
        )

    def test_prebuild_phase_reads_local_amber_not_stalled(self) -> None:
        """Regression (fails before the fix): a healthy prebuild used to read red."""
        state = self._fresh_zero_rate_panel()
        # The pre-fix shape: same panel, pull active, NO phase signal → stalled.
        with _client(state, pull_active=True) as c:  # type: ignore[attr-defined]
            assert c.get("/api/v1/pull/rate").json()["flow_state"] == "stalled"
        # The fix: the phase probe names the local prebuild stage → amber local.
        with self._phase_client(state, phase="prebuild") as c:  # type: ignore[attr-defined]
            body = c.get("/api/v1/pull/rate").json()
        assert body["flow_state"] == "local"
        assert body["stalled"] is False
        assert body["flow_detail"] == "local processing — prebuild (no vendor calls)"

    def test_movers_warm_phase_reads_local(self) -> None:
        state = self._fresh_zero_rate_panel()
        with self._phase_client(state, phase="movers") as c:  # type: ignore[attr-defined]
            body = c.get("/api/v1/pull/rate").json()
        assert body["flow_state"] == "local"
        assert body["flow_detail"] == "local processing — movers (no vendor calls)"

    def test_historical_phase_zero_rate_still_reads_stalled(self) -> None:
        """THE MANDATED PIN end-to-end: a dead FETCH phase is a real stall."""
        state = self._fresh_zero_rate_panel()
        with self._phase_client(state, phase="historical") as c:  # type: ignore[attr-defined]
            body = c.get("/api/v1/pull/rate").json()
        assert body["flow_state"] == "stalled"
        assert body["stalled"] is True
        assert body["flow_detail"] == ""

    def test_widget_renders_amber_local_state(self) -> None:
        """The served widget carries the amber 'local' classes + flow_detail wiring."""
        state = DashboardState()
        with _client(state) as c:  # type: ignore[attr-defined]
            js = c.get("/static/bx-rate-monitor.js").text
        # Amber CSS for every dot/state twin (compact pill + expanded overlay).
        assert ".bxrm-dot.local{background:var(--yellow,#FFB74D)" in js
        assert ".bxrm-state.local{color:var(--yellow,#FFB74D)" in js
        assert ".bxrm-edot.local{background:var(--yellow,#FFB74D)" in js
        assert ".bxrm-estate.local{color:var(--yellow,#FFB74D)" in js
        # setClass whitelists "local" (a new state must never silently render dark).
        assert 'el.classList.remove("flowing", "stalled", "local", "idle")' in js
        assert 'state === "flowing" || state === "stalled" || state === "local"' in js
        # flow_detail reaches the expanded state text (textContent — no HTML sink).
        assert "data.flow_detail" in js
        assert "eStateEl.textContent = detail" in js


class TestFailingVerdictEndToEnd:
    """DEC-PULL-TELEMETRY-TRIAD-001 — the meter's measured ``failed_rps`` reaches the
    ``/api/v1/pull/rate`` verdict end to end: an issuing-but-ALL-FAILING pull reads the
    distinct red "failing", closing the #1022 flagged follow-up."""

    def test_issuing_but_all_failing_reads_failing(self) -> None:
        state = DashboardState()
        # A REAL failing meter snapshot shape: nothing completing, failures burning.
        make_rate_observer(state)(
            {"current_rps": 8.0, "recent_hits": 0, "recent_minute": 240, "ceiling_per_min": 500},
            {"effective_rps": 0.0, "latency_ms_p95": 0.0, "failed_rps": 2.5},
        )
        with _client(state, pull_active=True) as c:  # type: ignore[attr-defined]
            resp = c.get("/api/v1/pull/rate")
        body = resp.json()
        assert body["flow_state"] == "failing"
        assert body["stalled"] is True  # red-light consumers unchanged
        assert body["req_per_sec_failed"] == 2.5
        assert body["req_per_sec_achieved"] == 0.0

    def test_no_failures_keeps_the_plain_stalled_verdict(self) -> None:
        """PARITY: the pre-decision stalled verdict is unchanged when failed_rps is 0."""
        state = DashboardState()
        make_rate_observer(state)(
            {"current_rps": 8.0, "recent_hits": 50, "recent_minute": 2, "ceiling_per_min": 500},
            {"effective_rps": 0.0, "latency_ms_p95": 9999.0},
        )
        with _client(state, pull_active=True) as c:  # type: ignore[attr-defined]
            resp = c.get("/api/v1/pull/rate")
        body = resp.json()
        assert body["flow_state"] == "stalled"
        assert body["req_per_sec_failed"] == 0.0

    def test_widget_renders_failing_as_the_red_stalled_styling(self) -> None:
        """The widget script maps the "failing" state onto the red stalled class (an
        unknown state would silently fall back to the dark idle styling — hiding red)."""
        js = (
            Path(__file__).resolve().parents[2]
            / "brutex"
            / "dashboard"
            / "static"
            / "bx-rate-monitor.js"
        ).read_text(encoding="utf-8")
        assert 'state === "stalled" || state === "failing"' in js


# ---------------------------------------------------------------------------
# DEC-RATE-METER-LIFECYCLE-001 (MIGRATION.md #1482): the meter lifecycle + the
# kind-aware rate probe, end to end through the REAL routes + REAL job.
# ---------------------------------------------------------------------------


def _nifty_instrument() -> object:
    """A minimal tradable NIFTY row for the wired-store routes."""
    from decimal import Decimal

    from brutex.models import Exchange, Instrument, InstrumentType, Segment

    return Instrument(
        exch_id=Exchange.NSE,
        security_id=13,
        symbol_name="NIFTY",
        display_name="NIFTY",
        instrument=InstrumentType.IDX,
        segment=Segment.INDEX,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=0,
        tick_size=Decimal("0"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
    )


def _wired_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **app_kwargs: Any
) -> tuple[Any, Any]:
    """Build a ``(TestClient, InstrumentStore)`` pair over a real temp store.

    ISOLATION (review round 2): pins ``BRUTEX_LAKE_ROOT`` to a per-test tmp dir so
    the app's ``BackgroundPullJob`` (built with the DEFAULT lake-root resolver — the
    production wiring these tests deliberately exercise) takes its cross-process
    single-flight ``flock`` under ``tmp_path``, NEVER under the operator's real
    ``~/.brutex/lake`` — so consecutive tests, xdist shards, or a genuinely-running
    local pull can never collide on the global lock file.
    """
    pytest.importorskip("duckdb")
    from datetime import date

    from brutex.dashboard.app import build_app
    from brutex.data.instrument_store import InstrumentStore
    from fastapi.testclient import TestClient

    monkeypatch.setenv("BRUTEX_LAKE_ROOT", str(_job_lake_root(tmp_path)))
    store = InstrumentStore.open(tmp_path / "instruments.duckdb")
    store.upsert_many(
        [_nifty_instrument()],  # type: ignore[list-item]
        snapshot_date=date(2024, 1, 2),
        run_id="r1",
        csv_source_sha256="0" * 64,
    )
    client = TestClient(build_app(DashboardState(), instrument_store=store, **app_kwargs))  # type: ignore[arg-type]
    return client, store


def _job_lake_root(tmp_path: Path) -> Path:
    """The per-test lake root the job's default resolver reads via ``BRUTEX_LAKE_ROOT``."""
    return tmp_path / "job_lake"


def _wait_for_job_settled(
    client: Any, *, lake_root: Path, timeout: float = 20.0
) -> dict[str, object]:
    """Poll ``/pull-progress`` until settled, THEN wait for the single-flight release.

    The job flips the progress state to ``done``/``error`` EARLY
    (``BackgroundPullJob._run``'s try/else), and only LATER — inside the
    identity-gated ``finally`` — fires ``on_finally`` + ``on_run_end`` (the meter
    clear) and releases the cross-process ``flock``. A settle-only wait therefore
    races BOTH the meter-clear assertions and the NEXT test's ``start`` (the
    captured ``pull_job.singleflight_refused`` flake — review round 2). The second
    phase polls until the lake-root single-flight lock is acquirable, which by the
    ``finally``'s ordering guarantees ``on_run_end`` has already run.
    """
    from brutex.adapters.groww.pull_singleflight import (
        PullAlreadyRunningError,
        pull_singleflight_lock,
    )

    deadline = time.monotonic() + timeout
    snap: dict[str, object] | None = None
    while time.monotonic() < deadline:
        got = client.get("/api/v1/universe/pull-progress").json()  # type: ignore[attr-defined]
        assert isinstance(got, dict)
        if got["state"] in {"done", "error"}:
            snap = got
            break
        time.sleep(0.01)
    if snap is None:
        raise AssertionError("the background job did not settle within the deadline")
    while time.monotonic() < deadline:
        try:
            with pull_singleflight_lock(lake_root):
                pass
        except PullAlreadyRunningError:
            time.sleep(0.01)
        else:
            return snap
    raise AssertionError("the settled job did not release the single-flight lock in time")


class TestSweepKindAwareRateProbe:
    """Audit C5: a dashboard SWEEP on the shared job must never red the widget."""

    def test_sweep_on_shared_job_reads_idle_not_stalled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The C5 repro pin (FAILS on pre-#1482 HEAD — it read a red ``stalled``).

        MUTANTS KILLED: ``job_kind=JOB_KIND_SWEEP`` dropped from the sweep route's
        ``start`` (the default pull kind → stalled); the default rate probe left as
        ``lambda: _pull_job.running`` (sweep-inclusive → stalled); ``==`` inverted
        to ``!=``; the ``running_job_kind`` kind gate severed.
        """
        import importlib
        import threading

        app_module = importlib.import_module("brutex.dashboard.app")
        release = threading.Event()
        started_running = threading.Event()

        def _blocking_sweep(request: object, *, lake_root: Path, results_root: Path) -> int:
            started_running.set()
            assert release.wait(timeout=20.0), "the test must release the sweep"
            return 0

        monkeypatch.setattr(app_module, "run_sweep_from_request", _blocking_sweep)
        # DEFAULT probes — the production wiring
        client, store = _wired_client(tmp_path, monkeypatch)
        try:
            with client:  # type: ignore[attr-defined]
                resp = client.post("/api/v1/sweep/run")  # type: ignore[attr-defined]
                assert resp.status_code == 200
                assert resp.json()["started"] is True
                assert started_running.wait(timeout=10.0)
                body = client.get("/api/v1/pull/rate").json()  # type: ignore[attr-defined]
                # The honest verdict for a zero-vendor-call sweep: dark idle — NEVER
                # the site-wide red "stalled" the audit reproduced for 2-4-day runs.
                assert body["flow_state"] == "idle"
                assert body["stalled"] is False
                assert body["active"] is False
                release.set()
                settled = _wait_for_job_settled(client, lake_root=_job_lake_root(tmp_path))
                assert settled["state"] == "done"
                after = client.get("/api/v1/pull/rate").json()  # type: ignore[attr-defined]
                assert after["flow_state"] == "idle"
        finally:
            release.set()  # a failed assertion must never wedge the single-flight job
            store.close()  # type: ignore[attr-defined]

    def test_real_pull_kind_still_reads_stalled_via_default_probe(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The #1117 stall-gate pin PRESERVED through the NEW kind-aware probe.

        MUTANTS KILLED: the default rate probe hardcoded to ``False`` /
        ``running_job_kind`` returning ``None`` for a pull-kind run — a live
        pull with a never-ticked panel must STILL read the red ``stalled``.
        """
        import importlib
        import threading

        app_module = importlib.import_module("brutex.dashboard.app")
        release = threading.Event()
        started_running = threading.Event()

        def _blocking_metadata_pull(*args: object, **kwargs: object) -> object:
            started_running.set()
            assert release.wait(timeout=20.0), "the test must release the pull"
            raise RuntimeError("released — end the fake pull without a report")

        monkeypatch.setattr(app_module, "pull_expiries_contracts", _blocking_metadata_pull)
        client, store = _wired_client(tmp_path, monkeypatch)  # DEFAULT probes
        try:
            with client:  # type: ignore[attr-defined]
                resp = client.post(
                    "/api/v1/universe/pull-expiries-contracts",
                    params={"symbols": "NIFTY", "dry_run": "true"},
                )  # type: ignore[attr-defined]
                assert resp.status_code == 200
                assert resp.json()["started"] is True
                assert started_running.wait(timeout=10.0)
                body = client.get("/api/v1/pull/rate").json()  # type: ignore[attr-defined]
                # A PULL-kind job with a never-ticked panel: the kind-aware probe
                # reads True → the honest verdict is the red "stalled" (#1117).
                assert body["stalled"] is True
                assert body["flow_state"] == "stalled"
                release.set()
                settled = _wait_for_job_settled(client, lake_root=_job_lake_root(tmp_path))
                assert settled["state"] == "error"
        finally:
            release.set()
            store.close()  # type: ignore[attr-defined]


class TestMeterLifecycleAppWiring:
    """C0 + D1 end-to-end: the job-end clear + the dry-run deregistration, through
    ``build_app``'s REAL construction (the injectable ``session_rate`` seam)."""

    def test_job_end_clears_registered_meter_via_app_wiring(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MUTANT KILLED: ``on_run_end=_session_rate.clear_meter`` omitted from the
        ``BackgroundPullJob`` construction — the registered meter would survive the
        job's end (the C0 production-wiring pin).

        Uses the SWEEP route (whose work NEVER touches ``register_pull_meter``) so
        the assertion isolates the job-end clear from the dry-run gate — a pull
        route's own build-site deregistration could mask the severed wiring.
        """
        import importlib

        from brutex.adapters.call_meter import CallMeter
        from brutex.dashboard.pull_controller import SessionRateController

        app_module = importlib.import_module("brutex.dashboard.app")
        monkeypatch.setattr(
            app_module, "run_sweep_from_request", lambda request, *, lake_root, results_root: 0
        )
        injected = SessionRateController()
        client, store = _wired_client(tmp_path, monkeypatch, session_rate=injected)
        try:
            with client:  # type: ignore[attr-defined]
                injected.register_meter(CallMeter())  # pull #1's meter (simulated)
                assert injected.peek_meter() is not None
                assert client.post("/api/v1/sweep/run").json()["started"] is True  # type: ignore[attr-defined]
                # The release-aware settle wait is the ordering barrier: the flock is
                # released strictly AFTER on_run_end in the same identity-gated
                # finally, so the peek below can never race the clear it pins.
                settled = _wait_for_job_settled(client, lake_root=_job_lake_root(tmp_path))
                assert settled["state"] == "done"
                assert injected.peek_meter() is None, (
                    "the job's identity-gated finally must clear the registered meter"
                )
        finally:
            store.close()  # type: ignore[attr-defined]

    def test_warm_process_dry_run_stays_honest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The D1/C0 END-TO-END ORACLE (vs the audit's captured `flowing achieved: 5.0`).

        A WARM process — an earlier real pull built the controller AND registered a
        TICKED CallMeter — then a genuine ``?dry_run=true`` metadata pull runs the
        REAL route end to end (the real offline ``_build_dry_run_client``). Post-
        completion the state must be honest: no meter registered, and the final
        rate read is never a fabricated MEASURED "flowing". Asserts POST-COMPLETION
        state only (never an intermediate 2 s sampler tick — flake-hardened).

        MUTANT KILLED: a full revert of {the site's ``dry_run=`` kwarg + the
        ``register_pull_meter`` gate + the job-end clear} together.
        """
        from brutex.adapters.call_meter import CallMeter
        from brutex.adapters.rate_common import RateController
        from brutex.dashboard.pull_controller import SessionRateController

        warmed = SessionRateController(factory=lambda: RateController(ceiling_per_min=500))
        warmed.get()  # an earlier REAL pull built the controller (never torn down)
        ticked = CallMeter()
        for _ in range(25):  # …and its meter measured real completions
            ticked.start()
            ticked.stop(elapsed_ms=100.0, completed=True)
        warmed.register_meter(ticked)

        client, store = _wired_client(tmp_path, monkeypatch, session_rate=warmed)
        try:
            with client:  # type: ignore[attr-defined]
                resp = client.post(
                    "/api/v1/universe/pull-expiries-contracts",
                    params={"symbols": "NIFTY", "dry_run": "true"},
                )  # type: ignore[attr-defined]
                assert resp.status_code == 200
                assert resp.json()["started"] is True
                settled = _wait_for_job_settled(client, lake_root=_job_lake_root(tmp_path))
                assert settled["state"] == "done"
                # The dead pull's meter is gone (the dry-run gate cleared it at the
                # build site, and the job-end hook cleared again — belt-and-braces).
                assert warmed.peek_meter() is None
                body = client.get("/api/v1/pull/rate").json()  # type: ignore[attr-defined]
                # NEVER a fabricated MEASURED flowing rate after a synthetic pull.
                assert not (body["flow_state"] == "flowing" and body["req_per_sec_achieved"] > 0.0)
                assert body["req_per_sec_achieved"] == 0.0
        finally:
            store.close()  # type: ignore[attr-defined]

    def test_build_app_default_session_rate_byte_identical(self) -> None:
        """The seam's DEFAULT arm: ``session_rate=None`` builds the app's own holder
        and the rate route behaves exactly as before (honest idle on a fresh app)."""
        pytest.importorskip("fastapi")
        from brutex.dashboard.app import build_app
        from fastapi.testclient import TestClient

        with TestClient(build_app(DashboardState(), session_rate=None)) as c:
            body = c.get("/api/v1/pull/rate").json()
        assert body["flow_state"] == "idle"
        assert body["active"] is False
        assert body["req_per_sec_achieved"] == 0.0

    def test_pull_live_stays_sweep_inclusive(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The anti-#920-regression pin: ``_pull_live`` (the duty-degrade probe every
        cache floor shares) DELIBERATELY stays sweep-INCLUSIVE — only the rate route
        went kind-aware.

        MUTANT KILLED: a future "unification" of ``_pull_live`` onto the kind-aware
        probe (the SummaryCache floors would stop engaging during a 2-4-day sweep,
        resurrecting the #920 starvation).
        """
        import importlib
        import threading

        from brutex.dashboard.summary_cache import SummaryCache

        app_module = importlib.import_module("brutex.dashboard.app")
        release = threading.Event()
        started_running = threading.Event()

        def _blocking_sweep(request: object, *, lake_root: Path, results_root: Path) -> int:
            started_running.set()
            assert release.wait(timeout=20.0), "the test must release the sweep"
            return 0

        monkeypatch.setattr(app_module, "run_sweep_from_request", _blocking_sweep)

        captured: dict[str, object] = {}

        class _ProbeCapturingCache(SummaryCache):
            def set_pull_active_probe(self, probe: object) -> None:  # type: ignore[override]
                captured["probe"] = probe
                super().set_pull_active_probe(probe)  # type: ignore[arg-type]

        cache = _ProbeCapturingCache()
        client, store = _wired_client(tmp_path, monkeypatch, summary_cache=cache)
        try:
            with client:  # type: ignore[attr-defined]
                probe = captured["probe"]
                assert callable(probe)
                assert probe() is False  # idle — no job running
                assert client.post("/api/v1/sweep/run").json()["started"] is True  # type: ignore[attr-defined]
                assert started_running.wait(timeout=10.0)
                assert probe() is True, (
                    "_pull_live must read True during a SWEEP — the #920 duty-degrade "
                    "floors still engage (only the rate route is kind-aware)"
                )
                release.set()
                settled = _wait_for_job_settled(client, lake_root=_job_lake_root(tmp_path))
                assert settled["state"] == "done"
                assert probe() is False
        finally:
            release.set()
            store.close()  # type: ignore[attr-defined]


class TestDirectPullRouteMeterLifecycle:
    """Round 2: the DIRECT (off-job) single-symbol route ``POST /api/v1/universe/pull``.

    It bypasses the single-flight ``BackgroundPullJob`` (no ``on_run_end`` ever fires
    for it), so it must pair its OWN meter lifecycle: a REAL pull clears its build-site
    registration in the route's ``finally`` (success AND failure — the C0 residual),
    and a DRY-RUN passes NO holder so it can never evict a LIVE pull's meter through
    ``register_pull_meter``'s dry-run gate.
    """

    @staticmethod
    def _metered_source() -> Any:
        """A stand-in for the live adapter: carries a real ``.meter`` CallMeter."""
        from brutex.adapters.call_meter import CallMeter

        class _Source:
            def __init__(self) -> None:
                self.meter = CallMeter()

        return _Source()

    def test_direct_real_pull_clears_meter_after_success(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MUTANT KILLED: the route's ``finally: _session_rate.clear_meter()`` omitted —
        a finished single-symbol pull's meter stayed registered with NO pull running
        (readable by the next sampler-wrapped run's pre-registration ticks)."""
        import importlib

        from brutex.dashboard.pull_controller import (
            PullCandlesReport,
            SessionRateController,
            register_pull_meter,
        )

        app_module = importlib.import_module("brutex.dashboard.app")
        monkeypatch.setattr(app_module, "groww_credentials_error", lambda: None)
        injected = SessionRateController()
        seen: dict[str, object] = {}
        source = self._metered_source()

        def _fake_pull_candles(instrument: Any, **kwargs: Any) -> Any:
            seen["session_rate"] = kwargs["session_rate"]
            # Simulate the REAL build-site registration inside pull_candles.
            register_pull_meter(kwargs["session_rate"], source, dry_run=False)
            assert injected.peek_meter() is source.meter
            return PullCandlesReport(symbol=instrument.symbol_name, year=2024, dry_run=False)

        monkeypatch.setattr(app_module, "pull_candles", _fake_pull_candles)
        client, store = _wired_client(tmp_path, monkeypatch, session_rate=injected)
        try:
            with client:
                resp = client.post("/api/v1/universe/pull", params={"symbol": "NIFTY"})
                assert resp.status_code == 200
                assert resp.json()["dry_run"] is False
                # The REAL arm threads the app's injected holder (never None).
                assert seen["session_rate"] is injected
                assert injected.peek_meter() is None, (
                    "the direct route's finally must clear its build-site registration "
                    "— no in-process pull running ⇒ no meter registered"
                )
        finally:
            store.close()

    def test_direct_real_pull_failure_still_clears_meter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ``finally`` covers the FAILURE arm too: a pull that registered its meter
        then raised (the route's 400 path) must not leave the dead meter registered."""
        import importlib

        from brutex.dashboard.pull_controller import SessionRateController, register_pull_meter

        app_module = importlib.import_module("brutex.dashboard.app")
        monkeypatch.setattr(app_module, "groww_credentials_error", lambda: None)
        injected = SessionRateController()
        source = self._metered_source()

        def _registering_boom(instrument: Any, **kwargs: Any) -> Any:
            register_pull_meter(kwargs["session_rate"], source, dry_run=False)
            assert injected.peek_meter() is source.meter
            raise RuntimeError("vendor exploded mid-pull")

        monkeypatch.setattr(app_module, "pull_candles", _registering_boom)
        client, store = _wired_client(tmp_path, monkeypatch, session_rate=injected)
        try:
            with client:
                resp = client.post("/api/v1/universe/pull", params={"symbol": "NIFTY"})
                assert resp.status_code == 400
                assert "RuntimeError" in resp.json()["detail"]
                assert injected.peek_meter() is None, (
                    "the finally must clear the dead pull's meter on the failure arm too"
                )
        finally:
            store.close()

    def test_direct_dry_run_never_evicts_live_meter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MUTANT KILLED: ``session_rate=_session_rate`` passed unconditionally (the
        pre-round-2 shape) — a concurrent ``?dry_run=true`` on this route (which
        bypasses the single-flight job) reached ``register_pull_meter``'s dry-run gate
        and BLANKED a live pull's registered meter mid-phase. The dry-run arm now
        passes NO holder, driven end to end through the REAL offline dry-run spine."""
        from brutex.adapters.call_meter import CallMeter
        from brutex.dashboard.pull_controller import SessionRateController

        live = SessionRateController()
        live_meter = CallMeter()
        live.register_meter(live_meter)  # a LIVE job pull's registration (simulated)
        client, store = _wired_client(tmp_path, monkeypatch, session_rate=live)
        try:
            with client:
                resp = client.post(
                    "/api/v1/universe/pull",
                    params={"symbol": "NIFTY", "dry_run": "true"},
                )
                assert resp.status_code == 200
                assert resp.json()["dry_run"] is True
                assert live.peek_meter() is live_meter, (
                    "an operator's concurrent dry-run on the off-job route must never "
                    "evict a live pull's registered meter (and the finally clear is "
                    "real-pull-only)"
                )
        finally:
            store.close()
