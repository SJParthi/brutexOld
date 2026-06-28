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

When :class:`FetchPipeline` is ``None`` (the default) the spine calls
``_fetch_window_raw`` then ``_process_window_tail`` as ONE synchronous call —
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
* **back-pressure** → each queue is bounded; a ``put`` BLOCKS when full, so a slow
  consumer throttles the producer (the fetch lane) to the processing rate instead of
  growing an unbounded backlog that OOMs. This is the ONLY way internal work touches
  the fetch lane — and it is bounded, never per-window blocking on durability.
* **rate controller (8/500 + AIMD)** stays ENTIRELY on the producer side, UNCHANGED.

## Honest envelope (Golden Rule #10)

This delivers **ISOLATION** — the fetch lane's throughput is decoupled from the
internal processing (proven by a stalled-consumer test where the producer keeps
issuing at full rate). It is **NOT** a throughput increase: the achieved rate is
still vendor-bound (Groww 429s above ~5 r/s), and the controller still caps the
issue rate. The benefit is fault + latency isolation of the fetch. ``None`` (the
default) reproduces TODAY's fully-synchronous path BYTE-IDENTICALLY. stdlib-only at
import (``queue`` / ``threading`` / ``dataclasses`` / ``hashlib`` / ``os``).
"""

from __future__ import annotations

import hashlib
import os
import queue
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

from brutex.data.errors import LakeWriteHalt

if TYPE_CHECKING:  # pragma: no cover
    from brutex.data.models import CandleBar

__all__ = [
    "FETCH_PIPELINE_ENV",
    "FETCH_PIPELINE_SIZE_ENV",
    "FetchPipeline",
    "FetchTail",
    "RawFetchResult",
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

#: Environment flag that opts the live pull into the FULL async fetch/process separation.
#: DEFAULT OFF: the synchronous, byte-identical path stays the production default until the
#: operator flips it (Golden Rule #10 — an isolation feature ships dormant). Truthy values:
#: ``1`` / ``true`` / ``yes`` / ``on`` (case-insensitive). Anything else (incl. unset,
#: blank, ``0``, ``false``) keeps the pipeline OFF.
FETCH_PIPELINE_ENV: str = "BRUTEX_GROWW_FETCH_PIPELINE"

#: Environment override for the consumer-pool width (operator escape hatch, no code change).
FETCH_PIPELINE_SIZE_ENV: str = "BRUTEX_GROWW_FETCH_PIPELINE_SIZE"

_FETCH_PIPELINE_TRUTHY: frozenset[str] = frozenset({"1", "true", "yes", "on"})


def resolve_fetch_pipeline_size(raw: str | None = None, *, flag: str | None = None) -> int | None:
    """Resolve the consumer-pool width: ``None`` (sync, OFF) or a clamped ``M >= 1``.

    Returns ``None`` (the synchronous, byte-identical path — the pipeline is OFF) unless the
    :data:`FETCH_PIPELINE_ENV` flag is an explicit truthy token. When ON, the width is read
    from :data:`FETCH_PIPELINE_SIZE_ENV` (or *raw* in tests) and clamped to
    ``[1, _FETCH_PIPELINE_SIZE_MAX]``; an unset / blank / non-integer / out-of-range value
    falls back to :data:`_DEFAULT_FETCH_PIPELINE_SIZE` (fail-safe to the audited default,
    Golden Rule #1). A thin helper so the live build site + tests share ONE normalisation
    (CLAUDE.md §11 COMMON). O(1).

    Parameters
    ----------
    raw
        The size value (``None`` reads :data:`FETCH_PIPELINE_SIZE_ENV`); a test may pass one.
    flag
        The on/off token (``None`` reads :data:`FETCH_PIPELINE_ENV`); a test may pass one.
    """
    flag_value = os.environ.get(FETCH_PIPELINE_ENV) if flag is None else flag
    if flag_value is None or flag_value.strip().lower() not in _FETCH_PIPELINE_TRUTHY:
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
        The bars to process (empty for an EMPTY window; never ``None``).
    """

    shard_key: str
    process: Callable[[FetchTail], None] = field(compare=False)
    raw_bars: Sequence[CandleBar] = ()


@dataclass(frozen=True, slots=True)
class RawFetchResult:
    """One immutable unit of consumer work routed to a shard (wraps a :class:`FetchTail`).

    The producer enqueues this and does NOT wait on the tail completing — the consumer
    drains it and runs ``tail.process(tail)``. There is no per-window result box (unlike
    :mod:`brutex.data.write_consumer`): the producer does not block on durability, so the
    consumer's outcome is folded straight into the spine's thread-safe aggregator by the
    ``process`` closure, never returned to the producer.
    """

    tail: FetchTail

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
        monotonic: Callable[[], float] = time.monotonic,
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
        monotonic
            Injected monotonic clock (tests pass a fake to drive the watchdog deterministically).
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
        self._monotonic = monotonic
        # `object` poison-pill sentinel: a unique instance per pipeline so a stray
        # RawFetchResult can never be mistaken for the stop signal.
        self._poison: object = object()
        self._queues: list[queue.Queue[RawFetchResult | object]] = [
            queue.Queue(maxsize=queue_maxsize) for _ in range(workers)
        ]
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
        # Watchdog shutdown signal — set at drain so the watchdog thread exits its poll loop.
        self._watchdog_stop = threading.Event()
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

    def fold_totals(self, *, bars_appended: int, conflicts: int) -> None:
        """Thread-safe fold of ONE processed window's bars/conflicts into the pull totals.

        Called by ``_process_window_tail`` (on a consumer thread) after a durable append, so
        the pull-level observability totals reflect every deferred window. O(1) under the
        totals lock.
        """
        with self._totals_lock:
            self._processed += 1
            self._bars_appended += bars_appended
            self._conflicts += conflicts

    @property
    def totals(self) -> tuple[int, int, int]:
        """The pull-level ``(processed_windows, bars_appended, conflicts)`` so far (thread-safe)."""
        with self._totals_lock:
            return (self._processed, self._bars_appended, self._conflicts)

    def submit(self, result: RawFetchResult) -> None:
        """Route *result* to its series shard; block ONLY on back-pressure, never on durability.

        Routes to ``shard = blake2b(series) % M`` and ``put``s the request (blocking when
        the shard queue is full — the bounded back-pressure that throttles the fetch lane to
        the processing rate, never an OOM). Returns IMMEDIATELY once enqueued — the producer
        does NOT wait on the consumer's tail (the whole point: internal work never blocks the
        fetch lane on durability).

        If a consumer already failed (a captured exception — e.g. a
        :class:`~brutex.data.errors.LakeWriteHalt`), the request is REJECTED immediately by
        re-raising that exception on the producer thread, so a pull-wide stop reaches the
        spine's ``_GLOBAL_STOP_ERRORS`` path without waiting for the rest of the pull.
        """
        self._raise_if_stopped()
        # Count the window in flight BEFORE the (possibly blocking) put, so the watchdog sees
        # work pending even while the producer is back-pressured on a full queue. The matching
        # ``_mark_completed`` (success OR fail-fast drop) keeps in-flight = submitted − completed
        # accurate.
        with self._progress_lock:
            self._submitted += 1
        shard = _shard_index(result.shard_key(), self._workers)
        self._queues[shard].put(result)

    def _raise_if_stopped(self) -> None:
        """Re-raise the first captured consumer exception if the pool hard-stopped."""
        if self._stopped.is_set():
            exc = self._exc
            if exc is not None:
                raise exc

    def _run_consumer(self, shard: int) -> None:
        """Drain shard *shard*'s queue until the poison-pill, processing each window's tail.

        Each tail's ``process`` is wrapped so a failure (disk-full / any sink error) is
        captured into the pool-wide stop (so the producer + drain re-raise it) — never
        swallowed. The consumer thread stays alive to drain the rest of its queue (so no
        producer blocks forever on a full queue), settling subsequent windows fail-fast.
        """
        q = self._queues[shard]
        while True:
            item = q.get()
            if item is self._poison:
                return
            # `item` is a RawFetchResult here: the queue only ever carries a RawFetchResult
            # or the unique poison-pill sentinel, and the sentinel already returned above.
            result = item if isinstance(item, RawFetchResult) else None
            if result is None:  # pragma: no cover — only poison + RawFetchResult are queued
                continue
            self._process_one(result)

    def _process_one(self, result: RawFetchResult) -> None:
        """Run ONE window's tail; capture any failure into the pool-wide stop.

        Once the pool has hard-stopped (a prior failure), subsequent windows are DROPPED
        without processing — a full disk fails every remaining window identically, so re-
        running the tail would just hammer it. The dropped window's ledger row stays
        UNSETTLED → re-fetched next pull (idempotent keep-first).
        """
        if self._stopped.is_set():
            # Dropped fail-fast — still a "completion" for the in-flight accounting so the
            # watchdog does not flag a permanently-stuck window after a halt.
            self._mark_completed()
            return
        tail = result.tail
        try:
            tail.process(tail)
        except Exception as exc:  # surface ANY tail FAILURE, never swallow
            # Narrow to ``Exception`` (NOT ``BaseException``) so a ``KeyboardInterrupt`` /
            # ``SystemExit`` is NOT captured as a recoverable failure — it propagates out of
            # the consumer so an interpreter shutdown / operator interrupt is honoured.
            self._trip(exc)
        finally:
            # Advance progress on EVERY processed window (success or captured failure) so the
            # stall watchdog re-arms — a steadily-failing-but-progressing pipeline is not stuck.
            self._mark_completed()

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

    def drain(self) -> None:
        """Flush every queued window's tail, then stop + join all consumers; re-raise on halt.

        Called ONCE at pull end (producers already stopped submitting) BEFORE the final
        ledger flush, so every still-queued window is processed + ledger-recorded before the
        ledger persists (settle-after-durable at pull granularity). Sends one poison-pill per
        shard AFTER the queued work so every still-queued :class:`RawFetchResult` is processed
        (unless the pool already hard-stopped, in which case the remaining windows are dropped
        fail-fast — they re-fetch next pull), then joins each thread. After :meth:`drain`
        returns, if a consumer captured a failure (e.g. a
        :class:`~brutex.data.errors.LakeWriteHalt`) it is RE-RAISED here so the pull halts
        through the spine's ``_GLOBAL_STOP_ERRORS`` path. Idempotent: a second call (no live
        threads) is a no-op.
        """
        # Stop the watchdog first so it does not flag a stall during the (bounded) join.
        self._watchdog_stop.set()
        if not any(t.is_alive() for t in self._threads):
            self._watchdog.join(timeout=self._drain_join_timeout_seconds)
            return
        for q in self._queues:
            q.put(self._poison)
        # Bounded join — a live consumer exits on the poison-pill within one write cycle, so
        # this is a SAFETY net: a wedged consumer is REPORTED (never hangs the pull's finally
        # forever). The daemon thread does not keep the process alive, so an unjoined-but-
        # reported thread can never wedge shutdown.
        for t in self._threads:
            t.join(timeout=self._drain_join_timeout_seconds)
            if t.is_alive():
                _LOG.warning(
                    "fetch_pipeline.drain_join_timeout",
                    thread=t.name,
                    timeout_seconds=self._drain_join_timeout_seconds,
                )
        self._watchdog.join(timeout=self._drain_join_timeout_seconds)
        processed, bars_appended, conflicts = self.totals
        _LOG.info(
            "fetch_pipeline.drained",
            processed_windows=processed,
            bars_appended=bars_appended,
            conflicts=conflicts,
        )
        exc = self._exc
        if exc is not None:
            raise exc
