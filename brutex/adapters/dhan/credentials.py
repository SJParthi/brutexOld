"""AWS Parameter-Store credential reader — the SOLE source of the Dhan credentials.

DEC-DHAN-ADAPTER-001, mirroring DEC-AWS-ONLY-CREDS-001. The Dhan ``access_token`` +
``client_id`` live in AWS Systems Manager Parameter Store as SecureStrings —
``/tickvault/prod/dhan/access-token`` + ``/tickvault/prod/dhan/client-id`` (region
``ap-south-1``) — read fail-fast, NEVER from env vars / a local file / a webpage. This
mirrors the Groww reader exactly and REUSES the shared, vendor-neutral SSM surface
(:class:`~brutex.adapters.groww.aws_client.AwsClient` + the shared
:class:`~brutex.config.credentials_types.CredentialsError`) — CLAUDE.md §11 COMMON, no
copy of the AWS primitive.

**GATED ON SUBSCRIPTION (Golden Rule #10):** this reader is READY but INERT — the
Dhan Data API is a paid ₹499/mo add-on, and until the operator provisions the two SSM
SecureStrings a read raises a clear fail-fast error. The injected-client seam drives
every branch to 100% with the shared ``FakeAwsClient`` (no boto3, no network); the real
boto3 build is confined to a ``# pragma: no cover`` factory. The conftest
AWS-hermeticity belt additionally blocks any live boto3 call from these tests.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from brutex.adapters.dhan.constants import (
    DHAN_TOKEN_PARAM_ACCESS_TOKEN_DEFAULT,
    DHAN_TOKEN_PARAM_ACCESS_TOKEN_ENV,
    DHAN_TOKEN_PARAM_CLIENT_ID_DEFAULT,
    DHAN_TOKEN_PARAM_CLIENT_ID_ENV,
    resolve_token_broker_region,
    resolve_token_param_path,
)
from brutex.adapters.groww.aws_client import (
    SSM_PARAMETER_NOT_FOUND,
    AwsClient,
    build_real_aws_client,
)
from brutex.config.credentials_types import CredentialsError

_LOG = structlog.get_logger("brutex.adapters.dhan.credentials")

#: How to fix a Dhan AWS credential-resolution failure — names the paths + the
#: subscription gate + the deploy pointer; contains NO secret values.
DHAN_CREDS_HOW_TO_FIX: str = (
    "subscribe to the Dhan Data API (₹499/mo add-on) then seed the Dhan credentials in "
    "AWS Parameter Store (/tickvault/prod/dhan/access-token + /tickvault/prod/dhan/client-id, "
    "SecureStrings in ap-south-1); BruteX reads Dhan credentials ONLY from AWS "
    "(no env-var / local-file / webpage fallback)"
)


@dataclass(frozen=True, slots=True)
class DhanCredentials:
    """An immutable pair of resolved Dhan credentials (both fields non-blank).

    Attributes
    ----------
    access_token
        The Dhan access-token JWT (SSM ``/tickvault/prod/dhan/access-token``).
    client_id
        The Dhan client id (SSM ``/tickvault/prod/dhan/client-id``) — sent as the
        ``client-id`` header where an endpoint requires it.
    """

    access_token: str
    client_id: str


def load_dhan_aws_credentials(*, client: AwsClient | None = None) -> DhanCredentials:
    """Load the Dhan credentials from AWS Parameter Store — the SOLE source (fail-fast).

    Reads ``/tickvault/prod/dhan/access-token`` + ``/tickvault/prod/dhan/client-id``
    (both env-overridable) as SecureStrings. Any failure — boto3 missing, either
    parameter absent/blank, or ANY SSM/network/IAM/KMS error — raises
    :class:`~brutex.config.credentials_types.CredentialsError` with a clear,
    secret-free fix message. It NEVER returns ``None`` and NEVER falls back to env /
    file / webpage (DEC-DHAN-ADAPTER-001, mirroring DEC-AWS-ONLY-CREDS-001).

    Parameters
    ----------
    client
        Injected :class:`~brutex.adapters.groww.aws_client.AwsClient` for tests (a
        fake). ``None`` builds the real boto3 client lazily (``# pragma: no cover``).
    """
    resolved_client = client if client is not None else _build_client_or_raise()

    access_token = _read_param(
        resolved_client,
        resolve_token_param_path(
            DHAN_TOKEN_PARAM_ACCESS_TOKEN_ENV, DHAN_TOKEN_PARAM_ACCESS_TOKEN_DEFAULT
        ),
    )
    client_id = _read_param(
        resolved_client,
        resolve_token_param_path(
            DHAN_TOKEN_PARAM_CLIENT_ID_ENV, DHAN_TOKEN_PARAM_CLIENT_ID_DEFAULT
        ),
    )
    missing: list[str] = []
    if access_token is None:
        missing.append(
            resolve_token_param_path(
                DHAN_TOKEN_PARAM_ACCESS_TOKEN_ENV, DHAN_TOKEN_PARAM_ACCESS_TOKEN_DEFAULT
            )
        )
    if client_id is None:
        missing.append(
            resolve_token_param_path(
                DHAN_TOKEN_PARAM_CLIENT_ID_ENV, DHAN_TOKEN_PARAM_CLIENT_ID_DEFAULT
            )
        )
    if access_token is None or client_id is None:
        raise CredentialsError(
            f"missing Dhan credentials in AWS Parameter Store: {', '.join(missing)}; "
            f"{DHAN_CREDS_HOW_TO_FIX}"
        )

    return DhanCredentials(access_token=access_token, client_id=client_id)


def _build_client_or_raise() -> AwsClient:  # pragma: no cover — real boto3 client build
    """Build the real boto3 :class:`AwsClient`, raising a clear error when it cannot be built."""
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

    A ``ParameterNotFound`` (matched by exception class name — no botocore import) or an
    absent/blank value returns ``None``. ANY OTHER SSM/network/IAM/KMS error is
    FAIL-FAST — it raises :class:`~brutex.config.credentials_types.CredentialsError`
    (redacting), since a transient/denied read must not be mistaken for "absent".
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


__all__ = ["DHAN_CREDS_HOW_TO_FIX", "DhanCredentials", "load_dhan_aws_credentials"]
