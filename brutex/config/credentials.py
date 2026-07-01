"""Secure local credentials store for the vendor adapter (DEC-CREDENTIALS-STORE-001).

The operator sets their token-style **access token** + **client id** ONCE — via
``brutex auth set``, the dashboard Credentials page, or the
``BRUTEX_ACCESS_TOKEN`` / ``BRUTEX_CLIENT_ID`` env vars — and they auto-load
every session, satisfying ``CLAUDE.md §11`` AUTOMATED (no ``export`` per shell,
zero clicks per day).

This is the file-backed half of the token-style credential resolution
(DEC-DHAN-CREDS-WIRING-001): the vendor adapter's env-first resolver falls back
to the file this module writes, so creds saved on the dashboard **actually
enable a live pull** — closing the prior store-vs-env disconnect that forced
every dashboard pull to dry-run.

## Security model (the only thing this module is for)

* **Never logged.** No secret VALUE is ever passed to ``structlog`` /
  ``print`` / ``logging``. Error messages name WHICH field is missing,
  never its value.
* **Mode 0600, atomic write.** The credentials file is written via the COMMON
  :func:`brutex.data.atomic_io.atomic_write_simple` with ``chmod=0o600`` — a temp
  in the same directory (created 0600 by ``mkstemp``) is ``chmod``-ed 0600, then
  ``os.replace``-d into place — so there is never a window where it exists
  world-readable. The parent dir is created mode 0700.
* **Insecure-perms refusal on read.** A file whose mode has any group /
  world bits (``mode & 0o077 != 0``) is REFUSED with a
  :class:`CredentialsError` telling the operator to ``chmod 600``.
* **Never serialized into runtime state.** Status surfaces return ONLY
  booleans (:func:`credentials_present`) + masked previews; secret values
  never enter ``DashboardState`` or ``~/.brutex/runtime.json``.
* **Outside the repo.** The default path is ``~/.brutex/credentials.json``,
  outside the git tree (a ``.gitignore`` safety net guards the repo too).

## Resolution order

:func:`resolve_token_file_credentials` is **per-field env-first, file-fallback**:
each of ``access_token`` / ``client_id`` is taken from its env var when
non-empty, else from the saved file. This lets an operator override a single
field from the environment without rewriting the file.
"""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from brutex.adapters.groww.auth import ENV_API_KEY as ENV_GROWW_API_KEY
from brutex.adapters.groww.auth import ENV_TOTP_SECRET as ENV_GROWW_TOTP_SECRET
from brutex.data.atomic_io import atomic_write_simple

#: Env var that overrides the credentials file location (path, not a secret).
ENV_CREDENTIALS_FILE: Final[str] = "BRUTEX_CREDENTIALS_FILE"

#: Env var holding the access token (the env var NAME, not the value).
ENV_ACCESS_TOKEN: Final[str] = "BRUTEX_ACCESS_TOKEN"  # noqa: S105  # env var name, not a secret

#: Env var holding the client id (env var NAME, not the value).
ENV_CLIENT_ID: Final[str] = "BRUTEX_CLIENT_ID"

#: Permission bits that, if set, mean the file is group / world accessible.
_INSECURE_MODE_BITS: Final[int] = 0o077

#: JSON top-level section the token-style credentials live under.
_SECTION: Final[str] = "token"

#: JSON top-level section the Groww credentials live under. The Groww section
#: and the legacy token-style section coexist in ONE file (read-merge-write), so
#: saving one vendor's credentials never drops the other's
#: (DEC-DHAN-GROWW-REMIGRATE-001).
_GROWW_SECTION: Final[str] = "groww"

#: How to set Groww credentials, appended to "missing field" errors. Names the
#: supported mechanisms; contains NO secret values.
_GROWW_HOW_TO_SET: Final[str] = (
    "use the dashboard Groww Credentials page, or export "
    "BRUTEX_GROWW_API_KEY / BRUTEX_GROWW_TOTP_SECRET"
)

