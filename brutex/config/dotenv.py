"""Minimal, dependency-free ``.env`` loader — env persistence (GAP C6).

The operator runs the overnight pull on a MacBook (M4 Pro). Every BruteX knob
is a ``BRUTEX_*`` process env var — pool/pacing flags
(``BRUTEX_GROWW_SMOOTH_PACING``), the lake location (``BRUTEX_LAKE_ROOT``),
AWS token-broker overrides (``BRUTEX_TOKEN_PARAM_*`` /
``BRUTEX_TOKEN_AWS_REGION``) … Without a ``.env`` those vanish on every
shell/reboot, so the operator must re-export them by hand. (Groww credentials
are NOT env vars — AWS-SSM-only, DEC-AWS-ONLY-CREDS-001.)

This module loads a ``.env`` file (repo root → cwd → ``~/.brutex/.env``) ONCE at
process bootstrap, **without ever overriding an env var that is already set** —
a real exported value ALWAYS wins over the file (no surprise shadowing). It is a
**no-op when no ``.env`` exists** and ``python-dotenv`` is intentionally NOT a
dependency: the parser is a tiny safe ``KEY=VALUE`` reader.

Golden Rule honesty: secret VALUES are NEVER logged — only the (masked) set of
applied KEY NAMES, so an operator can see *that* the file was honoured without a
credential leaking into a log file.

Everything is pure + injectable (the search paths AND the target ``env``
mapping), so the unit tests run with zero side effects on the real process env.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, MutableMapping
from pathlib import Path
from typing import Final

import structlog

#: The conventional dotenv filename.
DEFAULT_DOTENV_NAME: Final[str] = ".env"

#: Directory under ``$HOME`` holding default BruteX state (mirrors config.paths).
_HOME_DIR_NAME: Final[str] = ".brutex"

#: Minimum value length for the surrounding-quote-stripping check (open+close).
_MIN_QUOTED_LEN: Final[int] = 2

_log = structlog.get_logger(__name__)


def parse_dotenv(text: str) -> dict[str, str]:
    """Parse ``.env`` text into a ``{KEY: VALUE}`` mapping.

    Rules (deliberately minimal + safe — no shell expansion, no interpolation):

    * blank lines and ``#`` comment lines are ignored,
    * a leading ``export`` is tolerated,
    * the FIRST ``=`` splits key from value (``=`` may appear in the value),
    * an empty key (``=foo``) or a line with no ``=`` is skipped,
    * surrounding matching single/double quotes are stripped; key+value are
      whitespace-trimmed.

    Parameters
    ----------
    text
        The raw file contents.

    Returns
    -------
    dict[str, str]
        Parsed key/value pairs (later duplicate keys override earlier ones).
    """
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= _MIN_QUOTED_LEN and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        out[key] = value
    return out


def load_dotenv_file(path: Path) -> dict[str, str]:
    """Read + parse a single ``.env`` *path*; ``{}`` when it is not a file.

    A missing path OR a path that exists but is not a regular file (e.g. a
    directory) yields an empty mapping — never an error, so a bootstrap call is
    always safe.

    Parameters
    ----------
    path
        The candidate ``.env`` file.

    Returns
    -------
    dict[str, str]
        Parsed pairs, or ``{}`` when *path* is not a readable file.
    """
    if not path.is_file():
        return {}
    return parse_dotenv(path.read_text(encoding="utf-8"))


def apply_dotenv(values: Mapping[str, str], *, env: MutableMapping[str, str]) -> tuple[str, ...]:
    """Set each *values* key in *env* ONLY when it is not already present.

    The real (already-set) env ALWAYS wins — this never overwrites an existing
    key, so an exported shell value is never silently shadowed by the file.

    Parameters
    ----------
    values
        Parsed ``.env`` pairs.
    env
        The target environment mapping to mutate (e.g. ``os.environ``).

    Returns
    -------
    tuple[str, ...]
        The sorted key names that were actually applied (absent before).
    """
    applied: list[str] = []
    for key, value in values.items():
        if key not in env:
            env[key] = value
            applied.append(key)
    return tuple(sorted(applied))


def candidate_dotenv_paths(*, repo_root: Path, cwd: Path, home: Path) -> tuple[Path, ...]:
    """Return the ordered, de-duplicated ``.env`` search paths.

    Order (first match wins per key): repo root → current working dir →
    ``~/.brutex/.env``. Identical paths (repo root == cwd) collapse so a key is
    not "applied twice".

    Parameters
    ----------
    repo_root
        The repository checkout root.
    cwd
        The current working directory.
    home
        The user's home directory.

    Returns
    -------
    tuple[Path, ...]
        The candidate paths in priority order, de-duplicated.
    """
    raw = (
        repo_root / DEFAULT_DOTENV_NAME,
        cwd / DEFAULT_DOTENV_NAME,
        home / _HOME_DIR_NAME / DEFAULT_DOTENV_NAME,
    )
    seen: set[Path] = set()
    ordered: list[Path] = []
    for path in raw:
        if path not in seen:
            seen.add(path)
            ordered.append(path)
    return tuple(ordered)


def _default_repo_root() -> Path:
    """Best-effort repository root — two parents up from this file's package."""
    return Path(__file__).resolve().parents[2]


def bootstrap_env(
    *,
    env: MutableMapping[str, str] | None = None,
    repo_root: Path | None = None,
    cwd: Path | None = None,
    home: Path | None = None,
) -> tuple[str, ...]:
    """Load ``.env`` files into *env*, never overriding already-set vars.

    Call this ONCE at process bootstrap (CLI ``main`` + dashboard app factory).
    A no-op when no ``.env`` file exists anywhere on the search path. The real
    process env always wins; earlier candidate files win over later ones.

    Parameters
    ----------
    env
        Target environment mapping (defaults to :data:`os.environ`).
    repo_root, cwd, home
        Injectable search-path roots (default to the repo root, ``Path.cwd()``,
        and ``Path.home()``).

    Returns
    -------
    tuple[str, ...]
        The sorted key names applied across all files (``()`` when none).
    """
    target: MutableMapping[str, str] = os.environ if env is None else env
    roots_repo = _default_repo_root() if repo_root is None else repo_root
    roots_cwd = Path.cwd() if cwd is None else cwd
    roots_home = Path.home() if home is None else home

    merged: dict[str, str] = {}
    # Reverse priority order so EARLIER candidates overwrite later ones in the
    # merged view; the real-env-wins guard then runs once on the merge.
    for path in reversed(
        candidate_dotenv_paths(repo_root=roots_repo, cwd=roots_cwd, home=roots_home)
    ):
        merged.update(load_dotenv_file(path))

    applied = apply_dotenv(merged, env=target)
    if applied:
        # NEVER log values — only the (already-public) key NAMES.
        _log.info("config.dotenv.loaded", applied_keys=list(applied), count=len(applied))
    return applied


__all__ = [
    "DEFAULT_DOTENV_NAME",
    "apply_dotenv",
    "bootstrap_env",
    "candidate_dotenv_paths",
    "load_dotenv_file",
    "parse_dotenv",
]
