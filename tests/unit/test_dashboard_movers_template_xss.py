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


# ── baseline-contiguity marker + legend (DEC-MOVERS-PREVCLOSE-CONTIGUITY-001) ──
#
# Substring-PRESENCE pins over the rendered template — the honest envelope: no
# headless JS runner executes the page, so these pin that the marker/legend JS
# SHIPS (and its strict `=== false` gates), not that a browser ran it (the
# same envelope as the staleness-pack pins, DEC-PULL-UI-STALENESS-001).


def test_movers_prevclose_marker_appends_star_on_strictly_false() -> None:
    """Both ' *' appends ship, each gated STRICTLY `=== false` (an abstained
    null contiguity must NEVER mark), placed BEFORE the esc(v) return so the
    marker rides the same escape as the cell value."""
    html = _render_movers_html()
    assert (
        'if ((field==="change"||field==="pct_change") && row.prev_close_contiguous === false'
        ' && v!=="—" && v!=="?") v += " *";'
    ) in html
    assert (
        'if ((field==="oi_chg"||field==="oi_chg_pct") && row.oi_baseline_contiguous === false'
        ' && v!=="—" && v!=="?") v += " *";'
    ) in html
    # STRICT-equality gates only — a truthy check would mark null (abstain).
    assert "row.prev_close_contiguous === false" in html
    assert "row.oi_baseline_contiguous === false" in html


def test_movers_prevclose_marker_is_appended_before_the_escaped_cell_write() -> None:
    """Pre-esc placement: both marker appends occur BEFORE the single escaped
    cell interpolation (the ' ~' moneyness-marker precedent) — the marker text
    passes through esc(v), never a second unescaped write."""
    html = _render_movers_html()
    cell_write = '<td class="${cls}">${esc(v)}</td>'
    assert html.index("row.prev_close_contiguous === false") < html.index(cell_write)
    assert html.index("row.oi_baseline_contiguous === false") < html.index(cell_write)


def test_movers_note_legend_ships_and_stays_textcontent() -> None:
    """The note legend string ships, appended to j.note via textContent only
    (never innerHTML for server strings)."""
    html = _render_movers_html()
    assert (
        " · * baseline is not the immediately-preceding NSE session (nearest-prior shown)"
    ) in html
    # The legend rides the ONE noteText textContent write, gated on a rendered
    # row carrying a strictly-false flag.
    assert "r.prev_close_contiguous === false || r.oi_baseline_contiguous === false" in html
