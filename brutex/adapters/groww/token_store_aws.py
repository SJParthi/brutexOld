"""AWS token store — the shared **Parameter-Store** read-only daily-token backend.

DEC-TOKEN-BROKER-AWS-001 (operator's FINAL design, 2026-07-01). Two apps on ONE Groww
account (TickVault live + BruteX backtesting) share the daily access token via a SINGLE
AWS Systems Manager Parameter Store SecureString — ``/tickvault/prod/groww/access-token``
(region ``ap-south-1``). A dedicated Lambda is the ONLY writer/minter of that token;
BruteX and TickVault are **strictly READ-ONLY consumers**. Because there is exactly ONE
writer there is NO compare-and-swap / DynamoDB / KMS-encrypt surface — a plain
SecureString read (``GetParameter WithDecryption=True``, KMS-decrypted server-side; AWS
SSM docs "Working with SecureString parameters") is sufficient, and BruteX never mints.

This module implements the daily-token backend against that read-only surface:

* :class:`ParameterStoreTokenStore` — the read-only :class:`~brutex.adapters.groww.\
  token_store.TokenStore`. ``load`` reads the shared SecureString (degrading ANY
  boto3/network error to ``None`` — NEVER raises, the mint spine's contract); ``store``
  is a fail-fast :exc:`TokenBrokerReadOnlyError` (a mint attempt on a read-only host is a
  LOUD honest error, never a silent local write — the Lambda owns minting).

The read token is validated only as a non-blank string. Session-anchoring / staleness is
the Lambda's responsibility (it re-mints at the 06:00-IST boundary); BruteX trusts the
current shared value and self-heals via the existing forced re-mint path
(:class:`~brutex.adapters.groww.reauth.SdkRefresher`) if the token is stale — except that
on a read-only host the forced re-mint fails loud rather than minting locally.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import structlog

from brutex.adapters.groww.auth import GrowwCredentials
from brutex.adapters.groww.aws_client import (
    SSM_PARAMETER_NOT_FOUND,
    AwsClient,
    build_real_aws_client,
)
from brutex.adapters.groww.constants import (
    GROWW_TOKEN_PARAM_ACCESS_TOKEN_DEFAULT,
    GROWW_TOKEN_PARAM_ACCESS_TOKEN_ENV,
    resolve_token_broker_region,
    resolve_token_param_path,
)
from brutex.adapters.groww.errors import GrowwAuthError
from brutex.adapters.groww.token_cache import CachedToken, current_session

_LOG = structlog.get_logger("brutex.adapters.groww.token_store_aws")


class TokenBrokerReadOnlyError(GrowwAuthError):
    """A mint was attempted while the read-only Parameter-Store token broker is active.

    BruteX is a READ-ONLY consumer of the shared daily token (a dedicated Lambda mints it) —
    AWS is the SOLE token source (DEC-AWS-ONLY-CREDS-001), there is NO local-mint fallback.
    A ``store`` call means the mint spine reached a fresh-mint (typically because the shared
    token is ABSENT — the Lambda has not minted it yet), which must NOT happen on a
    read-only host — so this fails LOUD (a terminal :class:`~brutex.adapters.groww.errors.\
    GrowwAuthError`, redacting) instead of silently writing a local token that would fork the
    shared state. The operator's fix: deploy the minter so the Lambda mints the shared token.
    """


@dataclass(frozen=True, slots=True)
class TokenLoadMiss:
    """WHY a Parameter-Store token load missed (the reason-carrying seam, register R7).

    The production ``load`` contract degrades ANY failure to ``None`` (the mint
    spine's NEVER-raises contract), which makes a reader-side IAM/KMS/network
    failure indistinguishable from "token genuinely absent" — the exact R7
    misleading-diagnosis trap. This record carries the distinction for callers
    that need it (``brutex doctor --live`` stage 3; the eventual R7 cold-start
    guard reuses it).

    Attributes
    ----------
    kind
        ``"error"`` (the read RAISED — IAM/KMS/network/boto3; the token may well
        exist) or ``"absent"`` (the read SUCCEEDED and the parameter is
        genuinely missing/blank).
    error_type
        The raising exception's class name for ``kind="error"`` (e.g.
        ``AccessDeniedException``, ``ParameterNotFound``), else ``None``.
    detail
        A short human-readable, secret-free description (exception text or the
        parameter path that came back absent/blank).
    """

    kind: str
    error_type: str | None
    detail: str


class ParameterStoreTokenStore:
    """The shared daily-token backend — a READ-ONLY Parameter-Store SecureString reader.

    Parameters
    ----------
    client
        The injected :class:`~brutex.adapters.groww.aws_client.AwsClient` (real boto3 SSM
        client in production, a fake in tests) — the one read flows through it.
    token_param
        The full Parameter-Store path of the shared access token (default
        ``/tickvault/prod/groww/access-token``).
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
        """Always ``True`` — this backend NEVER mints (the Lambda owns minting).

        Drives the mid-run reauth to RE-READ the shared token from SSM instead of minting
        (DEC-BROKER-READONLY-REAUTH-REREAD-001), so the read-only host self-heals the
        06:00-IST rotation without the fail-loud :meth:`store` + a manual restart.
        """
        return True

    def load(self, creds: GrowwCredentials, *, now_utc: datetime) -> CachedToken | None:
        """Return the shared daily token for the current session, else ``None`` (NEVER raises).

        Reads the shared SecureString (``GetParameter WithDecryption=True``). Any
        boto3/network/IAM/KMS error, or an absent/blank value, degrades to ``None`` (a
        miss — the spine then reauths, which fails LOUD on a read-only host rather than
        minting locally). The returned token is stamped with the CURRENT session
        (:func:`~brutex.adapters.groww.token_cache.current_session`) so it satisfies the
        :class:`~brutex.adapters.groww.token_cache.CachedToken` contract; *creds* is unused
        (the shared parameter is account-scoped, not fingerprint-keyed — single account).
        """
        _ = creds
        token, miss = self.load_with_reason(now_utc=now_utc)
        if miss is not None:
            if miss.kind == "error":
                _LOG.warning("groww.token.aws_load_failed", error_type=miss.error_type)
            else:
                _LOG.debug("groww.token.aws_miss", reason="absent")
            return None
        return token

    def load_with_reason(
        self, *, now_utc: datetime
    ) -> tuple[CachedToken | None, TokenLoadMiss | None]:
        """Read the shared token, RETURNING the miss reason instead of swallowing it (R7 seam).

        Exactly one side of the pair is non-``None``. The production :meth:`load`
        delegates here (so the read stays in ONE place — CLAUDE.md §11 COMMON) and
        keeps its NEVER-raises degrade-to-``None`` contract; a caller that must
        distinguish "read failed" (reader-side IAM/KMS/network — do NOT deploy
        anything) from "token genuinely absent" (run the minter Lambda) reads the
        :class:`TokenLoadMiss` instead (``brutex doctor --live`` stage 3).
        """
        try:
            token = self._client.get_parameter(name=self._token_param, with_decryption=True)
        except Exception as exc:  # pylint: disable=broad-exception-caught — a raise ⇒ unreachable ⇒ miss
            # A ParameterNotFound IS a definitive "genuinely absent" answer (the
            # SSM read worked; the parameter does not exist) — the R7 distinction
            # mirrors ``credentials_aws._read_param``'s class-name classification.
            if type(exc).__name__ == SSM_PARAMETER_NOT_FOUND:
                return None, TokenLoadMiss(
                    kind="absent",
                    error_type=type(exc).__name__,
                    detail=f"parameter {self._token_param} not found (ParameterNotFound)",
                )
            return None, TokenLoadMiss(kind="error", error_type=type(exc).__name__, detail=str(exc))
        if token is None or not token.strip():
            return None, TokenLoadMiss(
                kind="absent",
                error_type=None,
                detail=f"parameter {self._token_param} absent or blank",
            )
        session = current_session(now_utc)
        _LOG.debug("groww.token.aws_hit", session=session.isoformat())
        return CachedToken(access_token=token, issued_for_session=session), None

    def store(self, creds: GrowwCredentials, token: str, *, now_utc: datetime) -> bool:
        """Fail LOUD — BruteX is read-only and must NEVER mint/write the shared token.

        Reaching ``store`` means the mint spine minted a fresh token, which is forbidden on
        a read-only host (a dedicated Lambda is the ONLY minter). Raising
        :exc:`TokenBrokerReadOnlyError` here is a terminal, redacting fail-fast — never a
        silent local write that would fork the shared token (the operator's explicit "never
        a silent local mint"). *creds* / *token* / *now_utc* are unused (nothing is written).
        """
        _ = (creds, token, now_utc)
        _LOG.error("groww.token.aws_readonly_mint_blocked")
        raise TokenBrokerReadOnlyError(
            "No Groww token in AWS Parameter Store "
            "(/tickvault/prod/groww/access-token) — deploy the minter "
            "(deploy/aws_token_minter/deploy.sh) first. BruteX is a read-only consumer of "
            "the shared Groww access token (a dedicated Lambda mints it); it reads the token "
            "ONLY from AWS and never mints locally (DEC-AWS-ONLY-CREDS-001)."
        )


