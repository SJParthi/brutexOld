"""Groww live-client factory — compose creds → token → SDK → client.

DEC-DHAN-GROWW-REMIGRATE-001 (ADR-0009 §5). The dashboard "Test / verify" button
needs a LIVE :class:`~brutex.adapters.groww.client.GrowwClient` built from the
stored Groww credentials. This module is the single composition seam:

    GrowwCredentials → get_access_token(...) → sdk_factory(token) → GrowwClient

Every side-effecting step is an **injected callable** (``token_factory`` /
``totp_minter`` flow through to :func:`get_access_token`, and ``sdk_factory``
builds the real ``GrowwAPI``), so the whole composition is unit-tested with fakes
— no ``growwapi`` / ``pyotp`` install and no network. The only
``# pragma: no cover`` line is the default ``sdk_factory`` that lazily imports
the real SDK (mirrors ``brutex.adapters.groww.auth``'s real shims).
"""

from __future__ import annotations

from collections.abc import Callable

from brutex.adapters.groww.auth import (
    GrowwCredentials,
    _real_token_factory,
    _real_totp,
    get_access_token,
)
from brutex.adapters.groww.client import GrowwClient, GrowwSDK


def _real_sdk_factory(token: str) -> GrowwSDK:  # pragma: no cover — needs growwapi + network
    """Default SDK factory: ``GrowwAPI(token)`` (lazy import of the real SDK)."""
    from growwapi import GrowwAPI  # type: ignore[import-not-found]

    return GrowwAPI(token)  # type: ignore[no-any-return]


def build_live_client(
    creds: GrowwCredentials,
    *,
    token_factory: Callable[[str, str], str] = _real_token_factory,
    totp_minter: Callable[[str], str] = _real_totp,
    sdk_factory: Callable[[str], GrowwSDK] = _real_sdk_factory,
) -> GrowwClient:
    """Build a live :class:`GrowwClient` from *creds*.

    Mints a TOTP access token (:func:`get_access_token`), hands it to
    *sdk_factory* to build the SDK instance, and wraps that in a
    :class:`GrowwClient`. A failed token issuance surfaces as
    :class:`~brutex.adapters.groww.errors.GrowwAuthError` (with a redacting
    ``safe_message``) straight from :func:`get_access_token`.

    Every side-effecting step is an injected callable; the defaults are the same
    real ``pyotp`` / SDK shims :mod:`brutex.adapters.groww.auth` uses (the only
    ``# pragma: no cover`` lines), so this composition is unit-tested with fakes.

    Parameters
    ----------
    creds
        The Groww TOTP api key + shared secret.
    token_factory
        Access-token issuer (default: the real ``GrowwAPI.get_access_token``).
    totp_minter
        TOTP code minter (default: the real ``pyotp.TOTP(...).now()``).
    sdk_factory
        Builds the SDK instance from the issued token (default: ``GrowwAPI(token)``).

    Returns
    -------
    GrowwClient
        A neutral client wrapping the freshly built SDK.
    """
    token_info = get_access_token(creds, token_factory=token_factory, totp_minter=totp_minter)
    sdk = sdk_factory(token_info.access_token)
    return GrowwClient(sdk=sdk)


__all__ = ["build_live_client"]
