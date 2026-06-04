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
* the page states the pull **stores 1m + 1d only** and the 19 intraday
  timeframes are **derived on demand, not written to the drive** (pre-
  materialising is a future external-drive step) — the operator asked "where do
  I click to generate timeframes?" and wants no local-drive bloat.
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
