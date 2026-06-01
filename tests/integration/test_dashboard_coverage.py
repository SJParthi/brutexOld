"""Integration tests for the dashboard coverage-ledger endpoints.

Exercises the ``/api/v1/coverage`` + ``/coverage`` routes
(DEC-COVERAGE-LEDGER-001) via FastAPI's
:class:`TestClient` with a real wired :class:`InstrumentStore`, plus the
unwired-store 503 path. These cover the app.py glue (coverage-omit in the
unit run) end-to-end.

Skipped when ``fastapi`` / ``duckdb`` are absent — matches the existing
``test_dashboard_instruments.py`` convention.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("duckdb")

from brutex.dashboard.app import (
    DashboardState,
    _coverage_years,
    _render_coverage_html,
    build_app,
)
from brutex.data.coverage_ledger import CoverageRecord, classify_cell
from brutex.data.instrument_store import InstrumentStore
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.testclient import TestClient


def _idx(symbol: str, *, exchange: Exchange = Exchange.NSE) -> Instrument:
    return Instrument(
        exchange=exchange,
        exchange_token=abs(hash(symbol)) % 100_000,
        trading_symbol=symbol,
        vendor_symbol=f"{exchange.value}-{symbol}",
        name=symbol,
        instrument_type=InstrumentType.IDX,
        segment=Segment.CASH,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_exchange_token=None,
        lot_size=1,
        tick_size=Decimal("0.05"),
        freeze_quantity=None,
        expiry_date=None,
        strike_price=None,
        is_reserved=False,
        buy_allowed=True,
        sell_allowed=True,
    )


@pytest.fixture
def store(tmp_path: Path):
    s = InstrumentStore.open(tmp_path / "instruments.duckdb")
    # One index → the expected grid has (NSE, NIFTY) × every coverage year.
    s.upsert_many(
        [_idx("NIFTY")],
        snapshot_date=date(2024, 1, 2),
        run_id="r1",
        csv_source_sha256="0" * 64,
    )
    yield s
    s.close()


def _cov(year: int, *, error: str | None = None, expiries: int = 2) -> CoverageRecord:
    return CoverageRecord(
        exchange="NSE",
        underlying_symbol="NIFTY",
        year=year,
        expiries_pulled=expiries,
        contracts_pulled=expiries * 10,
        status=classify_cell(expiries, expiries * 10, error),
        last_run_id="run-1",
        last_fetched_at=datetime(2026, 5, 30, 9, 15, tzinfo=UTC),
        error_message=error,
    )


class TestCoverageApi:
    def test_coverage_empty_ledger_all_missing(self, store: InstrumentStore) -> None:
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        resp = client.get("/api/v1/coverage")
        assert resp.status_code == 200
        body = resp.json()
        years = _coverage_years()
        assert body["headline"]["total"] == len(years)
        assert body["headline"]["complete"] == 0
        assert body["headline"]["missing"] == len(years)
        assert body["headline"]["percent"] == 0
        # Every row is a missing cell for NIFTY.
        assert len(body["rows"]) == len(years)
        assert all(r["status"] == "missing" for r in body["rows"])
        assert all(r["underlying"] == "NIFTY" for r in body["rows"])

    def test_coverage_with_complete_and_failed_cells(self, store: InstrumentStore) -> None:
        years = _coverage_years()
        store.upsert_coverage(_cov(years[0]))  # complete
        store.upsert_coverage(_cov(years[1], error="boom", expiries=0))  # failed
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        body = client.get("/api/v1/coverage").json()
        assert body["headline"]["complete"] == 1
        assert body["headline"]["failed"] == 1
        assert body["headline"]["missing"] == len(years) - 2
        statuses = {r["year"]: r["status"] for r in body["rows"]}
        assert statuses[years[0]] == "complete"
        assert statuses[years[1]] == "failed"
        failed_row = next(r for r in body["rows"] if r["year"] == years[1])
        assert failed_row["error"] == "boom"

    def test_coverage_503_when_store_unwired(self) -> None:
        client = TestClient(build_app(DashboardState(), instrument_store=None))
        resp = client.get("/api/v1/coverage")
        assert resp.status_code == 503

    def test_coverage_page_renders_html(self, store: InstrumentStore) -> None:
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        resp = client.get("/coverage")
        assert resp.status_code == 200
        assert "Coverage" in resp.text
        assert 'href="/coverage"' in resp.text


class TestCoverageHelpers:
    def test_coverage_years_starts_2020(self) -> None:
        years = _coverage_years()
        assert years[0] == 2020
        assert years[-1] == datetime.now(UTC).year
        assert years == list(range(2020, datetime.now(UTC).year + 1))

    def test_render_coverage_html_has_nav_and_table(self) -> None:
        html = _render_coverage_html()
        assert "/coverage" in html
        assert "Underlying" in html
