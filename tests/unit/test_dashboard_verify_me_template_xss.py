"""Regression: /verify-me never uses innerHTML (R44).

Guards the XSS HARD RULE (CI-enforced, universal-engineering-standards /
CLAUDE.md §11): server-derived strings reach the DOM via ``.textContent`` /
``createElement`` / ``BX.el(...)`` (itself textContent-only) ONLY, never
``.innerHTML``. Mirrors the established
``test_dashboard_strategy_template_xss.py`` / ``test_dashboard_v29_oneclick_xss.py``
convention (static-template assertions, no JS engine) — scoped to the NEW
``templates/verify_me.html`` page.

Every value verify_me.html renders is, in production, ultimately P2/P3-
derived (check status/detail strings, run identity fields, manifest funnel
counts, top100 combo sentences, trade rows) — exactly the class of string
the repo's XSS law exists to protect against.
"""

from __future__ import annotations

import re

import pytest

pytest.importorskip("fastapi", reason="dashboard routes need the [ui] extra")
pytest.importorskip("httpx", reason="TestClient needs httpx")

from brutex.dashboard.app import DashboardState, build_app
from fastapi.testclient import TestClient


def _client() -> TestClient:
    return TestClient(build_app(DashboardState(), instrument_store=None))


def _verify_me_html() -> str:
    return _client().get("/verify-me").text


def _verify_me_inline_js() -> str:
    html = _verify_me_html()
    match = re.search(r"<script>(.*?)</script>", html, re.DOTALL)
    assert match is not None, "verify_me.html must ship an inline <script> block"
    return match.group(1)


def test_verify_me_page_never_uses_innerhtml() -> None:
    html = _verify_me_html()
    assert ".innerHTML" not in html, "verify_me.html must build the DOM via textContent/createElement only"
    assert ".outerHTML" not in html
    assert "insertAdjacentHTML" not in html
    assert "document.write" not in html


def test_verify_me_page_never_uses_eval_or_function_constructor() -> None:
    js = _verify_me_inline_js()
    assert re.search(r"\beval\s*\(", js) is None
    assert "new Function(" not in js


def test_verify_me_page_header_fields_use_textcontent_or_el_helper() -> None:
    js = _verify_me_inline_js()
    for needle in (
        "hdrTitle.textContent = ",
        "hdrMeta.textContent = ",
        "detail.textContent = ",
        "idLine.textContent = ",
    ):
        assert needle in js, f"expected a textContent-only sink: {needle!r}"


def test_verify_me_page_status_pill_uses_el_helper() -> None:
    js = _verify_me_inline_js()
    assert 'BX.el("span", "bx-chip " + style.cls, style.label)' in js


def test_verify_me_page_arithmetic_lines_use_textcontent() -> None:
    js = _verify_me_inline_js()
    for needle in (
        "totalLabel.textContent = ",
        "totalValue.textContent = ",
        "arith4.compare.textContent = ",
        "arith4.claim.textContent = ",
    ):
        assert needle in js, f"expected a textContent-only sink: {needle!r}"


def test_verify_me_page_funnel_table_uses_textcontent() -> None:
    js = _verify_me_inline_js()
    assert "tdStage.textContent = " in js
    assert "tdCount.textContent = " in js
    assert "tdLabel.textContent = " in js
    assert "tdValue.textContent = " in js


def test_verify_me_page_timeline_rows_use_el_helper() -> None:
    js = _verify_me_inline_js()
    assert 'BX.el("span", "ts", ' in js
    assert 'BX.el("span", "kind", ' in js
    assert 'BX.el("span", "payload", BX.safeJson(rec.payload))' in js


def test_verify_me_page_banner_fields_use_textcontent() -> None:
    js = _verify_me_inline_js()
    assert "el.textContent = text;" in js  # showBanner()


def test_verify_me_page_never_reflects_a_hostile_field_into_an_innerhtml_write() -> None:
    """Defensive whole-page scan: no line anywhere assigns .innerHTML/.outerHTML
    using any of the server-response field names this page consumes."""
    html = _verify_me_html()
    hostile_fields = (
        "checkObj.detail",
        "row.simple_sentence",
        "row.pro_sentence",
        "rec.payload",
        "rec.kind",
        "vm.run_id",
        "manifest.dedup_skips",
        "t.entry_ts",
        "t.pnl_paisa",
    )
    for line in html.splitlines():
        if (".innerHTML" in line or ".outerHTML" in line) and "=" in line:
            for field in hostile_fields:
                assert field not in line, (
                    f"server-derived field {field!r} must not reach innerHTML: {line.strip()!r}"
                )


def test_verify_me_page_svg_chart_widget_reuses_the_shared_renderer() -> None:
    """Check-3 (trade_chart_match) must mount the IDENTICAL renderer /strategy
    uses (BXChart.renderCandleChart) — never a second, divergent chart
    implementation."""
    js = _verify_me_inline_js()
    assert "BXChart.renderCandleChart(" in js
    assert "BXChart.clearNode(" in js


def test_chart_js_still_has_zero_dom_injection_sinks() -> None:
    """Redundant with test_dashboard_strategy_template_xss.py but pinned here
    too since /verify-me is this file's stage's newest consumer of the same
    shared script."""
    resp = _client().get("/static/bx-oneclick-chart.js")
    assert resp.status_code == 200
    js = resp.text
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert sink not in js
