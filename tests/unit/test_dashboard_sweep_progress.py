"""Tests for the dashboard live sweep-progress panel route (DEC-SWEEP-WIRE-GA-001).

Covers :meth:`DashboardState.update_sweep_progress`,
:meth:`DashboardState.sweep_progress_view` (all three resolution branches: file
bridge, in-process push, idle), and the ``GET /api/v1/sweep-progress`` route.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from brutex.dashboard.app import DashboardState
from brutex.engine.sweep_progress import SweepProgress


def _running_snapshot() -> dict[str, object]:
    """A realistic running progress snapshot dict (as the supervisor publishes)."""
    progress = SweepProgress()
    progress.start(enumerated=100)
    progress.advance(current="NIFTY/1minute/bull")
    return progress.snapshot().to_dict()


class TestSweepProgressState:
    def test_idle_when_nothing_set(self) -> None:
        state = DashboardState()
        view = state.sweep_progress_view()
        assert view["state"] == "idle"
        assert view["running"] is False

    def test_in_process_push_is_served(self) -> None:
        state = DashboardState()
        snap = _running_snapshot()
        state.update_sweep_progress(snap)
        view = state.sweep_progress_view()
        assert view["state"] == "running"
        assert view["enumerated"] == 100
        assert view["evaluated"] == 1

    def test_file_bridge_takes_precedence(self, tmp_path: Path) -> None:
        state = DashboardState()
        # An in-process push that should be OVERRIDDEN by the fresher file.
        state.update_sweep_progress(_running_snapshot())
        progress_file = tmp_path / "sweep-progress.json"
        on_disk = _running_snapshot()
        on_disk["enumerated"] = 999
        # A FRESH wall_time stamp, as publish_progress writes it — a running
        # claim without one is DEAD EVIDENCE and would (correctly) be ignored.
        on_disk["wall_time"] = time.time()
        progress_file.write_text(json.dumps(on_disk), encoding="utf-8")
        state.sweep_progress_path = progress_file
        view = state.sweep_progress_view()
        assert view["enumerated"] == 999  # file wins over the in-process push

    def test_stale_running_file_decays_to_in_process(self, tmp_path: Path) -> None:
        """A SIGKILLed writer's frozen running=True file is dead evidence (review fold).

        Fails-before: ``sweep_progress_view`` preferred the file whenever it
        existed with NO freshness bound, so a killed ``brutex sweep-all`` left a
        phantom "running" view (+ Stop button) on the dashboard FOREVER.
        """
        from brutex.engine.sweep_supervisor import SWEEP_PROGRESS_MAX_AGE_S

        state = DashboardState()
        state.update_sweep_progress(_running_snapshot())
        progress_file = tmp_path / "sweep-progress.json"
        on_disk = _running_snapshot()
        on_disk["enumerated"] = 999
        on_disk["wall_time"] = time.time() - SWEEP_PROGRESS_MAX_AGE_S - 60.0
        progress_file.write_text(json.dumps(on_disk), encoding="utf-8")
        state.sweep_progress_path = progress_file
        view = state.sweep_progress_view()
        assert view["enumerated"] == 100  # the stale file is ≡ absent

    def test_stale_running_file_and_no_push_is_idle(self, tmp_path: Path) -> None:
        from brutex.engine.sweep_supervisor import SWEEP_PROGRESS_MAX_AGE_S

        state = DashboardState()
        progress_file = tmp_path / "sweep-progress.json"
        on_disk = _running_snapshot()
        on_disk["wall_time"] = time.time() - SWEEP_PROGRESS_MAX_AGE_S - 60.0
        progress_file.write_text(json.dumps(on_disk), encoding="utf-8")
        state.sweep_progress_path = progress_file
        view = state.sweep_progress_view()
        assert view["state"] == "idle"
        assert view["running"] is False

    def test_running_file_without_wall_time_is_dead_evidence(self, tmp_path: Path) -> None:
        """A pre-stamp / junk running file decays too (the #1593 non-finite posture)."""
        state = DashboardState()
        progress_file = tmp_path / "sweep-progress.json"
        progress_file.write_text(json.dumps(_running_snapshot()), encoding="utf-8")
        state.sweep_progress_path = progress_file
        assert state.sweep_progress_view()["state"] == "idle"

    def test_stale_terminal_file_never_decays(self, tmp_path: Path) -> None:
        """A finished sweep's done/cancelled record stands regardless of age."""
        from brutex.engine.sweep_supervisor import SWEEP_PROGRESS_MAX_AGE_S

        state = DashboardState()
        progress = SweepProgress()
        progress.start(enumerated=3)
        progress.advance(current="NIFTY/1minute")
        progress.finish(detail="done")
        on_disk = progress.snapshot().to_dict()
        on_disk["wall_time"] = time.time() - SWEEP_PROGRESS_MAX_AGE_S - 3600.0
        progress_file = tmp_path / "sweep-progress.json"
        progress_file.write_text(json.dumps(on_disk), encoding="utf-8")
        state.sweep_progress_path = progress_file
        view = state.sweep_progress_view()
        assert view["state"] == "done"
        assert view["running"] is False

    def test_missing_file_falls_back_to_in_process(self, tmp_path: Path) -> None:
        state = DashboardState()
        state.update_sweep_progress(_running_snapshot())
        state.sweep_progress_path = tmp_path / "absent.json"  # never written
        view = state.sweep_progress_view()
        assert view["state"] == "running"
        assert view["enumerated"] == 100

    def test_missing_file_and_no_push_is_idle(self, tmp_path: Path) -> None:
        state = DashboardState()
        state.sweep_progress_path = tmp_path / "absent.json"
        assert state.sweep_progress_view()["state"] == "idle"


class TestSweepProgressRoute:
    @pytest.fixture
    def client(self) -> Iterator[object]:
        pytest.importorskip("fastapi")
        from brutex.dashboard.app import build_app
        from fastapi.testclient import TestClient

        state = DashboardState()
        state.update_sweep_progress(_running_snapshot())
        with TestClient(build_app(state)) as c:
            yield c

    def test_route_returns_progress(self, client: object) -> None:
        resp = client.get("/api/v1/sweep-progress")  # type: ignore[attr-defined]
        assert resp.status_code == 200
        body = resp.json()
        assert body["state"] == "running"
        assert body["enumerated"] == 100

    def test_route_idle_by_default(self) -> None:
        pytest.importorskip("fastapi")
        from brutex.dashboard.app import build_app
        from fastapi.testclient import TestClient

        with TestClient(build_app(DashboardState())) as client:
            resp = client.get("/api/v1/sweep-progress")
            assert resp.status_code == 200
            assert resp.json()["state"] == "idle"


class TestSweepProgressViewFileReadSeam:
    """The ``file_read`` seam (DEC-SWEEP-PROGRESS-NONBLOCK-CACHE-001).

    The route injects :meth:`SweepProgressCache.read` here so the file arm is
    memoised + deadline-bounded; these pins fix the seam's contract — a dict
    is served as the file arm, ``None`` falls through to the in-process /
    idle arms exactly like the direct ``exists()`` check.
    """

    def test_file_read_payload_is_served(self, tmp_path: Path) -> None:
        state = DashboardState()
        state.update_sweep_progress(_running_snapshot())
        state.sweep_progress_path = tmp_path / "sweep-progress.json"
        view = state.sweep_progress_view(file_read=lambda path: {"state": "from-cache"})
        assert view == {"state": "from-cache"}

    def test_file_read_none_falls_back_to_in_process(self, tmp_path: Path) -> None:
        state = DashboardState()
        state.update_sweep_progress(_running_snapshot())
        state.sweep_progress_path = tmp_path / "sweep-progress.json"
        view = state.sweep_progress_view(file_read=lambda path: None)
        assert view["state"] == "running"
        assert view["enumerated"] == 100

    def test_file_read_none_and_no_push_is_idle(self, tmp_path: Path) -> None:
        state = DashboardState()
        state.sweep_progress_path = tmp_path / "sweep-progress.json"
        view = state.sweep_progress_view(file_read=lambda path: None)
        assert view["state"] == "idle"

    def test_no_path_never_calls_file_read(self) -> None:
        state = DashboardState()

        def _must_not_call(path: Path) -> dict[str, object]:
            raise AssertionError("file_read must not run without a configured path")

        assert state.sweep_progress_view(file_read=_must_not_call)["state"] == "idle"

    def test_file_read_stale_running_payload_decays(self, tmp_path: Path) -> None:
        """The DEC-JOB-COOPERATIVE-CANCEL-001 staleness law covers the cached arm too.

        A ``running=True`` payload served through the ``file_read`` seam whose
        ``wall_time`` stamp is older than ``SWEEP_PROGRESS_MAX_AGE_S`` is DEAD
        EVIDENCE ≡ absent — the view falls through to idle, exactly like the
        direct-read arm (merge fold: #1640 cache seam × the staleness law).
        """
        from brutex.engine.sweep_supervisor import SWEEP_PROGRESS_MAX_AGE_S

        state = DashboardState()
        state.sweep_progress_path = tmp_path / "sweep-progress.json"
        stale = _running_snapshot()
        stale["wall_time"] = time.time() - SWEEP_PROGRESS_MAX_AGE_S - 60.0
        view = state.sweep_progress_view(file_read=lambda path: stale)
        assert view["state"] == "idle"
        assert view["running"] is False

    def test_route_serves_cache_backed_file_bridge(self, tmp_path: Path) -> None:
        """End-to-end: the route's injected cache serves the file payload, stamped."""
        pytest.importorskip("fastapi")
        from brutex.dashboard.app import build_app
        from brutex.dashboard.sweep_progress_cache import SweepProgressCache
        from fastapi.testclient import TestClient

        state = DashboardState()
        progress_file = tmp_path / "sweep-progress.json"
        on_disk = _running_snapshot()
        on_disk["enumerated"] = 999
        # A FRESH wall_time stamp, as publish_progress writes it — a running
        # claim without one is DEAD EVIDENCE under progress_bridge_stale
        # (DEC-JOB-COOPERATIVE-CANCEL-001), on the cached arm too.
        on_disk["wall_time"] = time.time()
        progress_file.write_text(json.dumps(on_disk), encoding="utf-8")
        state.sweep_progress_path = progress_file
        app = build_app(state, sweep_progress_cache=SweepProgressCache(ttl_s=0.0))
        with TestClient(app) as client:
            resp = client.get("/api/v1/sweep-progress")
            assert resp.status_code == 200
            body = resp.json()
            assert body["enumerated"] == 999
            # The honest additive provenance keys (never a stale-as-fresh illusion).
            assert body["progress_source"] == "live"
            assert body["progress_probe_age_s"] == 0.0
