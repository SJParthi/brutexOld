"""Pull-id-keyed liveness-beat seam — defence-in-depth for the pull watchdog.

DEC-PULL-CONTENTION-ANTISTALL-001 (the seam) + DEC-WATCHDOG-PERRUN-BEAT-001 /
DEC-PULL-BEAT-PERRUN-001 (per-run channels). The
:class:`~brutex.dashboard.pull_stall.PullWatchdog` declares a pull worker HUNG when no
heartbeat lands for the audited timeout. Its real heartbeat
(:meth:`brutex.dashboard.pull_jobs.BackgroundPullJob._on_progress`) fires only on a
COMPLETED window. A pull that is genuinely PROGRESSING but momentarily slow — e.g. a
fan-out worker repeatedly retrying a non-blocking partition ``flock`` against a busy
peer, or pacing through the per-minute rate governor — produces NO completed-window beat
for that interval, so a long-enough slow patch could FALSELY trip the watchdog.

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
* **Per-RUN keyed channels — attribution by the ambient ``pull_id``.** Beats are keyed
  by the run's 12-hex ``pull_id`` (:mod:`brutex.observability.correlation`): a production
  run registers its beat KEYED on its own run id, and :func:`emit_progress_beat` routes
  each beat by the emitting thread's ambient ``pull_id`` — so a foreign / unattributed
  in-process thread's beat can NEVER refresh a run it does not belong to and mask the
  ~600 s stall alarm. The :data:`LEGACY_CHANNEL` (``""``) is the unkeyed back-compat
  slot: a zero-arg register/emit pair behaves exactly as the pre-keyed seam did, and a
  KEYED emit that finds no keyed registrant falls back OUTWARD to the legacy channel
  (never the reverse — the anti-masking invariant: nothing ever routes INTO a keyed
  channel it was not addressed to). A beat during a keyed run that matches NO channel is
  DROPPED + WARNed once per register-epoch (``progress_beat.foreign_beat_dropped`` —
  Golden Rule #10 diagnosability for an unattributed emitter). Standing discipline: any
  NEW emit site running on a pool / consumer thread must copy the canonical
  capture-then-rebind pattern (``run_ingest.py`` ``_run_pair`` — capture
  ``current_pull_id()`` on the driver, re-bind ``pull_id_scope`` on the worker) or its
  beats DROP; the failure direction is a LOUD false stalled-SUSPECT, never a masked true
  stall.
* **Best-effort.** A raising beat callback is swallowed + logged so a buggy beat can
  NEVER crash the fetch worker, the lock-wait, or the pacer (Golden Rule #10). The beat
  is additive liveness signal, never a gate.
* **No global mutable engine state.** This is observability-only liveness plumbing; it
  carries no pull data and influences no stored bytes (Rule #1 / #7 untouched). The
  callback is invoked OUTSIDE :data:`_BEAT_LOCK` (it does journal I/O and must never run
  under the registry lock).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Final

from brutex.observability.correlation import current_pull_id

logger = logging.getLogger(__name__)

#: A liveness beat — fired by the hot paths, supplied by a watchdog. Takes no args.
ProgressBeat = Callable[[], None]

#: The unkeyed back-compat channel: a zero-arg :func:`register_progress_beat` /
#: :func:`emit_progress_beat` pair rides it with the exact pre-keyed semantics, and a
#: KEYED emit that finds no keyed registrant falls back OUTWARD to it (never inward).
LEGACY_CHANNEL: Final[str] = ""

#: The registered beats, keyed by run ``pull_id`` (:data:`LEGACY_CHANNEL` = the unkeyed
#: slot). Guarded by :data:`_BEAT_LOCK`; last write wins WITHIN a channel.
_BEATS: dict[str, ProgressBeat] = {}

#: Register-epoch counter — bumped on every register / unregister / reset so the
#: foreign-beat drop WARN fires at most once per epoch (a new registration re-arms it).
#: Guarded by :data:`_BEAT_LOCK`.
_EPOCH: int = 0

#: The epoch the drop WARN last fired for (``-1`` = never). Guarded by :data:`_BEAT_LOCK`.
_WARNED_EPOCH: int = -1

#: Count of beats DROPPED in the current register-epoch (the once-per-epoch WARN alone
#: cannot convey magnitude — one stray beat vs a systematically-unattributed emitter
#: population). Flushed to a debug ``progress_beat.drop_summary`` line whenever the epoch
#: closes (register / unregister / reset). Guarded by :data:`_BEAT_LOCK`.
_DROPPED_IN_EPOCH: int = 0

#: Guards every read / mutation of :data:`_BEATS` / :data:`_EPOCH` / :data:`_WARNED_EPOCH`
#: — register / unregister run on the dashboard control thread, while emit runs on the
#: fetch worker threads.
_BEAT_LOCK = threading.Lock()


def _close_epoch_locked() -> int:
    """Bump the register-epoch + collect the closed epoch's drop count — lock held.

    The ONE place the epoch advances (register / unregister / reset all funnel here), so
    the per-epoch drop counter can never straddle two epochs. Returns the closed epoch's
    drop count so the caller can flush it to :func:`_log_drop_summary` OUTSIDE the lock.
    """
    global _EPOCH, _DROPPED_IN_EPOCH  # noqa: PLW0603 — the seam's epoch state, lock-guarded
    _EPOCH += 1
    dropped = _DROPPED_IN_EPOCH
    _DROPPED_IN_EPOCH = 0
    return dropped


def _log_drop_summary(dropped: int) -> None:
    """Debug-flush the closed epoch's drop tally (magnitude signal; 0 = silent)."""
    if dropped:
        logger.debug(
            "progress_beat.drop_summary — %d beat(s) dropped in the closed register-epoch "
            "(the once-per-epoch foreign_beat_dropped WARN carries the first drop only)",
            dropped,
        )


