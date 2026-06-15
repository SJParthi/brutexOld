"""Tests for the LIVE-lake browse routes surfaced under ``/db`` (DEC-DB-LIVE-TABLES-001).

The two live Parquet tables (``live_ticks`` + ``live_candles_1m``) are surfaced
PRIMARILY on the /db Database page (operator request) via a thin ``/api/v1/db/live/*``
route family that delegates to the SAME 100%-tested
:mod:`brutex.dashboard.live_lake_view` reader as the /live page (no duplicate
reader). These tests pin the /db-side routes: the table list (with counts), a
browse page (columns + rows) when the lake holds partitions, an honest empty
page when absent, and the allowlist 400 on an unknown table name. The lake is
seeded via ``brutex.live.lake`` — the dashboard never imports ``brutex.live``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="dashboard needs the [ui] extra")
pytest.importorskip("httpx", reason="TestClient needs httpx")

from brutex.dashboard.app import DashboardState, build_app
from brutex.data.models import CandleBar
from brutex.live.lake import write_live_candles, write_live_ticks
from brutex.live.store import TickRecord
from fastapi.testclient import TestClient


class _FakeStore:
    """Minimal stand-in: the routes only read ``.path`` (lake root = its parent)."""

    def __init__(self, path: Path) -> None:
        self.path = path


def _seed_lake(lake: Path) -> None:
    """Write a tiny ``live_ticks`` + ``live_candles_1m`` partition into *lake*."""
    rec = TickRecord(
        seq=0,
        recv_ts=datetime(2026, 6, 15, 3, 45, tzinfo=UTC),
        snapshot={"NSE:IDX:NIFTY": 22500.0},
        recv_ns=1,
    )
    write_live_ticks([rec], lake_root=lake, capture_run_id="r1", feed_kind="index")
    bar = CandleBar(
        timestamp=datetime(2026, 6, 15, 3, 45, tzinfo=UTC),
        open=Decimal("1"),
        high=Decimal("1"),
        low=Decimal("1"),
        close=Decimal("1"),
        volume=0,
        open_interest=None,
    )
    write_live_candles({"NSE:IDX:NIFTY": [bar]}, lake_root=lake, built_run_id="r1")


def _wired_client(tmp_path: Path) -> TestClient:
    """A dashboard wired to a seeded lake (lake root = ``store.path.parent``)."""
    lake = tmp_path / "lake"
    (lake / "instruments").mkdir(parents=True)
    _seed_lake(lake)
    store = _FakeStore(lake / "instruments")
    return TestClient(build_app(DashboardState(), instrument_store=store))


def test_db_live_tables_without_store_are_zero(tmp_path: Path) -> None:
    """No store wired → both live tables reported with row_count 0 (never a 500)."""
    client = TestClient(build_app(DashboardState()))
    tabs = client.get("/api/v1/db/live/tables").json()["tables"]
    assert {t["table"]: t["row_count"] for t in tabs} == {
        "live_ticks": 0,
        "live_candles_1m": 0,
    }


def test_db_live_table_empty_when_no_store(tmp_path: Path) -> None:
    """No store → an honest empty page for a live table (never a 500)."""
    client = TestClient(build_app(DashboardState()))
    page = client.get("/api/v1/db/live/table/live_ticks").json()
    assert page["total"] == 0 and page["rows"] == [] and page["columns"] == []


def test_db_live_tables_with_store_read_the_lake(tmp_path: Path) -> None:
    """A seeded lake → /db lists BOTH live tables with their real row counts."""
    client = _wired_client(tmp_path)
    tabs = client.get("/api/v1/db/live/tables").json()["tables"]
    assert {t["table"]: t["row_count"] for t in tabs} == {
        "live_ticks": 1,
        "live_candles_1m": 1,
    }


def test_db_live_table_browse_returns_columns_and_rows(tmp_path: Path) -> None:
    """Browsing a live table via /db returns the live columns + rows."""
    client = _wired_client(tmp_path)
    page = client.get("/api/v1/db/live/table/live_ticks?limit=5").json()
    assert page["total"] == 1
    assert "instrument_key" in page["columns"]
    assert "groww_symbol" in page["columns"]
    candles = client.get("/api/v1/db/live/table/live_candles_1m").json()
    assert candles["total"] == 1 and "close" in candles["columns"]


def test_db_live_table_bad_name_is_400(tmp_path: Path) -> None:
    """An unknown live table name is rejected (allowlist 400) — never read."""
    client = _wired_client(tmp_path)
    assert client.get("/api/v1/db/live/table/bogus").status_code == 400


def test_db_live_columns_route(tmp_path: Path) -> None:
    """The /db live columns picker: empty without a store, real cols with one, 400 on a bad name."""
    no_store = TestClient(build_app(DashboardState()))
    assert no_store.get("/api/v1/db/live/columns/live_ticks").json()["columns"] == []
    client = _wired_client(tmp_path)
    assert "instrument_key" in client.get("/api/v1/db/live/columns/live_ticks").json()["columns"]
    assert client.get("/api/v1/db/live/columns/bogus").status_code == 400


def test_db_live_query_route(tmp_path: Path) -> None:
    """The /db live structured query: empty without a store, filter+project+sort with one, 400 on bad name."""
    no_store = TestClient(build_app(DashboardState()))
    empty = no_store.get("/api/v1/db/live/query/live_ticks").json()
    assert empty["total"] == 0 and empty["rows"] == []
    client = _wired_client(tmp_path)
    page = client.get(
        "/api/v1/db/live/query/live_ticks"
        "?f=groww_symbol:eq:NSE-NIFTY&column=groww_symbol&order_by=seq&desc=false"
    ).json()
    assert page["total"] == 1 and page["columns"] == ["groww_symbol"]
    assert page["rows"][0][0] == "NSE-NIFTY"
    assert client.get("/api/v1/db/live/query/bogus").status_code == 400


def test_db_live_query_bad_filter_is_400(tmp_path: Path) -> None:
    """A malformed ``f=`` token is rejected (_parse_filters 400) before the read."""
    client = _wired_client(tmp_path)
    assert client.get("/api/v1/db/live/query/live_ticks?f=bad_token").status_code == 400


def test_db_page_renders_live_lake_section(tmp_path: Path) -> None:
    """The /db page itself ships the LIVE LAKE section + the /db-side live routes."""
    client = _wired_client(tmp_path)
    text = client.get("/db").text
    assert "Live lake" in text
    assert "/api/v1/db/live" in text
    assert 'const LIVE_TABLES = ["live_ticks", "live_candles_1m"];' in text
