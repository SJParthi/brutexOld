"""Tests for `brutex.adapters.dhan.auth` — credentials + RenewToken refresh.

100% line+branch: env resolution (happy, missing each var, default os.environ
branch), RenewToken success + malformed responses, and token application.
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest
from brutex.adapters.dhan.auth import (
    ENV_ACCESS_TOKEN,
    ENV_CLIENT_ID,
    DhanCredentials,
    TokenInfo,
    renew_token,
    resolve_dhan_credentials,
    with_token,
)
from brutex.adapters.dhan.client import DhanClient
from brutex.adapters.dhan.errors import DhanAuthError


class _Transport:
    """Fake transport returning a canned (status, body)."""

    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.body = body

    def __call__(
        self, url: str, *, body: bytes, headers: Mapping[str, str], timeout: float
    ) -> tuple[int, bytes]:
        return self.status, self.body


def _client(status: int, body: bytes) -> DhanClient:
    return DhanClient(
        access_token="old",
        client_id="cid",
        transport=_Transport(status, body),  # type: ignore[arg-type]
        base_url="https://api.dhan.co/v2/",
    )


class TestResolveCredentials:
    def test_happy_path_from_mapping(self) -> None:
        creds = resolve_dhan_credentials({ENV_ACCESS_TOKEN: " jwt ", ENV_CLIENT_ID: " 100 "})
        assert creds == DhanCredentials(access_token="jwt", client_id="100")

    def test_missing_token_raises(self) -> None:
        with pytest.raises(DhanAuthError, match=ENV_ACCESS_TOKEN):
            resolve_dhan_credentials({ENV_CLIENT_ID: "100"})

    def test_missing_client_id_raises(self) -> None:
        with pytest.raises(DhanAuthError, match=ENV_CLIENT_ID):
            resolve_dhan_credentials({ENV_ACCESS_TOKEN: "jwt"})

    def test_default_env_branch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # env=None → reads os.environ (covers the `env is None` branch).
        monkeypatch.setenv(ENV_ACCESS_TOKEN, "envjwt")
        monkeypatch.setenv(ENV_CLIENT_ID, "envcid")
        creds = resolve_dhan_credentials()
        assert creds == DhanCredentials(access_token="envjwt", client_id="envcid")


class TestRenewToken:
    def test_success(self) -> None:
        body = b'{"accessToken": "newjwt", "expiryTime": "2026-01-01T00:00:00.000"}'
        info = renew_token(_client(200, body))
        assert info == TokenInfo(access_token="newjwt", expiry_time="2026-01-01T00:00:00.000")

    def test_missing_fields_raises(self) -> None:
        with pytest.raises(DhanAuthError, match="missing string accessToken"):
            renew_token(_client(200, b'{"accessToken": "x"}'))

    def test_non_string_fields_raises(self) -> None:
        with pytest.raises(DhanAuthError, match="missing string accessToken"):
            renew_token(_client(200, b'{"accessToken": 1, "expiryTime": 2}'))


class TestWithToken:
    def test_applies_new_token_keeps_client_id(self) -> None:
        creds = DhanCredentials(access_token="old", client_id="cid")
        updated = with_token(creds, TokenInfo(access_token="new", expiry_time="t"))
        assert updated == DhanCredentials(access_token="new", client_id="cid")
