"""Regression: the ``/movers`` page escapes vendor-derived cells + the reflected note.

Guards **DEC-DASHBOARD-XSS-HARDENING-001** — two MAJOR frontend XSS gaps in
``brutex/dashboard/templates/movers.html``:

GAP A (stored XSS): the ``renderRows`` loop interpolated vendor-derived string
cell values (symbol name + moneyness) into ``innerHTML`` WITHOUT escaping. The
fix adds the same ``esc()`` helper ``db.html`` / ``universe.html`` /
``completeness.html`` ship (CLAUDE.md §11 COMMON) and wraps the cell value:
``<td class="${cls}">${esc(v)}</td>``.

GAP B (reflected XSS): the page set the note via ``innerHTML`` with server text
that echoes the raw ``?asset=`` query param. The fix makes the bold prefix a
static node and sets ONLY the dynamic note via ``textContent`` (a dedicated
``#noteText`` span), so a reflected ``?asset=<img …>`` renders as inert text.

These assertions FAIL on the pre-fix template (no ``esc()``, raw ``${v}`` cell,
``$("note").innerHTML = "<b>Note:</b> "+...``) and pass after the fix. They are
static-template assertions (no JS engine), matching the
``test_dashboard_universe_template.py`` convention.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import re

from brutex.dashboard.app import _render_movers_html


def test_movers_ships_the_shared_esc_helper() -> None:
    """GAP A: the COMMON ``esc()`` HTML-escaper is defined on the page."""
    html = _render_movers_html()
    assert "function esc(s)" in html, "the shared esc() helper must be defined"
    # The same five-replacement body the sibling templates ship (one source of truth).
    for needle in (
        'replace(/&/g, "&amp;")',
        'replace(/</g, "&lt;")',
        'replace(/>/g, "&gt;")',
        'replace(/"/g, "&quot;")',
        'replace(/\'/g, "&#39;")',
    ):
        assert needle in html, f"esc() must perform {needle!r}"


def test_movers_row_cells_are_escaped_before_innerhtml() -> None:
    """GAP A: every rendered cell value passes through ``esc()`` — never raw ``${v}``."""
    html = _render_movers_html()
    # The cell is interpolated as esc(v), not the bare value.
    assert '<td class="${cls}">${esc(v)}</td>' in html
    # The vulnerable raw form must be gone.
    assert '<td class="${cls}">${v}</td>' not in html


def test_movers_note_uses_static_label_and_textcontent() -> None:
    """GAP B: the note's bold label is static; the dynamic text is set via textContent."""
    html = _render_movers_html()
    # The static "<b>Note:</b>" label + the dynamic #noteText span live in the DOM.
    assert '<b>Note:</b> <span id="noteText"></span>' in html
    # The dynamic (possibly-reflected) note is set via textContent on that span.
    assert '$("noteText").textContent = (j.note||"")' in html
    # The vulnerable innerHTML note assignments are GONE (no server text → innerHTML).
    assert '$("note").innerHTML = "<b>Note:</b> "+(j.note||"")' not in html
    assert '$("note").innerHTML' not in html, (
        "no #note innerHTML write may remain — the reflected note must not reach innerHTML"
    )


def test_movers_no_unescaped_server_field_reaches_innerhtml() -> None:
    """GAP A+B: no ``j.note`` / raw cell value is concatenated into an innerHTML write.

    A defensive scan: any ``.innerHTML =`` assignment that interpolates ``j.note``
    would re-open the reflected hole; the fix routes the note through textContent.
    """
    html = _render_movers_html()
    for line in html.splitlines():
        if ".innerHTML" in line and "=" in line and "j.note" in line:
            raise AssertionError(f"server note must not reach innerHTML: {line.strip()!r}")
    # And the rows write is the ONLY innerHTML data-write, and it escapes its cells.
    rows_write = re.search(r'tb\.innerHTML = data\.map\(.*?\)\.join\(""\)', html, re.DOTALL)
    assert rows_write is not None, "the rows innerHTML write must still exist"
    assert "esc(v)" in rows_write.group(0), "the rows write must escape each cell value"
