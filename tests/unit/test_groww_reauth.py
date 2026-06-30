"""Unit tests for ``brutex.adapters.groww.reauth`` (DEC-GROWW-TOKEN-REAUTH-001).

Injected fakes (``token_factory`` / ``totp_minter`` / ``sdk_factory``) drive every
branch of the thread-safe single-flight :class:`SdkRefresher` — the initial mint, the
re-mint generation bump, the single-flight no-second-mint race, the per-leaf SDK fleet,
and the redacting mint-failure path — with no ``growwapi`` / ``pyotp`` and no network.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable, Generator, Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import SupportsIndex

import pytest
from brutex.adapters.groww.auth import GrowwCredentials
from brutex.adapters.groww.constants import GROWW_TOKEN_CACHE_PATH_ENV
from brutex.adapters.groww.errors import (
    GrowwAuthError,
    GrowwTimeoutError,
    GrowwTokenIssuanceThrottled,
)
from brutex.adapters.groww.reauth import SdkRefresher
from brutex.adapters.groww.sdk_protocol import GrowwSDK

_CREDS = GrowwCredentials(api_key="fake_key", totp_secret="fake_secret")


@pytest.fixture(autouse=True)
def _isolated_token_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the on-disk token cache at a clean tmp file for EVERY test in this module.

    Without this the cache-first cold-start (DEC-GROWW-TOKEN-DISK-CACHE-001) would read a
    real ``~/.brutex/cache/groww_access_token.json`` and skip the mint the legacy tests
    assert happens. A fresh tmp path has no file ⇒ cache MISS ⇒ the byte-identical
    mint-at-construction behaviour these tests already pin. The cache-specific tests below
    inject their OWN cache hooks, bypassing the on-disk path entirely.
    """
    monkeypatch.setenv(GROWW_TOKEN_CACHE_PATH_ENV, str(tmp_path / "tok.json"))


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
        # embed the secret verbatim so the scrub is genuinely exercised here too
        raise RuntimeError("issuance down for fake_secret")

    with pytest.raises(GrowwAuthError) as exc:
        SdkRefresher(
            _CREDS,
            token_factory=token_factory,
            totp_minter=lambda _s: "000000",
            sdk_factory=_FakeSDK,
        )
    # the redacting safe_message now surfaces the (sanitized) vendor reason
    assert str(exc.value).startswith("Groww access-token issuance failed: ")
    assert "issuance down" in str(exc.value)
    assert "fake_secret" not in str(exc.value)
    assert "***" in str(exc.value)


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


# --------------------------------------------------------------------------- #
# DEC-GROWW-TOKEN-DISK-CACHE-001 — cache-first cold-start + B1 forced reauth mint
# --------------------------------------------------------------------------- #
from brutex.adapters.groww.token_cache import CachedToken  # noqa: E402

_NOW = datetime(2026, 6, 30, 2, 30, tzinfo=UTC)


def _cache_hooks(
    cached: CachedToken | None,
) -> tuple[list[tuple[str, str]], Callable[..., CachedToken | None], Callable[..., bool]]:
    """A (stores, load, store) triple: ``load`` returns *cached*; ``store`` records writes."""
    stores: list[tuple[str, str]] = []

    def load(creds: GrowwCredentials, *, now_utc: datetime) -> CachedToken | None:
        return cached

    def store(creds: GrowwCredentials, token: str, *, now_utc: datetime) -> bool:
        stores.append((creds.api_key, token))
        return True

    return stores, load, store


