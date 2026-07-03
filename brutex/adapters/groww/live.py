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
)
from brutex.adapters.groww.client import GrowwClient, GrowwSDK, _noop_hook, _noop_pace
from brutex.adapters.groww.constants import GROWW_MAX_REAUTHS, GROWW_PULL_CALL_TIMEOUT_S
from brutex.adapters.groww.errors import GrowwAuthError
from brutex.adapters.groww.pool import ClientPool
from brutex.adapters.groww.reauth import SdkRefresher, _real_sdk_factory
from brutex.adapters.groww.token_cache import CachedToken
from brutex.adapters.groww.token_store import resolve_token_store

#: Type aliases for the injected daily-token cache hooks (DEC-TOKEN-BROKER-AWS-001).
TokenCacheLoad = Callable[..., CachedToken | None]
TokenCacheStore = Callable[..., bool]


class KitMintBlockedError(GrowwAuthError):
    """A Groww token MINT was reached while the live-proof kit's no-mint guard is active.

    ``brutex doctor --live`` (DEC-DOCTOR-LIVE-PROOF-KIT-001) NEVER mints a Groww
    token: under the recorded single-active-token behaviour (UNVERIFIED — treated
    as true, register rows R7/M4) a kit-side mint could REVOKE the shared token
    the minter Lambda / TickVault own, corrupting the very pull the kit is
    diagnosing. The kit therefore wires :func:`mint_blocked_token_factory` /
    :func:`mint_blocked_totp_minter` into :func:`build_live_client`, making a
    mint PHYSICALLY impossible (safety invariant 2 of the kit spec). Reaching
    this error means the kit tried to build a client without an SSM token —
    re-run with ``--mint-anyway`` to override (prints a red warning first).
    """


def mint_blocked_token_factory(api_key: str, totp: str) -> str:
    """A ``token_factory`` that REFUSES to mint (the kit's no-mint guard, R7/M4)."""
    _ = (api_key, totp)
    raise KitMintBlockedError(
        "doctor --live never mints a Groww token (a mint may revoke the shared "
        "Lambda/TickVault token — single-active-token, UNVERIFIED but treated as "
        "true). Re-run with --mint-anyway to override."
    )


def mint_blocked_totp_minter(secret: str) -> str:
    """A ``totp_minter`` that REFUSES to mint (belt-and-suspenders over the factory)."""
    _ = secret
    raise KitMintBlockedError(
        "doctor --live never mints a TOTP code for token issuance (no-mint guard). "
        "Re-run with --mint-anyway to override."
    )


def installed_growwapi_version() -> str | None:
    """Return the installed ``growwapi`` distribution version, or ``None`` when absent.

    The G2 telemetry seam: the SDK version is the single most reality-divergent
    variable on the operator's Mac and appears in no log today. Kit stage 0
    prints it; the eventual client-build logging fix reuses this helper.
    """
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("growwapi")
    except PackageNotFoundError:
        return None


def _resolve_token_cache_hooks(
    token_cache_load: TokenCacheLoad | None,
    token_cache_store: TokenCacheStore | None,
) -> tuple[TokenCacheLoad, TokenCacheStore, TokenCacheLoad | None]:
    """Return ``(load, store, readonly_reread)`` hooks to thread into :class:`SdkRefresher`.

    An explicit ``load`` AND ``store`` win (tests / a caller wiring a specific backend), and
    then ``readonly_reread`` is ``None`` (the explicit-hook path does NOT resolve the store,
    so the caller wires the read-only re-read itself when needed); otherwise BOTH come from
    :func:`~brutex.adapters.groww.token_store.resolve_token_store` — the shared read-only AWS
    Parameter-Store token, the SOLE source (DEC-AWS-ONLY-CREDS-001). Resolving the store ONCE
    here (not per hook) keeps the load + store from the SAME backend instance (no split-brain).

    ``readonly_reread`` (DEC-BROKER-READONLY-REAUTH-REREAD-001) is the resolved store's
    ``load`` IFF that store reports :attr:`~brutex.adapters.groww.token_store.TokenStore.\
    readonly` — so a mid-run reauth on the read-only broker RE-READS the Lambda-minted token
    instead of hitting the fail-loud ``store`` (which would need a manual restart); a
    non-read-only backend (or the explicit-hook path) leaves it ``None`` (the byte-identical
    local-mint reauth).
    """
    if token_cache_load is not None and token_cache_store is not None:
        return (token_cache_load, token_cache_store, None)
    store = resolve_token_store()
    readonly_reread = store.load if store.readonly else None
    return (token_cache_load or store.load, token_cache_store or store.store, readonly_reread)


