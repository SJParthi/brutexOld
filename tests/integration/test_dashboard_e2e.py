"""End-to-end integration tests for the FastAPI dashboard + WebSocket.

These tests install FastAPI + httpx and make real HTTP/WebSocket calls
against the app built by :func:`brutex.dashboard.app.build_app`. They
verify that every route the dashboard serves is wired correctly:

* ``GET /`` — the inline HTML index
* ``GET /api/v1/state`` — JSON snapshot of :class:`DashboardState`
* ``GET /api/v1/events`` — JSON event ring buffer
* ``GET /metrics`` — Prometheus text format
* ``WS /ws`` — streaming state updates every 500 ms

This closes the honesty gap from PR #1 where ``tests/unit/test_dashboard.py``
only exercised :class:`DashboardState` (the pure model) and never
actually invoked any FastAPI route or WebSocket.

Skipped automatically if ``fastapi`` / ``httpx`` aren't installed so
it doesn't break minimal runs.
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime

import pytest

# Skip the whole module if fastapi isn't installed. Guard on `httpx2`
# (not `httpx`) because Starlette's TestClient imports `httpx2` as its HTTP
# backend — guarding on bare `httpx` would let the module run without httpx2
# and raise a RuntimeError instead of skipping (DEC-DASHBOARD-CI-UNMASK-001).
pytest.importorskip("fastapi")
pytest.importorskip("httpx2")

from brutex.dashboard.app import DashboardState, build_app
from brutex.orchestrator.events import (
    IngestPhase,
    PipelineEvent,
    PipelineEventKind,
)
from fastapi.testclient import TestClient

# ────────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────────


@pytest.fixture
def state() -> DashboardState:
    s = DashboardState()
    s.on_pipeline_event(
        PipelineEvent(
            kind=PipelineEventKind.PHASE_START,
            phase=IngestPhase.A_INSTRUMENTS,
            run_id="test-run",
            at=datetime.now(UTC),
            message="phase starting",
        )
    )
    return s


@pytest.fixture
def client(state: DashboardState) -> Generator[TestClient]:
    app = build_app(state)
    with TestClient(app) as c:
        yield c


# ────────────────────────────────────────────────────────────────────────
# HTTP routes
# ────────────────────────────────────────────────────────────────────────


class TestHttpRoutes:
    def test_index_returns_html(self, client: TestClient) -> None:
        r = client.get("/")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/html")
        body = r.text
        assert "BruteX" in body
        # Per locked design DEC-DASHBOARD-LIVE-FINALE (MIGRATION.md #234) the
        # index drives live updates via a /api/v1/state poller (every 2s), not
        # an inline /ws socket — the /ws route still exists and is exercised by
        # the websocket tests below.
        assert "/api/v1/state" in body

    def test_api_state_returns_json(self, client: TestClient, state: DashboardState) -> None:
        r = client.get("/api/v1/state")
        assert r.status_code == 200
        data = r.json()
        assert "phases" in data
        # Phase A should be tracked because we fired PHASE_START in the fixture
        phase_names = {p["phase"] for p in data["phases"]}
        assert IngestPhase.A_INSTRUMENTS.value in phase_names

    def test_api_events_returns_json(self, client: TestClient) -> None:
        r = client.get("/api/v1/events")
        assert r.status_code == 200
        data = r.json()
        assert "events" in data
        assert isinstance(data["events"], list)
        # One event was fired in the fixture setup
        assert len(data["events"]) >= 1

    def test_metrics_returns_prometheus_text(self, client: TestClient) -> None:
        r = client.get("/metrics")
        assert r.status_code == 200
        assert "text/plain" in r.headers["content-type"]
        text = r.text
        # Prometheus text format lines start with "# HELP", "# TYPE",
        # or a metric-name line. Empty registry is also valid.
        for line in text.splitlines():
            if not line:
                continue
            assert line.startswith("#") or line[0].isalpha(), f"bad Prometheus line: {line!r}"

    def test_unknown_route_is_404(self, client: TestClient) -> None:
        r = client.get("/does-not-exist")
        assert r.status_code == 404

    def test_logs_webpage_removed_is_404(self, client: TestClient) -> None:
        # The /logs webpage was removed (DEC-DASHBOARD-LOGS-REMOVE-001); the
        # /api/v1/logs data endpoint is intentionally kept.
        assert client.get("/logs").status_code == 404
        assert client.get("/api/v1/logs").status_code == 200


# ────────────────────────────────────────────────────────────────────────
# WebSocket streaming
# ────────────────────────────────────────────────────────────────────────


class TestWebSocketStream:
    def test_ws_sends_initial_state(self, client: TestClient, state: DashboardState) -> None:
        """Connecting to /ws must immediately receive a state snapshot."""
        with client.websocket_connect("/ws") as socket:
            payload = socket.receive_json()
            assert "phases" in payload
            phase_names = {p["phase"] for p in payload["phases"]}
            assert IngestPhase.A_INSTRUMENTS.value in phase_names

    def test_ws_streams_multiple_frames(self, client: TestClient) -> None:
        """/ws must push a new frame every ~500ms."""
        with client.websocket_connect("/ws") as socket:
            first = socket.receive_json()
            second = socket.receive_json()
            third = socket.receive_json()
            for frame in (first, second, third):
                assert "phases" in frame

    def test_ws_reflects_state_updates(self, client: TestClient, state: DashboardState) -> None:
        """Firing a new pipeline event between two WS frames must be visible."""
        with client.websocket_connect("/ws") as socket:
            _first = socket.receive_json()
            # Fire a new event
            state.on_pipeline_event(
                PipelineEvent(
                    kind=PipelineEventKind.PHASE_START,
                    phase=IngestPhase.B_EXPIRIES,
                    run_id="test-run",
                    at=datetime.now(UTC),
                    message="phase B starting",
                )
            )
            # Next frame (within ~500 ms) should include phase B
            next_frame = socket.receive_json()
            phase_names = {p["phase"] for p in next_frame["phases"]}
            assert IngestPhase.B_EXPIRIES.value in phase_names


# ────────────────────────────────────────────────────────────────────────
# Prometheus /metrics format validation via prometheus_client.parser
# ────────────────────────────────────────────────────────────────────────


class TestPrometheusFormat:
    def test_metrics_parses_as_prometheus(self, client: TestClient) -> None:
        """The /metrics response must be parseable by any Prometheus
        text-format parser. We use prometheus_client if available,
        otherwise fall back to structural validation.
        """
        r = client.get("/metrics")
        text = r.text

        try:
            from prometheus_client.parser import (
                text_string_to_metric_families,
            )
        except ImportError:
            # prometheus_client isn't a hard dep; do basic structural check.
            for line in text.splitlines():
                if line and not line.startswith("#"):
                    # Metric line — must have at least name and value
                    parts = line.rsplit(" ", 1)
                    assert len(parts) == 2, f"malformed prom line: {line!r}"
            return

        # Full parse
        families = list(text_string_to_metric_families(text))
        for family in families:
            assert family.name
            assert family.type in {
                "counter",
                "gauge",
                "histogram",
                "summary",
                "untyped",
                "info",
                "stateset",
                "unknown",
            }


# ────────────────────────────────────────────────────────────────────────
# Offloaded read routes (DEC-DASHBOARD-FASTREAD-001)
# ────────────────────────────────────────────────────────────────────────
#
# These verify that wrapping each blocking db_view/store read in
# `run_in_threadpool(...)` did not change the JSON contract — the offloaded
# routes must still return correct payloads against a real wired store. A real
# `InstrumentStore` needs duckdb, so the section is guarded + fixtured.


class TestOffloadedReadRoutes:
    """The threadpool-offloaded read routes still return correct JSON."""

    @pytest.fixture
    def wired_client(self, tmp_path: object) -> Generator[TestClient]:
        from datetime import date
        from decimal import Decimal
        from pathlib import Path

        pytest.importorskip("duckdb")
        from brutex.data.instrument_store import InstrumentStore
        from brutex.models import Exchange, Instrument, InstrumentType, Segment

        assert isinstance(tmp_path, Path)
        store = InstrumentStore.open(tmp_path / "instruments.duckdb")
        store.upsert_many(
            [
                Instrument(
                    exch_id=Exchange.NSE,
                    security_id=13,
                    symbol_name="NIFTY",
                    display_name="NIFTY",
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
            ],
            snapshot_date=date(2024, 1, 2),
            run_id="r1",
            csv_source_sha256="0" * 64,
        )
        app = build_app(DashboardState(), instrument_store=store)
        with TestClient(app) as c:
            yield c
        store.close()

    def test_welcome_landing_page_serves_html(self, wired_client: TestClient) -> None:
        # The premium animated landing route returns the self-contained HTML page
        # (DEC-DASHBOARD-WELCOME-001) — additive, never touches the v29 "/" dashboard.
        r = wired_client.get("/welcome")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert "<title>BruteX" in r.text
        assert 'href="/universe"' in r.text

    def test_db_tables_offloaded(self, wired_client: TestClient) -> None:
        r = wired_client.get("/api/v1/db/tables")
        assert r.status_code == 200
        names = {t["table"] for t in r.json()["tables"]}
        assert "instruments" in names

    def test_db_table_page_offloaded(self, wired_client: TestClient) -> None:
        r = wired_client.get("/api/v1/db/table/instruments?limit=10")
        assert r.status_code == 200
        body = r.json()
        assert body["table"] == "instruments"
        assert body["total"] == 1

    def test_db_table_bad_name_still_maps_to_400(self, wired_client: TestClient) -> None:
        # The try/except ValueError → HTTPException(400) survives the offload.
        r = wired_client.get("/api/v1/db/table/not_a_real_table")
        assert r.status_code == 400

    def test_db_columns_offloaded(self, wired_client: TestClient) -> None:
        r = wired_client.get("/api/v1/db/columns/instruments")
        assert r.status_code == 200
        assert "security_id" in r.json()["columns"]

    def test_db_query_offloaded(self, wired_client: TestClient) -> None:
        r = wired_client.get("/api/v1/db/query/instruments?limit=5")
        assert r.status_code == 200
        assert r.json()["total"] == 1

    def test_db_query_bad_filter_maps_to_400(self, wired_client: TestClient) -> None:
        # _parse_filters rejects a 2-token filter BEFORE the offload (400).
        r = wired_client.get("/api/v1/db/query/instruments?f=bad_token")
        assert r.status_code == 400

    def test_instruments_stats_offloaded(self, wired_client: TestClient) -> None:
        r = wired_client.get("/api/v1/instruments/stats")
        assert r.status_code == 200
        assert r.json()["totals"]["instruments"] == 1

    def test_instruments_list_offloaded(self, wired_client: TestClient) -> None:
        r = wired_client.get("/api/v1/instruments?limit=10")
        assert r.status_code == 200
        symbols = {i["symbol_name"] for i in r.json()["instruments"]}
        assert "NIFTY" in symbols

    def test_coverage_offloaded(self, wired_client: TestClient) -> None:
        r = wired_client.get("/api/v1/coverage")
        assert r.status_code == 200
        assert "headline" in r.json()

    def test_coverage_spot_offloaded_and_cached(self, wired_client: TestClient) -> None:
        # Two polls — both 200; the second is served from the SummaryCache.
        first = wired_client.get("/api/v1/coverage/spot")
        second = wired_client.get("/api/v1/coverage/spot")
        assert first.status_code == second.status_code == 200
        assert "headline" in first.json()

    def test_coverage_options_offloaded(self, wired_client: TestClient) -> None:
        r = wired_client.get("/api/v1/coverage/options")
        assert r.status_code == 200
        assert "headline" in r.json()

    def test_expiries_offloaded_empty_lake(self, wired_client: TestClient) -> None:
        # No options in the lake → empty list (the offloaded list_option_series).
        r = wired_client.get("/api/v1/expiries")
        assert r.status_code == 200
        assert r.json()["expiries"] == []

    def test_contracts_offloaded_empty_lake(self, wired_client: TestClient) -> None:
        r = wired_client.get("/api/v1/contracts")
        assert r.status_code == 200
        assert r.json()["contracts"] == []

    def test_coverage_counters_route(self, wired_client: TestClient) -> None:
        # The O(1) incremental headline route — empty before any pull.
        r = wired_client.get("/api/v1/coverage/counters")
        assert r.status_code == 200
        body = r.json()
        assert body == {"by_label": {}, "total_bars": 0, "updates": 0}

    def test_preopen_xlsx_offloaded(self, wired_client: TestClient) -> None:
        # The offloaded build_preopen_rows path still produces a workbook.
        r = wired_client.get("/api/v1/reports/preopen.xlsx?from=2024-01-02&to=2024-01-02")
        assert r.status_code == 200
        assert "spreadsheetml" in r.headers["content-type"]

    def test_preopen_xlsx_bad_date_still_400(self, wired_client: TestClient) -> None:
        # Date validation runs on the event loop, before the offload.
        r = wired_client.get("/api/v1/reports/preopen.xlsx?from=2024-99-99&to=2024-01-02")
        assert r.status_code == 400
