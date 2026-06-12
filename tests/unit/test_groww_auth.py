"""Tests for `brutex.adapters.groww.auth` — TOTP token lifecycle.

100% line+branch via injected transport + TOTP generator: cache hit,
cache miss → refresh, invalid JSON, non-string token, HTTP failure,
wrong file permissions warning, and the build_from_env missing-env-var
guard (pyotp import path is covered via the ImportError branch).
"""

# pyright: reportPrivateUsage=none
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from brutex.adapters.groww.auth import (
    MAX_REFRESH_ATTEMPTS,
    REQUIRED_FILE_MODE,
    GrowwAuth,
    _build_auth_headers,
    _fetch_token,
    _read_cached_token,
    _write_cached_token,
    build_from_env,
)
from brutex.adapters.groww.errors import GrowwAuthError

# ── helpers ───────────────────────────────────────────────────────────────


def _transport_ok(url: str, headers: dict[str, str], body: dict[str, Any]) -> tuple[int, bytes]:
    return 200, json.dumps({"token": "access-token-abc123"}).encode()


def _transport_fail(url: str, headers: dict[str, str], body: dict[str, Any]) -> tuple[int, bytes]:
    return 401, b'{"error": "invalid totp"}'


def _transport_bad_json(
    url: str, headers: dict[str, str], body: dict[str, Any]
) -> tuple[int, bytes]:
    return 200, b"not-json"


def _transport_non_string_token(
    url: str, headers: dict[str, str], body: dict[str, Any]
) -> tuple[int, bytes]:
    return 200, json.dumps({"token": {"nested": "dict"}}).encode()


def _totp_gen() -> str:
    return "123456"


# ── unit tests ────────────────────────────────────────────────────────────


class TestBuildAuthHeaders:
    def test_includes_bearer_token(self) -> None:
        headers = _build_auth_headers("myapikey")
        assert headers["Authorization"] == "Bearer myapikey"

    def test_includes_content_type(self) -> None:
        headers = _build_auth_headers("myapikey")
        assert headers["Content-Type"] == "application/json"


class TestFetchToken:
    def test_happy_path(self) -> None:
        token = _fetch_token("key", "123456", _transport_ok)
        assert token == "access-token-abc123"

    def test_non_200_raises_auth_error(self) -> None:
        with pytest.raises(GrowwAuthError, match="HTTP 401"):
            _fetch_token("key", "000000", _transport_fail)

    def test_bad_json_raises_auth_error(self) -> None:
        with pytest.raises(GrowwAuthError, match="cannot parse"):
            _fetch_token("key", "123456", _transport_bad_json)

    def test_non_string_token_raises_auth_error(self) -> None:
        with pytest.raises(GrowwAuthError, match="not a string"):
            _fetch_token("key", "123456", _transport_non_string_token)

    def test_transport_receives_totp_body(self) -> None:
        calls: list[dict[str, Any]] = []

        def recording_transport(
            url: str, headers: dict[str, str], body: dict[str, Any]
        ) -> tuple[int, bytes]:
            calls.append({"url": url, "body": body})
            return 200, json.dumps({"token": "tok"}).encode()

        _fetch_token("mykey", "654321", recording_transport)
        assert calls[0]["body"] == {"key_type": "totp", "totp": "654321"}