#: How to set credentials, appended to "missing field" errors. Names the
#: three supported mechanisms; contains NO secret values.
_HOW_TO_SET: Final[str] = (
    "run `brutex auth set`, use the dashboard Credentials page, or export "
    "BRUTEX_ACCESS_TOKEN / BRUTEX_CLIENT_ID"
)


class CredentialsError(Exception):
    """Credentials could not be saved, read, or resolved.

    Raised for blank inputs, insecure file permissions, a corrupt /
    wrong-shape file, or missing fields on resolution. The message names
    WHICH field is involved (and how to set it) but NEVER a secret value.
    """


def normalise_token(value: str) -> str:
    """Return *value* with surrounding whitespace stripped.

    A copy-pasted access token / client id very often carries a trailing
    newline or stray spaces (clipboard / password-manager artefacts). The
    client puts the access token straight into an ``access-token`` header, so a
    trailing ``\\n`` produces a malformed header and the vendor rejects the request
    with **HTTP 401** (DEC-CREDS-NORMALISE-001). Stripping the outer whitespace
    removes that whole class of opaque auth failures. Internal characters are
    left untouched (the token is an opaque JWT).
    """
    return value.strip()


#: A token-style access token is a JWT (``CLAUDE.md`` §6: "header ``access-token``
#: (JWT, 24h)") — exactly three base64url segments joined by ``.``
#: (``header.payload.signature``). A clipboard paste that drops characters
#: almost always loses or empties the trailing signature segment, collapsing
#: the segment count, so a structural JWT check is a fast, offline catch for
#: the dominant paste-truncation failure mode (DEC-CREDS-JWT-SHAPE-001).
_JWT_SEGMENTS: Final[int] = 3

#: A single JWT segment: non-empty base64url (the JWT-unpadded alphabet,
#: ``A-Z a-z 0-9 - _``). An empty or otherwise-charactered segment means the
#: paste was truncated or corrupted.
_B64URL_SEGMENT: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_-]+$")


def token_shape_problem(token: str) -> str | None:
    """Return a reason *token* is not a well-formed JWT, or ``None`` when it is.

    A token-style access token is a JWT (``CLAUDE.md`` §6) — exactly three
    non-empty base64url segments joined by ``.`` (``header.payload.signature``).
    A clipboard paste that drops characters almost always loses or empties the
    trailing signature segment, collapsing the segment count, so this
    structural check catches the dominant truncation failure mode **offline,
    before a doomed token is ever saved** (DEC-CREDS-JWT-SHAPE-001). It is the
    web-form save guard's first line of defence against the ``DH-906 Invalid
    Token`` failures an incomplete paste produces.

    This is a STRUCTURAL check, and it is honest about its limits: it reliably
    catches a token that lost a whole segment, or whose paste introduced a
    non-base64url character, or has an empty segment. A token clipped *within*
    its (3rd) segment but still leaving three segments can pass this check —
    the live pre-flight data-probe (``token_preflight``) is the backstop for
    that residual case.

    Parameters
    ----------
    token
        The pasted access token. Surrounding whitespace is stripped before the
        check (mirrors :func:`normalise_token`), so a trailing newline is not
        mistaken for malformation.

    Returns
    -------
    str | None
        A human-readable, **secret-free** reason (segment count / length only,
        never the token value) when the token is malformed; ``None`` when the
        token has a valid JWT shape.
    """
    stripped = token.strip()
    segments = stripped.split(".")
    if len(segments) != _JWT_SEGMENTS:
        return (
            f"access token is not a JWT: expected {_JWT_SEGMENTS} dot-separated "
            f"segments, found {len(segments)} ({len(stripped)} chars) — the "
            f"paste looks truncated. Clear the field and re-paste the FULL token."
        )
    if any(_B64URL_SEGMENT.match(seg) is None for seg in segments):
        return (
            "access token is not a JWT: a segment is empty or contains "
            "non-base64url characters — the paste looks truncated or corrupt. "
            "Clear the field and re-paste the FULL token."
        )
    return None


