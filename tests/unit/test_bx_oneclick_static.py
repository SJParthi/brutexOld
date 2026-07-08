"""The shared bx-oneclick.js module (DEC-INSTITUTIONAL-ONECLICK-001, P4 stage 3).

Served from ``/static`` (mirrors ``test_dashboard_static_assets.py``'s
``bx-ui.js`` coverage). Standalone module — does NOT require ``bx-ui.js`` to
be loaded first — reused verbatim by ``templates/results.html`` (this stage)
and, per the FINAL APPROVED SPEC, by ``templates/v29.html`` /
``templates/strategy.html`` / ``templates/verify_me.html`` in later stages.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi", reason="static mount needs the [ui] extra")
pytest.importorskip("httpx", reason="TestClient needs httpx")

from brutex.dashboard.app import DashboardState, build_app
from fastapi.testclient import TestClient


def test_bx_oneclick_is_served() -> None:
    client = TestClient(build_app(DashboardState(), instrument_store=None))
    resp = client.get("/static/bx-oneclick.js")
    assert resp.status_code == 200
    assert "javascript" in resp.headers["content-type"]
    assert len(resp.content) > 500


def test_bx_oneclick_has_zero_dom_injection_sinks() -> None:
    """CodeQL guard: bx-oneclick.js builds DOM via createElement/textContent
    ONLY — no string-to-DOM sink may ever appear (XSS hard rule)."""
    client = TestClient(build_app(DashboardState(), instrument_store=None))
    js = client.get("/static/bx-oneclick.js").text
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert sink not in js, f"forbidden DOM sink {sink!r} in bx-oneclick.js"
    assert "createElement" in js
    assert "textContent" in js


def test_bx_oneclick_never_uses_eval_or_function_constructor() -> None:
    import re

    client = TestClient(build_app(DashboardState(), instrument_store=None))
    js = client.get("/static/bx-oneclick.js").text
    assert re.search(r"\beval\s*\(", js) is None
    assert "new Function(" not in js


def test_bx_oneclick_exposes_the_shared_api() -> None:
    client = TestClient(build_app(DashboardState(), instrument_store=None))
    js = client.get("/static/bx-oneclick.js").text
    assert "window.BXOneclick" in js
    for name in (
        "esc",
        "el",
        "clear",
        "fetchJSON",
        "mode",
        "explain",
        "explainMetric",
        "METRIC_EXPLANATIONS",
        "exitReasonLabel",
        "EXIT_REASON_LABELS",
        "virtualList",
        "safeJson",
    ):
        assert f"{name}:" in js, f"BXOneclick.{name} missing from the exported API"


def test_bx_oneclick_mode_defaults_to_beginner_and_persists() -> None:
    """The honest, safe default is Beginner (plain English) — an unset/garbled
    localStorage value must never silently resolve to Pro."""
    client = TestClient(build_app(DashboardState(), instrument_store=None))
    js = client.get("/static/bx-oneclick.js").text
    assert 'stored === "pro" ? "pro" : "beginner"' in js
    assert "localStorage.setItem(MODE_KEY" in js
    assert "localStorage.getItem(MODE_KEY" in js


def test_bx_oneclick_exit_reason_table_covers_the_contract_codes() -> None:
    """The dual-language exit-reason phrase table (R33) is a FIXED lookup —
    every code the trade-row contract can emit must have BOTH a pro and a
    simple label, so no per-trade text is ever generated on the fly."""
    client = TestClient(build_app(DashboardState(), instrument_store=None))
    js = client.get("/static/bx-oneclick.js").text
    for code in ("TP", "SL", "SIGNAL_FLIP", "FORCED_1520"):
        assert f"{code}:" in js, f"exit reason code {code!r} missing a phrase-table entry"
    assert "pro:" in js
    assert "simple:" in js


def test_bx_oneclick_virtual_list_never_materializes_more_than_the_viewport() -> None:
    """R26 'no shrink, virtual-scroll render': the full item array is kept,
    but only rows intersecting the viewport (+ overscan) become real DOM
    nodes — asserted structurally (the pool is pruned every render, never
    grown unboundedly with the item count)."""
    client = TestClient(build_app(DashboardState(), instrument_store=None))
    js = client.get("/static/bx-oneclick.js").text
    assert "function virtualList(" in js
    assert "overscan" in js
    assert "idx < first || idx > last" in js  # the prune condition
