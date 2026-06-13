"""Unit tests for ``brutex.adapters.groww.client`` (DEC-DHAN-GROWW-REMIGRATE-001 / PR-F).

A fake SDK drives every retry branch — no ``growwapi``, no network, no real sleep.
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest
from brutex.adapters.groww.client import GrowwClient
from brutex.adapters.groww.errors import GrowwRateLimitError, GrowwTimeoutError


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


def test_rate_limit_is_not_fast_retried() -> None:
    sdk = _FakeSDK(raises=[GrowwAPIRateLimitException("429")])
    client, slept = _client(sdk)
    with pytest.raises(GrowwRateLimitError):
        client.historical_candles(
            exchange="NSE",
            segment="CASH",
            groww_symbol="NSE-NIFTY",
            interval="1minute",
            start="s",
            end="e",
        )
    assert sdk.calls == 1  # raised immediately — the bucket owns 429 backoff
    assert slept == []


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
