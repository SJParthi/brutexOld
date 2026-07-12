"""AWS-client Protocol seam — the dependency-free surface the token brokers call boto3 through.

DEC-AWS-SSM-SEAM-COMMON-001 (MIGRATION.md #1754). This is the COMMON home for the AWS
Systems Manager **Parameter Store** seam, moved verbatim from
:mod:`brutex.adapters.groww.aws_client` (Groww byte-identical; that module is now a thin
re-export shim, identity-pinned) so a SECOND vendor (Dhan) can share it without importing
anything from the Groww package. DEC-TOKEN-BROKER-AWS-001 originated the design.

The seam mirrors the proven ``growwapi`` SDK-Protocol pattern:

* a dependency-free ``runtime_checkable`` :class:`AwsClient` Protocol declaring ONLY the
  narrow READ surface a token broker calls — a single ``get_parameter`` SSM read —
  importing NOTHING from any ``brutex`` package (stdlib only), so it can never join an
  import cycle;
* a single :func:`build_real_aws_client` factory (``# pragma: no cover``) that lazily
  imports boto3 and builds the SSM client — reached ONLY when a read-only broker is
  selected AND boto3 is installed;
* :exc:`AwsBackendUnavailable` (a plain ``ImportError`` subclass) raised by the factories
  when boto3 is missing, so the resolver fails-safe / fail-fast instead of crashing;
* :func:`resolve_token_broker_region` / :func:`resolve_token_param_path` — the ONE region /
  parameter-path resolvers, shared by every vendor's credential + token-store readers.

WRITE surface (DEC-DHAN-AUTH-001). The Groww :class:`AwsClient` Protocol is deliberately
**read-only** — its write-incapability is a standing security property (BruteX + TickVault
are strictly read-only consumers of the Lambda-minted shared Groww token). Dhan is
different: BruteX self-mints the daily Dhan token and must WRITE it back to a SecureString,
so a SEPARATE :class:`AwsClientWriter` Protocol adds a single ``put_parameter``. The Groww
Protocol is **not widened** — a Groww-side store still cannot write — so the write grant is
expressed narrowly (an ``AwsClientWriter`` is required only by the Dhan token store, and the
IAM policy grants ``ssm:PutParameter`` on exactly ``.../dhan/access-token``).

The one classifier name a broker matches (``ParameterNotFound``) is declared here as a plain
constant string so BOTH the broker and the fake compare the error's ``__class__.__name__`` —
never importing ``botocore.exceptions`` — keeping the whole broker importable with boto3
absent.
"""

from __future__ import annotations

import os
from typing import Protocol, runtime_checkable

#: SSM's "parameter absent" exception CLASS NAME. Matched by ``type(exc).__name__`` so
#: neither a broker nor a fake imports ``botocore.exceptions``, keeping the whole broker
#: importable with boto3 absent.
SSM_PARAMETER_NOT_FOUND: str = "ParameterNotFound"  # exception class name, not a secret

#: Primary token-broker SSM region env override. Unset/blank ⇒ :data:`AWS_REGION_ENV`, then
#: the operator default :data:`TOKEN_BROKER_REGION_DEFAULT`.
TOKEN_BROKER_REGION_ENV: str = "BRUTEX_TOKEN_AWS_REGION"  # noqa: S105  # env var name, not a secret
#: Standard AWS SDK region env var, honoured as a secondary override.
AWS_REGION_ENV: str = "AWS_REGION"
#: The operator-confirmed default region (the SecureStrings live in ap-south-1).
TOKEN_BROKER_REGION_DEFAULT: str = "ap-south-1"  # noqa: S105  # AWS region, not a secret


class AwsBackendUnavailable(ImportError):
    """boto3 is not installed, so the AWS credential/token backend cannot be built.

    An :class:`ImportError` subclass. AWS is the SOLE credential/token source
    (DEC-AWS-ONLY-CREDS-001), so the resolver maps this to a clear fail-fast error naming the
    missing extra — there is NO local fallback (a missing extra means the pull cannot get
    credentials and must stop loudly, never silently).
    """


