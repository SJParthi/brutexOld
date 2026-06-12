"""Groww live-client factory — injected composition (DEC-DHAN-GROWW-REMIGRATE-001).

Drives :func:`brutex.adapters.groww.live.build_live_client` with injected fakes
(``token_factory`` / ``totp_minter`` / ``sdk_factory``) so the whole
creds → token → SDK → client composition is 100% covered without ``growwapi`` /
``pyotp`` and without network.
"""

from __future__ import annotations

import pytest
from brutex.adapters.groww.auth import GrowwCredentials
from brutex.adapters.groww.client import GrowwClient
from brutex.adapters.groww.errors import GrowwAuthError
from brutex.adapters.groww.live import build_live_client

_CREDS = GrowwCredentials(api_key="fake_key", totp_secret="fake_secret")


class _FakeSDK:
    """A minimal duck-typed SDK carrying the token it was built from."""

    def __init__(self, token: str) -> None:
        self.token = token

    def get_historical_candles(self, **_kwargs: object) -> dict[str, object]:
        return {"candles": []}

    def get_expiries(self, **_kwargs: object) -> dict[str, object]:
        return {"expiries": []}

    def get_contracts(self, **_kwargs: object) -> dict[str, object]:
        return {"contracts": []}


def test_build_live_client_with_all_injections() -> None:
    minted: list[str] = []

    def totp_minter(secret: str) -> str:
        minted.append(secret)
        return "123456"

    def token_factory(api_key: str, totp: str) -> str:
        assert api_key == "fake_key"
        assert totp == "123456"
        return "ACCESS-TOKEN"

    captured: list[str] = []

    def sdk_factory(token: str) -> _FakeSDK:
        captured.append(token)
        return _FakeSDK(token)

    client = build_live_client(
        _CREDS,
        token_factory=token_factory,
        totp_minter=totp_minter,
        sdk_factory=sdk_factory,
    )
    assert isinstance(client, GrowwClient)
    assert isinstance(client.sdk, _FakeSDK)
    assert client.sdk.token == "ACCESS-TOKEN"
    assert minted == ["fake_secret"]
    assert captured == ["ACCESS-TOKEN"]


def test_build_live_client_token_issuance_failure_is_redacted() -> None:
    """A failed token issuance surfaces as a redacting GrowwAuthError (no secret leak)."""

    def token_factory(_api_key: str, _totp: str) -> str:
        raise RuntimeError("issuance down")

    with pytest.raises(GrowwAuthError) as exc:
        build_live_client(
            _CREDS,
            token_factory=token_factory,
            totp_minter=lambda _s: "000000",
            sdk_factory=_FakeSDK,
        )
    # The auth layer wraps the failure with a redacting safe_message.
    assert str(exc.value) == "Groww access-token issuance failed"
    assert "fake_secret" not in str(exc.value)
