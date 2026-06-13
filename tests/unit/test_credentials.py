"""Tests for :mod:`brutex.config.credentials` — secure token credential store.

Exhaustive line + branch coverage of the save / load / resolve / present /
clear surface (DEC-CREDENTIALS-STORE-001, DEC-DHAN-CREDS-WIRING-001). Every
test uses FAKE values — no real secret ever appears. The suite asserts the
security invariants directly: mode 0600 file, mode 0700 parent, atomic write
(no temp left behind), insecure-perms refusal on read, symlink refusal,
corrupt-file refusal, and that NO error message ever echoes a secret VALUE.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from brutex.config.credentials import (
    ENV_ACCESS_TOKEN,
    ENV_CLIENT_ID,
    ENV_CREDENTIALS_FILE,
    CredentialsError,
    TokenFileCredentials,
    clear_token_credentials,
    credentials_path,
    credentials_present,
    credentials_preview,
    load_token_credentials,
    normalise_token,
    resolve_token_file_credentials,
    save_token_credentials,
    token_shape_problem,
)

# FAKE constants — never real secrets. The whole suite uses only these.
FAKE_TOKEN = "eyJraWQ.fake-access-token.signature"
FAKE_CLIENT_ID = "1100123456"


def _mode(path: Path) -> int:
    """Return the permission bits of *path*."""
    return stat.S_IMODE(path.stat().st_mode)


def _creds(token: str = FAKE_TOKEN, client_id: str = FAKE_CLIENT_ID) -> TokenFileCredentials:
    return TokenFileCredentials(access_token=token, client_id=client_id)


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
# save_token_credentials
# ────────────────────────────────────────────────────────────────────────


class TestSave:
    def test_writes_0600_and_parent_0700_and_shape(self, tmp_path: Path) -> None:
        path = tmp_path / "nest" / "credentials.json"
        written = save_token_credentials(_creds(), path=path)

        assert written == path
        assert _mode(path) == 0o600
        assert _mode(path.parent) == 0o700
        assert _mode(path) & 0o077 == 0

        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload == {"token": {"access_token": FAKE_TOKEN, "client_id": FAKE_CLIENT_ID}}

    def test_leaves_no_temp_file_behind(self, tmp_path: Path) -> None:
        save_token_credentials(_creds(), path=tmp_path / "credentials.json")
        leftovers = [p.name for p in tmp_path.iterdir() if p.name != "credentials.json"]
        assert leftovers == []

    def test_blank_access_token_raises_without_value(self, tmp_path: Path) -> None:
        with pytest.raises(CredentialsError) as exc:
            save_token_credentials(_creds(token="   "), path=tmp_path / "c.json")
        assert "access_token" in str(exc.value)
        assert FAKE_CLIENT_ID not in str(exc.value)
        assert not (tmp_path / "c.json").exists()

    def test_blank_client_id_raises_without_value(self, tmp_path: Path) -> None:
        with pytest.raises(CredentialsError) as exc:
            save_token_credentials(_creds(client_id=""), path=tmp_path / "c.json")
        assert "client_id" in str(exc.value)
        assert FAKE_TOKEN not in str(exc.value)

    def test_default_path_used_when_none(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        target = tmp_path / "home" / "credentials.json"
        monkeypatch.setenv(ENV_CREDENTIALS_FILE, str(target))
        written = save_token_credentials(_creds())
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
        with pytest.raises(OSError, match="disk full"):
            save_token_credentials(_creds(), path=tmp_path / "credentials.json")
        assert list(tmp_path.iterdir()) == []


# ────────────────────────────────────────────────────────────────────────
# load_token_credentials
# ────────────────────────────────────────────────────────────────────────


class TestLoad:
    def test_round_trip(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.json"
        save_token_credentials(_creds(), path=path)
        assert load_token_credentials(path=path) == _creds()

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert load_token_credentials(path=tmp_path / "nope.json") is None

    def test_default_path_when_none(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        target = tmp_path / "credentials.json"
        monkeypatch.setenv(ENV_CREDENTIALS_FILE, str(target))
        assert load_token_credentials() is None
        save_token_credentials(_creds())
        loaded = load_token_credentials()
        assert loaded is not None
        assert loaded.access_token == FAKE_TOKEN

    def test_insecure_perms_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.json"
        save_token_credentials(_creds(), path=path)
        path.chmod(0o644)
        with pytest.raises(CredentialsError) as exc:
            load_token_credentials(path=path)
        assert "chmod 600" in str(exc.value)
        assert "insecure permissions" in str(exc.value)

    def test_symlink_refused(self, tmp_path: Path) -> None:
        # A symlinked credentials path is refused (O_NOFOLLOW) rather than
        # followed — closes the perms-check/read TOCTOU via a swapped symlink.
        real = tmp_path / "real.json"
        save_token_credentials(_creds(), path=real)
        link = tmp_path / "credentials.json"
        link.symlink_to(real)
        with pytest.raises(CredentialsError) as exc:
            load_token_credentials(path=link)
        assert "symlink" in str(exc.value)
        assert FAKE_TOKEN not in str(exc.value)
        assert FAKE_CLIENT_ID not in str(exc.value)

    def test_corrupt_json_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.json"
        path.write_text("{not valid json", encoding="utf-8")
        path.chmod(0o600)
        with pytest.raises(CredentialsError, match="corrupt or unreadable"):
            load_token_credentials(path=path)

    def test_non_dict_top_level_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.json"
        path.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
        path.chmod(0o600)
        with pytest.raises(CredentialsError, match="invalid shape"):
            load_token_credentials(path=path)

    def test_missing_token_section_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.json"
        path.write_text(json.dumps({"other": {}}), encoding="utf-8")
        path.chmod(0o600)
        with pytest.raises(CredentialsError, match="missing the 'token' section"):
            load_token_credentials(path=path)

    def test_token_section_not_dict_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.json"
        path.write_text(json.dumps({"token": "scalar"}), encoding="utf-8")
        path.chmod(0o600)
        with pytest.raises(CredentialsError, match="missing the 'token' section"):
            load_token_credentials(path=path)

    def test_missing_access_token_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.json"
        path.write_text(json.dumps({"token": {"client_id": FAKE_CLIENT_ID}}), encoding="utf-8")
        path.chmod(0o600)
        with pytest.raises(CredentialsError) as exc:
            load_token_credentials(path=path)
        assert "access_token" in str(exc.value)
        assert FAKE_CLIENT_ID not in str(exc.value)

    def test_blank_access_token_value_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.json"
        path.write_text(
            json.dumps({"token": {"access_token": "  ", "client_id": FAKE_CLIENT_ID}}),
            encoding="utf-8",
        )
        path.chmod(0o600)
        with pytest.raises(CredentialsError, match="access_token"):
            load_token_credentials(path=path)

    def test_non_string_access_token_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.json"
        path.write_text(
            json.dumps({"token": {"access_token": 123, "client_id": FAKE_CLIENT_ID}}),
            encoding="utf-8",
        )
        path.chmod(0o600)
        with pytest.raises(CredentialsError, match="access_token"):
            load_token_credentials(path=path)

    def test_missing_client_id_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.json"
        path.write_text(json.dumps({"token": {"access_token": FAKE_TOKEN}}), encoding="utf-8")
        path.chmod(0o600)
        with pytest.raises(CredentialsError) as exc:
            load_token_credentials(path=path)
        assert "client_id" in str(exc.value)
        assert FAKE_TOKEN not in str(exc.value)

    def test_blank_client_id_value_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.json"
        path.write_text(
            json.dumps({"token": {"access_token": FAKE_TOKEN, "client_id": ""}}),
            encoding="utf-8",
        )
        path.chmod(0o600)
        with pytest.raises(CredentialsError, match="client_id"):
            load_token_credentials(path=path)


# ────────────────────────────────────────────────────────────────────────
# resolve_token_file_credentials
# ────────────────────────────────────────────────────────────────────────


class TestResolve:
    def test_env_only(self, tmp_path: Path) -> None:
        env = {ENV_ACCESS_TOKEN: FAKE_TOKEN, ENV_CLIENT_ID: FAKE_CLIENT_ID}
        assert resolve_token_file_credentials(env, path=tmp_path / "absent.json") == _creds()

    def test_file_only(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.json"
        save_token_credentials(_creds(), path=path)
        creds = resolve_token_file_credentials({}, path=path)
        assert creds.access_token == FAKE_TOKEN
        assert creds.client_id == FAKE_CLIENT_ID

    def test_per_field_mix_env_token_file_client(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.json"
        save_token_credentials(_creds(token="file-token"), path=path)
        creds = resolve_token_file_credentials({ENV_ACCESS_TOKEN: "env-token"}, path=path)
        assert creds.access_token == "env-token"  # env wins for this field
        assert creds.client_id == FAKE_CLIENT_ID  # file fills the other

    def test_env_overrides_file_for_both(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.json"
        save_token_credentials(_creds(token="file-token", client_id="file-client"), path=path)
        env = {ENV_ACCESS_TOKEN: "env-token", ENV_CLIENT_ID: "env-client"}
        creds = resolve_token_file_credentials(env, path=path)
        assert creds == TokenFileCredentials(access_token="env-token", client_id="env-client")

    def test_blank_env_field_falls_back_to_file(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.json"
        save_token_credentials(_creds(), path=path)
        env = {ENV_ACCESS_TOKEN: "   ", ENV_CLIENT_ID: ""}
        creds = resolve_token_file_credentials(env, path=path)
        assert creds.access_token == FAKE_TOKEN
        assert creds.client_id == FAKE_CLIENT_ID

    def test_neither_raises_naming_both(self, tmp_path: Path) -> None:
        with pytest.raises(CredentialsError) as exc:
            resolve_token_file_credentials({}, path=tmp_path / "absent.json")
        msg = str(exc.value)
        assert ENV_ACCESS_TOKEN in msg
        assert ENV_CLIENT_ID in msg
        assert "brutex auth set" in msg

    def test_only_access_token_missing(self, tmp_path: Path) -> None:
        env = {ENV_CLIENT_ID: FAKE_CLIENT_ID}
        with pytest.raises(CredentialsError) as exc:
            resolve_token_file_credentials(env, path=tmp_path / "absent.json")
        missing_part = str(exc.value).split(";", maxsplit=1)[0]
        assert ENV_ACCESS_TOKEN in missing_part
        assert ENV_CLIENT_ID not in missing_part

    def test_only_client_id_missing(self, tmp_path: Path) -> None:
        env = {ENV_ACCESS_TOKEN: FAKE_TOKEN}
        with pytest.raises(CredentialsError) as exc:
            resolve_token_file_credentials(env, path=tmp_path / "absent.json")
        missing_part = str(exc.value).split(";", maxsplit=1)[0]
        assert ENV_CLIENT_ID in missing_part
        assert ENV_ACCESS_TOKEN not in missing_part
        assert FAKE_TOKEN not in str(exc.value)

    def test_defaults_to_os_environ(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv(ENV_CREDENTIALS_FILE, str(tmp_path / "absent.json"))
        monkeypatch.setenv(ENV_ACCESS_TOKEN, FAKE_TOKEN)
        monkeypatch.setenv(ENV_CLIENT_ID, FAKE_CLIENT_ID)
        assert resolve_token_file_credentials().access_token == FAKE_TOKEN

    def test_insecure_file_propagates(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.json"
        save_token_credentials(_creds(), path=path)
        path.chmod(0o644)
        with pytest.raises(CredentialsError, match="chmod 600"):
            resolve_token_file_credentials({}, path=path)


# ────────────────────────────────────────────────────────────────────────
# credentials_present
# ────────────────────────────────────────────────────────────────────────


class TestPresent:
    def test_true_from_env(self, tmp_path: Path) -> None:
        env = {ENV_ACCESS_TOKEN: FAKE_TOKEN, ENV_CLIENT_ID: FAKE_CLIENT_ID}
        assert credentials_present(env, path=tmp_path / "absent.json") is True

    def test_true_from_file(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.json"
        save_token_credentials(_creds(), path=path)
        assert credentials_present({}, path=path) is True

    def test_false_when_absent(self, tmp_path: Path) -> None:
        assert credentials_present({}, path=tmp_path / "absent.json") is False

    def test_false_on_insecure_file(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.json"
        save_token_credentials(_creds(), path=path)
        path.chmod(0o644)
        assert credentials_present({}, path=path) is False

    def test_defaults_to_os_environ(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv(ENV_CREDENTIALS_FILE, str(tmp_path / "absent.json"))
        monkeypatch.delenv(ENV_ACCESS_TOKEN, raising=False)
        monkeypatch.delenv(ENV_CLIENT_ID, raising=False)
        assert credentials_present() is False


# ────────────────────────────────────────────────────────────────────────
# clear_token_credentials
# ────────────────────────────────────────────────────────────────────────


class TestClear:
    def test_removes_existing(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.json"
        save_token_credentials(_creds(), path=path)
        assert clear_token_credentials(path=path) is True
        assert not path.exists()

    def test_returns_false_when_absent(self, tmp_path: Path) -> None:
        assert clear_token_credentials(path=tmp_path / "absent.json") is False

    def test_default_path_when_none(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        target = tmp_path / "credentials.json"
        monkeypatch.setenv(ENV_CREDENTIALS_FILE, str(target))
        assert clear_token_credentials() is False
        save_token_credentials(_creds())
        assert clear_token_credentials() is True
        assert not target.exists()


# ────────────────────────────────────────────────────────────────────────
# normalise_token — DEC-CREDS-NORMALISE-001
# ────────────────────────────────────────────────────────────────────────


class TestNormalise:
    def test_strips_surrounding_whitespace(self) -> None:
        # A paste with a trailing newline → "access-token: <tok>\n" → a vendor 401.
        assert normalise_token("  eyJraWQ.payload.sig \n") == "eyJraWQ.payload.sig"

    def test_keeps_internal_chars(self) -> None:
        # JWTs contain dots/dashes/underscores — only the ENDS are trimmed.
        assert normalise_token("a.b-c_d") == "a.b-c_d"

    def test_save_persists_normalised_values(self, tmp_path: Path) -> None:
        """A whitespace-laden paste is stored CANONICAL (the 401 root-cause fix)."""
        path = tmp_path / "credentials.json"
        save_token_credentials(_creds(token="  tok.en \n", client_id=" 110099 "), path=path)
        stored = json.loads(path.read_text())["token"]
        assert stored["access_token"] == "tok.en"
        assert stored["client_id"] == "110099"

    def test_resolve_normalises_env_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Even env-sourced creds (an exported paste) are canonicalised."""
        monkeypatch.setenv(ENV_ACCESS_TOKEN, "  envtok \n")
        monkeypatch.setenv(ENV_CLIENT_ID, " 110099 ")
        creds = resolve_token_file_credentials(path=Path("/nonexistent/creds.json"))
        assert creds.access_token == "envtok"
        assert creds.client_id == "110099"


