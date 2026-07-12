"""Tests for the Dhan auth error hierarchy (DEC-DHAN-AUTH-001) — scrub-at-construction."""

from __future__ import annotations

import pytest
from brutex.adapters.dhan.errors import (
    DhanAuthError,
    DhanAuthInvalidCredentials,
    DhanAuthLockdown,
    DhanAuthTransportError,
    DhanError,
    DhanTokenMintThrottled,
    DhanTokenRenewError,
    DhanTokenStoreError,
    is_retryable_auth,
)


def test_hierarchy() -> None:
    for cls in (
        DhanAuthInvalidCredentials,
        DhanTokenMintThrottled,
        DhanAuthTransportError,
        DhanTokenRenewError,
        DhanTokenStoreError,
        DhanAuthLockdown,
    ):
        assert issubclass(cls, DhanAuthError)
    assert issubclass(DhanAuthError, DhanError)
    assert issubclass(DhanError, Exception)


class TestScrubAtConstruction:
    def test_masks_secrets(self) -> None:
        exc = DhanAuthError("pin=FAKE-PIN totp=FAKE-TOTP failed", secrets=["FAKE-PIN", "FAKE-TOTP"])
        assert "FAKE-PIN" not in str(exc)
        assert "FAKE-TOTP" not in str(exc)
        assert exc.safe_message == str(exc)

    def test_redacts_url_query_even_without_secrets(self) -> None:
        exc = DhanAuthError(
            "HTTP 400 for https://auth.dhan.co/app/generateAccessToken?pin=1&totp=2: nope"
        )
        assert "pin=1" not in str(exc)
        assert "totp=2" not in str(exc)
        assert "[REDACTED]" in str(exc)
        assert "auth.dhan.co" in str(exc)  # host/path preserved for diagnostics

    def test_trailing_punctuation_after_url(self) -> None:
        exc = DhanAuthError("failed https://h/p?pin=9: 400")
        assert "pin=9" not in str(exc)
        assert str(exc).endswith(": 400")

    def test_non_string_message_coerced(self) -> None:
        exc = DhanAuthError(ValueError("boom"))
        assert "boom" in str(exc)

    def test_subclass_also_scrubs(self) -> None:
        exc = DhanAuthInvalidCredentials("bad FAKE-PIN", secrets=["FAKE-PIN"])
        assert "FAKE-PIN" not in str(exc)


class TestIsRetryableAuth:
    @pytest.mark.parametrize("cls", [DhanTokenMintThrottled, DhanAuthTransportError])
    def test_retryable(self, cls: type[DhanAuthError]) -> None:
        assert is_retryable_auth(cls("x")) is True

    @pytest.mark.parametrize(
        "cls",
        [DhanAuthInvalidCredentials, DhanAuthLockdown, DhanTokenStoreError, DhanTokenRenewError],
    )
    def test_not_retryable(self, cls: type[DhanAuthError]) -> None:
        assert is_retryable_auth(cls("x")) is False

    def test_unrelated_exception_not_retryable(self) -> None:
        assert is_retryable_auth(ValueError("x")) is False
