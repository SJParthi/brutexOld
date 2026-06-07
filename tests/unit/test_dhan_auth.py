"""Tests for `brutex.adapters.dhan.auth` — credentials + RenewToken refresh.

100% line+branch: env-first/file-fallback resolution (env, saved file, missing
each var), RenewToken success + malformed responses, and token application.
The credentials FILE is isolated to a per-test tmp path (DEC-DHAN-CREDS-WIRING-001)
so the file-fallback is hermetic — env-only tests see an empty file by default.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest
import structlog
from brutex.adapters.dhan.auth import (
    ENV_ACCESS_TOKEN,
    ENV_CLIENT_ID,
    DhanCredentials,
    TokenInfo,
    dhan_creds_present,
    maybe_renew_token,
    renew_token,
    resolve_dhan_credentials,
    with_token,
)
from brutex.adapters.dhan.client import DhanClient
from brutex.adapters.dhan.errors import DhanAuthError
from brutex.config.credentials import (
    ENV_CREDENTIALS_FILE,
    DhanFileCredentials,
    save_dhan_credentials,
)


@pytest.fixture(autouse=True)
def creds_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the credentials file at an (initially absent) tmp path for every test.

    `resolve_dhan_credentials`/`dhan_creds_present` now fall back to the saved
    file via `credentials_path()` (which reads ``os.environ``). Isolating it here
    keeps the env-only tests hermetic (empty file) and lets file-fallback tests
    write to a known path.
    """
    target = tmp_path / "credentials.json"
    monkeypatch.setenv(ENV_CREDENTIALS_FILE, str(target))
    monkeypatch.delenv(ENV_ACCESS_TOKEN, raising=False)
    monkeypatch.delenv(ENV_CLIENT_ID, raising=False)
    return target


class _Transport:
    """Fake transport returning a canned (status, body) and recording the verb/body.

    ``RenewToken`` is a GET (DEC-DHAN-RENEWTOKEN-GET-001), so the fake records the
    ``method`` + request ``body`` the client sent; the renew tests assert it is a
    GET with an EMPTY body (no POST ``{}``).
    """

    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.body = body
        self.calls: list[dict[str, object]] = []

    def __call__(
        self,
        url: str,
        *,
        body: bytes,
        headers: Mapping[str, str],
        timeout: float,
        method: str = "POST",
    ) -> tuple[int, bytes]:
        self.calls.append({"url": url, "body": body, "method": method})
        return self.status, self.body


def _client(status: int, body: bytes) -> DhanClient:
    return DhanClient(
        access_token="old",
        client_id="cid",
        transport=_Transport(status, body),  # type: ignore[arg-type]
        base_url="https://api.dhan.co/v2/",
    )


class TestResolveCredentials:
    def test_happy_path_from_mapping(self) -> None:
        creds = resolve_dhan_credentials({ENV_ACCESS_TOKEN: " jwt ", ENV_CLIENT_ID: " 100 "})
        assert creds == DhanCredentials(access_token="jwt", client_id="100")

    def test_missing_token_raises(self) -> None:
        with pytest.raises(DhanAuthError, match=ENV_ACCESS_TOKEN):
            resolve_dhan_credentials({ENV_CLIENT_ID: "100"})

    def test_missing_client_id_raises(self) -> None:
        with pytest.raises(DhanAuthError, match=ENV_CLIENT_ID):
            resolve_dhan_credentials({ENV_ACCESS_TOKEN: "jwt"})

    def test_default_env_branch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # env=None → reads os.environ (covers the `env is None` branch).
        monkeypatch.setenv(ENV_ACCESS_TOKEN, "envjwt")
        monkeypatch.setenv(ENV_CLIENT_ID, "envcid")
        creds = resolve_dhan_credentials()
        assert creds == DhanCredentials(access_token="envjwt", client_id="envcid")

    def test_file_fallback_enables_resolution(self, creds_file: Path) -> None:
        # The CORE fix (DEC-DHAN-CREDS-WIRING-001): with NO env vars, creds saved
        # to the file (as the dashboard does) resolve successfully → live pulls work.
        save_dhan_credentials(
            DhanFileCredentials(access_token="filetok", client_id="fileclient"),
            path=creds_file,
        )
        creds = resolve_dhan_credentials({})
        assert creds == DhanCredentials(access_token="filetok", client_id="fileclient")


