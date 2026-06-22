"""Integration tests for the ``/db`` SPOT symbol→interval cascade route + frontend.

DEC-DB-SPOT-CASCADE-001. The flat SPOT dataset dropdown (the ~750-name reference
universe × stored intervals) is replaced by a lazy, O(1)-cached symbol picker whose
leaves carry their STORED intervals inline — the spot twin of the F&O cascade.
Exercised end-to-end against a real lake written by the production
:class:`~brutex.data.sink.ParquetBarSink`, plus the no-lake-root degrade (empty
shape, never a 500) and the frontend wiring.
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


def _write_spot(lake: Path, *, exchange: str, segment: str, symbol: str, interval: str) -> None:
    """Write one spot 1-minute bar to ``bars/<exchange>/<segment>/<symbol>/<interval>/…``."""
    bar = CandleBar(
        timestamp=datetime(2025, 1, 2, 9, 15, tzinfo=UTC),
        open=Decimal("100.10"),
        high=Decimal("101.20"),
        low=Decimal("99.50"),
        close=Decimal("100.80"),
        volume=375,
        open_interest=None,
    )
    ParquetBarSink(writer=ParquetWriter(lake_root=lake)).append(
        exchange=exchange, segment=segment, symbol=symbol, interval=interval, bars=[bar]
    )


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    """An app over a tmp lake seeded with two indices + one equity (mixed intervals)."""
    lake = tmp_path / "lake"
    # Two INDEX symbols (one at 1minute + the derived 1day), one CASH equity.
    _write_spot(lake, exchange="NSE", segment="INDEX", symbol="NSE-NIFTY", interval="1minute")
    _write_spot(lake, exchange="NSE", segment="INDEX", symbol="NSE-NIFTY", interval="1day")
    _write_spot(lake, exchange="BSE", segment="INDEX", symbol="BSE-SENSEX", interval="1minute")
    _write_spot(lake, exchange="NSE", segment="CASH", symbol="NSE-RELIANCE", interval="1minute")
    # store.path → <lake>/instruments.duckdb, so the route's lake root = <lake>.
    fake_store = SimpleNamespace(path=lake / "instruments.duckdb")
    return TestClient(build_app(DashboardState(), instrument_store=fake_store))


def test_symbols_indices_first_then_alpha_with_intervals_inline(client: TestClient) -> None:
    body = client.get("/api/v1/db/spot/symbols").json()
    rows = body["symbols"]
    # INDEX segment floats above CASH; within a rank, symbol-alphabetical.
    assert [(r["segment"], r["symbol"]) for r in rows] == [
        ("INDEX", "BSE-SENSEX"),
        ("INDEX", "NSE-NIFTY"),
        ("CASH", "NSE-RELIANCE"),
    ]
    # The NIFTY leaf carries BOTH stored intervals inline (ascending) — the picker
    # needs no second fetch; only intervals with a real on-disk partition appear.
    nifty = next(r for r in rows if r["symbol"] == "NSE-NIFTY")
    assert nifty["intervals"] == ["1day", "1minute"]
    assert nifty["exchange"] == "NSE" and nifty["label"] == "NSE-NIFTY"
    # A single-interval equity carries just its one stored interval.
    reliance = next(r for r in rows if r["symbol"] == "NSE-RELIANCE")
    assert reliance["intervals"] == ["1minute"]


def test_fno_segment_excluded_from_spot_symbols(tmp_path: Path) -> None:
    # An F&O partition must NOT leak into the spot picker (it has its own cascade) —
    # the route filters the non-FNO segments exactly like the datasets endpoint.
    lake = tmp_path / "lake"
    _write_spot(lake, exchange="NSE", segment="INDEX", symbol="NSE-NIFTY", interval="1minute")
    # An F&O contract partition under segment=FNO.
    ParquetBarSink(writer=ParquetWriter(lake_root=lake)).append(
        exchange="NSE",
        segment="FNO",
        symbol="NSE-NIFTY-02Jan25-24000-CE",
        interval="1minute",
        bars=[
            CandleBar(
                timestamp=datetime(2025, 1, 2, 9, 15, tzinfo=UTC),
                open=Decimal("1.10"),
                high=Decimal("1.20"),
                low=Decimal("1.00"),
                close=Decimal("1.15"),
                volume=10,
                open_interest=500,
            )
        ],
    )
    fake_store = SimpleNamespace(path=lake / "instruments.duckdb")
    c = TestClient(build_app(DashboardState(), instrument_store=fake_store))
    rows = c.get("/api/v1/db/spot/symbols").json()["symbols"]
    assert [r["symbol"] for r in rows] == ["NSE-NIFTY"]  # the F&O leg is excluded


def test_no_lake_root_degrades_to_empty() -> None:
    fake = SimpleNamespace(path=None)
    c = TestClient(build_app(DashboardState(), instrument_store=fake))
    assert c.get("/api/v1/db/spot/symbols").json() == {"symbols": []}  # never a 500


def test_db_page_wires_the_spot_cascade_frontend() -> None:
    # The /db page serves the spot cascade picker: it must reference the symbols
    # endpoint + the handlers, so the symbol→interval UI is live (the repo's
    # template-presence convention — JS is not Python-coverage-gated).
    c = TestClient(build_app(DashboardState(), instrument_store=SimpleNamespace(path=None)))
    html = c.get("/db").text
    assert "/api/v1/db/spot/symbols" in html
    for fn in ("loadSpotCascade", "onSpotSymbolChange", "spotCascadeHtml"):
        assert fn in html
    assert 'id="csym"' in html  # the symbol select element
