"""Guards the accessibility + motion polish across the remaining dashboard pages
(DEC-DASHBOARD-A11Y-POLISH-002, extends DEC-DASHBOARD-A11Y-POLISH-001 / #497).

The keyboard focus ring (``:focus-visible`` — keyboard-only, never on a mouse
click) + the ``prefers-reduced-motion`` honouring are additive, opt-in polish
that cannot change the static mouse layout. #497 shipped them to ``/universe`` +
``/coverage``; this pins them on the remaining interactive pages (``/db``,
``/reports``, ``/credentials``) so the accessibility cannot silently
regress. (The ``/logs`` webpage was removed — DEC-DASHBOARD-LOGS-REMOVE-001.)
Rendered against the pages' own ``_render_*_html`` helpers, mirroring
``test_dashboard_universe_ux.py``.
"""
# pyright: reportPrivateUsage=none

from __future__ import annotations

import pytest

pytest.importorskip("fastapi", reason="dashboard render helpers need the [ui] extra")

from brutex.dashboard.app import (
    _render_credentials_html,
    _render_db_html,
    _render_reports_html,
)

#: page path → its pure render helper (all return the full HTML string).
_RENDERERS = {
    "/db": _render_db_html,
    "/reports": _render_reports_html,
    "/credentials": _render_credentials_html,
}


def test_remaining_pages_have_focus_ring_and_reduced_motion() -> None:
    """Every remaining interactive page carries the keyboard focus ring + the
    reduce-motion query (DEC-DASHBOARD-A11Y-POLISH-002)."""
    for page, render in _RENDERERS.items():
        html = render()
        assert ":focus-visible" in html, f"{page} is missing keyboard focus rings"
        assert "prefers-reduced-motion" in html, f"{page} is missing the reduce-motion query"
        assert "outline: 2px solid var(--blue)" in html, (
            f"{page} is missing the focus-ring outline rule"
        )
