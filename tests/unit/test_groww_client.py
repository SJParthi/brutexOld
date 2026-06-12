"""Tests for `brutex.adapters.groww.client` — GET logic via fake transport.

100% line+branch of the non-I/O surface: SSRF guard, header construction,
response parsing (200 success / FAILURE status / HTTP errors / non-JSON),
payload extraction (payload key present/absent), automatic auth-refresh on
GrowwAuthError, retry-limit exhaustion, and the three public endpoint
methods (get_candles, get_expiries, get_contracts).

The real `urllib_transport` is `# pragma: no cover` (network shim).
"""

# pyright: reportPrivateUsage=none
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from brutex.adapters.groww.auth import GrowwAuth
from brutex.adapters.groww.client import (
    GrowwClient,
    _build_request_headers,
    _parse_response,
    _ssrf_guard,
)
from brutex.adapters.groww.errors import (
    GrowwAdapterError,
    GrowwApiFailureError,
    GrowwAuthError,
    GrowwBadRequestError,
    GrowwRateLimitError,
    GrowwServerError,
    GrowwTransportError,
)


# ── fakes ─────────────────────────────────────────────────────────────────

def _fixed_transport(
    status: int, body: bytes
) -> Any:
    """Return a transport callable that always yields the given (status, body)."""
    def transport(url: str, headers: dict[str, str], timeout: int) -> tuple[int, bytes]:
        return status, body

    return transport


def _recording_transport(responses: list[tuple[int, bytes]]) -> Any:
    """Return a transport that yields responses in order (asserts not exceeded)."""
    calls: list[dict[str, object]] = []

    def transport(url: str, headers: dict[str, str], timeout: int) -> tuple[int, bytes]:
        idx = len(calls)
        calls.append({"url": url, "headers": headers})
        return responses[idx]

    transport.calls = calls  # type: ignore[attr-defined]
    return transport


def _make_auth(tmp_path: Path, token: str = "tok") -> GrowwAuth:
    token_path = tmp_path / "token.json"
    token_path.write_text(json.dumps({"access_token": token}))
    return GrowwAuth(
        api_key="apikey",
        totp_generator=lambda: "123456",
        token_path=token_path,
        transport=lambda url, h, b: (200, json.dumps({"token": "refreshed"}).encode()),
    )


def _ok_body(candles: list[Any] | None = None) -> bytes:
    payload: dict[str, Any] = {"candles": candles or []}
    return json.dumps({"payload": payload}).encode()


# ── unit tests ────────────────────────────────────────────────────────────

class TestSsrfGuard:
    def test_api_groww_in_allowed(self) -> None:
        _ssrf_guard("https://api.groww.in/v1/historical/candles?foo=bar")

    def test_disallowed_host_raises(self) -> None:
        with pytest.raises(GrowwTransportError, match="SSRF guard"):
            _ssrf_guard("https://evil.example.com/steal")

    def test_localhost_rejected(self) -> None:
        with pytest.raises(GrowwTransportError):
            _ssrf_guard("http://localhost/internal")

    def test_empty_host_rejected(self) -> None:
        with pytest.raises(GrowwTransportError):
            _ssrf_guard("https:///no-host")


class TestBuildRequestHeaders:
    def test_authorization_header(self) -> None:
        h = _build_request_headers("mytoken")
        assert h["Authorization"] == "Bearer mytoken"

    def test_content_type_is_json(self) -> None:
        h = _build_request_headers("t")
        assert h["Content-Type"] == "application/json"

    def test_request_id_is_uuid_format(self) -> None:
        import re
        h = _build_request_headers("t")
        uuid_re = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
        assert re.match(uuid_re, h["x-request-id"])

    def test_each_call_gets_unique_request_id(self) -> None:
        ids = {_build_request_headers("t")["x-request-id"] for _ in range(50)}
        assert len(ids) == 50


