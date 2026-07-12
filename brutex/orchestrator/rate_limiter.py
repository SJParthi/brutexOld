"""Adaptive token-bucket rate limiter — the COMMON pacing core reused by the vendor adapter.

Vendor facts (CLAUDE.md §2 Rule 6 + §6, current): Groww backtesting
(``get_historical_candles``) runs under an operator-confirmed HARD
**500 req/min** governor and has **NO daily quota** (DEC-GROWW-RATE-PERMIN-001,
2026-06-13, not vendor-documented); the per-SECOND cap is **UNVERIFIED**
(``GROWW_RATE_LIMIT_RPS_UNVERIFIED`` — an open Phase-0 probe gate). Production
pacing is owned by :class:`~brutex.adapters.rate_common.RateController` (AIMD
per-second ladder, operator-directed 8 r/s ceiling
``GROWW_RATE_LIMIT_RPS_CEILING`` + the 500/min governor), which REUSES this
bucket as its single shared token stream (CLAUDE.md §11 COMMON — no
re-implementation of the token math); the production rate is injected via
``initial_rps`` / :meth:`AdaptiveTokenBucket.set_rate`. This module's OWN
defaults (3.0 / 1.5 r/s below, DEC-P0-030, PR #50c) date from the RETIRED
prior-vendor "5 req/s + 100,000/day" model and govern only the direct
env-var path, not the production pull.

## Contract

* **Start at ``DEFAULT_INITIAL_RPS`` req/s** (= 3.0; the
  ``BRUTEX_OHLC_RATE_LIMIT_RPS`` env-var default — a historic 60%-of-5
  tuning, DEC-P0-030; today it merely coincides with the AIMD soft floor.
  The Groww per-second cap is UNVERIFIED per CLAUDE.md §6; production
  injects its own rate via ``RateController``).
* **Auto-downshift to ``DEFAULT_DOWNSHIFT_RPS`` req/s** (= 1.5) after
  ``DEFAULT_DOWNSHIFT_TRIGGER`` (= 3) consecutive 429 / rate-limit
  exception hits inside ``DEFAULT_DOWNSHIFT_WINDOW`` (= 60) seconds.
* **Emit a ``rate_limit.downshift`` Telegram event** when downshifting.
* **Upshift back to ``DEFAULT_INITIAL_RPS``** after
  ``DEFAULT_UPSHIFT_RECOVERY`` (= 120) seconds of clean operation
  (no 429s).
* **Never drops a request silently.** When the bucket is empty,
  :meth:`acquire` blocks until a token is available. The caller
  always gets through eventually.
* **Optional daily quota gate.** When constructed with a
  ``DailyQuotaTracker``, :meth:`acquire` consults the tracker AFTER
  bucket success — if the tracker's daily limit is exhausted, raises
  :class:`~brutex.orchestrator.quota_tracker.DailyQuotaExceeded` which
  the adapter maps to ``Outcome.FATAL`` (halt the phase, no auto-retry).
  DORMANT in production: Groww backtesting has NO daily quota
  (operator-confirmed 2026-06-13, DEC-GROWW-RATE-PERMIN-001), so both
  live build sites construct the controller with ``quota_tracker=None``
  (``dashboard/pull_controller.py`` + ``cli.py``).

## Design

Classic token bucket:

* ``rate`` — tokens added per second (this is what "adaptive" changes)
* ``capacity`` — max tokens in the bucket (small burst allowance)
* ``tokens`` — current token count, recomputed on every call
* ``last_refill_monotonic`` — when tokens were last added

The bucket uses ``time.monotonic()`` so it's immune to NTP clock
jumps, daylight savings, etc.

## Testability

The limiter takes optional ``clock`` and ``sleep`` callables as
constructor arguments. Tests inject fake implementations that
advance a synthetic clock and skip real ``time.sleep`` calls. In
production, these default to ``time.monotonic`` and ``time.sleep``.
"""

from __future__ import annotations

import math
import threading
import time as _time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from brutex.orchestrator.quota_tracker import DailyQuotaTracker

from brutex.models import BruteXError

# ────────────────────────────────────────────────────────────────────────
# Constants (tuned per FR-ING-017)
# ────────────────────────────────────────────────────────────────────────

