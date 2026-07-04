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


# ──────────────────────────────────────────────────────────────────────────
# Socket CONNECT / READ timeouts → RETRYABLE GrowwTimeoutError, NOT rate-limit
# (DEC-GROWW-HTTP-SOCKET-TIMEOUT-001 — once the SDK's requests carry a
# (connect, read) timeout, a half-open / dead socket raises one of these).
# Fakes named EXACTLY like the requests / stdlib timeout classes (matched by
# class NAME, so ``requests`` need not be importable + the message is irrelevant).
# ──────────────────────────────────────────────────────────────────────────
class ReadTimeout(Exception):
    """Fake ``requests.exceptions.ReadTimeout`` (a stalled mid-transfer socket)."""


class ConnectTimeout(Exception):
    """Fake ``requests.exceptions.ConnectTimeout`` (a dead-peer TCP handshake).

    In real ``requests`` this is ALSO a ``ConnectionError`` subclass — the test that it
    still reads as a TIMEOUT (not a network blip) is the priority proof below.
    """


class Timeout(Exception):
    """Fake ``requests.exceptions.Timeout`` (the base connect/read timeout)."""


@pytest.mark.parametrize(
    "exc",
    [
        # class-name matched (the message is irrelevant — caught by the transport class)
        ReadTimeout("HTTPSConnectionPool(host='api.groww.in'): Read timed out. (read timeout=30)"),
        ReadTimeout("nondescript stall"),  # name wins even with no message hint
        ConnectTimeout("Connection to api.groww.in timed out. (connect timeout=10)"),
        ConnectTimeout("nondescript"),
        Timeout("timed out"),
    ],
)
def test_socket_timeout_classified_retryable(exc: Exception) -> None:
    """A socket CONNECT / READ timeout → RETRYABLE :class:`GrowwTimeoutError`.

    DEC-GROWW-HTTP-SOCKET-TIMEOUT-001 — a half-open / dead socket the SDK's injected
    ``(connect, read)`` timeout aborts in seconds. It must map to a RETRYABLE timeout
    (matched by the transport class NAME, so ``requests`` need not be importable + the
    ``requests`` message text can vary by version) so it flows into the existing
    retry → defer → re-pull machinery — NOT a rate-limit (never steps the rate ladder).
    """
    mapped = e.from_groww_exception(exc)
    assert type(mapped) is e.GrowwTimeoutError
    assert e.is_retryable(mapped) is True
    assert e.is_rate_limit(mapped) is False  # a socket timeout is NOT a quota signal
    assert e.is_auth_failure(mapped) is False
    assert isinstance(mapped, e.GrowwError)
    assert isinstance(mapped, BruteXError)
    assert type(exc).__name__ in str(mapped)  # message preserved for the audit log


def test_connect_timeout_reads_as_timeout_not_network() -> None:
    """``ConnectTimeout`` — ALSO a ``ConnectionError`` by inheritance — reads as a TIMEOUT.

    The timeout class-name check runs BEFORE the network class-name check, so a
    ``ConnectTimeout`` (whose OWN class name is ``"ConnectTimeout"``, not in the network
    set) is classified as :class:`GrowwTimeoutError` and rides the gentle timeout ladder,
    never mis-read as a plain :class:`GrowwNetworkError`. (The mirror ``ConnectionError``
    fake — whose name IS in the network set — is asserted network-mapped above, so this
    proves the ``ConnectTimeout``-vs-``ConnectionError`` name split is honoured.)
    """
    mapped = e.from_groww_exception(ConnectTimeout("connect timed out"))
    assert type(mapped) is e.GrowwTimeoutError
    assert not isinstance(mapped, e.GrowwNetworkError)


def test_typed_groww_class_wins_over_timeout_name_collision() -> None:
    """A typed Groww SDK class always wins over the timeout name-match too.

    The timeout class-name match is checked ONLY after the typed-SDK map, so a real
    ``GrowwAPINotFoundException`` (or any typed class) is never re-classified — the
    timeout branch cannot poach a vendor-typed error.
    """
    mapped = e.from_groww_exception(GrowwAPINotFoundException("symbol not found"))
    assert type(mapped) is e.GrowwNotFoundError


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
    # DEC-BROKER-READONLY-REAUTH-REREAD-001: the still-stale read-only re-read is likewise
    # a GrowwAuthError subclass, so `except GrowwAuthError` sites keep matching it too.
    assert e.is_auth_failure(e.GrowwTokenStillStaleError("x")) is True


def test_token_issuance_throttle_is_retryable_auth_subclass() -> None:
    """DEC-GROWW-TOKEN-DISK-CACHE-001: the issuance throttle is a retryable auth error."""
    throttle = e.GrowwTokenIssuanceThrottled("x")
    assert isinstance(throttle, e.GrowwAuthError)  # subclass — every auth site matches
    assert e.is_retryable(throttle) is True  # but it IS retryable (backs off / re-pulls)
    assert e.is_rate_limit(throttle) is False  # NOT a data-fetch rate-limit (no ladder step)


