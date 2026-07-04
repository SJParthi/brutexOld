"""Groww SDK refresher — thread-safe, single-flight access-token RE-MINT.

DEC-GROWW-TOKEN-REAUTH-001. Groww's daily access token resets at 06:00 IST. The
live client is built ONCE per pull (:func:`~brutex.adapters.groww.live.build_live_client`
/ :func:`~brutex.adapters.groww.live.build_live_pool` mint the token once and hand it
to ``sdk_factory(token)``), so the in-flight token is NEVER re-minted mid-pull — and
when the 06:00 reset invalidates it, EVERY subsequent call fails with a terminal
:class:`~brutex.adapters.groww.errors.GrowwAuthError` (which :func:`~brutex.adapters.\
groww.errors.is_retryable` returns ``False`` for → quarantined) and the overnight
pull dies needing a manual restart. ``errors.py`` already documented the intent (the
:class:`GrowwAuthError` docstring: "triggers re-auth, not blind retry") and
``ingest.py`` noted the latch "can be added if/when" — this is that sanctioned latch.

The refresher is the SINGLE mint path: it mints the INITIAL token + the *size*
independent SDK connections at construction (so there is ONE code path, not a
separate first-mint in ``live.py``) and re-mints on demand via :meth:`reauth`. A
SINGLE refresher backs both the single :class:`~brutex.adapters.groww.client.GrowwClient`
(``size=1``) and the K-leaf :class:`~brutex.adapters.groww.pool.ClientPool`
(``size=K``): each pool leaf binds to its own SDK index via :meth:`sdk_at`, so the
pool's K-way concurrency is preserved AND a single re-mint at 06:00 refreshes EVERY
leaf at once. It is:

* **THREAD-SAFE** — a :class:`threading.Lock` guards every read + the re-mint, so a
  multi-connection pool whose K worker threads all hit the expired token at 06:00 is
  correct.
* **SINGLE-FLIGHT** — a monotonically-rising :attr:`generation` counter lets the
  06:00 stampede mint EXACTLY ONCE: a caller passes the generation it last saw to
  :meth:`reauth`; if another thread already re-authed since (the stored generation
  moved past the caller's), the caller gets the CURRENT SDKs with NO second mint (no
  token stampede against the vendor).
* **BOUNDED** — the bound lives at the call site (:class:`~brutex.adapters.groww.\
  client.GrowwClient`'s ``max_reauths`` budget): a genuinely-bad credential re-mints
  once, the call fails again, and the client raises terminally — never an infinite
  loop.
* **FREEZE-GUARDED** — the re-mint network call is itself wrapped by the wall-clock
  :func:`~brutex.adapters.groww.deadline.run_with_deadline` freeze guard (MINOR-1 of
  DEC-GROWW-TOKEN-REAUTH-001): a re-mint that HANGS on a half-open socket at exactly
  06:00 IST would otherwise hold :attr:`_lock` forever and FREEZE every pool leaf
  blocked on :meth:`sdk_at` / :meth:`current_sdk`. The deadline wraps ONLY the network
  mint (it returns the new SDK tuple WITHOUT writing shared state — the lock-guarded
  publish runs only after it returns), so a timeout raises a RETRYABLE
  :class:`~brutex.adapters.groww.errors.GrowwTimeoutError` OUT of the locked block
  (the lock is released, no half-state published) and the caller's resilience
  quarantines + re-pulls that window instead of the pool freezing.

Every side-effecting step is an **injected callable** (``token_factory`` /
``totp_minter`` flow through to :func:`~brutex.adapters.groww.auth.get_access_token`,
and ``sdk_factory`` builds the real ``GrowwAPI``), exactly the callables ``live.py``
already uses — so the whole refresher is unit-tested with fakes, no ``growwapi`` /
``pyotp`` install and no network. A mint failure surfaces as a redacting
:class:`GrowwAuthError` straight from :func:`get_access_token` (the secret never
reaches a log).

DISK-CACHE (DEC-GROWW-TOKEN-DISK-CACHE-001) — mint ONCE per session per machine:

* **COLD-START (``__init__``) is CACHE-FIRST.** Before any network mint the initial
  build reads the on-disk daily token (:func:`~brutex.adapters.groww.token_cache.\
  load_cached_token`); a valid same-session token builds the SDKs with NO network call
  (so the 2nd/3rd pipeline phase, a restart, and a concurrent process all reuse the
  one token the first phase wrote). Only a cache MISS mints, then WRITES the cache. If
  that first mint is itself issuance-THROTTLED (no cache yet), a SMALL bounded back-off
  re-checks the cache + retries before re-raising a RETRYABLE
  :class:`~brutex.adapters.groww.errors.GrowwTokenIssuanceThrottled` (the pull quarantines
  + re-pulls — never a whole-pull crash). The back-off runs on THIS cold-start path,
  OUTSIDE any lock (hostile-review M2).
* **IN-PULL ``reauth()`` FORCES a real mint (B1).** It is called ONLY because the current
  token was REJECTED, so consulting the cache for the very token we just learned is dead
  would feed the dead token back into the bounded ``max_reauths=1`` loop and STALL the
  fetch. ``reauth()`` therefore bypasses the cache READ entirely, does a real network
  mint (still freeze-guarded), and OVERWRITES the cache with the fresh token — so the
  06:00-IST revoked-token case self-corrects exactly as before AND the new token is
  shared with the other phases/processes (hostile-review B1 fold).

All cache I/O is injected (``token_cache_load`` / ``token_cache_store`` / ``clock``,
defaulting to the real :mod:`~brutex.adapters.groww.token_cache` functions), and is
BEST-EFFORT — a cache miss/failure never blocks a mint, so the refresher is unchanged
when the cache is unavailable.

READ-ONLY BROKER RE-READ (DEC-BROKER-READONLY-REAUTH-REREAD-001) — a read-only host
NEVER mints, it RE-READS the shared token:

* On the shared-AWS read-only broker (``BRUTEX_TOKEN_BROKER_READONLY``) a dedicated Lambda
  is the SOLE minter; BruteX is a strictly read-only consumer. Without this seam an in-pull
  :meth:`reauth` would FORCE a real mint (B1) and hit the read-only token store's fail-loud
  ``store``, halting the pull and needing a manual RESTART. With the ``readonly_reread``
  hook wired (only in read-only mode — ``build_live_client`` passes it iff the resolved
  store is read-only), :meth:`reauth` RE-READS the shared Parameter-Store SecureString and
  ADOPTS the Lambda's freshly-minted token instead of minting locally — self-healing the
  06:00-IST rotation with NO restart. The re-read reuses the EXACT single-flight + lock +
  generation machinery (only ONE re-read per generation; concurrent callers wait + adopt
  the same result) and the SAME freeze-guard deadline (a hung SSM read raises a retryable
  timeout, never freezes the pool). A read that returns a NEW token is adopted immediately;
  a read that returns the SAME dead token (the Lambda has not minted yet — reauth fired
  just before the ~06:05 mint) or an SSM miss raises the RETRYABLE
  :class:`~brutex.adapters.groww.errors.GrowwTokenStillStaleError` (mirroring the
  reauth-deadline transient, DEC-GROWW-REAUTH-DEADLINE-001) so the window quarantines +
  re-pulls LATER — never an infinite loop, never a crash. The reauth cap
  (``max_reauths`` at the call site) still bounds it.

TOKEN-STALE FETCH-LANE PAUSE (DEC-TOKEN-STALE-PAUSE-001) — a dead token PAUSES the
fetch lane instead of burning window attempts:

* Forensically proven (operator's real overnight run, 2026-07-04): at the 06:00-IST
  Groww token reset the read-only broker held no fresh token until the external
  Lambda's 06:05:31 mint. During those 5.5 minutes EVERY fetch raised the retryable
  :class:`~brutex.adapters.groww.errors.GrowwTokenStillStaleError`, and the F&O
  window ladder burned all 3 attempts INSIDE the outage for 111 windows (339 failed
  calls across 115 windows) — settling them ledger-FAILED, rescued only by the outer
  until-clean rounds. This recurs on every 06:00-IST crossing (CLAUDE.md §6 token
  lifetime row).
* :meth:`SdkRefresher.await_token_recovery` is the sanctioned pause: when the
  client's reauth branch sees a StillStale re-read, it WAITS (bounded by the shared
  per-generation deadline :data:`~brutex.adapters.groww.constants.\
  GROWW_TOKEN_STALE_WAIT_S`, env-tunable) for the fresh token to land, polling every
  :data:`~brutex.adapters.groww.constants.GROWW_TOKEN_STALE_POLL_S` seconds. It is
  SINGLE-FLIGHT-AWARE: per tick, exactly ONE waiter (a non-blocking leader lock)
  re-reads the broker via the EXISTING :meth:`reauth` machinery; every other waiter
  only polls the in-process generation — N pool threads can never stampede SSM. It
  logs ONE bounded-cadence ``groww.token.stale_wait`` event per pool and BEATS the
  progress-beat seam (:func:`~brutex.observability.progress_beat.emit_progress_beat`)
  each tick so the pull stall watchdog never false-fires during the pause.
* The deadline is SHARED per generation (anchored at the FIRST stale observation),
  so a genuinely dead broker degrades to the pre-gate behaviour after ONE bounded
  wait: once the anchor expires, every subsequent gate entry for that generation
  fails IMMEDIATELY — the error propagates exactly as before (the window defers
  FAILED; the until-clean loop stays bounded; the dead-credential escalation of
  DEC-VENDOR-DOWN-BREAKER-001 still applies). A LOCAL-MINT refresher
  (``readonly_reread is None``) never raises StillStale — the mint IS the recovery —
  so the gate never double-waits there by construction.

READ-ONLY COLD START NEVER MINTS (DEC-REAUTH-COLDSTART-READONLY-001) — the read-only
guard now runs BEFORE any network mint (closes audit finding C-2, MAJOR / auth risk):

* Previously the read-only-ness was enforced ONLY at the store WRITE: a COLD START
  (:meth:`SdkRefresher.__init__` → ``_cold_start_sdks``) with the token store
  absent/unreachable (``load`` degrades ANY failure to a miss) fell straight into
  ``_mint_and_cache_sdks``, which fired the REAL Groww ``get_access_token`` network mint
  FIRST and only THEN hit the read-only store's fail-loud ``store`` — the dangerous mint
  had ALREADY happened. Under the centralized-token architecture (DEC-AWS-ONLY-CREDS-001:
  the ``groww-token-minter`` Lambda is the SOLE minter; BruteX + TickVault are read-only
  consumers of ONE shared SSM SecureString) a BruteX-side mint may invalidate the token
  TickVault is actively using — the vendor's single-active-token behaviour is UNVERIFIED,
  so a stray mint is treated as DANGEROUS (Golden Rule #1).
* NOW: when the refresher is in read-only mode (``readonly_reread`` wired —
  ``build_live_client`` passes it iff the resolved store reports ``readonly``, the
  production default under DEC-AWS-ONLY-CREDS-001), the cold-start loop BOUNDED-RETRIES
  THE READ (the SAME ``GROWW_TOKEN_ISSUANCE_RETRY_ATTEMPTS`` budget + back-off, sleeping
  outside any lock — the Lambda's ~06:05-IST mint may land any moment) and, when the
  budget exhausts with still no readable token, FAILS LOUD with a precise, actionable
  :class:`~brutex.adapters.groww.errors.GrowwAuthError` naming the RESOLVED SSM parameter
  + the operator fix — it can NEVER reach the mint call. The mint path remains ONLY for
  explicitly non-read-only configs (``readonly_reread`` is ``None`` — local dev / callers
  injecting their own hooks), byte-identical to before.
"""

