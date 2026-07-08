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

import math
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Final

import structlog

from brutex.observability.progress_beat import emit_progress_beat
from brutex.orchestrator.quota_tracker import DailyQuotaTracker
from brutex.orchestrator.rate_limiter import AdaptiveTokenBucket

#: Structured logger — surfaces the otherwise-invisible throttle: a 429
#: (``rate.429``), a step DOWN under sustained pressure (``rate.stepdown``), and
#: the recovery back up (``rate.stepup``) into the ``/logs`` stream so the
#: operator SEES exactly when/why effective r/s dropped (DEC-DHAN-RATE-LOGGING-001).
#: (The name derives from ``__name__`` — the identical
#: ``"brutex.adapters.rate_common"`` string — so the line carries NO string
#: literal to mutate, and its one remaining ``= None`` mutant crashes the first
#: emission (suite-killed). The prior pragma here hid that killable ``= None``
#: mutant — removed by the DEC-MUTATION-RUNTIME-003 fixer round, MIGRATION.md
#: #1597.)
_LOG = structlog.get_logger(__name__)

# ── Deferred rate.* emission (DEC-RATE-LOG-OUTSIDE-LOCK-001) ────────────────
# structlog routes through stdlib handlers (a stderr StreamHandler + a rotating
# JSON file handler, ``brutex/observability/logging.py``) — real blocking
# ``write()`` I/O behind handler locks. An emission under
# ``RateController._lock`` lets ONE wedged/slow log sink freeze every worker's
# ``pace()`` and the dashboard sampler's ``snapshot()`` read path, stalling the
# whole pull's pacing. Every rate.* event is therefore CAPTURED under the lock
# as a :data:`_PendingLog` — ``(level, event, fields)`` with every field VALUE
# resolved at the original emission program point, so later same-critical-
# section mutation (``_rps -= STEP_RPS``, ``_hits.clear()``,
# ``_timeouts.clear()``, ``_last_seen = now``) can never drift the emitted
# line — and EMITTED strictly AFTER the lock is released, on the SAME thread
# (contextvars like ``pull_id`` merge at emit time; handing events to another
# thread would silently drop them — deliberately not done). Fields hold only
# immutables or freshly-built containers never referenced by controller state
# afterward (``bucket_counts`` is a fresh list per call). Levels are plain
# strings so NO structlog machinery (lazy proxy binding, logging-manager lock,
# handlers, processors) is touched while ``_lock`` is held. Generalizes the
# file's own ``rate.snapshot`` emit-outside-the-lock precedent
# (:meth:`RateController._maybe_log_snapshot`).
_PendingLog = tuple[str, str, dict[str, object]]  # (level, event, fields)

# ── The pending-slot annotation alias (DEC-MUTATION-RUNTIME-003 fixer round) ──
# Every local ``= None`` capture slot in this module is annotated with THIS
# runtime-evaluated alias instead of a per-line ``_PendingLog | None`` union, so
# no ``|`` sits on any local slot line: the PEP-563 ``|`` → ``&`` annotation
# mutants that previously needed 13 line-scoped pragmas disappear STRUCTURALLY
# (a bare alias NAME generates no operator mutant). The alias line's own two
# mutants are both hard-killed: ``|`` → ``&`` raises ``TypeError`` at import (a
# ``types.GenericAlias`` does not support ``&``) and ``= None`` is pinned by the
# alias-object test. The ONLY mutant left on a local slot line is the
# initializer value ``None`` → ``""`` (mutmut 2.5.1 mutates an annotated
# ``= None`` to ``= ""`` — measured; the earlier "``None`` is not a mutmut
# target" proof comment was FALSE and is superseded by this one). That ``""``
# mutant is SUITE-KILLED on EVERY slot line — no pragma anywhere
# (DEC-MUTATION-RUNTIME-003 fixer round 3, F2; supersedes the earlier
# "dominated dead store … unobservable" claim, which was an untested-path
# admission, not an equivalence proof):
#
# * wherever the initializer can FLOW into ``_flush_rate_logs`` on a plain
#   tested path: the RETURNED ``pending`` slot in
#   :meth:`RateController._handle_clock_gap_locked` (the common no-gap path
#   returns it) and every CONDITIONALLY-assigned ``step_evt`` slot (the
#   no-step path flushes the initializer). ``""`` fails the callers'
#   ``is not None`` guard and crashes the tuple unpack — loud.
# * on the unconditionally-overwritten slots (``gap_evt`` / ``gov_gap_evt`` /
#   ``hit_evt`` / ``latch_evt`` / ``to_evt``): the initializer is LIVE on the
#   in-lock-exception path — each method's ``finally`` reads it — and that
#   path is FORCED through the sanctioned injected-clock seam
#   (``now = self._clock()`` is the first in-lock statement of both ``pace``
#   gates and every ``on_*`` method; ``_maybe_log_snapshot`` reads its clock
#   pre-try, so its gap handler is patched to raise instead) by
#   ``TestInLockExceptionSlotSafety`` (tests/unit/test_rate_common.py), which
#   pins the documented :func:`_flush_rate_logs` exception-safety contract:
#   the injected error propagates unmasked, the lock is released, and NO
#   partial event is emitted. A ``""`` mutant instead passes the flush's
#   ``is not None`` guard and dies on the 3-tuple unpack (ValueError replaces
#   the injected error) — all 9 mutants probe-verified killed, one slot line
#   each.
_PendingLogSlot = _PendingLog | None


def _flush_rate_logs(*events: _PendingLog | None) -> None:
    """Emit captured rate.* events in positional (= capture) order.

    THE single deferred-emission choke point (CLAUDE.md §11 COMMON — one
    capture-then-emit mechanism for every rate.* event in this module). MUST
    only be called with ``RateController._lock`` NOT held — every call site is
    the ``finally`` of a ``try`` whose body is the ``with self._lock:`` block,
    so the lock is provably released first, on success AND on an in-lock
    exception. ``None`` slots (events that did not fire) are skipped. A wedged
    sink now stalls ONLY the emitting thread's own call — never the lock.
    """
    for item in events:
        if item is None:
            continue
        level, event, fields = item
        if level == "info":
            _LOG.info(event, **fields)
        else:
            _LOG.warning(event, **fields)


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

#: Trailing window (seconds) the OBSERVE-ONLY per-second issue COUNTER ring
#: (:attr:`RateController._issue_buckets`) retains — ONE HOUR. EVERY admitted issue
#: (governor-on OR governor-off) increments the current second's bucket AFTER the per-second
#: token bucket clears, so :meth:`RateController.snapshot` can report the REAL integer request
#: count in ANY trailing window up to 3600 s (req/sec, req/min, req/10min, req/hour) measured
#: from ACTUAL per-second issue counts — not a per-second figure multiplied up
#: (DEC-RATE-MONITOR-EXPAND-001, MIGRATION.md #1004). The counter is PURELY observational: it
#: is NEVER read by any pacing gate (the governor still reads its own 60 s :attr:`_issues`
#: deque), so it changes NO pacing decision — the fetch rate is byte-for-byte unchanged.
#: O(1) MEMORY: a FIXED 3600-int ring (:data:`TRAILING_LOG_BUCKETS`) regardless of the issue
#: rate — chosen OVER a per-issue timestamp deque (which would grow O(rate × 3600) ≈ 18k
#: floats at 5 r/s and exceed the DEC-MEMLEAK-GATE-001 256 KiB sustained ceiling). Push is
#: O(1) amortized; a :meth:`snapshot` count is O(window-seconds) (a bounded sum over the last
#: W buckets — documented honestly, NOT claimed O(1)).
TRAILING_LOG_WINDOW_S: float = 3600.0

#: The number of per-second COUNT buckets in the observe-only trailing-hour issue counter ring
#: (:attr:`RateController._issue_buckets`) — one per second of :data:`TRAILING_LOG_WINDOW_S`.
#: The ring is FIXED at this size (3600 ints) so the controller's observability memory is O(1)
#: in the issue rate (DEC-RATE-MONITOR-EXPAND-001).
TRAILING_LOG_BUCKETS: int = 3600

