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


class _GenericGrowwAPIException(Exception):
    """Generic Groww client error carrying an optional HTTP-ish ``code`` (the live 429 shape)."""

    def __init__(self, message: str, code: object = None) -> None:
        super().__init__(message)
        self.code = code


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        # message-sniffed (no typed subclass, no usable code) — the LIVE 429 shape
        (
            _GenericGrowwAPIException("Rate limit has breached for your request."),
            e.GrowwRateLimitError,
        ),
        (_GenericGrowwAPIException("Too many requests, slow down"), e.GrowwRateLimitError),
        (_GenericGrowwAPIException("The request timed out"), e.GrowwTimeoutError),
        # code-classified (int)
        (_GenericGrowwAPIException("failed", code=429), e.GrowwRateLimitError),
        (_GenericGrowwAPIException("failed", code=408), e.GrowwTimeoutError),
        (_GenericGrowwAPIException("failed", code=504), e.GrowwTimeoutError),
        # code-classified (digit string)
        (_GenericGrowwAPIException("failed", code="429"), e.GrowwRateLimitError),
        # bool code is IGNORED (bool is an int subclass) → falls through to message
        (_GenericGrowwAPIException("nondescript", code=True), e.GrowwError),
        (_GenericGrowwAPIException("rate limit hit", code=True), e.GrowwRateLimitError),
        # unknown code + unknown message → terminal base (conservative, Golden Rule #1)
        (_GenericGrowwAPIException("weird", code=500), e.GrowwError),
        (_GenericGrowwAPIException("weird", code="notnumber"), e.GrowwError),
    ],
)
def test_from_groww_exception_classifies_generic_by_code_and_message(
    exc: Exception, expected: type[e.GrowwError]
) -> None:
    """A generic ``GrowwAPIException`` (no typed subclass) is classified by code + message.

    DEC-GROWW-RATELIMIT-RETRY-001 — the SDK surfaces a live rate-limit as the BASE
    ``GrowwAPIException`` (a ``429`` ``code`` and/or a "Rate limit …" message), not
    the typed subclass, so it must still be recognised retryably.
    """
    assert type(e.from_groww_exception(exc)) is expected


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
