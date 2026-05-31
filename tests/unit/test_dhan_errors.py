"""Tests for `brutex.adapters.dhan.errors` — HTTP→neutral mapping + retry.

100% line+branch: every status branch of `from_http_status`, the rate-limit
predicate, and the retryable/terminal classification.
"""

from __future__ import annotations

import pytest
from brutex.adapters.dhan.errors import (
    DhanAuthError,
    DhanBadRequestError,
    DhanError,
    DhanNotFoundError,
    DhanRateLimitError,
    DhanServerError,
    DhanTimeoutError,
    DhanTransportError,
    from_http_status,
    is_rate_limit,
    is_retryable,
)


class TestFromHttpStatus:
    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (400, DhanBadRequestError),
            (401, DhanAuthError),
            (403, DhanAuthError),
            (404, DhanNotFoundError),
            (429, DhanRateLimitError),
            (500, DhanServerError),
            (503, DhanServerError),
            (599, DhanServerError),
        ],
    )
    def test_status_maps_to_expected_type(self, status: int, expected: type[DhanError]) -> None:
        exc = from_http_status(status, "boom")
        assert type(exc) is expected
        assert "boom" in str(exc)

    def test_unexpected_status_is_bare_dhan_error(self) -> None:
        exc = from_http_status(418, "teapot")
        assert type(exc) is DhanError
        assert "418" in str(exc)


class TestIsRateLimit:
    def test_true_for_rate_limit(self) -> None:
        assert is_rate_limit(DhanRateLimitError("x")) is True

    @pytest.mark.parametrize(
        "exc",
        [DhanServerError("x"), DhanAuthError("x"), DhanError("x")],
    )
    def test_false_for_others(self, exc: DhanError) -> None:
        assert is_rate_limit(exc) is False


class TestIsRetryable:
    @pytest.mark.parametrize(
        "exc",
        [
            DhanRateLimitError("x"),
            DhanServerError("x"),
            DhanTimeoutError("x"),
            DhanTransportError("x"),
        ],
    )
    def test_retryable(self, exc: DhanError) -> None:
        assert is_retryable(exc) is True

    @pytest.mark.parametrize(
        "exc",
        [
            DhanBadRequestError("x"),
            DhanNotFoundError("x"),
            DhanAuthError("x"),
            DhanError("x"),
        ],
    )
    def test_terminal(self, exc: DhanError) -> None:
        assert is_retryable(exc) is False
