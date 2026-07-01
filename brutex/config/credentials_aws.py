"""AWS Parameter-Store credential reader — the middle tier of ``env → AWS → file``.

DEC-TOKEN-BROKER-AWS-001 (operator's FINAL design, 2026-07-01). So a key rotation
propagates to BOTH BruteX and TickVault (not just one machine's
``~/.brutex/credentials.json``), the Groww ``api_key`` + TOTP secret live in AWS Systems
Manager Parameter Store as SecureStrings — ``/tickvault/prod/groww/api-key`` +
``/tickvault/prod/groww/totp-secret`` (region ``ap-south-1``). This module is the single
reader; :func:`~brutex.config.credentials.resolve_groww_file_credentials` slots it in as
the middle tier — ABOVE the local file, BELOW an explicit env var — returning ``None`` when
the shared broker is not enabled so the existing env→file path is byte-identical.

Design (mirrors the read-only token store):

* **Gated on ``BRUTEX_TOKEN_BROKER_READONLY``.** Unset/blank/falsey ⇒ return ``None`` (a
  no-op — env→file only, byte-identical to today, so local dev + CI need NO AWS).
* **Lazy boto3, confined to a ``# pragma: no cover`` factory.** A local run that never
  enables the broker imports ZERO AWS code; a missing boto3 with the flag set logs
  ``groww.creds.aws_unavailable`` → ``None`` (never breaks the file/env path).
* **NEVER raises into the resolver.** Every SSM/network/``ParameterNotFound``/KMS error is
  caught → logged (no secret value) → ``None``, so an unreachable / IAM-denied AWS degrades
  to the local file, never a crash (Golden Rule #10).
* **Both fields or nothing.** A partial fetch (api_key present, totp_secret absent) is a
  ``None`` return so the resolver falls to the file for a COHERENT pair — never a mixed
  AWS-key/file-secret credential.

A read is a plain ``GetParameter WithDecryption=True`` — AWS KMS-decrypts the SecureString
server-side (AWS SSM docs "Working with SecureString parameters"; ``GetParameter`` API
reference). BruteX only READS these parameters (never writes them).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

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
    resolve_token_broker_readonly,
    resolve_token_broker_region,
    resolve_token_param_path,
)

if TYPE_CHECKING:
    from brutex.config.credentials import GrowwFileCredentials

_LOG = structlog.get_logger("brutex.config.credentials_aws")


def load_groww_aws_credentials(*, client: AwsClient | None = None) -> GrowwFileCredentials | None:
    """Load the Groww creds from Parameter Store, or ``None`` when disabled/unavailable.

    Reads ``/tickvault/prod/groww/api-key`` + ``/tickvault/prod/groww/totp-secret`` (both
    env-overridable) as SecureStrings (``WithDecryption=True``). Returns ``None`` — so the
    resolver falls to the local file — when: the read-only broker is not enabled
    (``BRUTEX_TOKEN_BROKER_READONLY`` unset/falsey); boto3 is missing; either parameter is
    absent/blank; or ANY SSM/network/KMS error occurs. NEVER raises.

    Parameters
    ----------
    client
        Injected :class:`~brutex.adapters.groww.aws_client.AwsClient` for tests (a fake).
        ``None`` builds the real boto3 client lazily (``# pragma: no cover``) when the
        broker is enabled.
    """
    if not resolve_token_broker_readonly():
        return None

    resolved_client = client if client is not None else _build_client_or_none()
    if resolved_client is None:
        return None

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
    if api_key is None or totp_secret is None:
        return None

    # Lazy import to avoid an import cycle (credentials.py imports this module).
    from brutex.config.credentials import GrowwFileCredentials

    return GrowwFileCredentials(api_key=api_key, totp_secret=totp_secret)


def _build_client_or_none() -> AwsClient | None:  # pragma: no cover — real boto3 client build
    """Build the real boto3 :class:`AwsClient`, or ``None`` when it cannot be built.

    A missing boto3 (:class:`~brutex.adapters.groww.aws_client.AwsBackendUnavailable`, an
    ``ImportError``) OR any boto3-client construction error — a bad region, an absent
    profile (``ProfileNotFound``), a credential-chain failure — logs + returns ``None`` so
    the file/env path is NEVER broken (matching the token-store sibling's
    ``except Exception`` fail-safe; a construction error is NOT an ``ImportError`` and must
    not escape the NEVER-RAISE resolver into a hard pull-startup crash).
    """
    try:
        return build_real_aws_client(resolve_token_broker_region())
    except Exception as exc:  # pylint: disable=broad-exception-caught — degrade ANY build failure to the file tier
        _LOG.warning("groww.creds.aws_unavailable", error_type=type(exc).__name__)
        return None


def _read_param(client: AwsClient, name: str) -> str | None:
    """Read one SecureString parameter, returning its non-blank value or ``None``.

    A ``ParameterNotFound`` (matched by exception class name — no botocore import), an
    absent/blank value, or ANY other SSM/network/KMS error → ``None`` (a redacting log,
    never the value).
    """
    try:
        value = client.get_parameter(name=name, with_decryption=True)
    except Exception as exc:  # pylint: disable=broad-exception-caught — degrade ANY read failure to None
        reason = type(exc).__name__
        if reason == SSM_PARAMETER_NOT_FOUND:
            _LOG.debug("groww.creds.aws_param_absent", name=name)
        else:
            _LOG.warning("groww.creds.aws_read_failed", error_type=reason)
        return None
    if value is None or not value.strip():
        return None
    return value


__all__ = ["load_groww_aws_credentials"]
