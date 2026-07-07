"""The ``/api/v1/sweep/preflight`` + confirm-token-gated ``/api/v1/sweep/run`` routes.

DEC-SWEEP-PREFLIGHT-ESTIMATOR-001 (§3.3 dashboard sequence) — the route glue
over the 100%-tested :func:`~brutex.dashboard.sweep_runner.preflight_sweep_request`
core: the always-200 probe-only preflight (a refusal is DATA, never an HTTP
error — hunt G5), the mandatory two-step confirm gate on ``/sweep/run``
(409 ``preflight_refused`` / 409 ``confirm_token_stale`` with a FRESH payload
— no job registered on either), the ok+match happy start over the D8 launch
fan-out (launches run sequentially, worst exit code wins), and the
/reports-page confirm-JS wiring (LIVE in P1 — the #935 dormant-feature trap).

Injection seams (house style — mirrors ``test_dashboard_sweep_run_route.py``):
the merged preflight and the per-launch sweep body are monkeypatched at the
``app_module`` namespace with offline fakes; ONE real-gate arm drives the full
engine preflight against an empty tmp lake (probe-only pinned on disk).
Skipped cleanly when the ``[ui]`` extra (FastAPI) or duckdb is absent.
"""

from __future__ import annotations

import importlib
import time
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


_CAPTURED: dict[str, Any] = {}


def _fake_run_sweep_from_request(request: Any, *, lake_root: Path, results_root: Path) -> int:
    """Offline stand-in for one grid-sweep launch (no lake, returns at once)."""
    _CAPTURED.setdefault("runs", []).append(request)
    _CAPTURED["lake_root"] = lake_root
    _CAPTURED["results_root"] = results_root
    return 0


def _ok_gate_payload() -> dict[str, Any]:
    """A canned OK merged-preflight payload (the preflight_sweep_request shape)."""
    token = "tok-live-1"
    return {
        "schema_version": 1,
        "ok": True,
        "refusals": [],
        "refusal_text": "",
        "headline": "this selection = 42 trials, est. 0.1 h (PROVISIONAL) — proceed?",
        "estimate": {"trials": 42, "tuples": 2, "provisional": True},
        "warnings": [],
        "notes": ["note-a"],
        "launches": [],
        "confirm_token": token,
        "acknowledge_sla": False,
    }


def _refused_gate_payload() -> dict[str, Any]:
    """A canned REFUSED merged payload — coverage hole named, no token minted."""
    return {
        "schema_version": 1,
        "ok": False,
        "refusals": [{"code": "coverage_missing", "message": "NIFTY × 1minute missing"}],
        "refusal_text": (
            "launch NSE/INDEX · NIFTY,BANKNIFTY:\nsweep preflight REFUSED — pull on /universe first"
        ),
        "headline": "this selection = 42 trials — proceed?",
        "estimate": {"trials": 42, "tuples": 2, "provisional": True},
        "warnings": [],
        "notes": [],
        "launches": [],
        "confirm_token": None,
        "acknowledge_sla": False,
    }


