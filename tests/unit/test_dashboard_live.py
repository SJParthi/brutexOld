"""Integration tests for the dashboard ``/live`` routes (control + lake browse).

The capture subprocess is faked through an injected :class:`LiveRunner` (no real
process, no signals), and the lake is seeded via ``brutex.live.lake``. Covers the
``/live`` page + ``/api/v1/live/*`` routes in :mod:`brutex.dashboard.app`.
"""

from __future__ import annotations

import json
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


def test_live_page_renders_query_builder_controls(tmp_path) -> None:
    """The /live page ships the query-builder UI (DEC-LIVE-UI-CONTROLS-001).

    Mirrors /db: a column picker, AND-combined filter rows, a sort dropdown, and
    Prev/Next paging — all wired to the live columns + query routes the JS calls.
    """
    client = TestClient(build_app(DashboardState(), live_runner=_runner(tmp_path)))
    text = client.get("/live").text
    # Column picker, filter rows, sort dropdown, + paging markup are present.
    assert 'class="qcol"' in text and 'class="colbox"' in text  # column picker
    assert 'class="fcol"' in text and 'class="fop"' in text and 'class="fval"' in text  # filters
    assert 'id="addf"' in text and 'id="sortcol"' in text and 'id="sortdir"' in text
    assert 'id="applyq"' in text  # apply-query button
    assert 'id="liveprev"' in text and 'id="livenext"' in text  # paging
    # The controls call the already-merged live columns + query routes.
    assert "/api/v1/live/columns/" in text and "/api/v1/live/query/" in text


def test_start_status_stop_cycle(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)  # runs/ + .brutex-logs/ resolve under tmp
    client = TestClient(build_app(DashboardState(), live_runner=_runner(tmp_path)))
    assert client.get("/api/v1/live/status").json()["running"] is False
    started = client.post("/api/v1/live/start").json()
    assert started["running"] is True and started["pid"] == 4242
    # The N-connection capture-all is spawned with --report/--manifest/--feed both.
    assert started["feed"] == "both"
    assert "capture-all" in started["argv"]
    assert "--report" in started["argv"] and "--manifest" in started["argv"]
    status = client.get("/api/v1/live/status").json()
    assert status["running"] is True and status["ticks_captured"] == 0
    assert client.post("/api/v1/live/stop").json()["running"] is False


def test_start_forwards_lake_root_to_capture(tmp_path, monkeypatch) -> None:
    """The Live-ON toggle passes the dashboard's READ lake root to the capture spawn.

    DEC-LIVE-REALTIME-FOLD-001: the capture must materialise the SAME lake root the
    dashboard reads (``instrument_store.path.parent``) or the live tables stay
    empty. This captures the spawned argv and asserts ``--lake-root`` is that exact
    root, and the argv still parses as a valid ``capture-all`` spawn.
    """
    monkeypatch.chdir(tmp_path)
    captured_argv: list[str] = []
    lake = tmp_path / "lake"
    (lake / "instruments").mkdir(parents=True)
    store = _FakeStore(lake / "instruments")  # lake root = .path.parent = <tmp>/lake
    runner = LiveRunner(
        state_path=tmp_path / "live.json",
        popen_factory=lambda argv, env: (captured_argv.extend(argv), _FakeProc(4242))[1],
        pid_alive=lambda p: True,
        clock=lambda: 1000.0,
        cred_env=lambda: {},
    )
    client = TestClient(build_app(DashboardState(), instrument_store=store, live_runner=runner))
    assert client.post("/api/v1/live/start").json()["running"] is True
    assert captured_argv[captured_argv.index("--lake-root") + 1] == str(lake)
    # The exact spawned argv parses cleanly through the real capture-all parser.
    from brutex.live.__main__ import _build_parser

    cli_args = captured_argv[captured_argv.index("brutex.live") + 1 :]
    parsed = _build_parser().parse_args(cli_args)
    assert parsed.command == "capture-all" and parsed.lake_root == str(lake)


