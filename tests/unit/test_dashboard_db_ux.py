"""Guards the ``/db`` visual upgrade (DASH-UX D2, DEC-DASHBOARD-DB-UX-001).

DASH-UX page 2 — the read-only DuckDB browser. The dashboard JS has no unit
harness, so these substring guards lock the markup + wiring in CI (the same
established pattern as ``test_dashboard_universe_ux.py``). Each guard pins one
additive, presentational upgrade against the rendered ``_render_db_html``:

* a **sticky, frosted header** (the nav + db-path stay put while content scrolls
  under it);
* **column-type chips** in every ``th`` (⏱ time · # numeric · A text), inferred
  from the column NAME — cosmetic only, the backend re-validates;
* **zebra striping** + a brighter row hover for dense rows;
* **click-to-copy** cells (delegated on ``#data`` so it survives re-renders) with
  a green flash + a "copied" toast;
* **empty-state art** (a floating glyph) instead of a bare sentence.

All additive: no route, data-contract, or query-control change — the existing
structured-query + candle-lake wiring is untouched.
"""
# pyright: reportPrivateUsage=none
# (whitebox: these guards render the dashboard's own ``_render_db_html``.)

from __future__ import annotations

import re

from brutex.dashboard.app import _render_db_html


def test_header_is_sticky_and_frosted() -> None:
    """The header is ``position:sticky`` with a blur backdrop so it never scrolls away."""
    html = _render_db_html()
    rule = re.search(r"\bheader \{[^}]*\}", html, re.DOTALL)
    assert rule is not None, "the header CSS rule is missing"
    block = rule.group(0)
    assert "position:sticky" in block, "the header must be sticky"
    assert "backdrop-filter:blur" in block, "the header must have a frosted blur backdrop"


def test_column_type_chips_present_and_wired() -> None:
    """Header cells render a column-type chip keyed off a name heuristic."""
    html = _render_db_html()
    # The heuristic + the three chip classes.
    assert "function colType" in html, "the column-type inference helper is missing"
    for cls in (".tchip.t-num", ".tchip.t-time", ".tchip.t-txt"):
        assert cls in html, f"the type-chip style {cls} is missing"
    # The chip is injected into every <th> at grid build time.
    assert 'class="tchip ' in html, "the type chip is not rendered in the header cells"
    assert "const t = colType(c);" in html, "the header build does not call colType per column"


def test_zebra_striping_and_hover_present() -> None:
    """Dense rows get zebra striping; hover stays brighter (declared after zebra)."""
    html = _render_db_html()
    assert "tbody tr:nth-child(even)" in html, "zebra striping is missing"
    assert "tbody tr:hover" in html, "the row hover rule is missing"
    # Hover must be declared AFTER the zebra rule so it wins on hover.
    assert html.index("tbody tr:nth-child(even)") < html.index("tbody tr:hover"), (
        "the hover rule must come after the zebra rule so hover overrides the stripe"
    )


def test_click_to_copy_cells_wired() -> None:
    """Cells are click-to-copy: a delegated listener, the flash, and the toast."""
    html = _render_db_html()
    # Delegated on #data (survives re-render) and skips null cells.
    assert 'dataEl.addEventListener("click"' in html, "the copy-cell listener is missing"
    assert 'ev.target.closest("td")' in html, "the listener does not target a cell"
    assert 'td.classList.contains("nullcell")' in html, (
        "null cells must be skipped (nothing to copy)"
    )
    # Feature-detected clipboard write — never throws on insecure/old contexts.
    assert "navigator.clipboard && navigator.clipboard.writeText" in html, (
        "clipboard access must be feature-detected"
    )
    # The flash class + the toast.
    assert "td.copied" in html, "the copied-cell flash style is missing"
    assert "function showCopyToast" in html, "the copy toast helper is missing"
    assert 'id="copytoast"' in html, "the copy toast element is missing"


def test_empty_state_art_present() -> None:
    """The 'select a table' empty state shows a floating art glyph, not a bare line."""
    html = _render_db_html()
    assert 'class="empty emptyart"' in html, "the empty-state art container is missing"
    assert 'class="art"' in html, "the empty-state art glyph is missing"
    assert "@keyframes floatArt" in html, "the empty-state float animation is missing"