def build_readonly_token_store() -> ParameterStoreTokenStore:
    """Build the read-only Parameter-Store token store from the environment.

    A missing boto3 extra / SSM-client build failure raises (from
    :func:`~brutex.adapters.groww.aws_client.build_real_aws_client`); the caller
    (:func:`~brutex.adapters.groww.token_store.resolve_token_store`) maps ANY such failure to
    a clear fail-fast :class:`~brutex.adapters.groww.errors.GrowwAuthError` — AWS is the SOLE
    token source, there is NO local fallback (DEC-AWS-ONLY-CREDS-001). The real boto3 client
    build is ``# pragma: no cover``; a test injects a fake via
    :func:`build_readonly_token_store_with`.
    """
    client = build_real_aws_client(
        resolve_token_broker_region()
    )  # pragma: no cover — real boto3 client build
    return build_readonly_token_store_with(client)  # pragma: no cover — wraps the real client


def build_readonly_token_store_with(client: AwsClient) -> ParameterStoreTokenStore:
    """Build the read-only token store around an injected *client* (the tested seam).

    Resolves the shared access-token path from the environment
    (``BRUTEX_TOKEN_PARAM_ACCESS_TOKEN``, default ``/tickvault/prod/groww/access-token``).
    This helper is the injectable core so the fake drives it to 100%.
    """
    token_param = resolve_token_param_path(
        GROWW_TOKEN_PARAM_ACCESS_TOKEN_ENV, GROWW_TOKEN_PARAM_ACCESS_TOKEN_DEFAULT
    )
    return ParameterStoreTokenStore(client=client, token_param=token_param)


__all__ = [
    "ParameterStoreTokenStore",
    "TokenBrokerReadOnlyError",
    "TokenLoadMiss",
    "build_readonly_token_store",
    "build_readonly_token_store_with",
]
