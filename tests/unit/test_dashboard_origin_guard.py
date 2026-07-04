"""Host/Origin guard middleware tests (R2-DS-F1).

The dashboard carries no auth/CSRF tokens, so ``build_app`` registers two
thin guards: Starlette's ``TrustedHostMiddleware`` (refuses unknown ``Host``
headers — the DNS-rebinding vector) and a POST-only same-origin check
(refuses browser cross-site state changes — the CSRF fire-and-forget
vector). These tests drive both through a real ``TestClient`` and cover the
pure decision helpers (`_trusted_host_patterns`, `_origin_header_host`,
`_cross_origin_reason`) at 100% line+branch.
"""

from __future__ import annotations

import pytest
from brutex.dashboard.app import (
    _CROSS_ORIGIN_DETAIL,
    DEFAULT_ALLOWED_HOSTS,
    DashboardState,
    _cross_origin_reason,
    _origin_header_host,
    _trusted_host_patterns,
    build_app,
)
from fastapi.testclient import TestClient


@pytest.fixture
def client() -> TestClient:
    """A TestClient over a bare app (no store) with the default host allowlist.

    Rides the root-conftest ``_testclient_loopback_base_url`` fixture, so the
    default request Host is the allowlisted ``localhost`` (the production
    default is loopback-ONLY — ``testserver`` was removed, R2-DS-F1).
    """
    return TestClient(build_app(DashboardState(), instrument_store=None))


# ── Origin/Referer guard — integration through the real middleware ──────────


class TestOriginGuardIntegration:
    """POST-only same-origin enforcement via the live middleware stack."""

    def test_cross_origin_post_blocked_403(self, client: TestClient) -> None:
        """A POST carrying a foreign Origin is refused with the honest 403 detail."""
        resp = client.post(
            "/api/v1/universe/load-instruments",
            headers={"Origin": "https://evil.example"},
        )
        assert resp.status_code == 403
        assert resp.json() == {"detail": _CROSS_ORIGIN_DETAIL}

    def test_same_origin_post_passes(self, client: TestClient) -> None:
        """A same-origin Origin reaches the route (503 = no store wired, NOT 403)."""
        resp = client.post(
            "/api/v1/universe/load-instruments",
            headers={"Origin": "http://localhost"},
        )
        assert resp.status_code == 503
        assert "InstrumentStore not wired" in resp.json()["detail"]

    def test_no_origin_no_referer_post_passes(self, client: TestClient) -> None:
        """A header-less POST (CLI / curl) is never origin-blocked."""
        resp = client.post("/api/v1/universe/load-instruments")
        assert resp.status_code == 503

    def test_cross_origin_get_not_blocked(self, client: TestClient) -> None:
        """GET is a side-effect-free read — never origin-checked."""
        resp = client.get("/api/v1/state", headers={"Origin": "https://evil.example"})
        assert resp.status_code == 200

    def test_cross_site_referer_post_blocked(self, client: TestClient) -> None:
        """With no Origin, a foreign Referer alone refuses the POST."""
        resp = client.post(
            "/api/v1/universe/load-instruments",
            headers={"Referer": "https://evil.example/page"},
        )
        assert resp.status_code == 403
        assert resp.json() == {"detail": _CROSS_ORIGIN_DETAIL}

    def test_same_origin_referer_post_passes(self, client: TestClient) -> None:
        """A same-origin Referer (no Origin) reaches the route."""
        resp = client.post(
            "/api/v1/universe/load-instruments",
            headers={"Referer": "http://localhost/universe"},
        )
        assert resp.status_code == 503

    def test_second_post_route_still_reachable(self, client: TestClient) -> None:
        """Spot-check another existing POST route: routing intact, not 403."""
        resp = client.post("/api/v1/universe/pull")
        # No `symbol` query param → FastAPI validation 422 (route was reached).
        assert resp.status_code == 422


# ── TrustedHostMiddleware — DNS-rebinding guard ──────────────────────────────