class TestParseResponse:
    def test_success_with_payload_key(self) -> None:
        body = json.dumps({"payload": {"candles": []}}).encode()
        result = _parse_response(200, body)
        assert result == {"candles": []}

    def test_success_without_payload_key(self) -> None:
        body = json.dumps({"candles": [], "other": 1}).encode()
        result = _parse_response(200, body)
        assert result == {"candles": [], "other": 1}

    def test_failure_status_raises_api_failure(self) -> None:
        body = json.dumps({
            "status": "FAILURE",
            "error": {"code": "INVALID_SYMBOL", "message": "not found"},
        }).encode()
        with pytest.raises(GrowwApiFailureError) as exc_info:
            _parse_response(200, body)
        assert exc_info.value.code == "INVALID_SYMBOL"

    def test_failure_with_unknown_code(self) -> None:
        body = json.dumps({
            "status": "FAILURE",
            "error": {},
        }).encode()
        exc = pytest.raises(GrowwApiFailureError, _parse_response, 200, body)
        assert exc.value.code == "UNKNOWN"

    def test_http_400_raises_bad_request(self) -> None:
        body = json.dumps({"message": "bad"}).encode()
        with pytest.raises(GrowwBadRequestError):
            _parse_response(400, body)

    def test_http_429_raises_rate_limit(self) -> None:
        with pytest.raises(GrowwRateLimitError):
            _parse_response(429, b"{}")

    def test_http_500_raises_server_error(self) -> None:
        with pytest.raises(GrowwServerError):
            _parse_response(500, b"{}")

    def test_non_json_response_raises(self) -> None:
        with pytest.raises(GrowwAdapterError):
            _parse_response(200, b"not-json")

    def test_non_json_with_error_status_raises(self) -> None:
        with pytest.raises(GrowwAdapterError):
            _parse_response(500, b"Internal Server Error")

    def test_payload_list_wrapped_in_data(self) -> None:
        body = json.dumps({"payload": [1, 2, 3]}).encode()
        result = _parse_response(200, body)
        assert result == {"data": [1, 2, 3]}


class TestGetCandlesHappyPath:
    def test_returns_candles_list(self, tmp_path: Path) -> None:
        candles = [["2024-06-03T09:15:00", 22000.0, 22100.0, 21900.0, 22050.0, 1500]]
        transport = _fixed_transport(200, _ok_body(candles))
        client = GrowwClient(auth=_make_auth(tmp_path), transport=transport)
        result = client.get_candles(
            exchange="NSE",
            segment="CASH",
            groww_symbol="NSE-NIFTY",
            start_time="2024-06-03 09:15:00",
            end_time="2024-06-03 15:30:00",
            candle_interval="1minute",
        )
        assert result["candles"] == candles

    def test_url_contains_groww_symbol(self, tmp_path: Path) -> None:
        calls: list[str] = []

        def recording(url: str, headers: dict[str, str], timeout: int) -> tuple[int, bytes]:
            calls.append(url)
            return 200, _ok_body()

        client = GrowwClient(auth=_make_auth(tmp_path), transport=recording)
        client.get_candles(
            exchange="NSE", segment="CASH", groww_symbol="NSE-BANKNIFTY",
            start_time="2024-01-01 09:15:00", end_time="2024-01-01 15:30:00",
            candle_interval="1minute",
        )
        assert "NSE-BANKNIFTY" in calls[0]


