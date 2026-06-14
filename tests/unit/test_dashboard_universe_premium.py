"""Pins the ``/universe`` premium custom controls (DEC-UNIVERSE-PREMIUM-CONTROLS-001).

The operator's expectation was a premium, TradingView-grade UI; the browser-native
``<input type="date">`` calendar + ``<select>`` dropdowns read as legacy because
they are unstylable OS widgets. This replaces them with custom, animated
components (a dual-month date-range picker with presets + custom select
dropdowns) that DRIVE the still-present, test-pinned native controls (kept hidden
as the value source of truth) — pure progressive enhancement.

This locks the new components so they cannot silently regress, mirroring the
established substring-guard pattern, and re-asserts that the native pins survive.
"""

from __future__ import annotations

from brutex.dashboard.app import _render_universe_html


def test_custom_daterange_picker_present() -> None:
    """The custom date-range picker CSS + JS enhancer ship."""
    html = _render_universe_html()
    assert ".dp-pop" in html, "the date-range popover style is missing"
    assert ".dp-day" in html, "the calendar day-cell style is missing"
    assert "function enhanceCalendar" in html, "the calendar enhancer is missing"
    assert 'data-day="' in html, "the calendar grid day cells are not built"
    # presets that make a multi-year range usable in one click.
    assert "Full history" in html, "the quick-range presets are missing"


def test_custom_select_dropdowns_present() -> None:
    """The native selects are replaced by custom animated dropdowns."""
    html = _render_universe_html()
    assert ".cs-menu" in html, "the custom-select menu style is missing"
    assert "function enhanceSelect" in html, "the select enhancer is missing"
    # the granularity + derivatives selects are all enhanced.
    assert '["spotInterval","derivInterval","derivType"]' in html, (
        "the enhanced-select id list is missing"
    )


def test_native_controls_remain_as_value_source() -> None:
    """Progressive enhancement: the test-pinned native controls still exist
    underneath (hidden) so the pull JS reads real values + the pins hold."""
    html = _render_universe_html()
    # native date inputs (the picker drives these).
    assert 'id="fromDate" min="2020-01-01"' in html
    assert 'id="toDate" min="2020-01-01"' in html
    # native granularity select (the dropdown drives this).
    assert 'id="spotInterval"' in html
    assert '<option value="1day" selected>' in html
    # the enhancers do not remove the native nodes (they hide + sync them).
    assert 'sel.style.display = "none"' in html or 'sel.style.display="none"' in html


def test_picker_has_drill_nav_and_tabs() -> None:
    """The picker matches the TradingView/Go-to UX: Date + Custom-range tabs and
    a day->month->year(decade) DRILL header so any year is two clicks away."""
    html = _render_universe_html()
    # tabs.
    assert 'data-m="range"' in html, "the Custom-range tab is missing"
    assert "Custom range" in html, "the Custom-range tab label is missing"
    # drill levels: header drill button + month-grid + year(decade)-grid cells.
    assert "data-drill" in html, "the header drill button is missing"
    assert "data-mon" in html, "the month-grid drill cells are missing"
    assert "data-yr" in html, "the year(decade)-grid drill cells are missing"
    # the decade label helper (e.g. 2020 - 2039).
    assert "function decade" in html, "the decade drill helper is missing"
