"""Tests for :mod:`brutex.dashboard.trusted_host` (DEC-ENGINE-MISC-PACK-002).

The bracket-aware ``TrustedHostMiddleware`` subclass: Starlette's naive
``split(":")[0]`` port-strip mangles a bracketed IPv6 ``Host`` (``[::1]:8000``
→ ``[``), so the IPv6 loopback could never pass the guard no matter what the
allowlist said. The subclass admits an EXACT (case-insensitive) allowlisted
bracketed host on BOTH the ``http`` and ``websocket`` scopes — the HARD
CONSTRAINT: never an ``@app.middleware("http")`` shim, which would drop
Host-guard coverage on the live ``/ws`` websocket route — and delegates every
other case verbatim to the parent (400 refusal, wildcards, non-bracketed
hosts byte-identical).
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("starlette")

from brutex.dashboard.trusted_host import (
    BracketAwareTrustedHostMiddleware,
    host_without_port,
)
from starlette.testclient import TestClient, WebSocketDenialResponse

# ── host_without_port — the pure bracket-aware port strip ────────────────────


class TestHostWithoutPort:
    @pytest.mark.parametrize(
        ("header", "expected"),
        [
            ("[::1]:8000", "[::1]"),  # bracketed + port → brackets kept, port dropped.
            ("[::1]", "[::1]"),  # bracketed, no port → unchanged.
            ("[2401:db00::1]:443", "[2401:db00::1]"),  # non-loopback literal too.
            ("localhost:8000", "localhost"),  # plain host+port → Starlette behaviour.
            ("localhost", "localhost"),
            ("127.0.0.1:9095", "127.0.0.1"),
            ("", ""),  # absent header degrades to the empty host (no match).
        ],
    )
    def test_strip_cases(self, header: str, expected: str) -> None:
        assert host_without_port(header) == expected

    def test_malformed_open_bracket_degrades_like_starlette(self) -> None:
        """No closing ``]`` → the naive split result (fail-closed, never fail-open)."""
        assert host_without_port("[::1") == "["
        assert host_without_port("[broken") == "[broken"


# ── the middleware over a raw ASGI app — http AND websocket scopes ───────────


async def _ok_app(scope: Any, receive: Any, send: Any) -> None:
    """Minimal ASGI app: 200 ``ok`` for http; accept-then-close for websocket."""
    if scope["type"] == "websocket":
        await receive()  # the connect message.
        await send({"type": "websocket.accept"})
        await send({"type": "websocket.close", "code": 1000})
        return
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def _client(allowed_hosts: list[str]) -> TestClient:
    return TestClient(BracketAwareTrustedHostMiddleware(_ok_app, allowed_hosts=allowed_hosts))


class TestBracketAwareHttpScope:
    def test_allowlisted_bracketed_host_passes_with_and_without_port(self) -> None:
        client = _client(["[::1]"])
        assert client.get("/", headers={"Host": "[::1]:8000"}).status_code == 200
        assert client.get("/", headers={"Host": "[::1]"}).status_code == 200

    def test_bracketed_match_is_case_insensitive(self) -> None:
        """IPv6 hex digits may arrive uppercased — the exact match ignores case."""
        client = _client(["[::a]"])
        assert client.get("/", headers={"Host": "[::A]:9000"}).status_code == 200

    def test_foreign_bracketed_host_still_400s(self) -> None:
        client = _client(["[::1]"])
        resp = client.get("/", headers={"Host": "[::2]:8000"})
        assert resp.status_code == 400
        assert resp.text == "Invalid host header"

    def test_malformed_bracket_host_400s_fail_closed(self) -> None:
        client = _client(["[::1]"])
        assert client.get("/", headers={"Host": "[::1"}).status_code == 400

    def test_non_bracketed_hosts_delegate_to_parent_byte_identical(self) -> None:
        """The parent's behaviour is untouched for every non-bracketed Host."""
        client = _client(["localhost", "[::1]"])
        assert client.get("/", headers={"Host": "localhost:7777"}).status_code == 200
        refused = client.get("/", headers={"Host": "evil.example"})
        assert refused.status_code == 400
        assert refused.text == "Invalid host header"

    def test_allow_any_wildcard_short_circuits(self) -> None:
        """``*`` (allow_any) admits everything via the parent — our branch skipped."""
        client = _client(["*"])
        assert client.get("/", headers={"Host": "[::2]:1"}).status_code == 200


class TestBracketAwareWebsocketScope:
    """The HARD-CONSTRAINT regression: the guard covers the websocket scope."""

    def test_allowlisted_bracketed_host_ws_connects(self) -> None:
        client = _client(["[::1]"])
        with client.websocket_connect("/", headers={"host": "[::1]:8000"}):
            pass  # handshake accepted — the guard admitted the IPv6 loopback.

    def test_foreign_host_ws_still_refused(self) -> None:
        """A foreign Host on the WEBSOCKET scope is still refused (coverage kept)."""
        client = _client(["[::1]"])
        with (
            pytest.raises(WebSocketDenialResponse),
            client.websocket_connect("/", headers={"host": "evil.example"}),
        ):
            pytest.fail("foreign-Host websocket must never connect")

    def test_foreign_bracketed_host_ws_refused(self) -> None:
        client = _client(["[::1]"])
        with (
            pytest.raises(WebSocketDenialResponse),
            client.websocket_connect("/", headers={"host": "[::2]"}),
        ):
            pytest.fail("foreign bracketed-Host websocket must never connect")
