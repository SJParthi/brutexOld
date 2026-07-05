"""Unit tests for ``brutex.adapters.groww.client`` (DEC-DHAN-GROWW-REMIGRATE-001 / PR-F).

A fake SDK drives every retry branch — no ``growwapi``, no network, no real sleep.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

import pytest
from brutex.adapters.groww.client import REAUTH_RETRY_BACKOFF_SECONDS, GrowwClient
from brutex.adapters.groww.errors import (
    GrowwRateLimitError,
    GrowwTimeoutError,
    GrowwTokenStillStaleError,
)
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


def test_self_paces_predicate_single_admission_seam() -> None:
    """`self_paces` is True iff a real pace is bound; `client_self_paces` is getattr-safe.

    The single-admission seam (DEC-PULL-FORENSIC-WAVE1-001): the spines drop the
    per-window pace only when the client self-paces, so a fetch is admitted once.
    """
    from dataclasses import replace

    from brutex.adapters.groww.client import client_self_paces

    unpaced = GrowwClient(sdk=_FakeSDK())  # default pace = the no-op sentinel
    paced = replace(unpaced, pace=lambda: None)

    assert unpaced.self_paces is False
    assert paced.self_paces is True
    assert client_self_paces(paced) is True
    assert client_self_paces(unpaced) is False
    assert client_self_paces(None) is False  # a None client never self-paces
    assert client_self_paces(object()) is False  # a fake with no attr → False


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


# Transient network / DNS failure → RETRYABLE (DEC-GROWW-NETWORK-ERROR-RETRY-001).
# A RAW transport exception (socket.gaierror / requests ConnectionError shape) the
# SDK raises is now retried by the client's in-call fast-retry arm, instead of
# escaping terminally — the 218-contract "Failed to resolve api.groww.in" loss class.
class gaierror(Exception):
    """Fake ``socket.gaierror`` raised raw by the SDK on a host DNS outage."""


class ConnectionError(Exception):  # noqa: A001 — mirrors requests.exceptions.ConnectionError
    """Fake ``requests.exceptions.ConnectionError`` (the ``"Failed to resolve …"`` shape)."""


def test_dns_error_then_success_retries() -> None:
    """A transient DNS failure then a clean response → the client RETRIES, returns the payload.

    The raw transport error the SDK raises is classified RETRYABLE, so the in-call
    fast-retry re-issues it rather than letting it escape terminally.
    """
    sdk = _FakeSDK(raises=[gaierror("[Errno -2] Name or service not known")], payload={"ok": True})
    client, slept = _client(sdk)
    out = client.expiries(exchange="NSE", underlying_symbol="NIFTY", year=2024, month=1)
    assert out == {"ok": True}
    assert sdk.calls == 2  # one DNS failure + one success
    assert slept == [0.1]  # one bounded backoff (the gentle timeout ladder, base * attempt 1)


def test_dns_error_exhausts_and_raises_retryable() -> None:
    """A SUSTAINED DNS outage exhausts the in-call budget then raises a RETRYABLE network error.

    The exhausted residual is then re-queued by the per-window transient path /
    until-clean loop — NEVER a terminal quarantine (the 218-contract loss class).
    """
    from brutex.adapters.groww.errors import GrowwNetworkError, is_rate_limit, is_retryable

    sdk = _FakeSDK(
        raises=[
            ConnectionError("Failed to resolve 'api.groww.in'"),
            ConnectionError("Failed to resolve 'api.groww.in'"),
            ConnectionError("Failed to resolve 'api.groww.in'"),
        ]
    )
    client, _slept = _client(sdk)
    with pytest.raises(GrowwNetworkError) as caught:
        client.contracts(exchange="NSE", underlying_symbol="NIFTY", expiry_date="2024-01-25")
    assert sdk.calls == 3  # all attempts used (NOT a single terminal drop)
    assert is_retryable(caught.value) is True  # residual stays re-queueable
    assert is_rate_limit(caught.value) is False  # a DNS blip never steps the rate ladder


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

    def test_run_with_deadline_re_export_is_the_deadline_module_helper(self) -> None:
        """`client.run_with_deadline` is the SAME object moved to `deadline` (back-compat re-export).

        The helper was MOVED to ``brutex.adapters.groww.deadline`` (MINOR-1: a neutral
        stdlib-only module so ``reauth`` can use it without re-introducing the
        client↔reauth import cycle). ``client`` re-exports it so any legacy import path
        keeps working; the move's own behaviour is tested in ``test_groww_deadline.py``.
        """
        from brutex.adapters.groww.client import run_with_deadline as from_client
        from brutex.adapters.groww.deadline import run_with_deadline as from_deadline

        assert from_client is from_deadline


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


# ── AUTH-error auto-RE-MINT (DEC-GROWW-TOKEN-REAUTH-001) ────────────────────


# Named like the SDK exception so from_groww_exception maps it to GrowwAuthError.
class GrowwAPIAuthenticationException(Exception):
    """Fake SDK auth exception (named so from_groww_exception maps it to GrowwAuthError)."""


class _FakeRefresher:
    """A fake SdkRefresher: a queue of SDKs, single-flight via a generation counter.

    Models the real refresher's contract (current_sdk / current_generation / reauth)
    so the client's auto-RE-MINT path is driven with NO pyotp / growwapi / network.
    """

    def __init__(self, sdks: list[_FakeSDK]) -> None:
        self._sdks = sdks
        self._index = 0
        self.generation = 0
        self.reauth_calls = 0

    def sdk_at(self, _index: int) -> _FakeSDK:
        # The client resolves its leaf SDK by index; this single-leaf fake ignores it
        # and serves the current queue position (the re-mint advances the queue).
        return self._sdks[self._index]

    def current_sdk(self) -> _FakeSDK:  # pragma: no cover — leaf path uses sdk_at
        return self._sdks[self._index]

    def current_generation(self) -> int:
        return self.generation

    def reauth(self, seen_generation: int) -> int:
        self.reauth_calls += 1
        if self.generation != seen_generation:  # pragma: no cover — single-thread test path
            return self.generation
        self._index = min(self._index + 1, len(self._sdks) - 1)
        self.generation += 1
        return self.generation


def test_auth_error_triggers_reauth_then_succeeds() -> None:
    """An SDK that auth-fails once then a fresh SDK succeeds → exactly ONE reauth, used.

    THE 06:00 IST regression: the daily token reset invalidated the in-flight token, so
    every call hit GrowwAPIAuthenticationException → terminal GrowwAuthError → quarantine.
    Now the client RE-MINTS and retries against the refreshed SDK, self-healing the pull.
    """
    stale = _FakeSDK(raises=[GrowwAPIAuthenticationException("token expired or invalid")])
    fresh = _FakeSDK(payload={"candles": [[1, 2, 3, 4, 5, 6, None]]})
    refresher = _FakeRefresher([stale, fresh])
    slept: list[float] = []
    client = GrowwClient(
        sdk=stale,
        refresher=refresher,  # type: ignore[arg-type]
        sleep=slept.append,
        max_attempts=3,
        backoff_seconds=0.1,
    )
    out = client.historical_candles(
        exchange="NSE",
        segment="CASH",
        groww_symbol="NSE-NIFTY",
        interval="1minute",
        start="2024-01-01 09:15:00",
        end="2024-01-02 15:30:00",
    )
    assert out == {"candles": [[1, 2, 3, 4, 5, 6, None]]}  # the REFRESHED sdk's payload
    assert refresher.reauth_calls == 1  # exactly one re-mint
    assert stale.calls == 1  # the stale sdk was hit once (the auth failure)
    assert fresh.calls == 1  # the refreshed sdk served the retry
    # A re-auth retry does NOT spend the rate-limit backoff ladder — it pays ONLY the
    # small fixed propagation-lag pause (DEC-GROWW-AUTH-HALT-001 FP-1 hardening: give
    # the fresh token a beat to become fleet-valid before retrying).
    assert slept == [REAUTH_RETRY_BACKOFF_SECONDS]


def test_auth_error_bounded_reauths_then_raises_terminally() -> None:
    """A persistently-invalid credential re-mints AT MOST max_reauths times then raises.

    Bounded — no infinite loop: a genuinely-bad credential still quarantines (today's
    terminal behaviour) after the budget is spent.
    """
    from brutex.adapters.groww.errors import GrowwAuthError

    sdks = [
        _FakeSDK(raises=[GrowwAPIAuthenticationException("bad")]),
        _FakeSDK(raises=[GrowwAPIAuthenticationException("still bad")]),
        _FakeSDK(raises=[GrowwAPIAuthenticationException("never good")]),
    ]
    refresher = _FakeRefresher(sdks)
    client = GrowwClient(
        sdk=sdks[0],
        refresher=refresher,  # type: ignore[arg-type]
        sleep=lambda _s: None,
        max_attempts=3,
        max_reauths=2,
    )
    with pytest.raises(GrowwAuthError):
        client.expiries(exchange="NSE", underlying_symbol="NIFTY")
    assert refresher.reauth_calls == 2  # bounded at max_reauths (not infinite)
    # Each sdk auth-failed once: initial + 2 reauthed attempts = 3 physical calls total.
    assert sum(s.calls for s in sdks) == 3


def test_auth_error_without_refresher_raises_immediately_back_compat() -> None:
    """No refresher (offline / dry-run / tests) → an auth error is terminal, exactly as before."""
    from brutex.adapters.groww.errors import GrowwAuthError

    sdk = _FakeSDK(raises=[GrowwAPIAuthenticationException("token expired")])
    client, slept = _client(sdk)  # _client wires NO refresher
    with pytest.raises(GrowwAuthError):
        client.contracts(exchange="NSE", underlying_symbol="NIFTY", expiry_date="2024-01-25")
    assert sdk.calls == 1  # raised immediately, no retry, no reauth
    assert slept == []


def test_default_max_reauths_is_one() -> None:
    """The bounded re-auth budget defaults to 1 (one self-heal then terminal)."""
    sdks = [
        _FakeSDK(raises=[GrowwAPIAuthenticationException("bad")]),
        _FakeSDK(raises=[GrowwAPIAuthenticationException("still bad")]),
    ]
    refresher = _FakeRefresher(sdks)
    from brutex.adapters.groww.errors import GrowwAuthError

    client = GrowwClient(
        sdk=sdks[0],
        refresher=refresher,  # type: ignore[arg-type]
        sleep=lambda _s: None,
    )
    assert client.max_reauths == 1
    with pytest.raises(GrowwAuthError):
        client.expiries(exchange="NSE", underlying_symbol="NIFTY")
    assert refresher.reauth_calls == 1  # exactly the default single re-mint


def test_rotation_lag_second_reauth_heals_within_production_budget() -> None:
    """FP-1 regression (DEC-GROWW-AUTH-HALT-001): the 06:00 IST rotation lag self-heals.

    The rotation-edge shape: the refresh ADOPTS the Lambda's fresh token, but the
    first post-refresh retry still 401s (the fresh token is not yet fleet-valid —
    a propagation lag, vendor-UNVERIFIED). With the production budget
    ``GROWW_MAX_REAUTHS = 2`` the SECOND re-auth + retry succeeds and the pull
    CONTINUES — no terminal ``GrowwAuthError``, no pull-wide halt. With the old
    budget of 1 the second 401 exhausted the budget and (post-halt) killed the
    whole overnight pull. Each post-refresh retry pays the small fixed
    propagation-lag backoff via the injectable ``sleep``.
    """
    from brutex.adapters.groww.constants import GROWW_MAX_REAUTHS

    stale = _FakeSDK(raises=[GrowwAPIAuthenticationException("token expired")])
    # The refresh adopted the FRESH token, but the fleet hasn't propagated it yet:
    # the first retry 401s once more; the second re-auth lands on a valid view.
    fresh_lagging = _FakeSDK(raises=[GrowwAPIAuthenticationException("not fleet-valid yet")])
    fresh_valid = _FakeSDK(payload={"candles": [[1, 2, 3, 4, 5, 6, None]]})
    refresher = _FakeRefresher([stale, fresh_lagging, fresh_valid])
    slept: list[float] = []
    client = GrowwClient(
        sdk=stale,
        refresher=refresher,  # type: ignore[arg-type]
        sleep=slept.append,
        max_reauths=GROWW_MAX_REAUTHS,  # the production live-build-site budget (2)
    )
    out = client.historical_candles(
        exchange="NSE",
        segment="CASH",
        groww_symbol="NSE-NIFTY",
        interval="1minute",
        start="2024-01-01 09:15:00",
        end="2024-01-02 15:30:00",
    )
    assert out == {"candles": [[1, 2, 3, 4, 5, 6, None]]}  # the pull CONTINUES
    assert refresher.reauth_calls == 2  # rotation lag absorbed within the budget
    assert stale.calls == 1 and fresh_lagging.calls == 1 and fresh_valid.calls == 1
    # One propagation-lag pause per post-refresh retry — never the 429 ladder.
    assert slept == [REAUTH_RETRY_BACKOFF_SECONDS, REAUTH_RETRY_BACKOFF_SECONDS]


def test_reauth_backoff_constant_is_small_and_positive() -> None:
    """The propagation-lag pause is a small fixed beat — never a minutes-scale stall."""
    assert 0.0 < REAUTH_RETRY_BACKOFF_SECONDS <= 5.0


def test_default_sleep_binding_runs_reauth_backoff_with_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CodeQL-arity regression (PR #1070): the DEFAULT sleep binding takes the seconds arg.

    The scan flagged ``self.sleep(REAUTH_RETRY_BACKOFF_SECONDS)`` as "too many
    arguments; should be no more than 0" — its model binds a bare class-body
    function default as a method whose implicit ``self`` consumes the ``seconds``
    parameter. Runtime was always correct (a dataclass default lands as an
    INSTANCE attribute; a plain function never binds), and the default is now
    routed through ``field(default=...)`` so the model cannot mis-bind it. This
    pins the exact flagged configuration: a client built WITHOUT injecting
    ``sleep`` drives the post-re-mint backoff through the default
    ``_noop_sleep(seconds)`` — a zero-arg default would ``TypeError`` here, so
    the arity can never regress silently.
    """
    from brutex.adapters.groww.client import (
        _noop_sleep,  # pyright: ignore[reportPrivateUsage]  — whitebox: pin the DEFAULT binding
    )

    slept: list[float] = []
    # Intercept the real time.sleep the default sleeper delegates to (the repo's
    # standard module-path patch), so no real time passes.
    monkeypatch.setattr("brutex.adapters.groww.client.time.sleep", slept.append)
    stale = _FakeSDK(raises=[GrowwAPIAuthenticationException("token expired")])
    fresh = _FakeSDK(payload={"candles": [[1, 2, 3, 4, 5, 6, None]]})
    refresher = _FakeRefresher([stale, fresh])
    client = GrowwClient(sdk=stale, refresher=refresher)  # type: ignore[arg-type]
    assert client.sleep is _noop_sleep  # the DEFAULT binding — not an injected recorder
    out = client.expiries(exchange="NSE", underlying_symbol="NIFTY")
    assert out == {"candles": [[1, 2, 3, 4, 5, 6, None]]}  # the pull self-healed
    assert slept == [REAUTH_RETRY_BACKOFF_SECONDS]  # the flagged call ran, seconds accepted


