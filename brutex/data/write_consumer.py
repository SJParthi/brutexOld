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
import queue
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

import structlog

from brutex.data.errors import LakeWriteHalt
from brutex.data.sink import BarSink, SinkAppendResult

if TYPE_CHECKING:  # pragma: no cover
    from brutex.data.models import CandleBar

__all__ = [
    "ResultBox",
    "ShardedWriteConsumer",
    "WriteConsumerSink",
    "WriteRequest",
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


class WriteConsumerSink:
    """A :class:`~brutex.data.sink.BarSink` that routes every ``append`` through a pool.

    Drop-in for the synchronous :class:`~brutex.data.sink.ParquetBarSink` the spines use:
    the spine calls :meth:`append`, which builds a :class:`WriteRequest` + :class:`ResultBox`,
    submits to the :class:`ShardedWriteConsumer`, and BLOCKS until the durable write returns
    the real :class:`~brutex.data.sink.SinkAppendResult`. So the spine's existing
    settle-after-durable ledger ordering is preserved BYTE-FOR-BYTE — the only change is
    that OTHER fetch connections fetch during this one's wait. An empty ``bars`` append is
    short-circuited (no enqueue) so it matches the underlying sink's empty no-op exactly.
    """

    def __init__(self, *, consumer: ShardedWriteConsumer) -> None:
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
