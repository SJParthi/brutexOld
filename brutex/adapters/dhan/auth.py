"""Dhan API token loading.

Two sources, in priority order:

1. **File** — ``~/.brutex/dhan/access_token.json`` (mode 0600). Format:
   ``{"access_token": "<token>"}``. This is the production path —
   mode-0600 means the token is not world-readable, never appears in
   shell history, and is not exported to subprocesses.
2. **Env var** — ``BRUTEX_DHAN_ACCESS_TOKEN`` (CI / dev fallback).
   Used when the file does not exist. CI sets this from a GitHub
   Action secret; local dev can use it for quick experiments.

If neither is set, :func:`load_dhan_token` raises
:class:`MissingDhanTokenError`.

The path can be overridden via ``BRUTEX_DHAN_TOKEN_PATH`` for testing.

Why a wrapper rather than passing the string everywhere:

* :class:`DhanToken` has ``__repr__`` returning ``DhanToken(***)`` so
  the raw string never appears in logs, tracebacks, or error messages.
* The dataclass is ``frozen=True`` so the token cannot be mutated.
* Subclassing :class:`object` (not ``str``) prevents accidental string
  concatenation that would leak the value.

Decision context (DEC-P0-035, PR #2b):

* The pre-impl Security agent flagged ``trust_env=True`` and ``repr()``
  leaks as B-level blockers. This module addresses the second; the
  first lives in ``client.py``.
* The pre-impl Cite/path agent flagged the env-var-vs-file conflict
  in the docs. Both are now supported, with file-based primary per the
  security agent recommendation.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from brutex.models import BruteXError

#: Default path for the Dhan access token file. The ``~/`` expansion is
#: deferred until :func:`load_dhan_token` runs so tests can monkeypatch
#: ``HOME`` and observe a different path.
DEFAULT_TOKEN_PATH: Final[str] = "~/.brutex/dhan/access_token.json"  # noqa: S105  # path, not a secret

#: Env var that overrides :data:`DEFAULT_TOKEN_PATH`.
PATH_ENV_VAR: Final[str] = "BRUTEX_DHAN_TOKEN_PATH"

#: Env var that holds the access token directly (fallback when the
#: file does not exist).
TOKEN_ENV_VAR: Final[str] = "BRUTEX_DHAN_ACCESS_TOKEN"  # noqa: S105  # env var name, not the secret

#: File-permission bits the file must have. Anything looser (e.g. group
#: or world read) raises :class:`InsecureTokenFileError`.
REQUIRED_FILE_MODE: Final[int] = 0o600


class MissingDhanTokenError(BruteXError):
    """No Dhan token found in either the file or the env var."""


class InsecureTokenFileError(BruteXError):
    """Token file exists but has too-permissive mode bits."""


class MalformedTokenFileError(BruteXError):
    """Token file exists but cannot be parsed as expected JSON shape."""


@dataclass(frozen=True, slots=True)
class DhanToken:
    """Opaque wrapper around the Dhan access token.

    ``__repr__`` and ``__str__`` redact the raw value so the token never
    appears in tracebacks, structlog event dicts, or interactive debuggers.
    Use :meth:`reveal` exactly once, at the HTTP-header construction site
    in :class:`brutex.adapters.dhan.client.DhanClient`, to extract the
    underlying string.
    """

    _value: str

    def __post_init__(self) -> None:
        if not self._value:
            raise MissingDhanTokenError(
                "DhanToken._value is empty",
                safe_message="Dhan access token is empty (refusing to construct)",
            )

    def __repr__(self) -> str:
        return "DhanToken(***)"

    def __str__(self) -> str:
        return "DhanToken(***)"

    def reveal(self) -> str:
        """Return the underlying token string.

        Call this ONCE at the HTTP-header construction site only. Never
        log the return value; never pass it to format strings; never
        store it in an event dict.
        """
        return self._value


def _resolve_token_path() -> Path:
    """Return the resolved Path to the token file (honours env override)."""
    override = os.environ.get(PATH_ENV_VAR)
    raw = override if override else DEFAULT_TOKEN_PATH
    return Path(raw).expanduser()


def _read_token_from_file(path: Path) -> str:
    """Read + parse the token file. Enforces 0600 perms.

    Raises
    ------
    InsecureTokenFileError
        If the file's mode bits include any group or other access.
    MalformedTokenFileError
        If the file isn't JSON or lacks the ``access_token`` field.
    """
    file_stat = path.stat()
    mode_bits = file_stat.st_mode & 0o777
    if mode_bits & 0o077:
        raise InsecureTokenFileError(
            f"_read_token_from_file: {path} has mode 0o{mode_bits:o}; refusing — chmod 600 to fix",
            safe_message="Dhan token file has insecure permissions",
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MalformedTokenFileError(
            f"_read_token_from_file: {path} is not valid JSON",
            safe_message="Dhan token file is not valid JSON",
        ) from exc
    if not isinstance(payload, dict) or "access_token" not in payload:
        raise MalformedTokenFileError(
            f"_read_token_from_file: {path} missing 'access_token' field",
            safe_message="Dhan token file missing 'access_token' field",
        )
    token = payload["access_token"]
    if not isinstance(token, str) or not token:
        raise MalformedTokenFileError(
            f"_read_token_from_file: {path} access_token is not a non-empty string",
            safe_message="Dhan token file has empty or non-string access_token",
        )
    return token


def load_dhan_token() -> DhanToken:
    """Load the Dhan access token from file (primary) or env (fallback).

    Returns
    -------
    DhanToken
        The wrapped token, ready for :meth:`DhanToken.reveal` at the
        HTTP-header site.

    Raises
    ------
    MissingDhanTokenError
        Neither source resolved.
    InsecureTokenFileError
        File present but with too-loose permissions.
    MalformedTokenFileError
        File present but malformed.
    """
    path = _resolve_token_path()
    if path.is_file():
        return DhanToken(_value=_read_token_from_file(path))

    env_token = os.environ.get(TOKEN_ENV_VAR, "")
    if env_token:
        return DhanToken(_value=env_token)

    raise MissingDhanTokenError(
        f"load_dhan_token: no token at {path} and env {TOKEN_ENV_VAR} is unset",
        safe_message=(
            "Dhan access token not configured (set " + TOKEN_ENV_VAR + " or create the token file)"
        ),
    )
