"""The DEC-AWS-ONLY-CREDS-001 permutation matrix — every failure/success scenario.

Each test drives one scenario end-to-end through the REAL read-only broker
(:class:`ParameterStoreTokenStore`) + the REAL mint spine
(:class:`~brutex.adapters.groww.reauth.SdkRefresher`) against ``FakeAwsClient`` — no
boto3, no network.

The spine wiring (a refresher taking the store's ``load``/``store`` hooks) is exactly what
``build_live_client`` threads in production, so these prove the broker behaves under the
true cold-start-cache-first + forced-reauth semantics — and that BruteX, being READ-ONLY,
NEVER mints the shared token (a mint attempt fails LOUD). AWS is the SOLE token source; the
local disk backend was removed, so there is no local-mint path to test.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import cast

import pytest
from brutex.adapters.groww.auth import GrowwCredentials
from brutex.adapters.groww.constants import (
    GROWW_TOKEN_CACHE_PATH_ENV,
    GROWW_TOKEN_PARAM_ACCESS_TOKEN_DEFAULT,
)
from brutex.adapters.groww.reauth import SdkRefresher
from brutex.adapters.groww.sdk_protocol import GrowwSDK
from brutex.adapters.groww.token_store_aws import (
    ParameterStoreTokenStore,
    TokenBrokerReadOnlyError,
    build_readonly_token_store_with,
)

from tests.fakes.fake_aws_client import FakeAwsClient

_CREDS = GrowwCredentials(api_key="key-1", totp_secret="sec-1")
_NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
_TOKEN_PATH = GROWW_TOKEN_PARAM_ACCESS_TOKEN_DEFAULT


def _str_sdk(token: str) -> GrowwSDK:
    """SDK factory whose ``GrowwSDK`` IS the token string, for easy equality assertions.

    The refresher only stores + hands back whatever ``sdk_factory`` returns; a test never
    calls the SDK's methods, so the token string stands in (cast to satisfy the typed
    ``Callable[[str], GrowwSDK]`` seam).
    """
    return cast(GrowwSDK, token)


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A clean broker env + an isolated disk-cache path for every permutation test."""
    monkeypatch.setenv(GROWW_TOKEN_CACHE_PATH_ENV, str(tmp_path / "cache.json"))
    for name in (
        "BRUTEX_TOKEN_AWS_REGION",
        "AWS_REGION",
        "BRUTEX_TOKEN_PARAM_ACCESS_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)


def _store(fake: FakeAwsClient) -> ParameterStoreTokenStore:
    """Build the read-only Parameter-Store store around *fake* (concrete type)."""
    return build_readonly_token_store_with(fake)


def _refresher(store: ParameterStoreTokenStore, minter: Callable[[str, str], str]) -> SdkRefresher:
    """A real SdkRefresher wired to *store*'s hooks (as ``build_live_client`` does)."""
    return SdkRefresher(
        _CREDS,
        token_factory=minter,
        totp_minter=lambda _s: "000000",
        sdk_factory=_str_sdk,  # SDK == the token string (easy assertions)
        token_cache_load=store.load,
        token_cache_store=store.store,
        clock=lambda: _NOW,
    )


class _CountingMinter:
    """A token factory that counts mints + returns a fresh token each time."""

    def __init__(self) -> None:
        self.mints = 0

    def __call__(self, _api_key: str, _totp: str) -> str:
        self.mints += 1
        return f"MINT-{self.mints}"


class TestColdStartReadsSharedToken:
    """A cold start reads the Lambda-minted shared token with NO mint (the cross-app fix)."""

    def test_shared_token_served_without_mint(self) -> None:
        fake = FakeAwsClient(parameters={_TOKEN_PATH: "LAMBDA-TOKEN"})
        store = _store(fake)
        minter = _CountingMinter()
        refresher = _refresher(store, minter)
        assert refresher.current_sdk() == "LAMBDA-TOKEN"
        assert minter.mints == 0

    def test_two_apps_read_same_token_no_mint(self) -> None:
        """Two apps (BruteX + TickVault-shape) sharing the SSM param read the same token."""
        fake = FakeAwsClient(parameters={_TOKEN_PATH: "SHARED"})
        a_minter, b_minter = _CountingMinter(), _CountingMinter()
        assert _refresher(_store(fake), a_minter).current_sdk() == "SHARED"
        assert _refresher(_store(fake), b_minter).current_sdk() == "SHARED"
        assert a_minter.mints == 0 and b_minter.mints == 0


class TestReadOnlyMintBlocked:
    """BruteX is READ-ONLY: reaching a mint (absent shared token) fails LOUD, never silent."""

    def test_absent_token_mint_fails_fast(self) -> None:
        fake = FakeAwsClient()  # no shared token minted yet
        store = _store(fake)
        minter = _CountingMinter()
        refresher_ctor = lambda: _refresher(store, minter)  # noqa: E731 — deferred build for pytest.raises
        # The cold-start read is a miss ⇒ the spine tries to mint ⇒ the read-only store's
        # store() raises TokenBrokerReadOnlyError (loud, terminal) rather than a silent write.
        with pytest.raises(TokenBrokerReadOnlyError):
            refresher_ctor()

    def test_unreachable_token_mint_fails_fast(self) -> None:
        fake = FakeAwsClient(fail_get_parameter=True)  # SSM read raises ⇒ load miss
        store = _store(fake)
        with pytest.raises(TokenBrokerReadOnlyError):
            _refresher(store, _CountingMinter())

    def test_direct_store_call_raises_and_writes_nothing(self) -> None:
        fake = FakeAwsClient()
        with pytest.raises(TokenBrokerReadOnlyError):
            _store(fake).store(_CREDS, "TOK", now_utc=_NOW)
        assert fake.parameters == {}  # nothing ever written to SSM


class TestForcedReauthWithoutReReadHookStillFailsLoud:
    """A refresher WITHOUT the read-only re-read hook still fails LOUD on a forced reauth.

    This pins the SdkRefresher PRIMITIVE's legacy behaviour: when ``readonly_reread`` is NOT
    wired (a caller that only threads ``token_cache_load``/``store``), ``reauth`` FORCE-MINTS,
    which on a read-only store hits the fail-loud ``store`` (BruteX never writes the shared
    token). The PRODUCTION path (``build_live_client``) DOES wire the re-read hook so it
    self-heals with NO restart — proven in :class:`TestReadOnlyReauthSelfHeals` below
    (DEC-BROKER-READONLY-REAUTH-REREAD-001). HONEST envelope: the store cannot suppress the
    mint network CALL (the injected ``token_factory``), only the local WRITE — so this
    primitive-level path still surfaces a loud, non-silent failure.
    """

    def test_forced_reauth_fails_fast(self) -> None:
        fake = FakeAwsClient(parameters={_TOKEN_PATH: "TOKEN-V1"})
        store = _store(fake)
        minter = _CountingMinter()
        refresher = _refresher(store, minter)
        assert refresher.current_sdk() == "TOKEN-V1"
        # The token is rejected mid-day ⇒ a FORCED reauth reaches the read-only store's
        # store() persist hook, which fails LOUD (no silent local write).
        gen0 = refresher.current_generation()
        with pytest.raises(TokenBrokerReadOnlyError):
            refresher.reauth(gen0)
        # The mint CALL fired (store cannot suppress the factory), but the WRITE was blocked
        # loud — nothing was persisted to the shared parameter (BruteX never writes it).
        assert fake.parameters == {_TOKEN_PATH: "TOKEN-V1"}

    def test_fresh_cold_start_picks_up_lambda_reminted_token(self) -> None:
        """After the Lambda rotates the shared token, a NEW cold start reads V2 (no mint)."""
        fake = FakeAwsClient(parameters={_TOKEN_PATH: "TOKEN-V1"})
        assert _refresher(_store(fake), _CountingMinter()).current_sdk() == "TOKEN-V1"
        # The Lambda re-mints the shared token; a fresh refresher (new process) reads V2.
        fake.parameters[_TOKEN_PATH] = "TOKEN-V2"
        minter = _CountingMinter()
        assert _refresher(_store(fake), minter).current_sdk() == "TOKEN-V2"
        assert minter.mints == 0


def _readonly_refresher(
    store: ParameterStoreTokenStore, minter: Callable[[str, str], str]
) -> SdkRefresher:
    """A refresher wired EXACTLY as production (``build_live_client`` with a read-only store).

    ``readonly_reread`` is the SAME read-only store's ``load``, so a mid-run reauth RE-READS
    the shared token instead of minting — the DEC-BROKER-READONLY-REAUTH-REREAD-001 self-heal.
    """
    return SdkRefresher(
        _CREDS,
        token_factory=minter,
        totp_minter=lambda _s: "000000",
        sdk_factory=_str_sdk,
        token_cache_load=store.load,
        token_cache_store=store.store,
        readonly_reread=store.load,
        clock=lambda: _NOW,
    )


class TestReadOnlyReauthSelfHeals:
    """DEC-BROKER-READONLY-REAUTH-REREAD-001 — the read-only reauth RE-READS SSM, self-heals.

    The FIX for the shared-AWS gap: on a mid-run token rejection a read-only host no longer
    tries to re-mint (hitting the fail-loud store → a manual restart). It RE-READS the
    Lambda-minted token from SSM and adopts it — the process self-heals the 06:00-IST
    rotation with NO restart. The refresher is wired exactly as production
    (``build_live_client`` threads the read-only store's ``load`` as ``readonly_reread``).
    """

    def test_reauth_adopts_lambda_reminted_token_no_mint_no_restart(self) -> None:
        """BEFORE: reauth re-mints → fails loud → restart. AFTER: re-reads V2, self-heals."""
        fake = FakeAwsClient(parameters={_TOKEN_PATH: "TOKEN-V1"})
        store = _store(fake)
        minter = _CountingMinter()
        refresher = _readonly_refresher(store, minter)
        assert refresher.current_sdk() == "TOKEN-V1"
        # The Lambda rotates the shared token at ~06:05 IST; the current token is rejected.
        fake.parameters[_TOKEN_PATH] = "TOKEN-V2"
        new_gen = refresher.reauth(refresher.current_generation())
        assert new_gen == 1  # a real refresh (the re-read), NOT a no-op
        assert refresher.current_sdk() == "TOKEN-V2"  # the fresh Lambda token was ADOPTED
        assert minter.mints == 0  # BruteX NEVER minted — it re-read (read-only)
        assert fake.parameters == {_TOKEN_PATH: "TOKEN-V2"}  # BruteX never wrote SSM

    def test_reauth_still_stale_is_retryable_not_a_crash(self) -> None:
        """SSM still holds the SAME dead token (Lambda not minted yet) ⇒ retryable, no restart."""
        from brutex.adapters.groww.errors import GrowwTokenStillStaleError, is_retryable

        fake = FakeAwsClient(parameters={_TOKEN_PATH: "TOKEN-V1"})
        refresher = _readonly_refresher(_store(fake), _CountingMinter())
        with pytest.raises(GrowwTokenStillStaleError) as exc:
            refresher.reauth(refresher.current_generation())  # SSM unchanged
        assert is_retryable(exc.value)  # the window quarantines + re-pulls later
        assert refresher.current_generation() == 0  # never advanced, never looped


class TestReadDegradesNeverRaises:
    """The read path NEVER raises: SSM errors / absent / blank all resolve to a miss."""

    @pytest.mark.parametrize(
        "fake",
        [
            FakeAwsClient(),  # absent
            FakeAwsClient(parameters={_TOKEN_PATH: "   "}),  # blank
            FakeAwsClient(fail_get_parameter=True),  # IAM/region/network raise
            FakeAwsClient(param_not_found=frozenset({_TOKEN_PATH})),  # ParameterNotFound
        ],
    )
    def test_load_returns_none_for_every_failure(self, fake: FakeAwsClient) -> None:
        assert _store(fake).load(_CREDS, now_utc=_NOW) is None
