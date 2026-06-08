"""Dhan REST client — POST a JSON body (or GET headers-only) to a Data API endpoint.

The client is the ONLY component that performs Dhan network I/O. The
**transport is injected** (a ``(url, body, headers, timeout, method) →
(status, bytes)`` callable), so every branch of the request/response logic
— header construction, SSRF guard, HTTP-status → :mod:`errors` mapping,
JSON parsing — is unit-testable with a fake transport (no network). The
real transport (:func:`urllib_transport`) is a thin ``urllib`` shim and is
the only ``# pragma: no cover`` surface, exactly like the Groww adapter's
SSRF-guarded fetch.

Most Dhan Data-API surfaces are POSTs (a JSON request body); ``RenewToken``
is the one **GET** — headers-only, NO body, exactly as the official DhanHQ
SDK calls it (``dhan-oss/DhanHQ-py`` ``src/dhanhq/auth.py``:
``requests.get(url, headers=headers)``). :meth:`DhanClient.get_json` serves
that path; :meth:`DhanClient.post_json` is byte-unchanged for the POST
surfaces. Both share the same metered, paced, bounded fast-retry loop
(DEC-DHAN-RENEWTOKEN-GET-001).

Rate limiting + *logical* retry/re-queue stay the orchestrator's concern
(CLAUDE.md §11 COMMON) — it paces calls via the adaptive token bucket and
re-queues windows that fail. The client owns ONE narrow, bounded
*latency-hygiene* concern (DEC-DHAN-CALL-TIMEOUT-RETRY-001): a stalled /
transient transport (or 5xx) call is fast-retried in place
(:attr:`DhanClient.max_attempts`, exponential backoff seeded at the 5 r/s
interval) so a single 30s-style stall can no longer freeze a SEQUENTIAL pull.
A rate-limit (HTTP 429) is NEVER fast-retried here (the token bucket owns 429
backoff). The configured 5 r/s cap + ``rate_control`` are byte-unchanged and the
backoff is spaced at the rate interval, so a single sequential stream stays
within the per-second budget; the retries are NOT token-bucket-coordinated,
though, so under a *sustained fast-failing* burst the instantaneous physical
rate can briefly overlap the next paced window (bounded by ``max_attempts``,
non-sustained — honest envelope in MIGRATION.md #427). Not delegated to
:func:`brutex.orchestrator.retry.retry_call` (which RETRIES 429 and does not
meter/log each physical attempt); the predicate reuses the COMMON
:func:`brutex.adapters.dhan.errors.is_retryable`.
"""

from __future__ import annotations

import json
import threading
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from http import HTTPStatus
from math import inf
from time import perf_counter
from time import sleep as _time_sleep
from typing import Final, Protocol
from urllib.parse import urlparse

import structlog

from brutex.adapters.dhan.constants import (
    DATA_RATE_LIMIT_RPS,
    DHAN_BASE_URL,
    HEADER_ACCESS_TOKEN,
    HEADER_CLIENT_ID,
)
from brutex.adapters.dhan.errors import (
    DhanError,
    DhanTimeoutError,
    DhanTransportError,
    from_http_status,
    is_rate_limit,
    is_retryable,
)

#: Structured logger — emits ``dhan.call`` (per-call DEBUG) + ``dhan.throughput``
#: (periodic INFO) into the same structlog stream the operator reads on /logs.
_LOG = structlog.get_logger("brutex.adapters.dhan.client")

#: Hosts the client is allowed to POST to (SSRF guard).
_ALLOWED_HOSTS: frozenset[str] = frozenset({"api.dhan.co"})

