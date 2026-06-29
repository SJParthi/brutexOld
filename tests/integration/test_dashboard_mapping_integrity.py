"""Integration tests for the /api/v1/universe/mapping-integrity route.

DEC-MAPPING-INTEGRITY-RECONCILE-001 / DEC-MAPPING-INTEGRITY-ATTEMPTED-001 — exercises the
thin route over a real wired :class:`InstrumentStore`: an options-eligible spot that was
ATTEMPTED yet pulled zero expiries (M&M-style — a coverage row with ``expiries_pulled=0``)
turns the verdict RED; a never-attempted spot (no coverage row, a selective pull) stays
GREEN as ``not_attempted``; a fully-covered spot stays green; an unwired store → 503.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("duckdb")

from brutex.dashboard.app import DashboardState, _coverage_years, build_app
from brutex.data.coverage_ledger import CoverageRecord, classify_cell
from brutex.data.instrument_store import InstrumentStore
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.testclient import TestClient


def _eq(symbol: str) -> Instrument:
    """An NSE cash-equity row — F&O-eligible by construction in the lean store."""
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=abs(hash(symbol)) % 100_000,
        symbol_name=symbol,
        display_name=symbol,
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
    )


def _cov(underlying: str, year: int, *, expiries: int = 2) -> CoverageRecord:
    return CoverageRecord(
        exch_id="NSE",
        underlying_symbol=underlying,
        year=year,
        expiries_pulled=expiries,
        contracts_pulled=expiries * 10,
        status=classify_cell(expiries, expiries * 10, None),
        last_run_id="run-1",
        last_fetched_at=datetime(2026, 5, 30, 9, 15, tzinfo=UTC),
    )


@pytest.fixture
def store(tmp_path: Path):
    s = InstrumentStore.open(tmp_path / "instruments.duckdb")
    # Two F&O-eligible equities: RELIANCE (will be fully covered) + M&M (will pull nothing).
    s.upsert_many(
        [_eq("RELIANCE"), _eq("M&M")],
        snapshot_date=date(2024, 1, 2),
        run_id="r1",
        csv_source_sha256="0" * 64,
    )
    yield s
    s.close()


def test_mapping_integrity_flags_attempted_zero_coverage_spot(store: InstrumentStore) -> None:
    """RELIANCE covered; M&M ATTEMPTED yet zero expiries (a row) → RED, M&M gating none.

    The attempted-and-empty marker is a coverage row with ``expiries_pulled=0`` (what Phase-1
    expiry discovery writes for a silent-vanish underlying), so the gate still catches the real
    failure (DEC-MAPPING-INTEGRITY-ATTEMPTED-001).
    """
    for year in _coverage_years():
        store.upsert_coverage(_cov("RELIANCE", year))
    # M&M was attempted but discovered nothing — one zero-expiry cell records the attempt.
    store.upsert_coverage(_cov("M&M", _coverage_years()[0], expiries=0))
    client = TestClient(build_app(DashboardState(), instrument_store=store))

    body = client.get("/api/v1/universe/mapping-integrity").json()

    assert body["is_green"] is False
    assert body["total_eligible"] == 2 and body["fully_covered"] == 1
    assert body["gating_failures"] == 1
    assert body["not_attempted"] == 0
    [under] = body["under_pulled"]
    assert under["symbol"] == "M&M" and under["reason"] == "none"
    assert under["whitelisted"] is False
    assert body["expected_years"] == _coverage_years()


def test_mapping_integrity_green_on_clean_selective_pull(store: InstrumentStore) -> None:
    """The headline fix: a selective pull (only RELIANCE attempted) reads GREEN, not RED.

    M&M was NEVER attempted (no coverage row), so it is the neutral ``not_attempted`` state,
    never a gating ``none`` — the false-RED-on-selective-pull bug is closed
    (DEC-MAPPING-INTEGRITY-ATTEMPTED-001).
    """
    for year in _coverage_years():
        store.upsert_coverage(_cov("RELIANCE", year))
    client = TestClient(build_app(DashboardState(), instrument_store=store))

    body = client.get("/api/v1/universe/mapping-integrity").json()

    assert body["is_green"] is True
    assert body["total_eligible"] == 2 and body["fully_covered"] == 1
    assert body["gating_failures"] == 0
    assert body["not_attempted"] == 1
    [under] = body["under_pulled"]
    assert under["symbol"] == "M&M" and under["reason"] == "not_attempted"


def test_mapping_integrity_green_when_all_eligible_covered(store: InstrumentStore) -> None:
    """Both eligible spots fully covered → green, no under-pulled rows."""
    for underlying in ("RELIANCE", "M&M"):
        for year in _coverage_years():
            store.upsert_coverage(_cov(underlying, year))
    client = TestClient(build_app(DashboardState(), instrument_store=store))

    body = client.get("/api/v1/universe/mapping-integrity").json()

    assert body["is_green"] is True
    assert body["total_eligible"] == 2 and body["fully_covered"] == 2
    assert body["not_attempted"] == 0
    assert body["under_pulled"] == []


def test_mapping_integrity_503_when_store_unwired() -> None:
    """No store wired → 503 (consistent with the other universe/coverage routes)."""
    client = TestClient(build_app(DashboardState(), instrument_store=None))
    assert client.get("/api/v1/universe/mapping-integrity").status_code == 503
