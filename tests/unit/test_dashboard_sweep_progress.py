"""Tests for the dashboard live sweep-progress panel route (DEC-SWEEP-WIRE-GA-001).

Covers :meth:`DashboardState.update_sweep_progress`,
:meth:`DashboardState.sweep_progress_view` (all three resolution branches: file
bridge, in-process push, idle), and the ``GET /api/v1/sweep-progress`` route.
"""

from __future__ import annotations

import json
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
        progress_file.write_text(json.dumps(on_disk), encoding="utf-8")
        state.sweep_progress_path = progress_file
        view = state.sweep_progress_view()
        assert view["enumerated"] == 999  # file wins over the in-process push

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
