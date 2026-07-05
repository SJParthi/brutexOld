"""Sharded parallel write-consumer that decouples the fetch lane from the lake write.

DEC-FETCH-WRITE-OVERLAP-LEVER1-001 (Lever 1 of DEC-FETCH-WRITE-OVERLAP-001).

## The problem this solves (the evidence)

The deep-partition bench (#910) PROVED the lake write is **O(month-depth) per save**
(12 ms → 213 ms over 500 → 15,000 stored rows) and runs **serially on the fetch worker
thread** under the per-partition lock. So each fetch connection sits idle ~74 % of the
time waiting on its own write, capping the achieved rate at ~2.8-3.2 r/s BELOW the 5-8
the rate controller allows (DEC-FETCH-WRITE-OVERLAP-001). The hard requirement
(operator decree): the fetch lane must NEVER be impacted by internal code/db work — the
ONLY legitimate limiter is Groww's rate.

## The design (verified Design B)

A :class:`ShardedWriteConsumer` runs *W* background worker threads, each draining its
OWN bounded :class:`queue.Queue`. The fetch worker, instead of calling the synchronous
:meth:`~brutex.data.sink.BarSink.append` itself, ENQUEUES a :class:`WriteRequest` and
BLOCKS on its one-shot :class:`ResultBox` until the consumer's durable
``atomic_write_via`` returns the real :class:`~brutex.data.sink.SinkAppendResult`. While
this one fetch worker waits, the OTHER fetch connections keep fetching — that overlap is
the whole point: the write no longer occupies a fetch connection's time budget.

Three invariants make this safe (the failure matrix the operator demanded — ALL of it):

1. **Settle-after-durable (Golden Rule #7, the #1 invariant — NO silent data loss).**
   The ledger is recorded by the spine ONLY after :meth:`append` returns, and
   :meth:`append` returns ONLY after the consumer thread's durable
   :meth:`~brutex.data.sink.BarSink.append` (→ ``atomic_write_via`` → ``os.replace``)
   completed. A settle-BEFORE-durable is therefore structurally impossible — the spine
   never sees a :class:`SinkAppendResult` until the bytes are on disk. The ``#919``
   back-channel ``bars_appended`` stays REAL (it is the underlying sink's own count).

2. **One partition → one shard (no single-serializer regression — the #886/#921 trap).**
   Each request is routed to a shard by ``hash(resolved series coordinates) % W``,
   so ALL writes to one ``(exchange, segment, symbol, interval)`` series always land on
   the SAME shard worker. The per-partition ``threading.Lock`` +
   :func:`~brutex.data.partition_lock.partition_write_lock` ``flock`` inside the writer
   are PRESERVED (a different shard could still race a shared month partition across two
   DIFFERENT series — but two writes to the SAME series never run on two shards at once).
   A SINGLE serialiser would be a NET REGRESSION (it would re-serialise all writes at
   ~3/s); *W* independent shards keep the writes parallel.

3. **Bounded queue = back-pressure, not OOM.** Each shard's queue has a ``maxsize``; a
   ``put`` BLOCKS when full, so a slow disk throttles the producer (the fetch lane) down
   to the write rate instead of growing an unbounded backlog that OOMs. The fetch lane is
   never *faster* than the disk can absorb — it just stops *idling* on its own writes.

## Failure matrix (every permutation handled)

* **disk-full / OOM at the consumer write** → the writer raises
  :class:`~brutex.data.errors.LakeWriteHalt`; the consumer captures it into the
  :class:`ResultBox` and the fetch worker RE-RAISES it (a member of both ingest spines'
  ``_GLOBAL_STOP_ERRORS`` → graceful pull halt). On a ``LakeWriteHalt`` the consumer also
  trips a pool-wide STOP flag so every queued + in-flight request fails fast (a full disk
  fails every remaining window identically — drain-to-durable would just hammer the full
  disk).
* **consumer thread dies mid-drain** → the box is settled with the captured exception and
  the fetch worker re-raises it. A SUCCESS is NEVER settled for a write that did not
  durably complete (the box is set to success ONLY on the line AFTER ``append`` returns).
* **end-of-pull shutdown with a non-empty queue** → :meth:`drain` stops producers, then
  sends one poison-pill per shard and JOINs every worker, so every still-queued request is
  written durably (and its box settled) BEFORE the caller proceeds to ``end_pull_ledger``.
* **hard exit (kill) with queued work** → a request still in the queue at a hard kill was
  never settled, so its ledger row stays UNSETTLED → re-fetched next pull (idempotent
  keep-first sink). No half-state, no silent loss.
* **keep-first / order-independence** → the underlying writer's merge is order-independent
  (it unions by timestamp); routing to a deterministic shard does not change which bytes
  land. Property-tested.
* **partial/zero-price bar, 2-dp tick grid** → handled UPSTREAM on the fetch worker
  (session filter + the writer's own ``_tick_round``); the consumer ships bars verbatim.
* **clock-skew** → durability is the ``os.replace`` RETURN, never a timestamp.

## Honest envelope (Golden Rule #10)

``write_pool=None`` (the default) reproduces TODAY's fully-synchronous path
BYTE-IDENTICALLY — the spine calls the sink directly, no thread, no queue. The decouple
ships ONLY because the A/B bench proved a real throughput gain
(``tests/bench/test_write_consumer_overlap_bench.py``). The consumer cannot exceed the
vendor rate (it only removes the SELF-inflicted write idle); a catastrophic disk-full
still halts the pull gracefully. This is stdlib-only at import (``queue`` / ``threading``
/ ``hashlib``) — no heavy dep is pulled in just by importing the module.
"""