#: The trailing-1 s EVIDENCE window (req/sec = ``recent_1s``) the issue log reports so the
#: operator can see the per-second number is a real trailing-1 s issue count, not a derived
#: average (DEC-RATE-MONITOR-EXPAND-001).
LOG_WINDOW_1S: float = 1.0

#: The trailing-minute evidence window (req/min = ``recent_60s``) — matches
#: :data:`PER_MIN_WINDOW`, so the issue-log ``recent_60s`` approximates the governor's
#: ``recent_minute`` (to within one boundary second) when a governor is wired (one number,
#: two INDEPENDENT sources — a built-in cross-check the operator can verify,
#: DEC-RATE-MONITOR-EXPAND-001). The two windows are NOT bit-identical by construction: the
#: governor's deque uses exact float instants ``(now - 60, now]`` while the ring is binned to
#: integer seconds, so they can legitimately differ by the single boundary second's issues.
LOG_WINDOW_60S: float = 60.0

#: The trailing-TEN-minute evidence window (req/10min = ``recent_600s``) — the NEW metric,
#: a REAL count of issues in the trailing 600 s (DEC-RATE-MONITOR-EXPAND-001).
LOG_WINDOW_600S: float = 600.0

#: The trailing-HOUR evidence window (req/hour = ``recent_3600s``) — a MEASURED trailing-hour
#: count from real timestamps (DEC-RATE-MONITOR-EXPAND-001), equal to
#: :data:`TRAILING_LOG_WINDOW_S` (the log's full retention).
LOG_WINDOW_3600S: float = TRAILING_LOG_WINDOW_S

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
#: so it climbs the soft floor (3) back toward the INJECTED ceiling — the production
#: Groww ceiling is the operator-DIRECTED **8 r/s** (``GROWW_RATE_LIMIT_RPS_CEILING``,
#: RAISED 5 → 8 by DEC-GROWW-RATE-CEILING-8-002; the vendor-neutral :data:`CEILING_RPS`
#: DEFAULT here stays 5) — at ~1 rung/clean-minute (3→8 in ~5 clean windows, ~5 min)
#: instead of the prior 300 s/rung (~25 min — longer than a whole pull, so a
#: stepped-down pull never reached the ceiling; the operator-observed "stuck at
#: ~3 r/s with 2.4x headroom", DEC-GROWW-RATE-RECOVER-8-001; stale "5 r/s ceiling"
#: wording reconciled by DEC-SUSPICION-SWEEP-FIXES-001 G4). SAFETY is unchanged: the DECREMENTAL ladder still steps
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

#: Safety cap on the optional smooth-pacing re-check loop (DEC-GROWW-SMOOTH-PACING-001).
#: Mirrors :data:`_GOVERNOR_LOOP_CAP`: under contention a worker may wake from its
#: spacing sleep to find a peer already issued in the slot it was waiting for, so it
#: must re-test the spacing. This bounds that re-contend loop so a pathological
#: INJECTED clock (one that never advances) can't spin forever — after the cap the
#: pacer fails OPEN (stamps + admits) exactly as the governor's loop cap does.
_SMOOTH_LOOP_CAP: int = 64

# ── Smooth-pacing MODES (DEC-SMOOTH-PACING-AUTOLATCH-001) ─────────────────────────
#: Smooth pacing FORCED ON from construction — the prior ``smooth_pacing=True``
#: behaviour: every issue is spaced ``>= 1/current_rps`` from the previous one.
SMOOTH_PACING_ON: Final[str] = "on"
#: Smooth pacing (and the auto-latch) DISABLED entirely — the prior
#: ``smooth_pacing=False`` behaviour, byte-identical pacing.
SMOOTH_PACING_OFF: Final[str] = "off"
#: ADAPTIVE auto-latch: smoothing starts OFF (byte-identical pacing) and ONE-WAY
#: latches ON — permanently, for the controller's lifetime — the first time
#: :meth:`RateController.on_rate_limit` observes a 429 WITH measured per-1s burst
#: evidence: any of the trailing :data:`SMOOTH_LATCH_SCAN_SECONDS` per-second issue
#: buckets exceeding the then-current allowed ``current_rps``. Evidence-gated, so a
#: 429 that arrives WITHOUT a local burst (a genuine vendor quota squeeze at an
#: even issue cadence) never latches — the AIMD ladder alone handles it.
SMOOTH_PACING_ADAPTIVE: Final[str] = "adaptive"
#: The valid smooth-pacing mode tokens (an unknown token fails LOUD at construction
#: — a rate-gate mode is never silently coerced; CLAUDE.md §2 Rule 6).
_SMOOTH_PACING_MODES: Final[frozenset[str]] = frozenset(
    {SMOOTH_PACING_ON, SMOOTH_PACING_OFF, SMOOTH_PACING_ADAPTIVE}
)

#: Trailing per-1s issue buckets the ADAPTIVE latch scans on each 429
#: (DEC-SMOOTH-PACING-AUTOLATCH-001). A 429 lands AFTER the offending request's
#: network latency, so the burst that provoked it sits a bucket or two BEHIND the
#: current second — scanning the last ~3 seconds covers the observed sub-3s
#: request→429 round-trip envelope while staying O(1) per 429.
SMOOTH_LATCH_SCAN_SECONDS: Final[int] = 3