@dataclass(frozen=True, slots=True)
class GrowwFileCredentials:
    """An immutable pair of Groww TOTP credentials as stored on disk.

    Attributes
    ----------
    api_key
        The Groww TOTP api key (``BRUTEX_GROWW_API_KEY``).
    totp_secret
        The Groww TOTP shared secret (``BRUTEX_GROWW_TOTP_SECRET``), held only
        to mint the 6-digit code.
    """

    api_key: str
    totp_secret: str


@dataclass(frozen=True, slots=True)
class TokenFileCredentials:
    """An immutable pair of token-style credentials as stored on disk.

    Attributes
    ----------
    access_token
        The access token (``BRUTEX_ACCESS_TOKEN``).
    client_id
        The client id (``BRUTEX_CLIENT_ID``).
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


def _read_raw_sections(target: Path) -> dict[str, object]:
    """Return the parsed top-level object of *target*, or ``{}`` when absent.

    The READ half of the read-merge-write that lets the token-style + Groww
    sections coexist: saving one vendor's credentials reads the existing file
    first so the OTHER vendor's section is preserved (it was previously clobbered
    by a whole-file ``json.dumps`` — DEC-DHAN-GROWW-REMIGRATE-001). Mirrors the
    security posture of :func:`load_token_credentials`: ``O_NOFOLLOW`` open +
    insecure-permission refusal on the same fd (TOCTOU-safe). A corrupt /
    wrong-shape existing file is refused rather than silently overwritten, so a
    save can never destroy an unreadable-but-present file's contents.

    Raises
    ------
    CredentialsError
        If the file exists but is a symlink, group / world accessible, or
        corrupt / not a JSON object.
    """
    try:
        fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise CredentialsError(
            f"credentials file {target} could not be opened securely "
            f"(is it a symlink?); recreate it via the dashboard Credentials page"
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
    return raw


def _atomic_write(target: Path, payload: str) -> None:
    """Atomically write *payload* to *target* at mode 0600 (parent dir 0700).

    The WRITE half of the read-merge-write, shared by the token-style + Groww
    saves. Delegates to the COMMON guard-free
    :func:`brutex.data.atomic_io.atomic_write_simple` (the ONE
    tmp→``fsync``→``os.replace``→cleanup, CLAUDE.md §11, DEC-ATOMIC-WRITE-CONSOLIDATE-001)
    with ``chmod=0o600`` — the ``mkstemp`` temp is already 0600 and the explicit
    ``chmod`` pins the final file's mode, so the secret is NEVER world-readable, even
    briefly. Guard-FREE so a raw ``OSError`` propagates (the credential store maps its
    own errors), never the lake's ``LakeWriteHalt``.
    """
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_write_simple(target, lambda tmp: tmp.write_text(payload, encoding="utf-8"), chmod=0o600)


def _save_section(target: Path, section: str, values: dict[str, str]) -> None:
    """Read-merge-write *values* into *section* of *target*, preserving others.

    Reads the existing file (:func:`_read_raw_sections`), replaces only
    *section*, and writes the whole object back atomically — so a token-style save
    never drops a stored Groww section and vice versa (DEC-DHAN-GROWW-REMIGRATE-001).
    """
    raw = _read_raw_sections(target)
    raw[section] = values
    _atomic_write(target, json.dumps(raw))


def save_token_credentials(creds: TokenFileCredentials, *, path: Path | None = None) -> Path:
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
    The write is a **read-merge-write** (:func:`_save_section`): the existing
    file is read first so a stored Groww section is preserved, then only the
    ``token`` section is replaced and the whole object written atomically — via
    the COMMON :func:`brutex.data.atomic_io.atomic_write_simple` with
    ``chmod=0o600`` (temp in the same dir, ``chmod`` 0600, then ``os.replace``),
    so the file never exists world-readable. On any failure the temp is removed.
    """
    if not creds.access_token.strip():
        raise CredentialsError("access_token must not be blank")
    if not creds.client_id.strip():
        raise CredentialsError("client_id must not be blank")

    # Persist the CANONICAL (stripped) form (DEC-CREDS-NORMALISE-001) so a paste
    # with a trailing newline never reaches the ``access-token`` header
    # malformed (the opaque 401 the operator would otherwise hit).
    access_token = normalise_token(creds.access_token)
    client_id = normalise_token(creds.client_id)

    target = credentials_path() if path is None else path
    _save_section(target, _SECTION, {"access_token": access_token, "client_id": client_id})
    return target


