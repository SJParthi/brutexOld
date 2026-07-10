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
    # The dynamic (possibly-reflected) note is set via textContent on that span —
    # every real-payload path routes it through the ONE legend helper
    # (refreshBaselineLegend), which is itself a textContent-only write.
    assert '$("noteText").textContent = (note||"")' in html
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


def test_movers_pcr_panel_present_hidden_and_dom_rendered() -> None:
    """DEC-MOVERS-OPTIONS-PCR-001: the PCR panel exists, hidden by default,
    is invalidated unconditionally at the top of ``load()``, and its renderer
    builds every payload-derived value via DOM APIs / ``textContent`` ONLY."""
    html = _render_movers_html()
    # The static skeleton, hidden by default.
    assert '<div class="note" id="pcrPanel" hidden>' in html
    assert '<div id="pcrRows"></div>' in html
    assert "function renderPcr(rows)" in html
    assert "function showPcr(j)" in html
    # Unconditional invalidation at the top of load() — covers asset switches,
    # gated payloads, preview/catch fallbacks, and absent-key payloads.
    assert '$("pcrPanel").hidden = true; $("pcrRows").textContent = "";' in html
    # The success-path guard: options-only, non-gated, array-guarded.
    assert 'state.asset === "options" && !j.gated && Array.isArray(j.pcr) && j.pcr.length' in html
    # Design edge 20's honest limit STATED in the panel help text (round-1
    # hostile-review fold): the all-exchanges view folds both listings of an
    # underlying into one row — the same population the table ranks.
    assert "all-exchanges view folds both listings of an underlying into one row" in html


def test_movers_pcr_renderer_never_touches_innerhtml() -> None:
    """The renderPcr/showPcr sources contain NO string-to-DOM sink — payload
    values reach the DOM only through ``textContent`` / the ``title`` DOM
    property (stricter than the esc()+innerHTML pattern; CodeQL XSS gate).

    Round-8 hostile-review fold: forbids the FULL 4-sink set the sibling
    ``test_bx_ui_has_zero_dom_injection_sinks`` uses (CLAUDE.md §11 COMMON) —
    an innerHTML-only pin would let a future ``insertAdjacentHTML`` /
    ``outerHTML`` / ``document.write`` edit through, exactly the sink class
    the DOM-only law exists to forbid."""
    html = _render_movers_html()
    match = re.search(r"function renderPcr\(rows\).*?function showPcr\(j\).*?\n}", html, re.DOTALL)
    assert match is not None, "renderPcr + showPcr must both be defined"
    pcr_source = match.group(0)
    for sink in (".innerHTML", ".outerHTML", "insertAdjacentHTML", "document.write"):
        assert sink not in pcr_source, f"forbidden DOM sink {sink!r} in renderPcr/showPcr"
    assert "textContent" in pcr_source
    assert "document.createElement" in pcr_source
    assert "document.createTextNode" in pcr_source
    # Coverage honesty: the server-composed note is shown verbatim, never
    # re-derived; the "~" non-full marker rides the muted span.
    assert "r.coverage_note" in pcr_source
    assert 'r.coverage !== "full" ? " ~" : ""' in pcr_source


def test_movers_pcr_null_sum_renders_dash_never_zero() -> None:
    """The grouped-sum helper ``grp()`` renders a null put/call sum as an honest
    "—", never a fabricated ``0`` (round-4 hostile-review fold; Golden Rule #1).

    Unreachable on today's wire — ``pcr_row_json`` always emits raw ints for
    put_oi/call_oi/put_volume/call_volume — but the defensive branch must match
    the panel's own null law (null ratios render "—"), so a future wire variant
    carrying a null sum could never display a fabricated 0.
    """
    html = _render_movers_html()
    match = re.search(r"function renderPcr\(rows\).*?function showPcr\(j\).*?\n}", html, re.DOTALL)
    assert match is not None, "renderPcr + showPcr must both be defined"
    pcr_source = match.group(0)
    # The honest null → "—" branch is present…
    assert 'const grp = (n) => n == null ? "—" : Number(n).toLocaleString("en-IN");' in pcr_source
    # …and the fabricated-zero form is gone.
    assert "Number(n == null ? 0 : n)" not in pcr_source


