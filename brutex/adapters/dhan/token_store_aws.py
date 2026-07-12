"""DhanHQ v2 token store — the SELF-MINTED daily token in an AWS SecureString (read + WRITE).

DEC-DHAN-AUTH-001. Unlike the Groww token store (read-only — a Lambda mints; BruteX only
reads), Dhan is assumed BruteX-EXCLUSIVE: BruteX self-mints the daily token and WRITES it
back to a single SecureString (``/tickvault/prod/dhan/access-token``). So this store is the
FIRST BruteX SSM writer, backed by the write-capable
:class:`~brutex.adapters.aws_client.AwsClientWriter` — its ``readonly`` is ``False`` (the
security hinge: the IAM grant is ``ssm:PutParameter`` on EXACTLY this one parameter; the
Groww namespace stays read-only).

The token is wrapped in a strict JSON ENVELOPE ``{"v":1,"token":…,"minted_at_utc":…,
"source":…}`` so the expiry budget is computed from OUR mint instant, NEVER decoded from a
JWT claim (claims UNVERIFIED). No disk cache and no 06:00-IST session key (those are Groww
read-only-broker artifacts). Logs carry only the parameter path + ``sha256(token)[:8]`` — the
token bytes never touch a log.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

import structlog

from brutex.adapters.aws_client import (
    SSM_PARAMETER_NOT_FOUND,
    AwsClientWriter,
    resolve_token_broker_region,
    resolve_token_param_path,
)
from brutex.adapters.dhan.errors import DhanTokenStoreError
from brutex.config.credentials_aws import (
    DHAN_TOKEN_PARAM_ACCESS_TOKEN_DEFAULT,
    DHAN_TOKEN_PARAM_ACCESS_TOKEN_ENV,
)

_LOG = structlog.get_logger("brutex.adapters.dhan.token_store_aws")

#: The envelope schema version — bumped only on an incompatible shape change.
_ENVELOPE_VERSION: int = 1


def token_fingerprint(token: str) -> str:
    """Return ``sha256(token)[:8]`` — a stable, NON-reversible token id for logs/audit. O(1)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]


@dataclass(frozen=True, slots=True)
class DhanTokenEnvelope:
    """The stored daily-token record — token + OUR mint instant + how it was obtained.

    Attributes
    ----------
    token
        The access-token JWT (never logged raw — logs use :func:`token_fingerprint`).
    minted_at_utc
        OUR mint/renew instant (the expiry budget anchor — never a JWT claim).
    source
        ``"mint"`` or ``"renew"``.
    """

    token: str
    minted_at_utc: datetime
    source: Literal["mint", "renew"]


@dataclass(frozen=True, slots=True)
class DhanTokenLoadMiss:
    """WHY a token load missed — the reason-carrying seam.

    Attributes
    ----------
    kind
        ``"absent"`` (the read succeeded, the parameter is missing/blank), ``"error"`` (the
        read RAISED — IAM/KMS/network; the token may well exist), or ``"corrupt"`` (the read
        returned a value that is not a valid envelope).
    error_type
        The raising exception's class name for ``kind="error"``, else ``None``.
    detail
        A short, secret-free description (never the token value).
    """

    kind: Literal["absent", "error", "corrupt"]
    error_type: str | None
    detail: str


def encode_envelope(envelope: DhanTokenEnvelope) -> str:
    """Serialize *envelope* to the strict JSON codec. O(1)."""
    return json.dumps(
        {
            "v": _ENVELOPE_VERSION,
            "token": envelope.token,
            "minted_at_utc": envelope.minted_at_utc.astimezone(UTC).isoformat(),
            "source": envelope.source,
        },
        separators=(",", ":"),
    )


def decode_envelope(text: str) -> DhanTokenEnvelope:
    """Parse the strict JSON codec, raising :class:`ValueError` on ANY bad shape.

    Enforces: valid JSON object, ``v == 1``, a non-blank string ``token``, a parseable ISO
    ``minted_at_utc`` (coerced to UTC), and ``source`` in ``{"mint","renew"}``. The caller
    (:meth:`DhanParameterStoreTokenStore.load_with_reason`) maps a raise to a ``"corrupt"``
    miss so a poisoned parameter degrades to a re-mint, never a crash. O(1).
    """
    try:
        raw = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"token envelope is not valid JSON: {type(exc).__name__}") from exc
    if not isinstance(raw, dict):
        raise ValueError("token envelope is not a JSON object")
    if raw.get("v") != _ENVELOPE_VERSION:
        raise ValueError(f"unsupported token envelope version: {raw.get('v')!r}")
    token = raw.get("token")
    if not isinstance(token, str) or not token.strip():
        raise ValueError("token envelope has no non-blank token")
    minted_raw = raw.get("minted_at_utc")
    if not isinstance(minted_raw, str):
        raise ValueError("token envelope has no minted_at_utc string")
    try:
        minted_at = datetime.fromisoformat(minted_raw)
    except ValueError as exc:
        raise ValueError(f"token envelope minted_at_utc is not ISO-8601: {exc}") from exc
    minted_at = (
        minted_at.astimezone(UTC) if minted_at.tzinfo is not None else minted_at.replace(tzinfo=UTC)
    )
    source = raw.get("source")
    if source not in ("mint", "renew"):
        raise ValueError(f"token envelope has an invalid source: {source!r}")
    return DhanTokenEnvelope(token=token.strip(), minted_at_utc=minted_at, source=source)