def test_auth_error_whose_remint_times_out_surfaces_retryable_timeout() -> None:
    """A re-mint that HANGS (freeze guard fires) surfaces a RETRYABLE timeout, not a freeze.

    MINOR-1 of DEC-GROWW-TOKEN-REAUTH-001: when the 06:00 IST re-mint's network call hangs,
    the refresher's bounded mint raises a :class:`GrowwTimeoutError`. The client must let it
    propagate out of ``_call`` (a transient error the ingest quarantines + re-pulls) — NOT a
    terminal auth failure and NOT a hung call that freezes the pool.
    """

    auth_sdk = _FakeSDK(raises=[GrowwAPIAuthenticationException("token expired")])

    class _TimingOutRefresher:
        """A refresher whose re-mint always times out (the hung-mint freeze guard fired)."""

        def __init__(self, sdk: _FakeSDK) -> None:
            self.reauth_calls = 0
            self.sdk = sdk

        def sdk_at(self, _index: int) -> _FakeSDK:
            return self.sdk

        def current_sdk(self) -> _FakeSDK:  # pragma: no cover — leaf path uses sdk_at
            return self.sdk

        def current_generation(self) -> int:
            return 0

        def reauth(self, _seen_generation: int) -> int:
            self.reauth_calls += 1
            raise GrowwTimeoutError("TimeoutError: Groww call deadline exceeded (45s)")

    refresher = _TimingOutRefresher(auth_sdk)
    client = GrowwClient(
        sdk=auth_sdk,
        refresher=refresher,  # type: ignore[arg-type]
        sleep=lambda _s: None,
    )
    # The auth error triggers a re-mint; the re-mint times out → a retryable
    # GrowwTimeoutError propagates (the ingest then quarantines + re-pulls the window).
    with pytest.raises(GrowwTimeoutError):
        client.expiries(exchange="NSE", underlying_symbol="NIFTY")
    assert refresher.reauth_calls == 1  # tried exactly one bounded re-mint, then surfaced