def test_movers_5xx_renders_server_detail_not_the_sample_preview() -> None:
    """DEC-MOVERS-OPTIONS-PCR-001 round-8 fold: a ``>= 500`` response on a
    RUNNING dashboard renders the server's own parsed ``detail`` (the
    group-key-enriched ``"lake corruption: … (underlying X, expiry …)"``
    remediation) via ``textContent`` over an EMPTY grid and returns — it must
    NEVER fall into the offline SAMPLE-preview catch, whose "run the dashboard
    for real movers data" note misdirects the operator away from the real fix.
    """
    html = _render_movers_html()
    start = html.index("if (r.status >= 500)")
    end = html.index('throw new Error("HTTP "+r.status)')
    assert start < end, "the 5xx branch must run BEFORE the generic non-ok throw"
    branch = html[start:end]
    # The server's own detail is parsed from the error body…
    assert "JSON.parse(await r.text()).detail" in branch
    # …and reaches the note via textContent ONLY (never innerHTML — the
    # DEC-DASHBOARD-XSS-HARDENING-001 law for server strings).
    assert '$("noteText").textContent = detail' in branch
    assert ".innerHTML" not in branch
    # An EMPTY grid + an honest status line — never the sample rows.
    assert "rowsData = [];" in branch
    assert "renderHead(); renderRows();" in branch
    assert '$("asof").textContent = `server error · HTTP ${r.status}`' in branch
    assert "SAMPLE" not in branch
    # The branch RETURNS — the sample-preview catch stays 4xx/network-only.
    assert "return;" in branch
    # The no-detail fallback still points at the real diagnostic, not "run the
    # dashboard".
    assert "check the dashboard log (movers.pcr_corruption" in branch


def test_movers_pcr_rendered_on_both_success_paths() -> None:
    """showPcr runs on the full-render path AND the skip-render-when-unchanged
    fast path (load() hides the panel at its top, so an identical repeat fetch
    must restore it — otherwise a repeat ↻ Refresh would blank the panel).

    Counts CALL SITES only: the negative-lookbehind excludes the function
    DEFINITION (``function showPcr(j) {``), so removing EITHER call site —
    the unchanged-skip fast path or the full-render path — fails this test
    (a bare ``count("showPcr(j)") >= 2`` would still pass at 2 with the
    definition making up the shortfall; round-2 hostile-review fold).
    """
    html = _render_movers_html()
    call_sites = re.findall(r"(?<!function )showPcr\(j\)", html)
    assert len(call_sites) >= 2, (
        "showPcr must be CALLED on both the full render and the unchanged-skip "
        f"path (call sites found: {len(call_sites)})"
    )


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
    """The note legend string ships, appended to the stored server note via
    textContent only (never innerHTML for server strings). The " *" legend
    carries the one-word REGULAR truth fix + the shape-neutral "possible lake
    hole" (DEC-PREVSESSION-MUHURAT-COHERENCE-001 — no unconditional re-pull
    imperative: the hole may be a stray bar instead)."""
    html = _render_movers_html()
    assert (
        " · * baseline is not the immediately-preceding REGULAR NSE session "
        "(nearest-prior shown — possible lake hole)"
    ) in html
    # The legend rides the ONE noteText textContent write, gated on a rendered
    # row carrying a strictly-false flag.
    assert "r.prev_close_contiguous === false || r.oi_baseline_contiguous === false" in html