class DhanParameterStoreTokenStore:
    """Read + WRITE the self-minted Dhan token in a single SecureString (``readonly is False``).

    Parameters
    ----------
    client
        The injected write-capable :class:`~brutex.adapters.aws_client.AwsClientWriter` (real
        boto3 SSM in production; a fake in tests).
    param_path
        The full Parameter-Store path (``None`` resolves the env override / default).
    """

    def __init__(self, *, client: AwsClientWriter, param_path: str | None = None) -> None:
        self._client = client
        self._param_path = (
            param_path
            if param_path is not None
            else resolve_token_param_path(
                DHAN_TOKEN_PARAM_ACCESS_TOKEN_ENV, DHAN_TOKEN_PARAM_ACCESS_TOKEN_DEFAULT
            )
        )

    @property
    def token_param(self) -> str:
        """The Parameter-Store path of the Dhan access token (for diagnostics)."""
        return self._param_path

    @property
    def readonly(self) -> bool:
        """Always ``False`` — BruteX self-mints and WRITES the Dhan token here."""
        return False

    def load(self) -> DhanTokenEnvelope | None:
        """Return the stored envelope, or ``None`` on ANY miss (NEVER raises).

        A read error / absent value / corrupt envelope all degrade to ``None`` (the reauth
        spine then mints). Logs the param path + a fingerprint on a hit; a raised read logs
        WARNING; an absent/corrupt value logs DEBUG.
        """
        envelope, miss = self.load_with_reason()
        if miss is not None:
            if miss.kind == "error":
                _LOG.warning(
                    "dhan.token.load_failed", param=self._param_path, error_type=miss.error_type
                )
            else:
                _LOG.debug("dhan.token.load_miss", param=self._param_path, kind=miss.kind)
            return None
        return envelope

    def load_with_reason(self) -> tuple[DhanTokenEnvelope | None, DhanTokenLoadMiss | None]:
        """Read the token, RETURNING the miss reason instead of swallowing it.

        Exactly one side of the pair is non-``None``. Distinguishes ``"absent"`` (run the
        mint), ``"error"`` (reader-side IAM/KMS/network — inspect access), and ``"corrupt"``
        (a poisoned parameter — re-mint). O(1).
        """
        try:
            value = self._client.get_parameter(name=self._param_path, with_decryption=True)
        except Exception as exc:  # pylint: disable=broad-exception-caught — a raise ⇒ miss (absent vs hard failure)
            if type(exc).__name__ == SSM_PARAMETER_NOT_FOUND:
                return None, DhanTokenLoadMiss(
                    kind="absent",
                    error_type=type(exc).__name__,
                    detail=f"parameter {self._param_path} not found (ParameterNotFound)",
                )
            return None, DhanTokenLoadMiss(
                kind="error", error_type=type(exc).__name__, detail=type(exc).__name__
            )
        if value is None or not value.strip():
            return None, DhanTokenLoadMiss(
                kind="absent",
                error_type=None,
                detail=f"parameter {self._param_path} absent or blank",
            )
        try:
            envelope = decode_envelope(value)
        except ValueError as exc:
            return None, DhanTokenLoadMiss(kind="corrupt", error_type=None, detail=str(exc))
        _LOG.debug(
            "dhan.token.load_hit",
            param=self._param_path,
            fingerprint=token_fingerprint(envelope.token),
        )
        return envelope, None

    def store(self, envelope: DhanTokenEnvelope) -> None:
        """Write *envelope* to the SecureString (``PutParameter`` SecureString, Overwrite=True).

        A write failure raises a SCRUBBED :class:`~brutex.adapters.dhan.errors.\
        DhanTokenStoreError` (never the token). Logs the param path + a fingerprint only.
        """
        try:
            self._client.put_parameter(
                name=self._param_path,
                value=encode_envelope(envelope),
                secure=True,
                overwrite=True,
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught — scrubbed below; token never logged
            _LOG.error(
                "dhan.token.store_failed", param=self._param_path, error_type=type(exc).__name__
            )
            raise DhanTokenStoreError(
                f"failed to write the Dhan access token to {self._param_path}: "
                f"{type(exc).__name__}",
                secrets=(envelope.token,),
            ) from None
        _LOG.info(
            "dhan.token.store",
            param=self._param_path,
            fingerprint=token_fingerprint(envelope.token),
            source=envelope.source,
        )


def build_dhan_token_store() -> DhanParameterStoreTokenStore:  # pragma: no cover — real boto3
    """Build the production Dhan token store (real boto3 write-capable client)."""
    from brutex.adapters.aws_client import build_real_aws_writer_client

    client = build_real_aws_writer_client(resolve_token_broker_region())
    return DhanParameterStoreTokenStore(client=client)


__all__ = [
    "DhanParameterStoreTokenStore",
    "DhanTokenEnvelope",
    "DhanTokenLoadMiss",
    "build_dhan_token_store",
    "decode_envelope",
    "encode_envelope",
    "token_fingerprint",
]
