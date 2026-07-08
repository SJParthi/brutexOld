"""Route integration for the INTERIM ``/launch`` page (DEC-SWEEP-LAUNCH-BUTTON-INTERIM-001).

Drives the REAL routes through ``build_app`` + ``TestClient`` (the house
convention — mirrors ``test_dashboard_sweep_run_route.py``): the page render,
the polled ``/api/v1/launch/state`` payload, and the single-flight POST
(200 started · 409 already-running · 400 invalid card / junk ack /
futures-unavailable · 412 red preconditions · 500 failed launch · 503 no
lake). The detached spawner is ALWAYS monkeypatched — no test launches a
real sweep. Skipped cleanly when the ``[ui]`` extra (FastAPI) is absent.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("fastapi", reason="dashboard routes need the [ui] extra")

from brutex.dashboard import launch_button as lb
from brutex.dashboard.app import DashboardState, build_app
from fastapi.testclient import TestClient


@pytest.fixture
def lake(tmp_path: Path) -> Path:
    # M4: the presence precondition covers ALL 21 interval partitions.
    for interval in lb.LOCKED_INTERVALS:
        (tmp_path / "bars" / "NSE" / "INDEX" / "NIFTY" / interval).mkdir(parents=True)
    (tmp_path / "sweeps").mkdir()
    return tmp_path


@pytest.fixture
def client(lake: Path) -> Iterator[TestClient]:
    # A lean pathed store stand-in is enough for the launch routes — the
    # resolver only reads `.path` (the app.py store convention).
    store = SimpleNamespace(path=lake / "instruments.duckdb")
    with TestClient(build_app(DashboardState(), instrument_store=store)) as c:
        yield c


@pytest.fixture
def bare_client() -> Iterator[TestClient]:
    with TestClient(build_app(DashboardState())) as c:
        yield c


@pytest.fixture
def fake_spawner(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    record: dict[str, Any] = {}

    def _spawn(argv: list[str], log_path: Path) -> int:
        record["argv"] = argv
        record["log_path"] = log_path
        return 34567

    monkeypatch.setattr(lb, "_default_spawner", _spawn)
    # The fake pid 34567 may collide with a REAL sandbox process whose
    # cmdline is not a sweep — pin the disambiguation probe to "unknown"
    # so liveness rides the (also monkeypatched where needed) pid probe.
    monkeypatch.setattr(lb, "_cmdline_contains_sweep_all", lambda pid: None)
    return record


class TestLaunchPage:
    def test_page_renders_interim_banner(self, client: TestClient) -> None:
        resp = client.get("/launch")
        assert resp.status_code == 200
        body = resp.text
        assert "INTERIM" in body
        assert "textContent" in body  # textContent-only DOM updates
        assert "innerHTML" not in body  # CodeQL XSS guard — never innerHTML
        assert "/api/v1/launch/state" in body
        assert "/api/v1/launch/sweep" in body
        assert "Restart the dashboard BEFORE launching" in body
        # coordinator delta: the off-by-default SLA acknowledge checkbox
        assert 'id="ackSla"' in body
        assert "acknowledge_sla_overrun" in body
        # M4 honesty caveat: freshness is the CLI preflight's job
        assert "FRESHNESS is verified by the CLI" in body
        # M1: keep-awake is a separate watcher note
        assert "caffeinate -dimsu -w" in body
        assert "follow-on delta run" in body  # audit delta: k=6 note

    def test_page_renders_without_store(self, bare_client: TestClient) -> None:
        assert bare_client.get("/launch").status_code == 200


class TestLaunchStateRoute:
    def test_state_payload(self, client: TestClient, lake: Path) -> None:
        resp = client.get("/api/v1/launch/state")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["interim"] is True
        assert payload["lake_root"] == str(lake)
        assert payload["grid_note"] == lb.GRID_HONESTY_NOTE
        names = {c["name"] for c in payload["preconditions"]}
        assert "nifty_interval_partitions" in names
        assert "no_active_launch" in names
        assert payload["cards"]["spot"]["enabled"] is True
        # coordinator delta: base command display carries NO ack flag
        assert lb.SLA_ACK_FLAG not in payload["cards"]["spot"]["command_display"]
        assert payload["cards"]["spot"]["sla_ack_flag"] == lb.SLA_ACK_FLAG
        # On main the futures alias layer (PR #1264) is absent — honest verdict.
        assert payload["futures"]["available"] is False
        assert payload["cards"]["futures"]["enabled"] is False
        assert "PR #1264" in payload["cards"]["futures"]["reason"]

    def test_state_without_store_is_honest(self, bare_client: TestClient) -> None:
        payload = bare_client.get("/api/v1/launch/state").json()
        assert payload["lake_root"] is None
        assert payload["cards"]["spot"]["enabled"] is False
        assert payload["active"] == {"present": False, "running": False}


class TestLaunchSweepRoute:
    def test_invalid_card_400(self, client: TestClient) -> None:
        resp = client.post("/api/v1/launch/sweep?card=evil;rm -rf /")
        assert resp.status_code == 400
        assert "unknown card" in resp.json()["detail"]

    def test_missing_card_422(self, client: TestClient) -> None:
        assert client.post("/api/v1/launch/sweep").status_code == 422

    def test_junk_ack_param_400_strict_whitelist(self, client: TestClient) -> None:
        """Coordinator delta: the ack is a STRICT true/false whitelist."""
        for junk in ("1", "yes", "TRUE", "True", "on", ""):
            resp = client.post(f"/api/v1/launch/sweep?card=spot&acknowledge_sla_overrun={junk}")
            assert resp.status_code == 400, junk
            assert "acknowledge_sla_overrun" in resp.json()["detail"]

    def test_no_lake_503(self, bare_client: TestClient) -> None:
        resp = bare_client.post("/api/v1/launch/sweep?card=spot")
        assert resp.status_code == 503

    def test_futures_unavailable_400_on_main(self, client: TestClient) -> None:
        resp = client.post("/api/v1/launch/sweep?card=futures")
        assert resp.status_code == 400
        assert "PR #1264" in resp.json()["detail"]

    def test_missing_partitions_412_nothing_spawned(
        self, tmp_path: Path, fake_spawner: dict[str, Any]
    ) -> None:
        """m3: POST-time re-check — red preconditions refuse with 412."""
        # Only 1minute present — 20 partitions missing.
        (tmp_path / "bars" / "NSE" / "INDEX" / "NIFTY" / "1minute").mkdir(parents=True)
        store = SimpleNamespace(path=tmp_path / "instruments.duckdb")
        with TestClient(build_app(DashboardState(), instrument_store=store)) as c:
            resp = c.post("/api/v1/launch/sweep?card=spot")
        assert resp.status_code == 412
        assert "nifty_interval_partitions" in resp.json()["detail"]
        assert "argv" not in fake_spawner

    def test_failed_launch_500_with_detail(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """M2 surfacing: a failed (compensated) launch is an honest 500."""
        monkeypatch.setattr(
            lb,
            "launch_sweep",
            lambda card, **kw: lb.LaunchOutcome(
                started=False, status="failed", active=None, detail="finalize failed"
            ),
        )
        resp = client.post("/api/v1/launch/sweep?card=spot")
        assert resp.status_code == 500
        body = resp.json()
        assert body["status"] == "failed"
        assert body["detail"] == "finalize failed"

    def test_spot_launch_started_then_409_single_flight(
        self,
        client: TestClient,
        lake: Path,
        fake_spawner: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Make the recorded pid read as ALIVE so the second click is refused.
        monkeypatch.setattr(lb, "probe_pid", lambda pid: "running" if pid == 34567 else "exited")
        first = client.post("/api/v1/launch/sweep?card=spot")
        assert first.status_code == 200
        body = first.json()
        assert body["started"] is True
        assert body["status"] == "started"
        assert body["active"]["pid"] == 34567
        assert body["active"]["card"] == "spot"
        argv = fake_spawner["argv"]
        assert "--symbols" in argv
        assert argv[argv.index("--symbols") + 1] == "NIFTY"
        # coordinator delta: default POST carries NO ack flag
        assert lb.SLA_ACK_FLAG not in argv
        # double-click: HTTP 409, NEVER a duplicate launch
        fake_spawner.pop("argv")
        second = client.post("/api/v1/launch/sweep?card=spot")
        assert second.status_code == 409
        body2 = second.json()
        assert body2["started"] is False
        assert body2["status"] == "already_running"
        assert body2["active"]["pid"] == 34567
        assert "argv" not in fake_spawner
        # the state poll reflects the active run
        state = client.get("/api/v1/launch/state").json()
        assert state["active"]["present"] is True
        assert state["active"]["running"] is True
        assert state["active"]["pid"] == 34567

    def test_acknowledged_spot_launch_appends_flag(
        self, client: TestClient, fake_spawner: dict[str, Any]
    ) -> None:
        resp = client.post("/api/v1/launch/sweep?card=spot&acknowledge_sla_overrun=true")
        assert resp.status_code == 200
        assert fake_spawner["argv"][-1] == lb.SLA_ACK_FLAG
        assert resp.json()["active"]["acknowledge_sla"] is True

    def test_futures_launch_flows_when_alias_available(
        self,
        client: TestClient,
        fake_spawner: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            lb,
            "probe_futures_alias",
            lambda: lb.FuturesAvailability(available=True, reason="alias merged"),
        )
        resp = client.post("/api/v1/launch/sweep?card=futures")
        assert resp.status_code == 200
        argv = fake_spawner["argv"]
        assert argv[argv.index("--symbols") + 1] == "NSE-NIFTY-FUT"
        assert argv[argv.index("--segment") + 1] == "FNO"

    def test_exited_run_self_heals_and_relaunches(
        self, client: TestClient, lake: Path, fake_spawner: dict[str, Any]
    ) -> None:
        lb._write_launch_state(lake, {"pid": -9, "card": "spot"})
        resp = client.post("/api/v1/launch/sweep?card=spot")
        assert resp.status_code == 200
        assert resp.json()["active"]["pid"] == 34567

    def test_dashboard_restart_does_not_lose_the_run(
        self, lake: Path, fake_spawner: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The pidfile is durable — a NEW app instance still sees the run."""
        store = SimpleNamespace(path=lake / "instruments.duckdb")
        with TestClient(build_app(DashboardState(), instrument_store=store)) as first_app:
            assert first_app.post("/api/v1/launch/sweep?card=spot").status_code == 200
        monkeypatch.setattr(lb, "probe_pid", lambda pid: "running" if pid == 34567 else "exited")
        with TestClient(build_app(DashboardState(), instrument_store=store)) as second_app:
            state = second_app.get("/api/v1/launch/state").json()
            assert state["active"]["running"] is True
            assert state["active"]["pid"] == 34567
            # and the single-flight still refuses a duplicate
            assert second_app.post("/api/v1/launch/sweep?card=spot").status_code == 409