class TestColdStartCache:
    """The cold-start ``__init__`` path is cache-first (DEC-GROWW-TOKEN-DISK-CACHE-001)."""

    def test_cache_hit_skips_mint(self) -> None:
        minted, _t, built, totp_minter, token_factory, sdk_factory = _counting_factories()
        _stores, load, store = _cache_hooks(
            CachedToken(access_token="CACHED-TOK", issued_for_session=_NOW.date())
        )
        refresher = SdkRefresher(
            _CREDS,
            token_factory=token_factory,
            totp_minter=totp_minter,
            sdk_factory=sdk_factory,
            token_cache_load=load,
            token_cache_store=store,
            clock=lambda: _NOW,
        )
        sdk = refresher.current_sdk()
        assert isinstance(sdk, _FakeSDK)
        assert sdk.token == "CACHED-TOK"  # the CACHED token was reused
        assert minted == []  # NO network mint
        assert built == ["CACHED-TOK"]

    def test_cache_miss_mints_and_writes_cache(self) -> None:
        minted, _t, _built, totp_minter, token_factory, sdk_factory = _counting_factories()
        stores, load, store = _cache_hooks(None)
        refresher = SdkRefresher(
            _CREDS,
            token_factory=token_factory,
            totp_minter=totp_minter,
            sdk_factory=sdk_factory,
            token_cache_load=load,
            token_cache_store=store,
            clock=lambda: _NOW,
        )
        assert refresher.current_sdk().token == "ACCESS-1"  # type: ignore[attr-defined]
        assert minted == ["fake_secret"]  # ONE real mint
        assert stores == [(_CREDS.api_key, "ACCESS-1")]  # the fresh token was cached

    def test_three_refreshers_share_one_mint_via_cache(self) -> None:
        """The 3-phase pipeline collapses to ONE real mint: phase 1 writes, 2+3 read."""
        minted, _t, _b, totp_minter, token_factory, sdk_factory = _counting_factories()
        # A shared dict cache the store writes + the load reads (a tiny in-memory disk).
        box: dict[str, CachedToken] = {}

        def load(creds: GrowwCredentials, *, now_utc: datetime) -> CachedToken | None:
            return box.get("tok")

        def store(creds: GrowwCredentials, token: str, *, now_utc: datetime) -> bool:
            box["tok"] = CachedToken(access_token=token, issued_for_session=now_utc.date())
            return True

        for _phase in range(3):
            SdkRefresher(
                _CREDS,
                token_factory=token_factory,
                totp_minter=totp_minter,
                sdk_factory=sdk_factory,
                token_cache_load=load,
                token_cache_store=store,
                clock=lambda: _NOW,
            )
        assert minted == ["fake_secret"]  # exactly ONE network mint across 3 phases

    def test_raising_cache_load_degrades_to_mint_never_crashes_init(self) -> None:
        """A cache-load that RAISES must degrade to a real mint, NOT crash ``__init__``.

        Belt-and-suspenders for the MAJOR (``load_cached_token`` raising
        ``UnicodeDecodeError`` on a non-UTF-8 cache file): even if a future load path
        regresses and raises, ``_safe_cache_load`` swallows it → the cold-start falls
        through to a real mint, so the whole pull build can NEVER crash on a cache
        problem. This test FAILS before the defensive guard (the raise escapes
        ``SdkRefresher.__init__``).
        """
        minted, _t, _built, totp_minter, token_factory, sdk_factory = _counting_factories()
        stores, _load, store = _cache_hooks(None)

        def boom_load(creds: GrowwCredentials, *, now_utc: datetime) -> CachedToken | None:
            # The exact failure mode of the MAJOR: a non-UTF-8 file's decode error.
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

        # __init__ must NOT raise — it degrades the broken cache to a real mint.
        refresher = SdkRefresher(
            _CREDS,
            token_factory=token_factory,
            totp_minter=totp_minter,
            sdk_factory=sdk_factory,
            token_cache_load=boom_load,
            token_cache_store=store,
            clock=lambda: _NOW,
        )
        assert refresher.current_sdk().token == "ACCESS-1"  # type: ignore[attr-defined]
        assert minted == ["fake_secret"]  # the broken cache degraded to a real mint
        assert stores == [(_CREDS.api_key, "ACCESS-1")]  # the fresh token was cached


class TestColdStartThrottle:
    """The cold-start mint rides out an issuance throttle with bounded back-off (M2)."""

    def test_throttle_then_cache_recovers(self) -> None:
        """A racing phase writes the cache while we are throttled → we reuse it, no crash."""
        attempts: list[int] = []
        box: dict[str, CachedToken] = {}

        def load(creds: GrowwCredentials, *, now_utc: datetime) -> CachedToken | None:
            # First read: miss. After the first throttled mint, a "racing phase" populated it.
            return box.get("tok")

        def token_factory(_api_key: str, _totp: str) -> str:
            attempts.append(1)
            # simulate a racing writer landing a cache between our attempts
            box["tok"] = CachedToken(access_token="RACED-TOK", issued_for_session=_NOW.date())
            raise _throttle()

        sleeps: list[float] = []
        with _patched_sleep(sleeps):
            refresher = SdkRefresher(
                _CREDS,
                token_factory=token_factory,
                totp_minter=lambda _s: "111111",
                sdk_factory=_FakeSDK,
                token_cache_load=load,
                token_cache_store=lambda *_a, **_k: True,
                clock=lambda: _NOW,
            )
        assert refresher.current_sdk().token == "RACED-TOK"  # type: ignore[attr-defined]
        assert len(attempts) == 1  # one throttled mint, then the cache hit on retry
        assert len(sleeps) == 1  # one back-off slept before the retry

    def test_persistent_throttle_no_cache_reraises_retryable(self) -> None:
        """Budget exhausted with NO cache ⇒ re-raise the RETRYABLE throttle (no crash)."""
        attempts: list[int] = []

        def token_factory(_api_key: str, _totp: str) -> str:
            attempts.append(1)
            raise _throttle()

        sleeps: list[float] = []
        with _patched_sleep(sleeps), pytest.raises(GrowwTokenIssuanceThrottled):
            SdkRefresher(
                _CREDS,
                token_factory=token_factory,
                totp_minter=lambda _s: "111111",
                sdk_factory=_FakeSDK,
                token_cache_load=lambda *_a, **_k: None,  # always a miss
                token_cache_store=lambda *_a, **_k: True,
                clock=lambda: _NOW,
            )
        # GROWW_TOKEN_ISSUANCE_RETRY_ATTEMPTS=3 → 3 mints, 2 back-offs between them.
        assert len(attempts) == 3
        assert len(sleeps) == 2

    def test_terminal_auth_error_not_retried(self) -> None:
        """A NON-throttle auth failure on cold-start is NOT backed off — it raises at once."""
        attempts: list[int] = []

        def token_factory(_api_key: str, _totp: str) -> str:
            attempts.append(1)
            raise _ApiExc400("Groww API Error 400: Invalid TOTP")

        with pytest.raises(GrowwAuthError) as ei:
            SdkRefresher(
                _CREDS,
                token_factory=token_factory,
                totp_minter=lambda _s: "111111",
                sdk_factory=_FakeSDK,
                token_cache_load=lambda *_a, **_k: None,
                token_cache_store=lambda *_a, **_k: True,
                clock=lambda: _NOW,
            )
        assert not isinstance(ei.value, GrowwTokenIssuanceThrottled)
        assert len(attempts) == 1  # NO retry on a terminal credential error


