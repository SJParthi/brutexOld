"""Total-wipe core — enumerate every BruteX persistent path, then delete it SAFELY.

Why this exists: ALL BruteX state lives under ``$HOME/.brutex`` (plus any
``BRUTEX_*`` override pointing elsewhere), NOT inside the repo — so deleting the
project folder leaves the lake, credentials, token, and audit log behind
(operator-reported, 2026-05-30). This is the pure, 100%-tested core behind
``brutex wipe`` / ``make wipe``:

* :func:`wipe_targets` ENUMERATES the paths (the home dir + the in-repo
  ``./.brutex-logs`` + any override resolving outside the home dir), from ONE
  override registry so a new ``BRUTEX_*`` location can't be silently forgotten.
* :func:`assert_safe` is a strict resolved-path ALLOWLIST (irreversible op →
  default-deny): it REFUSES symlinks, the filesystem root, ``$HOME`` and any
  ancestor of it, the repo/cwd and any ancestor of it, and anything too shallow
  — so a mis-set env var or a ``~/.brutex`` symlink can never turn "wipe my
  data" into ``rm -rf $HOME``.
* :func:`execute_wipe` deletes the survivors, continuing past per-target
  failures (a partial failure is reported, never hidden).

Pure + injectable: the filesystem primitives (``rmtree`` / ``unlink`` /
existence + dir probes) are injected, so every branch — including the
symlink-refusal and partial-failure paths — is unit-reachable.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from brutex.config.paths import ENV_LAKE_ROOT, brutex_home
from brutex.models import BruteXError

#: ``BRUTEX_*`` env vars whose target MAY live outside ``$HOME/.brutex`` and so
#: must be wiped explicitly when set. ONE registry so a new override can't be
#: silently forgotten by the wiper (CLAUDE.md §11 COMMON). Order is stable.
OVERRIDE_ENV_VARS: Final[tuple[str, ...]] = (
    ENV_LAKE_ROOT,  # lake root DIR (holds .duckdb + .wal + .tmp)
    "BRUTEX_CREDENTIALS_FILE",  # saved Groww API key + TOTP secret
    "BRUTEX_GROWW_TOKEN_PATH",  # cached Groww access token
    "BRUTEX_LOG_DIR",  # rotating run-log dir (make run → ./.brutex-logs)
    "BRUTEX_LOG_FILE",  # explicit run-log file
    "BRUTEX_RUNTIME_FILE",  # service-discovery runtime.json
    "BRUTEX_AUDIT_PATH",  # hash-chained audit log (Golden Rule #8)
    "BRUTEX_LIVE_SUBJECTS_FILE",  # live-subjects pointer
)

#: In-repo run-log dir ``make run`` writes to by default (Makefile BRUTEX_LOG_DIR).
_REPO_LOG_DIR_NAME: Final[str] = ".brutex-logs"

#: A target must have at least this many components below the filesystem anchor
#: to be wipeable — refuses ``/`` and top-level dirs like ``/data`` (too broad
#: to be a BruteX-owned leaf). ``$HOME/.brutex`` clears it on every real OS.
_MIN_DEPTH_BELOW_ANCHOR: Final[int] = 2


class UnsafeWipeError(BruteXError):
    """A wipe target failed the safety allowlist — refused, never deleted."""


@dataclass(frozen=True, slots=True)
class WipeTarget:
    """One path the wiper will attempt to remove, with a human label."""

    path: Path
    label: str


@dataclass(frozen=True, slots=True)
class WipeReport:
    """Outcome of an :func:`execute_wipe` (or its dry-run preview)."""

    removed: tuple[Path, ...]  # deleted (or, in dry-run, WOULD be deleted)
    absent: tuple[Path, ...]  # nothing there to delete
    failed: tuple[tuple[Path, str], ...]  # refused-unsafe or a delete error
    dry_run: bool


def _default_rmtree(path: Path) -> None:
    """Recursively delete a directory (production :func:`shutil.rmtree`)."""
    shutil.rmtree(path)


def _default_unlink(path: Path) -> None:
    """Delete a single file (production ``Path.unlink``)."""
    path.unlink()


def _default_exists(path: Path) -> bool:
    """True if *path* exists on disk (production ``Path.exists``)."""
    return path.exists()


def _default_is_dir(path: Path) -> bool:
    """True if *path* is a directory (production ``Path.is_dir``)."""
    return path.is_dir()


def _is_within(path: Path, parent: Path) -> bool:
    """True if *path* IS *parent* or is nested under it (resolved real paths)."""
    resolved = path.resolve(strict=False)
    base = parent.resolve(strict=False)
    return resolved == base or base in resolved.parents


def wipe_targets(
    env: Mapping[str, str], *, home: Path, cwd: Path | None = None
) -> list[WipeTarget]:
    """Enumerate every BruteX persistent path to wipe (stable order, deduped).

    Always includes ``$HOME/.brutex`` (where all default state lives), plus the
    in-repo ``./.brutex-logs`` when *cwd* is given (``make run``'s default log
    dir), plus any :data:`OVERRIDE_ENV_VARS` whose target resolves OUTSIDE
    ``$HOME/.brutex`` — an override INSIDE the home dir is already covered by
    removing it, so it is skipped to avoid a redundant target.

    Parameters
    ----------
    env
        Environment mapping (typically :data:`os.environ`).
    home
        The user's home directory.
    cwd
        The current working directory (the repo). When given, the in-repo
        ``./.brutex-logs`` is added as a candidate target.

    Returns
    -------
    list[WipeTarget]
        Targets in a stable order: home first, then in-repo logs, then each
        outside-home override in registry order.
    """
    base = brutex_home(home=home)
    targets: list[WipeTarget] = [
        WipeTarget(base, "BruteX home (lake · credentials · token · audit · runtime)")
    ]
    seen: set[Path] = {base.resolve(strict=False)}

    def _add(path: Path, label: str) -> None:
        if _is_within(path, base):
            return  # removing `base` already covers it
        key = path.resolve(strict=False)
        if key in seen:
            return
        seen.add(key)
        targets.append(WipeTarget(path, label))

    if cwd is not None:
        _add(cwd / _REPO_LOG_DIR_NAME, "in-repo run logs (make run)")
    for env_var in OVERRIDE_ENV_VARS:
        raw = env.get(env_var)
        if raw:
            _add(Path(raw).expanduser(), env_var)
    return targets


def assert_safe(path: Path, *, home: Path, cwd: Path) -> None:
    """Raise :class:`UnsafeWipeError` unless *path* is safe to delete.

    Strict allowlist on the RESOLVED real path (irreversible op → default-deny):
    refuses a symlinked target, anything shallower than
    :data:`_MIN_DEPTH_BELOW_ANCHOR` below its mount (the filesystem root +
    top-level dirs), ``$HOME`` and any ancestor of it, and the working
    directory / repo and any ancestor of it. Anything surviving is a deep,
    non-system, BruteX-owned leaf.

    Parameters
    ----------
    path
        Candidate target to validate.
    home
        The user's home directory (refused, with its ancestors).
    cwd
        The working directory / repo (refused, with its ancestors).

    Raises
    ------
    UnsafeWipeError
        If *path* is a symlink or resolves to a protected location.
    """
    if path.is_symlink():
        raise UnsafeWipeError(
            f"refusing to wipe a symlink: {path}",
            safe_message=f"refused unsafe wipe target (symlink): {path}",
        )
    resolved = path.resolve(strict=False)
    home_resolved = home.resolve(strict=False)
    cwd_resolved = cwd.resolve(strict=False)
    anchor = Path(resolved.anchor)
    below_anchor = resolved.parts[len(anchor.parts) :]

    if len(below_anchor) < _MIN_DEPTH_BELOW_ANCHOR:
        reason = "filesystem root or too shallow"
    elif resolved == home_resolved or resolved in home_resolved.parents:
        reason = "home directory or an ancestor of it"
    elif resolved == cwd_resolved or resolved in cwd_resolved.parents:
        reason = "the working directory / repo or an ancestor of it"
    else:
        return
    raise UnsafeWipeError(
        f"refusing to wipe {path} ({reason})",
        safe_message=f"refused unsafe wipe target ({reason}): {path}",
    )


def execute_wipe(
    targets: Sequence[WipeTarget],
    *,
    home: Path,
    cwd: Path,
    dry_run: bool,
    rmtree: Callable[[Path], None] = _default_rmtree,
    unlink: Callable[[Path], None] = _default_unlink,
    exists: Callable[[Path], bool] = _default_exists,
    is_dir: Callable[[Path], bool] = _default_is_dir,
) -> WipeReport:
    """Delete each safe, existing *target*; continue past per-target failures.

    Every target is re-validated through :func:`assert_safe` immediately before
    deletion (defence in depth — the enumerator and the executor agree on the
    guard). A refused target or a delete error is recorded in
    :attr:`WipeReport.failed` and the wipe continues, so one un-deletable path
    never aborts the whole wipe. In *dry_run* mode nothing is deleted; existing
    targets are reported under ``removed`` ("would remove").

    Parameters
    ----------
    targets
        The paths to attempt (typically from :func:`wipe_targets`).
    home, cwd
        Passed to :func:`assert_safe` for the safety allowlist.
    dry_run
        When ``True``, report what WOULD be removed without deleting anything.
    rmtree, unlink, exists, is_dir
        Injected filesystem primitives (defaults call the real OS); tests
        inject fakes to exercise the failure branch without un-deletable files.

    Returns
    -------
    WipeReport
        Removed (or would-remove), absent, and failed targets.
    """
    removed: list[Path] = []
    absent: list[Path] = []
    failed: list[tuple[Path, str]] = []
    for target in targets:
        path = target.path
        try:
            assert_safe(path, home=home, cwd=cwd)
        except UnsafeWipeError as exc:
            failed.append((path, str(exc)))
            continue
        if not exists(path):
            absent.append(path)
            continue
        if dry_run:
            removed.append(path)
            continue
        try:
            if is_dir(path):
                rmtree(path)
            else:
                unlink(path)
        except OSError as exc:
            failed.append((path, str(exc)))
            continue
        removed.append(path)
    return WipeReport(tuple(removed), tuple(absent), tuple(failed), dry_run)


def server_is_running(pid: int | None, *, pid_alive: Callable[[int], bool]) -> bool:
    """True if a live BruteX dashboard owns the lake (so a CLI wipe must refuse).

    Parameters
    ----------
    pid
        The OS pid recorded in the service-discovery runtime file, or ``None``
        when no runtime file exists (no server was started).
    pid_alive
        Liveness probe for an OS pid (production: ``os.kill(pid, 0)``).

    Returns
    -------
    bool
        ``True`` only when a pid is present AND alive.
    """
    return pid is not None and pid_alive(pid)


__all__ = [
    "OVERRIDE_ENV_VARS",
    "UnsafeWipeError",
    "WipeReport",
    "WipeTarget",
    "assert_safe",
    "execute_wipe",
    "server_is_running",
    "wipe_targets",
]
