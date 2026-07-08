"""Regression: /strategy + bx-oneclick-chart.js never use innerHTML (R36).

Guards the XSS HARD RULE (CI-enforced, universal-engineering-standards /
CLAUDE.md §11): server-derived strings reach the DOM via ``.textContent`` /
``createElement`` / SVG attribute assignment ONLY, never ``.innerHTML``.
Mirrors the established ``test_dashboard_movers_template_xss.py`` /
``test_dashboard_results_oneclick_xss.py`` convention (static-template
assertions, no JS engine) — scoped to the NEW ``templates/strategy.html``
page and its shared chart renderer ``static/bx-oneclick-chart.js`` (per the
FINAL APPROVED SPEC: "including bx-oneclick-chart.js's label/tooltip
rendering (createElementNS + textContent only)").

Every value strategy.html renders is, in production, ultimately vendor/lake-
derived (trade timestamps/prices, exit_reason, combo_explanation sentences,
run_id/combo_id/mask_hex from the URL query string, candle OHLCV rows) —
exactly the class of string the repo's XSS law exists to protect against.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="dashboard routes need the [ui] extra")
pytest.importorskip("httpx", reason="TestClient needs httpx")

from brutex.dashboard.app import DashboardState, build_app
from fastapi.testclient import TestClient

_STATIC_DIR = Path(__file__).resolve().parents[2] / "brutex" / "dashboard" / "static"


def _client() -> TestClient:
    return TestClient(build_app(DashboardState(), instrument_store=None))


def _strategy_html() -> str:
    return _client().get("/strategy").text


def _strategy_inline_js() -> str:
    html = _strategy_html()
    match = re.search(r"<script>(.*?)</script>", html, re.DOTALL)
    assert match is not None, "strategy.html must ship an inline <script> block"
    return match.group(1)


def _chart_js() -> str:
    return (_STATIC_DIR / "bx-oneclick-chart.js").read_text(encoding="utf-8")


# ── templates/strategy.html ─────────────────────────────────────────────────


def test_strategy_page_never_uses_innerhtml() -> None:
    html = _strategy_html()
    assert ".innerHTML" not in html, "strategy.html must build the DOM via textContent/createElement only"
    assert ".outerHTML" not in html
    assert "insertAdjacentHTML" not in html
    assert "document.write" not in html


def test_strategy_page_never_uses_eval_or_function_constructor() -> None:
    js = _strategy_inline_js()
    assert re.search(r"\beval\s*\(", js) is None
    assert "new Function(" not in js


def test_strategy_page_header_fields_use_textcontent_or_el_helper() -> None:
    js = _strategy_inline_js()
    for needle in (
        "hdrTitle.textContent = ",
        "hdrMeta.textContent = ",
        'BX.el("span", "jargon", comboExplanation.pro_sentence',
        'BX.el("span", "plain-eng", comboExplanation.simple_sentence',
        'BX.el("span", "chip",',
    ):
        assert needle in js, f"expected a textContent-only sink: {needle!r}"


def test_strategy_page_trade_row_fields_use_textcontent() -> None:
    js = _strategy_inline_js()
    for needle in (
        "td.textContent = text;",
        "pnlTd.textContent = fmtRupees(t.pnl_paisa);",
        "costsTd.textContent = fmtRupees(t.costs_paisa);",
        'BX.el("span", "jargon", BX.exitReasonLabel(t.exit_reason, "pro"))',
    ):
        assert needle in js, f"expected a textContent-only sink: {needle!r}"


def test_strategy_page_banner_fields_use_textcontent() -> None:
    js = _strategy_inline_js()
    assert "el.textContent = text;" in js  # showBanner()
    assert "renderTradesAwaiting" in js


def test_strategy_page_never_reflects_a_hostile_field_into_an_innerhtml_write() -> None:
    """Defensive whole-page scan: no line anywhere assigns .innerHTML/.outerHTML
    using any of the server-response field names this page consumes."""
    html = _strategy_html()
    hostile_fields = (
        "comboExplanation.pro_sentence",
        "comboExplanation.simple_sentence",
        "t.exit_reason",
        "t.entry_ts",
        "t.exit_ts",
        "trade.entry_ts",
        "Q.run_id",
        "Q.combo_id",
        "Q.mask_hex",
    )
    for line in html.splitlines():
        if (".innerHTML" in line or ".outerHTML" in line) and "=" in line:
            for field in hostile_fields:
                assert field not in line, (
                    f"server-derived field {field!r} must not reach innerHTML: {line.strip()!r}"
                )


def test_strategy_page_view_chart_link_is_reachable_from_results() -> None:
    """The documented reverse link: /results' story card sets scViewChart.href
    to a /strategy?... URL (built from URLSearchParams, never a markup string)."""
    resp = TestClient(build_app(DashboardState(), instrument_store=None)).get("/results")
    results_html = resp.text
    assert 'id="scViewChart"' in results_html
    assert "function strategyUrl(row)" in results_html
    assert 'scViewChart.href = strategyUrl(row);' in results_html
    # Built via URLSearchParams + property assignment, never markup concatenation.
    assert "scViewChart.innerHTML" not in results_html


# ── static/bx-oneclick-chart.js ─────────────────────────────────────────────


def test_chart_js_is_served_and_has_zero_dom_injection_sinks() -> None:
    client = _client()
    resp = client.get("/static/bx-oneclick-chart.js")
    assert resp.status_code == 200
    assert "javascript" in resp.headers["content-type"]
    js = resp.text
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert sink not in js, f"forbidden DOM sink {sink!r} in bx-oneclick-chart.js"


def test_chart_js_never_uses_eval_or_function_constructor() -> None:
    js = _chart_js()
    assert re.search(r"\beval\s*\(", js) is None
    assert "new Function(" not in js


def test_chart_js_builds_svg_via_createelementns_and_textcontent_only() -> None:
    js = _chart_js()
    assert "document.createElementNS(SVG_NS" in js
    assert "setAttribute(" in js
    # Every tooltip text sink is a native SVG <title> element's textContent —
    # never a markup string.
    assert "title.textContent = " in js
    assert "t1.textContent =" in js
    assert "t2.textContent =" in js


def test_chart_js_exposes_the_shared_api() -> None:
    js = _chart_js()
    assert "window.BXChart" in js or "global.BXChart" in js
    for name in (
        "SVG_NS",
        "svgEl",
        "clearNode",
        "tfToInterval",
        "exchangeForInstrument",
        "segmentForInstrument",
        "istWallClock",
        "sessionDateOf",
        "findBarIndex",
        "renderCandleChart",
        "renderEquityCurve",
    ):
        assert f"{name}:" in js, f"BXChart.{name} missing from the exported API"


def test_chart_js_sensex_maps_to_bse_others_to_nse() -> None:
    """A pin on the one documented exchange-mapping exception."""
    js = _chart_js()
    assert '"SENSEX"' in js
    assert '"BSE"' in js
    assert '"NSE"' in js


def test_chart_js_tf_mapping_covers_minute_hour_day() -> None:
    js = _chart_js()
    assert '"minute"' in js
    assert '"hour"' in js
    assert '"day"' in js
