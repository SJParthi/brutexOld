"""Bracket-aware ``TrustedHostMiddleware`` for IPv6-loopback Hosts (DEC-ENGINE-MISC-PACK-002).

Starlette's :class:`~starlette.middleware.trustedhost.TrustedHostMiddleware`
strips the ``Host`` header's port with a naive ``split(":")[0]`` (verified on
the installed Starlette), which MANGLES a bracketed RFC 3986 IPv6 literal:
``Host: [::1]:8000`` → ``[`` (and even the port-less ``[::1]`` → ``[``), so an
IPv6-loopback client was 400ed no matter what the allowlist said. Adding
``[::1]`` to :data:`~brutex.dashboard.host_allowlist.DEFAULT_ALLOWED_HOSTS`
alone could therefore never work — the extraction had to become bracket-aware.

HARD CONSTRAINT honoured: this is a SUBCLASS of the pure-ASGI
``TrustedHostMiddleware``, NOT an ``@app.middleware("http")`` shim — an
http-only shim would silently DROP Host-guard coverage on the live ``/ws``
websocket route (``app.add_api_websocket_route("/ws", ...)``), because
FastAPI's decorator middleware runs only for ``http`` scopes. The subclass
keeps guarding BOTH the ``http`` AND ``websocket`` scopes.

Scope of the override (Golden Rule #7 — byte-identical elsewhere): ONLY a
``Host`` header opening with ``[`` (a bracketed IPv6 literal) is handled here,
by an exact case-insensitive match of its bracket-aware port-stripped form
against the allowed patterns; every other Host — and every non-match — is
DELEGATED verbatim to the parent middleware (same 400 body, same ``www.``
redirect behaviour, same ``*`` / ``*.domain`` wildcard semantics). Wildcard
patterns are deliberately NOT applied to bracketed literals (a ``*.`` suffix
match is meaningless for an address literal). NAMED FOLLOW-UP (not in scope):
generalized bracketed-IPv6 support for arbitrary non-loopback IPv6 binds in
``brutex.cli._dashboard_allowed_hosts`` (its ``{bare}:*`` extension would need
re-bracketing for an IPv6 bind).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.datastructures import Headers
from starlette.middleware.trustedhost import TrustedHostMiddleware

if TYPE_CHECKING:
    from starlette.types import Receive, Scope, Send

__all__ = ["BracketAwareTrustedHostMiddleware", "host_without_port"]


def host_without_port(host_header: str) -> str:
    """Strip the ``:port`` suffix from a ``Host`` header value, bracket-aware.

    A bracketed IPv6 literal (RFC 3986 host form) keeps its brackets:
    ``"[::1]:8000"`` → ``"[::1]"`` and ``"[::1]"`` → ``"[::1]"``. Every other
    shape gets Starlette's own ``split(":")[0]`` behaviour
    (``"localhost:8000"`` → ``"localhost"``), including a MALFORMED bracket
    opener with no closing ``]`` — which degrades to the same mangled token
    Starlette would produce, and therefore still fails the allowlist match
    (fail-closed, never fail-open). Pure, O(len(header)).

    Parameters
    ----------
    host_header
        The raw ``Host`` header value.

    Returns
    -------
    str
        The host with any port suffix removed.
    """
    if host_header.startswith("["):
        end = host_header.find("]")
        if end != -1:
            return host_header[: end + 1]
    return host_header.split(":", maxsplit=1)[0]


class BracketAwareTrustedHostMiddleware(TrustedHostMiddleware):  # pyright: ignore[reportUntypedBaseClass]
    """``TrustedHostMiddleware`` that admits allowlisted BRACKETED IPv6 Hosts.

    Pure-ASGI (guards ``http`` AND ``websocket`` scopes — the ``/ws`` route
    stays covered). A guarded-scope request whose ``Host`` opens with ``[`` is
    matched here: its bracket-aware port-stripped form
    (:func:`host_without_port`) is compared case-insensitively against the
    configured patterns, and an exact match passes straight to the app. EVERY
    other case — a non-bracketed Host, a non-matching bracketed Host, a
    non-guarded scope (``lifespan``), or an ``allow_any`` (``*``) config — is
    delegated verbatim to the parent implementation, so its behaviour (the
    400 refusal, wildcard patterns, the ``www.`` redirect) is byte-identical.
    """

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Admit an allowlisted bracketed-IPv6 ``Host``; else defer to Starlette."""
        if not self.allow_any and scope["type"] in ("http", "websocket"):
            host_header = Headers(scope=scope).get("host", "")
            if host_header.startswith("["):
                host = host_without_port(host_header).lower()
                if any(host == pattern.lower() for pattern in self.allowed_hosts):
                    await self.app(scope, receive, send)
                    return
        await super().__call__(scope, receive, send)
