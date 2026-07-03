"""Adaptive rate-limit state machine — DORMANT (no production caller).

HISTORY: designed per ``docs/MIGRATION.md`` entry #246 (DEC-VENDOR-PIVOT-001)
against the RETIRED prior vendor's "5 req/s + 100,000/day" model; the planned
Phase-1 adapter wiring never landed. Production rate governance is instead
:class:`~brutex.adapters.rate_common.RateController` (AIMD per-second ladder +
the operator-confirmed HARD 500 req/min governor), injected at both live build
sites — see CLAUDE.md §6 "Adapter effective cap". CURRENT vendor facts
(CLAUDE.md §6): the Groww backtesting per-SECOND cap is UNVERIFIED (Phase-0
probe gate), the hard limit is **500 req/min** and there is **NO daily quota**
(DEC-GROWW-RATE-PERMIN-001, 2026-06-13). This module stays a pure-logic
4-state machine with no importer outside its unit tests
(``tests/unit/test_adaptive_rate_limiter.py``); its ``BRUTEX_RATE_*`` env vars
are documented as tracked residue in ``docs/runbooks/ENV_VARS.md``. It
**trends a rate within ``[min, max]``** via a 4-state machine:

* ``PROBING_UP`` — actively trying to raise the rate by 1 r/s every
  ``step_up_clean_window`` seconds of zero rate-limit hits.
* ``LOCKED`` — rate has hit ``max_rps`` (the ceiling); stays locked
  until a rate-limit hit forces a transition to ``PROBING_DOWN``.
* ``PROBING_DOWN`` — actively dropping the rate by 1 r/s every
  ``step_down_window`` seconds in which ``step_down_hits`` rate-limit
  exceptions occurred.
* ``FLOOR_BREACHED`` — rate dropped below ``min_rps``. Terminal state:
  the caller MUST halt and the operator MUST intervene
  (Telegram CRITICAL alert + manual investigation).

The asymmetry is intentional — step-down is fast (1 minute of pain),
step-up is slow (5 minutes clean) — so the limiter trends down on
trouble and up on calm, matching the operator's stated risk
preference in MIGRATION entry #246.

## Contract

The module is a **pure state machine** — it owns no HTTP client, no
token bucket, no I/O. The caller drives it via three methods:

* :meth:`AdaptiveRateLimiter.record_rate_limit_hit` — called by the
  HTTP layer whenever the Groww adapter raises ``GrowwRateLimitError``
  (``brutex/adapters/groww/errors.py``; or any caller-defined
  equivalent). Advances the step-down counter and may trigger a
  transition.
* :meth:`AdaptiveRateLimiter.record_clean_call` — called by the HTTP
  layer on every successful (non-rate-limited) request. Advances the
  step-up clean-window counter.
* :meth:`AdaptiveRateLimiter.tick` — optional periodic check called
  by a scheduler (e.g. every ``recheck_interval`` seconds) so a long
  clean window in low-traffic conditions still progresses through
  the step-up cadence even without active calls.

The caller reads :attr:`AdaptiveRateLimiter.current_rps` to decide
how to space outgoing requests, and listens to :attr:`on_transition`
to wire audit-log entries + Telegram events per the DEC-VENDOR-PIVOT-001
matrix.

All thresholds are env-overridable per MIGRATION entry #246. The defaults
below are MODULE defaults inherited from the retired [3..5] r/s design —
NOT current Groww limits (per-second cap UNVERIFIED, CLAUDE.md §6):

* ``BRUTEX_RATE_MIN_RPS`` (default ``3.0``)
* ``BRUTEX_RATE_MAX_RPS`` (default ``5.0``)
* ``BRUTEX_RATE_INITIAL_RPS`` (default ``5.0`` — start at cap, descend on errors)
* ``BRUTEX_RATE_STEP_DOWN_HITS`` (default ``3``)
* ``BRUTEX_RATE_STEP_DOWN_WINDOW`` (default ``60.0`` seconds)
* ``BRUTEX_RATE_STEP_UP_CLEAN_WINDOW`` (default ``300.0`` seconds = 5 min)
* ``BRUTEX_RATE_RECHECK_INTERVAL`` (default ``60.0`` seconds — informational)

## Testability

The state machine takes an optional ``clock`` callable. Tests inject a
synthetic clock so all timing is deterministic + side-effect-free.

## Decision

DEC-ADAPTIVE-RATE-LIMITER-001 (MIGRATION entry #249). Pure-logic module;
the Phase-1 vendor-adapter wiring was SUPERSEDED by
``brutex/adapters/rate_common.py::RateController`` — this module has no
production caller and is retained as tested, dormant logic.
"""

