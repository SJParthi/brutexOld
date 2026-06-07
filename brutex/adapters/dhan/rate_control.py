"""Adaptive Dhan rate controller — start at 5 r/s, step 5→4→3 under heavy 429s.

The Dhan Data API ceiling is **5 req/s** (verified, ``docs/dhan-ref/01``). This
controller (DEC-DHAN-ADAPTIVE-RPS-001) runs the pull at the **max 5 r/s** when
Dhan is healthy and only **steps down 1 r/s** (5 → 4 → 3) under *sustained*
rate-limiting, recovering 1 r/s at a time (3 → 4 → 5) once the pressure clears.
It never exceeds **5** (the hard ceiling). The 429 ladder never drops below **3**
(:data:`FLOOR_RPS` — clamped, not a halt); a SUSTAINED *timeout* storm on a heavy
/ flaky endpoint eases FURTHER, to :data:`TIMEOUT_FLOOR_RPS` (1), so a struggling
vendor can recover (DEC-DHAN-OPTION-TIMEOUT-STORM-001) — then recovers up to the
ceiling. Maximises throughput while staying inside Dhan's limit (CLAUDE.md §2
Rule 6).

It owns only the small Dhan-specific step ladder; the **O(1) blocking pace** +
the daily-quota gate are reused from
:class:`~brutex.orchestrator.rate_limiter.AdaptiveTokenBucket` (CLAUDE.md §11
COMMON — no re-implementation of the token math). On a (rare) step the ONE
persistent bucket's rate is changed IN PLACE via
:meth:`~brutex.orchestrator.rate_limiter.AdaptiveTokenBucket.set_rate` (never
swapped for a fresh object), so there is only ever a SINGLE shared token stream
— in-flight ``pace`` callers and new callers all pace the same bucket, keeping
the aggregate physical rate strictly ≤ the current rate even across a 5→4→3
cascade (DEC-DHAN-RATE-SINGLE-BUCKET-001 — closes the dual-bucket
``rate.breach rps_1s=11`` opening transient). The same
:class:`DailyQuotaTracker` is retained throughout so the day's count is never
reset.

Wiring (next installment): ``pace`` → the ingest ``pace=`` hook;
``on_rate_limit`` ← a 429 (``DhanRateLimitError``) in the fetch loop;
``on_success`` ← a clean window. The hooks are O(1) (a bounded deque + a few
comparisons).
"""

from __future__ import annotations

import threading
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

#: The DEEPER floor the **timeout** ladder alone may reach under a SUSTAINED
#: server-stress storm (DEC-DHAN-OPTION-TIMEOUT-STORM-001, operator 2026-06-05).
#: The 429 ladder still clamps at :data:`FLOOR_RPS` (3) — a quota breach is no
#: reason to crawl — but a relentless TIMEOUT cascade is different: the operator's
#: 50-stock options ``charts/rollingoption`` pull floored at 3 r/s yet kept
#: ``latency_ms_max`` pinned at the 10s client timeout with ``recent_timeouts``
#: saturated for 13+ min, because the timeout ladder had nowhere lower to ease.
#: A struggling endpoint needs MORE room: the timeout ladder now steps
#: 5→4→3→2→1; :meth:`on_success` recovers it back up to the ceiling once the storm
#: clears, exactly as before. ``rollingoption`` is heavier + flakier than the spot
#: ``intraday`` endpoint, so this deeper floor is the server-stress relief valve.
TIMEOUT_FLOOR_RPS: float = 1.0

#: One ladder rung.
STEP_RPS: float = 1.0

#: "Heavy" = this many rate-limit hits inside :data:`STEP_DOWN_WINDOW` seconds.
STEP_DOWN_HITS: int = 3
STEP_DOWN_WINDOW: float = 60.0

