"""build_live_* token-backend wiring (DEC-AWS-ONLY-CREDS-001).

Proves: (a) the two build sites thread the resolved backend's ``load``/``store`` hooks —
now the shared read-only AWS token, the SOLE source; (b) an explicit hook wins over the
resolver (the test/override seam); (c) ``_resolve_token_cache_hooks`` resolves the store
ONCE so both hooks come from the SAME instance (no split-brain).

The default resolver (:func:`~brutex.adapters.groww.token_store.resolve_token_store`) is
mocked to an in-memory fake store so these tests need NO boto3 and NO network.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import cast

import pytest
from brutex.adapters.groww.auth import GrowwCredentials
from brutex.adapters.groww.constants import GROWW_TOKEN_CACHE_PATH_ENV
from brutex.adapters.groww.live import (
    _resolve_token_cache_hooks,
    build_live_client,
    build_live_pool,
)
from brutex.adapters.groww.sdk_protocol import GrowwSDK
from brutex.adapters.groww.token_cache import CachedToken
from brutex.adapters.groww.token_store import reset_token_store_cache

_CREDS = GrowwCredentials(api_key="key-1", totp_secret="sec-1")


def _str_sdk(token: str) -> GrowwSDK:
    """SDK factory whose ``GrowwSDK`` IS the token string (a test never calls its methods)."""
    return cast(GrowwSDK, token)


class _FakeStore:
    """An in-memory :class:`~brutex.adapters.groww.token_store.TokenStore` for the resolver."""

    def __init__(self, *, token: str | None = None, readonly: bool = True) -> None:
        self.token = token
        self.stored: str | None = None
        self._readonly = readonly

    @property
    def readonly(self) -> bool:
        return self._readonly

    def load(self, creds: object, *, now_utc: datetime) -> CachedToken | None:
        if self.token is None:
            return None
        return CachedToken(access_token=self.token, issued_for_session=now_utc.date())

    def store(self, creds: object, token: str, *, now_utc: datetime) -> bool:
        self.stored = token
        return True


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Isolate the token backend env + reset the memoised store between tests."""
    monkeypatch.setenv(GROWW_TOKEN_CACHE_PATH_ENV, str(tmp_path / "cache.json"))
    reset_token_store_cache()
    yield
    reset_token_store_cache()


class TestResolvedBackendThreadedIn:
    """The build sites thread the resolved AWS store's hooks (the SOLE token source)."""

    def test_client_reads_shared_token_no_mint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        store = _FakeStore(token="LAMBDA-TOKEN")
        monkeypatch.setattr("brutex.adapters.groww.live.resolve_token_store", lambda: store)
        mints = {"n": 0}

        def minter(_k: str, _t: str) -> str:
            mints["n"] += 1
            return "SHOULD-NOT-MINT"

        client = build_live_client(
            _CREDS,
            token_factory=minter,
            totp_minter=lambda _s: "000000",
            sdk_factory=_str_sdk,
        )
        # ``_str_sdk`` returns the token string cast as the SDK, so ``client.sdk`` IS the token.
        assert client.sdk == "LAMBDA-TOKEN"
        assert mints["n"] == 0  # the shared AWS token was read, never minted

    def test_pool_reads_shared_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        store = _FakeStore(token="POOL-TOKEN")
        monkeypatch.setattr("brutex.adapters.groww.live.resolve_token_store", lambda: store)
        build_live_pool(
            _CREDS,
            size=2,
            token_factory=lambda _k, _t: "SHOULD-NOT-MINT",
            totp_minter=lambda _s: "000000",
            sdk_factory=_str_sdk,
        )  # no raise ⇒ the shared token was read for every leaf

    def test_default_resolves_the_store_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, int] = {"n": 0}
        real = _FakeStore(token="T")

        def spy() -> _FakeStore:
            seen["n"] += 1
            return real

        monkeypatch.setattr("brutex.adapters.groww.live.resolve_token_store", spy)
        load, store, reread = _resolve_token_cache_hooks(None, None)
        assert seen["n"] == 1  # resolved exactly ONCE (no split-brain)
        assert load.__self__ is real  # type: ignore[attr-defined]
        assert store.__self__ is real  # type: ignore[attr-defined]
        # A read-only store also yields the read-only re-read hook (same backend instance).
        assert reread is not None
        assert reread.__self__ is real  # type: ignore[attr-defined]


class TestExplicitHooksWin:
    """An explicit token_cache_load/store wins over the resolver (test/override seam)."""

    def test_explicit_hooks_used(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def spy() -> _FakeStore:
            raise AssertionError("resolver must not run when both hooks are explicit")

        monkeypatch.setattr("brutex.adapters.groww.live.resolve_token_store", spy)
        calls: dict[str, object] = {}

        def my_load(creds: object, *, now_utc: object) -> None:
            calls["loaded"] = True

        def my_store(creds: object, token: str, *, now_utc: object) -> bool:
            calls["stored"] = token
            return True

        build_live_client(
            _CREDS,
            token_factory=lambda _k, _t: "EXPLICIT-TOK",
            totp_minter=lambda _s: "000000",
            sdk_factory=_str_sdk,
            token_cache_load=my_load,
            token_cache_store=my_store,
        )
        assert calls.get("loaded") is True
        assert calls.get("stored") == "EXPLICIT-TOK"

    def test_one_explicit_one_default_uses_resolver(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real = _FakeStore(token=None)
        monkeypatch.setattr("brutex.adapters.groww.live.resolve_token_store", lambda: real)

        def my_load(creds: object, *, now_utc: object) -> None:
            return None

        load, store, reread = _resolve_token_cache_hooks(my_load, None)
        assert load is my_load  # explicit load kept
        assert store.__self__ is real  # type: ignore[attr-defined]  # default store from resolver
        # The resolved store is read-only, so the read-only re-read hook is its `load`
        # (DEC-BROKER-READONLY-REAUTH-REREAD-001) — the mid-run reauth re-reads, never mints.
        assert reread is not None
        assert reread.__self__ is real  # type: ignore[attr-defined]
