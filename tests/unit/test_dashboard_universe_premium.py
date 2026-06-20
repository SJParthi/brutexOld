"""Pins the shared date-picker component wiring on ``/universe``
(DEC-UNIVERSE-PREMIUM-CONTROLS-001 + DEC-UNIVERSE-DATEPICKER-SHARED-001).

The custom calendar + dropdowns now live in ONE shared static asset
(``brutex/dashboard/static/bx-datepicker.{js,css}``) loaded by every dashboard
page (no inline copy — the duplication gate stays clean). This locks:

* the picker internals live in the static component (read the file directly);
* ``/universe`` WIRES the component — links the asset, marks its range container
  ``data-bxdp="range"`` + its selects ``data-bxsel``, and keeps the native
  ``<input type=date>``/``<select>`` as the hidden value source (every prior pin
  survives).
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


def test_universe_wires_the_shared_component() -> None:
    """/universe links the asset + marks its range container and selects."""
    html = _render_universe_html()
    assert "/static/bx-datepicker.css" in html, "the component stylesheet is not linked"
    assert "/static/bx-datepicker.js" in html, "the component script is not linked"
    # the from->to RANGE container. The granularity dropdown was retired by
    # DEC-1MINUTE-ONLY-DERIVE-1DAY-001 (1-minute is the only granularity fetched; 1-day is
    # derived from it), and the two derivatives dropdowns (interval / contract-type) were
    # removed when the bulk derivative buttons became metadata-only
    # (DEC-DERIV-DECOMMISSION-FUSED-001) — so NO ``data-bxsel`` select remains to wire.
    assert 'data-bxdp="range" data-from="fromDate" data-to="toDate"' in html, (
        "the date-range container is not wired to the component"
    )
    assert html.count("data-bxsel") == 0, "no granularity select remains (the dropdown is retired)"
    # the inline picker must be GONE (single source of truth in the static file).
    assert "function enhanceCalendar" not in html and "function buildCalendar" not in html, (
        "the picker JS must not be inlined in the template (it lives in the static asset)"
    )


def test_universe_keeps_native_controls_as_value_source() -> None:
    """Progressive enhancement: the test-pinned native date inputs still exist
    so the pull JS reads real values + the pins hold. The granularity dropdown is
    retired (DEC-1MINUTE-ONLY-DERIVE-1DAY-001 — 1-minute is the only fetched granularity)."""
    html = _render_universe_html()
    assert 'id="fromDate" min="2020-01-01"' in html
    assert 'id="toDate" min="2020-01-01"' in html
    assert 'id="spotInterval"' not in html  # the granularity dropdown is removed
