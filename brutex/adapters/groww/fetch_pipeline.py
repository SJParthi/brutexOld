"""Full async separation of the Groww fetch lane from ALL internal processing.

DEC-GROWW-FETCH-ASYNC-SEPARATION-001.

## What this is (the operator's core requirement)

The fetch thread must do **NOTHING but fetch + hand off**. A SEPARATE consumer
pool does ALL internal work — session-filter, dedup/lake-write, conflict-aggregate,
and ledger.record — so internal processing can **NEVER block or slow the fetch**.
This is STRONGER than the write-only decouple of
:mod:`brutex.data.write_consumer` (DEC-FETCH-WRITE-OVERLAP-LEVER1-001): there the
fetch worker still ran the session filter + parse + BLOCKED on the durable write
result. Here the producer enqueues the RAW (post-fetch, pre-process) bars and does
NOT wait on durability at all — it immediately re-paces and fetches the next
window. The ONLY legitimate limiter of the fetch lane becomes Groww's rate.

## The producer / consumer split (re-derived from the spine)

The two ingest spines (:mod:`brutex.adapters.groww.ingest` spot,
:mod:`brutex.adapters.groww.option_ingest` F&O) each had ONE per-window function
that did fetch + filter + append + ledger.record. That function is SPLIT in two:

* ``_fetch_window_raw`` — pace + fetch + in-pull EMPTY-retry + the rate-controller
  feedback hooks (``on_rate_limit`` / ``on_timeout``) + the skip check + the
  transient/terminal error handling. STAYS on the fetch (producer) thread. It
  returns EITHER a producer-resolved :class:`FetchTail` (the success path's
  raw bars + metadata to hand to the consumer) OR ``None`` with a producer-side
  :class:`WindowSettlement` already computed (a skip / transient / terminal — those
  never touch the consumer).
* ``_process_window_tail`` — session-filter + ``sink.append`` (+ the
  ``PartitionLockTimeout`` defer) + conflict-aggregate + ``ledger.record`` (SUCCESS
  / EMPTY) + ``on_success``. This is the CONSUMER work.

When :class:`FetchPipeline` is ``None`` (the kill-switched synchronous path) the spine
calls ``_fetch_window_raw`` then ``_process_window_tail`` as ONE synchronous call —
**byte-identical to today**. When a pipeline is wired the producer calls
``_fetch_window_raw`` then :meth:`FetchPipeline.submit` (a bounded ``put`` that
blocks ONLY on back-pressure, NEVER on the write completing) and immediately moves
on; a consumer thread runs the SAME ``_process_window_tail`` (CLAUDE.md §11 COMMON
— ONE tail, no duplicated logic).

## Consumer sharding (preserves every partition-lock invariant)

Each consumer drains its OWN bounded :class:`queue.Queue`; a request is routed to a
consumer by ``blake2b(exchange, segment, symbol, interval) % M`` (the SAME series
shard key :class:`~brutex.data.write_consumer.WriteRequest` uses). So all writes to
one ``(exchange, segment, symbol, interval)`` series land on ONE consumer — two
writes to the same series never run on two consumers at once, preserving the per-
partition :class:`threading.Lock` + ``flock``. (A different series could still
race a shared month partition across two consumers — but the sink's own per-
partition lock + cross-process ``flock`` already serialise THAT, exactly as the
synchronous + write-consumer paths do.)

## Failure matrix (every permutation handled — Golden Rule #7)

* **settle-after-durable (the #1 invariant — NO silent data loss).** A window's
  ``ledger.record(SUCCESS/EMPTY)`` runs INSIDE ``_process_window_tail`` on the
  consumer, strictly AFTER ``sink.append`` returns (→ ``atomic_write_via`` →
  ``os.replace``). :meth:`drain` (poison-pill + join) runs at pull end BEFORE the
  final ledger flush, so every queued window is processed + ledger-recorded before
  the ledger persists. A window enqueued but never drained (hard kill) leaves its
  ledger row UNSETTLED → re-fetched next pull (idempotent keep-first sink).
* **disk-full / OOM in a consumer** → ``_process_window_tail`` raises
  :class:`~brutex.data.errors.LakeWriteHalt`; the consumer trips a pool-wide STOP
  flag and stores the halt, re-raised on the next :meth:`submit` AND at
  :meth:`drain` so it reaches the spine's ``_GLOBAL_STOP_ERRORS`` (graceful pull
  halt). A full disk fails every remaining window identically — drain-to-durable
  would just hammer the full disk.
* **consumer crash mid-drain** → the captured exception is stored + re-raised at the
  next :meth:`submit` / :meth:`drain`; a SUCCESS is NEVER settled for a window whose
  tail did not complete.
* **back-pressure on a SLOW (not hung) write** → each queue is bounded; when it fills,
  the producer does NOT block on the slow write — it SPILLS the window's bars to the
  bounded on-disk overflow (:class:`~brutex.adapters.groww.fetch_spill.FetchSpillArea`,
  DEC-FETCH-NONBLOCKING-HANDOFF-001) + returns immediately, so a SUSTAINED-SLOW disk
  can never back-pressure the fetch lane to the write rate while memory stays bounded
  (the bulk bars live on disk while overflowed). PAST the spill cap (a disk that falls
  behind for the WHOLE pull) the producer falls back to the original bounded
  back-pressure ``put`` (a slowdown — preferring it to an OOM), which a wedged consumer
  can NEVER block forever (the stop / drain re-check). A disk-full at the spill maps to
  :class:`~brutex.data.errors.LakeWriteHalt` (the graceful-halt path).
* **rate controller (8/500 + AIMD)** stays ENTIRELY on the producer side, UNCHANGED.

## Honest envelope (Golden Rule #10)

This delivers **ISOLATION** — the fetch lane's throughput is decoupled from the
internal processing: a stalled consumer never blocks the producer (a stalled-consumer
test keeps the producer issuing at full rate), and a SUSTAINED-SLOW write is absorbed by
the bounded on-disk spill so the fetch lane never throttles to the write rate (up to the
spill cap; past it the bounded back-pressure resumes — a slowdown, never an OOM). It is
**NOT** a throughput increase: the achieved rate is still vendor-bound (Groww 429s above
~5 r/s), and the controller still caps the issue rate. It bounds the OVERFLOW; it is not
an OS-level RSS ceiling (out of scope, as in DEC-LAKE-WRITE-HALT-001). The pipeline is ON
BY DEFAULT (operator-authorized 2026-06-28); ``None`` (the kill-switch, set
``BRUTEX_GROWW_FETCH_PIPELINE=off``) reproduces TODAY's fully-synchronous path
BYTE-IDENTICALLY. stdlib-only at import (``queue`` / ``threading`` / ``dataclasses`` /
``hashlib`` / ``os``); the spill is the COMMON
:func:`~brutex.data.atomic_io.atomic_write_via`.
"""

from __future__ import annotations

import collections
import hashlib
import os
import queue
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING

import structlog

from brutex.adapters.groww.fetch_spill import FetchSpillArea, SpilledWindow
from brutex.data.errors import LakeWriteHalt
from brutex.data.parquet_writer import fold_max_close_delta

if TYPE_CHECKING:  # pragma: no cover
    from brutex.data.models import CandleBar
    from brutex.data.volume_keepalive import VolumeKeepalive


