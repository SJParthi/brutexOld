"""Tests for `brutex.adapters.dhan.client` — POST logic via a fake transport.

100% line+branch of the non-I/O surface: header build, SSRF guard, status
mapping, JSON parsing, timeout/transport error translation. The real
`urllib_transport` is `# pragma: no cover` (network shim).
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest
from brutex.adapters.dhan.client import CallMeter, DhanClient, _guard_url
from brutex.adapters.dhan.constants import HEADER_ACCESS_TOKEN, HEADER_CLIENT_ID
from brutex.adapters.dhan.errors import (
    DhanError,
    DhanRateLimitError,
    DhanTimeoutError,
    DhanTransportError,
)
from structlog.testing import capture_logs


class _RecordingTransport:
    """A fake transport that records its call and returns a canned response."""

    def __init__(self, status: int = 200, body: bytes = b"{}") -> None:
        self.status = status
        self.body = body
        self.calls: list[dict[str, object]] = []

    def __call__(
        self, url: str, *, body: bytes, headers: Mapping[str, str], timeout: float
    ) -> tuple[int, bytes]:
        self.calls.append({"url": url, "body": body, "headers": dict(headers), "timeout": timeout})
        return self.status, self.body


def _client(transport: object) -> DhanClient:
    return DhanClient(
        access_token="jwt-abc",
        client_id="100000001",
        transport=transport,  # type: ignore[arg-type]
        base_url="https://api.dhan.co/v2/",
    )


class TestGuardUrl:
    def test_allows_dhan_https(self) -> None:
        _guard_url("https://api.dhan.co/v2/charts/intraday")  # no raise

    @pytest.mark.parametrize(
        "url",
        [
            "http://api.dhan.co/v2/charts/intraday",  # not https
            "https://evil.com/v2/charts/intraday",  # not allowlisted
            "https://api.dhan.co.evil.com/x",  # lookalike host
        ],
    )
    def test_rejects_bad_url(self, url: str) -> None:
        with pytest.raises(DhanError, match="non-allowlisted"):
            _guard_url(url)


class TestPostJson:
    def test_happy_path_returns_dict_and_sends_headers(self) -> None:
        t = _RecordingTransport(200, b'{"data": {"open": [1, 2]}}')
        result = _client(t).post_json("charts/intraday", {"securityId": "13"})
        assert result == {"data": {"open": [1, 2]}}
        sent = t.calls[0]
        headers = sent["headers"]
        assert isinstance(headers, dict)
        assert headers[HEADER_ACCESS_TOKEN] == "jwt-abc"
        assert headers[HEADER_CLIENT_ID] == "100000001"
        assert headers["Content-Type"] == "application/json"
        assert sent["url"] == "https://api.dhan.co/v2/charts/intraday"
        assert sent["body"] == b'{"securityId": "13"}'

    def test_ssrf_guard_blocks_bad_base_url(self) -> None:
        client = DhanClient(
            access_token="x",
            client_id="y",
            transport=_RecordingTransport(),  # type: ignore[arg-type]
            base_url="https://evil.example/",
        )
        with pytest.raises(DhanError, match="non-allowlisted"):
            client.post_json("charts/intraday", {})

    def test_non_200_status_mapped_to_typed_error(self) -> None:
        with pytest.raises(DhanRateLimitError):
            _client(_RecordingTransport(429, b"rate limited")).post_json("x", {})

    def test_malformed_json_raises(self) -> None:
        with pytest.raises(DhanError, match="malformed JSON"):
            _client(_RecordingTransport(200, b"not-json")).post_json("x", {})

    def test_non_object_json_raises(self) -> None:
        with pytest.raises(DhanError, match="expected a JSON object"):
            _client(_RecordingTransport(200, b"[1, 2, 3]")).post_json("x", {})

    def test_timeout_translated(self) -> None:
        def _boom(*_a: object, **_k: object) -> tuple[int, bytes]:
            raise TimeoutError("slow")

        with pytest.raises(DhanTimeoutError, match="timeout POSTing"):
            _client(_boom).post_json("charts/intraday", {})

    def test_transport_error_translated(self) -> None:
        def _boom(*_a: object, **_k: object) -> tuple[int, bytes]:
            raise OSError("conn reset")

        with pytest.raises(DhanTransportError, match="transport error"):
            _client(_boom).post_json("charts/intraday", {})


class _StepClock:
    """Deterministic monotonic clock advancing a fixed step each call.

    ``post_json`` reads the clock twice per call (start + completion), so a
    ``step`` of 0.1 s yields a MEASURED per-call latency of exactly 100 ms.
    """

    def __init__(self, step: float = 0.1) -> None:
        self._t = 0.0
        self._step = step

    def __call__(self) -> float:
        self._t += self._step
        return self._t


class TestCallMeter:
    def test_empty_snapshot_is_all_zero(self) -> None:
        # No calls yet → vacuously zero, never divides by zero.
        assert CallMeter().snapshot() == {
            "calls": 0,
            "effective_rps": 0.0,
            "latency_ms_avg": 0.0,
            "latency_ms_min": 0.0,
            "latency_ms_max": 0.0,
            "latency_ms_p95": 0.0,
        }

    def test_records_latency_and_effective_rps(self) -> None:
        m = CallMeter()
        m.record(latency_s=0.10, at=1.0)  # first call sets _first_at
        m.record(latency_s=0.30, at=1.5)  # 1 interval over 0.5 s → 2 r/s
        snap = m.snapshot()
        assert snap == {
            "calls": 2,
            "effective_rps": 2.0,
            "latency_ms_avg": 200.0,
            "latency_ms_min": 100.0,
            "latency_ms_max": 300.0,
            "latency_ms_p95": 300.0,
        }

    def test_single_record_zero_span_reports_zero_rps(self) -> None:
        # One call → first == last → span 0 → the guarded `span > 0` False arm.
        m = CallMeter()
        m.record(latency_s=0.2, at=5.0)
        snap = m.snapshot()
        assert snap["calls"] == 1
        assert snap["effective_rps"] == 0.0
        assert snap["latency_ms_avg"] == 200.0


class TestPostJsonInstrumentation:
    def _metered(self, transport: object, *, log_every: int) -> DhanClient:
        return DhanClient(
            access_token="jwt-abc",
            client_id="100000001",
            transport=transport,  # type: ignore[arg-type]
            base_url="https://api.dhan.co/v2/",
            clock=_StepClock(0.1),
            log_every=log_every,
        )

    def test_each_call_records_measured_latency(self) -> None:
        t = _RecordingTransport(200, b"{}")
        client = self._metered(t, log_every=25)
        client.post_json("charts/intraday", {"securityId": "13"})
        assert client.meter.count == 1  # default log_every → no summary (1 % 25 ≠ 0)
        assert client.meter.snapshot()["latency_ms_avg"] == 100.0

    def test_throughput_summary_emitted_every_log_every(self) -> None:
        t = _RecordingTransport(200, b"{}")
        client = self._metered(t, log_every=2)
        with capture_logs() as logs:
            client.post_json("charts/intraday", {"securityId": "13"})  # call 1 → 1 % 2 ≠ 0
            client.post_json("charts/intraday", {"securityId": "13"})  # call 2 → 2 % 2 == 0
        per_call = [e for e in logs if e["event"] == "dhan.call"]
        throughput = [e for e in logs if e["event"] == "dhan.throughput"]
        assert len(per_call) == 2  # every call logs a DEBUG line
        assert len(throughput) == 1  # summary fires once, on the 2nd call
        assert throughput[0]["calls"] == 2
        assert throughput[0]["latency_ms_avg"] == 100.0

    def test_latency_recorded_even_when_transport_times_out(self) -> None:
        def _boom(*_a: object, **_k: object) -> tuple[int, bytes]:
            raise TimeoutError("slow")

        client = self._metered(_boom, log_every=25)
        with pytest.raises(DhanTimeoutError):
            client.post_json("charts/intraday", {})
        assert client.meter.count == 1  # a failed call's latency is NOT invisible
