"""Guards the global mobile-responsiveness pass (DASH-UX D5, DEC-DASHBOARD-MOBILE-UX-001).

DASH-UX page 5 (final) — a consistent ``@media (max-width: 680px)`` block added to
the four primary nav pages (``/db``, ``/coverage``, ``/reports``, ``/credentials``)
that previously had NO mobile breakpoint, so on a phone the fixed-width ``/db``
sidebar grid + dense headers/bands overflowed. The dashboard JS/CSS has no unit
harness, so these substring guards lock the breakpoint + its key rules in CI (the
same established pattern as the D1–D4 UX tests).

All additive: the rules live inside a ``max-width: 680px`` query, so the desktop
layout (≥681px) is byte-identical — no route, data, or desktop-layout change.
"""
# pyright: reportPrivateUsage=none
# (whitebox: these guards render the dashboard's own page-render helpers.)

from __future__ import annotations

import pytest
from brutex.dashboard.app import (
    _render_coverage_html,
    _render_db_html,
    _render_reports_html,
)

#: The three pages that gained a mobile breakpoint in D5, keyed by their render
#: helper, each with a page-specific rule the breakpoint must carry.
_PAGES = {
    "db": (_render_db_html, "grid-template-columns: 1fr"),
    "coverage": (_render_coverage_html, "grid-template-columns: 1fr"),
    "reports": (_render_reports_html, "width: 100%"),
}


@pytest.mark.parametrize("page", sorted(_PAGES))
def test_mobile_breakpoint_present(page: str) -> None:
    """Each page carries the shared ``max-width: 680px`` mobile breakpoint + its DEC ID."""
    render, _ = _PAGES[page]
    html = render()
    assert "@media (max-width: 680px)" in html, f"/{page} is missing the mobile breakpoint"
    assert "DEC-DASHBOARD-MOBILE-UX-001" in html, f"/{page} mobile block must cite its decision ID"


@pytest.mark.parametrize("page", sorted(_PAGES))
def test_mobile_breakpoint_carries_page_rule(page: str) -> None:
    """The breakpoint carries the page-specific stacking/full-width rule."""
    render, needle = _PAGES[page]
    html = render()
    # The page-specific rule must appear AFTER the breakpoint opens (i.e. inside it).
    idx = html.find("@media (max-width: 680px)")
    assert idx != -1, f"/{page} is missing the mobile breakpoint"
    assert needle in html[idx:], f"/{page} mobile block must carry the rule {needle!r}"


@pytest.mark.parametrize("page", sorted(_PAGES))
def test_header_wraps_on_mobile(page: str) -> None:
    """Every page's mobile block lets the header nav wrap instead of overflowing."""
    render, _ = _PAGES[page]
    html = render()
    idx = html.find("@media (max-width: 680px)")
    assert "header { flex-wrap: wrap;" in html[idx:], (
        f"/{page} mobile block must let the header wrap"
    )
