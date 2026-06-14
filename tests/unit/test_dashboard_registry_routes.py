"""Route tests for the F&O registry browser endpoints (DEC-DERIV-REGISTRY-DB-VIEW-001).

A real DuckDB-backed :class:`~brutex.data.instrument_store.InstrumentStore` wired into
``build_app`` + a :class:`~fastapi.testclient.TestClient` (mirrors
``test_dashboard_app_backfill``), so the thin ``/api/v1/db/registry/*`` wrappers are
checked end-to-end: a 200 + honest empty-grid ``note`` when no ``registry.duckdb``
exists yet, the structured chain shape once a sibling registry is seeded, and the
HTTP-400 on a malformed ``expiry_date``. Skipped cleanly when the ``[ui]`` extra is
absent.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="dashboard routes need the [ui] extra")
pytest.importorskip("duckdb", reason="the wired-store routes need duckdb")

from brutex.config.credentials import ENV_CREDENTIALS_FILE
from brutex.dashboard.app import DashboardState, build_app
from brutex.dashboard.db_view import REGISTRY_CONTRACT_COLUMNS, REGISTRY_EXPIRY_COLUMNS
from brutex.data.instrument_store import InstrumentStore
from brutex.data.registry_store import RegistryStore
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.testclient import TestClient

_NOW = datetime(2026, 6, 14, 9, 15, tzinfo=UTC)
_EXP = date(2020, 1, 30)


def _nifty() -> Instrument:
    """The admitted NIFTY index fixture (one row so the store opens cleanly)."""
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=13,
        symbol_name="NIFTY",
        display_name="NIFTY",
        instrument=InstrumentType.IDX,
        segment=Segment.CASH,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=1,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
    )


@pytest.fixture
def store(tmp_path: Path) -> Iterator[InstrumentStore]:
    """A real DuckDB store at ``<tmp>/instruments.duckdb`` (registry sibling lives here)."""
    instrument_store = InstrumentStore.open(tmp_path / "instruments.duckdb")
    instrument_store.upsert_many(
        [_nifty()],
        snapshot_date=date(2024, 1, 2),
        run_id="r1",
        csv_source_sha256="0" * 64,
    )
    yield instrument_store
    instrument_store.close()


@pytest.fixture
def client(
    store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[TestClient]:
    """A TestClient over the wired store; credentials redirected to an empty tmp file."""
    monkeypatch.setenv(ENV_CREDENTIALS_FILE, str(tmp_path / "credentials.json"))
    with TestClient(build_app(DashboardState(), instrument_store=store)) as c:
        yield c


def _seed_registry(tmp_path: Path) -> None:
    """Seed the sibling ``registry.duckdb`` with one NIFTY expiry + a CE+PE pair."""
    reg = RegistryStore.open(tmp_path / "registry.duckdb")
    try:
        reg.upsert_expiry("NSE", "NIFTY", _EXP, run_id="r1", fetched_at=_NOW)
        reg.upsert_contract(
            "NSE",
            "NIFTY",
            _EXP,
            "NSE-NIFTY-30Jan20-12100-CE",
            side="CE",
            strike=12100.0,
            run_id="r1",
            fetched_at=_NOW,
        )
        reg.upsert_contract(
            "NSE",
            "NIFTY",
            _EXP,
            "NSE-NIFTY-30Jan20-12100-PE",
            side="PE",
            strike=12100.0,
            run_id="r1",
            fetched_at=_NOW,
        )
    finally:
        reg.close()


def test_registry_expiries_empty_returns_note(client: TestClient) -> None:
    r = client.get("/api/v1/db/registry/expiries")
    assert r.status_code == 200
    body = r.json()
    assert body["columns"] == list(REGISTRY_EXPIRY_COLUMNS)
    assert body["rows"] == []
    assert body["total"] == 0
    assert body["note"] == "no registry yet — pull expiries + contracts first"
    assert body["counts"]["exists"] is False


def test_registry_contracts_empty_returns_note(client: TestClient) -> None:
    r = client.get("/api/v1/db/registry/contracts")
    assert r.status_code == 200
    body = r.json()
    assert body["columns"] == list(REGISTRY_CONTRACT_COLUMNS)
    assert body["rows"] == []
    assert body["total"] == 0
    assert body["note"] == "no registry yet — pull expiries + contracts first"


def test_registry_expiries_seeded_chain(client: TestClient, tmp_path: Path) -> None:
    _seed_registry(tmp_path)
    body = client.get("/api/v1/db/registry/expiries").json()
    assert body["total"] == 1
    assert body["note"] is None
    assert body["rows"][0][:3] == ["NSE", "NIFTY", "2020-01-30"]
    assert body["counts"] == {"expiries": 1, "contracts": 2, "exists": True, "note": None}


def test_registry_contracts_seeded_chain(client: TestClient, tmp_path: Path) -> None:
    _seed_registry(tmp_path)
    body = client.get("/api/v1/db/registry/contracts?underlying=NIFTY").json()
    assert body["total"] == 2
    assert {row[5] for row in body["rows"]} == {"CE", "PE"}
    assert body["counts"]["contracts"] == 2


def test_registry_contracts_filter_by_expiry(client: TestClient, tmp_path: Path) -> None:
    _seed_registry(tmp_path)
    body = client.get("/api/v1/db/registry/contracts?expiry_date=2020-01-30").json()
    assert body["total"] == 2


def test_registry_contracts_malformed_expiry_is_400(client: TestClient, tmp_path: Path) -> None:
    _seed_registry(tmp_path)
    r = client.get("/api/v1/db/registry/contracts?expiry_date=2020/01/30")
    assert r.status_code == 400
    assert "expiry_date" in r.json()["detail"]
