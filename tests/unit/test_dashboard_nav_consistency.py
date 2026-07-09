"""Cross-page top-nav consistency guard (DEC-DASHBOARD-NAV-UNIFY-001).

Renders EVERY served dashboard page through its own ``_render_*_html`` helper
and asserts the top-nav matches the single canonical :data:`NAV_LINKS` spec —
same links, same order, no dead ``/credentials`` 404 link, at most one
``aria-current``. Any future page whose nav drifts (a missing / extra /
reordered / renamed link, or a re-introduced dead link) fails CI, so the
``DEC-DASHBOARD-NAV-CREDS-RECONCILE-001`` class of bug cannot recur.

A meta-guard (:func:`test_every_render_helper_is_classified`) asserts every
``_render_*_html`` helper is either checked here or explicitly exempted with a
rationale — so a NEWLY added page cannot be silently omitted from the guard.
"""
# pyright: reportPrivateUsage=none

from __future__ import annotations

import re

import pytest

pytest.importorskip("fastapi", reason="dashboard render helpers need the [ui] extra")

from brutex.dashboard import app as dashboard_app
from brutex.dashboard.nav import NAV_LINKS, render_topnav

#: Every page that carries the canonical top-nav → its pure render helper.
#: The ``/groww-credentials`` page was REMOVED with the whole credentials webpage
#: (DEC-AWS-ONLY-CREDS-001 — Groww creds now live ONLY in AWS Parameter Store), so
#: it is neither guarded here nor exempt: its render helper no longer exists.
_TOPNAV_PAGES = {
    "/universe": dashboard_app._render_universe_html,
    "/db": dashboard_app._render_db_html,
    "/coverage": dashboard_app._render_coverage_html,
    "/movers": dashboard_app._render_movers_html,
    "/results": dashboard_app._render_results_html,
    "/reports": dashboard_app._render_reports_html,
    "/coverage/spot": dashboard_app._render_spot_coverage_html,
    "/coverage/windows": dashboard_app._render_fetch_windows_html,
    "/coverage/options": dashboard_app._render_expired_options_html,
    "/completeness": dashboard_app._render_completeness_html,
    # DEC-OPERATOR-PAGE-REAL-001: the screener + strategy pages carry the
    # canonical topnav (sub-pages — their own hrefs are NOT nav entries, so
    # no link is aria-current; the ≤1-current guard covers that contract).
    "/screener": dashboard_app._render_screener_html,
    "/strategy": dashboard_app._render_strategy_html,
}

#: Render helpers that intentionally do NOT carry the canonical top-nav — listed
#: explicitly (not silently skipped) so the meta-guard stays exhaustive:
#:  - ``_render_index_html`` — the operator dashboard uses in-page tabs, not topnav.
#:  - ``_render_welcome_html`` — the marketing landing page has its own nav + footer.
#: (The in-app Groww credentials page + its render helper were deleted in
#: DEC-AWS-ONLY-CREDS-001 — credentials now live ONLY in AWS Parameter Store.)
_EXEMPT_HELPERS = {
    "_render_index_html",
    "_render_welcome_html",
}

_NAV_RE = re.compile(r'<nav class="topnav".*?</nav>', re.S)
_LINK_RE = re.compile(r'<a\s+href="([^"]+)"([^>]*)>([^<]+)</a>')

_CANONICAL = [(link.href, link.text) for link in NAV_LINKS]


def _nav_block(html: str) -> str:
    match = _NAV_RE.search(html)
    assert match is not None, "page is missing its <nav class='topnav'> block"
    return match.group(0)


@pytest.mark.parametrize("page", sorted(_TOPNAV_PAGES))
def test_page_nav_matches_canonical(page: str) -> None:
    """Each page's nav is exactly the canonical link set, in order."""
    nav = _nav_block(_TOPNAV_PAGES[page]())
    links = [(href, text.strip()) for href, _attrs, text in _LINK_RE.findall(nav)]
    assert links == _CANONICAL, f"{page} nav drifted from the canonical NAV_LINKS"


@pytest.mark.parametrize("page", sorted(_TOPNAV_PAGES))
def test_page_nav_has_no_dead_link_and_one_current(page: str) -> None:
    """No dead ``/credentials`` 404 link survives; at most one link is current."""
    nav = _nav_block(_TOPNAV_PAGES[page]())
    assert 'href="/credentials"' not in nav, f"{page} re-introduced the dead /credentials link"
    assert "Groww (legacy)" not in nav, f"{page} re-introduced the 'Groww (legacy)' label"
    currents = [href for href, attrs, _text in _LINK_RE.findall(nav) if "aria-current" in attrs]
    assert len(currents) <= 1, f"{page} marks more than one nav link current: {currents}"


def test_render_topnav_marks_active_current() -> None:
    """``render_topnav`` emits the canonical links with exactly one current marker."""
    nav = render_topnav("/db")
    links = [(href, text.strip()) for href, _attrs, text in _LINK_RE.findall(nav)]
    assert links == _CANONICAL
    assert nav.count('aria-current="page"') == 1
    assert 'href="/db" aria-current="page"' in nav


def test_render_topnav_with_unknown_active_marks_none() -> None:
    """An ``active`` href that is not a nav entry (a sub-page) marks no link current."""
    nav = render_topnav("/coverage/spot")
    assert "aria-current" not in nav
    links = [(href, text.strip()) for href, _attrs, text in _LINK_RE.findall(nav)]
    assert links == _CANONICAL


@pytest.mark.parametrize("page", ["/movers", "/reports"])
def test_operator_link_present_in_nav(page: str) -> None:
    """The leading ``Operator`` link (href ``/``) is in each page's top-nav.

    The Operator link points at the live ``/`` route (the rich operator
    dashboard rendering ``v29.html``) — distinct from ``/results`` — and was
    added to every top-nav block (DEC-MOVERS-FULL-CONTRACT-NAME-001 bundle).
    """
    nav = _nav_block(_TOPNAV_PAGES[page]())
    links = [(href, text.strip()) for href, _attrs, text in _LINK_RE.findall(nav)]
    assert ("/", "Operator") in links, f"{page} nav is missing the Operator (/) link"
    # It is the FIRST nav entry.
    assert links[0] == ("/", "Operator"), f"{page} Operator link is not first in nav"


def test_index_page_still_renders() -> None:
    """The ``/`` operator dashboard (``v29.html``) still renders non-empty.

    The Operator nav link targets this page; it intentionally carries NO
    ``topnav`` (it uses in-page tabs), so it stays exempt from the canonical
    nav guard while remaining a valid, reachable destination.
    """
    html = dashboard_app._render_index_html()
    assert html.strip(), "the operator dashboard index page rendered empty"
    assert '<nav class="topnav"' not in html, "the / page should not carry the topnav"


def test_every_render_helper_is_classified() -> None:
    """Meta-guard: every dashboard page render helper is either guarded or exempt.

    A newly added ``_render_*_html`` page that is neither checked nor explicitly
    exempted fails here — so a new page cannot silently skip the nav guard.
    """
    all_helpers = {
        name
        for name in dir(dashboard_app)
        if name.startswith("_render_") and name.endswith("_html")
    }
    guarded = {fn.__name__ for fn in _TOPNAV_PAGES.values()}
    unclassified = all_helpers - guarded - _EXEMPT_HELPERS
    assert not unclassified, f"unclassified dashboard page render helper(s): {sorted(unclassified)}"
