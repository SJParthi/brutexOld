"""Unit tests for :mod:`brutex.backup.plan` (DEC-BACKUP-OFFBOX-001 §5.3).

Preflight (F1 phantom-mount / F2 empty-set via injected ``identity_fn``), the
fail-safe whole-root include/exclude plan, the non-blocking pull-lock probe
(F16 — REAL flock held from the test), and the backup single-flight lock
(F15 — REAL flock contention → :class:`BackupBusyError`).
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path

import pytest
import structlog.testing
from brutex.adapters.groww.pull_singleflight import pull_singleflight_lock_path
from brutex.backup.errors import BackupBusyError
from brutex.backup.plan import (
    EXCLUDE_DIR_NAMES,
    EXCLUDE_GLOBS,
    backup_singleflight_lock,
    build_backup_plan,
    preflight_lake,
    pull_lock_held,
)
from brutex.config.backup_env import BackupEnvConfig, resolve_backup_env
from brutex.config.paths import LakeRootIdentity


def _identity(
    lake_root: Path,
    *,
    exists: bool = True,
    volume_parent: Path | None = None,
    volume_mounted: bool | None = None,
    device: int | None = 111,
) -> LakeRootIdentity:
    """Build one injected identity verdict."""
    return LakeRootIdentity(
        path=lake_root,
        exists=exists,
        volume_parent=volume_parent,
        volume_mounted=volume_mounted,
        device=device,
    )


def _cfg(tmp_path: Path, **env: str) -> BackupEnvConfig:
    """Resolve a config with the state dir under *tmp_path* by default."""
    env.setdefault("BRUTEX_BACKUP_STATE_DIR", str(tmp_path / "backup-state"))
    return resolve_backup_env(env, home=tmp_path)


class TestPreflight:
    def test_preflight_ok_mounted_volume(self, tmp_path: Path) -> None:
        """A mounted /Volumes lake with members passes (F1 ok branch)."""
        lake = tmp_path / "lake"
        (lake / "bars").mkdir(parents=True)
        verdict = preflight_lake(
            lake,
            identity_fn=lambda p: _identity(
                p, volume_parent=Path("/Volumes/T7"), volume_mounted=True
            ),
        )
        assert verdict.ok is True
        assert verdict.reason == ""

    def test_preflight_refuses_unmounted_volume(self, tmp_path: Path) -> None:
        """The phantom /Volumes dir is REFUSED even when members exist (F1)."""
        lake = tmp_path / "lake"
        (lake / "bars").mkdir(parents=True)
        verdict = preflight_lake(
            lake,
            identity_fn=lambda p: _identity(
                p, volume_parent=Path("/Volumes/T7"), volume_mounted=False
            ),
        )
        assert verdict.ok is False
        assert verdict.reason == "lake_unmounted"

    def test_preflight_abstains_non_volume_root(self, tmp_path: Path) -> None:
        """No volume parent → no mount hazard to judge (the third identity branch)."""
        lake = tmp_path / "lake"
        (lake / "sweeps").mkdir(parents=True)
        verdict = preflight_lake(lake, identity_fn=_identity)
        assert verdict.ok is True

    def test_preflight_lake_missing(self, tmp_path: Path) -> None:
        verdict = preflight_lake(
            tmp_path / "absent", identity_fn=lambda p: _identity(p, exists=False)
        )
        assert verdict.ok is False
        assert verdict.reason == "lake_missing"

    def test_preflight_empty_set_refused(self, tmp_path: Path) -> None:
        """A present-but-empty lake root is refused (F2 — empty worse than none)."""
        lake = tmp_path / "lake"
        lake.mkdir()
        verdict = preflight_lake(lake, identity_fn=_identity)
        assert verdict.ok is False
        assert verdict.reason == "backup_set_empty"

    @pytest.mark.parametrize("member", ["bars", "sweeps", "audit", "window_ledger/ledger.parquet"])
    def test_preflight_each_member_alone_passes(self, tmp_path: Path, member: str) -> None:
        """ANY one of bars/sweeps/audit/the ledger parquet makes the set non-empty."""
        lake = tmp_path / "lake"
        target = lake / member
        if member.endswith(".parquet"):
            target.parent.mkdir(parents=True)
            target.write_bytes(b"PAR1")
        else:
            target.mkdir(parents=True)
        verdict = preflight_lake(lake, identity_fn=_identity)
        assert verdict.ok is True

    def test_allow_empty_escape_logged(self, tmp_path: Path) -> None:
        """--allow-empty proceeds on an empty set with ONE loud WARN (F2 escape)."""
        lake = tmp_path / "lake"
        lake.mkdir()
        with structlog.testing.capture_logs() as logs:
            verdict = preflight_lake(lake, allow_empty=True, identity_fn=_identity)
        assert verdict.ok is True
        assert [entry["event"] for entry in logs] == ["backup.preflight.allow_empty"]

    def test_allow_empty_irrelevant_when_members_exist(self, tmp_path: Path) -> None:
        """allow_empty on a NON-empty lake takes the normal ok path (no WARN)."""
        lake = tmp_path / "lake"
        (lake / "audit").mkdir(parents=True)
        with structlog.testing.capture_logs() as logs:
            verdict = preflight_lake(lake, allow_empty=True, identity_fn=_identity)
        assert verdict.ok is True
        assert logs == []


class TestBuildBackupPlan:
    def test_exclude_rendering_golden(self, tmp_path: Path) -> None:
        """Fixed dirs + globs + extra + the state-dir-under-lake auto-exclude, in order."""
        lake = tmp_path / "lake"
        lake.mkdir()
        state_dir = lake / ".backup-state"
        cfg = _cfg(tmp_path, BRUTEX_BACKUP_STATE_DIR=str(state_dir))
        plan = build_backup_plan(cfg, lake, home=tmp_path, pull_probe=lambda p: False)
        assert plan.excludes == (
            ".locks",
            ".rate",
            "*.tmp",
            ".brutex-write-*",
            "logs",
            "dedup-cache",
            str(state_dir),
        )
        assert plan.paths == (lake,)
        assert plan.tags == ("brutex", "pull-quiet")
        assert plan.pull_active is False
        assert plan.lake_root == lake

    def test_state_dir_outside_lake_not_excluded(self, tmp_path: Path) -> None:
        """The guard's other branch: an outside state dir is NOT auto-excluded."""
        lake = tmp_path / "lake"
        lake.mkdir()
        cfg = _cfg(tmp_path)
        plan = build_backup_plan(cfg, lake, home=tmp_path, pull_probe=lambda p: False)
        assert str(cfg.state_dir) not in plan.excludes
        assert plan.excludes == (*EXCLUDE_DIR_NAMES, *EXCLUDE_GLOBS, "logs", "dedup-cache")

    def test_include_paths_audit_dir_only_if_exists(self, tmp_path: Path) -> None:
        """~/.brutex/audit joins the include set IFF it exists."""
        lake = tmp_path / "lake"
        lake.mkdir()
        cfg = _cfg(tmp_path)
        audit = tmp_path / ".brutex" / "audit"
        plan = build_backup_plan(cfg, lake, home=tmp_path, pull_probe=lambda p: False)
        assert plan.paths == (lake,)
        audit.mkdir(parents=True)
        plan2 = build_backup_plan(cfg, lake, home=tmp_path, pull_probe=lambda p: False)
        assert plan2.paths == (lake, audit)

    def test_pull_active_tag(self, tmp_path: Path) -> None:
        """A held pull lock tags the snapshot pull-active (F16)."""
        lake = tmp_path / "lake"
        lake.mkdir()
        plan = build_backup_plan(_cfg(tmp_path), lake, home=tmp_path, pull_probe=lambda p: True)
        assert plan.tags == ("brutex", "pull-active")
        assert plan.pull_active is True


