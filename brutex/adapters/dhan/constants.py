"""DhanHQ v2 auth-plane constants — the pure-data lookup surface (AUTH ONLY, no data plane).

DEC-DHAN-AUTH-001 (MIGRATION.md #1755). Every value here is traceable to a verified source
(Golden Rule #1/#2): the official DhanHQ-py SDK (``dhan-oss/DhanHQ-py``, fetched live) +
the Phase-0 research (``scratchpad/phase0-dhan-auth.md``). Facts whose operational envelope
could NOT be confirmed live carry a ``*_UNVERIFIED`` suffix — the honesty requirement in the
identifier itself (ADR-0020 §3). Those sentinels are OBSERVE-ONLY: no auth decision branch
may import them (AST-pinned by ``test_dhan_constants``), so an unverified assumption can
never silently gate a mint/renew.

Data-plane caps (the 5 r/s / 100k-per-day Data-API limits, the 5-year history floor, the
90-day chunk) are DELIBERATELY ABSENT — they belong to PR-3 (the fetch adapter), not here.

No I/O, no mutable state.
"""

from __future__ import annotations

import os

# ── Endpoints (VERIFIED live in the official DhanHQ-py SDK) ──────────────────
#: DhanHQ v2 REST base (data-plane calls; PR-3 consumes it). Present here only as the
#: single source of the base URL.
DHAN_API_BASE: str = "https://api.dhan.co/v2/"
#: Headless PIN+TOTP mint endpoint (``generate_token(pin, totp)`` in the official SDK). The
#: secrets ride the QUERY STRING (``dhanClientId``/``pin``/``totp``) — so every transport
#: exception off this URL MUST be scrubbed (its query redacted) before it can be logged /
#: audited / re-raised (DEC-DHAN-AUTH-001; :func:`brutex.adapters.secret_scrub.redact_url_query`).
DHAN_AUTH_MINT_URL: str = "https://auth.dhan.co/app/generateAccessToken"
#: Token-renewal path, appended to :data:`DHAN_API_BASE` (``GET .../v2/RenewToken``). Headers
#: only — ``access-token`` + ``dhanClientId`` — NO query secrets. Works only on a still-ACTIVE
#: token (Phase-0 #7).
DHAN_RENEW_PATH: str = "RenewToken"
#: The auth header name Dhan expects on every API call — the minted JWT rides here.
DHAN_TOKEN_HEADER: str = "access-token"  # noqa: S105  # HTTP header name, not a secret
#: The client-id header name Dhan requires on RenewToken (+ profile).
DHAN_CLIENT_ID_HEADER: str = "dhanClientId"

# ── Token lifetime + refresh policy ─────────────────────────────────────────
#: Dhan access-token lifetime, seconds (24h). PARTIAL provenance: the 24-hour figure rests on
#: official-page search snippets + the SDK + the openalgo issue (Phase-0 #2), effective
#: 2025-10-01 per SEBI algo-trading guidelines — NOT a first-party direct-fetch. Used ONLY as
#: the budget ceiling in :mod:`token_lifecycle`; expiry is stamped from OUR mint instant, never
#: decoded from a JWT claim (claims UNVERIFIED).
DHAN_TOKEN_LIFETIME_S: int = 86_400
#: OUR refresh policy: re-mint/renew once the token is older than 20h. The 4h margin below the
#: 24h lifetime absorbs clock skew + a multi-hour pull that starts near the boundary — a policy
#: choice, not a vendor fact.
DHAN_TOKEN_REFRESH_AGE_S: int = 72_000

# ── Mint issuance retry (throttle / 5xx / network only) ─────────────────────
#: Bounded issuance-retry attempts for a throttle / transient transport failure (a TERMINAL
#: invalid-credential is NEVER retried). Mirrors the Groww GROWW_TOKEN_ISSUANCE_RETRY_* shape.
DHAN_TOKEN_ISSUANCE_RETRY_ATTEMPTS: int = 3
#: Base back-off seconds for the issuance retry (exponential from here).
DHAN_TOKEN_ISSUANCE_RETRY_BASE_S: float = 1.0
#: Back-off cap seconds so the total issuance wait stays well under a pull deadline.
DHAN_TOKEN_ISSUANCE_RETRY_CAP_S: float = 4.0

#: Consecutive TERMINAL mint failures that latch the lockdown (a single invalid-credential
#: latches immediately; this bounds the OTHER-terminal-failure class).
DHAN_MINT_LOCKDOWN_THRESHOLD: int = 3

