"""Secure local credentials store for the Groww adapter (DEC-CREDENTIALS-STORE-001).

The operator sets their Groww **API key** + **TOTP secret** ONCE — via
``brutex auth set``, the dashboard Credentials page, or the
``BRUTEX_GROWW_*`` env vars — and they auto-load every session, satisfying
``CLAUDE.md §11`` AUTOMATED (no ``export`` per shell, zero clicks per day).

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
  :class:`CredentialsError` telling the operator to ``chmod 600`` — the
  same ``_INSECURE_MODE_BITS = 0o077`` philosophy as the token loader in
  :mod:`brutex.adapters.groww.auth`.
* **Never serialized into runtime state.** Status surfaces return ONLY
  booleans (:func:`credentials_present`); secret values never enter
  ``DashboardState`` or ``~/.brutex/runtime.json``.
* **Outside the repo.** The default path is ``~/.brutex/credentials.json``,
  outside the git tree (a ``.gitignore`` safety net guards the repo too).

## Resolution order

:func:`resolve_groww_credentials` is **per-field env-first, file-fallback**:
each of ``api_key`` / ``totp_secret`` is taken from its env var when
non-empty, else from the saved file. This lets an operator override a
single field from the environment without rewriting the file.
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

#: Env var holding the Groww API key (this is the env var NAME, not the value).
ENV_API_KEY: Final[str] = "BRUTEX_GROWW_API_KEY"

#: Env var holding the Groww TOTP secret (env var NAME, not the value).
ENV_TOTP_SECRET: Final[str] = "BRUTEX_GROWW_TOTP_SECRET"  # noqa: S105  # env var name, not a secret

#: Permission bits that, if set, mean the file is group / world accessible.
#: Mirrors ``brutex.adapters.groww.auth._INSECURE_MODE_BITS``.
_INSECURE_MODE_BITS: Final[int] = 0o077

#: How to set credentials, appended to "missing field" errors. Names the
#: three supported mechanisms; contains NO secret values.
_HOW_TO_SET: Final[str] = (
    "run `brutex auth set`, use the dashboard Credentials page, or export "
    "BRUTEX_GROWW_API_KEY / BRUTEX_GROWW_TOTP_SECRET"
)


class CredentialsError(Exception):
    """Credentials could not be saved, read, or resolved.

    Raised for blank inputs, insecure file permissions, a corrupt /
    wrong-shape file, or missing fields on resolution. The message names
    WHICH field is involved (and how to set it) but NEVER a secret value.
    """


def normalise_api_key(api_key: str) -> str:
    """Return *api_key* with surrounding whitespace stripped.

    A copy-pasted Groww API key very often carries a trailing newline or
    stray spaces (clipboard / password-manager artefacts). The SDK puts the
    raw value straight into an ``Authorization: Bearer <key>`` header, so a
    trailing ``\\n`` produces a malformed header and Groww rejects the
    token mint with **HTTP 400** (DEC-CREDS-NORMALISE-001). Stripping the
    outer whitespace removes that whole class of opaque 400s. Internal
    characters are left untouched (an API key is an opaque token).
    """
    return api_key.strip()


def normalise_totp_secret(totp_secret: str) -> str:
    """Return *totp_secret* canonicalised for ``pyotp`` (all whitespace removed, upper-cased).

    Groww (and most authenticator setups) display a base32 TOTP secret in
    space-separated groups — e.g. ``"ABCD EFGH IJKL MNOP"``. ``pyotp`` feeds
    the secret straight to :func:`base64.b32decode`, which treats an embedded
    space as an invalid byte: the code it then generates does not match what
    Groww expects, so the token mint fails with **HTTP 400**
    (DEC-CREDS-NORMALISE-001). Removing **all** whitespace (not just the ends)
    and upper-casing yields the canonical base32 the decoder expects. base32
    is case-insensitive, so upper-casing is loss-free.
    """
    return "".join(totp_secret.split()).upper()


@dataclass(frozen=True, slots=True)
class GrowwCredentials:
    """An immutable pair of Groww credentials.

    Attributes
    ----------
    api_key
        The Groww API key (``BRUTEX_GROWW_API_KEY``).
    totp_secret
        The base32 TOTP secret (``BRUTEX_GROWW_TOTP_SECRET``).
    """

    api_key: str
    totp_secret: str


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


def save_groww_credentials(creds: GrowwCredentials, *, path: Path | None = None) -> Path:
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
        If ``api_key`` or ``totp_secret`` is blank (after strip). The
        message names the field, never its value.

    Notes
    -----
    The write is atomic: a :func:`tempfile.NamedTemporaryFile` is created
    in the destination directory, ``chmod``-ed 0600, then ``os.replace``-d
    over *path* — so the file never exists world-readable, even briefly.
    On any failure the temp file is removed.
    """
    if not creds.api_key.strip():
        raise CredentialsError("api_key must not be blank")
    if not creds.totp_secret.strip():
        raise CredentialsError("totp_secret must not be blank")

    # Persist the CANONICAL form (DEC-CREDS-NORMALISE-001) so a paste with a
    # trailing newline / spaced-out base32 secret never reaches the Groww
    # token mint as a malformed Bearer header / undecodable TOTP secret (the
    # opaque "Groww API Error 400" the operator hit).
    api_key = normalise_api_key(creds.api_key)
    totp_secret = normalise_totp_secret(creds.totp_secret)

    target = credentials_path() if path is None else path
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    payload = json.dumps({"groww": {"api_key": api_key, "totp_secret": totp_secret}})
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


