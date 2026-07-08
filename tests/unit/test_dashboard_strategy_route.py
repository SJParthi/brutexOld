"""The ``GET /strategy`` route (DEC-INSTITUTIONAL-ONECLICK-001, P4 stage 4 of 6).

Mirrors the ``test_dashboard_oneclick_routes.py`` harness style but is much
thinner: ``/strategy`` is a plain cached static-file page with ZERO
server-side token substitution (the client parses its own query string —
same discipline as ``_load_reports_template``), so the route glue itself has
almost no surface — the only things worth pinning are (1) it serves 200 HTML,
(2) the SAME byte-identical page regardless of query string (never templates
a caller-supplied value into the response — the reflected-XSS class
``movers.html``'s GAP B had to fix cannot recur here), and (3) it links the
two data endpoints it needs (``GET /api/v1/sweep/trades``, the EXISTING
``GET /api/v1/db/candles``) — both already covered end-to-end by their own
route test files, so this file does not re-test them.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

pytest.importorskip("fastapi", reason="dashboard routes need the [ui] extra")
pytest.importorskip("httpx", reason="TestClient needs httpx")

from brutex.dashboard.app import DashboardState, build_app
from fastapi.testclient import TestClient


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(build_app(DashboardState(), instrument_store=None)) as c:
        yield c


def test_strategy_page_serves_200_html(client: TestClient) -> None:
    resp = client.get("/strategy")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "<title>BruteX — Strategy chart</title>" in resp.text


def test_strategy_page_is_byte_identical_with_and_without_query_string(client: TestClient) -> None:
    """No query param is ever templated server-side — same page either way."""
    plain = client.get("/strategy").text
    with_qs = client.get(
        "/strategy",
        params={
            "run_id": "run_2026-07-08T090000Z_nifty_full",
            "rank": "3",
            "combo_id": "combo-complete-0003",
            "mask_hex": "10a081400000000",
            "direction": "short",
            "tf": "3m",
            "instrument": "NIFTY",
        },
    ).text
    assert plain == with_qs


@pytest.mark.parametrize(
    "run_id",
    [
        "<script>alert(1)</script>",
        "\"><img src=x onerror=alert(1)>",
        "../../etc/passwd",
        "run_id_with_'quotes'_and_\"doublequotes\"",
    ],
)
def test_strategy_page_never_reflects_hostile_query_values(client: TestClient, run_id: str) -> None:
    """A hostile run_id/combo_id in the URL must never appear in the response body.

    ``/strategy`` parses its query string entirely client-side (``new
    URLSearchParams(window.location.search)``) — the server never sees, let
    alone echoes, these values. This pins that structurally: the raw hostile
    string must not appear anywhere in the served HTML.
    """
    resp = client.get(
        "/strategy",
        params={"run_id": run_id, "combo_id": run_id, "mask_hex": run_id, "tf": run_id},
    )
    assert resp.status_code == 200
    assert run_id not in resp.text
    assert "<script>alert(1)</script>" not in resp.text
    assert "onerror=alert(1)" not in resp.text


def test_strategy_page_parses_its_own_query_string_client_side(client: TestClient) -> None:
    html = client.get("/strategy").text
    assert "new URLSearchParams(window.location.search)" in html
    assert 'qs("run_id")' in html
    assert 'qs("rank")' in html
    assert 'qs("mask_hex")' in html
    assert 'qs("direction")' in html
    assert 'qs("tf")' in html
    assert 'qs("instrument")' in html
    assert 'qs("combo_id")' in html


def test_strategy_page_reads_the_two_documented_data_endpoints(client: TestClient) -> None:
    html = client.get("/strategy").text
    assert "/api/v1/sweep/trades?" in html
    assert "/api/v1/db/candles?" in html


def test_strategy_page_loads_shared_helper_scripts(client: TestClient) -> None:
    html = client.get("/strategy").text
    assert '<script defer src="/static/bx-oneclick.js"></script>' in html
    assert '<script defer src="/static/bx-oneclick-chart.js"></script>' in html


def test_strategy_page_carries_no_topnav(client: TestClient) -> None:
    """Deliberately no canonical <nav class="topnav"> block (grounding-fact
    preference for a contextual link over a new nav.py NAV_LINKS row) — just
    plain back-links to / and /results."""
    html = client.get("/strategy").text
    assert '<nav class="topnav"' not in html
    assert 'href="/"' in html
    assert 'href="/results"' in html


def test_strategy_route_not_registered_in_nav_links() -> None:
    """/strategy is intentionally absent from the canonical nav.py NAV_LINKS."""
    from brutex.dashboard.nav import NAV_LINKS

    assert all(link.href != "/strategy" for link in NAV_LINKS)


def test_bx_oneclick_chart_js_is_served(client: TestClient) -> None:
    resp = client.get("/static/bx-oneclick-chart.js")
    assert resp.status_code == 200
    assert "javascript" in resp.headers["content-type"]
    assert "window.BXChart" in resp.text
