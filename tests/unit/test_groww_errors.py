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


@pytest.mark.parametrize(
    "message",
    [
        # An unrelated error code / id / timestamp that merely CONTAINS "429" as a
        # substring — NONE of these is a rate-limit and none must be retried.
        "Contract NSE-FOO24JUL18500CE error code STK-429-DELISTED, not tradable",
        "order id 1442904291 rejected",  # "429" sits inside the order id
        "no candles at epoch 1714294290",  # "429" sits inside the epoch timestamp
        "instrument 4290 is not on Groww",
    ],
)
def test_bare_429_substring_is_not_a_rate_limit(message: str) -> None:
    """DEC-ADAPTER-ROBUSTNESS-001 (H8) regression: a message merely CONTAINING ``429``
    (an error code, an order id, a timestamp) must NOT be classified as a retryable
    rate-limit.

    The old ``_RATE_LIMIT_HINTS`` carried a bare ``"429"`` substring, so any such
    message false-matched → mapped to ``GrowwRateLimitError`` → re-pulled forever,
    needlessly stepping the rate ladder down + escaping per-symbol quarantine. With the
    bare substring dropped these stay the TERMINAL base :class:`GrowwError` (no ``code``,
    no genuine phrase), so they are neither retryable nor a ladder-stepping rate-limit.
    """
    mapped = e.from_groww_exception(_GenericGrowwAPIException(message))
    assert type(mapped) is e.GrowwError
    assert e.is_retryable(mapped) is False
    assert e.is_rate_limit(mapped) is False


@pytest.mark.parametrize(
    "exc",
    [
        # Genuine rate-limit — STRUCTURED status code (int + digit-string), the path that
        # survives dropping the bare "429" substring (DEC-ADAPTER-ROBUSTNESS-001 H8).
        _GenericGrowwAPIException("Service throttled", code=429),
        _GenericGrowwAPIException("Service throttled", code="429"),
        # Genuine rate-limit — the live phrase shapes.
        _GenericGrowwAPIException("Rate limit has breached for your request."),
        _GenericGrowwAPIException("HTTP 429: Too Many Requests, slow down"),
    ],
)
def test_genuine_rate_limit_still_retryable_without_bare_substring(
    exc: _GenericGrowwAPIException,
) -> None:
    """DEC-ADAPTER-ROBUSTNESS-001 (H8): dropping the bare ``"429"`` substring loses NO
    genuine rate-limit detection — a real 429 is caught STRUCTURALLY (``code == 429``,
    int or digit-string) or by a genuine phrase hint, so it still maps to a retryable
    :class:`GrowwRateLimitError` that DOES step the rate ladder.
    """
    mapped = e.from_groww_exception(exc)
    assert type(mapped) is e.GrowwRateLimitError
    assert e.is_retryable(mapped) is True
    assert e.is_rate_limit(mapped) is True


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


# ──────────────────────────────────────────────────────────────────────────
# Transient network / DNS errors → RETRYABLE GrowwNetworkError, NOT rate-limit
# (DEC-GROWW-NETWORK-ERROR-RETRY-001 — the 218-contract DNS-outage quarantine).
# Fakes named EXACTLY like the requests / urllib3 / socket transport classes
# (matched by class NAME, so neither package need be importable).
# ──────────────────────────────────────────────────────────────────────────
class ConnectionError(Exception):  # noqa: A001 — mirrors requests.exceptions.ConnectionError / builtin
    """Fake transport ``ConnectionError`` (requests / urllib3 wraps a DNS blip in it)."""


class NameResolutionError(Exception):
    """Fake ``urllib3.exceptions.NameResolutionError`` (a DNS failure)."""


class NewConnectionError(Exception):
    """Fake ``urllib3.exceptions.NewConnectionError`` (a refused/unresolvable connect)."""


class MaxRetryError(Exception):
    """Fake ``urllib3.exceptions.MaxRetryError`` (urllib3 gave up after retries)."""


class ConnectionResetError(Exception):  # noqa: A001 — mirrors the builtin transport class
    """Fake ``ConnectionResetError`` (a dropped socket)."""


class ConnectionRefusedError(Exception):  # noqa: A001 — mirrors the builtin transport class
    """Fake ``ConnectionRefusedError`` (a refused socket)."""


class gaierror(Exception):
    """Fake ``socket.gaierror`` (``"Name or service not known"``)."""


@pytest.mark.parametrize(
    "exc",
    [
        # class-name matched (the message is irrelevant — caught by the transport class)
        ConnectionError("Failed to resolve 'api.groww.in'"),
        ConnectionError("nondescript connection failure"),  # name wins even with no hint
        NameResolutionError("Failed to resolve 'api.groww.in' ([Errno -3])"),
        NewConnectionError("Failed to establish a new connection"),
        MaxRetryError("Max retries exceeded with url: /historical"),
        ConnectionResetError("[Errno 104] Connection reset by peer"),
        ConnectionRefusedError("[Errno 111] Connection refused"),
        gaierror("[Errno -2] Name or service not known"),
    ],
)
def test_transient_network_error_classified_retryable(exc: Exception) -> None:
    """A transport-layer network / DNS failure → RETRYABLE :class:`GrowwNetworkError`.

    DEC-GROWW-NETWORK-ERROR-RETRY-001 — the 28h-pull forensic: a transient
    ``"Failed to resolve api.groww.in"`` DNS outage permanently quarantined 218
    option contracts because the failure fell to the terminal base. It must now map
    to a RETRYABLE error (matched by the transport class NAME, so neither
    ``requests`` nor ``urllib3`` need be importable).
    """
    mapped = e.from_groww_exception(exc)
    assert type(mapped) is e.GrowwNetworkError
    assert e.is_retryable(mapped) is True
    assert e.is_rate_limit(mapped) is False  # a DNS blip is NOT a quota signal
    assert e.is_auth_failure(mapped) is False
    assert isinstance(mapped, e.GrowwError)
    assert isinstance(mapped, BruteXError)
    assert type(exc).__name__ in str(mapped)  # message preserved for the audit log


