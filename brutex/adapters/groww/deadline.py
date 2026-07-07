"""Wall-clock freeze guard — bound an unbounded SDK call by a deadline.

DEC-GROWW-PULL-CALL-DEADLINE-001 + DEC-GROWW-TOKEN-REAUTH-001 (MINOR-1). The Groww
``growwapi`` SDK calls carry no inherent wall-clock bound, so a stalled connection
(a half-open socket, a server that never responds) would block its caller FOREVER:

* the historical-pull worker thread (``get_historical_candles``), AND
* the daily 06:00-IST access-token RE-MINT (the lock-holding network mint inside
  :meth:`~brutex.adapters.groww.reauth.SdkRefresher.reauth`).

Both share the same hazard — a hung call with no timeout. This module is the SINGLE
freeze guard for both. It is deliberately **stdlib-only** (it imports ``contextvars`` +
``threading`` and nothing from the :mod:`brutex.adapters.groww` package), so it can
never take part in an import cycle — the reason it lives here and not in
:mod:`brutex.adapters.groww.client` (a ``client`` import from ``reauth`` would
re-create the client↔reauth cycle CodeQL flagged on #868).

Run correlation (DEC-PULL-BEAT-PERRUN-001 fix round): the daemon worker runs inside a
COPY of the CALLER thread's contextvars context (one fresh copy per call — the
:class:`~brutex.orchestrator.parallel.PoolRunParallel` precedent), so the wrapped *fn*
keeps the caller's ambient ``pull_id``. Load-bearing for the deadline-wrapped durable
write (:func:`~brutex.adapters.groww.write_deadline.append_with_deadline`, BOTH spine
tails): the partition-flock lock-wait poll inside ``sink.append`` fires
:func:`~brutex.observability.progress_beat.emit_progress_beat` per retry (the
DEC-PULL-CONTENTION-ANTISTALL-001 GAP-#15 antistall seam), and a fresh thread's EMPTY
context would resolve the ambient run id to ``""`` — the run's OWN write-contention
beats would DROP as foreign instead of feeding its keyed watchdog channel, consuming
the once-per-epoch ``progress_beat.foreign_beat_dropped`` WARN and silencing a later
genuine zombie's WARN.
"""

from __future__ import annotations

import contextvars
import threading
from collections.abc import Callable


class DeadlineExceeded(TimeoutError):
    """The deadline guard's OWN timeout — raised ONLY by :func:`run_with_deadline` on expiry.

    A SUBCLASS of :class:`TimeoutError` (DEC-FETCH-HUNG-WRITE-DEADLINE-001 hardening), so every
    existing ``except TimeoutError`` caller (the reauth re-mint at ``reauth.py`` + the
    ``from_groww_exception`` ``"deadline exceeded"`` message-hint classification at the candle
    call) still catches it BYTE-IDENTICALLY — the message is unchanged. The reason for a distinct
    type: a caller that needs to tell "the deadline guard fired" apart from "the wrapped *fn*
    itself raised a real :class:`TimeoutError` / :class:`socket.timeout`" can match THIS subclass.
    :func:`~brutex.adapters.groww.write_deadline.append_with_deadline` does exactly that, so a
    future ``sink.append`` that raises its OWN ``TimeoutError`` (e.g. a network writer) propagates
    UNCHANGED instead of being silently deferred as a write-deadline hang.
    """


def run_with_deadline[T](fn: Callable[[], T], seconds: float) -> T:
    """Run *fn* in a daemon thread, raising a timeout if it blows the wall-clock deadline.

    THE FREEZE GUARD (DEC-GROWW-PULL-CALL-DEADLINE-001). The Groww SDK call carries
    no inherent wall-clock bound, so a stalled connection (half-open socket, server
    not responding) would block the caller FOREVER — a retry loop cannot help because
    it only fires on a *raised* error, and a hung call never raises. This caps the
    work: if *fn* has not returned within *seconds*, raise a :class:`DeadlineExceeded`
    (a :class:`TimeoutError` SUBCLASS — every existing ``except TimeoutError`` caller still
    catches it) whose message carries the ``deadline exceeded`` hint, so
    :func:`~brutex.adapters.groww.errors.from_groww_exception` classifies it as a
    retryable :class:`~brutex.adapters.groww.errors.GrowwTimeoutError`. The caller's
    bounded retry / quarantine path then handles the timeout instead of freezing.
    The abandoned worker is a daemon — it dies with the process and never blocks
    shutdown; its eventual result (if it ever returns) is discarded.

    A caller that must distinguish THIS guard's timeout from a :class:`TimeoutError` raised
    by *fn* ITSELF matches the :class:`DeadlineExceeded` subclass (a real ``fn``-raised
    ``TimeoutError`` is re-raised UNCHANGED on the caller thread, never re-typed).

    Generic over the return type so it bounds BOTH the historical candle call (which
    returns a candle ``Mapping``) AND the token re-mint (which returns an SDK tuple) —
    one freeze guard, no copy (CLAUDE.md §11 COMMON).

    Daemon-thread bound (DEC-PULL-HEALTH-CONCURRENCY-001 GAP #18; ADR-0009 §3): under a
    sustained timeout storm each timed-out call leaves ONE abandoned daemon worker behind
    until its underlying SDK socket finally returns / fails. The number of live daemons is
    therefore bounded by the SDK's own (finite) socket timeout, NOT by this guard — a
    daemon dies with the call it wraps. The guard relies on that finite socket timeout to
    keep ``threading.active_count()`` from growing without bound;
    ``tests/unit/test_groww_deadline.py`` pins this by firing N timed-out calls and
    asserting the thread count returns to its pre-storm baseline once every simulated
    socket fires.

    The worker runs inside a COPY of the CALLER's contextvars context
    (DEC-PULL-BEAT-PERRUN-001 fix round — a fresh thread starts with an EMPTY context,
    so without the copy every ``emit_progress_beat`` fired inside *fn* — the
    partition-flock lock-wait poll under a deadline-wrapped ``sink.append`` in BOTH
    spine tails — resolved its ambient ``pull_id`` to ``""`` and DROPPED as a foreign
    beat instead of feeding the run's keyed watchdog channel). One fresh copy per call
    (the ``PoolRunParallel`` precedent): anything *fn* binds mutates only the discarded
    copy, so no bind can leak across calls or back into the caller; a scope-less caller
    (the CLI path) propagates an empty copy — byte-identical to the pre-copy behaviour.
    """
    result: dict[str, T] = {}
    failure: dict[str, Exception] = {}
    done = threading.Event()

    def _runner() -> None:
        try:
            result["value"] = fn()
        except Exception as exc:  # re-raised on the caller thread below (any error)
            failure["error"] = exc
        finally:
            done.set()

    # Capture on the CALLER thread; run the worker inside the copy so the wrapped fn
    # keeps the caller's ambient run correlation (pull_id) — see the docstring above.
    ctx = contextvars.copy_context()
    threading.Thread(
        target=ctx.run, args=(_runner,), name="groww-deadline-call", daemon=True
    ).start()
    if not done.wait(seconds):
        # The guard's OWN timeout — a DeadlineExceeded (TimeoutError subclass). A real
        # TimeoutError raised by ``fn`` itself takes the ``failure`` path below and is re-raised
        # UNCHANGED (never re-typed to DeadlineExceeded), so a caller can tell the two apart.
        raise DeadlineExceeded(f"Groww call deadline exceeded ({seconds:g}s)")
    if "error" in failure:
        raise failure["error"]
    return result["value"]


__all__ = ["DeadlineExceeded", "run_with_deadline"]