def load_groww_credentials(*, path: Path | None = None) -> GrowwCredentials | None:
    """Load saved credentials, or ``None`` when the file is absent.

    Parameters
    ----------
    path
        File to read. Defaults to :func:`credentials_path`.

    Returns
    -------
    GrowwCredentials | None
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
    groww = raw.get("groww")
    if not isinstance(groww, dict):
        raise CredentialsError(f"credentials file {target} is missing the 'groww' section")
    api_key = groww.get("api_key")
    totp_secret = groww.get("totp_secret")
    if not isinstance(api_key, str) or not api_key.strip():
        raise CredentialsError(f"credentials file {target} has a missing or blank api_key")
    if not isinstance(totp_secret, str) or not totp_secret.strip():
        raise CredentialsError(f"credentials file {target} has a missing or blank totp_secret")
    return GrowwCredentials(api_key=api_key, totp_secret=totp_secret)


def resolve_groww_credentials(
    env: Mapping[str, str] | None = None, *, path: Path | None = None
) -> GrowwCredentials:
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
    GrowwCredentials
        The resolved credentials.

    Raises
    ------
    CredentialsError
        If either field cannot be resolved from env or file. The message
        names WHICH field(s) are missing and how to set them — never a
        value. Also propagated from :func:`load_groww_credentials` for an
        insecure / corrupt file.
    """
    source = os.environ if env is None else env
    stored = load_groww_credentials(path=path)

    env_api_key = source.get(ENV_API_KEY)
    env_totp = source.get(ENV_TOTP_SECRET)

    api_key: str | None = None
    if env_api_key is not None and env_api_key.strip():
        api_key = env_api_key
    elif stored is not None:
        api_key = stored.api_key

    totp_secret: str | None = None
    if env_totp is not None and env_totp.strip():
        totp_secret = env_totp
    elif stored is not None:
        totp_secret = stored.totp_secret

    missing: list[str] = []
    if api_key is None:
        missing.append(ENV_API_KEY)
    if totp_secret is None:
        missing.append(ENV_TOTP_SECRET)
    if api_key is None or totp_secret is None:
        raise CredentialsError(f"missing Groww credentials: {', '.join(missing)}; {_HOW_TO_SET}")
    # Canonicalise at the resolution boundary (DEC-CREDS-NORMALISE-001) so the
    # auth layer always gets a clean Bearer key + decodable base32 secret —
    # whether the value came from the saved file OR a ``BRUTEX_GROWW_*`` env
    # var (an exported value can carry the same paste whitespace). Idempotent
    # on already-canonical values.
    return GrowwCredentials(
        api_key=normalise_api_key(api_key),
        totp_secret=normalise_totp_secret(totp_secret),
    )


def credentials_present(env: Mapping[str, str] | None = None, *, path: Path | None = None) -> bool:
    """Return ``True`` iff :func:`resolve_groww_credentials` would succeed.

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
        resolve_groww_credentials(env, path=path)
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
    """Return a NON-SECRET preview of the stored Groww credentials for the operator.

    The operator hit an opaque Groww 400 and could not tell whether BruteX had
    actually stored their paste or was sending empty values — and the credentials
    file lives outside the repo, so it is invisible in the IDE. This surfaces, on
    the dashboard Credentials page, a masked ``head…tail`` preview + character
    length of each resolved field so the paste can be VERIFIED at a glance
    (DEC-CREDS-PREVIEW-001). It NEVER returns (or logs) a full secret.

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
        ``{"present": True, "api_key_preview", "api_key_len",
        "totp_secret_preview", "totp_secret_len"}`` — masked previews + lengths
        of the normalised values, never the raw secrets.
    """
    try:
        creds = resolve_groww_credentials(env, path=path)
    except CredentialsError:
        return {"present": False}
    return {
        "present": True,
        "api_key_preview": _mask_secret(creds.api_key, head=6, tail=4),
        "api_key_len": len(creds.api_key),
        "totp_secret_preview": _mask_secret(creds.totp_secret, head=2, tail=2),
        "totp_secret_len": len(creds.totp_secret),
    }


def clear_groww_credentials(*, path: Path | None = None) -> bool:
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
    "ENV_API_KEY",
    "ENV_CREDENTIALS_FILE",
    "ENV_TOTP_SECRET",
    "CredentialsError",
    "GrowwCredentials",
    "clear_groww_credentials",
    "credentials_path",
    "credentials_present",
    "credentials_preview",
    "load_groww_credentials",
    "normalise_api_key",
    "normalise_totp_secret",
    "resolve_groww_credentials",
    "save_groww_credentials",
]