@runtime_checkable
class AwsClient(Protocol):
    """The narrow AWS surface a READ-ONLY token broker calls — one SSM read.

    ``get_parameter`` takes/returns the boto3 shapes verbatim (``str``/``None``); the real
    client is boto3's low-level SSM client, the fake is a pure-Python in-memory double.
    Neither the broker nor this Protocol imports boto3. This Protocol is READ-ONLY, so there
    is no write / put method — its write-incapability is a standing security property (a
    Groww-side store can never write the shared token). A vendor that self-mints (Dhan) uses
    the wider :class:`AwsClientWriter` instead.
    """

    def get_parameter(self, *, name: str, with_decryption: bool) -> str | None:
        """Return the SecureString value at *name*, or ``None`` when absent.

        Raises on an access/network error (the caller degrades any raise to ``None``).
        """
        raise NotImplementedError  # structural Protocol stub — never executed


@runtime_checkable
class AwsClientWriter(AwsClient, Protocol):
    """The AWS surface a SELF-MINTING token store calls — one SSM read + one SSM write.

    Extends :class:`AwsClient` with ``put_parameter`` for a vendor whose daily token BruteX
    mints itself and must persist back to a SecureString (Dhan — DEC-DHAN-AUTH-001). The
    write grant is deliberately narrow: only the Dhan token store requires an
    ``AwsClientWriter``, and the IAM policy grants ``ssm:PutParameter`` on exactly the one
    ``.../dhan/access-token`` parameter. The read-only :class:`AwsClient` is NOT widened, so
    every Groww-side store remains structurally unable to write.
    """

    def put_parameter(
        self, *, name: str, value: str, secure: bool = True, overwrite: bool = True
    ) -> None:
        """Write *value* to the SecureString at *name* (``Overwrite=True`` by default).

        Raises on an access/network error (the caller maps it to a typed store error).
        """
        raise NotImplementedError  # structural Protocol stub — never executed


def resolve_token_broker_region() -> str:
    """Resolve the token-broker SSM region: ``BRUTEX_TOKEN_AWS_REGION`` → ``AWS_REGION`` → default.

    The operator's SecureStrings live in :data:`TOKEN_BROKER_REGION_DEFAULT` (``ap-south-1``).
    A non-blank ``BRUTEX_TOKEN_AWS_REGION`` wins; else a non-blank ``AWS_REGION``; else the
    ap-south-1 default (never ``None`` — BruteX always targets a concrete region so a mis-set
    profile default cannot silently read the wrong region). O(1).
    """
    for env_name in (TOKEN_BROKER_REGION_ENV, AWS_REGION_ENV):
        raw = os.environ.get(env_name)
        if raw is not None and raw.strip():
            return raw.strip()
    return TOKEN_BROKER_REGION_DEFAULT


def resolve_token_param_path(env_name: str, default: str) -> str:
    """Return the Parameter-Store path from *env_name*, or *default* when unset/blank.

    A thin COMMON resolver shared by every vendor's shared-secret paths (Groww
    access-token / api-key / totp-secret; Dhan access-token / client-id / pin / totp-secret)
    so each honours its own env override with the operator's fixed ``/tickvault/prod/…``
    default. O(1).
    """
    raw = os.environ.get(env_name)
    if raw is not None and raw.strip():
        return raw.strip()
    return default