def _is_pool_isolatable(exc: BaseException) -> bool:
    """True iff *exc* is a PER-SYMBOL-quarantinable error that must NOT trip the pool-wide stop.

    DEC-FETCH-CORRUPT-PARTITION-ISOLATE-001. Mirrors the synchronous spine's quarantine
    carve-out (``run_ingest._GLOBAL_STOP_ERRORS``): a :class:`~brutex.models.BruteXError`
    is per-symbol-quarantinable (isolate to its own series, re-pull next pull) UNLESS it is
    a pull-wide STOP — a :class:`~brutex.data.errors.LakeWriteHalt` (disk-full / OOM), a
    quota stop (:class:`~brutex.orchestrator.quota_tracker.DailyQuotaExceeded` /
    ``QuotaInvariantError``), a tripped mass-empty circuit breaker
    (:class:`~brutex.adapters.empty_circuit_breaker.MassEmptySettleHalt`,
    DEC-EMPTY-WINDOW-CIRCUIT-BREAKER-001), or a TERMINAL Groww auth failure
    (:func:`~brutex.adapters.groww.errors.is_terminal_auth_failure` — a dead credential
    / an exhausted re-mint budget, DEC-GROWW-AUTH-HALT-001), which fail/poison every
    remaining window identically and so MUST halt the whole pull. The RETRYABLE
    ``GrowwAuthError`` subclasses (a throttled mint / a stale read-only re-read) stay
    isolatable — transient by design, never a halt. A non-``BruteXError`` (a genuine code bug) is NEVER isolatable — it
    trips the pool so the bug is surfaced, never silently swallowed.

    The heavy ``quota_tracker`` import is LAZY (inside the function) so this module stays
    stdlib-light at import (the async-pipeline import-cost contract). The
    ``run_ingest._GLOBAL_STOP_ERRORS`` tuple cannot be imported here directly (``run_ingest``
    imports THIS module — a cycle), so the global-stop classes are re-listed; a unit test
    pins this list against ``run_ingest._GLOBAL_STOP_ERRORS`` so the two can never drift.
    """
    from brutex.models import BruteXError

    if not isinstance(exc, BruteXError):
        return False  # a genuine code bug → trip the pool (surface it)
    if isinstance(exc, LakeWriteHalt):
        return False  # disk-full / OOM → pull-wide halt
    from brutex.adapters.empty_circuit_breaker import MassEmptySettleHalt
    from brutex.adapters.error_storm_breaker import VendorDownError
    from brutex.adapters.groww.errors import GrowwCredentialDeadError, is_terminal_auth_failure
    from brutex.orchestrator.quota_tracker import DailyQuotaExceeded, QuotaInvariantError

    if is_terminal_auth_failure(exc):
        # A TERMINAL dead-credential auth failure (DEC-GROWW-AUTH-HALT-001) fails every
        # remaining window identically → pull-wide halt. The retryable auth-family
        # subclasses return False from the predicate and stay isolatable below (the
        # DEC-VENDOR-DOWN-BREAKER-001 ``GrowwCredentialDeadError`` escalation is a
        # TERMINAL ``GrowwAuthError`` subclass, so it halts here too).
        return False
    # A quota stop, a tripped mass-empty circuit breaker (DEC-EMPTY-WINDOW-CIRCUIT-
    # BREAKER-001 — a suspected soft-throttle/mis-addressing episode poisons every
    # remaining window identically), a tripped vendor-down error-storm breaker OR the
    # dead-credential escalation (DEC-VENDOR-DOWN-BREAKER-001 — a down vendor / dead
    # token fails every remaining window identically) → pull-wide halt; anything else
    # is a per-symbol-quarantinable BruteXError (e.g. a corrupt-partition WriterError)
    # → isolate, do NOT trip the pool.
    return not isinstance(
        exc,
        (
            DailyQuotaExceeded,
            QuotaInvariantError,
            MassEmptySettleHalt,
            VendorDownError,
            GrowwCredentialDeadError,
        ),
    )


__all__ = [
    "FETCH_PIPELINE_ENV",
    "FETCH_PIPELINE_SIZE_ENV",
    "FetchPipeline",
    "FetchTail",
    "RawFetchResult",
    "SeriesTotals",
    "resolve_fetch_pipeline_size",
]

_LOG = structlog.get_logger("brutex.adapters.groww.fetch_pipeline")

#: Default per-consumer queue depth. Small + bounded so a slow consumer back-pressures
#: the producer (the fetch lane) instead of growing an unbounded backlog (OOM). The
#: producer only needs a handful of in-flight windows to keep fetching while one window's
#: tail drains.
_DEFAULT_QUEUE_MAXSIZE: int = 4

#: Default consumer-pool width when the pipeline is enabled with no explicit size.
#: One consumer per fetch connection is plenty — the tail (filter + Parquet append) is the
#: work the connection used to do inline, so M ≈ the pool size keeps the fetch lane never
#: waiting on a busy consumer. Clamped to ``[1, _FETCH_PIPELINE_SIZE_MAX]``.
_DEFAULT_FETCH_PIPELINE_SIZE: int = 4

#: Hard upper bound on the consumer-pool size (a safety clamp on the env override). More
#: consumers than this only thrash the GIL on the Parquet append render without isolating
#: the fetch lane any further.
_FETCH_PIPELINE_SIZE_MAX: int = 8

#: STALL WATCHDOG threshold (seconds). When the pipeline has windows in flight (submitted but
#: not yet completed) and NO window completes for this long, the watchdog fires ONE loud
#: ``fetch_pipeline.stalled`` WARNING so a stuck consumer can never "look alive but be stuck"
#: silently. A pure OBSERVATION (it never kills the pull); the operator + the pull-health
#: stalled rule act on it. Generous (a deep-month merge is hundreds of ms, not minutes), so a
#: healthy slow write never false-fires.
_DEFAULT_STALL_WATCHDOG_SECONDS: float = 120.0

#: How often the watchdog thread wakes to check for a stall (seconds). Small relative to the
#: threshold so the stall is surfaced promptly without busy-spinning.
_WATCHDOG_POLL_SECONDS: float = 1.0

#: Bounded per-thread join timeout (seconds) at :meth:`FetchPipeline.drain`. A live consumer
#: drains the queue + exits on the poison-pill within one write cycle, so this is only a
#: SAFETY net: a wedged consumer is REPORTED (``fetch_pipeline.drain_join_timeout`` WARNING)
#: rather than hanging the pull's ``finally`` forever. The daemon thread does not keep the
#: process alive, so a reported-but-unjoined thread can never wedge shutdown.
_DEFAULT_DRAIN_JOIN_TIMEOUT_SECONDS: float = 60.0

#: How long a consumer blocks on each ``queue.get`` before re-checking the ``_draining`` flag
#: (seconds). Small so a consumer whose poison-pill could NOT be enqueued (a FULL queue while a
#: PEER consumer is wedged) still notices the drain promptly and exits once its queue empties —
#: the producer-side poison is best-effort, the timed re-check is the guaranteed exit path.
_CONSUMER_GET_POLL_SECONDS: float = 0.5

#: Environment KILL-SWITCH for the FULL async fetch/process separation.
#: DEFAULT ON: the async pipeline is the production default (operator-authorized 2026-06-28,
#: DEC-GROWW-FETCH-ASYNC-SEPARATION-001 default-flip). The synchronous, byte-identical path is
#: still reachable as a kill-switch: setting this to an explicit FALSY token —
#: ``off`` / ``0`` / ``false`` / ``no`` (case-insensitive) — DISABLES the pipeline. Anything
#: else (incl. UNSET, blank, ``1`` / ``true`` / ``yes`` / ``on``) keeps the pipeline ON.
FETCH_PIPELINE_ENV: str = "BRUTEX_GROWW_FETCH_PIPELINE"

#: Environment override for the consumer-pool width (operator escape hatch, no code change).
FETCH_PIPELINE_SIZE_ENV: str = "BRUTEX_GROWW_FETCH_PIPELINE_SIZE"

#: Explicit FALSY tokens that flip the kill-switch (disable the pipeline → synchronous path).
_FETCH_PIPELINE_FALSY: frozenset[str] = frozenset({"off", "0", "false", "no"})


def resolve_fetch_pipeline_size(raw: str | None = None, *, flag: str | None = None) -> int | None:
    """Resolve the consumer-pool width: ``None`` (sync, kill-switched) or a clamped ``M >= 1``.

    The async pipeline is ON BY DEFAULT (operator-authorized 2026-06-28). Returns a clamped
    ``M >= 1`` (the pipeline is ON) for an UNSET flag OR any non-falsy token; returns ``None``
    (the synchronous, byte-identical path — the KILL-SWITCH) ONLY when the
    :data:`FETCH_PIPELINE_ENV` flag is an explicit falsy token (``off`` / ``0`` / ``false`` /
    ``no``, case-insensitive). When ON, the width is read from :data:`FETCH_PIPELINE_SIZE_ENV`
    (or *raw* in tests) and clamped to ``[1, _FETCH_PIPELINE_SIZE_MAX]``; an unset / blank /
    non-integer / out-of-range value falls back to :data:`_DEFAULT_FETCH_PIPELINE_SIZE`
    (fail-safe to the audited default, Golden Rule #1). A thin helper so the live build site +
    tests share ONE normalisation (CLAUDE.md §11 COMMON). O(1).

    Parameters
    ----------
    raw
        The size value (``None`` reads :data:`FETCH_PIPELINE_SIZE_ENV`); a test may pass one.
    flag
        The on/off token (``None`` reads :data:`FETCH_PIPELINE_ENV`); a test may pass one.
    """
    flag_value = os.environ.get(FETCH_PIPELINE_ENV) if flag is None else flag
    if flag_value is not None and flag_value.strip().lower() in _FETCH_PIPELINE_FALSY:
        return None
    size_value = os.environ.get(FETCH_PIPELINE_SIZE_ENV) if raw is None else raw
    if size_value is None or not size_value.strip():
        return _DEFAULT_FETCH_PIPELINE_SIZE
    try:
        size = int(size_value)
    except ValueError:
        return _DEFAULT_FETCH_PIPELINE_SIZE
    if size < 1 or size > _FETCH_PIPELINE_SIZE_MAX:
        return _DEFAULT_FETCH_PIPELINE_SIZE
    return size


