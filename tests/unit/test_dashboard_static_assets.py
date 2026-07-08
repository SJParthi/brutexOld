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


# ── One-click frontend cores (DEC-OPERATOR-PAGE-REAL-001) ───────────────────

#: The five stage-2/3 JS files: poll core · chart math · screener math ·
#: explain lines · the operator widget layer.
_ONECLICK_JS = (
    "bx-poll.js",
    "bx-chart.js",
    "bx-screener.js",
    "bx-explain.js",
    "bx-oneclick.js",
)


def test_oneclick_js_files_are_served() -> None:
    """Each one-click JS file serves 200 with the right type + real content."""
    client = TestClient(build_app(DashboardState(), instrument_store=None))
    for name in _ONECLICK_JS:
        resp = client.get(f"/static/{name}")
        assert resp.status_code == 200, f"{name} did not serve"
        assert "javascript" in resp.headers["content-type"], f"{name} wrong content-type"
        assert len(resp.content) > 500, f"{name} suspiciously small"


def test_oneclick_js_files_have_zero_dom_injection_sinks() -> None:
    """CodeQL guard: the one-click JS builds DOM via createElement(NS) +
    textContent ONLY — no artifact-derived string can ever parse as markup
    (F-M2 rendering contract)."""
    client = TestClient(build_app(DashboardState(), instrument_store=None))
    for name in _ONECLICK_JS:
        js = client.get(f"/static/{name}").text
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
            assert sink not in js, f"forbidden DOM sink {sink!r} in {name}"


# ── Shared design system (DEC-DASHBOARD-UX-ANIMATE-001) ─────────────────────


def test_design_system_assets_are_served() -> None:
    """bx-design.css + bx-ui.js serve 200 with the right content types."""
    client = TestClient(build_app(DashboardState(), instrument_store=None))
    for path, ctype in (
        ("/static/bx-design.css", "css"),
        ("/static/bx-ui.js", "javascript"),
    ):
        resp = client.get(path)
        assert resp.status_code == 200, f"{path} did not serve"
        assert ctype in resp.headers["content-type"], f"{path} wrong content-type"
        assert len(resp.content) > 500, f"{path} suspiciously small"


def test_bx_ui_has_zero_dom_injection_sinks() -> None:
    """CodeQL guard: bx-ui.js builds DOM via createElement/textContent ONLY.

    No string-to-DOM sink may ever appear — a vendor/server-derived string
    can then never be parsed as markup through the shared helpers.
    """
    client = TestClient(build_app(DashboardState(), instrument_store=None))
    js = client.get("/static/bx-ui.js").text
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert sink not in js, f"forbidden DOM sink {sink!r} in bx-ui.js"
    assert "createElement" in js
    assert "textContent" in js


def test_bx_ui_exposes_the_shared_api() -> None:
    """The BXUI namespace carries every helper the templates adopt."""
    client = TestClient(build_app(DashboardState(), instrument_store=None))
    js = client.get("/static/bx-ui.js").text
    assert "window.BXUI" in js
    for name in (
        "esc",
        "num",
        "fetchWithTimeout",
        "skeleton",
        "stateCard",
        "errorCard",
        "emptyState",
        "setDotState",
        "countUp",
        "stagger",
        "initVisibilityPause",
    ):
        assert f"{name}:" in js, f"BXUI.{name} missing from the exported API"
    # a hung fetch must resolve to an honest error, never a stuck "Loading…"
    assert "AbortController" in js
    assert "TimeoutError" in js
    # idempotent injection guard
    assert "if (window.BXUI) { return; }" in js


def test_bx_design_carries_the_component_inventory() -> None:
    """Every documented component class + token exists in the served sheet."""
    client = TestClient(build_app(DashboardState(), instrument_store=None))
    css = client.get("/static/bx-design.css").text
    for needle in (
        ".bx-wrap",
        ".bx-card",
        ".bx-chip",
        ".bx-btn",
        ".bx-table",
        ".bx-tablewrap",
        ".bx-skel",
        ".bx-state",
        ".bx-live-dot",
        ".bx-ribbon",
        ".bx-stagger",
        ".bx-countpop",
        ".bx-paused",
        "--dock-h",
        "--page-max",
        "--gutter",
        "--accent",
        'body[data-accent="yellow"]',
        "prefers-reduced-motion",
    ):
        assert needle in css, f"{needle!r} missing from bx-design.css"
    # the canonical .001ms reduced-motion kill form (one vocabulary)
    assert ".001ms !important" in css
    # padding-only widget docking (both widget JS files are open-PR fenced)
    assert "padding-bottom: calc(var(--dock-h)" in css


def test_bx_design_keyframes_animate_transform_opacity_only() -> None:
    """Motion law: keyframes animate transform/opacity (plus the sanctioned
    background-position gradient drift) — never a layout property."""
    import re

    client = TestClient(build_app(DashboardState(), instrument_store=None))
    css = client.get("/static/bx-design.css").text
    body = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    allowed = {"opacity", "transform", "background-position"}
    for match in re.finditer(r"@keyframes\s+(\w+)\s*\{", body):
        name = match.group(1)
        depth, j = 0, match.end() - 1
        while True:
            if body[j] == "{":
                depth += 1
            elif body[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        block = body[match.end() - 1 : j]
        props = {p.strip() for p in re.findall(r"([a-z-]+)\s*:", block)}
        extra = props - allowed
        assert not extra, f"@keyframes {name} animates layout props: {extra}"
