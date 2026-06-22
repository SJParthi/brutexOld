"""Adaptive rate controller — start at the ceiling, step down under heavy 429s.

Vendor-neutral home (PR-J4a of the Groww cutover — CLAUDE.md §11 COMMON,
one source of truth, no copy-paste between modules). The controller
(:class:`RateController`, DEC-DHAN-ADAPTIVE-RPS-001) runs a pull at the **ceiling
rate** (injected per vendor; the verified data-API ceiling is **5 req/s**
— :data:`CEILING_RPS`) when the vendor is healthy and only
**steps down 1 r/s** under *sustained* rate-limiting, recovering 1 r/s at a time
once the pressure clears. It never exceeds the ceiling. The 429 ladder reaches the SOFT
:data:`FLOOR_RPS` (**3**) quickly on a brief burst (a quota nudge is no reason to
crawl), and under a SUSTAINED 429 storm — every call rejected even at 3 r/s — eases
FURTHER to :data:`RATE_LIMIT_STORM_FLOOR_RPS` (**1**) on the higher
:data:`STORM_HITS` evidence threshold (DEC-DHAN-RATE-STORM-FLOOR-001), exactly as a
SUSTAINED *timeout* storm on a heavy / flaky endpoint already eases to
:data:`TIMEOUT_FLOOR_RPS` (1) (DEC-DHAN-OPTION-TIMEOUT-STORM-001) — then recovers up
to the ceiling once the pressure clears. Maximises throughput while staying inside the
vendor's limit (CLAUDE.md §2 Rule 6).

It owns only the small step ladder; the **O(1) blocking pace** +
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
``on_rate_limit`` ← a 429 (``GrowwRateLimitError``) in the fetch loop;
``on_success`` ← a clean window. The hooks are O(1) (a bounded deque + a few
comparisons).
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable

import structlog

from brutex.orchestrator.quota_tracker import DailyQuotaTracker
from brutex.orchestrator.rate_limiter import AdaptiveTokenBucket

#: Structured logger — surfaces the otherwise-invisible throttle: a 429
#: (``rate.429``), a step DOWN under sustained pressure (``rate.stepdown``), and
#: the recovery back up (``rate.stepup``) into the ``/logs`` stream so the
#: operator SEES exactly when/why effective r/s dropped (DEC-DHAN-RATE-LOGGING-001).
_LOG = structlog.get_logger("brutex.adapters.rate_common")

#: The verified data-API ceiling (5 r/s), kept here as the DEFAULT ceiling so this
#: vendor-neutral controller needs NO vendor import — the width policy is owned
#: locally and mirrors the verified 5 r/s cap. A different vendor injects its own
#: ``ceiling_rps`` (e.g. the Groww cutover, PR-J2).
CEILING_RPS: float = 5.0

#: The SOFT floor the 429 ladder reaches quickly on a brief burst (a quota nudge is
#: no reason to crawl). A SUSTAINED 429 storm eases BELOW it toward
#: :data:`RATE_LIMIT_STORM_FLOOR_RPS` on the higher :data:`STORM_HITS` threshold
#: (DEC-DHAN-RATE-STORM-FLOOR-001) — never a halt.
FLOOR_RPS: float = 3.0

#: The DEEPER floor the **timeout** ladder alone may reach under a SUSTAINED
#: server-stress storm (DEC-DHAN-OPTION-TIMEOUT-STORM-001, operator 2026-06-05).
#: A BRIEF 429 burst clamps at :data:`FLOOR_RPS` (3) — a quota nudge is no reason to
#: crawl — but a SUSTAINED 429 storm (DEC-DHAN-RATE-STORM-FLOOR-001) AND a relentless
#: TIMEOUT cascade both need to ease deeper: the operator's
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

#: Rolling window (seconds) for the optional per-minute governor — a HARD ceiling on
#: the number of requests ISSUED inside any trailing 60 s, orthogonal to the AIMD
#: per-second ladder (DEC-GROWW-RATE-PERMIN-001). Mirrors :data:`STEP_DOWN_WINDOW`'s
#: 60 s eviction idiom but governs an issue-count cap, not the step ladder. The cap
#: itself is INJECTED per vendor (``ceiling_per_min``); this controller stays free of
#: vendor numbers (mirrors :data:`CEILING_RPS`'s injected-width policy).
PER_MIN_WINDOW: float = 60.0

#: The DEEPER floor the **429** ladder may reach under a SUSTAINED rate-limit storm
#: (DEC-DHAN-RATE-STORM-FLOOR-001). The 429 ladder reaches the soft :data:`FLOOR_RPS`
#: (3) quickly on a brief burst — a mere quota nudge is no reason to crawl — but a
#: RELENTLESS 429 cascade where EVERY call is rejected even AT 3 r/s proves 3 is
#: still too fast (the operator's 90-min log: ``current_rps=3.0`` with ``recent_hits``
#: pinned at ~179 for 3.5 min). Like the timeout ladder it then needs room to ease to
#: :data:`TIMEOUT_FLOOR_RPS` (1) so the vendor can recover; :meth:`on_success` raises
#: it back to the ceiling once the storm clears. Easing below the soft floor requires
#: SUSTAINED evidence (:data:`STORM_HITS`), so a transient burst never crawls.
RATE_LIMIT_STORM_FLOOR_RPS: float = TIMEOUT_FLOOR_RPS

#: Sustained-storm evidence: this many 429 hits inside :data:`STEP_DOWN_WINDOW` while
#: ALREADY at (or below) the soft :data:`FLOOR_RPS` → ease ONE rung toward
#: :data:`RATE_LIMIT_STORM_FLOOR_RPS`. At 3 r/s this is ~10 s of 100%-rejection — an
#: unmistakable storm — vs the 3-hit :data:`STEP_DOWN_HITS` that reaches the soft floor
#: from above. Deliberately high so only a genuine cascade (not a brief quota nudge)
#: drops below 3 (DEC-DHAN-RATE-STORM-FLOOR-001).
STORM_HITS: int = 30

#: "Heavy" TIMEOUTS = this many server-stress stalls (timeout / transport / 5xx —
#: a SLOW, struggling vendor, NOT a quota breach) inside :data:`STEP_DOWN_WINDOW`
#: seconds → step DOWN 1 r/s (DEC-DHAN-TIMEOUT-BACKOFF-001, gentle adaptive
#: step-down chosen by the operator 2026-06-05). Deliberately MORE tolerant than
#: the 429 :data:`STEP_DOWN_HITS` (3): a timeout is softer evidence than an
#: explicit 429, and a one-off stall already self-heals via the client's bounded
#: fast-retry — so a single timeout must NOT cost throughput. Only a *sustained*
#: storm (the `latency_ms_max → 10s` + `GrowwTimeoutError` cascade the operator's
#: long-pull logs caught) eases the rate; it then recovers 3→4→5 via
#: :meth:`on_success` once the storm clears, exactly like the 429 ladder.
STEP_DOWN_TIMEOUT_HITS: int = 5

#: "Recovered" = this many seconds without step-down-level pressure since the last step
#: → step UP 1 r/s (the INCREMENTAL half of the AIMD ladder). Set to ONE
#: :data:`STEP_DOWN_WINDOW` (60 s) — symmetric with the rolling window the step-DOWN
#: ladder watches: after a single clean 60 s window the controller probes up one rung,
#: so it climbs the soft floor (3) back to the operator-DIRECTED 5 r/s ceiling
#: (``GROWW_RATE_LIMIT_RPS_CEILING``) in ~2 clean windows (~2 min) instead of the prior
#: 300 s/rung (~25 min — longer than a whole pull, so a stepped-down pull never reached
#: the ceiling; the operator-observed "stuck at ~3 r/s with 2.4x headroom",
#: DEC-GROWW-RATE-RECOVER-8-001). SAFETY is unchanged: the DECREMENTAL ladder still steps
#: DOWN immediately on a 429 / timeout storm, so a too-eager probe self-corrects (AIMD) —
#: faster recovery can only REACH the sustainable rate sooner, never exceed it (the rate.*
#: + the new rate.snapshot telemetry make the real trajectory visible, DEC-GROWW-RATE-
#: SNAPSHOT-LOG-001). The soft/storm floors + every step-DOWN trigger are untouched.
STEP_UP_WINDOW: float = STEP_DOWN_WINDOW

#: Token-bucket burst allowance. EASE-IN (DEC-DHAN-EASE-IN-001): **1**, not 5, so
#: a pull does NOT fire a multi-request spike at t=0. With burst 5 the bucket
#: started FULL, so the parallel window fan-out dumped 5 requests instantly →
#: an instantaneous rate far above the strict 5 r/s cap → the opening 429 storm
#: the operator saw on every pull (3× ``rate.429`` → ``rate.stepdown`` within the
#: first second). With burst 1 each worker paces from the first call at the
#: current r/s. Steady-state throughput is UNCHANGED (still ``current_rps`` — one
#: token per ``1/rps`` s); only the opening + post-step spike is removed. A 429
#: that still slips through self-heals in-run (DEC-DHAN-429-RETRY-001, #466).
BURST: int = 1

#: Safety cap on the per-minute governor's re-check loop (DEC-GROWW-RATE-PERMIN-001).
#: Mirrors :class:`~brutex.orchestrator.rate_limiter.AdaptiveTokenBucket`'s acquire
#: loop cap: under contention a worker may wake to find its freed slot already taken
#: by a peer, so it must re-evict + re-test. This bounds that re-contend loop so a
#: pathological INJECTED clock (one that never advances) can't spin forever — after
#: the cap the governor fails OPEN (lets the call through) exactly as the bucket's
#: acquire cap forces a token, keeping behaviour consistent (CLAUDE.md §11 COMMON).
_GOVERNOR_LOOP_CAP: int = 64

#: Minimum seconds between periodic ``rate.snapshot`` telemetry logs during active
#: pacing (DEC-GROWW-RATE-SNAPSHOT-LOG-001). The existing rate.* events fire ONLY on a
#: rung CHANGE (rate.429 / rate.stepdown / rate.stepup), so a pull HOLDING at a
#: stepped-down rate (e.g. the soft floor) logs NOTHING and /logs cannot show WHY the
#: achieved r/s sits below the ceiling. This periodic snapshot fills that visibility gap
#: — 15 s is frequent enough to see the rate's trajectory across a multi-minute pull yet
#: coarse enough (~one line / 15 s) to never flood /logs. Observability only; the
#: throttle is O(1) and changes NO pacing decision.
SNAPSHOT_LOG_INTERVAL_S: float = 15.0


class RateController:
    """Adaptive [1..5] r/s controller: start 5, step down on heavy 429s, recover up.

    Steady-state envelope is [3..5] (the 429 ladder reaches the soft
    :data:`FLOOR_RPS` on a brief burst); a SUSTAINED 429 storm (:data:`STORM_HITS`)
    OR a sustained timeout storm eases further to the deep floor (1), so the full
    range is [1..5] (DEC-DHAN-RATE-STORM-FLOOR-001 + DEC-DHAN-OPTION-TIMEOUT-STORM-001).

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
        ceiling_rps: float = CEILING_RPS,
        ceiling_per_min: int | None = None,
    ) -> None:
        """Build a controller starting at the ceiling rate (default 5 r/s).

        Parameters
        ----------
        quota_tracker
            The shared daily-quota gate (production MUST pass one; ``None`` is a
            test-only "bucket-only" mode). Persists across rate steps.
        clock, sleep
            Injected for deterministic testing (default the real monotonic clock
            + sleep). Threaded into the reused token bucket.
        ceiling_rps
            The start rate AND the upshift cap (default :data:`CEILING_RPS`, the
            verified 5 r/s). Injected so the SAME adaptive mechanism can pace
            a different vendor at a different ceiling WITHOUT hardcoding a verified
            rate here (the Groww vendor cutover, DEC-DHAN-GROWW-REMIGRATE-001 PR-J2,
            feeds the UNVERIFIED Groww backtesting rate). The 429/timeout ladders
            still step DOWN to their existing soft/deep floors — only the start +
            upshift cap moves.
        ceiling_per_min
            Optional HARD cap on the number of requests ISSUED inside any rolling
            :data:`PER_MIN_WINDOW` (60 s). ``None`` (the default) = NO per-minute
            governor = byte-for-byte backward-compatible (the only blocking gate
            stays the per-second token bucket). Orthogonal to the AIMD per-second
            ladder: a 429/timeout step-down lowers ``_rps`` but NEVER changes this
            cap, and a recovery step-up never raises it — it is a true fixed
            ceiling (DEC-GROWW-RATE-PERMIN-001). The Groww live path injects the
            operator-confirmed backtesting cap (500/min); at ``ceiling_rps ≤ 5``
            the governor is mathematically never the binding gate (5×60 = 300 < a
            300/500 cap), so existing 5 r/s behaviour is unchanged even when a cap
            is injected.
        """
        self._clock = clock
        self._sleep = sleep
        self._quota = quota_tracker
        self._ceiling: float = ceiling_rps
        self._rps: float = ceiling_rps
        #: Per-minute governor (DEC-GROWW-RATE-PERMIN-001): the injected HARD cap +
        #: the rolling deque of issue instants. ``None`` cap ⇒ governor disabled, the
        #: deque stays empty + ``pace`` skips both governor phases entirely.
        self._ceiling_per_min: int | None = ceiling_per_min
        self._issues: deque[float] = deque()
        self._hits: deque[float] = deque()
        #: Server-stress stalls (timeout / transport / 5xx) inside the rolling
        #: STEP_DOWN_WINDOW — the gentle adaptive timeout ladder's evidence
        #: buffer (DEC-DHAN-TIMEOUT-BACKOFF-001), kept separate from the 429
        #: ``_hits`` so the two signals never cross-COUNT (a timeout never advances
        #: the 429 ladder, nor vice-versa). NOTE they still share ``_rps`` /
        #: ``_last_step``, so a MIXED storm (429s + timeouts) compounds on the rate —
        #: each ladder steps the shared rate independently, descending faster than
        #: either alone (intended: more pressure → back off harder), and a step on
        #: EITHER ladder restamps ``_last_step``, delaying recovery for both. Both
        #: floor at 1 (DEC-DHAN-RATE-STORM-FLOOR-001), so they cannot fight below it.
        self._timeouts: deque[float] = deque()
        self._last_step: float = clock()
        #: Last ``rate.snapshot`` telemetry emit instant (DEC-GROWW-RATE-SNAPSHOT-LOG-001)
        #: — the throttle baseline so the periodic log fires at most once per
        #: :data:`SNAPSHOT_LOG_INTERVAL_S` of active pacing. Stamped ``now`` at
        #: construction so the FIRST pace does not immediately log (warm-up grace).
        self._last_snapshot_log: float = clock()
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

    def _governor_wait_locked(self, now: float) -> float:
        """Evict the rolling window; return seconds to wait before an issue may proceed.

        The caller MUST hold ``self._lock``. **O(1) amortized** (each issue instant is
        appended once + popped once; ``len`` on a deque is O(1)). Returns ``0.0`` when
        the governor is disabled (``ceiling_per_min is None``) OR the rolling
        :data:`PER_MIN_WINDOW` has room; otherwise the seconds until the OLDEST
        in-window issue ages out — ``(oldest + PER_MIN_WINDOW) - now``, clamped
        ``>= 0`` — which is the soonest a new slot can free (DEC-GROWW-RATE-PERMIN-001).
        """
        cap = self._ceiling_per_min
        if cap is None:
            return 0.0
        # Evict any issue that has reached PER_MIN_WINDOW age, using the SAME
        # ``(oldest + WINDOW) - now`` arithmetic as the wait below — so float rounding
        # at the 60 s boundary cannot make eviction and the wait disagree (which would
        # admit a cap+1 overshoot when the oldest is ~exactly 60 s old). After the loop
        # every remaining issue has a strictly-positive remaining lifetime.
        while self._issues and (self._issues[0] + PER_MIN_WINDOW) - now <= 0.0:
            self._issues.popleft()
        if len(self._issues) < cap:
            return 0.0
        # Window full: wait until the oldest reaches PER_MIN_WINDOW age (then the next
        # pass evicts it and a slot frees). Strictly > 0 here — anything <= 0 was just
        # evicted — so no clamp is needed.
        return (self._issues[0] + PER_MIN_WINDOW) - now

    def pace(self) -> None:
        """Block until both rate gates allow an issue — **O(1)** amortized fast path.

        TWO sequenced gates (DEC-GROWW-RATE-PERMIN-001):

        1. **Per-minute governor** (only when ``ceiling_per_min`` is set) — a HARD
           ceiling on the issue count inside any rolling :data:`PER_MIN_WINDOW`. It
           runs its own compute-under-lock / sleep-outside-lock / re-check loop,
           mirroring :meth:`AdaptiveTokenBucket.acquire` (CLAUDE.md §11 COMMON). When
           the window is full, N woken workers re-evict + re-test under the lock so
           only the freed slots pass — no overshoot.
        2. **Per-second token bucket** — the unchanged two-phase delegation below.

        CRITICAL (DEC-DHAN-PARALLEL-INGEST-001): the controller lock is taken ONLY
        for O(1) state work and is RELEASED before EVERY sleep (the governor sleep
        AND ``bucket.acquire()``). Holding ``self._lock`` across a sleep would
        serialise every worker through one mutex and collapse the achieved rate to
        ~1 r/s. The two gates SEQUENCE (never nest), so there is no lock-ordering
        cycle: the governor never holds ``self._lock`` when the bucket acquires its
        own lock. The issue instant is recorded only AFTER both gates clear, under
        the lock, so the deque counts true issue instants and can never exceed the
        cap. A concurrent per-second step mutates the one bucket IN PLACE
        (:meth:`AdaptiveTokenBucket.set_rate`) and never touches the governor state.
        """
        # ── Gate 1: per-minute governor (skipped entirely when disabled). ──
        if self._ceiling_per_min is not None:
            iterations = 0
            while True:
                with self._lock:
                    wait = self._governor_wait_locked(self._clock())
                    if wait <= 0.0:
                        break  # window has room — fall through to the token bucket
                iterations += 1
                if iterations > _GOVERNOR_LOOP_CAP:
                    break  # fail-OPEN after the cap (mirrors the bucket's acquire cap)
                # lock RELEASED before sleeping; re-evict + re-test on the next pass.
                self._sleep(wait)
        # ── Gate 2: per-second token bucket (two-phase: copy ref under lock, sleep
        #    outside it). ──
        with self._lock:
            bucket = self._bucket
        bucket.acquire()
        # ── Record the issue instant AFTER both gates cleared (governor only). ──
        if self._ceiling_per_min is not None:
            with self._lock:
                self._issues.append(self._clock())
        # ── Periodic telemetry: a throttled rate.snapshot so a HOLDING rate is visible
        #    in /logs even with no rung change (DEC-GROWW-RATE-SNAPSHOT-LOG-001). ──
        self._maybe_log_snapshot()

    def _maybe_log_snapshot(self) -> None:
        """Emit a THROTTLED ``rate.snapshot`` (≤ once per :data:`SNAPSHOT_LOG_INTERVAL_S`).

        DEC-GROWW-RATE-SNAPSHOT-LOG-001 — the gap the existing rate.* events leave: they
        fire ONLY on a rung CHANGE (rate.429 / rate.stepdown / rate.stepup), so a pull
        HOLDING at a stepped-down rate (the operator's "stuck at ~3 r/s with 2.4× ceiling
        headroom") logs nothing, leaving /logs unable to show WHY. This periodic line
        carries the SAME fields :meth:`snapshot` exposes to the dashboard rate panel
        (``current_rps`` + the recent 429 / timeout / per-minute gauges), so the operator
        sees the live rate trajectory + the pressure holding it down. The throttle CLAIM
        is O(1) under the lock; the (rare) :meth:`snapshot` read + log runs OUTSIDE the
        lock (no nesting). Called from :meth:`pace`, so it ticks ONLY during active
        pacing — no background thread, and it stops the instant the pull stops.
        Observability only — it changes no pacing decision.
        """
        now = self._clock()
        with self._lock:
            if now - self._last_snapshot_log < SNAPSHOT_LOG_INTERVAL_S:
                return
            self._last_snapshot_log = now
        _LOG.info("rate.snapshot", **self.snapshot())

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
            # Two-tier 429 ladder (DEC-DHAN-RATE-STORM-FLOOR-001): reach the soft
            # FLOOR_RPS (3) quickly on a brief burst (STEP_DOWN_HITS=3), but ease BELOW
            # it toward the deep RATE_LIMIT_STORM_FLOOR_RPS (1) ONLY on SUSTAINED
            # evidence (STORM_HITS). So a transient quota nudge rests at 3 (a quota
            # breach is no reason to crawl), while a relentless cascade — every call
            # rejected even at 3 r/s — can finally back off instead of spinning pinned
            # at the floor getting 100%-rejected for minutes. The floor for the 429
            # ladder is now the deep RATE_LIMIT_STORM_FLOOR_RPS, gated by the higher
            # STORM_HITS threshold below 3 so it is reached only under a true storm.
            above_soft_floor = self._rps > FLOOR_RPS
            threshold = STEP_DOWN_HITS if above_soft_floor else STORM_HITS
            if len(self._hits) >= threshold and self._rps > RATE_LIMIT_STORM_FLOOR_RPS:
                old_rps = self._rps
                self._rps -= STEP_RPS
                self._bucket.set_rate(self._rps)
                self._hits.clear()
                self._last_step = now
                # Log the EFFECTIVE floor for THIS rung: a soft-floor step (still
                # above 3) heads toward FLOOR_RPS (3); a sustained-storm step below
                # the soft floor heads toward the deep RATE_LIMIT_STORM_FLOOR_RPS (1).
                _LOG.warning(
                    "rate.stepdown",
                    from_rps=old_rps,
                    to_rps=self._rps,
                    floor_rps=FLOOR_RPS if above_soft_floor else RATE_LIMIT_STORM_FLOOR_RPS,
                )

    def on_timeout(self) -> None:
        """Record a server-stress stall; step **down** 1 r/s on a sustained storm — O(1).

        The gentle adaptive timeout ladder (DEC-DHAN-TIMEOUT-BACKOFF-001 +
        DEC-DHAN-OPTION-TIMEOUT-STORM-001): a long pull that starts TIMING OUT (a
        slow, struggling vendor — not a quota breach) eases 5→4→3→2→1 r/s once
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
        atomic O(1) critical section (no I/O / sleep under the lock). Stale 429
        hits AND timeouts (older than :data:`STEP_DOWN_WINDOW`) are evicted here, so
        a storm that pinned the rate at the floor — where ``on_rate_limit``'s
        step-down branch, the only OTHER place ``_hits`` is cleared, can no longer
        fire — cannot block recovery forever once the hits age out
        (DEC-DHAN-RATE-STORM-FLOOR-001 + DEC-DHAN-TIMEOUT-BACKOFF-001).

        Recovery is gated on the **step-down trigger**, NOT on a strictly-empty
        window (DEC-DHAN-RATE-RECOVERY-TRICKLE-001) — see the inline note for the
        trickle-trap this closes.
        """
        with self._lock:
            now = self._clock()
            cutoff = now - STEP_DOWN_WINDOW
            # Evict aged-out 429 hits AND timeouts (DEC-DHAN-RATE-STORM-FLOOR-001).
            # on_success previously evicted ONLY _timeouts. But on_rate_limit clears
            # _hits exclusively inside its step-DOWN branch, which cannot fire once the
            # rate is pinned at the floor (`rps > RATE_LIMIT_STORM_FLOOR_RPS` False) — so
            # after a storm reached the floor, _hits stayed pinned at its rolling-window
            # count and `not self._hits` was NEVER True, leaving the rate STUCK at the
            # floor for the rest of the pull, never recovering to 5 (the operator's
            # 90-min log: 3 r/s the whole run after one storm). Aging stale hits out here
            # lets recovery proceed once the storm has truly cleared (no new 429s).
            while self._hits and self._hits[0] < cutoff:
                self._hits.popleft()
            while self._timeouts and self._timeouts[0] < cutoff:
                self._timeouts.popleft()
            # Recover when NEITHER ladder currently shows step-down-level pressure.
            # This was previously gated on a STRICTLY-empty window (`not self._hits
            # and not self._timeouts`), which created the "trickle-trap"
            # (DEC-DHAN-RATE-RECOVERY-TRICKLE-001): under a CHRONIC mild 429 (or
            # timeout) drip — a few rejections per minute, far BELOW the step-down
            # trigger — the rolling window never fully empties, so `not self._hits`
            # was never True and the rate stayed pinned at the floor FOREVER, never
            # probing back toward the ceiling. (#539's age-out fix only closed the
            # case where a storm FULLY clears to zero; a persistent drip still pinned
            # it — the residual of the operator's "never recovered above 3" log.)
            # Gating on the step-down trigger instead lets a mild drip recover: probe
            # up one rung, and if the higher rate genuinely storms the ladder steps
            # straight back down (self-correcting AIMD) — while genuine
            # step-down-level pressure still HOLDS the rate down. Note the threshold
            # bites only at/below the floor: above it on_rate_limit/on_timeout clear
            # their deque on every step, so at most STEP_DOWN_HITS-1 hits ever sit in
            # the window there and recovery is purely STEP_UP_WINDOW-timer gated; at/
            # below the floor (where no further step clears the deque) a real
            # >= STEP_DOWN_HITS drip is what holds recovery. O(1): len() on a deque.
            if (
                self._rps < self._ceiling
                and len(self._hits) < STEP_DOWN_HITS
                and len(self._timeouts) < STEP_DOWN_TIMEOUT_HITS
                and now - self._last_step >= STEP_UP_WINDOW
            ):
                old_rps = self._rps
                self._rps += STEP_RPS
                self._bucket.set_rate(self._rps)
                self._last_step = now
                _LOG.info(
                    "rate.stepup", from_rps=old_rps, to_rps=self._rps, ceiling_rps=self._ceiling
                )

    def snapshot(self) -> dict[str, object]:
        """A JSON-friendly state snapshot for the dashboard rate panel (thread-safe, O(1)).

        When a :class:`~brutex.orchestrator.quota_tracker.DailyQuotaTracker` is wired
        (the live path — DEC-AUTO-RESUME-QUOTA-001), the snapshot ALSO carries the
        day's quota gauge (``quota_used`` / ``quota_remaining`` / ``quota_limit``) so
        the dashboard SHOWS how much of the mandatory 100,000/day quota is
        spent, not just the live r/s. The quota tracker is read OUTSIDE the
        controller lock (``self._quota`` is set once at construction, never mutated)
        to avoid nesting the controller lock over the tracker's own lock + LMDB read.
        """
        with self._lock:
            # Evict aged-out issue instants first so ``recent_minute`` is an accurate
            # rolling-60s gauge (DEC-GROWW-RATE-PERMIN-001). No-op when disabled (the
            # deque is always empty in that case).
            if self._ceiling_per_min is not None:
                now = self._clock()
                while self._issues and (self._issues[0] + PER_MIN_WINDOW) - now <= 0.0:
                    self._issues.popleft()
            snap: dict[str, object] = {
                "current_rps": self._rps,
                "recent_hits": len(self._hits),
                "recent_timeouts": len(self._timeouts),
                "recent_minute": len(self._issues),
                "ceiling_per_min": self._ceiling_per_min,
            }
        if self._quota is not None:
            snap["quota_used"] = self._quota.current_count()
            snap["quota_remaining"] = self._quota.remaining()
            snap["quota_limit"] = self._quota.daily_limit
        return snap

    def quota_exhausted(self) -> bool:
        """True iff a quota tracker is wired AND the day's quota is fully spent.

        The hook the re-pull-until-clean / auto-resume loop reads after each round
        (DEC-AUTO-RESUME-QUOTA-001): when ``remaining() <= 0`` the loop stops with
        ``stop_quota`` instead of re-firing batches that would only raise
        :class:`~brutex.orchestrator.quota_tracker.DailyQuotaExceeded`. ``False`` when
        no tracker is wired (the bucket-only test path). Read outside the controller
        lock (the tracker has its own lock); O(1) bar one LMDB read.
        """
        return self._quota is not None and self._quota.remaining() <= 0


__all__ = [
    "BURST",
    "CEILING_RPS",
    "FLOOR_RPS",
    "PER_MIN_WINDOW",
    "RATE_LIMIT_STORM_FLOOR_RPS",
    "SNAPSHOT_LOG_INTERVAL_S",
    "STEP_DOWN_HITS",
    "STEP_DOWN_TIMEOUT_HITS",
    "STEP_DOWN_WINDOW",
    "STEP_RPS",
    "STEP_UP_WINDOW",
    "STORM_HITS",
    "TIMEOUT_FLOOR_RPS",
    "RateController",
]
