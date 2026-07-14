"""Tests for :mod:`brutex.adapters.dhan.errors` — the flat-dict classifier + retry rules."""

from __future__ import annotations

import pytest
from brutex.adapters.dhan.errors import (
    DhanAuthError,
    DhanBadRequestError,
    DhanEmptyResponseError,
    DhanError,
    DhanServerError,
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
    is_terminal_pull_stop,
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


def test_classify_dh902_is_subscription_terminal() -> None:
    exc = classify_dhan_failure({"errorCode": "DH-902", "errorMessage": "not subscribed"})
    assert isinstance(exc, DhanSubscriptionError)
    assert not is_retryable(exc)
    assert not is_auth_failure(exc)


def test_classify_dh905_relabel_is_bad_request_not_subscription() -> None:
    """DH-905 RELABEL pin (design §2, A-MINOR-2 verified): the annexure documents
    DH-905 as the INPUT exception — a terminal bad request — NOT the subscription
    class (that is 806/DH-902). The original branch mislabeled it; this pin keeps
    the relabel honest against a revert.
    """
    exc = classify_dhan_failure({"errorCode": "DH-905", "errorMessage": "input exception"})
    assert isinstance(exc, DhanBadRequestError)
    assert not isinstance(exc, DhanSubscriptionError)
    assert not is_retryable(exc)


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
        ("dh-902", DhanSubscriptionError),
        ("DH-905", DhanBadRequestError),
    ],
)
def test_error_code_case_insensitive(hint_code: str, expected: type[DhanError]) -> None:
    exc = classify_dhan_failure({"errorCode": hint_code, "errorMessage": "x"})
    assert isinstance(exc, expected)


# ─────────────────────────────────────────────────────────────────────────────
# The documented code table (design §2) × BOTH remark-key spellings (design §0).
# The live SDK envelope is snake_case (``dhan_http.py`` ``_parse_response`` builds
# {'error_code': …, 'error_type': …, 'error_message': …}); the docs' raw-HTTP
# shape is camelCase — every arm must classify identically under both.
# ─────────────────────────────────────────────────────────────────────────────


def _remarks(code: object, message: str, *, spelling: str) -> dict[str, object]:
    """Build a remarks dict in the requested key spelling."""
    if spelling == "camel":
        return {"errorCode": code, "errorMessage": message}
    return {"error_code": code, "error_message": message}


_SPELLINGS = ("camel", "snake")


@pytest.mark.parametrize("spelling", _SPELLINGS)
@pytest.mark.parametrize(
    ("code", "expected"),
    [
        # Rate limit → retryable + is_rate_limit (AIMD feedback).
        ("805", DhanThrottleError),
        ("DH-904", DhanThrottleError),
        # Subscription → terminal + pull-wide stop.
        ("806", DhanSubscriptionError),
        ("DH-902", DhanSubscriptionError),
        # Auth family → drives the SSM re-read.
        ("807", DhanAuthError),
        ("808", DhanAuthError),
        ("809", DhanAuthError),
        ("810", DhanAuthError),
        ("DH-901", DhanAuthError),
        # Bad request → terminal, never retried.
        ("804", DhanBadRequestError),
        ("811", DhanBadRequestError),
        ("812", DhanBadRequestError),
        ("813", DhanBadRequestError),
        ("814", DhanBadRequestError),
        ("DH-905", DhanBadRequestError),
        ("DH-907", DhanBadRequestError),
        # Server-side → retryable, NOT rate-limit, NOT auth.
        ("800", DhanServerError),
        ("DH-908", DhanServerError),
        ("DH-909", DhanServerError),
    ],
)
def test_code_table_both_spellings(spelling: str, code: str, expected: type[DhanError]) -> None:
    exc = classify_dhan_failure(_remarks(code, "vendor text", spelling=spelling))
    assert isinstance(exc, expected), f"{code} ({spelling}) → {type(exc).__name__}"


@pytest.mark.parametrize("spelling", _SPELLINGS)
def test_numeric_int_codes_normalised(spelling: str) -> None:
    # A numeric Data-API code may arrive as an ``int`` — normalised via str().
    exc = classify_dhan_failure(_remarks(805, "too many", spelling=spelling))
    assert isinstance(exc, DhanThrottleError)
    assert is_rate_limit(exc)


def test_805_throttle_by_code_phrase_free() -> None:
    # The CODE arm alone classifies 805 — no throttle phrase anywhere in the body.
    exc = classify_dhan_failure({"error_code": "805", "error_message": "zzz opaque zzz"})
    assert isinstance(exc, DhanThrottleError)
    assert is_rate_limit(exc)
    assert is_retryable(exc)


