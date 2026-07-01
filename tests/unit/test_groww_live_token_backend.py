"""build_live_* token-backend wiring + backward-compat proof (DEC-TOKEN-BROKER-AWS-001).

Proves: (a) with NO ``BRUTEX_TOKEN_BROKER_READONLY`` the two build sites thread the DISK
cache hooks byte-identically to #1041 (spy on ``resolve_token_store`` → ``DiskTokenStore``,
and that a mint write-backs to the on-disk cache); (b) an explicit hook wins; (c) a
local-only build imports no boto3; (d) ``_resolve_token_cache_hooks`` resolves the store
ONCE (no split-brain).
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
from brutex.adapters.groww.auth import GrowwCredentials
from brutex.adapters.groww.constants import (
    GROWW_TOKEN_BROKER_READONLY_ENV,
    GROWW_TOKEN_CACHE_PATH_ENV,
)
from brutex.adapters.groww.live import (
    _resolve_token_cache_hooks,
    build_live_client,
    build_live_pool,
)
from brutex.adapters.groww.sdk_protocol import GrowwSDK
from brutex.adapters.groww.token_store import (
    DiskTokenStore,
    reset_token_store_cache,
)

_CREDS = GrowwCredentials(api_key="key-1", totp_secret="sec-1")


def _str_sdk(token: str) -> GrowwSDK:
    """SDK factory whose ``GrowwSDK`` IS the token string (a test never calls its methods)."""
    return cast(GrowwSDK, token)


@pytest.fixture(autouse=True)
def cache_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """Isolate the token backend env + the on-disk cache path; yield the cache path."""
    monkeypatch.delenv(GROWW_TOKEN_BROKER_READONLY_ENV, raising=False)
    cache = tmp_path / "cache.json"
    monkeypatch.setenv(GROWW_TOKEN_CACHE_PATH_ENV, str(cache))
    reset_token_store_cache()
    yield cache
    reset_token_store_cache()


class TestBackwardCompatLocalDefault:
    """No BRUTEX_TOKEN_BROKER_READONLY ⇒ the disk cache is used byte-identically to #1041."""

    def test_client_mint_writes_disk_cache(self, cache_path: Path) -> None:
        build_live_client(
            _CREDS,
            token_factory=lambda _k, _t: "TOKEN-X",
            totp_minter=lambda _s: "000000",
            sdk_factory=_str_sdk,
        )
        # A mint wrote the #1041 on-disk cache (the default backend, byte-identical).
        assert cache_path.exists()
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        assert payload["access_token"] == "TOKEN-X"

    def test_client_reuses_cached_token_no_second_mint(self, cache_path: Path) -> None:
        mints = {"n": 0}

        def minter(_k: str, _t: str) -> str:
            mints["n"] += 1
            return "TOKEN-Y"

        build_live_client(
            _CREDS,
            token_factory=minter,
            totp_minter=lambda _s: "000000",
            sdk_factory=_str_sdk,
        )
        reset_token_store_cache()  # a fresh resolve for the 2nd build (like a new process)
        build_live_client(
            _CREDS,
            token_factory=minter,
            totp_minter=lambda _s: "000000",
            sdk_factory=_str_sdk,
        )
        # The 2nd build read the shared disk token — exactly ONE mint (the #1041 win).
        assert mints["n"] == 1

    def test_default_resolves_disk_store(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, object] = {}
        real = DiskTokenStore()

        def spy() -> DiskTokenStore:
            seen["called"] = True
            return real

        monkeypatch.setattr("brutex.adapters.groww.live.resolve_token_store", spy)
        load, store = _resolve_token_cache_hooks(None, None)
        assert seen == {"called": True}
        # Both hooks come from the SAME resolved store (no split-brain).
        assert load.__self__ is real  # type: ignore[attr-defined]
        assert store.__self__ is real  # type: ignore[attr-defined]

    def test_pool_default_backend(self, cache_path: Path) -> None:
        build_live_pool(
            _CREDS,
            size=2,
            token_factory=lambda _k, _t: "POOL-TOK",
            totp_minter=lambda _s: "000000",
            sdk_factory=_str_sdk,
        )
        assert json.loads(cache_path.read_text())["access_token"] == "POOL-TOK"


class TestExplicitHooksWin:
    """An explicit token_cache_load/store wins over the resolver (test/override seam)."""

    def test_explicit_hooks_used(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def spy() -> DiskTokenStore:
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
        real = DiskTokenStore()
        monkeypatch.setattr("brutex.adapters.groww.live.resolve_token_store", lambda: real)

        def my_load(creds: object, *, now_utc: object) -> None:
            return None

        load, store = _resolve_token_cache_hooks(my_load, None)
        assert load is my_load  # explicit load kept
        assert store.__self__ is real  # type: ignore[attr-defined]  # default store from resolver


class TestNoBoto3OnLocalBuild:
    """A default (local) build never imports boto3."""

    def test_build_client_no_boto3(self) -> None:
        build_live_client(
            _CREDS,
            token_factory=lambda _k, _t: "T",
            totp_minter=lambda _s: "000000",
            sdk_factory=_str_sdk,
        )
        assert "boto3" not in sys.modules
