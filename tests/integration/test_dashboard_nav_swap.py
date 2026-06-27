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
re-execution, the page-poller TEARDOWN + head-script RECONCILE added in the
hostile-review fix pass) is browser-only. Where a JS runtime (``node``) is
available it is EXERCISED for real by ``test_bx_nav_dom_behaviours_node`` via a
headless DOM harness (no jsdom dependency); without ``node`` that one test
skips and the behaviour is MANUAL-verified in a browser per the PR's honest
envelope (Golden Rule #10). The remaining tests cover the server contract the
JS depends on plus the JS-source contract.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Generator
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="dashboard tests need fastapi")
pytest.importorskip("httpx", reason="fastapi TestClient needs httpx")

from brutex.dashboard.app import DashboardState, build_app
from brutex.dashboard.nav import NAV_LINKS
from fastapi.testclient import TestClient

#: The 6 canonical top-nav destinations.
_TOPNAV_HREFS = tuple(link.href for link in NAV_LINKS)

#: EVERY route that serves a template carrying the ``bx-nav.js`` include — the 6
#: top-nav pages PLUS the 4 sub-pages (spot/options/windows coverage splits +
#: the completeness scoreboard) that also got the enhancement. The include test
#: parametrizes over ALL of them so a future sub-page drift is caught (MINOR fix:
#: the original test only covered the 6 NAV_LINKS).
_ENHANCED_PAGE_ROUTES = (
    *_TOPNAV_HREFS,
    "/coverage/spot",
    "/coverage/options",
    "/coverage/windows",
    "/completeness",
)

#: The pages that load ``bx-datepicker.js`` in ``<head>`` — the reason the
#: head-script reconcile fix exists.
_DATEPICKER_HEAD_ROUTES = ("/db", "/movers", "/reports", "/universe")

_BX_NAV_JS = Path(__file__).resolve().parents[2] / "brutex" / "dashboard" / "static" / "bx-nav.js"
_DOM_HARNESS = Path(__file__).resolve().parent / "bx_nav_dom_harness.mjs"


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

    def test_bx_nav_js_has_teardown_and_head_reconcile(self, client: TestClient) -> None:
        # The hostile-review fixes are present in the served asset (the behaviour
        # itself is EXERCISED by the node DOM harness test below).
        body = client.get("/static/bx-nav.js").text
        # BLOCKER fix: page pollers torn down on swap.
        assert "teardownPage" in body
        assert "clearInterval" in body
        assert "clearTimeout" in body
        assert "bx:teardown" in body
        # MAJOR fix: head <script src> reconcile (so bx-datepicker.js loads on a
        # swapped-in page).
        assert "syncHeadScripts" in body
        assert "script[src]" in body


class TestPagesShipTheEnhancement:
    @pytest.mark.parametrize("href", _ENHANCED_PAGE_ROUTES)
    def test_page_includes_nav_script(self, client: TestClient, href: str) -> None:
        # Covers ALL 10 enhanced templates — the 6 top-nav pages AND the 4
        # sub-pages — so a sub-page that drops the include can never slip by.
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


class TestHeadScriptReconcileSurface:
    """The pages that load ``bx-datepicker.js`` in ``<head>`` are the reason the
    head-script reconcile (MAJOR fix) exists — verify the surface stays real so
    the JS fix has something to reconcile against."""

    @pytest.mark.parametrize("href", _DATEPICKER_HEAD_ROUTES)
    def test_datepicker_page_loads_script_in_head(self, client: TestClient, href: str) -> None:
        body = client.get(href).text
        head = body.split("</head>", 1)[0]
        assert '<script defer src="/static/bx-datepicker.js"></script>' in head


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available")
def test_bx_nav_dom_behaviours_node() -> None:
    """Exercise the real browser-only DOM behaviours of bx-nav.js in a headless
    node DOM harness: page pollers are torn down on a swap (no leaked
    setInterval), the incoming page's head ``<script src>`` is loaded as an
    executable script (and deduped on a second swap), the ``bx:teardown`` event
    fires before the swap, and bx-nav's own click/popstate listeners register
    exactly once. This is the no-illusion proof the fixes work — not a string
    assertion."""
    node = shutil.which("node")
    assert node is not None  # narrowed by skipif
    result = subprocess.run(
        [node, str(_DOM_HARNESS), str(_BX_NAV_JS)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, f"bx-nav DOM harness failed:\n{result.stdout}\n{result.stderr}"
    assert "ALL bx-nav DOM behaviours verified" in result.stdout
