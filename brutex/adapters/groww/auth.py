"""Groww auth — TOTP access-token issuance (the AUTOMATED unattended flow).

DEC-DHAN-GROWW-REMIGRATE-001 (ADR-0009 §5, PR-E). Groww's TOTP flow
(``docs/groww/api-reference/01-introduction.md`` §"TOTP Flow") mints an access
token from an API key + a TOTP code; the API-key+secret flow is REFUSED because
it "requires daily approval" (violates CLAUDE.md §11 AUTOMATED "zero clicks/day").

Both side-effecting calls — ``pyotp.TOTP(secret).now()`` and
``GrowwAPI.get_access_token(...)`` — are **injected callables**, so every branch
is unit-testable without ``pyotp`` / ``growwapi`` installed (the optional-extra
resilience, DEC-GROWW-DEPS-REINSTATE-001). The real SDK/pyotp default shims are
the only ``# pragma: no cover`` lines, mirroring the canonical adapter auth layout.

Secret hygiene: :class:`GrowwAuthError` is raised with a ``safe_message`` so
``str(exc)`` (which the audit log / dashboard persist) NEVER leaks the api_key or
TOTP secret.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final

from brutex.adapters.groww.errors import GrowwAuthError

#: Env var carrying the Groww TOTP api key.
ENV_API_KEY: Final[str] = "BRUTEX_GROWW_API_KEY"
#: Env var carrying the Groww TOTP shared secret (used to mint the 6-digit code).
ENV_TOTP_SECRET: Final[str] = "BRUTEX_GROWW_TOTP_SECRET"  # noqa: S105  # env var name, not a secret


@dataclass(frozen=True, slots=True)
class GrowwCredentials:
    """A Groww TOTP api key + its shared secret (held only to mint codes)."""

    api_key: str
    totp_secret: str


@dataclass(frozen=True, slots=True)
class TokenInfo:
    """A freshly issued Groww access token (no documented expiry under TOTP)."""

    access_token: str


def _real_totp(secret: str) -> str:  # pragma: no cover — needs pyotp + a real secret
    """Default TOTP minter: ``pyotp.TOTP(secret).now()`` (lazy import)."""
    import pyotp  # type: ignore[import-not-found]

    return str(pyotp.TOTP(secret).now())


def _real_token_factory(api_key: str, totp: str) -> str:  # pragma: no cover — needs growwapi
    """Default token factory: ``GrowwAPI.get_access_token(...)`` (lazy import)."""
    from growwapi import GrowwAPI  # type: ignore[import-not-found]

    return str(GrowwAPI.get_access_token(api_key=api_key, totp=totp))


def resolve_groww_credentials(env: Mapping[str, str] | None = None) -> GrowwCredentials:
    """Read Groww TOTP credentials from the environment.

    Raises :class:`GrowwAuthError` (naming the missing var(s), never their values)
    if either is absent/blank.
    """
    source = os.environ if env is None else env
    api_key = (source.get(ENV_API_KEY) or "").strip()
    totp_secret = (source.get(ENV_TOTP_SECRET) or "").strip()
    missing = [
        name
        for name, value in ((ENV_API_KEY, api_key), (ENV_TOTP_SECRET, totp_secret))
        if not value
    ]
    if missing:
        raise GrowwAuthError(
            f"missing Groww credentials: {missing}",
            safe_message=f"missing Groww credentials: {missing}",
        )
    return GrowwCredentials(api_key=api_key, totp_secret=totp_secret)


def groww_creds_present(env: Mapping[str, str] | None = None) -> bool:
    """True if both Groww credentials are present (dry-run gate; never raises)."""
    try:
        resolve_groww_credentials(env)
    except GrowwAuthError:
        return False
    return True


def get_access_token(
    creds: GrowwCredentials,
    *,
    token_factory: Callable[[str, str], str] = _real_token_factory,
    totp_minter: Callable[[str], str] = _real_totp,
) -> TokenInfo:
    """Mint a TOTP code and issue a Groww access token.

    Both side effects are injected (defaults wrap the real pyotp/SDK calls). Any
    failure is wrapped as a :class:`GrowwAuthError` with a redacting
    ``safe_message`` so the secret never reaches a log.
    """
    totp = totp_minter(creds.totp_secret)
    try:
        token = token_factory(creds.api_key, totp)
    except Exception as exc:
        raise GrowwAuthError(
            f"Groww access-token issuance failed: {exc}",
            safe_message="Groww access-token issuance failed",
        ) from exc
    return TokenInfo(access_token=token)


__all__ = [
    "ENV_API_KEY",
    "ENV_TOTP_SECRET",
    "GrowwCredentials",
    "TokenInfo",
    "get_access_token",
    "groww_creds_present",
    "resolve_groww_credentials",
]