@pytest.mark.parametrize(
    "message",
    [
        "Failed to resolve 'api.groww.in'",
        "Temporary failure in name resolution",
        "nodename nor servname provided, or not known",
        "Name resolution failure",
        "Connection reset by peer",
        "('Connection aborted.', ...)",
        "Connection refused",
        "Connection broken: ...",
        "Max retries exceeded with url",
    ],
)
def test_generic_exception_network_message_is_retryable(message: str) -> None:
    """A GENERIC exception whose CLASS NAME is unknown but whose MESSAGE names a DNS /

    connection failure is still recognised as a RETRYABLE :class:`GrowwNetworkError`
    by the message-hint fallback (DEC-GROWW-NETWORK-ERROR-RETRY-001) — the belt to
    the class-name suspenders, for a wrapper whose class is not a known transport
    name.
    """
    mapped = e.from_groww_exception(_GenericGrowwAPIException(message))
    assert type(mapped) is e.GrowwNetworkError
    assert e.is_retryable(mapped) is True
    assert e.is_rate_limit(mapped) is False


def test_network_hint_priority_below_rate_limit_timeout_server() -> None:
    """A message co-mentioning a connection AND a 429 / timeout / 5xx never reads as

    a plain network error — the more specific transient class wins (priority order
    preserved: rate-limit > timeout > server > network).
    """
    # rate-limit beats a co-mentioned connection
    assert (
        type(e.from_groww_exception(_GenericGrowwAPIException("rate limit hit; connection reset")))
        is e.GrowwRateLimitError
    )
    # timeout beats a co-mentioned connection
    assert (
        type(
            e.from_groww_exception(_GenericGrowwAPIException("request timed out: connection reset"))
        )
        is e.GrowwTimeoutError
    )
    # 5xx server error beats a co-mentioned connection
    assert (
        type(
            e.from_groww_exception(
                _GenericGrowwAPIException("internal server error; connection refused")
            )
        )
        is e.GrowwServerError
    )


def test_typed_groww_class_wins_over_network_name_collision() -> None:
    """A typed Groww SDK class always wins, even if its bare name could look network-ish.

    The network class-name match is checked ONLY after the typed-SDK map, so a real
    ``GrowwAPINotFoundException`` stays a terminal :class:`GrowwNotFoundError` and is
    never re-classified as a retryable network error.
    """
    mapped = e.from_groww_exception(GrowwAPINotFoundException("symbol not found"))
    assert type(mapped) is e.GrowwNotFoundError
    assert e.is_retryable(mapped) is False


def test_is_rate_limit() -> None:
    assert e.is_rate_limit(e.GrowwRateLimitError("x")) is True
    assert e.is_rate_limit(e.GrowwTimeoutError("x")) is False
    assert e.is_rate_limit(e.GrowwServerError("x")) is False  # 5xx must NOT step the rate ladder
    assert e.is_rate_limit(e.GrowwNetworkError("x")) is False  # DNS blip must NOT step the ladder


def test_is_auth_failure() -> None:
    assert e.is_auth_failure(e.GrowwAuthError("x")) is True
    assert e.is_auth_failure(e.GrowwBadRequestError("x")) is False
    # DEC-GROWW-TOKEN-DISK-CACHE-001: the throttle is a GrowwAuthError subclass, so it
    # STILL reads as an auth failure (every `except GrowwAuthError` site keeps matching).
    assert e.is_auth_failure(e.GrowwTokenIssuanceThrottled("x")) is True


def test_token_issuance_throttle_is_retryable_auth_subclass() -> None:
    """DEC-GROWW-TOKEN-DISK-CACHE-001: the issuance throttle is the ONE retryable auth error."""
    throttle = e.GrowwTokenIssuanceThrottled("x")
    assert isinstance(throttle, e.GrowwAuthError)  # subclass — every auth site matches
    assert e.is_retryable(throttle) is True  # but it IS retryable (backs off / re-pulls)
    assert e.is_rate_limit(throttle) is False  # NOT a data-fetch rate-limit (no ladder step)


@pytest.mark.parametrize(
    ("exc", "retryable"),
    [
        (e.GrowwRateLimitError("x"), True),
        (e.GrowwTimeoutError("x"), True),
        (e.GrowwEmptyResponseError("x"), True),
        (e.GrowwServerError("x"), True),
        (e.GrowwNetworkError("x"), True),
        (e.GrowwTokenIssuanceThrottled("x"), True),
        (e.GrowwBadRequestError("x"), False),
        (e.GrowwAuthError("x"), False),
        (e.GrowwNotFoundError("x"), False),
        (e.GrowwFeedError("x"), False),
        (e.GrowwError("x"), False),
    ],
)
def test_is_retryable(exc: e.GrowwError, retryable: bool) -> None:
    assert e.is_retryable(exc) is retryable
