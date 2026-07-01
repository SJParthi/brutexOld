"""AWS-client Protocol — the dependency-free seam the token broker calls boto3 through.

DEC-TOKEN-BROKER-AWS-001. The shared token broker READS the Groww creds AND the shared
daily access token from AWS Systems Manager **Parameter Store** SecureStrings, but
``boto3`` is a NEW OPTIONAL extra (``pip install -e ".[token-broker]"``) that a purely-
LOCAL run never installs. To keep the local path free of any AWS import, this module
mirrors the proven ``growwapi`` pattern (:mod:`brutex.adapters.groww.sdk_protocol` +
``reauth._real_sdk_factory`` + ``tests/fakes/fake_groww_sdk.py``):

* a dependency-free ``runtime_checkable`` :class:`AwsClient` Protocol declaring ONLY the
  narrow surface the broker calls — a single ``get_parameter`` SSM read — importing
  NOTHING from the package (stdlib only) so it can never join an import cycle;
* a single :func:`build_real_aws_client` factory marked ``# pragma: no cover`` (the exact
  shape of ``reauth._real_sdk_factory``) that lazily imports boto3 and builds the SSM
  client — reached ONLY when the read-only broker is selected AND boto3 is installed;
* :exc:`AwsBackendUnavailable` (a plain ``ImportError`` subclass) raised by the factory
  when boto3 is missing, so the resolver fails-safe to the local backend instead of
  crashing.

The design is **single-writer, read-only for BruteX** (a dedicated Lambda is the ONLY
minter of the shared access token; BruteX + TickVault only READ) — so there is NO
compare-and-swap / DynamoDB / KMS-encrypt surface here: a read is a plain
``GetParameter WithDecryption=True`` that KMS-DECRYPTS the SecureString server-side (AWS
SSM docs "Working with SecureString parameters"; ``GetParameter`` API reference), and
BruteX never writes anything.

Every side-effecting AWS call in :mod:`brutex.adapters.groww.token_store_aws` /
:mod:`brutex.config.credentials_aws` is made through an injected :class:`AwsClient`, so a
deterministic in-memory fake (``tests/fakes/fake_aws_client.py``) drives those modules to
100% line+branch with NO boto3 install and NO network.

The one classifier name the broker matches (``ParameterNotFound``) is declared here as a
plain constant string so BOTH the broker and the fake compare the error's
``__class__.__name__`` — never importing ``botocore.exceptions`` — keeping the whole
broker importable with boto3 absent.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

#: SSM's "parameter absent" exception CLASS NAME. Matched by ``type(exc).__name__`` so
#: neither the broker nor the fake imports ``botocore.exceptions``, keeping the whole
#: broker importable with boto3 absent.
SSM_PARAMETER_NOT_FOUND: str = "ParameterNotFound"  # exception class name, not a secret


class AwsBackendUnavailable(ImportError):
    """boto3 is not installed, so the AWS token backend cannot be built.

    An :class:`ImportError` subclass so the resolver's ``except ImportError`` fail-safe
    (DEC-TOKEN-BROKER-AWS-001) catches it and degrades to the local disk backend —
    a missing extra can never crash a pull.
    """


@runtime_checkable
class AwsClient(Protocol):
    """The narrow AWS surface the read-only token broker calls — one SSM read.

    ``get_parameter`` takes/returns the boto3 shapes verbatim (``str``/``None``); the real
    client is boto3's low-level SSM client, the fake is a pure-Python in-memory double.
    Neither the broker nor this Protocol imports boto3. BruteX is READ-ONLY, so there is no
    write / put / encrypt method — the shared access token is minted by a dedicated Lambda.
    """

    def get_parameter(self, *, name: str, with_decryption: bool) -> str | None:
        """Return the SecureString value at *name*, or ``None`` when absent.

        Raises on an access/network error (the caller degrades any raise to ``None``).
        """
        raise NotImplementedError  # structural Protocol stub — never executed


def build_real_aws_client(region: str) -> AwsClient:  # pragma: no cover — needs boto3 + AWS
    """Build the production :class:`AwsClient` backed by a boto3 SSM client (lazy import).

    Reached ONLY when the read-only broker is selected AND the extra is installed. A
    missing boto3 raises :class:`AwsBackendUnavailable` (caught by the resolver → local
    fallback). *region* (from :func:`~brutex.adapters.groww.constants.\
    resolve_token_broker_region`, default ``ap-south-1``) is passed to the client so
    BruteX always targets the region the SecureStrings live in.
    """
    try:
        import boto3  # type: ignore[import-not-found, unused-ignore]
    except ImportError as exc:
        raise AwsBackendUnavailable(
            "boto3 is required for BRUTEX_TOKEN_BROKER_READONLY; "
            'install the extra: pip install -e ".[token-broker]"'
        ) from exc

    ssm = boto3.client("ssm", region_name=region)
    return _Boto3AwsClient(ssm=ssm)


class _Boto3AwsClient:  # pragma: no cover — needs boto3 + AWS
    """Adapts the boto3 low-level SSM client to the :class:`AwsClient` Protocol.

    A thin translation layer only: it maps the Protocol's kwargs to boto3's request shape
    and unwraps the boto3 response envelope. No caching, no retry (the shared
    :class:`~brutex.adapters.rate_common.RateController` does not govern the broker — it is
    a handful of O(1) reads per pull). Kept ``# pragma: no cover`` because it can only run
    against a real AWS account; the fake drives every branch of the broker logic that USES
    this surface.
    """

    def __init__(self, *, ssm: object) -> None:
        self._ssm = ssm

    def get_parameter(self, *, name: str, with_decryption: bool) -> str | None:
        """Read the SecureString value at *name* via SSM, or ``None`` when absent.

        Unwraps boto3's ``{"Parameter": {"Value": ...}}`` envelope; a non-string /
        missing value returns ``None``.
        """
        resp = self._ssm.get_parameter(Name=name, WithDecryption=with_decryption)  # type: ignore[attr-defined]
        param = resp.get("Parameter") or {}
        value = param.get("Value")
        return value if isinstance(value, str) else None


__all__ = [
    "SSM_PARAMETER_NOT_FOUND",
    "AwsBackendUnavailable",
    "AwsClient",
    "build_real_aws_client",
]
