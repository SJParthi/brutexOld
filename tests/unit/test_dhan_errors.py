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
    is_auth_failure,
    is_no_data_error,
    is_no_data_message,
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


class TestIsNoDataError:
    """``is_no_data_error`` flags Dhan's DH-905 "no data" 400 (DEC-DHAN-NO-DATA-KNOWN-GAP-001)."""

    def test_true_for_dh905_marker(self) -> None:
        exc = from_http_status(
            400,
            '{"errorType":"Input_Exception","errorCode":"DH-905","errorMessage":"... no data present"}',
        )
        assert is_no_data_error(exc) is True

    def test_true_for_no_data_present_phrase(self) -> None:
        assert (
            is_no_data_error(DhanBadRequestError("HTTP 400 bad request: no data present")) is True
        )

    def test_false_for_generic_bad_request(self) -> None:
        assert (
            is_no_data_error(DhanBadRequestError("HTTP 400 bad request: malformed body")) is False
        )

    @pytest.mark.parametrize(
        "exc",
        [
            DhanRateLimitError("DH-905"),  # right marker, WRONG type → not a 400 no-data
            DhanServerError("no data present"),
            DhanError("DH-905"),
        ],
    )
    def test_false_for_non_bad_request(self, exc: DhanError) -> None:
        assert is_no_data_error(exc) is False


class TestIsNoDataMessage:
    """``is_no_data_message`` — the STRING twin used by the auto-retry classifier."""

    @pytest.mark.parametrize(
        "text",
        [
            "HTTP 400 bad request: DH-905 no data",
            "no data present for this range",
            '{"errorCode":"DH-905"}',
        ],
    )
    def test_true_for_markers(self, text: str) -> None:
        assert is_no_data_message(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            "HTTP 429 rate limited: slow down",
            "HTTP 400 bad request: malformed body",
            "",
        ],
    )
    def test_false_for_non_no_data(self, text: str) -> None:
        assert is_no_data_message(text) is False


class TestIsAuthFailure:
    """``is_auth_failure`` flags a dead token (401/403 OR DH-906 400) — DEC-DHAN-AUTH-FAILFAST-RENEW-001."""

    @pytest.mark.parametrize("status", [401, 403])
    def test_true_for_auth_status(self, status: int) -> None:
        exc = from_http_status(
            status, "Client ID or user generated access token is invalid or expired"
        )
        assert isinstance(exc, DhanAuthError)
        assert is_auth_failure(exc) is True

    def test_true_for_dh906_marker(self) -> None:
        exc = from_http_status(
            400,
            '{"errorType":"Input_Exception","errorCode":"DH-906","errorMessage":"Invalid Token"}',
        )
        # A token-invalid 400 masquerading as a bad request is still a dead token.
        assert isinstance(exc, DhanBadRequestError)
        assert is_auth_failure(exc) is True

    def test_true_for_invalid_token_phrase(self) -> None:
        assert is_auth_failure(DhanBadRequestError("HTTP 400 bad request: Invalid Token")) is True

    def test_false_for_plain_bad_request(self) -> None:
        assert is_auth_failure(DhanBadRequestError("HTTP 400 bad request: malformed body")) is False

    def test_false_for_no_data_bad_request(self) -> None:
        # A DH-905 no-data 400 is NOT a dead token — it stays a known-gap.
        assert (
            is_auth_failure(DhanBadRequestError("HTTP 400 bad request: DH-905 no data present"))
            is False
        )

    @pytest.mark.parametrize(
        "exc",
        [
            DhanNotFoundError("HTTP 404 not found: x"),
            DhanRateLimitError("HTTP 429 rate limited: x"),
            DhanServerError("HTTP 500 server error: x"),
            DhanError("DH-906"),  # right marker, but a bare DhanError is not a 400
        ],
    )
    def test_false_for_others(self, exc: DhanError) -> None:
        assert is_auth_failure(exc) is False
