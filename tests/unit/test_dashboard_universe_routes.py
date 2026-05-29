"""Smoke test — the F&O Universe Explorer routes are wired in ``build_app``.

``brutex.dashboard.app`` is coverage-excluded (it needs FastAPI at
runtime); the real logic lives in the unit-tested ``universe_query`` +
``pull_controller`` modules. This smoke test only confirms the thin
routes are registered when FastAPI is available, and is skipped cleanly
when it is not (e.g. unit-only environments without the ``[ui]`` extra).
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi", reason="dashboard routes need the [ui] extra")

from brutex.dashboard.app import DashboardState, build_app

# The four routes added by DEC-UNIVERSE-DASHBOARD-001.
_EXPECTED_PATHS = frozenset(
    {
        "/api/v1/universe/summary",
        "/api/v1/universe/instruments",
        "/api/v1/universe/pull",
        "/universe",
    }
)


def test_universe_routes_registered() -> None:
    """``build_app`` exposes every F&O Universe Explorer route."""
    app = build_app(DashboardState(), instrument_store=None)
    paths = {route.path for route in app.routes if hasattr(route, "path")}
    assert paths >= _EXPECTED_PATHS


def test_pull_route_is_post() -> None:
    """The pull route accepts POST (it mutates the lake)."""
    app = build_app(DashboardState(), instrument_store=None)
    methods: set[str] = set()
    for route in app.routes:
        if getattr(route, "path", None) == "/api/v1/universe/pull":
            methods |= set(getattr(route, "methods", set()) or set())
    assert "POST" in methods