def load_token_credentials(*, path: Path | None = None) -> TokenFileCredentials | None:
    """Load saved credentials, or ``None`` when the file is absent.

    Parameters
    ----------
    path
        File to read. Defaults to :func:`credentials_path`.

    Returns
    -------
    TokenFileCredentials | None
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
    return TokenFileCredentials(access_token=access_token, client_id=client_id)


def resolve_token_file_credentials(
    env: Mapping[str, str] | None = None, *, path: Path | None = None
) -> TokenFileCredentials:
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
    TokenFileCredentials
        The resolved credentials.

    Raises
    ------
    CredentialsError
        If either field cannot be resolved from env or file. The message
        names WHICH field(s) are missing and how to set them — never a
        value. Also propagated from :func:`load_token_credentials` for an
        insecure / corrupt file.
    """
    source = os.environ if env is None else env
    stored = load_token_credentials(path=path)

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
        raise CredentialsError(f"missing credentials: {', '.join(missing)}; {_HOW_TO_SET}")
    # Canonicalise at the resolution boundary (DEC-CREDS-NORMALISE-001) so the
    # auth layer always gets a clean ``access-token`` header value — whether the
    # value came from the saved file OR a ``BRUTEX_ACCESS_TOKEN`` / ``BRUTEX_CLIENT_ID``
    # env var (an exported value can carry the same paste whitespace). Idempotent
    # on clean values.
    return TokenFileCredentials(
        access_token=normalise_token(access_token),
        client_id=normalise_token(client_id),
    )


def credentials_present(env: Mapping[str, str] | None = None, *, path: Path | None = None) -> bool:
    """Return ``True`` iff :func:`resolve_token_file_credentials` would succeed.

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
        resolve_token_file_credentials(env, path=path)
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
    """Return a NON-SECRET preview of the stored token-style credentials for the operator.

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
        creds = resolve_token_file_credentials(env, path=path)
    except CredentialsError:
        return {"present": False}
    return {
        "present": True,
        "access_token_preview": _mask_secret(creds.access_token, head=6, tail=4),
        "access_token_len": len(creds.access_token),
        "client_id_preview": _mask_secret(creds.client_id, head=2, tail=2),
        "client_id_len": len(creds.client_id),
    }


def clear_token_credentials(*, path: Path | None = None) -> bool:
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


def save_groww_credentials(creds: GrowwFileCredentials, *, path: Path | None = None) -> Path:
    """Atomically write Groww *creds* into the ``groww`` section (mode 0600).

    A **read-merge-write** (:func:`_save_section`): the existing file is read
    first so a stored ``token`` section is preserved, then only the ``groww``
    section is replaced and the whole object written atomically — saving Groww
    credentials never drops the legacy section (DEC-DHAN-GROWW-REMIGRATE-001).

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
        If ``api_key`` or ``totp_secret`` is blank (after strip). The message
        names the field, never its value.
    """
    if not creds.api_key.strip():
        raise CredentialsError("api_key must not be blank")
    if not creds.totp_secret.strip():
        raise CredentialsError("totp_secret must not be blank")

    api_key = normalise_token(creds.api_key)
    totp_secret = normalise_token(creds.totp_secret)

    target = credentials_path() if path is None else path
    _save_section(target, _GROWW_SECTION, {"api_key": api_key, "totp_secret": totp_secret})
    return target


