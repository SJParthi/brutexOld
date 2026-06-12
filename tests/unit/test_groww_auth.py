"""Unit tests for ``brutex.adapters.groww.auth`` (DEC-DHAN-GROWW-REMIGRATE-001 / PR-E).

All side effects are injected, so no ``pyotp`` / ``growwapi`` is needed.
"""

from __future__ import annotations

import pytest
from brutex.adapters.groww import auth
from brutex.adapters.groww.errors import GrowwAuthError

_ENV = {auth.ENV_API_KEY: "key-123", auth.ENV_TOTP_SECRET: "secret-xyz"}


def test_resolve_credentials_from_env() -> None:
    creds = auth.resolve_groww_credentials(_ENV)
    assert creds.api_key == "key-123"
    assert creds.totp_secret == "secret-xyz"


@pytest.mark.parametrize(
    "env",
    [
        {},
        {auth.ENV_API_KEY: "k"},  # missing secret
        {auth.ENV_TOTP_SECRET: "s"},  # missing key
        {auth.ENV_API_KEY: "  ", auth.ENV_TOTP_SECRET: "s"},  # blank key
    ],
)
def test_resolve_credentials_missing_raises(env: dict[str, str]) -> None:
    with pytest.raises(GrowwAuthError, match="missing Groww credentials"):
        auth.resolve_groww_credentials(env)


def test_creds_present_true_and_false() -> None:
    assert auth.groww_creds_present(_ENV) is True
    assert auth.groww_creds_present({}) is False


def test_get_access_token_happy_path() -> None:
    creds = auth.GrowwCredentials(api_key="key-123", totp_secret="secret-xyz")
    seen: dict[str, str] = {}

    def fake_totp(secret: str) -> str:
        seen["secret"] = secret
        return "654321"

    def fake_factory(api_key: str, totp: str) -> str:
        seen["api_key"] = api_key
        seen["totp"] = totp
        return "ACCESS-TOKEN"

    info = auth.get_access_token(creds, token_factory=fake_factory, totp_minter=fake_totp)
    assert info.access_token == "ACCESS-TOKEN"
    # the minted code is actually threaded through to the factory (kills a drop-totp mutant)
    assert seen == {"secret": "secret-xyz", "api_key": "key-123", "totp": "654321"}


def test_get_access_token_wraps_and_redacts_on_failure() -> None:
    creds = auth.GrowwCredentials(api_key="key-123", totp_secret="secret-xyz")

    def boom_factory(_api_key: str, _totp: str) -> str:
        raise RuntimeError("vendor exploded with secret-xyz in the message")

    with pytest.raises(GrowwAuthError) as excinfo:
        auth.get_access_token(creds, token_factory=boom_factory, totp_minter=lambda _s: "111111")
    # str(exc) must be the redacted safe_message — never the secret-bearing detail
    assert str(excinfo.value) == "Groww access-token issuance failed"
    assert "secret-xyz" not in str(excinfo.value)