# ────────────────────────────────────────────────────────────────────────
# credentials_preview — DEC-CREDS-PREVIEW-001 (masked, never a usable secret)
# ────────────────────────────────────────────────────────────────────────


class TestTokenShape:
    """token_shape_problem — JWT-shape guard (DEC-CREDS-JWT-SHAPE-001).

    A truncated paste (the DH-906 root cause) almost always loses or empties
    the trailing signature segment; this structural check catches that offline,
    before a doomed token is saved. The reason is always SECRET-FREE.
    """

    def test_valid_three_segment_jwt_passes(self) -> None:
        # Three non-empty base64url segments → well-formed → no problem.
        assert token_shape_problem("eyJhbGc.eyJzdWI.sIg-na_ture") is None

    def test_surrounding_whitespace_is_stripped_before_check(self) -> None:
        # A trailing newline must NOT be mistaken for malformation.
        assert token_shape_problem("  eyJhbGc.eyJzdWI.sIgnature \n") is None

    def test_two_segments_flagged_as_truncated(self) -> None:
        # Lost the whole signature → only 2 segments → flagged.
        reason = token_shape_problem("eyJhbGc.eyJzdWI")
        assert reason is not None
        assert "truncated" in reason
        assert "found 2" in reason

    def test_single_segment_no_dots_flagged(self) -> None:
        reason = token_shape_problem("notajwtatall")
        assert reason is not None
        assert "found 1" in reason

    def test_empty_trailing_segment_flagged(self) -> None:
        # A trailing dot leaves an empty 3rd segment → base64url check fails.
        reason = token_shape_problem("eyJhbGc.eyJzdWI.")
        assert reason is not None
        assert "base64url" in reason

    def test_empty_leading_segment_flagged(self) -> None:
        reason = token_shape_problem(".eyJzdWI.sIgnature")
        assert reason is not None
        assert "base64url" in reason

    def test_non_base64url_character_flagged(self) -> None:
        # '+' / '/' are standard base64 but NOT base64url → a JWT never has them.
        reason = token_shape_problem("eyJhbGc.eyJ+dWI.sIgnature")
        assert reason is not None
        assert "base64url" in reason

    def test_reason_never_contains_the_token(self) -> None:
        secret = "supersecretpastethatgottruncated"
        reason = token_shape_problem(secret)
        assert reason is not None
        assert secret not in reason