from __future__ import annotations

import math
import os
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from functools import partial

import structlog

from brutex.adapters.groww.auth import (
    GrowwCredentials,
    _real_token_factory,
    _real_totp,
    get_access_token,
)
from brutex.adapters.groww.constants import (
    GROWW_PULL_CALL_TIMEOUT_S,
    GROWW_TOKEN_ISSUANCE_RETRY_ATTEMPTS,
    GROWW_TOKEN_ISSUANCE_RETRY_BASE_S,
    GROWW_TOKEN_ISSUANCE_RETRY_CAP_S,
    GROWW_TOKEN_PARAM_ACCESS_TOKEN_DEFAULT,
    GROWW_TOKEN_PARAM_ACCESS_TOKEN_ENV,
    GROWW_TOKEN_STALE_LOG_EVERY_S,
    GROWW_TOKEN_STALE_POLL_S,
    GROWW_TOKEN_STALE_WAIT_ENV,
    GROWW_TOKEN_STALE_WAIT_S,
    resolve_token_param_path,
)
from brutex.adapters.groww.deadline import run_with_deadline
from brutex.adapters.groww.errors import (
    GrowwAuthError,
    GrowwTokenIssuanceThrottled,
    GrowwTokenStillStaleError,
    from_groww_exception,
)
from brutex.adapters.groww.sdk_protocol import GrowwSDK
from brutex.adapters.groww.token_cache import (
    CachedToken,
    load_cached_token,
    store_token,
)
from brutex.observability.progress_beat import emit_progress_beat

_LOG = structlog.get_logger("brutex.adapters.groww.reauth")