from __future__ import annotations

import hashlib
import os
import queue
import threading
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, cast, runtime_checkable

import structlog

from brutex.data.errors import LakeWriteHalt
from brutex.data.partition import partition_for_timestamp
from brutex.data.sink import BarSink, SinkAppendResult, vendor_surface_from_write_results

if TYPE_CHECKING:  # pragma: no cover
    from brutex.data.models import CandleBar

__all__ = [
    "BatchingWriteConsumer",
    "ResultBox",
    "ShardedWriteConsumer",
    "WriteConsumer",
    "WriteConsumerSink",
    "WriteRequest",
    "resolve_write_batch_enabled",
    "resolve_write_pool_size",
]

_LOG = structlog.get_logger("brutex.data.write_consumer")

#: Default per-shard queue depth. Bounded so a slow disk back-pressures the producer
#: instead of growing an unbounded backlog (OOM). Small — the producer only needs a
#: handful of in-flight writes to keep the fetch lane busy while one write drains.
_DEFAULT_QUEUE_MAXSIZE: int = 64


class ResultBox:
    """A one-shot settle slot the fetch worker blocks on until its write is durable.

    The fetch worker enqueues a :class:`WriteRequest` carrying ONE box, then calls
    :meth:`get` which blocks until a shard worker calls EITHER :meth:`set_result`
    (the durable :class:`~brutex.data.sink.SinkAppendResult`) or :meth:`set_exception`
    (a write failure — captured, NEVER a fabricated success). Exactly one of the two is
    ever set; :meth:`get` then returns the result or RE-RAISES the captured exception on
    the fetch-worker thread, so a :class:`~brutex.data.errors.LakeWriteHalt` raised on a
    consumer thread propagates into the spine's ``_GLOBAL_STOP_ERRORS`` path unchanged.
    """

    __slots__ = ("_done", "_exc", "_result")

    def __init__(self) -> None:
        self._done = threading.Event()
        self._result: SinkAppendResult | None = None
        self._exc: BaseException | None = None

    def set_result(self, result: SinkAppendResult) -> None:
        """Settle the box with a durable result (called AFTER ``append`` returns)."""
        self._result = result
        self._done.set()

    def set_exception(self, exc: BaseException) -> None:
        """Settle the box with the captured write failure (NEVER a success)."""
        self._exc = exc
        self._done.set()

    def get(self) -> SinkAppendResult:
        """Block until the box is settled; return the result or re-raise the exception.

        Returns the durable :class:`~brutex.data.sink.SinkAppendResult` on success, or
        re-raises the exception the consumer captured (e.g. a
        :class:`~brutex.data.errors.LakeWriteHalt`) on the CALLING (fetch-worker) thread —
        so settle-after-durable holds and a halt reaches the spine's global-stop handling.
        """
        self._done.wait()
        exc = self._exc
        if exc is not None:
            # Re-raise the captured failure on THIS (fetch-worker) thread. Bound to a
            # local first so the narrowed `BaseException` (never `None`) is what is
            # raised — a settled box with `_exc is not None` is the ONLY raise path.
            raise exc
        result = self._result
        if result is None:
            # Defensive: a settled box (``_done`` set) with neither a result nor an
            # exception is an impossible state — ``set_result`` always passes a real
            # result and ``set_exception`` always passes a real exception. Surface it
            # as a RuntimeError rather than silently returning ``None`` (which would
            # break the SinkAppendResult contract and corrupt settle-after-durable).
            raise RuntimeError("ResultBox settled with neither a result nor an exception")
        return result


@dataclass(frozen=True, slots=True)
class WriteRequest:
    """One immutable unit of write work routed to a shard.

    Carries the full partition coordinates + bars + ``merge_mode`` (so the consumer's
    :meth:`~brutex.data.sink.BarSink.append` call is byte-identical to the synchronous
    one the spine would have made) plus the :class:`ResultBox` the producer blocks on.
    """

    exchange: str
    segment: str
    symbol: str
    interval: str
    bars: Sequence[CandleBar]
    merge_mode: str
    box: ResultBox = field(compare=False)

    def shard_key(self) -> str:
        """The deterministic routing key — the resolved partition SERIES coordinates.

        All writes to one ``(exchange, segment, symbol, interval)`` series hash to the
        SAME shard, so two writes to the same series never run on two shards concurrently
        (preserving the per-partition lock + ``flock`` invariant). The ``(year, month)``
        is deliberately EXCLUDED: a 30/90-day fetch chunk straddles a month boundary, so
        adjacent windows of ONE series must stay on one shard to serialise their
        shared-month merge — keying by series (not by partition file) guarantees that
        without needing to resolve the timestamp here.
        """
        return f"{self.exchange}\x00{self.segment}\x00{self.symbol}\x00{self.interval}"


