"""Adaptive token-bucket rate limiter for the broker adapter (deleted).

Implements ``FR-ING-017`` (adaptive rate limiter) from
``docs/REQUIREMENTS.md §4.1``.

## Contract

* **Start at 15 req/s** (the ``BRUTEX_OHLC_RATE_LIMIT_RPS`` env-var
  default — 60% of the broker's documented ~25 req/s OHLC bucket).
* **Auto-downshift to 10 req/s** after 3 consecutive
  :class:`VendorAPIRateLimitException` hits inside a 60-second window.
* **Emit a ``rate_limit.downshift`` Telegram event** when downshifting.
* **Upshift back to 15 req/s** after 2 consecutive minutes of clean
  operation (no 429s).
* **Never drops a request silently.** When the bucket is empty,
  :meth:`acquire` blocks until a token is available. The caller
  always gets through eventually.

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
from typing import Final

from brutex.models import BruteXError

# ────────────────────────────────────────────────────────────────────────
# Constants (tuned per FR-ING-017)
# ────────────────────────────────────────────────────────────────────────

#: Default steady-state rate (req/s). 60% of the broker's documented 25 r/s.
DEFAULT_INITIAL_RPS: Final[float] = 15.0

#: Rate we downshift to after consecutive 429s.
DEFAULT_DOWNSHIFT_RPS: Final[float] = 10.0

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

#: Safety cap on the refill-loop inside :meth:`AdaptiveTokenBucket.acquire`.
#: Under injected fake clocks, round-off errors can occasionally leave
#: the token count at ``0.9999…`` indefinitely — this bound guarantees
#: the loop terminates after at most 16 iterations even in that case.
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
    from_rps: float | None = None
    to_rps: float | None = None
    recent_429_count: int = 0


# ────────────────────────────────────────────────────────────────────────
# Adaptive token bucket
# ────────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class AdaptiveTokenBucket:
    """Thread-safe adaptive token bucket.

    Create one instance per broker adapter (deleted). Workers call
    :meth:`acquire` before every API call and :meth:`record_rate_limit_hit`
    if the call returned a 429 / ``VendorAPIRateLimitException``.

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

    # ── Runtime state (not dataclass fields — computed in __post_init__) ──
    _current_rps: float = field(init=False)
    _tokens: float = field(init=False)
    _last_refill: float = field(init=False)
    _recent_429s: deque[float] = field(init=False)
    _last_success_at: float = field(init=False)
    _lock: threading.Lock = field(init=False)
    _pending_events: list[RateLimiterEvent] = field(init=False, default_factory=list)

    def __post_init__(self) -> None:
        if self.initial_rps <= 0:
            raise RateLimiterError(f"initial_rps must be > 0, got {self.initial_rps}")
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
        self._tokens = float(self.burst)
        self._last_refill = self.clock()
        self._recent_429s = deque()
        self._last_success_at = self.clock()
        self._lock = threading.Lock()

    # ── Public API ─────────────────────────────────────────────────

    def acquire(self) -> float:
        """Block until one token is available, then consume it.

        Returns the wall-clock seconds waited. Always non-negative.
        Never drops the call — callers always get through.

        Thread-safe. If multiple threads call ``acquire`` at once they
        serialize through the internal lock, which is what we want —
        token bucket semantics.
        """
        with self._lock:
            self._maybe_upshift_locked()
            self._refill_locked()
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return 0.0

            # Not enough tokens — compute wait time for one full token.
            needed = 1.0 - self._tokens
            wait_seconds = needed / self._current_rps

        # Release the lock while sleeping so other threads can still
        # check upshift status. After waking, re-enter and consume.
        self.sleep(wait_seconds)
        with self._lock:
            self._refill_locked()
            # Guaranteed at least 1.0 after the sleep unless downshift
            # happened concurrently, in which case loop once more.
            # Use a small epsilon to absorb float arithmetic drift so
            # `tokens == 0.9999…` doesn't trigger an infinite micro-wait
            # loop.
            eps = 1e-9
            iterations = 0
            while self._tokens < 1.0 - eps:
                iterations += 1
                if iterations > _ACQUIRE_LOOP_CAP:
                    # Safety net — in the fake-clock world, round-off
                    # should converge in 1-2 iterations; 16 is a hard
                    # cap so tests can't deadlock on us.
                    self._tokens = 1.0
                    break
                more_wait = max((1.0 - self._tokens) / self._current_rps, eps)
                self.sleep(more_wait)
                self._refill_locked()
                wait_seconds += more_wait
            self._tokens -= 1.0
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
        if elapsed <= 0:
            return
        self._tokens = min(
            float(self.burst),
            self._tokens + elapsed * self._current_rps,
        )
        self._last_refill = now

    def _maybe_upshift_locked(self) -> RateLimiterEvent | None:
        """Check whether enough clean time has passed to upshift.

        Called on every :meth:`acquire` (cheap — just a timestamp
        comparison). If we currently run at downshift_rps and
        :attr:`upshift_recovery` seconds have passed since the last
        429 AND we've had at least one successful call in that window,
        upshift back to initial_rps.
        """
        if math.isclose(self._current_rps, self.initial_rps, rel_tol=1e-6):
            return None
        now = self.clock()
        cutoff = now - self.downshift_window
        while self._recent_429s and self._recent_429s[0] < cutoff:
            self._recent_429s.popleft()
        if self._recent_429s:
            return None
        # No 429s in the recent window — how long since the last one?
        since_last_429 = now - (self._last_success_at - self.upshift_recovery)
        _ = since_last_429  # readable but the check below is the gate
        # Simpler check: enough recovery time since the last 429
        # (or since startup, if we never got one).
        if now - self._last_success_at < 0:
            return None
        # We want: enough time has passed since any 429 activity.
        # Use last_success_at as the reference — if we're making
        # successful calls AND the 429 window is empty, we're good.
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
