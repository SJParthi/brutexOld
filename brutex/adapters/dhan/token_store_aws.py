"""AWS token store — the shared read-only daily Dhan-token backend.

DEC-DHAN-ADAPTER-001, mirroring DEC-TOKEN-BROKER-AWS-001. The shared daily Dhan
access token lives in a single AWS Systems Manager Parameter Store SecureString
(``/tickvault/prod/dhan/access-token``, region ``ap-south-1``); a dedicated
minter is the ONLY writer, BruteX is a strictly READ-ONLY consumer. A read is a
plain ``GetParameter WithDecryption=True`` (KMS-decrypted server-side); BruteX never
mints.

* :class:`DhanParameterStoreTokenStore` — the read-only backend. :meth:`read_token`
  degrades ANY boto3/network error to ``None`` (the refresher's contract); :meth:`store`
  fail-fasts (a mint on a read-only host is a LOUD honest error, never a silent local
  write). :attr:`readonly` is ``True`` — the reauth path RE-READS instead of minting.

REUSES the shared, vendor-neutral :class:`~brutex.adapters.groww.aws_client.AwsClient`
surface (CLAUDE.md §11 COMMON) — no copy of the AWS primitive. INERT / GATED ON the
Data-API subscription: the injected-client seam drives every branch with the shared
``FakeAwsClient`` (no boto3, no network).
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from brutex.adapters.dhan.constants import (
    DHAN_TOKEN_PARAM_ACCESS_TOKEN_DEFAULT,
    DHAN_TOKEN_PARAM_ACCESS_TOKEN_ENV,
    resolve_token_broker_region,
    resolve_token_param_path,
)
from brutex.adapters.dhan.errors import DhanAuthError
from brutex.adapters.groww.aws_client import (
    SSM_PARAMETER_NOT_FOUND,
    AwsClient,
    build_real_aws_client,
)

_LOG = structlog.get_logger("brutex.adapters.dhan.token_store_aws")


class DhanReadOnlyMintError(DhanAuthError):
    """A mint was attempted while the read-only Parameter-Store Dhan-token broker is active.

    BruteX is a READ-ONLY consumer of the shared daily token (a dedicated minter owns
    writing). A ``store`` call means the spine reached a fresh-mint (typically because
    the shared token is ABSENT — the minter has not written it yet), which must NOT
    happen on a read-only host — so this fails LOUD (a terminal
    :class:`~brutex.adapters.dhan.errors.DhanAuthError`) instead of a silent local write.
    """


@dataclass(frozen=True, slots=True)
class DhanTokenLoadMiss:
    """WHY a Parameter-Store token read missed — ``"error"`` (raised) vs ``"absent"``.

    The ``read_token`` contract degrades ANY failure to ``None``, which makes a
    reader-side IAM/KMS/network failure indistinguishable from "genuinely absent". This
    record carries the distinction for a diagnostic caller.
    """

    kind: str
    error_type: str | None
    detail: str


class DhanParameterStoreTokenStore:
    """The shared daily-token backend — a READ-ONLY Parameter-Store SecureString reader.

    Parameters
    ----------
    client
        The injected :class:`~brutex.adapters.groww.aws_client.AwsClient` (real boto3
        SSM client in production, a fake in tests).
    token_param
        The full Parameter-Store path of the shared access token.
    """

    def __init__(self, *, client: AwsClient, token_param: str) -> None:
        self._client = client
        self._token_param = token_param

    @property
    def token_param(self) -> str:
        """The Parameter-Store path of the shared access token (for diagnostics)."""
        return self._token_param

    @property
    def readonly(self) -> bool:
        """Always ``True`` — this backend NEVER mints (a dedicated minter owns writing)."""
        return True

    def read_token(self) -> str | None:
        """Return the shared token, else ``None`` (NEVER raises) — the refresher's source.

        Reads the shared SecureString. Any boto3/network/IAM/KMS error, or an
        absent/blank value, degrades to ``None`` (a miss). A raised read logs WARNING
        (a missing shared token must be loud); a blank value logs DEBUG.
        """
        token, miss = self.read_with_reason()
        if miss is not None:
            if miss.error_type is not None:
                _LOG.warning("dhan.token.aws_load_failed", error_type=miss.error_type)
            else:
                _LOG.debug("dhan.token.aws_miss", reason="absent")
            return None
        return token

    def read_with_reason(self) -> tuple[str | None, DhanTokenLoadMiss | None]:
        """Read the shared token, RETURNING the miss reason instead of swallowing it.

        Exactly one side of the pair is non-``None``. A ``ParameterNotFound`` is a
        definitive "genuinely absent" answer; any other raise is a reader-side failure
        (the token may well exist).
        """
        try:
            token = self._client.get_parameter(name=self._token_param, with_decryption=True)
        except Exception as exc:  # pylint: disable=broad-exception-caught — a raise ⇒ unreachable ⇒ miss
            if type(exc).__name__ == SSM_PARAMETER_NOT_FOUND:
                return None, DhanTokenLoadMiss(
                    kind="absent",
                    error_type=type(exc).__name__,
                    detail=f"parameter {self._token_param} not found (ParameterNotFound)",
                )
            return None, DhanTokenLoadMiss(
                kind="error", error_type=type(exc).__name__, detail=str(exc)
            )
        if token is None or not token.strip():
            return None, DhanTokenLoadMiss(
                kind="absent",
                error_type=None,
                detail=f"parameter {self._token_param} absent or blank",
            )
        _LOG.debug("dhan.token.aws_hit")
        return token, None

    def store(self, token: str) -> bool:
        """Fail LOUD — BruteX is read-only and must NEVER mint/write the shared token.

        Reaching ``store`` means the spine minted a fresh token, forbidden on a
        read-only host. Raising :exc:`DhanReadOnlyMintError` is a terminal fail-fast,
        never a silent local write. *token* is unused (nothing is written).
        """
        _ = token
        _LOG.error("dhan.token.aws_readonly_mint_blocked")
        raise DhanReadOnlyMintError(
            "No Dhan token in AWS Parameter Store (/tickvault/prod/dhan/access-token) — "
            "deploy the minter first. BruteX is a read-only consumer of the shared Dhan "
            "access token; it reads the token ONLY from AWS and never mints locally "
            "(DEC-DHAN-ADAPTER-001)."
        )


def build_readonly_token_store(*, client: AwsClient | None = None) -> DhanParameterStoreTokenStore:
    """Build the read-only Dhan token store — injected *client* for tests, real boto3 otherwise.

    Resolves the shared access-token path from the environment
    (``BRUTEX_DHAN_TOKEN_PARAM_ACCESS_TOKEN``, default
    ``/tickvault/prod/dhan/access-token``). ``None`` builds the real boto3 client
    lazily (``# pragma: no cover``).
    """
    resolved_client = client if client is not None else _build_client()
    token_param = resolve_token_param_path(
        DHAN_TOKEN_PARAM_ACCESS_TOKEN_ENV, DHAN_TOKEN_PARAM_ACCESS_TOKEN_DEFAULT
    )
    return DhanParameterStoreTokenStore(client=resolved_client, token_param=token_param)


def _build_client() -> AwsClient:  # pragma: no cover — real boto3 client build
    """Build the real boto3 :class:`AwsClient` for the read-only token store."""
    return build_real_aws_client(resolve_token_broker_region())


__all__ = [
    "DhanParameterStoreTokenStore",
    "DhanReadOnlyMintError",
    "DhanTokenLoadMiss",
    "build_readonly_token_store",
]
