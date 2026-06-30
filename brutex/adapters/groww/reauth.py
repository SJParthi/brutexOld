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
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime

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
)
from brutex.adapters.groww.deadline import run_with_deadline
from brutex.adapters.groww.errors import GrowwTokenIssuanceThrottled, from_groww_exception
from brutex.adapters.groww.sdk_protocol import GrowwSDK
from brutex.adapters.groww.token_cache import (
    CachedToken,
    load_cached_token,
    store_token,
)

_LOG = structlog.get_logger("brutex.adapters.groww.reauth")


def _now_utc() -> datetime:
    """The current UTC instant — the token cache's production clock (injectable in tests)."""
    return datetime.now(tz=UTC)


def _real_sdk_factory(token: str) -> GrowwSDK:  # pragma: no cover — needs growwapi + network
    """Default SDK factory: ``GrowwAPI(token)`` (lazy import of the real SDK)."""
    from growwapi import GrowwAPI  # type: ignore[import-not-found, unused-ignore]

    return GrowwAPI(token)  # type: ignore[no-any-return]


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

    Raises
    ------
    GrowwAuthError
        If the INITIAL token issuance fails (redacting ``safe_message`` — the secret
        never leaks), straight from :func:`~brutex.adapters.groww.auth.get_access_token`.
    GrowwTokenIssuanceThrottled
        If the cold-start mint is issuance-THROTTLED with no cache to fall back to,
        after the bounded back-off budget is exhausted (a RETRYABLE subclass — the pull
        quarantines + re-pulls; DEC-GROWW-TOKEN-DISK-CACHE-001).
    """

    __slots__ = (
        "_call_timeout_seconds",
        "_clock",
        "_creds",
        "_generation",
        "_lock",
        "_sdk_factory",
        "_sdks",
        "_size",
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
        self._lock = threading.Lock()
        #: Monotonically rising re-auth counter — starts at 0 for the INITIAL mint;
        #: each successful :meth:`reauth` bumps it, so a caller can prove single-flight.
        self._generation = 0
        #: The INITIAL build — CACHE-FIRST (reuse a same-session token if present, else
        #: mint + write the cache). This is the ONLY cache-READING path; the in-pull
        #: :meth:`reauth` always FORCES a real mint (B1).
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
        2. On a MISS, mint a fresh token. On success, WRITE the cache (best-effort) so
           the other phases/processes reuse it, then build the SDKs.
        3. If the mint is issuance-THROTTLED (no cache), re-check the cache (a racing
           phase/process may have just written one) and, while the bounded attempt budget
           remains, sleep a small back-off + retry. When the budget is exhausted with no
           cache, re-raise the RETRYABLE :class:`~brutex.adapters.groww.errors.\
           GrowwTokenIssuanceThrottled` (the pull quarantines + re-pulls — never a crash).

        The back-off sleeps run HERE, on the cold-start path, OUTSIDE any lock (hostile-
        review M2) — ``reauth``'s locked block never sleeps on a throttle.
        """
        attempts = max(1, GROWW_TOKEN_ISSUANCE_RETRY_ATTEMPTS)
        for attempt in range(attempts):
            cached = self._safe_cache_load()
            if cached is not None:
                _LOG.info("groww.token.cache_hit", session=cached.issued_for_session.isoformat())
                return tuple(self._sdk_factory(cached.access_token) for _ in range(self._size))
            try:
                return self._mint_and_cache_sdks()
            except GrowwTokenIssuanceThrottled:
                # On the LAST attempt re-raise the RETRYABLE throttle (the pull quarantines
                # + re-pulls — never a crash). Otherwise sleep a small back-off OUTSIDE the
                # lock (hostile-review M2) + retry, re-checking the cache first (a racing
                # phase/process may have just written one).
                if attempt == attempts - 1:
                    raise
                backoff = min(
                    GROWW_TOKEN_ISSUANCE_RETRY_BASE_S * (2.0**attempt),
                    GROWW_TOKEN_ISSUANCE_RETRY_CAP_S,
                )
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

    def _mint_and_cache_sdks(self) -> tuple[GrowwSDK, ...]:
        """Mint a fresh token (real network call), WRITE the cache, then build the SDKs.

        The cache write is best-effort (a failure logs + is swallowed inside
        :func:`~brutex.adapters.groww.token_cache.store_token`), so it never blocks the
        build. Used by BOTH the cold-start miss path and (after the forced mint) by
        :meth:`reauth`, so the fresh token is always persisted for reuse.
        """
        token_info = get_access_token(
            self._creds,
            token_factory=self._token_factory,
            totp_minter=self._totp_minter,
        )
        token = token_info.access_token
        self._token_cache_store(self._creds, token, now_utc=self._clock())
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
        """Re-mint the token + SDK connections, SINGLE-FLIGHT against *seen_generation*.

        Under the lock: if :attr:`generation` already moved past *seen_generation*
        (another thread re-authed since the caller's last read — the 06:00 stampede),
        return the CURRENT generation WITHOUT a second mint. Otherwise FORCE a fresh
        network mint, rebuild all *size* SDK connections, bump the generation, store, and
        return the new generation. (Callers re-resolve their SDK via :meth:`sdk_at` /
        :meth:`current_sdk` on the retry — so a returned generation alone suffices.)

        FORCED MINT — NEVER cache-first (hostile-review B1): ``reauth`` is called ONLY
        because the current token was REJECTED, so it does a REAL mint (it would loop on a
        dead token if it re-read the same-session cache) and OVERWRITES the cache with the
        fresh token via :meth:`_mint_and_cache_sdks` — so the 06:00-IST revoked-token case
        self-corrects AND the new token is shared with the other phases/processes. The
        cache READ lives ONLY on the cold-start path (:meth:`_cold_start_sdks`).

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
            If the re-mint itself fails (redacting ``safe_message``) — propagated so
            the client's bounded budget eventually raises terminally for a genuinely
            bad credential (never an infinite loop).
        GrowwTimeoutError
            If the re-mint network call HANGS past :attr:`_call_timeout_seconds` (a
            half-open socket at the 06:00 IST reset) — the FREEZE GUARD (MINOR-1 of
            DEC-GROWW-TOKEN-REAUTH-001). The bounded mint raises a :class:`TimeoutError`
            which is mapped to a RETRYABLE :class:`~brutex.adapters.groww.errors.\
            GrowwTimeoutError`, so the caller's resilience quarantines + re-pulls THAT
            window instead of freezing the whole pool. CRUCIALLY the deadline wraps ONLY
            the network mint (which returns the new SDK tuple WITHOUT touching shared
            state); the lock-guarded PUBLISH happens only AFTER it returns, so a timeout
            propagates out of the ``with self._lock`` block (the lock is RELEASED — never
            a permanent hold) leaving NO half-written state (the abandoned daemon's
            eventual result is discarded).
        """
        with self._lock:
            if self._generation != seen_generation:
                # Another thread already re-authed — single-flight: no second mint.
                return self._generation
            # FREEZE GUARD (MINOR-1): bound ONLY the FORCED network mint by the wall-clock
            # deadline. `_mint_and_cache_sdks` returns the new SDK tuple WITHOUT writing the
            # refresher's shared `_sdks`/`_generation`, so on a timeout `run_with_deadline`
            # raises, the exception leaves the `with self._lock` block (lock released — no
            # permanent hold), and NOTHING was half-published. The in-memory publish (store
            # + bump) runs only AFTER a clean return. The best-effort disk-cache WRITE
            # happens inside the minted call (the token IS valid once minted; the write
            # never raises) — NOT a multi-second sleep, so it cannot starve the deadline.
            try:
                new_sdks = run_with_deadline(self._mint_and_cache_sdks, self._call_timeout_seconds)
            except TimeoutError as exc:
                # Map the deadline TimeoutError to a RETRYABLE GrowwTimeoutError (the
                # "deadline exceeded" hint is recognised by from_groww_exception) so the
                # caller treats a hung re-mint as transient, NOT a terminal auth failure.
                raise from_groww_exception(exc) from exc
            self._sdks = new_sdks
            self._generation += 1
            return self._generation


__all__ = ["SdkRefresher"]
