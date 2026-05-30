"""Tests for `brutex.adapters.groww.auth` — TOTP token lifecycle (Surface A).

Fakes for the SDK-facing callables (mint / build / totp / auth-error) prove —
with no growwapi/pyotp installed — the full lifecycle: mint-on-no-cache,
reuse the on-disk token, REFUSE a world-readable token file, re-mint on a
corrupt cache, force-refresh, and the single clock-skew retry. The token file
is always written mode 0600. Kept orchestrator-free → standalone-gateable.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from brutex.adapters.groww.auth import GrowwAuth, GrowwAuthError


class FakeAuthError(Exception):
    """Stand-in for growwapi's GrowwAPIAuthenticationException."""


def _auth(
    tmp_path: Path,
    *,
    mint: Any,
    build: Any = None,
    totp: Any = None,
    sleep: Any = None,
) -> GrowwAuth:
    return GrowwAuth(
        api_key="KEY",
        totp_secret="SECRET",
        token_path=tmp_path / "groww" / "access_token.json",
        mint_token=mint,
        build_session=build or (lambda tok: f"SESSION::{tok}"),
        totp_now=totp or (lambda _secret: "123456"),
        auth_error=FakeAuthError,
        sleep=sleep or mock.Mock(),
        skew_retry_seconds=0.0,
    )


def _seed_token_file(tmp_path: Path, body: str, mode: int) -> Path:
    path = tmp_path / "groww" / "access_token.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    os.chmod(path, mode)
    return path


class TestMintAndCache:
    def test_mints_when_no_cache_and_caches_in_memory(self, tmp_path: Path) -> None:
        mint = mock.Mock(return_value="tok-1")
        auth = _auth(tmp_path, mint=mint)
        session = auth.session()
        assert session == "SESSION::tok-1"
        mint.assert_called_once_with("KEY", "123456")
        # written to disk at mode 0600
        path = tmp_path / "groww" / "access_token.json"
        assert json.loads(path.read_text())["access_token"] == "tok-1"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        # second call reuses the in-memory session — no re-mint
        assert auth.session() is session
        mint.assert_called_once()

    def test_uses_secure_on_disk_cache(self, tmp_path: Path) -> None:
        _seed_token_file(tmp_path, json.dumps({"access_token": "cached"}), 0o600)
        mint = mock.Mock()
        auth = _auth(tmp_path, mint=mint)
        assert auth.session() == "SESSION::cached"
        mint.assert_not_called()


class TestCacheGuards:
    def test_refuses_world_readable_token(self, tmp_path: Path) -> None:
        _seed_token_file(tmp_path, json.dumps({"access_token": "leaked"}), 0o644)
        auth = _auth(tmp_path, mint=mock.Mock())
        with pytest.raises(GrowwAuthError) as ei:
            auth.session()
        assert "leaked" not in str(ei.value)  # redacted via safe_message
        assert "insecure" in str(ei.value).lower()

    def test_corrupt_json_cache_remints(self, tmp_path: Path) -> None:
        _seed_token_file(tmp_path, "{not valid json", 0o600)
        mint = mock.Mock(return_value="fresh")
        auth = _auth(tmp_path, mint=mint)
        assert auth.session() == "SESSION::fresh"
        mint.assert_called_once()

    def test_missing_access_token_key_remints(self, tmp_path: Path) -> None:
        _seed_token_file(tmp_path, json.dumps({"wrong": "x"}), 0o600)
        mint = mock.Mock(return_value="fresh2")
        auth = _auth(tmp_path, mint=mint)
        assert auth.session() == "SESSION::fresh2"


class TestRefresh:
    def test_refresh_forces_remint_and_rewrites_file(self, tmp_path: Path) -> None:
        mint = mock.Mock(side_effect=["tok-a", "tok-b"])
        auth = _auth(tmp_path, mint=mint)
        first = auth.session()
        second = auth.refresh()
        assert (first, second) == ("SESSION::tok-a", "SESSION::tok-b")
        assert mint.call_count == 2
        path = tmp_path / "groww" / "access_token.json"
        assert json.loads(path.read_text())["access_token"] == "tok-b"

    def test_write_enforces_0600_over_preexisting_insecure_file(self, tmp_path: Path) -> None:
        # refresh() does NOT read the cache, so a pre-existing 0644 file is overwritten.
        _seed_token_file(tmp_path, "stale", 0o644)
        auth = _auth(tmp_path, mint=mock.Mock(return_value="new"))
        auth.refresh()
        path = tmp_path / "groww" / "access_token.json"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert json.loads(path.read_text())["access_token"] == "new"


class TestClockSkewRetry:
    def test_auth_error_then_success_retries_once(self, tmp_path: Path) -> None:
        mint = mock.Mock(side_effect=[FakeAuthError("edge of window"), "tok-ok"])
        sleep = mock.Mock()
        auth = _auth(tmp_path, mint=mint, sleep=sleep)
        assert auth.session() == "SESSION::tok-ok"
        sleep.assert_called_once_with(0.0)
        assert mint.call_count == 2

    def test_persistent_auth_failure_raises_redacted(self, tmp_path: Path) -> None:
        mint = mock.Mock(side_effect=[FakeAuthError("a"), FakeAuthError("b")])
        sleep = mock.Mock()
        auth = _auth(tmp_path, mint=mint, sleep=sleep)
        with pytest.raises(GrowwAuthError) as ei:
            auth.session()
        assert "check API key" in str(ei.value)
        sleep.assert_called_once()
        assert mint.call_count == 2


class TestClearCachedToken:
    """clear_cached_token() + default_token_path() — DEC-CREDS-NORMALISE-001."""

    def test_default_token_path_uses_env_override(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from brutex.adapters.groww.auth import default_token_path

        target = tmp_path / "tok.json"
        monkeypatch.setenv("BRUTEX_GROWW_TOKEN_PATH", str(target))
        assert default_token_path() == target

    def test_default_token_path_falls_back_to_home(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from brutex.adapters.groww.auth import default_token_path

        monkeypatch.delenv("BRUTEX_GROWW_TOKEN_PATH", raising=False)
        assert default_token_path() == Path.home() / ".brutex" / "groww" / "access_token.json"

    def test_clear_removes_existing_token(self, tmp_path: Path) -> None:
        from brutex.adapters.groww.auth import clear_cached_token

        path = tmp_path / "groww" / "access_token.json"
        path.parent.mkdir(parents=True)
        path.write_text("{}")
        assert clear_cached_token(path=path) is True
        assert not path.exists()

    def test_clear_returns_false_when_absent(self, tmp_path: Path) -> None:
        from brutex.adapters.groww.auth import clear_cached_token

        assert clear_cached_token(path=tmp_path / "absent.json") is False

    def test_clear_uses_default_path_when_none(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from brutex.adapters.groww.auth import clear_cached_token

        target = tmp_path / "tok.json"
        monkeypatch.setenv("BRUTEX_GROWW_TOKEN_PATH", str(target))
        assert clear_cached_token() is False
        target.write_text("{}")
        assert clear_cached_token() is True
        assert not target.exists()
