"""Groww live-client factory — injected composition (DEC-DHAN-GROWW-REMIGRATE-001).

Drives :func:`brutex.adapters.groww.live.build_live_client` with injected fakes
(``token_factory`` / ``totp_minter`` / ``sdk_factory``) so the whole
creds → token → SDK → client composition is 100% covered without ``growwapi`` /
``pyotp`` and without network.

The default daily-token backend is now the shared read-only AWS store (the SOLE source,
DEC-AWS-ONLY-CREDS-001); these composition tests fake it via ``_ephemeral_token_store`` so
the refresher mints through the injected ``token_factory`` (backend load ⇒ miss, store ⇒
no-op) exactly as the old local backend did — keeping the composition assertions intact
without any AWS/boto3.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from brutex.adapters.groww.auth import GrowwCredentials
from brutex.adapters.groww.client import GrowwClient
from brutex.adapters.groww.errors import GrowwAuthError
from brutex.adapters.groww.live import (
    KitMintBlockedError,
    build_live_client,
    build_live_pool,
    installed_growwapi_version,
    mint_blocked_token_factory,
    mint_blocked_totp_minter,
)
from brutex.adapters.groww.pool import ClientPool
from brutex.adapters.groww.token_cache import CachedToken

_CREDS = GrowwCredentials(api_key="fake_key", totp_secret="fake_secret")


class _EphemeralTokenStore:
    """A no-cache token store: ``load`` always misses ⇒ the refresher mints via its factory.

    ``readonly`` is ``False`` so the resolver keeps the LOCAL-mint reauth path (no read-only
    re-read hook wired), exactly the mint→SDK→client composition these tests exercise.
    """

    @property
    def readonly(self) -> bool:
        return False

    def load(self, creds: object, *, now_utc: datetime) -> CachedToken | None:
        return None

    def store(self, creds: object, token: str, *, now_utc: datetime) -> bool:
        return True


@pytest.fixture(autouse=True)
def _ephemeral_token_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fake the resolved daily-token backend so the composition mints via ``token_factory``.

    The default backend is now AWS-only + fail-fast; a composition test that does not inject
    explicit token hooks would otherwise hit the real AWS store. A no-cache fake keeps these
    tests exercising the mint→SDK→client path with NO AWS."""
    monkeypatch.setattr("brutex.adapters.groww.live.resolve_token_store", _EphemeralTokenStore)


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


def test_build_live_client_both_cache_hooks_injected_skip_resolve() -> None:
    """When BOTH token-cache hooks are injected, ``_resolve_token_cache_hooks``
    returns them verbatim WITHOUT resolving a token store (DEC-TOKEN-BROKER-AWS-001).

    Covers the both-hooks-provided early-return: a caller wiring a specific backend
    (e.g. a test double or a preconfigured broker) must have its exact ``(load, store)``
    threaded straight into the refresher, never overridden by ``resolve_token_store``.
    """
    load_calls: list[object] = []
    store_calls: list[object] = []

    def fake_load(*_a: object, **_k: object) -> CachedToken | None:
        load_calls.append(True)
        return None

    def fake_store(*_a: object, **_k: object) -> bool:
        store_calls.append(True)
        return True

    client = build_live_client(
        _CREDS,
        token_factory=lambda _k, _t: "ACCESS-TOKEN",
        totp_minter=lambda _s: "123456",
        sdk_factory=_FakeSDK,
        token_cache_load=fake_load,
        token_cache_store=fake_store,
    )
    assert isinstance(client, GrowwClient)
    # the injected load hook is the one the refresher consulted (proves the
    # explicit pair was threaded, not a resolved-store default)
    assert load_calls == [True]


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


# ── kit helpers: no-mint guard + growwapi version stamp (G2) ────────────────
# DEC-DOCTOR-LIVE-PROOF-KIT-001. Stem-matched HERE (the module's own per-file
# coverage gate): scripts/ci/coverage_gate_discovery.py maps
# brutex/adapters/groww/live.py -> tests/unit/test_groww_live*.py.


class TestLiveKitHelpers:
    def test_mint_blocked_token_factory_refuses(self) -> None:
        with pytest.raises(KitMintBlockedError, match="never mints a Groww token"):
            mint_blocked_token_factory("key", "123456")

    def test_mint_blocked_totp_minter_refuses(self) -> None:
        with pytest.raises(KitMintBlockedError, match="never mints a TOTP code"):
            mint_blocked_totp_minter("secret")

    def test_installed_growwapi_version_absent_is_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # DETERMINISTIC absence: the helper must map PackageNotFoundError -> None.
        # (growwapi is a MAIN dependency - pyproject growwapi>=1.0,<2.0 - so a
        # standard install HAS it; an env-dependent bare assert fails on any such
        # env, including CI. Simulate the absent-distribution case explicitly.)
        import importlib.metadata

        def _absent(_name: str) -> str:
            raise importlib.metadata.PackageNotFoundError("growwapi")

        monkeypatch.setattr(importlib.metadata, "version", _absent)
        assert installed_growwapi_version() is None

    def test_installed_growwapi_version_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import importlib.metadata

        monkeypatch.setattr(importlib.metadata, "version", lambda _name: "1.5.2")
        assert installed_growwapi_version() == "1.5.2"
