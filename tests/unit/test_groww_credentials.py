"""Groww credential store + read-merge-write coexistence (DEC-DHAN-GROWW-REMIGRATE-001).

Covers the ``groww`` section added to :mod:`brutex.config.credentials`: save /
load / resolve / present / preview / clear, the per-field env-first resolution,
the perm / symlink / blank / corrupt refusals, and — the load-bearing pitfall —
the **read-merge-write coexistence** proving that saving one vendor's
credentials never drops the other vendor's section (in BOTH directions).

All values are FAKE — never a real secret.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from brutex.config.credentials import (
    ENV_GROWW_API_KEY,
    ENV_GROWW_TOTP_SECRET,
    CredentialsError,
    DhanFileCredentials,
    GrowwFileCredentials,
    clear_groww_credentials,
    credentials_path,
    groww_credentials_present,
    groww_credentials_preview,
    load_dhan_credentials,
    load_groww_credentials,
    resolve_groww_file_credentials,
    save_dhan_credentials,
    save_groww_credentials,
)

# FAKE values only — never a real secret.
FAKE_API_KEY = "gw_api_key_abcdef123456"
FAKE_TOTP_SECRET = "JBSWY3DPEHPK3PXP"
FAKE_DHAN_TOKEN = "eyJhbGc.eyJzdWImC6.sIg-na_ture"
FAKE_DHAN_CLIENT = "1100123456"


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _groww(
    api_key: str = FAKE_API_KEY, totp_secret: str = FAKE_TOTP_SECRET
) -> GrowwFileCredentials:
    return GrowwFileCredentials(api_key=api_key, totp_secret=totp_secret)


# ── save ──────────────────────────────────────────────────────────────────


class TestSaveGroww:
    def test_writes_0600_and_shape(self, tmp_path: Path) -> None:
        path = tmp_path / "nest" / "credentials.json"
        out = save_groww_credentials(_groww(), path=path)
        assert out == path
        assert _mode(path) == 0o600
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert raw["groww"] == {"api_key": FAKE_API_KEY, "totp_secret": FAKE_TOTP_SECRET}

    def test_strips_whitespace_on_save(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.json"
        save_groww_credentials(_groww(api_key="  k  \n", totp_secret=" s "), path=path)
        loaded = load_groww_credentials(path=path)
        assert loaded == GrowwFileCredentials(api_key="k", totp_secret="s")

    def test_blank_api_key_raises_without_value(self, tmp_path: Path) -> None:
        with pytest.raises(CredentialsError) as exc:
            save_groww_credentials(_groww(api_key="   "), path=tmp_path / "c.json")
        assert "api_key must not be blank" in str(exc.value)
        assert not (tmp_path / "c.json").exists()

    def test_blank_totp_secret_raises_without_value(self, tmp_path: Path) -> None:
        with pytest.raises(CredentialsError) as exc:
            save_groww_credentials(_groww(totp_secret=""), path=tmp_path / "c.json")
        assert "totp_secret must not be blank" in str(exc.value)
        assert not (tmp_path / "c.json").exists()

    def test_default_path_when_none(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        target = tmp_path / "credentials.json"
        monkeypatch.setenv("BRUTEX_CREDENTIALS_FILE", str(target))
        assert save_groww_credentials(_groww()) == credentials_path()
        assert target.exists()


# ── read-merge-write coexistence (the pitfall) ──────────────────────────────


class TestCoexistence:
    def test_save_dhan_then_groww_keeps_both(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.json"
        save_dhan_credentials(
            DhanFileCredentials(access_token=FAKE_DHAN_TOKEN, client_id=FAKE_DHAN_CLIENT),
            path=path,
        )
        save_groww_credentials(_groww(), path=path)
        dhan = load_dhan_credentials(path=path)
        groww = load_groww_credentials(path=path)
        assert dhan is not None
        assert dhan.access_token == FAKE_DHAN_TOKEN
        assert groww == _groww()

    def test_save_groww_then_dhan_keeps_both(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.json"
        save_groww_credentials(_groww(), path=path)
        save_dhan_credentials(
            DhanFileCredentials(access_token=FAKE_DHAN_TOKEN, client_id=FAKE_DHAN_CLIENT),
            path=path,
        )
        groww = load_groww_credentials(path=path)
        dhan = load_dhan_credentials(path=path)
        assert groww == _groww()
        assert dhan is not None
        assert dhan.client_id == FAKE_DHAN_CLIENT


# ── load ──────────────────────────────────────────────────────────────────


class TestLoadGroww:
    def test_round_trip(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.json"
        save_groww_credentials(_groww(), path=path)
        assert load_groww_credentials(path=path) == _groww()

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert load_groww_credentials(path=tmp_path / "nope.json") is None

    def test_dhan_only_file_returns_none(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.json"
        save_dhan_credentials(
            DhanFileCredentials(access_token=FAKE_DHAN_TOKEN, client_id=FAKE_DHAN_CLIENT),
            path=path,
        )
        assert load_groww_credentials(path=path) is None

    def test_empty_object_file_returns_none(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.json"
        path.write_text("{}", encoding="utf-8")
        path.chmod(0o600)
        assert load_groww_credentials(path=path) is None

    def test_default_path_when_none(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        target = tmp_path / "credentials.json"
        monkeypatch.setenv("BRUTEX_CREDENTIALS_FILE", str(target))
        save_groww_credentials(_groww())
        assert load_groww_credentials() == _groww()

    def test_insecure_perms_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.json"
        save_groww_credentials(_groww(), path=path)
        path.chmod(0o644)
        with pytest.raises(CredentialsError) as exc:
            load_groww_credentials(path=path)
        assert "insecure permissions" in str(exc.value)

    def test_symlink_refused(self, tmp_path: Path) -> None:
        real = tmp_path / "real.json"
        save_groww_credentials(_groww(), path=real)
        link = tmp_path / "credentials.json"
        link.symlink_to(real)
        with pytest.raises(CredentialsError) as exc:
            load_groww_credentials(path=link)
        assert "symlink" in str(exc.value)

    def test_corrupt_json_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.json"
        path.write_text("{not json", encoding="utf-8")
        path.chmod(0o600)
        with pytest.raises(CredentialsError) as exc:
            load_groww_credentials(path=path)
        assert "corrupt" in str(exc.value)

    def test_non_dict_top_level_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.json"
        path.write_text("[1, 2, 3]", encoding="utf-8")
        path.chmod(0o600)
        with pytest.raises(CredentialsError) as exc:
            load_groww_credentials(path=path)
        assert "invalid shape" in str(exc.value)

    def test_groww_section_not_dict_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.json"
        path.write_text(json.dumps({"groww": "nope"}), encoding="utf-8")
        path.chmod(0o600)
        with pytest.raises(CredentialsError) as exc:
            load_groww_credentials(path=path)
        assert "invalid 'groww' section" in str(exc.value)

    def test_missing_api_key_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.json"
        path.write_text(json.dumps({"groww": {"totp_secret": "s"}}), encoding="utf-8")
        path.chmod(0o600)
        with pytest.raises(CredentialsError) as exc:
            load_groww_credentials(path=path)
        assert "blank api_key" in str(exc.value)

    def test_blank_totp_secret_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.json"
        path.write_text(
            json.dumps({"groww": {"api_key": "k", "totp_secret": "  "}}), encoding="utf-8"
        )
        path.chmod(0o600)
        with pytest.raises(CredentialsError) as exc:
            load_groww_credentials(path=path)
        assert "blank totp_secret" in str(exc.value)


# ── resolve / present / preview ─────────────────────────────────────────────


class TestResolveGroww:
    def test_file_fallback(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.json"
        save_groww_credentials(_groww(), path=path)
        assert resolve_groww_file_credentials({}, path=path) == _groww()

    def test_env_first_both_fields(self, tmp_path: Path) -> None:
        env = {ENV_GROWW_API_KEY: "env_key", ENV_GROWW_TOTP_SECRET: "env_secret"}
        # No file at all — env alone resolves.
        out = resolve_groww_file_credentials(env, path=tmp_path / "nope.json")
        assert out == GrowwFileCredentials(api_key="env_key", totp_secret="env_secret")

    def test_env_overrides_one_field_file_supplies_other(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.json"
        save_groww_credentials(_groww(), path=path)
        env = {ENV_GROWW_API_KEY: "env_key"}
        out = resolve_groww_file_credentials(env, path=path)
        assert out == GrowwFileCredentials(api_key="env_key", totp_secret=FAKE_TOTP_SECRET)

    def test_blank_env_falls_back_to_file(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.json"
        save_groww_credentials(_groww(), path=path)
        env = {ENV_GROWW_API_KEY: "  ", ENV_GROWW_TOTP_SECRET: ""}
        assert resolve_groww_file_credentials(env, path=path) == _groww()

    def test_strips_env_values(self, tmp_path: Path) -> None:
        env = {ENV_GROWW_API_KEY: " k \n", ENV_GROWW_TOTP_SECRET: " s "}
        out = resolve_groww_file_credentials(env, path=tmp_path / "nope.json")
        assert out == GrowwFileCredentials(api_key="k", totp_secret="s")

    def test_missing_both_raises_naming_vars(self, tmp_path: Path) -> None:
        with pytest.raises(CredentialsError) as exc:
            resolve_groww_file_credentials({}, path=tmp_path / "nope.json")
        msg = str(exc.value)
        assert ENV_GROWW_API_KEY in msg
        assert ENV_GROWW_TOTP_SECRET in msg

    def test_missing_only_totp_raises(self, tmp_path: Path) -> None:
        env = {ENV_GROWW_API_KEY: "k"}
        with pytest.raises(CredentialsError) as exc:
            resolve_groww_file_credentials(env, path=tmp_path / "nope.json")
        msg = str(exc.value)
        # Only the TOTP var is in the "missing: ..." list; the api key resolved.
        missing_part = msg.split(";", 1)[0]
        assert ENV_GROWW_TOTP_SECRET in missing_part
        assert ENV_GROWW_API_KEY not in missing_part

    def test_default_env_is_os_environ(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv(ENV_GROWW_API_KEY, "k")
        monkeypatch.setenv(ENV_GROWW_TOTP_SECRET, "s")
        out = resolve_groww_file_credentials(path=tmp_path / "nope.json")
        assert out == GrowwFileCredentials(api_key="k", totp_secret="s")


class TestPresentGroww:
    def test_true_when_resolvable(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.json"
        save_groww_credentials(_groww(), path=path)
        assert groww_credentials_present({}, path=path) is True

    def test_false_when_missing(self, tmp_path: Path) -> None:
        assert groww_credentials_present({}, path=tmp_path / "nope.json") is False

    def test_false_on_insecure_file(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.json"
        save_groww_credentials(_groww(), path=path)
        path.chmod(0o644)
        assert groww_credentials_present({}, path=path) is False


class TestPreviewGroww:
    def test_absent(self, tmp_path: Path) -> None:
        assert groww_credentials_preview({}, path=tmp_path / "nope.json") == {"present": False}

    def test_present_masked(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.json"
        save_groww_credentials(_groww(), path=path)
        out = groww_credentials_preview({}, path=path)
        assert out["present"] is True
        assert out["api_key_len"] == len(FAKE_API_KEY)
        assert out["totp_secret_len"] == len(FAKE_TOTP_SECRET)
        # Never echo a usable secret.
        assert FAKE_API_KEY not in str(out)
        assert FAKE_TOTP_SECRET not in str(out)


# ── clear ───────────────────────────────────────────────────────────────────


class TestClearGroww:
    def test_removes_only_groww_section(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.json"
        save_dhan_credentials(
            DhanFileCredentials(access_token=FAKE_DHAN_TOKEN, client_id=FAKE_DHAN_CLIENT),
            path=path,
        )
        save_groww_credentials(_groww(), path=path)
        assert clear_groww_credentials(path=path) is True
        # Groww gone, Dhan preserved.
        assert load_groww_credentials(path=path) is None
        assert load_dhan_credentials(path=path) is not None

    def test_absent_file_returns_false(self, tmp_path: Path) -> None:
        assert clear_groww_credentials(path=tmp_path / "nope.json") is False

    def test_no_groww_section_returns_false(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.json"
        save_dhan_credentials(
            DhanFileCredentials(access_token=FAKE_DHAN_TOKEN, client_id=FAKE_DHAN_CLIENT),
            path=path,
        )
        assert clear_groww_credentials(path=path) is False

    def test_default_path_when_none(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        target = tmp_path / "credentials.json"
        monkeypatch.setenv("BRUTEX_CREDENTIALS_FILE", str(target))
        save_groww_credentials(_groww())
        assert clear_groww_credentials() is True
