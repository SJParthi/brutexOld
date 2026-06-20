"""Unit tests for ``brutex.adapters.groww.client`` (DEC-DHAN-GROWW-REMIGRATE-001 / PR-F).

A fake SDK drives every retry branch — no ``growwapi``, no network, no real sleep.
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest
from brutex.adapters.groww.client import GrowwClient
from brutex.adapters.groww.errors import GrowwRateLimitError, GrowwTimeoutError
from structlog.testing import capture_logs


# Fakes named like the SDK exceptions so from_groww_exception maps them.
class GrowwAPITimeoutException(Exception): ...


class GrowwAPIRateLimitException(Exception): ...


class _FakeSDK:
    """Returns canned payloads, or raises a scripted sequence of exceptions."""

    def __init__(
        self, *, raises: list[Exception] | None = None, payload: Mapping[str, object] | None = None
    ) -> None:
        self._raises = list(raises or [])
        self._payload = payload if payload is not None else {"candles": []}
        self.calls = 0

    def _next(self) -> Mapping[str, object]:
        self.calls += 1
        if self._raises:
            raise self._raises.pop(0)
        return self._payload

    def get_historical_candles(self, **_kw: object) -> Mapping[str, object]:
        return self._next()

    def get_expiries(self, **_kw: object) -> Mapping[str, object]:
        return self._next()

    def get_contracts(self, **_kw: object) -> Mapping[str, object]:
        return self._next()


def _client(sdk: _FakeSDK) -> tuple[GrowwClient, list[float]]:
    slept: list[float] = []
    client = GrowwClient(sdk=sdk, sleep=slept.append, max_attempts=3, backoff_seconds=0.1)
    return client, slept


def test_happy_path_returns_payload_one_call() -> None:
    sdk = _FakeSDK(payload={"candles": [[1, 2, 3, 4, 5, 6, None]]})
    client, _slept = _client(sdk)
    out = client.historical_candles(
        exchange="NSE",
        segment="CASH",
        groww_symbol="NSE-NIFTY",
        interval="1minute",
        start="2024-01-01 09:15:00",
        end="2024-01-02 15:30:00",
    )
    assert out == {"candles": [[1, 2, 3, 4, 5, 6, None]]}
    assert sdk.calls == 1


def test_base_exception_still_releases_the_in_flight_meter_slot() -> None:
    # A BaseException (KeyboardInterrupt = Ctrl-C mid-call during a live pull) escapes
    # the `except Exception` guard, but the try/finally bracket MUST still release the
    # in-flight meter slot — else the shared concurrency gauge leaks forever, the very
    # gauge DEC-GROWW-RATE-ACHIEVED-METER-001 exists to make trustworthy.
    class _InterruptSDK:
        def get_historical_candles(self, **_kw: object) -> Mapping[str, object]:
            raise KeyboardInterrupt("operator hit Ctrl-C mid-call")

        def get_expiries(self, **_kw: object) -> Mapping[str, object]:  # pragma: no cover
            return {}

        def get_contracts(self, **_kw: object) -> Mapping[str, object]:  # pragma: no cover
            return {}

    client = GrowwClient(sdk=_InterruptSDK(), sleep=lambda _s: None)
    with pytest.raises(KeyboardInterrupt):
        client.historical_candles(
            exchange="NSE",
            segment="INDEX",
            groww_symbol="NSE-NIFTY",
            interval="1day",
            start="2020-01-01 09:15:00",
            end="2020-06-29 15:30:00",
        )
    assert client.meter.snapshot()["in_flight"] == 0  # slot released despite BaseException


def test_replace_preserves_the_same_meter_instance() -> None:
    # `_with_paced_client` rebuilds the client via dataclasses.replace to wire `pace`;
    # the meter MUST stay the SAME instance so the live pull + the dashboard read ONE
    # aggregate, never two fragmented per-client meters (the invisibility this fixes).
    from dataclasses import replace

    client = GrowwClient(sdk=_FakeSDK())
    paced = replace(client, pace=lambda: None)
    assert paced.meter is client.meter


def test_call_logs_real_per_call_latency() -> None:
    """Every successful Groww call logs its REAL wall-clock latency (DEC-GROWW-CALL-LATENCY-LOG-001).

    So the operator SEES the true per-call response time live in /logs instead of it
    being back-derived — the measurement that ends the "is it 15 s or stuck?" guessing.
    """
    sdk = _FakeSDK(payload={"candles": []})
    times = iter([10.0, 10.5])  # start, end → 0.5 s = 500 ms
    client = GrowwClient(sdk=sdk, sleep=lambda _s: None, clock=lambda: next(times))
    with capture_logs() as logs:
        client.historical_candles(
            exchange="NSE",
            segment="INDEX",
            groww_symbol="NSE-NIFTY",
            interval="1day",
            start="2020-01-01 09:15:00",
            end="2020-06-29 15:30:00",
        )
    calls = [e for e in logs if e["event"] == "groww.call"]
    assert len(calls) == 1
    assert calls[0]["method"] == "historical_candles"
    assert calls[0]["attempt"] == 1
    assert calls[0]["elapsed_ms"] == 500.0


def test_call_latency_log_labels_each_method() -> None:
    """The latency log tags WHICH Groww call it timed (historical / expiries / contracts)."""
    sdk = _FakeSDK(payload={"expiries": []})
    client = GrowwClient(sdk=sdk, sleep=lambda _s: None, clock=lambda: 0.0)
    with capture_logs() as logs:
        client.expiries(exchange="NSE", underlying_symbol="NIFTY")
        client.contracts(exchange="NSE", underlying_symbol="NIFTY", expiry_date="2024-01-25")
    methods = [e["method"] for e in logs if e["event"] == "groww.call"]
    assert methods == ["get_expiries", "get_contracts"]


def test_timeout_then_success_retries() -> None:
    sdk = _FakeSDK(raises=[GrowwAPITimeoutException("stall")], payload={"ok": True})
    client, slept = _client(sdk)
    out = client.expiries(exchange="NSE", underlying_symbol="NIFTY", year=2024, month=1)
    assert out == {"ok": True}
    assert sdk.calls == 2  # one failure + one success
    assert slept == [0.1]  # one backoff (base * attempt 1)


def test_timeout_exhausts_and_raises() -> None:
    sdk = _FakeSDK(
        raises=[
            GrowwAPITimeoutException("a"),
            GrowwAPITimeoutException("b"),
            GrowwAPITimeoutException("c"),
        ]
    )
    client, slept = _client(sdk)
    with pytest.raises(GrowwTimeoutError):
        client.contracts(exchange="NSE", underlying_symbol="NIFTY", expiry_date="2024-01-25")
    assert sdk.calls == 3  # max_attempts
    assert slept == [0.1, 0.2]  # backoff before attempts 2 and 3, none after the last


# The SDK's BASE client-error class — a live rate-limit arrives as THIS (with a
# "Rate limit …" message + optional `code`), NOT the typed subclass.
class GrowwAPIException(Exception):
    """Generic Groww client error; carries an optional HTTP-ish ``code``."""

    def __init__(self, message: str, code: object = None) -> None:
        super().__init__(message)
        self.code = code


def _client_with_hooks(
    sdk: _FakeSDK,
) -> tuple[GrowwClient, list[float], list[int], list[int]]:
    """A client wired with recording decrement/recover hooks + a fast rate-limit backoff."""
    slept: list[float] = []
    rate_hits: list[int] = []
    successes: list[int] = []
    client = GrowwClient(
        sdk=sdk,
        sleep=slept.append,
        on_rate_limit=lambda: rate_hits.append(1),
        on_success=lambda: successes.append(1),
        max_attempts=3,
        backoff_seconds=0.1,
        rate_limit_backoff_seconds=0.2,
    )
    return client, slept, rate_hits, successes


def test_rate_limit_retries_decrements_then_exhausts() -> None:
    """A sustained 429 retries (decrementing each time) then raises (DEC-GROWW-RATELIMIT-RETRY-001)."""
    sdk = _FakeSDK(
        raises=[
            GrowwAPIRateLimitException("429"),
            GrowwAPIRateLimitException("429"),
            GrowwAPIRateLimitException("429"),
        ]
    )
    client, slept, rate_hits, successes = _client_with_hooks(sdk)
    with pytest.raises(GrowwRateLimitError):
        client.expiries(exchange="NSE", underlying_symbol="NIFTY")
    assert sdk.calls == 3  # all 3 attempts used (was 1 in the old fail-fast design)
    assert slept == [0.2, 0.4]  # rate_limit_backoff × attempt for the two retries
    assert rate_hits == [1, 1, 1]  # decrement fired on EVERY 429
    assert successes == []


def test_rate_limit_then_success_recovers() -> None:
    """A transient 429 retries, succeeds, and recovers the rate (on_success)."""
    sdk = _FakeSDK(raises=[GrowwAPIRateLimitException("429")])
    client, slept, rate_hits, successes = _client_with_hooks(sdk)
    out = client.contracts(exchange="NSE", underlying_symbol="NIFTY", expiry_date="2025-01-30")
    assert out == {"candles": []}  # retried → succeeded on the 2nd call
    assert sdk.calls == 2
    assert slept == [0.2]
    assert rate_hits == [1]  # one decrement
    assert successes == [1]  # recover on the clean call


def test_generic_api_exception_rate_limit_message_is_retried() -> None:
    """THE live-bug regression: a generic ``GrowwAPIException('Rate limit …')`` is retried + decremented.

    The SDK raised the BASE class, not ``GrowwAPIRateLimitException`` — the old
    mapping saw it as terminal and the chain pull died instantly (the operator's
    "0/1 derivatives failed: Rate limit has breached"). It must now classify as a
    rate-limit, decrement, and retry to success.
    """
    sdk = _FakeSDK(
        raises=[
            GrowwAPIException("Rate limit has breached for your request. Please try again later.")
        ]
    )
    client, _slept, rate_hits, successes = _client_with_hooks(sdk)
    out = client.expiries(exchange="NSE", underlying_symbol="NIFTY")
    assert out == {"candles": []}  # retried → succeeded
    assert sdk.calls == 2
    assert rate_hits == [1]
    assert successes == [1]


def test_generic_api_exception_429_code_is_retried() -> None:
    """A generic exception whose ``code`` is 429 (no rate-limit wording) still classifies + retries."""
    sdk = _FakeSDK(raises=[GrowwAPIException("request failed", code=429)])
    client, _slept, rate_hits, successes = _client_with_hooks(sdk)
    out = client.expiries(exchange="NSE", underlying_symbol="NIFTY")
    assert out == {"candles": []}
    assert sdk.calls == 2
    assert rate_hits == [1]
    assert successes == [1]


def test_terminal_generic_exception_is_not_retried() -> None:
    """An unrecognised generic error is terminal — never blindly retried (Golden Rule #1)."""
    from brutex.adapters.groww.errors import GrowwError

    sdk = _FakeSDK(raises=[GrowwAPIException("some unknown failure")])
    client, slept, rate_hits, successes = _client_with_hooks(sdk)
    with pytest.raises(GrowwError):
        client.expiries(exchange="NSE", underlying_symbol="NIFTY")
    assert sdk.calls == 1  # raised immediately
    assert slept == []
    assert rate_hits == []
    assert successes == []


def _empty_body_decode_error() -> Exception:
    """The exact ``JSONDecodeError`` an SDK ``response.json()`` raises on an empty body."""
    import json

    try:
        json.loads("")
    except json.JSONDecodeError as exc:
        return exc
    raise AssertionError("json.loads('') must raise")  # pragma: no cover


def test_empty_body_json_decode_error_is_retried_then_succeeds() -> None:
    """THE TITAN / SHRIRAMFIN regression: an empty-body ``JSONDecodeError`` is RETRIED, not terminal.

    DEC-GROWW-EMPTY-BODY-RETRY-001 — the SDK's ``response.json()`` raises a
    ``json.JSONDecodeError`` on a transient empty/throttled body. The old mapping
    saw it terminal ("not on Groww") and dropped real F&O stocks. It must now
    classify retryable so the in-call loop re-issues it and the pull recovers.
    """
    sdk = _FakeSDK(raises=[_empty_body_decode_error()], payload={"candles": []})
    client, slept = _client(sdk)
    out = client.historical_candles(
        exchange="NSE",
        segment="CASH",
        groww_symbol="NSE-TITAN",
        interval="1minute",
        start="2024-01-01 09:15:00",
        end="2024-01-02 15:30:00",
    )
    assert out == {"candles": []}  # retried → succeeded
    assert sdk.calls == 2  # one empty-body failure + one success (NOT a terminal drop)
    assert slept == [0.1]  # one bounded backoff


def test_empty_body_exhausts_and_raises_retryable() -> None:
    """A SUSTAINED empty-body throttle exhausts the in-call budget then raises a RETRYABLE error.

    The exhausted residual is then re-queued by the until-clean loop (the per-window
    transient path) — NEVER a terminal "not on Groww" quarantine.
    """
    from brutex.adapters.groww.errors import GrowwEmptyResponseError, is_retryable

    sdk = _FakeSDK(
        raises=[_empty_body_decode_error(), _empty_body_decode_error(), _empty_body_decode_error()]
    )
    client, _slept = _client(sdk)
    with pytest.raises(GrowwEmptyResponseError) as caught:
        client.historical_candles(
            exchange="NSE",
            segment="CASH",
            groww_symbol="NSE-SHRIRAMFIN",
            interval="1minute",
            start="2024-01-01 09:15:00",
            end="2024-01-02 15:30:00",
        )
    assert sdk.calls == 3  # all attempts used (NOT a single terminal drop)
    assert is_retryable(caught.value) is True  # residual stays re-queueable


class _RecordingSDK:
    """Captures the exact kwargs forwarded to ``get_expiries`` (DEC-GROWW-OPTION-INGEST-001)."""

    def __init__(self) -> None:
        self.expiry_kwargs: dict[str, object] = {}

    def get_historical_candles(self, **_kw: object) -> Mapping[str, object]:  # pragma: no cover
        return {"candles": []}

    def get_expiries(self, **kw: object) -> Mapping[str, object]:
        self.expiry_kwargs = dict(kw)
        return {"expiries": []}

    def get_contracts(self, **_kw: object) -> Mapping[str, object]:  # pragma: no cover
        return {"contracts": []}


def test_expiries_omits_year_and_month_when_none() -> None:
    sdk = _RecordingSDK()
    GrowwClient(sdk=sdk, sleep=lambda _s: None).expiries(exchange="NSE", underlying_symbol="NIFTY")
    assert sdk.expiry_kwargs == {"exchange": "NSE", "underlying_symbol": "NIFTY"}


def test_expiries_forwards_year_but_omits_month() -> None:
    sdk = _RecordingSDK()
    GrowwClient(sdk=sdk, sleep=lambda _s: None).expiries(
        exchange="NSE", underlying_symbol="NIFTY", year=2024
    )
    assert sdk.expiry_kwargs == {"exchange": "NSE", "underlying_symbol": "NIFTY", "year": 2024}


def test_expiries_forwards_both_year_and_month() -> None:
    sdk = _RecordingSDK()
    GrowwClient(sdk=sdk, sleep=lambda _s: None).expiries(
        exchange="NSE", underlying_symbol="NIFTY", year=2024, month=3
    )
    assert sdk.expiry_kwargs["year"] == 2024 and sdk.expiry_kwargs["month"] == 3


# ── Historical-pull FREEZE GUARD (DEC-GROWW-PULL-CALL-DEADLINE-001) ──────────


class TestCallDeadline:
    """A stalled SDK call is bounded by ``call_timeout_seconds`` — the pull never freezes."""

    def test_stalled_call_times_out_instead_of_freezing(self) -> None:
        import threading

        from brutex.adapters.groww.client import GrowwClient

        release = threading.Event()

        class _HangingSDK:
            calls = 0

            def get_historical_candles(self, **_kw: object) -> Mapping[str, object]:
                type(self).calls += 1
                release.wait(5)  # would block ~forever without the deadline guard
                return {"candles": []}

            def get_expiries(self, **_kw: object) -> Mapping[str, object]:  # pragma: no cover
                return {}  # GrowwSDK Protocol conformance only — never called by this test

            def get_contracts(self, **_kw: object) -> Mapping[str, object]:  # pragma: no cover
                return {}  # GrowwSDK Protocol conformance only — never called by this test

        sdk = _HangingSDK()
        client = GrowwClient(
            sdk=sdk, sleep=lambda _s: None, max_attempts=2, call_timeout_seconds=0.05
        )
        try:
            with pytest.raises(GrowwTimeoutError):
                client.historical_candles(
                    exchange="NSE",
                    segment="INDEX",
                    groww_symbol="NSE-NIFTY",
                    interval="1day",
                    start="2020-01-01 09:15:00",
                    end="2020-06-29 15:30:00",
                )
        finally:
            release.set()  # let the abandoned daemon worker(s) exit
        # It RETRIED (each attempt hit the deadline), proving the loop handled the
        # timeout as retryable rather than the call hanging forever.
        assert _HangingSDK.calls == 2

    def test_fast_call_within_the_deadline_returns_normally(self) -> None:
        sdk = _FakeSDK(payload={"candles": [[1, 2, 3, 4, 5, 6, None]]})
        client = GrowwClient(sdk=sdk, sleep=lambda _s: None, call_timeout_seconds=5.0)
        out = client.historical_candles(
            exchange="NSE",
            segment="INDEX",
            groww_symbol="NSE-NIFTY",
            interval="1day",
            start="2020-01-01 09:15:00",
            end="2020-06-29 15:30:00",
        )
        assert out == {"candles": [[1, 2, 3, 4, 5, 6, None]]}

    def test_run_with_deadline_propagates_the_underlying_error(self) -> None:
        from brutex.adapters.groww.client import run_with_deadline

        def _boom() -> Mapping[str, object]:
            raise ValueError("real failure, not a timeout")

        with pytest.raises(ValueError, match="real failure"):
            run_with_deadline(_boom, 5.0)

    def test_run_with_deadline_returns_the_value_when_fast(self) -> None:
        from brutex.adapters.groww.client import run_with_deadline

        assert run_with_deadline(lambda: {"candles": []}, 5.0) == {"candles": []}


def test_with_pace_rebinds_pace_and_preserves_meter() -> None:
    """`with_pace` returns a NEW client with pace rebound, sharing the SAME meter (immutable).

    The GrowwClientProtocol seam `_with_paced_client` uses to wire the shared rate bucket
    after the adapter is built (DEC-GROWW-CLIENT-POOL-001) — a `dataclasses.replace`, so the
    original is untouched and the achieved-rate meter (compare=False) carries through.
    """
    sdk = _FakeSDK()
    client = GrowwClient(sdk=sdk)

    def _pace() -> None:
        pass

    repaced = client.with_pace(_pace)
    assert repaced is not client  # immutable copy, not a mutation
    assert repaced.pace is _pace  # the new bucket hook is wired
    assert client.pace is not _pace  # the original is untouched
    assert repaced.meter is client.meter  # the shared aggregate meter carries through
    assert repaced.sdk is sdk  # same SDK connection


def test_historical_candles_normalizes_iso_t_to_space() -> None:
    """DEC-GROWW-CANDLE-TS-SPACE-001 — the SDK receives Groww's documented SPACE format.

    Windows are tiled with ``datetime.isoformat`` (``T`` separator), but the API
    reference specifies ``yyyy-MM-dd HH:mm:ss`` with a space. The client normalizes
    at this single funnel; the replace is a no-op for an epoch-seconds bound.
    """

    class _CapturingSDK:
        def __init__(self) -> None:
            self.kwargs: dict[str, object] = {}

        def get_historical_candles(self, **kw: object) -> Mapping[str, object]:
            self.kwargs = kw
            return {"candles": []}

        def get_expiries(self, **_kw: object) -> Mapping[str, object]:  # pragma: no cover
            return {}

        def get_contracts(self, **_kw: object) -> Mapping[str, object]:  # pragma: no cover
            return {}

    sdk = _CapturingSDK()
    client = GrowwClient(sdk=sdk, sleep=lambda _s: None)
    client.historical_candles(
        exchange="NSE",
        segment="CASH",
        groww_symbol="NSE-RELIANCE",
        interval="1day",
        start="2025-11-30T00:00:00",
        end="2026-05-29T00:00:00",
    )
    assert sdk.kwargs["start_time"] == "2025-11-30 00:00:00"
    assert sdk.kwargs["end_time"] == "2026-05-29 00:00:00"


def test_historical_candles_epoch_bound_unchanged_by_normalize() -> None:
    """An epoch-seconds bound has no ``T`` — the normalize is a safe no-op."""

    class _CapturingSDK:
        def __init__(self) -> None:
            self.kwargs: dict[str, object] = {}

        def get_historical_candles(self, **kw: object) -> Mapping[str, object]:
            self.kwargs = kw
            return {"candles": []}

        def get_expiries(self, **_kw: object) -> Mapping[str, object]:  # pragma: no cover
            return {}

        def get_contracts(self, **_kw: object) -> Mapping[str, object]:  # pragma: no cover
            return {}

    sdk = _CapturingSDK()
    client = GrowwClient(sdk=sdk, sleep=lambda _s: None)
    client.historical_candles(
        exchange="NSE",
        segment="CASH",
        groww_symbol="NSE-RELIANCE",
        interval="1day",
        start="1700000000",
        end="1710000000",
    )
    assert sdk.kwargs["start_time"] == "1700000000"
    assert sdk.kwargs["end_time"] == "1710000000"
