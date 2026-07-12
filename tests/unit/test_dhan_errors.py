"""Tests for :mod:`brutex.adapters.dhan.errors` — the flat-dict classifier + retry rules."""

from __future__ import annotations

import pytest
from brutex.adapters.dhan.errors import (
    DhanAuthError,
    DhanEmptyResponseError,
    DhanError,
    DhanSubscriptionError,
    DhanThrottleError,
    DhanTimeoutError,
    DhanTokenReadOnlyError,
    DhanTokenStillStaleError,
    DhanVendorDriftError,
    classify_dhan_failure,
    from_dhan_exception,
    is_auth_failure,
    is_rate_limit,
    is_retryable,
    steps_timeout_ladder,
)
from brutex.models import BruteXError


def test_dhan_error_is_a_baseexception_subclass() -> None:
    # FeedErrorPolicy.terminal_base contract: must be a BaseException subclass.
    assert issubclass(DhanError, BaseException)
    assert issubclass(DhanError, BruteXError)


def test_classify_dh901_is_auth() -> None:
    exc = classify_dhan_failure({"errorCode": "DH-901", "errorMessage": "Invalid_Authentication"})
    assert isinstance(exc, DhanAuthError)
    assert not is_retryable(exc)
    assert is_auth_failure(exc)


def test_classify_dh905_is_subscription_terminal() -> None:
    exc = classify_dhan_failure({"errorCode": "DH-905", "errorMessage": "not subscribed"})
    assert isinstance(exc, DhanSubscriptionError)
    assert not is_retryable(exc)
    assert not is_auth_failure(exc)


def test_classify_throttle_heuristic_retryable() -> None:
    exc = classify_dhan_failure({"errorType": "Rate Limit", "errorMessage": "too many requests"})
    assert isinstance(exc, DhanThrottleError)
    assert is_rate_limit(exc)
    assert is_retryable(exc)
    assert not steps_timeout_ladder(exc)  # a rate-limit keeps its own path


def test_classify_empty_body_heuristic_retryable() -> None:
    exc = classify_dhan_failure({"errorType": "", "errorMessage": "Expecting value"})
    assert isinstance(exc, DhanEmptyResponseError)
    assert is_retryable(exc)
    assert not is_rate_limit(exc)
    assert steps_timeout_ladder(exc)  # transient non-rate-limit non-auth → may ease the ladder


def test_classify_unknown_is_terminal_base() -> None:
    exc = classify_dhan_failure({"errorCode": "DH-808", "errorMessage": "mystery"})
    assert type(exc) is DhanError
    assert not is_retryable(exc)


def test_classify_empty_remarks_is_terminal() -> None:
    exc = classify_dhan_failure({})
    assert type(exc) is DhanError
    assert "DH-UNKNOWN" in str(exc)


def test_from_dhan_exception_timeout_is_retryable() -> None:
    exc = from_dhan_exception(TimeoutError("read timed out"))
    assert isinstance(exc, DhanTimeoutError)
    assert is_retryable(exc)
    assert not is_rate_limit(exc)
    assert steps_timeout_ladder(exc)


def test_from_dhan_exception_other_is_terminal() -> None:
    exc = from_dhan_exception(RuntimeError("boom"))
    assert type(exc) is DhanError
    assert not is_retryable(exc)


def test_token_readonly_error_is_terminal_auth() -> None:
    exc = DhanTokenReadOnlyError("no write")
    assert isinstance(exc, DhanAuthError)
    assert is_auth_failure(exc)
    assert not is_retryable(exc)


def test_token_still_stale_is_retryable_but_not_auth_family_for_ladder() -> None:
    exc = DhanTokenStillStaleError("stale")
    assert is_retryable(exc)
    # Deliberately NOT a DhanAuthError subclass → it steps no auth machinery, but it is
    # an auth-family stale race, so it must not ease the timeout ladder.
    assert not is_auth_failure(exc)
    assert steps_timeout_ladder(exc) is True


def test_vendor_drift_is_terminal_per_chunk() -> None:
    exc = DhanVendorDriftError("unequal arrays")
    assert not is_retryable(exc)
    assert not is_rate_limit(exc)


@pytest.mark.parametrize(
    ("hint_code", "expected"),
    [
        ("dh-901", DhanAuthError),  # lower-case code normalised
        ("DH-905", DhanSubscriptionError),
    ],
)
def test_error_code_case_insensitive(hint_code: str, expected: type[DhanError]) -> None:
    exc = classify_dhan_failure({"errorCode": hint_code, "errorMessage": "x"})
    assert isinstance(exc, expected)