def register_progress_beat(beat: ProgressBeat, *, pull_id: str = LEGACY_CHANNEL) -> None:
    """Register *beat* on the *pull_id* channel (last write wins per channel). O(1).

    The dashboard pull worker registers its watchdog's beat KEYED on its own run id for
    the lifetime of a run, so the hot liveness paths (lock-wait / pace, on any fan-out
    worker thread carrying the run's re-bound ``pull_id``) keep the heartbeat alive —
    and a foreign thread's beat can never reach it. The zero-arg call registers on the
    :data:`LEGACY_CHANNEL` with the exact pre-keyed semantics (back-compat).

    A LIVE-channel collision — overwriting a DIFFERENT callback already registered on
    the same channel — is always a caller bug (two live runs sharing one ``pull_id``:
    while both live, the OLDER run's beats then route to the NEWER registrant — a
    residual cross-run mask this seam cannot discriminate, because the channel key IS
    the only identity). It is therefore WARNed loudly
    (``progress_beat.channel_collision``), never silent.
    """
    with _BEAT_LOCK:
        collided = pull_id in _BEATS and _BEATS[pull_id] is not beat
        _BEATS[pull_id] = beat
        dropped = _close_epoch_locked()
    _log_drop_summary(dropped)
    if collided:
        logger.warning(
            "progress_beat.channel_collision — channel %s was ALREADY registered by a "
            "different callback and has been overwritten (last write wins). Two LIVE runs "
            "sharing one pull_id is a caller bug: while both live, the older run's beats "
            "route to the NEWER registrant.",
            repr(pull_id),
        )


def unregister_progress_beat(
    *, pull_id: str = LEGACY_CHANNEL, beat: ProgressBeat | None = None
) -> None:
    """Clear the *pull_id* channel's beat (idempotent). O(1).

    Called from the pull worker's ``finally`` so a finished run leaves no stale beat that
    a later poll could fire into a dead watchdog. A keyed unregister removes ONLY its OWN
    channel — never a blanket clear — so a stale worker's unregister can never starve a
    NEWER run's beat (the latent race the single-slot seam carried).

    *beat* (optional) is an IDENTITY guard: when given, the channel is popped only if it
    still holds exactly *beat* — so a finished run whose channel was re-keyed by a
    duplicate-id survivor (see the collision WARN on :func:`register_progress_beat`) can
    never starve that still-live survivor. ``None`` keeps the unconditional pop
    (back-compat). A no-op (missing channel / identity mismatch) leaves the epoch — and
    thus the drop-WARN throttle — untouched.
    """
    with _BEAT_LOCK:
        current = _BEATS.get(pull_id)
        if current is None or (beat is not None and current is not beat):
            return  # nothing popped — no channel change, no epoch bump
        del _BEATS[pull_id]
        dropped = _close_epoch_locked()
    _log_drop_summary(dropped)