def resolve_token_stale_wait_seconds(raw: str | None = None) -> float:
    """Resolve the token-stale fetch-lane pause bound: env override, else the default.

    DEC-TOKEN-STALE-PAUSE-001. Reads :data:`~brutex.adapters.groww.constants.\
    GROWW_TOKEN_STALE_WAIT_ENV` (``BRUTEX_GROWW_TOKEN_STALE_WAIT_S``); falls back to
    :data:`~brutex.adapters.groww.constants.GROWW_TOKEN_STALE_WAIT_S` when unset,
    blank, non-numeric, non-finite (``nan``/``inf`` would make the wait ill-defined /
    unbounded — an infinite pause would re-introduce the very hang the deadline
    prevents) or NEGATIVE. An explicit ``0`` is honoured — it DISABLES the pause
    (byte-identical to the pre-gate behaviour: the stale error propagates on the
    first gate entry), the deliberate operator revert lever. Mirrors the
    :func:`~brutex.adapters.groww.write_deadline.resolve_write_deadline_seconds`
    validation shape (CLAUDE.md §11 COMMON shape). O(1).

    Parameters
    ----------
    raw
        The wait value (``None`` reads the env var); a test may pass one directly.
    """
    value = os.environ.get(GROWW_TOKEN_STALE_WAIT_ENV) if raw is None else raw
    if value is None or not value.strip():
        return GROWW_TOKEN_STALE_WAIT_S
    try:
        parsed = float(value)
    except ValueError:
        return GROWW_TOKEN_STALE_WAIT_S
    if not math.isfinite(parsed) or parsed < 0.0:
        return GROWW_TOKEN_STALE_WAIT_S
    return parsed


def _now_utc() -> datetime:
    """The current UTC instant — the token cache's production clock (injectable in tests)."""
    return datetime.now(tz=UTC)


def _issuance_backoff(attempt: int) -> float:
    """The bounded cold-start back-off (seconds) before retry *attempt* + 1.

    ONE formula (CLAUDE.md §11 COMMON) shared by the throttled-mint retry and the
    read-only cold-start READ retry (DEC-REAUTH-COLDSTART-READONLY-001): exponential from
    :data:`GROWW_TOKEN_ISSUANCE_RETRY_BASE_S`, capped at
    :data:`GROWW_TOKEN_ISSUANCE_RETRY_CAP_S` so the total cold-start wait stays well under
    a pull deadline. O(1).
    """
    return min(GROWW_TOKEN_ISSUANCE_RETRY_BASE_S * (2.0**attempt), GROWW_TOKEN_ISSUANCE_RETRY_CAP_S)


def _readonly_cold_start_error(attempts: int) -> GrowwAuthError:
    """The LOUD, actionable cold-start error for a read-only host with no readable token.

    DEC-REAUTH-COLDSTART-READONLY-001 (audit C-2). Names the RESOLVED SSM parameter (the
    env override :data:`GROWW_TOKEN_PARAM_ACCESS_TOKEN_ENV` is honoured, so the message
    points at the parameter that was ACTUALLY read) + the operator fix. Raised INSTEAD of
    ever reaching the local mint — this host is a strictly read-only consumer of the shared
    daily token (the ``groww-token-minter`` Lambda is the SOLE minter,
    DEC-AWS-ONLY-CREDS-001); a BruteX-side mint could invalidate the token TickVault is
    actively using (vendor single-active-token behaviour UNVERIFIED — treated as dangerous).
    Contains NO secret values. O(1).
    """
    param = resolve_token_param_path(
        GROWW_TOKEN_PARAM_ACCESS_TOKEN_ENV, GROWW_TOKEN_PARAM_ACCESS_TOKEN_DEFAULT
    )
    return GrowwAuthError(
        f"cold start on a READ-ONLY token host: no readable shared Groww access token after "
        f"{attempts} read attempts — AWS Parameter Store parameter {param!r} is absent or "
        f"unreachable, and this host NEVER mints (the groww-token-minter Lambda is the sole "
        f"minter; a local mint could invalidate the shared token another consumer is using). "
        f"Fix: verify the minter is deployed + ran (deploy/aws_token_minter/deploy.sh; "
        f"EventBridge mints daily ~06:05 IST), verify this host's IAM reader role grants "
        f"ssm:GetParameter + kms:Decrypt on {param!r} in the token broker region, then re-run "
        f"the pull once the parameter is present."
    )


def _real_sdk_factory(token: str) -> GrowwSDK:  # pragma: no cover — needs growwapi + network
    """Default SDK factory: ``GrowwAPI(token)`` with a connect+read socket timeout.

    The single COMMON factory seam BOTH live build sites default to
    (:func:`~brutex.adapters.groww.live.build_live_client` /
    :func:`~brutex.adapters.groww.live.build_live_pool`), so imposing the socket timeout
    HERE means every live pull inherits it with no per-site change
    (DEC-GROWW-HTTP-SOCKET-TIMEOUT-001). :func:`~brutex.adapters.groww.http_timeout.\
    apply_default_timeout` patches the SDK's ``requests``-issuing ``_request_*`` helpers so
    a half-open / dead socket fails in seconds (the env-tunable connect 10 s / read 30 s)
    instead of burning the 45 s wall-clock guard, which stays as belt-and-suspenders.
    """
    # No inline import ignore: the growwapi dual-env hygiene lives in the
    # ``[[tool.mypy.overrides]]`` per-module table (DEC-MYPY-GROWWAPI-STUBS-001).
    from growwapi import GrowwAPI

    from brutex.adapters.groww.http_timeout import apply_default_timeout

    return apply_default_timeout(GrowwAPI(token))  # type: ignore[return-value]