def _shard_index(key: str, shards: int) -> int:
    """Map a routing *key* to a shard in ``[0, shards)`` (stable, well-distributed).

    Uses ``blake2b`` (stdlib, fast, no per-process salt unlike ``hash()``) so the routing
    is deterministic across processes/runs — a property the byte-parity oracle relies on
    when it compares ``W=None`` vs ``W=2`` vs ``W=4``.
    """
    digest = hashlib.blake2b(key.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") % shards


class ShardedWriteConsumer:
    """*W* background write workers, each draining its own bounded queue.

    Build one per pull at the live site, wire it into the ingest spines through a
    :class:`WriteConsumerSink`, and :meth:`drain` it at pull end BEFORE saving the window
    ledger. The producer (fetch worker) submits via :meth:`submit`, which blocks on
    back-pressure (a full shard queue) and then blocks on the durable result.
    """

    def __init__(
        self, *, sink: BarSink, workers: int, queue_maxsize: int = _DEFAULT_QUEUE_MAXSIZE
    ) -> None:
        """Start *workers* shard threads, each draining a ``queue_maxsize``-bounded queue.

        Parameters
        ----------
        sink
            The REAL durable sink (a :class:`~brutex.data.sink.ParquetBarSink`); each shard
            worker calls ``sink.append`` on it. The sink's per-partition lock + ``flock``
            keep cross-shard same-partition writes safe.
        workers
            Number of shard worker threads (``W``). Must be ``>= 1``.
        queue_maxsize
            Per-shard bounded queue depth (back-pressure; ``>= 1``).
        """
        if workers < 1:
            raise ValueError(f"workers must be >= 1, got {workers}")
        if queue_maxsize < 1:
            raise ValueError(f"queue_maxsize must be >= 1, got {queue_maxsize}")
        self._sink = sink
        self._workers = workers
        # `object` poison-pill sentinel: a unique instance per consumer so a stray
        # WriteRequest can never be mistaken for the stop signal.
        self._poison: object = object()
        self._queues: list[queue.Queue[WriteRequest | object]] = [
            queue.Queue(maxsize=queue_maxsize) for _ in range(workers)
        ]
        # A pull-wide hard-stop flag: tripped on a LakeWriteHalt so every queued +
        # subsequently-submitted request fails fast instead of hammering a full disk.
        self._stopped = threading.Event()
        self._halt_exc: LakeWriteHalt | None = None
        self._halt_lock = threading.Lock()
        self._threads = [
            threading.Thread(
                target=self._run_shard,
                args=(i,),
                name=f"write-consumer-{i}",
                daemon=True,
            )
            for i in range(workers)
        ]
        for t in self._threads:
            t.start()

    @property
    def workers(self) -> int:
        """Number of shard worker threads (``W``)."""
        return self._workers

    def submit(self, request: WriteRequest) -> SinkAppendResult:
        """Enqueue *request*, block on back-pressure, then block on the durable result.

        Routes to ``shard = blake2b(series) % W``, ``put``s the request (blocking when the
        shard queue is full — the back-pressure that throttles the fetch lane to the write
        rate, never an OOM), then blocks on the request's :class:`ResultBox` until the
        consumer's durable write returns. Returns the real
        :class:`~brutex.data.sink.SinkAppendResult` (so the spine settles the ledger AFTER
        the durable write) or re-raises the consumer's captured exception (e.g. a
        :class:`~brutex.data.errors.LakeWriteHalt`) on THIS thread.

        If the consumer already hard-stopped (a prior ``LakeWriteHalt``), the request is
        rejected immediately with that halt — a full disk fails every remaining window.
        """
        halt = self._halt_exc
        if self._stopped.is_set() and halt is not None:
            # Fail fast — the disk is full / OOM; do not queue more work onto it.
            raise halt
        shard = _shard_index(request.shard_key(), self._workers)
        self._queues[shard].put(request)
        return request.box.get()

    def _run_shard(self, shard: int) -> None:
        """Drain shard *shard*'s queue until the poison-pill, writing each request durably.

        Each request's durable :meth:`~brutex.data.sink.BarSink.append` is wrapped so its
        result (success) OR its exception (failure — captured, never a fabricated success)
        is settled into the request's box; a :class:`~brutex.data.errors.LakeWriteHalt`
        additionally trips the pool-wide stop so the rest of the pull fails fast.
        """
        q = self._queues[shard]
        while True:
            item = q.get()
            if item is self._poison:
                return
            # `item` is a WriteRequest here: the queue only ever carries a WriteRequest or
            # the unique poison-pill sentinel, and the sentinel already returned above.
            # `cast` narrows the type for the checkers without a strippable `assert`
            # (which bandit B101 flags) and without an unreachable runtime branch.
            self._write_one(cast(WriteRequest, item))

    def _write_one(self, request: WriteRequest) -> None:
        """Durably write ONE request and settle its box (result XOR captured exception)."""
        try:
            result = self._sink.append(
                exchange=request.exchange,
                segment=request.segment,
                symbol=request.symbol,
                interval=request.interval,
                bars=request.bars,
                merge_mode=request.merge_mode,
            )
        except LakeWriteHalt as exc:
            # A full disk / OOM fails EVERY remaining window identically — trip the
            # pool-wide stop so queued + future submits fail fast (no disk hammering),
            # then settle THIS box with the halt so its producer re-raises into the
            # spine's _GLOBAL_STOP_ERRORS path (graceful pull halt).
            self._trip_halt(exc)
            request.box.set_exception(exc)
        except Exception as exc:  # surface ANY write FAILURE, never settle a success
            # A consumer thread must NEVER settle a SUCCESS for a write that did not
            # durably complete (Golden Rule #7). Capture the failure into the box; the
            # producer re-raises it (a domain error is quarantined per-symbol; a real bug
            # propagates). The shard thread stays alive to drain the rest of its queue.
            #
            # Narrow to ``Exception`` (NOT ``BaseException``) so a ``KeyboardInterrupt`` /
            # ``SystemExit`` is NOT swallowed or mis-settled as a recoverable write
            # failure — it propagates out of the shard worker so an interpreter shutdown /
            # operator interrupt is honoured. That request stays UN-settled (hard-exit
            # case in the failure matrix → re-fetched next pull, idempotent keep-first).
            request.box.set_exception(exc)
        else:
            # Durable: `append` returned → `atomic_write_via`'s `os.replace` completed.
            # This is the ONLY line that settles a SUCCESS — strictly after the write.
            request.box.set_result(result)

    def _trip_halt(self, exc: LakeWriteHalt) -> None:
        """Record the first :class:`LakeWriteHalt` and set the pool-wide stop flag."""
        with self._halt_lock:
            if self._halt_exc is None:
                self._halt_exc = exc
        self._stopped.set()

    def drain(self) -> None:
        """Flush every queued request durably, then stop + join all shard workers.

        Called ONCE at pull end (producers already stopped submitting). Sends one
        poison-pill per shard AFTER the queued work so every still-queued
        :class:`WriteRequest` is written durably (and its box settled) before the worker
        exits; then joins each thread. After :meth:`drain` returns, the caller may safely
        save the window ledger — every settled window is durable. Idempotent: a second
        call (no live threads) is a no-op.
        """
        if not any(t.is_alive() for t in self._threads):
            return
        for q in self._queues:
            q.put(self._poison)
        for t in self._threads:
            t.join()
        if self._halt_exc is not None:
            _LOG.warning("write_consumer.drained_after_halt", reason=self._halt_exc.reason)


@runtime_checkable
class WriteConsumer(Protocol):
    """The common contract a :class:`WriteConsumerSink` routes every append through.

    Both background consumers satisfy it: the per-window
    :class:`ShardedWriteConsumer` (one durable write per submitted window) and the
    coalescing :class:`BatchingWriteConsumer` (many same-series windows merged into one
    durable write per drain cycle). The sink only needs ``submit`` (block-until-durable)
    and ``drain`` (flush + join at pull end), so a single Protocol lets the live build
    site swap the two behind the ``BRUTEX_WRITE_BATCH`` flag with no other change
    (CLAUDE.md §11 COMMON — one wiring surface).
    """

    def submit(self, request: WriteRequest) -> SinkAppendResult:
        """Enqueue *request*; block on back-pressure then on the durable result.

        Parameters
        ----------
        request:
            The write request to enqueue and settle durably.

        Returns
        -------
        SinkAppendResult
            The durable append outcome for *request*.
        """
        ...

    def drain(self) -> None:
        """Flush every pending request durably, then stop + join all workers."""
        ...


class WriteConsumerSink:
    """A :class:`~brutex.data.sink.BarSink` that routes every ``append`` through a pool.

    Drop-in for the synchronous :class:`~brutex.data.sink.ParquetBarSink` the spines use:
    the spine calls :meth:`append`, which builds a :class:`WriteRequest` + :class:`ResultBox`,
    submits to a :class:`WriteConsumer` (either :class:`ShardedWriteConsumer` or
    :class:`BatchingWriteConsumer`), and BLOCKS until the durable write returns the real
    :class:`~brutex.data.sink.SinkAppendResult`. So the spine's existing
    settle-after-durable ledger ordering is preserved BYTE-FOR-BYTE — the only change is
    that OTHER fetch connections fetch during this one's wait. An empty ``bars`` append is
    short-circuited (no enqueue) so it matches the underlying sink's empty no-op exactly.
    """

    def __init__(self, *, consumer: WriteConsumer) -> None:
        self._consumer = consumer

    def append(
        self,
        *,
        exchange: str,
        segment: str,
        symbol: str,
        interval: str,
        bars: Sequence[CandleBar],
        merge_mode: str = "keep-first",
    ) -> SinkAppendResult:
        """Route the write through the pool; block until it is durable, then return it."""
        if not bars:
            # Mirror ParquetBarSink's empty no-op exactly — never enqueue an empty write.
            return SinkAppendResult(bars_appended=0, partitions_written=0, write_results=())
        request = WriteRequest(
            exchange=exchange,
            segment=segment,
            symbol=symbol,
            interval=interval,
            bars=bars,
            merge_mode=merge_mode,
            box=ResultBox(),
        )
        return self._consumer.submit(request)


def resolve_write_pool_size(workers: int | None) -> int | None:
    """Normalise a requested write-pool size to ``None`` (sync) or a clamped ``W >= 1``.

    ``None`` or a value ``< 1`` → ``None`` (the synchronous, byte-identical path — the
    decouple is OFF). Any ``>= 1`` is the worker count. A thin helper so the live build
    site + tests share ONE normalisation (CLAUDE.md §11 COMMON).
    """
    if workers is None or workers < 1:
        return None
    return workers


#: Environment flag that opts the live pull into the coalescing batched write path
#: (DEC-WRITE-BATCH-COALESCE-001). DEFAULT OFF: the synchronous, byte-identical path
#: stays the production default until the M4 A/B proof lands (Golden Rule #10 — a perf
#: feature ships dormant until it is measured on the real envelope). Truthy values:
#: ``1`` / ``true`` / ``yes`` / ``on`` (case-insensitive). Anything else (incl. unset,
#: blank, ``0``, ``false``) keeps the batched path OFF.
WRITE_BATCH_ENV: str = "BRUTEX_WRITE_BATCH"

_WRITE_BATCH_TRUTHY: frozenset[str] = frozenset({"1", "true", "yes", "on"})


def resolve_write_batch_enabled(raw: str | None = None) -> bool:
    """True iff the coalescing batched write path is opted in via ``BRUTEX_WRITE_BATCH``.

    Reads :data:`WRITE_BATCH_ENV` when *raw* is ``None`` (the live-site call); a test may
    pass an explicit value. Fail-SAFE: ONLY an explicit truthy token enables it, so a
    typo / unset / blank / ``0`` keeps the synchronous default (DEC-WRITE-BATCH-COALESCE-001;
    CLAUDE.md §11 COMMON — one flag-read for the live site + tests). O(1).
    """
    value = os.environ.get(WRITE_BATCH_ENV) if raw is None else raw
    if value is None:
        return False
    return value.strip().lower() in _WRITE_BATCH_TRUTHY


class BatchingWriteConsumer:
    """A coalescing background writer: many same-series windows → ONE merge-write/cycle.

    ## Why this beats the per-window :class:`ShardedWriteConsumer`

    The lake merge-write is **O(month-depth) per save and ~constant in the batch size**
    (the cost is dominated by re-reading the whole stored month — ~163 ms regardless of
    whether 1 or 50 new bars are merged, #910 evidence). The per-window consumer still
    runs ONE merge per window, so it cannot beat the serial path (the GIL-held merge binds
    both — the documented NO-GO of DEC-FETCH-WRITE-OVERLAP-LEVER1-001). This consumer
    instead COALESCES: a single background writer drains ALL immediately-pending requests,
    GROUPS them by ``(series, year, month)`` partition, and issues ONE ``sink.append`` per
    group — so N windows queued for one deep month cost ONE ~163 ms merge, not N. The
    achieved rate climbs toward the vendor rate because the per-window write amortises to
    ~163/N ms (the A/B bench proves the real margin —
    ``tests/bench/test_write_consumer_overlap_bench.py``).

    ## Failure matrix (every permutation handled — Golden Rule #7)

    * **settle-after-durable** — a request's :class:`ResultBox` is settled ONLY after its
      group's ``sink.append`` (→ ``atomic_write_via`` → ``os.replace``) returns. A success
      is NEVER settled before the bytes are durable; the spine's ``ledger.record`` runs
      strictly after.
    * **crash mid-batch** — one ``atomic_write_via`` (tmp→fsync→``os.replace``) per
      coalesced group: a crash BEFORE the rename leaves EVERY request in that group
      un-settled → re-fetched next pull (idempotent keep-first union, no half-state).
    * **disk-full / OOM** — the writer's :class:`LakeWriteHalt` settles EVERY box in the
      failing batch WITH the halt and trips a pull-wide stop, so producers re-raise into
      the spines' ``_GLOBAL_STOP_ERRORS`` (graceful halt) and queued/future submits fail
      fast (no disk-hammering).
    * **partition lock** — coalescing is PER SERIES, so one merge targets one partition
      under the existing per-partition :class:`threading.Lock` + ``flock``; fewer + larger
      writes mean LESS lock contention, and a ``PartitionLockTimeout`` from the sink
      propagates per-window (defer still works) exactly like the synchronous path.
    * **bounded queue** — a single ``maxsize``-bounded queue: ``submit`` BLOCKS when full,
      back-pressuring the fetch lane to the write rate (never OOM).
    * **drain** — a poison-pill flushes EVERY pending request durably (one final coalesced
      cycle) before :meth:`drain` returns, so the caller may save the window ledger knowing
      every settled window is on disk. Idempotent.
    * **per-window ``bars_appended`` (#919)** — attributed PER WINDOW (not per batch) via
      :func:`_coalesce_partition_attribution`, so the back-channel stays REAL.

    ## Honest envelope (Golden Rule #10)

    The coalescing window is "whatever is already queued when the writer wakes" — a single
    ``get`` (block) + drain-to-empty. So under light load (one window in flight) it
    degenerates to a per-window write (no worse than sync); the win materialises exactly
    when the fetch lane out-paces the disk (the write-bound regime the pull actually hits).
    A single writer thread is deliberate: the merge is GIL-bound, so MORE writer threads do
    not parallelise it — the win is FEWER merges, not concurrent ones. stdlib-only at import.
    """

    def __init__(
        self,
        *,
        sink: BarSink,
        queue_maxsize: int = _DEFAULT_QUEUE_MAXSIZE,
        max_coalesce: int = 256,
    ) -> None:
        """Start ONE background writer draining a ``queue_maxsize``-bounded queue.

        Parameters
        ----------
        sink
            The REAL durable sink (a :class:`~brutex.data.sink.ParquetBarSink`). Its
            per-partition lock + ``flock`` keep a coalesced merge safe against any peer.
        queue_maxsize
            Bounded queue depth (back-pressure; ``>= 1``).
        max_coalesce
            Safety cap on how many pending requests one drain cycle pulls before writing
            (``>= 1``) — bounds the per-cycle memory + a single group's combined bar count
            so a pathological backlog cannot build one unbounded merge.
        """
        if queue_maxsize < 1:
            raise ValueError(f"queue_maxsize must be >= 1, got {queue_maxsize}")
        if max_coalesce < 1:
            raise ValueError(f"max_coalesce must be >= 1, got {max_coalesce}")
        self._sink = sink
        self._max_coalesce = max_coalesce
        self._poison: object = object()
        self._queue: queue.Queue[WriteRequest | object] = queue.Queue(maxsize=queue_maxsize)
        self._stopped = threading.Event()
        self._halt_exc: LakeWriteHalt | None = None
        self._halt_lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, name="write-batch-consumer", daemon=True)
        self._thread.start()

    @property
    def workers(self) -> int:
        """The writer thread count — always ``1`` (one coalescing writer, by design)."""
        return 1

    def submit(self, request: WriteRequest) -> SinkAppendResult:
        """Enqueue *request*, block on back-pressure, then block on the durable result.

        ``put``s the request (blocking when the queue is full — the back-pressure that
        throttles the fetch lane to the write rate, never an OOM), then blocks on the
        request's :class:`ResultBox` until the writer's durable coalesced write returns
        THIS window's attributed :class:`~brutex.data.sink.SinkAppendResult` — or re-raises
        the writer's captured exception (e.g. a :class:`LakeWriteHalt`) on THIS thread.

        If the consumer already hard-stopped (a prior ``LakeWriteHalt``), the request is
        rejected immediately with that halt — a full disk fails every remaining window. The
        rejected request's box is ALSO settled with the halt (not only raised), so anyone
        holding the box — not just the submitting thread — observes the halt and never
        blocks forever on an unsettled box.
        """
        halt = self._halt_exc
        if self._stopped.is_set() and halt is not None:
            request.box.set_exception(halt)
            raise halt
        self._queue.put(request)
        return request.box.get()

    def _drain_queue_with_halt(self, halt: LakeWriteHalt) -> None:
        """After a halt, settle EVERY still-queued request with *halt* (never block).

        Once :meth:`_trip_halt` fires, the disk is full / OOM — every remaining window
        fails identically. The writer must NOT loop back to a blocking ``get`` (a window
        ``put`` before the trip but not yet collected would sit unsettled forever, blocking
        its producer); instead it drains the queue NON-blocking, settling each request's box
        with the halt, until empty or the poison-pill (which it pushes back so :meth:`drain`
        still joins cleanly). O(queued).
        """
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            if item is self._poison:
                self._queue.put(self._poison)
                return
            cast(WriteRequest, item).box.set_exception(halt)

    def _run(self) -> None:
        """Drain the queue until the poison-pill, coalescing each cycle's pending writes."""
        while True:
            batch = self._collect_batch()
            if batch is None:
                return
            self._write_batch(batch)
            # After a tripped halt every remaining window fails identically — fail-fast
            # settle the rest of the queue (never loop back to a blocking get, which would
            # leave a queued-but-uncollected window's box unsettled forever).
            halt = self._halt_exc
            if halt is not None:
                self._drain_queue_with_halt(halt)

    def _collect_batch(self) -> list[WriteRequest] | None:
        """Block for one request, then drain up to ``max_coalesce`` more without blocking.

        Returns the collected requests, or ``None`` when the poison-pill is reached (so the
        writer exits). A poison-pill encountered AFTER some requests are already collected
        is pushed back so the next cycle sees it — the collected requests are flushed first
        (drain-flushes-all). O(batch size).
        """
        first = self._queue.get()
        if first is self._poison:
            return None
        batch: list[WriteRequest] = [cast(WriteRequest, first)]
        while len(batch) < self._max_coalesce:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is self._poison:
                # Re-enqueue the pill so the NEXT cycle stops after this batch flushes.
                self._queue.put(self._poison)
                break
            batch.append(cast(WriteRequest, item))
        return batch

    def _write_batch(self, batch: Sequence[WriteRequest]) -> None:
        """Coalesce *batch* by ``(series, merge_mode, year, month)`` partition; ONE write per group.

        Every request's box is settled — a success ONLY after its group's durable write
        returns (settle-after-durable), an exception captured verbatim. A
        :class:`LakeWriteHalt` settles the whole failing group with the halt + trips the
        pull-wide stop. A request's box is NEVER left unsettled on a handled write outcome
        (so no producer blocks forever); only a hard interpreter kill leaves boxes unset
        (→ re-fetched next pull, idempotent).
        """
        # Group by (series coordinates, merge_mode, year, month) so each group maps to
        # EXACTLY one partition file written under ONE merge mode → one `_merge_write`. Two
        # requests of the SAME series in different months land in different groups (each its
        # own single write). ``merge_mode`` is part of the key so two requests to the SAME
        # partition with DIFFERENT modes (every spine write is ``keep-first`` today — this is
        # DEFENSIVE) never coalesce: `_write_one_group` applies ``reqs[0].merge_mode`` to the
        # whole union, so a mixed-mode group would silently mis-apply one request's mode to
        # the other (MINOR-1). Splitting the key makes that impossible. The empty no-op of an
        # empty-bars request is settled directly (mirrors the sink's empty short-circuit).
        groups: dict[tuple[str, str, int, int], list[WriteRequest]] = defaultdict(list)
        for request in batch:
            if not request.bars:
                request.box.set_result(
                    SinkAppendResult(bars_appended=0, partitions_written=0, write_results=())
                )
                continue
            series = request.shard_key()
            # A request's bars may straddle a month boundary; route each (year, month)
            # slice to its own partition group so one group is exactly one partition write.
            for (year, month), _slice in _group_request_by_partition(request).items():
                groups[(series, request.merge_mode, year, month)].append(request)

        # A request can appear in MULTIPLE groups (it straddled a month). Track each
        # request's remaining-group count + accumulate its per-partition attribution so its
        # box is settled exactly ONCE, after ALL its partitions are durable.
        remaining: dict[int, int] = {}
        accumulated: dict[int, list[WriteResultParts]] = defaultdict(list)
        for reqs in groups.values():
            for request in reqs:
                remaining[id(request)] = remaining.get(id(request), 0) + 1

        for (series, mode, year, month), reqs in groups.items():
            del series, mode
            self._write_one_group((year, month), reqs, remaining, accumulated)

    def _write_one_group(
        self,
        partition: tuple[int, int],
        reqs: Sequence[WriteRequest],
        remaining: dict[int, int],
        accumulated: dict[int, list[WriteResultParts]],
    ) -> None:
        """Do ONE coalesced merge for *reqs* (all targeting *partition*); settle boxes.

        The combined bars are the UNION (submission order) of every request's bars that fall
        in *partition* (a request straddling a month contributes only its slice for THIS
        partition; its other slices are written by their own groups). After the durable
        write returns, each request's ``bars_appended`` is attributed per-window and its
        conflicts carried, then — once every partition the request spanned has completed —
        its box is settled with the merged result.
        """
        per_request_bars: list[list[CandleBar]] = [_bars_in_partition(r, partition) for r in reqs]
        combined: list[CandleBar] = [bar for bars in per_request_bars for bar in bars]
        try:
            result = self._sink.append(
                exchange=reqs[0].exchange,
                segment=reqs[0].segment,
                symbol=reqs[0].symbol,
                interval=reqs[0].interval,
                bars=combined,
                merge_mode=reqs[0].merge_mode,
            )
        except LakeWriteHalt as exc:
            self._trip_halt(exc)
            for request in reqs:
                request.box.set_exception(exc)
            return
        except Exception as exc:  # surface ANY write FAILURE; never settle a success
            for request in reqs:
                request.box.set_exception(exc)
            return
        # Durable: the merge returned. Attribute the combined `bars_appended` PER WINDOW
        # (#919 back-channel) by first-occurrence of each distinct timestamp, then settle
        # each request once ALL its partitions are durable.
        counts = _attribute_bars_appended(per_request_bars, result.bars_appended)
        for request, added in zip(reqs, counts, strict=True):
            accumulated[id(request)].append(
                WriteResultParts(bars_appended=added, write_results=result.write_results)
            )
            remaining[id(request)] -= 1
            if remaining[id(request)] == 0:
                request.box.set_result(_merge_parts(accumulated[id(request)]))

    def _trip_halt(self, exc: LakeWriteHalt) -> None:
        """Record the first :class:`LakeWriteHalt` and set the pull-wide stop flag."""
        with self._halt_lock:
            if self._halt_exc is None:
                self._halt_exc = exc
        self._stopped.set()

    def drain(self) -> None:
        """Flush every pending request durably (one final coalesced cycle), then join.

        Called ONCE at pull end (producers stopped submitting). Sends the poison-pill;
        the writer flushes every still-queued request — coalesced — durably (settling each
        box) before exiting, then this joins the thread. After :meth:`drain` returns every
        settled window is on disk, so the caller may save the window ledger. Idempotent.

        Poison-pill delivery (MINOR-3): the worker is a live :class:`threading.Thread`
        that is continuously :meth:`queue.Queue.get`-consuming, so the bounded queue always
        drains and a blocking ``put`` cannot deadlock under normal operation. The ONE way a
        worker could die with a still-full queue is a ``BaseException`` (``KeyboardInterrupt``
        / ``SystemExit`` — interpreter teardown); the worker is a ``daemon`` thread (it does
        NOT keep the process alive) so that path is an interpreter shutdown anyway. To avoid
        any chance of this drain BLOCKING the pull's ``finally`` even then, the pill is sent
        ``put_nowait`` first; only if the queue is momentarily full do we fall back to a
        bounded blocking ``put`` (the live worker is consuming, so a slot frees within one
        write), re-checking liveness so a dead worker never wedges the pull.
        """
        if not self._thread.is_alive():
            return
        try:
            self._queue.put_nowait(self._poison)
        except queue.Full:
            # The live worker is consuming, so a slot frees within one write cycle; the
            # bounded blocking put then succeeds. If the worker has died (daemon hard-kill),
            # re-check liveness and skip — the process is tearing down regardless.
            if self._thread.is_alive():
                self._queue.put(self._poison)
        self._thread.join()
        if self._halt_exc is not None:
            _LOG.warning("write_consumer.batched_drained_after_halt", reason=self._halt_exc.reason)