def reset_progress_beats() -> None:
    """Clear EVERY channel + re-arm the drop WARN — THE sanctioned test reset seam.

    O(registered channels) — the ``dict.clear`` walks every entry (trivially bounded:
    production holds at most a couple of live channels). Supersedes a bare
    :func:`unregister_progress_beat` for test fixtures: that clears only the legacy
    channel, so a keyed registration leaked by a test would bleed into the next one.
    Production never calls this (each run pops only its own channel).
    """
    global _WARNED_EPOCH  # noqa: PLW0603 — the sanctioned test reset, lock-guarded
    with _BEAT_LOCK:
        _BEATS.clear()
        dropped = _close_epoch_locked()
        _WARNED_EPOCH = -1
    _log_drop_summary(dropped)


def emit_progress_beat(pull_id: str | None = None) -> None:
    """Fire the beat registered for this context's run, if any — best-effort.

    O(1) in REGISTERED CHANNELS (a dict lookup, never a scan of the registry); the
    channel resolution itself costs one full contextvars-context copy + scan
    (:func:`~brutex.observability.correlation.current_pull_id` — linear in the emitting
    thread's BOUND contextvars, ≤ ~3 keys in this repo), skipped entirely on the
    empty-registry fast path below.

    The hot liveness paths (the bounded partition lock-wait poll, the rate-controller
    ``pace`` return) call this after each poll / pace. Routing: the channel is the
    explicit *pull_id* when given, else the emitting thread's ambient
    :func:`~brutex.observability.correlation.current_pull_id`. An exact keyed hit fires
    that run's beat; a keyed MISS falls back OUTWARD to the :data:`LEGACY_CHANNEL`
    registrant (never into another keyed channel — the anti-masking invariant). With an
    EMPTY registry this is a silent cheap no-op (every non-dashboard caller); with a
    non-empty registry an unroutable beat is DROPPED + counted, WARNed once per
    register-epoch (a foreign thread can never mask a real stall — it can only be
    loud; the closed epoch's drop tally is debug-flushed on the next
    register/unregister/reset). A raising beat is swallowed + logged so it can never
    crash the worker that is merely signalling liveness (Golden Rule #10).
    """
    global _WARNED_EPOCH, _DROPPED_IN_EPOCH  # noqa: PLW0603 — throttle stamps, lock-guarded
    # Empty-registry fast path (every CLI / unit-test caller): skip the contextvars
    # channel resolution entirely. The UNLOCKED truthiness read is a benign race — the
    # registry is best-effort liveness signal and a beat straddling a register boundary
    # is already tolerated (the register itself seeds the watchdog's heartbeat).
    if pull_id is None and not _BEATS:
        return
    # Resolve the channel OUTSIDE the lock — one contextvars-context copy + scan
    # (current_pull_id), no I/O.
    channel = pull_id if pull_id is not None else current_pull_id()
    warn = False
    channel_count = 0
    dropped_so_far = 0
    with _BEAT_LOCK:
        beat = _BEATS.get(channel)
        if beat is None and channel != LEGACY_CHANNEL:
            # Outward-only fallback: a keyed emit with no keyed registrant may reach the
            # legacy unkeyed slot (the preserved shared-channel back-compat), but an
            # unkeyed/foreign emit NEVER routes into a keyed run's channel.
            beat = _BEATS.get(LEGACY_CHANNEL)
        if beat is None:
            if not _BEATS:
                return  # empty registry — the explicit-pull_id silent no-op (no warn)
            _DROPPED_IN_EPOCH += 1
            dropped_so_far = _DROPPED_IN_EPOCH
            warn = _WARNED_EPOCH != _EPOCH
            if warn:
                _WARNED_EPOCH = _EPOCH
            channel_count = len(_BEATS)
    if beat is None:
        if warn:
            logger.warning(
                "progress_beat.foreign_beat_dropped — a beat from channel %s matched no "
                "registered run (%d registered channel(s), %d drop(s) this epoch); the "
                "emitting thread is missing the capture-then-rebind pull_id binding. "
                "Dropped (never routed into a foreign run — anti-masking); worst case is "
                "a loud false stalled-SUSPECT, never a masked true stall.",
                repr(channel),
                channel_count,
                dropped_so_far,
            )
        return
    # Copy-then-call preserved: the callback does journal I/O and must never run under
    # _BEAT_LOCK.
    try:
        beat()
    except Exception:  # pylint: disable=broad-exception-caught — best-effort liveness beat
        logger.exception("progress_beat.emit failed; the worker is unaffected")


__all__ = [
    "LEGACY_CHANNEL",
    "ProgressBeat",
    "emit_progress_beat",
    "register_progress_beat",
    "reset_progress_beats",
    "unregister_progress_beat",
]
