"""Unit tests for ``brutex.adapters.groww.auth`` (DEC-DHAN-GROWW-REMIGRATE-001 / PR-E).

All side effects are injected, so no ``pyotp`` / ``growwapi`` is needed.
"""

# whitebox: directly exercises _vendor_status / _sanitize_reason
# pyright: reportPrivateUsage=none

from __future__ import annotations

from collections.abc import Callable

import pytest
from brutex.adapters.groww import auth
from brutex.adapters.groww.errors import (
    GrowwAuthError,
    GrowwTokenIssuanceThrottled,
    is_retryable,
)
from structlog.testing import capture_logs


def test_get_access_token_happy_path() -> None:
    creds = auth.GrowwCredentials(api_key="key-123", totp_secret="secret-xyz")
    seen: dict[str, str] = {}

    def fake_totp(secret: str) -> str:
        seen["secret"] = secret
        return "654321"

    def fake_factory(api_key: str, totp: str) -> str:
        seen["api_key"] = api_key
        seen["totp"] = totp
        return "ACCESS-TOKEN"

    info = auth.get_access_token(creds, token_factory=fake_factory, totp_minter=fake_totp)
    assert info.access_token == "ACCESS-TOKEN"
    # the minted code is actually threaded through to the factory (kills a drop-totp mutant)
    assert seen == {"secret": "secret-xyz", "api_key": "key-123", "totp": "654321"}


def test_get_access_token_surfaces_sanitized_vendor_reason() -> None:
    """The wrapped error surfaces the vendor reason (so the operator sees WHY)."""
    creds = auth.GrowwCredentials(api_key="key-123", totp_secret="secret-xyz")

    class _ApiExc(RuntimeError):
        code = 400

    def boom_factory(_api_key: str, _totp: str) -> str:
        raise _ApiExc("Groww API Error 400: Invalid TOTP")

    with pytest.raises(GrowwAuthError) as excinfo:
        auth.get_access_token(creds, token_factory=boom_factory, totp_minter=lambda _s: "111111")
    exc = excinfo.value
    # (a) the sanitized vendor reason is surfaced — the operator finally sees WHY
    assert "Groww API Error 400: Invalid TOTP" in str(exc)
    assert exc.detail == "Groww API Error 400: Invalid TOTP"
    # the contract type is preserved — callers catching GrowwAuthError still work
    assert isinstance(exc, GrowwAuthError)


@pytest.mark.parametrize(
    "vendor_message",
    [
        # the secret appears verbatim inside the vendor message …
        "auth failed for api_key=key-123 totp=111111 secret=secret-xyz",
        # … or substring-embedded in a URL-ish blob
        "https://groww/auth?key=key-123&secret=secret-xyz&code=111111 rejected",
    ],
)
def test_get_access_token_never_leaks_any_secret(vendor_message: str) -> None:
    """(b) the api_key / TOTP secret / minted code are NEVER in the surfaced text."""
    creds = auth.GrowwCredentials(api_key="key-123", totp_secret="secret-xyz")

    def boom_factory(_api_key: str, _totp: str) -> str:
        raise RuntimeError(vendor_message)

    with pytest.raises(GrowwAuthError) as excinfo:
        auth.get_access_token(creds, token_factory=boom_factory, totp_minter=lambda _s: "111111")
    surfaced = str(excinfo.value)
    for secret in ("key-123", "secret-xyz", "111111"):
        assert secret not in surfaced
        assert excinfo.value.detail is not None
        assert secret not in excinfo.value.detail
    # the scrub mark proves the reason was carried (not dropped wholesale)
    assert "***" in surfaced


def test_get_access_token_emits_sanitized_structlog_event() -> None:
    """(c) the groww.token.issuance_failed event carries the reason + status, no secret."""
    creds = auth.GrowwCredentials(api_key="key-123", totp_secret="secret-xyz")

    class _ApiExc(RuntimeError):
        code = 400

    def boom_factory(_api_key: str, _totp: str) -> str:
        raise _ApiExc("Groww API Error 400: bad secret-xyz key-123 111111")

    with capture_logs() as logs, pytest.raises(GrowwAuthError):
        auth.get_access_token(creds, token_factory=boom_factory, totp_minter=lambda _s: "111111")

    events = [e for e in logs if e["event"] == "groww.token.issuance_failed"]
    assert len(events) == 1
    event = events[0]
    assert event["status"] == 400
    assert event["error_type"] == "_ApiExc"
    rendered = repr(event)
    for secret in ("key-123", "secret-xyz", "111111"):
        assert secret not in rendered


