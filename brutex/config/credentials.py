"""Credentials store for the vendor adapter (DEC-CREDENTIALS-STORE-001).

**Groww credentials now live ONLY in AWS Parameter Store (DEC-AWS-ONLY-CREDS-001).** The
in-app credentials webpage AND the local env-var / ``credentials.json`` file paths for the
Groww ``api_key`` + TOTP secret were REMOVED — :func:`resolve_groww_aws_credentials` reads
them from AWS as the SOLE source (fail-fast, no env / file / webpage fallback). Only the
legacy **token-style** access-token + client-id path (``brutex auth set``) still uses the
on-disk file below; it is orthogonal to the Groww pull.

The operator sets the legacy token-style **access token** + **client id** ONCE — via
``brutex auth set`` or the ``BRUTEX_ACCESS_TOKEN`` / ``BRUTEX_CLIENT_ID`` env vars — and
they auto-load every session, satisfying ``CLAUDE.md §11`` AUTOMATED (no ``export`` per
shell, zero clicks per day).

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

# RETIRED names, re-exported for back-compat only (DEC-AWS-ONLY-CREDS-001: no code reads
# these env vars for auth — credentials are AWS-SSM-only, `resolve_groww_aws_credentials`).
from brutex.adapters.groww.auth import ENV_API_KEY as ENV_GROWW_API_KEY
from brutex.adapters.groww.auth import ENV_TOTP_SECRET as ENV_GROWW_TOTP_SECRET
from brutex.config.credentials_types import CredentialsError, GrowwFileCredentials
from brutex.data.atomic_io import atomic_write_simple

#: Env var that overrides the credentials file location (path, not a secret).
ENV_CREDENTIALS_FILE: Final[str] = "BRUTEX_CREDENTIALS_FILE"

#: Env var holding the access token (the env var NAME, not the value).
ENV_ACCESS_TOKEN: Final[str] = "BRUTEX_ACCESS_TOKEN"  # noqa: S105  # env var name, not a secret

#: Env var holding the client id (env var NAME, not the value).
ENV_CLIENT_ID: Final[str] = "BRUTEX_CLIENT_ID"

#: Permission bits that, if set, mean the file is group / world accessible.
_INSECURE_MODE_BITS: Final[int] = 0o077

#: JSON top-level section the token-style credentials live under. Only the legacy
#: token-style (``brutex auth set``) path still uses the on-disk file; the Groww
#: credentials moved to AWS Parameter Store (DEC-AWS-ONLY-CREDS-001) and no longer touch
#: this file.
_SECTION: Final[str] = "token"

#: How to set the token-style credentials, appended to "missing field" errors. Names the
#: supported mechanisms; contains NO secret values.
_HOW_TO_SET: Final[str] = "run `brutex auth set`, or export BRUTEX_ACCESS_TOKEN / BRUTEX_CLIENT_ID"


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

    The READ half of the token-style save's read-merge-write: it reads the existing file
    first so a re-save preserves any coexisting section (the Groww section that once lived
    here is GONE — Groww creds moved to AWS, DEC-AWS-ONLY-CREDS-001 — but the read-merge
    contract is kept generic + robust). Mirrors the security posture of
    :func:`load_token_credentials`: ``O_NOFOLLOW`` open + insecure-permission refusal on the
    same fd (TOCTOU-safe). A corrupt / wrong-shape existing file is refused rather than
    silently overwritten, so a save can never destroy an unreadable-but-present file's
    contents.

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
    return raw


def _atomic_write(target: Path, payload: str) -> None:
    """Atomically write *payload* to *target* at mode 0600 (parent dir 0700).

    The WRITE half of the token-style save's read-merge-write. Delegates to the COMMON
    guard-free
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
    preserves any other section already present (a generic, robust read-merge contract).
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
    file is read first so any coexisting section is preserved, then only the
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

    Surfaces (e.g. via ``brutex auth status``) a masked ``head…tail`` preview +
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