from __future__ import annotations

import os
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final


class RateLimiterState(StrEnum):
    """Current state of the adaptive-rate state machine.

    Each value is the operator-facing string serialised to audit log
    + Telegram event payloads. Strings are stable across releases.
    """

    PROBING_UP = "probing_up"
    LOCKED = "locked"
    PROBING_DOWN = "probing_down"
    FLOOR_BREACHED = "floor_breached"


@dataclass(frozen=True, slots=True)
class StateTransition:
    """Emitted on every state change.

    Carries enough context for the listener to write an audit-JSONL
    entry, fire a Telegram event, refresh a dashboard, etc., without
    re-reading the limiter's internal state.

    Attributes
    ----------
    from_state
        State the machine was in before the transition.
    to_state
        State the machine is in after the transition.
    from_rps
        Rate (req/s) the limiter was at before the transition.
    to_rps
        Rate (req/s) the limiter is at after the transition.
    timestamp
        Monotonic clock value at transition time.
    reason
        Short human-readable explanation (e.g. ``"step_down_hits"``,
        ``"step_up_clean_window"``, ``"floor_breach"``,
        ``"ceiling_reached"``).
    """

    from_state: RateLimiterState
    to_state: RateLimiterState
    from_rps: float
    to_rps: float
    timestamp: float
    reason: str


TransitionCallback = Callable[[StateTransition], None]


# ────────────────────────────────────────────────────────────────────────
# Default thresholds (env-overridable per MIGRATION entry #246)
# ────────────────────────────────────────────────────────────────────────

DEFAULT_MIN_RPS: Final[float] = 3.0
DEFAULT_MAX_RPS: Final[float] = 5.0
DEFAULT_INITIAL_RPS: Final[float] = 5.0  # start at the 5 r/s cap, descend on errors
DEFAULT_STEP_DOWN_HITS: Final[int] = 3
DEFAULT_STEP_DOWN_WINDOW: Final[float] = 60.0
DEFAULT_STEP_UP_CLEAN_WINDOW: Final[float] = 300.0  # 5 minutes
DEFAULT_RECHECK_INTERVAL: Final[float] = 60.0


def _parse_float_env(name: str, default: float) -> float:
    """Parse a float env var, falling back to ``default`` on missing / unparseable."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _parse_int_env(name: str, default: int) -> int:
    """Parse an int env var, falling back to ``default`` on missing / unparseable."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True, slots=True)
class AdaptiveRateLimiterConfig:
    """Thresholds + initial rate for the adaptive state machine."""

    min_rps: float = DEFAULT_MIN_RPS
    max_rps: float = DEFAULT_MAX_RPS
    initial_rps: float = DEFAULT_INITIAL_RPS
    step_down_hits: int = DEFAULT_STEP_DOWN_HITS
    step_down_window: float = DEFAULT_STEP_DOWN_WINDOW
    step_up_clean_window: float = DEFAULT_STEP_UP_CLEAN_WINDOW
    recheck_interval: float = DEFAULT_RECHECK_INTERVAL

    def __post_init__(self) -> None:
        if self.min_rps <= 0:
            raise ValueError(f"min_rps must be > 0, got {self.min_rps}")
        if self.max_rps < self.min_rps:
            raise ValueError(f"max_rps ({self.max_rps}) must be >= min_rps ({self.min_rps})")
        if not (self.min_rps <= self.initial_rps <= self.max_rps):
            raise ValueError(
                f"initial_rps ({self.initial_rps}) must be in [{self.min_rps}, {self.max_rps}]"
            )
        if self.step_down_hits < 1:
            raise ValueError(f"step_down_hits must be >= 1, got {self.step_down_hits}")
        if self.step_down_window <= 0:
            raise ValueError(f"step_down_window must be > 0, got {self.step_down_window}")
        if self.step_up_clean_window <= 0:
            raise ValueError(f"step_up_clean_window must be > 0, got {self.step_up_clean_window}")

    @classmethod
    def from_env(cls) -> AdaptiveRateLimiterConfig:
        """Build from ``BRUTEX_RATE_*`` env vars; defaults on missing / bad."""
        return cls(
            min_rps=_parse_float_env("BRUTEX_RATE_MIN_RPS", DEFAULT_MIN_RPS),
            max_rps=_parse_float_env("BRUTEX_RATE_MAX_RPS", DEFAULT_MAX_RPS),
            initial_rps=_parse_float_env("BRUTEX_RATE_INITIAL_RPS", DEFAULT_INITIAL_RPS),
            step_down_hits=_parse_int_env("BRUTEX_RATE_STEP_DOWN_HITS", DEFAULT_STEP_DOWN_HITS),
            step_down_window=_parse_float_env(
                "BRUTEX_RATE_STEP_DOWN_WINDOW", DEFAULT_STEP_DOWN_WINDOW
            ),
            step_up_clean_window=_parse_float_env(
                "BRUTEX_RATE_STEP_UP_CLEAN_WINDOW", DEFAULT_STEP_UP_CLEAN_WINDOW
            ),
            recheck_interval=_parse_float_env(
                "BRUTEX_RATE_RECHECK_INTERVAL", DEFAULT_RECHECK_INTERVAL
            ),
        )


