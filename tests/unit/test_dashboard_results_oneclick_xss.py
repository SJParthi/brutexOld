"""Regression: /results' NEW Top-100/story-card/trades JS never uses innerHTML.

Guards the XSS HARD RULE (CI-enforced, universal-engineering-standards /
CLAUDE.md §11): server-derived strings reach the DOM via ``.textContent`` /
``createElement`` ONLY, never ``.innerHTML``. Mirrors the established
``test_dashboard_movers_template_xss.py`` / ``test_dashboard_v29_oneclick_xss.py``
convention (static-template assertions, no JS engine) — scoped to the NEW
Global Top-100 / story-card / List-of-trades block added in
DEC-INSTITUTIONAL-ONECLICK-001 stage 3 (everything between the "Global
Top-100 Best-of-Best + story card + trades" markers).

The block renders every field straight from the ``GET /api/v1/sweep/top100``
and ``GET /api/v1/sweep/trades`` JSON responses whose values are, in
production, ultimately vendor/lake-derived (sentences, instrument/tf/
direction, combo_id/sweep_run_id, family-twin mask_hex, exit_reason, trade
timestamps/prices) — exactly the class of string the repo's XSS law exists
to protect against. The pre-existing screener block ABOVE this one (which
already escapes-then-innerHTML per its own established pattern) is
deliberately OUT OF SCOPE here — this file only guards the NEW stage-3 code.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import re

from brutex.dashboard.app import _render_results_html

_BLOCK_START = "Global Top-100 Best-of-Best + story card + trades"


def _oneclick_js_block() -> str:
    """Return just the new stage-3 Top-100/story-card/trades ``<script>`` block's source."""
    html = _render_results_html()
    for match in re.finditer(r"<script>(.*?)</script>", html, re.DOTALL):
        if _BLOCK_START in match.group(1):
            return match.group(1)
    raise AssertionError("the stage-3 Top-100/story-card/trades <script> block must be present")


def test_block_is_present_exactly_once() -> None:
    html = _render_results_html()
    assert html.count(_BLOCK_START) == 1


def test_bx_oneclick_script_tag_is_present() -> None:
    html = _render_results_html()
    assert '<script defer src="/static/bx-oneclick.js"></script>' in html


def test_oneclick_block_never_uses_innerhtml() -> None:
    js = _oneclick_js_block()
    assert ".innerHTML" not in js, (
        "the Top-100/story-card/trades block must build the DOM via textContent/createElement only"
    )
    assert ".outerHTML" not in js
    assert "insertAdjacentHTML" not in js
    assert "document.write" not in js


def test_oneclick_block_never_uses_eval_or_function_constructor() -> None:
    js = _oneclick_js_block()
    assert re.search(r"\beval\s*\(", js) is None
    assert "new Function(" not in js


def test_top100_row_fields_are_assigned_via_textcontent_or_el_helper() -> None:
    """Every server-derived Top-100 field reaches the DOM through BX.el(...)
    (which itself is textContent-only, asserted in test_bx_oneclick_static.py)
    or a direct .textContent assignment — never string concatenation into
    innerHTML."""
    js = _oneclick_js_block()
    for needle in (
        'BX.el("span", "rank", "#" + String(row.rank))',
        'BX.el("span", "instrument", String(row.instrument || "—"))',
        'BX.el("span", "jargon", row.pro_sentence || "")',
        'BX.el("span", "plain-eng", row.simple_sentence || "")',
    ):
        assert needle in js, f"expected a textContent-only sink: {needle!r}"


def test_story_card_fields_use_textcontent() -> None:
    js = _oneclick_js_block()
    for needle in (
        "scTitle.textContent = ",
        "scMeta.textContent = metaBits.join",
        "li.textContent = ",
        "scTwinsSummary.textContent = ",
    ):
        assert needle in js, f"expected a textContent-only sink: {needle!r}"


def test_trade_row_fields_use_textcontent() -> None:
    js = _oneclick_js_block()
    for needle in (
        "td.textContent = text;",
        "pnlTd.textContent = fmtRupees(t.pnl_paisa);",
        "costsTd.textContent = fmtRupees(t.costs_paisa);",
        'BX.el("span", "jargon", BX.exitReasonLabel(t.exit_reason, "pro"))',
    ):
        assert needle in js, f"expected a textContent-only sink: {needle!r}"


def test_no_server_field_reaches_an_innerhtml_write_anywhere_in_the_page() -> None:
    """Defensive whole-page scan: no line anywhere in results.html assigns
    ``.innerHTML``/``.outerHTML`` using any of the new one-click response
    field names (the pre-existing screener's own innerHTML usage above is
    intentionally excluded from this specific field-name check — it uses a
    disjoint field vocabulary from the leaderboard route, and is covered by
    its own escape-before-innerHTML discipline, unchanged by this stage)."""
    html = _render_results_html()
    hostile_fields = (
        "row.pro_sentence",
        "row.simple_sentence",
        "row.combo_id",
        "row.sweep_run_id",
        "row.mask_hex",
        "t.exit_reason",
        "t.entry_ts",
        "t.exit_ts",
        "m.mask_hex",
    )
    for line in html.splitlines():
        if (".innerHTML" in line or ".outerHTML" in line) and "=" in line:
            for field in hostile_fields:
                assert field not in line, (
                    f"one-click server-derived field {field!r} must not reach innerHTML: {line.strip()!r}"
                )


def test_awaiting_and_error_banners_use_textcontent() -> None:
    js = _oneclick_js_block()
    assert "t100Banner.textContent = text;" in js
    assert "renderTradesAwaiting" in js
    assert "td.textContent = text;" in js  # the awaiting/error trade-row cell