def test_query_controls_and_routes_unchanged() -> None:
    """The structured-query + candle wiring is intact.

    The DuckDB structured-query URL is built from the ``DB_API`` base
    (``DB_API + "/query/"``); the ``/api/v1/db/query/`` route is unchanged
    server-side. (The live-lake ``LIVE_API`` alias was removed with the LIVE
    feature — DEC-LIVE-REMOVE-001, MIGRATION.md #859.)
    """
    html = _render_db_html()
    for needle in (
        "/api/v1/db/tables",
        'const DB_API = "/api/v1/db";',
        'DB_API + "/query/"',
        "/api/v1/db/candles/datasets",
        "function runTableQuery",
        "function runCandleQuery",
        'id="applyq"',
    ):
        assert needle in html, f"a pre-existing /db control regressed: {needle}"


def test_option_dataset_label_rendered_with_raw_series_fallback() -> None:
    """Option datasets render the decoded label (DEC-DB-OPTION-DATASET-DECODE-001).

    Both the picker option text and the grid title prefer the server-decoded
    ``d.label`` (``BANKNIFTY · MONTH#1 ATM-10 CALL · 1m``) and fall back to the
    raw series when no label arrives (an older API) — display-only, the
    ``series`` query key is untouched (guarded below).
    """
    html = _render_db_html()
    # The picker option text (candleControlsHtml).
    assert '"[option] " + (d.label || (d.series + "·" + d.interval))' in html, (
        "the dataset picker must prefer the decoded label with a raw-series fallback"
    )
    # The grid title (renderCandleView).
    assert 'title = esc(d.label || (d.series + " · " + d.interval));' in html, (
        "the grid title must prefer the decoded label with a raw-series fallback"
    )
    # The QUERY key stays the raw series — dsKey/buildCandleUrl byte-unchanged.
    assert 'if (d.kind === "option") return "option:" + d.series;' in html, (
        "dsKey must keep addressing the raw series key"
    )
    assert '"/api/v1/db/options?series=" + encodeURIComponent(d.series)' in html, (
        "the option query URL must keep addressing the raw series key"
    )


def test_greeks_render_broker_style_display_only() -> None:
    """Greeks cells render broker-style decimals at DISPLAY time only
    (DEC-DB-UX-PACK-001 Feature 4): the payload arrives FULL PRECISION (stored
    ``iv``/statistics are never rounded — CLAUDE.md §4); ``fmtCell`` formats
    ``iv`` as PERCENT 2dp (fraction × 100 → "15.23") and delta 3dp / gamma 5dp /
    theta 2dp / vega 3dp / rho 2dp. The provenance columns (``spot_at_bar`` /
    ``t_years_used`` / ``rate_used`` / ``greeks_provenance_id``) are excluded by
    the exact map literal — they render verbatim."""
    html = _render_db_html()
    # The exact broker-decimals map — its literal excludes the provenance columns.
    assert "const GREEK_DP = { delta: 3, gamma: 5, theta: 2, vega: 3, rho: 2 };" in html, (
        "the broker-style Greeks display-decimals map is missing or drifted"
    )
    # iv renders as a PERCENT to 2dp (stored as a fraction).
    assert 'col === "iv"' in html, "fmtCell must special-case the iv column"
    assert "(n * 100).toFixed(2)" in html, "iv must render as percent 2dp (fraction × 100)"
    # Display-only: fmtCell formats at render; the map drives the fixed decimals.
    assert "n.toFixed(GREEK_DP[col])" in html, "the Greeks must render via the GREEK_DP map"


def test_column_picker_stays_removed_everywhere() -> None:
    """The COLUMNS checkbox strips stay REMOVED on every /db tab
    (DEC-DB-UX-PACK-001 Feature 1): every column is always visible — no colbox,
    no column checkboxes, no dead column-visibility state, and no ``column=``
    projection ever emitted (omitting it returns all columns server-side)."""
    html = _render_db_html()
    for gone in (
        'class="colbox"',
        ".colbox {",
        'class="qcol"',
        'class="ccol"',
        "curCols",
        "cstate.cols",
        "st.cols",
        '"&column=" + encodeURIComponent',
        'id="capply"',
    ):
        assert gone not in html, f"the removed column picker leaked back: {gone!r}"


def test_candle_grid_carries_the_prev_close_anchor_footnote() -> None:
    """DEC-PREVSESSION-MUHURAT-COHERENCE-001 — the STATIC display-law footnote
    ships beside the candle grid: the /db derived %-change anchors on the
    nearest STORED prior bar INCLUDING a Diwali Muhurat session's close (the
    display baseline law), while the engine's day anchors deliberately use the
    prior REGULAR session (#1616). Template-only — zero payload/backend change;
    the per-row marker is the named follow-up DEC-DB-MUHURAT-PREVCLOSE-MARK-001."""
    html = _render_db_html()
    assert (
        "%CHNG anchors on the nearest stored prior bar "
        "(including a Diwali Muhurat session\\'s close)."
    ) in html
