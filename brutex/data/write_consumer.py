"""Sharded producer/consumer that overlaps lake writes with fetch (DEC-FETCH-WRITE-OVERLAP-001 Lever 1).

The historical Groww pull fans contracts across K SDK connections, but a forensic
log analysis (MIGRATION.md #921) PROVED the connections still run *serialized*: each
fetch worker blocks on a ~440 ms lake Parquet append on its critical path
(``read-modify-write`` of a month partition), so the achieved rate caps at ~2.86 r/s
while the rate controller already allows ~4.62 r/s. The connections are idle
(``in_flight == 0``) 78 % of the time, waiting on the write — not the vendor.

This module moves the lake append OFF the fetch worker. A fetch worker *enqueues* its
bars and returns to fetch the next contract immediately; a small pool of dedicated
**writer** threads drains the queues and performs the ``BarSink.append``. The four
connections then overlap fetch + write instead of serialising on the append.

Why SHARDED (not one writer thread)
-----------------------------------
A SINGLE writer thread would serialise every append at ~3/s — a NET REGRESSION
(the #906 trap). The writes are sharded by their destination MONTH-PARTITION path
via a stable hash, so:

* writes to the **same** partition always route to the **same** shard → they stay
  serialised AND ordered (preserving the lake's per-partition ``flock`` +
  keep-first merge-on-write idempotence, CLAUDE.md §2 Rule 7), and
* writes to **different** partitions route to different shards → they run in
  PARALLEL on different writer threads.

Different contracts write different partitions (the contract ``groww_symbol`` is in
the path), so a real pull's appends fan across the W shards and overlap.

Durability ordering (constraint #4)
-----------------------------------
A window/contract is settled in the durable :class:`~brutex.data.window_ledger.WindowLedger`
ONLY AFTER its bars are durably written. The caller hands a *settle* callback to
:meth:`WriteConsumer.submit`; the consumer runs it on the writer thread AFTER the
append returns, passing the real :class:`~brutex.data.sink.SinkAppendResult`. A
write FAILURE is propagated to the settle callback (it receives the exception), so a
failed/blocked write never deadlocks and never settles a window as durable
(the #919 back-channel-block fix, folded).

Error tiers (constraint #4 / DEC-FETCH-WRITE-OVERLAP-001 MAJOR-3)
----------------------------------------------------------------
A write error reaches the producer via TWO distinct tiers:

* a **GLOBAL-STOP halt** (:class:`~brutex.data.errors.LakeWriteHalt`, disk-full / OOM —
  a member of both ingest spines' ``_GLOBAL_STOP_ERRORS``) fails every remaining write
  identically, so the FIRST one is latched and RE-RAISED on the next :meth:`submit` /
  :meth:`close` to HALT the whole pull (never keep hammering a full disk), and
* a **non-halt** per-contract write error is delivered to THAT submit's settle callback
  as its error (so the caller quarantines / excludes the ONE contract) but is NOT latched
  for re-raise — a single bad write never aborts the whole pull (the MAJOR-3 fix).

Lifecycle (constraint #5)
-------------------------
:meth:`WriteConsumer.start` spins up the W writer threads (one-consumer-per-shard
lifecycle); the context-manager / :meth:`close` DRAINS every queue (no lost tail)
and joins the threads before returning. A latched :class:`~brutex.data.errors.LakeWriteHalt`
is re-raised on the next :meth:`submit` (or :meth:`close`) so the pull HALTS; a non-halt
error only reaches the producer through its settle callback.

Honest envelope (Golden Rule #10)
---------------------------------
This overlaps fetch with write; it does NOT make a single write faster. The win is
real only when the fetch worker has more work to do while the write runs (it does —
the next contract). The per-shard queue is BOUNDED, so a producer that outruns the
writers BLOCKS (back-pressure) — never unbounded memory, never a dropped write. The
default behaviour of the pull is byte-unchanged until the operator opts in
(``BRUTEX_WRITE_CONSUMER`` env flag at the wiring site); this module is pure
infrastructure with no global state of its own.
"""

from __future__ import annotations