def test_get_access_token_status_none_when_no_code() -> None:
    """A plain exception (no ``code``) logs status=None (the bool/None branches)."""
    creds = auth.GrowwCredentials(api_key="key-123", totp_secret="secret-xyz")

    def boom_factory(_api_key: str, _totp: str) -> str:
        raise RuntimeError("opaque vendor failure")

    with capture_logs() as logs, pytest.raises(GrowwAuthError):
        auth.get_access_token(creds, token_factory=boom_factory, totp_minter=lambda _s: "111111")

    events = [e for e in logs if e["event"] == "groww.token.issuance_failed"]
    assert events[0]["status"] is None


def test_vendor_status_only_numeric_survives() -> None:
    """``_vendor_status`` keeps an int / digit-string but rejects bool + free text.

    Only a genuine numeric status survives — a free-text ``code`` (an attacker /
    SDK-controlled string) is dropped to ``None`` so it can never reach the
    ``status`` log field unscrubbed.
    """

    class _BoolCode(RuntimeError):
        code = True  # an int subclass — must NOT be read as a status

    class _IntCode(RuntimeError):
        code = 400

    class _DigitStrCode(RuntimeError):
        code = "503"

    class _TextCode(RuntimeError):
        code = "secret-xyz-embedded-503"  # NON-digit string — must be dropped

    assert auth._vendor_status(_BoolCode("x")) is None
    assert auth._vendor_status(_IntCode("x")) == 400
    assert auth._vendor_status(_DigitStrCode("x")) == 503  # coerced to int
    assert auth._vendor_status(_TextCode("x")) is None  # free text is dropped
    assert auth._vendor_status(RuntimeError("no code attr")) is None


def test_vendor_status_nonascii_digit_code_returns_none_never_raises() -> None:
    """A non-ASCII Unicode digit ``code`` returns ``None`` instead of raising.

    DEC-GROWW-ERRORCODE-ASCII-GUARD-001 regression — ``'²'.isdigit()`` is True but
    ``int('²')`` raises, so pre-fix ``_vendor_status`` escaped a raw ``ValueError``
    (production-unreachable in the AWS-only read-only config — the mint path — but
    the same bug class as the errors.py mapping site). An Nd-category digit string
    (``'٥٠٣'``) coerced fine pre-fix; the ASCII guard now drops it to ``None`` too
    (sanctioned narrowing — the status log field stays canonical-ASCII-only).
    """

    class _SuperscriptCode(RuntimeError):
        code = "²"  # No-category digit — int() raises

    class _ArabicIndicCode(RuntimeError):
        code = "٥٠٣"  # Nd-category digits — int() coerces, but non-ASCII is dropped

    class _AsciiDigitCode(RuntimeError):
        code = "429"  # ASCII digits — parity pin, byte-identical to pre-guard

    assert auth._vendor_status(_SuperscriptCode("x")) is None
    assert auth._vendor_status(_ArabicIndicCode("x")) is None
    assert auth._vendor_status(_AsciiDigitCode("x")) == 429


def test_sanitize_reason_skips_blank_secret_and_masks_longest_first() -> None:
    """Blank secrets are skipped; an overlapping pair masks the longest first."""
    # a blank secret must not corrupt the text (str.replace("", …) would)
    assert auth._sanitize_reason("hello world", "") == "hello world"
    # the longer secret ("abc-def") is fully masked even though "abc" is also a secret
    assert auth._sanitize_reason("token abc-def here", "abc", "abc-def") == "token *** here"


# --------------------------------------------------------------------------- #
# DEC-GROWW-TOKEN-DISK-CACHE-001 — issuance-throttle classification (M2 order)
# --------------------------------------------------------------------------- #
_THROTTLE_CREDS = auth.GrowwCredentials(api_key="key-123", totp_secret="secret-xyz")


