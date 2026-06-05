"""Guards the ``/universe`` ease-of-use controls (DEC-UNIVERSE-MULTISELECT-001).

The operator hit a scroll round-trip (scroll down to select, scroll up to pull)
and could not pull several instruments at once. This pins the fixes against the
rendered template string (the dashboard JS has no unit harness, so these
substring guards are the established way to lock the markup + wiring in CI,
mirroring ``test_dashboard_universe_template.py``):

* the pull bar is **sticky** (never scrolls out of view);
* every row has a **multi-select checkbox** that toggles ``selMulti`` WITHOUT
  triggering the row's single-select (``stopPropagation``);
* ONE unified **"⤓ Pull spot"** button (DEC-UNIVERSE-PULL-UNIFY-001 — the old
  separate ``pullmulti`` "⤓ Pull N selected" button is gone; the single
  ``pullbtn`` now drives one OR many) + **select-all-shown** / **clear**
  controls;
* the page states the pull **stores 1m + 1d only** and the 19 intraday
  timeframes are **derived on demand, not written to the drive** (pre-
  materialising is a future external-drive step) — the operator asked "where do
  I click to generate timeframes?" and wants no local-drive bloat.
"""
# pyright: reportPrivateUsage=none
# (whitebox: these guards render the dashboard's own ``_render_universe_html``.)

from __future__ import annotations

import re

from brutex.dashboard.app import _render_universe_html


def test_pull_bar_is_sticky() -> None:
    """The pull bar is ``position:sticky`` so the date + pull buttons never scroll away."""
    html = _render_universe_html()
    rule = re.search(r"\.pullbar \{[^}]*\}", html, re.DOTALL)
    assert rule is not None, "the .pullbar CSS rule is missing"
    assert "position:sticky" in rule.group(0), (
        "the pull bar must be sticky so it stays visible while scrolling the list"
    )


def test_accessibility_focus_ring_and_reduced_motion_present() -> None:
    """The page carries keyboard focus rings + honours reduce-motion
    (DEC-DASHBOARD-A11Y-POLISH-001).

    Both are safe, additive polish: ``:focus-visible`` shows the ring ONLY for
    keyboard users (mouse clicks never trigger it → no visual/layout change for
    mouse), and ``prefers-reduced-motion`` only applies when the OS opts in. This
    pins them so the accessibility polish cannot silently regress.
    """
    html = _render_universe_html()
    assert ":focus-visible" in html, "keyboard focus rings (:focus-visible) are missing"
    assert "prefers-reduced-motion" in html, "the reduce-motion media query is missing"
    assert "outline: 2px solid var(--blue)" in html, "the focus-ring outline rule is missing"


def test_multi_select_controls_present() -> None:
    """The checkbox + the unified pull + select-all/clear controls are rendered."""
    html = _render_universe_html()
    for needle in ('id="pullbtn"', 'id="selallbtn"', 'id="clrselbtn"', 'class="msel"'):
        assert needle in html, f"missing multi-select control: {needle}"


def test_pull_button_is_unified_single_or_many() -> None:
    """ONE "⤓ Pull spot" button drives single OR multi (DEC-UNIVERSE-PULL-UNIFY-001).

    The operator asked "why we have two pull spot" — the separate ``pullmulti``
    "⤓ Pull N selected" button is removed and the single ``pullbtn`` now pulls
    the highlighted instrument when nothing is ticked, or every ticked row in
    turn when rows are selected. This guards: (a) the old button is GONE, so a
    null ``getElementById("pullmulti").onclick`` can never throw at page load;
    (b) the unified label + selection helper + re-entrancy guard are wired.
    """
    html = _render_universe_html()
    # (a) the second "Pull spot" button must be gone — single source of truth.
    assert 'id="pullmulti"' not in html, (
        "the separate multi-pull button must be removed (one unified Pull spot)"
    )
    assert 'getElementById("pullmulti")' not in html, (
        "no code may reference the removed pullmulti element (would throw at load)"
    )
    assert "Pull N selected" not in html and "Pull 0 selected" not in html, (
        "the old 'Pull N/0 selected' button text must be gone"
    )
    # (b) the unified wiring: the selection helper, the in-flight guard, and the
    # single button's label all reference the one pullbtn.
    assert "function spotSelection" in html, "the one-or-many selection helper is missing"
    assert "spotPullInFlight" in html, "the double-click re-entrancy guard is missing"
    assert "⤓ Pull spot" in html, "the unified button label is missing"


