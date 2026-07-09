"""Pins the ``/universe`` visual-polish + accessibility pass (DEC-UNIVERSE-UI-POLISH-001).

The operator reported the page read as static / misaligned / not touch-friendly.
This redesign is CSS + markup only — every test-pinned id / class / function /
text from ``test_dashboard_universe_template.py`` + ``test_dashboard_universe_ux.py``
is preserved (those suites still pass); this file locks the *new* polish so it
cannot silently regress, mirroring the established substring-guard pattern (the
dashboard JS/CSS has no unit harness).

What this pins:

* a single design-token layer (radius / spacing / control-height / gutter) so
  surfaces align to one rhythm instead of ad-hoc per-element pixels;
* the ``--line`` custom property is finally DEFINED (the date/select inline
  styles referenced it but the palette never declared it → invisible borders);
* the pull toolbar is grouped (``pull-row`` clusters) with a shared control
  height, and carries ``role="toolbar"`` (the ``daterange`` group has since been
  RE-ADDED + wired by DEC-UNIVERSE-PULL-DATERANGE-001; the ``ctl-field``
  granularity chip was later REMOVED by DEC-UNIVERSE-UI-POLISH-002);
* the responsive grid collapses (no fixed-3-col overflow on laptop/tablet/phone);
* the nav is class-driven with an ``aria-current`` "you are here" pill (was
  inline-styled, which blocked hover/focus states);
* instrument rows are keyboard-operable (``role="button"`` + Enter/Space) — they
  had a ``:focus-visible`` style but no ``tabindex`` so keyboard users could not
  reach them;
* empty states carry an icon affordance (``.empty .ei``).
"""

from __future__ import annotations

import re

from brutex.dashboard.app import _render_universe_html


def test_design_tokens_present() -> None:
    """One source of truth for the design tokens.

    The radius / spacing / gutter / elevation tokens moved to the TWO shared
    sheets (bx-theme.css + bx-design.css — DEC-DASHBOARD-VISUAL-OVERHAUL-001
    Batch A deleted the inline :root duplicate block); the page keeps ONLY its
    page-scoped control-height/touch-target tokens inline.
    """
    html = _render_universe_html()
    for token in ("--ctl-h: 38px", "--tap: 40px"):
        assert token in html, f"page-scoped design token missing: {token}"
    assert '<link rel="stylesheet" href="/static/bx-design.css">' in html, (
        "the shared design-system sheet (the radius/spacing/gutter token owner) must be linked"
    )


def test_line_custom_property_is_defined() -> None:
    """``--line`` is declared (the date/select inline styles referenced it but the
    palette never defined it, so their borders rendered with the initial colour)."""
    html = _render_universe_html()
    assert "--line: var(--border-strong)" in html, "the --line custom property is undefined"


def test_pull_toolbar_is_grouped_and_labelled() -> None:
    """The pull bar is a grouped toolbar (lead row + control row), not a ragged wrap."""
    html = _render_universe_html()
    assert 'class="pullbar" role="toolbar"' in html, "the pull bar must be a labelled toolbar"
    assert 'class="pull-row"' in html, "the control row grouping is missing"
    # The from→to spot date GROUP is RE-ADDED + wired (DEC-UNIVERSE-PULL-DATERANGE-001
    # — the pipeline route now threads from_date/to_date into the spot phase). The
    # static ``ctl-field`` granularity chip that shared its shape is REMOVED
    # (DEC-UNIVERSE-UI-POLISH-002 — dead UI noise since 1-minute is the ONLY
    # granularity, DEC-1MINUTE-ONLY-DERIVE-1DAY-001).
    assert 'class="daterange"' in html, "the spot date-range group must be present"
    assert 'class="ctl-field"' not in html, (
        "the granularity chip must stay removed (DEC-UNIVERSE-UI-POLISH-002)"
    )
    # every pull-bar button shares ONE height so the row aligns.
    assert re.search(r"\.pullbtn, \.loadbtn \{[^}]*min-height:var\(--ctl-h\)", html), (
        "the shared button height that aligns the toolbar is missing"
    )


def test_grid_is_responsive() -> None:
    """The 3-pane drill collapses instead of squashing on small screens."""
    html = _render_universe_html()
    assert "@media (max-width:1100px)" in html, "the tablet grid breakpoint is missing"
    assert "@media (max-width:720px)" in html, "the phone grid breakpoint is missing"


def test_nav_is_class_driven_with_current_marker() -> None:
    """The nav uses classes (hover/focus work) + marks the active section."""
    html = _render_universe_html()
    assert 'class="brandmark"' in html, "the brand mark is missing"
    # the PRIMARY nav is now class-driven (no inline style on its links → hover +
    # focus states work) and marks the active section with aria-current.
    assert '<nav class="topnav" aria-label="Primary">' in html, (
        "the primary nav landmark is missing"
    )
    assert '<a href="/universe" aria-current="page">Universe</a>' in html, (
        "the /universe page must mark its OWN nav link active (not /db), and a "
        "Universe link must exist so other pages can navigate back"
    )
    # The nav hover affordance lives in the SHARED bx-theme.css header rules
    # (the page's duplicate .topnav skin was removed —
    # DEC-DASHBOARD-VISUAL-OVERHAUL-001 Batch A; one source, Golden Rule #11).
    assert '<link rel="stylesheet" href="/static/bx-theme.css">' in html, (
        "the shared theme sheet that owns the nav hover affordance must be linked"
    )


def test_instrument_rows_are_keyboard_operable() -> None:
    """Rows are Tab-reachable + Enter/Space-operable (without an invalid nested
    ``role="button"`` — the row contains an independent multi-select checkbox)."""
    html = _render_universe_html()
    assert "d.tabIndex = 0" in html, "rows are not focusable (no tabindex)"
    assert "d.onkeydown" in html, "rows have no keyboard activation handler"
    assert 'd.setAttribute("aria-label"' in html, "rows carry no descriptive label"
    # a button must NOT contain interactive descendants — the checkbox is one, so
    # the row deliberately stays a labelled focusable element, not a button.
    assert 'd.setAttribute("role", "button")' not in html, (
        "the row must not claim role=button while containing a checkbox"
    )


def test_empty_states_have_icon_affordance() -> None:
    """Empty panes carry an icon so they read as intentional, not broken."""
    html = _render_universe_html()
    assert ".empty .ei" in html, "the empty-state icon style is missing"
    assert 'class="ei" aria-hidden="true"' in html, "no empty-state icon is rendered"
