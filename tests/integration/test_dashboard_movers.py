"""Integration tests for the ``/movers`` dashboard route (DEC-MOVERS-DASHBOARD-PAGE-001).

Exercises ``GET /api/v1/movers`` + ``GET /movers`` end-to-end through FastAPI's
:class:`TestClient` against a REAL Parquet + DuckDB lake — the honest proof that
the production :class:`~brutex.dashboard.movers_reader.MoversLakeReader`
(``Catalog.open`` → reconcile → ``get_bars`` → ``arrow_table_to_bars``) feeds the
already-merged screener cores correctly. Mirrors the
``test_dashboard_coverage.py`` convention (store at ``tmp_path/instruments.duckdb``
⇒ lake root ``tmp_path``; ``ParquetWriter`` seeds the bars).

Skipped when ``fastapi`` / ``duckdb`` are absent.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("duckdb")
pytest.importorskip("polars")
pytest.importorskip("pyarrow")

from brutex.dashboard.app import DashboardState, build_app
from brutex.data.instrument_store import InstrumentStore
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.testclient import TestClient

_EXCHANGE = "NSE"
_SEGMENT = "INDEX"
_INTERVAL = "1day"


def _idx(symbol: str) -> Instrument:
    """A minimal NSE index instrument (the store only needs a row to exist)."""
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=abs(hash(symbol)) % 100_000,
        symbol_name=symbol,
        display_name=symbol,
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
def store(tmp_path: Path):
    s = InstrumentStore.open(tmp_path / "instruments.duckdb")
    s.upsert_many(
        [_idx("NIFTY"), _idx("BANKNIFTY")],
        snapshot_date=date(2024, 1, 2),
        run_id="r1",
        csv_source_sha256="0" * 64,
    )
    yield s
    s.close()


def _bar(*, day: int, open_: str, close: str, volume: int) -> CandleBar:
    """A valid daily OHLCV bar on ``2024-01-<day>`` (09:15 IST ≈ 03:45 UTC)."""
    o = Decimal(open_)
    c = Decimal(close)
    return CandleBar(
        timestamp=datetime(2024, 1, day, 3, 45, tzinfo=UTC),
        open=o,
        high=max(o, c),
        low=min(o, c),
        close=c,
        volume=volume,
        open_interest=None,
    )


def _seed_lake(tmp_path: Path) -> None:
    """Write a NIFTY (+10% gainer) + BANKNIFTY (-10% loser) 1day lake with volume."""
    writer = ParquetWriter(lake_root=tmp_path)
    writer.write(
        exchange=_EXCHANGE,
        segment=_SEGMENT,
        symbol="NIFTY",
        interval=_INTERVAL,
        bars=[
            _bar(day=2, open_="20000", close="20100", volume=100),
            _bar(day=3, open_="20100", close="20100", volume=100),
            _bar(day=4, open_="20100", close="22000", volume=500),
        ],
    )
    writer.write(
        exchange=_EXCHANGE,
        segment=_SEGMENT,
        symbol="BANKNIFTY",
        interval=_INTERVAL,
        bars=[
            _bar(day=2, open_="45000", close="44000", volume=80),
            _bar(day=3, open_="44000", close="44000", volume=80),
            _bar(day=4, open_="44000", close="40500", volume=90),
        ],
    )


class TestMoversApi:
    def test_503_when_store_unwired(self) -> None:
        client = TestClient(build_app(DashboardState(), instrument_store=None))
        assert client.get("/api/v1/movers").status_code == 503

    def test_empty_payload_when_no_lake_root(self) -> None:
        # A store whose `path` is not a filesystem Path → empty payload, never 500.
        fake = SimpleNamespace(path=None)
        client = TestClient(build_app(DashboardState(), instrument_store=fake))
        body = client.get("/api/v1/movers").json()
        assert body["instrument_count"] == 0
        assert body["gainers"] == []
        assert "pull spot first" in body["note"]

    def test_400_on_malformed_date(self, store: InstrumentStore) -> None:
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        resp = client.get("/api/v1/movers", params={"start": "not-a-date"})
        assert resp.status_code == 400
        assert "malformed date" in resp.json()["detail"]

    def test_400_on_from_after_to(self, store: InstrumentStore) -> None:
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        resp = client.get("/api/v1/movers", params={"start": "2024-03-31", "end": "2024-03-01"})
        assert resp.status_code == 400
        assert "must be on or before" in resp.json()["detail"]

    def test_empty_lake_degrades_honestly(self, store: InstrumentStore) -> None:
        # Store wired but no parquet written → honest empty payload (never 500).
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        body = client.get("/api/v1/movers").json()
        assert body["instrument_count"] == 0
        assert "pull spot first" in body["note"]

    def test_ranks_a_real_parquet_lake(self, store: InstrumentStore, tmp_path: Path) -> None:
        # store.path is tmp_path/instruments.duckdb → lake root is tmp_path.
        _seed_lake(tmp_path)
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        body = client.get(
            "/api/v1/movers",
            params={
                "interval": "1day",
                "start": "2024-01-01",
                "end": "2024-12-31",
                "top_n": 5,
                "recent_bars": 1,
            },
        ).json()
        assert body["instrument_count"] == 2
        assert body["skipped_count"] == 0
        # NIFTY ran 20000 → 22000 (+10%); BANKNIFTY 45000 → 40500 (-10%).
        assert [r["symbol"] for r in body["gainers"]] == ["NIFTY"]
        assert [r["symbol"] for r in body["losers"]] == ["BANKNIFTY"]
        assert body["gainers"][0]["last_price"] == "₹22,000.00"
        assert Decimal(body["gainers"][0]["pct_change"]) == Decimal("10")
        # Both have volume + range → both metric tables populated, ₹ last close.
        assert {r["symbol"] for r in body["volume_movers"]} == {"NIFTY", "BANKNIFTY"}
        assert {r["symbol"] for r in body["volatility_movers"]} == {"NIFTY", "BANKNIFTY"}
        assert body["volume_movers"][0]["last_close"].startswith("₹")

    def test_empty_window_over_populated_lake_is_honest(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        # Data exists, but the requested window predates it → all skipped, honest note.
        _seed_lake(tmp_path)
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        body = client.get(
            "/api/v1/movers", params={"start": "2021-01-01", "end": "2021-01-31"}
        ).json()
        assert body["instrument_count"] == 2
        assert body["skipped_count"] == 2
        assert body["gainers"] == []
        assert "none had usable bars" in body["note"]


class TestMoversPage:
    def test_movers_page_renders(self, store: InstrumentStore) -> None:
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        resp = client.get("/movers")
        assert resp.status_code == 200
        assert "Movers" in resp.text
        assert "/api/v1/movers" in resp.text
        # Cross-links to the established dashboard pages + the four tables.
        assert 'href="/coverage"' in resp.text
        assert 'href="/reports"' in resp.text
        assert "Top gainers" in resp.text
        assert "Volatility movers" in resp.text
        # A second render is served verbatim from the module template cache
        # (exercises the cached branch of `_load_movers_template`).
        again = client.get("/movers")
        assert again.status_code == 200
        assert again.text == resp.text
