"""Tests for `brutex.adapters.groww.errors` — HTTP→neutral mapping + retry.

100% line+branch: every status branch of `from_http_status`, rate-limit
predicate, retryable/terminal classification, and auth-failure predicate.
"""

from __future__ import annotations

import pytest
from brutex.adapters.groww.errors import (
    GrowwAdapterError,
    GrowwApiFailureError,
    GrowwAuthError,
    GrowwBadRequestError,
    GrowwNotFoundError,
    GrowwRateLimitError,
    GrowwServerError,
    GrowwTimeoutError,
    GrowwTransportError,
    from_http_status,
    is_auth_failure,
    is_rate_limit,
    is_retryable,
)
from brutex.models import BruteXError


class TestHierarchy:
    def test_all_errors_inherit_base(self) -> None:
        for cls in (
            GrowwBadRequestError,
            GrowwAuthError,
            GrowwNotFoundError,
            GrowwRateLimitError,
            GrowwServerError,
            GrowwTimeoutError,
            GrowwTransportError,
            GrowwApiFailureError,
        ):
            assert issubclass(cls, GrowwAdapterError)

    def test_base_inherits_brutex_error(self) -> None:
        assert issubclass(GrowwAdapterError, BruteXError)


class TestFromHttpStatus:
    @pytest.mark.parametrize(
        ("status", "expected_type"),
        [
            (400, GrowwBadRequestError),
            (401, GrowwAuthError),
            (403, GrowwAuthError),
            (404, GrowwNotFoundError),
            (429, GrowwRateLimitError),
            (500, GrowwServerError),
            (502, GrowwServerError),
            (503, GrowwServerError),
            (599, GrowwServerError),
        ],
    )
    def test_status_to_type(self, status: int, expected_type: type[GrowwAdapterError]) -> None:
        exc = from_http_status(status, "msg")
        assert type(exc) is expected_type

    def test_message_included_in_str(self) -> None:
        exc = from_http_status(500, "boom")
        assert "boom" in str(exc)

    def test_unexpected_status_falls_back_to_base(self) -> None:
        exc = from_http_status(418, "teapot")
        assert type(exc) is GrowwAdapterError
        assert "418" in str(exc)

    def test_401_message_includes_status(self) -> None:
        exc = from_http_status(401, "expired")
        assert "401" in str(exc)

    def test_403_message_includes_status(self) -> None:
        exc = from_http_status(403, "forbidden")
        assert "403" in str(exc)

    def test_5xx_boundary_499_is_fallback(self) -> None:
        exc = from_http_status(499, "x")
        assert type(exc) is GrowwAdapterError

    def test_5xx_boundary_600_is_fallback(self) -> None:
        exc = from_http_status(600, "x")
        assert type(exc) is GrowwAdapterError


class TestGrowwApiFailureError:
    def test_code_attribute(self) -> None:
        exc = GrowwApiFailureError("RATE_LIMIT", "slow down")
        assert exc.code == "RATE_LIMIT"

    def test_str_contains_code_and_message(self) -> None:
        exc = GrowwApiFailureError("INVALID_SYMBOL", "not found")
        s = str(exc)
        assert "INVALID_SYMBOL" in s
        assert "not found" in s

    def test_inherits_adapter_error(self) -> None:
        assert isinstance(GrowwApiFailureError("X", "y"), GrowwAdapterError)


class TestIsRateLimit:
    def test_true_for_rate_limit(self) -> None:
        assert is_rate_limit(GrowwRateLimitError("x")) is True

    @pytest.mark.parametrize(
        "exc",
        [
            GrowwServerError("x"),
            GrowwAuthError("x"),
            GrowwBadRequestError("x"),
            GrowwAdapterError("x"),
            GrowwTimeoutError("x"),
            GrowwTransportError("x"),
        ],
    )
    def test_false_for_non_rate_limit(self, exc: GrowwAdapterError) -> None:
        assert is_rate_limit(exc) is False


class TestIsRetryable:
    @pytest.mark.parametrize(
        "exc",
        [
            GrowwRateLimitError("x"),
            GrowwServerError("x"),
            GrowwTimeoutError("x"),
            GrowwTransportError("x"),
        ],
    )
    def test_retryable(self, exc: GrowwAdapterError) -> None:
        assert is_retryable(exc) is True

    @pytest.mark.parametrize(
        "exc",
        [
            GrowwBadRequestError("x"),
            GrowwNotFoundError("x"),
            GrowwAuthError("x"),
            GrowwAdapterError("x"),
            GrowwApiFailureError("X", "y"),
        ],
    )
    def test_terminal(self, exc: GrowwAdapterError) -> None:
        assert is_retryable(exc) is False


class TestIsAuthFailure:
    def test_true_for_401(self) -> None:
        exc = from_http_status(401, "expired token")
        assert is_auth_failure(exc) is True

    def test_true_for_403(self) -> None:
        exc = from_http_status(403, "forbidden")
        assert is_auth_failure(exc) is True

    def test_true_for_direct_auth_error(self) -> None:
        assert is_auth_failure(GrowwAuthError("x")) is True

    @pytest.mark.parametrize(
        "exc",
        [
            GrowwBadRequestError("x"),
            GrowwNotFoundError("x"),
            GrowwRateLimitError("x"),
            GrowwServerError("x"),
            GrowwAdapterError("x"),
        ],
    )
    def test_false_for_non_auth(self, exc: GrowwAdapterError) -> None:
        assert is_auth_failure(exc) is False