#: Default whole-request timeout (seconds). History:
#: 30 → 10 (DEC-DHAN-CALL-TIMEOUT-RETRY-001: a single 30s-ceiling stall froze the
#: SEQUENTIAL pull for 30s — ``latency_ms_max: 30080``, RPS collapsing 5.1→3.75).
#: 10 → 25 (DEC-DHAN-INTRADAY-TIMEOUT-RAISE-001, operator live run 2026-06-08):
#: the original "responses land well under a few seconds" assumption was WRONG
#: for a 783-symbol full-history pull — Dhan's ``charts/intraday`` takes ≥10s to
#: build a 90-day **1-minute** payload (~22k bars) under sustained load (observed
#: ``latency_ms_p95 ≈ 10,072`` — pinned right at the old 10s ceiling), so a flood
#: of would-have-SUCCEEDED calls were killed as ``DhanTimeoutError`` and
#: re-requested, ADDING load. 25s lets a slow-but-valid intraday response finish
#: on the first attempt. The 30→10 "one stall freezes everything" concern no
#: longer applies: the window fetch is now PARALLEL (DEC-DHAN-PARALLEL-WINDOWS-001),
#: so one slow call doesn't block the pull, and the bounded fast-retry below still
#: recovers a TRUE stall (caught at 25s). Overridable per client via the
#: :attr:`DhanClient.timeout` field.
_DEFAULT_TIMEOUT: float = 25.0

#: Default bounded fast-retry budget: total POST attempts for one logical call
#: (1 initial + up to 2 in-place retries) on a transient transport/server stall.
#: Bounded → O(1); see :func:`_should_fast_retry` for what is (and is NOT, e.g.
#: a 429) retried, and DEC-DHAN-CALL-TIMEOUT-RETRY-001 for the rationale.
_DEFAULT_MAX_ATTEMPTS: Final[int] = 3

#: Base backoff (seconds) for the fast-retry. Seeded at the **5 r/s
#: inter-request interval** (1 / 5 = 0.2 s, CLAUDE.md §2 Rule 6) so a single
#: SEQUENTIAL stream's in-place retries are spaced no tighter than the rate
#: budget would space them — keeping a self-healing window within the per-second
#: cap without coupling the client to the token bucket. The *k*-th retry waits
#: ``base × 2**(k-1)`` (0.2 s, 0.4 s …), clamped to :data:`_MAX_BACKOFF_S`. Still
#: latency hygiene (≤ ~0.6 s added on a fully-failing window) vs the 10 s timeout.
_DEFAULT_RETRY_BACKOFF_S: Final[float] = 0.2

#: Hard ceiling (seconds) on a single fast-retry backoff — defends against an
#: operator-set large ``max_attempts`` driving ``base × 2**(k-1)`` toward ``inf``
#: (and ``sleep(inf)`` hanging the pull). Mirrors the clamp in
#: :func:`brutex.orchestrator.retry.backoff_delay`.
_MAX_BACKOFF_S: Final[float] = 5.0

#: Bounded ring of recent per-call latencies kept for the p95 (O(1) memory).
_LATENCY_WINDOW: Final[int] = 256

#: Emit a ``dhan.throughput`` summary every Nth call (per-call stays DEBUG so a
#: 16k-call options fan-out never floods INFO, while the rate stays visible).
_DEFAULT_LOG_EVERY: Final[int] = 25

#: Width (seconds) of the rolling window for the INSTANTANEOUS issue-rate gauge
#: (``rps_1s`` / ``rps_1s_max``). One second = the unit Dhan's 5 r/s cap is
#: expressed in (CLAUDE.md §2 Rule 6), so a trailing-1s POST count is the
#: DIRECT real-time measurement of "did we ever exceed 5 per second?" — unlike
#: the cumulative ``effective_rps`` average, which can read high in the opening
#: without any true per-second breach (DEC-DHAN-RATE-REALTIME-GUARANTEE-001).
_RPS_WINDOW_S: Final[float] = 1.0


def _no_pace() -> None:
    """Default no-op pace hook — the client is rate-agnostic unless a bucket is wired."""


