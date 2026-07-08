# pyright: reportPrivateUsage=none
"""Tests for `brutex.nightly.probes` (DEC-SWEEP-NIGHTLY-AUTOMATION-001).

Disk floors: fail-loud env parses (junk/negative → NightlyEnvError naming the
var + value), boundary exactness (free == required PASSES; one byte under
REFUSES), the inventory-headroom arithmetic, and the nearest-existing-ancestor
volume probe. Liveness: the full matrix — no file / idle / running+fresh /
running+stale / garbled JSON / checkpoint second signal / forward-compat
dead-pid demotion / the P2 flock capability probe — with ``heuristic=True``
always.
"""

from __future__ import annotations

import errno
import fcntl
import json
import math
import os
from dataclasses import dataclass
from pathlib import Path

import pytest
from brutex.nightly.probes import (
    NIGHTLY_BACKUP_HEADROOM,
    NIGHTLY_MIN_FREE_BACKUP_ENV,
    NIGHTLY_MIN_FREE_LAKE_ENV,
    NIGHTLY_SWEEP_FRESH_ENV,
    NightlyEnvError,
    _default_pid_alive,
    check_disk_floors,
    probe_sweep_liveness,
    resolve_free_floor,
    resolve_sweep_fresh_seconds,
)


@dataclass(frozen=True)
class _Usage:
    total: int
    used: int
    free: int


def _usage_map(mapping: dict[str, int]) -> object:
    """Fake ``shutil.disk_usage`` keyed by probed path string."""

    def usage(path: str) -> _Usage:
        return _Usage(total=10**12, used=0, free=mapping[path])

    return usage


# ── resolve_free_floor / resolve_sweep_fresh_seconds ─────────────────────


def test_free_floor_default_when_missing_or_blank() -> None:
    assert resolve_free_floor({}, NIGHTLY_MIN_FREE_LAKE_ENV, 7) == 7
    assert resolve_free_floor({NIGHTLY_MIN_FREE_LAKE_ENV: "   "}, NIGHTLY_MIN_FREE_LAKE_ENV, 7) == 7


def test_free_floor_parses_valid_values_including_zero() -> None:
    env = {NIGHTLY_MIN_FREE_BACKUP_ENV: "12345"}
    assert resolve_free_floor(env, NIGHTLY_MIN_FREE_BACKUP_ENV, 7) == 12345
    assert (
        resolve_free_floor({NIGHTLY_MIN_FREE_BACKUP_ENV: "0"}, NIGHTLY_MIN_FREE_BACKUP_ENV, 7) == 0
    )


def test_free_floor_junk_fails_loud_naming_var_and_value() -> None:
    with pytest.raises(NightlyEnvError) as excinfo:
        resolve_free_floor({NIGHTLY_MIN_FREE_LAKE_ENV: "20GB"}, NIGHTLY_MIN_FREE_LAKE_ENV, 7)
    assert NIGHTLY_MIN_FREE_LAKE_ENV in str(excinfo.value)
    assert "'20GB'" in str(excinfo.value)


def test_free_floor_negative_fails_loud() -> None:
    with pytest.raises(NightlyEnvError, match=">= 0"):
        resolve_free_floor({NIGHTLY_MIN_FREE_LAKE_ENV: "-1"}, NIGHTLY_MIN_FREE_LAKE_ENV, 7)


def test_free_floor_env_none_reads_process_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(NIGHTLY_MIN_FREE_LAKE_ENV, "42")
    assert resolve_free_floor(None, NIGHTLY_MIN_FREE_LAKE_ENV, 7) == 42


def test_fresh_seconds_default_valid_junk_and_nonpositive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert resolve_sweep_fresh_seconds({}) == 900.0
    assert resolve_sweep_fresh_seconds({NIGHTLY_SWEEP_FRESH_ENV: "60.5"}) == 60.5
    with pytest.raises(NightlyEnvError, match="number of seconds"):
        resolve_sweep_fresh_seconds({NIGHTLY_SWEEP_FRESH_ENV: "fast"})
    with pytest.raises(NightlyEnvError, match="> 0"):
        resolve_sweep_fresh_seconds({NIGHTLY_SWEEP_FRESH_ENV: "0"})
    monkeypatch.setenv(NIGHTLY_SWEEP_FRESH_ENV, "120")
    assert resolve_sweep_fresh_seconds(None) == 120.0