def resolve_groww_aws_credentials(
    *,
    aws_loader: Callable[[], GrowwFileCredentials] | None = None,
) -> GrowwFileCredentials:
    """Resolve the Groww credentials from AWS Parameter Store — the SOLE source.

    AWS is now the default AND only credential source (DEC-AWS-ONLY-CREDS-001): the in-app
    credentials webpage AND the local env-var / ``credentials.json`` file paths were
    REMOVED. This is FAIL-FAST — a missing/unreachable AWS parameter raises
    :class:`CredentialsError` with a clear fix message; it NEVER falls back to an env var, a
    local file, or the removed webpage.

    Parameters
    ----------
    aws_loader
        Injectable AWS-creds reader (default:
        :func:`~brutex.config.credentials_aws.load_groww_aws_credentials`). Tests pass a
        fake (which injects a fake SSM client) so CI needs NO real AWS; production takes the
        real lazy-boto3 loader.

    Returns
    -------
    GrowwFileCredentials
        The resolved credentials (whitespace-normalised).

    Raises
    ------
    CredentialsError
        When AWS is unreachable / not configured, boto3 is missing, or either credential
        parameter is absent or blank. The message names the parameter paths + the deploy
        pointer — never a secret value.
    """
    if aws_loader is not None:
        creds = aws_loader()
    else:
        # Function-local so a purely-local import of this module never pulls in the AWS
        # reader chain (aws_client / constants). The import is ONE-DIRECTIONAL —
        # credentials_aws imports the leaf credentials_types, NOT this module — so there is
        # no module-level cycle (DEC-AWS-ONLY-CREDS-001; the shared types live in the leaf).
        from brutex.config.credentials_aws import load_groww_aws_credentials

        creds = load_groww_aws_credentials()
    return GrowwFileCredentials(
        api_key=normalise_token(creds.api_key),
        totp_secret=normalise_token(creds.totp_secret),
    )


def groww_credentials_error(
    *, aws_loader: Callable[[], GrowwFileCredentials] | None = None
) -> str | None:
    """Return ``None`` when the Groww AWS credentials resolve, else the fix text.

    The LOUD twin of :func:`groww_credentials_present`
    (DEC-PULL-LOUD-CREDENTIAL-REFUSAL-001). The bool gate swallows the
    :class:`CredentialsError` to a bare ``False``, which DISCARDS the error's own
    actionable fix message (which parameter/env var is wrong + what to do) — the
    swallow that let the dashboard silently downgrade a REAL pull to a synthetic
    dry-run on ANY credential failure. This function preserves that text VERBATIM
    (never re-derived — CLAUDE.md §11 COMMON) so every REAL pull surface can
    refuse loudly with the exact fix. The message is secret-free by the loader's
    contract (it names parameter paths, never values).

    Parameters
    ----------
    aws_loader
        Injectable AWS-creds reader (default: the real lazy-boto3 loader). Tests
        pass a fake so CI needs NO real AWS.

    Returns
    -------
    str | None
        ``None`` when :func:`resolve_groww_aws_credentials` succeeds; otherwise
        the raised :class:`CredentialsError`'s own message — AWS unreachable,
        boto3 missing, or a parameter absent/blank.
    """
    try:
        resolve_groww_aws_credentials(aws_loader=aws_loader)
    except CredentialsError as exc:
        return str(exc)
    return None


def groww_credentials_present(
    *, aws_loader: Callable[[], GrowwFileCredentials] | None = None
) -> bool:
    """Return ``True`` iff :func:`resolve_groww_aws_credentials` would succeed.

    Resolves the Groww credentials from AWS Parameter Store (the SOLE source,
    DEC-AWS-ONLY-CREDS-001); returns ``False`` on any :class:`CredentialsError` — AWS
    unreachable, boto3 missing, or a parameter absent/blank — so a STATUS surface
    degrades gracefully instead of raising. It is a status/display gate ONLY: a
    REAL pull decision must use :func:`groww_credentials_error` (via
    :func:`brutex.adapters.ingest_common.gate_pull_credentials`) so the failure's
    own fix text is relayed instead of discarded
    (DEC-PULL-LOUD-CREDENTIAL-REFUSAL-001 — never a silent synthetic downgrade).

    Parameters
    ----------
    aws_loader
        Injectable AWS-creds reader (default: the real lazy-boto3 loader). Tests pass a fake
        so CI needs NO real AWS.
    """
    return groww_credentials_error(aws_loader=aws_loader) is None


__all__ = [
    "ENV_ACCESS_TOKEN",
    "ENV_CLIENT_ID",
    "ENV_CREDENTIALS_FILE",
    "ENV_GROWW_API_KEY",  # RETIRED name (DEC-AWS-ONLY-CREDS-001) — back-compat export only
    "ENV_GROWW_TOTP_SECRET",  # RETIRED name (DEC-AWS-ONLY-CREDS-001) — back-compat export only
    "CredentialsError",
    "GrowwFileCredentials",
    "TokenFileCredentials",
    "clear_token_credentials",
    "credentials_path",
    "credentials_present",
    "credentials_preview",
    "groww_credentials_error",
    "groww_credentials_present",
    "load_token_credentials",
    "normalise_token",
    "resolve_groww_aws_credentials",
    "resolve_token_file_credentials",
    "save_token_credentials",
    "token_shape_problem",
]
