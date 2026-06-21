"""Integration tests for the economics-integrity routes + ingest hook.

DEC-INSTRUMENT-ECONOMICS-INTEGRITY-001 — the JSON + CSV routes over a real wired
:class:`InstrumentStore`, plus the ``_persist_economics_integrity`` hook that audits the full
leg-bearing index and persists the snapshot to ``schema_meta``.
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
from brutex.dashboard.instrument_loader import _persist_economics_integrity
from brutex.data.economics_integrity import META_KEY
from brutex.data.instrument_store import InstrumentStore
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.testclient import TestClient


def _leg(sym: str, sid: int, under: str, expiry: date, lot: int) -> Instrument:
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=sid,
        symbol_name=sym,
        display_name=sym,
        instrument=InstrumentType.CE,
        segment=Segment.FNO,
        series=None,
        isin=None,
        underlying_symbol=under,
        underlying_security_id=100,
        lot_size=lot,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=lot * 10,
        sm_expiry_date=expiry,
        strike_price=Decimal("100"),
        asm_gsm_flag=False,
    )


def _gsym(inst: Instrument) -> str:
    return f"{inst.exch_id.value}-{inst.symbol_name}-{inst.security_id}"


@pytest.fixture
def store(tmp_path: Path):
    s = InstrumentStore.open(tmp_path / "instruments.duckdb")
    yield s
    s.close()


def test_routes_503_when_store_unwired() -> None:
    client = TestClient(build_app(DashboardState(), instrument_store=None))
    assert client.get("/api/v1/universe/economics-integrity").status_code == 503
    assert client.get("/api/v1/universe/economics-integrity.csv").status_code == 503


def test_present_false_before_any_load(store: InstrumentStore) -> None:
    client = TestClient(build_app(DashboardState(), instrument_store=store))
    assert client.get("/api/v1/universe/economics-integrity").json() == {"present": False}
    csv = client.get("/api/v1/universe/economics-integrity.csv")
    assert csv.status_code == 200 and "no audit recorded yet" in csv.text


def test_ingest_hook_persists_and_routes_render(store: InstrumentStore) -> None:
    e1 = date(2026, 6, 25)
    good1 = _leg("RCE1", 1, "RELIANCE", e1, 500)
    bad = _leg("RCE2", 2, "RELIANCE", e1, 750)  # within-expiry lot mismatch → RED
    index = build_index([(_gsym(good1), good1), (_gsym(bad), bad)])

    payload = _persist_economics_integrity(index, store, run_id="run-x", csv_sha256="sha-x")
    assert payload["is_green"] is False
    assert payload["lot_mismatch_expiries"] == 1
    assert store.get_meta(META_KEY) is not None

    client = TestClient(build_app(DashboardState(), instrument_store=store))
    body = client.get("/api/v1/universe/economics-integrity").json()
    assert body["present"] is True and body["is_green"] is False
    assert body["lot_mismatch_expiries"] == 1

    csv = client.get("/api/v1/universe/economics-integrity.csv")
    assert csv.status_code == 200
    assert "Status,RED" in csv.text
    assert "NSE,RELIANCE,2026-06-25,500 750" in csv.text
