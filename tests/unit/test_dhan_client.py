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
#: The subscription failure in the LIVE SDK's snake_case remark spelling (806/DH-902
#: is the subscription class post-relabel; DH-905 is a bad request — design §2).
_SUB_806 = {"status": "failure", "remarks": {"error_code": "806", "error_message": "no sub"}}
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
    # 806 in the live SDK's snake_case spelling — terminal, no retry, honest message.
    sdk = _FakeSdk(_SUB_806)
    client = _client({"A": sdk}, "A")
    with pytest.raises(DhanSubscriptionError) as excinfo:
        client.intraday_candles(security_id=13, from_date="a", to_date="b")
    assert sdk.calls == 1  # terminal — no retry
    assert "web.dhan.co" in str(excinfo.value)


def test_snake_case_807_drives_ssm_reread_self_heal() -> None:
    """The judge-found BLOCKER regression (design §0): a snake_case 807 failure —
    the LIVE ``dhanhq`` SDK envelope shape (``dhan_http.py`` ``_parse_response``
    spells the remark keys ``error_code``/``error_message``) — must classify as
    :class:`DhanAuthError` and DRIVE ``refresher.reauth`` (the read-only SSM
    re-read self-heal). This test FAILS on the pre-fix classifier, which read only
    the camelCase spellings: the code arm never fired, the failure fell to the
    terminal base :class:`DhanError`, and the advertised self-heal was
    production-dead (no re-read, no second SDK).
    """
    failure_807 = {
        "status": "failure",
        "remarks": {"error_code": "807", "error_message": "access token expired"},
    }
    sdks = {"A": _FakeSdk(failure_807), "B": _FakeSdk(_SUCCESS)}
    client = _client(sdks, "A", "B")  # cold-start A; the re-read adopts B
    data = client.intraday_candles(security_id=13, from_date="a", to_date="b")
    assert data == {"open": [1.0]}
    # The re-read actually FIRED: the refresher's generation advanced and the
    # second (fresh-token) SDK served the retry.
    assert client.refresher.current_generation() == 1
    assert sdks["B"].calls == 1


def test_string_remarks_tolerated_as_terminal_base() -> None:
    # On an SDK transport exception `remarks` is a plain STRING (dhan_http.py's
    # broad except) — the client tolerates the non-Mapping shape (design §0 pin).
    failure = {"status": "failure", "remarks": "Connection aborted"}
    sdk = _FakeSdk(failure)
    client = _client({"A": sdk}, "A", max_attempts=1)
    with pytest.raises(DhanError):
        client.intraday_candles(security_id=13, from_date="a", to_date="b")
    assert sdk.calls == 1


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


# ─────────────────────────────────────────────────────────────────────────────
# daily_candles — the NATIVE-daily twin on the SAME _call funnel
# (DEC-DHAN-NATIVE-DAILY-001, implementing DEC-SCOPE-MINIMAL-CORE-001)
# ─────────────────────────────────────────────────────────────────────────────


class _FakeDailySdk:
    """A fake SDK exposing ONLY ``historical_daily_data`` — records kwargs verbatim."""

    def __init__(self, *responses: object) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def historical_daily_data(self, **kwargs: object) -> Mapping[str, object]:
        self.calls.append(kwargs)
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response  # type: ignore[return-value]


def _daily_client(sdks_by_token: dict[str, _FakeDailySdk], *tokens: str) -> DhanClient:
    refresher = DhanSdkRefresher(
        client_id="C1",
        token_reread=_Reread(*tokens),
        sdk_factory=lambda _cid, tok: sdks_by_token[tok],  # type: ignore[arg-type,return-value]
        clock=lambda: _NOW,
        sleep=lambda _s: None,
    )
    return DhanClient(refresher=refresher)


def test_daily_candles_success_and_exact_sdk_kwargs() -> None:
    sdk = _FakeDailySdk(_SUCCESS)
    client = _daily_client({"A": sdk}, "A")
    data = client.daily_candles(security_id=13, from_date="2022-06-01", to_date="2022-06-30")
    assert data == {"open": [1.0]}
    # The SDK call shape: index defaults, oi=False ALWAYS, expiry_code NEVER passed.
    assert sdk.calls == [
        {
            "security_id": 13,
            "exchange_segment": "IDX_I",
            "instrument_type": "INDEX",
            "from_date": "2022-06-01",
            "to_date": "2022-06-30",
            "oi": False,
        }
    ]


def test_daily_candles_rides_the_same_dh901_reread_funnel() -> None:
    # A DH-901 on token A heals via the read-only SSM re-read (token B) — the SAME
    # _call funnel the intraday path uses; the daily retry lands on the new SDK.
    sdks = {"A": _FakeDailySdk(_DH901), "B": _FakeDailySdk(_SUCCESS)}
    client = _daily_client(sdks, "A", "B")
    data = client.daily_candles(security_id=13, from_date="a", to_date="b")
    assert data == {"open": [1.0]}
    assert len(sdks["A"].calls) == 1
    assert len(sdks["B"].calls) == 1


class _FakeRollingSdk:
    """A fake SDK exposing ONLY ``expired_options_data`` — records the body verbatim."""

    def __init__(self, *responses: object) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def expired_options_data(self, **kwargs: object) -> Mapping[str, object]:
        self.calls.append(kwargs)
        return self._responses.pop(0)  # type: ignore[return-value]


def test_rolling_option_data_rides_the_same_call_funnel() -> None:
    # The rolling-ATM twin: the VERBATIM camelCase body reaches the SDK unchanged
    # through the SAME bounded-retry `_call` funnel (coverage for the delegator).
    sdk = _FakeRollingSdk(_SUCCESS)
    refresher = DhanSdkRefresher(
        client_id="C1",
        token_reread=_Reread("A"),
        sdk_factory=lambda _cid, _tok: sdk,  # type: ignore[arg-type,return-value]
        clock=lambda: _NOW,
        sleep=lambda _s: None,
    )
    client = DhanClient(refresher=refresher)
    body = {"exchangeSegment": "IDX_I", "interval": "1"}
    assert client.rolling_option_data(body=body) == {"open": [1.0]}
    assert sdk.calls == [body]
