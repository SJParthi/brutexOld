"""Process-global liveness-beat seam — defence-in-depth for the job watchdogs.

DEC-PULL-CONTENTION-ANTISTALL-001 (single-slot origin) + the MULTI-REGISTRANT
registry of DEC-GREEKS-BULK-REENRICH-001 fix round 3. The
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
:func:`emit_progress_beat` after each poll / pace / unit. Every watchdog that
has :func:`register_progress_beat`-ed its beat callback receives it — so a
slow-but-progressing job keeps its heartbeat alive and is NEVER falsely
killed. When nothing is registered (a CLI dry-run, a unit test, any
non-dashboard caller) :func:`emit_progress_beat` is a cheap no-op.

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
  (refuter-confirmed, fix round 3). The registry now holds EVERY registered
  beat and :func:`emit_progress_beat` fires them ALL: a beat is pure liveness
  (it carries no job data and gates nothing), so a beat reaching a sibling
  job's watchdog can only KEEP A HEARTBEAT ALIVE, never corrupt anything.
  Honest envelope (Golden Rule #10): the broadcast means one job's beats can
  mask a genuinely-wedged SIBLING's silence at the HUNG stage — accepted
  because the terminal WEDGED verdict additionally requires the lake-revision
  DUAL signal (DEC-PULL-WEDGE-ESCALATE-001), and a stray liveness beat was
  the module's sanctioned failure direction from day one. A pull still fans
  out across worker threads and the registry reaches them all (guarded by
  one lock; the underlying :meth:`PullWatchdog.beat` is itself thread-safe).
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


def register_progress_beat(beat: ProgressBeat) -> ProgressBeatHandle:
    """Register *beat* as A process-global liveness beat; return its handle. O(1).

    Each dashboard job worker registers its watchdog's ``beat`` for the
    lifetime of a run so the hot liveness paths (lock-wait / pace / per-unit
    seams, on any fan-out worker thread) keep that watchdog's heartbeat alive.
    Registrations are INDEPENDENT — a second concurrent job (the pull + the
    Greeks backfill run side by side by design) adds its own entry and can
    never displace this one. The caller MUST keep the returned handle and pass
    it to :func:`unregister_progress_beat` in its ``finally``.
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


def emit_progress_beat() -> None:
    """Fire EVERY registered liveness beat — best-effort. O(registrants).

    The hot liveness paths (the bounded partition lock-wait poll, the
    rate-controller ``pace`` return, the per-unit scan/enrich/prebuild seams)
    call this after each poll / pace / unit. A no-op when nothing is
    registered (every non-dashboard caller). A beat is pure liveness, so
    broadcasting to every registered watchdog is safe by construction (see the
    module docstring); a raising beat is swallowed + logged — and the
    remaining beats still fire — so it can never crash the worker that is
    merely signalling liveness (Golden Rule #10).
    """
    with _BEAT_LOCK:
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
    precedent).
    """
    with _BEAT_LOCK:
        _BEATS.clear()


__all__ = [
    "ProgressBeat",
    "ProgressBeatHandle",
    "emit_progress_beat",
    "register_progress_beat",
    "reset_progress_beats_for_tests",
    "unregister_progress_beat",
]