class TestReauthForcedMint:
    """B1 (BLOCKER): in-pull ``reauth`` FORCES a real mint and NEVER reads the cache."""

    def test_reauth_does_not_read_cache(self) -> None:
        """Even with a (stale, valid-session) cache present, reauth force-mints a NEW token."""
        minted, _t, _built, totp_minter, token_factory, sdk_factory = _counting_factories()
        load_calls: list[int] = []

        def load(creds: GrowwCredentials, *, now_utc: datetime) -> CachedToken | None:
            load_calls.append(1)
            return CachedToken(access_token="STALE-TOK", issued_for_session=_NOW.date())

        stores: list[str] = []

        def store(creds: GrowwCredentials, token: str, *, now_utc: datetime) -> bool:
            stores.append(token)
            return True

        refresher = SdkRefresher(
            _CREDS,
            token_factory=token_factory,
            totp_minter=totp_minter,
            sdk_factory=sdk_factory,
            token_cache_load=load,
            token_cache_store=store,
            clock=lambda: _NOW,
        )
        # Cold-start was a cache HIT (STALE-TOK), no mint yet.
        assert refresher.current_sdk().token == "STALE-TOK"  # type: ignore[attr-defined]
        assert minted == []
        assert load_calls == [1]  # the cache was read ONCE, at cold-start only

        # The token is "revoked" → reauth FORCES a real mint, IGNORING the cache.
        refresher.reauth(seen_generation=0)
        assert load_calls == [1]  # reauth did NOT read the cache again (B1)
        assert minted == ["fake_secret"]  # a REAL mint happened
        assert refresher.current_sdk().token == "ACCESS-1"  # type: ignore[attr-defined]
        assert stores == ["ACCESS-1"]  # reauth OVERWROTE the cache with the fresh token

    def test_revoked_token_self_corrects_no_loop(self) -> None:
        """REGRESSION (B1): a revoked-but-valid-session cache does NOT loop the dead token.

        Cold-start serves the cached (dead) token; reauth must force a mint that
        OVERWRITES it, so the next cold-start would see the GOOD token — proving the
        ``max_reauths=1`` self-heal is not defeated by cache-first reauth.
        """
        box: dict[str, CachedToken] = {
            "tok": CachedToken(access_token="DEAD-TOK", issued_for_session=_NOW.date())
        }
        minted: list[str] = []

        def load(creds: GrowwCredentials, *, now_utc: datetime) -> CachedToken | None:
            return box.get("tok")

        def store(creds: GrowwCredentials, token: str, *, now_utc: datetime) -> bool:
            box["tok"] = CachedToken(access_token=token, issued_for_session=now_utc.date())
            return True

        def token_factory(_api_key: str, _totp: str) -> str:
            minted.append("GOOD")
            return "GOOD-TOK"

        refresher = SdkRefresher(
            _CREDS,
            token_factory=token_factory,
            totp_minter=lambda _s: "111111",
            sdk_factory=_FakeSDK,
            token_cache_load=load,
            token_cache_store=store,
            clock=lambda: _NOW,
        )
        assert refresher.current_sdk().token == "DEAD-TOK"  # type: ignore[attr-defined]
        # The dead token is rejected → reauth force-mints GOOD and overwrites the cache.
        refresher.reauth(seen_generation=0)
        assert minted == ["GOOD"]  # a real mint, NOT a cache re-read of DEAD-TOK
        assert box["tok"].access_token == "GOOD-TOK"  # the cache was self-corrected


class _ApiExc400(RuntimeError):
    code = 400


def _throttle() -> RuntimeError:
    class _RateExc(RuntimeError):
        code = 429

    return _RateExc("Rate limit exceeded")


@contextlib.contextmanager
def _patched_sleep(sink: list[float]) -> Generator[None]:
    """Record (and skip) the cold-start back-off ``time.sleep`` calls.

    ``reauth`` does ``import time`` then ``time.sleep(...)``, so patching the attribute
    on the SHARED stdlib :mod:`time` module object intercepts it (no module-private
    access). The real sleeper is restored on exit.
    """
    orig = time.sleep

    def _record(seconds: float | SupportsIndex) -> None:
        sink.append(float(seconds))

    time.sleep = _record  # records the requested seconds, sleeps for none
    try:
        yield
    finally:
        time.sleep = orig
