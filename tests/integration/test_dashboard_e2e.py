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

from datetime import UTC, datetime

import pytest

# Skip the whole module if fastapi isn't installed.
pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from brutex.dashboard.app import DashboardState, build_app
from brutex.orchestrator.pipeline import (
    PipelineEvent,
    PipelineEventKind,
)
from brutex.orchestrator.state import IngestPhase
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
def client(state: DashboardState) -> TestClient:
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
        # Basic sanity — the inline JS connects to /ws
        assert "/ws" in body

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
