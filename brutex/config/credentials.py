"""Secure local credentials store for the Dhan adapter (DEC-CREDENTIALS-STORE-001).

The operator sets their Dhan **access token** + **client id** ONCE — via
``brutex auth set``, the dashboard Credentials page, or the
``BRUTEX_DHAN_*`` env vars — and they auto-load every session, satisfying
``CLAUDE.md §11`` AUTOMATED (no ``export`` per shell, zero clicks per day).

This is the file-backed half of the Dhan credential resolution
(DEC-DHAN-CREDS-WIRING-001): the adapter's env-first resolver
(:func:`brutex.adapters.dhan.auth.resolve_dhan_credentials`) falls back to the
file this module writes, so creds saved on the dashboard **actually enable a
live pull** — closing the prior Groww-store-vs-Dhan-env disconnect that forced
every dashboard pull to dry-run.

## Security model (the only thing this module is for)

* **Never logged.** No secret VALUE is ever passed to ``structlog`` /
  ``print`` / ``logging``. Error messages name WHICH field is missing,
  never its value.
* **Mode 0600, atomic write.** The credentials file is written to a temp
  file in the same directory, ``chmod`` 0600, then ``os.replace``-d into
  place — there is never a window where it exists world-readable. The
  parent dir is created mode 0700.
* **Insecure-perms refusal on read.** A file whose mode has any group /
  world bits (``mode & 0o077 != 0``) is REFUSED with a
  :class:`CredentialsError` telling the operator to ``chmod 600``.
* **Never serialized into runtime state.** Status surfaces return ONLY
  booleans (:func:`credentials_present`) + masked previews; secret values
  never enter ``DashboardState`` or ``~/.brutex/runtime.json``.
* **Outside the repo.** The default path is ``~/.brutex/credentials.json``,
  outside the git tree (a ``.gitignore`` safety net guards the repo too).

## Resolution order

:func:`resolve_dhan_file_credentials` is **per-field env-first, file-fallback**:
each of ``access_token`` / ``client_id`` is taken from its env var when
non-empty, else from the saved file. This lets an operator override a single
field from the environment without rewriting the file.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

#: Env var that overrides the credentials file location (path, not a secret).
ENV_CREDENTIALS_FILE: Final[str] = "BRUTEX_CREDENTIALS_FILE"

#: Env var holding the Dhan access token (the env var NAME, not the value).
ENV_ACCESS_TOKEN: Final[str] = "BRUTEX_DHAN_ACCESS_TOKEN"  # noqa: S105  # env var name, not a secret

#: Env var holding the Dhan client id (env var NAME, not the value).
ENV_CLIENT_ID: Final[str] = "BRUTEX_DHAN_CLIENT_ID"

#: Permission bits that, if set, mean the file is group / world accessible.
_INSECURE_MODE_BITS: Final[int] = 0o077

#: JSON top-level section the credentials live under.
_SECTION: Final[str] = "dhan"

#: How to set credentials, appended to "missing field" errors. Names the
#: three supported mechanisms; contains NO secret values.
_HOW_TO_SET: Final[str] = (
    "run `brutex auth set`, use the dashboard Credentials page, or export "
    "BRUTEX_DHAN_ACCESS_TOKEN / BRUTEX_DHAN_CLIENT_ID"
)


class CredentialsError(Exception):
    """Credentials could not be saved, read, or resolved.

    Raised for blank inputs, insecure file permissions, a corrupt /
    wrong-shape file, or missing fields on resolution. The message names
    WHICH field is involved (and how to set it) but NEVER a secret value.
    """


def normalise_token(value: str) -> str:
    """Return *value* with surrounding whitespace stripped.

    A copy-pasted Dhan access token / client id very often carries a trailing
    newline or stray spaces (clipboard / password-manager artefacts). The
    client puts the access token straight into an ``access-token`` header, so a
    trailing ``\\n`` produces a malformed header and Dhan rejects the request
    with **HTTP 401** (DEC-CREDS-NORMALISE-001). Stripping the outer whitespace
    removes that whole class of opaque auth failures. Internal characters are
    left untouched (the token is an opaque JWT).
    """
    return value.strip()


@dataclass(frozen=True, slots=True)
class DhanFileCredentials:
    """An immutable pair of Dhan credentials as stored on disk.

    Attributes
    ----------
    access_token
        The Dhan access token (``BRUTEX_DHAN_ACCESS_TOKEN``).
    client_id
        The Dhan client id (``BRUTEX_DHAN_CLIENT_ID``).
    """

    access_token: str
    client_id: str


def credentials_path(env: Mapping[str, str] | None = None) -> Path:
    """Return the resolved credentials file path.

    Parameters
    ----------
    env
        Environment mapping to inspect for :data:`ENV_CREDENTIALS_FILE`.
        Defaults to :data:`os.environ`.

    Returns
    -------
    Path
        :data:`ENV_CREDENTIALS_FILE` (with ``~`` expanded) when set and
        non-blank, else ``~/.brutex/credentials.json``.
    """
    source = os.environ if env is None else env
    override = source.get(ENV_CREDENTIALS_FILE)
    if override is not None and override.strip():
        return Path(override).expanduser()
    return Path.home() / ".brutex" / "credentials.json"


def save_dhan_credentials(creds: DhanFileCredentials, *, path: Path | None = None) -> Path:
    """Atomically write *creds* to *path* at mode 0600 (parent dir 0700).

    Parameters
    ----------
    creds
        The credentials to persist. Both fields must be non-blank.
    path
        Destination file. Defaults to :func:`credentials_path`.

    Returns
    -------
    Path
        The path written.

    Raises
    ------
    CredentialsError
        If ``access_token`` or ``client_id`` is blank (after strip). The
        message names the field, never its value.

    Notes
    -----
    The write is atomic: a :func:`tempfile.NamedTemporaryFile` is created
    in the destination directory, ``chmod``-ed 0600, then ``os.replace``-d
    over *path* — so the file never exists world-readable, even briefly.
    On any failure the temp file is removed.
    """
    if not creds.access_token.strip():
        raise CredentialsError("access_token must not be blank")
    if not creds.client_id.strip():
        raise CredentialsError("client_id must not be blank")

    # Persist the CANONICAL (stripped) form (DEC-CREDS-NORMALISE-001) so a paste
    # with a trailing newline never reaches the Dhan ``access-token`` header
    # malformed (the opaque 401 the operator would otherwise hit).
    access_token = normalise_token(creds.access_token)
    client_id = normalise_token(creds.client_id)

    target = credentials_path() if path is None else path
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    payload = json.dumps({_SECTION: {"access_token": access_token, "client_id": client_id}})
    # Atomic, never-world-readable write: NamedTemporaryFile in the SAME
    # dir as the target (so os.replace is atomic), chmod 0600 before the
    # rename, then os.replace. No predictable-name temp file and no
    # world-readable window — bandit raises no finding here.
    tmp = tempfile.NamedTemporaryFile(  # noqa: SIM115
        mode="w", dir=parent, prefix=".credentials-", suffix=".tmp", delete=False
    )
    tmp_path = Path(tmp.name)
    try:
        tmp.write(payload)
        tmp.flush()
        tmp.close()
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, target)
    except OSError:
        tmp.close()
        tmp_path.unlink(missing_ok=True)
        raise
    return target


def load_dhan_credentials(*, path: Path | None = None) -> DhanFileCredentials | None:
    """Load saved credentials, or ``None`` when the file is absent.

    Parameters
    ----------
    path
        File to read. Defaults to :func:`credentials_path`.

    Returns
    -------
    DhanFileCredentials | None
        The stored credentials, or ``None`` when *path* does not exist.

    Raises
    ------
    CredentialsError
        If the file exists but is group / world accessible
        (``mode & 0o077 != 0`` → ``chmod 600``), is a symlink (refused
        rather than followed), or is corrupt / wrong-shape / has a blank
        field. A present-but-broken file is surfaced, NOT silently treated
        as absent.
    """
    target = credentials_path() if path is None else path
    # Open with O_NOFOLLOW + check perms on the SAME fd (fstat) so the
    # permission check and the read can't be split by a symlink/file swap
    # (TOCTOU). O_NOFOLLOW refuses a symlinked final component outright.
    try:
        fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CredentialsError(
            f"credentials file {target} could not be opened securely "
            f"(is it a symlink?); recreate it via `brutex auth set`"
        ) from exc

    with os.fdopen(fd, encoding="utf-8") as handle:
        mode = stat.S_IMODE(os.fstat(handle.fileno()).st_mode)
        if mode & _INSECURE_MODE_BITS:
            raise CredentialsError(
                f"credentials file {target} has insecure permissions "
                f"(mode {oct(mode)}); run `chmod 600 {target}`"
            )
        try:
            raw = json.loads(handle.read())
        except (OSError, json.JSONDecodeError) as exc:
            raise CredentialsError(f"credentials file {target} is corrupt or unreadable") from exc

    if not isinstance(raw, dict):
        raise CredentialsError(f"credentials file {target} has an invalid shape")
    section = raw.get(_SECTION)
    if not isinstance(section, dict):
        raise CredentialsError(f"credentials file {target} is missing the '{_SECTION}' section")
    access_token = section.get("access_token")
    client_id = section.get("client_id")
    if not isinstance(access_token, str) or not access_token.strip():
        raise CredentialsError(f"credentials file {target} has a missing or blank access_token")
    if not isinstance(client_id, str) or not client_id.strip():
        raise CredentialsError(f"credentials file {target} has a missing or blank client_id")
    return DhanFileCredentials(access_token=access_token, client_id=client_id)


def resolve_dhan_file_credentials(
    env: Mapping[str, str] | None = None, *, path: Path | None = None
) -> DhanFileCredentials:
    """Resolve credentials per-field: env-first, saved-file fallback.

    Each field is taken from its env var when non-blank, otherwise from the
    saved file. This lets an operator override one field from the
    environment without rewriting the file.

    Parameters
    ----------
    env
        Environment mapping. Defaults to :data:`os.environ`.
    path
        Credentials file. Defaults to :func:`credentials_path`.

    Returns
    -------
    DhanFileCredentials
        The resolved credentials.

    Raises
    ------
    CredentialsError
        If either field cannot be resolved from env or file. The message
        names WHICH field(s) are missing and how to set them — never a
        value. Also propagated from :func:`load_dhan_credentials` for an
        insecure / corrupt file.
    """
    source = os.environ if env is None else env
    stored = load_dhan_credentials(path=path)

    env_token = source.get(ENV_ACCESS_TOKEN)
    env_client = source.get(ENV_CLIENT_ID)

    access_token: str | None = None
    if env_token is not None and env_token.strip():
        access_token = env_token
    elif stored is not None:
        access_token = stored.access_token

    client_id: str | None = None
    if env_client is not None and env_client.strip():
        client_id = env_client
    elif stored is not None:
        client_id = stored.client_id

    missing: list[str] = []
    if access_token is None:
        missing.append(ENV_ACCESS_TOKEN)
    if client_id is None:
        missing.append(ENV_CLIENT_ID)
    if access_token is None or client_id is None:
        raise CredentialsError(f"missing Dhan credentials: {', '.join(missing)}; {_HOW_TO_SET}")
    # Canonicalise at the resolution boundary (DEC-CREDS-NORMALISE-001) so the
    # auth layer always gets a clean ``access-token`` header value — whether the
    # value came from the saved file OR a ``BRUTEX_DHAN_*`` env var (an exported
    # value can carry the same paste whitespace). Idempotent on clean values.
    return DhanFileCredentials(
        access_token=normalise_token(access_token),
        client_id=normalise_token(client_id),
    )


def credentials_present(env: Mapping[str, str] | None = None, *, path: Path | None = None) -> bool:
    """Return ``True`` iff :func:`resolve_dhan_file_credentials` would succeed.

    Parameters
    ----------
    env
        Environment mapping. Defaults to :data:`os.environ`.
    path
        Credentials file. Defaults to :func:`credentials_path`.

    Returns
    -------
    bool
        ``True`` when both fields resolve (from env OR the saved file).
        ``False`` on any :class:`CredentialsError` — including an
        insecure-permissions file, which makes this ``False`` rather than
        raising (so a status surface degrades gracefully).
    """
    try:
        resolve_dhan_file_credentials(env, path=path)
    except CredentialsError:
        return False
    return True


def _mask_secret(value: str, *, head: int, tail: int) -> str:
    """Mask *value* to ``head…tail`` (or a full bullet-fill when too short to split).

    Used by :func:`credentials_preview` to show ENOUGH of a stored secret for the
    operator to recognise their own paste (right value, right length) without the
    dashboard ever echoing a usable secret. A value short enough that ``head +
    tail`` would reveal it whole is returned as bullets only.
    """
    if len(value) <= head + tail:
        return "•" * len(value)
    return f"{value[:head]}…{value[-tail:]}"


def credentials_preview(
    env: Mapping[str, str] | None = None, *, path: Path | None = None
) -> dict[str, object]:
    """Return a NON-SECRET preview of the stored Dhan credentials for the operator.

    Surfaces, on the dashboard Credentials page, a masked ``head…tail`` preview +
    character length of each resolved field so the paste can be VERIFIED at a
    glance (DEC-CREDS-PREVIEW-001). It NEVER returns (or logs) a full secret.

    Parameters
    ----------
    env
        Environment mapping. Defaults to :data:`os.environ`.
    path
        Credentials file. Defaults to :func:`credentials_path`.

    Returns
    -------
    dict[str, object]
        ``{"present": False}`` when credentials do not resolve, else
        ``{"present": True, "access_token_preview", "access_token_len",
        "client_id_preview", "client_id_len"}`` — masked previews + lengths
        of the normalised values, never the raw secrets.
    """
    try:
        creds = resolve_dhan_file_credentials(env, path=path)
    except CredentialsError:
        return {"present": False}
    return {
        "present": True,
        "access_token_preview": _mask_secret(creds.access_token, head=6, tail=4),
        "access_token_len": len(creds.access_token),
        "client_id_preview": _mask_secret(creds.client_id, head=2, tail=2),
        "client_id_len": len(creds.client_id),
    }


def clear_dhan_credentials(*, path: Path | None = None) -> bool:
    """Delete the credentials file if present.

    Parameters
    ----------
    path
        File to remove. Defaults to :func:`credentials_path`.

    Returns
    -------
    bool
        ``True`` if a file was removed, ``False`` if none existed.
    """
    target = credentials_path() if path is None else path
    # unlink-then-catch (not exists-then-unlink) so a concurrent removal
    # can't race between the check and the call.
    try:
        target.unlink()
    except FileNotFoundError:
        return False
    return True


__all__ = [
    "ENV_ACCESS_TOKEN",
    "ENV_CLIENT_ID",
    "ENV_CREDENTIALS_FILE",
    "CredentialsError",
    "DhanFileCredentials",
    "clear_dhan_credentials",
    "credentials_path",
    "credentials_present",
    "credentials_preview",
    "load_dhan_credentials",
    "normalise_token",
    "resolve_dhan_file_credentials",
    "save_dhan_credentials",
]
