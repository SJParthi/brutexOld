"""AWS token store — the shared Dhan access-token READ-ONLY Parameter-Store backend.

DEC-DHAN-READONLY-AUTH-001 (ADR-0022). The Dhan 24-h JWT access token is minted by
a dedicated Lambda / TickVault into ONE AWS Systems Manager Parameter Store
SecureString (``/tickvault/prod/dhan/access-token``); BruteX is a strictly
READ-ONLY consumer. Because there is exactly ONE writer there is NO
compare-and-swap surface — a plain ``GetParameter WithDecryption=True`` read
suffices, and BruteX never mints.

This module implements the token backend against that read-only surface:

* :class:`ParameterStoreDhanTokenStore` — ``load`` reads the shared SecureString
  (degrading ANY boto3/network error to ``None`` — NEVER raises), ``load_with_reason``
  distinguishes "read failed" from "genuinely absent" (the R7 reason seam), and
  ``store`` is a fail-fast :exc:`~brutex.adapters.dhan.errors.DhanTokenReadOnlyError`
  (a mint attempt on a read-only host is a LOUD honest error, never a silent local
  write — the minter owns minting).

Unlike Groww, a Dhan token is validated **as a non-blank string only** — it is NOT
06:00-IST session-anchored (a Groww artifact), so there is NO disk token cache and
NO ``current_session`` stamp. The 24-h JWT's own expiry + the minter's rotation are
the lifecycle; BruteX trusts the current shared value and RE-READS on a DH-901
(:class:`~brutex.adapters.dhan.reauth.DhanSdkRefresher`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import structlog

from brutex.adapters.dhan.constants import (
    DHAN_TOKEN_PARAM_ACCESS_TOKEN_DEFAULT,
    DHAN_TOKEN_PARAM_ACCESS_TOKEN_ENV,
)
from brutex.adapters.dhan.errors import DhanTokenReadOnlyError
from brutex.adapters.groww.aws_client import (
    SSM_PARAMETER_NOT_FOUND,
    AwsClient,
    build_real_aws_client,
)
from brutex.adapters.groww.constants import (
    resolve_token_broker_region,
    resolve_token_param_path,
)

_LOG = structlog.get_logger("brutex.adapters.dhan.token_store_aws")


@dataclass(frozen=True, slots=True)
class DhanCachedToken:
    """The shared Dhan access token as read from SSM (non-blank string only).

    Attributes
    ----------
    access_token
        The 24-h JWT. ``repr``-suppressed so it never leaks into a log / traceback.
    """

    access_token: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class DhanTokenLoadMiss:
    """WHY a Dhan token load missed — the reason-carrying seam (the R7 distinction).

    ``load`` degrades ANY failure to ``None`` (its NEVER-raises contract), which
    makes a reader-side IAM/KMS/network failure indistinguishable from "token
    genuinely absent". This record carries the distinction for callers that need it.

    Attributes
    ----------
    kind
        ``"error"`` (the read RAISED — the token may well exist) or ``"absent"``
        (the read SUCCEEDED and the parameter is missing/blank, incl. a
        ``ParameterNotFound``).
    error_type
        The raising exception's class name for ``kind="error"``, else ``None``.
    detail
        A short, secret-free description (exception text / the absent path).
    """

    kind: str
    error_type: str | None
    detail: str


class ParameterStoreDhanTokenStore:
    """The shared Dhan token backend — a READ-ONLY Parameter-Store SecureString reader.

    Parameters
    ----------
    client
        The injected :class:`~brutex.adapters.groww.aws_client.AwsClient` (real boto3
        SSM client in production, a fake in tests) — the one read flows through it.
    token_param
        The full Parameter-Store path of the shared access token (default
        ``/tickvault/prod/dhan/access-token``).
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
        """Always ``True`` — this backend NEVER mints (the minter owns minting).

        Drives the mid-run reauth to RE-READ the shared token from SSM instead of
        minting locally, so the read-only host self-heals the minter's rotation.
        """
        return True

    def load(self, *, now_utc: datetime) -> DhanCachedToken | None:
        """Return the shared Dhan token, else ``None`` (NEVER raises).

        Reads the shared SecureString. Any boto3/network/IAM/KMS error, or an
        absent/blank value, degrades to ``None`` (a miss). Logging contract (mirrors
        the Groww store): ANY raised read — including ``ParameterNotFound`` — logs
        WARNING ``dhan.token.aws_load_failed`` (production observability must see a
        missing shared token loudly); a read that SUCCEEDED and returned a
        blank/``None`` value logs DEBUG ``dhan.token.aws_miss``. *now_utc* is unused
        (a Dhan token is not session-anchored) — accepted for reauth-hook signature
        parity.
        """
        token, miss = self.load_with_reason(now_utc=now_utc)
        if miss is not None:
            if miss.error_type is not None:
                _LOG.warning("dhan.token.aws_load_failed", error_type=miss.error_type)
            else:
                _LOG.debug("dhan.token.aws_miss", reason="absent")
            return None
        return token

    def load_with_reason(
        self, *, now_utc: datetime
    ) -> tuple[DhanCachedToken | None, DhanTokenLoadMiss | None]:
        """Read the shared token, RETURNING the miss reason instead of swallowing it.

        Exactly one side of the pair is non-``None``. A ``ParameterNotFound`` IS a
        definitive "genuinely absent" answer (the SSM read worked; the parameter does
        not exist); any other raise is an "error" miss (the token may exist). *now_utc*
        is accepted for signature parity but unused (Dhan tokens are not
        session-anchored).
        """
        _ = now_utc
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
        return DhanCachedToken(access_token=token), None

    def store(self, *, token: str, now_utc: datetime) -> bool:
        """Fail LOUD — BruteX is read-only and must NEVER mint/write the shared token.

        Reaching ``store`` means a caller tried to MINT, which is forbidden on a
        read-only host (a dedicated Lambda / TickVault is the ONLY minter). Raising
        :exc:`~brutex.adapters.dhan.errors.DhanTokenReadOnlyError` here is a terminal,
        redacting fail-fast — never a silent local write that would fork the shared
        token. *token* / *now_utc* are unused (nothing is written).
        """
        _ = (token, now_utc)
        _LOG.error("dhan.token.aws_readonly_mint_blocked")
        raise DhanTokenReadOnlyError(
            "No Dhan token write is permitted: BruteX is a READ-ONLY consumer of the "
            f"shared Dhan access token ({self._token_param}) — a dedicated Lambda / "
            "TickVault mints it. Deploy / run the minter so the fresh token lands in "
            "AWS Parameter Store; BruteX reads it ONLY and never mints "
            "(DEC-DHAN-READONLY-AUTH-001)."
        )


