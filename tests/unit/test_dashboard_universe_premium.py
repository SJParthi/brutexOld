"""Pins the shared date-picker component wiring on ``/universe``
(DEC-UNIVERSE-PREMIUM-CONTROLS-001 + DEC-UNIVERSE-DATEPICKER-SHARED-001).

The custom calendar + dropdowns now live in ONE shared static asset
(``brutex/dashboard/static/bx-datepicker.{js,css}``) loaded by every dashboard
page (no inline copy — the duplication gate stays clean). This locks:

* the picker internals live in the static component (read the file directly);
* ``/universe`` LINKS the shared component asset (other pages — e.g. ``/`` reports
  — still wire a real range container to it). The ``/universe`` from→to range
  container ITSELF was REMOVED by DEC-UNIVERSE-PULL-CONSOLIDATE-001 (FIX-FIRST
  follow-up): the consolidated pipeline button accepts no from/to, so an inert
  picker was dead/misleading UI. The asset links stay (the component simply finds
  no range container to enhance on this page), so the shared single source of truth
  is unchanged.
"""

from __future__ import annotations

from pathlib import Path

import brutex.dashboard
from brutex.dashboard.app import _render_universe_html

_STATIC = Path(brutex.dashboard.__file__).resolve().parent / "static"
_JS = (_STATIC / "bx-datepicker.js").read_text(encoding="utf-8")
_CSS = (_STATIC / "bx-datepicker.css").read_text(encoding="utf-8")


def test_component_files_exist_and_carry_the_picker_internals() -> None:
    """The shared static component holds the drill calendar + custom dropdown."""
    assert ".dp-pop" in _CSS and ".dp-day" in _CSS, "the picker styles are missing"
    assert ".cs-menu" in _CSS, "the custom-select menu style is missing"
    # the drill day->month->year(decade) navigation.
    assert "function buildCalendar" in _JS, "the calendar builder is missing"
    assert "data-drill" in _JS and "data-mon" in _JS and "data-yr" in _JS, (
        "the drill nav is missing"
    )
    assert "function decade" in _JS, "the decade drill helper is missing"
    assert "Custom range" in _JS and "Full history" in _JS, "the tabs/presets are missing"
    assert "function buildSelect" in _JS, "the custom-select enhancer is missing"


def test_component_is_progressive_enhancement() -> None:
    """The component only HIDES the native controls (the value source) + carries
    the premature-close fix + auto-wires dynamic inputs."""
    assert 'style.display = "none"' in _JS, (
        "the native controls are not hidden (they must stay as value source)"
    )
    assert "e.stopPropagation()" in _JS, (
        "the premature-close fix (inside-click stopPropagation) is missing"
    )
    assert "MutationObserver" in _JS, (
        "dynamically-injected date inputs (e.g. /db) won't be enhanced"
    )
    assert "wireLooseInput" in _JS, "the universal every-date-input enhancement is missing"
    assert "window.BXDatePicker" in _JS, "the public API is missing"


def test_universe_links_the_shared_component() -> None:
    """/universe still LINKS the shared picker asset (other pages reuse it).

    The from→to RANGE container itself was REMOVED by the pull consolidation
    (DEC-UNIVERSE-PULL-CONSOLIDATE-001 FIX-FIRST follow-up — the pipeline accepts no
    from/to, so the picker was dead UI), so there is NO ``data-bxdp`` container left
    to wire on this page; the asset links stay (harmless — the component finds no
    range container to enhance) so the shared single source of truth is unchanged.
    The granularity dropdown was retired by DEC-1MINUTE-ONLY-DERIVE-1DAY-001, and the
    two derivatives dropdowns were removed with the bulk derivative buttons
    (DEC-DERIV-DECOMMISSION-FUSED-001) — so NO ``data-bxsel`` select remains either.
    """
    html = _render_universe_html()
    assert "/static/bx-datepicker.css" in html, "the component stylesheet is not linked"
    assert "/static/bx-datepicker.js" in html, "the component script is not linked"
    assert 'data-bxdp="range"' not in html, "the dead date-range container must be removed"
    assert html.count("data-bxsel") == 0, "no granularity select remains (the dropdown is retired)"
    # the inline picker must be GONE (single source of truth in the static file).
    assert "function enhanceCalendar" not in html and "function buildCalendar" not in html, (
        "the picker JS must not be inlined in the template (it lives in the static asset)"
    )


def test_universe_has_no_dead_date_inputs() -> None:
    """The native from/to date inputs are GONE (the consolidated pipeline takes no
    from/to). The granularity dropdown is likewise retired
    (DEC-1MINUTE-ONLY-DERIVE-1DAY-001 — 1-minute is the only fetched granularity)."""
    html = _render_universe_html()
    assert 'id="fromDate"' not in html
    assert 'id="toDate"' not in html
    assert 'id="spotInterval"' not in html  # the granularity dropdown is removed
