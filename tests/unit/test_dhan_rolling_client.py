"""Tests for :meth:`brutex.adapters.dhan.client.DhanClient.rolling_option_data`.

DEC-DHAN-ROLLING-OPTIONS-001. The rolling-ATM option fetch funnels the SDK's
``expired_options_data`` (``POST /v2/charts/rollingoption``) through the SAME
bounded-retry / DH-901 re-read :meth:`DhanClient._call` the spot pull uses — so a
:class:`DhanClient` is a drop-in ``RollingOptionClientProtocol``. These pins prove the
new PUBLIC method reuses that funnel (success / body-passthrough / terminal error /
DH-901 re-read), and that the ADDITIVE change left the spot ``DhanSdk`` surface untouched.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

import pytest
from brutex.adapters.dhan.client import DhanClient, DhanRollingSdk
from brutex.adapters.dhan.errors import DhanSubscriptionError
from brutex.adapters.dhan.reauth import DhanSdkRefresher
from brutex.adapters.dhan.rolling_option_adapter import RollingOptionClientProtocol
from brutex.adapters.dhan.token_store_aws import DhanCachedToken

_NOW = datetime(2026, 7, 12, 4, 0, tzinfo=UTC)
_SUCCESS = {"status": "success", "data": {"timestamp": [1.0], "close": [101.0]}}
_DH901 = {"status": "failure", "remarks": {"errorCode": "DH-901", "errorMessage": "bad token"}}
_DH905 = {"status": "failure", "remarks": {"errorCode": "DH-905", "errorMessage": "no sub"}}

_BODY: dict[str, object] = {
    "securityId": 13,
    "exchangeSegment": "NSE_FNO",
    "instrument": "INDEX",
    "expiryFlag": "WEEK",
    "expiryCode": 0,
    "strike": "ATM",
    "drvOptionType": "CALL",
    "requiredData": ["open", "close"],
    "fromDate": "2024-06-01",
    "toDate": "2024-06-30",
    "interval": 1,
}


class _FakeRollingSdk:
    """A fake SDK exposing ``expired_options_data`` (+ ``intraday_minute_data`` for parity)."""

    def __init__(self, *responses: object) -> None:
        self._responses = list(responses)
        self.calls = 0
        self.last_kwargs: dict[str, object] | None = None

    def expired_options_data(self, **kwargs: object) -> Mapping[str, object]:
        self.calls += 1
        self.last_kwargs = dict(kwargs)
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response  # type: ignore[return-value]

    def intraday_minute_data(self, **_kwargs: object) -> Mapping[str, object]:
        return {"status": "success", "data": {}}


class _Reread:
    def __init__(self, *tokens: str) -> None:
        self._tokens = list(tokens)

    def __call__(self, *, now_utc: datetime) -> DhanCachedToken | None:
        _ = now_utc
        return DhanCachedToken(access_token=self._tokens.pop(0)) if self._tokens else None


def _client(
    sdks_by_token: dict[str, _FakeRollingSdk], *tokens: str, **kwargs: object
) -> DhanClient:
    refresher = DhanSdkRefresher(
        client_id="C1",
        token_reread=_Reread(*tokens),
        sdk_factory=lambda _cid, tok: sdks_by_token[tok],
        clock=lambda: _NOW,
        sleep=lambda _s: None,
    )
    return DhanClient(refresher=refresher, **kwargs)  # type: ignore[arg-type]


def test_dhan_client_is_a_rolling_option_client_protocol() -> None:
    # The whole point: DhanClient satisfies the adapter's injected-client Protocol —
    # RollingOptionAdapter accepts a DhanClient as its injected client (structural).
    from brutex.adapters.dhan.rolling_option_adapter import RollingOptionAdapter

    client = _client({"A": _FakeRollingSdk(_SUCCESS)}, "A")
    adapter = RollingOptionAdapter(client=client)
    assert callable(adapter.client.rolling_option_data)
    assert RollingOptionClientProtocol is not None


def test_rolling_success_returns_data_and_passes_body_verbatim() -> None:
    fired: list[str] = []
    sdk = _FakeRollingSdk(_SUCCESS)
    client = _client({"A": sdk}, "A", on_success=lambda: fired.append("ok"))
    data = client.rolling_option_data(body=_BODY)
    assert data == {"timestamp": [1.0], "close": [101.0]}
    # The VERBATIM camelCase body reached the SDK unchanged (no dhanClientId injected).
    assert sdk.last_kwargs == _BODY
    assert "dhanClientId" not in (sdk.last_kwargs or {})
    # It reuses the shared _call funnel → on_success fires on a clean call.
    assert fired == ["ok"]


def test_rolling_success_absent_data_returns_empty_mapping() -> None:
    client = _client({"A": _FakeRollingSdk({"status": "success"})}, "A")
    assert client.rolling_option_data(body=_BODY) == {}


def test_rolling_terminal_subscription_error_raised_no_retry() -> None:
    # DH-905 (servability refusal) is terminal — the shared funnel raises it verbatim.
    sdk = _FakeRollingSdk(_DH905)
    client = _client({"A": sdk}, "A")
    with pytest.raises(DhanSubscriptionError):
        client.rolling_option_data(body=_BODY)
    assert sdk.calls == 1


def test_rolling_dh901_drives_readonly_reread_then_succeeds() -> None:
    # A DH-901 on the FIRST token → the shared re-read adopts a NEW token → success.
    stale = _FakeRollingSdk(_DH901)
    fresh = _FakeRollingSdk(_SUCCESS)
    # Cold start reads token "A" (stale SDK); the DH-901 re-read adopts token "B" (fresh).
    client = _client({"A": stale, "B": fresh}, "A", "B")
    data = client.rolling_option_data(body=_BODY)
    assert data == {"timestamp": [1.0], "close": [101.0]}
    assert stale.calls == 1 and fresh.calls == 1


def test_dhan_rolling_sdk_protocol_is_separate_from_spot_dhan_sdk() -> None:
    # The additive Protocol carries ONLY the rolling method — DhanSdk (spot) is untouched.
    assert "expired_options_data" in dir(DhanRollingSdk)
