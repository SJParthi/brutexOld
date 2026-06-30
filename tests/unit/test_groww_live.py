"""Groww live-client factory — injected composition (DEC-DHAN-GROWW-REMIGRATE-001).

Drives :func:`brutex.adapters.groww.live.build_live_client` with injected fakes
(``token_factory`` / ``totp_minter`` / ``sdk_factory``) so the whole
creds → token → SDK → client composition is 100% covered without ``growwapi`` /
``pyotp`` and without network.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from brutex.adapters.groww.auth import GrowwCredentials
from brutex.adapters.groww.client import GrowwClient
from brutex.adapters.groww.constants import GROWW_TOKEN_CACHE_PATH_ENV
from brutex.adapters.groww.errors import GrowwAuthError
from brutex.adapters.groww.live import build_live_client, build_live_pool
from brutex.adapters.groww.pool import ClientPool

_CREDS = GrowwCredentials(api_key="fake_key", totp_secret="fake_secret")


@pytest.fixture(autouse=True)
def _isolated_token_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect the on-disk daily-token cache (DEC-GROWW-TOKEN-DISK-CACHE-001) at a clean
    tmp file so the cache-first cold-start mints fresh here (no real ``~/.brutex`` read)."""
    monkeypatch.setenv(GROWW_TOKEN_CACHE_PATH_ENV, str(tmp_path / "tok.json"))


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
        # the vendor message embeds the secret verbatim, so the scrub is
        # genuinely exercised through the build_live_client path (not a tautology)
        raise RuntimeError("issuance down for fake_secret")

    with pytest.raises(GrowwAuthError) as exc:
        build_live_client(
            _CREDS,
            token_factory=token_factory,
            totp_minter=lambda _s: "000000",
            sdk_factory=_FakeSDK,
        )
    # The auth layer wraps the failure with a redacting safe_message that now
    # surfaces the (sanitized) vendor reason — the operator sees WHY.
    assert str(exc.value).startswith("Groww access-token issuance failed: ")
    assert "issuance down" in str(exc.value)
    # the secret embedded in the vendor message is provably scrubbed end-to-end
    assert "fake_secret" not in str(exc.value)
    assert "***" in str(exc.value)


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


def test_build_live_pool_wires_same_hooks_on_every_leaf() -> None:
    """Every leaf carries the SAME injected pace + decrement/recover hook objects (not just one)."""

    def _pace() -> None:
        pass

    def _rl() -> None:
        pass

    def _ok() -> None:
        pass

    pool = build_live_pool(
        _CREDS,
        size=3,
        token_factory=lambda _k, _t: "T",
        totp_minter=lambda _s: "000000",
        sdk_factory=_FakeSDK,
        pace=_pace,
        on_rate_limit=_rl,
        on_success=_ok,
    )
    for leaf in pool._clients:
        assert leaf.pace is _pace
        assert leaf.on_rate_limit is _rl
        assert leaf.on_success is _ok


def test_build_live_pool_size_one_fetches_through_its_single_leaf() -> None:
    """A size-1 pool still fetches through its one leaf — behaviourally a single client."""
    pool = build_live_pool(
        _CREDS,
        size=1,
        token_factory=lambda _k, _t: "T",
        totp_minter=lambda _s: "000000",
        sdk_factory=_FakeSDK,
    )
    assert pool.size == 1
    out = pool.historical_candles(
        exchange="NSE",
        segment="CASH",
        groww_symbol="NSE-NIFTY",
        interval="1day",
        start="2024-01-01T00:00:00",
        end="2024-02-01T00:00:00",
    )
    assert out == {"candles": []}


# ── AUTH-error auto-RE-MINT wiring (DEC-GROWW-TOKEN-REAUTH-001) ──────────────


def test_build_live_client_wires_a_refresher_at_leaf_zero() -> None:
    """The single live client carries a SdkRefresher (the auto-RE-MINT latch) at index 0."""
    from brutex.adapters.groww.reauth import SdkRefresher

    client = build_live_client(
        _CREDS,
        token_factory=lambda _k, _t: "ACCESS-TOKEN",
        totp_minter=lambda _s: "123456",
        sdk_factory=_FakeSDK,
    )
    assert isinstance(client.refresher, SdkRefresher)
    assert client.refresher_index == 0
    # The client's SDK is the refresher's current one (the ONE mint path).
    assert client.sdk is client.refresher.current_sdk()


def test_build_live_pool_leaves_share_one_refresher_with_distinct_indices() -> None:
    """Every pool leaf shares the ONE refresher; each binds a DISTINCT leaf index.

    So a single 06:00 re-mint refreshes EVERY leaf at once, while the K leaves keep
    independent SDK connections (distinct indices into the refresher's fleet).
    """
    pool = build_live_pool(
        _CREDS,
        size=3,
        token_factory=lambda _k, _t: "ACCESS-TOKEN",
        totp_minter=lambda _s: "123456",
        sdk_factory=_FakeSDK,
    )
    leaves = pool._clients
    refreshers = {id(leaf.refresher) for leaf in leaves}
    assert len(refreshers) == 1  # ONE shared refresher across all leaves
    assert leaves[0].refresher is not None
    assert leaves[0].refresher.size == 3  # the refresher fans 3 independent connections
    # Each leaf binds a distinct index → distinct SDK connections (K-way concurrency kept).
    assert sorted(leaf.refresher_index for leaf in leaves) == [0, 1, 2]
    assert len({id(leaf.sdk) for leaf in leaves}) == 3