@dataclass(frozen=True, slots=True)
class WriteResultParts:
    """One partition's contribution to a request that straddled multiple partitions."""

    bars_appended: int
    write_results: tuple[WriteResult, ...]


def _merge_parts(parts: Sequence[WriteResultParts]) -> SinkAppendResult:
    """Combine a request's per-partition parts into ONE :class:`SinkAppendResult`.

    Sums the per-partition ``bars_appended`` (each already attributed to THIS request) and
    concatenates the per-partition ``write_results`` (so the spine sees every partition's
    conflicts), with ``partitions_written`` = the number of distinct partitions written.
    The two vendor-behaviour counters (DEC-VENDOR-CONFLICT-SURFACE-001) ride the SAME
    COMMON fold :class:`~brutex.data.sink.ParquetBarSink` uses, so the pooled sink
    reports them identically. Honest attribution note (Golden Rule #10): a COALESCED
    group's :class:`~brutex.data.parquet_writer.WriteResult` describes the whole
    union write, so — exactly like ``conflicts`` today — a request sharing a
    coalesced group reads the group-level counters, never a silently-dropped zero.
    """
    bars_appended = sum(p.bars_appended for p in parts)
    write_results = tuple(wr for p in parts for wr in p.write_results)
    dup_rows, max_delta = vendor_surface_from_write_results(write_results)
    return SinkAppendResult(
        bars_appended=bars_appended,
        partitions_written=len(parts),
        write_results=write_results,
        vendor_dup_rows_dropped=dup_rows,
        max_close_delta=max_delta,
    )


