"""The shared dashboard static assets are served from ``/static``
(DEC-UNIVERSE-DATEPICKER-SHARED-001).

``build_app`` mounts ``brutex/dashboard/static`` so every page can load the ONE
shared date-picker component (no inline copy). Skipped cleanly without the
``[ui]`` extra (FastAPI + httpx).
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi", reason="static mount needs the [ui] extra")
pytest.importorskip("httpx", reason="TestClient needs httpx")

from brutex.dashboard.app import DashboardState, build_app
from fastapi.testclient import TestClient


def test_datepicker_component_is_served() -> None:
    """Both the JS + CSS of the shared component serve 200 with the right type."""
    client = TestClient(build_app(DashboardState(), instrument_store=None))
    for path, ctype in (
        ("/static/bx-datepicker.js", "javascript"),
        ("/static/bx-datepicker.css", "css"),
    ):
        resp = client.get(path)
        assert resp.status_code == 200, f"{path} did not serve"
        assert ctype in resp.headers["content-type"], f"{path} wrong content-type"
        assert len(resp.content) > 500, f"{path} suspiciously small"


def test_component_carries_the_drill_picker() -> None:
    """The served JS is the real drill-nav picker (not an empty stub)."""
    client = TestClient(build_app(DashboardState(), instrument_store=None))
    js = client.get("/static/bx-datepicker.js").text
    assert "window.BXDatePicker" in js
    assert "data-drill" in js  # day->month->year drill
    assert "MutationObserver" in js  # dynamic inputs (e.g. /db) enhanced too