def load_groww_credentials(*, path: Path | None = None) -> GrowwFileCredentials | None:
    """Load the stored Groww credentials, or ``None`` when the file is absent.

    Parameters
    ----------
    path
        File to read. Defaults to :func:`credentials_path`.

    Returns
    -------
    GrowwFileCredentials | None
        The stored credentials, ``None`` when *path* does not exist OR exists
        but has no ``groww`` section (a legacy-only file). A present-but-broken
        ``groww`` section (blank field) is surfaced, NOT silently ``None``.

    Raises
    ------
    CredentialsError
        If the file is group / world accessible, a symlink, corrupt /
        wrong-shape, or its ``groww`` section is the wrong shape / has a blank
        field.
    """
    target = credentials_path() if path is None else path
    raw = _read_raw_sections(target)
    if not raw:
        return None
    section = raw.get(_GROWW_SECTION)
    if section is None:
        return None
    if not isinstance(section, dict):
        raise CredentialsError(
            f"credentials file {target} has an invalid '{_GROWW_SECTION}' section"
        )
    api_key = section.get("api_key")
    totp_secret = section.get("totp_secret")
    if not isinstance(api_key, str) or not api_key.strip():
        raise CredentialsError(f"credentials file {target} has a missing or blank api_key")
    if not isinstance(totp_secret, str) or not totp_secret.strip():
        raise CredentialsError(f"credentials file {target} has a missing or blank totp_secret")
    return GrowwFileCredentials(api_key=api_key, totp_secret=totp_secret)


def _resolve_aws_credentials(
    aws_loader: Callable[[], GrowwFileCredentials | None] | None,
) -> GrowwFileCredentials | None:
    """Resolve the AWS Parameter-Store creds tier, or ``None`` when unconfigured.

    Uses *aws_loader* when injected (tests), else the real lazy-boto3 loader
    :func:`~brutex.config.credentials_aws.load_groww_aws_credentials`, imported HERE (not
    at module top) so a purely-local resolve never imports the AWS module. The loader
    itself never raises + returns ``None`` when unconfigured, so this is a thin wrapper.
    """
    if aws_loader is not None:
        return aws_loader()
    from brutex.config.credentials_aws import load_groww_aws_credentials

    return load_groww_aws_credentials()


def resolve_groww_file_credentials(
    env: Mapping[str, str] | None = None,
    *,
    path: Path | None = None,
    aws_loader: Callable[[], GrowwFileCredentials | None] | None = None,
) -> GrowwFileCredentials:
    """Resolve Groww credentials per-field: ``env → AWS → file``.

    Each field is taken from its env var (:data:`ENV_GROWW_API_KEY` /
    :data:`ENV_GROWW_TOTP_SECRET`) when non-blank, else from the SHARED AWS
    Parameter-Store creds (DEC-TOKEN-BROKER-AWS-001 — so a key rotation
    propagates to both BruteX and TickVault), else from the saved file. The AWS
    tier returns ``None`` when the read-only broker is not enabled (no
    ``BRUTEX_TOKEN_BROKER_READONLY`` / boto3 absent / unreachable), so the existing
    env→file path is byte-identical whenever AWS is not in use.

    Parameters
    ----------
    env
        Environment mapping. Defaults to :data:`os.environ`.
    path
        Credentials file. Defaults to :func:`credentials_path`.
    aws_loader
        Injectable AWS-creds reader (default:
        :func:`~brutex.config.credentials_aws.load_groww_aws_credentials`). Tests
        pass a fake; production takes the real lazy-boto3 loader.

    Returns
    -------
    GrowwFileCredentials
        The resolved credentials (whitespace-normalised).

    Raises
    ------
    CredentialsError
        If either field cannot be resolved from env, AWS, or file. The message
        names WHICH field(s) are missing and how to set them — never a value.
    """
    source = os.environ if env is None else env

    env_api_key = source.get(ENV_GROWW_API_KEY)
    env_totp = source.get(ENV_GROWW_TOTP_SECRET)
    env_has_api_key = env_api_key is not None and bool(env_api_key.strip())
    env_has_totp = env_totp is not None and bool(env_totp.strip())

    stored = load_groww_credentials(path=path)
    # Short-circuit the AWS tier (a hostile-review MINOR fold): when env already
    # satisfies BOTH fields, skip the SSM round-trips entirely — the AWS values would be
    # discarded anyway (env wins). Only consult AWS when a field is still open.
    aws = None if env_has_api_key and env_has_totp else _resolve_aws_credentials(aws_loader)

    api_key: str | None = None
    if env_has_api_key:
        api_key = env_api_key
    elif aws is not None:
        api_key = aws.api_key
    elif stored is not None:
        api_key = stored.api_key

    totp_secret: str | None = None
    if env_has_totp:
        totp_secret = env_totp
    elif aws is not None:
        totp_secret = aws.totp_secret
    elif stored is not None:
        totp_secret = stored.totp_secret

    missing: list[str] = []
    if api_key is None:
        missing.append(ENV_GROWW_API_KEY)
    if totp_secret is None:
        missing.append(ENV_GROWW_TOTP_SECRET)
    if api_key is None or totp_secret is None:
        raise CredentialsError(
            f"missing Groww credentials: {', '.join(missing)}; {_GROWW_HOW_TO_SET}"
        )
    return GrowwFileCredentials(
        api_key=normalise_token(api_key),
        totp_secret=normalise_token(totp_secret),
    )