def test_movers_legend_helper_recomputes_from_current_rows() -> None:
    """The legend lives in ONE helper that recomputes staleBaseline from the
    CURRENT rowsData (both strictly-false flags) and writes via textContent —
    the category-switch desync fix (hostile-review MINOR): previously computed
    only inside load(), so a cached pill switch re-rendered rows carrying
    ``prev_close_contiguous === false`` with NO legend (and the inverse left a
    stale legend)."""
    html = _render_movers_html()
    assert "function refreshBaselineLegend(note)" in html
    # Recomputed from the LIVE rowsData, strict `=== false` on BOTH flags.
    assert (
        "const staleBaseline = rowsData.some(r => r && "
        "(r.prev_close_contiguous === false || r.oi_baseline_contiguous === false));"
    ) in html
    # textContent-only write inside the helper (never innerHTML for server strings).
    assert '$("noteText").textContent = (note||"")' in html
    # No stray innerHTML write of the note anywhere (re-pinned defensively).
    assert '$("noteText").innerHTML' not in html


# ── Muhurat baseline marker + legend (DEC-PREVSESSION-MUHURAT-COHERENCE-001) ──


def test_movers_muhurat_marker_appends_dagger_on_strictly_true() -> None:
    """Both ' †' appends ship, each gated STRICTLY `=== true` on the
    EMIT-ON-TRUE muhurat keys, pre-esc like the ' *' marker — and the ' *'
    appends stay byte-intact beside them (a genuine hole still marks ' *')."""
    html = _render_movers_html()
    assert (
        'if ((field==="change"||field==="pct_change") && row.prev_close_muhurat === true'
        ' && v!=="—" && v!=="?") v += " †";'
    ) in html
    assert (
        'if ((field==="oi_chg"||field==="oi_chg_pct") && row.oi_baseline_muhurat === true'
        ' && v!=="—" && v!=="?") v += " †";'
    ) in html
    # The ' *' hole markers survive UNCHANGED beside the new ' †' markers.
    assert (
        'if ((field==="change"||field==="pct_change") && row.prev_close_contiguous === false'
        ' && v!=="—" && v!=="?") v += " *";'
    ) in html
    # Pre-esc placement: both muhurat appends occur BEFORE the single escaped
    # cell interpolation (the ' *' precedent — the marker rides esc(v)).
    cell_write = '<td class="${cls}">${esc(v)}</td>'
    assert html.index("row.prev_close_muhurat === true") < html.index(cell_write)
    assert html.index("row.oi_baseline_muhurat === true") < html.index(cell_write)


def test_movers_muhurat_legend_ships_beside_the_hole_legend() -> None:
    """The ' †' legend line ships (recomputed from rowsData on the EMIT-ON-TRUE
    keys, strict `=== true`) and names the two-law design — the Muhurat close
    is a real session the lake legitimately carries, while engine day-anchors
    deliberately use the prior regular session."""
    html = _render_movers_html()
    assert (
        " · † baseline is the Diwali Muhurat session close (a real NSE session the lake "
        "legitimately carries; engine day-anchors deliberately use the prior regular session)"
    ) in html
    assert (
        "const muhuratBaseline = rowsData.some(r => r && "
        "(r.prev_close_muhurat === true || r.oi_baseline_muhurat === true));"
    ) in html


def test_movers_legend_helper_called_from_both_load_and_applycategory() -> None:
    """BOTH render paths call the helper — the cached pill switch
    (applyCategory) AND load() (main render path + the skip-unchanged
    restore) — so the legend can never desync from the rendered rows; the base
    server note is stored (baseNote) so the pill switch rebuilds the note
    WITHOUT a refetch."""
    html = _render_movers_html()
    # The stored base note ships.
    assert 'let baseNote = "";' in html
    # applyCategory: cached re-render THEN the legend refresh, same statement.
    assert (
        "if (lastPayload) { rowsData = lastPayload[state.category] || "
        "lastPayload.gainers || []; renderRows(); refreshBaselineLegend(baseNote); }"
    ) in html
    # load() stores the server note then refreshes through the SAME helper on
    # BOTH its real-payload paths (main render + skip-unchanged restore).
    assert html.count('baseNote = j.note || "";') == 2
    # Three call sites total: applyCategory + the two load() paths.
    assert html.count("refreshBaselineLegend(baseNote);") == 3