class TestPreview:
    def test_absent_returns_present_false_only(self, tmp_path: Path) -> None:
        assert credentials_preview(path=tmp_path / "nope.json") == {"present": False}

    def test_present_returns_masked_previews_and_lengths(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.json"
        token = "eyJraWQabcdefghij1234WXYZ"  # 25 chars
        save_token_credentials(_creds(token=token, client_id="1100123456"), path=path)
        preview = credentials_preview(env={}, path=path)
        assert preview["present"] is True
        # Masked head…tail — never the full secret.
        assert preview["access_token_preview"] == "eyJraW…WXYZ"
        assert preview["access_token_len"] == len(token)
        assert preview["client_id_preview"] == "11…56"
        assert preview["client_id_len"] == 10
        assert token not in str(preview)

    def test_short_secret_is_fully_bulleted(self, tmp_path: Path) -> None:
        """A value too short to split head…tail is bulleted, never revealed."""
        path = tmp_path / "credentials.json"
        # client_id 'ABCD' (4 chars) ≤ head(2)+tail(2) → fully masked.
        save_token_credentials(_creds(token="tok12345", client_id="ABCD"), path=path)
        preview = credentials_preview(env={}, path=path)
        assert preview["client_id_preview"] == "••••"
        assert preview["client_id_len"] == 4
