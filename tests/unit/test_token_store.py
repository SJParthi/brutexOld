"""Unit tests for the AWS-only daily-token backend (DEC-AWS-ONLY-CREDS-001).

AWS Parameter Store is now the SOLE token source: the local disk-cache backend +
the ``BRUTEX_TOKEN_BROKER_READONLY`` opt-in flag were removed. :func:`resolve_token_store`
builds the read-only Parameter-Store store UNCONDITIONALLY and is FAIL-FAST — a missing
boto3 extra / an SSM-client build failure raises :class:`GrowwAuthError` naming the deploy
pointer, never a local fallback. The store is memoised so all three refreshers of one pull
share ONE client.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from brutex.adapters.groww.errors import GrowwAuthError
from brutex.adapters.groww.token_store import (
    TokenStore,
    reset_token_store_cache,
    resolve_token_store,
)


@pytest.fixture(autouse=True)
def _clear_backend_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Each test starts with a clean token-broker env + a fresh store cache."""
    for name in (
        "BRUTEX_TOKEN_AWS_REGION",
        "AWS_REGION",
        "BRUTEX_TOKEN_PARAM_ACCESS_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    reset_token_store_cache()
    yield
    reset_token_store_cache()


class _FakeStore:
    """A minimal :class:`TokenStore` stand-in for the build-success path."""

    @property
    def readonly(self) -> bool:  # pragma: no cover - stub (never read on this path)
        return True

    def load(self, creds: object, *, now_utc: object) -> None:  # pragma: no cover - stub
        return None

    def store(self, creds: object, token: str, *, now_utc: object) -> bool:  # pragma: no cover
        return True


class TestResolveTokenStoreAwsMandatory:
    """``resolve_token_store`` — AWS unconditionally, memoised, fail-fast on build error."""

    def test_returns_aws_store_on_build_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sentinel = _FakeStore()
        monkeypatch.setattr(
            "brutex.adapters.groww.token_store_aws.build_readonly_token_store",
            lambda: sentinel,
        )
        assert resolve_token_store() is sentinel
        assert isinstance(resolve_token_store(), TokenStore)

    def test_memoised_same_instance(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "brutex.adapters.groww.token_store_aws.build_readonly_token_store",
            _FakeStore,
        )
        first = resolve_token_store()
        second = resolve_token_store()
        assert first is second

    def test_reset_clears_cache(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "brutex.adapters.groww.token_store_aws.build_readonly_token_store",
            _FakeStore,
        )
        first = resolve_token_store()
        reset_token_store_cache()
        assert resolve_token_store() is not first

    def test_build_failure_is_fail_fast(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A boto3/SSM build error raises GrowwAuthError naming the deploy pointer — no fallback."""

        def boom() -> object:
            raise RuntimeError("boto3 broke")

        monkeypatch.setattr(
            "brutex.adapters.groww.token_store_aws.build_readonly_token_store", boom
        )
        with pytest.raises(GrowwAuthError) as exc:
            resolve_token_store()
        assert "deploy/aws_token_minter/deploy.sh" in str(exc.value)
        assert "RuntimeError" in str(exc.value)

    def test_missing_boto3_import_error_is_fail_fast(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An ImportError (boto3 extra absent) is also fail-fast, not a silent local fallback."""

        def raise_import() -> object:
            raise ImportError("No module named 'boto3'")

        monkeypatch.setattr(
            "brutex.adapters.groww.token_store_aws.build_readonly_token_store", raise_import
        )
        with pytest.raises(GrowwAuthError):
            resolve_token_store()