#: Default steady-state rate (req/s). A historic 60%-of-5 tuning from the
#: RETIRED prior-vendor model (the Groww per-second cap is UNVERIFIED per
#: CLAUDE.md §6). Was 15.0 (calibrated for the deleted broker's 25 r/s);
#: retuned to 3.0 in DEC-P0-030 (PR #50c).
DEFAULT_INITIAL_RPS: Final[float] = 3.0

#: Rate we downshift to after consecutive 429s. Was 10.0 (broker-tuned);
#: retuned to 1.5 (50% of new initial) in DEC-P0-030. The invariant
#: ``downshift_rps < initial_rps`` enforced in ``__post_init__`` forces
#: atomic swap of both constants.
DEFAULT_DOWNSHIFT_RPS: Final[float] = 1.5

#: How many consecutive 429s within :data:`DEFAULT_DOWNSHIFT_WINDOW`
#: seconds trigger the downshift.
DEFAULT_DOWNSHIFT_TRIGGER: Final[int] = 3

#: Rolling window (seconds) over which we count 429s for downshift.
DEFAULT_DOWNSHIFT_WINDOW: Final[float] = 60.0

#: How long (seconds) we must stay clean before upshifting back.
DEFAULT_UPSHIFT_RECOVERY: Final[float] = 120.0

#: Burst capacity — how many tokens the bucket holds when full.
#: A burst of 5 lets us absorb short cadence variations without
#: dropping below the steady-state rate.
DEFAULT_BURST: Final[int] = 5

#: Legacy hard cap (5 r/s) carried over from the pre-pivot adapter — a
#: RETIRED prior-vendor figure (the Groww per-second cap is UNVERIFIED per
#: CLAUDE.md §6). The ``initial_rps <= _LEGACY_HARD_CAP_RPS`` defense-in-depth
#: clamp proposed per DEC-P0-031 (PR #50d, Agent 2 §D) was DEFERRED and never
#: landed (see the ``__post_init__`` NOTE) — the production
#: :class:`RateController` in ``brutex/adapters/rate_common.py`` relies on the
#: clamp's ABSENCE to run its operator-directed 8 r/s ceiling; do not add it.
_LEGACY_HARD_CAP_RPS: Final[float] = 5.0

#: Safety cap on the refill-loop inside :meth:`AdaptiveTokenBucket.acquire`.
#: Under injected fake clocks, round-off errors can occasionally leave
#: the token count at ``0.9999…`` indefinitely — this bound guarantees
#: the loop terminates after at most 16 iterations even in that case.
#: Post-DEC-RATE-SLEEP-OUTSIDE-LOCK-001 it ALSO bounds wake/re-test
#: contention loops (a woken sleeper can lose its token to a peer) —
#: fail-OPEN, the same posture as ``_GOVERNOR_LOOP_CAP`` /
#: ``_SMOOTH_LOOP_CAP`` in ``brutex/adapters/rate_common.py``.
#:
#: RATE-SAFETY RELEVANCE (round-2 refuter, MIGRATION.md #1759): each cap trip
#: force-grants ONE token WITHOUT a matching refill (a "phantom" issue that
#: momentarily exceeds the configured SOFT per-second rate), and the trip is
#: production-REACHABLE under sustained contention at demand > rate — so
#: LOWERING this value MULTIPLIES phantoms (measured ~10x overshoot at cap=2
#: on a 32-thread hammer) while raising it only delays the fail-open. Change
#: it consciously: the value is pinned by
#: ``test_peer_steal_to_cap_force_grant_is_counted`` and every trip is
#: counted observably (:meth:`AdaptiveTokenBucket.acquire_fail_open_count`).
#: The HARD 500/min rolling governor in ``rate_common.py`` is independent of
#: this cap and remains the true account cap (CLAUDE.md §2 Rule 6).
_ACQUIRE_LOOP_CAP: Final[int] = 16


# ────────────────────────────────────────────────────────────────────────
# Errors
# ────────────────────────────────────────────────────────────────────────


class RateLimiterError(BruteXError):
    """Base class for rate limiter errors."""


