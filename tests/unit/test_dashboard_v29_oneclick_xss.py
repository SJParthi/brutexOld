"""Regression: the v29 Operator page's NEW one-click JS never uses innerHTML.

Guards the XSS HARD RULE (CI-enforced, universal-engineering-standards /
CLAUDE.md §11): server-derived strings reach the DOM via ``.textContent`` /
``createElement`` ONLY, never ``.innerHTML``. This mirrors the established
``test_dashboard_movers_template_xss.py`` / ``test_dashboard_reports_template_xss.py``
convention (static-template assertions, no JS engine) — scoped to the NEW
run-control / chain-map / timeline / milestone-mirror block added in
DEC-INSTITUTIONAL-ONECLICK-001 stage 2 (everything between the
``RUN CONTROL + LIVE CHAIN MAP + RUN TIMELINE + MILESTONE MIRROR`` markers).

The block renders every field straight from JSON responses whose values are,
in production, ultimately vendor/lake-derived (run_id, chain-map ``detail``
strings, manifest funnel/door names, timeline ``payload`` JSON, milestone
payloads, preflight refusal ``symbol``/``reason``/``missing_spans``) — exactly
the class of string the repo's XSS law exists to protect against.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import re

from brutex.dashboard.app import _render_index_html

_BLOCK_START = "RUN CONTROL + LIVE CHAIN MAP + RUN TIMELINE + MILESTONE MIRROR"


def _oneclick_js_block() -> str:
    """Return just the new stage-2 one-click JS ``<script>`` block's source."""
    html = _render_index_html()
    for match in re.finditer(r"<script>(.*?)</script>", html, re.DOTALL):
        if _BLOCK_START in match.group(1):
            return match.group(1)
    raise AssertionError("the stage-2 one-click <script> block must be present")


def test_block_is_present_exactly_once() -> None:
    html = _render_index_html()
    assert html.count(_BLOCK_START) == 1


def test_oneclick_block_never_uses_innerhtml() -> None:
    js = _oneclick_js_block()
    # ``.innerHTML`` (the actual DOM-sink property access), not the bare word
    # "innerHTML" — this file's own explanatory comment mentions the word.
    assert ".innerHTML" not in js, (
        "the one-click block must build the DOM via textContent/createElement only"
    )
    assert ".outerHTML" not in js
    assert "insertAdjacentHTML" not in js
    assert "document.write" not in js


def test_oneclick_block_never_uses_document_write_or_eval() -> None:
    js = _oneclick_js_block()
    assert re.search(r"\beval\s*\(", js) is None
    assert "new Function(" not in js


def test_run_status_fields_are_assigned_via_textcontent() -> None:
    js = _oneclick_js_block()
    for needle in (
        "el.textContent = ",
        "lab.textContent = ",
        "chip.textContent = ",
        "tdStage.textContent = ",
        "tdCount.textContent = ",
        "tdName.textContent = ",
        "tdKilled.textContent = ",
    ):
        assert needle in js, f"expected a textContent-only sink: {needle!r}"


def test_timeline_and_milestone_payloads_go_through_textcontent_only() -> None:
    """The flight-recorder ``payload`` (arbitrary, vendor/run-derived JSON) is
    stringified then assigned via ``textContent`` — never concatenated into a
    markup string."""
    js = _oneclick_js_block()
    assert "payload.textContent = safeJson(rec.payload)" in js
    assert "text.textContent = safeJson(rec.payload)" in js
    # The stringify helper itself never builds markup — it only ever feeds
    # a textContent assignment (checked above); this asserts no alternate
    # sink was introduced for the same value.
    for line in js.splitlines():
        if "rec.payload" in line:
            assert ".innerHTML" not in line, f"payload must never reach innerHTML: {line.strip()!r}"


def test_proceed_dialog_refusal_fields_use_textcontent() -> None:
    """R4 loud refusal rendering (symbol/tf/missing_spans/reason) is
    textContent-only — a hostile/garbled fixture value must render inert."""
    js = _oneclick_js_block()
    assert "head.textContent = 'REFUSED — '" in js
    assert "spans.textContent = 'missing: '" in js
    assert "reason.textContent = String((ref && ref.reason) || '')" in js


def test_run_picker_options_use_textcontent() -> None:
    js = _oneclick_js_block()
    assert "opt.textContent = String(run.run_id)" in js


def test_no_server_field_reaches_an_innerhtml_write_anywhere_in_the_page() -> None:
    """Defensive whole-page scan: no line anywhere in v29.html assigns
    ``.innerHTML`` using any of the new one-click response field names."""
    html = _render_index_html()
    hostile_fields = (
        "rec.payload",
        "row.simple_sentence",
        "row.pro_sentence",
        "ref.reason",
        "ref.missing_spans",
        "manifest.suspect_reasons",
        "run.run_id",
        "chainMap",
    )
    for line in html.splitlines():
        if ".innerHTML" in line and "=" in line:
            for field in hostile_fields:
                assert field not in line, (
                    f"one-click server-derived field {field!r} must not reach innerHTML: {line.strip()!r}"
                )
