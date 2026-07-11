"""``/openapi.json`` renders — the ForwardRef 500 fix (DEC-OPENAPI-ANNOTATIONS-RESOLVE-001).

``brutex/dashboard/app.py`` + ``brutex/dashboard/launch_button.py`` both use
``from __future__ import annotations`` and import the FastAPI response classes
FUNCTION-LOCALLY (the lazy-import doctrine — the modules must import without
fastapi installed). Every route closure's ``-> JSONResponse`` (+ ``Response`` /
``HTMLResponse``) return annotation is therefore a STRING that FastAPI/pydantic
resolve against ``endpoint.__globals__`` — the defining MODULE's dict, where a
function-local import is invisible. FastAPI's lenient ``try_eval_type`` kept
the unresolvable ForwardRefs at registration time, so ``app.openapi()`` raised
``PydanticUserError`` → ``/openapi.json`` 500'd and ``/docs`` + ``/redoc``
were dead.

The fix is the module's OWN established idiom (``_make_ws_handler``'s
``WebSocket`` hoist): after the guarded function-local import succeeds, the
annotation-referenced response classes are hoisted into ``globals()`` in BOTH
modules — fixing app.py alone still 500s the whole schema on launch_button's
two ``-> JSONResponse`` closures, which is why the launch paths are pinned
here. Docs-surface only: zero route behavior change, and both modules still
import without fastapi (pinned by the subprocess test below +
``test_build_app_raises_helpful_error_without_fastapi``).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi", reason="the openapi surface needs the [ui] extra")

from brutex.dashboard.app import DashboardState, build_app
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client() -> TestClient:
    """One REAL app + client for the read-only schema/docs probes."""
    app = build_app(DashboardState())
    return TestClient(app, base_url="http://127.0.0.1")


def test_openapi_json_renders_200_with_nonempty_paths(client: TestClient) -> None:
    """GET /openapi.json returns 200 and a parseable schema with real paths.

    Pre-fix this was a 500: ``PydanticUserError — TypeAdapter[...
    ForwardRef('JSONResponse') ...] is not fully defined`` (captured on the
    pinned merge-base).
    """
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    schema: dict[str, Any] = resp.json()
    paths = schema.get("paths", {})
    assert isinstance(paths, dict)
    assert len(paths) > 0
    # A couple of long-standing routes as sanity anchors (never the full list —
    # the schema grows with every route family).
    assert "/api/v1/state" in paths


def test_openapi_direct_call_does_not_raise(client: TestClient) -> None:
    """``app.openapi()`` itself builds the schema (the direct pre-fix repro)."""
    schema = client.app.openapi()  # type: ignore[attr-defined]  # Starlette types .app as ASGIApp
    assert isinstance(schema, dict)
    assert schema.get("paths")


def test_docs_and_redoc_render(client: TestClient) -> None:
    """/docs (Swagger UI) and /redoc serve their shells.

    HONEST scope: these two endpoints return their static HTML shell even
    pre-fix (the browser then died on the failing ``/openapi.json`` fetch),
    so this is a companion pin, not the fails-before regression — that lives
    in the schema tests above.
    """
    assert client.get("/docs").status_code == 200
    assert client.get("/redoc").status_code == 200


def test_launch_button_routes_present_in_schema(client: TestClient) -> None:
    """The launch_button module's routes appear in the schema.

    Pins the TWO-MODULE completeness: launch_button's closures carry their own
    ``-> JSONResponse`` annotations resolved against launch_button's module
    globals, so hoisting in app.py ALONE still raises ``PydanticUserError``
    (verified: reverting only the launch_button hunk re-500s /openapi.json and
    fails this test).
    """
    schema = client.get("/openapi.json").json()
    paths = schema["paths"]
    assert "/launch" in paths
    assert "/api/v1/launch/state" in paths
    assert "/api/v1/launch/sweep" in paths


def test_modules_import_without_fastapi_subprocess() -> None:
    """Both modules import cleanly with fastapi BLOCKED (the lazy-import contract).

    The state-model tests need no FastAPI: a meta-path blocker makes ANY
    ``fastapi`` import raise ImportError in a fresh interpreter, then both
    dashboard modules are imported — the fix must not add a module-level
    fastapi dependency. (``build_app``'s friendly RuntimeError arm is pinned
    separately by ``test_build_app_raises_helpful_error_without_fastapi``.)
    """
    script = (
        "import importlib.abc, importlib.machinery, sys\n"
        "class _Block(importlib.abc.MetaPathFinder):\n"
        "    def find_spec(self, fullname, path=None, target=None):\n"
        "        if fullname == 'fastapi' or fullname.startswith('fastapi.'):\n"
        "            raise ImportError(\"No module named 'fastapi' (blocked)\")\n"
        "        return None\n"
        "sys.meta_path.insert(0, _Block())\n"
        "import brutex.dashboard.launch_button\n"
        "import brutex.dashboard.app\n"
        "print('IMPORT_OK')\n"
    )
    repo_root = Path(__file__).resolve().parents[2]
    proc = subprocess.run(  # fixed argv, our own interpreter, no user input
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=repo_root,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "IMPORT_OK" in proc.stdout