def test_auth_error_whose_remint_is_throttled_propagates_retryable_no_recursion() -> None:
    """DEC-GROWW-TOKEN-DISK-CACHE-001 (B1/step-5): a THROTTLED re-mint surfaces a retryable
    throttle that propagates OUT of ``_call`` WITHOUT re-entering the reauth branch.

    When the forced re-mint is itself issuance-throttled, ``refresher.reauth`` raises a
    :class:`GrowwTokenIssuanceThrottled` (a GrowwAuthError subclass). It must NOT recurse
    into another reauth (that would loop); it propagates as a retryable error the ingest
    quarantines + re-pulls. Proven: reauth is called EXACTLY once.
    """
    from brutex.adapters.groww.errors import GrowwTokenIssuanceThrottled

    auth_sdk = _FakeSDK(raises=[GrowwAPIAuthenticationException("token expired")])

    class _ThrottlingRefresher:
        def __init__(self, sdk: _FakeSDK) -> None:
            self.reauth_calls = 0
            self.sdk = sdk

        def sdk_at(self, _index: int) -> _FakeSDK:
            return self.sdk

        def current_sdk(self) -> _FakeSDK:  # pragma: no cover — leaf path uses sdk_at
            return self.sdk

        def current_generation(self) -> int:
            return 0

        def reauth(self, _seen_generation: int) -> int:
            self.reauth_calls += 1
            raise GrowwTokenIssuanceThrottled(
                "Groww access-token issuance throttled: ***", safe_message="throttled"
            )

    refresher = _ThrottlingRefresher(auth_sdk)
    client = GrowwClient(
        sdk=auth_sdk,
        refresher=refresher,  # type: ignore[arg-type]
        sleep=lambda _s: None,
        max_reauths=1,
    )
    with pytest.raises(GrowwTokenIssuanceThrottled):
        client.expiries(exchange="NSE", underlying_symbol="NIFTY")
    assert refresher.reauth_calls == 1  # exactly ONE forced re-mint — NO infinite recursion


