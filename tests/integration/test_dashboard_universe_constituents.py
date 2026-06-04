"""Integration tests for ``GET /api/v1/universe/index-constituents``.

DEC-UNIVERSE-NSE-CONSTITUENTS-001 (MIGRATION.md #445). Drives the route
with FastAPI's :class:`TestClient` against a real
:class:`~brutex.data.instrument_store.InstrumentStore` holding a few
F&O-eligible single stocks. The route's only network surface is the NSE
constituent fetch; here we monkeypatch the module-level
:func:`brutex.dashboard.nse_constituents.members_in_universe` /
``fetch`` path via an injected fake opener so NO real network is touched
(NSE is bot-blocked + network-blocked in CI — see the module docstring).

Three branches are pinned:

* a working fake opener → 200 ``source:"nse"`` with the intersected members;
* an opener that raises ``OSError`` → 200 ``source:"unavailable"`` (graceful,
  NEVER a 500);
* an unknown index → 200 ``source:"unknown-index"``.

Skipped when ``fastapi`` / ``duckdb`` are absent (matches the sibling
``test_dashboard_instruments.py`` convention).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("duckdb")

import brutex.dashboard.nse_constituents as nc
from brutex.dashboard.app import DashboardState, build_app
from brutex.data.instrument_store import InstrumentStore
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.testclient import TestClient

# A constituent CSV the fake opener returns for ANY URL — RELIANCE +
# HDFCBANK + M&M are F&O stocks we hold; ZZZNOTHELD is in the index but
# NOT in our store (so it must be dropped from the response).
_FIXTURE_CSV = (
    "Company Name,Industry,Symbol,Series,ISIN Code\n"
    "Reliance Industries Ltd.,Refineries,RELIANCE,EQ,INE002A01018\n"
    "HDFC Bank Ltd.,Banks,HDFCBANK,EQ,INE040A01034\n"
    "Mahindra & Mahindra Ltd.,Automobile,M&M,EQ,INE101A01026\n"
    "Some Co Not Held,Misc,ZZZNOTHELD,EQ,INE999A01099\n"
)


def _eq(symbol: str, security_id: int) -> Instrument:
    """An NSE EQ row — F&O-eligible by construction in the lean store."""
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=security_id,
        symbol_name=symbol,
        display_name=symbol,
        instrument=InstrumentType.EQ,
        segment=Segment.CASH,
        series="EQ",
        isin=None,
        underlying_symbol=symbol,
        underlying_security_id=None,
        lot_size=1,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=100000,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
    )


def _idx(symbol: str, security_id: int) -> Instrument:
    """An NSE IDX row (so the index itself shows in the explorer)."""
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=security_id,
        symbol_name=symbol,
        display_name=symbol,
        instrument=InstrumentType.IDX,
        segment=Segment.CASH,
        series=None,
        isin=None,
        underlying_symbol=symbol,
        underlying_security_id=None,
        lot_size=1,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=0,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
    )


@pytest.fixture
def populated_store(tmp_path: Path):
    store = InstrumentStore.open(tmp_path / "instruments.duckdb")
    store.upsert_many(
        [
            _idx("NIFTY", 26000),
            _eq("RELIANCE", 2885),
            _eq("HDFCBANK", 1333),
            _eq("M&M", 2031),
        ],
        snapshot_date=date(2024, 1, 2),
        run_id="test-run",
        csv_source_sha256="a" * 64,
    )
    yield store
    store.close()


@pytest.fixture
def client(populated_store: InstrumentStore) -> TestClient:
    return TestClient(build_app(DashboardState(), instrument_store=populated_store))


def test_known_index_success_returns_intersected_members(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A working fetch → 200 ``source:"nse"`` with only the held members."""
    monkeypatch.setattr(nc, "_default_opener", lambda _url: _FIXTURE_CSV)
    r = client.get("/api/v1/universe/index-constituents", params={"symbol": "NIFTY"})
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "nse"
    # ZZZNOTHELD dropped (not in store); index order preserved.
    assert body["members"] == ["RELIANCE", "HDFCBANK", "M&M"]
    assert "constituent" in body["note"]


def test_fetch_failure_degrades_gracefully_no_500(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An opener that raises → 200 ``source:"unavailable"`` (NEVER a 500)."""

    def _boom(_url: str) -> str:
        raise OSError("Host not in allowlist")

    monkeypatch.setattr(nc, "_default_opener", _boom)
    r = client.get("/api/v1/universe/index-constituents", params={"symbol": "NIFTY"})
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "unavailable"
    assert body["members"] == []
    assert "Pull ALL F&O stocks" in body["note"]


def test_unknown_index_returns_unknown_source(client: TestClient) -> None:
    """An index with no NSE CSV mapping → 200 ``source:"unknown-index"``."""
    r = client.get("/api/v1/universe/index-constituents", params={"symbol": "NIFTY IT"})
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "unknown-index"
    assert body["members"] == []
    assert "superset" in body["note"]
