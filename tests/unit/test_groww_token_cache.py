"""Unit tests for ``brutex.adapters.groww.token_cache`` (DEC-GROWW-TOKEN-DISK-CACHE-001).

The on-disk daily-token cache is driven against a tmp path with an injected UTC clock,
covering every guard (absent / insecure-mode / unreadable / corrupt / not-object /
schema-mismatch / no-token / fingerprint-mismatch / no-session / bad-session /
stale-session / valid), the 06:00-IST session boundary, the env overrides, and the
secret-hygiene property (the api_key is NEVER written, only its blake3 fingerprint) —
all stdlib-only, no ``growwapi`` / ``pyotp`` / network.
"""

from __future__ import annotations

import json
import os
import stat
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from brutex.adapters.groww.auth import GrowwCredentials
from brutex.adapters.groww.constants import (
    GROWW_TOKEN_CACHE_PATH_ENV,
    GROWW_TOKEN_CACHE_SCHEMA,
    GROWW_TOKEN_RESET_HOUR_ENV,
)
from brutex.adapters.groww.token_cache import (
    CachedToken,
    api_key_fingerprint,
    current_session,
    groww_token_cache_path,
    load_cached_token,
    store_token,
)

_CREDS = GrowwCredentials(api_key="fake_key_976chars", totp_secret="fake_secret_32chars")
#: 2026-06-30 08:00 IST = 2026-06-30 02:30 UTC — after the 06:00 reset ⇒ session 2026-06-30.
_NOW_UTC = datetime(2026, 6, 30, 2, 30, tzinfo=UTC)
_SESSION = date(2026, 6, 30)


