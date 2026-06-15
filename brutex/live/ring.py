"""In-RAM ring buffer + periodic durable flush for the live-capture hot path.

The Groww SDK invokes :meth:`~brutex.live.capture.TickCapture._on_tick` on its own
daemon thread on every update (``docs/groww/api-reference/07-feed.md``; CLAUDE.md
§6, verified 2026-06-15). Before this module that callback wrote ONE durable JSONL
line per tick (``TickStore.append`` + ``flush()``) — correct, but the disk
``flush()`` ran INSIDE the callback, so a slow write or a folding rotation could
add latency to the feed's own thread. This module decouples the two so the
per-tick hot path is a single O(1) in-memory append and the durable write happens
off-cadence on a ~1s flush.

Two ledger entries, both honoured here:

* **DEC-LIVE-RAM-RING-001 (#797)** — :class:`TickRing`: a BOUNDED in-memory ring of
  the most-recent un-flushed captured items. O(1) :meth:`~TickRing.append`, O(1)
  :meth:`~TickRing.drain_pending` (pointer swap). The bound is honoured WITHOUT
  silent data loss: a ``collections.deque(maxlen=N)`` would drop the OLDEST
  un-flushed entry on overflow (a zero-loss violation, Golden Rule #7), so instead
  the ring never evicts and its OWNER (:class:`RingFlusher`) keeps it bounded by
  spilling overflow SYNCHRONOUSLY to the durable sink. An entry therefore leaves
  the ring ONLY after it is either drained to disk OR spilled to disk — never
  dropped.

* **DEC-LIVE-FLUSH-LOOP-001 (#798)** — :class:`RingFlusher`: a periodic (~1s,
  injected interval + injected monotonic clock) drain of the ring to the EXISTING
  durable :class:`~brutex.live.store.TickSink` (``TickStore`` /
  ``RollingTickStore`` — REUSED verbatim, the WAL/fold writers are NOT duplicated,
  Golden Rule #11 COMMON), plus a FULL :meth:`~RingFlusher.drain` on graceful
  shutdown. ``seq`` is assigned by the SINK at flush time (never by the ring), so
  the global gap-free sequence, the ``.next-seq`` watermark
  (DEC-LIVE-AUTOROLL-001), and the lake PK ``(capture_run_id, seq,
  instrument_key)`` idempotence are all preserved unchanged.

**Durability (honest envelope, Golden Rule #10).** A RAM buffer + 1s flush trades
per-tick durability for hot-path latency. The exact guarantee:

* **Graceful shutdown → ZERO loss.** :meth:`RingFlusher.drain` writes every
  remaining in-RAM item to the durable sink before the sink is closed.
* **Capacity overflow → ZERO loss.** The overflow is spilled synchronously to the
  durable sink, never dropped — it degrades to the prior per-tick durability under
  sustained burst.
* **Hard crash (SIGKILL / power loss) → BOUNDED loss.** Only the items still in
  RAM (drained no later than ``flush_interval`` ago, and at most ``capacity`` of
  them) can be lost — a window of at most one flush interval of un-flushed ticks.
  This is a DELIBERATE, NAMED weakening of the prior flush-every-line guarantee; it
  is NOT claimed as unconditional "zero loss".

**Thread-safety.** :meth:`TickRing.append` (SDK daemon thread) and
:meth:`TickRing.drain_pending` (the cooperative ``run_for`` loop) are guarded by a
single :class:`threading.Lock` held only across the O(1) buffer SWAP — released
before any disk write — so the appending callback never waits on disk.
``collections.deque.append``/``popleft`` are individually atomic under CPython's
GIL; the lock exists solely to make the multi-element batch swap atomic against a
concurrent append. ``seq`` is assigned exclusively by the single-threaded drainer
inside the sink, so there is no race on the sequence counter. The cooperative
:meth:`~RingFlusher.flush_due` design means EVERY branch is unit-tested
synchronously with an injected clock and NO real threads (Golden Rule #11; mirrors
:mod:`brutex.live.capture` / :mod:`brutex.live.rolling`).

Read-only measurement surface (Golden Rule #3): no orders, never imported by the
engine or dashboard (DEC-LIVE-DECOUPLE-001). Pure + injected (clock/sink) →
unit-tested with fakes, no ``growwapi`` and no network.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import structlog

from brutex.live.store import TickSink

_LOG = structlog.get_logger("brutex.live.ring")


@dataclass(frozen=True, slots=True)
class RingItem:
    """One captured observation buffered in RAM, BEFORE the sink assigns its ``seq``.

    The ring deliberately holds the capture PAYLOAD only — the pruned snapshot plus
    OUR receive timestamps — NOT a :class:`~brutex.live.store.TickRecord`: ``seq``
    is the sink's to assign at flush time (the single-threaded drainer), so the
    ring never owns a sequence number and never races the sink's counter.

    Attributes
    ----------
    snapshot
        The (delta-pruned) feed snapshot mapping for this callback, exactly as
        :class:`~brutex.live.capture.TickCapture` would have handed to the sink.
    recv_ts
        OUR UTC tz-aware wall-clock receive time (µs).
    recv_ns
        OUR receive time in nanoseconds (``time.time_ns()`` — the cadence oracle).
    """

    snapshot: Mapping[str, Any]
    recv_ts: datetime
    recv_ns: int


class TickRing:
    """Bounded in-RAM ring of un-flushed captured items — the O(1) hot-path buffer.

    DEC-LIVE-RAM-RING-001 (#797). :meth:`append` is the per-tick hot path: one
    O(1) in-memory enqueue under a short lock, never touching disk.
    :meth:`drain_pending` atomically SWAPS out everything currently buffered (O(1)
    pointer swap under the lock) so the caller can write the batch to the durable
    sink OUTSIDE the lock. :meth:`snapshot` is a consistent point-in-time read of
    the buffered items for an observer (the read does not mutate the ring).

    The capacity is enforced by the OWNER, not by silent eviction: :meth:`append`
    returns ``True`` when the buffer is now AT OR OVER ``capacity`` so the owner
    (:class:`RingFlusher`) can spill the overflow to the durable sink immediately
    rather than drop it (Golden Rule #7 — never lose an un-flushed tick).
    """

    def __init__(self, *, capacity: int) -> None:
        """Construct an empty ring bounded at *capacity* items.

        Parameters
        ----------
        capacity
            The soft high-watermark (≥ 1). When the buffered item count reaches it,
            :meth:`append` signals the owner to flush/spill — the ring itself never
            drops, so this bounds RAM via prompt draining, not via eviction.

        Raises
        ------
        ValueError
            If *capacity* is below 1 (a zero/negative bound is meaningless).
        """
        if capacity < 1:
            raise ValueError(f"capacity must be >= 1, got {capacity}")
        self._capacity = capacity
        self._buffer: deque[RingItem] = deque()
        self._lock = threading.Lock()

    @property
    def capacity(self) -> int:
        """The soft high-watermark at which :meth:`append` signals a flush."""
        return self._capacity

    def __len__(self) -> int:
        """Number of items currently buffered (O(1), lock-guarded)."""
        with self._lock:
            return len(self._buffer)

    def append(self, item: RingItem) -> bool:
        """Buffer one *item* (O(1) hot path); return ``True`` when at/over capacity.

        The single lock is held only for the O(1) ``deque.append`` so the SDK
        daemon thread never waits on disk. The boolean lets the owner decide to
        flush/spill synchronously (keeping RAM bounded WITHOUT dropping an
        un-flushed tick).
        """
        with self._lock:
            self._buffer.append(item)
            return len(self._buffer) >= self._capacity

    def drain_pending(self) -> list[RingItem]:
        """Atomically remove and return EVERY buffered item, in append order.

        The lock is held only across the O(1) buffer SWAP (steal the current deque,
        install a fresh empty one); the caller writes the returned batch to the
        durable sink OUTSIDE the lock, so a concurrent :meth:`append` never waits on
        the disk write and is captured by the NEXT drain. FIFO order is preserved
        across swaps, so the sink assigns ``seq`` in strict capture order.
        """
        with self._lock:
            batch = self._buffer
            self._buffer = deque()
        return list(batch)

    def snapshot(self) -> tuple[RingItem, ...]:
        """Return the currently-buffered items WITHOUT removing them (observer read).

        A consistent point-in-time view (O(n) copy, n = current depth) for a
        read-accelerator consumer; the ring is unchanged. Held under the lock so the
        copy is atomic against a concurrent append/drain.
        """
        with self._lock:
            return tuple(self._buffer)


class RingFlusher:
    """Periodic + on-demand drain of a :class:`TickRing` to a durable sink.

    DEC-LIVE-FLUSH-LOOP-001 (#798). Owns a :class:`TickRing` and the existing
    durable :class:`~brutex.live.store.TickSink`. The capture callback calls
    :meth:`capture` (ring append + capacity-spill); the host loop calls
    :meth:`flush_due` every poll (cooperative — no dedicated thread needed, so the
    whole drain is unit-tested with an injected clock and no real threads); and the
    shutdown path calls :meth:`drain` for a full, zero-loss final flush.

    REUSES the durable sink verbatim (Golden Rule #11 COMMON) — it neither
    duplicates the WAL writer nor the Parquet fold. ``seq`` is assigned by the sink
    at flush time, so the gap-free global sequence + ``.next-seq`` watermark +
    lake-PK idempotence are unchanged from the per-tick path.
    """

    def __init__(
        self,
        *,
        sink: TickSink,
        ring: TickRing,
        flush_interval: float,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """Wire the flusher.

        Parameters
        ----------
        sink
            The durable append-only sink (``TickStore`` / ``RollingTickStore``) —
            REUSED, not duplicated.
        ring
            The bounded in-RAM buffer to drain.
        flush_interval
            Seconds of monotonic wall-clock between periodic drains (> 0; ~1.0 for
            the live capture). The hard-crash loss bound is one such interval of
            un-flushed ticks.
        monotonic
            Monotonic clock for the cadence (``time.monotonic`` by default;
            injected as a fake in tests).

        Raises
        ------
        ValueError
            If *flush_interval* is not strictly positive.
        """
        if flush_interval <= 0:
            raise ValueError(f"flush_interval must be > 0, got {flush_interval}")
        self._sink = sink
        self._ring = ring
        self._flush_interval = flush_interval
        self._monotonic = monotonic
        self._next_flush = monotonic() + flush_interval
        self._flushed = 0
        self._spilled = 0
        self._flushes = 0

    @property
    def flushed(self) -> int:
        """Total items drained from the ring to the sink by periodic/final flushes."""
        return self._flushed

    @property
    def spilled(self) -> int:
        """Total items written SYNCHRONOUSLY on capacity overflow (never dropped)."""
        return self._spilled

    @property
    def flushes(self) -> int:
        """Number of periodic/forced drain passes performed so far."""
        return self._flushes

    def capture(self, snapshot: Mapping[str, Any], *, recv_ts: datetime, recv_ns: int) -> None:
        """Buffer one captured snapshot; spill SYNCHRONOUSLY if the ring is full.

        The per-tick hot path: an O(1) ring append. When the append reports the
        ring has reached ``capacity``, the overflow is drained to the durable sink
        IMMEDIATELY (degrading to the prior per-tick durability under sustained
        burst) rather than dropped — so an un-flushed tick is NEVER lost
        (Golden Rule #7). The common case (below capacity) touches no disk.
        """
        full = self._ring.append(RingItem(snapshot=snapshot, recv_ts=recv_ts, recv_ns=recv_ns))
        if full:
            self._spilled += self._write_batch(self._ring.drain_pending())

    def flush_due(self, now: float) -> int:
        """Drain the ring IF at least ``flush_interval`` has elapsed; return items written.

        Cooperative: the host loop (``TickCapture.run_for``) calls this every poll
        with the injected monotonic *now*. Below the deadline it is a no-op
        returning ``0``; at/after it, it drains the ring to the sink and advances
        the next deadline by exactly one interval (so a long pause does not burst a
        backlog of empty flushes — it catches up to the next aligned slot).
        """
        if now < self._next_flush:
            return 0
        self._next_flush += self._flush_interval
        return self.drain()

    def drain(self) -> int:
        """Drain EVERY buffered item to the durable sink NOW; return items written.

        The graceful-shutdown + forced-flush path (zero loss): swap the ring empty
        and write the whole batch to the sink. Safe to call when the ring is empty
        (writes nothing, returns ``0``).
        """
        written = self._write_batch(self._ring.drain_pending())
        self._flushed += written
        self._flushes += 1
        return written

    def _write_batch(self, batch: list[RingItem]) -> int:
        """Append *batch* to the durable sink in capture order; return the count.

        ``seq`` is the sink's to assign (single-threaded here), so FIFO order in →
        gap-free strictly-increasing seq out, exactly as the per-tick path.
        """
        for item in batch:
            self._sink.append(item.snapshot, recv_ts=item.recv_ts, recv_ns=item.recv_ns)
        if batch:
            _LOG.info("live_ring_flushed", items=len(batch), sink_next_seq=self._sink.next_seq)
        return len(batch)


__all__ = ["RingFlusher", "RingItem", "TickRing"]