class _StillStaleReauthRefresher:
    """A read-only-broker-shaped fake: ``reauth`` is always still-stale.

    Models the DEC-TOKEN-STALE-PAUSE-001 surface the client's gate depends on:
    ``reauth`` raises :class:`GrowwTokenStillStaleError` (the Lambda has not minted
    yet) and ``await_token_recovery`` is scripted per test — either an ADOPTION (the
    fresh token landed within the pause: swap the SDK, bump the generation, return
    it) or a DEADLINE EXPIRY (re-raise the StillStale, the pre-gate propagation).
    """

    def __init__(self, sdk: _FakeSDK, *, recovered_sdk: _FakeSDK | None = None) -> None:
        self.reauth_calls = 0
        self.await_calls: list[dict[str, object]] = []
        self.sdk = sdk
        self._recovered_sdk = recovered_sdk
        self.generation = 0

    def sdk_at(self, _index: int) -> _FakeSDK:
        return self.sdk

    def current_sdk(self) -> _FakeSDK:  # pragma: no cover — leaf path uses sdk_at
        return self.sdk

    def current_generation(self) -> int:
        return self.generation

    def reauth(self, _seen_generation: int) -> int:
        self.reauth_calls += 1
        raise GrowwTokenStillStaleError(
            "read-only broker re-read returned no fresh token yet", safe_message="stale"
        )

    def await_token_recovery(
        self,
        seen_generation: int,
        *,
        wait_seconds: float | None = None,
        sleep: Callable[[float], None] = lambda _s: None,
        clock: Callable[[], float] = lambda: 0.0,
        method: str = "",
    ) -> int:
        self.await_calls.append(
            {
                "seen_generation": seen_generation,
                "wait_seconds": wait_seconds,
                "sleep": sleep,
                "clock": clock,
                "method": method,
            }
        )
        if self._recovered_sdk is None:
            # The DEADLINE-EXPIRED shape: the broker never minted — propagate as
            # before the gate existed (retryable; the window defers FAILED).
            raise GrowwTokenStillStaleError(
                "token-stale pause exhausted: still no fresh token", safe_message="stale"
            )
        # The ADOPTION shape: the Lambda's mint landed mid-pause.
        self.sdk = self._recovered_sdk
        self.generation = seen_generation + 1
        return self.generation


