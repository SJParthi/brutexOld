"""Groww REST client — GET historical data from the Groww v1 API.

The client is the ONLY component that performs Groww network I/O. The
**transport is injected** — a ``(url, params, headers, timeout) →
(status, bytes)`` callable — so every branch of the request/response
logic (SSRF guard, header construction, HTTP-status → error mapping,
JSON parsing) is unit-testable with a fake transport (no network).

The real transport (:func:`urllib_transport`) is a thin ``urllib`` shim
and is the only ``# pragma: no cover`` surface.

Rate limiting + logical retry/re-queue stay the orchestrator's concern
(CLAUDE.md §11 COMMON). The client owns ONE narrow concern: a single
REST call with SSRF protection and error mapping.

All Groww backtesting endpoints are **GET** requests with query parameters.
The auth endpoint is a **POST** (handled by :mod:`brutex.adapters.groww.auth`).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlparse

import structlog

from brutex.adapters.groww.auth import GrowwAuth
from brutex.adapters.groww.constants import (
    ALLOWED_HOSTS,
    CANDLES_PATH,
    CONTRACTS_PATH,
    DEFAULT_TIMEOUT_SEC,
    ENV_SMOKE,
    EXPIRIES_PATH,
    GROWW_API_VERSION,
    GROWW_BASE_URL,
    GROWW_CLIENT_ID,
    GROWW_CLIENT_PLATFORM,
    GROWW_CLIENT_PLATFORM_VERSION,
    HEADER_API_VERSION,
    HEADER_AUTHORIZATION,
    HEADER_CLIENT_ID,
    HEADER_CLIENT_PLATFORM,
    HEADER_CLIENT_PLATFORM_VERSION,
    HEADER_CONTENT_TYPE,
    HEADER_REQUEST_ID,
)
from brutex.adapters.groww.errors import (
    GrowwAdapterError,
    GrowwAuthError,
    GrowwTransportError,
    from_http_status,
)

_LOG = structlog.get_logger("brutex.adapters.groww.client")

#: Maximum automatic auth-refresh attempts per call before propagating the error.
_MAX_AUTH_RETRIES: int = 2

#: Type alias for the injected GET transport.
#: Signature: ``(url_with_params, headers, timeout) → (status_code, response_bytes)``
GetTransport = Callable[[str, dict[str, str], int], tuple[int, bytes]]


def urllib_transport(
    url: str,
    headers: dict[str, str],
    timeout: int,
) -> tuple[int, bytes]:  # pragma: no cover — real network, tested via smoke only
    """Real urllib-based GET transport."""
    import urllib.request

    req = urllib.request.Request(url, headers=headers, method="GET")  # noqa: S310
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310  # nosec B310
        return resp.status, resp.read()


def _ssrf_guard(url: str) -> None:
    """Raise ``GrowwTransportError`` if *url* targets a non-allowlisted host."""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if host not in ALLOWED_HOSTS:
        raise GrowwTransportError(
            f"SSRF guard: host {host!r} is not in ALLOWED_HOSTS {ALLOWED_HOSTS}"
        )


def _build_request_headers(token: str) -> dict[str, str]:
    """Construct the standard Groww API request headers for an authenticated GET."""
    return {
        HEADER_AUTHORIZATION: f"Bearer {token}",
        HEADER_CONTENT_TYPE: "application/json",
        HEADER_REQUEST_ID: str(uuid.uuid4()),
        HEADER_CLIENT_ID: GROWW_CLIENT_ID,
        HEADER_CLIENT_PLATFORM: GROWW_CLIENT_PLATFORM,
        HEADER_CLIENT_PLATFORM_VERSION: GROWW_CLIENT_PLATFORM_VERSION,
        HEADER_API_VERSION: GROWW_API_VERSION,
    }


def _parse_response(status: int, raw: bytes) -> dict[str, Any]:
    """Parse the raw bytes → dict, mapping Groww errors to BruteX exceptions.

    Groww returns ``{"status": "FAILURE", "error": {...}}`` for API-level
    errors even on HTTP 200. This function normalises both paths.

    Raises
    ------
    GrowwAdapterError
        On HTTP error status codes or Groww ``FAILURE`` responses.
    """
    try:
        body: dict[str, Any] = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise from_http_status(status, f"non-JSON response: {raw[:200]!r}") from exc

    if status != 200:  # noqa: PLR2004
        msg = str(body) if body else raw.decode(errors="replace")[:200]
        raise from_http_status(status, msg)

    if body.get("status") == "FAILURE":
        error = body.get("error", {})
        code = str(error.get("code", "UNKNOWN"))
        message = str(error.get("message", "no message"))
        from brutex.adapters.groww.errors import GrowwApiFailureError
        raise GrowwApiFailureError(code, message)

    payload = body.get("payload", body)
    return dict(payload) if isinstance(payload, dict) else {"data": payload}


@dataclass
class GrowwClient:
    """Thin authenticated REST client for the Groww v1 backtesting API.

    Parameters
    ----------
    auth:
        :class:`~brutex.adapters.groww.auth.GrowwAuth` instance managing
        token lifecycle.
    transport:
        Injected GET transport callable. Defaults to :func:`urllib_transport`.
    timeout_sec:
        Per-call request timeout in seconds.
    """

    auth: GrowwAuth
    transport: GetTransport = urllib_transport
    timeout_sec: int = DEFAULT_TIMEOUT_SEC

    def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        """Execute an authenticated GET against *path* with *params*.

        Automatically refreshes the access token up to ``_MAX_AUTH_RETRIES``
        times on HTTP 401/403 before propagating the error.
        """
        url_base = f"{GROWW_BASE_URL}/{path}"
        full_url = f"{url_base}?{urlencode(params)}" if params else url_base
        _ssrf_guard(full_url)

        last_exc: GrowwAdapterError | None = None
        token = self.auth.get_token()
        for attempt in range(1 + _MAX_AUTH_RETRIES):
            headers = _build_request_headers(token)
            _LOG.debug(
                "groww.client.get",
                path=path,
                params=params,
                attempt=attempt + 1,
            )
            try:
                status, raw = self.transport(full_url, headers, self.timeout_sec)
                return _parse_response(status, raw)
            except GrowwAuthError as exc:
                last_exc = exc
                if attempt >= _MAX_AUTH_RETRIES:
                    break
                _LOG.warning(
                    "groww.client.auth_refresh",
                    attempt=attempt + 1,
                    reason=str(exc),
                )
                token = self.auth.refresh()
            except GrowwAdapterError:
                raise
        assert last_exc is not None  # noqa: S101  # nosec B101 — guaranteed by loop logic
        raise last_exc

    def get_candles(
        self,
        exchange: str,
        segment: str,
        groww_symbol: str,
        start_time: str,
        end_time: str,
        candle_interval: str,
    ) -> dict[str, Any]:
        """Fetch historical OHLCV candles for one time window.

        Parameters
        ----------
        exchange, segment, groww_symbol:
            Instrument identifiers (e.g. ``"NSE"``, ``"CASH"``, ``"NSE-NIFTY"``).
        start_time, end_time:
            IST datetime strings in ``"YYYY-MM-DD HH:mm:ss"`` format.
        candle_interval:
            One of the ``CANDLE_INTERVAL_*`` constants (e.g. ``"1minute"``).

        Returns
        -------
        dict
            Parsed response payload containing ``"candles"`` array.
        """
        params: dict[str, str] = {
            "exchange": exchange,
            "segment": segment,
            "groww_symbol": groww_symbol,
            "start_time": start_time,
            "end_time": end_time,
            "candle_interval": candle_interval,
        }
        result = self._get(CANDLES_PATH, params)
        _LOG.debug(
            "groww.client.candles_ok",
            groww_symbol=groww_symbol,
            start_time=start_time,
            end_time=end_time,
            count=len(result.get("candles", [])),
        )
        return result

    def get_expiries(
        self,
        exchange: str,
        underlying_symbol: str,
        year: int | None = None,
        month: int | None = None,
    ) -> list[str]:
        """Return expiry date strings for *underlying_symbol*.

        Parameters
        ----------
        exchange:
            ``"NSE"`` or ``"BSE"``.
        underlying_symbol:
            Bare underlying (e.g. ``"NIFTY"``, ``"RELIANCE"``).
        year, month:
            Optional filter; if omitted Groww returns all expiries.

        Returns
        -------
        list[str]
            ISO date strings (``"YYYY-MM-DD"``).
        """
        params: dict[str, str] = {
            "exchange": exchange,
            "underlying_symbol": underlying_symbol,
        }
        if year is not None:
            params["year"] = str(year)
        if month is not None:
            params["month"] = str(month)
        payload = self._get(EXPIRIES_PATH, params)
        raw = payload.get("expiries", [])
        return [str(e) for e in raw]

    def get_contracts(
        self,
        exchange: str,
        underlying_symbol: str,
        expiry_date: str,
    ) -> list[str]:
        """Return all contract groww_symbols for *underlying_symbol* at *expiry_date*.

        Parameters
        ----------
        expiry_date:
            ISO date string (``"YYYY-MM-DD"``).

        Returns
        -------
        list[str]
            Groww symbol strings (e.g. ``"NSE-NIFTY-02Jan25-28500-PE"``).
        """
        params: dict[str, str] = {
            "exchange": exchange,
            "underlying_symbol": underlying_symbol,
            "expiry_date": expiry_date,
        }
        payload = self._get(CONTRACTS_PATH, params)
        raw = payload.get("contracts", [])
        return [str(c) for c in raw]


def is_smoke_enabled() -> bool:
    """Return ``True`` if ``BRUTEX_GROWW_SMOKE=1`` is set in the environment."""
    import os

    return os.environ.get(ENV_SMOKE, "") == "1"


__all__ = [
    "GetTransport",
    "GrowwClient",
    "is_smoke_enabled",
    "urllib_transport",
]
