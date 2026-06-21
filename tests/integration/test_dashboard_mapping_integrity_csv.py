"""Integration tests for the /api/v1/universe/mapping-integrity.csv download.

DEC-MAPPING-INTEGRITY-EXPORT-001 — the CSV companion to the JSON route. Exercises the thin
route over a real wired :class:`InstrumentStore`: the file is text/csv with an attachment
disposition, an M&M-style zero-coverage eligible spot appears as a gating ``none`` row, and
an unwired store → 503 (consistent with the JSON sibling).
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
    s.upsert_many(
        [_eq("RELIANCE"), _eq("M&M")],
        snapshot_date=date(2024, 1, 2),
        run_id="r1",
        csv_source_sha256="0" * 64,
    )
    yield s
    s.close()


def test_csv_download_headers_and_gating_row(store: InstrumentStore) -> None:
    """RELIANCE covered, M&M empty → a downloadable CSV with M&M as a gating none row."""
    for year in _coverage_years():
        store.upsert_coverage(_cov("RELIANCE", year))
    client = TestClient(build_app(DashboardState(), instrument_store=store))

    resp = client.get("/api/v1/universe/mapping-integrity.csv")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "attachment" in resp.headers["content-disposition"]
    assert "mapping_integrity.csv" in resp.headers["content-disposition"]
    body = resp.text
    assert "Status,RED" in body
    # M&M is a gating none row; the `&` symbol survives intact.
    assert "NSE,M&M,none,yes," in body


def test_csv_download_green_when_all_covered(store: InstrumentStore) -> None:
    for underlying in ("RELIANCE", "M&M"):
        for year in _coverage_years():
            store.upsert_coverage(_cov(underlying, year))
    client = TestClient(build_app(DashboardState(), instrument_store=store))

    resp = client.get("/api/v1/universe/mapping-integrity.csv")

    assert resp.status_code == 200
    assert "Status,GREEN" in resp.text


def test_csv_download_503_when_store_unwired() -> None:
    client = TestClient(build_app(DashboardState(), instrument_store=None))
    assert client.get("/api/v1/universe/mapping-integrity.csv").status_code == 503
