"""Bound ONE durable lake write by a wall-clock deadline (DEC-FETCH-HUNG-WRITE-DEADLINE-001).

The Groww fetch lane must NEVER be blocked by an internal write. The async fetch pipeline
(:mod:`brutex.adapters.groww.fetch_pipeline`) already DECOUPLES the durable write from the
producer onto a bounded-queue consumer pool — but there was one remaining HUNG-WRITE BLIND
SPOT. The consumer's durable write (``sink.append`` → :class:`~brutex.data.parquet_writer.ParquetWriter`
→ :func:`~brutex.data.atomic_io.atomic_write_via` → ``os.fsync`` + ``os.replace``) carries NO
inherent wall-clock bound. The existing bounds do NOT cover it:

* :func:`~brutex.adapters.groww.deadline.run_with_deadline` bounds only the *fetch* SDK call;
* :data:`~brutex.data.partition_lock.DEFAULT_PARTITION_LOCK_TIMEOUT_SECONDS` bounds only the
  cross-process ``flock`` ACQUIRE — a hung write happens AFTER the lock is held, inside the
  merge / render / fsync / replace. Because THIS module's deadline wraps the whole
  ``sink.append`` INCLUDING that acquire, the lock bound must stay strictly BELOW the write
  deadline (75 s < 90 s — DEC-WRITE-DEADLINE-LOCK-ORDER-001) or it is unreachable: the write
  deadline would abandon the write thread mid-``flock``, leaking the thread + lock fd instead
  of taking the clean :class:`~brutex.data.errors.PartitionLockTimeout` defer path. Env
  overrides can re-invert the ordering; :func:`append_with_deadline` WARNs once per process
  (``groww.write_deadline.lock_timeout_inversion``) when the EFFECTIVE configuration is
  inverted.

So a WEDGED durable write that NEVER raises (a hung ``fsync`` / ``os.replace`` on a stalled
external SSD / NFS mount) would block its consumer thread forever, fill that consumer's bounded
queue, and eventually wedge the PRODUCER on :meth:`~brutex.adapters.groww.fetch_pipeline.FetchPipeline.submit`
back-pressure FOREVER — the whole fetch lane stalls at 0 r/s while the rate controller still
"allows" the ceiling. The fetch-pipeline back-pressure loop only breaks on ``_stopped`` /
``_draining``, which a non-RAISING hang never sets.

This module is the FREEZE GUARD for the durable write. :func:`append_with_deadline` runs
``sink.append`` under a generous wall-clock deadline (reusing the COMMON
:func:`~brutex.adapters.groww.deadline.run_with_deadline` — NO copy, CLAUDE.md §11) and, on
expiry, raises the TRANSIENT :class:`~brutex.data.errors.WriteDeadlineExceeded`. The ingest
spines catch it at the window boundary EXACTLY like a :class:`~brutex.data.errors.PartitionLockTimeout`
— record the window ``FAILED``, return a transient outcome, DEFER it to the next pull — so the
consumer keeps processing subsequent windows and the producer is RELEASED. Per-window graceful
deferral, NOT a pull-wide trip.

**Honest envelope (Golden Rule #10).** :func:`~brutex.adapters.groww.deadline.run_with_deadline`
runs ``sink.append`` in a daemon worker thread; a wedged C-level ``fsync`` thread CANNOT be
killed — it dies with the process. The guarantee here is **"the fetch lane is never blocked"**
(the producer is released + the window is deferred + re-pulled), NOT "the zombie write thread is
terminated". The abandoned daemon STILL holds this partition's in-process
:class:`threading.Lock` + cross-process ``flock`` until its wedged syscall finally returns or the
process exits; because all windows of ONE series route to ONE consumer (the per-partition-lock
shard invariant), a LATER window of the SAME partition that lands on that consumer could block on
the still-held in-process lock — but that later append is ALSO wrapped here, so it too defers in
bounded time rather than wedging. A persistently-wedged volume therefore defers the same window(s)
every pull until the volume recovers (the operator's signal to act), never silently losing data
(idempotent keep-first sink + the durable window ledger). That indefinite-re-pull promise holds
BECAUSE the ledger EXEMPTS the deferral error types (``WriteDeadlineExceeded`` +
``PartitionLockTimeout`` — ``window_ledger.FAILED_STREAK_EXEMPT_ERRORS``, review round 1 MAJOR
1.8a) from the fetch-exhausted ``FAILED`` -> ``SKIPPED`` terminal settle: a local write stall's
fetch SUCCEEDED, so its FAILED records never consume the K-attempt exhaustion budget and the
window can never be cemented un-pullable by its own deferrals. The number of abandoned daemons is
bounded by the distinct wedged partitions, not by the window count.

**Bytes envelope of an abandoned daemon (DEC-WRITE-DEADLINE-ZOMBIE-SLIM-001).** Each zombie's
EXECUTING frame irreducibly pins whatever ``sink.append`` currently holds — its ``bars``
argument (~10 MiB for a 30-day 1-minute window) plus, past the render phase, the arrow table —
until the wedged syscall finally returns; no external fix can free a running frame's locals.
What this module CAN control is the GUARD layer, and since the one-shot pop handoff in
:func:`append_with_deadline` it holds NO second strong reference once the call starts: the old
``lambda`` closure cell (which pinned ``bars`` for the zombie's whole life even if the sink had
already dropped it) is replaced by a single-slot holder the worker pops inline, and the guard
frame ``del``\\ s its own ``bars`` parameter so the raised
:class:`~brutex.data.errors.WriteDeadlineExceeded`'s traceback — held by the spine's handler
while it records/defers the window — no longer pins the payload either. Net: the payload's
lifetime is exactly the in-flight ``sink.append`` frame's need, nothing longer; a wedge in the
flock-ACQUIRE phase (the common contended shape) or a sink that releases its argument early
frees the bytes even while the thread stays wedged.

This module is import-cheap: it imports only :func:`run_with_deadline`, the constants, the
partition-lock timeout resolver (a pure env-reader), and the exception type; it does NOT
import the heavy lake writers (the ``sink`` is injected). O(1) per window — the deadline
wrap is one ``threading.Event.wait``, NOT per-bar.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from typing import TYPE_CHECKING

import structlog

from brutex.adapters.groww.constants import GROWW_WRITE_DEADLINE_S
from brutex.adapters.groww.deadline import DeadlineExceeded, run_with_deadline
from brutex.config.deadline import resolve_env_seconds
from brutex.data.errors import WriteDeadlineExceeded
from brutex.data.partition_lock import resolve_partition_lock_timeout_seconds

if TYPE_CHECKING:  # pragma: no cover
    from brutex.data.models import CandleBar
    from brutex.data.sink import BarSink, SinkAppendResult

_LOG = structlog.get_logger(__name__)

__all__ = [
    "WRITE_DEADLINE_ENV",
    "append_with_deadline",
    "reset_lock_timeout_inversion_warning",
    "resolve_write_deadline_seconds",
]

#: Environment override for the durable-write deadline (operator escape hatch, no code change).
WRITE_DEADLINE_ENV: str = "BRUTEX_GROWW_WRITE_DEADLINE_S"

# Once-per-process guard for the lock-timeout inversion WARN below. A tiny sanctioned
# process-global (the `groww.parse.epoch_timestamp` warn-once precedent,
# DEC-INGEST-BOUNDARY-INTEGRITY-001 — a mutated set, mirroring its shape): the inversion
# is a CONFIGURATION property, constant for the process's lifetime, and the check runs
# once per window — warning per call would spam thousands of identical lines per pull.
# Thread-safe; explicit reset seam for tests. Bounded: at most one sentinel element.
_INVERSION_WARN_GUARD = threading.Lock()
_inversion_warned: set[str] = set()
_WARNED_SENTINEL = "warned"


def reset_lock_timeout_inversion_warning() -> None:
    """Test seam: re-arm the once-per-process lock-timeout inversion WARN."""
    with _INVERSION_WARN_GUARD:
        _inversion_warned.clear()


def _warn_once_if_lock_timeout_inverted(deadline_seconds: float) -> None:
    """WARN once per process when the EFFECTIVE lock timeout re-inverts the ordering.

    The ordering invariant (DEC-WRITE-DEADLINE-LOCK-ORDER-001) is
    ``partition-lock acquire timeout < durable-write deadline`` — the defaults hold it
    (75 s < 90 s, pinned by ``tests/unit/test_write_deadline_lock_order.py``), but the
    env overrides (``BRUTEX_PARTITION_LOCK_TIMEOUT_S`` / ``BRUTEX_GROWW_WRITE_DEADLINE_S``)
    can re-invert it at runtime. An inverted effective configuration makes the lock bound
    unreachable: this module's deadline abandons the write thread while it still blocks in
    the ``flock`` poll loop (thread + lock fd leaked per contended window) instead of the
    clean :class:`~brutex.data.errors.PartitionLockTimeout` defer. Called only on the
    DEFAULT-deadline path (an explicit per-call ``deadline_seconds`` — a test exercising a
    tiny timeout — is not the process configuration and must not trip it). Observe-only:
    never raises, never changes pacing. O(1).
    """
    lock_timeout = resolve_partition_lock_timeout_seconds()
    if lock_timeout < deadline_seconds:
        return
    with _INVERSION_WARN_GUARD:
        if _WARNED_SENTINEL in _inversion_warned:
            return
        _inversion_warned.add(_WARNED_SENTINEL)
    _LOG.warning(
        "groww.write_deadline.lock_timeout_inversion",
        partition_lock_timeout_s=lock_timeout,
        write_deadline_s=deadline_seconds,
        detail=(
            "effective partition-lock acquire timeout >= durable-write deadline — the "
            "lock bound is unreachable; a contended acquire will be abandoned mid-flock "
            "(thread+fd leak) instead of the clean PartitionLockTimeout defer "
            "(DEC-WRITE-DEADLINE-LOCK-ORDER-001)"
        ),
    )


def resolve_write_deadline_seconds(raw: str | None = None) -> float:
    """Resolve the durable-write deadline: env override, else the audited default.

    Reads :data:`WRITE_DEADLINE_ENV` (:data:`BRUTEX_GROWW_WRITE_DEADLINE_S`); falls back to
    :data:`~brutex.adapters.groww.constants.GROWW_WRITE_DEADLINE_S` when unset, blank,
    non-numeric, non-positive, OR non-finite (``nan`` / ``inf`` parse as floats but would
    DEFEAT the bound — a ``nan`` deadline makes ``done.wait(nan)`` ill-defined and an ``inf``
    deadline never expires, re-introducing the infinite block this fix removes). A bad value
    can therefore NEVER disable the bound (fail-safe to the audited default, Golden Rule #1).
    Delegates to the ONE COMMON validation shape
    :func:`~brutex.config.deadline.resolve_env_seconds` — shared byte-identically with
    :func:`~brutex.data.partition_lock.resolve_partition_lock_timeout_seconds` and
    :func:`~brutex.data.store_write_deadline.resolve_store_write_deadline_seconds`
    (DEC-STORE-WRITE-DEADLINE-001; CLAUDE.md §11 COMMON). O(1).

    Parameters
    ----------
    raw
        The deadline value (``None`` reads :data:`WRITE_DEADLINE_ENV`); a test may pass one.
    """
    return resolve_env_seconds(WRITE_DEADLINE_ENV, GROWW_WRITE_DEADLINE_S, raw)


def append_with_deadline(
    sink: BarSink,
    *,
    exchange: str,
    segment: str,
    symbol: str,
    interval: str,
    bars: Sequence[CandleBar],
    deadline_seconds: float | None = None,
) -> SinkAppendResult:
    """Run ``sink.append`` under a wall-clock deadline; a hang → :class:`WriteDeadlineExceeded`.

    The COMMON freeze guard for the durable lake write. Delegates the append to
    :func:`~brutex.adapters.groww.deadline.run_with_deadline` (NO copy, CLAUDE.md §11) so a
    WEDGED durable write (a hung ``fsync`` / ``os.replace`` that never raises) raises a
    :class:`~brutex.adapters.groww.deadline.DeadlineExceeded` after *deadline_seconds*, which this
    re-raises as the TRANSIENT :class:`~brutex.data.errors.WriteDeadlineExceeded` — the ingest
    spines catch it at the window boundary EXACTLY like a
    :class:`~brutex.data.errors.PartitionLockTimeout` and DEFER the window to the next pull,
    freeing the fetch lane.

    Any error the underlying ``sink.append`` raises ITSELF (a
    :class:`~brutex.data.errors.PartitionLockTimeout` from the lock acquire, a
    :class:`~brutex.data.errors.LakeWriteHalt` disk-full, a
    :class:`~brutex.data.errors.WriterError`, …) propagates UNCHANGED through
    :func:`run_with_deadline` (it re-raises the worker's exception on the caller thread). Only the
    GUARD's OWN deadline — a :class:`~brutex.adapters.groww.deadline.DeadlineExceeded` — becomes
    :class:`WriteDeadlineExceeded`; a real :class:`TimeoutError` / :class:`socket.timeout` raised
    by ``sink.append`` ITSELF (e.g. a future network-backed writer) is NOT caught here and
    propagates UNCHANGED (DEC-FETCH-HUNG-WRITE-DEADLINE-001 hardening — it is NOT silently
    deferred as a hung durable write). So this wraps the existing error contract additively: it
    adds the hung-write bound, it never reclassifies a real write error.

    Parameters
    ----------
    sink
        The lake write sink (injected — this module never imports the heavy writers).
    exchange, segment, symbol, interval, bars
        Forwarded verbatim to :meth:`~brutex.data.sink.BarSink.append`.
    deadline_seconds
        The wall-clock bound; defaults to :func:`resolve_write_deadline_seconds`
        (env-tunable). Tests pass a small value to exercise the timeout fast.

    Returns
    -------
    SinkAppendResult
        The sink's result on a write that completes within the deadline (the normal /
        merely-slow path — byte-identical to a direct ``sink.append`` call).

    Raises
    ------
    WriteDeadlineExceeded
        If the durable write does not complete within *deadline_seconds* (a wedged
        fsync / replace). A TRANSIENT defer signal, NOT a pull-wide stop.
    """
    if deadline_seconds is None:
        deadline = resolve_write_deadline_seconds()
        # The default path IS the process's effective configuration — check the
        # ordering invariant here (an explicit per-call deadline is a test/caller
        # choice, not the configuration; DEC-WRITE-DEADLINE-LOCK-ORDER-001).
        _warn_once_if_lock_timeout_inverted(deadline)
    else:
        deadline = deadline_seconds
    # ONE-SHOT PAYLOAD HANDOFF (DEC-WRITE-DEADLINE-ZOMBIE-SLIM-001): the previous
    # ``lambda: sink.append(..., bars=bars)`` closure CELL — plus THIS frame's own
    # ``bars`` parameter, which rides the raised WriteDeadlineExceeded's traceback
    # up the spine — kept a GUARD-layer strong reference to the whole window's
    # payload for the abandoned daemon's entire wedge. The worker instead POPS the
    # payload from a single-slot holder INLINE in the call expression (CPython
    # transfers argument ownership into the callee frame), and this frame ``del``s
    # its parameter, so after the call starts NO guard-layer reference (closure /
    # thread args / traceback frame) survives beyond the in-flight ``sink.append``
    # frame itself. ``_append_popped`` runs exactly once (the run_with_deadline
    # worker contract), so the pop can never see an empty holder.
    payload: list[Sequence[CandleBar]] = [bars]
    del bars  # this frame rides the deadline exception's traceback — hold no payload ref

    def _append_popped() -> SinkAppendResult:
        return sink.append(
            exchange=exchange,
            segment=segment,
            symbol=symbol,
            interval=interval,
            bars=payload.pop(),
        )

    try:
        return run_with_deadline(_append_popped, deadline)
    except DeadlineExceeded as exc:
        # The GUARD's OWN deadline fired (a wedged fsync/replace that never raised). We match the
        # DeadlineExceeded subclass — NOT the broad TimeoutError — so a real TimeoutError /
        # socket.timeout raised by sink.append ITSELF propagates UNCHANGED (it never reaches this
        # arm), never silently deferred as a hung durable write (DEC-FETCH-HUNG-WRITE-DEADLINE-001
        # hardening). Re-raise as the TRANSIENT WriteDeadlineExceeded so the spine DEFERS this
        # window (record FAILED → re-pull) exactly like a PartitionLockTimeout — never a
        # pull-wide stop, never silent data loss (Golden Rule #1). The abandoned daemon write
        # thread cannot be killed (honest envelope) but the FETCH LANE is freed: the consumer
        # returns + processes the next window, releasing the back-pressured producer.
        raise WriteDeadlineExceeded(deadline) from exc