def test_start_without_store_passes_no_lake_root(tmp_path, monkeypatch) -> None:
    """With no instrument store, the toggle omits --lake-root (no crash)."""
    monkeypatch.chdir(tmp_path)
    captured_argv: list[str] = []
    runner = LiveRunner(
        state_path=tmp_path / "live.json",
        popen_factory=lambda argv, env: (captured_argv.extend(argv), _FakeProc(4242))[1],
        pid_alive=lambda p: True,
        clock=lambda: 1000.0,
        cred_env=lambda: {},
    )
    client = TestClient(build_app(DashboardState(), live_runner=runner))
    assert client.post("/api/v1/live/start").json()["running"] is True
    assert "--lake-root" not in captured_argv


def test_live_connections_empty_when_no_report(tmp_path, monkeypatch) -> None:
    """No proof written yet → an empty connections payload + honest note."""
    monkeypatch.chdir(tmp_path)
    client = TestClient(build_app(DashboardState(), live_runner=_runner(tmp_path)))
    body = client.get("/api/v1/live/connections").json()
    assert body["connections"] == [] and body["note"]


def test_live_connections_parses_report(tmp_path, monkeypatch) -> None:
    """A written proof Markdown is parsed into the per-connection table."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runs").mkdir()
    (tmp_path / "runs" / "live-connections.md").write_text(
        "# Live-feed connection subscription plan\n\n"
        "**2 connections (1 spot + 1 leg), 1775 instruments**\n\n"
        "| Connection | Feed | Instruments |\n|---|---|---|\n"
        "| #0 | both | 775 |\n| #1 | ltp | 1000 |\n",
        encoding="utf-8",
    )
    client = TestClient(build_app(DashboardState(), live_runner=_runner(tmp_path)))
    body = client.get("/api/v1/live/connections").json()
    assert body["note"] is None
    assert body["connections"] == [
        {"index": 0, "feed": "both", "subscribed": 775},
        {"index": 1, "feed": "ltp", "subscribed": 1000},
    ]


def test_live_page_renders_connections_panel_and_feed_chip(tmp_path) -> None:
    """The /live page ships the per-connection panel + the feed chip element."""
    client = TestClient(build_app(DashboardState(), live_runner=_runner(tmp_path)))
    text = client.get("/live").text
    assert 'id="connGrid"' in text and 'id="connCount"' in text  # connection proof panel
    assert 'id="chipFeed"' in text  # the feed chip the status drives (no hardcoded ltp)
    assert "capture-all" in text  # the chip note reflects the N-connection spawn
    assert "/api/v1/live/connections" in text  # the page calls the connections route


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
    # Schema-when-empty: the column DEFINITIONS show even with no store
    # (DEC-LIVE-DB-SCHEMA-WHEN-EMPTY-001).
    assert "exchange_ts" in page["columns"] and "avg_price" in page["columns"]
    assert page["note"] == "schema only — no data yet (market closed / feed off)"
    # An unknown table is still a 400 even with no store.
    assert client.get("/api/v1/live/table/bogus").status_code == 400


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
    # no store → the canonical schema column names (schema-when-empty), incl. merged cols
    # (DEC-LIVE-DB-SCHEMA-WHEN-EMPTY-001); an unknown table is still a 400.
    client = TestClient(build_app(DashboardState(), live_runner=_runner(tmp_path)))
    no_store_cols = client.get("/api/v1/live/columns/live_ticks").json()["columns"]
    assert "exchange_ts" in no_store_cols and "payload_hash" in no_store_cols
    assert client.get("/api/v1/live/columns/bogus").status_code == 400
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
    # no store → a schema-only page (canonical columns + note), 0 rows; bad name → 400
    # (DEC-LIVE-DB-SCHEMA-WHEN-EMPTY-001).
    client = TestClient(build_app(DashboardState(), live_runner=_runner(tmp_path)))
    empty = client.get("/api/v1/live/query/live_ticks").json()
    assert empty["total"] == 0 and empty["rows"] == []
    assert "exchange_ts" in empty["columns"]
    assert empty["note"] == "schema only — no data yet (market closed / feed off)"
    assert client.get("/api/v1/live/query/bogus").status_code == 400
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


# ── N-connection plan + probe + ticks-flowing (DEC-LIVE-DASH-PROBE-001) ──


def test_live_plan_empty_when_no_manifest(tmp_path, monkeypatch) -> None:
    """No manifest written yet → an empty plan payload + honest note."""
    monkeypatch.chdir(tmp_path)
    client = TestClient(build_app(DashboardState(), live_runner=_runner(tmp_path)))
    body = client.get("/api/v1/live/plan").json()
    assert body["plan"]["connections"] == 0 and body["note"]
    assert body["probe"] is None and body["probe_note"]


def test_live_plan_reads_manifest(tmp_path, monkeypatch) -> None:
    """A written manifest is parsed into the planned connection count + probe."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runs").mkdir()
    (tmp_path / "runs" / "live-manifest.json").write_text(
        json.dumps(
            {
                "spot": [{"exchange": "NSE", "exchange_token": str(i)} for i in range(775)],
                "legs": [{"exchange": "NSE", "exchange_token": str(i)} for i in range(1000)],
                "meta": {
                    "plan": {"per_connection_cap": 1000},
                    "probe": {"max_held": 8, "failed_at": 9, "reached_ceiling": True},
                },
            }
        ),
        encoding="utf-8",
    )
    client = TestClient(build_app(DashboardState(), live_runner=_runner(tmp_path)))
    body = client.get("/api/v1/live/plan").json()
    assert body["note"] is None
    # 775 spot + 1000 legs at cap 1000 → ceil(1775/1000) = 2 connections.
    assert body["plan"] == {
        "connections": 2,
        "per_connection_cap": 1000,
        "spot": 775,
        "legs": 1000,
        "total": 1775,
    }
    assert body["probe"] == {"max_held": 8, "failed_at": 9, "reached_ceiling": True}


