"""Integration tests for the instrument-master integrity routes + ingest hook.

DEC-INSTRUMENT-MASTER-INTEGRITY-001 — the JSON + CSV routes over a real wired
:class:`InstrumentStore`, plus the ``_persist_master_integrity`` ingest hook that runs the
audit against the full leg-bearing index and persists the snapshot to ``schema_meta``.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("duckdb")

from brutex.adapters.groww.instruments import build_index
from brutex.dashboard.app import DashboardState, build_app
from brutex.dashboard.instrument_loader import _persist_master_integrity
from brutex.data.instrument_store import InstrumentStore
from brutex.data.master_integrity import META_KEY
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.testclient import TestClient


def _spot(sym: str, sid: int, ft: str) -> Instrument:
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=sid,
        symbol_name=sym,
        display_name=sym,
        instrument=InstrumentType.EQ,
        segment=Segment.CASH,
        series="EQ",
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=1,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
        feed_token=ft,
    )


def _leg(sym: str, sid: int, usym: str, usid: int | None) -> Instrument:
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=sid,
        symbol_name=sym,
        display_name=sym,
        instrument=InstrumentType.CE,
        segment=Segment.FNO,
        series=None,
        isin=None,
        underlying_symbol=usym,
        underlying_security_id=usid,
        lot_size=50,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=None,
        sm_expiry_date=date(2024, 1, 25),
        strike_price=Decimal("100"),
        asm_gsm_flag=False,
    )


def _gsym(inst: Instrument) -> str:
    return f"{inst.exch_id.value}-{inst.symbol_name}"


@pytest.fixture
def store(tmp_path: Path):
    s = InstrumentStore.open(tmp_path / "instruments.duckdb")
    s.upsert_many(
        [_spot("RELIANCE", 100, "100")],
        snapshot_date=date(2024, 1, 2),
        run_id="r1",
        csv_source_sha256="0" * 64,
    )
    yield s
    s.close()


def test_routes_503_when_store_unwired() -> None:
    client = TestClient(build_app(DashboardState(), instrument_store=None))
    assert client.get("/api/v1/universe/master-integrity").status_code == 503
    assert client.get("/api/v1/universe/master-integrity.csv").status_code == 503


def test_present_false_before_any_load(store: InstrumentStore) -> None:
    client = TestClient(build_app(DashboardState(), instrument_store=store))
    body = client.get("/api/v1/universe/master-integrity").json()
    assert body == {"present": False}
    # CSV gives a valid placeholder file (not a crash).
    csv = client.get("/api/v1/universe/master-integrity.csv")
    assert csv.status_code == 200
    assert "no audit recorded yet" in csv.text


def test_ingest_hook_persists_and_routes_render(store: InstrumentStore) -> None:
    # Build a full leg-bearing index: RELIANCE spot + a resolving CE + an ORPHAN CE.
    spot = _spot("RELIANCE", 100, "100")
    good = _leg("R24JAN3000CE", 900, "RELIANCE", 100)
    orphan = _leg("NOPECE", 901, "NOPE", None)
    index = build_index([(_gsym(spot), spot), (_gsym(good), good), (_gsym(orphan), orphan)])

    payload = _persist_master_integrity(index, store, run_id="run-x", csv_sha256="sha-x")
    assert payload["is_green"] is False
    assert payload["orphan_count"] == 1
    # The snapshot is in schema_meta.
    assert store.get_meta(META_KEY) is not None

    client = TestClient(build_app(DashboardState(), instrument_store=store))
    body = client.get("/api/v1/universe/master-integrity").json()
    assert body["present"] is True
    assert body["is_green"] is False
    assert body["orphan_count"] == 1
    assert body["type_counts"]["legs"] == 2

    csv = client.get("/api/v1/universe/master-integrity.csv")
    assert csv.status_code == 200
    assert "Status,RED" in csv.text
    assert "NSE,NOPECE,CE,NOPE,,orphan" in csv.text
    assert "attachment" in csv.headers["content-disposition"]