def test_readonly_reauth_still_stale_propagates_retryable_no_recursion() -> None:
    """DEC-BROKER-READONLY-REAUTH-REREAD-001 + DEC-TOKEN-STALE-PAUSE-001: a read-only
    re-read that is still stale enters the BOUNDED pause; when the pause deadline expires
    (a never-minting broker) the RETRYABLE error propagates OUT of ``_call`` WITHOUT
    re-entering the reauth branch — the pre-gate behaviour, preserved.

    On the shared-AWS read-only host the reauth RE-READS the shared token; if the Lambda
    has not minted the fresh one yet, ``refresher.reauth`` raises a
    :class:`GrowwTokenStillStaleError` (a GrowwAuthError subclass that ``is_retryable``).
    The client PAUSES on ``await_token_recovery`` (the token-stale gate); a deadline
    expiry propagates as a retryable error the ingest quarantines + re-pulls — never a
    restart, never an infinite loop. Proven: reauth AND the pause are each called
    EXACTLY once, and the pause received the client's injected sleep/clock + wait bound.
    """
    from brutex.adapters.groww.errors import is_retryable

    auth_sdk = _FakeSDK(raises=[GrowwAPIAuthenticationException("token expired")])
    refresher = _StillStaleReauthRefresher(auth_sdk)

    def _sleep(_s: float) -> None:
        pass

    client = GrowwClient(
        sdk=auth_sdk,
        refresher=refresher,  # type: ignore[arg-type]
        sleep=_sleep,
        max_reauths=1,
        token_stale_wait_seconds=42.0,
    )
    with pytest.raises(GrowwTokenStillStaleError) as exc:
        client.expiries(exchange="NSE", underlying_symbol="NIFTY")
    assert is_retryable(exc.value)  # the ingest quarantines + re-pulls later, never a restart
    assert refresher.reauth_calls == 1  # exactly ONE read-only re-read — NO infinite recursion
    assert len(refresher.await_calls) == 1  # the gate paused exactly once
    gate_call = refresher.await_calls[0]
    assert gate_call["seen_generation"] == 0
    assert gate_call["wait_seconds"] == 42.0  # the client's bound is threaded through
    assert gate_call["sleep"] is _sleep  # the client's injected sleeper drives the pause
    assert gate_call["clock"] is client.clock  # …and its monotonic clock
    assert gate_call["method"] == "get_expiries"