import hashlib
import queue
import threading
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from types import TracebackType

import structlog

from brutex.data.errors import LakeWriteHalt
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import WriteResult
from brutex.data.partition import partition_for_timestamp
from brutex.data.sink import BarSink, SinkAppendResult

__all__ = [
    "DEFAULT_WRITE_CONSUMER_SHARDS",
    "WriteConsumer",
    "resolve_shard_count",
    "shard_for_partition",
]

_LOG = structlog.get_logger("brutex.data.write_consumer")

#: Default number of writer shards. Four matches the production SDK connection-pool
#: size (DEC-FETCH-WRITE-OVERLAP-001 Lever 2 raised ``GROWW_CLIENT_POOL_SIZE`` 2→4),
#: so each fetch connection has a writer to hand off to without over-subscribing the
#: disk. Tunable via ``BRUTEX_WRITE_CONSUMER_SHARDS`` at the wiring site.
DEFAULT_WRITE_CONSUMER_SHARDS = 4

#: Default bounded depth of each per-shard queue. Small enough to bound memory and
#: apply back-pressure quickly (a producer blocks once a shard is this far behind),
#: large enough to absorb a normal burst of multi-partition appends.
_DEFAULT_QUEUE_DEPTH = 64


def resolve_shard_count(raw: str | None, *, default: int = DEFAULT_WRITE_CONSUMER_SHARDS) -> int:
    """Resolve a configured shard count from a raw env string — clamped to ``>= 1``.

    A blank / unset / non-integer / non-positive value yields *default* (never 0 or
    negative — a 0-shard consumer could never drain). This is the ONE place the
    ``BRUTEX_WRITE_CONSUMER_SHARDS`` env value is parsed (CLAUDE.md §11 COMMON).

    Parameters
    ----------
    raw
        The raw env string (``os.environ.get(...)``), or ``None`` when unset.
    default
        The fallback when *raw* is blank / invalid.

    Returns
    -------
    int
        A shard count ``>= 1``.
    """
    if raw is None:
        return default
    stripped = raw.strip()
    if not stripped:
        return default
    try:
        value = int(stripped)
    except ValueError:
        return default
    return value if value >= 1 else default


