"""AWS Parameter-Store reader for the READ-ONLY Dhan credentials — the SOLE source.

DEC-DHAN-READONLY-AUTH-001 (ADR-0022). The Dhan read path needs exactly two
strings — ``client_id`` (public account id) + ``access_token`` (24-h JWT, a
SecureString) — both stored in AWS Systems Manager Parameter Store and MINTED by a
dedicated Lambda / TickVault. BruteX is a strictly READ-ONLY consumer: it only
``GetParameter``s these and NEVER mints / renews / ``PutParameter``s (a stray write
could invalidate the token TickVault is actively using — the single-active-token
hazard, CLAUDE.md §6 Dhan row).

This is the single reader (the direct mirror of
:func:`~brutex.config.credentials_aws.load_groww_aws_credentials`):

* **Fail-fast, no silent fallback.** A missing boto3 extra, an absent/blank
  parameter, or ANY SSM/network/IAM/KMS error raises a clear
  :class:`~brutex.config.credentials_types.CredentialsError` naming the fix — never
  ``None``, never an env / file fallback (Golden Rule #1).
* **Read-only primitives reused.** The narrow read-only :class:`AwsClient` Protocol
  + region/path resolvers + the boto3 client factory come from
  :mod:`brutex.adapters.groww.aws_client` / ``groww.constants`` (present on
  ``origin/main``, stdlib-only leaves); NO writer seam is imported (no
  ``ssm:PutParameter`` grant — Groww parity).
* **Both fields or a loud error.** A partial fetch (client-id present, token absent)
  raises — never a half-resolved credential.
* **Injectable client for tests.** The real boto3 client build is confined to a
  ``# pragma: no cover`` factory; the deterministic ``tests/fakes/fake_aws_client.py``
  drives every branch to 100% line+branch with NO boto3 install and NO network.
"""

from __future__ import annotations

import structlog

from brutex.adapters.dhan.constants import (
    DHAN_TOKEN_PARAM_ACCESS_TOKEN_DEFAULT,
    DHAN_TOKEN_PARAM_ACCESS_TOKEN_ENV,
    DHAN_TOKEN_PARAM_CLIENT_ID_DEFAULT,
    DHAN_TOKEN_PARAM_CLIENT_ID_ENV,
)
from brutex.adapters.groww.aws_client import (
    SSM_PARAMETER_NOT_FOUND,
    AwsClient,
    build_real_aws_client,
)
from brutex.adapters.groww.constants import (
    resolve_token_broker_region,
    resolve_token_param_path,
)
from brutex.config.credentials_types import CredentialsError, DhanCredentials

_LOG = structlog.get_logger("brutex.config.credentials_dhan_aws")

#: How to fix a Dhan credential-resolution failure, appended to the raised error.
#: Names both parameter paths + the minter pointer; contains NO secret values.
DHAN_CREDS_HOW_TO_FIX: str = (
    "seed the Dhan credentials in AWS Parameter Store "
    "(/tickvault/prod/dhan/client-id [String] + /tickvault/prod/dhan/access-token "
    "[SecureString, 24-h JWT], in the token-broker region) — the minter "
    "(Lambda / TickVault) owns the token lifecycle; BruteX reads them ONLY from AWS "
    "and NEVER mints (no env-var / local-file / webpage fallback)"
)