def build_real_aws_client(region: str) -> AwsClient:  # pragma: no cover — needs boto3 + AWS
    """Build the production READ-ONLY :class:`AwsClient` backed by boto3 SSM (lazy import).

    AWS is the SOLE credential/token source (DEC-AWS-ONLY-CREDS-001). A missing boto3 raises
    :class:`AwsBackendUnavailable`, which the resolver maps to a clear fail-fast error (no
    env / file / local-mint fallback). *region* (from :func:`resolve_token_broker_region`,
    default ``ap-south-1``) is passed to the client so BruteX always targets the region the
    SecureStrings live in.
    """
    try:
        import boto3  # type: ignore[import-not-found, unused-ignore]
    except ImportError as exc:
        raise AwsBackendUnavailable(
            "boto3 is required to read the vendor credentials + token from AWS Parameter "
            'Store; install the extra: pip install -e ".[token-broker]"'
        ) from exc

    ssm = boto3.client("ssm", region_name=region)
    return _Boto3AwsClient(ssm=ssm)


def build_real_aws_writer_client(
    region: str,
) -> AwsClientWriter:  # pragma: no cover — needs boto3 + AWS
    """Build the production WRITE-capable :class:`AwsClientWriter` backed by boto3 SSM.

    The Dhan self-mint path (DEC-DHAN-AUTH-001) persists the freshly-minted token back to a
    SecureString, so it needs a client that can ``PutParameter`` in addition to
    ``GetParameter``. Lazily imports boto3 exactly like :func:`build_real_aws_client`; a
    missing boto3 raises :class:`AwsBackendUnavailable`. The write grant is provisioned
    narrowly in IAM (``ssm:PutParameter`` on exactly ``.../dhan/access-token``).
    """
    try:
        import boto3  # type: ignore[import-not-found, unused-ignore]
    except ImportError as exc:
        raise AwsBackendUnavailable(
            "boto3 is required to read/write the Dhan credentials + token in AWS Parameter "
            'Store; install the extra: pip install -e ".[token-broker]"'
        ) from exc

    ssm = boto3.client("ssm", region_name=region)
    return _Boto3AwsWriterClient(ssm=ssm)


class _Boto3AwsClient:  # pragma: no cover — needs boto3 + AWS
    """Adapts the boto3 low-level SSM client to the READ-ONLY :class:`AwsClient` Protocol.

    A thin translation layer only: it maps the Protocol's kwargs to boto3's request shape and
    unwraps the boto3 response envelope. No caching, no retry. Kept ``# pragma: no cover``
    because it can only run against a real AWS account; the fake drives every branch of the
    broker logic that USES this surface.
    """

    def __init__(self, *, ssm: object) -> None:
        self._ssm = ssm

    def get_parameter(self, *, name: str, with_decryption: bool) -> str | None:
        """Read the SecureString value at *name* via SSM, or ``None`` when absent."""
        resp = self._ssm.get_parameter(Name=name, WithDecryption=with_decryption)  # type: ignore[attr-defined]
        param = resp.get("Parameter") or {}
        value = param.get("Value")
        return value if isinstance(value, str) else None


class _Boto3AwsWriterClient(_Boto3AwsClient):  # pragma: no cover — needs boto3 + AWS
    """Adapts boto3 SSM to the WRITE-capable :class:`AwsClientWriter` Protocol.

    Inherits the read half from :class:`_Boto3AwsClient` and adds ``put_parameter`` — the ONE
    write BruteX ever performs (the daily Dhan access token). Kept ``# pragma: no cover`` (a
    real AWS account is required); the fake drives the store logic that USES this surface.
    """

    def put_parameter(
        self, *, name: str, value: str, secure: bool = True, overwrite: bool = True
    ) -> None:
        """Write *value* to the SecureString at *name* via ``PutParameter``."""
        self._ssm.put_parameter(  # type: ignore[attr-defined]
            Name=name,
            Value=value,
            Type="SecureString" if secure else "String",
            Overwrite=overwrite,
        )


__all__ = [
    "AWS_REGION_ENV",
    "SSM_PARAMETER_NOT_FOUND",
    "TOKEN_BROKER_REGION_DEFAULT",
    "TOKEN_BROKER_REGION_ENV",
    "AwsBackendUnavailable",
    "AwsClient",
    "AwsClientWriter",
    "build_real_aws_client",
    "build_real_aws_writer_client",
    "resolve_token_broker_region",
    "resolve_token_param_path",
]