@pytest.fixture
def cache_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the token cache to a tmp file via the env override."""
    target = tmp_path / "groww_access_token.json"
    monkeypatch.setenv(GROWW_TOKEN_CACHE_PATH_ENV, str(target))
    return target


# --------------------------------------------------------------------------- #
# current_session — the 06:00-IST boundary
# --------------------------------------------------------------------------- #
class TestCurrentSession:
    def test_after_reset_is_today(self) -> None:
        # 08:00 IST 2026-06-30 → session 2026-06-30 (≥ 06:00).
        assert current_session(datetime(2026, 6, 30, 2, 30, tzinfo=UTC)) == date(2026, 6, 30)

    def test_before_reset_is_yesterday(self) -> None:
        # 05:59 IST 2026-06-30 = 00:29 UTC → session 2026-06-29 (< 06:00).
        assert current_session(datetime(2026, 6, 30, 0, 29, tzinfo=UTC)) == date(2026, 6, 29)

    def test_exactly_at_reset_is_today(self) -> None:
        # 06:00:00 IST 2026-06-30 = 00:30 UTC → session 2026-06-30 (boundary inclusive).
        assert current_session(datetime(2026, 6, 30, 0, 30, tzinfo=UTC)) == date(2026, 6, 30)

    def test_env_reset_hour_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(GROWW_TOKEN_RESET_HOUR_ENV, "9")
        # 08:00 IST < 09:00 custom reset ⇒ previous session.
        assert current_session(datetime(2026, 6, 30, 2, 30, tzinfo=UTC)) == date(2026, 6, 29)

    @pytest.mark.parametrize("raw", ["", "  ", "not-int", "-1", "24", "99"])
    def test_env_reset_hour_bad_value_falls_back(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        monkeypatch.setenv(GROWW_TOKEN_RESET_HOUR_ENV, raw)
        # Falls back to the default 06:00 ⇒ 08:00 IST is today.
        assert current_session(_NOW_UTC) == _SESSION

    def test_env_reset_hour_unset_uses_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(GROWW_TOKEN_RESET_HOUR_ENV, raising=False)
        assert current_session(_NOW_UTC) == _SESSION

    def test_naive_datetime_rejected(self) -> None:
        """A NAIVE (tz-unaware) datetime fails LOUD rather than silently mis-bucketing.

        ``astimezone`` would otherwise interpret a naive instant as the host's LOCAL
        time, assigning the wrong session on a non-UTC host. Every production caller
        passes an aware UTC instant; this guard hardens against a future misuse.
        """
        with pytest.raises(ValueError, match="timezone-aware"):
            current_session(datetime(2026, 6, 30, 2, 30))  # naive on purpose


# --------------------------------------------------------------------------- #
# groww_token_cache_path
# --------------------------------------------------------------------------- #
class TestCachePath:
    def test_env_override(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        target = tmp_path / "sub" / "tok.json"
        monkeypatch.setenv(GROWW_TOKEN_CACHE_PATH_ENV, str(target))
        assert groww_token_cache_path() == target

    def test_env_override_expands_user(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(GROWW_TOKEN_CACHE_PATH_ENV, "~/custom_tok.json")
        assert groww_token_cache_path() == Path.home() / "custom_tok.json"

    def test_default_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(GROWW_TOKEN_CACHE_PATH_ENV, raising=False)
        path = groww_token_cache_path()
        assert path == Path.home() / ".brutex" / "cache" / "groww_access_token.json"

    def test_default_when_blank(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(GROWW_TOKEN_CACHE_PATH_ENV, "   ")
        assert groww_token_cache_path() == Path.home() / ".brutex" / "cache" / (
            "groww_access_token.json"
        )


# --------------------------------------------------------------------------- #
# api_key_fingerprint — secret hygiene
# --------------------------------------------------------------------------- #
class TestFingerprint:
    def test_is_16_hex_and_not_the_key(self) -> None:
        fp = api_key_fingerprint("fake_key_976chars")
        assert len(fp) == 16
        assert all(c in "0123456789abcdef" for c in fp)
        assert "fake_key_976chars" not in fp

    def test_differs_per_key(self) -> None:
        assert api_key_fingerprint("key_a") != api_key_fingerprint("key_b")

    def test_stable(self) -> None:
        assert api_key_fingerprint("key_a") == api_key_fingerprint("key_a")


# --------------------------------------------------------------------------- #
# store_token then load_cached_token — the happy round-trip + the no-leak property
# --------------------------------------------------------------------------- #
class TestRoundTrip:
    def test_store_then_load_valid(self, cache_path: Path) -> None:
        assert store_token(_CREDS, "TOKEN-XYZ", now_utc=_NOW_UTC) is True
        loaded = load_cached_token(_CREDS, now_utc=_NOW_UTC)
        assert loaded == CachedToken(access_token="TOKEN-XYZ", issued_for_session=_SESSION)

    def test_store_writes_0600(self, cache_path: Path) -> None:
        store_token(_CREDS, "TOKEN-XYZ", now_utc=_NOW_UTC)
        mode = stat.S_IMODE(cache_path.stat().st_mode)
        assert mode == 0o600

    def test_store_creates_parent_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "a" / "b" / "tok.json"
        monkeypatch.setenv(GROWW_TOKEN_CACHE_PATH_ENV, str(target))
        assert store_token(_CREDS, "T", now_utc=_NOW_UTC) is True
        assert target.exists()

    def test_no_secret_in_cache_file(self, cache_path: Path) -> None:
        store_token(_CREDS, "TOKEN-XYZ", now_utc=_NOW_UTC)
        body = cache_path.read_text(encoding="utf-8")
        # The token IS in the file (it is the cached value), but NEVER the api_key/secret.
        assert _CREDS.api_key not in body
        assert _CREDS.totp_secret not in body
        assert api_key_fingerprint(_CREDS.api_key) in body

    def test_store_blank_token_is_noop(self, cache_path: Path) -> None:
        assert store_token(_CREDS, "", now_utc=_NOW_UTC) is False
        assert not cache_path.exists()


# --------------------------------------------------------------------------- #
# load_cached_token — every miss reason
# --------------------------------------------------------------------------- #
class TestLoadMisses:
    def test_absent(self, cache_path: Path) -> None:
        assert load_cached_token(_CREDS, now_utc=_NOW_UTC) is None

    def test_insecure_mode_rejected(self, cache_path: Path) -> None:
        store_token(_CREDS, "T", now_utc=_NOW_UTC)
        cache_path.chmod(0o644)  # group/other-readable — a bearer token must be 0600
        assert load_cached_token(_CREDS, now_utc=_NOW_UTC) is None

    def test_corrupt_json(self, cache_path: Path) -> None:
        cache_path.write_text("{not json", encoding="utf-8")
        cache_path.chmod(0o600)
        assert load_cached_token(_CREDS, now_utc=_NOW_UTC) is None

    def test_not_object(self, cache_path: Path) -> None:
        cache_path.write_text("[1, 2, 3]", encoding="utf-8")
        cache_path.chmod(0o600)
        assert load_cached_token(_CREDS, now_utc=_NOW_UTC) is None

    def test_schema_mismatch(self, cache_path: Path) -> None:
        _write(cache_path, {"schema": 999, "access_token": "T", "issued_for_session": "2026-06-30"})
        assert load_cached_token(_CREDS, now_utc=_NOW_UTC) is None

    def test_no_token(self, cache_path: Path) -> None:
        _write(
            cache_path,
            {
                "schema": GROWW_TOKEN_CACHE_SCHEMA,
                "access_token": "",
                "issued_for_session": "2026-06-30",
                "api_key_fingerprint": api_key_fingerprint(_CREDS.api_key),
            },
        )
        assert load_cached_token(_CREDS, now_utc=_NOW_UTC) is None

    def test_token_not_string(self, cache_path: Path) -> None:
        _write(
            cache_path,
            {
                "schema": GROWW_TOKEN_CACHE_SCHEMA,
                "access_token": 12345,
                "issued_for_session": "2026-06-30",
                "api_key_fingerprint": api_key_fingerprint(_CREDS.api_key),
            },
        )
        assert load_cached_token(_CREDS, now_utc=_NOW_UTC) is None

    def test_fingerprint_mismatch(self, cache_path: Path) -> None:
        _write(
            cache_path,
            {
                "schema": GROWW_TOKEN_CACHE_SCHEMA,
                "access_token": "T",
                "issued_for_session": "2026-06-30",
                "api_key_fingerprint": "deadbeefdeadbeef",
            },
        )
        assert load_cached_token(_CREDS, now_utc=_NOW_UTC) is None

    def test_no_session(self, cache_path: Path) -> None:
        _write(
            cache_path,
            {
                "schema": GROWW_TOKEN_CACHE_SCHEMA,
                "access_token": "T",
                "api_key_fingerprint": api_key_fingerprint(_CREDS.api_key),
            },
        )
        assert load_cached_token(_CREDS, now_utc=_NOW_UTC) is None

    def test_bad_session_string(self, cache_path: Path) -> None:
        _write(
            cache_path,
            {
                "schema": GROWW_TOKEN_CACHE_SCHEMA,
                "access_token": "T",
                "issued_for_session": "not-a-date",
                "api_key_fingerprint": api_key_fingerprint(_CREDS.api_key),
            },
        )
        assert load_cached_token(_CREDS, now_utc=_NOW_UTC) is None

    def test_stale_session(self, cache_path: Path) -> None:
        store_token(_CREDS, "T", now_utc=_NOW_UTC)
        # A day later: current_session has advanced past the stored session → miss.
        later = _NOW_UTC + timedelta(days=1)
        assert load_cached_token(_CREDS, now_utc=later) is None

    def test_post_06_00_stale(self, cache_path: Path) -> None:
        # Minted yesterday 08:00 IST → session 2026-06-29.
        minted = datetime(2026, 6, 29, 2, 30, tzinfo=UTC)
        store_token(_CREDS, "T", now_utc=minted)
        # Now today 07:00 IST = 01:30 UTC → session 2026-06-30 ≠ stored → miss.
        now = datetime(2026, 6, 30, 1, 30, tzinfo=UTC)
        assert load_cached_token(_CREDS, now_utc=now) is None

    def test_unreadable_file_returns_none(
        self, cache_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store_token(_CREDS, "T", now_utc=_NOW_UTC)

        def _boom(*_a: object, **_k: object) -> str:
            raise OSError("read denied")

        # 0600-mode passes, then the read raises — exercises the unreadable
        # branch. DEC-LAKE-READ-INODE-GUARD-001: the read rides the guarded
        # reader now — fault THAT seam (Path.read_text is no longer used).
        from brutex.data import guarded_read

        monkeypatch.setattr(guarded_read, "read_regular_text", _boom)
        assert load_cached_token(_CREDS, now_utc=_NOW_UTC) is None

    def test_non_utf8_bytes_returns_none(self, cache_path: Path) -> None:
        """A non-UTF-8 cache file is a cache-MISS, NEVER a raise (the MAJOR regression).

        ``path.read_text(encoding="utf-8")`` raises ``UnicodeDecodeError`` (a
        ``ValueError`` subclass, NOT an ``OSError``) on non-UTF-8 bytes. Before the
        ``except (OSError, ValueError)`` broadening this escaped ``load_cached_token``
        and crashed ``SdkRefresher.__init__`` (and the whole cold-start pull build) —
        worse than the re-mint storm the cache exists to prevent. A corrupt cache must
        re-mint, not kill the pull. This test FAILS before the fix (the call raises).
        """
        # 0600 file of raw non-UTF-8 bytes — passes the stat + insecure-mode guards,
        # then the UTF-8 decode of the body fails.
        cache_path.write_bytes(b"\xff\xfe\x00\x80garbage")
        cache_path.chmod(0o600)
        # Must return None WITHOUT raising (the module's "NEVER raises" contract).
        assert load_cached_token(_CREDS, now_utc=_NOW_UTC) is None


# --------------------------------------------------------------------------- #
# store_token — best-effort write-failure path
# --------------------------------------------------------------------------- #
class TestStoreFailure:
    def test_write_failure_returns_false(
        self, cache_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(*_a: object, **_k: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr("brutex.adapters.groww.token_cache.atomic_write_simple", _boom)
        # Never raises — returns False so a caching hiccup can't break the build.
        assert store_token(_CREDS, "T", now_utc=_NOW_UTC) is False

    def test_mkdir_failure_returns_false(
        self, cache_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(*_a: object, **_k: object) -> None:
            raise OSError("mkdir denied")

        monkeypatch.setattr(Path, "mkdir", _boom)
        assert store_token(_CREDS, "T", now_utc=_NOW_UTC) is False


def _write(path: Path, payload: dict[str, object]) -> None:
    """Write *payload* as JSON at 0600 (bypassing store_token to craft edge cases)."""
    path.write_text(json.dumps(payload), encoding="utf-8")
    os.chmod(path, 0o600)
