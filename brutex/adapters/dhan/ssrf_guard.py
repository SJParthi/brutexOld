"""SSRF guard for the Dhan HTTP transport.

The Dhan adapter resolves ``api.dhan.co`` to a public IP and POSTs JSON
to it. A poisoned DNS resolver (or attacker-controlled ``/etc/hosts`` on
a shared CI runner) could route the hostname to a private IP — typically
``127.0.0.1`` (loopback) or ``169.254.169.254`` (AWS IMDS) — and the
adapter would happily POST the access token there.

This module defends against that by pre-resolving the hostname at adapter
construction time and asserting EVERY resolved IP is "global" per
:meth:`ipaddress._BaseAddress.is_global`. Private / loopback / link-local
/ multicast / unspecified addresses all fail the check.

What this catches:
* Resolver poisoning → 10.x.x.x, 192.168.x.x, 172.16-31.x.x
* Loopback redirect → 127.x.x.x, ::1
* IMDS attack → 169.254.169.254
* Link-local IPv6 → fe80::/10
* Reserved / unspecified → 0.0.0.0, ::

What this does NOT catch:
* DNS rebinding mid-session — we resolve once. A resolver that returns
  public for the first lookup and private for a subsequent one (TTL=0)
  would slip through. Mitigation: short-lived ``DhanClient`` instances +
  IP pinning at the transport level (deferred — would require a custom
  httpcore Transport).
* IP-direct attacks — if the caller bypasses :func:`assert_host_is_public`
  and constructs an ``httpx.Client`` with a literal IP, the guard never
  runs. The :class:`DhanClient` always uses a hostname so this is moot
  in practice.

Decision context (DEC-P0-036, PR #2c):

* The pre-impl Security agent flagged SSRF DNS resolution as a Blocker
  for the Dhan adapter (4-agent review, 2026-05-18).
* The guard runs at adapter construction; failure raises
  :class:`SSRFGuardError` so the orchestrator fails fast (refusing to
  even SEND the token).
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Final
from urllib.parse import urlparse

from brutex.models import BruteXError

#: Default DNS-resolution timeout (seconds). Short — DNS should be fast,
#: and a hung resolver itself is a red flag worth surfacing.
_DNS_TIMEOUT_SECONDS: Final[float] = 5.0


class SSRFGuardError(BruteXError):
    """Hostname resolved to a non-global IP — refusing to connect."""


def assert_host_is_public(url: str) -> None:
    """Resolve the hostname in *url* and assert every IP is public.

    Parameters
    ----------
    url
        Full URL whose hostname (and port, if non-default) will be
        resolved via ``socket.getaddrinfo``.

    Raises
    ------
    SSRFGuardError
        If any resolved address is non-global (private, loopback,
        link-local, multicast, unspecified, reserved). Also raised when
        the URL has no hostname or DNS resolution fails.
    """
    parsed = urlparse(url)
    hostname = parsed.hostname
    if not hostname:
        raise SSRFGuardError(
            f"assert_host_is_public: URL has no hostname: {url!r}",
            safe_message="Dhan SSRF guard: URL has no hostname",
        )

    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == "https" else 80

    socket.setdefaulttimeout(_DNS_TIMEOUT_SECONDS)
    try:
        infos = socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise SSRFGuardError(
            f"assert_host_is_public: DNS resolution failed for {hostname!r}: {exc}",
            safe_message="Dhan SSRF guard: DNS resolution failed",
        ) from exc

    if not infos:
        raise SSRFGuardError(
            f"assert_host_is_public: no addresses returned for {hostname!r}",
            safe_message="Dhan SSRF guard: no addresses returned",
        )

    for info in infos:
        sockaddr = info[4]
        addr_str = sockaddr[0]
        try:
            addr = ipaddress.ip_address(addr_str)
        except ValueError as exc:
            raise SSRFGuardError(
                f"assert_host_is_public: cannot parse address {addr_str!r}: {exc}",
                safe_message="Dhan SSRF guard: cannot parse resolved address",
            ) from exc
        # ``is_global`` is False for private / loopback / link-local /
        # unspecified / reserved. ``is_multicast`` is True for 224/4 (IPv4)
        # and ff00::/8 (IPv6); Python's stdlib counts multicast as
        # "global" — we don't, because no production API would route via
        # multicast and accepting it is a needless attack surface.
        if not addr.is_global or addr.is_multicast:
            raise SSRFGuardError(
                f"assert_host_is_public: {hostname!r} resolves to non-global "
                f"address {addr_str!r}; refusing connection",
                safe_message=(
                    "Dhan SSRF guard: hostname resolves to a private / "
                    "loopback / link-local / multicast address "
                    "(DNS poisoning suspected)"
                ),
            )