def test_806_subscription_message_names_cause_and_fix() -> None:
    exc = classify_dhan_failure({"error_code": "806", "error_message": "Data APIs not subscribed"})
    text = str(exc)
    assert "₹499/mo" in text
    assert "25-trades-in-30d" in text
    assert "web.dhan.co" in text
    assert "No data was fetched" in text


def test_807_message_names_token_expiry() -> None:
    exc = classify_dhan_failure({"error_code": "807", "error_message": "expired"})
    assert isinstance(exc, DhanAuthError)
    assert "access token expired" in str(exc)


def test_810_message_names_client_id() -> None:
    exc = classify_dhan_failure({"error_code": "810", "error_message": "bad id"})
    assert isinstance(exc, DhanAuthError)
    assert "client id invalid" in str(exc)


def test_813_message_names_security_id_map_and_probe_gate() -> None:
    exc = classify_dhan_failure({"error_code": "813", "error_message": "Invalid SecurityId"})
    assert isinstance(exc, DhanBadRequestError)
    text = str(exc)
    assert "DHAN_INDEX_SECURITY_IDS" in text
    assert "scrip-master" in text
    assert "G-C" in text
    assert repr("Invalid SecurityId") in text  # the vendor errorMessage, repr()'d


def test_server_error_retryable_not_rate_limit_not_auth() -> None:
    for code in ("800", "DH-908", "DH-909"):
        exc = classify_dhan_failure({"error_code": code, "error_message": "boom"})
        assert isinstance(exc, DhanServerError)
        assert is_retryable(exc)
        assert not is_rate_limit(exc)
        assert not is_auth_failure(exc)
        # The gentle ladder: server stress may ease the pull rate, never the rate ceiling.
        assert steps_timeout_ladder(exc) is True


def test_snake_case_is_the_live_sdk_envelope_drift_pin() -> None:
    """SDK drift pin (design §0, judge-found BLOCKER): the official ``dhanhq`` 2.2.0
    SDK's ``dhan_http.py`` ``_parse_response`` builds failure remarks with
    SNAKE_CASE keys — ``{'error_code': json.get('errorCode'), 'error_type': …,
    'error_message': …}`` — so the live envelope the client actually sees is
    snake_case; the camelCase spelling is kept for raw-HTTP / docs-shaped payloads.
    Pre-fix the classifier read ONLY camelCase, so against the real SDK every
    failure fell to the terminal base and the DH-901 SSM re-read self-heal was
    production-dead. This pin fails if the snake_case arm is ever dropped.
    """
    snake_only = {"error_code": "DH-901", "error_message": "Invalid_Authentication"}
    exc = classify_dhan_failure(snake_only)
    assert isinstance(exc, DhanAuthError), (
        "the snake_case (live-SDK) spelling must classify identically to camelCase"
    )
    # And the phrase-heuristic text reads snake_case too (a code-less throttle body).
    throttle_snake = {"error_type": "Rate Limit", "error_message": "too many requests"}
    assert isinstance(classify_dhan_failure(throttle_snake), DhanThrottleError)


def test_phrase_heuristics_stay_the_fallback() -> None:
    # A code-less body still classifies by phrase (throttle / empty-body).
    assert isinstance(
        classify_dhan_failure({"errorType": "Throttle", "errorMessage": "slow down"}),
        DhanThrottleError,
    )
    assert isinstance(
        classify_dhan_failure({"error_message": "Expecting value"}),
        DhanEmptyResponseError,
    )


def test_unknown_code_stays_terminal_base() -> None:
    exc = classify_dhan_failure({"error_code": "999", "error_message": "mystery"})
    assert type(exc) is DhanError
    assert not is_retryable(exc)


# ─────────────────────────────────────────────────────────────────────────────
# is_terminal_pull_stop — the pull-wide global-stop carve-out (design §3).
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (DhanAuthError("dead credential"), True),
        (DhanTokenReadOnlyError("no write"), True),  # DhanAuthError subclass
        (DhanSubscriptionError("no sub"), True),
        (DhanTokenStillStaleError("stale"), True),
        (DhanBadRequestError("bad input"), False),
        (DhanServerError("blip"), False),
        (DhanThrottleError("slow"), False),
        (DhanEmptyResponseError("empty"), False),
        (DhanTimeoutError("hung"), False),
        (DhanVendorDriftError("drift"), False),
        (DhanError("unknown"), False),
        (RuntimeError("not dhan"), False),  # a non-Dhan exception reads honestly False
    ],
)
def test_is_terminal_pull_stop_truth_table(exc: BaseException, expected: bool) -> None:
    assert is_terminal_pull_stop(exc) is expected


def test_bad_request_is_terminal_never_retried() -> None:
    exc = DhanBadRequestError("813: invalid SecurityId")
    assert not is_retryable(exc)
    assert not is_rate_limit(exc)
    assert not is_auth_failure(exc)
    assert not steps_timeout_ladder(exc)
