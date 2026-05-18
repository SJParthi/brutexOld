"""Dhan v2 historical-data HTTP client.

The ``DhanClient`` is the integration point that wires together the
foundation modules (``constants``, ``body``, ``parser``, ``timeconv``,
``auth``) with the orchestrator's rate-limiter / quota tracker / error
classifier.

Design contract (locked by 4-agent pre-impl review on 2026-05-18):

1. **Never raises** — every call returns a :class:`FetchResult`. The
   orchestrator FSM consumes the :class:`Outcome` enum value to decide
   retry vs quarantine vs FATAL.
2. **Sync execution path** — :class:`brutex.orchestrator.rate_limiter.AdaptiveTokenBucket`
   is sync (``threading.Lock`` + ``time.sleep``). Async callers wrap the
   adapter call in :func:`asyncio.to_thread`. This avoids the
   sync-in-async-event-loop trap entirely.
3. **No internal retry** — orchestrator owns retry policy. The adapter
   issues exactly ONE HTTP call per public method invocation and ONE
   ``bucket.acquire()`` token consumption.
4. **Token redaction discipline** — :class:`DhanToken` ``__repr__``
   redacts; raw token reaches only the HTTP-header construction site
   and is never logged. :class:`DhanError` subclasses follow the safe-
   message discipline from DEC-P0-031.
5. **TLS posture** — ``trust_env=False`` so a poisoned ``REQUESTS_CA_BUNDLE``
   or ``HTTPS_PROXY`` cannot MITM us. ``verify=True``, ``follow_redirects=False``.

Each public method (:meth:`fetch_intraday` / :meth:`fetch_historical_daily`)
performs:

  bucket.acquire()         # blocks until token available, raises DailyQuotaExceeded if quota hit
  client.post(url, json)   # one HTTP call
  classify_response()      # state machine that picks the Outcome
  parse_response()         # only on 200 + valid JSON → bars
  return FetchResult(...)
"""

from __future__ import annotations

import json
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any, Final

import httpx

from brutex.adapters.dhan.auth import DhanToken
from brutex.adapters.dhan.body import (
    HistoricalDailyRequest,
    IntradayChunkRequest,
    build_historical_body,
    build_intraday_body,
)
from brutex.adapters.dhan.parser import parse_response
from brutex.adapters.dhan.ssrf_guard import assert_host_is_public
from brutex.adapters.dhan.types import FetchResult
from brutex.orchestrator.dhan_errors import (
    DHAN_CODE_TO_EXCEPTION,
    DHAN_ERROR_CLASSIFICATION,
    HTTP_5XX_FALLBACK_CODE,
    DhanErrorDH908,
    DhanErrorDH909,
)
from brutex.orchestrator.rate_limiter import AdaptiveTokenBucket
from brutex.orchestrator.state import Outcome

#: Base URL for the Dhan v2 historical-data API. Pinned at module level
#: so tests can monkeypatch via the ``base_url`` constructor kwarg.
DHAN_API_BASE_URL: Final[str] = "https://api.dhan.co"

#: Path for the 1-minute (up to 60-min) intraday endpoint.
INTRADAY_PATH: Final[str] = "/v2/charts/intraday"

#: Path for the daily historical endpoint.
HISTORICAL_PATH: Final[str] = "/v2/charts/historical"

#: Default connect + total request timeout. 30s total / 10s connect is
#: generous enough for cold-cache Dhan servers but short enough that a
#: hung connection fails fast.
_DEFAULT_TIMEOUT: Final[httpx.Timeout] = httpx.Timeout(30.0, connect=10.0)

#: Connection pool limits. 4 max concurrent (intentionally more than
#: 3 r/s × 1s = 3 to absorb burst, but capped low to avoid Dhan-side
#: rate-limit accidents on parallel workers).
_DEFAULT_LIMITS: Final[httpx.Limits] = httpx.Limits(max_keepalive_connections=2, max_connections=4)

#: HTTP status codes the classify_response state machine branches on.
_HTTP_OK: Final[int] = 200
_HTTP_SERVER_ERROR_FLOOR: Final[int] = 500