def load_dhan_aws_credentials(*, client: AwsClient | None = None) -> DhanCredentials:
    """Load the READ-ONLY Dhan credentials from AWS Parameter Store — the SOLE source.

    Reads ``/tickvault/prod/dhan/client-id`` + ``/tickvault/prod/dhan/access-token``
    (both env-overridable path names) as SecureStrings (``WithDecryption=True`` — a
    ``String`` decrypts to itself). MANDATORY + FAIL-FAST: any failure — boto3
    missing, either parameter absent/blank, or ANY SSM/network/IAM/KMS error — raises
    :class:`~brutex.config.credentials_types.CredentialsError` with a clear,
    secret-free fix message. It NEVER returns ``None`` and NEVER falls back to an env
    var / local file (DEC-DHAN-READONLY-AUTH-001).

    Parameters
    ----------
    client
        Injected :class:`~brutex.adapters.groww.aws_client.AwsClient` for tests (a
        fake). ``None`` builds the real boto3 client lazily (``# pragma: no cover``).

    Returns
    -------
    DhanCredentials
        The resolved credentials (both fields non-blank).

    Raises
    ------
    CredentialsError
        When AWS is unreachable / not configured, boto3 is missing, or either
        parameter is absent or blank. The message names the parameter paths + the
        minter pointer, never a secret value.
    """
    resolved_client = client if client is not None else _build_client_or_raise()

    client_id_path = resolve_token_param_path(
        DHAN_TOKEN_PARAM_CLIENT_ID_ENV, DHAN_TOKEN_PARAM_CLIENT_ID_DEFAULT
    )
    access_token_path = resolve_token_param_path(
        DHAN_TOKEN_PARAM_ACCESS_TOKEN_ENV, DHAN_TOKEN_PARAM_ACCESS_TOKEN_DEFAULT
    )
    client_id = _read_param(resolved_client, client_id_path)
    access_token = _read_param(resolved_client, access_token_path)
    missing: list[str] = []
    if client_id is None:
        missing.append(client_id_path)
    if access_token is None:
        missing.append(access_token_path)
    if client_id is None or access_token is None:
        raise CredentialsError(
            f"missing Dhan credentials in AWS Parameter Store: {', '.join(missing)}; "
            f"{DHAN_CREDS_HOW_TO_FIX}"
        )
    return DhanCredentials(client_id=client_id, access_token=access_token)


def _build_client_or_raise() -> AwsClient:  # pragma: no cover — real boto3 client build
    """Build the real boto3 :class:`AwsClient`, raising a clear error when it cannot be built.

    A missing boto3 (:class:`~brutex.adapters.groww.aws_client.AwsBackendUnavailable`,
    an ``ImportError``) OR any boto3-client construction error is FAIL-FAST: it raises
    :class:`~brutex.config.credentials_types.CredentialsError` naming the fix, never a
    silent fallback (DEC-DHAN-READONLY-AUTH-001).
    """
    try:
        return build_real_aws_client(resolve_token_broker_region())
    except Exception as exc:  # pylint: disable=broad-exception-caught — any build failure is fail-fast
        _LOG.error("dhan.creds.aws_unavailable", error_type=type(exc).__name__)
        raise CredentialsError(
            f"could not reach AWS Parameter Store for the Dhan credentials "
            f"({type(exc).__name__}); {DHAN_CREDS_HOW_TO_FIX}"
        ) from exc


def _read_param(client: AwsClient, name: str) -> str | None:
    """Read one SecureString parameter, returning its non-blank value or ``None``.

    A ``ParameterNotFound`` (matched by exception class name — no botocore import) or
    an absent/blank value returns ``None`` (the caller raises a coherent all-fields
    error). ANY OTHER SSM/network/IAM/KMS error is FAIL-FAST — it raises
    :class:`~brutex.config.credentials_types.CredentialsError` (a redacting log, never
    the value), since a transient/denied AWS read must not be mistaken for
    "credential absent". Mirrors ``credentials_aws._read_param`` exactly.
    """
    try:
        value = client.get_parameter(name=name, with_decryption=True)
    except Exception as exc:  # pylint: disable=broad-exception-caught — classify absent vs hard failure
        reason = type(exc).__name__
        if reason == SSM_PARAMETER_NOT_FOUND:
            _LOG.debug("dhan.creds.aws_param_absent", name=name)
            return None
        _LOG.error("dhan.creds.aws_read_failed", error_type=reason)
        raise CredentialsError(
            f"failed to read the Dhan credential parameter {name} from AWS "
            f"({reason}); {DHAN_CREDS_HOW_TO_FIX}"
        ) from exc
    if value is None or not value.strip():
        return None
    return value


__all__ = ["DHAN_CREDS_HOW_TO_FIX", "load_dhan_aws_credentials"]
