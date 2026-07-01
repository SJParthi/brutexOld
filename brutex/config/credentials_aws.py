"""AWS Parameter-Store credential reader — the SOLE source of the Groww credentials.

DEC-AWS-ONLY-CREDS-001 (operator's FINAL design, 2026-07-01, stacked on
DEC-TOKEN-BROKER-AWS-001). The Groww ``api_key`` + TOTP secret live in AWS Systems
Manager Parameter Store as SecureStrings — ``/tickvault/prod/groww/api-key`` +
``/tickvault/prod/groww/totp-secret`` (region ``ap-south-1``) — so a key rotation
propagates to BOTH BruteX and TickVault at once (not just one machine's
``~/.brutex/credentials.json``). This module is the single reader; the in-app credentials
webpage AND the local env-var / ``credentials.json`` file paths were REMOVED, so AWS is now
the ONLY credential source (no env fallback, no file fallback, no webpage).

Design (fail-fast — the opposite of the opt-in graceful-fallback design of #1015):

* **NOT gated on any flag.** AWS is the default AND only source. Reading is mandatory.
* **Fail-fast, no silent fallback.** A missing boto3 extra, an absent/blank parameter, or
  ANY SSM/network/IAM/KMS error raises a CLEAR :class:`~brutex.config.credentials_types.\
  CredentialsError` telling the operator exactly what to fix — it NEVER returns ``None`` and
  NEVER falls back to env vars, a local file, or the removed webpage (Golden Rule #1: no
  silent fabrication of a credential source).
* **Leaf-level shared types.** :class:`CredentialsError` + :class:`GrowwFileCredentials` are
  imported from the dependency-free leaf :mod:`brutex.config.credentials_types` (which imports
  neither this module nor :mod:`credentials`), so this reader is a lower-level PRIMITIVE that
  :mod:`credentials` imports one-directionally — NO module-level import cycle
  (DEC-AWS-ONLY-CREDS-001; the earlier bidirectional import was flagged by CodeQL).
* **Both fields or a loud error.** A partial fetch (api_key present, totp_secret absent)
  raises — never a mixed / half-resolved credential.
* **Injectable client for tests.** The real boto3 client build is confined to a
  ``# pragma: no cover`` factory; a deterministic in-memory fake
  (``tests/fakes/fake_aws_client.py``) drives every branch to 100% line+branch with NO
  boto3 install and NO network.

A read is a plain ``GetParameter WithDecryption=True`` — AWS KMS-decrypts the SecureString
server-side (AWS SSM docs "Working with SecureString parameters"; ``GetParameter`` API
reference). BruteX only READS these parameters (never writes them — a dedicated Lambda /
the operator owns writing them).
"""

from __future__ import annotations

import structlog

from brutex.adapters.groww.aws_client import (
    SSM_PARAMETER_NOT_FOUND,
    AwsClient,
    build_real_aws_client,
)
from brutex.adapters.groww.constants import (
    GROWW_TOKEN_PARAM_API_KEY_DEFAULT,
    GROWW_TOKEN_PARAM_API_KEY_ENV,
    GROWW_TOKEN_PARAM_TOTP_SECRET_DEFAULT,
    GROWW_TOKEN_PARAM_TOTP_SECRET_ENV,
    resolve_token_broker_region,
    resolve_token_param_path,
)
from brutex.config.credentials_types import CredentialsError, GrowwFileCredentials

_LOG = structlog.get_logger("brutex.config.credentials_aws")

#: How to fix an AWS credential-resolution failure, appended to the raised error. Names
#: the fixed default parameter paths + the deploy pointer; contains NO secret values.
AWS_CREDS_HOW_TO_FIX: str = (
    "seed the Groww credentials in AWS Parameter Store "
    "(/tickvault/prod/groww/api-key + /tickvault/prod/groww/totp-secret, SecureStrings "
    "in ap-south-1) — deploy the minter (deploy/aws_token_minter/deploy.sh) first; "
    "BruteX reads credentials ONLY from AWS (no env-var / local-file / webpage fallback)"
)