def test_token_stale_gate_adoption_retries_without_burning_attempts() -> None:
    """DEC-TOKEN-STALE-PAUSE-001: a fresh token adopted DURING the pause retries the call.

    The 06:00-IST fetch-lane pause: ``reauth`` is still-stale, the gate waits, the
    Lambda's mint lands mid-pause (the fake's adoption shape), and the SAME call retries
    against the refreshed SDK and SUCCEEDS — no error ever reaches the window ladder, so
    no window attempt burns. ``max_attempts=1`` proves the retry rides the reauth
    buy-back (``attempt -= 1``), not the transient-retry budget.
    """
    auth_sdk = _FakeSDK(raises=[GrowwAPIAuthenticationException("token expired")])
    fresh_sdk = _FakeSDK(payload={"expiries": ["2026-07-30"]})
    refresher = _StillStaleReauthRefresher(auth_sdk, recovered_sdk=fresh_sdk)
    client = GrowwClient(
        sdk=auth_sdk,
        refresher=refresher,  # type: ignore[arg-type]
        sleep=lambda _s: None,
        max_attempts=1,
        max_reauths=1,
    )
    out = client.expiries(exchange="NSE", underlying_symbol="NIFTY")
    assert out == {"expiries": ["2026-07-30"]}  # the ADOPTED sdk served the retry
    assert refresher.reauth_calls == 1
    assert len(refresher.await_calls) == 1
    assert refresher.await_calls[0]["wait_seconds"] is None  # default ⇒ env-resolved bound
    assert auth_sdk.calls == 1  # the stale sdk was hit once (the auth failure)
    assert fresh_sdk.calls == 1  # the pause never re-hit the vendor with the dead token


def test_with_pace_carries_the_refresher_through() -> None:
    """`with_pace` (dataclasses.replace) preserves the refresher + leaf index unchanged."""
    refresher = _FakeRefresher([_FakeSDK(payload={"candles": []})])
    client = GrowwClient(sdk=_FakeSDK(), refresher=refresher, refresher_index=2)  # type: ignore[arg-type]

    def _pace() -> None:
        pass

    repaced = client.with_pace(_pace)
    assert repaced.refresher is refresher  # the SAME shared refresher carries through
    assert repaced.refresher_index == 2  # the leaf binding is preserved
    assert repaced.pace is _pace


