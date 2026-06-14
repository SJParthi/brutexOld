"""The ``/api/v1/universe/pull-expiries-contracts`` route (DEC-DERIV-METADATA-ROUTE-001).

PR-3 of the metadata/historical split: the "⛓ Pull expiries + contracts" button now
calls THIS metadata-only route (persist expiries + contracts, ZERO candles). The fused
candle-pulling ``/pull-derivatives`` route it replaced was DECOMMISSIONED in PR-6
(DEC-DERIV-DECOMMISSION-FUSED-001) — the bulk derivative buttons now POST here too.
This covers the route glue — the year-grid resolution (+ its 400 arms), the
``symbols`` / ``symbol`` / ``kind`` selection forms (+ their 400 arms), and the
single-flight background-job start. The background ``_work`` body (which calls
``pull_expiries_contracts`` on the job thread) is replaced by an OFFLINE fake — NEVER a
real network call. A real DuckDB-backed store + a ``TestClient``, creds pinned dry-run.
Skipped cleanly when the ``[ui]`` extra (FastAPI) or duckdb is absent.
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
from brutex.dashboard.metadata_pull import MetadataPullReport
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


def _fake_report(instruments: list[Instrument], *, years: Any, dry_run: bool) -> MetadataPullReport:
    """Honest-shaped offline report echoing the selection (NO network, no candles)."""
    return MetadataPullReport(
        dry_run=dry_run,
        years=tuple(years),
        underlyings=tuple(inst.symbol_name for inst in instruments),
        expiry_cells=0,
        cells_skipped=0,
        expiry_failures=0,
        expiries_persisted=0,
        contract_jobs=0,
        contract_failures=0,
        contracts_persisted=0,
        note="offline fake",
    )


def _fake_pull_expiries_contracts(
    instruments: list[Instrument], **kwargs: Any
) -> MetadataPullReport:
    """Offline stand-in for the metadata pull (no network, returns at once)."""
    return _fake_report(instruments, years=kwargs["years"], dry_run=kwargs["dry_run"])


def _patch_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wire the offline metadata fake so the route never makes a real network call.

    The fused candle-pulling ``pull_all_derivatives`` was decommissioned (PR-6,
    DEC-DERIV-DECOMMISSION-FUSED-001) and is no longer importable here, so there is
    nothing left to fence off — the metadata module structurally has no candle sink.
    """
    monkeypatch.setattr(app_module, "pull_expiries_contracts", _fake_pull_expiries_contracts)


def _wait_for_job(client: TestClient, *, timeout: float = 10.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snap = client.get("/api/v1/universe/pull-progress").json()
        assert isinstance(snap, dict)
        if snap["state"] in {"done", "error"}:
            return snap
        time.sleep(0.01)
    raise AssertionError("background metadata job did not settle within the timeout")


# ── selection forms (happy paths) ────────────────────────────────────────────


def test_kind_form_starts_metadata_job(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _force_dry_run(monkeypatch)
    _patch_metadata(monkeypatch)
    resp = client.post("/api/v1/universe/pull-expiries-contracts", params={"kind": "index"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["started"] is True
    assert body["kind"] == "index"
    assert body["dry_run"] is True
    # full 2020 → current-year grid when no date bounds are supplied
    assert body["years"][0] == 2020
    assert _wait_for_job(client)["state"] == "done"


def test_symbol_form_starts_metadata_job(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force_dry_run(monkeypatch)
    _patch_metadata(monkeypatch)
    resp = client.post("/api/v1/universe/pull-expiries-contracts", params={"symbol": "NIFTY"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["symbol"] == "NIFTY"
    assert _wait_for_job(client)["state"] == "done"


def test_symbols_form_echoes_selection(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _force_dry_run(monkeypatch)
    _patch_metadata(monkeypatch)
    resp = client.post("/api/v1/universe/pull-expiries-contracts", params={"symbols": "NIFTY"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["symbols"] == "NIFTY"
    assert _wait_for_job(client)["state"] == "done"


def test_date_bounds_narrow_the_year_grid(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force_dry_run(monkeypatch)
    _patch_metadata(monkeypatch)
    resp = client.post(
        "/api/v1/universe/pull-expiries-contracts",
        params={"kind": "index", "from_date": "2022-03-01", "to_date": "2023-09-30"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["years"] == [2022, 2023]
    assert _wait_for_job(client)["state"] == "done"


# ── 400 arms ──────────────────────────────────────────────────────────────────


def test_unknown_symbol_400(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _force_dry_run(monkeypatch)
    resp = client.post("/api/v1/universe/pull-expiries-contracts", params={"symbol": "NOPE"})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "unknown security_id 'NOPE' — not in the instrument store"


def test_unknown_symbols_400(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _force_dry_run(monkeypatch)
    resp = client.post("/api/v1/universe/pull-expiries-contracts", params={"symbols": "NIFTY,NOPE"})
    assert resp.status_code == 400
    assert "NOPE" in resp.json()["detail"]


def test_bad_kind_400(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _force_dry_run(monkeypatch)
    resp = client.post("/api/v1/universe/pull-expiries-contracts", params={"kind": "bogus"})
    assert resp.status_code == 400
    assert "kind must be one of" in resp.json()["detail"]


def test_no_selection_400(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _force_dry_run(monkeypatch)
    resp = client.post("/api/v1/universe/pull-expiries-contracts")
    assert resp.status_code == 400
    assert resp.json()["detail"] == (
        "pull-expiries-contracts requires ?symbols=..., ?symbol=... or ?kind=..."
    )


def test_bad_from_date_400(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _force_dry_run(monkeypatch)
    resp = client.post(
        "/api/v1/universe/pull-expiries-contracts",
        params={"kind": "index", "from_date": "not-a-date"},
    )
    assert resp.status_code == 400
    assert "from_date must be an ISO date" in resp.json()["detail"]


def test_reversed_year_range_400(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _force_dry_run(monkeypatch)
    resp = client.post(
        "/api/v1/universe/pull-expiries-contracts",
        params={"kind": "index", "from_date": "2024-01-01", "to_date": "2022-01-01"},
    )
    assert resp.status_code == 400
    assert "after to_date year" in resp.json()["detail"]


# ── the button is repointed ──────────────────────────────────────────────────


def test_universe_page_button_targets_metadata_route(client: TestClient) -> None:
    """The /universe ⛓ button now POSTs the metadata-only route (no candles)."""
    resp = client.get("/universe")
    assert resp.status_code == 200
    assert "/api/v1/universe/pull-expiries-contracts" in resp.text
    assert 'id="pullDerivBtn"' in resp.text
    # the metadata-only tooltip wording is present
    assert "no history" in resp.text


def test_universe_page_no_longer_references_fused_candle_route(client: TestClient) -> None:
    """PR-6 (DEC-DERIV-DECOMMISSION-FUSED-001): no /universe surface still hits the
    decommissioned fused candle path — the bulk derivative buttons POST the
    metadata-only route, never ``/api/v1/universe/pull-derivatives``."""
    resp = client.get("/universe")
    assert resp.status_code == 200
    assert "/api/v1/universe/pull-derivatives" not in resp.text
    # the bulk derivative buttons survive (repointed, not removed)
    assert 'id="bulkIdxDeriv"' in resp.text
    assert 'id="bulkStkDeriv"' in resp.text