@dataclass(slots=True)
class CallMeter:
    """Rolling latency + effective-RPS meter for the Dhan transport.

    Every Dhan HTTP round-trip records its MEASURED latency here so the operator
    can SEE the real per-call latency AND the effective requests/second (calls ÷
    wall-elapsed) against the 5 r/s design ceiling (CLAUDE.md §2 Rule 6) — no
    inference. O(1) per :meth:`record` (running count / sum / min / max + a
    bounded ring for the p95); :meth:`snapshot` is O(window) and called only
    periodically.

    Thread-safety (DEC-DHAN-PARALLEL-INGEST-001). One :class:`DhanClient` (hence
    one ``CallMeter``) is shared by every parallel worker, so multiple calls
    COMPLETE concurrently. A :class:`threading.Lock` makes each :meth:`record`
    and :meth:`snapshot` atomic so the running aggregates never tear under a
    racing increment. The lock is held ONLY for the in-memory bookkeeping at
    call COMPLETION — never across the POST itself (the transport runs before
    ``record`` is reached), so it adds no network serialisation.
    """

    count: int = 0
    _sum_s: float = 0.0
    _min_s: float = inf
    _max_s: float = 0.0
    _first_at: float = 0.0
    _last_at: float = 0.0
    _recent: deque[float] = field(default_factory=lambda: deque(maxlen=_LATENCY_WINDOW))
    #: Monotonic timestamps of POST ISSUES within the trailing ``_RPS_WINDOW_S``
    #: (evicted on each :meth:`record_issue`) — the instantaneous per-second gauge.
    _issued: deque[float] = field(default_factory=deque)
    #: Peak trailing-1s issue count seen this run — the real-time ASSURANCE that
    #: per-second never crossed 5 (``rps_1s_max <= 5`` ⇒ no breach, ever).
    _max_rps_1s: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def record_issue(self, at: float) -> int:
        """Record one POST ISSUED at monotonic *at*; return the trailing-1s count — O(1), atomic.

        Counts EVERY physical POST at the moment it is sent (initial + every
        fast-retry), so the returned ``rps_1s`` is the TRUE instantaneous issue
        rate against the 5 r/s cap (CLAUDE.md §2 Rule 6) — the caller logs a
        ``rate.breach`` WARNING if it exceeds the cap. Evicting entries older than
        ``_RPS_WINDOW_S`` keeps the deque bounded to ≈ the rate, so this stays
        O(1) amortised (DEC-DHAN-RATE-REALTIME-GUARANTEE-001).
        """
        with self._lock:
            self._issued.append(at)
            cutoff = at - _RPS_WINDOW_S
            while self._issued and self._issued[0] <= cutoff:
                self._issued.popleft()
            rps_1s = len(self._issued)
            self._max_rps_1s = max(self._max_rps_1s, rps_1s)
            return rps_1s

    def record(self, *, latency_s: float, at: float) -> None:
        """Record one completed call's *latency_s* (seconds) at wall-time *at* — O(1), atomic."""
        with self._lock:
            if self.count == 0:
                self._first_at = at
            self.count += 1
            self._sum_s += latency_s
            self._min_s = min(self._min_s, latency_s)
            self._max_s = max(self._max_s, latency_s)
            self._last_at = at
            self._recent.append(latency_s)

    def snapshot(self) -> dict[str, float | int]:
        """Return a JSON-friendly MEASURED summary (latency in ms, rate in r/s) — atomic."""
        with self._lock:
            if self.count == 0:
                # No COMPLETED round-trip yet — but POSTs may already be in flight,
                # so still surface the issue-rate gauge (record_issue fires at send).
                return {
                    "calls": 0,
                    "effective_rps": 0.0,
                    "rps_1s": len(self._issued),
                    "rps_1s_max": self._max_rps_1s,
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
                # The INSTANTANEOUS gauge: ``rps_1s_max`` is the peak trailing-1s
                # POST count this run — ``<= 5`` is the real-time PROOF the 5 r/s
                # cap was never crossed (DEC-DHAN-RATE-REALTIME-GUARANTEE-001).
                "rps_1s": len(self._issued),
                "rps_1s_max": self._max_rps_1s,
                "latency_ms_avg": round(self._sum_s / self.count * 1000.0, 1),
                "latency_ms_min": round(self._min_s * 1000.0, 1),
                "latency_ms_max": round(self._max_s * 1000.0, 1),
                "latency_ms_p95": round(p95 * 1000.0, 1),
            }