class TestPullLockProbe:
    def test_pull_lock_probe_held_and_free(self, tmp_path: Path) -> None:
        """REAL flock: held from the test → True; released → False (F16)."""
        lake = tmp_path / "lake"
        lock_file = pull_singleflight_lock_path(lake)
        lock_file.parent.mkdir(parents=True)
        fd = os.open(lock_file, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            assert pull_lock_held(lake) is True
        finally:
            os.close(fd)  # kernel releases the flock
        assert pull_lock_held(lake) is False

    def test_pull_lock_missing_file_not_held(self, tmp_path: Path) -> None:
        """No lock file = no pull ever ran → not held."""
        assert pull_lock_held(tmp_path / "fresh-lake") is False

    def test_pull_lock_exists_probe_error_reads_not_held(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An EACCES-class error from the exists() PROBE itself (pathlib re-raises
        it — only ENOENT/ENOTDIR/EBADF/ELOOP read False) logs + reads not-held —
        round-1 review pin: the probe error guard covers its OWN exists() call."""

        class _ExistsRaises(Path):
            def exists(self, *, follow_symlinks: bool = True) -> bool:
                raise PermissionError(13, "probe denied")

        monkeypatch.setattr(
            "brutex.backup.plan.pull_singleflight_lock_path",
            lambda root: _ExistsRaises(root, "pull.lock"),
        )
        with structlog.testing.capture_logs() as logs:
            assert pull_lock_held(tmp_path / "lake") is False
        assert [entry["event"] for entry in logs] == ["backup.plan.pull_probe_failed"]

    def test_pull_lock_probe_error_reads_not_held(self, tmp_path: Path) -> None:
        """A probe OSError (directory at the lock path) logs + reads not-held."""
        lake = tmp_path / "lake"
        pull_singleflight_lock_path(lake).mkdir(parents=True)
        with structlog.testing.capture_logs() as logs:
            assert pull_lock_held(lake) is False
        assert [entry["event"] for entry in logs] == ["backup.plan.pull_probe_failed"]


class TestSingleflight:
    def test_singleflight_busy_raises(self, tmp_path: Path) -> None:
        """A second acquire while held → BackupBusyError (F15)."""
        state_dir = tmp_path / "state"
        with (
            backup_singleflight_lock(state_dir),
            pytest.raises(BackupBusyError, match="another backup run holds"),
            backup_singleflight_lock(state_dir),
        ):
            pytest.fail("second acquire must not enter the with block")

    def test_singleflight_releases_on_exit(self, tmp_path: Path) -> None:
        """After a clean exit the lock is re-acquirable."""
        state_dir = tmp_path / "state"
        with backup_singleflight_lock(state_dir):
            pass
        with backup_singleflight_lock(state_dir):
            assert (state_dir / "backup.lock").exists()

    def test_singleflight_releases_on_error(self, tmp_path: Path) -> None:
        """An exception inside the block still releases (finally-close)."""
        state_dir = tmp_path / "state"
        with pytest.raises(RuntimeError, match="boom"), backup_singleflight_lock(state_dir):
            raise RuntimeError("boom")
        with backup_singleflight_lock(state_dir):
            pass