class TestTrustedHost:
    """Host-header allowlisting through the live middleware stack."""

    def test_disallowed_host_rejected_400(self, client: TestClient) -> None:
        """A rebound / foreign Host header is refused with Starlette's 400."""
        resp = client.get("/api/v1/state", headers={"Host": "evil.example"})
        assert resp.status_code == 400
        assert resp.text == "Invalid host header"

    def test_allowed_hosts_pass(self, client: TestClient) -> None:
        """The default allowlist admits the loopback names (any port)."""
        assert client.get("/api/v1/state").status_code == 200
        for host in ("127.0.0.1", "localhost:8000", "127.0.0.1:9095"):
            resp = client.get("/api/v1/state", headers={"Host": host})
            assert resp.status_code == 200, host

    def test_testserver_not_in_production_default(self, client: TestClient) -> None:
        """`testserver` is REFUSED by the production default (R2-DS-F1 fix).

        Regression: the single-label name `testserver` used to ship in the
        production allowlist — an attacker-controlled DNS/DHCP search-domain
        could resolve it at the operator machine and bypass the rebinding
        guard. It now lives only in the test fixture (conftest base_url).
        """
        for host in ("testserver", "testserver:9080"):
            resp = client.get("/api/v1/state", headers={"Host": host})
            assert resp.status_code == 400, host
            assert resp.text == "Invalid host header"

    def test_custom_allowed_hosts_override(self) -> None:
        """An injected allowlist replaces the default (CLI bound-host seam)."""
        app = build_app(
            DashboardState(),
            instrument_store=None,
            allowed_hosts=["myhost:*"],
        )
        with TestClient(app) as custom:
            ok = custom.get("/api/v1/state", headers={"Host": "myhost:7777"})
            assert ok.status_code == 200
            refused = custom.get("/api/v1/state", headers={"Host": "testserver"})
            assert refused.status_code == 400


class TestTrustedHostIPv6Loopback:
    """The bracketed IPv6 loopback rides the default guard (DEC-ENGINE-MISC-PACK-002).

    Through the REAL ``build_app`` middleware stack: an ``http://[::1]:port``
    client sends ``Host: [::1]:port`` (the RFC 3986 bracketed form), which
    Starlette's naive ``split(":")[0]`` port-strip used to mangle to ``[`` —
    a 400 despite being loopback. The ``BracketAwareTrustedHostMiddleware``
    subclass + the ``[::1]`` default-allowlist entries fix it, and — the HARD
    CONSTRAINT — the guard still covers the ``/ws`` WEBSOCKET route (a pure
    ASGI middleware, never an http-only shim).
    """

    def test_ipv6_loopback_get_passes_default_allowlist(self, client: TestClient) -> None:
        """`[::1]` on any port passes the DEFAULT allowlist (was a 400 before)."""
        for host in ("[::1]", "[::1]:8000", "[::1]:9095"):
            resp = client.get("/api/v1/state", headers={"Host": host})
            assert resp.status_code == 200, host

    def test_foreign_bracketed_host_still_400(self, client: TestClient) -> None:
        """A NON-loopback IPv6 literal is still refused — no fail-open."""
        resp = client.get("/api/v1/state", headers={"Host": "[2401:db00::1]:8000"})
        assert resp.status_code == 400
        assert resp.text == "Invalid host header"

    def test_ws_route_admits_ipv6_loopback(self, client: TestClient) -> None:
        """The live /ws websocket handshake passes with the IPv6-loopback Host."""
        with client.websocket_connect("/ws", headers={"host": "[::1]:8000"}) as socket:
            payload = socket.receive_json()
            assert "phases" in payload

    def test_ws_route_still_guards_foreign_host(self, client: TestClient) -> None:
        """REGRESSION (the HARD CONSTRAINT): /ws keeps Host-guard coverage.

        An ``@app.middleware("http")`` swap would have silently dropped this —
        the WEBSOCKET scope must still refuse a foreign / rebound Host.
        """
        from starlette.testclient import WebSocketDenialResponse

        with (
            pytest.raises(WebSocketDenialResponse),
            client.websocket_connect("/ws", headers={"host": "evil.example"}),
        ):
            pytest.fail("foreign-Host /ws handshake must never connect")


# ── Pure helpers — 100% line+branch ──────────────────────────────────────────