@dataclass(frozen=True, slots=True)
class FetchTail:
    """The post-fetch, pre-process payload handed from the producer to the consumer.

    ``_fetch_window_raw`` returns one of these on the SUCCESS path (a clean fetch — the
    bars may be empty, an EMPTY window is still a tail to record). It carries the RAW bars
    (post-fetch, pre-session-filter) + the per-window fetch metadata; the consumer's
    ``_process_window_tail`` does the session-filter + ``sink.append`` + conflict-aggregate
    + ``ledger.record``. The spine builds it (the tail is spine-specific), so this module
    stays vendor/spine-agnostic — it just routes it.

    Attributes
    ----------
    shard_key
        The deterministic routing key — the resolved partition SERIES coordinates, so all
        of one series' windows land on ONE consumer (the per-partition-lock invariant).
    process
        The bound ``_process_window_tail`` closure: takes this :class:`FetchTail` and runs
        the consumer work, returning ``None`` (its outcome is folded by the closure into the
        spine's thread-safe aggregator). It may raise :class:`LakeWriteHalt` (disk-full) /
        any sink error — the pipeline captures + propagates it.
    raw_bars
        The bars to process (empty for an EMPTY window; never ``None``). The pipeline SPILLS
        these to disk when the queue is full (so the producer never blocks on a slow write) and
        reloads them with *rehydrate* before the consumer runs ``process``.
    rehydrate
        Optional spine hook ``(bars) -> None`` the pipeline calls to swap the window's bars in
        the closure's captured state — ``rehydrate(())`` after a spill (DROP the in-memory bulk),
        ``rehydrate(loaded)`` after a reload (RESTORE before processing). The spine wires it so
        ``process`` reads the (possibly-rehydrated) bars rather than a stale captured copy
        (DEC-FETCH-NONBLOCKING-HANDOFF-001). ``None`` (a tail with no spillable bars) DISABLES
        spilling for that window — it back-pressures as before, never an OOM.
    """

    shard_key: str
    process: Callable[[FetchTail], None] = field(compare=False)
    raw_bars: Sequence[CandleBar] = ()
    rehydrate: Callable[[Sequence[CandleBar]], None] | None = field(default=None, compare=False)


@dataclass(frozen=True, slots=True)
class SeriesTotals:
    """The post-drain consumer totals for ONE ``(symbol, interval)`` series (async-mode fold).

    In async-pipeline mode the producer returns a DEFERRED per-series ``IngestReport`` with
    ``bars_appended == 0`` (the real durable work runs on a BACKGROUND consumer, not known
    synchronously). The consumer folds its outcome here keyed by ``(symbol, interval)``; after
    :meth:`FetchPipeline.drain` the controller RECONCILES each per-series report with these
    totals so the async-mode reported ``completed`` / ``bars`` / verdict equal the synchronous
    path's (DEC-GROWW-FETCH-ASYNC-SEPARATION-001 completion-count fix). Without this the
    controller read ``bars_appended == 0`` for EVERY series and falsely reported
    ``completed == 0`` even though every bar was durably written.

    Attributes
    ----------
    bars_appended
        Total bars the consumer durably appended for this series.
    conflicts
        Total keep-first vendor re-statements detected for this series.
    vendor_dup_rows_dropped
        Total WITHIN-BATCH duplicate vendor rows the writer collapsed for this
        series (DEC-VENDOR-CONFLICT-SURFACE-001 — sum-fold across the series'
        windows, exactly like ``conflicts``).
    max_close_delta
        The MAX absolute keep-first close delta observed across this series'
        windows (exact Decimal rupees; ``None`` = no conflict anywhere —
        max-fold via
        :func:`~brutex.data.parquet_writer.fold_max_close_delta`).
    transient
        The series' LAST-FOLD tail-transient state — ``1`` iff the most recently processed
        window of this series was deferred by a ``PartitionLockTimeout``, ``0`` once a later
        window of the SAME series folds a durable success (LAST-WRITE-WINS, NOT a cumulative
        count; DEC-FNO-ASYNC-PIPELINE-WIRE-001 MAJOR-1 fix). All windows of one series route to
        ONE consumer thread (the per-partition-lock invariant), so these folds are SERIALISED in
        processing order and the value reflects the series' NEWEST outcome — matching the
        synchronous spine's ``passes[-1].still_failed_windows`` last-round residual semantics. A
        contract that transient'd in an early until-clean round but DURABLY SUCCEEDS in a later
        round therefore reads ``transient == 0`` (resolved), not a stale cumulative ``1`` (which
        would falsely report it ``still_failing`` / ``clean=False`` with its bars already durable).
    """

    bars_appended: int = 0
    conflicts: int = 0
    transient: int = 0
    vendor_dup_rows_dropped: int = 0
    max_close_delta: Decimal | None = None


@dataclass(frozen=True, slots=True)
class RawFetchResult:
    """One immutable unit of consumer work routed to a shard (wraps a :class:`FetchTail`).

    The producer enqueues this and does NOT wait on the tail completing — the consumer
    drains it and runs ``tail.process(tail)``. There is no per-window result box (unlike
    :mod:`brutex.data.write_consumer`): the producer does not block on durability, so the
    consumer's outcome is folded straight into the spine's thread-safe aggregator by the
    ``process`` closure, never returned to the producer.

    Attributes
    ----------
    tail
        The window's :class:`FetchTail` (the closure + its bars + the rehydrate hook).
    spilled
        When the window's bars were SPILLED to disk (a full queue + a slow consumer), this is
        the on-disk :class:`~brutex.adapters.groww.fetch_spill.SpilledWindow` handle and the
        tail's in-memory bars were dropped via ``rehydrate(())``. The consumer reloads + restores
        the bars before processing, then discards the spill file. ``None`` for an in-memory
        window (the common path — the queue was not full). Kept as an ``object`` so this module
        imports nothing heavy at module scope.
    """

    tail: FetchTail
    spilled: object | None = None

    def shard_key(self) -> str:
        """The deterministic routing key — the wrapped tail's series coordinates."""
        return self.tail.shard_key


