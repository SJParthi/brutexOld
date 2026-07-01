"""Dashboard UX consistency + polish pass (DEC-DASHBOARD-UX-POLISH-001).

Pins the operator-requested visual layer on top of the nav-link unification
(DEC-DASHBOARD-NAV-UNIFY-001):

1. **Consistent header alignment** — every served top-nav page links the ONE
   shared theme stylesheet (``/static/bx-theme.css``), so the brand + subtitle +
   top-nav header block renders against a single grid/spacing source of truth
   (CLAUDE.md §11 COMMON). The shared file itself carries the canonical ``header``
   rules, the keyboard focus ring, and the ``prefers-reduced-motion`` opt-out.
2. **/db hides the expired-options lake from the browsable list** — the
   ``data-candles="option"`` list row is dropped (SCOPE-SAFE: the lake, its data,
   and the browse route are untouched); the spot ``data-candles="spot"`` row stays.

(The former "Credentials page de-clutter" checks were removed with the whole in-app
credentials webpage — DEC-AWS-ONLY-CREDS-001 — credentials now live ONLY in AWS
Parameter Store.)

Rendered against the pages' own ``_render_*_html`` helpers + a ``TestClient`` for
the served static asset, mirroring ``test_dashboard_a11y_polish.py``. Skipped cleanly
without ``[ui]``.
"""
# pyright: reportPrivateUsage=none

from __future__ import annotations

import pytest

pytest.importorskip("fastapi", reason="dashboard render helpers need the [ui] extra")

from brutex.dashboard.app import (
    DashboardState,
    _render_coverage_html,
    _render_db_html,
    _render_expired_options_html,
    _render_fetch_windows_html,
    _render_movers_html,
    _render_reports_html,
    _render_spot_coverage_html,
    _render_universe_html,
    build_app,
)
from fastapi.testclient import TestClient

#: Every served top-nav page → its pure render helper. Each must link the shared
#: theme so the header aligns to ONE grid (the consistency the operator asked for).
#: (The in-app Groww credentials page was removed — DEC-AWS-ONLY-CREDS-001 — credentials
#: now live ONLY in AWS Parameter Store, so there is no page to render/de-clutter.)
_TOPNAV_PAGES = {
    "/universe": _render_universe_html,
    "/db": _render_db_html,
    "/coverage": _render_coverage_html,
    "/movers": _render_movers_html,
    "/reports": _render_reports_html,
    "/coverage/spot": _render_spot_coverage_html,
    "/coverage/windows": _render_fetch_windows_html,
    "/coverage/options": _render_expired_options_html,
}

#: The shared theme stylesheet link every page must carry — the single header /
#: motion source of truth (the "shared header wrapper marker").
_THEME_LINK = '<link rel="stylesheet" href="/static/bx-theme.css">'


@pytest.mark.parametrize("page", sorted(_TOPNAV_PAGES))
def test_every_topnav_page_links_the_shared_theme(page: str) -> None:
    """Header alignment: every page links the ONE shared theme stylesheet."""
    html = _TOPNAV_PAGES[page]()
    assert _THEME_LINK in html, f"{page} is missing the shared bx-theme.css link"


@pytest.mark.parametrize("page", sorted(_TOPNAV_PAGES))
def test_every_topnav_header_block_is_structurally_identical(page: str) -> None:
    """Every page's brand + subtitle + nav header uses the same markup grid.

    The brand mark + ``.logo`` + ``.sub`` + a ``<nav class="topnav">`` carrying NO
    page-local inline ``style=`` override (so the shared CSS governs spacing) —
    that is what makes the header look identical everywhere.
    """
    html = _TOPNAV_PAGES[page]()
    assert '<div class="brandmark"' in html, f"{page} lost the brand mark"
    assert '<div class="logo">BruteX</div>' in html, f"{page} brand/logo markup drifted"
    assert '<div class="sub">' in html, f"{page} subtitle markup drifted"
    # The nav must NOT re-introduce a page-local inline style that would re-skew
    # the alignment the shared stylesheet now owns.
    assert '<nav class="topnav" aria-label="Primary" style=' not in html, (
        f"{page} re-introduced an inline-styled nav that drifts the header grid"
    )


def test_shared_theme_is_served_and_canonical() -> None:
    """The shared theme serves 200 as CSS and carries the canonical header + a11y."""
    client = TestClient(build_app(DashboardState(), instrument_store=None))
    resp = client.get("/static/bx-theme.css")
    assert resp.status_code == 200, "bx-theme.css did not serve"
    assert "css" in resp.headers["content-type"], "bx-theme.css wrong content-type"
    body = resp.text
    # Canonical header rules + the restrained-motion opt-out live in ONE place.
    assert "header .topnav" in body
    assert "header .logo" in body
    assert "prefers-reduced-motion" in body
    assert ":focus-visible" in body


# ── Complaint 3: /db hides the expired-options lake from the browsable list ───


def test_db_page_hides_expired_options_from_the_table_list() -> None:
    """The expired-options lake is dropped from the /db browsable list."""
    html = _render_db_html()
    assert 'data-candles="option"' not in html, "the expired-options list row survived"
    assert "🅾 expired options" not in html, "the expired-options list label survived"


def test_db_page_keeps_the_spot_candle_lake_row() -> None:
    """SCOPE-SAFE: only the options LIST row is hidden — the spot row stays."""
    html = _render_db_html()
    assert 'data-candles="spot"' in html, "the spot candle-lake row was wrongly removed"
    assert "📈 candles" in html
