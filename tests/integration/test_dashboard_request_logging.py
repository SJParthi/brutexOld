"""Integration: the dashboard request-logging middleware wires X-Request-ID.

DEC-DASHBOARD-REQUEST-LOGGING-001. The per-request structured emit + level policy
are unit-tested in ``test_request_logging.py``; this proves the FastAPI middleware
GLUE is actually installed — every response carries an ``X-Request-ID``, and an
inbound id is echoed back so a client can correlate one call end-to-end.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from brutex.dashboard.app import DashboardState, build_app
from fastapi.testclient import TestClient


def test_response_carries_a_generated_request_id() -> None:
    """A request with no inbound id gets a fresh 12-hex X-Request-ID on the response."""
    with TestClient(build_app(DashboardState())) as c:
        r = c.get("/api/v1/state")
    assert r.status_code == 200
    rid = r.headers.get("X-Request-ID")
    assert rid is not None
    assert len(rid) == 12
    assert all(ch in "0123456789abcdef" for ch in rid)


def test_inbound_request_id_is_echoed_back() -> None:
    """An inbound X-Request-ID is honoured + echoed (cross-service correlation)."""
    with TestClient(build_app(DashboardState())) as c:
        r = c.get("/api/v1/state", headers={"X-Request-ID": "client0000id"})
    assert r.status_code == 200
    assert r.headers.get("X-Request-ID") == "client0000id"