# ── check_disk_floors ────────────────────────────────────────────────────


def _floors_env(lake: int, backup: int) -> dict[str, str]:
    return {
        NIGHTLY_MIN_FREE_LAKE_ENV: str(lake),
        NIGHTLY_MIN_FREE_BACKUP_ENV: str(backup),
    }


def test_disk_floors_pass_and_verdict_numbers(tmp_path: Path) -> None:
    lake = tmp_path / "lake"
    backup = tmp_path / "backups"
    lake.mkdir()
    backup.mkdir()
    usage = _usage_map({str(lake): 1000, str(backup): 500})
    verdict = check_disk_floors(
        lake_root=lake,
        backup_root=backup,
        inventory_bytes=100,
        env=_floors_env(1000, 200),
        disk_usage=usage,  # type: ignore[arg-type]
    )
    assert verdict.ok is True
    assert verdict.lake_free == 1000
    assert verdict.lake_floor == 1000  # boundary: free == floor PASSES
    assert verdict.backup_free == 500
    assert verdict.backup_required == 200  # floor > ceil(1.2 × 100) = 120
    assert "disk ok" in verdict.detail


def test_disk_floors_backup_requirement_is_inventory_sized() -> None:
    """ceil(1.2 × inventory) beats a smaller static floor."""
    lake = Path("/")
    usage = _usage_map({"/": 10**10})
    inventory = 1001
    verdict = check_disk_floors(
        lake_root=lake,
        backup_root=Path("/"),
        inventory_bytes=inventory,
        env=_floors_env(0, 10),
        disk_usage=usage,  # type: ignore[arg-type]
    )
    assert verdict.backup_required == math.ceil(NIGHTLY_BACKUP_HEADROOM * inventory) == 1202


def test_disk_floors_one_byte_under_refuses_lake(tmp_path: Path) -> None:
    usage = _usage_map({str(tmp_path): 999})
    verdict = check_disk_floors(
        lake_root=tmp_path,
        backup_root=tmp_path,
        inventory_bytes=0,
        env=_floors_env(1000, 0),
        disk_usage=usage,  # type: ignore[arg-type]
    )
    assert verdict.ok is False
    assert "lake volume" in verdict.detail
    assert "free 999 bytes < required 1000 bytes" in verdict.detail


def test_disk_floors_backup_breach_names_exact_bytes(tmp_path: Path) -> None:
    lake = tmp_path / "lake"
    lake.mkdir()
    backup = tmp_path / "bk"
    backup.mkdir()
    usage = _usage_map({str(lake): 10**9, str(backup): 119})
    verdict = check_disk_floors(
        lake_root=lake,
        backup_root=backup,
        inventory_bytes=100,
        env=_floors_env(0, 0),
        disk_usage=usage,  # type: ignore[arg-type]
    )
    assert verdict.ok is False
    assert verdict.backup_required == 120
    assert "backup volume" in verdict.detail
    assert "free 119 bytes < required 120 bytes" in verdict.detail


def test_disk_floors_both_breached_lists_both(tmp_path: Path) -> None:
    usage = _usage_map({str(tmp_path): 1})
    verdict = check_disk_floors(
        lake_root=tmp_path,
        backup_root=tmp_path,
        inventory_bytes=100,
        env=_floors_env(1000, 1000),
        disk_usage=usage,  # type: ignore[arg-type]
    )
    assert verdict.ok is False
    assert "lake volume" in verdict.detail
    assert "backup volume" in verdict.detail


def test_disk_floors_probe_nearest_existing_ancestor(tmp_path: Path) -> None:
    """A not-yet-created backup root probes its nearest existing ancestor."""
    missing = tmp_path / "not" / "yet" / "created"
    usage = _usage_map({str(tmp_path): 10**9})
    verdict = check_disk_floors(
        lake_root=tmp_path,
        backup_root=missing,
        inventory_bytes=0,
        env=_floors_env(0, 0),
        disk_usage=usage,  # type: ignore[arg-type]
    )
    assert verdict.ok is True


