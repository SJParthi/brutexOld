"""Tests for `brutex.adapters.groww.pull_singleflight` (DEC-ACCOUNT-PULL-SINGLEFLIGHT-001).

100% line+branch: the lock path, the held → second-acquire-refused contract (a SEPARATE
fd in the same process raises :class:`PullAlreadyRunningError` under ``LOCK_NB``), that
releasing lets it re-acquire, the disk side-effects (``.locks`` dir created, 0o600 perms),
and the error message carrying the lock path.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import socket
import stat
import threading
import time
from pathlib import Path

import pytest
from brutex.adapters.groww import pull_singleflight
from brutex.adapters.groww.pull_singleflight import (
    ACCOUNT_LOCK_FILE_PREFIX,
    PULL_SINGLEFLIGHT_LOCK_NAME,
    ForeignPullActiveError,
    PullAlreadyRunningError,
    PullLease,
    account_pull_singleflight_lock,
    account_singleflight_lock_path,
    default_account_lock_dir,
    foreign_pull_lease,
    pull_lease_enabled,
    pull_lease_guard,
    pull_lease_path,
    pull_singleflight_lock,
    pull_singleflight_lock_path,
    read_pull_lease,
)
from brutex.data.errors import LakeWriteHalt


def test_lock_path_is_stable_sidecar_under_lake_root(tmp_path: Path) -> None:
    path = pull_singleflight_lock_path(tmp_path)
    assert path == tmp_path / ".locks" / PULL_SINGLEFLIGHT_LOCK_NAME
    # Computed only — never created by the path helper.
    assert not path.exists()


def test_acquire_creates_locks_dir_and_owner_only_lockfile(tmp_path: Path) -> None:
    with pull_singleflight_lock(tmp_path):
        lock_path = pull_singleflight_lock_path(tmp_path)
        assert lock_path.exists()
        # Owner-only 0o600 (CodeQL py/overly-permissive-file-permission).
        assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600


def test_second_acquire_while_held_is_refused(tmp_path: Path) -> None:
    # Acquire once; a SECOND acquisition (a SEPARATE fd, same process) must be refused
    # immediately by the non-blocking flock — the cross-process guard's core contract.
    # Contexts enter left-to-right: the holder takes the lock, then ``pytest.raises``
    # wraps the contended acquire, whose ``__enter__`` raises while the holder is active.
    with (
        pull_singleflight_lock(tmp_path),
        pytest.raises(PullAlreadyRunningError) as excinfo,
        pull_singleflight_lock(tmp_path),  # pragma: no cover — __enter__ raises first
    ):
        pass  # pragma: no cover — body never runs (the inner acquire raises)
    # The error names the contended lock file for an actionable operator message.
    assert excinfo.value.lock_path == pull_singleflight_lock_path(tmp_path)
    assert "a pull is already running" in str(excinfo.value)


def test_release_lets_it_reacquire(tmp_path: Path) -> None:
    # First holder releases on ``with`` exit; a fresh acquire then succeeds.
    with pull_singleflight_lock(tmp_path):
        pass
    with pull_singleflight_lock(tmp_path):
        pass  # re-acquired without raising — the release worked.


def test_two_distinct_lake_roots_never_contend(tmp_path: Path) -> None:
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    # Different lake roots hash to different lock files, so both can be held at once.
    with pull_singleflight_lock(root_a), pull_singleflight_lock(root_b):
        assert pull_singleflight_lock_path(root_a) != pull_singleflight_lock_path(root_b)


def test_lock_released_on_error_inside_block(tmp_path: Path) -> None:
    # An exception inside the ``with`` block still releases the lock (finally closes fd).
    class _Boom(Exception):
        pass

    def _raise_inside_lock() -> None:
        with pull_singleflight_lock(tmp_path):
            raise _Boom

    # ``pytest.raises`` catches the error raised inside the lock block; the lock's
    # ``finally`` runs during unwinding so the fd is closed before we return here.
    with pytest.raises(_Boom):
        _raise_inside_lock()
    # The lock is now free → re-acquire succeeds.
    with pull_singleflight_lock(tmp_path):
        pass


def test_contended_path_does_not_close_the_holders_lock(tmp_path: Path) -> None:
    # The refused acquire opens its OWN fd, fails to flock it, and closes only that fd in
    # its finally — the holder's lock must remain held throughout.
    with pull_singleflight_lock(tmp_path):
        for _ in range(3):
            with pytest.raises(PullAlreadyRunningError), pull_singleflight_lock(tmp_path):
                pass  # pragma: no cover — body never runs (acquire raises first)
        # Still held: a fourth attempt is still refused (the holder was never released).
        with pytest.raises(PullAlreadyRunningError), pull_singleflight_lock(tmp_path):
            pass  # pragma: no cover — body never runs (acquire raises first)
    # Holder released now → free.
    with pull_singleflight_lock(tmp_path):
        pass


def test_lock_survives_when_lockfile_preexists(tmp_path: Path) -> None:
    # A pre-existing lock file (from a prior process) is reused (O_CREAT is idempotent),
    # not clobbered — open + flock the same inode.
    locks = tmp_path / ".locks"
    locks.mkdir(parents=True)
    (locks / PULL_SINGLEFLIGHT_LOCK_NAME).write_text("")
    with pull_singleflight_lock(tmp_path):
        assert os.path.exists(locks / PULL_SINGLEFLIGHT_LOCK_NAME)


def test_lockfile_open_enospc_maps_to_lake_write_halt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # M3 (DEC-PULL-HARDENING-FOLLOWUP-001, MIGRATION.md #996): a full disk at the lock-file
    # open maps to a LakeWriteHalt (consistent with partition_write_lock), NOT a raw OSError
    # that would escape the dashboard start() path uncaught.
    def _enospc(*_args: object, **_kwargs: object) -> int:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr("brutex.adapters.groww.pull_singleflight.os.open", _enospc)
    with pytest.raises(LakeWriteHalt) as excinfo, pull_singleflight_lock(tmp_path):
        pass  # pragma: no cover — open raises before the body runs
    assert excinfo.value.reason == "disk_full"


# ════════════════════════════════════════════════════════════════════════
# Account-keyed single-flight lock (DEC-ACCOUNT-KEY-SINGLEFLIGHT-001)
# ════════════════════════════════════════════════════════════════════════


def test_account_lock_path_is_digest_only(tmp_path: Path) -> None:
    """The filename carries a 16-hex-char PBKDF2 fingerprint — NEVER the api_key itself.

    PBKDF2-HMAC-SHA256 over the fixed app salt instead of a bare sha256, so the
    visible filename cannot be used as a cheap offline-guessing oracle for the
    credential it fingerprints (CodeQL py/weak-sensitive-data-hashing).
    """
    api_key = "super-secret-api-key"
    path = account_singleflight_lock_path(api_key, lock_dir=tmp_path)
    assert path.parent == tmp_path
    name = path.name
    assert name.startswith(ACCOUNT_LOCK_FILE_PREFIX)
    assert name.endswith(".flock")
    digest = name[len(ACCOUNT_LOCK_FILE_PREFIX) : -len(".flock")]
    assert len(digest) == 16
    assert all(c in "0123456789abcdef" for c in digest)
    assert api_key not in str(path)  # no secret in a path or a log line
    expected = hashlib.pbkdf2_hmac(
        "sha256", api_key.encode("utf-8"), b"brutex-pull-account-lock", 200_000
    ).hex()[:16]
    assert digest == expected
    # Computed only — never created by the path helper.
    assert not path.exists()


def test_account_lock_path_stable_per_key_distinct_across_keys(tmp_path: Path) -> None:
    a1 = account_singleflight_lock_path("key-A", lock_dir=tmp_path)
    a2 = account_singleflight_lock_path("key-A", lock_dir=tmp_path)
    b = account_singleflight_lock_path("key-B", lock_dir=tmp_path)
    assert a1 == a2
    assert a1 != b


def test_account_lock_default_dir_is_per_user_home() -> None:
    assert default_account_lock_dir() == Path.home() / ".brutex" / "locks"
    # The default-dir branch of the path helper (path computation only — no disk touch).
    path = account_singleflight_lock_path("key-A")
    assert path.parent == default_account_lock_dir()


def test_account_lock_acquire_creates_dir_and_owner_only_file(tmp_path: Path) -> None:
    lock_dir = tmp_path / "locks"  # not pre-created — mkdir(parents=True) path
    with account_pull_singleflight_lock("key-A", lock_dir=lock_dir):
        path = account_singleflight_lock_path("key-A", lock_dir=lock_dir)
        assert path.exists()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_account_lock_contended_refused_and_released_on_exit(tmp_path: Path) -> None:
    """The SAME api_key contends across two handles — even with DIFFERENT lake roots."""
    with (
        account_pull_singleflight_lock("key-A", lock_dir=tmp_path),
        pytest.raises(PullAlreadyRunningError) as excinfo,
        account_pull_singleflight_lock("key-A", lock_dir=tmp_path),  # pragma: no cover
    ):
        pass  # pragma: no cover — the inner acquire raises first
    assert excinfo.value.lock_path == account_singleflight_lock_path("key-A", lock_dir=tmp_path)
    # Holder released on exit → a fresh acquire succeeds.
    with account_pull_singleflight_lock("key-A", lock_dir=tmp_path):
        pass


def test_account_lock_distinct_keys_never_contend(tmp_path: Path) -> None:
    with (
        account_pull_singleflight_lock("key-A", lock_dir=tmp_path),
        account_pull_singleflight_lock("key-B", lock_dir=tmp_path),
    ):
        pass  # two DIFFERENT accounts may pull concurrently


def test_account_lock_enospc_maps_to_lake_write_halt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _enospc(*_args: object, **_kwargs: object) -> int:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr("brutex.adapters.groww.pull_singleflight.os.open", _enospc)
    with (
        pytest.raises(LakeWriteHalt) as excinfo,
        account_pull_singleflight_lock("key-A", lock_dir=tmp_path),
    ):
        pass  # pragma: no cover — open raises before the body runs
    assert excinfo.value.reason == "disk_full"


# ════════════════════════════════════════════════════════════════════════
# Cross-host foreign-pull LEASE (DEC-ACCOUNT-LEASE-CROSSHOST-001)
# ════════════════════════════════════════════════════════════════════════


def _write_lease(
    lake_root: Path,
    *,
    host: str = "other-host",
    token: str = "tok-1",  # noqa: S107 — a random lease identity, not a password
    pid: int = 4242,
    started_wall: float = 1_000.0,
    wall_time: float = 1_000.0,
    schema: int = pull_singleflight.PULL_LEASE_SCHEMA,
) -> Path:
    """Hand-write a lease file (the on-disk JSON shape) for read/verdict tests."""
    path = pull_lease_path(lake_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": schema,
        "token": token,
        "host": host,
        "pid": pid,
        "started_wall": started_wall,
        "wall_time": wall_time,
    }
    path.write_text(json.dumps(payload))
    return path


def test_lease_path_is_stable_sidecar_under_lake_root(tmp_path: Path) -> None:
    path = pull_lease_path(tmp_path)
    assert path == tmp_path / ".locks" / "pull_lease.json"
    assert not path.exists()  # computed only — never created by the path helper


def test_lease_enabled_default_on_and_off_values() -> None:
    assert pull_lease_enabled({}) is True
    assert pull_lease_enabled({"BRUTEX_PULL_LEASE": "1"}) is True
    assert pull_lease_enabled({"BRUTEX_PULL_LEASE": "anything"}) is True
    for off in ("0", "off", "false", "no", " OFF "):
        assert pull_lease_enabled({"BRUTEX_PULL_LEASE": off}) is False


def test_lease_enabled_env_none_reads_process_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRUTEX_PULL_LEASE", "off")
    assert pull_lease_enabled() is False
    monkeypatch.delenv("BRUTEX_PULL_LEASE")
    assert pull_lease_enabled() is True


def test_read_lease_absent_is_none(tmp_path: Path) -> None:
    assert read_pull_lease(tmp_path) is None


def test_read_lease_corrupt_json_is_none(tmp_path: Path) -> None:
    path = pull_lease_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"{not json")
    assert read_pull_lease(tmp_path) is None


def test_read_lease_non_object_is_none(tmp_path: Path) -> None:
    path = pull_lease_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("[1, 2, 3]")
    assert read_pull_lease(tmp_path) is None


def test_read_lease_unrecognised_schema_is_none(tmp_path: Path) -> None:
    _write_lease(tmp_path, schema=99)
    assert read_pull_lease(tmp_path) is None


def test_read_lease_missing_field_is_none(tmp_path: Path) -> None:
    path = pull_lease_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema": pull_singleflight.PULL_LEASE_SCHEMA, "host": "h"}))
    assert read_pull_lease(tmp_path) is None


def test_read_lease_wrong_typed_field_is_none(tmp_path: Path) -> None:
    path = pull_lease_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "schema": pull_singleflight.PULL_LEASE_SCHEMA,
                "token": "t",
                "host": "h",
                "pid": "not-an-int",
                "started_wall": 1.0,
                "wall_time": 1.0,
            }
        )
    )
    assert read_pull_lease(tmp_path) is None


def test_read_lease_happy_roundtrip(tmp_path: Path) -> None:
    _write_lease(tmp_path, host="peer", token="tok-9", pid=7, started_wall=5.0, wall_time=6.0)
    lease = read_pull_lease(tmp_path)
    assert lease == PullLease(
        schema=pull_singleflight.PULL_LEASE_SCHEMA,
        token="tok-9",
        host="peer",
        pid=7,
        started_wall=5.0,
        wall_time=6.0,
    )


def test_foreign_verdict_none_without_lease(tmp_path: Path) -> None:
    assert foreign_pull_lease(tmp_path, host="me", now_wall=100.0) is None


def test_foreign_verdict_ignores_same_host(tmp_path: Path) -> None:
    # SOUND because the caller holds both flocks: no live same-host pull can exist,
    # so a same-host lease — however fresh — is a crashed/finished pull's residue.
    _write_lease(tmp_path, host="me", wall_time=100.0)
    assert foreign_pull_lease(tmp_path, host="me", now_wall=100.0) is None


def test_foreign_verdict_ignores_stale_foreign(tmp_path: Path) -> None:
    _write_lease(tmp_path, host="peer", wall_time=100.0)
    assert (
        foreign_pull_lease(tmp_path, host="me", now_wall=100.0 + 180.0 + 1.0, stale_after_s=180.0)
        is None
    )


def test_foreign_verdict_flags_fresh_foreign_with_age(tmp_path: Path) -> None:
    _write_lease(tmp_path, host="peer", wall_time=100.0)
    verdict = foreign_pull_lease(tmp_path, host="me", now_wall=160.0, stale_after_s=180.0)
    assert verdict is not None
    lease, age_s = verdict
    assert lease.host == "peer"
    assert age_s == 60.0


def test_foreign_verdict_future_heartbeat_reads_fresh(tmp_path: Path) -> None:
    # A skewed-AHEAD writer clock (negative age) reads FRESH — the conservative
    # refusal; it self-heals once TTL + skew elapse (documented envelope).
    _write_lease(tmp_path, host="peer", wall_time=500.0)
    assert foreign_pull_lease(tmp_path, host="me", now_wall=100.0) is not None


def test_foreign_pull_error_message_and_compat(tmp_path: Path) -> None:
    lease = PullLease(
        schema=pull_singleflight.PULL_LEASE_SCHEMA,
        token="t",
        host="peer-host",
        pid=99,
        started_wall=1.0,
        wall_time=2.0,
    )
    err = ForeignPullActiveError(pull_lease_path(tmp_path), lease, 42.0)
    # Subclass compat: every existing PullAlreadyRunningError refusal path handles it.
    assert isinstance(err, PullAlreadyRunningError)
    assert err.lease is lease
    assert err.lock_path == pull_lease_path(tmp_path)
    message = str(err)
    assert "another host" in message
    assert "'peer-host'" in message
    assert "pid 99" in message
    assert "42s old" in message
    assert "goes stale by itself" in message  # honest self-heal note, never "kill it"


def test_keeper_refresh_writes_owner_only_lease(tmp_path: Path) -> None:
    clock = iter([10.0, 20.0, 30.0])
    keeper = pull_singleflight._PullLeaseKeeper(  # pyright: ignore[reportPrivateUsage]
        pull_lease_path(tmp_path), host="me", now_fn=lambda: next(clock)
    )
    keeper.refresh()
    path = pull_lease_path(tmp_path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    lease = read_pull_lease(tmp_path)
    assert lease is not None
    assert lease.host == "me"
    assert lease.pid == os.getpid()
    assert lease.started_wall == 10.0  # stamped at keeper construction
    assert lease.wall_time == 20.0  # the first refresh tick
    assert lease.token == keeper.token
    keeper.refresh()
    refreshed = read_pull_lease(tmp_path)
    assert refreshed is not None
    assert refreshed.wall_time == 30.0  # heartbeat moved
    assert refreshed.token == keeper.token  # identity stable across refreshes


def test_keeper_refresh_failure_warns_once_and_never_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keeper = pull_singleflight._PullLeaseKeeper(  # pyright: ignore[reportPrivateUsage]
        pull_lease_path(tmp_path), host="me", now_fn=lambda: 1.0
    )

    def _boom(*_a: object, **_k: object) -> None:
        raise OSError("disk unhappy")

    monkeypatch.setattr(pull_singleflight, "atomic_write_simple", _boom)
    warned: list[str] = []
    monkeypatch.setattr(
        pull_singleflight._LOG,  # pyright: ignore[reportPrivateUsage]
        "warning",
        lambda event, **_kw: warned.append(event),
    )
    keeper.refresh()  # must NOT raise — the pull is unaffected
    keeper.refresh()  # second failure: the warn-once flag suppresses a repeat
    assert warned == ["pull_lease.write_failed"]


def test_keeper_clear_removes_only_its_own_lease(tmp_path: Path) -> None:
    keeper = pull_singleflight._PullLeaseKeeper(  # pyright: ignore[reportPrivateUsage]
        pull_lease_path(tmp_path), host="me", now_fn=lambda: 1.0
    )
    keeper.refresh()
    assert pull_lease_path(tmp_path).exists()
    keeper.clear()
    assert not pull_lease_path(tmp_path).exists()  # ours → removed


def test_keeper_clear_leaves_a_racing_peers_lease(tmp_path: Path) -> None:
    keeper = pull_singleflight._PullLeaseKeeper(  # pyright: ignore[reportPrivateUsage]
        pull_lease_path(tmp_path), host="me", now_fn=lambda: 1.0
    )
    keeper.refresh()
    # A racing peer (inside the documented no-mutex envelope) overwrote our lease.
    _write_lease(tmp_path, host="peer", token="peer-token")
    keeper.clear()
    lease = read_pull_lease(tmp_path)
    assert lease is not None and lease.token == "peer-token"  # never deleted from under it


def test_keeper_clear_noop_when_lease_unreadable(tmp_path: Path) -> None:
    keeper = pull_singleflight._PullLeaseKeeper(  # pyright: ignore[reportPrivateUsage]
        pull_lease_path(tmp_path), host="me", now_fn=lambda: 1.0
    )
    keeper.clear()  # no lease on disk — nothing to do, never raises


def test_keeper_clear_unlink_failure_warns_and_swallows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keeper = pull_singleflight._PullLeaseKeeper(  # pyright: ignore[reportPrivateUsage]
        pull_lease_path(tmp_path), host="me", now_fn=lambda: 1.0
    )
    keeper.refresh()

    def _boom(_self: Path, *a: object, **k: object) -> None:
        raise OSError("busy")

    monkeypatch.setattr(Path, "unlink", _boom)
    warned: list[str] = []
    monkeypatch.setattr(
        pull_singleflight._LOG,  # pyright: ignore[reportPrivateUsage]
        "warning",
        lambda event, **_kw: warned.append(event),
    )
    keeper.clear()  # must NOT raise — a leftover lease goes stale by itself
    assert warned == ["pull_lease.clear_failed"]


def test_heartbeat_loop_exits_without_refresh_when_pre_stopped() -> None:
    stop = threading.Event()
    stop.set()
    calls: list[bool] = []
    pull_singleflight._lease_heartbeat_loop(  # pyright: ignore[reportPrivateUsage]
        stop, 0.001, lambda: calls.append(True)
    )
    assert calls == []


def test_heartbeat_loop_refreshes_until_stopped() -> None:
    stop = threading.Event()
    calls: list[bool] = []

    def refresh() -> None:
        calls.append(True)
        stop.set()  # one tick, then stop — deterministic exit

    pull_singleflight._lease_heartbeat_loop(  # pyright: ignore[reportPrivateUsage]
        stop, 0.001, refresh
    )
    assert calls == [True]


def test_lease_guard_disabled_is_a_pure_noop(tmp_path: Path) -> None:
    # A pre-existing FRESH foreign lease would refuse — but the kill switch is off.
    _write_lease(tmp_path, host="peer", wall_time=time.time())
    with pull_lease_guard(tmp_path, host="me", env={"BRUTEX_PULL_LEASE": "0"}):
        # No lease of OURS written; the foreign one untouched.
        lease = read_pull_lease(tmp_path)
        assert lease is not None and lease.host == "peer"
    lease_after = read_pull_lease(tmp_path)
    assert lease_after is not None and lease_after.host == "peer"  # never cleared


def test_lease_guard_happy_writes_then_clears(tmp_path: Path) -> None:
    with pull_lease_guard(tmp_path, host="me", env={}, heartbeat_interval_s=3600.0):
        lease = read_pull_lease(tmp_path)
        assert lease is not None
        assert lease.host == "me"
        assert lease.pid == os.getpid()
    assert read_pull_lease(tmp_path) is None  # cleared at exit


def test_lease_guard_default_host_is_gethostname(tmp_path: Path) -> None:
    with pull_lease_guard(tmp_path, env={}, heartbeat_interval_s=3600.0):
        lease = read_pull_lease(tmp_path)
        assert lease is not None
        assert lease.host == socket.gethostname()


def test_lease_guard_refuses_fresh_foreign_lease(tmp_path: Path) -> None:
    _write_lease(tmp_path, host="peer", token="peer-token", wall_time=1_000.0)
    with (
        pytest.raises(ForeignPullActiveError) as excinfo,
        pull_lease_guard(tmp_path, host="me", env={}, now_fn=lambda: 1_010.0),
    ):
        pass  # pragma: no cover — enter raises first
    assert "another host" in str(excinfo.value)
    # The foreign lease was NOT overwritten by the refused starter.
    lease = read_pull_lease(tmp_path)
    assert lease is not None and lease.token == "peer-token"


def test_lease_guard_overwrites_stale_foreign_lease(tmp_path: Path) -> None:
    _write_lease(tmp_path, host="peer", wall_time=1_000.0)
    now = 1_000.0 + pull_singleflight.PULL_LEASE_STALE_AFTER_S + 1.0
    with pull_lease_guard(
        tmp_path, host="me", env={}, now_fn=lambda: now, heartbeat_interval_s=3600.0
    ):
        lease = read_pull_lease(tmp_path)
        assert lease is not None and lease.host == "me"  # stale peer lease superseded
    assert read_pull_lease(tmp_path) is None


def test_lease_guard_proceeds_over_same_host_lease(tmp_path: Path) -> None:
    # The guard is entered AFTER both flocks, so a same-host lease is provably a
    # crashed/finished pull's residue — proceed and supersede it.
    _write_lease(tmp_path, host="me", token="crashed", wall_time=time.time())
    with pull_lease_guard(tmp_path, host="me", env={}, heartbeat_interval_s=3600.0):
        lease = read_pull_lease(tmp_path)
        assert lease is not None and lease.token != "crashed"
    assert read_pull_lease(tmp_path) is None


def test_lease_guard_clears_lease_on_error_inside_block(tmp_path: Path) -> None:
    class _Boom(Exception):
        pass

    with (
        pytest.raises(_Boom),
        pull_lease_guard(tmp_path, host="me", env={}, heartbeat_interval_s=3600.0),
    ):
        raise _Boom
    assert read_pull_lease(tmp_path) is None  # finally cleared


def test_lease_guard_heartbeat_actually_refreshes(tmp_path: Path) -> None:
    # A REAL daemon heartbeat at a tiny interval moves wall_time within the deadline.
    with pull_lease_guard(tmp_path, host="me", env={}, heartbeat_interval_s=0.01):
        first = read_pull_lease(tmp_path)
        assert first is not None
        deadline = time.monotonic() + 5.0
        moved = False
        while time.monotonic() < deadline:
            current = read_pull_lease(tmp_path)
            if current is not None and current.wall_time > first.wall_time:
                moved = True
                break
            time.sleep(0.01)
        assert moved, "heartbeat never refreshed the lease within 5s"
    assert read_pull_lease(tmp_path) is None


def test_lease_guard_heartbeat_spawn_failure_degrades_gracefully(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _no_threads(_self: threading.Thread) -> None:
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading.Thread, "start", _no_threads)
    warned: list[str] = []
    monkeypatch.setattr(
        pull_singleflight._LOG,  # pyright: ignore[reportPrivateUsage]
        "warning",
        lambda event, **_kw: warned.append(event),
    )
    with pull_lease_guard(tmp_path, host="me", env={}):
        lease = read_pull_lease(tmp_path)
        assert lease is not None and lease.host == "me"  # lease still written
    assert read_pull_lease(tmp_path) is None  # still cleared at exit
    assert warned == ["pull_lease.heartbeat_spawn_failed"]
