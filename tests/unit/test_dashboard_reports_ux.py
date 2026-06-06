"""Guards the ``/reports`` visual upgrade (DASH-UX D3, DEC-DASHBOARD-REPORTS-UX-001).

DASH-UX page 3 — the pre-open Excel-export page. The dashboard JS has no unit
harness, so these substring guards lock the markup + wiring in CI (the same
established pattern as ``test_dashboard_universe_ux.py`` /
``test_dashboard_db_ux.py``). Each guard pins one additive, presentational
upgrade against the rendered ``_render_reports_html``:

* a **sticky, frosted header** (matches /db D2 — the nav stays put while the
  symbol grid scrolls);
* **skeleton-shimmer** chips while the symbol list loads (matches /universe D1);
* an **animated count-up "pop"** when the selected count changes;
* **empty-state art** (a floating glyph) for the no-symbols / load-failure
  states instead of a bare sentence;
* the stale ``/logs`` reference (a page removed in #426) is **gone** — the error
  message no longer points users at a 404.

All additive: no route, data-contract, or download-form change.
"""
# pyright: reportPrivateUsage=none
# (whitebox: these guards render the dashboard's own ``_render_reports_html``.)

from __future__ import annotations

import re

from brutex.dashboard.app import _render_reports_html


def test_header_is_sticky_and_frosted() -> None:
    """The header is ``position:sticky`` with a blur backdrop (matches /db D2)."""
    html = _render_reports_html()
    rule = re.search(r"\bheader \{[^}]*\}", html, re.DOTALL)
    assert rule is not None, "the header CSS rule is missing"
    block = rule.group(0)
    assert "position:sticky" in block, "the header must be sticky"
    assert "backdrop-filter:blur" in block, "the header must have a frosted blur backdrop"


def test_skeleton_chips_on_load() -> None:
    """The symbol list shows skeleton-shimmer chips while it loads."""
    html = _render_reports_html()
    assert ".skel-chip" in html, "the skeleton-chip style is missing"
    assert "function showSkeleton" in html, "the skeleton helper is missing"
    assert "showSkeleton();" in html, "loadSymbols must show the skeleton before fetching"
    # The bare 'loading symbols…' text is replaced by the skeleton.
    assert ">loading symbols…<" not in html, (
        "the bare loading text must be replaced by the skeleton"
    )


def test_animated_count_pop() -> None:
    """The selected-count gets a brief animated 'pop' on every change."""
    html = _render_reports_html()
    assert "@keyframes countPop" in html, "the count-pop animation is missing"
    assert ".count.pop" in html, "the count-pop trigger class is missing"
    assert 'countEl.classList.add("pop")' in html, "refreshCount must trigger the pop"


def test_empty_state_art_present() -> None:
    """The no-symbols + load-failure states show a floating art glyph, not a bare line."""
    html = _render_reports_html()
    assert "@keyframes floatArt" in html, "the empty-state float animation is missing"
    assert '<span class="art">' in html, "the empty-state art glyph is missing"
    # Both the no-data and the fetch-failure branches use the art empty-state.
    assert html.count('class="empty"><span class="art">') >= 2, (
        "both the no-symbols and the load-failure empty states should carry the art glyph"
    )


def test_stale_logs_reference_is_gone() -> None:
    """The dead ``/logs`` page reference (removed in #426) is no longer shown to users.

    The constituents-fetch error message used to read "(see /logs)", pointing at a
    page that 404s since DEC-DASHBOARD-LOGS-REMOVE-001. This pins that it is gone
    so the page never sends a user to a dead link.
    """
    html = _render_reports_html()
    assert "(see /logs)" not in html, "the error message must not point at the removed /logs page"
    assert "check the server logs" in html, "the reworded constituents-error message is missing"


def test_download_form_and_routes_unchanged() -> None:
    """The upgrade is additive — the export form + routes are intact."""
    html = _render_reports_html()
    for needle in (
        "/api/v1/reports/preopen/symbols",
        "/api/v1/reports/preopen.zip?",
        "/api/v1/universe/index-constituents?symbol=",
        'id="idxpick"',
        'name="mode"',
        'id="download"',
    ):
        assert needle in html, f"a pre-existing /reports control regressed: {needle}"