#: "Heavy" TIMEOUTS = this many server-stress stalls (timeout / transport / 5xx —
#: a SLOW, struggling Dhan, NOT a quota breach) inside :data:`STEP_DOWN_WINDOW`
#: seconds → step DOWN 1 r/s (DEC-DHAN-TIMEOUT-BACKOFF-001, gentle adaptive
#: step-down chosen by the operator 2026-06-05). Deliberately MORE tolerant than
#: the 429 :data:`STEP_DOWN_HITS` (3): a timeout is softer evidence than an
#: explicit 429, and a one-off stall already self-heals via the client's bounded
#: fast-retry — so a single timeout must NOT cost throughput. Only a *sustained*
#: storm (the `latency_ms_max → 10s` + `DhanTimeoutError` cascade the operator's
#: long-pull logs caught) eases the rate; it then recovers 3→4→5 via
#: :meth:`on_success` once the storm clears, exactly like the 429 ladder.
STEP_DOWN_TIMEOUT_HITS: int = 5

#: "Recovered" = this many seconds of zero hits since the last step → step up 1 r/s.
STEP_UP_WINDOW: float = 300.0

#: Token-bucket burst allowance. EASE-IN (DEC-DHAN-EASE-IN-001): **1**, not 5, so
#: a pull does NOT fire a multi-request spike at t=0. With burst 5 the bucket
#: started FULL, so the parallel window fan-out dumped 5 requests instantly →
#: an instantaneous rate far above Dhan's strict 5 r/s → the opening 429 storm
#: the operator saw on every pull (3× ``rate.429`` → ``rate.stepdown`` within the
#: first second). With burst 1 each worker paces from the first call at the
#: current r/s. Steady-state throughput is UNCHANGED (still ``current_rps`` — one
#: token per ``1/rps`` s); only the opening + post-step spike is removed. A 429
#: that still slips through self-heals in-run (DEC-DHAN-429-RETRY-001, #466).
BURST: int = 1


