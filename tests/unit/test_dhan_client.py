"""Tests for `brutex.adapters.dhan.client` — POST logic via a fake transport.

100% line+branch of the non-I/O surface: header build, SSRF guard, status
mapping, JSON parsing, timeout/transport error translation, and the bounded
latency-hygiene fast-retry (DEC-DHAN-CALL-TIMEOUT-RETRY-001 — timeout/transport/
5xx retried, 429 + terminal errors not). The real `urllib_transport` is
`# pragma: no cover` (network shim).

Whitebox: imports the module-private `_guard_url` / `_should_fast_retry`.
"""

# pyright: reportPrivateUsage=none
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

import pytest
from brutex.adapters.dhan.client import (
    _DEFAULT_RETRY_BACKOFF_S,
    _DEFAULT_TIMEOUT,
    _MAX_BACKOFF_S,
    CallMeter,
    DhanClient,
    _guard_url,
    _should_fast_retry,
)
from brutex.adapters.dhan.constants import (
    DATA_RATE_LIMIT_RPS,
    HEADER_ACCESS_TOKEN,
    HEADER_CLIENT_ID,
)
from brutex.adapters.dhan.errors import (
    DhanBadRequestError,
    DhanError,
    DhanRateLimitError,
    DhanServerError,
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
        self,
        url: str,
        *,
        body: bytes,
        headers: Mapping[str, str],
        timeout: float,
        method: str = "POST",
    ) -> tuple[int, bytes]:
        self.calls.append(
            {
                "url": url,
                "body": body,
                "headers": dict(headers),
                "timeout": timeout,
                "method": method,
            }
        )
        return self.status, self.body


def _noop_sleep(_seconds: float) -> None:
    """A backoff sleep that never actually waits (deterministic, fast tests)."""