#: Sleep/wake clock-gap threshold (DEC-RATE-CLOCK-SLEEPWAKE-001). The controller
#: paces off ``time.monotonic()`` which, on a laptop, jumps forward by a large delta
#: when the machine SLEEPS overnight and WAKES (and a rare platform/NTP glitch can even
#: step it slightly back). A SINGLE inter-event delta whose magnitude EXCEEDS this
#: threshold is treated as a sleep/wake event: the rolling-60s issue / 429 / timeout
#: histories are pruned and every time-anchored marker is re-based to ``now`` so a
#: post-wake pull starts FRESH — it can never burst past the per-second OR per-minute
#: cap on stale pre-sleep timestamps (failure mode (a)), and never mis-times AIMD
#: recovery/backoff by interpreting the whole sleep as elapsed pacing time (failure
#: mode (b)). Set to FIVE :data:`PER_MIN_WINDOW`s (300 s = 5 min), comfortably ABOVE
#: every normal operational gap so genuine pacing NEVER false-trips: a healthy pull
#: issues many times per second, and even at the 1 r/s deep floor a call lands every
#: ~1 s, so the LONGEST normal gap between two controller calls is the AIMD recovery
#: window (one :data:`STEP_UP_WINDOW` = 60 s) — well under 300 s. A laptop sleeping
#: overnight is minutes-to-hours, so 5 min cleanly separates "between recovery windows"
#: (keep the rolling state) from "the machine slept" (reset). Any gap this large already
#: evicts the ENTIRE 60 s rolling history under normal eviction; the explicit reset ALSO
#: re-bases the AIMD anchors (``_last_step`` / ``_last_snapshot_log`` / ``_last_issue_ts``)
#: that ordinary eviction does not touch. O(1): one subtraction + (rarely) a few clears.
CLOCK_GAP_THRESHOLD_S: float = 5.0 * PER_MIN_WINDOW

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
    NEVER held across network I/O, a sleep, or a structlog emission — every
    rate.* event is captured under the lock and emitted after release via
    :func:`_flush_rate_logs` (DEC-RATE-LOG-OUTSIDE-LOCK-001), so a wedged/slow
    log sink can never freeze the pacing lock. In particular :meth:`pace` copies
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

    # Attribute TYPE declarations (DEC-MUTATION-RUNTIME-003 fixer round): a bare
    # class-body annotation generates NO mutmut mutant (measured on the pinned
    # mutmut 2.5.1), so the PEP-563 ``|`` → ``&`` annotation mutants that
    # previously needed line-scoped pragmas on the ``__init__`` assignments
    # disappear STRUCTURALLY — while the un-annotated ``__init__`` assignments'
    # VALUE mutants (``= None`` / ``= ""`` / ``0 → 1``) stay in the gate and are
    # suite-killed (the ``= None`` mutant of ``_ceiling_per_min`` DISABLES the
    # operator-confirmed 500/min governor — exactly the regression detector the
    # prior pragma was hiding).
    _ceiling_per_min: int | None
    _issue_head: int
    _issue_newest_sec: int | None

    def __init__(
        self,
        *,
        quota_tracker: DailyQuotaTracker | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        ceiling_rps: float = CEILING_RPS,
        ceiling_per_min: int | None = None,
        smooth_pacing: bool = False,
        smooth_pacing_mode: str | None = None,
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
        smooth_pacing
            LEGACY boolean ANTI-BURST gate (DEC-GROWW-SMOOTH-PACING-001), the
            back-compat alias consulted ONLY when *smooth_pacing_mode* is ``None``:
            ``False`` (the default) resolves to :data:`SMOOTH_PACING_OFF` —
            byte-for-byte backward-compatible pacing with the auto-latch disabled —
            and ``True`` resolves to :data:`SMOOTH_PACING_ON`. The gate itself is
            unchanged: when ACTIVE it runs FIRST in :meth:`pace`, enforcing a global
            MINIMUM inter-issue interval of ``1.0 / current_rps`` seconds across ALL
            workers (a shared ``_last_issue_ts`` under the SAME lock the token math
            uses), so issuance is spread evenly and the per-second issue count can
            never exceed the current AIMD rate — no 9-burst. It only ever ADDS
            spacing: it never raises the per-second ceiling, the 500/min governor,
            or any AIMD rung — it composes WITH (never bypasses) both existing
            gates. SAFE-but-UNVERIFIED (Golden Rule #10): the smoothing is provably
            correct + cannot exceed any cap, but the live 429-reduction benefit is
            UNVERIFIED pending a live vendor probe — no throughput gain is claimed.
        smooth_pacing_mode
            TRI-STATE smooth-pacing mode (DEC-SMOOTH-PACING-AUTOLATCH-001),
            overriding the legacy boolean when given: :data:`SMOOTH_PACING_ON`
            (latched from construction — the legacy ``True``),
            :data:`SMOOTH_PACING_OFF` (gate AND auto-latch disabled — the legacy
            ``False``), or :data:`SMOOTH_PACING_ADAPTIVE` — smoothing starts OFF
            (byte-identical pacing) and :meth:`on_rate_limit` ONE-WAY latches it ON,
            permanently for this controller's lifetime, the first time a 429 lands
            WITH measured burst evidence (any of the trailing
            :data:`SMOOTH_LATCH_SCAN_SECONDS` per-second issue buckets exceeding the
            then-current allowed ``current_rps`` — the 429 arrives after request
            latency, so the offending burst sits in the trailing buckets, not the
            current instant). The latch only ever ADDS spacing on measured evidence;
            it never unlatches and never touches any cap or AIMD rung. ``None``
            (default) derives the mode from *smooth_pacing* — every existing caller
            is byte-identical. An unknown token raises :class:`ValueError`
            (fail-loud — a rate-gate mode is never silently coerced, CLAUDE.md §2
            Rule 6).
        """
        self._clock = clock
        self._sleep = sleep
        self._quota = quota_tracker
        self._ceiling: float = ceiling_rps
        self._rps: float = ceiling_rps
        #: Per-minute governor (DEC-GROWW-RATE-PERMIN-001): the injected HARD cap +
        #: the rolling deque of issue instants. ``None`` cap ⇒ governor disabled, the
        #: deque stays empty + ``pace`` skips both governor phases entirely.
        #: (Type declared on the class body; this un-annotated assignment's only
        #: mutant — ``= None``, which silently DISABLES the governor — is
        #: suite-killed. DEC-MUTATION-RUNTIME-003 fixer round: the prior
        #: annotation+pragma hid that mutant behind the equivalent ``|`` one.)
        self._ceiling_per_min = ceiling_per_min
        self._issues: deque[float] = deque()
        #: OBSERVE-ONLY trailing-hour issue COUNTER (DEC-RATE-MONITOR-EXPAND-001). A
        #: FIXED-SIZE ring of :data:`TRAILING_LOG_BUCKETS` (3600) per-second COUNT buckets —
        #: ``_issue_buckets[i]`` is the number of issues admitted in the integer second
        #: ``_issue_newest_sec - ((_issue_head - i) mod 3600)``. EVERY admitted issue
        #: (governor-on OR governor-off) increments the current second's bucket in
        #: :meth:`pace` AFTER the per-second token bucket clears; advancing to a newer second
        #: zeroes the buckets for the elapsed (now-evicted) seconds. This is the SCALABLE
        #: replacement for a per-issue timestamp deque (which would grow O(rate × 3600) ≈ 18k
        #: floats at 5 r/s and tripped the DEC-MEMLEAK-GATE-001 256 KiB stress ceiling): the
        #: ring is **O(1) MEMORY** — a FIXED 3600 ints regardless of the issue rate — so the
        #: controller's sustained memory stays flat. It is NEVER read by any pacing decision
        #: (the governor still reads its own 60 s :attr:`_issues` deque); it feeds ONLY
        #: :meth:`snapshot`'s REAL windowed counts (``recent_1s`` / ``recent_60s`` /
        #: ``recent_600s`` / ``recent_3600s``), so the operator can verify req/sec·min·10min·
        #: hour are genuine per-second issue counts, not a rate multiplied up. Push is O(1)
        #: amortized (one increment + a bounded advance that zeroes at most 3600 buckets once);
        #: a windowed count is O(window-seconds) (a bounded sum over the last W buckets).
        self._issue_buckets: list[int] = [0] * TRAILING_LOG_BUCKETS
        #: Ring index of the bucket holding :attr:`_issue_newest_sec` (the current second).
        #: (The head start IS behaviourally arbitrary while every bucket is still
        #: zero, but the documented "starts at slot 0" contract is pinned by the
        #: ring-head regression test so BOTH the ``0 → 1`` and the crash-class
        #: ``= None`` mutants are observable — no pragma;
        #: DEC-MUTATION-RUNTIME-003 fixer round.)
        self._issue_head = 0
        #: The integer second (``floor(clock())``) the head bucket currently counts. ``None``
        #: until the FIRST issue is recorded (an all-zero ring reads 0 in every window).
        #: (Type declared on the class body; the un-annotated assignment's only
        #: mutant — ``= ""`` — TypeErrors on the first recorded issue, suite-killed;
        #: no pragma. DEC-MUTATION-RUNTIME-003 fixer round.)
        self._issue_newest_sec = None
        #: Resolved smooth-pacing MODE (DEC-SMOOTH-PACING-AUTOLATCH-001): the explicit
        #: *smooth_pacing_mode* wins; ``None`` derives it from the legacy boolean
        #: (``True`` → ON, ``False`` → OFF — byte-identical for every existing caller).
        #: An unknown token fails LOUD (a rate-gate mode is never silently coerced).
        if smooth_pacing_mode is None:
            smooth_pacing_mode = SMOOTH_PACING_ON if smooth_pacing else SMOOTH_PACING_OFF
        if smooth_pacing_mode not in _SMOOTH_PACING_MODES:
            raise ValueError(f"unknown smooth_pacing_mode: {smooth_pacing_mode!r}")
        self._smooth_mode: str = smooth_pacing_mode
        #: Anti-burst smooth pacing — the LIVE gate switch (DEC-GROWW-SMOOTH-PACING-001
        #: + DEC-SMOOTH-PACING-AUTOLATCH-001). ``False`` ⇒ the smooth gate is skipped
        #: ENTIRELY in :meth:`pace` (byte-identical to the prior behaviour) and
        #: ``_last_issue_ts`` is never read/written. ``True`` ⇒ the gate enforces a
        #: ``1/current_rps`` minimum interval between consecutive issues across all
        #: workers, sharing the SAME lock as the token math. Starts ``True`` only in
        #: :data:`SMOOTH_PACING_ON` mode; in :data:`SMOOTH_PACING_ADAPTIVE` mode
        #: :meth:`on_rate_limit` flips it ``True`` ONCE — permanently — on the first
        #: 429 with measured burst evidence (the one-way auto-latch); in
        #: :data:`SMOOTH_PACING_OFF` mode it can never become ``True``.
        self._smooth_pacing: bool = smooth_pacing_mode == SMOOTH_PACING_ON
        #: Last issue instant under smooth pacing — the shared spacing baseline. ``-inf``
        #: so the FIRST issue under smooth mode never waits (any real gap exceeds the
        #: minimum interval). Only ever touched when ``_smooth_pacing`` is True.
        self._last_issue_ts: float = float("-inf")
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
        #: Last clock instant observed inside a lock-held critical section — the
        #: sleep/wake gap baseline (DEC-RATE-CLOCK-SLEEPWAKE-001). Every critical
        #: section calls :meth:`_handle_clock_gap_locked` FIRST, which compares ``now``
        #: against this marker: a magnitude jump > :data:`CLOCK_GAP_THRESHOLD_S` (a
        #: laptop sleep/wake, or a rare backward NTP/monotonic glitch) prunes the
        #: rolling histories + re-bases the AIMD anchors so issuance starts fresh.
        #: Seeded at construction so the first activity never spuriously trips it.
        self._last_seen: float = clock()
        self._bucket: AdaptiveTokenBucket = self._make_bucket()
        #: Guards the step-ladder state under concurrent worker access
        #: (DEC-DHAN-PARALLEL-INGEST-001). Held only for O(1) state reads/writes,
        #: NEVER across the bucket ``.acquire()`` sleep in :meth:`pace`, and NEVER
        #: across a structlog emission — rate.* events are captured under it and
        #: emitted after release (DEC-RATE-LOG-OUTSIDE-LOCK-001).
        self._lock = threading.Lock()

    def _make_bucket(self) -> AdaptiveTokenBucket:
        """Build the ONE persistent token bucket at the current rate (shares the quota tracker).

        Called ONCE at construction. Every subsequent rate step mutates THIS
        object in place via :meth:`AdaptiveTokenBucket.set_rate` rather than
        rebuilding — see the class docstring (DEC-DHAN-RATE-SINGLE-BUCKET-001).
        """
        return AdaptiveTokenBucket(
            initial_rps=self._rps,
            # unused (we never feed the bucket hits); just < initial. NOT
            # pragma'd (DEC-MUTATION-RUNTIME-003 fixer round — the prior
            # "value mutant is unobservable" claim was FALSE): the value IS
            # directly observable via ``bucket.downshift_rps``, pinned at half
            # the live rate by the bucket-attribute test (kills the ``/ 3.0``
            # mutant), and the ``* 2.0`` operator mutant already dies at
            # construction (``AdaptiveTokenBucket.__post_init__`` requires
            # ``downshift_rps < initial_rps``).
            downshift_rps=self._rps / 2.0,
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

    @property
    def smooth_pacing_mode(self) -> str:
        """The resolved smooth-pacing mode token (DEC-SMOOTH-PACING-AUTOLATCH-001) — O(1).

        One of :data:`SMOOTH_PACING_ON` / :data:`SMOOTH_PACING_OFF` /
        :data:`SMOOTH_PACING_ADAPTIVE`. Immutable after construction (the ADAPTIVE
        latch flips :attr:`smooth_pacing_active`, never the mode), so no lock is
        needed — the read is a plain attribute load.
        """
        return self._smooth_mode

    @property
    def smooth_pacing_active(self) -> bool:
        """``True`` iff the anti-burst smooth gate is currently ENFORCING — thread-safe.

        ON mode ⇒ always ``True``; OFF mode ⇒ always ``False``; ADAPTIVE mode ⇒
        ``False`` until the one-way latch fires (then ``True`` for the controller's
        lifetime — it never unlatches, DEC-SMOOTH-PACING-AUTOLATCH-001).
        """
        with self._lock:
            return self._smooth_pacing

    def _handle_clock_gap_locked(self, now: float) -> _PendingLog | None:
        """Detect a laptop sleep/wake clock jump; reset rolling state if one occurred — O(1).

        Returns the captured ``rate.clock_gap`` event (or ``None`` when no gap
        tripped); the caller MUST hand it to :func:`_flush_rate_logs` AFTER
        releasing ``self._lock`` — this helper never emits
        (DEC-RATE-LOG-OUTSIDE-LOCK-001).

        DEC-RATE-CLOCK-SLEEPWAKE-001. The caller MUST hold ``self._lock`` and pass the
        ``now`` it just read from the injected clock. Compares ``now`` against the last
        instant seen in any critical section (:attr:`_last_seen`); when the MAGNITUDE of
        the delta exceeds :data:`CLOCK_GAP_THRESHOLD_S` (300 s = 5 min) — a MacBook sleeping
        overnight and waking, or a rare backward monotonic/NTP glitch — it treats it as
        a sleep/wake event and:

        * clears the rolling-60s issue / 429 / timeout histories (``_issues`` /
          ``_hits`` / ``_timeouts``) so no STALE pre-sleep timestamp can let a post-wake
          burst overshoot the per-second OR per-minute cap (failure mode (a)), and
        * re-bases every time-anchored marker (``_last_step`` / ``_last_snapshot_log``,
          and ``_last_issue_ts`` only under smooth pacing) to ``now`` so AIMD
          recovery/backoff is timed from the wake, not mis-counting the whole sleep as
          elapsed pacing time (failure mode (b)).

        It NEVER changes ``_rps`` — a wake neither steps the rate up nor down; it only
        forgets stale timing so the NEXT genuine signal decides. ``_last_seen`` is always
        advanced to ``now`` (gap or not), so a steady stream of sub-threshold deltas never
        accumulates into a false trip. O(1): one subtraction + (rarely) a few deque
        clears.
        """
        # ``pending`` is the RETURNED slot — its ``None → ""`` initializer
        # mutant flows straight into every caller's ``_flush_rate_logs`` on the
        # common no-gap path and is suite-killed, so NO pragma (the pragma-class
        # proof for the capture slots lives at :data:`_PendingLogSlot`;
        # DEC-MUTATION-RUNTIME-003 fixer round, MIGRATION.md #1597).
        pending: _PendingLogSlot = None
        if abs(now - self._last_seen) > CLOCK_GAP_THRESHOLD_S:
            self._issues.clear()
            # Clear the observe-only trailing-hour issue counter ring too
            # (DEC-RATE-MONITOR-EXPAND-001): a laptop sleep/wake makes every pre-sleep count
            # stale, so a post-wake snapshot must not count them into req/sec·min·10min·hour.
            # Zero the buckets + reset the anchor so the ring restarts fresh. Observability
            # only — no pacing effect (the ring is never read by a gate).
            for i in range(TRAILING_LOG_BUCKETS):
                self._issue_buckets[i] = 0
            # (The gap-reset twin of the constructor's head start — pinned at
            # slot 0 by the ring-head regression test so both the ``0 → 1`` and
            # the crash-class ``= None`` mutants are observable — no pragma;
            # DEC-MUTATION-RUNTIME-003 fixer round.)
            self._issue_head = 0
            self._issue_newest_sec = None
            self._hits.clear()
            self._timeouts.clear()
            self._last_step = now
            self._last_snapshot_log = now
            if self._smooth_pacing:
                # Re-base the spacing baseline to the construction sentinel (``-inf``)
                # so the FIRST post-wake issue admits immediately (no spurious wait
                # against a pre-sleep instant); the per-second spacing re-binds from
                # that issue onward, exactly as on a fresh controller.
                self._last_issue_ts = float("-inf")
            # Captured BEFORE ``self._last_seen = now`` below overwrites the gap
            # baseline — the emitted ``gap_s`` is the real observed jump.
            pending = (
                "warning",
                "rate.clock_gap",
                {"gap_s": now - self._last_seen, "current_rps": self._rps},
            )
        self._last_seen = now
        return pending

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

    def _smooth_wait_locked(self, now: float) -> float:
        """Seconds to wait so consecutive issues are ≥ ``1/current_rps`` apart (caller holds lock).

        DEC-GROWW-SMOOTH-PACING-001 — the anti-burst spacing arithmetic, O(1). Returns
        ``0.0`` when the previous issue is already at least ``1/current_rps`` seconds in
        the past (or there has been no prior issue — ``_last_issue_ts`` starts ``-inf``),
        otherwise the remaining seconds until the minimum interval elapses,
        ``(_last_issue_ts + 1/rps) - now``. The interval is derived from the LIVE
        ``_rps`` so an AIMD step-down widens the spacing immediately + a step-up narrows
        it, always tracking the current sustainable rate (never exceeding it).
        """
        interval = 1.0 / self._rps
        wait = (self._last_issue_ts + interval) - now
        # ``max(wait, 0.0)`` — identical to ``wait if wait > 0.0 else 0.0`` (an
        # overdue/at-boundary wait clamps to exactly 0.0) but rephrased so the ONLY
        # generated mutant on this line is boundary-observable, never a silent
        # tie-equivalent (DEC-MUTATION-RUNTIME-003, MIGRATION.md #1597).
        return max(wait, 0.0)

    def _advance_issue_ring_locked(self, sec: int) -> None:
        """Advance the per-second issue ring's head to integer second *sec* (caller holds lock).

        DEC-RATE-MONITOR-EXPAND-001. Moves the ring head forward by ``sec - newest`` seconds,
        ZEROING each newly-entered bucket (those seconds had no issues yet and the bucket they
        re-use held a now-evicted >1 h-old count). Idempotent when ``sec`` equals the current
        newest second (no advance). A jump of ≥ :data:`TRAILING_LOG_BUCKETS` seconds zeroes the
        WHOLE ring once (every old count has aged out) — bounded to one full pass, never more.
        O(min(elapsed, 3600)) — O(1) amortized across the hour. A ``None`` ``_issue_newest_sec``
        (the ring never seeded) is a no-op: both callers seed/guard it before calling, but the
        explicit guard keeps this total + narrows the type without an ``assert``.
        """
        newest = self._issue_newest_sec
        if newest is None:
            return  # ring unseeded — nothing to advance (both callers guard, defensive)
        steps = sec - newest
        # ``steps < 1`` — int-identical to ``steps <= 0`` but rephrased so EVERY
        # generated mutant of this guard is boundary-observable (the ``<= 0 → < 0``
        # tie-equivalent disappears; DEC-MUTATION-RUNTIME-003, MIGRATION.md #1597).
        # NEVER pragma this line: it also carries the REAL off-by-one boundary mutant.
        if steps < 1:
            return  # same second (or a tiny backward skew already gap-handled): no advance
        # Zero at most a full ring: beyond 3600 elapsed seconds every old bucket has aged out.
        for _ in range(min(steps, TRAILING_LOG_BUCKETS)):
            self._issue_head = (self._issue_head + 1) % TRAILING_LOG_BUCKETS
            self._issue_buckets[self._issue_head] = 0
        self._issue_newest_sec = sec

    def _record_issue_locked(self, now: float) -> None:
        """Count one issue at *now* in the observe-only trailing-hour ring (caller holds lock).

        DEC-RATE-MONITOR-EXPAND-001. Called from :meth:`pace` for EVERY admitted issue (after
        the per-second token bucket clears), regardless of whether a governor is wired. Bins
        the issue into its integer second's bucket: on the FIRST issue it seeds the head at
        ``floor(now)``; otherwise it advances the ring head to that second (zeroing the elapsed
        buckets) then increments the head bucket. O(1) AMORTIZED (a bounded advance + one
        increment). NEVER read by a pacing gate — it feeds ONLY :meth:`snapshot`'s real
        windowed counts (the trust/transparency evidence). O(1) MEMORY (the fixed ring).
        """
        sec = math.floor(now)
        if self._issue_newest_sec is None:
            self._issue_newest_sec = sec  # seed the ring on the first ever issue
        else:
            self._advance_issue_ring_locked(sec)
        self._issue_buckets[self._issue_head] += 1

    def _count_in_window_locked(self, now: float, window: float) -> int:
        """Count issues in the trailing *window* seconds from the ring (caller holds lock).

        DEC-RATE-MONITOR-EXPAND-001. The REAL request count in the trailing window
        ``(now - window, now]`` from the per-second issue ring — the evidence behind req/sec
        (``window=1``), req/min (``60``), req/10min (``600``), and req/hour (``3600``). FIRST
        advances the ring head to ``floor(now)`` so that elapsed-but-issue-free seconds since
        the last issue are correctly counted as empty (a poll long after the last issue reads
        0, never a stale count). A bucket for integer second ``s`` holds issues in ``[s,
        s+1)``, so a trailing ``(now - window, now]`` touches the buckets for seconds
        ``floor(now - window) .. floor(now)`` — that span (``floor(now) - floor(now-window) +
        1`` buckets, capped at the ring size) is summed walking backward from the head. The
        oldest bucket uses ``floor`` (NOT ``ceil``): the bucket holding ``now - window`` —
        second ``floor(now - window)`` — also holds the sub-range ``(now - window, floor(now -
        window) + 1)`` that IS inside the trailing window, so ``ceil`` would DROP that boundary
        second and undercount it (the off-by-one that made the documented ``recent_60s ==
        recent_minute`` cross-check falsely UNEQUAL near a fractional-second boundary — the two
        windows now agree to within one boundary second).
        Returns 0 when no issue has ever been recorded (the ring is unseeded). O(window-seconds)
        — a bounded sum, honestly NOT O(1) (Golden Rule #10); a per-second figure from a 60 s
        ring would be cheaper but would not be a measured trailing-window count, which is
        exactly the trust the operator asked for.
        """
        if self._issue_newest_sec is None:
            return 0
        # Roll the ring forward to ``now`` so idle seconds since the last issue read as empty.
        head_sec = math.floor(now)
        self._advance_issue_ring_locked(head_sec)
        # Buckets touched by ``(now - window, now]`` = seconds floor(now-window)..floor(now).
        # ``floor`` (not ``ceil``): the bucket holding ``now - window`` also holds the sub-range
        # ``(now - window, …)`` inside the window, so ``ceil`` would drop that boundary second.
        oldest_sec = math.floor(now - window)
        # The raw bucket span for ``(now - window, now]``: the ``+ 1`` closes the
        # inclusive ``floor(now - window) .. floor(now)`` range (split onto its own
        # line so its off-by-one mutants stay observable; DEC-MUTATION-RUNTIME-003,
        # MIGRATION.md #1597).
        raw_span = head_sec - oldest_sec + 1
        # Cap at the ring size only. The former defensive ``max(raw_span, 0)``
        # clamp was PROVABLY DEAD and is DELETED instead of pragma'd (the #624
        # dead-clamp idiom; DEC-MUTATION-RUNTIME-003 fixer round): every caller
        # passes ``window >= 1``, so ``head_sec >= oldest_sec`` and
        # ``raw_span >= 1`` always — and even a pathological ``window < 0``
        # would only make ``range(span)`` empty (an honest 0 count), never an
        # error. The line's remaining ``= None`` mutant crashes ``range(span)``
        # below — suite-killed.
        span = min(raw_span, TRAILING_LOG_BUCKETS)
        count = 0
        for offset in range(span):
            count += self._issue_buckets[(self._issue_head - offset) % TRAILING_LOG_BUCKETS]
        return count

    def _recent_bucket_counts_locked(self, now: float, seconds: int) -> list[int]:
        """The trailing *seconds* per-second issue counts, OLDEST → NEWEST (caller holds lock).

        DEC-SMOOTH-PACING-AUTOLATCH-001 — the ADAPTIVE latch's burst evidence. Reads
        the EXISTING observe-only trailing-hour issue ring
        (DEC-RATE-MONITOR-EXPAND-001; maintained at issue time in
        :meth:`_record_issue_locked` — already granular per second, so NO new
        accounting is added), first advancing the head to ``floor(now)`` so
        issue-free elapsed seconds read as honest zeros. Returns ``[]`` when the
        ring is unseeded (no issue ever recorded — there is no burst evidence
        without issues). O(*seconds*) — a bounded walk, never the whole ring.
        """
        if self._issue_newest_sec is None:
            return []
        self._advance_issue_ring_locked(math.floor(now))
        span = min(max(seconds, 0), TRAILING_LOG_BUCKETS)
        return [
            self._issue_buckets[(self._issue_head - offset) % TRAILING_LOG_BUCKETS]
            for offset in range(span - 1, -1, -1)
        ]

    def _maybe_latch_smooth_locked(self, now: float) -> _PendingLog | None:
        """ONE-WAY latch smooth pacing ON when a 429 carries burst evidence (caller holds lock).

        Returns the captured ``rate.smooth_pacing_latched`` event (or ``None``
        when the latch did not fire); the caller MUST hand it to
        :func:`_flush_rate_logs` AFTER releasing ``self._lock`` — this helper
        never emits (DEC-RATE-LOG-OUTSIDE-LOCK-001).

        DEC-SMOOTH-PACING-AUTOLATCH-001. Runs ONLY in :data:`SMOOTH_PACING_ADAPTIVE`
        mode while not yet latched (ON is latched from construction; OFF disables the
        latch entirely). Scans the trailing :data:`SMOOTH_LATCH_SCAN_SECONDS`
        per-second issue buckets — a 429 lands AFTER the offending request's network
        latency, so the burst that provoked it sits in the trailing buckets, not the
        current instant — and latches iff ANY bucket's issue count EXCEEDED the
        then-current allowed ``current_rps`` (strictly ``>``: a bucket AT the allowed
        rate is compliant issuance, not a burst). Called BEFORE the 429 ladder's
        step-down mutates ``_rps``, so the comparison reads the rate that was allowed
        when the burst was issued (an APPROXIMATION when the rate stepped between the
        burst and the 429 — honest envelope: the ladder steps at most 1 r/s per
        :data:`STEP_DOWN_WINDOW`, so the drift is bounded at 1 r/s). Captures ONE
        ``rate.smooth_pacing_latched`` WARNING carrying the evidence (the scanned
        bucket counts + the PRE-stepdown current rate — the value at this program
        point, deliberately resolved before the caller's ladder mutates ``_rps``);
        latched state never clears. O(1) — a bounded 3-bucket scan per 429, only
        until the latch fires.
        """
        if self._smooth_mode != SMOOTH_PACING_ADAPTIVE or self._smooth_pacing:
            return None
        counts = self._recent_bucket_counts_locked(now, SMOOTH_LATCH_SCAN_SECONDS)
        if not any(count > self._rps for count in counts):
            return None
        self._smooth_pacing = True
        # Re-base the spacing baseline to the construction sentinel so the FIRST
        # smoothed issue admits immediately (no spurious wait against a pre-latch
        # instant); per-issue spacing binds from that issue onward, exactly as on a
        # construction-latched (ON-mode) controller.
        self._last_issue_ts = float("-inf")
        # ``counts`` is a fresh list built above and never referenced by controller
        # state afterward — safe to carry by reference into the deferred emission.
        return (
            "warning",
            "rate.smooth_pacing_latched",
            {
                "bucket_counts": counts,
                "current_rps": self._rps,
                "scan_seconds": SMOOTH_LATCH_SCAN_SECONDS,
            },
        )

    def pace(self) -> None:
        """Block until every rate gate allows an issue — **O(1)** amortized fast path.

        SEQUENCED gates (the smooth gate is OPTIONAL + OFF by default):

        0. **Anti-burst smooth pacing** (only when ``smooth_pacing`` is set,
           DEC-GROWW-SMOOTH-PACING-001) — enforces a global MINIMUM inter-issue
           interval of ``1/current_rps`` across all workers via a shared
           ``_last_issue_ts`` under the same lock, so the per-second issue count can
           never exceed the current AIMD rate (no sub-second burst). It only ADDS
           spacing; it never raises any cap. Skipped ENTIRELY when disabled —
           byte-for-byte identical to the prior two-gate ``pace``.
        1. **Per-minute governor** (only when ``ceiling_per_min`` is set) — a HARD
           ceiling on the issue count inside any rolling :data:`PER_MIN_WINDOW`. It
           runs its own compute-under-lock / sleep-outside-lock / re-check loop,
           mirroring :meth:`AdaptiveTokenBucket.acquire` (CLAUDE.md §11 COMMON). When a
           worker observes a free slot it RESERVES it (appends its issue instant) in the
           SAME lock acquisition — so the ``(check, reserve)`` is atomic and N concurrent
           workers can never all pass one free slot — no overshoot (DEC-GROWW-GOVERNOR
           -ATOMIC-001).
        2. **Per-second token bucket** — the unchanged two-phase delegation below.

        CRITICAL (DEC-DHAN-PARALLEL-INGEST-001): the controller lock is taken ONLY
        for O(1) state work and is RELEASED before EVERY sleep (the governor sleep
        AND ``bucket.acquire()``) AND before every structlog emission (a captured
        ``rate.clock_gap`` is flushed via :func:`_flush_rate_logs` after each lock
        release — DEC-RATE-LOG-OUTSIDE-LOCK-001). Holding ``self._lock`` across a
        sleep would serialise every worker through one mutex and collapse the
        achieved rate to ~1 r/s; holding it across an emission would let one
        wedged log sink freeze every worker's pacing. The two gates SEQUENCE (never nest), so there is no lock-ordering
        cycle: the governor never holds ``self._lock`` when the bucket acquires its
        own lock. The issue instant is RESERVED at governor admission (gate 1), under
        the lock, ATOMICALLY with the free-slot check — so the deque counts true
        admitted issues and can never exceed the cap even under concurrent workers
        (DEC-GROWW-GOVERNOR-ATOMIC-001 — the pre-fix code appended only AFTER
        ``bucket.acquire()``, a separate critical section, which let concurrent workers
        overshoot by ``N-1``). Recording at admission is a few ms before the per-second
        bucket clears — conservative for a HARD cap (never under-counts). A concurrent
        per-second step mutates the one bucket IN PLACE
        (:meth:`AdaptiveTokenBucket.set_rate`) and never touches the governor state.
        """
        # ── Gate 0: anti-burst smooth pacing (skipped entirely when disabled). ──
        # DEC-GROWW-SMOOTH-PACING-001. Enforce a global minimum ``1/current_rps``
        # interval between consecutive issues. Compute the wait under the lock
        # (O(1)), SLEEP outside it (so it never serialises workers through the mutex,
        # mirroring the governor + bucket), then re-test on wake: a peer may have
        # issued into the slot we waited for, so the spacing must be re-checked. We
        # stamp ``_last_issue_ts`` in the SAME lock acquisition that observes a
        # zero wait — so the (check, stamp) is atomic and N concurrent workers can
        # never collapse onto one issue instant (the governor's atomic-reserve idiom,
        # DEC-GROWW-GOVERNOR-ATOMIC-001). The bucket (gate 2) still binds the physical
        # token rate; this gate only ADDS spacing, never raises a cap.
        if self._smooth_pacing:
            iterations = 0
            while True:
                # ``gap_evt`` NEEDS the None initializer (the finally reads it on an
                # in-lock exception path — forced by the raising-clock test, so its
                # ``""`` mutant is suite-killed; :data:`_PendingLogSlot`); ``wait``
                # deliberately has NO initializer — every path that reads it is
                # dominated by the in-lock assignment below (an in-lock exception
                # propagates past all reads), and a dead ``wait = 0.0`` here was
                # flagged by CodeQL (py/multiple-definition).
                gap_evt: _PendingLogSlot = None
                try:
                    with self._lock:
                        now = self._clock()
                        gap_evt = self._handle_clock_gap_locked(now)
                        wait = self._smooth_wait_locked(now)
                        if wait <= 0.0:
                            self._last_issue_ts = now  # stamp the issue atomically
                finally:
                    # Emit the captured clock-gap AFTER releasing the lock
                    # (DEC-RATE-LOG-OUTSIDE-LOCK-001) — EVERY iteration (break
                    # path AND sleep path), so a loop-local capture is never
                    # overwritten unemitted. None-guard: the common (no-gap)
                    # path allocates NOTHING and skips the flush call entirely.
                    if gap_evt is not None:
                        _flush_rate_logs(gap_evt)
                if wait <= 0.0:
                    # Same local ``wait`` the atomic (check, stamp) above keyed
                    # on — the decision is byte-identical; only the ``break``
                    # keyword moved outside the with-block.
                    break  # spaced — fall through to the governor + bucket
                iterations += 1
                if iterations > _SMOOTH_LOOP_CAP:
                    # fail-OPEN after the cap (mirrors the governor's loop cap): still
                    # stamp so the spacing baseline stays honest under a frozen clock.
                    with self._lock:
                        self._last_issue_ts = self._clock()
                    break
                # lock RELEASED before sleeping; re-test the spacing on the next pass.
                self._sleep(wait)
        # ── Gate 1: per-minute governor (skipped entirely when disabled). ──
        # The issue instant is RESERVED atomically under the SAME lock acquisition that
        # observes a free slot (DEC-GROWW-GOVERNOR-ATOMIC-001) — NOT after the bucket. The
        # old code checked ``len < cap`` and released the lock, then appended only AFTER
        # ``bucket.acquire()``, so N concurrent workers could ALL pass a single free slot
        # (each saw ``len = cap-1`` before any appended) and then each append → overshoot by
        # ``N-1``. Reserving in the same critical section that saw room closes that gap: the
        # ``(check, append)`` is now indivisible, so the deque can never exceed the cap. The
        # instant is recorded at admission (a few ms before the per-second bucket clears);
        # for a HARD cap recording slightly early is conservative (never under-counts).
        if self._ceiling_per_min is not None:
            iterations = 0
            while True:
                # Mirrors gate 0: ``gov_gap_evt`` keeps its None initializer (read in
                # the finally on an in-lock exception — forced by the raising-clock
                # test, ``""`` mutant suite-killed; :data:`_PendingLogSlot`);
                # ``gov_wait`` has NO initializer — the in-lock assignment dominates
                # every read (CodeQL py/multiple-definition on the dead
                # ``gov_wait = 0.0``).
                gov_gap_evt: _PendingLogSlot = None
                try:
                    with self._lock:
                        now = self._clock()
                        gov_gap_evt = self._handle_clock_gap_locked(now)
                        gov_wait = self._governor_wait_locked(now)
                        if gov_wait <= 0.0:
                            # Reserve the slot atomically, in the SAME lock
                            # acquisition that observed it free
                            # (DEC-GROWW-GOVERNOR-ATOMIC-001 — untouched).
                            self._issues.append(now)
                finally:
                    # Emit AFTER releasing the lock (DEC-RATE-LOG-OUTSIDE-LOCK-001);
                    # mirrors gate 0 — every iteration flushes, None-guarded.
                    if gov_gap_evt is not None:
                        _flush_rate_logs(gov_gap_evt)
                if gov_wait <= 0.0:
                    # Same local the atomic (check, reserve) above keyed on —
                    # only the ``break`` keyword moved outside the with-block.
                    break  # slot reserved — fall through to the token bucket
                iterations += 1
                if iterations > _GOVERNOR_LOOP_CAP:
                    # fail-OPEN after the cap (mirrors the bucket's acquire cap): still
                    # record the issue so the rolling count stays honest (matches the
                    # pre-fix fail-open, which also recorded the instant).
                    with self._lock:
                        self._issues.append(self._clock())
                    break
                # lock RELEASED before sleeping; re-evict + re-test on the next pass.
                self._sleep(gov_wait)
        # ── Gate 2: per-second token bucket (two-phase: copy ref under lock, sleep
        #    outside it). ──
        with self._lock:
            bucket = self._bucket
        bucket.acquire()
        # Observe-only trailing-hour issue log (DEC-RATE-MONITOR-EXPAND-001): the per-second
        # bucket has now cleared, so THIS is the single true "a request is being issued"
        # point — covering BOTH the governor-on and governor-off paths with one record (the
        # governor's own _issues deque is only stamped when a governor is wired). Stamping it
        # here, never read by a pacing gate, feeds snapshot()'s real req/sec·min·10min·hour
        # evidence counts. O(1) amortized under a short lock.
        with self._lock:
            self._record_issue_locked(self._clock())
        # Liveness beat (DEC-PULL-CONTENTION-ANTISTALL-001 GAP #15): a pull pacing
        # through the per-minute governor / token bucket IS making progress — beat the
        # watchdog so a slow-but-progressing pull (heavily throttled, but advancing) is
        # never falsely declared hung. A no-op when no watchdog is registered (every
        # non-dashboard caller); best-effort, never raises into the pacer.
        emit_progress_beat()
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
        # Read in the finally on an in-lock exception (forced by the raising
        # gap-handler test, ``""`` mutant suite-killed; :data:`_PendingLogSlot`).
        gap_evt: _PendingLogSlot = None
        try:
            with self._lock:
                # Re-base on a sleep/wake gap FIRST (DEC-RATE-CLOCK-SLEEPWAKE-001) so even a
                # default controller — no governor, no smooth pacing, whose ``pace`` reads
                # the clock under lock only here — forgets stale AIMD anchors on wake. The
                # gap reset sets ``_last_snapshot_log = now``, so the throttle check below
                # then correctly suppresses an immediate post-wake snapshot.
                gap_evt = self._handle_clock_gap_locked(now)
                # ``due`` is ONE boolean expression branched on twice — reshaped
                # from a ``due = False`` flag + in-branch ``due = True`` whose
                # ``False → None`` assignment mutant was falsy-EQUIVALENT
                # (unkillable; DEC-MUTATION-RUNTIME-003 fixer round). Every
                # mutant of this shape is behavior-observable: an always-None
                # ``due`` never logs a snapshot, and the arithmetic/boundary
                # mutants shift the throttle window (both pinned by the
                # throttle tests).
                due = now - self._last_snapshot_log >= SNAPSHOT_LOG_INTERVAL_S
                if due:
                    self._last_snapshot_log = now
        finally:
            # Emit AFTER releasing the lock (DEC-RATE-LOG-OUTSIDE-LOCK-001). Per-thread
            # order kept: clock_gap → snapshot. The gap event now survives the
            # throttled early-return path too (it previously emitted under the lock
            # before the throttle ``return``).
            if gap_evt is not None:
                _flush_rate_logs(gap_evt)
        if not due:
            return
        # Already outside the lock — snapshot() re-acquires it SEQUENCED, never nested.
        _LOG.info("rate.snapshot", **self.snapshot())

    def on_rate_limit(self) -> None:
        """Record a rate-limit hit; step **down** 1 r/s on sustained pressure — O(1).

        Thread-safe: the whole hit-record → window-evict → maybe-step-down is one
        atomic O(1) critical section, so two workers reporting a 429 at once can
        never double-step or corrupt the deque. The ``set_rate`` mutation happens
        under the lock but only re-stamps the bucket's in-memory rate/token state
        (no I/O / sleep), keeping the single shared token stream intact. Field
        values are captured at the decision point; telemetry is emitted
        post-release (DEC-RATE-LOG-OUTSIDE-LOCK-001).
        """
        # The first three slots are unconditionally overwritten in the lock but
        # their initializers are LIVE on the in-lock-exception path (the finally
        # flushes them) — forced by the raising-clock test, so every ``None →
        # ""`` mutant here is suite-killed (:data:`_PendingLogSlot`).
        # ``step_evt`` is CONDITIONALLY assigned: on the no-step path its
        # initializer reaches ``_flush_rate_logs`` on the plain tested path too.
        gap_evt: _PendingLogSlot = None
        hit_evt: _PendingLogSlot = None
        latch_evt: _PendingLogSlot = None
        step_evt: _PendingLogSlot = None
        try:
            with self._lock:
                now = self._clock()
                gap_evt = self._handle_clock_gap_locked(now)
                self._hits.append(now)
                cutoff = now - STEP_DOWN_WINDOW
                while self._hits and self._hits[0] < cutoff:
                    self._hits.popleft()
                # Captured at the pre-change emission point — post-append/evict,
                # PRE-latch, PRE-stepdown, PRE-``_hits.clear()``: at the step
                # threshold this still reads e.g. current_rps=5.0, recent_hits=3
                # even though the method returns with _rps == 4.0 and _hits empty.
                hit_evt = (
                    "warning",
                    "rate.429",
                    {"current_rps": self._rps, "recent_hits": len(self._hits)},
                )
                # ADAPTIVE smooth-pacing auto-latch (DEC-SMOOTH-PACING-AUTOLATCH-001):
                # BEFORE the ladder below mutates ``_rps``, check the trailing per-1s
                # issue buckets for a burst above the then-current allowed rate — the
                # evidence that OUR sub-second micro-burst (not a plain vendor quota
                # squeeze) provoked this 429 — and one-way latch the smooth gate ON.
                # A no-op in ON (already latched) / OFF (latch disabled) modes and once
                # latched. The latch's STATE mutation runs under the controller lock
                # (no torn state with the token math), but ``pace()``'s gate-0 check
                # (``if self._smooth_pacing:``) reads the flag OUTSIDE the lock —
                # honest envelope: a worker already past (or concurrently reading)
                # that check may issue ONE more unsmoothed request after the flip.
                # Benign: the flag is a one-way GIL-atomic bool transition, bounded
                # at one unsmoothed issue per in-flight worker, and every later
                # ``pace()`` observes the latched state.
                latch_evt = self._maybe_latch_smooth_locked(now)
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
                    # Capture the EFFECTIVE floor for THIS rung: a soft-floor step
                    # (still above 3) heads toward FLOOR_RPS (3); a sustained-storm
                    # step below the soft floor heads toward the deep
                    # RATE_LIMIT_STORM_FLOOR_RPS (1).
                    step_evt = (
                        "warning",
                        "rate.stepdown",
                        {
                            "from_rps": old_rps,
                            "to_rps": self._rps,
                            "floor_rps": FLOOR_RPS
                            if above_soft_floor
                            else RATE_LIMIT_STORM_FLOOR_RPS,
                        },
                    )
        finally:
            # Positional order == the pre-change in-lock emission order:
            # gap → 429 → latch → stepdown (DEC-RATE-LOG-OUTSIDE-LOCK-001).
            _flush_rate_logs(gap_evt, hit_evt, latch_evt, step_evt)

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
        in-memory rate/token state (no I/O / sleep under the lock). Field values
        are captured at the decision point; telemetry is emitted post-release
        (DEC-RATE-LOG-OUTSIDE-LOCK-001).
        """
        # ``gap_evt``/``to_evt``: initializers LIVE on the in-lock-exception
        # path (the finally flushes them) — forced by the raising-clock test,
        # ``""`` mutants suite-killed (:data:`_PendingLogSlot`); ``step_evt``:
        # conditionally assigned — its ``""`` mutant reaches the flush on the
        # plain no-step path too.
        gap_evt: _PendingLogSlot = None
        to_evt: _PendingLogSlot = None
        step_evt: _PendingLogSlot = None
        try:
            with self._lock:
                now = self._clock()
                gap_evt = self._handle_clock_gap_locked(now)
                self._timeouts.append(now)
                cutoff = now - STEP_DOWN_WINDOW
                while self._timeouts and self._timeouts[0] < cutoff:
                    self._timeouts.popleft()
                # Captured at the pre-change emission point — post-append/evict,
                # PRE-stepdown, PRE-``_timeouts.clear()``.
                to_evt = (
                    "warning",
                    "rate.timeout",
                    {"current_rps": self._rps, "recent_timeouts": len(self._timeouts)},
                )
                if len(self._timeouts) >= STEP_DOWN_TIMEOUT_HITS and self._rps > TIMEOUT_FLOOR_RPS:
                    old_rps = self._rps
                    self._rps -= STEP_RPS
                    self._bucket.set_rate(self._rps)
                    self._timeouts.clear()
                    self._last_step = now
                    step_evt = (
                        "warning",
                        "rate.stepdown",
                        {
                            "from_rps": old_rps,
                            "to_rps": self._rps,
                            "floor_rps": TIMEOUT_FLOOR_RPS,
                            "cause": "timeout",
                        },
                    )
        finally:
            # Positional order == the pre-change in-lock emission order:
            # gap → timeout → stepdown (DEC-RATE-LOG-OUTSIDE-LOCK-001).
            _flush_rate_logs(gap_evt, to_evt, step_evt)

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
        trickle-trap this closes. Field values are captured at the decision
        point; telemetry is emitted post-release (DEC-RATE-LOG-OUTSIDE-LOCK-001).
        """
        # ``gap_evt``: initializer LIVE on the in-lock-exception path (the
        # finally flushes it) — forced by the raising-clock test, ``""`` mutant
        # suite-killed (:data:`_PendingLogSlot`); ``step_evt``: conditionally
        # assigned — its ``""`` mutant reaches the flush on the plain no-step
        # path too.
        gap_evt: _PendingLogSlot = None
        step_evt: _PendingLogSlot = None
        try:
            with self._lock:
                now = self._clock()
                gap_evt = self._handle_clock_gap_locked(now)
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
                    step_evt = (
                        "info",
                        "rate.stepup",
                        {
                            "from_rps": old_rps,
                            "to_rps": self._rps,
                            "ceiling_rps": self._ceiling,
                        },
                    )
        finally:
            # Positional order == the pre-change in-lock emission order:
            # gap → stepup (DEC-RATE-LOG-OUTSIDE-LOCK-001). The common (hot,
            # per-clean-call) path has both slots None — one varargs tuple + two
            # pointer checks, transient (the stress memory-flat gate pins this).
            _flush_rate_logs(gap_evt, step_evt)

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
            now = self._clock()
            # Evict aged-out issue instants first so ``recent_minute`` is an accurate
            # rolling-60s gauge (DEC-GROWW-RATE-PERMIN-001). No-op when disabled (the
            # deque is always empty in that case).
            if self._ceiling_per_min is not None:
                while self._issues and (self._issues[0] + PER_MIN_WINDOW) - now <= 0.0:
                    self._issues.popleft()
            # OBSERVE-ONLY trailing-window evidence counts (DEC-RATE-MONITOR-EXPAND-001): the
            # REAL integer request count in the trailing 1 s / 60 s / 600 s / 3600 s, summed
            # from the fixed per-second issue ring. The operator can verify rps == recent_1s,
            # req/min == recent_60s, req/10min == recent_600s, req/hour == recent_3600s —
            # every rate is a genuine windowed count of issues, not a per-second figure
            # multiplied up. ``recent_60s`` approximates ``recent_minute`` (to within one
            # boundary second — the ring is integer-second-binned, the deque uses exact float
            # instants) when a governor is wired (two independent sources, a built-in
            # cross-check). Each count first rolls the
            # ring forward to ``now`` so idle seconds read as empty, then sums its window.
            snap: dict[str, object] = {
                "current_rps": self._rps,
                "recent_hits": len(self._hits),
                "recent_timeouts": len(self._timeouts),
                "recent_minute": len(self._issues),
                "ceiling_per_min": self._ceiling_per_min,
                "recent_1s": self._count_in_window_locked(now, LOG_WINDOW_1S),
                "recent_60s": self._count_in_window_locked(now, LOG_WINDOW_60S),
                "recent_600s": self._count_in_window_locked(now, LOG_WINDOW_600S),
                "recent_3600s": self._count_in_window_locked(now, LOG_WINDOW_3600S),
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
    "CLOCK_GAP_THRESHOLD_S",
    "FLOOR_RPS",
    "LOG_WINDOW_1S",
    "LOG_WINDOW_60S",
    "LOG_WINDOW_600S",
    "LOG_WINDOW_3600S",
    "PER_MIN_WINDOW",
    "RATE_LIMIT_STORM_FLOOR_RPS",
    "SMOOTH_LATCH_SCAN_SECONDS",
    "SMOOTH_PACING_ADAPTIVE",
    "SMOOTH_PACING_OFF",
    "SMOOTH_PACING_ON",
    "SNAPSHOT_LOG_INTERVAL_S",
    "STEP_DOWN_HITS",
    "STEP_DOWN_TIMEOUT_HITS",
    "STEP_DOWN_WINDOW",
    "STEP_RPS",
    "STEP_UP_WINDOW",
    "STORM_HITS",
    "TIMEOUT_FLOOR_RPS",
    "TRAILING_LOG_BUCKETS",
    "TRAILING_LOG_WINDOW_S",
    "RateController",
]
