"""Canonical BruteX filesystem-path resolvers — one source of truth (CLAUDE.md §11 COMMON).

Every BruteX surface that needs the on-disk home or the lake root resolves it
HERE, so the rule lives in exactly one place (the CLI, the wiper, and any
future caller all delegate). Pure path computation — ``$HOME`` and the
``BRUTEX_*`` environment are injected, no directory is created, and nothing on
disk is touched — so the module imports and unit-tests with zero side effects.

Rust seam (``DEC-CONFIG-RUST-LEAVES-001``, MIGRATION.md #1943 — slice T35
milestone 3): when — and ONLY when — ``BRUTEX_CORE_RUST_PATHS`` is set
truthy, the lake-root decision + the volume-parent derivation route
through the default-ON, burn-in-checked, fail-closed
:mod:`brutex.config.paths_rust` facade to the ``rust/brutex-core`` crate
(strings only cross the FFI). The flag-OFF default is the verbatim
``_*_python`` oracle below — behavior-identical AND crate-untouched
(bomb-crate spy-pinned). Honesty banner (ADR-0020, mandatory verbatim):
expected payoff ≈ 0 — conversion is for uniformity, not speed.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from brutex.config.paths_rust import paths_rust_enabled, route_lake_root, route_volume_parent

#: Env var overriding the lake root (a directory). See ``CLAUDE.md §6``.
ENV_LAKE_ROOT: Final[str] = "BRUTEX_LAKE_ROOT"

#: Directory name, under the user's home, that holds ALL default BruteX state.
_HOME_DIR_NAME: Final[str] = ".brutex"


def _home(home: Path | None) -> Path:
    """Return the user's home directory (injected *home* wins, else ``Path.home()``)."""
    return home if home is not None else Path.home()


def brutex_home(*, home: Path | None = None) -> Path:
    """Return the BruteX home directory — ``$HOME/.brutex``.

    All default state (lake, credentials, token, audit, runtime, logs) lives
    under this one directory, deliberately OUTSIDE any repo checkout so an
    expensive ingest survives a code update or a fresh ``git clone``.

    Parameters
    ----------
    home
        Override for the user's home directory (tests inject a tmp path).

    Returns
    -------
    Path
        ``home/".brutex"`` (not created).
    """
    return _home(home) / _HOME_DIR_NAME


def resolve_lake_root(env: Mapping[str, str] | None = None, *, home: Path | None = None) -> Path:
    """Resolve the lake root — ``$BRUTEX_LAKE_ROOT`` → ``$HOME/.brutex/lake``.

    Parameters
    ----------
    env
        Environment mapping (defaults to :data:`os.environ`).
    home
        Override for the user's home directory (tests inject a tmp path).

    Returns
    -------
    Path
        The resolved lake-root directory (``~``-expanded; not created).
    """
    environ = os.environ if env is None else env
    override = environ.get(ENV_LAKE_ROOT)
    if not paths_rust_enabled():
        return _resolve_lake_root_python(override, home)
    return route_lake_root(
        override,
        lambda: _home(home),
        lambda: _resolve_lake_root_python(override, home),
    )


def _resolve_lake_root_python(override: str | None, home: Path | None) -> Path:
    """The verbatim :func:`resolve_lake_root` decision body — the LIVE oracle.

    Token-for-token the pre-seam public body (DEC-CONFIG-RUST-LEAVES-001);
    every flag-OFF / ineligible / degraded / burn-in call serves this.
    """
    if override:
        return Path(override).expanduser()
    return brutex_home(home=home) / "lake"


#: The macOS external-volume mount prefix — a lake root under ``/Volumes/<name>/…``
#: silently falls back to the INTERNAL disk when the volume is unmounted (macOS
#: leaves the dead ``/Volumes/<name>`` directory writable), so the identity check
#: below verifies the volume parent is a real mountpoint (register row G1).
_VOLUMES_PREFIX: Final[str] = "/Volumes"


@dataclass(frozen=True, slots=True)
class LakeRootIdentity:
    """The resolved lake root plus the on-disk identity facts a pull should log (G1).

    Attributes
    ----------
    path
        The resolved lake-root directory (not created).
    exists
        ``True`` iff *path* currently exists on disk.
    volume_parent
        For a lake root under ``/Volumes/<name>/…`` the ``/Volumes/<name>``
        directory whose mount state decides whether writes hit the external
        volume or silently rebuild on the internal disk; ``None`` for a
        non-``/Volumes`` path (no external-volume hazard).
    volume_mounted
        ``os.path.ismount`` verdict for :attr:`volume_parent` (``None`` when
        there is no volume parent — nothing to verify).
    device
        ``st_dev`` of the nearest EXISTING ancestor of *path* (``None`` when
        even the filesystem root stat fails) — two surfaces resolving the same
        textual path on different devices is the split-brain signature.
    """

    path: Path
    exists: bool
    volume_parent: Path | None
    volume_mounted: bool | None
    device: int | None


def lake_root_identity(
    lake_root: Path,
    *,
    ismount: Callable[[str], bool] = os.path.ismount,
    stat_dev: Callable[[str], int] | None = None,
) -> LakeRootIdentity:
    """Resolve the on-disk IDENTITY of *lake_root* — the G1 mount/split-brain facts.

    Pure computation over injected probes (``ismount`` / ``stat_dev``), so unit
    tests drive every branch without touching a real volume. The default
    ``stat_dev`` reads ``os.stat(...).st_dev`` of the nearest existing ancestor.

    Parameters
    ----------
    lake_root
        The resolved lake-root directory (e.g. from :func:`resolve_lake_root`).
    ismount
        Mountpoint probe (default :func:`os.path.ismount`).
    stat_dev
        Device-id probe (default: ``os.stat(...).st_dev``); ``None`` uses the real one.

    Returns
    -------
    LakeRootIdentity
        The identity facts (see the dataclass docs).
    """

    def _real_stat_dev(target: str) -> int:
        return os.stat(target).st_dev

    dev_probe = stat_dev if stat_dev is not None else _real_stat_dev

    resolved = lake_root.expanduser()
    if not paths_rust_enabled():
        volume_parent = _volume_parent_python(resolved)
    else:
        volume_parent = route_volume_parent(resolved, lambda: _volume_parent_python(resolved))
    volume_mounted = ismount(str(volume_parent)) if volume_parent is not None else None

    device: int | None = None
    for candidate in (resolved, *resolved.parents):
        try:
            device = dev_probe(str(candidate))
        except OSError:
            continue
        break

    return LakeRootIdentity(
        path=resolved,
        exists=resolved.exists(),
        volume_parent=volume_parent,
        volume_mounted=volume_mounted,
        device=device,
    )


def _volume_parent_python(resolved: Path) -> Path | None:
    """The verbatim volume-parent derivation body — the LIVE oracle.

    Token-for-token the pre-seam inline block of :func:`lake_root_identity`
    (DEC-CONFIG-RUST-LEAVES-001); every flag-OFF / ineligible / degraded /
    burn-in call serves this.
    """
    # A ``/Volumes/<name>/…`` path has ≥ 3 parts: ("/", "Volumes", "<name>", …).
    min_volume_parts = 3
    parts = resolved.parts
    if (
        len(parts) >= min_volume_parts
        and parts[0] == "/"
        and parts[1] == _VOLUMES_PREFIX.lstrip("/")
    ):
        return Path(parts[0]) / parts[1] / parts[2]
    return None


__all__ = [
    "ENV_LAKE_ROOT",
    "LakeRootIdentity",
    "brutex_home",
    "lake_root_identity",
    "resolve_lake_root",
]