class DhanRateController:
    """Adaptive [1..5] r/s controller: start 5, step down on heavy 429s, recover up.

    Steady-state envelope is [3..5] (the 429 ladder floors at :data:`FLOOR_RPS`);
    a SUSTAINED timeout storm eases further to :data:`TIMEOUT_FLOOR_RPS` (1), so
    the full range is [1..5] (DEC-DHAN-OPTION-TIMEOUT-STORM-001).

    Thread-safety (DEC-DHAN-PARALLEL-INGEST-001). The controller is the shared
    GOVERNOR for the parallelised ingest fan-out: one instance is threaded
    through every ``ThreadPoolExecutor`` worker, so :meth:`pace`,
    :meth:`on_rate_limit`, :meth:`on_success` and :meth:`snapshot` are now
    called CONCURRENTLY. A :class:`threading.Lock` guards the small step-ladder
    state (``_rps`` / ``_hits`` / ``_last_step`` / ``_bucket``) so a step DOWN /
    UP — which changes the ONE persistent ``_bucket``'s rate IN PLACE via
    :meth:`~brutex.orchestrator.rate_limiter.AdaptiveTokenBucket.set_rate` (never
    a swap) — is atomic and never races a concurrent ``pace``. Each critical
    section is **O(1)** (a few comparisons + a bounded deque) and the lock is
    NEVER held across network I/O or a sleep. In particular :meth:`pace` copies
    the bucket reference under the lock then calls ``.acquire()`` (which sleeps)
    OUTSIDE it — holding the lock across the bucket sleep would re-serialise every
    worker to ~1 r/s and defeat the whole parallelisation. Because the bucket
    object is NEVER replaced (only its rate mutates), every worker — in-flight or
    new — paces the SAME object, so there is exactly one ≤ ``current_rps`` token
    stream at all times (DEC-DHAN-RATE-SINGLE-BUCKET-001; this closes the
    dual-bucket transient where a per-step REBUILD let old- and new-bucket
    workers stack POSTs into the trailing-1s window → ``rate.breach rps_1s=11``).
    The reused :class:`AdaptiveTokenBucket` has its own lock, so the bucket's own
    token math stays the shared rate gate (Rule #6) across all workers.
    """

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
        #: Server-stress stalls (timeout / transport / 5xx) inside the rolling
        #: STEP_DOWN_WINDOW — the gentle adaptive timeout ladder's evidence
        #: buffer (DEC-DHAN-TIMEOUT-BACKOFF-001), kept separate from the 429
        #: ``_hits`` so the two signals never cross-trigger each other's ladder.
        self._timeouts: deque[float] = deque()
        self._last_step: float = clock()
        self._bucket: AdaptiveTokenBucket = self._make_bucket()
        #: Guards the step-ladder state under concurrent worker access
        #: (DEC-DHAN-PARALLEL-INGEST-001). Held only for O(1) state reads/writes,
        #: NEVER across the bucket ``.acquire()`` sleep in :meth:`pace`.
        self._lock = threading.Lock()

    def _make_bucket(self) -> AdaptiveTokenBucket:
        """Build the ONE persistent token bucket at the current rate (shares the quota tracker).

        Called ONCE at construction. Every subsequent rate step mutates THIS
        object in place via :meth:`AdaptiveTokenBucket.set_rate` rather than
        rebuilding — see the class docstring (DEC-DHAN-RATE-SINGLE-BUCKET-001).
        """
        return AdaptiveTokenBucket(
            initial_rps=self._rps,
            downshift_rps=self._rps / 2.0,  # unused (we never feed the bucket hits); just < initial
            burst=BURST,
            # HARD-CAP (DEC-DHAN-RATE-HARD-CAP-001): the bucket starts EMPTY, so
            # the opening call cannot release a free burst token — it waits a full
            # ``1/rate`` interval. ``set_rate`` likewise drains to empty on each
            # step, so a 5→4→3 stepdown can't release a stale higher-rate token
            # either. The PHYSICAL issue rate therefore stays strictly ≤ the
            # current rate (≤ 5 r/s at the ceiling) at all times.
            start_empty=True,
            clock=self._clock,
            sleep=self._sleep,
            quota_tracker=self._quota,
        )

    @property
    def current_rps(self) -> float:
        """The current rate (in ``[TIMEOUT_FLOOR_RPS, CEILING_RPS]``) — thread-safe."""
        with self._lock:
            return self._rps

    def pace(self) -> None:
        """Block until a token is available at the current rate — **O(1)** fast path.

        CRITICAL (DEC-DHAN-PARALLEL-INGEST-001): the controller lock is taken
        ONLY to copy the current ``_bucket`` reference into a local, then
        RELEASED before ``local.acquire()`` (which may sleep until a token frees
        up). Holding ``self._lock`` across that sleep would serialise every
        worker through a single mutex and collapse the achieved rate back to
        ~1 r/s — exactly the bug this change removes. The bucket itself is
        thread-safe (its own lock is the shared rate gate, Rule #6), so N workers
        acquiring on the SAME bucket object correctly share one ≤ ``current_rps``
        token stream. A concurrent step changes that one bucket's rate IN PLACE
        (:meth:`AdaptiveTokenBucket.set_rate`) — the object reference never
        changes, so a worker that copied the bucket simply paces its next call at
        whatever rate the step set, still on the single shared stream (no
        dual-bucket over-issue; DEC-DHAN-RATE-SINGLE-BUCKET-001).
        """
        with self._lock:
            bucket = self._bucket
        bucket.acquire()

    def on_rate_limit(self) -> None:
        """Record a rate-limit hit; step **down** 1 r/s on sustained pressure — O(1).

        Thread-safe: the whole hit-record → window-evict → maybe-step-down is one
        atomic O(1) critical section, so two workers reporting a 429 at once can
        never double-step or corrupt the deque. The ``set_rate`` mutation happens
        under the lock but only re-stamps the bucket's in-memory rate/token state
        (no I/O / sleep), keeping the single shared token stream intact.
        """
        with self._lock:
            now = self._clock()
            self._hits.append(now)
            cutoff = now - STEP_DOWN_WINDOW
            while self._hits and self._hits[0] < cutoff:
                self._hits.popleft()
            _LOG.warning("rate.429", current_rps=self._rps, recent_hits=len(self._hits))
            if len(self._hits) >= STEP_DOWN_HITS and self._rps > FLOOR_RPS:
                old_rps = self._rps
                self._rps -= STEP_RPS
                self._bucket.set_rate(self._rps)
                self._hits.clear()
                self._last_step = now
                _LOG.warning(
                    "rate.stepdown", from_rps=old_rps, to_rps=self._rps, floor_rps=FLOOR_RPS
                )

    def on_timeout(self) -> None:
        """Record a server-stress stall; step **down** 1 r/s on a sustained storm — O(1).

        The gentle adaptive timeout ladder (DEC-DHAN-TIMEOUT-BACKOFF-001 +
        DEC-DHAN-OPTION-TIMEOUT-STORM-001): a long pull that starts TIMING OUT (a
        slow, struggling Dhan — not a quota breach) eases 5→4→3→2→1 r/s once
        :data:`STEP_DOWN_TIMEOUT_HITS` stalls land inside :data:`STEP_DOWN_WINDOW`
        seconds, then recovers via :meth:`on_success` once the storm clears. A
        one-off timeout is ignored (it self-heals via the client's bounded
        fast-retry) so steady-state throughput is UNCHANGED — only a cascade eases
        the rate. Mirrors :meth:`on_rate_limit`'s ladder but reads the SEPARATE,
        more tolerant ``_timeouts`` buffer (so a 429 and a timeout never
        cross-count) AND eases to the DEEPER :data:`TIMEOUT_FLOOR_RPS` (1) rather
        than the 429 :data:`FLOOR_RPS` (3): a relentless server-stress storm on a
        heavy / flaky endpoint (e.g. ``charts/rollingoption``) needs more room to
        let the vendor recover than a mere quota breach does.

        Thread-safe: the record → window-evict → maybe-step-down is one atomic
        O(1) critical section, so concurrent workers reporting a stall can never
        double-step. The ``set_rate`` mutation only re-stamps the one bucket's
        in-memory rate/token state (no I/O / sleep under the lock).
        """
        with self._lock:
            now = self._clock()
            self._timeouts.append(now)
            cutoff = now - STEP_DOWN_WINDOW
            while self._timeouts and self._timeouts[0] < cutoff:
                self._timeouts.popleft()
            _LOG.warning("rate.timeout", current_rps=self._rps, recent_timeouts=len(self._timeouts))
            if len(self._timeouts) >= STEP_DOWN_TIMEOUT_HITS and self._rps > TIMEOUT_FLOOR_RPS:
                old_rps = self._rps
                self._rps -= STEP_RPS
                self._bucket.set_rate(self._rps)
                self._timeouts.clear()
                self._last_step = now
                _LOG.warning(
                    "rate.stepdown",
                    from_rps=old_rps,
                    to_rps=self._rps,
                    floor_rps=TIMEOUT_FLOOR_RPS,
                    cause="timeout",
                )

    def on_success(self) -> None:
        """Record a clean call; step **up** 1 r/s after a sustained clean window — O(1).

        Thread-safe: the recover-up check + the ``set_rate`` mutation are one
        atomic O(1) critical section (no I/O / sleep under the lock). Stale
        timeouts (older than :data:`STEP_DOWN_WINDOW`) are evicted here too, so a
        timeout storm that never reached the step-down threshold cannot block
        recovery forever once it has aged out (DEC-DHAN-TIMEOUT-BACKOFF-001).
        """
        with self._lock:
            now = self._clock()
            cutoff = now - STEP_DOWN_WINDOW
            while self._timeouts and self._timeouts[0] < cutoff:
                self._timeouts.popleft()
            if (
                self._rps < CEILING_RPS
                and not self._hits
                and not self._timeouts
                and now - self._last_step >= STEP_UP_WINDOW
            ):
                old_rps = self._rps
                self._rps += STEP_RPS
                self._bucket.set_rate(self._rps)
                self._last_step = now
                _LOG.info(
                    "rate.stepup", from_rps=old_rps, to_rps=self._rps, ceiling_rps=CEILING_RPS
                )

    def snapshot(self) -> dict[str, object]:
        """A JSON-friendly state snapshot for the dashboard rate panel (thread-safe, O(1))."""
        with self._lock:
            return {
                "current_rps": self._rps,
                "recent_hits": len(self._hits),
                "recent_timeouts": len(self._timeouts),
            }


__all__ = [
    "BURST",
    "CEILING_RPS",
    "FLOOR_RPS",
    "STEP_DOWN_HITS",
    "STEP_DOWN_TIMEOUT_HITS",
    "STEP_DOWN_WINDOW",
    "STEP_RPS",
    "STEP_UP_WINDOW",
    "TIMEOUT_FLOOR_RPS",
    "DhanRateController",
]
