"""Template-source pins for the DEC-DASHBOARD-JS-PERF-001 frontend perf fixes.

HONESTY ENVELOPE: this repo has NO JavaScript coverage harness — the inline
``<script>`` behavior of the dashboard templates cannot be exercised or
covered directly here. These tests therefore pin the TEMPLATE SOURCE
STRUCTURE (DOM-structure / template-string assertions, the
``test_dashboard_pull_rate_monitor.py`` every-template idiom) that the JS
perf fixes rely on:

(a) each of the five 4 s-poller pages (``coverage.html`` /
    ``fetch_windows.html`` / ``expired_options.html`` /
    ``spot_coverage.html`` / ``completeness.html`` — the last added by
    DEC-DASHBOARD-POLL-HYGIENE-001) carries its CLIENT-side
    skip-render-when-unchanged guard (raw response-text memo compare — the
    backend payloads are frozen, no generation/etag field exists on the
    wire) AND still polls on the 4 s ``setInterval`` cadence;
(b) ``v29.html`` ships the shared trailing-edge ``debounce`` helper and the
    five combos-filter controls are wired through ``applyFiltersDebounced``
    (the prior undebounced per-keystroke ``'change'``+``'input'``
    double-bind to ``applyFilters`` is gone); PLUS the bundled
    filter-column CORRECTION — the ONE deliberate value change of this
    otherwise values/pixels-unchanged PR (hostile-review MAJOR folded):
    ``applyFilters`` now reads ``children[3/4/5/10/11]`` (Inst/TF/Dir/
    Smoothness/RealEdge per the ``#combosTable`` thead) instead of the
    pre-fix mismapped ``[2/3/4/8/9]`` (Strategy/Inst/TF/Charges/Net —
    Charges' negative ``data-sort`` made any filter interaction hide
    EVERY row). Both the indices AND the thead order they map to are
    pinned so neither can silently drift apart again;
(c) ``db.html`` ships the in-place tbody patch (``patchGridRows`` over the
    same esc()'d ``gridRowsHtml`` strings) + the ONE delegated pager
    handler (``pagerState``/``setGridPager``), and the four deleted
    per-render ``wire*Pager`` helpers are gone;
(d) ``movers.html`` ships its skip/in-place mechanisms (``lastRenderSig``
    payload-signature skip, ``wasGated``/``lastGatedKey`` gated-shell
    paint-once, ``updateHeadSort`` in-place sort, delegated
    ``onPillsClick``/``onHeadClick``, the ``#topn``/``#minvol`` debounce)
    AND still renders the prebuild gate note via ``textContent`` with the
    5 s auto-retry (DEC-PIPELINE-AUTOPREBUILD-001 pins preserved);
(e) a security pin: per touched template, the count of ``innerHTML``
    ASSIGNMENT SINKS (``.innerHTML =`` / ``.innerHTML +=``) is bounded by
    the count measured at the pre-fix baseline commit — no NEW sink for
    vendor/user-derived strings (CodeQL watches this repo). The one
    sanctioned delta is ``db.html``'s ``tbody.innerHTML = gridRowsHtml``:
    the SAME already-esc()'d builder strings the full rebuild uses,
    against a NARROWER target.

These pins FAIL on the pre-fix templates (the guards/helpers did not
exist; the ``wire*Pager`` helpers did) and pass after the fix. No JS
coverage is claimed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

TEMPLATES = Path(__file__).resolve().parents[2] / "brutex" / "dashboard" / "templates"


def _template(name: str) -> str:
    text = (TEMPLATES / name).read_text(encoding="utf-8")
    assert text, f"expected a non-empty template at {name}"
    return text


# ---------------------------------------------------------------------------
# (a) The four 4 s-poller pages: skip-render guard + the poll cadence intact.
# ---------------------------------------------------------------------------

# Per page: the exact guard/memo declarations (mechanism names from
# CHANGES/pollers.md) + the skip line that consults the memo.
_POLLER_GUARDS: dict[str, tuple[str, ...]] = {
    "coverage.html": (
        # Four per-endpoint raw-text memos (opt/spot/win/cmp change independently).
        "const lastPayload = { opt: null, spot: null, win: null, cmp: null };",
        # The no-op-preserving single-target write guard.
        "function setText(el, s)",
        # The memo actually gates the parse+writes.
        "if (txt !== lastPayload.opt) {",
    ),
    "fetch_windows.html": (
        "let lastPayloadKey = null;",
        'let lastLiveLine = "";',
        # Skip: replay the honest heartbeat line and return before JSON.parse.
        "if (payloadKey === lastPayloadKey) { liveTxt.textContent = lastLiveLine; return; }",
        # Manual-refresh: a chip flip nulls the memo so a click ALWAYS renders.
        "lastPayloadKey = null; // manual filter flip: force the next render, never a stale skip",
    ),
    "expired_options.html": (
        "let lastCoverageTxt = null;",
        "let lastHeadline = {};",
        "let lastRowCount = 0;",
        # The coverage-payload memo gates the KPI writes + renderRows.
        "if (txt !== lastCoverageTxt) {",
    ),
    "spot_coverage.html": (
        "let lastPayloadTxt = null;",
        'let lastLiveLine = "";',
        "if (txt === lastPayloadTxt) { liveTxt.textContent = lastLiveLine; return; }",
    ),
    # completeness.html gained the raw-text memo in DEC-DASHBOARD-POLL-HYGIENE-001
    # (the ~750-row options table was rebuilt every 4 s with no diff gate).
    "completeness.html": (
        "let lastBoardTxt = null;",
        'let lastLiveLine = "";',
        "if (txt === lastBoardTxt) { liveTxt.textContent = lastLiveLine; return; }",
    ),
}


@pytest.mark.parametrize("name", sorted(_POLLER_GUARDS))
def test_poller_template_ships_skip_render_guard(name: str) -> None:
    """Each 4 s-poller page carries its client-side raw-text skip-render memo."""
    html = _template(name)
    for needle in _POLLER_GUARDS[name]:
        assert needle in html, f"{name}: missing skip-render guard fragment {needle!r}"


@pytest.mark.parametrize("name", sorted(_POLLER_GUARDS))
def test_poller_template_still_polls_every_4s(name: str) -> None:
    """The skip guard must not have removed the 4 s poll cadence itself."""
    html = _template(name)
    assert "setInterval(poll, 4000);" in html, f"{name}: the 4 s poll loop must survive"


@pytest.mark.parametrize("name", sorted(_POLLER_GUARDS))
def test_poller_template_reads_body_as_text_for_the_compare(name: str) -> None:
    """The compare gate is the RAW response text (backend payloads are frozen —

    no generation/etag field exists on the wire, so the exact text compare is
    the only honest client-side gate).
    """
    html = _template(name)
    assert "await r.text()" in html, f"{name}: the guard must compare the raw body text"


# ---------------------------------------------------------------------------
# (b) v29.html: debounced combos-filter wiring.
# ---------------------------------------------------------------------------


def test_v29_ships_the_debounce_helper() -> None:
    html = _template("v29.html")
    assert "const debounce = (fn, ms)" in html, "the shared trailing-edge debounce helper"
    assert "const applyFiltersDebounced = debounce(applyFilters, 200);" in html


def test_v29_filter_inputs_are_wired_through_the_debounce() -> None:
    """All five combos-filter controls bind ONCE, through the debounced wrapper."""
    html = _template("v29.html")
    assert "['fInst', 'fTF', 'fDir', 'fSharpe', 'fDSR'].forEach(id => {" in html
    assert "addEventListener('input', applyFiltersDebounced);" in html


def test_v29_filters_no_longer_call_apply_filters_directly_per_keystroke() -> None:
    """The pre-fix smell — a 'change'+'input' double-bind straight to
    ``applyFilters`` (twice per select change, once per keystroke, undebounced)
    — is gone. The Reset button's synchronous ``applyFilters()`` call stays.
    """
    html = _template("v29.html")
    assert "addEventListener('change', applyFilters);" not in html
    assert "addEventListener('input', applyFilters);" not in html
    # Reset stays synchronous (immediate feedback) — deliberate, not a regression.
    assert "applyFilters();  // reset stays SYNCHRONOUS" in html


# The #combosTable thead order the applyFilters indices map to. Kept as raw
# template source (entity-encoded &amp;) so the pin needs no HTML parser.
_V29_COMBOS_THEAD_ORDER = [
    "Rank",  # 0
    "Combo",  # 1
    "Strategy",  # 2
    "Inst",  # 3  ← rowInst
    "TF",  # 4  ← rowTF
    "Dir",  # 5  ← rowDir
    "Trades",  # 6
    "Gross P&amp;L",  # 7
    "Charges",  # 8
    "Net P&amp;L",  # 9
    "Smoothness",  # 10 ← rowSharpe (Min Smoothness)
    "Real Edge",  # 11 ← rowDSR (Min Real Edge)
    "Score",  # 12
    "",  # 13 (action column)
]


def test_v29_filter_column_indices_match_the_combos_thead() -> None:
    """Pin the bundled filter-column CORRECTION (the one deliberate value
    change of DEC-DASHBOARD-JS-PERF-001, called out in the DEC/PR note).

    Pre-fix, ``applyFilters`` read ``children[2/3/4/8/9]`` — Strategy as
    Inst, Inst as TF, TF as Dir, Charges (negative ``data-sort``) as Min
    Smoothness (so ANY filter interaction hid every row at the default
    ``minSharpe=0``), Net P&L as Min Real Edge. This pins BOTH the corrected
    index reads ``[3/4/5/10/11]`` verbatim AND the ``#combosTable`` thead
    order they map to, so a re-regression to the mismapped indices — or a
    thead reorder that silently invalidates the indices — fails here.
    """
    html = _template("v29.html")
    # 1) The corrected column reads, verbatim.
    for needle in (
        "const rowInst = row.children[3].textContent.trim();",
        "const rowTF = row.children[4].textContent.trim();",
        "const rowDir = row.children[5].textContent.trim();",
        "const rowSharpe = parseFloat(row.children[10].dataset.sort || 0);",
        "const rowDSR = parseFloat(row.children[11].dataset.sort || 0);",
    ):
        assert needle in html, f"corrected filter column read missing: {needle!r}"
    # 2) The pre-fix mismapped reads must stay gone.
    for stale in (
        "const rowInst = row.children[2]",
        "const rowTF = row.children[3]",
        "const rowDir = row.children[4]",
        "const rowSharpe = parseFloat(row.children[8]",
        "const rowDSR = parseFloat(row.children[9]",
    ):
        assert stale not in html, f"pre-fix mismapped filter column read resurfaced: {stale!r}"
    # 3) The thead order that JUSTIFIES those indices (a reorder must re-derive
    #    the indices, consciously — this half of the pin forces that).
    thead_match = re.search(
        r'<table class="combos-table" id="combosTable">\s*<thead>(.*?)</thead>',
        html,
        re.DOTALL,
    )
    assert thead_match is not None, "#combosTable thead must exist"
    labels = [
        re.sub(r"<[^>]+>", "", cell).strip()
        for cell in re.findall(r"<th[^>]*>(.*?)</th>", thead_match.group(1), re.DOTALL)
    ]
    assert labels == _V29_COMBOS_THEAD_ORDER, (
        f"#combosTable thead order drifted: {labels!r} — the applyFilters "
        "children[3/4/5/10/11] reads must be re-derived against the new order"
    )


# ---------------------------------------------------------------------------
# (c) db.html: in-place tbody patch + delegated pager; wire helpers deleted.
# ---------------------------------------------------------------------------


def test_db_ships_the_in_place_tbody_patch() -> None:
    html = _template("db.html")
    # The guard that authorizes a rows-only swap (any false → full rebuild).
    assert "function patchGridRows(grid, cols, rows, total, off)" in html
    # The ONE sanctioned narrower innerHTML target: the SAME esc()'d builder
    # strings the full rebuild uses, written to tbody instead of the grid.
    assert "tbody.innerHTML = gridRowsHtml(cols, rows);" in html
    # The rows builder is shared by BOTH the full build and the patch.
    assert "function gridRowsHtml(cols, rows)" in html


def test_db_ships_the_delegated_pager_handler() -> None:
    html = _template("db.html")
    # Published page-window state replaces the per-render closure captures.
    assert "const pagerState = { off: 0, lim: 100, onPage: null };" in html
    assert "function setGridPager(body, defLim, onPage)" in html
    # ONE delegated listener on #data resolves pager clicks.
    assert 'ev.target.closest("#dbprev, #dbnext")' in html


def test_db_per_render_pager_wire_helpers_are_gone() -> None:
    """The four per-render ``wire*Pager`` re-attach helpers were deleted —
    a resurrected copy would silently shadow the delegated handler.
    """
    html = _template("db.html")
    for helper in ("wireCandlePager", "wireTablePager", "wireRegistryPager", "wireLedgerPager"):
        assert helper not in html, f"deleted per-render pager helper {helper} must stay gone"


# ---------------------------------------------------------------------------
# (d) movers.html: skip/in-place mechanisms + prebuild-gate pins preserved.
# ---------------------------------------------------------------------------


def test_movers_ships_the_skip_render_signature() -> None:
    html = _template("movers.html")
    assert "let lastRenderSig = null;" in html
    # Signature = full query coordinate + raw body text (client-side only).
    assert 'const sig = p.toString() + "|" + bodyText;' in html
    # Skip is gated to the no-client-sort state so a sorted view never skips.
    assert "if (state.sort.idx === null && sig === lastRenderSig) {" in html


def test_movers_ships_the_gated_paint_once_guard() -> None:
    html = _template("movers.html")
    assert "let wasGated = false;" in html
    assert "let lastGatedKey = null;" in html
    # The paint-once key folds the asset tab + the phase + the server note
    # (the ticking elapsed rides #asof textContent, never a shell repaint —
    # DEC-PREBUILD-GATE-HONEST-NOTE-001).
    assert 'const gatedKey = state.asset + "|" + (j.phase || "") + "|" + (j.note || "");' in html
    assert "if (!(wasGated && gatedKey === lastGatedKey)) {" in html


def test_movers_prebuild_gate_still_uses_textcontent_and_5s_retry() -> None:
    """The DEC-PIPELINE-AUTOPREBUILD-001 gate behavior is byte-preserved: the
    dynamic server note renders via ``textContent`` (never innerHTML) and the
    screen self-recovers on the 5 s auto-retry.
    """
    html = _template("movers.html")
    assert (
        '$("noteText").textContent = j.note || '
        '"pull pipeline running — movers gated until the prebuild finishes.";'
    ) in html
    # The honest gated status line (DEC-PREBUILD-GATE-HONEST-NOTE-001) also
    # renders via textContent only.
    assert '$("asof").textContent = gatedStatus;' in html
    assert "function fmtElapsed(s)" in html
    assert "const GATE_RETRY_MS = 5000;" in html
    assert "gateTimer = setTimeout(load, GATE_RETRY_MS);" in html


def test_movers_ships_in_place_sort_and_delegated_listeners() -> None:
    html = _template("movers.html")
    # In-place arrow/class move on the EXISTING th nodes (no thead rebuild).
    assert "function updateHeadSort()" in html
    # ONE delegated listener each on #pills and the #head row, attached once.
    assert "function onPillsClick(e)" in html
    assert "function onHeadClick(e)" in html
    assert '$("pills").addEventListener("click", onPillsClick);' in html
    assert '$("head").addEventListener("click", onHeadClick);' in html


def test_movers_number_inputs_are_debounced() -> None:
    html = _template("movers.html")
    assert "const DEBOUNCE_MS = 200;" in html
    assert "function debouncedLoad()" in html
    assert '["topn","minvol"].forEach(id => $(id).onchange = debouncedLoad);' in html


# ---------------------------------------------------------------------------
# (e) Security pin: no NEW innerHTML sink per touched template.
# ---------------------------------------------------------------------------

# An innerHTML SINK is an assignment (`el.innerHTML = ...` / `+=`). The raw
# substring count of "innerHTML" is deliberately NOT the pinned quantity: the
# perf fixes added explanatory comments that mention innerHTML, so the raw
# occurrence count rose while the sink count did not — a raw-count pin would
# pin comments, not the security surface.
_INNERHTML_SINK = re.compile(r"\.innerHTML\s*\+?=")

# Baseline sink counts measured at commit aa034f266ba1a9828b7b1c1d46df084af6a42901
# (the pre-fix tip) via
#   git show aa034f266ba1a9828b7b1c1d46df084af6a42901:brutex/dashboard/templates/<name>
# piped through the same regex. Hard-coded so the test stays hermetic (no git
# subprocess at test time).
_BASELINE_INNERHTML_SINKS: dict[str, int] = {
    "coverage.html": 0,
    "fetch_windows.html": 2,
    "expired_options.html": 2,
    "spot_coverage.html": 2,
    "db.html": 27,
    "movers.html": 9,
    "v29.html": 10,
}

# db.html carries EXACTLY ONE sanctioned extra sink over baseline:
# `tbody.innerHTML = gridRowsHtml(cols, rows);` — the SAME already-esc()'d
# builder strings the pre-existing full rebuild writes, against a NARROWER
# target (tbody instead of the whole grid). Explicitly allowed by the
# DEC-DASHBOARD-JS-PERF-001 security invariant; pinned verbatim in
# test_db_ships_the_in_place_tbody_patch above.
# fetch_windows.html carries the SAME sanctioned pattern since
# DEC-WINDOWS-COUNTS-ONLY-001: `tbody.innerHTML = windowsRowsHtml(rows);` — the
# SAME already-esc()'d row-string builder the pre-existing full rebuild writes,
# against the narrower tbody target (pinned verbatim in
# test_fetch_windows_ships_the_in_place_tbody_patch below).
_SANCTIONED_EXTRA_SINKS: dict[str, int] = {"db.html": 1, "fetch_windows.html": 1}


@pytest.mark.parametrize("name", sorted(_BASELINE_INNERHTML_SINKS))
def test_no_new_innerhtml_sink_vs_baseline(name: str) -> None:
    html = _template(name)
    sinks = len(_INNERHTML_SINK.findall(html))
    allowed = _BASELINE_INNERHTML_SINKS[name] + _SANCTIONED_EXTRA_SINKS.get(name, 0)
    assert sinks <= allowed, (
        f"{name}: {sinks} innerHTML assignment sinks > allowed {allowed} "
        f"(baseline {_BASELINE_INNERHTML_SINKS[name]} at aa034f26 "
        f"+ sanctioned {_SANCTIONED_EXTRA_SINKS.get(name, 0)}) — "
        "a NEW innerHTML sink is forbidden (keep esc()/createElement+textContent)"
    )


# ---------------------------------------------------------------------------
# (f) The /coverage/windows payload diet (DEC-WINDOWS-COUNTS-ONLY-001).
#     HONESTY ENVELOPE: source-string pins only — the inline JS is NOT
#     browser-executed here (no JS harness exists in this repo).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["coverage.html", "universe.html"])
def test_counts_only_consumers_request_limit_0(name: str) -> None:
    """The two counts-only consumers append ``?limit=0`` to their windows poll.

    coverage.html's windows KPI card renders ONE integer (``b.count``) and
    universe.html's all-segments line reads ONLY ``.counts`` — before this fix
    each poll downloaded the 20k-row default row list (~294 B/row ⇒ ~5.9 MB
    mid-pull) it never read. Source-pinned, not browser-executed.
    """
    html = _template(name)
    assert "/api/v1/coverage/windows?limit=0" in html, (
        f"{name}: the counts-only poll must request ?limit=0"
    )


def test_fetch_windows_requests_a_sane_page() -> None:
    """fetch_windows.html pages its rows (&limit=&offset=) instead of the 20k default.

    The pager state + querystring wiring + the filter-flip page-1 reset + the
    payload-key fold (a page flip can never be skip-rendered as unchanged).
    Source-pinned, not browser-executed.
    """
    html = _template("fetch_windows.html")
    # The page window drives the querystring.
    assert 'qs.set("limit", String(winLimit()));' in html
    assert 'qs.set("offset", String(winOffset));' in html
    # Static pager DOM patched via textContent/disabled only.
    assert 'id="winpager"' in html
    assert 'id="winprev"' in html
    assert 'id="winnext"' in html
    assert 'id="winpagesize"' in html
    assert "winRange.textContent" in html
    # A filter flip resets to page 1; the memo key folds the page window.
    assert "winOffset = 0; // a fresh filter selection starts at page 1" in html
    assert (
        'marketFilter + "|" + statusFilter + "|" + winLimit() + "|" + winOffset + "|" + txt' in html
    )
    # Stranded-offset snap-back is bounded (re-poll only from a non-zero offset).
    assert "if (!rows.length && winOffset > 0 && (body.matched || 0) > 0) {" in html


def test_fetch_windows_ships_the_in_place_tbody_patch() -> None:
    """fetch_windows.html swaps ONLY the tbody on a changed payload (db.html idiom).

    The ONE sanctioned extra innerHTML sink: the SAME esc()'d row-string builder
    the full rebuild writes (``windowsRowsHtml``), against the narrower tbody
    target — the table shell / sticky header / scroll position survive a poll.
    Source-pinned, not browser-executed.
    """
    html = _template("fetch_windows.html")
    assert "function windowsRowsHtml(rows)" in html
    assert 'panel.querySelector("table > tbody")' in html
    assert "tbody.innerHTML = windowsRowsHtml(rows);" in html
    # The full-rebuild path writes the SAME builder strings.
    assert '"</tr></thead><tbody>" + windowsRowsHtml(rows) + "</tbody></table>"' in html
    # esc() discipline intact on every payload value in the row builder.
    assert "esc(r.symbol)" in html
    assert 'esc(r.last_error || "")' in html
