"""Integration tests for the ``/db`` F&O symbol→expiry→contract cascade routes.

DEC-DB-FNO-CASCADE-001. The flat F&O dataset dropdown (thousands of contract
partitions) is replaced by three lazy, O(1)-cached pickers — underlyings →
expiries(underlying) → contracts(underlying, expiry). Exercised end-to-end against
a real lake written by the production :class:`~brutex.data.sink.ParquetBarSink`,
plus the no-lake-root degrade (empty shapes, never a 500).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("duckdb")
pytest.importorskip("polars")

from brutex.dashboard.app import DashboardState, build_app
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink
from fastapi.testclient import TestClient


def _write_fno(lake: Path, symbol: str, *, oi: int = 1000, interval: str = "1minute") -> None:
    """Write one F&O 1-minute bar to ``bars/NSE/FNO/<symbol>/<interval>/…``."""
    bar = CandleBar(
        timestamp=datetime(2025, 1, 2, 9, 15, tzinfo=UTC),
        open=Decimal("100.10"),
        high=Decimal("101.20"),
        low=Decimal("99.50"),
        close=Decimal("100.80"),
        volume=375,
        open_interest=oi,
    )
    ParquetBarSink(writer=ParquetWriter(lake_root=lake)).append(
        exchange="NSE", segment="FNO", symbol=symbol, interval=interval, bars=[bar]
    )


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    """An app over a tmp lake seeded with a NIFTY chain (CE/PE/FUT) + a BANKNIFTY leg."""
    lake = tmp_path / "lake"
    _write_fno(lake, "NSE-NIFTY-02Jan25-24000-CE")
    _write_fno(lake, "NSE-NIFTY-02Jan25-24000-PE")
    _write_fno(lake, "NSE-NIFTY-02Jan25-23000-CE")
    _write_fno(lake, "NSE-NIFTY-02Jan25-FUT")
    _write_fno(lake, "NSE-BANKNIFTY-30Jan25-48000-CE")
    # store.path → <lake>/instruments.duckdb, so the route's lake root = <lake>.
    fake_store = SimpleNamespace(path=lake / "instruments.duckdb")
    return TestClient(build_app(DashboardState(), instrument_store=fake_store))


def test_underlyings_sorted(client: TestClient) -> None:
    body = client.get("/api/v1/db/fno/underlyings").json()
    assert body["underlyings"] == ["BANKNIFTY", "NIFTY"]  # ascending, deduped
    assert body["unparsed"] == []  # every seeded symbol decodes


def test_expiries_scoped_to_underlying(client: TestClient) -> None:
    body = client.get("/api/v1/db/fno/expiries", params={"underlying": "NIFTY"}).json()
    # `expiry_counts` (DEC-DB-UX-PACK-001 Feature 6): with no sibling registry.duckdb,
    # `listed` is a null (honest "unknown") and `with_data` counts the 4 seeded NIFTY
    # legs (FUT + 23000-CE + 24000-CE + 24000-PE) at the single seeded expiry.
    assert body == {
        "underlying": "NIFTY",
        "expiries": ["2025-01-02"],
        "expiry_counts": [{"expiry": "2025-01-02", "listed": None, "with_data": 4}],
    }
    bn = client.get("/api/v1/db/fno/expiries", params={"underlying": "BANKNIFTY"}).json()
    assert bn["expiries"] == ["2025-01-30"]
    assert bn["expiry_counts"] == [{"expiry": "2025-01-30", "listed": None, "with_data": 1}]


def test_expiries_unknown_underlying_is_empty(client: TestClient) -> None:
    body = client.get("/api/v1/db/fno/expiries", params={"underlying": "NOPE"}).json()
    # An unknown underlying → no expiries + no counts (never a 500).
    assert body == {"underlying": "NOPE", "expiries": [], "expiry_counts": []}


def test_contracts_futures_first_then_strike_then_type(client: TestClient) -> None:
    body = client.get(
        "/api/v1/db/fno/contracts", params={"underlying": "NIFTY", "expiry": "2025-01-02"}
    ).json()
    assert body["underlying"] == "NIFTY" and body["expiry"] == "2025-01-02"
    labels = [c["label"] for c in body["contracts"]]
    # FUT first, then ascending strike (23000 before 24000), CE before PE at a tie.
    assert labels == ["FUT", "23000 CE", "24000 CE", "24000 PE"]
    # The leaf carries the exact query key + interval the /db candle reader takes.
    ce = next(c for c in body["contracts"] if c["label"] == "24000 CE")
    assert ce["symbol"] == "NSE-NIFTY-02Jan25-24000-CE"
    assert ce["intervals"] == ["1minute"]
    assert ce["exchange"] == "NSE"


def test_contracts_unknown_cell_is_empty(client: TestClient) -> None:
    body = client.get(
        "/api/v1/db/fno/contracts", params={"underlying": "NIFTY", "expiry": "1999-01-01"}
    ).json()
    assert body["contracts"] == []  # never a 500


def test_no_lake_root_degrades_to_empty_on_all_three_routes() -> None:
    fake = SimpleNamespace(path=None)
    c = TestClient(build_app(DashboardState(), instrument_store=fake))
    assert c.get("/api/v1/db/fno/underlyings").json() == {"underlyings": [], "unparsed": []}
    exp = c.get("/api/v1/db/fno/expiries", params={"underlying": "NIFTY"}).json()
    assert exp == {"underlying": "NIFTY", "expiries": [], "expiry_counts": []}
    con = c.get(
        "/api/v1/db/fno/contracts", params={"underlying": "NIFTY", "expiry": "2025-01-02"}
    ).json()
    assert con == {"underlying": "NIFTY", "expiry": "2025-01-02", "contracts": []}


def test_undecodable_partition_surfaced_in_unparsed(tmp_path: Path) -> None:
    # A present-but-undecodable F&O partition (bad expiry token) is surfaced in
    # `unparsed`, never hidden, and never pollutes the cascade (Golden Rule #1).
    lake = tmp_path / "lake"
    _write_fno(lake, "NSE-NIFTY-02Jan25-24000-CE")
    _write_fno(lake, "NSE-NIFTY-BADX-FUT")  # bad expiry token → does not decode
    fake_store = SimpleNamespace(path=lake / "instruments.duckdb")
    c = TestClient(build_app(DashboardState(), instrument_store=fake_store))
    body = c.get("/api/v1/db/fno/underlyings").json()
    assert body["underlyings"] == ["NIFTY"]  # only the decodable one
    assert [u["symbol"] for u in body["unparsed"]] == ["NSE-NIFTY-BADX-FUT"]


def test_db_page_wires_the_cascade_frontend() -> None:
    # The /db page serves the cascade picker: it must reference all three lazy
    # endpoints + the level handlers, so the symbol→expiry→contract UI is live
    # (the repo's template-presence convention — JS is not Python-coverage-gated).
    c = TestClient(build_app(DashboardState(), instrument_store=SimpleNamespace(path=None)))
    html = c.get("/db").text
    assert "/api/v1/db/fno/underlyings" in html
    assert "/api/v1/db/fno/expiries?underlying=" in html
    assert "/api/v1/db/fno/contracts?underlying=" in html
    for fn in (
        "loadFnoCascade",
        "onFnoUnderlyingChange",
        "onFnoExpiryChange",
        "onFnoContractChange",
    ):
        assert fn in html
    # The cascade picker elements + the flat-dropdown fallback for spot/option.
    assert 'id="cund"' in html and 'id="cexp"' in html and 'id="ccon"' in html
