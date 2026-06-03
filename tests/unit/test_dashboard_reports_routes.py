"""Functional tests for the pre-open report routes (DEC-PREOPEN-REPORT-001).

Drives the real FastAPI app via ``TestClient`` over a temp lake holding one
symbol (COALINDIA, matching the operator's pre-open CSV) — confirms the symbol
picker, the ``.xlsx`` download (valid workbook, NSE-matching numbers), the
date-validation 400s, and the ``/reports`` page. Skipped cleanly without the
``[ui]`` extra.
"""

from __future__ import annotations

import io
import zipfile
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="dashboard routes need the [ui] extra")
pytest.importorskip("httpx", reason="TestClient needs httpx")

from brutex.dashboard.app import DashboardState, build_app
from brutex.data.instrument_store import InstrumentStore
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from fastapi.testclient import TestClient

_EXPECTED_PATHS = frozenset(
    {
        "/api/v1/reports/preopen/symbols",
        "/api/v1/reports/preopen.xlsx",
        "/reports",
    }
)


def _bar(ts: datetime, *, open_: str, close: str) -> CandleBar:
    return CandleBar(
        timestamp=ts,
        open=Decimal(open_),
        high=Decimal(close),
        low=Decimal(open_),
        close=Decimal(close),
        volume=10,
        open_interest=None,
    )


def _client(tmp_path: Path, *, with_data: bool = True) -> TestClient:
    if with_data:
        writer = ParquetWriter(lake_root=tmp_path)
        writer.write(
            exchange="NSE",
            segment="CASH",
            symbol="COALINDIA",
            interval="1day",
            bars=[
                _bar(datetime(2024, 3, 1, 3, 45, tzinfo=UTC), open_="455", close="458.15"),
                _bar(datetime(2024, 3, 4, 3, 45, tzinfo=UTC), open_="429", close="431.55"),
            ],
        )
        writer.write(
            exchange="NSE",
            segment="CASH",
            symbol="COALINDIA",
            interval="1minute",
            bars=[_bar(datetime(2024, 3, 4, 3, 45, tzinfo=UTC), open_="429.00", close="431.55")],
        )
    store = InstrumentStore.open(tmp_path / "instruments.duckdb")
    return TestClient(build_app(DashboardState(), instrument_store=store))


def test_report_routes_registered(tmp_path: Path) -> None:
    app = build_app(DashboardState(), instrument_store=None)
    paths = {route.path for route in app.routes if hasattr(route, "path")}
    assert paths >= _EXPECTED_PATHS


def test_symbols_lists_report_eligible(tmp_path: Path) -> None:
    resp = _client(tmp_path).get("/api/v1/reports/preopen/symbols")
    assert resp.status_code == 200
    symbols = [s["symbol"] for s in resp.json()["symbols"]]
    assert symbols == ["COALINDIA"]


def test_xlsx_download_matches_nse(tmp_path: Path) -> None:
    resp = _client(tmp_path).get("/api/v1/reports/preopen.xlsx?from=2024-01-01&to=2024-12-31")
    assert resp.status_code == 200
    assert "spreadsheetml.sheet" in resp.headers["content-type"]
    assert "attachment" in resp.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        sheet = zf.read("xl/worksheets/sheet1.xml").decode("utf-8")
    assert "COALINDIA" in sheet
    assert "458.15" in sheet  # PREV. CLOSE
    assert "-6.36" in sheet  # %CHNG matches NSE


def test_xlsx_symbol_filter(tmp_path: Path) -> None:
    # A filter that excludes the only symbol → a valid header-only workbook.
    resp = _client(tmp_path).get(
        "/api/v1/reports/preopen.xlsx?from=2024-01-01&to=2024-12-31&symbols=RELIANCE"
    )
    assert resp.status_code == 200
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        sheet = zf.read("xl/worksheets/sheet1.xml").decode("utf-8")
    assert "COALINDIA" not in sheet


def test_xlsx_bad_date_is_400(tmp_path: Path) -> None:
    resp = _client(tmp_path).get("/api/v1/reports/preopen.xlsx?from=not-a-date&to=2024-12-31")
    assert resp.status_code == 400


def test_xlsx_inverted_range_is_400(tmp_path: Path) -> None:
    resp = _client(tmp_path).get("/api/v1/reports/preopen.xlsx?from=2024-12-31&to=2024-01-01")
    assert resp.status_code == 400


def test_reports_page_renders(tmp_path: Path) -> None:
    resp = _client(tmp_path).get("/reports")
    assert resp.status_code == 200
    assert "Pre-Open" in resp.text
    assert "Download .xlsx" in resp.text
