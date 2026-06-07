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

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Final

import structlog

from brutex.adapters.dhan.client import DhanClient
from brutex.adapters.dhan.constants import ENDPOINT_RENEW_TOKEN
from brutex.adapters.dhan.errors import DhanAuthError, DhanError

if TYPE_CHECKING:
    from structlog.stdlib import BoundLogger

#: Module logger for the best-effort proactive-renew path (DEC-DHAN-AUTH-FAILFAST-
#: RENEW-001). Emits ``auth.renewed`` INFO on a successful +24h refresh and ONE
#: ``auth.renew_failed`` WARNING when RenewToken refuses (an expired / TOTP-minted
#: token) — never raising, so renewal can NEVER block or abort the pull.
_LOG = structlog.get_logger("brutex.adapters.dhan.auth")

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
    """Resolve credentials env-first, then the saved credentials file.

    Reads :data:`ENV_ACCESS_TOKEN` + :data:`ENV_CLIENT_ID` from *env*
    (defaults to :data:`os.environ`); for any field blank in the environment,
    falls back to the file written by the dashboard Credentials page / ``brutex
    auth set`` (:func:`brutex.config.credentials.resolve_dhan_file_credentials`).
    This is what makes a dashboard-saved credential actually enable a live pull
    (DEC-DHAN-CREDS-WIRING-001) — without it, creds set in the UI were ignored
    and every pull silently ran dry-run. Raises :class:`DhanAuthError` naming
    the missing variable(s) when neither env nor file supplies a field.
    """
    from brutex.config.credentials import CredentialsError, resolve_dhan_file_credentials

    try:
        creds = resolve_dhan_file_credentials(env)
    except CredentialsError as exc:
        raise DhanAuthError(str(exc)) from exc
    return DhanCredentials(access_token=creds.access_token, client_id=creds.client_id)


def renew_token(client: DhanClient, *, pace: Callable[[], None] | None = None) -> TokenInfo:
    """Refresh the active (web-generated) token via ``/v2/RenewToken`` (+24h).

    Parameters
    ----------
    client
        The configured :class:`DhanClient` performing the network refresh.
    pace
        Optional rate-bucket acquire hook gating the **initial** physical POST
        (defence-in-depth, DEC-DHAN-RATE-RESIDUAL-HARDEN-001). ``DhanClient``
        already gates each fast-retry through its own :attr:`~DhanClient.pace`
        field (DEC-DHAN-RATE-REALTIME-GUARANTEE-001, MIGRATION.md #545), but the
        FIRST attempt is paced by the caller — for the ingest spine that is the
        token bucket, but ``renew_token`` has no such pacing today (a LATENT
        residual: no production caller wires it into a live pull). Passing a
        ``pace`` here makes EVERY physical Dhan POST from this path
        bucket-bounded (≤ current r/s ≤ 5, CLAUDE.md §2 Rule 6) the moment it
        IS wired live. ``None`` (the default) falls back to the client's own
        ``pace`` hook — itself a no-op unless a controller is injected — so
        current callers are byte-unchanged.

    Raises
    ------
    DhanAuthError
        If the response lacks a string ``accessToken`` / ``expiryTime`` (e.g. an
        expired or TOTP-minted token that ``RenewToken`` refuses).
    """
    # Gate the INITIAL physical POST through the shared rate bucket (or the
    # client's own no-op default) BEFORE the call — the client's fast-retry path
    # gates each subsequent attempt itself (DEC-DHAN-RATE-RESIDUAL-HARDEN-001).
    (pace or client.pace)()
    payload = client.post_json(ENDPOINT_RENEW_TOKEN, {})
    token = payload.get("accessToken")
    expiry = payload.get("expiryTime")
    if not isinstance(token, str) or not isinstance(expiry, str):
        raise DhanAuthError("RenewToken response missing string accessToken/expiryTime")
    return TokenInfo(access_token=token, expiry_time=expiry)


def maybe_renew_token(
    client: DhanClient,
    *,
    pace: Callable[[], None] | None = None,
    log: BoundLogger | None = None,
) -> TokenInfo | None:
    """Best-effort proactive ``RenewToken`` at pull start — NEVER raises.

    ``RenewToken`` only refreshes an *active* token (an already-expired token
    errors — it CANNOT revive a dead one), so the wiring calls this PROACTIVELY at
    the start of a long live pull to push the 24h expiry ``+24h`` BEFORE the pull
    crosses the wall (DEC-DHAN-AUTH-FAILFAST-RENEW-001). It is strictly
    best-effort: a renewal failure is logged once and swallowed so it can never
    block or abort the pull — the pull simply proceeds on the existing token (and
    if that token then dies mid-pull, Piece 1's fail-fast guard handles it).

    Parameters
    ----------
    client
        The configured :class:`DhanClient` performing the network refresh.
    pace
        Optional rate-bucket acquire hook gating the physical POST, forwarded to
        :func:`renew_token` (the shared token bucket, so this physical Dhan POST
        is ``≤ current r/s ≤ 5`` — CLAUDE.md §2 Rule 6). ``None`` falls back to
        the client's own ``pace`` hook.
    log
        Optional structlog logger for the ``auth.renewed`` / ``auth.renew_failed``
        events (defaults to this module's logger).

    Returns
    -------
    TokenInfo | None
        The freshly renewed :class:`TokenInfo` (a ``+24h`` access token) on
        success; ``None`` when ``RenewToken`` refused (logged once, swallowed).
    """
    logger = log if log is not None else _LOG
    try:
        token = renew_token(client, pace=pace)
    except DhanError as exc:
        # RenewToken refused — the token may already be expired or TOTP-minted
        # (RenewToken only refreshes an ACTIVE web-generated token). Best-effort:
        # log ONE warning and proceed on the existing token; NEVER raise.
        logger.warning(
            "auth.renew_failed",
            reason=str(exc),
            note="RenewToken refused (token may be expired or TOTP-minted); proceeding on existing token",
        )
        return None
    logger.info("auth.renewed", expiry_time=token.expiry_time)
    return token


def with_token(creds: DhanCredentials, token: TokenInfo) -> DhanCredentials:
    """Return new credentials with the renewed access token applied."""
    return replace(creds, access_token=token.access_token)


def dhan_creds_present(env: Mapping[str, str] | None = None) -> bool:
    """True iff Dhan credentials resolve — from env **or** the saved file.

    The dry-run gate the dashboard/cli use — no credentials means a live pull
    is impossible, so the caller forces ``dry_run``. Reads env-first then the
    dashboard-saved file (DEC-DHAN-CREDS-WIRING-001), so saving creds on the
    Credentials page flips the spot/options pull buttons to LIVE. A pure
    predicate: never raises (unlike :func:`resolve_dhan_credentials`).
    """
    from brutex.config.credentials import credentials_present

    return credentials_present(env)


__all__ = [
    "ENV_ACCESS_TOKEN",
    "ENV_CLIENT_ID",
    "DhanCredentials",
    "TokenInfo",
    "dhan_creds_present",
    "maybe_renew_token",
    "renew_token",
    "resolve_dhan_credentials",
    "with_token",
]