#: Wall-clock deadline (seconds) for a single auth HTTP call — the freeze guard bound. A
#: hung mint/renew socket must not block the caller forever (mirrors the Groww reauth guard).
DHAN_AUTH_HTTP_TIMEOUT_S: float = 15.0

#: Kill-switch env for the RENEW optimization. Default ON; a junk value fails loud (never a
#: silent disable — the resolver raises). RENEW is only ever an optimization over MINT, so
#: disabling it merely forces a mint.
DHAN_RENEW_ENV: str = "BRUTEX_DHAN_RENEW"

# ── Honest UNVERIFIED sentinels (OBSERVE-ONLY — never gate a decision) ───────
# AST-pinned: no auth decision branch imports these. They record what Phase-0 could NOT
# confirm live so a future credentialed probe (docs/runbooks/dhan-auth.md) can flip them.
#: Whether the PIN+TOTP mint requires TOTP-2FA pre-enabled / an active Data-API subscription
#: (Phase-0 #8 caveat). ``None`` = unknown; never asserted.
DHAN_MINT_PRECONDITIONS_UNVERIFIED: None = None
#: Whether/how Dhan throttles the mint endpoint itself (no documented auth-endpoint rate).
DHAN_AUTH_RATE_LIMIT_UNVERIFIED: None = None
#: RenewToken's "only works on a still-ACTIVE token" semantics (Phase-0 #7) — believed True,
#: not first-party-confirmed. Recorded True as the CONSERVATIVE assumption (drives nothing).
DHAN_RENEW_REQUIRES_ACTIVE_TOKEN_UNVERIFIED: bool = True
#: Whether a fresh mint INVALIDATES the prior token (single-active-token behaviour). ``None``
#: = unknown — the reason the cross-process SSM re-read brake is mandatory before a mint.
DHAN_MINT_INVALIDATES_PRIOR_TOKEN_UNVERIFIED: None = None


def resolve_dhan_renew_enabled(raw: str | None = None) -> bool:
    """Resolve the RENEW kill switch: default ON; junk fails LOUD (never a silent disable).

    Reads :data:`DHAN_RENEW_ENV` (``BRUTEX_DHAN_RENEW``). Unset/blank ⇒ ``True`` (renew
    enabled). A recognised truthy (``1``/``true``/``yes``/``on``) ⇒ ``True``; a recognised
    falsy (``0``/``false``/``no``/``off``) ⇒ ``False``. ANY other value raises
    :class:`ValueError` (fail-loud — an operator typo must never silently flip the policy).
    Case-insensitive, whitespace-stripped. O(1).

    Parameters
    ----------
    raw
        The value to interpret (``None`` reads the env var); a test may pass one directly.
    """
    value = os.environ.get(DHAN_RENEW_ENV) if raw is None else raw
    if value is None or not value.strip():
        return True
    token = value.strip().lower()
    if token in {"1", "true", "yes", "on"}:
        return True
    if token in {"0", "false", "no", "off"}:
        return False
    raise ValueError(
        f"invalid {DHAN_RENEW_ENV}={value!r}: expected one of "
        "1/0/true/false/yes/no/on/off (default on)"
    )


__all__ = [
    "DHAN_API_BASE",
    "DHAN_AUTH_HTTP_TIMEOUT_S",
    "DHAN_AUTH_MINT_URL",
    "DHAN_AUTH_RATE_LIMIT_UNVERIFIED",
    "DHAN_CLIENT_ID_HEADER",
    "DHAN_MINT_INVALIDATES_PRIOR_TOKEN_UNVERIFIED",
    "DHAN_MINT_LOCKDOWN_THRESHOLD",
    "DHAN_MINT_PRECONDITIONS_UNVERIFIED",
    "DHAN_RENEW_ENV",
    "DHAN_RENEW_PATH",
    "DHAN_RENEW_REQUIRES_ACTIVE_TOKEN_UNVERIFIED",
    "DHAN_TOKEN_HEADER",
    "DHAN_TOKEN_ISSUANCE_RETRY_ATTEMPTS",
    "DHAN_TOKEN_ISSUANCE_RETRY_BASE_S",
    "DHAN_TOKEN_ISSUANCE_RETRY_CAP_S",
    "DHAN_TOKEN_LIFETIME_S",
    "DHAN_TOKEN_REFRESH_AGE_S",
    "resolve_dhan_renew_enabled",
]