def load_groww_aws_credentials(*, client: AwsClient | None = None) -> GrowwFileCredentials:
    """Load the Groww credentials from AWS Parameter Store — the SOLE source.

    Reads ``/tickvault/prod/groww/api-key`` + ``/tickvault/prod/groww/totp-secret`` (both
    env-overridable path names) as SecureStrings (``WithDecryption=True``). This is
    MANDATORY and FAIL-FAST: any failure — boto3 missing, either parameter absent/blank, or
    ANY SSM/network/IAM/KMS error — raises :class:`~brutex.config.credentials_types.CredentialsError`
    with a clear, secret-free fix message. It NEVER returns ``None`` and NEVER falls back to
    an env var, a local file, or the removed webpage (DEC-AWS-ONLY-CREDS-001).

    Parameters
    ----------
    client
        Injected :class:`~brutex.adapters.groww.aws_client.AwsClient` for tests (a fake).
        ``None`` builds the real boto3 client lazily (``# pragma: no cover``).

    Returns
    -------
    GrowwFileCredentials
        The resolved credentials (both fields non-blank).

    Raises
    ------
    CredentialsError
        When AWS is unreachable / not configured, boto3 is missing, or either parameter is
        absent or blank. The message names the parameter paths + the deploy pointer, never a
        secret value.
    """
    resolved_client = client if client is not None else _build_client_or_raise()

    api_key = _read_param(
        resolved_client,
        resolve_token_param_path(GROWW_TOKEN_PARAM_API_KEY_ENV, GROWW_TOKEN_PARAM_API_KEY_DEFAULT),
    )
    totp_secret = _read_param(
        resolved_client,
        resolve_token_param_path(
            GROWW_TOKEN_PARAM_TOTP_SECRET_ENV, GROWW_TOKEN_PARAM_TOTP_SECRET_DEFAULT
        ),
    )
    missing: list[str] = []
    if api_key is None:
        missing.append(
            resolve_token_param_path(
                GROWW_TOKEN_PARAM_API_KEY_ENV, GROWW_TOKEN_PARAM_API_KEY_DEFAULT
            )
        )
    if totp_secret is None:
        missing.append(
            resolve_token_param_path(
                GROWW_TOKEN_PARAM_TOTP_SECRET_ENV, GROWW_TOKEN_PARAM_TOTP_SECRET_DEFAULT
            )
        )
    if api_key is None or totp_secret is None:
        raise CredentialsError(
            f"missing Groww credentials in AWS Parameter Store: {', '.join(missing)}; "
            f"{AWS_CREDS_HOW_TO_FIX}"
        )

    return GrowwFileCredentials(api_key=api_key, totp_secret=totp_secret)


def _build_client_or_raise() -> AwsClient:  # pragma: no cover — real boto3 client build
    """Build the real boto3 :class:`AwsClient`, raising a clear error when it cannot be built.

    A missing boto3 (:class:`~brutex.adapters.groww.aws_client.AwsBackendUnavailable`, an
    ``ImportError``) OR any boto3-client construction error — a bad region, an absent
    profile (``ProfileNotFound``), a credential-chain failure — is FAIL-FAST: it raises
    :class:`~brutex.config.credentials_types.CredentialsError` naming the fix, never a silent
    fallback to env/file (DEC-AWS-ONLY-CREDS-001).
    """
    try:
        return build_real_aws_client(resolve_token_broker_region())
    except Exception as exc:  # pylint: disable=broad-exception-caught — any build failure is fail-fast
        _LOG.error("groww.creds.aws_unavailable", error_type=type(exc).__name__)
        raise CredentialsError(
            f"could not reach AWS Parameter Store for the Groww credentials "
            f"({type(exc).__name__}); {AWS_CREDS_HOW_TO_FIX}"
        ) from exc


def _read_param(client: AwsClient, name: str) -> str | None:
    """Read one SecureString parameter, returning its non-blank value or ``None``.

    A ``ParameterNotFound`` (matched by exception class name — no botocore import) or an
    absent/blank value returns ``None`` (the caller raises a coherent all-fields error). ANY
    OTHER SSM/network/IAM/KMS error is FAIL-FAST — it raises
    :class:`~brutex.config.credentials_types.CredentialsError` (a redacting log, never the
    value), since a transient/denied AWS read must not be mistaken for "credential absent".
    """
    try:
        value = client.get_parameter(name=name, with_decryption=True)
    except Exception as exc:  # pylint: disable=broad-exception-caught — classify absent vs hard failure
        reason = type(exc).__name__
        if reason == SSM_PARAMETER_NOT_FOUND:
            _LOG.debug("groww.creds.aws_param_absent", name=name)
            return None
        _LOG.error("groww.creds.aws_read_failed", error_type=reason)
        raise CredentialsError(
            f"failed to read the Groww credential parameter {name} from AWS "
            f"({reason}); {AWS_CREDS_HOW_TO_FIX}"
        ) from exc
    if value is None or not value.strip():
        return None
    return value


__all__ = ["AWS_CREDS_HOW_TO_FIX", "load_groww_aws_credentials"]