def _raise(exc: Exception) -> Callable[[str, str], str]:
    def _factory(_api_key: str, _totp: str) -> str:
        raise exc

    return _factory


class TestIssuanceThrottle:
    """A token-issuance RATE-LIMIT becomes the RETRYABLE throttle subclass, ordered
    BEFORE the generic terminal arm (hostile-review M2)."""

    def test_429_code_classifies_throttle(self) -> None:
        class _RateExc(RuntimeError):
            code = 429

        with pytest.raises(GrowwTokenIssuanceThrottled) as ei:
            auth.get_access_token(
                _THROTTLE_CREDS,
                token_factory=_raise(_RateExc("Groww API Error 429: Rate limit exceeded")),
                totp_minter=lambda _s: "111111",
            )
        # subclass of GrowwAuthError AND retryable
        assert isinstance(ei.value, GrowwAuthError)
        assert is_retryable(ei.value) is True
        assert "throttled" in str(ei.value)

    def test_rate_limit_message_on_generic_exc_classifies_throttle(self) -> None:
        with pytest.raises(GrowwTokenIssuanceThrottled):
            auth.get_access_token(
                _THROTTLE_CREDS,
                token_factory=_raise(RuntimeError("Rate limit has breached for your request")),
                totp_minter=lambda _s: "111111",
            )

    def test_sdk_ratelimit_class_name_classifies_throttle(self) -> None:
        # The SDK's GrowwAPIRateLimitException is matched by CLASS NAME in errors.py.
        class GrowwAPIRateLimitException(RuntimeError):
            pass

        with pytest.raises(GrowwTokenIssuanceThrottled):
            auth.get_access_token(
                _THROTTLE_CREDS,
                token_factory=_raise(GrowwAPIRateLimitException("limit")),
                totp_minter=lambda _s: "111111",
            )

    def test_non_ratelimit_stays_terminal_auth_error(self) -> None:
        class _ApiExc(RuntimeError):
            code = 400

        with pytest.raises(GrowwAuthError) as ei:
            auth.get_access_token(
                _THROTTLE_CREDS,
                token_factory=_raise(_ApiExc("Groww API Error 400: Invalid TOTP")),
                totp_minter=lambda _s: "111111",
            )
        # NOT the throttle subclass, NOT retryable — a wrong TOTP is terminal.
        assert not isinstance(ei.value, GrowwTokenIssuanceThrottled)
        assert is_retryable(ei.value) is False

    def test_throttle_emits_distinct_structlog_event_no_secret(self) -> None:
        class _RateExc(RuntimeError):
            code = 429

        with capture_logs() as logs, pytest.raises(GrowwTokenIssuanceThrottled):
            auth.get_access_token(
                _THROTTLE_CREDS,
                token_factory=_raise(_RateExc("Rate limit exceeded for key-123 secret-xyz 111111")),
                totp_minter=lambda _s: "111111",
            )
        throttled = [e for e in logs if e["event"] == "groww.token.issuance_throttled"]
        assert len(throttled) == 1
        assert throttled[0]["status"] == 429
        # the terminal-failure event must NOT fire on a throttle
        assert not [e for e in logs if e["event"] == "groww.token.issuance_failed"]
        rendered = repr(throttled[0])
        for secret in ("key-123", "secret-xyz", "111111"):
            assert secret not in rendered

    def test_throttle_never_leaks_secret_in_message(self) -> None:
        class _RateExc(RuntimeError):
            code = 429

        with pytest.raises(GrowwTokenIssuanceThrottled) as ei:
            auth.get_access_token(
                _THROTTLE_CREDS,
                token_factory=_raise(_RateExc("429 for key-123 secret-xyz 111111")),
                totp_minter=lambda _s: "111111",
            )
        surfaced = str(ei.value)
        assert ei.value.detail is not None
        for secret in ("key-123", "secret-xyz", "111111"):
            assert secret not in surfaced
            assert secret not in ei.value.detail

    def test_is_issuance_throttle_helper(self) -> None:
        class _Rate(RuntimeError):
            code = 429

        class _Plain(RuntimeError):
            code = 400

        assert auth._is_issuance_throttle(_Rate("x")) is True
        assert auth._is_issuance_throttle(_Plain("x")) is False
