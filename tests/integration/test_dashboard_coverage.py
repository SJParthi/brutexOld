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
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.testclient import TestClient


def _idx(symbol: str, *, exchange: Exchange = Exchange.NSE) -> Instrument:
    return Instrument(
        exch_id=exchange,
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
        exch_id="NSE",
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
        # The per-window status table is wired into the page (DEC-WINDOW-STATUS-001
        # surfaced via DEC-COVERAGE-WINDOWS-UI-001): the section + its poll of
        # /api/v1/coverage/windows must be present so the operator SEES which fetch
        # windows are SUCCESS / EMPTY / FAILED without hitting the raw API.
        assert "Fetch windows" in resp.text
        assert "/api/v1/coverage/windows" in resp.text
        # Regression guard for the 3-section overlap (DEC-COVERAGE-WINDOWS-LAYOUT-001):
        # ALL three panels — not just the first #panel — must carry the bounded
        # internal-scroll rule, else #spotpanel/#winpanel grow to content height and
        # overlap the next section. Without the fix the served CSS has "#panel {"
        # alone and this assertion fails.
        assert "#panel, #spotpanel, #winpanel" in resp.text


def _bar(ts: datetime, close: str) -> CandleBar:
    return CandleBar(
        timestamp=ts,
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal(close),
        volume=10,
        open_interest=None,
    )


class TestSpotCoverageApi:
    """The SPOT coverage section of /coverage (DEC-COVERAGE-SPOT-001)."""

    def test_spot_empty_lake_is_zero(self, store: InstrumentStore) -> None:
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        resp = client.get("/api/v1/coverage/spot")
        assert resp.status_code == 200
        body = resp.json()
        assert body == {"headline": {"datasets": 0, "total_bars": 0}, "rows": []}

    def test_spot_reports_written_datasets(self, store: InstrumentStore, tmp_path: Path) -> None:
        # store.path is tmp_path/instruments.duckdb → lake root is tmp_path.
        ParquetWriter(lake_root=tmp_path).write(
            exchange="NSE",
            segment="CASH",
            symbol="NSE-NIFTY",
            interval="1minute",
            bars=[
                _bar(datetime(2024, 3, 1, 9, 15, tzinfo=UTC), "100.4"),
                _bar(datetime(2024, 3, 2, 9, 15, tzinfo=UTC), "100.8"),
            ],
        )
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        body = client.get("/api/v1/coverage/spot").json()
        assert body["headline"] == {"datasets": 1, "total_bars": 2}
        assert body["rows"] == [
            {
                "exchange": "NSE",
                "segment": "CASH",
                "symbol": "NSE-NIFTY",
                "interval": "1minute",
                "bars": 2,
                "first_date": "2024-03-01",
                "last_date": "2024-03-02",
            }
        ]

    def test_spot_503_when_store_unwired(self) -> None:
        client = TestClient(build_app(DashboardState(), instrument_store=None))
        assert client.get("/api/v1/coverage/spot").status_code == 503

    def test_spot_empty_payload_when_no_lake_root(self) -> None:
        # A store whose `path` is not a filesystem Path → empty payload, never 500.
        from types import SimpleNamespace

        fake = SimpleNamespace(path=None)
        client = TestClient(build_app(DashboardState(), instrument_store=fake))
        body = client.get("/api/v1/coverage/spot").json()
        assert body == {"headline": {"datasets": 0, "total_bars": 0}, "rows": []}


class TestWindowStatusApi:
    """The per-window SUCCESS / EMPTY / FAILED status table (DEC-WINDOW-STATUS-001).

    Covers ``/api/v1/coverage/windows``: the empty-sidecar default, a recorded
    window read back through the durable sidecar, the unwired-store 503, and the
    no-lake-root empty-payload (never a 500).
    """

    def test_windows_empty_when_no_sidecar(self, store: InstrumentStore) -> None:
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        resp = client.get("/api/v1/coverage/windows")
        assert resp.status_code == 200
        assert resp.json() == {"count": 0, "windows": []}

    def test_windows_reports_recorded_status(self, store: InstrumentStore, tmp_path: Path) -> None:
        from brutex.data.fetch_coverage import WindowKey
        from brutex.data.window_status import WindowStatusLog
        from brutex.data.window_status_store import save_window_status

        log = WindowStatusLog()
        log.record(
            WindowKey(
                market="dhan",
                security_id="NIFTY",
                interval="1day",
                start=date(2021, 1, 1),
                end=date(2021, 1, 1),
            ),
            raw_bars=5,
            error_type=None,
            bars_appended=5,
            now=datetime(2026, 6, 4, 9, 15, tzinfo=UTC),
        )
        # store.path is tmp_path/instruments.duckdb → lake root is tmp_path.
        save_window_status(log, tmp_path)
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        body = client.get("/api/v1/coverage/windows").json()
        assert body["count"] == 1
        (row,) = body["windows"]
        assert row["market"] == "dhan"
        assert row["symbol"] == "NIFTY"
        assert row["interval"] == "1day"
        assert row["status"] == "SUCCESS"
        assert row["bars"] == 5
        assert row["attempts"] == 1
        assert row["last_error"] is None

    def test_windows_503_when_store_unwired(self) -> None:
        client = TestClient(build_app(DashboardState(), instrument_store=None))
        assert client.get("/api/v1/coverage/windows").status_code == 503

    def test_windows_empty_payload_when_no_lake_root(self) -> None:
        from types import SimpleNamespace

        fake = SimpleNamespace(path=None)
        client = TestClient(build_app(DashboardState(), instrument_store=fake))
        assert client.get("/api/v1/coverage/windows").json() == {"count": 0, "windows": []}


class TestCoverageHelpers:
    def test_coverage_years_starts_2021(self) -> None:
        # Floor is 2021-01-01 (DEC-DHAN-PULL-SCOPE-001); the old 2020 floor was a
        # stale Groww-era carryover (DEC-DHAN-COVERAGE-RESCOPE-001).
        years = _coverage_years()
        assert years[0] == 2021
        assert years[-1] == datetime.now(UTC).year
        assert years == list(range(2021, datetime.now(UTC).year + 1))

    def test_render_coverage_html_has_nav_and_table(self) -> None:
        html = _render_coverage_html()
        assert "/coverage" in html
        assert "Underlying" in html

    def test_coverage_has_accessibility_polish(self) -> None:
        # Keyboard focus rings + reduce-motion honoured, same safe a11y polish as
        # /universe (DEC-DASHBOARD-A11Y-POLISH-001) — additive, no layout change.
        html = _render_coverage_html()
        assert ":focus-visible" in html
        assert "prefers-reduced-motion" in html
