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
        "/api/v1/reports/preopen.zip",
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


def _write_ampersand_symbol(tmp_path: Path) -> None:
    """Seed the lake with M&M (a real ``&`` underlying) — both report intervals."""
    writer = ParquetWriter(lake_root=tmp_path)
    writer.write(
        exchange="NSE",
        segment="CASH",
        symbol="M&M",
        interval="1day",
        bars=[
            _bar(datetime(2024, 3, 1, 3, 45, tzinfo=UTC), open_="2900", close="2950.50"),
            _bar(datetime(2024, 3, 4, 3, 45, tzinfo=UTC), open_="2960", close="2975"),
        ],
    )
    writer.write(
        exchange="NSE",
        segment="CASH",
        symbol="M&M",
        interval="1minute",
        bars=[_bar(datetime(2024, 3, 4, 3, 45, tzinfo=UTC), open_="2960.00", close="2965")],
    )


def test_xlsx_all_symbols_with_ampersand_no_500(tmp_path: Path) -> None:
    # Reproduces the reported crash: an "all symbols" download (no ``?symbols=``)
    # over a lake holding M&M used to 500 with ``ValueError: illegal candle
    # symbol 'M&M'``. It must now stream a valid workbook with M&M included.
    _write_ampersand_symbol(tmp_path)
    resp = _client(tmp_path).get("/api/v1/reports/preopen.xlsx?from=2024-01-01&to=2024-12-31")
    assert resp.status_code == 200  # was 500 before DEC-PREOPEN-AMPERSAND-001
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        sheet = zf.read("xl/worksheets/sheet1.xml").decode("utf-8")
    assert "M&amp;M" in sheet  # the once-crashing symbol now renders (XML-escaped)
    assert "COALINDIA" in sheet  # and the rest of the report is intact


def test_xlsx_title_carries_requested_range(tmp_path: Path) -> None:
    # The row-1 banner states the requested date range (how a multi-day range
    # report conveys its dates beyond the per-row DATE column).
    resp = _client(tmp_path).get("/api/v1/reports/preopen.xlsx?from=2024-01-01&to=2024-12-31")
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        sheet = zf.read("xl/worksheets/sheet1.xml").decode("utf-8")
    assert "Pre-Open Market: 2024-01-01 to 2024-12-31" in sheet


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
    assert "Download .zip" in resp.text  # Part B: the per-year-zip download


def test_reports_page_has_index_constituent_dropdown(tmp_path: Path) -> None:
    # The index quick-select (DEC-REPORTS-INDEX-DROPDOWN-001): a dropdown that
    # adds an index's constituent stocks in one go, wired to the (existing,
    # cache-backed) /universe constituents endpoint — instead of clicking each
    # stock. Guards the control + its endpoint wiring against silent regression.
    html = _client(tmp_path).get("/reports").text
    assert 'id="idxpick"' in html, "the index quick-select dropdown is missing"
    assert "/api/v1/universe/index-constituents?symbol=" in html, (
        "the dropdown must call the constituents endpoint to add an index's stocks"
    )


def test_reports_page_has_category_toggle_and_minpct(tmp_path: Path) -> None:
    # The two-category restructure (DEC-REPORTS-EXCEL-CATEGORIES-001): a mode
    # toggle (All symbols / Top mover), a "Minimum % change" number input, and a
    # Download wired to the new per-year-zip endpoint with mode + min_pct params.
    html = _client(tmp_path).get("/reports").text
    assert 'name="mode"' in html, "the All-symbols / Top-mover toggle is missing"
    assert 'value="all"' in html and 'value="topmover"' in html, "both modes must be selectable"
    assert 'id="minpct"' in html and 'type="number"' in html, "the minimum-% input is missing"
    assert "/api/v1/reports/preopen.zip?" in html, "Download must hit the new .zip endpoint"
    assert "min_pct" in html and "mode" in html, "the mode + min_pct params must be wired"


def _open_zip_member(blob: bytes, member: str) -> str:
    """Read one ``.xlsx`` member out of the report ``.zip`` → its sheet1 XML."""
    with zipfile.ZipFile(io.BytesIO(blob)) as outer:
        inner = outer.read(member)
    with zipfile.ZipFile(io.BytesIO(inner)) as wb:
        return wb.read("xl/worksheets/sheet1.xml").decode("utf-8")


def test_zip_all_mode_download(tmp_path: Path) -> None:
    resp = _client(tmp_path).get(
        "/api/v1/reports/preopen.zip?from=2024-01-01&to=2024-12-31&mode=all"
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    assert "preopen_report.zip" in resp.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        names = zf.namelist()
    assert names == ["preopen_all_2024.xlsx"]  # one .xlsx for the single year
    assert "COALINDIA" in _open_zip_member(resp.content, "preopen_all_2024.xlsx")


def test_zip_topmover_mode_download(tmp_path: Path) -> None:
    resp = _client(tmp_path).get(
        "/api/v1/reports/preopen.zip?from=2024-01-01&to=2024-12-31&mode=topmover&min_pct=1"
    )
    assert resp.status_code == 200
    assert _open_zip_member(resp.content, "preopen_topmover_2024.xlsx").count("COALINDIA") == 1


def test_zip_default_mode_is_all(tmp_path: Path) -> None:
    resp = _client(tmp_path).get("/api/v1/reports/preopen.zip?from=2024-01-01&to=2024-12-31")
    assert resp.status_code == 200
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        assert zf.namelist() == ["preopen_all_2024.xlsx"]


def test_zip_bad_mode_is_400(tmp_path: Path) -> None:
    resp = _client(tmp_path).get(
        "/api/v1/reports/preopen.zip?from=2024-01-01&to=2024-12-31&mode=bogus"
    )
    assert resp.status_code == 400


def test_zip_negative_min_pct_is_400(tmp_path: Path) -> None:
    resp = _client(tmp_path).get(
        "/api/v1/reports/preopen.zip?from=2024-01-01&to=2024-12-31&mode=topmover&min_pct=-1"
    )
    assert resp.status_code == 400


def test_zip_bad_date_is_400(tmp_path: Path) -> None:
    resp = _client(tmp_path).get("/api/v1/reports/preopen.zip?from=nope&to=2024-12-31")
    assert resp.status_code == 400


def test_zip_inverted_range_is_400(tmp_path: Path) -> None:
    resp = _client(tmp_path).get("/api/v1/reports/preopen.zip?from=2024-12-31&to=2024-01-01")
    assert resp.status_code == 400
