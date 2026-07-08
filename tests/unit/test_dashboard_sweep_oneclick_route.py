"""The ONE-CLICK institutional sweep routes (R4/R20/R5, gap-hunt rulings 1/2/9).

``GET /api/v1/sweep/oneclick/preflight`` (estimate-only, never launches) and
``POST /api/v1/sweep/oneclick/run`` (preflight-gated launch, multi-exchange
fan-out). Mirrors ``test_dashboard_sweep_run_route.py``'s fixtures + offline
``run_sweep_from_request`` fake — never a real lake/sweep.
"""

from __future__ import annotations

import importlib
import time
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi", reason="dashboard routes need the [ui] extra")
pytest.importorskip("duckdb", reason="the wired-store routes need duckdb")

from brutex.dashboard.app import DashboardState, build_app
from brutex.data.instrument_store import InstrumentStore
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.testclient import TestClient

app_module = importlib.import_module("brutex.dashboard.app")


def _index_instrument(symbol: str, exch: Exchange) -> Instrument:
    return Instrument(
        exch_id=exch,
        security_id=13,
        symbol_name=symbol,
        display_name=symbol,
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
        [_index_instrument("NIFTY", Exchange.NSE)],
        snapshot_date=date(2024, 1, 2),
        run_id="r1",
        csv_source_sha256="0" * 64,
    )
    yield instrument_store
    instrument_store.close()


@pytest.fixture
def client(store: InstrumentStore) -> Iterator[TestClient]:
    with TestClient(build_app(DashboardState(), instrument_store=store)) as c:
        yield c


_CAPTURED: dict[str, Any] = {}


def _fake_run_sweep_from_request(request: Any, *, lake_root: Path, results_root: Path) -> int:
    _CAPTURED.setdefault("requests", []).append(request)
    return 0


def _wait_for_job(client: TestClient, *, timeout: float = 10.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snap = client.get("/api/v1/universe/pull-progress").json()
        assert isinstance(snap, dict)
        if snap["state"] in {"done", "error"}:
            return snap
        time.sleep(0.01)
    raise AssertionError("background sweep job did not settle within the timeout")


# ── GET /api/v1/sweep/oneclick/preflight ─────────────────────────────────────


def test_preflight_route_never_launches(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "run_sweep_from_request", _fake_run_sweep_from_request)
    _CAPTURED.clear()
    resp = client.get("/api/v1/sweep/oneclick/preflight")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"sweep_run_id", "ok", "refusals", "clamps", "estimator"}
    assert "requests" not in _CAPTURED  # nothing launched


def test_preflight_route_reports_estimator(client: TestClient) -> None:
    resp = client.get("/api/v1/sweep/oneclick/preflight", params={"symbols": "NIFTY"})
    assert resp.status_code == 200
    estimator = resp.json()["estimator"]
    assert estimator["trials"] > 0
    assert estimator["estimate_basis"] == "provisional"


def test_preflight_route_bad_interval_400(client: TestClient) -> None:
    resp = client.get("/api/v1/sweep/oneclick/preflight", params={"intervals": "7tick"})
    assert resp.status_code == 400
    assert "interval must be one of" in resp.json()["detail"]


def test_preflight_route_out_of_scope_symbol_400(client: TestClient) -> None:
    resp = client.get("/api/v1/sweep/oneclick/preflight", params={"symbols": "RELIANCE"})
    assert resp.status_code == 400
    assert "sweep REFUSED" in resp.json()["detail"]


def test_preflight_route_no_lake_configured_400() -> None:
    from types import SimpleNamespace

    fake_store = SimpleNamespace(path=None)
    with TestClient(build_app(DashboardState(), instrument_store=fake_store)) as c:
        resp = c.get("/api/v1/sweep/oneclick/preflight")
        assert resp.status_code == 400
        assert resp.json()["detail"] == "no lake configured for sweep"


def test_preflight_route_multi_exchange_selection_reports_both_groups(client: TestClient) -> None:
    resp = client.get("/api/v1/sweep/oneclick/preflight", params={"symbols": "NIFTY,SENSEX"})
    assert resp.status_code == 200
    body = resp.json()
    # Both NSE (NIFTY) and BSE (SENSEX) coverage gaps are named — one logical verdict.
    symbols_named = {r["symbol"] for r in body["refusals"]}
    assert "NIFTY" in symbols_named
    assert "SENSEX" in symbols_named


