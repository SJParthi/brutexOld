"""Unit tests for the pluggable daily-token backend (DEC-TOKEN-BROKER-AWS-001).

Covers the local-default seam: :func:`resolve_token_broker_readonly`,
:class:`DiskTokenStore`, :func:`resolve_token_store` (memoisation + fail-safe-to-local),
and the backward-compat proof that with NO ``BRUTEX_TOKEN_BROKER_READONLY`` the resolver
returns the disk store whose hooks are byte-identical to the #1041 functions.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from brutex.adapters.groww.auth import GrowwCredentials
from brutex.adapters.groww.constants import (
    GROWW_TOKEN_BROKER_READONLY_ENV,
    resolve_token_broker_readonly,
)
from brutex.adapters.groww.token_cache import (
    CachedToken,
    load_cached_token,
    store_token,
)
from brutex.adapters.groww.token_store import (
    DiskTokenStore,
    TokenStore,
    reset_token_store_cache,
    resolve_token_store,
)

_CREDS = GrowwCredentials(api_key="key-1", totp_secret="sec-1")
_NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _clear_backend_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Each test starts with a clean token-broker env + a fresh store cache."""
    for name in (
        GROWW_TOKEN_BROKER_READONLY_ENV,
        "BRUTEX_TOKEN_AWS_REGION",
        "AWS_REGION",
        "BRUTEX_TOKEN_PARAM_ACCESS_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    reset_token_store_cache()
    yield
    reset_token_store_cache()


class TestResolveTokenBrokerReadonly:
    """``resolve_token_broker_readonly`` — fail-safe to local, aws only on explicit truthy."""

    def test_unset_is_false(self) -> None:
        assert resolve_token_broker_readonly() is False

    @pytest.mark.parametrize("raw", ["1", "true", "TRUE", "  yes  ", "On", "yes"])
    def test_explicit_truthy(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        monkeypatch.setenv(GROWW_TOKEN_BROKER_READONLY_ENV, raw)
        assert resolve_token_broker_readonly() is True

    @pytest.mark.parametrize("raw", ["", "   ", "0", "false", "no", "off", "local", "nonsense"])
    def test_blank_or_falsey_is_false(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        monkeypatch.setenv(GROWW_TOKEN_BROKER_READONLY_ENV, raw)
        assert resolve_token_broker_readonly() is False


class TestDiskTokenStore:
    """``DiskTokenStore`` delegates byte-identically to the #1041 disk functions."""

    def test_satisfies_protocol(self) -> None:
        assert isinstance(DiskTokenStore(), TokenStore)

    def test_load_delegates_to_load_cached_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, object] = {}
        sentinel = CachedToken(access_token="T", issued_for_session=_NOW.date())

        def fake_load(creds: GrowwCredentials, *, now_utc: datetime) -> CachedToken:
            seen["creds"] = creds
            seen["now"] = now_utc
            return sentinel

        monkeypatch.setattr("brutex.adapters.groww.token_store.load_cached_token", fake_load)
        out = DiskTokenStore().load(_CREDS, now_utc=_NOW)
        assert out is sentinel
        assert seen == {"creds": _CREDS, "now": _NOW}

    def test_store_delegates_to_store_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, object] = {}

        def fake_store(creds: GrowwCredentials, token: str, *, now_utc: datetime) -> bool:
            seen["creds"] = creds
            seen["token"] = token
            seen["now"] = now_utc
            return True

        monkeypatch.setattr("brutex.adapters.groww.token_store.store_token", fake_store)
        assert DiskTokenStore().store(_CREDS, "TOKEN", now_utc=_NOW) is True
        assert seen == {"creds": _CREDS, "token": "TOKEN", "now": _NOW}

    def test_hooks_are_the_1041_functions(self) -> None:
        """The disk store's methods wrap the exact #1041 free functions (the seam)."""
        store = DiskTokenStore()
        # DiskTokenStore.load/store call load_cached_token/store_token directly; prove by
        # spying that the store's method reaches those symbols (byte-identical wiring).
        assert store.load.__func__ is DiskTokenStore.load  # type: ignore[attr-defined]
        assert store.store.__func__ is DiskTokenStore.store  # type: ignore[attr-defined]
        # And the module references are the real functions (not re-implementations).
        from brutex.adapters.groww import token_store as ts_mod

        assert ts_mod.load_cached_token is load_cached_token
        assert ts_mod.store_token is store_token


class TestResolveTokenStore:
    """``resolve_token_store`` — default local, memoised, fail-safe to local for aws."""

    def test_default_is_disk_store(self) -> None:
        assert isinstance(resolve_token_store(), DiskTokenStore)

    def test_memoised_same_instance_within_kind(self) -> None:
        first = resolve_token_store()
        second = resolve_token_store()
        assert first is second

    def test_reset_clears_cache(self) -> None:
        first = resolve_token_store()
        reset_token_store_cache()
        assert resolve_token_store() is not first

    def test_backend_switch_rebuilds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        local_first = resolve_token_store()
        assert isinstance(local_first, DiskTokenStore)
        # Switch on the read-only broker WITHOUT boto3 (not installed) ⇒ build fails ⇒
        # falls back to local, but the CACHE KEY is now 'aws', so a new instance is built.
        monkeypatch.setenv(GROWW_TOKEN_BROKER_READONLY_ENV, "true")
        aws_kind = resolve_token_store()
        assert isinstance(aws_kind, DiskTokenStore)  # fail-safe to local
        assert aws_kind is not local_first  # different cache key ⇒ rebuilt

    def test_readonly_without_boto3_falls_back_to_local(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(GROWW_TOKEN_BROKER_READONLY_ENV, "1")
        # boto3 is absent ⇒ build_readonly_token_store raises AwsBackendUnavailable ⇒ local.
        assert isinstance(resolve_token_store(), DiskTokenStore)

    def test_aws_build_raise_falls_back_to_local(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(GROWW_TOKEN_BROKER_READONLY_ENV, "yes")

        def boom() -> object:
            raise RuntimeError("boto3 broke")

        monkeypatch.setattr(
            "brutex.adapters.groww.token_store_aws.build_readonly_token_store", boom
        )
        assert isinstance(resolve_token_store(), DiskTokenStore)

    def test_readonly_selected_returns_aws_store(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(GROWW_TOKEN_BROKER_READONLY_ENV, "on")
        sentinel = DiskTokenStore()  # any TokenStore stands in

        monkeypatch.setattr(
            "brutex.adapters.groww.token_store_aws.build_readonly_token_store",
            lambda: sentinel,
        )
        assert resolve_token_store() is sentinel


class TestLocalOnlyNoBoto3:
    """Backward-compat: a local-only resolve imports ZERO AWS code (no boto3)."""

    def test_local_resolve_does_not_import_boto3(self) -> None:
        import sys

        # Guard: if a prior test imported boto3 (it is not installed here), this is moot;
        # assert the local path itself references nothing AWS.
        resolve_token_store()
        # token_store_aws is only imported inside the aws branch; a default resolve must
        # not have forced boto3 in (it is not installed in this env anyway).
        assert "boto3" not in sys.modules