class DhanClient:
    """Sync HTTP client for Dhan v2 historical-data endpoints.

    Constructed once per worker; reused across the lifetime of the
    orchestrator. The token is read once at construction time via
    :func:`brutex.adapters.dhan.auth.load_dhan_token` (or injected
    directly for tests).

    Parameters
    ----------
    token
        The wrapped Dhan access token (see
        :class:`brutex.adapters.dhan.auth.DhanToken`).
    bucket
        The adaptive token bucket gating outbound calls (and the daily
        quota tracker via its ``quota_tracker`` field).
    http_client
        Optional pre-built :class:`httpx.Client` for tests. Production
        callers should let the constructor build one with hardened
        defaults (``trust_env=False``, ``verify=True``,
        ``follow_redirects=False``).
    base_url
        Override the API base URL (test-only).
    """

    __slots__ = ("_base_url", "_bucket", "_http", "_owns_http", "_token")

    def __init__(
        self,
        token: DhanToken,
        bucket: AdaptiveTokenBucket,
        *,
        http_client: httpx.Client | None = None,
        base_url: str = DHAN_API_BASE_URL,
        ssrf_check: bool = True,
    ) -> None:
        self._token = token
        self._bucket = bucket
        self._base_url = base_url
        if http_client is None:
            # Pre-resolve the base URL hostname and refuse non-global IPs
            # (DEC-P0-036 / SSRF guard). Tests pass `ssrf_check=False`
            # when injecting an httpx.MockTransport against a fake host.
            if ssrf_check:
                assert_host_is_public(base_url)
            self._http = httpx.Client(
                base_url=base_url,
                trust_env=False,
                verify=True,
                follow_redirects=False,
                timeout=_DEFAULT_TIMEOUT,
                limits=_DEFAULT_LIMITS,
            )
            self._owns_http = True
        else:
            # Caller-supplied http_client (tests). SSRF check skipped —
            # the mock transport never makes a real network call.
            self._http = http_client
            self._owns_http = False

    def __repr__(self) -> str:
        return f"DhanClient(token=*** base_url={self._base_url!r})"

    def close(self) -> None:
        """Close the underlying httpx.Client (only if we own it)."""
        if self._owns_http:
            self._http.close()

    def fetch_intraday(self, req: IntradayChunkRequest) -> FetchResult:
        """Fetch a single intraday OHLC chunk."""
        body = build_intraday_body(req)
        return self._fetch_one(req, INTRADAY_PATH, body)

    def fetch_historical_daily(self, req: HistoricalDailyRequest) -> FetchResult:
        """Fetch a daily OHLC range."""
        body = build_historical_body(req)
        return self._fetch_one(req, HISTORICAL_PATH, body)

    # ── Internal ──────────────────────────────────────────────────────

    def _fetch_one(
        self,
        req: IntradayChunkRequest | HistoricalDailyRequest,
        path: str,
        body: dict[str, Any],
    ) -> FetchResult:
        """One token, one HTTP call, one FetchResult."""
        # 1. Consume one token. DailyQuotaExceeded propagates — the
        #    orchestrator wraps this call in a quota-aware worker.
        self._bucket.acquire()

        # 2. One HTTP call. Wrap every transport-level exception so
        #    callers only see FetchResult.
        try:
            response = self._http.post(
                path,
                json=body,
                headers=self._build_headers(),
            )
        except httpx.TimeoutException:
            return self._classify_timeout(req)
        except httpx.NetworkError:
            return self._classify_network_error(req)

        # 3. Translate response → FetchResult.
        return self._classify_response(req, response)

    def _build_headers(self) -> dict[str, str]:
        """Construct request headers. Token reveal happens HERE ONLY."""
        return {
            "access-token": self._token.reveal(),
            "Content-Type": "application/json",
        }

    def _classify_response(
        self,
        req: IntradayChunkRequest | HistoricalDailyRequest,
        response: httpx.Response,
    ) -> FetchResult:
        """Map an httpx.Response to a FetchResult per the 22-code table.

        The state machine has 4 top-level branches:

        * 200 + parsable JSON → either OK_WITH_DATA / OK_EMPTY (no
          error field) OR error-class outcome (Dhan returns 200 with
          embedded errorCode for some classes).
        * 200 + unparsable JSON → DH-908 → GENERIC_API_ERROR.
        * 4xx → classify by error_code in body, fall back to DH-908.
        * 5xx → DH-908.
        """
        status = response.status_code

        if status == _HTTP_OK:
            return self._classify_200(req, response)

        # Non-200: try to extract a Dhan error code from the body.
        code = _extract_error_code(response)
        if status >= _HTTP_SERVER_ERROR_FLOOR:
            code = HTTP_5XX_FALLBACK_CODE
        return self._build_error_result(req, code)

    def _classify_200(
        self,
        req: IntradayChunkRequest | HistoricalDailyRequest,
        response: httpx.Response,
    ) -> FetchResult:
        """Sub-state-machine for HTTP 200 responses.

        Dhan occasionally returns 200 with an embedded errorCode (e.g.,
        ``DH-901`` for stale token on what they consider a recoverable
        path). We honour the embedded code if present; otherwise treat
        as success.
        """
        try:
            payload = response.json()
        except json.JSONDecodeError:
            return self._build_error_result(req, HTTP_5XX_FALLBACK_CODE)
        if not isinstance(payload, dict):
            return self._build_error_result(req, HTTP_5XX_FALLBACK_CODE)

        embedded_code = payload.get("errorCode")
        if isinstance(embedded_code, str) and embedded_code:
            return self._build_error_result(req, embedded_code)

        try:
            bars = parse_response(payload)
        except DhanErrorDH908 as exc:
            # parser raised → length-mismatch / missing-key / non-list.
            outcome = DHAN_ERROR_CLASSIFICATION[exc.dhan_error_code]
            return FetchResult(request=req, outcome=outcome, bars=(), error=exc)

        outcome = Outcome.OK_WITH_DATA if bars else Outcome.OK_EMPTY
        return FetchResult(request=req, outcome=outcome, bars=bars, error=None)

    def _build_error_result(
        self,
        req: IntradayChunkRequest | HistoricalDailyRequest,
        code: str,
    ) -> FetchResult:
        """Build a FetchResult from a Dhan error code.

        Unknown codes fall back to DH-908 → GENERIC_API_ERROR (per
        DEC-P0-031 Agent 3 §K — unknown does NOT halt; that path is
        the orchestrator's job via ``UnknownDhanCodeError`` if needed).
        """
        if code not in DHAN_CODE_TO_EXCEPTION:
            code = HTTP_5XX_FALLBACK_CODE
        exc_class = DHAN_CODE_TO_EXCEPTION[code]
        outcome = DHAN_ERROR_CLASSIFICATION[code]
        return FetchResult(
            request=req,
            outcome=outcome,
            bars=(),
            error=exc_class(detailed_message=f"Dhan responded with {code}"),
        )

    def _classify_timeout(self, req: IntradayChunkRequest | HistoricalDailyRequest) -> FetchResult:
        """Map httpx.TimeoutException to Outcome.TIMEOUT + DhanErrorDH909."""
        return FetchResult(
            request=req,
            outcome=Outcome.TIMEOUT,
            bars=(),
            error=DhanErrorDH909(detailed_message="httpx.TimeoutException"),
        )

    def _classify_network_error(
        self, req: IntradayChunkRequest | HistoricalDailyRequest
    ) -> FetchResult:
        """Map httpx.NetworkError (DNS / connection refused / TLS) to TIMEOUT."""
        return FetchResult(
            request=req,
            outcome=Outcome.TIMEOUT,
            bars=(),
            error=DhanErrorDH909(detailed_message="httpx.NetworkError"),
        )


def _extract_error_code(response: httpx.Response) -> str:
    """Best-effort extraction of Dhan's ``errorCode`` field.

    Returns an empty string if the body isn't JSON or lacks the field.
    The caller is expected to fall back to DH-908 when this returns
    an empty string AND the status is 4xx (5xx already short-circuits).
    """
    try:
        payload = response.json()
    except json.JSONDecodeError:
        return ""
    if not isinstance(payload, dict):
        return ""
    code = payload.get("errorCode", "")
    return code if isinstance(code, str) else ""


@contextmanager
def dhan_client_context(
    token: DhanToken,
    bucket: AdaptiveTokenBucket,
) -> Generator[DhanClient, None, None]:
    """Context manager that closes the underlying httpx.Client on exit.

    Production code should prefer this over manual ``close()`` so
    connection pool resources are released deterministically.
    """
    client = DhanClient(token=token, bucket=bucket)
    try:
        yield client
    finally:
        client.close()


__all__ = [
    "DHAN_API_BASE_URL",
    "HISTORICAL_PATH",
    "INTRADAY_PATH",
    "DhanClient",
    "dhan_client_context",
]
