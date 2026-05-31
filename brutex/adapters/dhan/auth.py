"""Dhan auth — manual access-token credentials + RenewToken refresh.

Default auth path (operator decision 2026-05-31): a **manual 24-hour
access token** (generated from web.dhan.co), delivered via environment and
kept alive with the **RenewToken** endpoint while active
(``docs/dhan-ref/02``). TOTP ``generateAccessToken`` remains a documented
alternative for fully unattended operation, but is not the default.

Honest envelope (Golden Rule #10): ``RenewToken`` only refreshes an
*active*, *web-generated* token — once a token expires it errors, and a
fresh token must be generated. So this path is "automated while active,
manual on expiry"; it does not satisfy the §11 AUTOMATED "zero clicks/day"
guarantee on its own (TOTP does). The adapter surfaces a clean
:class:`DhanAuthError` so the orchestrator can HALT + alert rather than
serve 401s.

This module is pure logic + env reads (no file I/O); the network refresh
goes through the injected-transport :class:`~brutex.adapters.dhan.client.DhanClient`,
so every branch is unit-testable without a network.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Final

from brutex.adapters.dhan.client import DhanClient
from brutex.adapters.dhan.constants import ENDPOINT_RENEW_TOKEN
from brutex.adapters.dhan.errors import DhanAuthError

#: Env var carrying the JWT access token (the value, not stored here).
ENV_ACCESS_TOKEN: Final[str] = "BRUTEX_DHAN_ACCESS_TOKEN"  # noqa: S105  # env var name, not a secret

#: Env var carrying the Dhan client id.
ENV_CLIENT_ID: Final[str] = "BRUTEX_DHAN_CLIENT_ID"


@dataclass(frozen=True, slots=True)
class DhanCredentials:
    """A Dhan client id + its current access token."""

    access_token: str
    client_id: str


@dataclass(frozen=True, slots=True)
class TokenInfo:
    """A freshly issued/renewed token and its IST expiry timestamp."""

    access_token: str
    expiry_time: str


def resolve_dhan_credentials(env: Mapping[str, str] | None = None) -> DhanCredentials:
    """Resolve credentials from the environment.

    Reads :data:`ENV_ACCESS_TOKEN` + :data:`ENV_CLIENT_ID` from *env*
    (defaults to :data:`os.environ`). Raises :class:`DhanAuthError` naming
    every missing variable.
    """
    source = os.environ if env is None else env
    token = source.get(ENV_ACCESS_TOKEN, "").strip()
    client_id = source.get(ENV_CLIENT_ID, "").strip()
    missing = [
        name for name, val in ((ENV_ACCESS_TOKEN, token), (ENV_CLIENT_ID, client_id)) if not val
    ]
    if missing:
        raise DhanAuthError(f"missing Dhan credentials in env: {missing}")
    return DhanCredentials(access_token=token, client_id=client_id)


def renew_token(client: DhanClient) -> TokenInfo:
    """Refresh the active (web-generated) token via ``/v2/RenewToken`` (+24h).

    Raises :class:`DhanAuthError` if the response lacks a string
    ``accessToken`` / ``expiryTime`` (e.g. an expired or TOTP-minted token
    that ``RenewToken`` refuses).
    """
    payload = client.post_json(ENDPOINT_RENEW_TOKEN, {})
    token = payload.get("accessToken")
    expiry = payload.get("expiryTime")
    if not isinstance(token, str) or not isinstance(expiry, str):
        raise DhanAuthError("RenewToken response missing string accessToken/expiryTime")
    return TokenInfo(access_token=token, expiry_time=expiry)


def with_token(creds: DhanCredentials, token: TokenInfo) -> DhanCredentials:
    """Return new credentials with the renewed access token applied."""
    return replace(creds, access_token=token.access_token)


__all__ = [
    "ENV_ACCESS_TOKEN",
    "ENV_CLIENT_ID",
    "DhanCredentials",
    "TokenInfo",
    "renew_token",
    "resolve_dhan_credentials",
    "with_token",
]
