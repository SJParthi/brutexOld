"""Unit tests for ``brutex.adapters.dhan.auth``.

Pins:

* DhanToken redacts in repr() / str() / format().
* File-based loading at 0600 perms succeeds.
* File at 0640 / 0644 raises InsecureTokenFileError.
* Malformed JSON raises MalformedTokenFileError.
* Missing 'access_token' key raises MalformedTokenFileError.
* Env-var fallback when file is absent.
* Missing both raises MissingDhanTokenError.
* BRUTEX_DHAN_TOKEN_PATH overrides the default path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from brutex.adapters.dhan.auth import (
    PATH_ENV_VAR,
    TOKEN_ENV_VAR,
    DhanToken,
    InsecureTokenFileError,
    MalformedTokenFileError,
    MissingDhanTokenError,
    load_dhan_token,
)


class TestDhanTokenRedaction:
    """The token wrapper never leaks the raw value via __repr__/__str__."""

    def test_repr_redacts(self) -> None:
        token = DhanToken(_value="super_secret_xyz123")
        assert repr(token) == "DhanToken(***)"
        assert "super_secret_xyz123" not in repr(token)

    def test_str_redacts(self) -> None:
        token = DhanToken(_value="super_secret_xyz123")
        assert str(token) == "DhanToken(***)"
        assert "super_secret_xyz123" not in str(token)

    def test_fstring_redacts(self) -> None:
        token = DhanToken(_value="super_secret_xyz123")
        rendered = f"token = {token}"
        assert "super_secret_xyz123" not in rendered

    def test_reveal_returns_raw(self) -> None:
        """``reveal()`` is the only call that returns the underlying string."""
        token = DhanToken(_value="abc123")
        assert token.reveal() == "abc123"

    def test_empty_value_raises(self) -> None:
        with pytest.raises(MissingDhanTokenError):
            DhanToken(_value="")

    def test_frozen(self) -> None:
        """Cannot mutate the underlying value after construction."""
        import dataclasses

        token = DhanToken(_value="abc")
        with pytest.raises(dataclasses.FrozenInstanceError):
            token._value = "different"  # type: ignore[misc]


class TestFileBasedLoading:
    """File-based token loading + permission enforcement."""

    @pytest.fixture
    def token_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        path = tmp_path / "access_token.json"
        path.write_text(json.dumps({"access_token": "file_token_abc"}))
        path.chmod(0o600)
        monkeypatch.setenv(PATH_ENV_VAR, str(path))
        monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
        return path

    def test_loads_from_0600_file(self, token_file: Path) -> None:
        token = load_dhan_token()
        assert token.reveal() == "file_token_abc"

    def test_0644_raises_insecure(self, token_file: Path) -> None:
        token_file.chmod(0o644)
        with pytest.raises(InsecureTokenFileError):
            load_dhan_token()

    def test_0640_raises_insecure(self, token_file: Path) -> None:
        token_file.chmod(0o640)
        with pytest.raises(InsecureTokenFileError):
            load_dhan_token()

    def test_malformed_json_raises(self, token_file: Path) -> None:
        token_file.write_text("not valid json {")
        token_file.chmod(0o600)
        with pytest.raises(MalformedTokenFileError):
            load_dhan_token()

    def test_missing_access_token_key_raises(self, token_file: Path) -> None:
        token_file.write_text(json.dumps({"other_key": "x"}))
        token_file.chmod(0o600)
        with pytest.raises(MalformedTokenFileError):
            load_dhan_token()

    def test_empty_access_token_raises(self, token_file: Path) -> None:
        token_file.write_text(json.dumps({"access_token": ""}))
        token_file.chmod(0o600)
        with pytest.raises(MalformedTokenFileError):
            load_dhan_token()

    def test_non_string_access_token_raises(self, token_file: Path) -> None:
        token_file.write_text(json.dumps({"access_token": 12345}))
        token_file.chmod(0o600)
        with pytest.raises(MalformedTokenFileError):
            load_dhan_token()


class TestEnvFallback:
    """When the file is absent, the env var fills in."""

    @pytest.fixture
    def absent_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        missing = tmp_path / "missing.json"
        monkeypatch.setenv(PATH_ENV_VAR, str(missing))

    def test_env_var_used(self, absent_file: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(TOKEN_ENV_VAR, "env_token_xyz")
        token = load_dhan_token()
        assert token.reveal() == "env_token_xyz"

    def test_missing_both_raises(self, absent_file: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
        with pytest.raises(MissingDhanTokenError):
            load_dhan_token()

    def test_empty_env_var_treated_as_missing(
        self, absent_file: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(TOKEN_ENV_VAR, "")
        with pytest.raises(MissingDhanTokenError):
            load_dhan_token()


class TestPathOverride:
    """BRUTEX_DHAN_TOKEN_PATH overrides the default path."""

    def test_override_takes_precedence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        custom = tmp_path / "custom_token.json"
        custom.write_text(json.dumps({"access_token": "from_override"}))
        custom.chmod(0o600)
        monkeypatch.setenv(PATH_ENV_VAR, str(custom))
        monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
        token = load_dhan_token()
        assert token.reveal() == "from_override"
