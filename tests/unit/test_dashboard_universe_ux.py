"""Guards the ``/universe`` ease-of-use controls (DEC-UNIVERSE-MULTISELECT-001).

The operator hit a scroll round-trip (scroll down to select, scroll up to pull)
and could not pull several instruments at once. This pins the fixes against the
rendered template string (the dashboard JS has no unit harness, so these
substring guards are the established way to lock the markup + wiring in CI,
mirroring ``test_dashboard_universe_template.py``):

* the pull bar is **sticky** (never scrolls out of view);
* every row has a **multi-select checkbox** that toggles ``selMulti`` WITHOUT
  triggering the row's single-select (``stopPropagation``);
* a **"⤓ Pull N selected"** button + **select-all-shown** / **clear** controls;
* the 19 intraday timeframes are **auto-derived** from the 1-minute base, made
  explicit (the operator asked "where do I click to generate timeframes?").
"""

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


def test_multi_select_controls_present() -> None:
    """The checkbox + the Pull-N-selected + select-all/clear controls are rendered."""
    html = _render_universe_html()
    for needle in ('id="pullmulti"', 'id="selallbtn"', 'id="clrselbtn"', 'class="msel"'):
        assert needle in html, f"missing multi-select control: {needle}"


def test_multi_select_wiring_present() -> None:
    """The state + toggle + the sequential 'pull each' loop are wired."""
    html = _render_universe_html()
    assert "selMulti" in html, "the multi-select state is missing"
    assert "function toggleMulti" in html
    assert "function updateMultiBtn" in html
    # the per-row checkbox must NOT trigger the row's single-select (independent)
    assert "stopPropagation" in html, "the checkbox must stopPropagation off the row click"
    # 'Pull N selected' pulls each in turn via the existing (tested) runPull
    assert "await runPull(" in html, "the multi-pull must reuse runPull sequentially"


def test_timeframe_derivation_is_explained() -> None:
    """The page makes clear the intraday timeframes auto-derive (no manual step)."""
    html = _render_universe_html()
    assert "auto-derive from the 1-minute base" in html, (
        "the page must state the 19 intraday timeframes auto-derive (no 'generate' step)"
    )
