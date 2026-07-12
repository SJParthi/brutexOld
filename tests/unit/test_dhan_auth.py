"""Tests for the Dhan headless mint / renew boundary (DEC-DHAN-AUTH-001).

Every transport exception must be caught + re-raised SCRUBBED with no ``__cause__`` leak; a
terminal invalid-credential is NEVER retried (call-count == 1); the TOTP is recomputed per
attempt; the throttle / transport ladder backs off then raises typed.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from brutex.adapters.dhan import auth as auth_mod
from brutex.adapters.dhan.auth import (
    MintedToken,
    mint_access_token,
    renew_access_token,
)
from brutex.adapters.dhan.errors import (
    DhanAuthError,
    DhanAuthInvalidCredentials,
    DhanAuthTransportError,
    DhanTokenMintThrottled,
    DhanTokenRenewError,
)
from brutex.config.credentials_types import DhanCredentials

from tests.fakes.fake_dhan_auth_http import FakeDhanAuthHttp, FakeHttpError

_CREDS = DhanCredentials(client_id="FAKE-CID", pin="FAKE-PIN", totp_secret="FAKE-TOTP-SECRET")
_FIXED = datetime(2026, 7, 12, 6, 0, 0, tzinfo=UTC)


def _CLOCK() -> datetime:  # noqa: N802  # test helper name
    return _FIXED


def _NO_SLEEP(_s: float) -> None:  # noqa: N802  # test helper name
    return None


def _totp(_secret: str) -> str:
    return "FAKE-CODE"


class TestMintSuccess:
    def test_top_level_key(self) -> None:
        http = FakeDhanAuthHttp([{"accessToken": "FAKE-JWT"}])
        token = mint_access_token(
            _CREDS, transport=http, totp_minter=_totp, clock=_CLOCK, sleeper=_NO_SLEEP
        )
        assert token == MintedToken(token="FAKE-JWT", minted_at_utc=_CLOCK(), source="mint")
        assert http.call_count == 1
        # The mint URL carried the secrets in the query (the transport got them; scrubbing is
        # only needed on the ERROR path).
        assert "generateAccessToken" in http.requests[0].url
        assert http.requests[0].method == "POST"

    def test_nested_data_key(self) -> None:
        http = FakeDhanAuthHttp([{"data": {"access_token": "FAKE-JWT2"}}])
        token = mint_access_token(_CREDS, transport=http, totp_minter=_totp, clock=_CLOCK)
        assert token.token == "FAKE-JWT2"

    def test_plain_token_key(self) -> None:
        http = FakeDhanAuthHttp([{"token": " FAKE-JWT3 "}])
        token = mint_access_token(_CREDS, transport=http, totp_minter=_totp, clock=_CLOCK)
        assert token.token == "FAKE-JWT3"  # stripped


class TestMintTerminalZeroRetry:
    @pytest.mark.parametrize("status", [400, 401, 403])
    def test_invalid_cred_no_retry(self, status: int) -> None:
        http = FakeDhanAuthHttp([FakeHttpError(status), {"accessToken": "SHOULD-NOT-REACH"}])
        with pytest.raises(DhanAuthInvalidCredentials):
            mint_access_token(
                _CREDS, transport=http, totp_minter=_totp, clock=_CLOCK, sleeper=_NO_SLEEP
            )
        # ZERO retry: the transport was called EXACTLY once (mutant pin).
        assert http.call_count == 1

    def test_2xx_without_token_is_terminal(self) -> None:
        http = FakeDhanAuthHttp([{"nope": 1}])
        with pytest.raises(DhanAuthError):
            mint_access_token(_CREDS, transport=http, totp_minter=_totp, clock=_CLOCK)
        assert http.call_count == 1


class TestMintRetryLadder:
    def test_throttle_then_success_recomputes_totp(self) -> None:
        codes = iter(["CODE-A", "CODE-B"])
        totp_calls: list[str] = []

        def _counting_totp(_secret: str) -> str:
            code = next(codes)
            totp_calls.append(code)
            return code

        http = FakeDhanAuthHttp([FakeHttpError(429), {"accessToken": "FAKE-JWT"}])
        token = mint_access_token(
            _CREDS, transport=http, totp_minter=_counting_totp, clock=_CLOCK, sleeper=_NO_SLEEP
        )
        assert token.token == "FAKE-JWT"
        assert http.call_count == 2
        # TOTP recomputed per attempt (fresh code on the retry).
        assert totp_calls == ["CODE-A", "CODE-B"]

    def test_throttle_exhausted_raises_throttled(self) -> None:
        http = FakeDhanAuthHttp([FakeHttpError(429), FakeHttpError(429), FakeHttpError(429)])
        with pytest.raises(DhanTokenMintThrottled):
            mint_access_token(
                _CREDS, transport=http, totp_minter=_totp, clock=_CLOCK, sleeper=_NO_SLEEP
            )
        assert http.call_count == 3

    def test_transport_5xx_then_success(self) -> None:
        http = FakeDhanAuthHttp([FakeHttpError(503), {"accessToken": "FAKE-JWT"}])
        token = mint_access_token(
            _CREDS, transport=http, totp_minter=_totp, clock=_CLOCK, sleeper=_NO_SLEEP
        )
        assert token.token == "FAKE-JWT"

    def test_network_error_no_status_exhausts_transport(self) -> None:
        http = FakeDhanAuthHttp([FakeHttpError(None), FakeHttpError(None), FakeHttpError(None)])
        with pytest.raises(DhanAuthTransportError):
            mint_access_token(
                _CREDS, transport=http, totp_minter=_totp, clock=_CLOCK, sleeper=_NO_SLEEP
            )
        assert http.call_count == 3


class TestNoLeak:
    def test_error_scrubs_secrets_and_url_and_no_cause(self) -> None:
        # The fake error message embeds the mint URL (as a real HTTPError would).
        http = FakeDhanAuthHttp(
            [FakeHttpError(400, "HTTP 400 for https://auth.dhan.co/x?pin=FAKE-PIN&totp=FAKE-CODE")]
        )
        with pytest.raises(DhanAuthInvalidCredentials) as ei:
            mint_access_token(_CREDS, transport=http, totp_minter=_totp, clock=_CLOCK)
        blob = f"{ei.value!s} {ei.value!r} {ei.value.safe_message}"
        for secret in ("FAKE-PIN", "FAKE-CODE", "FAKE-CID", "FAKE-TOTP-SECRET"):
            assert secret not in blob
        # The raw transport object never rides __cause__ (from None).
        assert ei.value.__cause__ is None


class TestRenew:
    def test_success_headers_only(self) -> None:
        http = FakeDhanAuthHttp([{"accessToken": "FAKE-RENEWED"}])
        token = renew_access_token("FAKE-OLD", "FAKE-CID", transport=http, clock=_CLOCK)
        assert token == MintedToken(token="FAKE-RENEWED", minted_at_utc=_CLOCK(), source="renew")
        req = http.requests[0]
        assert req.method == "GET"
        assert req.headers["access-token"] == "FAKE-OLD"
        assert req.headers["dhanClientId"] == "FAKE-CID"
        # No secrets in the URL query on renew.
        assert "?" not in req.url

    def test_transport_failure_wrapped(self) -> None:
        http = FakeDhanAuthHttp([FakeHttpError(500, "boom for https://h/RenewToken")])
        with pytest.raises(DhanTokenRenewError) as ei:
            renew_access_token("FAKE-OLD", "FAKE-CID", transport=http, clock=_CLOCK)
        assert "FAKE-OLD" not in str(ei.value)
        assert ei.value.__cause__ is None

    def test_no_token_in_response(self) -> None:
        http = FakeDhanAuthHttp([{"nope": 1}])
        with pytest.raises(DhanTokenRenewError):
            renew_access_token("FAKE-OLD", "FAKE-CID", transport=http, clock=_CLOCK)


class TestHelpers:
    def test_http_status_bool_rejected(self) -> None:
        class _E(Exception):
            status = True

        assert auth_mod._http_status(_E()) is None

    def test_http_status_int_code(self) -> None:
        class _E(Exception):
            code = 429

        assert auth_mod._http_status(_E()) == 429

    def test_http_status_digit_string(self) -> None:
        class _E(Exception):
            status = "503"

        assert auth_mod._http_status(_E()) == 503

    def test_http_status_none(self) -> None:
        assert auth_mod._http_status(Exception("x")) is None

    def test_extract_token_none_when_absent(self) -> None:
        assert auth_mod._extract_token({"nope": 1}) is None

    def test_extract_token_ignores_non_string(self) -> None:
        assert auth_mod._extract_token({"token": 123}) is None

    def test_extract_token_data_not_mapping(self) -> None:
        assert auth_mod._extract_token({"data": "not-a-map", "token": "FAKE"}) == "FAKE"

    def test_issuance_backoff_caps(self) -> None:
        from brutex.adapters.dhan.constants import DHAN_TOKEN_ISSUANCE_RETRY_CAP_S

        assert auth_mod._issuance_backoff(0) <= DHAN_TOKEN_ISSUANCE_RETRY_CAP_S
        assert auth_mod._issuance_backoff(10) == DHAN_TOKEN_ISSUANCE_RETRY_CAP_S

    def test_now_utc_default_clock_is_tz_aware(self) -> None:
        now = auth_mod._now_utc()
        assert now.tzinfo is UTC

    def test_mint_uses_default_clock_when_unset(self) -> None:
        http = FakeDhanAuthHttp([{"accessToken": "FAKE-JWT"}])
        token = mint_access_token(_CREDS, transport=http, totp_minter=_totp)
        assert token.minted_at_utc.tzinfo is UTC


class TestParseJsonObject:
    """The extracted JSON-object guard (lifted out of the network pragma) is unit-tested."""

    def test_valid_object_returned(self) -> None:
        assert auth_mod._parse_json_object('{"accessToken": "FAKE-JWT"}') == {
            "accessToken": "FAKE-JWT"
        }

    def test_json_array_raises_dhan_auth_error(self) -> None:
        with pytest.raises(DhanAuthError):
            auth_mod._parse_json_object("[1, 2, 3]")

    def test_json_scalar_raises_dhan_auth_error(self) -> None:
        with pytest.raises(DhanAuthError):
            auth_mod._parse_json_object("42")