class TestAuthRefresh:
    def test_auth_error_triggers_token_refresh(self, tmp_path: Path) -> None:
        call_count = 0
        refreshed = False

        def transport(url: str, headers: dict[str, str], timeout: int) -> tuple[int, bytes]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return 401, b'{"error": "expired"}'
            return 200, _ok_body()

        def refreshing_transport(
            url: str, headers: dict[str, str], body: dict[str, Any]
        ) -> tuple[int, bytes]:
            nonlocal refreshed
            refreshed = True
            return 200, json.dumps({"token": "fresh-tok"}).encode()

        token_path = tmp_path / "token.json"
        token_path.write_text(json.dumps({"access_token": "expired-tok"}))
        auth = GrowwAuth(
            api_key="key", totp_generator=lambda: "999999",
            token_path=token_path, transport=refreshing_transport,
        )
        client = GrowwClient(auth=auth, transport=transport)
        client.get_candles(
            exchange="NSE", segment="CASH", groww_symbol="NSE-NIFTY",
            start_time="2024-06-03 09:15:00", end_time="2024-06-03 15:30:00",
            candle_interval="1minute",
        )
        assert refreshed
        assert call_count == 2

    def test_auth_error_after_max_retries_propagates(self, tmp_path: Path) -> None:
        def always_401(url: str, headers: dict[str, str], timeout: int) -> tuple[int, bytes]:
            return 401, b'{"error": "expired"}'

        auth = _make_auth(tmp_path)
        client = GrowwClient(auth=auth, transport=always_401)
        with pytest.raises(GrowwAuthError):
            client.get_candles(
                exchange="NSE", segment="CASH", groww_symbol="NSE-NIFTY",
                start_time="2024-06-03 09:15:00", end_time="2024-06-03 15:30:00",
                candle_interval="1minute",
            )


class TestGetExpiries:
    def test_returns_expiry_list(self, tmp_path: Path) -> None:
        body = json.dumps({"payload": {"expiries": ["2021-01-28", "2021-01-21"]}}).encode()
        transport = _fixed_transport(200, body)
        client = GrowwClient(auth=_make_auth(tmp_path), transport=transport)
        result = client.get_expiries("NSE", "NIFTY", year=2021, month=1)
        assert result == ["2021-01-28", "2021-01-21"]

    def test_empty_expiries_returns_empty_list(self, tmp_path: Path) -> None:
        body = json.dumps({"payload": {"expiries": []}}).encode()
        transport = _fixed_transport(200, body)
        client = GrowwClient(auth=_make_auth(tmp_path), transport=transport)
        assert client.get_expiries("NSE", "NIFTY") == []

    def test_year_month_params_in_url(self, tmp_path: Path) -> None:
        calls: list[str] = []

        def recording(url: str, headers: dict[str, str], timeout: int) -> tuple[int, bytes]:
            calls.append(url)
            return 200, json.dumps({"payload": {"expiries": []}}).encode()

        client = GrowwClient(auth=_make_auth(tmp_path), transport=recording)
        client.get_expiries("NSE", "NIFTY", year=2021, month=1)
        assert "year=2021" in calls[0]
        assert "month=1" in calls[0]

    def test_no_year_month_omitted_from_url(self, tmp_path: Path) -> None:
        calls: list[str] = []

        def recording(url: str, headers: dict[str, str], timeout: int) -> tuple[int, bytes]:
            calls.append(url)
            return 200, json.dumps({"payload": {"expiries": []}}).encode()

        client = GrowwClient(auth=_make_auth(tmp_path), transport=recording)
        client.get_expiries("NSE", "NIFTY")
        assert "year" not in calls[0]
        assert "month" not in calls[0]


class TestGetContracts:
    def test_returns_contract_list(self, tmp_path: Path) -> None:
        body = json.dumps({
            "payload": {"contracts": ["NSE-NIFTY-23Jan21-13000-CE", "NSE-NIFTY-23Jan21-13000-PE"]}
        }).encode()
        transport = _fixed_transport(200, body)
        client = GrowwClient(auth=_make_auth(tmp_path), transport=transport)
        result = client.get_contracts("NSE", "NIFTY", "2021-01-28")
        assert "NSE-NIFTY-23Jan21-13000-CE" in result
        assert "NSE-NIFTY-23Jan21-13000-PE" in result

    def test_empty_contracts_returns_empty_list(self, tmp_path: Path) -> None:
        body = json.dumps({"payload": {"contracts": []}}).encode()
        transport = _fixed_transport(200, body)
        client = GrowwClient(auth=_make_auth(tmp_path), transport=transport)
        assert client.get_contracts("NSE", "NIFTY", "2021-01-28") == []