def groww_credentials_present(
    env: Mapping[str, str] | None = None, *, path: Path | None = None
) -> bool:
    """Return ``True`` iff :func:`resolve_groww_file_credentials` would succeed.

    Returns ``False`` on any :class:`CredentialsError` (including an
    insecure-permissions file) so a status surface degrades gracefully.
    """
    try:
        resolve_groww_file_credentials(env, path=path)
    except CredentialsError:
        return False
    return True


def groww_credentials_preview(
    env: Mapping[str, str] | None = None, *, path: Path | None = None
) -> dict[str, object]:
    """Return a NON-SECRET masked preview of the stored Groww credentials.

    Mirrors :func:`credentials_preview`: a masked ``head…tail`` preview + length
    of each resolved field so the paste can be VERIFIED at a glance, never the
    raw secret.

    Returns
    -------
    dict[str, object]
        ``{"present": False}`` when credentials do not resolve, else
        ``{"present": True, "api_key_preview", "api_key_len",
        "totp_secret_preview", "totp_secret_len"}``.
    """
    try:
        creds = resolve_groww_file_credentials(env, path=path)
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
    """Remove only the ``groww`` section from the credentials file.

    Unlike :func:`clear_token_credentials` (which deletes the whole single-vendor
    file), this is a read-merge-write that drops only the ``groww`` section so a
    coexisting ``token`` section is preserved.

    Parameters
    ----------
    path
        File to edit. Defaults to :func:`credentials_path`.

    Returns
    -------
    bool
        ``True`` if a ``groww`` section was present and removed, ``False`` if the
        file was absent or had no ``groww`` section.
    """
    target = credentials_path() if path is None else path
    raw = _read_raw_sections(target)
    if _GROWW_SECTION not in raw:
        return False
    del raw[_GROWW_SECTION]
    _atomic_write(target, json.dumps(raw))
    return True


__all__ = [
    "ENV_ACCESS_TOKEN",
    "ENV_CLIENT_ID",
    "ENV_CREDENTIALS_FILE",
    "ENV_GROWW_API_KEY",
    "ENV_GROWW_TOTP_SECRET",
    "CredentialsError",
    "GrowwFileCredentials",
    "TokenFileCredentials",
    "clear_groww_credentials",
    "clear_token_credentials",
    "credentials_path",
    "credentials_present",
    "credentials_preview",
    "groww_credentials_present",
    "groww_credentials_preview",
    "load_groww_credentials",
    "load_token_credentials",
    "normalise_token",
    "resolve_groww_file_credentials",
    "resolve_token_file_credentials",
    "save_groww_credentials",
    "save_token_credentials",
    "token_shape_problem",
]
