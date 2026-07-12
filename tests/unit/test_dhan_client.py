"""Tests for :mod:`brutex.adapters.dhan.client` — the retry + DH-901 re-read funnel."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

import pytest
from brutex.adapters.dhan.client import (
    DhanClient,
    client_self_paces,
    client_self_reports_rate_limits,
    client_self_reports_successes,
)
from brutex.adapters.dhan.errors import (
    DhanAuthError,
    DhanEmptyResponseError,
    DhanError,
    DhanSubscriptionError,
)
from brutex.adapters.dhan.reauth import DhanSdkRefresher
from brutex.adapters.dhan.token_store_aws import DhanCachedToken

_NOW = datetime(2026, 7, 12, 4, 0, tzinfo=UTC)
_SUCCESS = {"status": "success", "data": {"open": [1.0]}}
_DH901 = {"status": "failure", "remarks": {"errorCode": "DH-901", "errorMessage": "bad token"}}
_DH905 = {"status": "failure", "remarks": {"errorCode": "DH-905", "errorMessage": "no sub"}}
_EMPTY = {"status": "failure", "remarks": {"errorMessage": "Expecting value"}}


class _FakeSdk:
    def __init__(self, *responses: object) -> None:
        self._responses = list(responses)
        self.calls = 0

    def intraday_minute_data(self, **_kwargs: object) -> Mapping[str, object]:
        self.calls += 1
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response  # type: ignore[return-value]


class _Reread:
    def __init__(self, *tokens: str) -> None:
        self._tokens = list(tokens)

    def __call__(self, *, now_utc: datetime) -> DhanCachedToken | None:
        _ = now_utc
        return DhanCachedToken(access_token=self._tokens.pop(0)) if self._tokens else None


def _client(sdks_by_token: dict[str, _FakeSdk], *tokens: str, **kwargs: object) -> DhanClient:
    refresher = DhanSdkRefresher(
        client_id="C1",
        token_reread=_Reread(*tokens),
        sdk_factory=lambda _cid, tok: sdks_by_token[tok],
        clock=lambda: _NOW,
        sleep=lambda _s: None,
    )
    return DhanClient(refresher=refresher, **kwargs)  # type: ignore[arg-type]


def test_success_returns_data_and_fires_on_success() -> None:
    fired: list[str] = []
    client = _client({"A": _FakeSdk(_SUCCESS)}, "A", on_success=lambda: fired.append("ok"))
    data = client.intraday_candles(security_id=13, from_date="2022-06-01", to_date="2022-06-30")
    assert data == {"open": [1.0]}
    assert fired == ["ok"]


def test_success_with_absent_data_returns_empty_mapping() -> None:
    client = _client({"A": _FakeSdk({"status": "success"})}, "A")
    assert client.intraday_candles(security_id=13, from_date="a", to_date="b") == {}


def test_terminal_subscription_error_raised_no_retry() -> None:
    sdk = _FakeSdk(_DH905)
    client = _client({"A": sdk}, "A")
    with pytest.raises(DhanSubscriptionError):
        client.intraday_candles(security_id=13, from_date="a", to_date="b")
    assert sdk.calls == 1  # terminal — no retry


def test_retryable_empty_body_exhausts_budget() -> None:
    slept: list[float] = []
    rate_hits: list[int] = []
    sdk = _FakeSdk(_EMPTY, _EMPTY)
    client = _client(
        {"A": sdk},
        "A",
        max_attempts=2,
        backoff_seconds=0.0,
        sleep=slept.append,
        on_rate_limit=lambda: rate_hits.append(1),
    )
    with pytest.raises(DhanEmptyResponseError):
        client.intraday_candles(security_id=13, from_date="a", to_date="b")
    assert sdk.calls == 2  # one retry
    assert slept == [0.0]  # slept once between the two attempts
    assert rate_hits == []  # empty body is NOT a rate-limit


def test_throttle_then_success_retries_and_feeds_rate_hook() -> None:
    throttle = {"status": "failure", "remarks": {"errorType": "rate limit", "errorMessage": "slow"}}
    rate_hits: list[int] = []
    sdk = _FakeSdk(throttle, _SUCCESS)
    client = _client(
        {"A": sdk},
        "A",
        max_attempts=3,
        backoff_seconds=0.0,
        sleep=lambda _s: None,
        on_rate_limit=lambda: rate_hits.append(1),
    )
    data = client.intraday_candles(security_id=13, from_date="a", to_date="b")
    assert data == {"open": [1.0]}
    assert rate_hits == [1]


def test_dh901_triggers_reread_then_success() -> None:
    sdks = {"A": _FakeSdk(_DH901), "B": _FakeSdk(_SUCCESS)}
    client = _client(sdks, "A", "B")  # cold-start A, reauth reads B
    data = client.intraday_candles(security_id=13, from_date="a", to_date="b")
    assert data == {"open": [1.0]}
    assert client.refresher.current_generation() == 1


def test_dh901_persists_after_reread_is_terminal() -> None:
    sdks = {"A": _FakeSdk(_DH901), "B": _FakeSdk(_DH901)}
    client = _client(sdks, "A", "B")
    with pytest.raises(DhanAuthError):
        client.intraday_candles(security_id=13, from_date="a", to_date="b")


def test_reauth_on_final_attempt_exhausts_loop_and_raises() -> None:
    # max_attempts=1: a DH-901 on the only attempt re-reads but the budget is spent,
    # so the loop exits and the post-loop raise fires (a degenerate budget edge).
    sdks = {"A": _FakeSdk(_DH901), "B": _FakeSdk(_SUCCESS)}
    client = _client(sdks, "A", "B", max_attempts=1)
    with pytest.raises(DhanAuthError):
        client.intraday_candles(security_id=13, from_date="a", to_date="b")


def test_transport_raise_maps_to_terminal_dhan_error() -> None:
    sdk = _FakeSdk(RuntimeError("connection reset"))
    client = _client({"A": sdk}, "A", max_attempts=1)
    with pytest.raises(DhanError):
        client.intraday_candles(security_id=13, from_date="a", to_date="b")


def test_pace_hook_fired_per_attempt() -> None:
    paces: list[int] = []
    client = _client({"A": _FakeSdk(_SUCCESS)}, "A", pace=lambda: paces.append(1))
    client.intraday_candles(security_id=13, from_date="a", to_date="b")
    assert paces == [1]


def test_with_rate_hooks_rebinds() -> None:
    client = _client({"A": _FakeSdk(_SUCCESS)}, "A")
    hits: list[str] = []
    rebound = client.with_rate_hooks(pace=lambda: hits.append("p"))
    rebound.intraday_candles(security_id=13, from_date="a", to_date="b")
    assert hits == ["p"]
    # A None hook keeps the prior binding (never downgraded).
    kept = rebound.with_rate_hooks(on_success=lambda: hits.append("s"))
    assert kept.pace is rebound.pace


def test_client_self_predicates_are_false() -> None:
    client = _client({"A": _FakeSdk(_SUCCESS)}, "A")
    assert client_self_paces(client) is False
    assert client_self_reports_rate_limits(client) is False
    assert client_self_reports_successes(client) is False
