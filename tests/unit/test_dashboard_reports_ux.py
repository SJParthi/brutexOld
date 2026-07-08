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


def test_reports_date_pickers_prefilled_with_canonical_default() -> None:
    """DEC-DEFAULT-DATE-RANGE-001: the /reports From/To pickers are PRE-FILLED with
    the SAME canonical [2020-01-01, previous day] default as the spot/options pickers."""
    from brutex.dashboard.app import _render_reports_html
    from brutex.dashboard.pull_controller import default_date_range

    html = _render_reports_html()
    from_iso, to_iso = default_date_range()
    assert f'id="from" value="{from_iso}"' in html
    assert f'id="to" value="{to_iso}"' in html
    assert "__BRUTEX_DATE_" not in html


def test_reports_page_carries_no_sweep_start_picker() -> None:
    """R1/R25: the ``swStart`` sweep-launch picker was RETIRED from /reports.

    Superseded by ``test_operator_page_sweep_start_picker_prefilled_with_backend_default``
    below — the picker (and the whole Run Sweep control) moved to the
    Operator page (DEC-INSTITUTIONAL-ONECLICK-001, R1/R25); /reports is
    pre-open-report-only now.
    """
    from brutex.dashboard.app import _render_reports_html

    html = _render_reports_html()
    assert 'id="swStart"' not in html


def test_operator_page_sweep_start_picker_prefilled_with_backend_default() -> None:
    """DEC-SWEEP-DEFAULT-2020-001 render-side drift guard (hostile-review fold).

    The ``swStart`` date-picker POSTs its value, so the browser-submitted date
    is the EFFECTIVE default for a UI-driven sweep — it silently OVERRIDES any
    backend-only default. Pinning the rendered template value to
    ``sweep_runner.DEFAULT_START`` (itself pinned to ``GROWW_HISTORY_FLOOR``
    by ``test_default_start_matches_pull_floor``) means a future floor move
    cannot leave the template stale. Relocated from /reports to the Operator
    page's new Run Control picker (R1/R25, DEC-INSTITUTIONAL-ONECLICK-001).
    """
    from brutex.dashboard.app import _render_index_html
    from brutex.dashboard.sweep_runner import DEFAULT_START

    html = _render_index_html()
    assert f'<input id="swStart" type="date" value="{DEFAULT_START}">' in html


def test_resolve_report_dates_defaults_blanks_to_canonical_range() -> None:
    """A blank from/to falls back to the canonical [2020-01-01, previous day]."""
    from datetime import date

    from brutex.dashboard.app import _resolve_report_dates
    from brutex.dashboard.pull_controller import default_date_range

    d_from, d_to = default_date_range()
    assert _resolve_report_dates(None, None) == (
        date.fromisoformat(d_from),
        date.fromisoformat(d_to),
    )
    # An explicit range overrides the default; a partial blank fills only the gap.
    assert _resolve_report_dates("2024-01-01", "2024-03-31") == (
        date(2024, 1, 1),
        date(2024, 3, 31),
    )
    assert _resolve_report_dates("2024-01-01", None) == (date(2024, 1, 1), date.fromisoformat(d_to))


def test_resolve_report_dates_rejects_bad_and_inverted() -> None:
    """A malformed ISO date or an inverted range raises ValueError (route → 400)."""
    import pytest
    from brutex.dashboard.app import _resolve_report_dates

    with pytest.raises(ValueError, match="bad date"):
        _resolve_report_dates("not-a-date", "2024-01-01")
    with pytest.raises(ValueError, match="on or before"):
        _resolve_report_dates("2024-12-31", "2024-01-01")


def test_reports_opt_spot_badge_present() -> None:
    """The OPT vs SPOT symbol badge (DEC-OPTIONS-ELIGIBLE-GUARD-001): each report
    chip is tagged green OPT (has an option chain) or amber SPOT (spot-only),
    mirroring the /universe explorer's OPT/REF chips."""
    html = _render_reports_html()
    assert ".chip .b.opt" in html, "the OPT badge style is missing"
    assert ".chip .b.spot" in html, "the SPOT badge style is missing"
    # The render branches on the API's option_pullable flag.
    assert "s.option_pullable" in html, "the chip does not branch on option_pullable"
    # The badge label is assigned via textContent (never innerHTML string-concat —
    # DEC-RESULTS-SURFACE-HONESTY-001 closed that sink; the old '>OPT<' markup form
    # is gone by design).
    assert 'badgeEl.textContent = s.option_pullable ? "OPT" : "SPOT"' in html, (
        "the OPT/SPOT badge label is missing"
    )


def test_reports_fno_only_selector_present() -> None:
    """The "F&O only" one-click selector (DEC-FNO-ONLY-FILTER-001): selects
    exactly the option-pullable symbols so a report (and the option-chain
    pulls it mirrors) can target the F&O universe alone."""
    html = _render_reports_html()
    assert '<button id="fno"' in html, "the F&O-only toolbar button is missing"
    assert "F&amp;O only</button>" in html, "the button label is missing"
    assert "allSymbols.filter(s => s.option_pullable)" in html, (
        "the selector does not filter on option_pullable"
    )