class Transport(Protocol):
    """An HTTP transport: ``(url, body, headers, timeout, method) → (status, bytes)``.

    ``method`` defaults to ``"POST"`` so every existing POST caller / fake
    transport keeps the original signature; the ``RenewToken`` GET path passes
    ``method="GET"`` with an empty body (DEC-DHAN-RENEWTOKEN-GET-001).
    """

    def __call__(
        self,
        url: str,
        *,
        body: bytes,
        headers: Mapping[str, str],
        timeout: float,
        method: str = "POST",
    ) -> tuple[int, bytes]:
        """Send the request and return ``(http_status, response_body_bytes)``."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed


def _guard_url(url: str) -> None:
    """Reject non-HTTPS or non-allowlisted hosts before any network call."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in _ALLOWED_HOSTS:
        raise DhanError(f"refusing non-allowlisted Dhan URL: {url!r}")


def _should_fast_retry(exc: DhanError) -> bool:
    """True if *exc* is a transient stall worth a bounded in-place fast-retry.

    Reuses the COMMON :func:`is_retryable` predicate (CLAUDE.md §11) — so the
    fast-retry set is timeout + transport + 5xx-server — but DELIBERATELY
    EXCLUDES rate-limit (HTTP 429) via :func:`is_rate_limit`: a 429 means "slow
    down", and the orchestrator's adaptive token bucket owns that backoff
    (CLAUDE.md §2 Rule 6). Fast-retrying a 429 here would directly fight the
    limiter; excluding it (plus a backoff seeded at the 5 r/s interval) keeps a
    single sequential stream within the per-second budget — latency hygiene, not
    a steady-state throughput change (DEC-DHAN-CALL-TIMEOUT-RETRY-001; honest
    envelope re: fast-failing bursts in MIGRATION.md #427).
    """
    return is_retryable(exc) and not is_rate_limit(exc)