# ── POST /api/v1/sweep/oneclick/run ──────────────────────────────────────────


def test_run_route_refuses_without_launching_on_empty_lake(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "run_sweep_from_request", _fake_run_sweep_from_request)
    _CAPTURED.clear()
    resp = client.post("/api/v1/sweep/oneclick/run", params={"symbols": "NIFTY"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["started"] is False
    assert body["ok"] is False
    assert "requests" not in _CAPTURED


def test_run_route_bad_field_400(client: TestClient) -> None:
    resp = client.post("/api/v1/sweep/oneclick/run", params={"symbols": "RELIANCE"})
    assert resp.status_code == 400
    assert "sweep REFUSED" in resp.json()["detail"]


def test_run_route_no_lake_configured_400() -> None:
    from types import SimpleNamespace

    fake_store = SimpleNamespace(path=None)
    with TestClient(build_app(DashboardState(), instrument_store=fake_store)) as c:
        resp = c.post("/api/v1/sweep/oneclick/run")
        assert resp.status_code == 400
        assert resp.json()["detail"] == "no lake configured for sweep"


def test_run_route_launches_every_group_when_preflight_ok(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Monkeypatch the preflight itself to force ``ok=True`` so the launch path runs."""
    _CAPTURED.clear()
    monkeypatch.setattr(app_module, "run_sweep_from_request", _fake_run_sweep_from_request)
    monkeypatch.setattr(
        app_module,
        "oneclick_preflight_for_plan",
        lambda plan, **kw: {
            "sweep_run_id": plan.sweep_run_id,
            "ok": True,
            "refusals": [],
            "clamps": [],
            "estimator": {
                "trials": 1,
                "est_seconds_provisional": 0.1,
                "est_bytes": 1,
                "estimate_basis": "provisional",
            },
        },
    )
    resp = client.post("/api/v1/sweep/oneclick/run", params={"symbols": "NIFTY,SENSEX"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["started"] is True
    assert body["ok"] is True
    assert _wait_for_job(client)["state"] == "done"
    assert len(_CAPTURED["requests"]) == 2  # NSE (NIFTY) + BSE (SENSEX), sequential
    assert {r.exchange for r in _CAPTURED["requests"]} == {"NSE", "BSE"}


def test_run_route_second_click_while_running_is_a_noop(
    store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    started_evt = threading.Event()
    release_evt = threading.Event()

    def _blocking(request: Any, *, lake_root: Path, results_root: Path) -> int:
        started_evt.set()
        release_evt.wait(timeout=10.0)
        return 0

    monkeypatch.setattr(app_module, "run_sweep_from_request", _blocking)
    monkeypatch.setattr(
        app_module,
        "oneclick_preflight_for_plan",
        lambda plan, **kw: {
            "sweep_run_id": plan.sweep_run_id,
            "ok": True,
            "refusals": [],
            "clamps": [],
            "estimator": {
                "trials": 1,
                "est_seconds_provisional": 0.1,
                "est_bytes": 1,
                "estimate_basis": "provisional",
            },
        },
    )
    with TestClient(build_app(DashboardState(), instrument_store=store)) as c:
        first = c.post("/api/v1/sweep/oneclick/run", params={"symbols": "NIFTY"})
        assert first.status_code == 200
        assert first.json()["started"] is True
        assert started_evt.wait(timeout=10.0)
        second = c.post("/api/v1/sweep/oneclick/run", params={"symbols": "NIFTY"})
        assert second.status_code == 200
        assert second.json()["started"] is False
        release_evt.set()


# ── legacy /api/v1/sweep/run still works, now with acknowledge_sla_overrun ──


def test_legacy_route_accepts_acknowledge_sla_overrun(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "run_sweep_from_request", _fake_run_sweep_from_request)
    resp = client.post("/api/v1/sweep/run", params={"acknowledge_sla_overrun": "true"})
    assert resp.status_code == 200
    assert resp.json()["started"] is True
