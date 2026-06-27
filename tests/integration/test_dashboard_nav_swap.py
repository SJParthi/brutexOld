"""In-place section-navigation enhancement (DEC-DASHBOARD-NAV-SWAP-001).

The dashboard top-nav was a set of full-page reloads — the dominant remaining
nav latency that DEC-DASHBOARD-NAV-LATENCY-001 (MIGRATION.md #933) explicitly
deferred as "a SEPARATE larger PR". This is that follow-up: a pure-frontend,
progressive-enhancement in-place DOM swap.

There is NO new Python route (the swap fetches the SAME byte-identical page
HTML the server already serves), so the integration surface to verify is:

* ``/static/bx-nav.js`` is served (it is the whole feature) and its content
  carries the progressive-enhancement + graceful-fallback contract.
* every canonical top-nav page now ships the ``bx-nav.js`` ``<script>`` include
  AND is still a complete, parseable HTML document with a ``<body>`` (so the
  client-side fetch+swap has a body to lift) AND still carries real ``<a href>``
  anchors (so a no-JS / failed-fetch user gets the normal full navigation).
* the page payloads are byte-identical whether requested plainly or with the
  ``X-Requested-With: bx-nav`` header the swap sends (the swap reuses the SAME
  route — no fragment branch, no payload drift; CLAUDE.md §3 scope lock).

The pure-JS DOM behaviour (DOMParser swap, pushState/popstate, script
re-execution) is browser-only and is MANUAL-verified in a browser per the PR's
honest envelope (Golden Rule #10); these tests cover the server contract the JS
depends on.
"""

from __future__ import annotations

from collections.abc import Generator

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx2")

from brutex.dashboard.app import DashboardState, build_app
from brutex.dashboard.nav import NAV_LINKS
from fastapi.testclient import TestClient

#: Every canonical top-nav destination that should carry the swap enhancement.
_TOPNAV_HREFS = tuple(link.href for link in NAV_LINKS)


@pytest.fixture
def client() -> Generator[TestClient]:
    app = build_app(DashboardState())
    with TestClient(app) as c:
        yield c


class TestNavScriptAsset:
    def test_bx_nav_js_is_served(self, client: TestClient) -> None:
        r = client.get("/static/bx-nav.js")
        assert r.status_code == 200
        assert "javascript" in r.headers["content-type"]

    def test_bx_nav_js_has_progressive_enhancement_contract(self, client: TestClient) -> None:
        body = client.get("/static/bx-nav.js").text
        # Cites the decision ID (Golden Rule #1 traceability).
        assert "DEC-DASHBOARD-NAV-SWAP-001" in body
        # The in-place swap primitives.
        assert "pushState" in body
        assert "popstate" in body
        assert "DOMParser" in body
        # The graceful full-reload fallback (never break nav).
        assert "window.location.href = url" in body
        # Only intercepts inside the canonical top-nav.
        assert "nav.topnav" in body


class TestPagesShipTheEnhancement:
    @pytest.mark.parametrize("href", _TOPNAV_HREFS)
    def test_page_includes_nav_script(self, client: TestClient, href: str) -> None:
        body = client.get(href).text
        assert '<script defer src="/static/bx-nav.js"></script>' in body

    @pytest.mark.parametrize("href", _TOPNAV_HREFS)
    def test_page_is_a_complete_swappable_document(self, client: TestClient, href: str) -> None:
        # The swap lifts the incoming <body> — the document must have one, plus a
        # <title> the swap copies into document.title.
        body = client.get(href).text
        assert "<body" in body and "</body>" in body
        assert "<title>" in body

    @pytest.mark.parametrize("href", _TOPNAV_HREFS)
    def test_page_keeps_real_anchor_for_fallback(self, client: TestClient, href: str) -> None:
        # Progressive enhancement: the nav stays a real <a href> so a no-JS /
        # failed-fetch user gets the normal full navigation.
        body = client.get(href).text
        assert '<nav class="topnav"' in body
        assert 'href="/movers"' in body


class TestPayloadIsByteIdenticalForSwapFetch:
    @pytest.mark.parametrize("href", _TOPNAV_HREFS)
    def test_swap_header_does_not_change_payload(self, client: TestClient, href: str) -> None:
        # The swap fetch sends X-Requested-With: bx-nav. There is NO fragment
        # branch — the SAME route returns the SAME bytes (scope lock).
        plain = client.get(href)
        swap = client.get(href, headers={"X-Requested-With": "bx-nav"})
        assert plain.status_code == swap.status_code == 200
        assert plain.text == swap.text