def shard_for_partition(partition_key: str, *, shards: int) -> int:
    """Map a partition key to a writer shard index in ``[0, shards)`` — stable + O(1).

    Uses ``sha256(partition_key)`` so the mapping is deterministic across processes
    and uniform across partitions — the SAME partition always routes to the SAME
    shard (serialised + ordered), different partitions spread across shards (parallel).

    Parameters
    ----------
    partition_key
        A stable string identifying the destination month partition (see
        :meth:`WriteConsumer._partition_key`).
    shards
        The number of writer shards (``>= 1``).

    Returns
    -------
    int
        A shard index in ``[0, shards)``.
    """
    digest = hashlib.sha256(partition_key.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % shards


@dataclass(frozen=True, slots=True)
class _AppendCoords:
    """The non-bar coordinates of one :meth:`WriteConsumer.submit` call."""

    exchange: str
    segment: str
    symbol: str
    interval: str
    merge_mode: str


@dataclass(slots=True)
class _SubmitState:
    """Tracks the outstanding partition-writes of ONE submit so its settle fires once.

    A single ``submit`` may straddle several month partitions (a chunk crossing a
    month boundary), each routed to a possibly-different shard. We fan the
    partition-groups out, then join them back: the LAST group to finish delivers the
    aggregated :class:`SinkAppendResult` (or the first error) to the settle callback.
    """

    coords: _AppendCoords
    settle: Callable[[SinkAppendResult | None, BaseException | None], None]
    total_bars: int
    remaining: int
    lock: threading.Lock = field(default_factory=threading.Lock)
    partitions_written: int = 0
    write_results: list[WriteResult] = field(default_factory=list)
    error: BaseException | None = None
    settled: bool = False


@dataclass(slots=True)
class _WriteTask:
    """One unit of writer work: append a single partition-group of bars."""

    coords: _AppendCoords
    bars: tuple[CandleBar, ...]
    state: _SubmitState


class WriteConsumer:
    """A sharded write-behind wrapper over a :class:`~brutex.data.sink.BarSink`.

    Construct, :meth:`start` (or use as a context manager), then :meth:`submit`
    bars + a *settle* callback from the fetch workers; the writer pool drains the
    bounded per-shard queues and runs *settle* AFTER each submit's bars are durably
    written. :meth:`close` drains every queue and joins the writers.

    Parameters
    ----------
    sink
        The real :class:`~brutex.data.sink.BarSink` (e.g.
        :class:`~brutex.data.sink.ParquetBarSink`) every write delegates to. The
        consumer adds NO write logic of its own — it only moves the SAME append off
        the fetch worker and shards it for parallelism.
    shards
        Number of writer threads / queues (``>= 1``; default
        :data:`DEFAULT_WRITE_CONSUMER_SHARDS`). Each shard owns one bounded queue and
        one writer thread.
    queue_depth
        Bounded depth of EACH shard's queue (default :data:`_DEFAULT_QUEUE_DEPTH`).
        A :meth:`submit` BLOCKS once a target shard is this far behind (back-pressure).
    """

    def __init__(
        self,
        sink: BarSink,
        *,
        shards: int = DEFAULT_WRITE_CONSUMER_SHARDS,
        queue_depth: int = _DEFAULT_QUEUE_DEPTH,
    ) -> None:
        if shards < 1:
            raise ValueError(f"shards must be >= 1, got {shards}")
        if queue_depth < 1:
            raise ValueError(f"queue_depth must be >= 1, got {queue_depth}")
        self._sink = sink
        self._shards = shards
        self._queues: list[queue.Queue[_WriteTask | None]] = [
            queue.Queue(maxsize=queue_depth) for _ in range(shards)
        ]
        self._threads: list[threading.Thread] = []
        self._started = False
        self._closed = False
        # The error back-channel has TWO tiers (DEC-FETCH-WRITE-OVERLAP-001 MAJOR-3):
        #
        #  * A GLOBAL-STOP halt (a LakeWriteHalt — disk-full / OOM, a member of both
        #    ingest spines' _GLOBAL_STOP_ERRORS) fails EVERY remaining write identically,
        #    so the FIRST one is latched here and RE-RAISED on the next submit / close to
        #    HALT the whole pull (it must not keep hammering a full disk). Guarded by its
        #    own lock.
        #  * A NON-HALT write error (a genuine per-contract write failure) is delivered to
        #    THAT submit's settle callback as its error (the #919 back-channel — never
        #    silently swallowed, never deadlocks) but is NOT latched for re-raise, so it
        #    quarantines ONE contract (the caller's settle / write-failure sink records it
        #    and excludes it from clean/complete) WITHOUT aborting the pull. This is the
        #    MAJOR-3 fix: a deferred non-halt failure leaves the contract NOT-clean, not
        #    the whole pull dead.
        self._error_lock = threading.Lock()
        self._latched_halt: LakeWriteHalt | None = None

    @property
    def shards(self) -> int:
        """The number of writer shards."""
        return self._shards

    def start(self) -> None:
        """Spin up the W writer threads. Idempotent within a not-yet-closed consumer."""
        if self._closed:
            raise RuntimeError("WriteConsumer is closed; create a new one")
        if self._started:
            return
        self._started = True
        for shard in range(self._shards):
            thread = threading.Thread(
                target=self._drain_shard,
                args=(shard,),
                name=f"brutex-write-consumer-{shard}",
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)

    def __enter__(self) -> WriteConsumer:
        """Start the writer pool and return self for ``with WriteConsumer(...) as wc``."""
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Drain + join on context exit (always, even on an error in the body).

        The context-manager protocol args (*exc_type* / *exc_val* / *exc_tb*) are
        unused — :meth:`close` drains + joins + re-raises any latched GLOBAL-STOP halt
        regardless of how the ``with`` body exits; the names match the whitelisted
        dunder-arg convention in ``scripts/ci/vulture_whitelist.py``.
        """
        self.close()

    def _partition_key(self, coords: _AppendCoords, year: int, month: int) -> str:
        """Build the stable shard key for one month partition (exchange…interval/yyyy/mm)."""
        return (
            f"{coords.exchange}/{coords.segment}/{coords.symbol}/"
            f"{coords.interval}/{year:04d}/{month:02d}"
        )

    def submit(
        self,
        *,
        exchange: str,
        segment: str,
        symbol: str,
        interval: str,
        bars: Sequence[CandleBar],
        settle: Callable[[SinkAppendResult | None, BaseException | None], None],
        merge_mode: str = "keep-first",
    ) -> None:
        """Enqueue *bars* for write-behind; run *settle* on the writer thread post-write.

        Groups *bars* by ``(year, month)`` partition (mirroring
        :meth:`ParquetBarSink.append`) and enqueues each group to its shard. When ALL
        of a submit's groups have been durably written, *settle* is invoked exactly
        once with the aggregated :class:`SinkAppendResult` (and ``None`` error); if any
        group's write RAISED, *settle* receives ``(None, exc)`` — the back-channel for
        the write result, so a failed write never deadlocks and never durably-settles
        a window (constraint #4). An EMPTY *bars* settles immediately on the calling
        thread with a zero-bar result (no writer work — matches the sink's empty fast
        path).

        Re-raises a latched :class:`LakeWriteHalt` from a prior write BEFORE enqueuing,
        so a full disk halts the producer promptly (constraint #5).

        Parameters
        ----------
        exchange, segment, symbol, interval, merge_mode
            Partition coordinates forwarded verbatim to ``sink.append``.
        bars
            The bars to persist (may straddle multiple month partitions).
        settle
            Called once after the durable write with ``(result, None)`` on success or
            ``(None, exc)`` on a write error. Runs on a writer thread (or the calling
            thread for the empty fast path); it must be thread-safe with respect to
            whatever durable ledger it settles.

        Raises
        ------
        RuntimeError
            If the consumer is not started or already closed.
        LakeWriteHalt
            Re-raises the FIRST latched GLOBAL-STOP halt (disk-full / OOM) from a prior
            submit's write, so a full disk halts the pull promptly. A NON-halt write error
            is NOT re-raised here — it was delivered to its own settle callback (MAJOR-3).
        """
        if not self._started or self._closed:
            raise RuntimeError("WriteConsumer.submit requires a started, open consumer")
        self._raise_if_halted()

        coords = _AppendCoords(
            exchange=exchange,
            segment=segment,
            symbol=symbol,
            interval=interval,
            merge_mode=merge_mode,
        )
        if not bars:
            # Empty fast path: nothing to write, settle synchronously with a zero
            # result (the sink itself short-circuits empty appends the same way).
            settle(SinkAppendResult(bars_appended=0, partitions_written=0, write_results=()), None)
            return

        groups: dict[tuple[int, int], list[CandleBar]] = defaultdict(list)
        for bar in bars:
            groups[partition_for_timestamp(bar.timestamp)].append(bar)

        state = _SubmitState(
            coords=coords,
            settle=settle,
            total_bars=len(bars),
            remaining=len(groups),
        )
        # Enqueue in sorted partition order for deterministic dispatch (the SAME order
        # ParquetBarSink.append writes), so a single-shard config is byte-identical to
        # the synchronous sink (idempotence, Golden Rule #7).
        for (year, month), group in sorted(groups.items()):
            shard = shard_for_partition(
                self._partition_key(coords, year, month), shards=self._shards
            )
            task = _WriteTask(coords=coords, bars=tuple(group), state=state)
            # Blocking put → back-pressure (constraint #3): a producer outrunning the
            # writers waits here instead of growing memory unbounded.
            self._queues[shard].put(task)

    def _drain_shard(self, shard: int) -> None:
        """Writer-thread loop for one shard: pull tasks, write, settle. Ends on a None poison."""
        q = self._queues[shard]
        while True:
            task = q.get()
            try:
                if task is None:
                    return  # poison pill from close() — this shard is drained
                self._write_task(task)
            finally:
                q.task_done()

    def _write_task(self, task: _WriteTask) -> None:
        """Append one partition-group to the sink and advance its submit's barrier."""
        coords = task.coords
        result: SinkAppendResult | None = None
        error: BaseException | None = None
        try:
            result = self._sink.append(
                exchange=coords.exchange,
                segment=coords.segment,
                symbol=coords.symbol,
                interval=coords.interval,
                bars=task.bars,
                merge_mode=coords.merge_mode,
            )
        except LakeWriteHalt as halt:
            # Disk-full / OOM (GLOBAL stop): LATCH it so the producer HALTS the pull on the
            # next submit / close, and propagate it to this submit's settle as an error
            # (the window is NOT settled durable). A full disk fails every remaining write.
            self._latch_halt(halt)
            error = halt
        except Exception as exc:  # mirror PoolRunParallel's capture contract
            # Any OTHER (non-halt) write error is delivered to THIS submit's settle (so the
            # caller can quarantine the ONE contract / exclude it from clean-complete),
            # NEVER silently dropped and NEVER allowed to settle the window as durable (the
            # #919 fix). It is NOT latched for re-raise — a single per-contract write
            # failure must not abort the whole pull (MAJOR-3). KeyboardInterrupt /
            # SystemExit deliberately propagate (kill the thread) — a real interpreter-level
            # signal is not a write error to be re-delivered.
            _LOG.error("write_consumer.write_error", error_type=type(exc).__name__)
            error = exc
        self._advance(task.state, result, error)

    def _advance(
        self,
        state: _SubmitState,
        result: SinkAppendResult | None,
        error: BaseException | None,
    ) -> None:
        """Record one group's outcome; when the submit's last group lands, fire settle once."""
        with state.lock:
            if error is not None and state.error is None:
                state.error = error
            if result is not None:
                state.partitions_written += result.partitions_written
                state.write_results.extend(result.write_results)
            state.remaining -= 1
            if state.remaining > 0 or state.settled:
                return
            state.settled = True
            final_error = state.error
            if final_error is None:
                aggregated: SinkAppendResult | None = SinkAppendResult(
                    bars_appended=state.total_bars,
                    partitions_written=state.partitions_written,
                    write_results=tuple(state.write_results),
                )
            else:
                aggregated = None
        # Run settle OUTSIDE the state lock so a slow ledger write never blocks a
        # sibling group's _advance; the barrier above guarantees this fires exactly once.
        state.settle(aggregated, final_error)

    def _latch_halt(self, halt: LakeWriteHalt) -> None:
        """Latch the FIRST GLOBAL-STOP halt seen on any writer thread (re-raised to halt)."""
        with self._error_lock:
            if self._latched_halt is None:
                self._latched_halt = halt
        _LOG.error("write_consumer.lake_halt", reason=halt.reason)

    def _raise_if_halted(self) -> None:
        """Re-raise the FIRST latched GLOBAL-STOP halt, if any, so the producer halts.

        Only a :class:`LakeWriteHalt` is re-raised here — a non-halt per-contract write
        error is delivered to its settle callback instead (it quarantines one contract,
        never aborts the pull; MAJOR-3).
        """
        with self._error_lock:
            halt = self._latched_halt
        if halt is not None:
            raise halt

    def close(self) -> None:
        """Drain every queue, join the writer threads, then re-raise any latched HALT.

        Idempotent: a second call is a no-op. Enqueues one poison pill per shard
        (after the real tasks already queued), then joins — so NO queued write is
        lost (constraint #5, drain-on-shutdown). The FIRST GLOBAL-STOP halt latched by
        any writer (a :class:`LakeWriteHalt`) is re-raised AFTER the join, so a disk-full
        on the very last write still halts the caller. A NON-halt per-contract write error
        is NOT re-raised here — it was delivered to its own settle callback (MAJOR-3), so a
        single bad write quarantines one contract without aborting the drain.
        """
        if self._closed:
            return
        if not self._started:
            self._closed = True
            return
        self._closed = True
        for q in self._queues:
            q.put(None)  # one poison pill per shard, after that shard's real tasks
        for thread in self._threads:
            thread.join()
        self._raise_if_halted()
