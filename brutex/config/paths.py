"""Canonical BruteX filesystem-path resolvers — one source of truth (CLAUDE.md §11 COMMON).

Every BruteX surface that needs the on-disk home or the lake root resolves it
HERE, so the rule lives in exactly one place (the CLI, the wiper, and any
future caller all delegate). Pure path computation — ``$HOME`` and the
``BRUTEX_*`` environment are injected, no directory is created, and nothing on
disk is touched — so the module imports and unit-tests with zero side effects.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Final

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
    if override:
        return Path(override).expanduser()
    return brutex_home(home=home) / "lake"


__all__ = ["ENV_LAKE_ROOT", "brutex_home", "resolve_lake_root"]
