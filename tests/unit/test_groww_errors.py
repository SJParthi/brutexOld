"""Unit tests for ``brutex.adapters.groww.errors`` (DEC-GROWW-DEPS-REINSTATE-001).

The classifier matches on the SDK exception's class NAME (string), so these
tests use locally-defined fakes with the exact Groww SDK class names — no
``growwapi`` import needed (the optional-extra resilience).
"""

from __future__ import annotations

import json

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
        # 5xx transient server error — code-classified (DEC-GROWW-SERVER-ERROR-RETRY-001)
        (_GenericGrowwAPIException("failed", code=500), e.GrowwServerError),
        (_GenericGrowwAPIException("failed", code=502), e.GrowwServerError),
        (_GenericGrowwAPIException("failed", code=503), e.GrowwServerError),
        (_GenericGrowwAPIException("failed", code="503"), e.GrowwServerError),  # digit string
        # 5xx transient server error — message-sniffed (no usable code)
        (_GenericGrowwAPIException("Internal Server Error"), e.GrowwServerError),
        (_GenericGrowwAPIException("502 Bad Gateway"), e.GrowwServerError),
        (_GenericGrowwAPIException("Service Unavailable, retry later"), e.GrowwServerError),
        # priority ordering: a co-mentioned 5xx never overrides rate-limit / timeout
        (_GenericGrowwAPIException("rate limit hit (server error)"), e.GrowwRateLimitError),
        (_GenericGrowwAPIException("request timed out: bad gateway"), e.GrowwTimeoutError),
        # code-classified (digit string)
        (_GenericGrowwAPIException("failed", code="429"), e.GrowwRateLimitError),
        # bool code is IGNORED (bool is an int subclass) → falls through to message
        (_GenericGrowwAPIException("nondescript", code=True), e.GrowwError),
        (_GenericGrowwAPIException("rate limit hit", code=True), e.GrowwRateLimitError),
        (_GenericGrowwAPIException("internal server error", code=True), e.GrowwServerError),
        # unknown numeric code + unknown message → terminal base (conservative, Golden Rule #1)
        (_GenericGrowwAPIException("weird", code=600), e.GrowwError),
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


def _empty_body_decode_error() -> json.JSONDecodeError:
    """The EXACT error the SDK's ``response.json()`` raises on an empty vendor body.

    Reproduces the live TITAN / SHRIRAMFIN quarantine signature
    ``"Expecting value: line 1 column 1 (char 0)"`` by decoding an empty string.
    """
    try:
        json.loads("")
    except json.JSONDecodeError as exc:
        return exc
    raise AssertionError("json.loads('') must raise JSONDecodeError")  # pragma: no cover


def test_empty_body_json_decode_error_is_retryable() -> None:
    """An empty / non-JSON vendor body (``JSONDecodeError``) → RETRYABLE, not terminal.

    DEC-GROWW-EMPTY-BODY-RETRY-001 — the TITAN / SHRIRAMFIN bug: an empty-body blip
    (a transient throttle) was classified terminal "not on Groww". It must map to a
    RETRYABLE :class:`GrowwEmptyResponseError` so the until-clean loop re-pulls it.
    """
    exc = _empty_body_decode_error()
    mapped = e.from_groww_exception(exc)
    assert type(mapped) is e.GrowwEmptyResponseError
    assert e.is_retryable(mapped) is True
    assert isinstance(mapped, e.GrowwError)
    assert isinstance(mapped, BruteXError)
    # The signature line is preserved verbatim so the audit log still shows the cause.
    assert "Expecting value: line 1 column 1 (char 0)" in str(mapped)
    assert "JSONDecodeError" in str(mapped)


def test_garbled_html_body_json_decode_error_is_retryable() -> None:
    """A garbled (non-empty, non-JSON) body also decodes to a retryable empty-response error."""
    try:
        json.loads("<html>throttled</html>")
    except json.JSONDecodeError as exc:
        mapped = e.from_groww_exception(exc)
    else:  # pragma: no cover - a non-JSON body always raises JSONDecodeError
        raise AssertionError("json.loads('<html>...') must raise JSONDecodeError")
    assert type(mapped) is e.GrowwEmptyResponseError
    assert e.is_retryable(mapped) is True


def test_plain_value_error_stays_terminal_not_retryable() -> None:
    """A plain ``ValueError`` (a code bug, NOT a JSONDecodeError) stays TERMINAL.

    Guards the narrow scope of the empty-body fix: ONLY ``json.JSONDecodeError`` is
    retryable; a generic ``ValueError`` must NOT be blindly retried (Golden Rule #1).
    """
    mapped = e.from_groww_exception(ValueError("a real code bug"))
    assert type(mapped) is e.GrowwError
    assert e.is_retryable(mapped) is False


def test_real_404_not_found_stays_terminal() -> None:
    """A genuine missing symbol (a real ``404`` → ``GrowwAPINotFoundException``) stays terminal.

    The empty-body fix must NOT make a real 404 retryable — a symbol truly absent
    from Groww remains a terminal :class:`GrowwNotFoundError` quarantine.
    """
    mapped = e.from_groww_exception(GrowwAPINotFoundException("symbol not found"))
    assert type(mapped) is e.GrowwNotFoundError
    assert e.is_retryable(mapped) is False


def test_transient_5xx_server_error_is_retryable_not_rate_limit() -> None:
    """A transient 5xx → RETRYABLE :class:`GrowwServerError`, but NOT a rate-limit.

    DEC-GROWW-SERVER-ERROR-RETRY-001 — a momentary upstream 5xx must be re-pulled,
    not terminally quarantined as "not on Groww"; and it must ride the gentle
    timeout ladder, NOT drop the per-second rate ceiling (a 5xx is not a quota
    signal), so :func:`is_rate_limit` must stay ``False`` for it.
    """
    mapped = e.from_groww_exception(_GenericGrowwAPIException("Internal Server Error", code=500))
    assert type(mapped) is e.GrowwServerError
    assert e.is_retryable(mapped) is True
    assert e.is_rate_limit(mapped) is False
    assert e.is_auth_failure(mapped) is False
    assert isinstance(mapped, e.GrowwError)
    assert isinstance(mapped, BruteXError)
    assert "Internal Server Error" in str(mapped)


def test_is_rate_limit() -> None:
    assert e.is_rate_limit(e.GrowwRateLimitError("x")) is True
    assert e.is_rate_limit(e.GrowwTimeoutError("x")) is False
    assert e.is_rate_limit(e.GrowwServerError("x")) is False  # 5xx must NOT step the rate ladder


def test_is_auth_failure() -> None:
    assert e.is_auth_failure(e.GrowwAuthError("x")) is True
    assert e.is_auth_failure(e.GrowwBadRequestError("x")) is False


@pytest.mark.parametrize(
    ("exc", "retryable"),
    [
        (e.GrowwRateLimitError("x"), True),
        (e.GrowwTimeoutError("x"), True),
        (e.GrowwEmptyResponseError("x"), True),
        (e.GrowwServerError("x"), True),
        (e.GrowwBadRequestError("x"), False),
        (e.GrowwAuthError("x"), False),
        (e.GrowwNotFoundError("x"), False),
        (e.GrowwFeedError("x"), False),
        (e.GrowwError("x"), False),
    ],
)
def test_is_retryable(exc: e.GrowwError, retryable: bool) -> None:
    assert e.is_retryable(exc) is retryable