def test_live_ticks_flowing_without_store_is_empty(tmp_path) -> None:
    """No instrument store → an honest empty ticks-flowing payload (never fabricated)."""
    client = TestClient(build_app(DashboardState(), live_runner=_runner(tmp_path)))
    body = client.get("/api/v1/live/ticks-flowing").json()
    assert body["flowing"] is False and body["instruments"] == [] and body["note"]


def test_live_ticks_flowing_reads_the_lake(tmp_path) -> None:
    """With a store + seeded lake the freshness payload reflects the ACTUAL lake state."""
    lake = tmp_path / "lake"
    (lake / "instruments").mkdir(parents=True)
    _seed_lake(lake)  # one NSE-NIFTY tick at 2026-06-15 03:45 UTC
    store = _FakeStore(lake / "instruments")
    client = TestClient(
        build_app(DashboardState(), instrument_store=store, live_runner=_runner(tmp_path))
    )
    body = client.get("/api/v1/live/ticks-flowing").json()
    assert body["total_ticks"] == 1 and len(body["instruments"]) == 1
    inst = body["instruments"][0]
    assert inst["groww_symbol"] == "NSE-NIFTY"
    # The rendered last-tick time is clean IST (no +05:30 offset).
    assert "+05:30" not in inst["last_recv_ts"] and inst["last_recv_ts"].startswith("2026-06-15 ")


def test_live_page_renders_plan_and_flow_panels(tmp_path) -> None:
    """The /live page ships the plan band + the ticks-flowing panel + their routes."""
    client = TestClient(build_app(DashboardState(), live_runner=_runner(tmp_path)))
    text = client.get("/live").text
    assert 'id="planBand"' in text and 'id="planConns"' in text and 'id="planProbe"' in text
    assert 'id="flowGrid"' in text and 'id="flowCount"' in text and 'id="cFlow"' in text
    assert "/api/v1/live/plan" in text and "/api/v1/live/ticks-flowing" in text
