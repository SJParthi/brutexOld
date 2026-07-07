"""Integration tests for the cross-process CLI→dashboard rate bridge (DEC-CLI-RATE-BRIDGE-001).

End-to-end: a CLI-style pull publishes its rate to ``<lake_root>/.rate/pull_rate.json``,
and the dashboard ``/api/v1/pull/rate`` route surfaces it on the widget JSON — so a
``brutex fetch`` pull (a separate process) is VISIBLE on the dashboard live-rate widget.
Also covers: a stale bridge → idle, no bridge file → the in-memory panel fallback, a
panel fresher than the bridge keeps the dashboard numbers, and the route never 500s when
no instrument store is wired (no resolvable lake root).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from brutex.dashboard.app import DashboardState, build_app, make_rate_observer
from brutex.data.rate_bridge import publish_rate_snapshot


class _FakeStore:
    """A minimal instrument-store stand-in exposing only ``.path`` (its parent = lake root)."""

    def __init__(self, lake_root: Path) -> None:
        self.path = lake_root / "instruments.duckdb"


def _client(
    state: DashboardState, instrument_store: Any = None, *, pull_active: bool | None = None
) -> Any:
    """``pull_active`` pins the stall gate's pull-run probe (DEC-RATE-STALL-PULL-GATE-001):
    ``True`` simulates a mid-flight DASHBOARD pull; ``None`` keeps the production
    default (the fresh app's job is not running)."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    probe = None if pull_active is None else (lambda: pull_active)
    return TestClient(build_app(state, instrument_store=instrument_store, pull_active_probe=probe))


def _publish_now(lake_root: Path, *, effective_rps: float, current_rps: float) -> None:
    """Publish a FRESH bridge snapshot (wall_time == now) into *lake_root*."""
    publish_rate_snapshot(
        lake_root,
        effective_rps=effective_rps,
        current_rps=current_rps,
        recent_minute=290,
        recent_429=3,
        ceiling_per_min=500,
        wall_time=time.time(),
        monotonic=time.monotonic(),
    )


def test_route_surfaces_fresh_cli_bridge_when_panel_idle(tmp_path: Path) -> None:
    """A fresh CLI bridge file + an idle dashboard panel → the widget shows the CLI rate."""
    _publish_now(tmp_path, effective_rps=4.8, current_rps=5.0)
    state = DashboardState()  # dashboard never pulled (panel idle)
    with _client(state, _FakeStore(tmp_path)) as c:
        body = c.get("/api/v1/pull/rate").json()
    assert body["flow_state"] == "flowing"
    assert body["active"] is True
    assert body["req_per_sec_achieved"] == 4.8
    assert body["req_per_sec_allowed"] == 5.0
    assert body["req_per_min"] == 290
    assert body["recent_429_count"] == 3


def test_route_ignores_stale_cli_bridge(tmp_path: Path) -> None:
    """A CLI bridge older than the staleness window → honest IDLE, never a stale live number."""
    publish_rate_snapshot(
        tmp_path,
        effective_rps=4.8,
        current_rps=5.0,
        recent_minute=290,
        recent_429=3,
        ceiling_per_min=500,
        wall_time=time.time() - 3600.0,  # an hour ago
        monotonic=time.monotonic(),
    )
    state = DashboardState()
    with _client(state, _FakeStore(tmp_path)) as c:
        body = c.get("/api/v1/pull/rate").json()
    assert body["flow_state"] == "idle"
    assert body["req_per_sec_achieved"] == 0.0
    assert body["req_per_min"] == 0


def test_route_no_bridge_file_uses_in_memory_panel(tmp_path: Path) -> None:
    """No CLI bridge file present → the dashboard's own in-memory panel is projected."""
    state = DashboardState()
    make_rate_observer(state)(
        {"current_rps": 8.0, "recent_hits": 1, "recent_minute": 400, "ceiling_per_min": 500},
        {"effective_rps": 7.0, "latency_ms_p95": 150.0},
    )
    with _client(state, _FakeStore(tmp_path), pull_active=True) as c:
        body = c.get("/api/v1/pull/rate").json()
    # No bridge → the dashboard pull's numbers win.
    assert body["flow_state"] == "flowing"
    assert body["req_per_sec_achieved"] == 7.0
    assert body["req_per_min"] == 400


def test_route_panel_fresher_than_bridge_keeps_dashboard_numbers(tmp_path: Path) -> None:
    """A just-ticked dashboard panel beats an older CLI bridge (the fresher source wins)."""
    publish_rate_snapshot(
        tmp_path,
        effective_rps=4.8,
        current_rps=5.0,
        recent_minute=290,
        recent_429=3,
        ceiling_per_min=500,
        wall_time=time.time() - 8.0,  # CLI ticked 8 s ago (still within the 12 s window)
        monotonic=time.monotonic(),
    )
    state = DashboardState()
    make_rate_observer(state)(
        {"current_rps": 8.0, "recent_hits": 0, "recent_minute": 410, "ceiling_per_min": 500},
        {"effective_rps": 7.5, "latency_ms_p95": 140.0},
    )  # dashboard ticked just now → fresher than the 8 s-old bridge
    with _client(state, _FakeStore(tmp_path), pull_active=True) as c:
        body = c.get("/api/v1/pull/rate").json()
    assert body["req_per_sec_achieved"] == 7.5
    assert body["req_per_min"] == 410


def test_route_no_store_wired_falls_back_to_panel(tmp_path: Path) -> None:
    """No instrument store (no resolvable lake) → the route never 500s, projects the panel."""
    state = DashboardState()  # idle panel, no store
    with _client(state, instrument_store=None) as c:
        resp = c.get("/api/v1/pull/rate")
    assert resp.status_code == 200
    assert resp.json()["flow_state"] == "idle"


def test_route_corrupt_bridge_file_falls_back_to_panel(tmp_path: Path) -> None:
    """A corrupt bridge file → ignored (read returns None), the panel is projected (no 500)."""
    bridge = tmp_path / ".rate" / "pull_rate.json"
    bridge.parent.mkdir(parents=True, exist_ok=True)
    bridge.write_text("{garbage", encoding="utf-8")
    state = DashboardState()
    make_rate_observer(state)(
        {"current_rps": 8.0, "recent_hits": 0, "recent_minute": 333, "ceiling_per_min": 500},
        {"effective_rps": 6.0, "latency_ms_p95": 100.0},
    )
    with _client(state, _FakeStore(tmp_path), pull_active=True) as c:
        resp = c.get("/api/v1/pull/rate")
    assert resp.status_code == 200
    body = resp.json()
    assert body["req_per_sec_achieved"] == 6.0  # panel, not the corrupt bridge
    assert body["req_per_min"] == 333


def test_route_bridge_pull_inactive_reads_done_not_stalled(tmp_path: Path) -> None:
    """DEC-CLI-BRIDGE-PULL-ACTIVE-001: a FRESH bridge with pull_active=False reads "done".

    Before schema v3 a just-finished CLI pull's fresh-but-zero final tick could read as a
    red "stalled"; the authoritative False flag now gates the verdict exactly like a
    dashboard pull's ``BackgroundPullJob.running`` probe.
    """
    publish_rate_snapshot(
        tmp_path,
        effective_rps=0.0,  # the pull just ended — nothing is completing any more
        current_rps=5.0,
        recent_minute=0,
        recent_429=0,
        ceiling_per_min=500,
        wall_time=time.time(),
        monotonic=time.monotonic(),
        pull_active=False,
    )
    state = DashboardState()
    with _client(state, _FakeStore(tmp_path)) as c:
        body = c.get("/api/v1/pull/rate").json()
    assert body["flow_state"] == "done"
    assert body["stalled"] is False
    assert body["active"] is False


def test_route_bridge_pull_active_true_zero_rate_reads_stalled(tmp_path: Path) -> None:
    """The True flag keeps the stall teeth: a running CLI pull at 0 achieved reads stalled."""
    publish_rate_snapshot(
        tmp_path,
        effective_rps=0.0,
        current_rps=5.0,
        recent_minute=0,
        recent_429=0,
        ceiling_per_min=500,
        wall_time=time.time(),
        monotonic=time.monotonic(),
        pull_active=True,
    )
    state = DashboardState()
    with _client(state, _FakeStore(tmp_path)) as c:
        body = c.get("/api/v1/pull/rate").json()
    assert body["flow_state"] == "stalled"
    assert body["stalled"] is True


def test_dead_cli_bridge_decays_to_idle_at_route(tmp_path: Path) -> None:
    """DEC-RATE-METER-LIFECYCLE-001 (audit C3): a DEAD CLI's stuck ``pull_active=True``
    file decays to an honest idle at the REAL route — never a PERMANENT red.

    A SIGKILLed ``brutex fetch`` leaves its last per-window ``pull_active=True`` stamp
    on disk forever (the in-process ``finally`` that flips it False never ran). On a
    freshly-restarted never-pulled dashboard (panel age = inf) any finite bridge age
    used to win the freshness pick FOREVER → a permanent site-wide red "stalled". The
    ``BRIDGE_MAX_AGE_S`` TTL treats the 2-day-old file as DEAD EVIDENCE ≡ absent.

    MUTANT KILLED: the TTL bypassed at the route/merged-projection boundary.
    """
    publish_rate_snapshot(
        tmp_path,
        effective_rps=4.8,
        current_rps=5.0,
        recent_minute=290,
        recent_429=0,
        ceiling_per_min=500,
        wall_time=time.time() - 172_800.0,  # the CLI died 2 days ago
        monotonic=time.monotonic(),
        pull_active=True,  # the stuck stamp
    )
    state = DashboardState()  # never-ticked panel (freshly-restarted dashboard)
    with _client(state, _FakeStore(tmp_path)) as c:
        body = c.get("/api/v1/pull/rate").json()
    assert body["flow_state"] == "idle"
    assert body["stalled"] is False
    assert body["active"] is False


def test_recently_dead_cli_bridge_still_reads_stalled_at_route(tmp_path: Path) -> None:
    """The 12 s < age ≤ 900 s ALERT WINDOW is preserved end to end: a CLI that died
    60 s ago (within the TTL) still reads the honest red "stalled" — the pull died
    mid-run and the operator should see it."""
    publish_rate_snapshot(
        tmp_path,
        effective_rps=4.8,
        current_rps=5.0,
        recent_minute=290,
        recent_429=0,
        ceiling_per_min=500,
        wall_time=time.time() - 60.0,  # died a minute ago — inside the alert window
        monotonic=time.monotonic(),
        pull_active=True,
    )
    state = DashboardState()
    with _client(state, _FakeStore(tmp_path)) as c:
        body = c.get("/api/v1/pull/rate").json()
    assert body["flow_state"] == "stalled"
    assert body["stalled"] is True