class TestTrustedHostPatterns:
    """`_trusted_host_patterns` normalization for Starlette."""

    def test_default_list_normalized_and_deduped(self) -> None:
        """None → the loopback-only default set, `:*` stripped, order-preserving dedupe.

        The bracketed IPv6 spelling rides the SAME `:*` strip
        (``"[::1]:*"`` → ``"[::1]"`` — DEC-ENGINE-MISC-PACK-002).
        """
        assert _trusted_host_patterns(None) == ["127.0.0.1", "localhost", "[::1]"]

    def test_custom_entries_port_wildcard_stripped(self) -> None:
        """A `host:*` entry collapses to its bare host."""
        assert _trusted_host_patterns(["0.0.0.0:*", "myhost"]) == ["0.0.0.0", "myhost"]

    def test_empty_and_duplicate_entries_dropped(self) -> None:
        """Empty strings (incl. a bare `:*`) and duplicates are dropped."""
        assert _trusted_host_patterns(["", ":*", "a", "a:*", "a"]) == ["a"]

    def test_default_constant_carries_port_wildcards(self) -> None:
        """The operator-facing default spells out the any-port intent."""
        assert "127.0.0.1:*" in DEFAULT_ALLOWED_HOSTS
        assert "localhost:*" in DEFAULT_ALLOWED_HOSTS

    def test_default_constant_is_loopback_only(self) -> None:
        """No non-loopback name — in particular no `testserver` — ships in production."""
        assert all(
            entry.removesuffix(":*") in {"127.0.0.1", "localhost", "[::1]"}
            for entry in DEFAULT_ALLOWED_HOSTS
        )


class TestOriginHeaderHost:
    """`_origin_header_host` parsing edge-cases."""

    def test_origin_value(self) -> None:
        """A plain Origin yields its lowercased host:port."""
        assert _origin_header_host("http://LocalHost:8000") == "localhost:8000"

    def test_referer_value_with_path(self) -> None:
        """A Referer's path is ignored — only the netloc matters."""
        assert _origin_header_host("http://testserver/universe?x=1") == "testserver"

    def test_opaque_null_origin(self) -> None:
        """`Origin: null` (sandboxed cross-site frame) has no parsable host."""
        assert _origin_header_host("null") is None

    def test_malformed_url_value_error(self) -> None:
        """An invalid IPv6 literal raises ValueError inside urlsplit → None."""
        assert _origin_header_host("http://[::1") is None


class TestCrossOriginReason:
    """`_cross_origin_reason` — every branch."""

    def test_unguarded_methods_always_allowed(self) -> None:
        """GET/HEAD/OPTIONS pass even with a hostile Origin."""
        for method in ("GET", "HEAD", "OPTIONS", "get"):
            assert _cross_origin_reason(method, "h", "https://evil.example", None) is None

    def test_no_headers_allowed(self) -> None:
        """Neither Origin nor Referer → allow (non-browser client)."""
        assert _cross_origin_reason("POST", "h", None, None) is None

    def test_origin_match_allows(self) -> None:
        """Same-origin Origin (case-insensitive) allows."""
        assert _cross_origin_reason("POST", "TestServer", "http://testserver", None) is None

    def test_origin_mismatch_blocks(self) -> None:
        """Foreign Origin blocks all four state-changing methods."""
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            reason = _cross_origin_reason(method, "h", "https://evil.example", None)
            assert reason == _CROSS_ORIGIN_DETAIL

    def test_origin_authoritative_over_referer(self) -> None:
        """A foreign Origin blocks even when the Referer is same-origin."""
        reason = _cross_origin_reason(
            "POST", "testserver", "https://evil.example", "http://testserver/p"
        )
        assert reason == _CROSS_ORIGIN_DETAIL

    def test_referer_fallback_match_and_mismatch(self) -> None:
        """Referer is consulted only when Origin is absent."""
        assert _cross_origin_reason("POST", "testserver", None, "http://testserver/p") is None
        blocked = _cross_origin_reason("POST", "testserver", None, "https://evil.example/p")
        assert blocked == _CROSS_ORIGIN_DETAIL

    def test_unparsable_origin_fails_closed(self) -> None:
        """`Origin: null` / malformed values refuse, never pass."""
        assert _cross_origin_reason("POST", "h", "null", None) == _CROSS_ORIGIN_DETAIL

    def test_missing_request_host_fails_closed(self) -> None:
        """No Host header on a browser-originated state change → refuse."""
        assert _cross_origin_reason("POST", None, "http://x", None) == _CROSS_ORIGIN_DETAIL
        assert _cross_origin_reason("POST", "  ", "http://x", None) == _CROSS_ORIGIN_DETAIL
