"""Regression: OpenAPI schema generation over the WHOLE dashboard app.

DEC-AUDIT-OPENAPI-SCHEMA-001 (renumber on merge): ``app.openapi()`` raised
``PydanticUserError`` (``ForwardRef('JSONResponse')`` not fully defined) and
``GET /openapi.json`` returned 500 — 71/80 routes failed schema generation, so
``/docs`` + ``/redoc`` rendered dead shells. ROOT CAUSE: ``from __future__
import annotations`` stringifies the route return annotations while the
response classes (``JSONResponse``/``HTMLResponse``/...) are imported LAZILY
inside ``build_app`` / ``register_launch_routes`` (the documented
fastapi-optional doctrine), so FastAPI/pydantic could not resolve the
ForwardRefs against the endpoint modules' ``__globals__``. FIX: both factories
publish the lazily-imported response classes to module scope (the
``app._make_ws_handler`` precedent) — module import stays fastapi-free.

House pattern (``test_dashboard_app_backfill``): real ``build_app`` +
``TestClient``; skipped cleanly without the ``[ui]`` extra.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi", reason="dashboard routes need the [ui] extra")

from brutex.dashboard.app import DashboardState, build_app
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient


class TestOpenApiSchemaGeneration:
    """``app.openapi()`` + the /openapi.json + /docs + /redoc HTTP surface."""

    def test_app_openapi_covers_every_schema_route(self) -> None:
        """Direct ``app.openapi()`` succeeds and covers EVERY in-schema route.

        Before the fix this raised ``pydantic.errors.PydanticUserError``
        (``ForwardRef('JSONResponse')`` unresolvable) — asserting full path
        parity keeps a partial-schema regression (some routes silently
        dropped) equally caught.
        """
        app = build_app(DashboardState())
        schema = app.openapi()

        expected_paths = {
            route.path
            for route in app.routes
            if isinstance(route, APIRoute) and route.include_in_schema
        }
        assert expected_paths, "app must expose in-schema API routes"
        assert set(schema["paths"]) == expected_paths

        # Pin BOTH fixed modules: an app.py JSONResponse-annotated route and
        # a launch_button.py one (the two routes that still failed after the
        # app.py-only fix during bisection).
        assert "/api/v1/state" in schema["paths"]
        assert "/api/v1/launch/state" in schema["paths"]

    def test_openapi_json_docs_and_redoc_routes_200(self) -> None:
        """/openapi.json serves the schema (was 500); /docs + /redoc are HTML."""
        app = build_app(DashboardState())
        with TestClient(app, base_url="http://localhost") as client:
            resp = client.get("/openapi.json")
            assert resp.status_code == 200
            payload = resp.json()
            assert payload["info"]["title"] == "BruteX Dashboard"
            assert "/api/v1/state" in payload["paths"]
            assert "/api/v1/launch/state" in payload["paths"]

            for path in ("/docs", "/redoc"):
                page = client.get(path)
                assert page.status_code == 200
                assert page.headers["content-type"].startswith("text/html")
