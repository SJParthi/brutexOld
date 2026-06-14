"""Integration tests for the dashboard ``/live`` routes (control + lake browse).

The capture subprocess is faked through an injected :class:`LiveRunner` (no real
process, no signals), and the lake is seeded via ``brutex.live.lake``. Covers the
``/live`` page + ``/api/v1/live/*`` routes in :mod:`brutex.dashboard.app`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

pytest.importorskip("fastapi", reason="dashboard needs the [ui] extra")
pytest.importorskip("httpx", reason="TestClient needs httpx")

from brutex.dashboard.app import DashboardState, build_app
from brutex.dashboard.live_runner import LiveRunner
from brutex.data.models import CandleBar
from brutex.live.lake import write_live_candles, write_live_ticks
from brutex.live.store import TickRecord
from fastapi.testclient import TestClient


class _FakeProc:
    def __init__(self, pid: int) -> None:
        self.pid = pid

    def poll(self) -> int | None:
        return None  # always "running" for the route tests


class _FakeStore:
    """Minimal stand-in: the routes only read ``.path`` (lake root = its parent)."""

    def __init__(self, path) -> None:
        self.path = path


def _runner(tmp_path) -> LiveRunner:
    return LiveRunner(
        state_path=tmp_path / "live.json",
        popen_factory=lambda argv, env: _FakeProc(4242),
        pid_alive=lambda p: True,
        killer=lambda p, s: None,
        clock=lambda: 1000.0,
        cred_env=lambda: {},
    )


def _seed_lake(lake) -> None:
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


def test_live_page_renders(tmp_path) -> None:
    client = TestClient(build_app(DashboardState(), live_runner=_runner(tmp_path)))
    resp = client.get("/live")
    assert resp.status_code == 200
    assert "Live feed" in resp.text and 'href="/live" aria-current="page"' in resp.text


def test_start_status_stop_cycle(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)  # runs/ + .brutex-logs/ resolve under tmp
    client = TestClient(build_app(DashboardState(), live_runner=_runner(tmp_path)))
    assert client.get("/api/v1/live/status").json()["running"] is False
    started = client.post("/api/v1/live/start").json()
    assert started["running"] is True and started["pid"] == 4242
    status = client.get("/api/v1/live/status").json()
    assert status["running"] is True and status["ticks_captured"] == 0
    assert client.post("/api/v1/live/stop").json()["running"] is False


def test_status_counts_jsonl_ticks(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runs").mkdir()
    (tmp_path / "runs" / "live-ticks.jsonl").write_text(
        '{"seq":0}\n{"seq":1}\n\n', encoding="utf-8"
    )
    client = TestClient(build_app(DashboardState(), live_runner=_runner(tmp_path)))
    assert client.get("/api/v1/live/status").json()["ticks_captured"] == 2


def test_tables_without_store_are_empty(tmp_path) -> None:
    client = TestClient(build_app(DashboardState(), live_runner=_runner(tmp_path)))
    tabs = client.get("/api/v1/live/tables").json()["tables"]
    assert {t["table"]: t["row_count"] for t in tabs} == {"live_ticks": 0, "live_candles_1m": 0}
    page = client.get("/api/v1/live/table/live_ticks").json()
    assert page["total"] == 0 and page["rows"] == []


def test_tables_with_store_read_the_lake(tmp_path) -> None:
    lake = tmp_path / "lake"
    (lake / "instruments").mkdir(parents=True)
    _seed_lake(lake)
    store = _FakeStore(lake / "instruments")  # lake root = .path.parent
    client = TestClient(
        build_app(DashboardState(), instrument_store=store, live_runner=_runner(tmp_path))
    )
    tabs = client.get("/api/v1/live/tables").json()["tables"]
    assert {t["table"]: t["row_count"] for t in tabs} == {"live_ticks": 1, "live_candles_1m": 1}
    page = client.get("/api/v1/live/table/live_ticks?limit=5").json()
    assert page["total"] == 1 and "instrument_key" in page["columns"]


def test_table_bad_name_is_400(tmp_path) -> None:
    lake = tmp_path / "lake"
    (lake / "instruments").mkdir(parents=True)
    store = _FakeStore(lake / "instruments")
    client = TestClient(
        build_app(DashboardState(), instrument_store=store, live_runner=_runner(tmp_path))
    )
    assert client.get("/api/v1/live/table/bogus").status_code == 400


def test_live_columns_route(tmp_path) -> None:
    # no store → empty columns
    client = TestClient(build_app(DashboardState(), live_runner=_runner(tmp_path)))
    assert client.get("/api/v1/live/columns/live_ticks").json()["columns"] == []
    # with store → real columns; bad name → 400
    lake = tmp_path / "lake"
    (lake / "instruments").mkdir(parents=True)
    _seed_lake(lake)
    store = _FakeStore(lake / "instruments")
    c2 = TestClient(
        build_app(DashboardState(), instrument_store=store, live_runner=_runner(tmp_path))
    )
    assert "instrument_key" in c2.get("/api/v1/live/columns/live_ticks").json()["columns"]
    assert c2.get("/api/v1/live/columns/bogus").status_code == 400


def test_live_query_route(tmp_path) -> None:
    # no store → empty page
    client = TestClient(build_app(DashboardState(), live_runner=_runner(tmp_path)))
    empty = client.get("/api/v1/live/query/live_ticks").json()
    assert empty["total"] == 0 and empty["rows"] == []
    # with store → filter + project + sort
    lake = tmp_path / "lake"
    (lake / "instruments").mkdir(parents=True)
    _seed_lake(lake)
    store = _FakeStore(lake / "instruments")
    c2 = TestClient(
        build_app(DashboardState(), instrument_store=store, live_runner=_runner(tmp_path))
    )
    page = c2.get(
        "/api/v1/live/query/live_ticks"
        "?f=groww_symbol:eq:NSE-NIFTY&column=groww_symbol&order_by=seq&desc=false"
    ).json()
    assert page["total"] == 1 and page["columns"] == ["groww_symbol"]
    assert page["rows"][0][0] == "NSE-NIFTY"
    assert c2.get("/api/v1/live/query/bogus").status_code == 400