def test_token_still_stale_is_retryable_auth_subclass() -> None:
    """DEC-BROKER-READONLY-REAUTH-REREAD-001: the still-stale re-read is a retryable auth error.

    The read-only broker RE-READ that returns the SAME dead token (the Lambda has not minted
    the fresh one yet) is a GrowwAuthError subclass so every `except GrowwAuthError` keeps
    matching it, but IS retryable so the window quarantines + re-pulls later — never a restart.
    """
    stale = e.GrowwTokenStillStaleError("x")
    assert isinstance(stale, e.GrowwAuthError)  # subclass — every auth site matches
    assert e.is_retryable(stale) is True  # retryable → re-pull later, never a crash-restart
    assert e.is_rate_limit(stale) is False  # NOT a rate-limit (no ladder step)


@pytest.mark.parametrize(
    ("exc", "retryable"),
    [
        (e.GrowwRateLimitError("x"), True),
        (e.GrowwTimeoutError("x"), True),
        (e.GrowwEmptyResponseError("x"), True),
        (e.GrowwServerError("x"), True),
        (e.GrowwNetworkError("x"), True),
        (e.GrowwTokenIssuanceThrottled("x"), True),
        (e.GrowwTokenStillStaleError("x"), True),
        (e.GrowwBadRequestError("x"), False),
        (e.GrowwAuthError("x"), False),
        (e.GrowwNotFoundError("x"), False),
        (e.GrowwFeedError("x"), False),
        (e.GrowwError("x"), False),
    ],
)
def test_is_retryable(exc: e.GrowwError, retryable: bool) -> None:
    assert e.is_retryable(exc) is retryable


# ── is_terminal_auth_failure (DEC-GROWW-AUTH-HALT-001) ───────────────────────


@pytest.mark.parametrize(
    ("exc", "terminal_auth"),
    [
        # The TERMINAL auth shape — a dead credential / re-mint budget exhausted → HALT.
        (e.GrowwAuthError("bad credential"), True),
        # The two RETRYABLE auth-family subclasses — transient by design → NEVER a halt.
        (e.GrowwTokenIssuanceThrottled("mint throttled"), False),
        (e.GrowwTokenStillStaleError("same dead token"), False),
        # Non-auth Groww errors (terminal or not) are NOT the auth halt shape.
        (e.GrowwError("unknown"), False),
        (e.GrowwBadRequestError("bad request"), False),
        (e.GrowwRateLimitError("429"), False),
        (e.GrowwTimeoutError("timeout"), False),
    ],
)
def test_is_terminal_auth_failure(exc: e.GrowwError, terminal_auth: bool) -> None:
    """The ONE pull-halt auth predicate: terminal GrowwAuthError only, never a self-heal shape."""
    assert e.is_terminal_auth_failure(exc) is terminal_auth


def test_is_terminal_auth_failure_non_groww_exception_is_false() -> None:
    """A non-Groww exception (typed BaseException at the halt-path call sites) is never auth."""
    assert e.is_terminal_auth_failure(RuntimeError("boom")) is False
    assert e.is_terminal_auth_failure(BruteXError("domain")) is False


# ── steps_timeout_ladder (DEC-AUTH-LADDER-EXCLUDE-001) ───────────────────────


@pytest.mark.parametrize(
    ("exc", "steps"),
    [
        # SERVER-STRESS transients — candle-endpoint evidence → MAY step the ladder down.
        (e.GrowwTimeoutError("stall"), True),
        (e.GrowwEmptyResponseError("empty body"), True),
        (e.GrowwServerError("502"), True),
        (e.GrowwNetworkError("dns blip"), True),
        # A rate-limit keeps its OWN on_rate_limit path (kills the `not is_rate_limit`
        # conjunct — a mutant dropping it flips this row).
        (e.GrowwRateLimitError("429"), False),
        # The two RETRYABLE auth-family shapes — a mint-endpoint throttle / a token
        # race says NOTHING about candle-endpoint health (kills `not is_auth_failure`).
        (e.GrowwTokenIssuanceThrottled("mint throttled"), False),
        (e.GrowwTokenStillStaleError("same dead token"), False),
        # Terminal shapes never reach the ladder at all (kills the `is_retryable`
        # conjunct — an inverted/weakened predicate flips these rows).
        (e.GrowwError("unknown"), False),
        (e.GrowwAuthError("bad credential"), False),
        (e.GrowwCredentialDeadError("dead credential"), False),
        (e.GrowwBadRequestError("bad request"), False),
        (e.GrowwNotFoundError("404"), False),
        (e.GrowwFeedError("feed"), False),
    ],
)
def test_steps_timeout_ladder(exc: e.GrowwError, steps: bool) -> None:
    """The ONE ladder predicate: retryable AND not-rate-limit AND not-auth-family.

    DEC-AUTH-LADDER-EXCLUDE-001: the AIMD timeout ladder is a SERVER-STRESS signal
    (timeout / empty body / 5xx / network) — a retryable auth-family failure must
    never ease the pull rate. Every error class in the module is enumerated so a
    new class must consciously pick a row (and each conjunct's mutant dies here).
    """
    assert e.steps_timeout_ladder(exc) is steps


def test_steps_timeout_ladder_reexported_from_package() -> None:
    """The package facade re-exports the predicate beside ``is_auth_failure``."""
    from brutex.adapters import groww as groww_pkg

    assert groww_pkg.steps_timeout_ladder is e.steps_timeout_ladder
    assert "steps_timeout_ladder" in groww_pkg.__all__
