"""Unit tests for ``brutex.adapters.groww.auth`` (DEC-DHAN-GROWW-REMIGRATE-001 / PR-E).

All side effects are injected, so no ``pyotp`` / ``growwapi`` is needed.
"""

# whitebox: directly exercises _vendor_status / _sanitize_reason
# pyright: reportPrivateUsage=none

from __future__ import annotations

import pytest
from brutex.adapters.groww import auth
from brutex.adapters.groww.errors import GrowwAuthError
from structlog.testing import capture_logs

_ENV = {auth.ENV_API_KEY: "key-123", auth.ENV_TOTP_SECRET: "secret-xyz"}


def test_resolve_credentials_from_env() -> None:
    creds = auth.resolve_groww_credentials(_ENV)
    assert creds.api_key == "key-123"
    assert creds.totp_secret == "secret-xyz"


@pytest.mark.parametrize(
    "env",
    [
        {},
        {auth.ENV_API_KEY: "k"},  # missing secret
        {auth.ENV_TOTP_SECRET: "s"},  # missing key
        {auth.ENV_API_KEY: "  ", auth.ENV_TOTP_SECRET: "s"},  # blank key
    ],
)
def test_resolve_credentials_missing_raises(env: dict[str, str]) -> None:
    with pytest.raises(GrowwAuthError, match="missing Groww credentials"):
        auth.resolve_groww_credentials(env)


def test_creds_present_true_and_false() -> None:
    assert auth.groww_creds_present(_ENV) is True
    assert auth.groww_creds_present({}) is False


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


def test_sanitize_reason_skips_blank_secret_and_masks_longest_first() -> None:
    """Blank secrets are skipped; an overlapping pair masks the longest first."""
    # a blank secret must not corrupt the text (str.replace("", …) would)
    assert auth._sanitize_reason("hello world", "") == "hello world"
    # the longer secret ("abc-def") is fully masked even though "abc" is also a secret
    assert auth._sanitize_reason("token abc-def here", "abc", "abc-def") == "token *** here"
