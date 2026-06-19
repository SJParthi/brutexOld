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

from brutex.adapters.call_meter import CallMeter
from brutex.adapters.groww.auth import (
    GrowwCredentials,
    _real_token_factory,
    _real_totp,
    get_access_token,
)
from brutex.adapters.groww.client import GrowwClient, GrowwSDK, _noop_hook, _noop_pace
from brutex.adapters.groww.constants import GROWW_PULL_CALL_TIMEOUT_S
from brutex.adapters.groww.pool import ClientPool


def _real_sdk_factory(token: str) -> GrowwSDK:  # pragma: no cover — needs growwapi + network
    """Default SDK factory: ``GrowwAPI(token)`` (lazy import of the real SDK)."""
    from growwapi import GrowwAPI  # type: ignore[import-not-found, unused-ignore]

    return GrowwAPI(token)  # type: ignore[no-any-return]


def build_live_client(
    creds: GrowwCredentials,
    *,
    token_factory: Callable[[str, str], str] = _real_token_factory,
    totp_minter: Callable[[str], str] = _real_totp,
    sdk_factory: Callable[[str], GrowwSDK] = _real_sdk_factory,
    pace: Callable[[], None] | None = None,
    on_rate_limit: Callable[[], None] | None = None,
    on_success: Callable[[], None] | None = None,
) -> GrowwClient:
    """Build a live :class:`GrowwClient` from *creds*, wired to the rate controller.

    Mints a TOTP access token (:func:`get_access_token`), hands it to
    *sdk_factory* to build the SDK instance, and wraps that in a
    :class:`GrowwClient` **carrying the adaptive-limiter hooks** so EVERY Groww
    call (candles / expiries / contracts — spot AND the F&O chain) is paced +
    retried + decrement/recover uniformly (DEC-GROWW-RATELIMIT-RETRY-001). A failed
    token issuance surfaces as
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
    pace, on_rate_limit, on_success
        The shared rate controller's token-bucket acquire + decrement-on-429 +
        recover-on-success hooks (from
        :meth:`~brutex.adapters.rate_common.RateController`). ``None`` leaves the
        client's no-op defaults (offline / unpaced); the live pull paths inject the
        session controller so the chain pull can never bypass the limiter.

    Returns
    -------
    GrowwClient
        A neutral client wrapping the freshly built SDK + the limiter hooks.
    """
    token_info = get_access_token(creds, token_factory=token_factory, totp_minter=totp_minter)
    sdk = sdk_factory(token_info.access_token)
    # A truthy callable wins; ``None`` falls back to the client's no-op default
    # (offline / unpaced) — every key is a ``Callable[[], None]`` (mypy-clean).
    return GrowwClient(
        sdk=sdk,
        pace=pace or _noop_pace,
        on_rate_limit=on_rate_limit or _noop_hook,
        on_success=on_success or _noop_hook,
        # FREEZE GUARD (DEC-GROWW-PULL-CALL-DEADLINE-001): bound every historical
        # SDK call so a stalled connection can never hang the pull — it raises a
        # retryable timeout instead, which the client's bounded retry loop handles.
        call_timeout_seconds=GROWW_PULL_CALL_TIMEOUT_S,
    )


def build_live_pool(
    creds: GrowwCredentials,
    *,
    size: int = 1,
    token_factory: Callable[[str, str], str] = _real_token_factory,
    totp_minter: Callable[[str], str] = _real_totp,
    sdk_factory: Callable[[str], GrowwSDK] = _real_sdk_factory,
    pace: Callable[[], None] | None = None,
    on_rate_limit: Callable[[], None] | None = None,
    on_success: Callable[[], None] | None = None,
) -> ClientPool:
    """Build a live multi-connection :class:`ClientPool` of *size* leaves from *creds*.

    DEC-GROWW-CLIENT-POOL-001. Mints the TOTP access token **ONCE**
    (:func:`get_access_token`) and hands the SAME token to *sdk_factory* *size* times to
    build *size* INDEPENDENT SDK connections, each wrapped in a :class:`GrowwClient` that
    shares ONE rate-limiter pace + decrement/recover hooks AND ONE
    :class:`~brutex.adapters.call_meter.CallMeter` (the aggregate the dashboard reads). The
    worker threads then fan across the leaves (thread-affinity) so they stop serialising on
    a single socket — while the shared controller still caps the AGGREGATE rate (CLAUDE.md
    §2 Rule 6) and the meter proves the achieved number (Rule 1 / Rule 10).

    *size* is clamped to ``>= 1``; ``size=1`` is behaviour-identical to
    :func:`build_live_client` (one connection) but wrapped in a 1-leaf pool, so the live
    build sites take ONE code path regardless of the configured size. Concurrent use of one
    TOTP token across K connections is vendor-UNVERIFIED (ADR-0009 §3); the AIMD ladder +
    bounded retry absorb any vendor pushback (429 → step down → recover), and the operator
    can fall straight back to ``size=1`` via ``BRUTEX_GROWW_POOL_SIZE`` (no code change).

    Parameters mirror :func:`build_live_client` (every side-effecting step is an injected
    callable, so the whole composition is unit-tested with fakes — no ``growwapi`` / network)
    plus *size* (the number of independent connections).

    Returns
    -------
    ClientPool
        A pool of *size* leaf clients sharing one limiter + one meter.
    """
    safe_size = max(1, size)
    token_info = get_access_token(creds, token_factory=token_factory, totp_minter=totp_minter)
    token = token_info.access_token
    # ONE shared meter across all leaves → the dashboard's achieved-rate gauge aggregates
    # across every connection (in_flight rises toward `size`, not split per-connection).
    meter = CallMeter()
    leaves = [
        GrowwClient(
            sdk=sdk_factory(token),
            pace=pace or _noop_pace,
            on_rate_limit=on_rate_limit or _noop_hook,
            on_success=on_success or _noop_hook,
            call_timeout_seconds=GROWW_PULL_CALL_TIMEOUT_S,
            meter=meter,
        )
        for _ in range(safe_size)
    ]
    return ClientPool(leaves)


__all__ = ["build_live_client", "build_live_pool"]
