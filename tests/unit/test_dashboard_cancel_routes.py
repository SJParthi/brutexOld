"""The cooperative-cancel routes (DEC-JOB-COOPERATIVE-CANCEL-001).

Covers the two POST cancel routes + the additive ``cancelling`` pull-progress
key: ``POST /api/v1/pull/cancel`` (kind-gated: only a running PULL job) and
``POST /api/v1/sweep/cancel`` (in-process sweep, detached launch-page sweep,
nothing running, and the no-lake guard). The running-job state is simulated by
monkeypatching the ``BackgroundPullJob`` kind/cancel seams + the sweep-cancel
sidecar writer — NEVER a real lake/sweep/pull. Skipped cleanly when the ``[ui]``
extra (FastAPI) or duckdb is absent.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("fastapi", reason="dashboard routes need the [ui] extra")
pytest.importorskip("duckdb", reason="the wired-store routes need duckdb")

from brutex.dashboard.app import DashboardState, build_app
from brutex.dashboard.pull_jobs import (
    JOB_KIND_PULL,
    JOB_KIND_SWEEP,
    BackgroundPullJob,
)
from brutex.data.instrument_store import InstrumentStore
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.testclient import TestClient

app_module = importlib.import_module("brutex.dashboard.app")


def _nifty() -> Instrument:
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


@pytest.fixture
def store(tmp_path: Path) -> Iterator[InstrumentStore]:
    instrument_store = InstrumentStore.open(tmp_path / "instruments.duckdb")
    instrument_store.upsert_many(
        [_nifty()], snapshot_date=date(2024, 1, 2), run_id="r1", csv_source_sha256="0" * 64
    )
    yield instrument_store
    instrument_store.close()


@pytest.fixture
def client(store: InstrumentStore) -> Iterator[TestClient]:
    with TestClient(build_app(DashboardState(), instrument_store=store)) as c:
        yield c


def _force_running(monkeypatch: pytest.MonkeyPatch, *, kind: str, cancelled: bool = True) -> None:
    """Make every BackgroundPullJob report a live run of *kind*.

    The cancel routes read ``_pull_job.running_job_kind`` + call
    ``_pull_job.request_cancel()`` / ``.cancel_requested()``; the app owns the
    job internally, so the state is simulated at the class level.
    """
    monkeypatch.setattr(BackgroundPullJob, "running_job_kind", property(lambda _self: kind))
    monkeypatch.setattr(BackgroundPullJob, "request_cancel", lambda _self: True)
    monkeypatch.setattr(BackgroundPullJob, "cancel_requested", lambda _self: cancelled)


# ── the additive pull-progress cancelling key ─────────────────────────────────


def test_pull_progress_carries_cancelling_key_idle(client: TestClient) -> None:
    snap = client.get("/api/v1/universe/pull-progress").json()
    assert snap["cancelling"] is False


def test_pull_progress_cancelling_true_when_token_set(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(BackgroundPullJob, "cancel_requested", lambda _self: True)
    snap = client.get("/api/v1/universe/pull-progress").json()
    assert snap["cancelling"] is True


# ── POST /api/v1/pull/cancel ──────────────────────────────────────────────────


def test_pull_cancel_idle_is_false_noop(client: TestClient) -> None:
    resp = client.post("/api/v1/pull/cancel")
    assert resp.status_code == 200
    assert resp.json() == {"cancelling": False}


def test_pull_cancel_running_pull_job(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _force_running(monkeypatch, kind=JOB_KIND_PULL)
    resp = client.post("/api/v1/pull/cancel")
    assert resp.status_code == 200
    assert resp.json() == {"cancelling": True}


def test_pull_cancel_ignores_a_running_sweep(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A SWEEP-kind job is never cancelled through the pull route (kind-gated)."""
    _force_running(monkeypatch, kind=JOB_KIND_SWEEP)
    resp = client.post("/api/v1/pull/cancel")
    assert resp.status_code == 200
    assert resp.json() == {"cancelling": False}


# ── POST /api/v1/sweep/cancel ─────────────────────────────────────────────────


def test_sweep_cancel_in_process(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _force_running(monkeypatch, kind=JOB_KIND_SWEEP)
    written: dict[str, Any] = {}
    monkeypatch.setattr(
        "brutex.engine.sweep_cancel.request_cancel",
        lambda root, *, source: written.update(root=root, source=source) or root,
    )
    resp = client.post("/api/v1/sweep/cancel")
    assert resp.status_code == 200
    assert resp.json() == {"cancelling": True, "target": "in-process"}
    assert written["source"] == "route"


def test_sweep_cancel_detached_running(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """No in-process sweep, but a DETACHED launch-page sweep is live → target detached."""
    # The job is idle (no monkeypatch of running_job_kind → real None).
    monkeypatch.setattr(
        "brutex.dashboard.launch_button.read_launch_state", lambda _lake: {"pid": 4242}
    )
    monkeypatch.setattr("brutex.dashboard.launch_button.state_liveness", lambda _state: "running")
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        "brutex.engine.sweep_cancel.request_cancel",
        lambda root, *, source: captured.update(source=source) or root,
    )
    resp = client.post("/api/v1/sweep/cancel")
    assert resp.status_code == 200
    assert resp.json() == {"cancelling": True, "target": "detached"}
    assert captured["source"] == "route-detached"


def test_sweep_cancel_nothing_running(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing running (no in-process sweep, no live detached state) → cancelling False."""
    monkeypatch.setattr("brutex.dashboard.launch_button.read_launch_state", lambda _lake: None)
    resp = client.post("/api/v1/sweep/cancel")
    assert resp.status_code == 200
    assert resp.json() == {"cancelling": False}


def test_sweep_cancel_no_lake_configured() -> None:
    """A store whose ``path`` is not a real lake path → a clean 400, never a 500."""
    fake_store = SimpleNamespace(path="not-a-real-path", get=lambda _k: None)
    with TestClient(build_app(DashboardState(), instrument_store=fake_store)) as c:
        resp = c.post("/api/v1/sweep/cancel")
    assert resp.status_code == 400
    assert "no lake configured" in resp.json()["detail"]
