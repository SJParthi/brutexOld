"""The ``/api/v1/universe/pull-derivatives`` route (DEC-PULL-DERIVATIVES-001).

Covers the route glue — interval / contract-type resolution (+ the 400 arms), the
``symbols`` / ``symbol`` / ``kind`` selection forms (+ their 400 arms), and the
single-flight background-job start. The background ``_work`` body (which calls
``pull_all_derivatives`` on the job thread) is replaced by an offline fake — NEVER a
real network call. Mirrors ``test_dashboard_app_backfill.py``'s pull-all pattern: a
real DuckDB-backed store + a ``TestClient``, creds pinned dry-run. Skipped cleanly
when the ``[ui]`` extra (FastAPI) or duckdb is absent.
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

from brutex.config.credentials import ENV_CREDENTIALS_FILE
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
def client(
    store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[TestClient]:
    monkeypatch.setenv(ENV_CREDENTIALS_FILE, str(tmp_path / "credentials.json"))
    with TestClient(build_app(DashboardState(), instrument_store=store)) as c:
        yield c


def _force_dry_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_module, "groww_credentials_present", lambda: False)


def _fake_pull_all_derivatives(instruments: list[Instrument], **_kwargs: Any) -> object:
    """Offline stand-in for the bulk derivatives pull (no network, returns at once)."""
    return object()


def _wait_for_job(client: TestClient, *, timeout: float = 10.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snap = client.get("/api/v1/universe/pull-progress").json()
        assert isinstance(snap, dict)
        if snap["state"] in {"done", "error"}:
            return snap
        time.sleep(0.01)
    raise AssertionError("background derivatives job did not settle within the timeout")


# ── selection forms (happy paths) ────────────────────────────────────────────


def test_kind_form_starts_job_default_knobs(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force_dry_run(monkeypatch)
    monkeypatch.setattr(app_module, "pull_all_derivatives", _fake_pull_all_derivatives)
    resp = client.post("/api/v1/universe/pull-derivatives", params={"kind": "index"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["started"] is True
    assert body["kind"] == "index"
    assert body["interval"] == "1day"  # default
    assert body["contract_type"] == "both"  # default
    assert body["intervals"] == ["1day"]
    assert body["contract_types"] == ["option", "future"]
    assert body["dry_run"] is True
    assert _wait_for_job(client)["state"] == "done"


def test_symbol_form_with_both_intervals(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force_dry_run(monkeypatch)
    monkeypatch.setattr(app_module, "pull_all_derivatives", _fake_pull_all_derivatives)
    resp = client.post(
        "/api/v1/universe/pull-derivatives",
        params={"symbol": "NIFTY", "interval": "both", "contract_type": "option"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["symbol"] == "NIFTY"
    assert body["intervals"] == ["1day", "1minute"]  # 1day FIRST
    assert body["contract_types"] == ["option"]
    assert _wait_for_job(client)["state"] == "done"


def test_symbols_form_starts_job(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _force_dry_run(monkeypatch)
    monkeypatch.setattr(app_module, "pull_all_derivatives", _fake_pull_all_derivatives)
    resp = client.post(
        "/api/v1/universe/pull-derivatives",
        params={"symbols": "NIFTY", "interval": "1minute", "contract_type": "future"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["symbols"] == "NIFTY"
    assert body["intervals"] == ["1minute"]
    assert body["contract_types"] == ["future"]
    assert _wait_for_job(client)["state"] == "done"


def test_until_clean_flag_is_accepted_and_echoed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ?until_clean=true escalates to the derivatives until-clean round-loop
    # (DEC-GROWW-DERIV-UNTIL-CLEAN-001); the route accepts + echoes it.
    _force_dry_run(monkeypatch)
    monkeypatch.setattr(app_module, "pull_all_derivatives", _fake_pull_all_derivatives)
    resp = client.post(
        "/api/v1/universe/pull-derivatives",
        params={"kind": "index", "until_clean": "true"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["until_clean"] is True
    assert body["started"] is True
    assert _wait_for_job(client)["state"] == "done"


# ── 400 arms ─────────────────────────────────────────────────────────────────


def test_bad_interval_400(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _force_dry_run(monkeypatch)
    resp = client.post(
        "/api/v1/universe/pull-derivatives", params={"kind": "index", "interval": "5minute"}
    )
    assert resp.status_code == 400
    assert "interval must be one of" in resp.json()["detail"]


def test_bad_contract_type_400(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _force_dry_run(monkeypatch)
    resp = client.post(
        "/api/v1/universe/pull-derivatives", params={"kind": "index", "contract_type": "swap"}
    )
    assert resp.status_code == 400
    assert "contract_type must be one of" in resp.json()["detail"]


def test_unknown_symbol_400(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _force_dry_run(monkeypatch)
    resp = client.post("/api/v1/universe/pull-derivatives", params={"symbol": "NOPE"})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "unknown security_id 'NOPE' — not in the instrument store"


def test_unknown_symbols_400(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _force_dry_run(monkeypatch)
    resp = client.post("/api/v1/universe/pull-derivatives", params={"symbols": "NIFTY,NOPE"})
    assert resp.status_code == 400
    assert "NOPE" in resp.json()["detail"]


def test_bad_kind_400(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _force_dry_run(monkeypatch)
    resp = client.post("/api/v1/universe/pull-derivatives", params={"kind": "bogus"})
    assert resp.status_code == 400
    assert "kind must be one of" in resp.json()["detail"]


def test_no_selection_400(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _force_dry_run(monkeypatch)
    resp = client.post("/api/v1/universe/pull-derivatives")
    assert resp.status_code == 400
    assert resp.json()["detail"] == (
        "pull-derivatives requires ?symbols=..., ?symbol=... or ?kind=..."
    )


def test_universe_page_carries_derivatives_ui(client: TestClient) -> None:
    """The /universe page wires the derivatives button, bulk buttons, dropdowns + route."""
    resp = client.get("/universe")
    assert resp.status_code == 200
    assert "/api/v1/universe/pull-derivatives" in resp.text
    assert 'id="pullDerivBtn"' in resp.text
    assert 'id="bulkIdxDeriv"' in resp.text
    assert 'id="bulkStkDeriv"' in resp.text
    assert 'id="derivInterval"' in resp.text
    assert 'id="derivType"' in resp.text
    assert "Pull expiries + contracts" in resp.text
