"""Backup preflight + include/exclude plan + locks (DEC-BACKUP-OFFBOX-001).

Preflight is the gate that runs BEFORE any restic verb (the F1 ordering pin):
a phantom ``/Volumes/<name>`` dir (macOS leaves the dead mountpoint writable)
or an empty backup set must never reach ``restic backup`` — and critically
never ``forget --prune``, the one path by which the backup system itself could
age REAL snapshots out of the repo.

The include stance is fail-safe WHOLE-ROOT (graft A): the plan backs up
``<lake_root>`` itself (a NEW durable artifact is included by default, never
silently missed — ``.quarantine/`` heal copies deliberately INCLUDED per
DEC-LAKE-HEAL-001 #1175) plus ``~/.brutex/audit``; excludes name only the
provably transient (``.locks``/``.rate``/atomic-writer temps) + the operator's
``BRUTEX_BACKUP_EXTRA_EXCLUDES``.

Two locks, two roles (both non-blocking ``flock`` — the
``_flock_singleflight`` shape, kernel-released on process death):

* :func:`backup_singleflight_lock` — the backup's OWN overlap guard (F15;
  contention → :class:`~brutex.backup.errors.BackupBusyError`, no alert).
* :func:`pull_lock_held` — a PROBE (acquire-and-release, never held) of the
  pull's single-flight lock, tagging the snapshot ``pull-active`` vs
  ``pull-quiet`` (F16; the backup never takes the pull lock — nightly backup
  must not block the nightly pull and vice versa).
"""

from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import structlog

from brutex.adapters.groww.pull_singleflight import pull_singleflight_lock_path
from brutex.backup.errors import BackupBusyError
from brutex.config.paths import LakeRootIdentity, brutex_home, lake_root_identity

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from pathlib import Path

    from brutex.config.backup_env import BackupEnvConfig

_LOG = structlog.get_logger(__name__)

#: Directory NAMES excluded from every snapshot — provably kernel-/telemetry-
#: transient ONLY (partition/pull flocks; the live rate-bridge JSON).
EXCLUDE_DIR_NAMES: Final[tuple[str, ...]] = (".locks", ".rate")

#: Glob patterns excluded from every snapshot — the atomic-writer temp prefix
#: (:mod:`brutex.data.atomic_io`): mid-write garbage by definition.
EXCLUDE_GLOBS: Final[tuple[str, ...]] = ("*.tmp", ".brutex-write-*")

#: The lake-root members at least ONE of which must exist for a non-empty
#: backup set (F2) — bars, the durable window ledger, sweeps, audit.
_BACKUP_SET_MEMBERS: Final[tuple[str, ...]] = ("bars", "sweeps", "audit")

#: The durable window-ledger Parquet relative to the lake root. Literal copy
#: of ``window_ledger_store.LEDGER_DIRNAME / LEDGER_FILENAME`` (:90-91) —
#: deliberately NOT imported: the backup package's import pin (§3) admits only
#: ``brutex.config`` / ``brutex.data.atomic_io`` / ``brutex.notifications`` /
#: the pull-singleflight lock-path helper, and ``window_ledger_store`` pulls
#: the whole ledger chain.
_LEDGER_RELPATH: Final[str] = "window_ledger/ledger.parquet"


@dataclass(frozen=True, slots=True)
class PreflightVerdict:
    """The F1/F2 preflight outcome for one lake root.

    Attributes
    ----------
    ok
        ``True`` iff the lake root is safe to snapshot.
    reason
        ``""`` on ok, else ``"lake_missing"`` | ``"lake_unmounted"`` |
        ``"backup_set_empty"``.
    identity
        The COMMON :class:`~brutex.config.paths.LakeRootIdentity` facts
        (mount state + device — the F1 drift signal).
    """

    ok: bool
    reason: str
    identity: LakeRootIdentity


@dataclass(frozen=True, slots=True)
class BackupPlan:
    """The rendered include/exclude/tag plan for one ``restic backup`` call.

    Attributes
    ----------
    lake_root
        The lake root being snapshotted.
    paths
        Whole-root includes: ``(lake_root[, ~/.brutex/audit if it exists])``.
    excludes
        :data:`EXCLUDE_DIR_NAMES` + :data:`EXCLUDE_GLOBS` + the operator's
        extra excludes + ``str(state_dir)`` IFF the state dir sits under the
        lake root (the backup must not recurse into its own state/canary/
        drill leftovers).
    tags
        ``("brutex", "pull-active" | "pull-quiet")`` (F16 consistency tag).
    pull_active
        Whether the pull single-flight lock was held at plan time.
    """

    lake_root: Path
    paths: tuple[Path, ...]
    excludes: tuple[str, ...]
    tags: tuple[str, ...]
    pull_active: bool