@dataclass(frozen=True, slots=True)
class DhanClient:
    """A configured Dhan Data-API caller (credentials + injected transport)."""

    access_token: str
    client_id: str
    transport: Transport
    base_url: str = DHAN_BASE_URL
    timeout: float = _DEFAULT_TIMEOUT
    #: Bounded fast-retry budget — total attempts (1 initial + retries) for a
    #: transient transport/server stall. Latency hygiene, NEVER a 429 retry.
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS
    #: Base exponential-backoff seconds between fast-retries.
    retry_backoff_s: float = _DEFAULT_RETRY_BACKOFF_S
    #: Monotonic clock for per-call latency (injected for deterministic tests).
    clock: Callable[[], float] = perf_counter
    #: Sleep hook for the inter-retry backoff (injected → no real wait in tests).
    sleep: Callable[[float], None] = _time_sleep
    #: Rolling latency / effective-RPS meter (a fresh one per client).
    meter: CallMeter = field(default_factory=CallMeter)
    #: Emit a ``dhan.throughput`` INFO summary every Nth call.
    log_every: int = _DEFAULT_LOG_EVERY
    #: Rate-bucket acquire hook, called before EVERY physical POST including each
    #: fast-retry, so a transient-stall retry can no longer bypass the shared 5
    #: r/s token bucket (DEC-DHAN-RATE-REALTIME-GUARANTEE-001). Default no-op keeps
    #: the client rate-agnostic (same injection pattern as ``transport`` / ``clock``
    #: / ``sleep``); the live ingest wires ``controller.pace`` in.
    pace: Callable[[], None] = field(default=_no_pace)
    #: Optional live-token provider (DEC-DHAN-TOKEN-KEEPALIVE-001). When set,
    #: :meth:`_headers` reads the ``access-token`` value from ``token_provider()``
    #: on EVERY request instead of the static :attr:`access_token`, so a background
    #: keepalive renew (which INVALIDATES the old token) propagates instantly to
    #: this client and every in-flight worker sharing it. ``None`` (the default)
    #: keeps the static-token behaviour, so every existing caller is byte-unchanged.
    token_provider: Callable[[], str] | None = field(default=None)

    def __post_init__(self) -> None:
        """Reject a non-positive fast-retry budget (fail fast on misconfig)."""
        if self.max_attempts < 1:
            raise ValueError(f"max_attempts must be >= 1; got {self.max_attempts!r}")

    def _headers(self) -> dict[str, str]:
        """Build the auth + content headers for every request.

        The ``access-token`` value is read from :attr:`token_provider` when one is
        injected (the live keepalive's holder — DEC-DHAN-TOKEN-KEEPALIVE-001), so a
        mid-life renew that invalidated the old token is picked up on the very next
        request; otherwise the static :attr:`access_token` is used (unchanged for
        every existing caller).
        """
        token = self.token_provider() if self.token_provider is not None else self.access_token
        return {
            HEADER_ACCESS_TOKEN: token,
            HEADER_CLIENT_ID: self.client_id,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def post_json(self, endpoint: str, body: Mapping[str, object]) -> dict[str, object]:
        """POST *body* as JSON to ``base_url + endpoint``; return the parsed object.

        A transient transport/server **stall** (timeout, connection drop, or a
        5xx) is fast-retried in place up to :attr:`max_attempts` times with an
        exponential backoff seeded at the 5 r/s interval (:attr:`retry_backoff_s`,
        clamped to :data:`_MAX_BACKOFF_S`), so a single slow call can no longer
        freeze a SEQUENTIAL pull (DEC-DHAN-CALL-TIMEOUT-RETRY-001). A **rate-limit
        (429)** is NOT fast-retried here — the orchestrator's token bucket owns
        that backoff, and the configured 5 r/s cap + ``rate_control`` are
        unchanged (CLAUDE.md §2 Rule 6; the retries are spaced at the rate
        interval so a single stream stays within the per-second budget — honest
        envelope re: fast-failing bursts in MIGRATION.md #427). Terminal errors
        (400/404/auth) never retry. Each physical attempt is metered by
        :attr:`meter`; a retry emits a ``dhan.retry`` INFO line so it is visible
        on /logs. Bounded attempts + O(1) backoff math keep the hot path O(1).

        Raises
        ------
        DhanError
            Non-allowlisted URL, malformed/non-object JSON, or an unexpected
            HTTP status (mapped via :func:`from_http_status`) — after the
            fast-retry budget is exhausted for transient faults.
        DhanTimeoutError / DhanTransportError
            On a transport timeout / connection failure that outlived the
            fast-retry budget.
        """
        return self._with_fast_retry("POST", endpoint, body)

    def get_json(self, endpoint: str) -> dict[str, object]:
        """GET ``base_url + endpoint`` headers-only (NO body); return the parsed object.

        The headers-only GET surface — currently only ``RenewToken``, which the
        official DhanHQ SDK calls as ``requests.get(url, headers=headers)``
        (``dhan-oss/DhanHQ-py`` ``src/dhanhq/auth.py``), NOT a POST with a JSON
        body (DEC-DHAN-RENEWTOKEN-GET-001). Shares the SAME metered + paced +
        bounded fast-retry loop as :meth:`post_json` (an empty ``b""`` body is
        sent), so pacing, the 5 r/s issue gauge / ``rate.breach`` alarm, and the
        transient-stall fast-retry all apply identically to a GET (DEC-DHAN-RATE-
        REALTIME-GUARANTEE-001 / DEC-DHAN-RATE-RESIDUAL-HARDEN-001).

        Raises
        ------
        DhanError / DhanTimeoutError / DhanTransportError
            Exactly as :meth:`post_json` — see its docstring.
        """
        return self._with_fast_retry("GET", endpoint, None)

    def _with_fast_retry(
        self, method: str, endpoint: str, body: Mapping[str, object] | None
    ) -> dict[str, object]:
        """Run :meth:`_send_once` under the bounded latency-hygiene fast-retry loop.

        The single retry policy shared by :meth:`post_json` (POST + body) and
        :meth:`get_json` (GET, no body): a transient transport/server stall is
        fast-retried in place up to :attr:`max_attempts` times with an
        exponential backoff (clamped to :data:`_MAX_BACKOFF_S`); a 429 / terminal
        error is never retried; each retry emits ``dhan.retry`` and is paced
        through the shared rate bucket. Bounded attempts + O(1) backoff keep the
        hot path O(1) for both verbs (DEC-DHAN-RENEWTOKEN-GET-001).
        """
        attempt = 0
        while True:
            attempt += 1
            try:
                return self._send_once(method, endpoint, body)
            except DhanError as exc:
                if not _should_fast_retry(exc):
                    raise  # terminal (400/404/auth) or 429 (token bucket owns it)
                if attempt >= self.max_attempts:
                    raise  # bounded budget exhausted — orchestrator re-queues
                backoff_s = min(self.retry_backoff_s * (2.0 ** (attempt - 1)), _MAX_BACKOFF_S)
                _LOG.info(
                    "dhan.retry",
                    endpoint=endpoint,
                    attempt=attempt,
                    max_attempts=self.max_attempts,
                    error=type(exc).__name__,
                    backoff_ms=round(backoff_s * 1000.0, 1),
                )
                self.sleep(backoff_s)
                # Gate the RETRY through the shared rate bucket — the initial
                # attempt was already paced by the ingest layer, so pacing here
                # (and only here) makes EVERY physical request bucket-bounded
                # without double-pacing the first (DEC-DHAN-RATE-REALTIME-GUARANTEE-001).
                self.pace()

    def _send_once(
        self, method: str, endpoint: str, body: Mapping[str, object] | None
    ) -> dict[str, object]:
        """One physical request attempt (guard → transport → status/JSON map).

        The single round-trip the :meth:`_with_fast_retry` loop wraps, for either
        verb: a POST serialises *body* to JSON; a GET sends an empty ``b""`` body
        and ``method="GET"`` (headers-only, the ``RenewToken`` shape — DEC-DHAN-
        RENEWTOKEN-GET-001). Every exit (success, timeout, transport error) is
        metered by :meth:`_observe` and every physical issue is counted against
        the 5 r/s cap, so a slow/failing attempt's latency + rate are never
        invisible regardless of verb.
        """
        url = self.base_url + endpoint
        _guard_url(url)
        data = json.dumps(body).encode("utf-8") if body is not None else b""
        started = self.clock()
        # Real-time assurance: count this request at its ISSUE instant and shout if
        # the trailing-1s rate ever crosses the 5 r/s cap (CLAUDE.md §2 Rule 6).
        # With every path now bucket-gated (burst=1) this WARNING must never fire
        # — its presence on /logs is the breach alarm (DEC-DHAN-RATE-REALTIME-GUARANTEE-001).
        rps_1s = self.meter.record_issue(started)
        if rps_1s > DATA_RATE_LIMIT_RPS:
            _LOG.warning(
                "rate.breach", rps_1s=rps_1s, cap_rps=DATA_RATE_LIMIT_RPS, endpoint=endpoint
            )
        try:
            status, raw = self.transport(
                url, body=data, headers=self._headers(), timeout=self.timeout, method=method
            )
        except TimeoutError as exc:
            self._observe(endpoint, None, started)
            raise DhanTimeoutError(f"timeout {method}ing {endpoint!r}") from exc
        except OSError as exc:
            self._observe(endpoint, None, started)
            raise DhanTransportError(f"transport error {method}ing {endpoint!r}: {exc}") from exc
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
    url: str,
    *,
    body: bytes,
    headers: Mapping[str, str],
    timeout: float,
    method: str = "POST",
) -> tuple[int, bytes]:
    """Real :mod:`urllib` transport (production wiring for :class:`DhanClient`).

    Sends *method* (POST by default; ``"GET"`` for the headers-only
    ``RenewToken`` path — DEC-DHAN-RENEWTOKEN-GET-001). Returns ``(status,
    body)`` for 2xx **and** 4xx/5xx (``HTTPError`` carries a status + body), so
    :meth:`DhanClient._send_once` maps non-2xx uniformly.
    """
    import urllib.error
    import urllib.request

    request = urllib.request.Request(  # noqa: S310 — https + host allow-listed by _guard_url
        url, data=body, headers=dict(headers), method=method
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
