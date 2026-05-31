"""Dhan REST client — POST a JSON request body to a Data API endpoint.

The client is the ONLY component that performs Dhan network I/O. The
**transport is injected** (a ``(url, body, headers, timeout) → (status,
bytes)`` callable), so every branch of the request/response logic — header
construction, SSRF guard, HTTP-status → :mod:`errors` mapping, JSON
parsing — is unit-testable with a fake transport (no network). The real
transport (:func:`urllib_transport`) is a thin ``urllib`` shim and is the
only ``# pragma: no cover`` surface, exactly like the Groww adapter's
SSRF-guarded fetch.

Rate limiting + retries are NOT the client's concern (CLAUDE.md §11
COMMON) — the orchestrator paces calls via the adaptive token bucket and
consults :func:`brutex.adapters.dhan.errors.is_retryable`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from http import HTTPStatus
from typing import Protocol
from urllib.parse import urlparse

from brutex.adapters.dhan.constants import (
    DHAN_BASE_URL,
    HEADER_ACCESS_TOKEN,
    HEADER_CLIENT_ID,
)
from brutex.adapters.dhan.errors import (
    DhanError,
    DhanTimeoutError,
    DhanTransportError,
    from_http_status,
)

#: Hosts the client is allowed to POST to (SSRF guard).
_ALLOWED_HOSTS: frozenset[str] = frozenset({"api.dhan.co"})

#: Default whole-request timeout (seconds).
_DEFAULT_TIMEOUT: float = 30.0


class Transport(Protocol):
    """A POST transport: ``(url, body, headers, timeout) → (status, bytes)``."""

    def __call__(
        self, url: str, *, body: bytes, headers: Mapping[str, str], timeout: float
    ) -> tuple[int, bytes]:
        """Send a POST and return ``(http_status, response_body_bytes)``."""
        ...


def _guard_url(url: str) -> None:
    """Reject non-HTTPS or non-allowlisted hosts before any network call."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in _ALLOWED_HOSTS:
        raise DhanError(f"refusing non-allowlisted Dhan URL: {url!r}")


@dataclass(frozen=True, slots=True)
class DhanClient:
    """A configured Dhan Data-API caller (credentials + injected transport)."""

    access_token: str
    client_id: str
    transport: Transport
    base_url: str = DHAN_BASE_URL
    timeout: float = _DEFAULT_TIMEOUT

    def _headers(self) -> dict[str, str]:
        """Build the auth + content headers for every request."""
        return {
            HEADER_ACCESS_TOKEN: self.access_token,
            HEADER_CLIENT_ID: self.client_id,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def post_json(self, endpoint: str, body: Mapping[str, object]) -> dict[str, object]:
        """POST *body* as JSON to ``base_url + endpoint``; return the parsed object.

        Raises
        ------
        DhanError
            Non-allowlisted URL, malformed/non-object JSON, or an
            unexpected HTTP status (mapped via :func:`from_http_status`).
        DhanTimeoutError / DhanTransportError
            On a transport timeout / connection failure.
        """
        url = self.base_url + endpoint
        _guard_url(url)
        data = json.dumps(body).encode("utf-8")
        try:
            status, raw = self.transport(
                url, body=data, headers=self._headers(), timeout=self.timeout
            )
        except TimeoutError as exc:
            raise DhanTimeoutError(f"timeout POSTing {endpoint!r}") from exc
        except OSError as exc:
            raise DhanTransportError(f"transport error POSTing {endpoint!r}: {exc}") from exc
        if status != HTTPStatus.OK:
            raise from_http_status(status, raw.decode("utf-8", "replace"))
        try:
            parsed: object = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DhanError(f"malformed JSON from {endpoint!r}: {exc}") from exc
        if not isinstance(parsed, dict):
            raise DhanError(
                f"expected a JSON object from {endpoint!r}, got {type(parsed).__name__}"
            )
        return parsed


def urllib_transport(  # pragma: no cover — thin urllib I/O shim, exercised via integration
    url: str, *, body: bytes, headers: Mapping[str, str], timeout: float
) -> tuple[int, bytes]:
    """Real :mod:`urllib` POST transport (production wiring for :class:`DhanClient`).

    Returns ``(status, body)`` for 2xx **and** 4xx/5xx (``HTTPError`` carries
    a status + body), so :meth:`DhanClient.post_json` maps non-2xx uniformly.
    """
    import urllib.error
    import urllib.request

    request = urllib.request.Request(  # noqa: S310 — https + host allow-listed by _guard_url
        url, data=body, headers=dict(headers), method="POST"
    )
    try:
        with urllib.request.urlopen(  # nosec B310 — https + host allow-listed by _guard_url  # noqa: S310
            request, timeout=timeout
        ) as resp:
            return int(resp.status), resp.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read()


__all__ = [
    "DhanClient",
    "Transport",
    "urllib_transport",
]