def _fake_gate(payload: dict[str, Any]) -> Any:
    """Build a preflight_sweep_request stand-in capturing its call, returning *payload*."""

    def _gate(
        launches: Any, *, lake_root: Path, results_root: Path | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        _CAPTURED.setdefault("gates", []).append(
            {"launches": launches, "lake_root": lake_root, "results_root": results_root}
        )
        return payload

    return _gate


def _wait_for_job(client: TestClient, *, timeout: float = 10.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snap = client.get("/api/v1/universe/pull-progress").json()
        assert isinstance(snap, dict)
        if snap["state"] in {"done", "error"}:
            return snap
        time.sleep(0.01)
    raise AssertionError("background sweep job did not settle within the timeout")


# ── POST /api/v1/sweep/preflight ─────────────────────────────────────────────


def test_preflight_is_always_200_even_when_refused(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusal is DATA — the probe route returns 200 with the full payload."""
    _CAPTURED.clear()
    monkeypatch.setattr(app_module, "preflight_sweep_request", _fake_gate(_refused_gate_payload()))
    resp = client.post("/api/v1/sweep/preflight")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "pull on /universe first" in body["refusal_text"]
    assert body["confirm_token"] is None
    # The D8 fan-out reached the gate: blank symbols ⇒ 2 launches, NIFTY-first.
    gate_call = _CAPTURED["gates"][0]
    launches = gate_call["launches"]
    assert [(launch.exchange, launch.symbols) for launch in launches] == [
        ("NSE", "NIFTY,BANKNIFTY"),
        ("BSE", "SENSEX"),
    ]
    assert gate_call["results_root"] == gate_call["lake_root"] / "sweeps"


def test_preflight_bad_selection_is_a_400_and_never_reaches_the_gate(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _CAPTURED.clear()
    monkeypatch.setattr(app_module, "preflight_sweep_request", _fake_gate(_ok_gate_payload()))
    resp = client.post("/api/v1/sweep/preflight", params={"intervals": "7tick"})
    assert resp.status_code == 400
    assert "interval must be one of" in resp.json()["detail"]
    assert "gates" not in _CAPTURED


def test_preflight_no_lake_400() -> None:
    fake_store = SimpleNamespace(path=None)
    with TestClient(build_app(DashboardState(), instrument_store=fake_store)) as c:
        resp = c.post("/api/v1/sweep/preflight")
        assert resp.status_code == 400
        assert resp.json()["detail"] == "no lake configured for sweep"


def test_preflight_real_gate_refuses_empty_lake_probe_only(client: TestClient) -> None:
    """The REAL merged gate end-to-end: an empty lake refuses with the remedy,
    and the probe writes NOTHING (no sweeps/, no .locks/, no catalog.duckdb)."""
    resp = client.post(
        "/api/v1/sweep/preflight",
        params={"symbols": "NIFTY", "intervals": "1minute"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "pull on /universe first" in body["refusal_text"]
    assert body["confirm_token"] is None
    assert body["estimate"]["trials"] > 0  # the cost of what was refused is shown


def test_preflight_probe_writes_nothing_on_disk(store: InstrumentStore) -> None:
    with TestClient(build_app(DashboardState(), instrument_store=store)) as c:
        resp = c.post(
            "/api/v1/sweep/preflight", params={"symbols": "NIFTY", "intervals": "1minute"}
        )
        assert resp.status_code == 200
    lake = Path(store.path).parent
    assert not (lake / "sweeps").exists()
    assert not (lake / ".locks").exists()
    assert not (lake / "catalog.duckdb").exists()


# ── POST /api/v1/sweep/run — the mandatory confirm gate ─────────────────────


def test_run_refused_preflight_is_409_and_registers_no_job(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _CAPTURED.clear()
    monkeypatch.setattr(app_module, "preflight_sweep_request", _fake_gate(_refused_gate_payload()))
    monkeypatch.setattr(app_module, "run_sweep_from_request", _fake_run_sweep_from_request)
    resp = client.post("/api/v1/sweep/run", params={"confirm_token": "whatever"})
    assert resp.status_code == 409
    body = resp.json()
    assert body["reason"] == "preflight_refused"
    assert "pull on /universe first" in body["refusal_text"]
    assert "runs" not in _CAPTURED  # NO launch executed
    snap = client.get("/api/v1/universe/pull-progress").json()
    assert snap["state"] == "idle"  # NO job registered


def test_run_without_token_is_409_stale_with_the_fresh_token(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The confirm gate is MANDATORY: an ok verdict without a token still 409s."""
    _CAPTURED.clear()
    monkeypatch.setattr(app_module, "preflight_sweep_request", _fake_gate(_ok_gate_payload()))
    monkeypatch.setattr(app_module, "run_sweep_from_request", _fake_run_sweep_from_request)
    resp = client.post("/api/v1/sweep/run")
    assert resp.status_code == 409
    body = resp.json()
    assert body["reason"] == "confirm_token_stale"
    assert body["confirm_token"] == "tok-live-1"  # the FRESH payload incl. new token
    assert "runs" not in _CAPTURED


def test_run_with_mismatched_token_is_409_stale(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale token (selection edited / lake_revision moved) forces a re-render."""
    _CAPTURED.clear()
    monkeypatch.setattr(app_module, "preflight_sweep_request", _fake_gate(_ok_gate_payload()))
    monkeypatch.setattr(app_module, "run_sweep_from_request", _fake_run_sweep_from_request)
    resp = client.post("/api/v1/sweep/run", params={"confirm_token": "tok-STALE"})
    assert resp.status_code == 409
    assert resp.json()["reason"] == "confirm_token_stale"
    assert "runs" not in _CAPTURED


def test_run_ok_and_matching_token_starts_the_fanned_out_job(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ok + match ⇒ started; ALL launches run sequentially on the ONE job."""
    _CAPTURED.clear()
    monkeypatch.setattr(app_module, "preflight_sweep_request", _fake_gate(_ok_gate_payload()))
    monkeypatch.setattr(app_module, "run_sweep_from_request", _fake_run_sweep_from_request)
    resp = client.post("/api/v1/sweep/run", params={"confirm_token": "tok-live-1"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["started"] is True
    # The estimate + headline + token are echoed in the response.
    assert body["estimate"] == {"trials": 42, "tuples": 2, "provisional": True}
    assert "proceed?" in body["headline"]
    assert body["confirm_token"] == "tok-live-1"
    # The launch fan-out is echoed (blank symbols ⇒ 2 launches, NIFTY-first).
    assert [launch["symbols"] for launch in body["launches"]] == [
        "NIFTY,BANKNIFTY",
        "SENSEX",
    ]
    assert _wait_for_job(client)["state"] == "done"
    # BOTH launches executed sequentially through run_sweep_from_request.
    runs = _CAPTURED["runs"]
    assert [(request.exchange, request.symbols) for request in runs] == [
        ("NSE", "NIFTY,BANKNIFTY"),
        ("BSE", "SENSEX"),
    ]
    assert _CAPTURED["results_root"] == _CAPTURED["lake_root"] / "sweeps"


def test_run_bad_selection_still_400(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _CAPTURED.clear()
    monkeypatch.setattr(app_module, "preflight_sweep_request", _fake_gate(_ok_gate_payload()))
    resp = client.post("/api/v1/sweep/run", params={"symbols": "RELIANCE"})
    assert resp.status_code == 400
    assert "sweep REFUSED" in resp.json()["detail"]
    assert "gates" not in _CAPTURED  # refused before the gate


def test_run_acknowledge_sla_threads_into_the_launches(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SLA consent axis rides its OWN query param into every launch."""
    _CAPTURED.clear()
    monkeypatch.setattr(app_module, "preflight_sweep_request", _fake_gate(_ok_gate_payload()))
    monkeypatch.setattr(app_module, "run_sweep_from_request", _fake_run_sweep_from_request)
    resp = client.post(
        "/api/v1/sweep/run",
        params={"confirm_token": "tok-live-1", "acknowledge_sla": "true"},
    )
    assert resp.status_code == 200
    gate_launches = _CAPTURED["gates"][0]["launches"]
    assert all(launch.acknowledge_sla is True for launch in gate_launches)
    assert resp.json()["acknowledge_sla"] is True
    _wait_for_job(client)


def test_run_second_click_while_running_is_a_noop(
    store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While a sweep (or pull) holds the single-flight job, a second start no-ops.

    The DEC-SWEEP-RUN-BUTTON-001 idempotence pin, re-homed here from the
    pre-confirm-gate suite: both clicks pass the gate (ok + matching token) —
    the SECOND is refused by the job single-flight, not the gate.
    """
    started = __import__("threading").Event()
    release = __import__("threading").Event()

    def _blocking(request: Any, *, lake_root: Path, results_root: Path) -> int:
        started.set()
        release.wait(timeout=10.0)
        return 0

    app_mod = app_module
    monkeypatch.setattr(app_mod, "preflight_sweep_request", _fake_gate(_ok_gate_payload()))
    monkeypatch.setattr(app_mod, "run_sweep_from_request", _blocking)
    with TestClient(build_app(DashboardState(), instrument_store=store)) as c:
        first = c.post("/api/v1/sweep/run", params={"confirm_token": "tok-live-1"})
        assert first.status_code == 200
        assert first.json()["started"] is True
        assert started.wait(timeout=10.0)
        second = c.post("/api/v1/sweep/run", params={"confirm_token": "tok-live-1"})
        assert second.status_code == 200
        assert second.json()["started"] is False  # single-flight no-op
        release.set()


def test_run_points_progress_at_the_sweep_file(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a gated start, GET /api/v1/sweep-progress is served (idle until the file lands)."""
    _CAPTURED.clear()
    monkeypatch.setattr(app_module, "preflight_sweep_request", _fake_gate(_ok_gate_payload()))
    monkeypatch.setattr(app_module, "run_sweep_from_request", _fake_run_sweep_from_request)
    assert (
        client.post("/api/v1/sweep/run", params={"confirm_token": "tok-live-1"}).status_code == 200
    )
    _wait_for_job(client)
    prog = client.get("/api/v1/sweep-progress").json()
    assert isinstance(prog, dict)
    assert "state" in prog  # the offline fake writes no file ⇒ idle snapshot


# ── /reports page wiring — the confirm JS is LIVE (the #935 trap) ────────────


def test_reports_page_carries_the_two_step_confirm_js(client: TestClient) -> None:
    resp = client.get("/reports")
    assert resp.status_code == 200
    assert "/api/v1/sweep/preflight" in resp.text
    assert "/api/v1/sweep/run" in resp.text
    assert "confirm_token" in resp.text
    assert "window.confirm" in resp.text
    assert "refusal_text" in resp.text
    assert 'id="run-sweep"' in resp.text
    # The repo XSS law: server refusal text lands via textContent, never innerHTML.
    assert "sweepMsg.textContent = pj.refusal_text" in resp.text