def test_reauth_resolves_the_refreshed_sdk_via_leaf_index() -> None:
    """The client resolves its SDK from the refresher BY ITS LEAF INDEX (pool correctness)."""

    class _IndexedRefresher:
        """A 2-leaf fleet that flips both leaves to a fresh SDK on reauth."""

        def __init__(self) -> None:
            self._fleets = [
                [_FakeSDK(raises=[GrowwAPIAuthenticationException("expired")]) for _ in range(2)],
                [_FakeSDK(payload={"leaf": 0}), _FakeSDK(payload={"leaf": 1})],
            ]
            self._gen = 0

        def sdk_at(self, index: int) -> _FakeSDK:
            return self._fleets[self._gen][index]

        def current_sdk(self) -> _FakeSDK:  # pragma: no cover — leaf path uses sdk_at
            return self._fleets[self._gen][0]

        def current_generation(self) -> int:
            return self._gen

        def reauth(self, seen_generation: int) -> int:
            if self._gen == seen_generation:
                self._gen += 1
            return self._gen

    refresher = _IndexedRefresher()
    # Leaf 1 must resolve leaf-1's refreshed SDK ({"leaf": 1}), proving index binding.
    client = GrowwClient(
        sdk=refresher.sdk_at(1),
        refresher=refresher,  # type: ignore[arg-type]
        refresher_index=1,
        sleep=lambda _s: None,
    )
    out = client.expiries(exchange="NSE", underlying_symbol="NIFTY")
    assert out == {"leaf": 1}  # resolved leaf 1, not leaf 0


def test_reauth_emits_observability_log() -> None:
    """The 06:00 IST self-heal is VISIBLE: a re-mint emits ONE ``groww.token.reauth`` WARNING
    carrying the bumped generation + the call label + the reauth count — so the operator can
    CONFIRM in /logs that the token reset self-healed, never logging the token/secret."""
    stale = _FakeSDK(raises=[GrowwAPIAuthenticationException("token expired or invalid")])
    fresh = _FakeSDK(payload={"candles": []})
    refresher = _FakeRefresher([stale, fresh])
    client = GrowwClient(
        sdk=stale,
        refresher=refresher,  # type: ignore[arg-type]
        sleep=lambda _s: None,
    )
    with capture_logs() as logs:
        client.expiries(exchange="NSE", underlying_symbol="NIFTY")
    reauth_logs = [e for e in logs if e["event"] == "groww.token.reauth"]
    assert len(reauth_logs) == 1  # exactly one re-mint, surfaced once
    entry = reauth_logs[0]
    assert entry["log_level"] == "warning"
    assert entry["method"] == "get_expiries"
    assert entry["reauth"] == 1
    assert entry["generation"] == 1  # the refresher bumped the generation
    # secret hygiene: the log carries ONLY safe metadata — no secret / access-token / totp value
    assert set(entry) == {"event", "log_level", "method", "reauth", "generation"}


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


# ════════════════════════════════════════════════════════════════════════
# Failed-attempt observability (DEC-PULL-TELEMETRY-TRIAD-001) — one
# ``groww.call`` outcome=failed line per FAILED attempt, real elapsed_ms.
# ════════════════════════════════════════════════════════════════════════


def _failed_calls(logs: list[dict[str, object]]) -> list[dict[str, object]]:
    """The captured ``groww.call`` events whose outcome is ``failed``."""
    return [e for e in logs if e["event"] == "groww.call" and e.get("outcome") == "failed"]


def test_terminal_failure_logs_one_failed_call_with_real_elapsed() -> None:
    """A terminal (non-retryable) SDK error logs EXACTLY one outcome=failed line whose
    ``elapsed_ms`` is the REAL wall-clock of the attempt (the shared success-path local
    stays 0.0 on failures — the 06:00 blip's burned calls become readable, not inferred)."""
    from brutex.adapters.groww.errors import GrowwError

    sdk = _FakeSDK(raises=[RuntimeError("boom")])  # → neutral terminal GrowwError
    times = iter([10.0, 10.3])  # started, failed-log read → 300 ms
    client = GrowwClient(sdk=sdk, sleep=lambda _s: None, clock=lambda: next(times))
    with capture_logs() as logs, pytest.raises(GrowwError):
        client.expiries(exchange="NSE", underlying_symbol="NIFTY")
    failed = _failed_calls(logs)
    assert len(failed) == 1
    assert failed[0]["method"] == "get_expiries"
    assert failed[0]["attempt"] == 1
    assert failed[0]["elapsed_ms"] == 300.0
    assert failed[0]["error_type"] == "GrowwError"
    # No success line — the call never completed.
    assert not [e for e in logs if e["event"] == "groww.call" and e.get("outcome") == "success"]