def _client(transport: object) -> DhanClient:
    # ``max_attempts=1`` pins the legacy single-call semantics these tests assert
    # (exactly one transport call); the fast-retry loop is covered by
    # ``TestFastRetry`` below.
    return DhanClient(
        access_token="jwt-abc",
        client_id="100000001",
        transport=transport,  # type: ignore[arg-type]
        base_url="https://api.dhan.co/v2/",
        max_attempts=1,
        sleep=_noop_sleep,
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

    def test_default_timeout_is_generous_for_large_intraday_payloads(self) -> None:
        # DEC-DHAN-INTRADAY-TIMEOUT-RAISE-001: a 90-day 1-minute chunk (~22k bars)
        # can take >=10s from Dhan under sustained load, so the default
        # whole-request timeout must leave room for a slow-but-valid response to
        # complete (the old 10s killed them as false timeouts + re-requested).
        assert _DEFAULT_TIMEOUT >= 25.0
        # A client built without an explicit timeout inherits the default and
        # passes it to the transport on every call.
        transport = _RecordingTransport(200, b"{}")
        _client(transport).post_json("charts/intraday", {})
        assert transport.calls[0]["timeout"] == _DEFAULT_TIMEOUT

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

    def test_token_provider_overrides_static_header(self) -> None:
        # DEC-DHAN-TOKEN-KEEPALIVE-001: when a token_provider is injected the
        # access-token header reads from it (the LIVE holder), NOT the static field,
        # so a mid-life renew propagates to the very next request.
        live = {"token": "fresh-jwt"}
        t = _RecordingTransport(200, b"{}")
        client = DhanClient(
            access_token="stale-jwt",
            client_id="100000001",
            transport=t,  # type: ignore[arg-type]
            base_url="https://api.dhan.co/v2/",
            max_attempts=1,
            sleep=_noop_sleep,
            token_provider=lambda: live["token"],
        )
        client.post_json("charts/intraday", {})
        headers = t.calls[0]["headers"]
        assert isinstance(headers, dict)
        assert headers[HEADER_ACCESS_TOKEN] == "fresh-jwt"
        # A renew swaps the holder's value; the NEXT call reads it live.
        live["token"] = "fresher-jwt"
        client.post_json("charts/intraday", {})
        headers2 = t.calls[1]["headers"]
        assert isinstance(headers2, dict)
        assert headers2[HEADER_ACCESS_TOKEN] == "fresher-jwt"

    def test_token_provider_none_uses_static_token(self) -> None:
        # Default (no provider) → the static access_token, byte-unchanged.
        t = _RecordingTransport(200, b"{}")
        _client(t).post_json("charts/intraday", {})
        headers = t.calls[0]["headers"]
        assert isinstance(headers, dict)
        assert headers[HEADER_ACCESS_TOKEN] == "jwt-abc"


class TestGetJson:
    """The headers-only GET surface — ``RenewToken`` (DEC-DHAN-RENEWTOKEN-GET-001).

    ``get_json`` is a GET (the fake records ``method == "GET"``), sends an EMPTY
    body, carries the same ``access-token`` + ``dhanClientId`` headers as a POST,
    and runs the SAME metered + paced + bounded fast-retry loop.
    """

    def test_issues_get_with_empty_body_and_headers(self) -> None:
        t = _RecordingTransport(200, b'{"accessToken": "new", "expiryTime": "t"}')
        result = _client(t).get_json("RenewToken")
        assert result == {"accessToken": "new", "expiryTime": "t"}
        sent = t.calls[0]
        assert sent["method"] == "GET"  # GET, not POST
        assert sent["body"] == b""  # headers-only, NO request body
        assert sent["url"] == "https://api.dhan.co/v2/RenewToken"
        headers = sent["headers"]
        assert isinstance(headers, dict)
        assert headers[HEADER_ACCESS_TOKEN] == "jwt-abc"
        assert headers[HEADER_CLIENT_ID] == "100000001"

    def test_post_json_still_sends_post(self) -> None:
        # The POST path is byte-unchanged — it still records method == "POST".
        t = _RecordingTransport(200, b"{}")
        _client(t).post_json("charts/intraday", {"securityId": "13"})
        assert t.calls[0]["method"] == "POST"
        assert t.calls[0]["body"] == b'{"securityId": "13"}'

    def test_get_json_meters_issue_and_warns_on_breach(self) -> None:
        # The 5 r/s issue gauge + ``rate.breach`` WARNING apply to GET identically
        # (DEC-DHAN-RATE-REALTIME-GUARANTEE-001): 6 GETs inside one second breach.
        t = _RecordingTransport(200, b'{"accessToken": "n", "expiryTime": "t"}')
        client = DhanClient(
            access_token="jwt-abc",
            client_id="100000001",
            transport=t,  # type: ignore[arg-type]
            clock=_StepClock(0.01),  # 6 issues land well within 1.0s
            max_attempts=1,
            sleep=_noop_sleep,
        )
        with capture_logs() as logs:
            for _ in range(DATA_RATE_LIMIT_RPS + 1):  # 6 GETs
                client.get_json("RenewToken")
        breaches = [e for e in logs if e["event"] == "rate.breach"]
        assert breaches and breaches[-1]["rps_1s"] > DATA_RATE_LIMIT_RPS
        assert client.meter.snapshot()["rps_1s_max"] > DATA_RATE_LIMIT_RPS

    def test_get_timeout_translated(self) -> None:
        def _boom(*_a: object, **_k: object) -> tuple[int, bytes]:
            raise TimeoutError("slow")

        with pytest.raises(DhanTimeoutError, match="timeout GETing"):
            _client(_boom).get_json("RenewToken")


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
            "rps_1s": 0,
            "rps_1s_max": 0,
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
            "rps_1s": 0,  # no record_issue() calls here → issue gauge stays 0
            "rps_1s_max": 0,
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

    def test_record_issue_tracks_peak_and_evicts_stale(self) -> None:
        # The trailing-1s issue gauge (DEC-DHAN-RATE-REALTIME-GUARANTEE-001): five
        # issues at 0.2s spacing peak at 5; jumping >1s ahead ages them out (the
        # eviction `popleft` loop) so the trailing count resets to 1.
        m = CallMeter()
        for t in (0.0, 0.2, 0.4, 0.6, 0.8):
            m.record_issue(t)
        assert m.snapshot()["rps_1s_max"] == 5
        assert m.record_issue(2.0) == 1  # exercises the stale-eviction popleft


class TestPostJsonInstrumentation:
    def _metered(self, transport: object, *, log_every: int) -> DhanClient:
        # Single-attempt (see ``_client``): one transport call ⇒ one meter record.
        return DhanClient(
            access_token="jwt-abc",
            client_id="100000001",
            transport=transport,  # type: ignore[arg-type]
            base_url="https://api.dhan.co/v2/",
            clock=_StepClock(0.1),
            log_every=log_every,
            max_attempts=1,
            sleep=_noop_sleep,
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

    def test_rate_breach_warning_when_issue_rate_exceeds_cap(self) -> None:
        # Real-time ASSURANCE (DEC-DHAN-RATE-REALTIME-GUARANTEE-001): a fine clock
        # keeps 6 successful POSTs inside one second → the trailing-1s issue rate
        # crosses the 5 r/s cap, so `_send_once` emits the `rate.breach` WARNING.
        t = _RecordingTransport(200, b"{}")
        client = DhanClient(
            access_token="jwt-abc",
            client_id="100000001",
            transport=t,  # type: ignore[arg-type]
            clock=_StepClock(0.01),  # 6 issues land well within 1.0s
            max_attempts=1,
            sleep=_noop_sleep,
        )
        with capture_logs() as logs:
            for _ in range(DATA_RATE_LIMIT_RPS + 1):  # 6 POSTs
                client.post_json("charts/intraday", {})
        breaches = [e for e in logs if e["event"] == "rate.breach"]
        assert breaches and breaches[-1]["rps_1s"] > DATA_RATE_LIMIT_RPS
        assert client.meter.snapshot()["rps_1s_max"] > DATA_RATE_LIMIT_RPS


#: One scripted transport outcome: an exception to RAISE, or a (status, body) to RETURN.
_Outcome = BaseException | tuple[int, bytes]


class _ScriptedTransport:
    """Fake transport replaying a script of outcomes, counting every call.

    Each outcome is either a ``BaseException`` to raise (a transport-level
    failure) or a ``(status, body)`` tuple to return. When the script is
    exhausted the LAST outcome repeats, so a one-item exception script models an
    'always fails' transport and a one-item tuple models 'always succeeds'.
    """

    def __init__(self, outcomes: Sequence[_Outcome]) -> None:
        self._outcomes: list[_Outcome] = list(outcomes)
        self.calls = 0

    def __call__(
        self,
        url: str,
        *,
        body: bytes,
        headers: Mapping[str, str],
        timeout: float,
        method: str = "POST",
    ) -> tuple[int, bytes]:
        outcome = self._outcomes[min(self.calls, len(self._outcomes) - 1)]
        self.calls += 1
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class _RecordingSleep:
    """A backoff sleep that records each requested wait instead of sleeping."""

    def __init__(self) -> None:
        self.waits: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


def _retry_client(
    transport: object,
    *,
    max_attempts: int = 3,
    retry_backoff_s: float = _DEFAULT_RETRY_BACKOFF_S,
    sleep: Callable[[float], None] = _noop_sleep,
) -> DhanClient:
    return DhanClient(
        access_token="jwt-abc",
        client_id="100000001",
        transport=transport,  # type: ignore[arg-type]
        base_url="https://api.dhan.co/v2/",
        clock=_StepClock(0.1),
        max_attempts=max_attempts,
        retry_backoff_s=retry_backoff_s,
        sleep=sleep,
    )


class TestFastRetry:
    """Bounded latency-hygiene fast-retry (DEC-DHAN-CALL-TIMEOUT-RETRY-001)."""

    def test_timeout_then_success_retries_in_place(self) -> None:
        # Times out twice, then 200 → returns on the 3rd physical attempt.
        t = _ScriptedTransport([TimeoutError("slow"), TimeoutError("slow"), (200, b'{"ok": 1}')])
        client = _retry_client(t, max_attempts=3)
        assert client.post_json("charts/intraday", {}) == {"ok": 1}
        assert t.calls == 3
        assert client.meter.count == 3  # every physical attempt is metered

    def test_transport_error_then_success_retries(self) -> None:
        t = _ScriptedTransport([OSError("conn reset"), (200, b"{}")])
        client = _retry_client(t, max_attempts=3)
        assert client.post_json("x", {}) == {}
        assert t.calls == 2

    def test_server_5xx_is_fast_retried(self) -> None:
        # 500 is retryable-and-not-429 → fast-retried in place, then succeeds.
        t = _ScriptedTransport([(500, b"boom"), (200, b"{}")])
        client = _retry_client(t, max_attempts=3)
        assert client.post_json("x", {}) == {}
        assert t.calls == 2

    def test_retry_budget_exhausted_raises_last_error(self) -> None:
        t = _ScriptedTransport([TimeoutError("slow")])  # always times out
        client = _retry_client(t, max_attempts=3)
        with pytest.raises(DhanTimeoutError):
            client.post_json("x", {})
        assert t.calls == 3  # bounded: exactly max_attempts physical calls

    def test_rate_limit_is_never_fast_retried(self) -> None:
        # 429 must NOT be retried in-client — the token bucket owns the 5 r/s cap.
        t = _ScriptedTransport([(429, b"slow down")])
        client = _retry_client(t, max_attempts=3)
        with pytest.raises(DhanRateLimitError):
            client.post_json("x", {})
        assert t.calls == 1  # exactly one attempt — no fast-retry storm on a 429

    def test_terminal_error_is_never_retried(self) -> None:
        t = _ScriptedTransport([(400, b"bad")])
        client = _retry_client(t, max_attempts=3)
        with pytest.raises(DhanBadRequestError):
            client.post_json("x", {})
        assert t.calls == 1

    def test_backoff_is_exponential_and_logged(self) -> None:
        sleeper = _RecordingSleep()
        t = _ScriptedTransport([TimeoutError("slow"), TimeoutError("slow"), (200, b"{}")])
        client = _retry_client(t, max_attempts=3, retry_backoff_s=0.05, sleep=sleeper)
        with capture_logs() as logs:
            client.post_json("charts/intraday", {})
        # 2 retries → 2 backoffs, doubling: 0.05 s then 0.10 s.
        assert sleeper.waits == pytest.approx([0.05, 0.10])
        retries = [e for e in logs if e["event"] == "dhan.retry"]
        assert [e["attempt"] for e in retries] == [1, 2]
        assert [e["backoff_ms"] for e in retries] == [50.0, 100.0]
        assert retries[0]["error"] == "DhanTimeoutError"
        assert retries[0]["endpoint"] == "charts/intraday"
        assert retries[0]["max_attempts"] == 3

    def test_no_retry_log_on_first_try_success(self) -> None:
        t = _ScriptedTransport([(200, b"{}")])
        client = _retry_client(t, max_attempts=3)
        with capture_logs() as logs:
            client.post_json("x", {})
        assert [e for e in logs if e["event"] == "dhan.retry"] == []
        assert t.calls == 1

    def test_max_attempts_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="max_attempts must be >= 1"):
            _retry_client(_ScriptedTransport([(200, b"{}")]), max_attempts=0)

    def test_should_fast_retry_predicate(self) -> None:
        # Whitebox check of the COMMON predicate's intent (reuse is_retryable,
        # exclude 429).
        assert _should_fast_retry(DhanTimeoutError("t")) is True
        assert _should_fast_retry(DhanTransportError("t")) is True
        assert _should_fast_retry(DhanServerError("t")) is True
        assert _should_fast_retry(DhanRateLimitError("t")) is False  # 429 excluded
        assert _should_fast_retry(DhanBadRequestError("t")) is False  # terminal
        assert _should_fast_retry(DhanError("t")) is False

    def test_default_backoff_is_the_rate_interval(self) -> None:
        # The default base backoff is the 5 r/s inter-request interval (0.2 s) so
        # a single sequential stream's retries stay within the per-second budget
        # (CLAUDE.md §2 Rule 6) — the rate-cap-honesty invariant.
        assert pytest.approx(0.2) == _DEFAULT_RETRY_BACKOFF_S
        sleeper = _RecordingSleep()
        t = _ScriptedTransport([TimeoutError("slow"), (200, b"{}")])
        client = _retry_client(t, max_attempts=2, sleep=sleeper)  # inherits default
        client.post_json("x", {})
        assert sleeper.waits == pytest.approx([0.2])

    def test_backoff_is_capped(self) -> None:
        # A large base can't produce an unbounded (sleep(inf)) wait — it is
        # clamped to _MAX_BACKOFF_S, defending the sequential pull from a hang.
        sleeper = _RecordingSleep()
        t = _ScriptedTransport([TimeoutError("slow"), (200, b"{}")])
        client = _retry_client(t, max_attempts=2, retry_backoff_s=10.0, sleep=sleeper)
        client.post_json("x", {})
        assert sleeper.waits == pytest.approx([_MAX_BACKOFF_S])  # min(10.0, 5.0)

    def test_get_json_is_fast_retried_and_paced_like_post(self) -> None:
        # A GET stall is fast-retried (bounded) and PACED between retries exactly
        # like a POST — pacing + backoff + bounded budget all apply to the GET path
        # (DEC-DHAN-RENEWTOKEN-GET-001 / DEC-DHAN-RATE-REALTIME-GUARANTEE-001).
        paces: list[str] = []
        sleeper = _RecordingSleep()
        body = b'{"accessToken": "n", "expiryTime": "t"}'
        t = _ScriptedTransport([TimeoutError("slow"), TimeoutError("slow"), (200, body)])
        client = DhanClient(
            access_token="jwt-abc",
            client_id="100000001",
            transport=t,  # type: ignore[arg-type]
            base_url="https://api.dhan.co/v2/",
            clock=_StepClock(0.1),
            max_attempts=3,
            retry_backoff_s=0.05,
            sleep=sleeper,
            pace=lambda: paces.append("pace"),
        )
        assert client.get_json("RenewToken") == {"accessToken": "n", "expiryTime": "t"}
        assert t.calls == 3  # 1 initial + 2 in-place retries (bounded)
        assert sleeper.waits == pytest.approx([0.05, 0.10])  # exponential backoff
        assert paces == ["pace", "pace"]  # paced before EACH retry, just like POST