class TestRenewToken:
    def test_success_is_a_get_with_no_body(self) -> None:
        # RenewToken is an HTTP GET, headers-only, NO body — exactly as the
        # official DhanHQ SDK calls it (DEC-DHAN-RENEWTOKEN-GET-001). Sending it
        # as a POST {} makes real Dhan reject it with HTTP 400 DH-905.
        body = b'{"accessToken": "newjwt", "expiryTime": "2026-01-01T00:00:00.000"}'
        transport = _Transport(200, body)
        client = DhanClient(
            access_token="old",
            client_id="cid",
            transport=transport,  # type: ignore[arg-type]
            base_url="https://api.dhan.co/v2/",
        )
        info = renew_token(client)
        assert info == TokenInfo(access_token="newjwt", expiry_time="2026-01-01T00:00:00.000")
        assert transport.calls[0]["method"] == "GET"  # GET, not POST
        assert transport.calls[0]["body"] == b""  # headers-only, NO request body
        assert transport.calls[0]["url"] == "https://api.dhan.co/v2/RenewToken"

    def test_missing_fields_raises(self) -> None:
        with pytest.raises(DhanAuthError, match="missing string accessToken"):
            renew_token(_client(200, b'{"accessToken": "x"}'))

    def test_non_string_fields_raises(self) -> None:
        with pytest.raises(DhanAuthError, match="missing string accessToken"):
            renew_token(_client(200, b'{"accessToken": 1, "expiryTime": 2}'))


class TestRenewTokenPacing:
    """The defence-in-depth pace hook (DEC-DHAN-RATE-RESIDUAL-HARDEN-001).

    Every physical Dhan request from the renew path must be bucket-gated even when
    wired into a live pull later: the explicit ``pace`` hook gates the INITIAL
    request and is called BEFORE the network call (RenewToken is a GET —
    DEC-DHAN-RENEWTOKEN-GET-001); with no explicit hook, the client's own ``pace``
    field is used (no-op by default → current callers unchanged).
    """

    def test_explicit_pace_called_before_get(self) -> None:
        # An explicit pace hook gates the initial GET and fires BEFORE it.
        order: list[str] = []

        class _RecordingTransport:
            def __call__(
                self,
                url: str,
                *,
                body: bytes,
                headers: Mapping[str, str],
                timeout: float,
                method: str = "POST",
            ) -> tuple[int, bytes]:
                order.append("get")
                return 200, b'{"accessToken": "newjwt", "expiryTime": "t"}'

        client = DhanClient(
            access_token="old",
            client_id="cid",
            transport=_RecordingTransport(),  # type: ignore[arg-type]
            base_url="https://api.dhan.co/v2/",
        )
        info = renew_token(client, pace=lambda: order.append("pace"))
        assert info == TokenInfo(access_token="newjwt", expiry_time="t")
        assert order == ["pace", "get"]  # paced BEFORE the physical GET

    def test_default_falls_back_to_client_pace(self) -> None:
        # No explicit hook → the client's own pace field gates the initial POST.
        calls: list[str] = []
        body = b'{"accessToken": "newjwt", "expiryTime": "t"}'
        client = DhanClient(
            access_token="old",
            client_id="cid",
            transport=_Transport(200, body),  # type: ignore[arg-type]
            base_url="https://api.dhan.co/v2/",
            pace=lambda: calls.append("client_pace"),
        )
        info = renew_token(client)
        assert info == TokenInfo(access_token="newjwt", expiry_time="t")
        assert calls == ["client_pace"]  # client.pace fired exactly once


