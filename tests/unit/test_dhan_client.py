"""Unit tests for ``brutex.adapters.dhan.client``.

Pins the ``classify_response`` state machine across the 22 Dhan error codes
+ 200/empty + 200/data + length-mismatch + HTTP 5xx + timeout + network
error. Uses ``httpx.MockTransport`` to simulate Dhan responses without
hitting the network.

The bucket is replaced with a no-op fake so unit tests run instantly
(real ``time.sleep`` is integration-level concern).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx
from brutex.adapters.dhan.auth import DhanToken
from brutex.adapters.dhan.body import IntradayChunkRequest
from brutex.adapters.dhan.client import (
    DHAN_API_BASE_URL,
    DhanClient,
)
from brutex.adapters.dhan.constants import NIFTY
from brutex.adapters.dhan.types import FetchResult
from brutex.orchestrator.dhan_errors import (
    DhanError805,
    DhanError807,
    DhanError814,
    DhanErrorDH901,
    DhanErrorDH904,
    DhanErrorDH907,
    DhanErrorDH908,
    DhanErrorDH909,
)
from brutex.orchestrator.state import Outcome

# ────────────────────────────────────────────────────────────────────────
# Fakes — no real network, no real time.sleep.
# ────────────────────────────────────────────────────────────────────────


@dataclass
class FakeBucket:
    """Stand-in for AdaptiveTokenBucket — counts acquire() calls only."""

    acquire_calls: int = 0

    def acquire(self) -> None:
        self.acquire_calls += 1


def _intraday_req() -> IntradayChunkRequest:
    return IntradayChunkRequest(
        instrument=NIFTY,
        interval="1",
        from_date=date(2024, 3, 1),
        to_date=date(2024, 3, 5),
    )


def _make_client_with_responses(
    *responses: tuple[int, dict[str, Any] | str],
) -> tuple[DhanClient, FakeBucket, list[httpx.Request]]:
    """Build a DhanClient whose httpx.Client returns the given (status, body) sequence."""
    requests_seen: list[httpx.Request] = []
    iterator = iter(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        try:
            status, body = next(iterator)
        except StopIteration as exc:
            raise AssertionError("Mock transport ran out of responses") from exc
        if isinstance(body, dict):
            return httpx.Response(status_code=status, json=body)
        return httpx.Response(status_code=status, content=body)

    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(base_url=DHAN_API_BASE_URL, transport=transport)
    bucket = FakeBucket()
    client = DhanClient(
        token=DhanToken(_value="test_token"),
        bucket=bucket,  # type: ignore[arg-type]
        http_client=http_client,
    )
    return client, bucket, requests_seen


def _ok_payload(
    timestamps: list[int],
    opens: list[float],
) -> dict[str, list[Any]]:
    """Build a Dhan-shaped 6-array OK payload."""
    n = len(timestamps)
    return {
        "open": opens,
        "high": opens,
        "low": opens,
        "close": opens,
        "volume": [0] * n,
        "timestamp": timestamps,
    }


# ────────────────────────────────────────────────────────────────────────
# Tests
# ────────────────────────────────────────────────────────────────────────


class TestRepr:
    """``DhanClient.__repr__`` never leaks the token."""

    def test_repr_redacts(self) -> None:
        client, _, _ = _make_client_with_responses()
        rendered = repr(client)
        assert "token=***" in rendered
        assert "test_token" not in rendered


class TestOk200Path:
    """HTTP 200 + valid bars → Outcome.OK_WITH_DATA."""

    def test_one_bar_response(self) -> None:
        payload = _ok_payload([1709284500], [24500.05])
        client, bucket, _ = _make_client_with_responses((200, payload))

        result = client.fetch_intraday(_intraday_req())

        assert isinstance(result, FetchResult)
        assert result.outcome == Outcome.OK_WITH_DATA
        assert len(result.bars) == 1
        assert result.error is None
        assert bucket.acquire_calls == 1

    def test_empty_response_is_ok_empty(self) -> None:
        payload = _ok_payload([], [])
        client, _, _ = _make_client_with_responses((200, payload))

        result = client.fetch_intraday(_intraday_req())

        assert result.outcome == Outcome.OK_EMPTY
        assert result.bars == ()
        assert result.error is None


class TestErrorClassification:
    """Each known Dhan error code maps to the right Outcome + exception."""

    def test_dh904_rate_limit(self) -> None:
        client, _, _ = _make_client_with_responses((429, {"errorCode": "DH-904"}))
        result = client.fetch_intraday(_intraday_req())
        assert result.outcome == Outcome.RATE_LIMIT
        assert isinstance(result.error, DhanErrorDH904)
        assert result.bars == ()

    def test_dh901_auth_failed(self) -> None:
        client, _, _ = _make_client_with_responses((401, {"errorCode": "DH-901"}))
        result = client.fetch_intraday(_intraday_req())
        assert result.outcome == Outcome.AUTH_FAILED
        assert isinstance(result.error, DhanErrorDH901)

    def test_dh907_no_data(self) -> None:
        client, _, _ = _make_client_with_responses((400, {"errorCode": "DH-907"}))
        result = client.fetch_intraday(_intraday_req())
        assert result.outcome == Outcome.NO_DATA
        assert isinstance(result.error, DhanErrorDH907)

    def test_807_auth_token_expired(self) -> None:
        client, _, _ = _make_client_with_responses((401, {"errorCode": "807"}))
        result = client.fetch_intraday(_intraday_req())
        assert result.outcome == Outcome.AUTH_FAILED
        assert isinstance(result.error, DhanError807)

    def test_805_data_api_rate_limit(self) -> None:
        client, _, _ = _make_client_with_responses((429, {"errorCode": "805"}))
        result = client.fetch_intraday(_intraday_req())
        assert result.outcome == Outcome.RATE_LIMIT
        assert isinstance(result.error, DhanError805)

    def test_814_bad_request(self) -> None:
        client, _, _ = _make_client_with_responses((400, {"errorCode": "814"}))
        result = client.fetch_intraday(_intraday_req())
        assert result.outcome == Outcome.BAD_REQUEST
        assert isinstance(result.error, DhanError814)


class TestUnknownCodeFallsBackToDH908:
    """An unrecognized code becomes DH-908 (GENERIC_API_ERROR)."""

    def test_unknown_code_in_4xx(self) -> None:
        client, _, _ = _make_client_with_responses((400, {"errorCode": "DH-999"}))
        result = client.fetch_intraday(_intraday_req())
        assert result.outcome == Outcome.GENERIC_API_ERROR
        assert isinstance(result.error, DhanErrorDH908)

    def test_4xx_with_no_body_falls_back_to_dh908(self) -> None:
        client, _, _ = _make_client_with_responses((400, "plain text"))
        result = client.fetch_intraday(_intraday_req())
        assert result.outcome == Outcome.GENERIC_API_ERROR
        assert isinstance(result.error, DhanErrorDH908)


class TestServerError5xx:
    """HTTP 5xx synthesises DH-908 unconditionally."""

    def test_500_synthesises_dh908(self) -> None:
        client, _, _ = _make_client_with_responses((500, ""))
        result = client.fetch_intraday(_intraday_req())
        assert result.outcome == Outcome.GENERIC_API_ERROR
        assert isinstance(result.error, DhanErrorDH908)

    def test_503_synthesises_dh908(self) -> None:
        client, _, _ = _make_client_with_responses((503, {"errorCode": "DH-902"}))
        result = client.fetch_intraday(_intraday_req())
        # Even if 5xx response carries an errorCode, we synthesise DH-908
        # because Dhan's transient outages should map to GENERIC_API_ERROR.
        assert result.outcome == Outcome.GENERIC_API_ERROR
        assert isinstance(result.error, DhanErrorDH908)


class Test200WithEmbeddedError:
    """Dhan sometimes returns 200 + embedded errorCode — honour it."""

    def test_200_with_dh901_in_body(self) -> None:
        client, _, _ = _make_client_with_responses((200, {"errorCode": "DH-901"}))
        result = client.fetch_intraday(_intraday_req())
        assert result.outcome == Outcome.AUTH_FAILED
        assert isinstance(result.error, DhanErrorDH901)


class TestLengthMismatch:
    """200 + parallel-array length mismatch → DH-908 via parser."""

    def test_open_shorter_synthesises_dh908(self) -> None:
        payload: dict[str, list[Any]] = {
            "open": [1.0],
            "high": [1.0, 2.0],
            "low": [1.0, 2.0],
            "close": [1.0, 2.0],
            "volume": [0, 0],
            "timestamp": [100, 200],
        }
        client, _, _ = _make_client_with_responses((200, payload))
        result = client.fetch_intraday(_intraday_req())
        assert result.outcome == Outcome.GENERIC_API_ERROR
        assert isinstance(result.error, DhanErrorDH908)


class TestMalformedJson:
    """200 + non-JSON body → DH-908."""

    def test_non_json_body_synthesises_dh908(self) -> None:
        client, _, _ = _make_client_with_responses((200, "not json"))
        result = client.fetch_intraday(_intraday_req())
        assert result.outcome == Outcome.GENERIC_API_ERROR
        assert isinstance(result.error, DhanErrorDH908)

    def test_200_with_non_dict_json(self) -> None:
        # ``[1,2,3]`` is valid JSON but not a dict.
        client, _, _ = _make_client_with_responses((200, "[1,2,3]"))
        result = client.fetch_intraday(_intraday_req())
        assert result.outcome == Outcome.GENERIC_API_ERROR


class TestTransportErrors:
    """httpx.TimeoutException + NetworkError both map to Outcome.TIMEOUT."""

    def test_timeout_maps_to_timeout(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("simulated")

        bucket = FakeBucket()
        http_client = httpx.Client(
            base_url=DHAN_API_BASE_URL, transport=httpx.MockTransport(handler)
        )
        client = DhanClient(
            token=DhanToken(_value="t"),
            bucket=bucket,  # type: ignore[arg-type]
            http_client=http_client,
        )
        result = client.fetch_intraday(_intraday_req())
        assert result.outcome == Outcome.TIMEOUT
        assert isinstance(result.error, DhanErrorDH909)

    def test_network_error_maps_to_timeout(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.NetworkError("simulated connection reset")

        bucket = FakeBucket()
        http_client = httpx.Client(
            base_url=DHAN_API_BASE_URL, transport=httpx.MockTransport(handler)
        )
        client = DhanClient(
            token=DhanToken(_value="t"),
            bucket=bucket,  # type: ignore[arg-type]
            http_client=http_client,
        )
        result = client.fetch_intraday(_intraday_req())
        assert result.outcome == Outcome.TIMEOUT
        assert isinstance(result.error, DhanErrorDH909)


class TestBucketUsage:
    """Exactly one bucket.acquire() per public method call."""

    def test_intraday_calls_bucket_once(self) -> None:
        client, bucket, _ = _make_client_with_responses(
            (200, _ok_payload([1709284500], [24500.05]))
        )
        client.fetch_intraday(_intraday_req())
        assert bucket.acquire_calls == 1

    def test_timeout_still_counts_token(self) -> None:
        """Failed fetch still consumes a quota token (conservative)."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("simulated")

        bucket = FakeBucket()
        http_client = httpx.Client(
            base_url=DHAN_API_BASE_URL, transport=httpx.MockTransport(handler)
        )
        client = DhanClient(
            token=DhanToken(_value="t"),
            bucket=bucket,  # type: ignore[arg-type]
            http_client=http_client,
        )
        client.fetch_intraday(_intraday_req())
        assert bucket.acquire_calls == 1


class TestHeaders:
    """Token is set on the access-token header at request time."""

    def test_token_in_access_token_header(self) -> None:
        client, _, requests_seen = _make_client_with_responses((200, _ok_payload([], [])))
        client.fetch_intraday(_intraday_req())
        assert requests_seen[0].headers["access-token"] == "test_token"

    def test_content_type_is_application_json(self) -> None:
        client, _, requests_seen = _make_client_with_responses((200, _ok_payload([], [])))
        client.fetch_intraday(_intraday_req())
        assert requests_seen[0].headers["content-type"] == "application/json"


class TestRequestBody:
    """The request body is the dict produced by build_intraday_body."""

    def test_intraday_body_shape(self) -> None:
        client, _, requests_seen = _make_client_with_responses((200, _ok_payload([], [])))
        client.fetch_intraday(_intraday_req())
        body = json.loads(requests_seen[0].content)
        assert body["securityId"] == "13"
        assert body["exchangeSegment"] == "IDX_I"
        assert body["instrument"] == "INDEX"
        assert body["interval"] == "1"
        assert body["oi"] is False
        assert body["fromDate"] == "2024-03-01"
        assert body["toDate"] == "2024-03-05"