def preflight_lake(
    lake_root: Path,
    *,
    allow_empty: bool = False,
    identity_fn: Callable[[Path], LakeRootIdentity] = lake_root_identity,
) -> PreflightVerdict:
    """Gate the lake root BEFORE any restic verb (F1 phantom-mount + F2 empty set).

    Parameters
    ----------
    lake_root
        The resolved lake root to snapshot.
    allow_empty
        Explicit first-install escape for a genuinely brand-new box — logged
        loudly (``backup.preflight.allow_empty``), never the default.
    identity_fn
        Injectable identity probe (tests drive every branch without a real
        volume); defaults to the COMMON
        :func:`brutex.config.paths.lake_root_identity`.

    Returns
    -------
    PreflightVerdict
        ``ok=False`` with a deterministic reason on refusal; the identity
        facts either way (the caller logs device drift).
    """
    identity = identity_fn(lake_root)
    if identity.volume_parent is not None and identity.volume_mounted is False:
        return PreflightVerdict(ok=False, reason="lake_unmounted", identity=identity)
    if not identity.exists:
        return PreflightVerdict(ok=False, reason="lake_missing", identity=identity)
    has_member = (
        any((lake_root / member).exists() for member in _BACKUP_SET_MEMBERS)
        or (lake_root / _LEDGER_RELPATH).exists()
    )
    if not has_member:
        if allow_empty:
            _LOG.warning("backup.preflight.allow_empty", lake_root=str(lake_root))
            return PreflightVerdict(ok=True, reason="", identity=identity)
        return PreflightVerdict(ok=False, reason="backup_set_empty", identity=identity)
    return PreflightVerdict(ok=True, reason="", identity=identity)


def pull_lock_held(lake_root: Path) -> bool:
    """Probe (never hold) the pull single-flight lock — the F16 consistency tag.

    A non-blocking acquire-and-release ``flock`` probe of
    :func:`brutex.adapters.groww.pull_singleflight.pull_singleflight_lock_path`.
    A missing lock file means no pull ever ran against this lake → not held.
    A probe error is logged and read as "not held" — the tag is informational
    only, never control flow.
    """
    path = pull_singleflight_lock_path(lake_root)
    try:
        # The exists() probe sits INSIDE the guard: pathlib re-raises
        # EACCES-class OSErrors (only ENOENT/ENOTDIR/EBADF/ELOOP read False),
        # and the docstring's "probe error reads not held" must hold for it.
        if not path.exists():
            return False
        fd = os.open(path, os.O_RDWR)
    except OSError as exc:
        _LOG.warning("backup.plan.pull_probe_failed", path=str(path), error=str(exc))
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def build_backup_plan(
    cfg: BackupEnvConfig,
    lake_root: Path,
    *,
    home: Path | None = None,
    pull_probe: Callable[[Path], bool] = pull_lock_held,
) -> BackupPlan:
    """Render the include/exclude/tag plan (§5.3 — pure over the injected probe).

    Parameters
    ----------
    cfg
        The resolved backup config (extra excludes + state dir).
    lake_root
        The preflighted lake root.
    home
        Home override for the ``~/.brutex/audit`` include (tests inject tmp).
    pull_probe
        Injectable pull-lock probe (defaults to :func:`pull_lock_held`).

    Returns
    -------
    BackupPlan
        The exact paths/excludes/tags handed to ``restic backup``.
    """
    paths: list[Path] = [lake_root]
    audit_dir = brutex_home(home=home) / "audit"
    if audit_dir.exists():
        paths.append(audit_dir)
    excludes: list[str] = [*EXCLUDE_DIR_NAMES, *EXCLUDE_GLOBS, *cfg.extra_excludes]
    # ~-expansion is OWNED by resolve_backup_env (total — an unexpandable
    # ~user value arrives raw by design); re-expanding here could raise.
    state_dir = cfg.state_dir
    if state_dir.is_relative_to(lake_root):
        excludes.append(str(state_dir))
    pull_active = pull_probe(lake_root)
    tags = ("brutex", "pull-active" if pull_active else "pull-quiet")
    return BackupPlan(
        lake_root=lake_root,
        paths=tuple(paths),
        excludes=tuple(excludes),
        tags=tags,
        pull_active=pull_active,
    )


@contextmanager
def backup_singleflight_lock(state_dir: Path) -> Generator[None, None, None]:
    """Hold the backup's OWN single-flight ``flock`` for the ``with`` block (F15).

    ``LOCK_EX | LOCK_NB`` on ``<state_dir>/backup.lock`` — the
    ``_flock_singleflight`` shape (:mod:`brutex.adapters.groww.pull_singleflight`):
    created 0600, contention raises :class:`BackupBusyError` immediately (a
    multi-day first seed must span launchd fire times without overlap), and
    the kernel releases the lock on fd-close/process death — no stale-lock
    code needed.

    Raises
    ------
    BackupBusyError
        Another backup run holds the lock (benign — no alert; exit 5).
    OSError
        Lock dir/file creation failed (e.g. ENOSPC) — the caller maps it.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(state_dir / "backup.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BackupBusyError(f"another backup run holds {state_dir / 'backup.lock'}") from exc
        yield
    finally:
        os.close(fd)


__all__ = [
    "EXCLUDE_DIR_NAMES",
    "EXCLUDE_GLOBS",
    "BackupPlan",
    "PreflightVerdict",
    "backup_singleflight_lock",
    "build_backup_plan",
    "preflight_lake",
    "pull_lock_held",
]