class TestMaybeRenewToken:
    """Best-effort proactive renew (DEC-DHAN-AUTH-FAILFAST-RENEW-001, Piece 2).

    Success returns the fresh TokenInfo + logs ``auth.renewed``; a RenewToken
    refusal (any DhanError, incl. DhanAuthError) returns ``None`` + logs ONE
    ``auth.renew_failed`` WARNING and NEVER raises.
    """

    def test_success_returns_token_and_logs_renewed(self) -> None:
        body = b'{"accessToken": "freshjwt", "expiryTime": "2026-06-08T09:00:00.000"}'
        with structlog.testing.capture_logs() as logs:
            info = maybe_renew_token(_client(200, body))
        assert info == TokenInfo(access_token="freshjwt", expiry_time="2026-06-08T09:00:00.000")
        events = [e for e in logs if e["event"] == "auth.renewed"]
        assert len(events) == 1
        assert events[0]["expiry_time"] == "2026-06-08T09:00:00.000"

    def test_dhan_error_returns_none_and_logs_once_never_raises(self) -> None:
        # Malformed RenewToken response → DhanAuthError inside renew_token. The
        # best-effort wrapper swallows it: returns None, logs ONE renew_failed.
        with structlog.testing.capture_logs() as logs:
            result = maybe_renew_token(_client(200, b'{"accessToken": "x"}'))
        assert result is None
        warned = [e for e in logs if e["event"] == "auth.renew_failed"]
        assert len(warned) == 1
        assert warned[0]["log_level"] == "warning"

    def test_auth_error_status_returns_none(self) -> None:
        # A 401 from RenewToken (an already-expired token Dhan refuses) → None.
        result = maybe_renew_token(_client(401, b"unauthorized"))
        assert result is None

    def test_forwards_pace_hook(self) -> None:
        # The pace hook is forwarded to renew_token (gates the physical POST).
        calls: list[str] = []
        body = b'{"accessToken": "freshjwt", "expiryTime": "t"}'
        info = maybe_renew_token(_client(200, body), pace=lambda: calls.append("pace"))
        assert info is not None
        assert calls == ["pace"]

    def test_custom_logger_receives_event(self) -> None:
        # An explicit *log* is honoured (covers the `log is not None` branch).
        body = b'{"accessToken": "freshjwt", "expiryTime": "t"}'
        custom = structlog.get_logger("test.custom")
        info = maybe_renew_token(_client(200, body), log=custom)
        assert info == TokenInfo(access_token="freshjwt", expiry_time="t")


class TestWithToken:
    def test_applies_new_token_keeps_client_id(self) -> None:
        creds = DhanCredentials(access_token="old", client_id="cid")
        updated = with_token(creds, TokenInfo(access_token="new", expiry_time="t"))
        assert updated == DhanCredentials(access_token="new", client_id="cid")


class TestDhanCredsPresent:
    def test_both_present_is_true(self) -> None:
        env = {ENV_ACCESS_TOKEN: "tok", ENV_CLIENT_ID: "cid"}
        assert dhan_creds_present(env) is True

    def test_access_token_missing_is_false(self) -> None:
        assert dhan_creds_present({ENV_CLIENT_ID: "cid"}) is False

    def test_client_id_missing_is_false(self) -> None:
        # token present, client id absent — exercises the second predicate.
        assert dhan_creds_present({ENV_ACCESS_TOKEN: "tok"}) is False

    def test_whitespace_only_is_false(self) -> None:
        assert dhan_creds_present({ENV_ACCESS_TOKEN: "  ", ENV_CLIENT_ID: "cid"}) is False

    def test_true_from_saved_file_without_env(self, creds_file: Path) -> None:
        # The gate flips to live when creds are saved on the dashboard (file),
        # even with NO env vars — the disconnect fix (DEC-DHAN-CREDS-WIRING-001).
        save_dhan_credentials(
            DhanFileCredentials(access_token="tok", client_id="cid"), path=creds_file
        )
        assert dhan_creds_present({}) is True

    def test_defaults_to_os_environ(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_ACCESS_TOKEN, "tok")
        monkeypatch.setenv(ENV_CLIENT_ID, "cid")
        assert dhan_creds_present() is True