@dataclass(slots=True)
class AdaptiveRateLimiter:
    """4-state adaptive rate limiter for the vendor Data API.

    See module docstring for the full state machine specification.

    Parameters
    ----------
    config
        Threshold + initial-rate bundle. Use :meth:`AdaptiveRateLimiterConfig.from_env`
        for the env-driven build.
    clock
        Monotonic clock callable. Defaults to ``time.monotonic``. Tests
        inject a synthetic clock for determinism.
    on_transition
        Optional listener invoked synchronously on every state transition.
        The callback must NOT block — it should enqueue work (audit-log
        entry, Telegram event) and return quickly. Exceptions raised by
        the callback propagate to the caller (this is intentional —
        unlike rate-limit hits which are recoverable, a broken audit
        callback is a programmer error and should fail loudly).
    """

    config: AdaptiveRateLimiterConfig
    clock: Callable[[], float] | None = None
    on_transition: TransitionCallback | None = None
    _clock_fn: Callable[[], float] = field(init=False)
    _state: RateLimiterState = field(init=False)
    _current_rps: float = field(init=False)
    _hit_timestamps: deque[float] = field(init=False)
    _last_hit_at: float | None = field(init=False, default=None)
    _last_step_up_check_at: float = field(init=False, default=0.0)
    _lock: threading.Lock = field(init=False, default_factory=threading.Lock)

    def __post_init__(self) -> None:
        # Late default so dataclass doesn't bind time.monotonic at import time
        # (which would freeze tests that monkeypatch it). The public `clock`
        # field stays None for introspection; `_clock_fn` is the always-set
        # one we read from internally.
        if self.clock is not None:
            self._clock_fn = self.clock
        else:
            import time

            self._clock_fn = time.monotonic
        self._current_rps = self.config.initial_rps
        # Initial state depends on initial vs ceiling/floor: if we start at the
        # ceiling, we're LOCKED (no room to probe up); otherwise PROBING_UP.
        self._state = (
            RateLimiterState.LOCKED
            if self._current_rps >= self.config.max_rps
            else RateLimiterState.PROBING_UP
        )
        self._hit_timestamps = deque(maxlen=max(self.config.step_down_hits * 4, 32))
        self._last_step_up_check_at = self._clock_fn()

    @property
    def current_rps(self) -> float:
        """The rate (req/s) the caller should use right now."""
        return self._current_rps

    @property
    def state(self) -> RateLimiterState:
        """Current state of the machine."""
        return self._state

    def record_rate_limit_hit(self) -> None:
        """Caller invokes when the API returns a rate-limit error.

        Advances the step-down sliding-window counter and may trigger a
        :class:`StateTransition` if ``step_down_hits`` hits land inside
        the ``step_down_window``.
        """
        with self._lock:
            now = self._clock_fn()
            self._last_hit_at = now
            self._hit_timestamps.append(now)
            self._prune_hits(now)
            if len(self._hit_timestamps) >= self.config.step_down_hits:
                self._step_down(now, reason="step_down_hits")

    def record_clean_call(self) -> None:
        """Caller invokes on every non-rate-limited success.

        If enough wall time has passed since the last rate-limit hit
        (or since limiter start), the limiter steps up by 1 r/s up to
        the ceiling. The check is O(1) — no per-call allocation.
        """
        with self._lock:
            now = self._clock_fn()
            self._maybe_step_up(now)

    def tick(self) -> None:
        """Periodic check — call from a scheduler at ``recheck_interval``.

        Useful in low-traffic conditions where ``record_clean_call`` may
        not fire often enough to drive step-up cadence. The method is
        a no-op when called more often than ``recheck_interval``.
        """
        with self._lock:
            now = self._clock_fn()
            self._maybe_step_up(now)

    def snapshot(self) -> dict[str, object]:
        """Return a serialisable view of the limiter's state.

        Used by the dashboard + audit-log replay tests to assert on
        limiter state without touching private fields.
        """
        with self._lock:
            return {
                "state": self._state.value,
                "current_rps": self._current_rps,
                "min_rps": self.config.min_rps,
                "max_rps": self.config.max_rps,
                "recent_hit_count": len(self._hit_timestamps),
                "last_hit_at": self._last_hit_at,
            }

    # ────────────────────────────────────────────────────────────────────
    # Internals
    # ────────────────────────────────────────────────────────────────────

    def _prune_hits(self, now: float) -> None:
        """Drop hit timestamps older than ``step_down_window``."""
        cutoff = now - self.config.step_down_window
        while self._hit_timestamps and self._hit_timestamps[0] < cutoff:
            self._hit_timestamps.popleft()

    def _step_down(self, now: float, *, reason: str) -> None:
        """Drop ``current_rps`` by 1 r/s; clear the hit buffer.

        Transitions:
        * PROBING_UP / LOCKED → PROBING_DOWN
        * PROBING_DOWN → PROBING_DOWN (stays; rps drops by 1)
        * After drop, if rps < min_rps → FLOOR_BREACHED.

        The hit buffer is cleared so the next step-down requires a
        fresh ``step_down_hits`` count (otherwise a single noisy burst
        could collapse the rate by multiple steps in one tick).
        """
        if self._state is RateLimiterState.FLOOR_BREACHED:
            # Terminal state — operator intervention required; no more
            # automatic transitions until the limiter is rebuilt.
            return

        from_state = self._state
        from_rps = self._current_rps
        new_rps = self._current_rps - 1.0

        if new_rps < self.config.min_rps:
            # Floor breach — emit CRITICAL transition and pin to min_rps.
            self._current_rps = self.config.min_rps
            self._state = RateLimiterState.FLOOR_BREACHED
            self._emit_transition(
                from_state=from_state,
                from_rps=from_rps,
                to_rps=self._current_rps,
                timestamp=now,
                reason="floor_breach",
            )
            return

        self._current_rps = new_rps
        self._state = RateLimiterState.PROBING_DOWN
        self._hit_timestamps.clear()
        self._last_step_up_check_at = now  # reset step-up clock too
        self._emit_transition(
            from_state=from_state,
            from_rps=from_rps,
            to_rps=self._current_rps,
            timestamp=now,
            reason=reason,
        )

    def _maybe_step_up(self, now: float) -> None:
        """Step up by 1 r/s if enough clean time has elapsed."""
        if self._state is RateLimiterState.FLOOR_BREACHED:
            # Terminal — never automatically recover.
            return
        if self._state is RateLimiterState.LOCKED:
            # Already at ceiling — nothing to do until a hit forces step-down.
            return

        elapsed = now - self._last_step_up_check_at
        if elapsed < self.config.step_up_clean_window:
            return  # Not enough clean time yet.

        # Drop hit timestamps older than the step-down window before
        # checking "have we been clean?". Without this, a single stale
        # hit sitting in the buffer would block step-up forever.
        self._prune_hits(now)

        if self._hit_timestamps:
            # Hits within the step-down window mean we haven't been clean —
            # reset the step-up clock but don't change the rate.
            self._last_step_up_check_at = now
            return

        from_state = self._state
        from_rps = self._current_rps
        new_rps = self._current_rps + 1.0

        if new_rps >= self.config.max_rps:
            self._current_rps = self.config.max_rps
            self._state = RateLimiterState.LOCKED
            reason = "ceiling_reached"
        else:
            self._current_rps = new_rps
            self._state = RateLimiterState.PROBING_UP
            reason = "step_up_clean_window"

        self._last_step_up_check_at = now
        self._emit_transition(
            from_state=from_state,
            from_rps=from_rps,
            to_rps=self._current_rps,
            timestamp=now,
            reason=reason,
        )

    def _emit_transition(
        self,
        *,
        from_state: RateLimiterState,
        from_rps: float,
        to_rps: float,
        timestamp: float,
        reason: str,
    ) -> None:
        """Fire ``on_transition`` if registered.

        Does NOT fire when from_state == to_state AND from_rps == to_rps
        (i.e., a no-op). All real state changes (including same-state
        rps drops like PROBING_DOWN → PROBING_DOWN with rps -1) fire.
        """
        if self.on_transition is None:
            return
        if from_state is self._state and from_rps == to_rps:
            return
        self.on_transition(
            StateTransition(
                from_state=from_state,
                to_state=self._state,
                from_rps=from_rps,
                to_rps=to_rps,
                timestamp=timestamp,
                reason=reason,
            )
        )
