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
from brutex.adapters.groww.live import build_live_client, build_live_pool
from brutex.adapters.groww.pool import ClientPool

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


def test_build_live_pool_builds_k_connections_sharing_one_meter() -> None:
    """size=3 → ONE token minted, 3 INDEPENDENT SDKs from it, all sharing ONE meter."""
    captured: list[str] = []

    def sdk_factory(token: str) -> _FakeSDK:
        captured.append(token)
        return _FakeSDK(token)

    pace_hits: list[str] = []
    pool = build_live_pool(
        _CREDS,
        size=3,
        token_factory=lambda _k, _t: "ACCESS-TOKEN",
        totp_minter=lambda _s: "123456",
        sdk_factory=sdk_factory,
        pace=lambda: pace_hits.append("p"),
    )
    assert isinstance(pool, ClientPool)
    assert pool.size == 3
    # The token is minted ONCE and reused for all three independent SDK connections.
    assert captured == ["ACCESS-TOKEN", "ACCESS-TOKEN", "ACCESS-TOKEN"]
    # Every leaf is a DISTINCT SDK instance, but they share the ONE aggregate meter.
    leaves = pool._clients
    assert len({id(leaf.sdk) for leaf in leaves}) == 3
    assert all(leaf.meter is pool.meter for leaf in leaves)
    assert all(leaf.pace is not None for leaf in leaves)


def test_build_live_pool_size_clamped_to_at_least_one() -> None:
    """A nonsensical size (0 / negative) clamps to a single-connection pool (fail-safe)."""
    pool = build_live_pool(
        _CREDS,
        size=0,
        token_factory=lambda _k, _t: "T",
        totp_minter=lambda _s: "000000",
        sdk_factory=_FakeSDK,
    )
    assert pool.size == 1


def test_build_live_pool_default_size_is_one_connection() -> None:
    """The default size (no arg) is a single connection — behaviour-identical to one client."""
    pool = build_live_pool(
        _CREDS,
        token_factory=lambda _k, _t: "T",
        totp_minter=lambda _s: "000000",
        sdk_factory=_FakeSDK,
    )
    assert pool.size == 1


def test_build_live_pool_token_issuance_failure_is_redacted() -> None:
    """A failed token issuance surfaces as a redacting GrowwAuthError (no secret leak)."""

    def token_factory(_api_key: str, _totp: str) -> str:
        raise RuntimeError("issuance down")

    with pytest.raises(GrowwAuthError) as exc:
        build_live_pool(
            _CREDS,
            size=2,
            token_factory=token_factory,
            totp_minter=lambda _s: "000000",
            sdk_factory=_FakeSDK,
        )
    assert "fake_secret" not in str(exc.value)
