"""Tests for :mod:`brutex.config.credentials` — secure Groww credential store.

Exhaustive line + branch coverage of the save / load / resolve / present /
clear surface (DEC-CREDENTIALS-STORE-001). Every test uses FAKE values
(``fake-api-key`` / ``JBSWY3DPEHPK3PXP``) — no real secret ever appears. The
suite asserts the security invariants directly: mode 0600 file, mode 0700
parent, atomic write (no temp left behind), insecure-perms refusal on read,
corrupt-file refusal, and that NO error message ever echoes a secret VALUE.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from brutex.config.credentials import (
    ENV_API_KEY,
    ENV_CREDENTIALS_FILE,
    ENV_TOTP_SECRET,
    CredentialsError,
    GrowwCredentials,
    clear_groww_credentials,
    credentials_path,
    credentials_present,
    load_groww_credentials,
    resolve_groww_credentials,
    save_groww_credentials,
)

# FAKE constants — never real secrets. The whole suite uses only these.
FAKE_API_KEY = "fake-api-key"
FAKE_TOTP_SECRET = "JBSWY3DPEHPK3PXP"  # well-known RFC base32 test vector, not a secret


def _mode(path: Path) -> int:
    """Return the permission bits of *path*."""
    return stat.S_IMODE(path.stat().st_mode)


# ────────────────────────────────────────────────────────────────────────
# credentials_path
# ────────────────────────────────────────────────────────────────────────


class TestCredentialsPath:
    def test_default_is_under_home(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(ENV_CREDENTIALS_FILE, raising=False)
        assert credentials_path({}) == Path.home() / ".brutex" / "credentials.json"

    def test_env_override_expands_user(self) -> None:
        path = credentials_path({ENV_CREDENTIALS_FILE: "~/custom/creds.json"})
        assert path == Path.home() / "custom" / "creds.json"

    def test_blank_override_falls_back_to_default(self) -> None:
        assert (
            credentials_path({ENV_CREDENTIALS_FILE: "   "})
            == Path.home() / ".brutex" / "credentials.json"
        )

    def test_defaults_to_os_environ(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_CREDENTIALS_FILE, "/tmp/x/creds.json")
        assert credentials_path() == Path("/tmp/x/creds.json")


# ────────────────────────────────────────────────────────────────────────
# save_groww_credentials
# ────────────────────────────────────────────────────────────────────────


class TestSave:
    def test_writes_0600_and_parent_0700_and_shape(self, tmp_path: Path) -> None:
        path = tmp_path / "nest" / "credentials.json"
        creds = GrowwCredentials(api_key=FAKE_API_KEY, totp_secret=FAKE_TOTP_SECRET)
        written = save_groww_credentials(creds, path=path)

        assert written == path
        assert _mode(path) == 0o600
        assert _mode(path.parent) == 0o700
        assert _mode(path) & 0o077 == 0

        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload == {"groww": {"api_key": FAKE_API_KEY, "totp_secret": FAKE_TOTP_SECRET}}

    def test_leaves_no_temp_file_behind(self, tmp_path: Path) -> None:
        creds = GrowwCredentials(api_key=FAKE_API_KEY, totp_secret=FAKE_TOTP_SECRET)
        save_groww_credentials(creds, path=tmp_path / "credentials.json")
        leftovers = [p.name for p in tmp_path.iterdir() if p.name != "credentials.json"]
        assert leftovers == []

    def test_blank_api_key_raises_without_value(self, tmp_path: Path) -> None:
        creds = GrowwCredentials(api_key="   ", totp_secret=FAKE_TOTP_SECRET)
        with pytest.raises(CredentialsError) as exc:
            save_groww_credentials(creds, path=tmp_path / "c.json")
        assert "api_key" in str(exc.value)
        assert FAKE_TOTP_SECRET not in str(exc.value)
        assert not (tmp_path / "c.json").exists()

    def test_blank_totp_secret_raises_without_value(self, tmp_path: Path) -> None:
        creds = GrowwCredentials(api_key=FAKE_API_KEY, totp_secret="")
        with pytest.raises(CredentialsError) as exc:
            save_groww_credentials(creds, path=tmp_path / "c.json")
        assert "totp_secret" in str(exc.value)
        assert FAKE_API_KEY not in str(exc.value)

    def test_default_path_used_when_none(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        target = tmp_path / "home" / "credentials.json"
        monkeypatch.setenv(ENV_CREDENTIALS_FILE, str(target))
        creds = GrowwCredentials(api_key=FAKE_API_KEY, totp_secret=FAKE_TOTP_SECRET)
        written = save_groww_credentials(creds)
        assert written == target
        assert target.exists()

    def test_cleanup_on_replace_failure(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Force os.replace to fail → the except branch must close + unlink the
        # temp file and re-raise, leaving NO temp file behind.
        import brutex.config.credentials as mod

        def _boom(_src: object, _dst: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(mod.os, "replace", _boom)
        creds = GrowwCredentials(api_key=FAKE_API_KEY, totp_secret=FAKE_TOTP_SECRET)
        with pytest.raises(OSError, match="disk full"):
            save_groww_credentials(creds, path=tmp_path / "credentials.json")
        leftovers = list(tmp_path.iterdir())
        assert leftovers == []


# ────────────────────────────────────────────────────────────────────────
# load_groww_credentials
# ────────────────────────────────────────────────────────────────────────


class TestLoad:
    def test_round_trip(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.json"
        original = GrowwCredentials(api_key=FAKE_API_KEY, totp_secret=FAKE_TOTP_SECRET)
        save_groww_credentials(original, path=path)
        loaded = load_groww_credentials(path=path)
        assert loaded == original

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert load_groww_credentials(path=tmp_path / "nope.json") is None

    def test_default_path_when_none(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        target = tmp_path / "credentials.json"
        monkeypatch.setenv(ENV_CREDENTIALS_FILE, str(target))
        assert load_groww_credentials() is None
        save_groww_credentials(GrowwCredentials(api_key=FAKE_API_KEY, totp_secret=FAKE_TOTP_SECRET))
        loaded = load_groww_credentials()
        assert loaded is not None
        assert loaded.api_key == FAKE_API_KEY

    def test_insecure_perms_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.json"
        save_groww_credentials(
            GrowwCredentials(api_key=FAKE_API_KEY, totp_secret=FAKE_TOTP_SECRET), path=path
        )
        path.chmod(0o644)
        with pytest.raises(CredentialsError) as exc:
            load_groww_credentials(path=path)
        assert "chmod 600" in str(exc.value)
        assert "insecure permissions" in str(exc.value)

    def test_symlink_refused(self, tmp_path: Path) -> None:
        # A symlinked credentials path is refused (O_NOFOLLOW) rather than
        # followed — closes the perms-check/read TOCTOU via a swapped symlink.
        real = tmp_path / "real.json"
        save_groww_credentials(
            GrowwCredentials(api_key=FAKE_API_KEY, totp_secret=FAKE_TOTP_SECRET), path=real
        )
        link = tmp_path / "credentials.json"
        link.symlink_to(real)
        with pytest.raises(CredentialsError) as exc:
            load_groww_credentials(path=link)
        assert "symlink" in str(exc.value)
        # the error names neither secret value
        assert FAKE_API_KEY not in str(exc.value)
        assert FAKE_TOTP_SECRET not in str(exc.value)

    def test_corrupt_json_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.json"
        path.write_text("{not valid json", encoding="utf-8")
        path.chmod(0o600)
        with pytest.raises(CredentialsError, match="corrupt or unreadable"):
            load_groww_credentials(path=path)

    def test_non_dict_top_level_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.json"
        path.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
        path.chmod(0o600)
        with pytest.raises(CredentialsError, match="invalid shape"):
            load_groww_credentials(path=path)

    def test_missing_groww_section_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.json"
        path.write_text(json.dumps({"other": {}}), encoding="utf-8")
        path.chmod(0o600)
        with pytest.raises(CredentialsError, match="missing the 'groww' section"):
            load_groww_credentials(path=path)

    def test_groww_section_not_dict_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.json"
        path.write_text(json.dumps({"groww": "scalar"}), encoding="utf-8")
        path.chmod(0o600)
        with pytest.raises(CredentialsError, match="missing the 'groww' section"):
            load_groww_credentials(path=path)

    def test_missing_api_key_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.json"
        path.write_text(json.dumps({"groww": {"totp_secret": FAKE_TOTP_SECRET}}), encoding="utf-8")
        path.chmod(0o600)
        with pytest.raises(CredentialsError) as exc:
            load_groww_credentials(path=path)
        assert "api_key" in str(exc.value)
        assert FAKE_TOTP_SECRET not in str(exc.value)

    def test_blank_api_key_value_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.json"
        path.write_text(
            json.dumps({"groww": {"api_key": "  ", "totp_secret": FAKE_TOTP_SECRET}}),
            encoding="utf-8",
        )
        path.chmod(0o600)
        with pytest.raises(CredentialsError, match="api_key"):
            load_groww_credentials(path=path)

    def test_non_string_api_key_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.json"
        path.write_text(
            json.dumps({"groww": {"api_key": 123, "totp_secret": FAKE_TOTP_SECRET}}),
            encoding="utf-8",
        )
        path.chmod(0o600)
        with pytest.raises(CredentialsError, match="api_key"):
            load_groww_credentials(path=path)

    def test_missing_totp_secret_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.json"
        path.write_text(json.dumps({"groww": {"api_key": FAKE_API_KEY}}), encoding="utf-8")
        path.chmod(0o600)
        with pytest.raises(CredentialsError) as exc:
            load_groww_credentials(path=path)
        assert "totp_secret" in str(exc.value)
        assert FAKE_API_KEY not in str(exc.value)

    def test_blank_totp_secret_value_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.json"
        path.write_text(
            json.dumps({"groww": {"api_key": FAKE_API_KEY, "totp_secret": ""}}),
            encoding="utf-8",
        )
        path.chmod(0o600)
        with pytest.raises(CredentialsError, match="totp_secret"):
            load_groww_credentials(path=path)


# ────────────────────────────────────────────────────────────────────────
# resolve_groww_credentials
# ────────────────────────────────────────────────────────────────────────


class TestResolve:
    def test_env_only(self, tmp_path: Path) -> None:
        env = {ENV_API_KEY: FAKE_API_KEY, ENV_TOTP_SECRET: FAKE_TOTP_SECRET}
        creds = resolve_groww_credentials(env, path=tmp_path / "absent.json")
        assert creds == GrowwCredentials(api_key=FAKE_API_KEY, totp_secret=FAKE_TOTP_SECRET)

    def test_file_only(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.json"
        save_groww_credentials(
            GrowwCredentials(api_key=FAKE_API_KEY, totp_secret=FAKE_TOTP_SECRET), path=path
        )
        creds = resolve_groww_credentials({}, path=path)
        assert creds.api_key == FAKE_API_KEY
        assert creds.totp_secret == FAKE_TOTP_SECRET

    def test_per_field_mix_env_key_file_secret(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.json"
        save_groww_credentials(
            GrowwCredentials(api_key="file-key", totp_secret=FAKE_TOTP_SECRET), path=path
        )
        env = {ENV_API_KEY: "env-key"}
        creds = resolve_groww_credentials(env, path=path)
        assert creds.api_key == "env-key"  # env wins for this field
        assert creds.totp_secret == FAKE_TOTP_SECRET  # file fills the other

    def test_env_overrides_file_for_both(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.json"
        save_groww_credentials(
            GrowwCredentials(api_key="file-key", totp_secret="file-secret"), path=path
        )
        env = {ENV_API_KEY: "env-key", ENV_TOTP_SECRET: "env-secret"}
        creds = resolve_groww_credentials(env, path=path)
        assert creds == GrowwCredentials(api_key="env-key", totp_secret="env-secret")

    def test_blank_env_field_falls_back_to_file(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.json"
        save_groww_credentials(
            GrowwCredentials(api_key=FAKE_API_KEY, totp_secret=FAKE_TOTP_SECRET), path=path
        )
        env = {ENV_API_KEY: "   ", ENV_TOTP_SECRET: ""}
        creds = resolve_groww_credentials(env, path=path)
        assert creds.api_key == FAKE_API_KEY
        assert creds.totp_secret == FAKE_TOTP_SECRET

    def test_neither_raises_naming_both(self, tmp_path: Path) -> None:
        with pytest.raises(CredentialsError) as exc:
            resolve_groww_credentials({}, path=tmp_path / "absent.json")
        msg = str(exc.value)
        assert ENV_API_KEY in msg
        assert ENV_TOTP_SECRET in msg
        assert "brutex auth set" in msg

    def test_only_api_key_missing(self, tmp_path: Path) -> None:
        env = {ENV_TOTP_SECRET: FAKE_TOTP_SECRET}
        with pytest.raises(CredentialsError) as exc:
            resolve_groww_credentials(env, path=tmp_path / "absent.json")
        msg = str(exc.value)
        # The "missing:" list (before the how-to text, which names both env
        # vars) names ONLY the api_key field.
        missing_part = msg.split(";", maxsplit=1)[0]
        assert ENV_API_KEY in missing_part
        assert ENV_TOTP_SECRET not in missing_part
        assert FAKE_TOTP_SECRET not in msg

    def test_only_totp_missing(self, tmp_path: Path) -> None:
        env = {ENV_API_KEY: FAKE_API_KEY}
        with pytest.raises(CredentialsError) as exc:
            resolve_groww_credentials(env, path=tmp_path / "absent.json")
        msg = str(exc.value)
        missing_part = msg.split(";", maxsplit=1)[0]
        assert ENV_TOTP_SECRET in missing_part
        assert ENV_API_KEY not in missing_part
        assert FAKE_API_KEY not in msg

    def test_defaults_to_os_environ(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv(ENV_CREDENTIALS_FILE, str(tmp_path / "absent.json"))
        monkeypatch.setenv(ENV_API_KEY, FAKE_API_KEY)
        monkeypatch.setenv(ENV_TOTP_SECRET, FAKE_TOTP_SECRET)
        creds = resolve_groww_credentials()
        assert creds.api_key == FAKE_API_KEY

    def test_insecure_file_propagates(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.json"
        save_groww_credentials(
            GrowwCredentials(api_key=FAKE_API_KEY, totp_secret=FAKE_TOTP_SECRET), path=path
        )
        path.chmod(0o644)
        with pytest.raises(CredentialsError, match="chmod 600"):
            resolve_groww_credentials({}, path=path)


# ────────────────────────────────────────────────────────────────────────
# credentials_present
# ────────────────────────────────────────────────────────────────────────


class TestPresent:
    def test_true_from_env(self, tmp_path: Path) -> None:
        env = {ENV_API_KEY: FAKE_API_KEY, ENV_TOTP_SECRET: FAKE_TOTP_SECRET}
        assert credentials_present(env, path=tmp_path / "absent.json") is True

    def test_true_from_file(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.json"
        save_groww_credentials(
            GrowwCredentials(api_key=FAKE_API_KEY, totp_secret=FAKE_TOTP_SECRET), path=path
        )
        assert credentials_present({}, path=path) is True

    def test_false_when_absent(self, tmp_path: Path) -> None:
        assert credentials_present({}, path=tmp_path / "absent.json") is False

    def test_false_on_insecure_file(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.json"
        save_groww_credentials(
            GrowwCredentials(api_key=FAKE_API_KEY, totp_secret=FAKE_TOTP_SECRET), path=path
        )
        path.chmod(0o644)
        # Insecure perms → caught → False (does NOT raise).
        assert credentials_present({}, path=path) is False

    def test_defaults_to_os_environ(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv(ENV_CREDENTIALS_FILE, str(tmp_path / "absent.json"))
        monkeypatch.delenv(ENV_API_KEY, raising=False)
        monkeypatch.delenv(ENV_TOTP_SECRET, raising=False)
        assert credentials_present() is False


# ────────────────────────────────────────────────────────────────────────
# clear_groww_credentials
# ────────────────────────────────────────────────────────────────────────


class TestClear:
    def test_removes_existing(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.json"
        save_groww_credentials(
            GrowwCredentials(api_key=FAKE_API_KEY, totp_secret=FAKE_TOTP_SECRET), path=path
        )
        assert clear_groww_credentials(path=path) is True
        assert not path.exists()

    def test_returns_false_when_absent(self, tmp_path: Path) -> None:
        assert clear_groww_credentials(path=tmp_path / "absent.json") is False

    def test_default_path_when_none(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        target = tmp_path / "credentials.json"
        monkeypatch.setenv(ENV_CREDENTIALS_FILE, str(target))
        assert clear_groww_credentials() is False
        save_groww_credentials(GrowwCredentials(api_key=FAKE_API_KEY, totp_secret=FAKE_TOTP_SECRET))
        assert clear_groww_credentials() is True
        assert not target.exists()