# ────────────────────────────────────────────────────────────────────────
# Event type emitted on state transitions
# ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class RateLimiterEvent:
    """One anomaly/state-change event from the limiter.

    Published to subscribers (dashboard WebSocket, Telegram notifier,
    audit log). Each event has a ``kind`` label and an ``at``
    timestamp (monotonic seconds, NOT wall clock).
    """

    kind: str  # one of: "downshift" | "upshift" | "rate_limit_hit"
    at: float  # monotonic seconds
    # ``# pragma: no mutate`` ×2 (DEC-MUTATION-RUNTIME-001): mutmut's
    # ``float | None`` → ``float & None`` annotation flips are provably
    # equivalent (PEP 563 ``from __future__ import annotations`` — field
    # annotations are never evaluated). The pragmas are line-scoped, so the
    # ``None`` defaults they also skip are pinned byte-exact by
    # ``test_optional_fields_default_byte_exact``.
    from_rps: float | None = None  # pragma: no mutate
    to_rps: float | None = None  # pragma: no mutate
    recent_429_count: int = 0


# ────────────────────────────────────────────────────────────────────────
# Adaptive token bucket
# ────────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class AdaptiveTokenBucket:
    """Thread-safe adaptive token bucket.

    Create one instance per vendor adapter. Workers call
    :meth:`acquire` before every API call and :meth:`record_rate_limit_hit`
    if the call returned a vendor 429 / rate-limit response
    (:class:`~brutex.adapters.groww.errors.GrowwRateLimitError` per the Groww
    error hierarchy in ``brutex/adapters/groww/errors.py``).

    Parameters
    ----------
    initial_rps
        Steady-state rate in tokens per second.
    downshift_rps
        Rate we switch to after consecutive 429s.
    downshift_trigger
        How many 429s within :attr:`downshift_window` trigger the
        downshift.
    downshift_window
        Seconds — the rolling window used for the 429 count.
    upshift_recovery
        Seconds — how long we must be clean before upshifting back.
    burst
        Max tokens held in the bucket (allows short bursts).
    clock
        Callable returning monotonic seconds. Defaults to
        :func:`time.monotonic`. Injectable for tests.
    sleep
        Callable taking a seconds argument. Defaults to
        :func:`time.sleep`. Injectable for tests.
    """

    initial_rps: float = DEFAULT_INITIAL_RPS
    downshift_rps: float = DEFAULT_DOWNSHIFT_RPS
    downshift_trigger: int = DEFAULT_DOWNSHIFT_TRIGGER
    downshift_window: float = DEFAULT_DOWNSHIFT_WINDOW
    upshift_recovery: float = DEFAULT_UPSHIFT_RECOVERY
    burst: int = DEFAULT_BURST
    clock: Callable[[], float] = _time.monotonic
    sleep: Callable[[float], None] = _time.sleep
    #: Optional daily-quota gate. When set, :meth:`acquire` calls
    #: ``quota_tracker.acquire_or_raise()`` AFTER the bucket token is
    #: consumed (Agent 1 §A — outside the bucket lock to avoid leaking
    #: 150µs LMDB latency into observability reads). When ``None``, the
    #: bucket runs in legacy "bucket-only" mode (production callers
    #: MUST always pass a tracker; ``None`` is for test fixtures only).
    #: ``# pragma: no mutate`` (DEC-MUTATION-RUNTIME-001): the ``| None`` →
    #: ``& None`` annotation flip is provably equivalent (PEP 563 — never
    #: evaluated; ``DailyQuotaTracker`` is a TYPE_CHECKING-only name). The
    #: ``None`` default the line-scoped pragma also skips stays pinned by
    #: ``test_quota_tracker_none_is_legacy_path``.
    quota_tracker: DailyQuotaTracker | None = None  # pragma: no mutate
    #: Ease-in: start the bucket EMPTY (0 tokens) instead of full (``burst``).
    #: With the default ``False`` the bucket grants its ``burst`` allowance
    #: immediately (legacy behaviour). The :class:`RateController` passes ``True`` so
    #: that the opening call cannot release a free burst token — it waits one full
    #: ``1/rate`` interval. A mid-pull rate change goes through :meth:`set_rate`
    #: (NOT a rebuild), which likewise drains the bucket to empty, so a stepdown
    #: can't release a stale higher-rate token either — keeping the PHYSICAL issue
    #: rate strictly ≤ the current rate at all times (DEC-DHAN-RATE-HARD-CAP-001 +
    #: DEC-DHAN-RATE-SINGLE-BUCKET-001 / Golden Rule #6).
    start_empty: bool = False

    # ── Runtime state (not dataclass fields — computed in __post_init__) ──
    _current_rps: float = field(init=False)
    _tokens: float = field(init=False)
    _last_refill: float = field(init=False)
    _recent_429s: deque[float] = field(init=False)
    _last_429_at: float = field(init=False)
    _last_success_at: float = field(init=False)
    _lock: threading.Lock = field(init=False)
    _pending_events: list[RateLimiterEvent] = field(init=False, default_factory=list)
    _acquire_fail_opens: int = field(init=False)

    def __post_init__(self) -> None:
        if self.initial_rps <= 0:
            raise RateLimiterError(f"initial_rps must be > 0, got {self.initial_rps}")
        # NOTE: Defense-in-depth clamp `initial_rps <= _LEGACY_HARD_CAP_RPS`
        # (Agent 2 §D from PR #50d pre-impl review) was intentionally
        # DEFERRED and never landed. The production caller is
        # `rate_common.RateController` (the shared live-pull pacer), which
        # RELIES on the clamp's ABSENCE to run the operator-directed 8 r/s
        # ceiling (GROWW_RATE_LIMIT_RPS_CEILING, DEC-GROWW-RATE-CEILING-8-002).
        # Do NOT add the deferred clamp — it would raise RateLimiterError
        # on the live pull.
        if self.downshift_rps <= 0:
            raise RateLimiterError(f"downshift_rps must be > 0, got {self.downshift_rps}")
        if self.downshift_rps >= self.initial_rps:
            raise RateLimiterError(
                f"downshift_rps ({self.downshift_rps}) must be < initial_rps ({self.initial_rps})"
            )
        if self.downshift_trigger < 1:
            raise RateLimiterError(f"downshift_trigger must be ≥ 1, got {self.downshift_trigger}")
        if self.burst < 1:
            raise RateLimiterError(f"burst must be ≥ 1, got {self.burst}")

        self._current_rps = self.initial_rps
        self._tokens = 0.0 if self.start_empty else float(self.burst)
        self._last_refill = self.clock()
        self._recent_429s = deque()
        # Sentinel "no 429 yet" — only ever read by ``_maybe_upshift_locked``,
        # which is itself unreachable until a downshift has stamped a real
        # timestamp here (the early-return at ``_current_rps == initial_rps``
        # guards the never-downshifted path). ``-inf`` keeps the recovery gate
        # ``now - _last_429_at >= upshift_recovery`` trivially open if it were
        # ever reached without a downshift.
        self._last_429_at = float("-inf")
        self._last_success_at = self.clock()
        self._lock = threading.Lock()
        self._acquire_fail_opens = 0

    # ── Public API ─────────────────────────────────────────────────

    def acquire(self) -> float:
        """Block until one token is available, then consume it.

        Returns the wall-clock seconds waited. Always non-negative.
        Never drops the call — callers always get through.

        Thread-safe. If multiple threads call ``acquire`` at once they
        serialize through the internal lock for O(1) state work only —
        ALL sleeps (both the first wait and the retry loop's) run with
        the lock released (DEC-RATE-SLEEP-OUTSIDE-LOCK-001) — which is
        what we want: token bucket semantics without a sleeper freezing
        the observability/`set_rate` readers.
        """
        with self._lock:
            self._maybe_upshift_locked()
            self._refill_locked()
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                wait_seconds = 0.0
            else:
                # Not enough tokens — compute wait time for one full token.
                needed = 1.0 - self._tokens
                wait_seconds = needed / self._current_rps

        if wait_seconds > 0.0:
            # Release the lock while sleeping so other threads can still
            # read stats / step the rate. BOTH sleeps in this method (this
            # first one and the retry loop's below) run with the lock
            # RELEASED (DEC-RATE-SLEEP-OUTSIDE-LOCK-001).
            self.sleep(wait_seconds)
            with self._lock:
                self._refill_locked()
                # NOT guaranteed >= 1.0 after the sleep: a concurrent
                # set_rate/downshift drains the bucket, and (post-fix) a
                # peer may consume the token we waited for while we slept
                # unlocked — in either case loop once more: compute-under-
                # lock / sleep-outside-lock / re-check, the
                # DEC-GROWW-GOVERNOR-ATOMIC-001 idiom (CLAUDE.md §11
                # COMMON; mirrors RateController.pace() Gate 1). Use a
                # small epsilon to absorb float arithmetic drift so
                # `tokens == 0.9999…` doesn't trigger an infinite
                # micro-wait loop.
                eps = 1e-9
                iterations = 0
                while self._tokens < 1.0 - eps:
                    iterations += 1
                    if iterations > _ACQUIRE_LOOP_CAP:
                        # Safety net — bounds BOTH fake-clock round-off AND
                        # (post-fix) wake/re-test contention loops; fails
                        # OPEN like _GOVERNOR_LOOP_CAP/_SMOOTH_LOOP_CAP.
                        self._tokens = 1.0
                        # Observe-only accounting (round-2 refuter fold,
                        # MIGRATION.md #1759): the force-granted token has NO
                        # matching refill — a "phantom" issue past the SOFT
                        # per-second rate — so every trip is counted
                        # (acquire_fail_open_count()). The module is
                        # deliberately logger-free (nothing blocks under or
                        # around _lock — the DEC-RATE-LOG-OUTSIDE-LOCK-001
                        # program), so the counter IS the exposure surface.
                        self._acquire_fail_opens += 1
                        # ``# pragma: no mutate`` (DEC-MUTATION-RUNTIME-001):
                        # ``_tokens`` was just set to 1.0 UNDER the lock and
                        # the lock is held through the guard re-test (the
                        # release below only spans the sleep), so the loop
                        # guard ``_tokens < 1.0 - eps`` is statically False —
                        # mutmut's ``break`` → ``continue`` re-tests the
                        # guard and exits identically. Provably equivalent.
                        break  # pragma: no mutate
                    more_wait = max((1.0 - self._tokens) / self._current_rps, eps)
                    # Sleep with the lock RELEASED (DEC-RATE-SLEEP-OUTSIDE-
                    # LOCK-001): holding it across this sleep froze
                    # current_rps()/set_rate()/record_*() — and,
                    # transitively, the WHOLE RateController (its
                    # on_rate_limit/on_timeout/on_success call set_rate
                    # under the CONTROLLER lock) — for up to a full
                    # 1/current_rps (measured 190-200 ms at 5 r/s). The
                    # finally re-acquires even if sleep raises, so the
                    # enclosing `with` stays balanced; the loop guard then
                    # re-tests tokens under the lock, so the (re-check,
                    # consume) stays atomic — a peer may legitimately take
                    # the token we waited for (bounded by
                    # _ACQUIRE_LOOP_CAP, fail-OPEN — the governor's
                    # accepted contention posture).
                    self._lock.release()
                    try:
                        self.sleep(more_wait)
                    finally:
                        self._lock.acquire()
                    self._refill_locked()
                    wait_seconds += more_wait
                self._tokens -= 1.0

        # Daily quota gate (DEC-P0-030, PR #50c). Called OUTSIDE the
        # bucket lock per Agent 1 §A to avoid leaking 150µs LMDB latency
        # into dashboard reads. The quota tracker has its own lock; the
        # two locks compose sequentially (bucket released → quota acquired)
        # so no deadlock possible. If the daily quota is exhausted, the
        # bucket token is "wasted" — acceptable per FATAL semantics
        # (phase halts, no auto-retry).
        if self.quota_tracker is not None:
            # May raise DailyQuotaExceeded; bubbles up to the caller.
            self.quota_tracker.acquire_or_raise()

        return wait_seconds

    def record_rate_limit_hit(self) -> RateLimiterEvent | None:
        """Called when a call just returned a 429 / rate-limit exception.

        Adds the hit to the rolling window. May trigger a downshift
        if the trigger threshold is exceeded.

        Returns
        -------
        RateLimiterEvent | None
            A ``downshift`` event if we just downshifted, otherwise
            a ``rate_limit_hit`` event for dashboard visibility.
            Callers should publish returned events to Telegram / the
            dashboard / the audit log.
        """
        with self._lock:
            now = self.clock()
            self._recent_429s.append(now)
            # Stamp the most-recent 429 so the upshift recovery gate
            # (``_maybe_upshift_locked``) can require a full
            # :attr:`upshift_recovery` window of quiet AFTER it, independently
            # of the shorter ``downshift_window`` the rolling 429 count uses.
            self._last_429_at = now
            # Evict 429s older than the window
            cutoff = now - self.downshift_window
            while self._recent_429s and self._recent_429s[0] < cutoff:
                self._recent_429s.popleft()

            count = len(self._recent_429s)

            # Should we downshift?
            if (
                math.isclose(self._current_rps, self.initial_rps, rel_tol=1e-6)
                and count >= self.downshift_trigger
            ):
                from_rps = self._current_rps
                self._current_rps = self.downshift_rps
                # Drain bucket so callers feel the new rate immediately
                self._tokens = min(self._tokens, float(self.burst))
                return RateLimiterEvent(
                    kind="downshift",
                    at=now,
                    from_rps=from_rps,
                    to_rps=self.downshift_rps,
                    recent_429_count=count,
                )

            return RateLimiterEvent(
                kind="rate_limit_hit",
                at=now,
                from_rps=self._current_rps,
                to_rps=self._current_rps,
                recent_429_count=count,
            )

    def record_success(self) -> None:
        """Called after any successful (non-429) call.

        Updates the "last clean operation" timestamp used by the
        upshift logic.
        """
        with self._lock:
            self._last_success_at = self.clock()

    def current_rps(self) -> float:
        """Dashboard stat — current effective rate."""
        with self._lock:
            return self._current_rps

    def set_rate(self, new_rps: float) -> None:
        """Atomically change the steady-state rate on this LIVE bucket — O(1).

        (O(1) holds even while another worker waits in :meth:`acquire`'s retry
        loop — the bucket never sleeps while holding its lock,
        DEC-RATE-SLEEP-OUTSIDE-LOCK-001.)

        The :class:`RateController`'s step ladder (5→4→3 down / 3→4→5 up) calls this
        so a rate change mutates the ONE shared bucket IN PLACE instead of swapping in
        a freshly-built one. Swapping created a transient where in-flight
        :meth:`acquire` callers paced on the OLD bucket while new callers paced on
        the NEW one — two independent token streams whose COMBINED physical issue
        rate briefly exceeded the cap (the operator's ``rate.breach rps_1s=11``
        opening 5→4→3 cascade). With a single persistent object every caller
        shares one ≤ ``new_rps`` token stream at all times
        (DEC-DHAN-RATE-SINGLE-BUCKET-001 / CLAUDE.md §2 Rule 6).

        Drains the bucket to EMPTY on the change — exactly mirroring the prior
        ``start_empty=True`` rebuild — so the first call after a DOWNSHIFT waits a
        full ``1/new_rps`` interval and cannot release a stale higher-rate token.
        ``initial_rps`` (and the unused ``downshift_rps``) move WITH the rate so
        the bucket's own dormant 429/upshift logic stays self-consistent: a
        controller never feeds the bucket 429s — it owns its own ladder — so with
        ``_current_rps == initial_rps`` the internal :meth:`_maybe_upshift_locked`
        stays a no-op and never fights the controller's chosen rate.

        Raises
        ------
        RateLimiterError
            If *new_rps* is not strictly positive.
        """
        if new_rps <= 0:
            raise RateLimiterError(f"new_rps must be > 0, got {new_rps}")
        with self._lock:
            self.initial_rps = new_rps
            self.downshift_rps = new_rps / 2.0
            self._current_rps = new_rps
            self._tokens = 0.0
            self._last_refill = self.clock()

    def available_tokens(self) -> float:
        """Dashboard stat — how many tokens are currently in the bucket."""
        with self._lock:
            self._refill_locked()
            return self._tokens

    def recent_429_count(self) -> int:
        """Dashboard stat — how many 429s in the rolling window."""
        with self._lock:
            now = self.clock()
            cutoff = now - self.downshift_window
            while self._recent_429s and self._recent_429s[0] < cutoff:
                self._recent_429s.popleft()
            return len(self._recent_429s)

    def acquire_fail_open_count(self) -> int:
        """Dashboard stat — cumulative :meth:`acquire` fail-OPEN force-grants.

        Each count is ONE "phantom" token granted WITHOUT a matching refill:
        the retry loop lost its refilled token to peers ``_ACQUIRE_LOOP_CAP``
        consecutive times and force-granted rather than starve — production-
        reachable under sustained contention at demand > rate since
        DEC-RATE-SLEEP-OUTSIDE-LOCK-001 (quantified envelope in MIGRATION.md
        #1759). Observe-only; the HARD 500/min governor is unaffected.
        """
        with self._lock:
            return self._acquire_fail_opens

    def pending_events(self) -> list[RateLimiterEvent]:
        """Drain and return any pending events.

        Currently unused — events are returned directly from
        :meth:`record_rate_limit_hit`. Kept for a future batched
        mode if we add upshift auto-events.
        """
        with self._lock:
            events = self._pending_events[:]
            self._pending_events.clear()
            return events

    # ── Internal helpers (lock held by caller) ────────────────────

    def _refill_locked(self) -> None:
        """Refill the token bucket based on elapsed wall-clock time (caller holds the lock)."""
        now = self.clock()
        elapsed = now - self._last_refill
        # ``# pragma: no mutate`` (DEC-MUTATION-RUNTIME-001): at ``elapsed == 0``
        # the body below is a no-op (``tokens + 0 * rps == tokens``; the ``min``
        # clamp is inert under the ``tokens <= burst`` invariant every writer
        # preserves; ``_last_refill = now`` rewrites the identical value), so
        # mutmut's ``<=`` → ``<`` flip is provably equivalent. The ``0 → 1`` flip
        # the line-scoped pragma also skips is a REAL mutant — pinned byte-exact
        # by ``test_subsecond_elapsed_refill_pinned``.
        if elapsed <= 0:  # pragma: no mutate
            return
        self._tokens = min(
            float(self.burst),
            self._tokens + elapsed * self._current_rps,
        )
        self._last_refill = now

    def _maybe_upshift_locked(self) -> RateLimiterEvent | None:
        """Check whether enough clean time has passed to upshift.

        Called on every :meth:`acquire` (cheap — just a timestamp
        comparison). If we currently run at downshift_rps and a full
        :attr:`upshift_recovery` window has elapsed since the most recent
        429 (``self._last_429_at``) AND we've had at least one successful
        call, upshift back to initial_rps.

        The recovery gate is keyed on :attr:`upshift_recovery` (the
        documented "stay clean before upshifting back" window — typically
        twice the rolling :attr:`downshift_window` used to COUNT 429s for the
        downshift trigger), so the bucket never recovers faster than the spec
        promises (Golden Rule #6: never recover faster than specified).
        """
        if math.isclose(self._current_rps, self.initial_rps, rel_tol=1e-6):
            return None
        now = self.clock()
        # Recovery gate: the most-recent 429 must be at least
        # ``upshift_recovery`` seconds in the past. ``_last_429_at`` is always
        # a real timestamp here because reaching this point requires a prior
        # downshift, and every downshift stamps it (the construction sentinel
        # ``-inf`` is never observed).
        if now - self._last_429_at < self.upshift_recovery:
            return None
        # Defensive clock-anomaly guard: a backwards-stepping injected clock
        # could put ``now`` before the last success — never upshift early.
        if now - self._last_success_at < 0:
            return None
        # A monotonic clock CAN legitimately start at 0.0; require at least one
        # recorded success before trusting the recovery window.
        if self._last_success_at <= 0:
            return None
        # Upshift!
        from_rps = self._current_rps
        self._current_rps = self.initial_rps
        event = RateLimiterEvent(
            kind="upshift",
            at=now,
            from_rps=from_rps,
            to_rps=self.initial_rps,
            recent_429_count=0,
        )
        self._pending_events.append(event)
        return event


__all__ = [
    "DEFAULT_BURST",
    "DEFAULT_DOWNSHIFT_RPS",
    "DEFAULT_DOWNSHIFT_TRIGGER",
    "DEFAULT_DOWNSHIFT_WINDOW",
    "DEFAULT_INITIAL_RPS",
    "DEFAULT_UPSHIFT_RECOVERY",
    "AdaptiveTokenBucket",
    "RateLimiterError",
    "RateLimiterEvent",
]
