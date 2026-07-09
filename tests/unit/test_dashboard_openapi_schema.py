"""Regression: OpenAPI schema generation over the WHOLE dashboard app.

DEC-AUDIT-OPENAPI-SCHEMA-001 (renumber on merge): under the installed
fastapi 0.139 / pydantic 2.13 stack an unresolved response-class ForwardRef
makes ``app.openapi()`` raise for the WHOLE schema (a single
``PydanticUserError`` — ``ForwardRef('JSONResponse')`` not fully defined —
not a partial per-route drop), so ``GET /openapi.json`` returned 500 and
``/docs`` + ``/redoc`` were dead (their HTML shells fetch the now-500 schema
client-side). ROOT CAUSE: ``from __future__ import annotations`` stringifies
the route return annotations while the response classes
(``JSONResponse``/``HTMLResponse``/...) are imported LAZILY inside
``build_app`` / ``register_launch_routes`` (the documented fastapi-optional
doctrine), so FastAPI/pydantic could not resolve the ForwardRefs against the
endpoint modules' ``__globals__``. FIX: BOTH ``app.py`` and
``launch_button.py`` publish their lazily-imported response classes into
module ``globals()`` (the ``app._make_ws_handler`` precedent) — module import
stays fastapi-free. Both edits are load-bearing: reverting EITHER one alone
reintroduces the whole-schema raise (proven by reversion).

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
        (``ForwardRef('JSONResponse')`` unresolvable) for the whole schema.
        Asserting full path parity (not just "does not raise") keeps a
        would-be partial-schema regression caught too.
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
        # a launch_button.py one. Since an unresolved ForwardRef fails the
        # WHOLE schema, either module's regression takes both paths down —
        # pinning one path per module keeps each fix load-bearing.
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

            # /docs + /redoc are static HTML shells that fetch /openapi.json
            # via browser JS (which TestClient never runs) — these are
            # presence checks, not render checks. The real teeth is the
            # /openapi.json == 200 assertion above.
            for path in ("/docs", "/redoc"):
                page = client.get(path)
                assert page.status_code == 200
                assert page.headers["content-type"].startswith("text/html")
