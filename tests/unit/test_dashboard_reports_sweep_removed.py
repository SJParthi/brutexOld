"""Regression: /reports carries NO sweep controls of any kind (R1/R25).

DEC-INSTITUTIONAL-ONECLICK-001 — the old light "Run Sweep" button + its
progress bar + its JS, AND the Stage-4 "Run cascade" button + its JS, were
RETIRED from ``brutex/dashboard/templates/reports.html`` (replace-not-add,
per the operator's earlier approval) and rebuilt on the Operator page
(``v29.html`` — see ``test_dashboard_sweep_run_route.py`` /
``test_dashboard_v29_oneclick_xss.py``). This file pins the REMOVAL half of
that migration so a future edit cannot silently reintroduce a second sweep
control on /reports. Static-template assertions (no JS engine), matching the
``test_dashboard_reports_template_xss.py`` convention.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

from brutex.dashboard.app import _render_reports_html


def test_run_sweep_button_and_ids_are_gone() -> None:
    html = _render_reports_html()
    assert 'id="run-sweep"' not in html
    assert 'id="swSymbols"' not in html
    assert 'id="swIntervals"' not in html
    assert 'id="swStart"' not in html
    assert 'id="swEnd"' not in html
    assert 'id="sweep-msg"' not in html
    assert 'id="sweep-bar"' not in html
    assert 'id="sweep-bar-fill"' not in html


def test_run_cascade_button_and_ids_are_gone() -> None:
    html = _render_reports_html()
    assert 'id="run-cascade"' not in html
    assert 'id="cascade-msg"' not in html
    assert "Stage-4 cascade" not in html


def test_sweep_launch_js_is_gone() -> None:
    """The ``pollSweep`` / ``POST /api/v1/sweep/run`` / ``POST /api/v1/cascade/run``
    wiring is gone — the sweep engine's public routes are untouched (they now
    serve the Operator page instead), but /reports no longer calls them."""
    html = _render_reports_html()
    assert "function pollSweep" not in html
    assert "function renderSweep" not in html
    assert "/api/v1/sweep/run?" not in html
    assert "/api/v1/sweep-progress" not in html
    assert "/api/v1/cascade/run" not in html


def test_stale_52_condition_copy_is_gone() -> None:
    """The stale "52-condition vocabulary" copy (the vocabulary is 60-condition,
    per the sweep-oneclick-requirements-living R2) never shipped again."""
    html = _render_reports_html()
    assert "52-condition" not in html


def test_reports_points_operators_at_the_new_home() -> None:
    """The page still tells an operator where the control moved to."""
    html = _render_reports_html()
    assert 'href="/"' in html


def test_download_form_untouched_by_the_removal() -> None:
    """The removal is additive-subtractive only — the pre-open export form survives."""
    html = _render_reports_html()
    for needle in (
        "/api/v1/reports/preopen/symbols",
        "/api/v1/reports/preopen.zip?",
        'id="download"',
        'id="syms"',
    ):
        assert needle in html, f"a pre-existing /reports control regressed: {needle}"