def build_live_client(
    creds: GrowwCredentials,
    *,
    token_factory: Callable[[str, str], str] = _real_token_factory,
    totp_minter: Callable[[str], str] = _real_totp,
    sdk_factory: Callable[[str], GrowwSDK] = _real_sdk_factory,
    pace: Callable[[], None] | None = None,
    on_rate_limit: Callable[[], None] | None = None,
    on_success: Callable[[], None] | None = None,
    token_cache_load: TokenCacheLoad | None = None,
    token_cache_store: TokenCacheStore | None = None,
    readonly_reread: TokenCacheLoad | None = None,
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
    readonly_reread
        OPTIONAL explicit read-only-broker RE-READ hook (DEC-BROKER-READONLY-REAUTH-REREAD-001).
        ``None`` (the default) resolves it automatically from the token store (the read-only
        AWS store's ``load`` when the resolved backend reports ``readonly``); a caller (tests)
        may pass one explicitly, which wins. Threaded into :class:`SdkRefresher` so a mid-run
        reauth RE-READS the Lambda-minted token instead of hitting the fail-loud ``store``.

    Returns
    -------
    GrowwClient
        A neutral client wrapping the freshly built SDK + the limiter hooks.
    """
    # The refresher is the ONE mint path (DEC-GROWW-TOKEN-REAUTH-001): it reads the
    # INITIAL token + builds the SDK at construction, AND auto-refreshes on the 06:00 IST
    # daily token reset so the overnight pull self-heals without a manual restart. The daily-
    # token cache hooks default to the shared read-only AWS Parameter-Store token — the SOLE
    # source (DEC-AWS-ONLY-CREDS-001); an absent token / unreachable AWS surfaces as a
    # redacting GrowwAuthError straight from the store (no local mint). On that read-only
    # backend the reauth RE-READS the Lambda-minted token via `readonly_reread` instead of
    # minting (DEC-BROKER-READONLY-REAUTH-REREAD-001) — a self-heal, not a fail-loud restart.
    load_hook, store_hook, resolved_reread = _resolve_token_cache_hooks(
        token_cache_load, token_cache_store
    )
    refresher = SdkRefresher(
        creds,
        token_factory=token_factory,
        totp_minter=totp_minter,
        sdk_factory=sdk_factory,
        token_cache_load=load_hook,
        token_cache_store=store_hook,
        readonly_reread=readonly_reread or resolved_reread,
    )
    # A truthy callable wins; ``None`` falls back to the client's no-op default
    # (offline / unpaced) — every key is a ``Callable[[], None]`` (mypy-clean).
    return GrowwClient(
        sdk=refresher.current_sdk(),
        refresher=refresher,
        refresher_index=0,
        pace=pace or _noop_pace,
        on_rate_limit=on_rate_limit or _noop_hook,
        on_success=on_success or _noop_hook,
        # FREEZE GUARD (DEC-GROWW-PULL-CALL-DEADLINE-001): bound every historical
        # SDK call so a stalled connection can never hang the pull — it raises a
        # retryable timeout instead, which the client's bounded retry loop handles.
        call_timeout_seconds=GROWW_PULL_CALL_TIMEOUT_S,
        # FP-1 hardening (DEC-GROWW-AUTH-HALT-001): the PRODUCTION re-auth budget is
        # 2 (the GrowwClient default stays 1 for offline/tests) so the 06:00 IST
        # rotation's possible fresh-token propagation lag cannot false-exhaust the
        # budget into a terminal auth halt; a truly-dead credential still halts.
        max_reauths=GROWW_MAX_REAUTHS,
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
    token_cache_load: TokenCacheLoad | None = None,
    token_cache_store: TokenCacheStore | None = None,
    readonly_reread: TokenCacheLoad | None = None,
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
    # ONE shared refresher across ALL leaves (DEC-GROWW-TOKEN-REAUTH-001): it mints the
    # INITIAL token ONCE and fans `safe_size` INDEPENDENT SDK connections off it (the
    # single mint path), and on the 06:00 IST daily token reset a single re-mint rebuilds
    # EVERY leaf's connection at once (each leaf binds its own SDK index via `sdk_at`), so
    # the overnight pull self-heals without a manual restart, WITHOUT a token stampede
    # (single-flight), AND the pool keeps its K-way concurrency (K distinct sockets).
    load_hook, store_hook, resolved_reread = _resolve_token_cache_hooks(
        token_cache_load, token_cache_store
    )
    refresher = SdkRefresher(
        creds,
        size=safe_size,
        token_factory=token_factory,
        totp_minter=totp_minter,
        sdk_factory=sdk_factory,
        token_cache_load=load_hook,
        token_cache_store=store_hook,
        readonly_reread=readonly_reread or resolved_reread,
    )
    # ONE shared meter across all leaves → the dashboard's achieved-rate gauge aggregates
    # across every connection (in_flight rises toward `size`, not split per-connection).
    meter = CallMeter()
    leaves = [
        GrowwClient(
            sdk=refresher.sdk_at(index),
            refresher=refresher,
            refresher_index=index,
            pace=pace or _noop_pace,
            on_rate_limit=on_rate_limit or _noop_hook,
            on_success=on_success or _noop_hook,
            call_timeout_seconds=GROWW_PULL_CALL_TIMEOUT_S,
            # FP-1 hardening (DEC-GROWW-AUTH-HALT-001): production re-auth budget 2
            # on EVERY leaf (mirrors build_live_client) — the 06:00 IST rotation lag
            # cannot false-exhaust one leaf's budget into a pull-wide auth halt.
            max_reauths=GROWW_MAX_REAUTHS,
            meter=meter,
        )
        for index in range(safe_size)
    ]
    return ClientPool(leaves)


__all__ = [
    "KitMintBlockedError",
    "TokenCacheLoad",
    "TokenCacheStore",
    "build_live_client",
    "build_live_pool",
    "installed_growwapi_version",
    "mint_blocked_token_factory",
    "mint_blocked_totp_minter",
]
