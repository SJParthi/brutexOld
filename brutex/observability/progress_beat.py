"""Process-global liveness-beat seam — defence-in-depth for the pull watchdog.

DEC-PULL-CONTENTION-ANTISTALL-001. The :class:`~brutex.dashboard.pull_stall.PullWatchdog`
declares a pull worker HUNG when no heartbeat lands for the audited timeout. Its real
heartbeat (:meth:`brutex.dashboard.pull_jobs.BackgroundPullJob._on_progress`) fires only
on a COMPLETED window. A pull that is genuinely PROGRESSING but momentarily slow — e.g. a
fan-out worker repeatedly retrying a non-blocking partition ``flock`` against a busy peer,
or pacing through the per-minute rate governor — produces NO completed-window beat for
that interval, so a long-enough slow patch could FALSELY trip the watchdog.

This module is the thin, decoupled seam that closes that false-positive: the hot
liveness paths (the bounded partition lock-wait poll
:func:`brutex.data.partition_lock.partition_write_lock`, and the rate controller's
:meth:`brutex.adapters.rate_common.RateController.pace` return) call
:func:`emit_progress_beat` after each poll / pace. When a watchdog has
:func:`register_progress_beat`-ed its beat callback (the dashboard does so for the
lifetime of a run), that beat fires — so a slow-but-progressing pull keeps its heartbeat
alive and is NEVER falsely killed. When nothing is registered (a CLI dry-run, a unit
test, any non-dashboard caller) :func:`emit_progress_beat` is a cheap no-op.

Design (COMMON · decoupled · best-effort · O(1)):

* **One source of truth.** The hot paths import ONLY :func:`emit_progress_beat`; they
  know nothing about the watchdog. The watchdog (the dashboard layer) installs its beat
  via :func:`register_progress_beat` and removes it via :func:`unregister_progress_beat`,
  so there is no import edge from the lake / rate layers up into the dashboard.
* **Process-global single beat — and that is CORRECT.** A pull fans out across worker
  threads, and the lock-wait / pace runs on those fan-out workers (NOT the control
  thread that registered the beat); a process-global beat reaches them all. The
  dashboard pull is SINGLE-FLIGHT (one pull at a time), so there is never a second beat
  to confuse — and a beat is pure liveness (it carries no pull data and gates nothing),
  so even a stray beat could only KEEP A HEARTBEAT ALIVE, never corrupt anything.
  Guarded by one lock; the underlying :meth:`PullWatchdog.beat` is itself thread-safe.
* **Best-effort.** A raising beat callback is swallowed + logged so a buggy beat can
  NEVER crash the fetch worker, the lock-wait, or the pacer (Golden Rule #10). The beat
  is additive liveness signal, never a gate.
* **No global mutable engine state.** This is observability-only liveness plumbing; it
  carries no pull data and influences no stored bytes (Rule #1 / #7 untouched).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

logger = logging.getLogger(__name__)

#: A liveness beat — fired by the hot paths, supplied by a watchdog. Takes no args.
ProgressBeat = Callable[[], None]

#: The single registered process-global beat (``None`` when nothing is registered).
#: Guarded by :data:`_BEAT_LOCK`.
_BEAT: ProgressBeat | None = None

#: Guards every read / mutation of :data:`_BEAT` — register / unregister run on the
#: dashboard control thread, while emit runs on the fetch worker threads.
_BEAT_LOCK = threading.Lock()


def register_progress_beat(beat: ProgressBeat) -> None:
    """Register *beat* as the process-global liveness beat (last write wins). O(1).

    The dashboard pull worker registers its watchdog's ``beat`` for the lifetime of a
    run so the hot liveness paths (lock-wait / pace, on any fan-out worker thread) keep
    the heartbeat alive. Single-flight guarantees one pull, so one beat at a time.
    """
    global _BEAT  # noqa: PLW0603 — the seam's single registered beat, lock-guarded
    with _BEAT_LOCK:
        _BEAT = beat


def unregister_progress_beat() -> None:
    """Clear the process-global liveness beat (idempotent). O(1).

    Called from the pull worker's ``finally`` so a finished run leaves no stale beat that
    a later poll could fire into a dead watchdog.
    """
    global _BEAT  # noqa: PLW0603 — the seam's single registered beat, lock-guarded
    with _BEAT_LOCK:
        _BEAT = None


def emit_progress_beat() -> None:
    """Fire the registered liveness beat, if one is installed — best-effort. O(1).

    The hot liveness paths (the bounded partition lock-wait poll, the rate-controller
    ``pace`` return) call this after each poll / pace. A no-op when no beat is registered
    (every non-dashboard caller). A raising beat is swallowed + logged so it can never
    crash the worker that is merely signalling liveness (Golden Rule #10).
    """
    with _BEAT_LOCK:
        beat = _BEAT
    if beat is None:
        return
    try:
        beat()
    except Exception:  # pylint: disable=broad-exception-caught — best-effort liveness beat
        logger.exception("progress_beat.emit failed; the worker is unaffected")


__all__ = [
    "ProgressBeat",
    "emit_progress_beat",
    "register_progress_beat",
    "unregister_progress_beat",
]