def test_retry_storm_logs_one_failed_line_per_attempt_then_success() -> None:
    """PER-ATTEMPT accounting (not per window): two timeouts + a success = two failed
    lines (attempts 1 + 2, ``error_type=GrowwTimeoutError``) + one outcome=success line
    — and the PULL PATH is byte-identical (same SDK calls, same backoffs, same payload,
    the Session-B parity pin: logging is the only delta)."""
    sdk = _FakeSDK(
        raises=[GrowwAPITimeoutException("t1"), GrowwAPITimeoutException("t2")],
        payload={"ok": True},
    )
    client, slept = _client(sdk)
    with capture_logs() as logs:
        out = client.expiries(exchange="NSE", underlying_symbol="NIFTY")
    assert out == {"ok": True}  # parity: same return value
    assert sdk.calls == 3  # parity: same physical call count
    assert slept == [0.1, 0.2]  # parity: same linear backoff sequence
    failed = _failed_calls(logs)
    assert [e["attempt"] for e in failed] == [1, 2]
    assert {e["error_type"] for e in failed} == {"GrowwTimeoutError"}
    success = [e for e in logs if e["event"] == "groww.call" and e.get("outcome") == "success"]
    assert len(success) == 1
    assert success[0]["attempt"] == 3
    # Meter parity + the new lifetime accounting: 3 issued = 1 completed + 2 failed.
    snap = client.meter.snapshot()
    assert snap["in_flight"] == 0
    assert (snap["issued"], snap["completed"], snap["failed"]) == (3, 1, 2)


def test_rate_limit_failure_logs_failed_line_and_still_fires_hook() -> None:
    """A 429 keeps firing ``on_rate_limit`` (the decremental contract — parity) AND now
    logs its failed attempt with the neutral ``GrowwRateLimitError`` class name."""
    hits: list[int] = []
    sdk = _FakeSDK(raises=[GrowwAPIRateLimitException("429")], payload={"ok": True})
    client = GrowwClient(
        sdk=sdk,
        sleep=lambda _s: None,
        on_rate_limit=lambda: hits.append(1),
        max_attempts=3,
    )
    with capture_logs() as logs:
        out = client.expiries(exchange="NSE", underlying_symbol="NIFTY")
    assert out == {"ok": True}
    assert hits == [1]  # parity: the shared-bucket decrement fired exactly once
    failed = _failed_calls(logs)
    assert len(failed) == 1
    assert failed[0]["error_type"] == "GrowwRateLimitError"


def test_reauth_attempt_logs_failed_line_before_the_self_heal() -> None:
    """The 06:00 auth-expiry attempt IS a failed attempt: one outcome=failed line
    (``GrowwAuthError``) lands alongside the existing ``groww.token.reauth`` WARNING,
    then the refreshed retry logs its success — one line per attempt, never per window."""
    stale = _FakeSDK(raises=[GrowwAPIAuthenticationException("token expired or invalid")])
    fresh = _FakeSDK(payload={"candles": []})
    refresher = _FakeRefresher([stale, fresh])
    client = GrowwClient(sdk=stale, refresher=refresher, sleep=lambda _s: None)  # type: ignore[arg-type]
    with capture_logs() as logs:
        client.expiries(exchange="NSE", underlying_symbol="NIFTY")
    failed = _failed_calls(logs)
    assert len(failed) == 1
    assert failed[0]["error_type"] == "GrowwAuthError"
    assert [e["event"] for e in logs].count("groww.token.reauth") == 1
    success = [e for e in logs if e["event"] == "groww.call" and e.get("outcome") == "success"]
    assert len(success) == 1  # the post-re-mint retry completed


def test_reauth_raise_path_still_logged_exactly_once_per_attempt() -> None:
    """When ``refresher.reauth`` itself raises (the issuance throttle), the attempt that
    TRIGGERED the re-mint was already logged failed — exactly one line, and the throttle
    error propagates out exactly as before (parity)."""
    from brutex.adapters.groww.errors import GrowwTokenIssuanceThrottled

    stale = _FakeSDK(raises=[GrowwAPIAuthenticationException("token expired or invalid")])

    class _ThrottledRefresher(_FakeRefresher):
        def reauth(self, seen_generation: int) -> int:
            raise GrowwTokenIssuanceThrottled("issuance throttled — cached token still stale")

    client = GrowwClient(
        sdk=stale,
        refresher=_ThrottledRefresher([stale]),  # type: ignore[arg-type]
        sleep=lambda _s: None,
    )
    with capture_logs() as logs, pytest.raises(GrowwTokenIssuanceThrottled):
        client.expiries(exchange="NSE", underlying_symbol="NIFTY")
    failed = _failed_calls(logs)
    assert len(failed) == 1  # the attempt logged ONCE; the reauth raise adds no second line
    assert failed[0]["error_type"] == "GrowwAuthError"
