"""Adaptive Dhan rate controller — start at 5 r/s, step 5→4→3 under heavy 429s.

The Dhan Data API ceiling is **5 req/s** (verified, ``docs/dhan-ref/01``). This
controller (DEC-DHAN-ADAPTIVE-RPS-001) runs the pull at the **max 5 r/s** when
Dhan is healthy and only **steps down 1 r/s** (5 → 4 → 3) under *sustained*
rate-limiting, recovering 1 r/s at a time (3 → 4 → 5) once the pressure clears.
It never exceeds **5** (the hard ceiling) and never drops below **3** (the floor
— clamped, not a halt), maximising throughput while staying inside Dhan's limit
(CLAUDE.md §2 Rule 6).

It owns only the small Dhan-specific step ladder; the **O(1) blocking pace** +
the daily-quota gate are reused from
:class:`~brutex.orchestrator.rate_limiter.AdaptiveTokenBucket` (CLAUDE.md §11
COMMON — no re-implementation of the token math). On a (rare) step the bucket is
rebuilt at the new rate, sharing the same :class:`DailyQuotaTracker` so the
day's count is never reset.

Wiring (next installment): ``pace`` → the ingest ``pace=`` hook;
``on_rate_limit`` ← a 429 (``DhanRateLimitError``) in the fetch loop;
``on_success`` ← a clean window. The hooks are O(1) (a bounded deque + a few
comparisons).
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable

import structlog

from brutex.adapters.dhan.constants import DATA_RATE_LIMIT_RPS
from brutex.orchestrator.quota_tracker import DailyQuotaTracker
from brutex.orchestrator.rate_limiter import AdaptiveTokenBucket

#: Structured logger — surfaces the otherwise-invisible throttle: a 429
#: (``rate.429``), a step DOWN under sustained pressure (``rate.stepdown``), and
#: the recovery back up (``rate.stepup``) into the ``/logs`` stream so the
#: operator SEES exactly when/why effective r/s dropped (DEC-DHAN-RATE-LOGGING-001).
_LOG = structlog.get_logger("brutex.adapters.dhan.rate_control")

#: Dhan's verified Data-API ceiling — the hard cap we never exceed (and start at).
CEILING_RPS: float = float(DATA_RATE_LIMIT_RPS)

#: The floor we clamp to under sustained pressure (never below — and never a halt).
FLOOR_RPS: float = 3.0

#: One ladder rung.
STEP_RPS: float = 1.0

#: "Heavy" = this many rate-limit hits inside :data:`STEP_DOWN_WINDOW` seconds.
STEP_DOWN_HITS: int = 3
STEP_DOWN_WINDOW: float = 60.0

#: "Recovered" = this many seconds of zero hits since the last step → step up 1 r/s.
STEP_UP_WINDOW: float = 300.0

#: Token-bucket burst allowance (short cadence absorption).
BURST: int = 5


class DhanRateController:
    """Adaptive [3..5] r/s controller: start 5, step down on heavy 429s, recover up."""

    def __init__(
        self,
        *,
        quota_tracker: DailyQuotaTracker | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Build a controller starting at the ceiling rate (5 r/s).

        Parameters
        ----------
        quota_tracker
            The shared daily-quota gate (production MUST pass one; ``None`` is a
            test-only "bucket-only" mode). Persists across rate steps.
        clock, sleep
            Injected for deterministic testing (default the real monotonic clock
            + sleep). Threaded into the reused token bucket.
        """
        self._clock = clock
        self._sleep = sleep
        self._quota = quota_tracker
        self._rps: float = CEILING_RPS
        self._hits: deque[float] = deque()
        self._last_step: float = clock()
        self._bucket: AdaptiveTokenBucket = self._make_bucket()

    def _make_bucket(self) -> AdaptiveTokenBucket:
        """Build a token bucket pacing at the current rate (shares the quota tracker)."""
        return AdaptiveTokenBucket(
            initial_rps=self._rps,
            downshift_rps=self._rps / 2.0,  # unused (we never feed the bucket hits); just < initial
            burst=BURST,
            clock=self._clock,
            sleep=self._sleep,
            quota_tracker=self._quota,
        )

    @property
    def current_rps(self) -> float:
        """The current steady-state rate (in ``[FLOOR_RPS, CEILING_RPS]``)."""
        return self._rps

    def pace(self) -> None:
        """Block until a token is available at the current rate — **O(1)** fast path."""
        self._bucket.acquire()

    def on_rate_limit(self) -> None:
        """Record a rate-limit hit; step **down** 1 r/s on sustained pressure — O(1)."""
        now = self._clock()
        self._hits.append(now)
        cutoff = now - STEP_DOWN_WINDOW
        while self._hits and self._hits[0] < cutoff:
            self._hits.popleft()
        _LOG.warning("rate.429", current_rps=self._rps, recent_hits=len(self._hits))
        if len(self._hits) >= STEP_DOWN_HITS and self._rps > FLOOR_RPS:
            old_rps = self._rps
            self._rps -= STEP_RPS
            self._bucket = self._make_bucket()
            self._hits.clear()
            self._last_step = now
            _LOG.warning("rate.stepdown", from_rps=old_rps, to_rps=self._rps, floor_rps=FLOOR_RPS)

    def on_success(self) -> None:
        """Record a clean call; step **up** 1 r/s after a sustained clean window — O(1)."""
        now = self._clock()
        if self._rps < CEILING_RPS and not self._hits and now - self._last_step >= STEP_UP_WINDOW:
            old_rps = self._rps
            self._rps += STEP_RPS
            self._bucket = self._make_bucket()
            self._last_step = now
            _LOG.info("rate.stepup", from_rps=old_rps, to_rps=self._rps, ceiling_rps=CEILING_RPS)

    def snapshot(self) -> dict[str, object]:
        """A JSON-friendly state snapshot for the dashboard rate panel."""
        return {"current_rps": self._rps, "recent_hits": len(self._hits)}


__all__ = [
    "BURST",
    "CEILING_RPS",
    "FLOOR_RPS",
    "STEP_DOWN_HITS",
    "STEP_DOWN_WINDOW",
    "STEP_RPS",
    "STEP_UP_WINDOW",
    "DhanRateController",
]