class TestRealPidLifecycle:
    def test_status_running_then_exited_with_log_tail(
        self, client: TestClient, lake: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end with a REAL (harmless) detached process — never a sweep."""
        import subprocess
        import sys
        import time

        def _spawn(argv: list[str], log_path: Path) -> int:
            # Ignore the sweep argv; spawn a short harmless process instead.
            with log_path.open("ab") as log_fh:
                proc = subprocess.Popen(
                    [sys.executable, "-c", "print('sweep-stand-in', flush=True)"],
                    stdout=log_fh,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            pid = proc.pid
            # Disown (mirrors _default_spawner): probe_pid's waitpid reaps.
            proc.returncode = 0
            return pid

        monkeypatch.setattr(lb, "_default_spawner", _spawn)
        resp = client.post("/api/v1/launch/sweep?card=spot")
        assert resp.status_code == 200
        pid = resp.json()["active"]["pid"]
        assert pid > 0
        deadline = time.monotonic() + 15.0
        state: dict[str, Any] = {}
        while time.monotonic() < deadline:
            state = client.get("/api/v1/launch/state").json()["active"]
            if state["running"] is False and "sweep-stand-in" in (state.get("log_tail") or ""):
                break
            time.sleep(0.05)
        assert state["running"] is False
        assert "sweep-stand-in" in state["log_tail"]
        # a fresh launch is now allowed again (stale-pid self-heal)
        second = client.post("/api/v1/launch/sweep?card=spot")
        assert second.status_code == 200
        second_pid = second.json()["active"]["pid"]
        assert second_pid != pid or not os.path.exists(f"/proc/{pid}")