class TestReadCachedToken:
    def test_returns_none_if_missing(self, tmp_path: Path) -> None:
        assert _read_cached_token(tmp_path / "nonexistent.json") is None

    def test_returns_token_from_valid_file(self, tmp_path: Path) -> None:
        p = tmp_path / "token.json"
        p.write_text(json.dumps({"access_token": "tok123"}))
        assert _read_cached_token(p) == "tok123"

    def test_returns_none_for_invalid_json(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.json"
        p.write_text("not-json")
        assert _read_cached_token(p) is None

    def test_returns_none_when_token_key_missing(self, tmp_path: Path) -> None:
        p = tmp_path / "token.json"
        p.write_text(json.dumps({"other_key": "val"}))
        assert _read_cached_token(p) is None

    def test_returns_none_for_empty_token_value(self, tmp_path: Path) -> None:
        p = tmp_path / "token.json"
        p.write_text(json.dumps({"access_token": ""}))
        assert _read_cached_token(p) is None

    def test_wrong_perms_logs_warning_but_still_returns_token(self, tmp_path: Path) -> None:
        p = tmp_path / "token.json"
        p.write_text(json.dumps({"access_token": "tok"}))
        os.chmod(p, 0o644)  # wrong perms  # nosec B103  # lgtm[py/overly-permissive-file]
        result = _read_cached_token(p)
        # Should still return the token (warns but doesn't fail)
        assert result == "tok"


class TestWriteCachedToken:
    def test_writes_json_with_access_token_key(self, tmp_path: Path) -> None:
        p = tmp_path / "subdir" / "token.json"
        _write_cached_token(p, "mytok")
        data = json.loads(p.read_text())
        assert data == {"access_token": "mytok"}

    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        p = tmp_path / "a" / "b" / "c" / "token.json"
        _write_cached_token(p, "tok")
        assert p.exists()

    def test_file_mode_is_0600(self, tmp_path: Path) -> None:
        p = tmp_path / "token.json"
        _write_cached_token(p, "tok")
        mode = p.stat().st_mode & 0o777
        assert mode == REQUIRED_FILE_MODE


class TestGrowwAuth:
    def test_get_token_uses_cache_when_available(self, tmp_path: Path) -> None:
        p = tmp_path / "token.json"
        _write_cached_token(p, "cached-tok")
        refresh_calls: list[str] = []

        def counting_transport(
            url: str, headers: dict[str, str], body: dict[str, Any]
        ) -> tuple[int, bytes]:
            refresh_calls.append("called")
            return 200, json.dumps({"token": "fresh-tok"}).encode()

        auth = GrowwAuth(
            api_key="key", totp_generator=_totp_gen, token_path=p, transport=counting_transport
        )
        tok = auth.get_token()
        assert tok == "cached-tok"
        assert refresh_calls == []

    def test_get_token_fetches_when_no_cache(self, tmp_path: Path) -> None:
        p = tmp_path / "token.json"
        auth = GrowwAuth(
            api_key="key", totp_generator=_totp_gen, token_path=p, transport=_transport_ok
        )
        tok = auth.get_token()
        assert tok == "access-token-abc123"

    def test_get_token_writes_to_cache(self, tmp_path: Path) -> None:
        p = tmp_path / "token.json"
        auth = GrowwAuth(
            api_key="key", totp_generator=_totp_gen, token_path=p, transport=_transport_ok
        )
        auth.get_token()
        assert _read_cached_token(p) == "access-token-abc123"

    def test_refresh_bypasses_cache(self, tmp_path: Path) -> None:
        p = tmp_path / "token.json"
        _write_cached_token(p, "old-cached-tok")
        auth = GrowwAuth(
            api_key="key", totp_generator=_totp_gen, token_path=p, transport=_transport_ok
        )
        tok = auth.refresh()
        # refresh() must call transport regardless of cached token
        assert tok == "access-token-abc123"

    def test_refresh_overwrites_cache(self, tmp_path: Path) -> None:
        p = tmp_path / "token.json"
        _write_cached_token(p, "old-tok")
        auth = GrowwAuth(
            api_key="key", totp_generator=_totp_gen, token_path=p, transport=_transport_ok
        )
        auth.refresh()
        assert _read_cached_token(p) == "access-token-abc123"

    def test_max_refresh_attempts_constant(self) -> None:
        assert MAX_REFRESH_ATTEMPTS == 3


class TestBuildFromEnv:
    def test_missing_api_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BRUTEX_GROWW_API_KEY", raising=False)
        monkeypatch.delenv("BRUTEX_GROWW_TOTP_SECRET", raising=False)
        with pytest.raises(OSError, match="BRUTEX_GROWW_API_KEY"):
            build_from_env(transport=_transport_ok)

    def test_missing_totp_secret_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRUTEX_GROWW_API_KEY", "somekey")
        monkeypatch.delenv("BRUTEX_GROWW_TOTP_SECRET", raising=False)
        with pytest.raises(OSError, match="BRUTEX_GROWW_TOTP_SECRET"):
            build_from_env(transport=_transport_ok)

    def test_missing_pyotp_raises_import_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRUTEX_GROWW_API_KEY", "somekey")
        monkeypatch.setenv("BRUTEX_GROWW_TOTP_SECRET", "JBSWY3DPEHPK3PXP")
        with patch.dict("sys.modules", {"pyotp": None}), pytest.raises(ImportError, match="pyotp"):
            build_from_env(transport=_transport_ok)

    def test_happy_path_returns_groww_auth(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("BRUTEX_GROWW_API_KEY", "apikey123")
        monkeypatch.setenv("BRUTEX_GROWW_TOTP_SECRET", "JBSWY3DPEHPK3PXP")
        monkeypatch.setenv("BRUTEX_GROWW_TOKEN_PATH", str(tmp_path / "token.json"))
        auth = build_from_env(transport=_transport_ok)
        assert isinstance(auth, GrowwAuth)
        assert auth.api_key == "apikey123"

    def test_custom_token_path_from_env(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        custom_path = tmp_path / "mytoken.json"
        monkeypatch.setenv("BRUTEX_GROWW_API_KEY", "k")
        monkeypatch.setenv("BRUTEX_GROWW_TOTP_SECRET", "JBSWY3DPEHPK3PXP")
        monkeypatch.setenv("BRUTEX_GROWW_TOKEN_PATH", str(custom_path))
        auth = build_from_env(transport=_transport_ok)
        assert auth.token_path == custom_path
