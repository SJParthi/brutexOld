"""Wall-clock freeze guard — bound an unbounded call by a deadline (COMMON home).

DEC-STORE-WRITE-DEADLINE-001 (layering move) over DEC-GROWW-PULL-CALL-DEADLINE-001 +
DEC-GROWW-TOKEN-REAUTH-001 (MINOR-1) + DEC-FETCH-HUNG-WRITE-DEADLINE-001 (#999). Several
callers carry no inherent wall-clock bound, so a stalled dependency (a half-open socket,
a server that never responds, a wedged ``fsync`` on a stalled external SSD, a hung DuckDB
write on a wedged volume) would block its caller FOREVER:

* the historical-pull worker thread (``get_historical_candles``),
* the daily 06:00-IST access-token RE-MINT
  (:meth:`~brutex.adapters.groww.reauth.SdkRefresher.reauth`),
* the durable lake write
  (:func:`~brutex.adapters.groww.write_deadline.append_with_deadline`), AND
* the registry/coverage DuckDB writes
  (:class:`~brutex.data.store_write_deadline.StoreWriteDeadlineGuard`).

All share the same hazard — a hung call with no timeout — and this module is the SINGLE
freeze guard for all of them (CLAUDE.md §11 COMMON, one primitive, no copy). It is
deliberately **stdlib-only** (it imports ``contextvars`` / ``threading`` / ``os`` /
``math`` and nothing from any ``brutex`` package), so it can never take part in an import
cycle. It lives in
:mod:`brutex.config` — the layer BOTH :mod:`brutex.adapters` and :mod:`brutex.data`
already import (the ``brutex.config.hardware_envelope`` precedent) — so the store guard
never needs a data→adapters import; :mod:`brutex.adapters.groww.deadline` re-exports it
byte-compatibly (the DEC-GROWW-REAUTH-DEADLINE-001 #910 relocation precedent).

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
import math
import os
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
    :func:`~brutex.adapters.groww.write_deadline.append_with_deadline` and
    :class:`~brutex.data.store_write_deadline.StoreWriteDeadlineGuard` do exactly that, so a
    wrapped call that raises its OWN ``TimeoutError`` propagates UNCHANGED instead of being
    silently deferred as a deadline hang.
    """


def run_with_deadline[T](
    fn: Callable[[], T],
    seconds: float,
    *,
    what: str = "Groww call",
    thread_name: str = "groww-deadline-call",
) -> T:
    """Run *fn* in a daemon thread, raising a timeout if it blows the wall-clock deadline.

    THE FREEZE GUARD (DEC-GROWW-PULL-CALL-DEADLINE-001). The wrapped call carries
    no inherent wall-clock bound, so a stalled dependency (half-open socket, server
    not responding, wedged volume) would block the caller FOREVER — a retry loop cannot
    help because it only fires on a *raised* error, and a hung call never raises. This
    caps the work: if *fn* has not returned within *seconds*, raise a
    :class:`DeadlineExceeded` (a :class:`TimeoutError` SUBCLASS — every existing
    ``except TimeoutError`` caller still catches it) whose message carries the
    ``deadline exceeded`` hint, so
    :func:`~brutex.adapters.groww.errors.from_groww_exception` classifies the DEFAULT
    (Groww-call) message as a retryable
    :class:`~brutex.adapters.groww.errors.GrowwTimeoutError`. The caller's bounded
    retry / quarantine / defer path then handles the timeout instead of freezing.
    The abandoned worker is a daemon — it dies with the process and never blocks
    shutdown; its eventual result (if it ever returns) is discarded.

    A caller that must distinguish THIS guard's timeout from a :class:`TimeoutError` raised
    by *fn* ITSELF matches the :class:`DeadlineExceeded` subclass (a real ``fn``-raised
    ``TimeoutError`` is re-raised UNCHANGED on the caller thread, never re-typed).

    Generic over the return type so it bounds the historical candle call, the token
    re-mint, the durable lake write AND the registry/coverage DuckDB writes — one
    freeze guard, no copy (CLAUDE.md §11 COMMON). *what* / *thread_name* label the
    timeout message + the daemon thread per call site; their DEFAULTS reproduce the
    original Groww-call message and thread name byte-for-byte, so every pre-move
    caller is byte-unchanged (DEC-STORE-WRITE-DEADLINE-001 — the ``"deadline
    exceeded"`` substring in the default message stays load-bearing for the
    ``from_groww_exception`` hint classification).

    Daemon-thread bound (DEC-PULL-HEALTH-CONCURRENCY-001 GAP #18; ADR-0009 §3): under a
    sustained timeout storm each timed-out call leaves ONE abandoned daemon worker behind
    until its underlying blocked call finally returns / fails. The number of live daemons is
    therefore bounded by the dependency's own (finite) release, NOT by this guard — a
    daemon dies with the call it wraps. ``tests/unit/test_groww_deadline.py`` +
    ``tests/unit/test_config_deadline.py`` pin this by firing N timed-out calls and
    asserting the thread count returns to its pre-storm baseline once every simulated
    hang releases.

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
    threading.Thread(target=ctx.run, args=(_runner,), name=thread_name, daemon=True).start()
    if not done.wait(seconds):
        # The guard's OWN timeout — a DeadlineExceeded (TimeoutError subclass). A real
        # TimeoutError raised by ``fn`` itself takes the ``failure`` path below and is re-raised
        # UNCHANGED (never re-typed to DeadlineExceeded), so a caller can tell the two apart.
        raise DeadlineExceeded(f"{what} deadline exceeded ({seconds:g}s)")
    if "error" in failure:
        raise failure["error"]
    return result["value"]


def resolve_env_seconds(env_name: str, default: float, raw: str | None = None) -> float:
    """Resolve an env-tunable positive-seconds bound: env override, else *default*.

    THE ONE validation shape for every deadline/timeout env override
    (DEC-STORE-WRITE-DEADLINE-001 — the consolidation of the byte-identical
    :func:`~brutex.adapters.groww.write_deadline.resolve_write_deadline_seconds` /
    :func:`~brutex.data.partition_lock.resolve_partition_lock_timeout_seconds`
    resolvers, which now DELEGATE here; CLAUDE.md §11 COMMON). Falls back to
    *default* when the value is unset, blank, non-numeric, non-positive, OR
    non-finite (``nan`` / ``inf`` parse as floats but would DEFEAT a bound — a
    ``nan`` deadline makes ``done.wait(nan)`` ill-defined and an ``inf`` deadline
    never expires, re-introducing the infinite block these guards remove). A bad
    value can therefore NEVER disable a bound (fail-safe to the audited default,
    Golden Rule #1). O(1).

    Parameters
    ----------
    env_name
        The environment variable to read when *raw* is ``None``.
    default
        The audited fail-safe default returned for any invalid/absent value.
    raw
        The value to validate (``None`` reads ``os.environ[env_name]``); a test
        may pass one directly.
    """
    value = os.environ.get(env_name) if raw is None else raw
    if value is None or not value.strip():
        return default
    try:
        parsed = float(value)
    except ValueError:
        return default
    if not math.isfinite(parsed) or parsed <= 0.0:
        return default
    return parsed


__all__ = ["DeadlineExceeded", "resolve_env_seconds", "run_with_deadline"]
