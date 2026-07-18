"""Unit tests for :mod:`brutex.adapters.dhan.errors` — 100% line+branch.

Covers HTTP-status + DH-9xx classification, transport-exception mapping, and every
retry/auth predicate. The DH-902/903/904/907 codes stay terminal (UNVERIFIED).
"""

from __future__ import annotations

import json

from brutex.adapters.dhan import errors as e


def test_from_dhan_response_verified_dh_codes() -> None:
    assert isinstance(e.from_dhan_response(400, {"errorCode": "DH-901"}), e.DhanAuthError)
    assert isinstance(e.from_dhan_response(200, {"errorCode": "DH-905"}), e.DhanBadRequestError)
    got = e.from_dhan_response(200, {"errorCode": "DH-906"})
    assert type(got) is e.DhanError  # terminal base (Order_Error, not a data-pull retry)


def test_from_dhan_response_unverified_dh_codes_are_terminal() -> None:
    for code in sorted(e.DH_CODE_UNVERIFIED):
        got = e.from_dhan_response(500, {"errorCode": code})
        assert type(got) is e.DhanError  # UNVERIFIED → terminal base, never blindly retried
        assert not e.is_retryable(got)


def test_from_dhan_response_nested_sdk_envelope() -> None:
    body = {
        "status": "failure",
        "remarks": {"error_code": "dh-901", "error_message": "bad token"},
    }
    got = e.from_dhan_response(200, body)
    assert isinstance(got, e.DhanAuthError)
    assert "bad token" in str(got)


def test_from_dhan_response_http_status_when_no_dh_code() -> None:
    assert isinstance(e.from_dhan_response(429, {}), e.DhanRateLimitError)
    assert isinstance(e.from_dhan_response(408, {}), e.DhanTimeoutError)
    assert isinstance(e.from_dhan_response(504, {}), e.DhanTimeoutError)
    assert isinstance(e.from_dhan_response(500, {}), e.DhanServerError)
    assert isinstance(e.from_dhan_response(502, {}), e.DhanServerError)
    assert isinstance(e.from_dhan_response(503, {}), e.DhanServerError)
    assert isinstance(e.from_dhan_response(400, {}), e.DhanBadRequestError)
    assert isinstance(e.from_dhan_response(401, {}), e.DhanAuthError)
    assert isinstance(e.from_dhan_response(403, {}), e.DhanAuthError)
    assert isinstance(e.from_dhan_response(404, {}), e.DhanNotFoundError)


def test_from_dhan_response_unknown_status_is_terminal_base() -> None:
    got = e.from_dhan_response(418, {})
    assert type(got) is e.DhanError
    assert "418" in str(got)


def test_from_dhan_response_detail_from_error_message() -> None:
    got = e.from_dhan_response(400, {"errorMessage": "  missing param  "})
    assert "missing param" in str(got)


def test_from_dhan_response_detail_fallback_when_no_message() -> None:
    got = e.from_dhan_response(500, {"weird": 1})
    assert "keys" in str(got)  # the shape-summary fallback


def test_extract_dh_code_blank_top_and_nested_none() -> None:
    # blank top-level errorCode + no remarks → HTTP status classification
    got = e.from_dhan_response(429, {"errorCode": "   "})
    assert isinstance(got, e.DhanRateLimitError)
    # remarks present but not a mapping → no code
    got2 = e.from_dhan_response(404, {"remarks": "oops"})
    assert isinstance(got2, e.DhanNotFoundError)
    # remarks mapping with blank nested code → no code
    got3 = e.from_dhan_response(500, {"remarks": {"error_code": ""}})
    assert isinstance(got3, e.DhanServerError)


def test_detail_nested_error_message_used() -> None:
    got = e.from_dhan_response(400, {"remarks": {"error_message": "  nested detail  "}})
    assert "nested detail" in str(got)


def test_from_transport_exception_empty_body() -> None:
    got = e.from_transport_exception(json.JSONDecodeError("Expecting value", "", 0))
    assert isinstance(got, e.DhanEmptyResponseError)
    assert e.is_retryable(got)


def test_from_transport_exception_timeout_name() -> None:
    class ReadTimeout(Exception):
        pass

    ReadTimeout.__name__ = "ReadTimeout"
    got = e.from_transport_exception(ReadTimeout("read timed out"))
    assert isinstance(got, e.DhanTimeoutError)


def test_from_transport_exception_network_name() -> None:
    got = e.from_transport_exception(ConnectionError("Failed to resolve api.dhan.co"))
    assert isinstance(got, e.DhanNetworkError)


def test_from_transport_exception_unknown_is_terminal_base() -> None:
    got = e.from_transport_exception(ValueError("some bug"))
    assert type(got) is e.DhanError
    assert not e.is_retryable(got)


def test_predicates_rate_limit_and_auth() -> None:
    assert e.is_rate_limit(e.DhanRateLimitError("x"))
    assert not e.is_rate_limit(e.DhanTimeoutError("x"))
    assert e.is_auth_failure(e.DhanAuthError("x"))
    assert e.is_auth_failure(e.DhanTokenStillStaleError("x"))
    assert not e.is_auth_failure(e.DhanServerError("x"))


def test_is_terminal_auth_failure() -> None:
    assert e.is_terminal_auth_failure(e.DhanAuthError("dead token"))
    # the ONE retryable auth-family error is NOT terminal
    assert not e.is_terminal_auth_failure(e.DhanTokenStillStaleError("stale"))
    # a non-auth error is never a terminal AUTH failure
    assert not e.is_terminal_auth_failure(e.DhanServerError("5xx"))


def test_is_retryable_membership() -> None:
    retryable = (
        e.DhanRateLimitError("x"),
        e.DhanTimeoutError("x"),
        e.DhanEmptyResponseError("x"),
        e.DhanServerError("x"),
        e.DhanNetworkError("x"),
        e.DhanTokenStillStaleError("x"),
    )
    for exc in retryable:
        assert e.is_retryable(exc)
    terminal = (
        e.DhanError("x"),
        e.DhanBadRequestError("x"),
        e.DhanNotFoundError("x"),
        e.DhanAuthError("x"),
        e.DhanFeedError("x"),
    )
    for exc in terminal:
        assert not e.is_retryable(exc)


def test_steps_timeout_ladder() -> None:
    assert e.steps_timeout_ladder(e.DhanTimeoutError("x"))
    assert e.steps_timeout_ladder(e.DhanServerError("x"))
    assert e.steps_timeout_ladder(e.DhanNetworkError("x"))
    # a rate-limit keeps its own path; a token race is auth-family; a terminal isn't retryable
    assert not e.steps_timeout_ladder(e.DhanRateLimitError("x"))
    assert not e.steps_timeout_ladder(e.DhanTokenStillStaleError("x"))
    assert not e.steps_timeout_ladder(e.DhanBadRequestError("x"))


def test_sdk_numeric_scheme_kept_separate() -> None:
    # the SDK 8xx scheme is documented but NOT a DH-9xx code — never conflated
    assert e.SDK_NUMERIC_CODES[807] == "token-expired"
    assert 807 not in e.HTTP_STATUS_TO_ERROR
