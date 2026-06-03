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
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from http import HTTPStatus
from math import inf
from time import perf_counter
from typing import Final, Protocol
from urllib.parse import urlparse

import structlog

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

#: Structured logger — emits ``dhan.call`` (per-call DEBUG) + ``dhan.throughput``
#: (periodic INFO) into the same structlog stream the operator reads on /logs.
_LOG = structlog.get_logger("brutex.adapters.dhan.client")

#: Hosts the client is allowed to POST to (SSRF guard).
_ALLOWED_HOSTS: frozenset[str] = frozenset({"api.dhan.co"})

#: Default whole-request timeout (seconds).
_DEFAULT_TIMEOUT: float = 30.0

#: Bounded ring of recent per-call latencies kept for the p95 (O(1) memory).
_LATENCY_WINDOW: Final[int] = 256

#: Emit a ``dhan.throughput`` summary every Nth call (per-call stays DEBUG so a
#: 16k-call options fan-out never floods INFO, while the rate stays visible).
_DEFAULT_LOG_EVERY: Final[int] = 25


@dataclass(slots=True)
class CallMeter:
    """Rolling latency + effective-RPS meter for the Dhan transport.

    Every Dhan HTTP round-trip records its MEASURED latency here so the operator
    can SEE the real per-call latency AND the effective requests/second (calls ÷
    wall-elapsed) against the 5 r/s design ceiling (CLAUDE.md §2 Rule 6) — no
    inference. O(1) per :meth:`record` (running count / sum / min / max + a
    bounded ring for the p95); :meth:`snapshot` is O(window) and called only
    periodically.
    """

    count: int = 0
    _sum_s: float = 0.0
    _min_s: float = inf
    _max_s: float = 0.0
    _first_at: float = 0.0
    _last_at: float = 0.0
    _recent: deque[float] = field(default_factory=lambda: deque(maxlen=_LATENCY_WINDOW))

    def record(self, *, latency_s: float, at: float) -> None:
        """Record one completed call's *latency_s* (seconds) at wall-time *at* — O(1)."""
        if self.count == 0:
            self._first_at = at
        self.count += 1
        self._sum_s += latency_s
        self._min_s = min(self._min_s, latency_s)
        self._max_s = max(self._max_s, latency_s)
        self._last_at = at
        self._recent.append(latency_s)

    def snapshot(self) -> dict[str, float | int]:
        """Return a JSON-friendly MEASURED summary (latency in ms, rate in r/s)."""
        if self.count == 0:
            return {
                "calls": 0,
                "effective_rps": 0.0,
                "latency_ms_avg": 0.0,
                "latency_ms_min": 0.0,
                "latency_ms_max": 0.0,
                "latency_ms_p95": 0.0,
            }
        span = self._last_at - self._first_at
        recent = sorted(self._recent)
        p95 = recent[min(len(recent) - 1, int(len(recent) * 0.95))]
        # Inter-arrival rate = (N-1) intervals ÷ wall span — never OVERSTATES a
        # steady stream (so a 5 r/s-paced run reads ≈5, never a false >5).
        return {
            "calls": self.count,
            "effective_rps": round((self.count - 1) / span, 2) if span > 0 else 0.0,
            "latency_ms_avg": round(self._sum_s / self.count * 1000.0, 1),
            "latency_ms_min": round(self._min_s * 1000.0, 1),
            "latency_ms_max": round(self._max_s * 1000.0, 1),
            "latency_ms_p95": round(p95 * 1000.0, 1),
        }


class Transport(Protocol):
    """A POST transport: ``(url, body, headers, timeout) → (status, bytes)``."""

    def __call__(
        self, url: str, *, body: bytes, headers: Mapping[str, str], timeout: float
    ) -> tuple[int, bytes]:
        """Send a POST and return ``(http_status, response_body_bytes)``."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed


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
    #: Monotonic clock for per-call latency (injected for deterministic tests).
    clock: Callable[[], float] = perf_counter
    #: Rolling latency / effective-RPS meter (a fresh one per client).
    meter: CallMeter = field(default_factory=CallMeter)
    #: Emit a ``dhan.throughput`` INFO summary every Nth call.
    log_every: int = _DEFAULT_LOG_EVERY

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
        started = self.clock()
        try:
            status, raw = self.transport(
                url, body=data, headers=self._headers(), timeout=self.timeout
            )
        except TimeoutError as exc:
            self._observe(endpoint, None, started)
            raise DhanTimeoutError(f"timeout POSTing {endpoint!r}") from exc
        except OSError as exc:
            self._observe(endpoint, None, started)
            raise DhanTransportError(f"transport error POSTing {endpoint!r}: {exc}") from exc
        self._observe(endpoint, int(status), started)
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

    def _observe(self, endpoint: str, status: int | None, started: float) -> None:
        """Record one call's MEASURED round-trip latency + emit institutional logging.

        Per call: a DEBUG ``dhan.call`` line (``endpoint`` · ``status`` ·
        ``latency_ms``) — deep-dive detail without flooding INFO. Every
        :attr:`log_every` calls: an INFO ``dhan.throughput`` summary carrying the
        MEASURED effective requests/second (calls ÷ wall-elapsed) + latency
        avg/min/max/p95, so the operator SEES the real rate against the 5 r/s
        design ceiling (CLAUDE.md §2 Rule 6) instead of inferring it. Called on
        EVERY exit of the transport — success, timeout, and connection error —
        so a slow/failing call's latency is never invisible. O(1) per call.
        """
        now = self.clock()
        latency_s = now - started
        self.meter.record(latency_s=latency_s, at=now)
        _LOG.debug(
            "dhan.call", endpoint=endpoint, status=status, latency_ms=round(latency_s * 1000.0, 1)
        )
        if self.meter.count % self.log_every == 0:
            _LOG.info("dhan.throughput", **self.meter.snapshot())


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
    "CallMeter",
    "DhanClient",
    "Transport",
    "urllib_transport",
]
