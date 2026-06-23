"""Unit tests for ``brutex.adapters.groww.reauth`` (DEC-GROWW-TOKEN-REAUTH-001).

Injected fakes (``token_factory`` / ``totp_minter`` / ``sdk_factory``) drive every
branch of the thread-safe single-flight :class:`SdkRefresher` — the initial mint, the
re-mint generation bump, the single-flight no-second-mint race, the per-leaf SDK fleet,
and the redacting mint-failure path — with no ``growwapi`` / ``pyotp`` and no network.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor

import pytest
from brutex.adapters.groww.auth import GrowwCredentials
from brutex.adapters.groww.errors import GrowwAuthError, GrowwTimeoutError
from brutex.adapters.groww.reauth import SdkRefresher
from brutex.adapters.groww.sdk_protocol import GrowwSDK

_CREDS = GrowwCredentials(api_key="fake_key", totp_secret="fake_secret")


class _FakeSDK:
    """A minimal duck-typed SDK carrying the token it was built from + a serial id."""

    _serial = 0

    def __init__(self, token: str) -> None:
        self.token = token
        type(self)._serial += 1
        self.serial = type(self)._serial

    def get_historical_candles(self, **_kw: object) -> Mapping[str, object]:  # pragma: no cover
        return {"candles": []}

    def get_expiries(self, **_kw: object) -> Mapping[str, object]:  # pragma: no cover
        return {"expiries": []}

    def get_contracts(self, **_kw: object) -> Mapping[str, object]:  # pragma: no cover
        return {"contracts": []}


def _counting_factories() -> tuple[
    list[str],
    list[str],
    list[str],
    Callable[[str], str],
    Callable[[str, str], str],
    Callable[[str], GrowwSDK],
]:
    """Return recording (totp_minter, token_factory, sdk_factory) + their call logs."""
    minted: list[str] = []
    tokens: list[str] = []
    built: list[str] = []

    def totp_minter(secret: str) -> str:
        minted.append(secret)
        return "123456"

    def token_factory(api_key: str, totp: str) -> str:
        tokens.append(f"{api_key}:{totp}:{len(tokens)}")
        return f"ACCESS-{len(tokens)}"

    def sdk_factory(token: str) -> GrowwSDK:
        built.append(token)
        return _FakeSDK(token)

    return minted, tokens, built, totp_minter, token_factory, sdk_factory


def test_initial_mint_builds_one_sdk_via_injected_factories() -> None:
    """Constructing the refresher mints ONE token + ONE SDK (size=1, generation 0)."""
    minted, _tokens, built, totp_minter, token_factory, sdk_factory = _counting_factories()
    refresher = SdkRefresher(
        _CREDS,
        token_factory=token_factory,
        totp_minter=totp_minter,
        sdk_factory=sdk_factory,
    )
    assert refresher.size == 1
    assert refresher.current_generation() == 0
    sdk = refresher.current_sdk()
    assert isinstance(sdk, _FakeSDK)
    assert sdk.token == "ACCESS-1"
    assert minted == ["fake_secret"]  # the secret reached the minter exactly once
    assert built == ["ACCESS-1"]  # one SDK built off the issued token


def test_reauth_bumps_generation_and_returns_a_fresh_sdk() -> None:
    """A re-auth from the seen generation mints a NEW token+SDK and bumps the generation."""
    _minted, _tokens, built, totp_minter, token_factory, sdk_factory = _counting_factories()
    refresher = SdkRefresher(
        _CREDS,
        token_factory=token_factory,
        totp_minter=totp_minter,
        sdk_factory=sdk_factory,
    )
    first = refresher.current_sdk()
    assert len(built) == 1  # only the initial mint so far

    new_gen = refresher.reauth(seen_generation=0)
    assert new_gen == 1
    assert refresher.current_generation() == 1
    second = refresher.current_sdk()
    assert second is not first  # a genuinely fresh SDK instance
    assert isinstance(second, _FakeSDK)  # narrow GrowwSDK → the fake before .token
    assert second.token == "ACCESS-2"  # a fresh token was issued
    assert len(built) == 2  # the sdk_factory was called a SECOND time (one re-mint)


def test_reauth_is_single_flight_two_callers_one_mint() -> None:
    """Two callers with the SAME seen_generation cause exactly ONE re-mint (no stampede).

    The first caller bumps the generation; the second — still passing the now-stale
    generation 0 — sees the generation has moved and gets the CURRENT SDK with NO
    second mint. This is the 06:00 IST stampede defence (single-flight).
    """
    _minted, _tokens, built, totp_minter, token_factory, sdk_factory = _counting_factories()
    refresher = SdkRefresher(
        _CREDS,
        token_factory=token_factory,
        totp_minter=totp_minter,
        sdk_factory=sdk_factory,
    )
    assert len(built) == 1  # initial mint

    gen_a = refresher.reauth(seen_generation=0)  # caller A wins → mints
    gen_b = refresher.reauth(seen_generation=0)  # caller B is stale → NO second mint
    assert gen_a == 1
    assert gen_b == 1  # B got the already-refreshed generation, did not bump it
    assert len(built) == 2  # ONLY ONE re-mint (the initial + caller A's), NOT two


def test_reauth_concurrent_stampede_mints_exactly_once() -> None:
    """N threads all hitting an expired token at gen 0 trigger EXACTLY ONE re-mint.

    The deterministic concurrency proof: a barrier releases all N threads together, each
    captures generation 0 then races into ``reauth(0)``; the lock + generation check let
    exactly ONE win the mint and the rest no-op. Exactly ONE extra SDK is built.
    """
    _minted, _tokens, built, totp_minter, token_factory, sdk_factory = _counting_factories()
    refresher = SdkRefresher(
        _CREDS,
        token_factory=token_factory,
        totp_minter=totp_minter,
        sdk_factory=sdk_factory,
    )
    assert len(built) == 1  # initial mint
    n = 16
    barrier = threading.Barrier(n)

    def _race() -> int:
        seen = refresher.current_generation()  # all read 0 before the barrier releases
        barrier.wait(timeout=5.0)
        return refresher.reauth(seen)

    with ThreadPoolExecutor(max_workers=n) as ex:
        gens = [f.result(timeout=10.0) for f in [ex.submit(_race) for _ in range(n)]]

    # Exactly one re-mint happened despite N concurrent callers (single-flight).
    assert len(built) == 2  # initial + exactly ONE re-mint
    assert refresher.current_generation() == 1
    assert set(gens) == {1}  # every caller observes the single refreshed generation


def test_pool_sized_refresher_fans_independent_sdks_and_remints_all() -> None:
    """A size=K refresher mints K INDEPENDENT SDKs and one re-mint rebuilds them ALL."""
    _minted, _tokens, built, totp_minter, token_factory, sdk_factory = _counting_factories()
    refresher = SdkRefresher(
        _CREDS,
        size=3,
        token_factory=token_factory,
        totp_minter=totp_minter,
        sdk_factory=sdk_factory,
    )
    assert refresher.size == 3
    initial = [refresher.sdk_at(i) for i in range(3)]
    assert len({id(s) for s in initial}) == 3  # three DISTINCT connections
    assert len(built) == 3  # one token, three SDKs fanned off it
    # current_sdk() is the leaf-0 convenience accessor.
    assert refresher.current_sdk() is initial[0]
    # An over-large index wraps modulo size (defensive).
    assert refresher.sdk_at(3) is initial[0]

    refresher.reauth(seen_generation=0)
    rebuilt = [refresher.sdk_at(i) for i in range(3)]
    assert all(rebuilt[i] is not initial[i] for i in range(3))  # EVERY leaf refreshed
    assert len({id(s) for s in rebuilt}) == 3  # still three distinct connections
    assert len(built) == 6  # 3 initial + 3 re-minted


def test_initial_mint_failure_surfaces_redacted_auth_error() -> None:
    """A failed INITIAL token issuance raises a redacting GrowwAuthError (no secret leak)."""

    def token_factory(_api_key: str, _totp: str) -> str:
        raise RuntimeError("issuance down")

    with pytest.raises(GrowwAuthError) as exc:
        SdkRefresher(
            _CREDS,
            token_factory=token_factory,
            totp_minter=lambda _s: "000000",
            sdk_factory=_FakeSDK,
        )
    assert str(exc.value) == "Groww access-token issuance failed"
    assert "fake_secret" not in str(exc.value)


def test_reauth_mint_failure_surfaces_redacted_auth_error() -> None:
    """A failed RE-MINT raises a redacting GrowwAuthError (the bounded-budget terminal path)."""
    calls = {"n": 0}

    def token_factory(_api_key: str, _totp: str) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            return "ACCESS-OK"  # the initial mint succeeds
        raise RuntimeError("token reset mid-pull, re-mint rejected")

    refresher = SdkRefresher(
        _CREDS,
        token_factory=token_factory,
        totp_minter=lambda _s: "000000",
        sdk_factory=_FakeSDK,
    )
    with pytest.raises(GrowwAuthError) as exc:
        refresher.reauth(seen_generation=0)
    assert "fake_secret" not in str(exc.value)
    # The generation did NOT advance on a failed re-mint (no half-state).
    assert refresher.current_generation() == 0


def test_size_is_clamped_to_at_least_one() -> None:
    """A nonsensical size (0 / negative) clamps to a single connection (fail-safe)."""
    refresher = SdkRefresher(
        _CREDS,
        size=0,
        token_factory=lambda _k, _t: "T",
        totp_minter=lambda _s: "000000",
        sdk_factory=_FakeSDK,
    )
    assert refresher.size == 1
    assert isinstance(refresher.current_sdk(), _FakeSDK)


# ── FREEZE GUARD: a hung re-mint must not hold the lock forever (MINOR-1) ────


def test_hung_remint_raises_retryable_timeout_releases_lock_no_half_state() -> None:
    """A re-mint whose network mint BLOCKS past the deadline raises a retryable timeout.

    THE MINOR-1 hazard: a half-open socket at 06:00 IST would hold ``_lock`` forever,
    freezing every pool leaf. The freeze guard bounds ONLY the network mint, so:
      * ``reauth`` raises a RETRYABLE :class:`GrowwTimeoutError` (NOT a terminal auth error),
      * the lock is RELEASED afterward (another thread's call does NOT deadlock),
      * NO partial state was published (generation unchanged, SDK still the OLD one).
    """
    release = threading.Event()
    minted: list[str] = []

    def token_factory(_api_key: str, _totp: str) -> str:
        if minted:  # the SECOND issuance (the re-mint) hangs past the deadline
            release.wait(timeout=5.0)
        minted.append(f"ACCESS-{len(minted) + 1}")
        return minted[-1]

    refresher = SdkRefresher(
        _CREDS,
        token_factory=token_factory,
        totp_minter=lambda _s: "000000",
        sdk_factory=_FakeSDK,
        call_timeout_seconds=0.05,  # a SHORT injected deadline so the test is fast
    )
    initial_sdk = refresher.current_sdk()
    try:
        with pytest.raises(GrowwTimeoutError):
            refresher.reauth(seen_generation=0)
        # The secret never leaked into the timeout message.
        # (the error message carries only the "deadline exceeded" hint)
        # NO half-state: generation unchanged, the OLD SDK still served.
        assert refresher.current_generation() == 0
        assert refresher.current_sdk() is initial_sdk
        # The lock was RELEASED — a subsequent call from "another thread" returns
        # (it would DEADLOCK if the timed-out re-mint had held the lock forever).
        done: list[int] = []

        def _probe() -> None:
            done.append(refresher.current_generation())

        probe = threading.Thread(target=_probe)
        probe.start()
        probe.join(timeout=2.0)
        assert not probe.is_alive()  # did not deadlock on the lock
        assert done == [0]
    finally:
        release.set()  # let the abandoned daemon mint worker exit


def test_hung_remint_does_not_leak_the_secret() -> None:
    """The freeze-guard timeout message carries only the deadline hint — never the secret."""
    release = threading.Event()
    minted: list[str] = []

    def token_factory(_api_key: str, _totp: str) -> str:
        if minted:
            release.wait(timeout=5.0)
        minted.append("ACCESS")
        return "ACCESS"

    refresher = SdkRefresher(
        _CREDS,
        token_factory=token_factory,
        totp_minter=lambda _s: "000000",
        sdk_factory=_FakeSDK,
        call_timeout_seconds=0.05,
    )
    try:
        with pytest.raises(GrowwTimeoutError) as exc:
            refresher.reauth(seen_generation=0)
        assert "fake_secret" not in str(exc.value)
        assert "deadline exceeded" in str(exc.value)
    finally:
        release.set()


def test_fast_remint_within_deadline_still_bumps_generation() -> None:
    """The NORMAL (fast) re-mint path is unchanged — deadline-wrapped but not hit.

    Back-compat: a mint that returns well within the deadline still mints a fresh SDK
    and bumps the generation exactly as before the freeze guard was added.
    """
    _minted, _tokens, built, totp_minter, token_factory, sdk_factory = _counting_factories()
    refresher = SdkRefresher(
        _CREDS,
        token_factory=token_factory,
        totp_minter=totp_minter,
        sdk_factory=sdk_factory,
        call_timeout_seconds=5.0,  # generous — the fast mint never hits it
    )
    first = refresher.current_sdk()
    assert len(built) == 1

    new_gen = refresher.reauth(seen_generation=0)
    assert new_gen == 1
    assert refresher.current_generation() == 1
    second = refresher.current_sdk()
    assert second is not first
    assert len(built) == 2  # exactly one re-mint within the deadline