def _shard_index(key: str, shards: int) -> int:
    """Map a routing *key* to a shard in ``[0, shards)`` (stable, well-distributed).

    Uses ``blake2b`` (stdlib, fast, no per-process salt unlike ``hash()``) so the routing
    is deterministic across processes/runs — the SAME helper
    :class:`~brutex.data.write_consumer.WriteRequest` routing uses (CLAUDE.md §11 COMMON
    shape; kept local so this module imports nothing from ``data.write_consumer``).
    """
    digest = hashlib.blake2b(key.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") % shards


class FetchPipeline:
    """*M* background consumer threads, each draining its own bounded queue.

    Build one per pull at the live site, thread it into the ingest spines, and
    :meth:`drain` it at pull end BEFORE the final ledger flush. The producer (fetch
    worker) submits via :meth:`submit`, which routes by series shard and BLOCKS ONLY on
    back-pressure (a full queue) — NEVER on the consumer's tail completing. So internal
    processing can never block or slow the fetch lane beyond the bounded back-pressure.
    """

    def __init__(
        self,
        *,
        workers: int,
        queue_maxsize: int = _DEFAULT_QUEUE_MAXSIZE,
        stall_watchdog_seconds: float = _DEFAULT_STALL_WATCHDOG_SECONDS,
        on_stall: Callable[[float], None] | None = None,
        drain_join_timeout_seconds: float = _DEFAULT_DRAIN_JOIN_TIMEOUT_SECONDS,
        spill_area: FetchSpillArea | None = None,
        keepalive: VolumeKeepalive | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        on_bars: Callable[[int], None] | None = None,
    ) -> None:
        """Start *workers* consumer threads, each draining a ``queue_maxsize``-bounded queue.

        Parameters
        ----------
        workers
            Number of consumer threads (``M``). Must be ``>= 1``.
        queue_maxsize
            Per-consumer bounded queue depth (back-pressure; ``>= 1``).
        stall_watchdog_seconds
            Fire the loud ``fetch_pipeline.stalled`` WARNING when windows are in flight but
            none completes for this long (``> 0``). A pure observation (never kills the pull).
        on_stall
            Optional best-effort hook ``(idle_seconds) -> None`` fired ALONGSIDE the WARNING on
            a stall edge (e.g. to route into the pull-health ``stalled`` rule). Guarded — a hook
            error can never crash the watchdog.
        drain_join_timeout_seconds
            Bounded per-thread join timeout at :meth:`drain` (``> 0``). A wedged consumer is
            REPORTED rather than hanging the pull's ``finally`` forever.
        spill_area
            Optional bounded on-disk OVERFLOW (DEC-FETCH-NONBLOCKING-HANDOFF-001). When given,
            a FULL shard queue SPILLS the window's bars to disk + returns immediately (the fetch
            lane never blocks on a slow write) instead of blocking on back-pressure, up to the
            spill cap; past the cap it falls back to the bounded back-pressure (preferring a
            slowdown to an OOM). ``None`` (the default / a dry-run) keeps the pure bounded
            back-pressure — byte-identical to the pre-spill path.
        keepalive
            Optional lake-volume wedge signal
            (:class:`~brutex.data.volume_keepalive.VolumeKeepalive`,
            DEC-VOLUME-KEEPALIVE-001). When given, :meth:`submit` PAUSES at its
            gate while the keepalive reads the volume as WEDGED (so the producer
            never reaches the unbounded spill ``fsync`` on a wedged volume) and
            escalates to a :class:`~brutex.data.errors.LakeWriteHalt`
            (``volume_wedged``) FROM THE PRODUCER THREAD once the wedge outlasts
            the keepalive's ``halt_after_s``; :meth:`drain` stops the daemon on
            every exit path. ``None`` (the default / a dry-run / the kill-switch)
            is byte-identical to today.
        monotonic
            Injected monotonic clock (tests pass a fake to drive the watchdog deterministically).
        on_bars
            Optional best-effort LIVE-progress hook ``(bars_appended_delta) -> None``
            (DEC-PULL-PROGRESS-HONESTY-001): invoked once per :meth:`fold_totals` with
            that window's durable appended-bar delta, so a dashboard bars counter can
            tick live while the producer-side per-pair reports honestly read 0 in
            async mode. Fired under the totals lock (the fold and the hook see one
            consistent delta); GUARDED — a hook error is logged and never trips the
            pipeline. ``None`` (the default — every non-dashboard build site) is
            byte-identical to before.
        """
        if workers < 1:
            raise ValueError(f"workers must be >= 1, got {workers}")
        if queue_maxsize < 1:
            raise ValueError(f"queue_maxsize must be >= 1, got {queue_maxsize}")
        if stall_watchdog_seconds <= 0:
            raise ValueError(f"stall_watchdog_seconds must be > 0, got {stall_watchdog_seconds}")
        if drain_join_timeout_seconds <= 0:
            raise ValueError(
                f"drain_join_timeout_seconds must be > 0, got {drain_join_timeout_seconds}"
            )
        self._workers = workers
        self._queue_maxsize = queue_maxsize
        self._stall_watchdog_seconds = stall_watchdog_seconds
        self._on_stall = on_stall
        self._drain_join_timeout_seconds = drain_join_timeout_seconds
        self._spill_area = spill_area
        self._keepalive = keepalive
        # Wedge-pause edge latch (DEC-VOLUME-KEEPALIVE-001): ONE loud
        # ``fetch_pipeline.paused_wedged`` WARNING per wedge episode, re-armed when a
        # paused producer observes the recovery — never per paused ``submit`` call.
        self._pause_fired = False
        self._pause_lock = threading.Lock()
        self._monotonic = monotonic
        self._on_bars = on_bars
        # `object` poison-pill sentinel: a unique instance per pipeline so a stray
        # RawFetchResult can never be mistaken for the stop signal.
        self._poison: object = object()
        self._queues: list[queue.Queue[RawFetchResult | object]] = [
            queue.Queue(maxsize=queue_maxsize) for _ in range(workers)
        ]
        # Per-shard OVERFLOW deque of SPILLED windows (DEC-FETCH-NONBLOCKING-HANDOFF-001). When a
        # shard's bounded queue is FULL the producer spills the window's bars to disk + appends the
        # TINY handle here (a Path + int), so the fetch lane returns immediately instead of blocking
        # on a slow write. The deque is bounded in BOTH count AND memory
        # (DEC-FETCH-SPILL-COUNT-CAP-001): the bulk bytes live on disk gated by the spill area's
        # BYTE cap, and each resident handle graph (frozen tail + closures) counts against the
        # spill area's WINDOW-COUNT cap — the byte cap alone could not bound a tiny/empty-window
        # flood (a 2-byte "[]" payload per empty window). Hitting EITHER cap re-applies the
        # bounded, stop/drain-aware back-pressure (no OOM, no dropped window). The consumer drains
        # its queue FIRST, then this overflow (FIFO).
        self._overflow: list[collections.deque[RawFetchResult]] = [
            collections.deque() for _ in range(workers)
        ]
        self._overflow_lock = threading.Lock()
        # A pull-wide hard-stop flag: tripped on a consumer failure so every queued +
        # subsequently-submitted request fails fast (the producer re-raises the captured
        # exception) instead of fetching more work that can never be processed.
        self._stopped = threading.Event()
        self._exc: BaseException | None = None
        self._exc_lock = threading.Lock()
        # Pull-level observability totals (the per-series IngestReport is producer-resolved
        # in async mode; these carry the deferred-window bars/conflicts the consumers folded,
        # logged at drain so the operator still sees the real processed totals). Thread-safe
        # under ``_totals_lock`` — many consumers fold concurrently.
        self._processed = 0
        self._bars_appended = 0
        self._conflicts = 0
        # Vendor-behaviour honesty counters (DEC-VENDOR-CONFLICT-SURFACE-001):
        # the pull-level sum of within-batch vendor duplicate rows dropped +
        # the pull-level MAX keep-first close delta, folded alongside the
        # bars/conflicts totals so the drain log + post-drain reconcile see them.
        self._vendor_dup_rows_dropped = 0
        self._max_close_delta: Decimal | None = None
        # Tail-side transient defers (DEC-GROWW-FETCH-ASYNC-SEPARATION-001 MAJOR-2 fix): a
        # ``PartitionLockTimeout`` raised INSIDE a consumer's tail (a wedged peer held the
        # cross-process partition lock past the deadline) is folded here so the deferred window
        # is COUNTED + surfaced at drain — otherwise an async-mode tail-transient was silently
        # dropped (neither in-run retried nor counted in ``still_failed_windows``), so the
        # across-pull retry never re-pulled it. A transient does NOT trip the pool-wide stop
        # (it is OUR contention, not a fatal halt) — the window re-pulls next round.
        self._tail_transients = 0
        # Per-series consumer totals keyed by ``(symbol, interval)`` (the producer passes the
        # series key when it folds). After :meth:`drain` the controller reconciles each deferred
        # per-series ``IngestReport`` with these so the async-mode reported completed/bars/verdict
        # equal the synchronous path's (the completion-count fix). A defaultdict-free accumulate.
        self._series: dict[tuple[str, str], SeriesTotals] = {}
        self._totals_lock = threading.Lock()
        # STALL WATCHDOG state (all under ``_progress_lock``): ``_submitted`` / ``_completed``
        # count windows enqueued vs tail-finished (in flight = submitted − completed); the
        # watchdog fires only while in flight > 0 and no completion advanced ``_last_progress``
        # for ``stall_watchdog_seconds``. ``_stall_fired`` edge-latches so the WARNING is one
        # per stall episode, re-armed by the next completion.
        self._submitted = 0
        self._completed = 0
        self._last_progress = monotonic()
        self._stall_fired = False
        self._progress_lock = threading.Lock()
        # Watchdog shutdown signal — set AFTER the bounded join at drain so the stall watchdog
        # stays ALIVE across the (possibly-wedged) drain join and can still surface a stuck
        # consumer (the BLOCKER fix: stopping it FIRST blinded the watchdog exactly when a wedge
        # mattered most).
        self._watchdog_stop = threading.Event()
        # DRAIN signal — set at the START of :meth:`drain` so a consumer that could NOT receive
        # its poison-pill (its queue was FULL because a PEER consumer is wedged on ``sink.append``
        # and the producer's bounded poison ``put`` was skipped) still notices the drain on its
        # next timed ``queue.get`` and exits once its own queue empties. The guaranteed-exit
        # companion to the best-effort producer-side poison (the BLOCKER fix: an UNBOUNDED
        # ``q.put(poison)`` blocked drain forever when the queue was full).
        self._draining = threading.Event()
        self._threads = [
            threading.Thread(
                target=self._run_consumer,
                args=(i,),
                name=f"groww-fetch-consumer-{i}",
                daemon=True,
            )
            for i in range(workers)
        ]
        for t in self._threads:
            t.start()
        self._watchdog = threading.Thread(
            target=self._run_watchdog, name="groww-fetch-watchdog", daemon=True
        )
        self._watchdog.start()

    @property
    def workers(self) -> int:
        """Number of consumer threads (``M``)."""
        return self._workers

    def fold_totals(
        self,
        *,
        bars_appended: int,
        conflicts: int,
        series_key: tuple[str, str] | None = None,
        vendor_dup_rows_dropped: int = 0,
        max_close_delta: Decimal | None = None,
    ) -> None:
        """Thread-safe fold of ONE processed window's bars/conflicts into the pull totals.

        Called by ``_process_window_tail`` (on a consumer thread) after a durable append, so
        the pull-level observability totals reflect every deferred window AND — when
        *series_key* (``(symbol, interval)``) is given — the per-series totals the controller
        reconciles after drain (the completion-count fix). The two vendor-behaviour counters
        (DEC-VENDOR-CONFLICT-SURFACE-001) fold alongside: duplicates SUM, the close delta
        MAX-folds (``None`` identity — the defaults keep every legacy caller byte-identical).
        O(1) under the totals lock.

        When an ``on_bars`` hook is wired (DEC-PULL-PROGRESS-HONESTY-001) it is invoked
        HERE with this window's *bars_appended* delta — once per folded window, so a
        live dashboard bars counter advances by exactly the folded deltas (the
        no-double-count invariant: the async producer's per-pair report carries 0
        bars, so nothing else adds them). GUARDED: a hook error is logged
        (``fetch_pipeline.on_bars_error``) and never trips the pipeline or loses the
        fold — the totals above are already committed.
        """
        with self._totals_lock:
            self._processed += 1
            self._bars_appended += bars_appended
            self._conflicts += conflicts
            self._vendor_dup_rows_dropped += vendor_dup_rows_dropped
            self._max_close_delta = fold_max_close_delta(self._max_close_delta, max_close_delta)
            if series_key is not None:
                # A durable success CLEARS the series' last-fold transient flag (this window
                # resolved) — LAST-WRITE-WINS, so an early-round transient that later succeeds
                # is no longer reported still-failing (DEC-FNO-ASYNC-PIPELINE-WIRE-001 MAJOR-1).
                self._accumulate_series(
                    series_key,
                    bars_appended=bars_appended,
                    conflicts=conflicts,
                    transient=0,
                    vendor_dup_rows_dropped=vendor_dup_rows_dropped,
                    max_close_delta=max_close_delta,
                )
            if self._on_bars is not None:
                try:
                    self._on_bars(bars_appended)
                except Exception:  # pylint: disable=broad-exception-caught — observe-only hook
                    _LOG.exception("fetch_pipeline.on_bars_error")

    def fold_tail_transient(self, series_key: tuple[str, str] | None = None) -> None:
        """Thread-safe fold of ONE tail-side transient defer (a ``PartitionLockTimeout``).

        Called by ``_process_window_tail`` (on a consumer thread) when the durable append was
        DEFERRED by a wedged-peer cross-process lock past the deadline. Counts the deferred
        window so it is surfaced at :meth:`drain`, attributed to its *series_key*
        (``(symbol, interval)``) for the controller's post-drain reconcile, and the across-pull
        retry round still fires — without it, an async-mode tail-transient was silently dropped
        (MAJOR-2 fix). A transient is a normal still-processed window (it advanced +
        ledger-recorded FAILED), so it is also counted as processed for the watchdog/observability
        totals; it is NOT a pool-wide stop. O(1) under the totals lock.
        """
        with self._totals_lock:
            self._processed += 1
            self._tail_transients += 1
            if series_key is not None:
                # SET the series' last-fold transient flag (this window deferred); a later
                # success of the same series clears it (LAST-WRITE-WINS, MAJOR-1).
                self._accumulate_series(series_key, transient=1)

    def _accumulate_series(
        self,
        series_key: tuple[str, str],
        *,
        bars_appended: int = 0,
        conflicts: int = 0,
        transient: int = 0,
        vendor_dup_rows_dropped: int = 0,
        max_close_delta: Decimal | None = None,
    ) -> None:
        """Fold one window's outcome into the per-series totals (caller holds ``_totals_lock``).

        ``bars_appended`` / ``conflicts`` / ``vendor_dup_rows_dropped`` ACCUMULATE across the
        series' windows (and across until-clean rounds — the keep-first sink makes a re-pull
        append only new bars); ``max_close_delta`` MAX-folds (exact Decimal, ``None`` identity —
        DEC-VENDOR-CONFLICT-SURFACE-001). The
        ``transient`` flag is LAST-WRITE-WINS, NOT cumulative: it is OVERWRITTEN to the caller's
        value (``1`` from a tail-transient fold, ``0`` from a durable-success fold) so the stored
        value reflects the series' MOST RECENT outcome — matching the synchronous spine's
        ``passes[-1]`` last-round residual semantics (DEC-FNO-ASYNC-PIPELINE-WIRE-001 MAJOR-1 fix).
        Per-series single-consumer-thread routing serialises these folds, so the last fold seen
        here is the series' newest window outcome.
        """
        prior = self._series.get(series_key, SeriesTotals())
        self._series[series_key] = SeriesTotals(
            bars_appended=prior.bars_appended + bars_appended,
            conflicts=prior.conflicts + conflicts,
            transient=transient,
            vendor_dup_rows_dropped=prior.vendor_dup_rows_dropped + vendor_dup_rows_dropped,
            max_close_delta=fold_max_close_delta(prior.max_close_delta, max_close_delta),
        )

    @property
    def totals(self) -> tuple[int, int, int]:
        """The pull-level ``(processed_windows, bars_appended, conflicts)`` so far (thread-safe)."""
        with self._totals_lock:
            return (self._processed, self._bars_appended, self._conflicts)

    @property
    def tail_transients(self) -> int:
        """Tail-side transient defers folded so far — the async ``still_failed_windows`` (safe)."""
        with self._totals_lock:
            return self._tail_transients

    @property
    def vendor_surface_totals(self) -> tuple[int, Decimal | None]:
        """The pull-level ``(vendor_dup_rows_dropped, max_close_delta)`` so far (thread-safe).

        DEC-VENDOR-CONFLICT-SURFACE-001 — the two honest vendor-behaviour
        counters the drain log + tests read; the per-series reconcile reads the
        SAME numbers off :meth:`per_series_totals`.
        """
        with self._totals_lock:
            return (self._vendor_dup_rows_dropped, self._max_close_delta)

    def per_series_totals(self) -> dict[tuple[str, str], SeriesTotals]:
        """A snapshot copy of the per-``(symbol, interval)`` consumer totals (thread-safe).

        The controller calls this AFTER :meth:`drain` to RECONCILE each deferred per-series
        ``IngestReport`` (which read ``bars_appended == 0`` in async mode) with the real durable
        counts the background consumers folded — so the async-mode reported completed/bars/verdict
        equal the synchronous path's (DEC-GROWW-FETCH-ASYNC-SEPARATION-001 completion-count fix).
        """
        with self._totals_lock:
            return dict(self._series)

    def submit(self, result: RawFetchResult) -> None:
        """Route *result* to its series shard; the fetch lane NEVER blocks on a slow write.

        Routes to ``shard = blake2b(series) % M`` and tries a NON-BLOCKING ``put`` of the
        request. When the shard queue is FULL (a slow consumer falling behind), the producer
        does NOT block on durability — it SPILLS the window's bars to the bounded on-disk
        overflow (:attr:`_spill_area`, DEC-FETCH-NONBLOCKING-HANDOFF-001), enqueues the tiny
        spill handle in its place, and returns IMMEDIATELY. So a SUSTAINED-SLOW (not hung) disk
        can never back-pressure the fetch lane to the write rate, while memory stays bounded (the
        bulk bars live on disk while overflowed). The consumer reloads the bars from the spill
        before processing, then deletes the spill file. Returns once enqueued (in-memory OR
        spilled) — the producer never waits on the consumer's tail.

        If a consumer already failed (a captured exception — e.g. a
        :class:`~brutex.data.errors.LakeWriteHalt`), the request is REJECTED immediately by
        re-raising that exception on the producer thread, so a pull-wide stop reaches the
        spine's ``_GLOBAL_STOP_ERRORS`` path without waiting for the rest of the pull.

        The fallback bounded back-pressure (the B1 liveness path) is retained for the cases where
        the spill cannot absorb the window — NO spill area (a dry-run / kill-switch), the spill
        CAP is hit (a disk that falls behind for the whole pull — preferring a slowdown to an
        OOM), or the tail has no rehydrate hook. There the ``put`` loops with a timeout that, on
        each :class:`queue.Full`, rechecks :meth:`_raise_if_stopped` (a peer halt re-raises +
        releases the producer) AND :attr:`_draining` (a pull-end drain drops the window — it
        re-fetches next pull, idempotent keep-first) before retrying — so a wedged-consumer queue
        can NEVER block the fetch lane forever, the window is never dropped while the pull is
        live, and no-OOM is intact.

        When a :class:`~brutex.data.volume_keepalive.VolumeKeepalive` is wired
        (DEC-VOLUME-KEEPALIVE-001), submit additionally PAUSES at the gate — BEFORE the
        in-flight accounting and BEFORE any spill write — while the lake volume reads as
        WEDGED, and escalates to a :class:`~brutex.data.errors.LakeWriteHalt`
        (``volume_wedged``) from THIS producer thread once the wedge outlasts the
        keepalive's ``halt_after_s`` (see :meth:`_pause_if_wedged`).
        """
        self._raise_if_stopped()
        self._pause_if_wedged()
        # Count the window in flight BEFORE the (possibly blocking) put, so the watchdog sees
        # work pending even while the producer is back-pressured on a full queue. The matching
        # ``_mark_completed`` (success OR fail-fast drop) keeps in-flight = submitted − completed
        # accurate.
        with self._progress_lock:
            self._submitted += 1
        shard = _shard_index(result.shard_key(), self._workers)
        q = self._queues[shard]
        # FAST PATH: a non-blocking put. The common case (queue not full) enqueues the in-memory
        # window immediately and returns — zero overhead, byte-identical to before.
        try:
            q.put_nowait(result)
            return
        except queue.Full:
            pass
        # FULL QUEUE: the consumer is behind. Try to SPILL the window's bars to disk + append the
        # TINY handle to the shard's OVERFLOW deque so the producer returns immediately (the fetch
        # lane is decoupled from the slow write). On a spill miss (no area / cap hit / no rehydrate
        # hook) fall through to the bounded back-pressure on the ORIGINAL in-memory result (bars
        # intact — never lost), preferring a slowdown to an OOM.
        if self._spill_full_window(shard, result):
            return
        self._back_pressure_put(q, shard, result)

    def _spill_full_window(self, shard: int, result: RawFetchResult) -> bool:
        """Spill *result*'s bars to disk + append the handle to the shard overflow; ``True`` on spill.

        The overflow deque (not the bounded queue) absorbs the window, so a FULL queue can never
        block the producer: the bulk bars go to disk (gated by the spill byte cap) and the tiny
        handle is appended to the unbounded-in-COUNT, bounded-in-MEMORY overflow. Ownership of the
        shared ``raw_bars`` list is handed off CLEANLY — the producer DROPS the in-memory bars
        (``rehydrate(())``) AFTER appending the handle to the overflow under ``_overflow_lock``, so
        the consumer cannot dequeue + restore the list before the producer is done with it. Returns
        ``False`` when the spill cannot route — no :attr:`_spill_area`, no ``rehydrate`` hook (the
        spine opted out), or the cap is hit (:meth:`~FetchSpillArea.try_spill` → ``None``) — leaving
        the original ``result``'s bars intact for the bounded back-pressure fallback (never a
        dropped-bars EMPTY window). A disk-full at the spill raises
        :class:`~brutex.data.errors.LakeWriteHalt` from ``try_spill`` and propagates to halt the
        pull (the existing graceful-halt path). O(1) per window.
        """
        area = self._spill_area
        rehydrate = result.tail.rehydrate
        if area is None or rehydrate is None:
            return False
        handle = area.try_spill(result.tail.raw_bars)
        if handle is None:
            return False  # cap hit — fall back to bounded back-pressure (no OOM), bars intact
        with self._overflow_lock:
            # Append the tiny handle FIRST (the spill holds the only durable bars copy), THEN drop
            # the in-memory bulk — both under the lock so a consumer draining the overflow cannot
            # observe a half-handed-off list (the producer never touches it again after this block).
            self._overflow[shard].append(RawFetchResult(tail=result.tail, spilled=handle))
            rehydrate(())
        return True

    def _back_pressure_put(
        self, q: queue.Queue[object], shard: int, result: RawFetchResult
    ) -> None:
        """Bounded, stop-aware back-pressure ``put`` (the fallback when the spill cannot absorb).

        Retries the put on a full queue, but on EACH timeout rechecks the pool-wide stop / drain
        so a wedged-consumer queue can NEVER block the fetch lane forever (the B1 fix — distinct
        from the drain() poison-put fix). The window is never dropped while the pull is live.
        """
        while True:
            try:
                q.put(result, timeout=_CONSUMER_GET_POLL_SECONDS)
                return
            except queue.Full:
                # A peer-consumer halt RE-RAISES here, releasing the blocked producer so the stop
                # reaches the spine's ``_GLOBAL_STOP_ERRORS`` path (never an unbounded wedge).
                # This window was counted ``_submitted`` above but never enqueued, so mark it
                # completed BEFORE re-raising — otherwise it lingers as a phantom ``in_flight``
                # window and the teardown watchdog fires ONE spurious ``fetch_pipeline.stalled``
                # WARNING (the symmetric twin of the ``_draining`` drop below, which already
                # accounts; DEC-PULL-HARDENING-FOLLOWUP-001, MIGRATION.md #996).
                if self._stopped.is_set():
                    self._mark_completed()
                    self._raise_if_stopped()
                # A pull-end drain with this window still un-enqueued: the consumer for this shard
                # is wedged (its queue stays full), so DROP the window fail-fast — it re-fetches
                # next pull (idempotent keep-first). Account for it so in-flight stays accurate.
                if self._draining.is_set():
                    self._mark_completed()
                    _LOG.warning("fetch_pipeline.submit_dropped_draining", shard=shard)
                    return
                # Otherwise the consumer is healthy-but-slow: keep blocking (retry) — the window
                # is never dropped while the pull is live (back-pressure preserved).

    def _raise_if_stopped(self) -> None:
        """Re-raise the first captured consumer exception if the pool hard-stopped."""
        if self._stopped.is_set():
            exc = self._exc
            if exc is not None:
                raise exc

    def _pause_if_wedged(self) -> None:
        """PAUSE the producer at the submit gate while the lake volume is WEDGED.

        DEC-VOLUME-KEEPALIVE-001 layers 2+3, the honest fallback to the best-effort
        keepalive prevention (the daemon only OBSERVES — it cannot raise into the
        pull, so the escalation is raised HERE, on the producer thread). While the
        keepalive's ``wedged`` event is set this loops on a bounded wait, so:

        * the producer never reaches the CONFIRMED-unbounded spill ``fsync``
          (:meth:`_spill_full_window` → ``try_spill``'s producer-thread
          ``atomic_write_via``) against a wedged volume — a deliberate throughput
          SACRIFICE while the volume is down, self-recovering within one wait cycle
          of the keepalive clearing;
        * a peer pool halt still releases the pause (:meth:`_raise_if_stopped`
          re-check + the ``_stopped`` event wait wakes immediately on a trip);
        * a pull-end :meth:`drain` never deadlocks behind the pause — the window
          RETURNS into the normal enqueue/drop logic (the ``_draining`` fallback
          there drops it fail-fast; unsettled ledger row → re-fetched next pull);
        * a wedge outlasting the keepalive's ``halt_after_s`` escalates: the pool is
          tripped and a :class:`~brutex.data.errors.LakeWriteHalt` with reason
          ``volume_wedged`` is raised FROM THIS PRODUCER THREAD, riding the spines'
          ``_GLOBAL_STOP_ERRORS`` → graceful halt + the existing CRITICAL pull-halt
          alert (no new alert plumbing).

        One ``fetch_pipeline.paused_wedged`` WARNING per wedge episode (edge-latched;
        re-armed when a paused producer observes the recovery). A ``None`` keepalive
        (the default) is byte-identical to today. O(1) per healthy submit.
        """
        keepalive = self._keepalive
        if keepalive is None or not keepalive.wedged.is_set():
            return
        with self._pause_lock:
            if not self._pause_fired:
                self._pause_fired = True
                _LOG.warning(
                    "fetch_pipeline.paused_wedged",
                    halt_after_seconds=keepalive.halt_after_s,
                )
        while keepalive.wedged.is_set():
            self._raise_if_stopped()
            if self._draining.is_set():
                return
            if keepalive.wedged_seconds() >= keepalive.halt_after_s:
                halt = LakeWriteHalt("volume_wedged")
                self._trip(halt)
                raise halt
            # Wait on the STOP event (not a bare sleep) so a peer trip wakes the
            # pause immediately; the timeout re-polls the wedge state.
            self._stopped.wait(_CONSUMER_GET_POLL_SECONDS)
        with self._pause_lock:
            self._pause_fired = False

    def _run_consumer(self, shard: int) -> None:
        """Drain shard *shard*'s queue (then its OVERFLOW), processing each window's tail.

        Each tail's ``process`` is wrapped so a failure (disk-full / any sink error) is
        captured into the pool-wide stop (so the producer + drain re-raise it) — never
        swallowed. The consumer thread stays alive to drain the rest of its queue (so no
        producer blocks forever on a full queue), settling subsequent windows fail-fast.

        After each queue item — AND whenever the queue is momentarily empty — the consumer
        drains the shard's SPILL OVERFLOW deque (DEC-FETCH-NONBLOCKING-HANDOFF-001): every
        spilled window is reloaded from disk + processed, so the overflow can never strand work.

        The ``queue.get`` is TIMED (``_CONSUMER_GET_POLL_SECONDS``) so a consumer whose
        poison-pill could NOT be enqueued — its queue was FULL because a PEER consumer is
        wedged on ``sink.append`` and the producer's bounded poison ``put`` was skipped — still
        notices :attr:`_draining` and exits once its OWN queue + overflow drain (the
        guaranteed-exit companion to the best-effort producer poison; the BLOCKER fix).
        """
        q = self._queues[shard]
        while True:
            try:
                item = q.get(timeout=_CONSUMER_GET_POLL_SECONDS)
            except queue.Empty:
                # No queued work right now: first drain any spilled overflow (a full queue spilled
                # windows here), then — if the pull is draining and BOTH the queue and overflow are
                # empty — this consumer is done (exit even without a poison-pill, which may never
                # have been enqueued on a full queue).
                self._drain_overflow(shard)
                if self._draining.is_set() and not self._overflow[shard]:
                    return
                continue
            if item is self._poison:
                # Drain any remaining spilled overflow BEFORE honouring the poison so no spilled
                # window is stranded behind the stop signal.
                self._drain_overflow(shard)
                return
            # `item` is a RawFetchResult here: the queue only ever carries a RawFetchResult
            # or the unique poison-pill sentinel, and the sentinel already returned above.
            result = item if isinstance(item, RawFetchResult) else None
            if result is None:  # pragma: no cover — only poison + RawFetchResult are queued
                continue
            self._process_one(result)
            # Drain any spilled overflow that built up while this in-memory window processed.
            self._drain_overflow(shard)

    def _drain_overflow(self, shard: int) -> None:
        """Process every spilled window currently in shard *shard*'s overflow deque (FIFO).

        Pops one handle at a time under ``_overflow_lock`` (so a concurrent producer ``append`` +
        ``rehydrate`` is never observed half-done) and processes it OUTSIDE the lock (the tail's
        durable write must not hold the producer's overflow lock). A window the producer appends
        WHILE this drains is picked up on the next ``_run_consumer`` iteration. O(overflow) — each
        spilled window is reloaded + processed exactly once.
        """
        while True:
            with self._overflow_lock:
                if not self._overflow[shard]:
                    return
                result = self._overflow[shard].popleft()
            self._process_one(result)

    def _process_one(self, result: RawFetchResult) -> None:
        """Run ONE window's tail; capture any failure into the pool-wide stop.

        A SPILLED window (its bars on disk because the producer overflowed a full queue) is
        REHYDRATED first — the bars are reloaded + restored into the tail's closure via
        ``rehydrate`` — and the spill file is DISCARDED afterwards (the byte budget released) in a
        ``finally`` so a failed / stop-dropped tail never leaks the spill (DEC-FETCH-NONBLOCKING-HANDOFF-001).

        The spill LOAD is GUARDED (DEC-FETCH-SPILL-ISOLATION-001): it is real file I/O on a
        file an external actor may have removed (before the per-pull spill namespacing, a
        CONCURRENT pull's drain ``cleanup()`` swept the SHARED ``<lake>/.fetch-spill`` dir —
        deleting THIS pull's live spill files) or corrupted. Unguarded, a load failure
        propagated out of ``_process_one`` and KILLED THE CONSUMER THREAD SILENTLY — no
        ``_trip``, no ``_mark_completed`` — so :meth:`drain` returned CLEAN while the producer
        could wedge forever on the dead shard's full-queue back-pressure (the loop only exits
        on ``_stopped``/``_draining``). Now a load failure is handled like any other per-window
        failure: logged LOUD (``fetch_pipeline.spill_load_failed``), folded as a tail-side
        transient (so :meth:`drain`'s ``tail_transients`` surfaces the count honestly),
        completion-accounted, and the window is DEFERRED — its ledger row was never recorded so
        it stays UNSETTLED → re-fetched next pull (idempotent keep-first). The consumer keeps
        running; the bars are never silently lost (they re-fetch).

        Once the pool has hard-stopped (a prior failure), subsequent windows are DROPPED
        without processing — a full disk fails every remaining window identically, so re-
        running the tail would just hammer it. The dropped window's ledger row stays
        UNSETTLED → re-fetched next pull (idempotent keep-first). A dropped SPILLED window still
        has its spill file discarded (the ``finally``), so a halt never leaves stale spill bytes.
        """
        spilled = result.spilled
        try:
            if self._stopped.is_set():
                # Dropped fail-fast — still a "completion" for the in-flight accounting so the
                # watchdog does not flag a permanently-stuck window after a halt.
                self._mark_completed()
                return
            tail = result.tail
            if (
                isinstance(spilled, SpilledWindow)
                and self._spill_area is not None
                and tail.rehydrate is not None
            ):
                # REHYDRATE: reload the spilled bars + restore them into the tail's closure so
                # ``process`` reads the real bars (not the dropped-to-empty in-memory copy).
                # GUARDED (DEC-FETCH-SPILL-ISOLATION-001): a load failure (deleted / corrupt
                # spill file) must DEFER this ONE window — never kill the consumer thread.
                # Narrow to ``Exception`` so a KeyboardInterrupt / SystemExit still propagates.
                try:
                    bars = self._spill_area.load(spilled)
                except Exception as exc:  # pylint: disable=broad-exception-caught — any load failure defers the window
                    _LOG.warning(
                        "fetch_pipeline.spill_load_failed",
                        error_type=type(exc).__name__,
                        shard_key=tail.shard_key,
                    )
                    # Fold the deferral into the drain-surfaced totals (honest failure count)
                    # + advance the watchdog progress clock. The window's ledger row was never
                    # recorded → it stays UNSETTLED → re-fetched next pull (keep-first sink).
                    self.fold_tail_transient()
                    self._mark_completed()
                    return
                tail.rehydrate(bars)
            try:
                tail.process(tail)
            except Exception as exc:  # surface ANY tail FAILURE, never swallow
                # Narrow to ``Exception`` (NOT ``BaseException``) so a ``KeyboardInterrupt`` /
                # ``SystemExit`` is NOT captured as a recoverable failure — it propagates out of
                # the consumer so an interpreter shutdown / operator interrupt is honoured.
                #
                # PER-SYMBOL ISOLATION (DEC-FETCH-CORRUPT-PARTITION-ISOLATE-001): a
                # per-symbol-quarantinable ``BruteXError`` — e.g. a ``WriterError`` from a single
                # CORRUPT existing Parquet partition (``ParquetWriter._read_existing_bars``) — must
                # NOT trip the POOL-wide stop. In SYNC mode such an error is isolated to ONE symbol
                # by the outer ``run_groww_*_ingest`` quarantine (it is NOT in
                # ``_GLOBAL_STOP_ERRORS``); without this branch the async consumer's generic
                # ``except`` routed it to ``_trip`` → ``_stopped`` → every symbol/contract after it
                # quarantined for the rest of the pull (a severe availability regression for a
                # single bad file). We mirror the sync isolation: a quarantinable error advances
                # progress (so the watchdog re-arms) but does NOT set ``_stopped``. The window's
                # ledger row was NOT recorded (the tail raised before ``ledger.record``), so it
                # stays UNSETTLED → re-fetched next pull (idempotent keep-first; a re-pull that
                # re-reads the still-corrupt file re-isolates the SAME one window). A
                # ``LakeWriteHalt`` / quota (in ``_GLOBAL_STOP_ERRORS``) and any non-``BruteXError``
                # genuine code bug STILL trip the pool, exactly as before.
                if _is_pool_isolatable(exc):
                    _LOG.warning(
                        "fetch_pipeline.window_quarantined",
                        error_type=type(exc).__name__,
                        shard_key=tail.shard_key,
                    )
                else:
                    self._trip(exc)
            finally:
                # Advance progress on EVERY processed window (success or captured failure) so the
                # stall watchdog re-arms — a steadily-failing-but-progressing pipeline is not stuck.
                self._mark_completed()
        finally:
            # Discard the spill file + release its bytes on EVERY exit (success, captured tail
            # failure, or stop-drop) so the bounded spill area never leaks — the bars are already
            # durable (success) or re-pulled next pull (failure/drop, the ledger row is unsettled).
            if isinstance(spilled, SpilledWindow) and self._spill_area is not None:
                self._spill_area.discard(spilled)

    def _mark_completed(self) -> None:
        """Record ONE window finished — advance the watchdog progress clock + re-arm the stall."""
        with self._progress_lock:
            self._completed += 1
            self._last_progress = self._monotonic()
            self._stall_fired = False

    def _run_watchdog(self) -> None:
        """Poll for a stall: in-flight windows but no completion for the threshold → WARN once.

        A pure observation thread — it NEVER kills the pull. It exits when
        :meth:`drain` sets ``_watchdog_stop``. Fires ONE ``fetch_pipeline.stalled`` WARNING (+
        the optional ``on_stall`` hook) per stall episode, re-armed by the next completion, so a
        long stall is loud but not log-spammy.
        """
        while not self._watchdog_stop.wait(_WATCHDOG_POLL_SECONDS):
            self._check_stall()

    def _check_stall(self) -> None:
        """Fire the stall WARNING if work is in flight and no completion advanced for too long."""
        now = self._monotonic()
        with self._progress_lock:
            in_flight = self._submitted - self._completed
            idle = now - self._last_progress
            should_fire = (
                in_flight > 0 and idle >= self._stall_watchdog_seconds and (not self._stall_fired)
            )
            if should_fire:
                self._stall_fired = True
        if not should_fire:
            return
        _LOG.warning(
            "fetch_pipeline.stalled",
            in_flight=in_flight,
            idle_seconds=round(idle, 1),
            threshold_seconds=self._stall_watchdog_seconds,
        )
        if self._on_stall is not None:
            try:
                self._on_stall(idle)
            except Exception:  # pylint: disable=broad-exception-caught — best-effort observation
                _LOG.exception("fetch_pipeline.on_stall_failed")

    def _trip(self, exc: BaseException) -> None:
        """Record the FIRST consumer failure and set the pool-wide stop flag."""
        with self._exc_lock:
            if self._exc is None:
                self._exc = exc
        self._stopped.set()
        if isinstance(exc, LakeWriteHalt):
            _LOG.warning("fetch_pipeline.halt", reason=exc.reason)
        else:
            _LOG.warning("fetch_pipeline.consumer_failed", error_type=type(exc).__name__)

    def _stop_keepalive(self) -> None:
        """Stop the wired volume-keepalive daemon (guarded, idempotent, drain-side).

        DEC-VOLUME-KEEPALIVE-001 lifecycle: the pipeline OWNS the keepalive's stop —
        called from :meth:`drain` so no ``volume-keepalive`` thread outlives the pull
        on ANY exit path (clean drain, early idle return, or a captured-halt
        re-raise). A stop failure is logged and swallowed: tearing down the observer
        can never block the pull's ``finally``.
        """
        keepalive = self._keepalive
        if keepalive is None:
            return
        try:
            keepalive.stop()
        except Exception:  # pylint: disable=broad-exception-caught — best-effort teardown, never blocks drain
            _LOG.exception("fetch_pipeline.keepalive_stop_failed")

    def drain(self) -> None:
        """Flush every queued window's tail, then stop + join all consumers; re-raise on halt.

        Called ONCE at pull end (producers already stopped submitting) BEFORE the final
        ledger flush, so every still-queued window is processed + ledger-recorded before the
        ledger persists (settle-after-durable at pull granularity). Sets :attr:`_draining`
        then delivers one poison-pill per shard via a BOUNDED, NON-BLOCKING ``put`` so every
        still-queued :class:`RawFetchResult` is processed (unless the pool already hard-stopped,
        in which case the remaining windows are dropped fail-fast — they re-fetch next pull),
        then joins each thread.

        The poison delivery is BEST-EFFORT (the BLOCKER fix): when a consumer is wedged inside
        ``sink.append`` (a stalled external SSD / NFS hiccup) AND its bounded queue is FULL,
        an UNBOUNDED ``q.put(poison)`` would block FOREVER and the join below would never run —
        hanging the pull's ``finally``, never persisting the ledger, with no signal. So the
        poison is enqueued with a bounded ``put`` and on :class:`queue.Full` we FALL THROUGH to
        the bounded join: a non-wedged consumer exits via its timed ``queue.get`` seeing
        :attr:`_draining`; the WEDGED consumer is REPORTED (``fetch_pipeline.drain_join_timeout``).
        The stall watchdog is kept ALIVE across the join (stopped only AFTER it) so a wedge is
        still surfaced as ``fetch_pipeline.stalled``.

        After :meth:`drain` returns, if a consumer captured a failure (e.g. a
        :class:`~brutex.data.errors.LakeWriteHalt`) it is RE-RAISED here so the pull halts
        through the spine's ``_GLOBAL_STOP_ERRORS`` path. Idempotent: a second call (no live
        threads) is a no-op.
        """
        # Signal the drain FIRST so a consumer whose poison could not be enqueued (full queue,
        # wedged peer) exits via its timed get once its own queue empties. Setting ``_draining``
        # also releases any producer paused at the wedge gate (``_pause_if_wedged``).
        self._draining.set()
        # Stop the volume-keepalive daemon on EVERY drain path (the early idle return, the
        # normal join path, AND the captured-halt re-raise below) — ONE call here covers all
        # three; idempotent + guarded so a stop failure never blocks the drain.
        self._stop_keepalive()
        if not any(t.is_alive() for t in self._threads):
            self._watchdog_stop.set()
            self._watchdog.join(timeout=self._drain_join_timeout_seconds)
            return
        # Best-effort BOUNDED poison delivery: a non-blocking put so a FULL queue (a wedged peer
        # consumer) can NEVER block the drain. On queue.Full the consumer still exits via the
        # _draining check on its timed get — fall through to the bounded join either way.
        for q in self._queues:
            try:
                q.put(self._poison, timeout=_CONSUMER_GET_POLL_SECONDS)
            except queue.Full:
                # A wedged consumer's queue is full — the timed-get _draining check is its exit
                # path; the bounded join below reports it if it stays wedged.
                _LOG.warning("fetch_pipeline.poison_queue_full")
        # Bounded join — a live consumer exits on the poison-pill (or the _draining timed-get)
        # within one write cycle, so this is a SAFETY net: a wedged consumer is REPORTED (never
        # hangs the pull's finally forever). The daemon thread does not keep the process alive,
        # so an unjoined-but-reported thread can never wedge shutdown. The watchdog stays ALIVE
        # across this join so a wedge is still surfaced as a stall.
        for t in self._threads:
            t.join(timeout=self._drain_join_timeout_seconds)
            if t.is_alive():
                _LOG.warning(
                    "fetch_pipeline.drain_join_timeout",
                    thread=t.name,
                    timeout_seconds=self._drain_join_timeout_seconds,
                )
        # Only NOW stop the watchdog — it was alive across the join, so a stall during the
        # (possibly-wedged) drain was still surfaced.
        self._watchdog_stop.set()
        self._watchdog.join(timeout=self._drain_join_timeout_seconds)
        processed, bars_appended, conflicts = self.totals
        dup_rows, max_delta = self.vendor_surface_totals
        _LOG.info(
            "fetch_pipeline.drained",
            processed_windows=processed,
            bars_appended=bars_appended,
            conflicts=conflicts,
            tail_transients=self.tail_transients,
            vendor_dup_rows_dropped=dup_rows,
            max_close_delta=None if max_delta is None else str(max_delta),
        )
        # Best-effort sweep any straggler spill files (a window dropped fail-fast on a halt, or a
        # consumer killed mid-tail). Each processed window already discarded its own spill in
        # ``_process_one``'s finally, so this is the bounded final cleanup. Never raises.
        if self._spill_area is not None:
            self._spill_area.cleanup()
        exc = self._exc
        if exc is not None:
            raise exc