def test_options_button_is_unified_single_or_many() -> None:
    """ONE "⛓ Pull options chain" drives single OR ticked-many
    (DEC-UNIVERSE-OPTIONS-MULTIPULL-001).

    Mirrors the unified spot button: nothing ticked + a single highlight → pull
    that one underlying's option grid via ``?symbol=``; ticked rows → send the
    whole selection as ``?symbols=a,b,c`` so the backend fans them all in one
    pass. Guards the selection helper, the multi label, and the ``?symbols=``
    request shape so the wiring cannot silently regress to single-only.
    """
    html = _render_universe_html()
    assert "function optionSelection" in html, "the options one-or-many selection helper is missing"
    assert "pull-options?symbols=" in html, "the multi-underlying ?symbols= request is not wired"
    assert "pull-options?symbol=" in html, "the single-underlying ?symbol= path must remain"
    assert "⛓ Pull options chain · " in html, (
        "the options button must show the selected-count label when rows are ticked"
    )


def test_multi_select_wiring_present() -> None:
    """The state + toggle + the sequential 'pull each' loop are wired."""
    html = _render_universe_html()
    assert "selMulti" in html, "the multi-select state is missing"
    assert "function toggleMulti" in html
    assert "function updateMultiBtn" in html
    # the per-row checkbox must NOT trigger the row's single-select (independent)
    assert "stopPropagation" in html, "the checkbox must stopPropagation off the row click"
    # the unified pull pulls each in turn via the existing (tested) runPull
    assert "await runPull(" in html, "the multi-pull must reuse runPull sequentially"


def test_timeframe_storage_is_explained_honestly() -> None:
    """The page states only 1m+1d are stored; intraday TFs are derived, not written to disk."""
    html = _render_universe_html()
    assert "fetches + stores 1-minute + 1-day only" in html, (
        "the page must state the pull stores ONLY 1m + 1d (no 19-timeframe local-drive bloat)"
    )
    assert "derived on demand, NOT written to your drive" in html, (
        "the page must state the intraday timeframes are derived on demand, not materialised; "
        "pre-materialising is a future external-drive (BRUTEX_LAKE_ROOT) step"
    )
    assert "BRUTEX_LAKE_ROOT" in html, "the external-drive config knob must be named"


def test_intrusive_tf_dropdown_stays_removed_but_clean_button_replaces_it() -> None:
    """The intrusive auto-appearing TF *dropdown* stays GONE; a single clean
    "⚙ Pre-build timeframes" button replaces it (DEC-UNIVERSE-TF-MATERIALIZE-BUTTON-001
    amends DEC-UNIVERSE-TFS-DROPDOWN-REMOVE-001).

    History: the operator first hit a confusing, intrusive ``TFs`` multi-select +
    "⚙ Derive timeframes" button that auto-appeared in the pull bar the moment an
    instrument was selected ("why suddenly this small tfs ... just remove that")
    → it was removed, keeping the materialise *backend*
    (``/api/v1/universe/derive-timeframes`` + ``timeframe_materialize``,
    DEC-TF-MATERIALIZE-001). The operator then explicitly asked for a
    pre-build-timeframes affordance back — so this adds ONE always-present,
    non-intrusive ``matbtn`` button (NOT the auto-appearing ``tfsel``/``derivetfs``
    dropdown). This guard pins BOTH halves: the dropdown cannot creep back, and
    the clean button is wired to the route.
    """
    html = _render_universe_html()
    # The intrusive dropdown + its auto-appearing widgets stay removed.
    for gone in ('id="derivebtn"', 'id="tfsel"', 'id="derivetfs"', "⚙ Derive timeframes"):
        assert gone not in html, (
            f"the intrusive derive-timeframes dropdown control {gone!r} must stay removed "
            "(DEC-UNIVERSE-TFS-DROPDOWN-REMOVE-001)"
        )
    assert 'getElementById("derivebtn")' not in html, (
        "no code may reference the removed dropdown button (would throw at load)"
    )
    # The clean replacement button IS present + wired to the (kept) backend route.
    assert 'id="matbtn"' in html, "the single '⚙ Pre-build timeframes' button must be present"
    assert "⚙ Pre-build timeframes" in html, "the clean materialise button label is missing"
    assert "function matSelection" in html, (
        "the one-or-many materialise selection helper is missing"
    )
    assert "matInFlight" in html, "the materialise double-click re-entrancy guard is missing"
    assert "derive-timeframes?symbol=" in html, (
        "the clean button must POST the (kept) materialise route"
    )