def _group_request_by_partition(
    request: WriteRequest,
) -> dict[tuple[int, int], list[CandleBar]]:
    """Split *request*'s bars by ``(year, month)`` partition (mirrors the sink's grouping)."""
    out: dict[tuple[int, int], list[CandleBar]] = defaultdict(list)
    for bar in request.bars:
        out[partition_for_timestamp(bar.timestamp)].append(bar)
    return out


def _bars_in_partition(request: WriteRequest, partition: tuple[int, int]) -> list[CandleBar]:
    """The subset of *request*'s bars belonging to *partition* (submission order)."""
    return [b for b in request.bars if partition_for_timestamp(b.timestamp) == partition]


def _attribute_bars_appended(
    per_request_bars: Sequence[Sequence[CandleBar]],
    total_appended: int,
) -> list[int]:
    """Split a coalesced merge's combined ``bars_appended`` PER WINDOW (#919 back-channel).

    A coalesced merge unions every request's bars into ONE partition write, and the sink
    reports ONE ``total_appended``. **What the sink actually returns** (honesty, Golden
    Rule #10): ``ParquetBarSink.append`` returns ``bars_appended = len(bars)`` — the INPUT
    bar count it was handed (``sink.py:106``/``:209``), NOT a net-new / de-duplicated count
    (mirrored by ``spot_daily_resample.py:762``). For a coalesced merge the consumer passes
    the union's input length, so for distinct timestamps the per-window credits SUM to
    exactly the union size, which is the byte-for-byte serial outcome.

    This re-attributes ``total_appended`` to the individual windows so the back-channel stays
    PER-WINDOW (not per-batch), honouring keep-first union order:

    * each distinct timestamp is credited to the FIRST request (submission order) that
      carries it — exactly which window a serial keep-first append would have credited it
      to (a later request carrying the same timestamp gets no credit: it was a within-batch
      duplicate / conflict);
    * the running credited total is CAPPED at ``total_appended``, so each per-window display
      figure is **AT MOST** what a serial keep-first append would have shown and the
      per-window counts can never be INFLATED above the combined total. (The cap also covers
      the defensive case where ``total_appended`` is smaller than the distinct-timestamp
      count — see ``test_pre_existing_bars_credit_zero_caps_at_total``; the real
      ``ParquetBarSink`` returns ``len(bars)`` so it does not produce that input, but the
      cap keeps the attribution sound for any conforming :class:`BarSink`.)

    The returned counts SUM to ``min(distinct-timestamps, total_appended)`` and NEVER exceed
    ``total_appended`` — so the per-window ledger figures reconcile to the combined total and
    are never inflated (Golden Rule #7). O(bars).
    """
    seen: set[datetime] = set()
    credited = 0
    counts: list[int] = []
    for bars in per_request_bars:
        added = 0
        for bar in bars:
            if credited >= total_appended:
                break
            ts = bar.timestamp
            if ts not in seen:
                seen.add(ts)
                added += 1
                credited += 1
        counts.append(added)
    return counts


if TYPE_CHECKING:  # pragma: no cover
    from datetime import datetime

    from brutex.data.parquet_writer import WriteResult