class SdkRefresher:
    """Thread-safe single-flight minter of *size* fresh Groww :class:`GrowwSDK` connections.

    Constructed with the SAME injected callables :func:`~brutex.adapters.groww.live.\
    build_live_client` uses; mints the INITIAL token + the *size* independent SDK
    connections eagerly (ONE mint path).

    Parameters
    ----------
    creds
        The Groww TOTP api key + shared secret (held only to mint codes).
    size
        The number of independent SDK connections to mint (clamped to ``>= 1``).
        ``1`` for the single client; ``K`` for a K-leaf pool (each leaf binds an index).
    token_factory
        Access-token issuer (default: the real ``GrowwAPI.get_access_token``).
    totp_minter
        TOTP code minter (default: the real ``pyotp.TOTP(...).now()``).
    sdk_factory
        Builds one SDK instance from the issued token (default: ``GrowwAPI(token)``);
        called *size* times per mint to fan independent connections off one token.
    token_cache_load
        Reads the on-disk daily token (default: :func:`~brutex.adapters.groww.\
        token_cache.load_cached_token`). Consulted ONLY on the cold-start ``__init__``
        path (cache-first); ``reauth`` NEVER reads it (B1). Returns ``None`` on a miss.
    token_cache_store
        Persists a freshly-minted token (default: :func:`~brutex.adapters.groww.\
        token_cache.store_token`). Best-effort — a failure never blocks the build.
    clock
        UTC-instant source for the cache's session anchoring (default: :func:`_now_utc`);
        injected in tests for deterministic session-boundary cases.
    readonly_reread
        OPTIONAL read-only-broker RE-READ hook (DEC-BROKER-READONLY-REAUTH-REREAD-001).
        ``None`` (the default) keeps the byte-identical LOCAL-mint reauth behaviour. When
        wired (``build_live_client`` passes the read-only Parameter-Store store's ``load``
        iff the resolved backend is read-only), :meth:`reauth` RE-READS the shared token via
        this hook and ADOPTS the Lambda's fresh token INSTEAD of minting locally, so the
        read-only host self-heals the 06:00-IST rotation with no restart. Same signature as
        ``token_cache_load`` (best-effort — it NEVER raises, returning ``None`` on a miss);
        a re-read that returns a NEW token is adopted, a SAME / ``None`` result raises the
        retryable :class:`~brutex.adapters.groww.errors.GrowwTokenStillStaleError`. Wiring
        this ALSO marks the refresher READ-ONLY for the COLD START
        (DEC-REAUTH-COLDSTART-READONLY-001): a cold start with no readable token
        bounded-retries the READ then fails LOUD — it NEVER reaches the local mint.

    Raises
    ------
    GrowwAuthError
        If the INITIAL token issuance fails (redacting ``safe_message`` — the secret
        never leaks), straight from :func:`~brutex.adapters.groww.auth.get_access_token`;
        OR — with *readonly_reread* wired (a READ-ONLY host) — if the cold start finds no
        readable shared token after the bounded READ retries: the loud, actionable
        :func:`_readonly_cold_start_error` naming the resolved SSM parameter + the fix,
        raised BEFORE any mint could fire (DEC-REAUTH-COLDSTART-READONLY-001, audit C-2).
    GrowwTokenIssuanceThrottled
        If the cold-start mint is issuance-THROTTLED with no cache to fall back to,
        after the bounded back-off budget is exhausted (a RETRYABLE subclass — the pull
        quarantines + re-pulls; DEC-GROWW-TOKEN-DISK-CACHE-001). Non-read-only mode only
        (a read-only cold start never mints, so it can never be issuance-throttled).
    """

    __slots__ = (
        "_call_timeout_seconds",
        "_clock",
        "_creds",
        "_current_token",
        "_generation",
        "_lock",
        "_readonly_reread",
        "_sdk_factory",
        "_sdks",
        "_size",
        "_stale_anchor_at",
        "_stale_anchor_gen",
        "_stale_last_log",
        "_stale_leader_lock",
        "_stale_note_lock",
        "_token_cache_load",
        "_token_cache_store",
        "_token_factory",
        "_totp_minter",
    )

    def __init__(
        self,
        creds: GrowwCredentials,
        *,
        size: int = 1,
        token_factory: Callable[[str, str], str] = _real_token_factory,
        totp_minter: Callable[[str], str] = _real_totp,
        sdk_factory: Callable[[str], GrowwSDK] = _real_sdk_factory,
        call_timeout_seconds: float = GROWW_PULL_CALL_TIMEOUT_S,
        token_cache_load: Callable[..., CachedToken | None] = load_cached_token,
        token_cache_store: Callable[..., bool] = store_token,
        clock: Callable[[], datetime] = _now_utc,
        readonly_reread: Callable[..., CachedToken | None] | None = None,
    ) -> None:
        self._creds = creds
        self._size = max(1, size)
        self._token_factory = token_factory
        self._totp_minter = totp_minter
        self._sdk_factory = sdk_factory
        #: Hard wall-clock deadline (seconds) for the network RE-MINT — the FREEZE
        #: GUARD (DEC-GROWW-PULL-CALL-DEADLINE-001 / MINOR-1 of DEC-GROWW-TOKEN-REAUTH-001).
        #: A hung re-mint at 06:00 IST must NOT block the lock forever (which would freeze
        #: every other pool leaf); it is bounded by :func:`~brutex.adapters.groww.deadline.\
        #: run_with_deadline` so a stall raises a retryable timeout instead. Injected SHORT
        #: in tests; the live build sites take the default ``GROWW_PULL_CALL_TIMEOUT_S``.
        self._call_timeout_seconds = call_timeout_seconds
        #: Best-effort on-disk daily-token cache hooks (DEC-GROWW-TOKEN-DISK-CACHE-001).
        #: Read ONLY on the cold-start path; written after every successful mint.
        self._token_cache_load = token_cache_load
        self._token_cache_store = token_cache_store
        self._clock = clock
        #: OPTIONAL read-only-broker RE-READ hook (DEC-BROKER-READONLY-REAUTH-REREAD-001).
        #: ``None`` ⇒ the byte-identical LOCAL-mint reauth; a callable ⇒ :meth:`reauth`
        #: RE-READS the shared token + adopts the Lambda's fresh one instead of minting.
        self._readonly_reread = readonly_reread
        self._lock = threading.Lock()
        #: TOKEN-STALE fetch-lane pause state (DEC-TOKEN-STALE-PAUSE-001), shared by
        #: every pool leaf riding this ONE refresher. The LEADER lock elects at most
        #: one broker re-reader per poll tick (acquired NON-blocking — a follower
        #: never blocks on it); the NOTE lock guards the per-generation deadline
        #: anchor + the bounded log cadence (held only for O(1) field reads/writes,
        #: never across a network call — no lock-order edge with :attr:`_lock`).
        self._stale_leader_lock = threading.Lock()
        self._stale_note_lock = threading.Lock()
        #: Generation the shared stale-wait deadline anchor belongs to (-1 = none).
        self._stale_anchor_gen = -1
        #: Monotonic instant the FIRST stale observation for that generation landed.
        self._stale_anchor_at = 0.0
        #: Monotonic instant of the last ``groww.token.stale_wait`` log (cadence).
        self._stale_last_log = float("-inf")
        #: Monotonically rising re-auth counter — starts at 0 for the INITIAL mint;
        #: each successful :meth:`reauth` bumps it, so a caller can prove single-flight.
        self._generation = 0
        #: The token the live :attr:`_sdks` were built from — tracked so a read-only
        #: RE-READ (:meth:`reauth`) can tell a Lambda-rotated NEW token (adopt) from the
        #: SAME dead token still in SSM (retryable). ``""`` until the cold-start builds.
        #: The empty-string init is an EMPTY SENTINEL, not a credential — bandit's B105
        #: name-heuristic false-positives on the ``token`` name (ruff's S105 does not fire
        #: on an EMPTY literal, so only the bandit ``# nosec B105`` is needed here).
        self._current_token = ""  # nosec B105
        #: The INITIAL build — CACHE-FIRST (reuse a same-session token if present, else
        #: mint + write the cache). This is the ONLY cache-READING path; the in-pull
        #: :meth:`reauth` FORCES a real mint locally, or RE-READS on a read-only host.
        self._sdks = self._cold_start_sdks()

    def _safe_cache_load(self) -> CachedToken | None:
        """Read the on-disk token cache, degrading ANY failure to a cache-MISS (``None``).

        :func:`~brutex.adapters.groww.token_cache.load_cached_token` is contractually
        best-effort ("returns None on ANY failure, NEVER raises"), but the cache-load hook
        is INJECTED and a future load path could regress (or a test double could raise).
        This belt-and-suspenders guard restores the "a cache problem must never break the
        build" invariant unconditionally: ANY unexpected exception out of the load hook is
        logged + degraded to ``None``, so the cold-start path falls through to a real mint
        instead of crashing :meth:`__init__` (and the whole pull) at startup. O(1).
        """
        try:
            return self._token_cache_load(self._creds, now_utc=self._clock())
        except Exception as exc:  # pylint: disable=broad-exception-caught — degrade ANY cache-load failure to a mint
            _LOG.warning("groww.token.cache_load_failed", error_type=type(exc).__name__)
            return None

    def _cold_start_sdks(self) -> tuple[GrowwSDK, ...]:
        """Build the INITIAL SDKs CACHE-FIRST, minting at most once with bounded back-off.

        The cold-start path (DEC-GROWW-TOKEN-DISK-CACHE-001):

        1. Read the on-disk daily token. A VALID same-session token builds the SDKs with
           NO network mint (the cache hit that collapses the 3-phase storm to one mint).
        2. On a MISS in READ-ONLY mode (:attr:`_readonly_reread` wired — the resolved store
           NEVER mints, DEC-REAUTH-COLDSTART-READONLY-001 / audit C-2): bounded-RETRY the
           READ (the Lambda's ~06:05-IST mint may land any moment) and, when the budget is
           exhausted with still no readable token, FAIL LOUD with the actionable
           :func:`_readonly_cold_start_error` — the mint call below is UNREACHABLE in
           read-only mode by construction. Previously this path fired the REAL network mint
           FIRST and only the subsequent store WRITE failed loud — the dangerous mint (which
           may invalidate the shared token another consumer is actively using; vendor
           single-active-token behaviour UNVERIFIED) had already happened.
        3. On a MISS in non-read-only mode, mint a fresh token. On success, WRITE the cache
           (best-effort) so the other phases/processes reuse it, then build the SDKs.
        4. If the mint is issuance-THROTTLED (no cache), re-check the cache (a racing
           phase/process may have just written one) and, while the bounded attempt budget
           remains, sleep a small back-off + retry. When the budget is exhausted with no
           cache, re-raise the RETRYABLE :class:`~brutex.adapters.groww.errors.\
           GrowwTokenIssuanceThrottled` (the pull quarantines + re-pulls — never a crash).

        The back-off sleeps run HERE, on the cold-start path, OUTSIDE any lock (hostile-
        review M2) — ``reauth``'s locked block never sleeps on a throttle. Cold-start runs
        on the CONSTRUCTING thread with NO freeze-guard deadline, so it records
        :attr:`_current_token` directly (safe — no abandoned-daemon hazard here; MAJOR-1).
        """
        attempts = max(1, GROWW_TOKEN_ISSUANCE_RETRY_ATTEMPTS)
        for attempt in range(attempts):
            cached = self._safe_cache_load()
            if cached is not None:
                _LOG.info("groww.token.cache_hit", session=cached.issued_for_session.isoformat())
                self._current_token = cached.access_token
                return self._build_sdks(cached.access_token)
            if self._readonly_reread is not None:
                # READ-ONLY COLD START (DEC-REAUTH-COLDSTART-READONLY-001, audit C-2): this
                # host NEVER mints — the guard runs BEFORE the mint call, not after it at the
                # store write. Bounded-retry the READ, then fail LOUD + actionable.
                if attempt == attempts - 1:
                    raise _readonly_cold_start_error(attempts)
                backoff = _issuance_backoff(attempt)
                _LOG.warning(
                    "groww.token.readonly_cold_start_retry",
                    attempt=attempt + 1,
                    backoff_s=backoff,
                )
                time.sleep(backoff)
                continue
            try:
                sdks, token = self._mint_and_cache_sdks()
                self._current_token = token
                return sdks
            except GrowwTokenIssuanceThrottled:
                # On the LAST attempt re-raise the RETRYABLE throttle (the pull quarantines
                # + re-pulls — never a crash). Otherwise sleep a small back-off OUTSIDE the
                # lock (hostile-review M2) + retry, re-checking the cache first (a racing
                # phase/process may have just written one).
                if attempt == attempts - 1:
                    raise
                backoff = _issuance_backoff(attempt)
                _LOG.warning(
                    "groww.token.issuance_throttled_backoff",
                    attempt=attempt + 1,
                    backoff_s=backoff,
                )
                time.sleep(backoff)
        # Unreachable: the final iteration either returns or re-raises above (the loop runs
        # >= 1 time). A bare raise here would have no active exception, so assert the invariant
        # for the type-checker without an unreachable runtime branch.
        raise AssertionError(
            "unreachable: cold-start loop always returns or raises"
        )  # pragma: no cover

    def _mint_and_cache_sdks(self) -> tuple[tuple[GrowwSDK, ...], str]:
        """Mint a fresh token (real network call), WRITE the cache, then build the SDKs.

        The cache write is best-effort (a failure logs + is swallowed inside
        :func:`~brutex.adapters.groww.token_cache.store_token`), so it never blocks the
        build. Used by BOTH the cold-start miss path and (after the forced mint) by
        :meth:`reauth`, so the fresh token is always persisted for reuse.

        Returns ``(sdks, token)`` so the CALLER records :attr:`_current_token` — under the
        reauth lock, on the caller thread, only after a clean :func:`run_with_deadline`
        return (MAJOR-1: an abandoned freeze-guard daemon must never write shared state).
        """
        token_info = get_access_token(
            self._creds,
            token_factory=self._token_factory,
            totp_minter=self._totp_minter,
        )
        token = token_info.access_token
        self._token_cache_store(self._creds, token, now_utc=self._clock())
        return (self._build_sdks(token), token)

    def _build_sdks(self, token: str) -> tuple[GrowwSDK, ...]:
        """Fan *size* SDK connections off *token* (a PURE builder — no shared-state write).

        Deliberately does NOT write :attr:`_current_token`: this runs on the freeze-guard
        DAEMON thread inside :func:`run_with_deadline` (via :meth:`_mint_and_cache_sdks` /
        :meth:`_reread_and_adopt_sdks`), and an ABANDONED daemon of a HUNG re-auth must
        never write shared state after the caller already raised + released the lock
        (MAJOR-1 of DEC-BROKER-READONLY-REAUTH-REREAD-001). The caller-thread publish block
        in :meth:`reauth` records :attr:`_current_token` ONLY after a clean
        :func:`run_with_deadline` return; the cold-start (:meth:`_cold_start_sdks`, on the
        constructing thread, no deadline) sets it directly. O(size).
        """
        return tuple(self._sdk_factory(token) for _ in range(self._size))

    @property
    def size(self) -> int:
        """The number of independent SDK connections this refresher fans (``>= 1``)."""
        return self._size

    def current_sdk(self) -> GrowwSDK:
        """Return the first live SDK (lock-guarded read, O(1)).

        The single-client convenience accessor; a pool leaf uses :meth:`sdk_at`.
        """
        with self._lock:
            return self._sdks[0]

    def sdk_at(self, index: int) -> GrowwSDK:
        """Return the live SDK bound to leaf *index*, modulo :attr:`size` (lock-guarded, O(1)).

        Each pool leaf calls this with its own fixed index, so a single re-mint
        refreshes EVERY leaf at once while preserving K independent connections. The
        modulo keeps an over-large index in range (defensive; the pool only ever passes
        ``0..size-1``).
        """
        with self._lock:
            return self._sdks[index % self._size]

    def current_generation(self) -> int:
        """Return the current re-auth generation (lock-guarded read, O(1))."""
        with self._lock:
            return self._generation

    def reauth(self, seen_generation: int) -> int:
        """Re-auth the token + SDK connections, SINGLE-FLIGHT against *seen_generation*.

        Under the lock: if :attr:`generation` already moved past *seen_generation*
        (another thread re-authed since the caller's last read — the 06:00 stampede),
        return the CURRENT generation WITHOUT a second re-auth. Otherwise refresh all
        *size* SDK connections, bump the generation, and return the new generation.
        (Callers re-resolve their SDK via :meth:`sdk_at` / :meth:`current_sdk` on the
        retry — so a returned generation alone suffices.) The refresh is one of two paths,
        chosen ONCE at construction by :attr:`_readonly_reread`:

        LOCAL MINT (default — :attr:`_readonly_reread` is ``None``) — FORCED, NEVER
        cache-first (hostile-review B1): ``reauth`` is called ONLY because the current token
        was REJECTED, so it does a REAL mint (it would loop on a dead token if it re-read the
        same-session cache) and OVERWRITES the cache with the fresh token via
        :meth:`_mint_and_cache_sdks` — so the 06:00-IST revoked-token case self-corrects AND
        the new token is shared with the other phases/processes. The cache READ lives ONLY on
        the cold-start path (:meth:`_cold_start_sdks`).

        READ-ONLY RE-READ (:attr:`_readonly_reread` wired — the shared-AWS broker,
        DEC-BROKER-READONLY-REAUTH-REREAD-001) — BruteX NEVER mints: it RE-READS the shared
        Parameter-Store token (:meth:`_reread_and_adopt_sdks`) and adopts the Lambda's fresh
        token. A NEW token rebuilds the SDKs + bumps the generation exactly like a mint; the
        SAME dead token still in SSM (or an SSM miss) raises the RETRYABLE
        :class:`~brutex.adapters.groww.errors.GrowwTokenStillStaleError` so the window
        quarantines + re-pulls LATER (when the Lambda's fresh token lands) — never an
        infinite loop, never a fail-loud restart.

        Parameters
        ----------
        seen_generation
            The generation the caller last observed (via :meth:`current_generation`).
            If the stored generation differs, another thread already refreshed and this
            call is a no-op handoff (single-flight).

        Returns
        -------
        int
            The live generation after this call — bumped if THIS caller won the
            single-flight race, else the already-refreshed current generation.

        Raises
        ------
        GrowwAuthError
            If a LOCAL re-mint itself fails (redacting ``safe_message``) — propagated so
            the client's bounded budget eventually raises terminally for a genuinely
            bad credential (never an infinite loop).
        GrowwTokenStillStaleError
            If a READ-ONLY re-read returns the SAME dead token or an SSM miss (the Lambda
            has not minted the fresh token yet) — a RETRYABLE :class:`GrowwAuthError`
            subclass so the window quarantines + re-pulls later, never a restart.
        GrowwTimeoutError
            If the re-auth network call (the mint OR the SSM re-read) HANGS past
            :attr:`_call_timeout_seconds` (a half-open socket at the 06:00 IST reset) —
            the FREEZE GUARD (MINOR-1 of DEC-GROWW-TOKEN-REAUTH-001). The bounded call
            raises a :class:`TimeoutError` mapped to a RETRYABLE
            :class:`~brutex.adapters.groww.errors.GrowwTimeoutError`, so the caller's
            resilience quarantines + re-pulls THAT window instead of freezing the whole
            pool. CRUCIALLY the deadline wraps ONLY the network call, which RETURNS the new
            ``(sdks, token)`` WITHOUT writing ANY shared state (``_build_sdks`` is a pure
            builder — MAJOR-1); the lock-guarded PUBLISH of ``_sdks`` / ``_generation`` /
            ``_current_token`` happens only AFTER a clean return, so a timeout propagates
            out of the ``with self._lock`` block (the lock is RELEASED — never a permanent
            hold) leaving NO half-written state, and the abandoned daemon's eventual result
            (the unpublished ``(sdks, token)``) is simply discarded — it can NEVER reach
            ``_current_token``, so a later re-read's NEW-vs-SAME decision is never poisoned.
        """
        with self._lock:
            if self._generation != seen_generation:
                # Another thread already re-authed — single-flight: no second re-auth.
                return self._generation
            # PIN the rejected reference token UNDER THE LOCK (MAJOR-1): the read-only
            # re-read compares the freshly-read SSM token against THIS captured value, not
            # `self._current_token` read live on the daemon thread (which a concurrent
            # publish or an abandoned earlier daemon could race/poison).
            pinned_token = self._current_token
            # FREEZE GUARD (MINOR-1): bound ONLY the FORCED network call (mint or read-only
            # SSM re-read) by the wall-clock deadline. Both `_mint_and_cache_sdks` and
            # `_reread_and_adopt_sdks` return `(sdks, token)` WITHOUT writing the refresher's
            # shared `_sdks`/`_generation`/`_current_token` (`_build_sdks` is a pure builder),
            # so on a timeout `run_with_deadline` raises, the exception leaves the
            # `with self._lock` block (lock released — no permanent hold), and NOTHING was
            # half-published. The in-memory publish runs only AFTER a clean return. A
            # GrowwTokenStillStaleError from the read-only re-read likewise propagates out of
            # the released lock (retryable). Bind the read-only re-read hook + pinned token via
            # `partial` so `_reread_and_adopt_sdks` takes them as non-optional args (no in-method
            # `assert`/dead None-branch); the None case selects the local mint. Both are zero-arg
            # for `run_with_deadline`.
            refresh: Callable[[], tuple[tuple[GrowwSDK, ...], str]] = (
                self._mint_and_cache_sdks
                if self._readonly_reread is None
                else partial(self._reread_and_adopt_sdks, self._readonly_reread, pinned_token)
            )
            try:
                new_sdks, new_token = run_with_deadline(refresh, self._call_timeout_seconds)
            except TimeoutError as exc:
                # Map the deadline TimeoutError to a RETRYABLE GrowwTimeoutError (the
                # "deadline exceeded" hint is recognised by from_groww_exception) so the
                # caller treats a hung re-auth as transient, NOT a terminal auth failure.
                raise from_groww_exception(exc) from exc
            # Caller-thread publish (MAJOR-1): record the fresh token HERE, beside `_sdks` /
            # `_generation`, ONLY after a clean deadline return — never on the daemon thread.
            self._sdks = new_sdks
            self._current_token = new_token
            self._generation += 1
            return self._generation

    def _reread_and_adopt_sdks(
        self, reread: Callable[..., CachedToken | None], pinned_token: str
    ) -> tuple[tuple[GrowwSDK, ...], str]:
        """RE-READ the shared read-only token + adopt a Lambda-rotated one (never mint).

        The read-only-broker reauth path (DEC-BROKER-READONLY-REAUTH-REREAD-001): BruteX is
        a strictly read-only consumer of the shared daily token (a dedicated Lambda mints it
        into AWS SSM Parameter Store), so on a mid-run auth rejection it RE-READS the shared
        SecureString via *reread* (the read-only store's ``load`` bound by :meth:`reauth`,
        contractually best-effort — it degrades any boto3/IAM/network error to ``None`` and
        NEVER raises) and ADOPTS the Lambda's freshly-minted token instead of minting locally.

        The rejected token to compare against is the *pinned_token* CAPTURED UNDER THE REAUTH
        LOCK at :meth:`reauth` entry — NOT :attr:`_current_token` read live here (MAJOR-1 of
        DEC-BROKER-READONLY-REAUTH-REREAD-001): this method runs on the freeze-guard DAEMON
        thread, so reading the live field could race a concurrent publish, and an abandoned
        daemon of an earlier HUNG re-read could have left the field stale — pinning the
        reference token at lock-entry makes the NEW-vs-SAME decision race-free.

        * A re-read that returns a NEW token (``!= pinned_token``) rebuilds the SDKs off it
          and logs ``groww.token.reread`` — the self-heal, visible in ``/logs``.
        * A re-read that returns the SAME token that just failed auth, or ``None`` (an SSM
          miss — the Lambda has not minted the fresh token yet, e.g. reauth fired at 05:59
          IST just before the ~06:05 mint), raises the RETRYABLE
          :class:`~brutex.adapters.groww.errors.GrowwTokenStillStaleError` so the caller
          quarantines + re-pulls that window LATER — never looping the dead token, never a
          fail-loud restart.

        Returns ``(sdks, token)`` so the CALLER records :attr:`_current_token` under the
        lock after a clean :func:`run_with_deadline` return. Runs UNDER the reauth lock +
        wrapped by the freeze-guard deadline (a hung SSM read raises a retryable timeout,
        mirroring the mint path). It NEVER calls the local token_factory/store, so the
        read-only host truly never mints. O(size).
        """
        cached = reread(self._creds, now_utc=self._clock())
        fresh_token = cached.access_token if cached is not None else None
        if fresh_token is None or fresh_token == pinned_token:
            # The Lambda has not rotated the shared token yet — do NOT adopt the dead token
            # (would loop it) and do NOT mint (read-only). Raise a retryable stall so the
            # window is re-pulled later, when the fresh token is present in SSM.
            _LOG.warning("groww.token.reread_still_stale", present=fresh_token is not None)
            raise GrowwTokenStillStaleError(
                "read-only broker re-read returned no fresh token yet: the shared Groww "
                "access token has not been re-minted by the Lambda; re-pull later.",
                safe_message=(
                    "read-only broker re-read returned no fresh token yet: the shared Groww "
                    "access token has not been re-minted by the Lambda; re-pull later."
                ),
            )
        _LOG.warning("groww.token.reread")
        return (self._build_sdks(fresh_token), fresh_token)

    def await_token_recovery(
        self,
        seen_generation: int,
        *,
        wait_seconds: float | None = None,
        poll_seconds: float = GROWW_TOKEN_STALE_POLL_S,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        method: str = "",
    ) -> int:
        """PAUSE the calling fetch worker (bounded) until a fresh token is adopted.

        THE TOKEN-STALE GATE (DEC-TOKEN-STALE-PAUSE-001). Called by
        :meth:`~brutex.adapters.groww.client.GrowwClient._call` when a read-only-broker
        :meth:`reauth` raised :class:`~brutex.adapters.groww.errors.\
        GrowwTokenStillStaleError` — the 06:00-IST reset window in which the external
        Lambda has not minted the fresh shared token yet. Instead of propagating (which
        burned the window ladder's whole ``max_attempts`` budget inside the outage —
        the forensically-measured 111 ledger-FAILED windows / 339 wasted calls of the
        2026-07-04 run), the worker WAITS here until ONE of:

        * **Adoption** — the generation moves past *seen_generation* (this waiter's own
          leader re-read adopted the Lambda's fresh token, or a sibling's did): the new
          generation is returned and the caller retries against the refreshed SDK with
          NO window attempt spent.
        * **Deadline** — the SHARED per-generation deadline (anchored at the FIRST
          stale observation for *seen_generation*, so N waiters + repeated ladder
          re-entries all share ONE bound — a dead broker costs ONE wait, then every
          subsequent entry fails fast) expires: a fresh
          :class:`~brutex.adapters.groww.errors.GrowwTokenStillStaleError` is raised
          and propagates exactly as before the gate existed (the window defers FAILED;
          the until-clean loop + the DEC-VENDOR-DOWN-BREAKER-001 dead-credential
          escalation stay intact — a never-minting broker still fails loudly).

        SINGLE-FLIGHT-AWARE (no SSM stampede): per poll tick at most ONE waiter — the
        non-blocking :attr:`_stale_leader_lock` holder — re-reads the broker, via the
        EXISTING :meth:`reauth` single-flight machinery (so the re-read also publishes
        + bumps the generation for everyone); every other waiter only sleeps + polls
        the in-process generation. Each tick BEATS the progress-beat seam
        (:func:`~brutex.observability.progress_beat.emit_progress_beat`) so the pull
        stall watchdog never false-fires during the pause, and ONE bounded-cadence
        ``groww.token.stale_wait`` structlog event per pool keeps the operator
        informed (elapsed + deadline; cadence
        :data:`~brutex.adapters.groww.constants.GROWW_TOKEN_STALE_LOG_EVERY_S`).

        LOCAL-MINT MODE IS UNAFFECTED by construction: a refresher with
        ``readonly_reread is None`` re-mints synchronously inside :meth:`reauth` and
        NEVER raises the StillStale shape, so the client's gate (which engages ONLY on
        that exception class) can never double-wait there. A TERMINAL auth failure (a
        revoked key) is likewise untouched — it is not a StillStale and never enters
        this method.

        Parameters
        ----------
        seen_generation
            The generation whose token was just observed stale (the value the caller
            passed to the failing :meth:`reauth`).
        wait_seconds
            The pause bound; ``None`` (production) resolves
            :func:`resolve_token_stale_wait_seconds` (env-tunable, default
            :data:`~brutex.adapters.groww.constants.GROWW_TOKEN_STALE_WAIT_S` = 420 s
            — the observed 331 s worst-case Lambda lag with margin). ``0`` disables
            the pause (raises on the first deadline check — pre-gate behaviour).
        poll_seconds
            Tick cadence between generation polls / leader re-reads.
        sleep, clock
            Injected sleeper + MONOTONIC clock (the client passes its own, so tests
            drive the wait with zero real time).
        method
            The originating call label (observability only).

        Returns
        -------
        int
            The live generation after a fresh token was adopted (> *seen_generation*).

        Raises
        ------
        GrowwTokenStillStaleError
            When the shared deadline expires with no fresh token adopted — the honest
            dead-broker propagation, byte-compatible with the pre-gate error path.
        GrowwTimeoutError
            If a leader re-read itself HANGS past the freeze-guard deadline (a wedged
            SSM read is a DIFFERENT failure from "no fresh token yet" — it propagates
            retryable, exactly like a hung :meth:`reauth` outside the gate).
        """
        bound = resolve_token_stale_wait_seconds() if wait_seconds is None else wait_seconds
        anchor = self._stale_wait_anchor(seen_generation, clock())
        while True:
            current = self.current_generation()
            if current != seen_generation:
                return current  # a sibling waiter adopted the fresh token — ride it
            now = clock()
            elapsed = now - anchor
            if elapsed >= bound:
                message = (
                    f"token-stale pause exhausted after {elapsed:.0f}s (bound {bound:g}s): "
                    "the read-only broker still holds no fresh Groww access token — "
                    "propagating so the window defers FAILED and the until-clean / "
                    "dead-credential escalation paths apply as before the pause."
                )
                raise GrowwTokenStillStaleError(message, safe_message=message)
            self._stale_wait_note(now=now, elapsed=elapsed, bound=bound, method=method)
            # Liveness: the pause is deliberate progress, not a hang — beat the
            # watchdog seam every tick so a paused pull is never falsely killed.
            emit_progress_beat()
            sleep(max(0.0, min(poll_seconds, bound - elapsed)))
            adopted = self._try_leader_reread(seen_generation)
            if adopted is not None:
                return adopted

    def _try_leader_reread(self, seen_generation: int) -> int | None:
        """ONE single-flight-aware broker re-read attempt for this poll tick.

        Elects the leader with a NON-blocking acquire of :attr:`_stale_leader_lock`:
        a follower returns ``None`` immediately (it keeps polling the generation —
        never a second SSM read this tick, so N pool threads issue at most ONE broker
        read per tick between them). The leader delegates to :meth:`reauth` — reusing
        the existing lock + generation single-flight + freeze-guard verbatim (CLAUDE.md
        §11 COMMON) — and maps its ``StillStale`` outcome to ``None`` (keep waiting;
        the CALLER's shared deadline bounds the loop). A successful adoption returns
        the bumped generation; any OTHER error (a hung-read timeout, a genuinely
        terminal auth failure from a future refresher shape) propagates unchanged.
        O(1) per tick.
        """
        if not self._stale_leader_lock.acquire(blocking=False):
            return None  # a sibling waiter is this tick's re-reader — no stampede
        try:
            try:
                return self.reauth(seen_generation)
            except GrowwTokenStillStaleError:
                return None  # still stale — keep waiting under the caller's deadline
        finally:
            self._stale_leader_lock.release()

    def _stale_wait_anchor(self, seen_generation: int, now: float) -> float:
        """Return the SHARED deadline anchor for *seen_generation*, creating it once.

        The FIRST gate entry for a generation stamps the anchor (and resets the log
        cadence so the new outage logs immediately); every later entry — a sibling
        pool thread, or the SAME window ladder re-entering after a propagated expiry —
        reuses it, so the whole pool spends AT MOST ONE ``wait_seconds`` pause per
        stale generation (a dead broker degrades to fail-fast, never a per-attempt
        re-wait). A generation CHANGE (a successful adoption, then a later 06:00
        crossing) re-arms the anchor naturally. Lock-guarded O(1).
        """
        with self._stale_note_lock:
            if self._stale_anchor_gen != seen_generation:
                self._stale_anchor_gen = seen_generation
                self._stale_anchor_at = now
                self._stale_last_log = float("-inf")
            return self._stale_anchor_at

    def _stale_wait_note(self, *, now: float, elapsed: float, bound: float, method: str) -> None:
        """Log ONE bounded-cadence ``groww.token.stale_wait`` event per pool. O(1).

        However many pool threads are pausing, the shared :attr:`_stale_last_log`
        stamp (under :attr:`_stale_note_lock`) admits at most one event per
        :data:`~brutex.adapters.groww.constants.GROWW_TOKEN_STALE_LOG_EVERY_S`
        interval — the operator sees a heartbeat-paced "the fetch lane is paused,
        waiting for the fresh token" line, never a per-thread-per-tick storm.
        """
        with self._stale_note_lock:
            if now - self._stale_last_log < GROWW_TOKEN_STALE_LOG_EVERY_S:
                return
            self._stale_last_log = now
        _LOG.warning(
            "groww.token.stale_wait",
            method=method,
            elapsed_s=round(elapsed, 1),
            deadline_s=bound,
        )


__all__ = ["SdkRefresher", "resolve_token_stale_wait_seconds"]
