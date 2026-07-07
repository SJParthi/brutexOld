"""Process-global liveness-beat seam — defence-in-depth for the job watchdogs.

DEC-PULL-CONTENTION-ANTISTALL-001 (single-slot origin) + the MULTI-REGISTRANT
registry of DEC-GREEKS-BULK-REENRICH-001 fix round 3 + the JOB-SCOPED beat
routing of fix round 4. The
:class:`~brutex.dashboard.pull_stall.PullWatchdog` declares a job worker HUNG
when no heartbeat lands for the audited timeout. Its real heartbeat
(:meth:`brutex.dashboard.pull_jobs.BackgroundPullJob._on_progress`) fires only
on a COMPLETED unit. A worker that is genuinely PROGRESSING but momentarily
slow — e.g. a fan-out worker repeatedly retrying a non-blocking partition
``flock`` against a busy peer, pacing through the per-minute rate governor, or
walking an O(files) scan — produces NO completed-unit beat for that interval,
so a long-enough slow patch could FALSELY trip the watchdog.

This module is the thin, decoupled seam that closes that false-positive: the
hot liveness paths (the bounded partition lock-wait poll
:func:`brutex.data.partition_lock.partition_write_lock`, the rate controller's
:meth:`brutex.adapters.rate_common.RateController.pace` return, the
greeks-enrich / prebuild / movers-warm / backfill-scan per-unit seams) call
:func:`emit_progress_beat` after each poll / pace / unit. When nothing is
registered (a CLI dry-run, a unit test, any non-dashboard caller)
:func:`emit_progress_beat` is a cheap no-op.

Design (COMMON · decoupled · best-effort · O(registrants)):

* **One source of truth.** The hot paths import ONLY :func:`emit_progress_beat`;
  they know nothing about the watchdogs. A watchdog owner (the dashboard job
  layer) installs its beat via :func:`register_progress_beat` — which returns
  an opaque :class:`ProgressBeatHandle` — and removes EXACTLY its own
  registration via :func:`unregister_progress_beat`, so there is no import
  edge from the lake / rate layers up into the dashboard.
* **Process-global MULTI-registrant registry — because one job is no longer
  guaranteed.** The original single-slot design leaned on "the dashboard pull
  is SINGLE-FLIGHT, so there is never a second beat to confuse" — an
  invariant DEC-GREEKS-BULK-REENRICH-001 broke: ``build_app`` now owns TWO
  :class:`~brutex.dashboard.pull_jobs.BackgroundPullJob` instances (the pull
  + the Greeks backfill), and a DRY-RUN backfill is sanctioned to run
  CONCURRENTLY with a real pull (``cross_process_lock=False``). Under the
  single slot the second job's register CLOBBERED the first's beat (its
  liveness fed the WRONG watchdog → a false CRITICAL "pull halted" alert on
  a healthy run) and whichever job finished first CLEARED the slot outright,
  stripping the survivor's only defence-in-depth liveness source
  (refuter-confirmed, fix round 3). The registry holds EVERY registered beat.
* **JOB-SCOPED routing (fix round 4, refuter-confirmed).** The naive round-3
  broadcast ("fire EVERY registered beat") introduced a FALSE-NEGATIVE: a
  concurrent dry-run backfill's O(files) scan beats — emitted per contract /
  per month file on ITS OWN job thread — kept a genuinely-WEDGED live pull's
  watchdog HEALTHY for the whole ~30 min scan, suppressing the HUNG edge and
  with it the STALLED-SUSPECT marker, the CRITICAL alert, AND the terminal
  WEDGED verdict (the DEC-PULL-WEDGE-ESCALATE-001 dual signal is gated ON the
  HUNG edge, so it was never even consulted — the round-3 docstring's "the
  WEDGED verdict still requires the lake-revision DUAL signal" mitigation
  claim was VACUOUS and is hereby retracted). Each job worker therefore binds
  its own registration as the THREAD-scoped beat target
  (:func:`bind_progress_beat_scope`, a :mod:`contextvars` var): a beat emitted
  on a job's OWN thread (the backfill scan, the prebuild driver loop, the
  movers warm) is delivered ONLY to that job's watchdog — one job's
  synchronous phases can no longer mask a wedged sibling in EITHER direction.
* **Unscoped emitters still broadcast — fail-open, honestly bounded.** Beats
  emitted on threads that carry NO scope (the fetch pipeline's fan-out worker
  threads firing the lock-wait / pace / reauth seams, the prebuild pool
  workers — threads the spine creates far below the dashboard, where no job
  token can be plumbed without the #1022-rejected multi-layer threading) are
  delivered to EVERY registrant: a beat must never be LOST to its own
  watchdog (losing it would resurrect the round-3 false-CRITICAL class, the
  worse failure direction). Honest envelope (Golden Rule #10): in that
  unscoped direction a running job's fan-out beats CAN still delay a wedged
  sibling's HUNG edge — and therefore its CRITICAL alert AND the WEDGED
  verdict, since HUNG is their precondition — for the emitter's lifetime; the
  wedge dual signal does NOT mitigate this (it is only evaluated after the
  HUNG edge). Detection resumes when the emitting job unregisters in its
  ``finally``.
* **Best-effort.** A raising beat callback is swallowed + logged — and never
  prevents the OTHER registered beats from firing — so a buggy beat can NEVER
  crash the fetch worker, the lock-wait, or the pacer (Golden Rule #10). The
  beat is additive liveness signal, never a gate.
* **No global mutable engine state.** This is observability-only liveness
  plumbing; it carries no pull data and influences no stored bytes
  (Rule #1 / #7 untouched).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from contextvars import ContextVar, Token
from typing import Final

logger = logging.getLogger(__name__)

#: A liveness beat — fired by the hot paths, supplied by a watchdog. Takes no args.
ProgressBeat = Callable[[], None]


class ProgressBeatHandle:
    """Opaque registration token returned by :func:`register_progress_beat`.

    Identity-keyed (no payload): each ``register`` mints a fresh handle, so the
    SAME beat callable registered twice yields two independent registrations
    and :func:`unregister_progress_beat` can only ever remove the caller's own
    — a job finishing can never clear a concurrent sibling job's beat (the
    fix-round-3 clobber regression).
    """

    __slots__ = ()


#: Every registered process-global beat, keyed by its opaque handle (insertion-
#: ordered — emit fires in registration order). Guarded by :data:`_BEAT_LOCK`.
_BEATS: dict[ProgressBeatHandle, ProgressBeat] = {}

#: Guards every read / mutation of :data:`_BEATS` — register / unregister run on
#: the job control threads, while emit runs on the fetch worker threads.
_BEAT_LOCK = threading.Lock()

#: The emitting context's OWN registration (fix round 4): bound by a job worker
#: on ITS thread for the run's lifetime, so a beat emitted there routes ONLY to
#: that job's watchdog. ``None`` (every fan-out / non-job thread) = broadcast.
_BEAT_SCOPE: Final[ContextVar[ProgressBeatHandle | None]] = ContextVar(
    "progress_beat_scope", default=None
)


def register_progress_beat(beat: ProgressBeat) -> ProgressBeatHandle:
    """Register *beat* as A process-global liveness beat; return its handle. O(1).

    Each dashboard job worker registers its watchdog's ``beat`` for the
    lifetime of a run so the hot liveness paths (lock-wait / pace / per-unit
    seams, on any fan-out worker thread) keep that watchdog's heartbeat alive.
    Registrations are INDEPENDENT — a second concurrent job (the pull + the
    Greeks backfill run side by side by design) adds its own entry and can
    never displace this one. The caller MUST keep the returned handle and pass
    it to :func:`unregister_progress_beat` in its ``finally`` — and SHOULD
    additionally bind it on its worker thread via
    :func:`bind_progress_beat_scope` so the thread's own beats never feed a
    sibling's watchdog (fix round 4).
    """
    handle = ProgressBeatHandle()
    with _BEAT_LOCK:
        _BEATS[handle] = beat
    return handle


def unregister_progress_beat(handle: ProgressBeatHandle) -> None:
    """Remove exactly the registration behind *handle* (idempotent). O(1).

    Called from a job worker's ``finally`` so a finished run leaves no stale
    beat that a later poll could fire into a dead watchdog. Removes ONLY the
    caller's own entry — a concurrently-running sibling job's beat is
    untouched (the single-slot design's unconditional clear stripped the
    survivor's liveness; refuter-confirmed, fix round 3). An unknown /
    already-removed handle is a no-op.
    """
    with _BEAT_LOCK:
        _BEATS.pop(handle, None)


def bind_progress_beat_scope(handle: ProgressBeatHandle) -> Token[ProgressBeatHandle | None]:
    """Make *handle* the CURRENT thread/context's beat target; return the token.

    Fix round 4 (refuter-confirmed): a job worker binds its own registration
    on its worker thread right after :func:`register_progress_beat`, so every
    beat the run emits SYNCHRONOUSLY on that thread (the backfill's O(files)
    scan, the prebuild driver loop, …) is delivered ONLY to its own watchdog —
    never to a concurrently-running sibling's, whose genuine wedge it would
    otherwise mask through the whole phase. Threads the run fans out to do NOT
    inherit the scope (:mod:`contextvars` do not propagate into pool worker
    threads) and keep the fail-open broadcast — see the module docstring's
    honest envelope. The caller MUST reset via
    :func:`unbind_progress_beat_scope` in the same ``finally`` that
    unregisters (Token-based restore — nesting-safe for an inline/sync
    spawner running one job inside another's thread). O(1).
    """
    return _BEAT_SCOPE.set(handle)


def unbind_progress_beat_scope(token: Token[ProgressBeatHandle | None]) -> None:
    """Restore the scope bound before the matching :func:`bind_progress_beat_scope`.

    Token-based (LIFO) restore, so an inline-spawned nested job hands the
    thread back to its outer job's scope exactly. O(1).
    """
    _BEAT_SCOPE.reset(token)


def emit_progress_beat() -> None:
    """Fire the emitting context's OWN beat — or broadcast when unscoped. O(registrants).

    The hot liveness paths (the bounded partition lock-wait poll, the
    rate-controller ``pace`` return, the per-unit scan/enrich/prebuild seams)
    call this after each poll / pace / unit. Routing (fix round 4):

    * a SCOPED context (a job's own worker thread, bound via
      :func:`bind_progress_beat_scope`) delivers ONLY to that job's
      registration — a sibling job's watchdog never sees it, so a long
      synchronous phase (the backfill scan) can no longer mask a genuinely
      wedged sibling; a scoped beat whose registration is already gone (the
      run finished) is delivered to NOBODY — its dying breaths must not feed
      siblings either;
    * an UNSCOPED context (fan-out worker threads, any non-job caller) keeps
      the fail-open broadcast to every registrant — a beat must never be lost
      to its own watchdog (the round-3 false-CRITICAL class).

    A no-op when nothing is registered (every non-dashboard caller). A raising
    beat is swallowed + logged — and the remaining beats still fire — so it
    can never crash the worker that is merely signalling liveness (Golden
    Rule #10).
    """
    scope = _BEAT_SCOPE.get()
    with _BEAT_LOCK:
        if scope is not None:
            own = _BEATS.get(scope)
            beats = [own] if own is not None else []
        else:
            beats = list(_BEATS.values())
    for beat in beats:
        try:
            beat()
        except Exception:  # pylint: disable=broad-exception-caught — best-effort liveness beat
            logger.exception("progress_beat.emit failed; the worker is unaffected")


def reset_progress_beats_for_tests() -> None:
    """Clear EVERY registered beat — a TEST-ONLY isolation seam. O(1).

    Production code never calls this (each job removes only its own handle);
    test fixtures use it to guarantee a clean registry across tests regardless
    of what a prior test leaked (the :func:`reset_notifier_for_tests`
    precedent). The thread-local scope var is NOT touched (it dies with the
    binding thread; tests bind/reset it via the token API).
    """
    with _BEAT_LOCK:
        _BEATS.clear()


__all__ = [
    "ProgressBeat",
    "ProgressBeatHandle",
    "bind_progress_beat_scope",
    "emit_progress_beat",
    "register_progress_beat",
    "reset_progress_beats_for_tests",
    "unbind_progress_beat_scope",
    "unregister_progress_beat",
]