def test_disk_floors_root_fallback_when_nothing_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Total-function guard: even an all-nonexistent chain terminates at root."""
    monkeypatch.setattr(Path, "exists", lambda _self: False)
    usage = _usage_map({"/": 10**9})
    verdict = check_disk_floors(
        lake_root=Path("/nope/a"),
        backup_root=Path("/nope/b"),
        inventory_bytes=0,
        env=_floors_env(0, 0),
        disk_usage=usage,  # type: ignore[arg-type]
    )
    assert verdict.ok is True


def test_disk_floors_junk_env_raises_before_any_probe(tmp_path: Path) -> None:
    def explode(_path: str) -> object:  # pragma: no cover — must never be called
        raise AssertionError("disk_usage must not run on a junk env")

    with pytest.raises(NightlyEnvError):
        check_disk_floors(
            lake_root=tmp_path,
            backup_root=tmp_path,
            inventory_bytes=0,
            env={NIGHTLY_MIN_FREE_LAKE_ENV: "junk"},
            disk_usage=explode,  # type: ignore[arg-type]
        )


# ── probe_sweep_liveness ─────────────────────────────────────────────────


def _write_progress(root: Path, payload: object, *, mtime: float | None = None) -> Path:
    path = root / "progress" / "sweep-progress.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = payload if isinstance(payload, str) else json.dumps(payload)
    path.write_text(text, encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _write_checkpoint(root: Path, *, mtime: float) -> Path:
    path = root / "checkpoint" / "sweep-all.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


_NOW = 1_000_000.0


def test_liveness_no_files_reads_not_live(tmp_path: Path) -> None:
    verdict = probe_sweep_liveness(tmp_path, fresh_seconds=900, now=lambda: _NOW)
    assert verdict.live is False
    assert verdict.heuristic is True
    assert "no fresh sweep signal" in verdict.evidence


def test_liveness_idle_progress_reads_not_live(tmp_path: Path) -> None:
    _write_progress(tmp_path, {"state": "done"}, mtime=_NOW)
    verdict = probe_sweep_liveness(tmp_path, fresh_seconds=900, now=lambda: _NOW)
    assert verdict.live is False


def test_liveness_running_fresh_reads_live_at_boundary(tmp_path: Path) -> None:
    """age == fresh_seconds is still fresh (<= boundary, pinned)."""
    _write_progress(tmp_path, {"state": "running"}, mtime=_NOW - 900)
    verdict = probe_sweep_liveness(tmp_path, fresh_seconds=900, now=lambda: _NOW)
    assert verdict.live is True
    assert "state=running" in verdict.evidence
    assert verdict.heuristic is True


def test_liveness_running_stale_reads_not_live(tmp_path: Path) -> None:
    _write_progress(tmp_path, {"state": "running"}, mtime=_NOW - 901)
    verdict = probe_sweep_liveness(tmp_path, fresh_seconds=900, now=lambda: _NOW)
    assert verdict.live is False


def test_liveness_garbled_progress_degrades_to_checkpoint_signal(tmp_path: Path) -> None:
    _write_progress(tmp_path, "{not json", mtime=_NOW)
    _write_checkpoint(tmp_path, mtime=_NOW - 10)
    verdict = probe_sweep_liveness(tmp_path, fresh_seconds=900, now=lambda: _NOW)
    assert verdict.live is True
    assert "checkpoint" in verdict.evidence


def test_liveness_non_dict_progress_ignored(tmp_path: Path) -> None:
    _write_progress(tmp_path, ["running"], mtime=_NOW)
    verdict = probe_sweep_liveness(tmp_path, fresh_seconds=900, now=lambda: _NOW)
    assert verdict.live is False


def test_liveness_checkpoint_fresh_second_signal(tmp_path: Path) -> None:
    _write_checkpoint(tmp_path, mtime=_NOW - 899)
    verdict = probe_sweep_liveness(tmp_path, fresh_seconds=900, now=lambda: _NOW)
    assert verdict.live is True


def test_liveness_checkpoint_stale_not_live(tmp_path: Path) -> None:
    _write_checkpoint(tmp_path, mtime=_NOW - 10_000)
    verdict = probe_sweep_liveness(tmp_path, fresh_seconds=900, now=lambda: _NOW)
    assert verdict.live is False


def test_liveness_dead_pid_demotes_fresh_running(tmp_path: Path) -> None:
    """Forward-compat arm: a P2-added pid key, dead ⇒ not live."""
    _write_progress(tmp_path, {"state": "running", "pid": 4242}, mtime=_NOW)
    verdict = probe_sweep_liveness(
        tmp_path, fresh_seconds=900, now=lambda: _NOW, pid_alive=lambda _pid: False
    )
    assert verdict.live is False
    assert "pid 4242 is dead" in verdict.evidence


def test_liveness_alive_pid_keeps_running_live(tmp_path: Path) -> None:
    _write_progress(tmp_path, {"state": "running", "pid": 4242}, mtime=_NOW)
    verdict = probe_sweep_liveness(
        tmp_path, fresh_seconds=900, now=lambda: _NOW, pid_alive=lambda _pid: True
    )
    assert verdict.live is True


def test_liveness_non_int_pid_ignored(tmp_path: Path) -> None:
    _write_progress(tmp_path, {"state": "running", "pid": "n/a"}, mtime=_NOW)
    verdict = probe_sweep_liveness(
        tmp_path, fresh_seconds=900, now=lambda: _NOW, pid_alive=lambda _pid: False
    )
    assert verdict.live is True  # junk pid never demotes


def test_liveness_p2_lock_held_is_mechanical_live(tmp_path: Path) -> None:
    """The P2 flock arm: a HELD sweep single-flight lock reads live."""
    lock = tmp_path / ".locks" / "sweep_singleflight.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("")
    fd = os.open(lock, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        verdict = probe_sweep_liveness(tmp_path, fresh_seconds=900, now=lambda: _NOW)
        assert verdict.live is True
        assert "lock HELD" in verdict.evidence
    finally:
        os.close(fd)


def test_liveness_p2_lock_free_falls_through_to_mtime_signals(tmp_path: Path) -> None:
    lock = tmp_path / ".locks" / "sweep_singleflight.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("")
    verdict = probe_sweep_liveness(tmp_path, fresh_seconds=900, now=lambda: _NOW)
    assert verdict.live is False  # acquirable lock + no other signal
    # And the mtime signals still work with a free lock present.
    _write_progress(tmp_path, {"state": "running"}, mtime=_NOW)
    verdict = probe_sweep_liveness(tmp_path, fresh_seconds=900, now=lambda: _NOW)
    assert verdict.live is True


def test_liveness_p2_lock_unreadable_falls_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = tmp_path / ".locks" / "sweep_singleflight.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("")
    real_open = os.open

    def flaky_open(path: object, flags: int, *args: object) -> int:
        if str(path) == str(lock):
            raise OSError(errno.EACCES, "unreadable lock")
        return real_open(path, flags, *args)  # type: ignore[arg-type]

    monkeypatch.setattr("brutex.nightly.probes.os.open", flaky_open)
    verdict = probe_sweep_liveness(tmp_path, fresh_seconds=900, now=lambda: _NOW)
    assert verdict.live is False


def test_default_pid_alive_arms(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _default_pid_alive(os.getpid()) is True

    # ESRCH — no such process.
    def esrch(_pid: int, _sig: int) -> None:
        raise OSError(errno.ESRCH, "no such process")

    monkeypatch.setattr("brutex.nightly.probes.os.kill", esrch)
    assert _default_pid_alive(1234) is False

    # EPERM — exists, owned by another user: alive.
    def eperm(_pid: int, _sig: int) -> None:
        raise OSError(errno.EPERM, "not permitted")

    monkeypatch.setattr("brutex.nightly.probes.os.kill", eperm)
    assert _default_pid_alive(1234) is True


def test_liveness_default_pid_checker_used_when_none(tmp_path: Path) -> None:
    """pid_alive=None uses the real checker — our own live pid keeps it live."""
    _write_progress(tmp_path, {"state": "running", "pid": os.getpid()}, mtime=_NOW)
    verdict = probe_sweep_liveness(tmp_path, fresh_seconds=900, now=lambda: _NOW)
    assert verdict.live is True