def build_readonly_dhan_token_store() -> (
    ParameterStoreDhanTokenStore
):  # pragma: no cover — real boto3
    """Build the read-only Dhan token store from the environment (real boto3 client).

    A missing boto3 extra / SSM-client build failure raises from
    :func:`~brutex.adapters.groww.aws_client.build_real_aws_client`; the caller maps
    it to a clear fail-fast error (AWS is the SOLE token source). The real boto3
    build is ``# pragma: no cover``; a test injects a fake via
    :func:`build_readonly_dhan_token_store_with`.
    """
    client = build_real_aws_client(resolve_token_broker_region())
    return build_readonly_dhan_token_store_with(client)


def build_readonly_dhan_token_store_with(client: AwsClient) -> ParameterStoreDhanTokenStore:
    """Build the read-only Dhan token store around an injected *client* (the tested seam).

    Resolves the shared access-token path from the environment
    (``BRUTEX_TOKEN_PARAM_DHAN_ACCESS_TOKEN``, default
    ``/tickvault/prod/dhan/access-token``). This helper is the injectable core so the
    fake drives it to 100%.
    """
    token_param = resolve_token_param_path(
        DHAN_TOKEN_PARAM_ACCESS_TOKEN_ENV, DHAN_TOKEN_PARAM_ACCESS_TOKEN_DEFAULT
    )
    return ParameterStoreDhanTokenStore(client=client, token_param=token_param)


__all__ = [
    "DhanCachedToken",
    "DhanTokenLoadMiss",
    "ParameterStoreDhanTokenStore",
    "build_readonly_dhan_token_store",
    "build_readonly_dhan_token_store_with",
]
