"""Unit tests for ``brutex.adapters.groww.errors`` (DEC-GROWW-DEPS-REINSTATE-001).

The classifier matches on the SDK exception's class NAME (string), so these
tests use locally-defined fakes with the exact Groww SDK class names — no
``growwapi`` import needed (the optional-extra resilience).
"""

from __future__ import annotations

import pytest
from brutex.adapters.groww import errors as e
from brutex.models import BruteXError


# Fakes named EXACTLY like the growwapi SDK exception classes (doc 05).
class GrowwAPIAuthenticationException(Exception): ...


class GrowwAPIAuthorisationException(Exception): ...


class GrowwAPIBadRequestException(Exception): ...


class GrowwAPINotFoundException(Exception): ...


class GrowwAPIRateLimitException(Exception): ...


class GrowwAPITimeoutException(Exception): ...


class GrowwFeedException(Exception): ...


class GrowwFeedConnectionException(Exception): ...


class GrowwFeedNotSubscribedException(Exception): ...


class GrowwAPIException(Exception): ...  # bare parent — unmapped


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (GrowwAPIAuthenticationException("x"), e.GrowwAuthError),
        (GrowwAPIAuthorisationException("x"), e.GrowwAuthError),
        (GrowwAPIBadRequestException("x"), e.GrowwBadRequestError),
        (GrowwAPINotFoundException("x"), e.GrowwNotFoundError),
        (GrowwAPIRateLimitException("x"), e.GrowwRateLimitError),
        (GrowwAPITimeoutException("x"), e.GrowwTimeoutError),
        (GrowwFeedException("x"), e.GrowwFeedError),
        (GrowwFeedConnectionException("x"), e.GrowwFeedError),
        (GrowwFeedNotSubscribedException("x"), e.GrowwFeedError),
        (GrowwAPIException("x"), e.GrowwError),  # bare parent → terminal base
        (ValueError("x"), e.GrowwError),  # non-SDK → terminal base
    ],
)
def test_from_groww_exception_maps_each_class(exc: Exception, expected: type[e.GrowwError]) -> None:
    mapped = e.from_groww_exception(exc)
    assert type(mapped) is expected
    assert isinstance(mapped, e.GrowwError)
    assert isinstance(mapped, BruteXError)
    assert type(exc).__name__ in str(mapped)  # message preserved


def test_is_rate_limit() -> None:
    assert e.is_rate_limit(e.GrowwRateLimitError("x")) is True
    assert e.is_rate_limit(e.GrowwTimeoutError("x")) is False


def test_is_auth_failure() -> None:
    assert e.is_auth_failure(e.GrowwAuthError("x")) is True
    assert e.is_auth_failure(e.GrowwBadRequestError("x")) is False


@pytest.mark.parametrize(
    ("exc", "retryable"),
    [
        (e.GrowwRateLimitError("x"), True),
        (e.GrowwTimeoutError("x"), True),
        (e.GrowwBadRequestError("x"), False),
        (e.GrowwAuthError("x"), False),
        (e.GrowwNotFoundError("x"), False),
        (e.GrowwFeedError("x"), False),
        (e.GrowwError("x"), False),
    ],
)
def test_is_retryable(exc: e.GrowwError, retryable: bool) -> None:
    assert e.is_retryable(exc) is retryable
