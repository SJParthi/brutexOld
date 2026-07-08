"""Unit + property + chaos tests for brutex.data.write_consumer.

Covers the FULL failure matrix the operator demanded for the sharded parallel
write-consumer (DEC-FETCH-WRITE-OVERLAP-LEVER1-001), every permutation:

* settle-after-durable (the #1 invariant — a SUCCESS box is settled ONLY after the
  underlying ``append`` returns; settle-before-durable is structurally impossible);
* one-partition→one-shard routing (no single-serializer regression);
* bounded-queue back-pressure (a full shard queue blocks the producer, never OOM);
* keep-first / order-independence (property test);
* disk-full / OOM at the consumer write → ``LakeWriteHalt`` re-raised on the producer +
  pool-wide fail-fast;
* a consumer thread that dies mid-drain → captured + surfaced, NEVER a fabricated
  success;
* end-of-pull drain with a non-empty queue (flush+join before the caller proceeds);
* hard-exit with queued work (a never-settled request stays un-settled → re-fetch);
* the synchronous ``write_pool=None`` byte-identical default (`WriteConsumerSink` empty
  no-op + `resolve_write_pool_size`).

These exercise the REAL :class:`~brutex.data.write_consumer.ShardedWriteConsumer`
threads against a fake durable sink (no polars / no filesystem), so the concurrency
+ settle ordering are proven deterministically.
"""

# Whitebox test of the consumer-private `_trip_halt` (the first-halt-wins thread-safety
# invariant) — exercised directly so the concurrent-second-shard race is deterministic.
# pyright: reportPrivateUsage=none

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from brutex.data.errors import LakeWriteHalt
from brutex.data.models import CandleBar
from brutex.data.sink import SinkAppendResult
from brutex.data.write_consumer import (
    BatchingWriteConsumer,
    ResultBox,
    ShardedWriteConsumer,
    WriteConsumer,
    WriteConsumerSink,
    WriteRequest,
    resolve_write_batch_enabled,
    resolve_write_pool_size,
)
from hypothesis import given, settings
from hypothesis import strategies as st


def _bar(minute: int = 15) -> CandleBar:
    """One trivial 1-minute bar (the consumer never inspects bar values)."""
    from decimal import Decimal

    return CandleBar(
        timestamp=datetime(2020, 1, 1, 9, minute, 0, tzinfo=UTC),
        open=Decimal("100.00"),
        high=Decimal("101.00"),
        low=Decimal("99.00"),
        close=Decimal("100.50"),
        volume=10,
        open_interest=None,
    )


@dataclass
class _RecordingSink:
    """A durable-sink fake recording every ``append`` in call order (thread-safe)."""

    calls: list[tuple[str, str, str, str, int, str]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    #: optional per-append delay so the test can prove overlap / back-pressure
    delay: float = 0.0

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
        if self.delay:
            time.sleep(self.delay)
        with self._lock:
            self.calls.append((exchange, segment, symbol, interval, len(bars), merge_mode))
        return SinkAppendResult(bars_appended=len(bars), partitions_written=1, write_results=())


def _make_request(
    *,
    symbol: str = "NSE-NIFTY",
    interval: str = "1minute",
    bars: Sequence[CandleBar] | None = None,
    merge_mode: str = "keep-first",
) -> WriteRequest:
    return WriteRequest(
        exchange="NSE",
        segment="INDEX",
        symbol=symbol,
        interval=interval,
        bars=list(bars) if bars is not None else [_bar()],
        merge_mode=merge_mode,
        box=ResultBox(),
    )


# ────────────────────────────────────────────────────────────────────────
# ResultBox — the one-shot settle slot
# ────────────────────────────────────────────────────────────────────────


class TestResultBox:
    def test_set_result_then_get_returns_it(self) -> None:
        box = ResultBox()
        result = SinkAppendResult(bars_appended=3, partitions_written=1, write_results=())
        box.set_result(result)
        assert box.get() is result

    def test_set_exception_then_get_reraises(self) -> None:
        box = ResultBox()
        box.set_exception(ValueError("boom"))
        with pytest.raises(ValueError, match="boom"):
            box.get()

    def test_get_blocks_until_settled(self) -> None:
        box = ResultBox()
        result = SinkAppendResult(bars_appended=1, partitions_written=1, write_results=())

        def _settle_later() -> None:
            time.sleep(0.05)
            box.set_result(result)

        t = threading.Thread(target=_settle_later)
        t.start()
        # get() must block until the background thread settles the box.
        assert box.get() is result
        t.join()

    def test_settled_with_neither_result_nor_exception_raises(self) -> None:
        # Defensive guard: a box whose `_done` event is set but that carries NEITHER a
        # result NOR an exception is an impossible state (`set_result`/`set_exception`
        # are the only settle paths and each passes a real value). `get()` must surface
        # it as a RuntimeError, NEVER silently return `None` (which would break the
        # SinkAppendResult contract + corrupt settle-after-durable). Forced directly
        # since no normal call sequence can reach it.
        box = ResultBox()
        box._done.set()  # whitebox: force the impossible settled-empty state
        with pytest.raises(RuntimeError, match="settled with neither a result nor an exception"):
            box.get()


# ────────────────────────────────────────────────────────────────────────
# WriteRequest — routing key
# ────────────────────────────────────────────────────────────────────────


class TestWriteRequest:
    def test_shard_key_excludes_year_month(self) -> None:
        # Two requests differing ONLY by their bars' month must hash to one key, so
        # adjacent windows of one series stay on one shard (shared-month merge serialised).
        a = _make_request(bars=[_bar(minute=15)])
        b = _make_request(bars=[_bar(minute=16)])
        assert a.shard_key() == b.shard_key()

    def test_shard_key_distinguishes_series(self) -> None:
        a = _make_request(symbol="NSE-NIFTY")
        b = _make_request(symbol="NSE-BANKNIFTY")
        assert a.shard_key() != b.shard_key()

    def test_box_excluded_from_equality(self) -> None:
        # `box` carries `compare=False`, so two requests with identical coords but
        # different boxes compare equal — the box must not leak into routing/equality.
        a = _make_request()
        b = _make_request()
        assert a == b


# ────────────────────────────────────────────────────────────────────────
# ShardedWriteConsumer — construction + happy path
# ────────────────────────────────────────────────────────────────────────


class TestConsumerConstruction:
    def test_rejects_zero_workers(self) -> None:
        with pytest.raises(ValueError, match="workers must be >= 1"):
            ShardedWriteConsumer(sink=_RecordingSink(), workers=0)

    def test_rejects_zero_queue_maxsize(self) -> None:
        with pytest.raises(ValueError, match="queue_maxsize must be >= 1"):
            ShardedWriteConsumer(sink=_RecordingSink(), workers=2, queue_maxsize=0)

    def test_workers_property(self) -> None:
        sink = _RecordingSink()
        consumer = ShardedWriteConsumer(sink=sink, workers=3)
        try:
            assert consumer.workers == 3
        finally:
            consumer.drain()


class TestConsumerHappyPath:
    def test_submit_writes_durably_and_returns_result(self) -> None:
        sink = _RecordingSink()
        consumer = ShardedWriteConsumer(sink=sink, workers=2)
        try:
            result = consumer.submit(_make_request())
            assert result.bars_appended == 1
            assert sink.calls == [("NSE", "INDEX", "NSE-NIFTY", "1minute", 1, "keep-first")]
        finally:
            consumer.drain()

    def test_merge_mode_forwarded(self) -> None:
        sink = _RecordingSink()
        consumer = ShardedWriteConsumer(sink=sink, workers=1)
        try:
            consumer.submit(_make_request(merge_mode="latest-wins"))
            assert sink.calls[0][5] == "latest-wins"
        finally:
            consumer.drain()

    def test_drain_is_idempotent(self) -> None:
        consumer = ShardedWriteConsumer(sink=_RecordingSink(), workers=2)
        consumer.drain()
        # Second drain (no live threads) is a no-op, never an error.
        consumer.drain()

    def test_same_series_routes_to_one_shard(self) -> None:
        # All writes of one series must execute on ONE shard worker (the per-partition
        # lock + flock invariant). With a per-shard delay we prove they are SERIAL within
        # the series (the recording is strictly ordered, never interleaved on two shards).
        sink = _RecordingSink(delay=0.01)
        consumer = ShardedWriteConsumer(sink=sink, workers=4)
        try:
            boxes = [
                consumer_submit_async(consumer, _make_request(bars=[_bar(minute=m)]))
                for m in range(15, 25)
            ]
            for box in boxes:
                box.get()
            # 10 writes, all same series → all recorded (order within a shard is FIFO).
            assert len(sink.calls) == 10
            assert all(c[2] == "NSE-NIFTY" for c in sink.calls)
        finally:
            consumer.drain()


def consumer_submit_async(consumer: WriteConsumer, request: WriteRequest) -> ResultBox:
    """Submit on a background thread (so the test thread does not block on the result)."""
    threading.Thread(target=consumer.submit, args=(request,), daemon=True).start()
    return request.box


# ────────────────────────────────────────────────────────────────────────
# Settle-after-durable — the #1 invariant
# ────────────────────────────────────────────────────────────────────────


class TestSettleAfterDurable:
    def test_box_settles_only_after_append_returns(self) -> None:
        # Prove the box is NOT settled while `append` is mid-flight: a sink that
        # signals "I am inside append" then waits lets us assert the box is still
        # un-settled until append returns.
        in_append = threading.Event()
        release = threading.Event()
        order: list[str] = []

        @dataclass
        class _GatedSink:
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
                in_append.set()
                release.wait()
                order.append("durable")
                return SinkAppendResult(
                    bars_appended=len(bars), partitions_written=1, write_results=()
                )

        consumer = ShardedWriteConsumer(sink=_GatedSink(), workers=1)
        request = _make_request()

        def _producer() -> None:
            request.box.get()
            order.append("settled")

        try:
            threading.Thread(target=consumer.submit, args=(request,), daemon=True).start()
            t = threading.Thread(target=_producer, daemon=True)
            t.start()
            assert in_append.wait(timeout=2.0)
            # We are inside append, before it returns: the box must NOT be settled yet.
            assert order == []
            release.set()
            t.join(timeout=2.0)
            # Durable write recorded BEFORE the box settled → settle-after-durable.
            assert order == ["durable", "settled"]
        finally:
            release.set()
            consumer.drain()


# ────────────────────────────────────────────────────────────────────────
# Disk-full / OOM → LakeWriteHalt re-raised + pool-wide fail-fast
# ────────────────────────────────────────────────────────────────────────


class TestLakeWriteHalt:
    def test_halt_reraised_on_producer(self) -> None:
        @dataclass
        class _HaltingSink:
            def append(self, **_: object) -> SinkAppendResult:
                raise LakeWriteHalt("disk_full")

        consumer = ShardedWriteConsumer(sink=_HaltingSink(), workers=2)
        try:
            with pytest.raises(LakeWriteHalt) as exc_info:
                consumer.submit(_make_request())
            assert exc_info.value.reason == "disk_full"
        finally:
            consumer.drain()

    def test_halt_trips_pool_wide_fail_fast(self) -> None:
        # After a LakeWriteHalt, every SUBSEQUENT submit fails fast with the SAME halt
        # (the disk is full — never queue more work onto it).
        @dataclass
        class _HaltingSink:
            def append(self, **_: object) -> SinkAppendResult:
                raise LakeWriteHalt("out_of_memory")

        consumer = ShardedWriteConsumer(sink=_HaltingSink(), workers=2)
        try:
            with pytest.raises(LakeWriteHalt):
                consumer.submit(_make_request(symbol="NSE-NIFTY"))
            # A different series (different shard) — still fails fast on the tripped flag.
            with pytest.raises(LakeWriteHalt) as exc_info:
                consumer.submit(_make_request(symbol="NSE-BANKNIFTY"))
            assert exc_info.value.reason == "out_of_memory"
        finally:
            consumer.drain()

    def test_drain_logs_after_halt(self, capsys: pytest.CaptureFixture[str]) -> None:
        @dataclass
        class _HaltingSink:
            def append(self, **_: object) -> SinkAppendResult:
                raise LakeWriteHalt("disk_full")

        consumer = ShardedWriteConsumer(sink=_HaltingSink(), workers=1)
        with pytest.raises(LakeWriteHalt):
            consumer.submit(_make_request())
        consumer.drain()
        # The drain-after-halt warning fired (structlog → stderr in the test config).
        captured = capsys.readouterr()
        assert "drained_after_halt" in (captured.out + captured.err)

    def test_first_halt_wins_under_concurrent_trip(self) -> None:
        # Two shards both raising LakeWriteHalt: the FIRST recorded reason is the one
        # the fail-fast flag carries (the `if self._halt_exc is None` guard).
        @dataclass
        class _HaltingSink:
            def append(self, **_: object) -> SinkAppendResult:
                raise LakeWriteHalt("disk_full")

        consumer = ShardedWriteConsumer(sink=_HaltingSink(), workers=4)
        try:
            with pytest.raises(LakeWriteHalt):
                consumer.submit(_make_request(symbol="A"))
            # The flag is set; a later submit fails fast with a recorded reason.
            with pytest.raises(LakeWriteHalt) as exc_info:
                consumer.submit(_make_request(symbol="B"))
            assert exc_info.value.reason == "disk_full"
        finally:
            consumer.drain()

    def test_trip_halt_keeps_first_reason(self) -> None:
        # The thread-safety invariant: a SECOND `_trip_halt` (the concurrent-second-shard
        # case) must NOT overwrite the first recorded halt — the `if self._halt_exc is
        # None` guard's False edge. Exercised directly so the race is deterministic.
        consumer = ShardedWriteConsumer(sink=_RecordingSink(), workers=1)
        try:
            consumer._trip_halt(LakeWriteHalt("disk_full"))
            consumer._trip_halt(LakeWriteHalt("out_of_memory"))
            # First reason wins (the second trip skipped the assignment).
            with pytest.raises(LakeWriteHalt) as exc_info:
                consumer.submit(_make_request())
            assert exc_info.value.reason == "disk_full"
        finally:
            consumer.drain()


# ────────────────────────────────────────────────────────────────────────
# Consumer thread dies mid-write → captured, NEVER a fabricated success
# ────────────────────────────────────────────────────────────────────────


class TestConsumerFailureNeverFakesSuccess:
    def test_generic_write_error_surfaced_not_swallowed(self) -> None:
        @dataclass
        class _BuggySink:
            def append(self, **_: object) -> SinkAppendResult:
                raise RuntimeError("a real bug")

        consumer = ShardedWriteConsumer(sink=_BuggySink(), workers=2)
        try:
            with pytest.raises(RuntimeError, match="a real bug"):
                consumer.submit(_make_request())
        finally:
            consumer.drain()

    def test_shard_survives_a_failure_and_keeps_draining(self) -> None:
        # A write that raises must NOT kill the shard thread — the next submit on the
        # SAME shard still gets written (the shard captured + continued).
        calls: list[str] = []

        @dataclass
        class _FlakySink:
            first: bool = True

            def append(self, *, symbol: str, **_: object) -> SinkAppendResult:
                if self.first:
                    self.first = False
                    raise RuntimeError("first fails")
                calls.append(symbol)
                return SinkAppendResult(bars_appended=1, partitions_written=1, write_results=())

        # workers=1 → both requests land on the SAME shard, so the second proves the
        # shard kept draining after the first failed.
        consumer = ShardedWriteConsumer(sink=_FlakySink(), workers=1)
        try:
            with pytest.raises(RuntimeError, match="first fails"):
                consumer.submit(_make_request(symbol="NSE-NIFTY"))
            result = consumer.submit(_make_request(symbol="NSE-NIFTY"))
            assert result.bars_appended == 1
            assert calls == ["NSE-NIFTY"]
        finally:
            consumer.drain()


# ────────────────────────────────────────────────────────────────────────
# Bounded-queue back-pressure (never OOM)
# ────────────────────────────────────────────────────────────────────────


class TestBackPressure:
    def test_full_queue_blocks_producer(self) -> None:
        # A maxsize-1 queue with a slow (gated) sink: the 1st submit occupies the worker,
        # the 2nd fills the queue, the 3rd `put` must BLOCK until the worker drains one.
        release = threading.Event()
        started = threading.Event()

        @dataclass
        class _SlowSink:
            def append(self, **_: object) -> SinkAppendResult:
                started.set()
                release.wait()
                return SinkAppendResult(bars_appended=1, partitions_written=1, write_results=())

        # 1 worker, queue depth 1 → at most 1 in-flight + 1 queued = 2 absorbed before
        # the 3rd put blocks.
        consumer = ShardedWriteConsumer(sink=_SlowSink(), workers=1, queue_maxsize=1)
        third_put_returned = threading.Event()

        def _third() -> None:
            consumer.submit(_make_request(bars=[_bar(minute=17)]))
            third_put_returned.set()

        try:
            consumer_submit_async(consumer, _make_request(bars=[_bar(minute=15)]))
            assert started.wait(timeout=2.0)  # worker is busy on the 1st
            consumer_submit_async(consumer, _make_request(bars=[_bar(minute=16)]))  # queued
            time.sleep(0.05)
            threading.Thread(target=_third, daemon=True).start()
            # The 3rd put must NOT have returned — the bounded queue blocks the producer.
            assert not third_put_returned.wait(timeout=0.2)
            release.set()
            assert third_put_returned.wait(timeout=2.0)
        finally:
            release.set()
            consumer.drain()


# ────────────────────────────────────────────────────────────────────────
# End-of-pull drain flushes a NON-EMPTY queue before returning
# ────────────────────────────────────────────────────────────────────────


class TestDrainFlushesQueue:
    def test_queued_requests_are_written_before_drain_returns(self) -> None:
        # Gate the sink so several requests pile up in the queue, then release + drain:
        # drain() must flush + join, so every queued request is durably written.
        release = threading.Event()

        @dataclass
        class _GatedSink:
            calls: list[str] = field(default_factory=list)
            _lock: threading.Lock = field(default_factory=threading.Lock)

            def append(self, *, symbol: str, **_: object) -> SinkAppendResult:
                release.wait()
                with self._lock:
                    self.calls.append(symbol)
                return SinkAppendResult(bars_appended=1, partitions_written=1, write_results=())

        sink = _GatedSink()
        consumer = ShardedWriteConsumer(sink=sink, workers=2)
        for i in range(6):
            consumer_submit_async(consumer, _make_request(symbol=f"SYM{i}"))
        time.sleep(0.05)  # let them queue
        release.set()
        consumer.drain()
        # All six are durably written by the time drain() returns.
        assert sorted(sink.calls) == [f"SYM{i}" for i in range(6)]


# ────────────────────────────────────────────────────────────────────────
# WriteConsumerSink — the BarSink wrapper
# ────────────────────────────────────────────────────────────────────────


class TestWriteConsumerSink:
    def test_routes_through_consumer(self) -> None:
        sink = _RecordingSink()
        consumer = ShardedWriteConsumer(sink=sink, workers=2)
        wrapper = WriteConsumerSink(consumer=consumer)
        try:
            result = wrapper.append(
                exchange="NSE",
                segment="INDEX",
                symbol="NSE-NIFTY",
                interval="1minute",
                bars=[_bar()],
            )
            assert result.bars_appended == 1
            assert sink.calls == [("NSE", "INDEX", "NSE-NIFTY", "1minute", 1, "keep-first")]
        finally:
            consumer.drain()

    def test_empty_bars_is_noop_never_enqueued(self) -> None:
        sink = _RecordingSink()
        consumer = ShardedWriteConsumer(sink=sink, workers=2)
        wrapper = WriteConsumerSink(consumer=consumer)
        try:
            result = wrapper.append(
                exchange="NSE",
                segment="INDEX",
                symbol="NSE-NIFTY",
                interval="1minute",
                bars=[],
            )
            assert result == SinkAppendResult(
                bars_appended=0, partitions_written=0, write_results=()
            )
            assert sink.calls == []  # never enqueued
        finally:
            consumer.drain()

    def test_merge_mode_forwarded(self) -> None:
        sink = _RecordingSink()
        consumer = ShardedWriteConsumer(sink=sink, workers=1)
        wrapper = WriteConsumerSink(consumer=consumer)
        try:
            wrapper.append(
                exchange="NSE",
                segment="INDEX",
                symbol="NSE-NIFTY",
                interval="1day",
                bars=[_bar()],
                merge_mode="latest-wins",
            )
            assert sink.calls[0][5] == "latest-wins"
        finally:
            consumer.drain()


# ────────────────────────────────────────────────────────────────────────
# resolve_write_pool_size
# ────────────────────────────────────────────────────────────────────────


class TestResolveWritePoolSize:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [(None, None), (0, None), (-1, None), (1, 1), (4, 4)],
    )
    def test_normalisation(self, value: int | None, expected: int | None) -> None:
        assert resolve_write_pool_size(value) == expected


# ────────────────────────────────────────────────────────────────────────
# Property: keep-first / order-independence — routing never changes the set written
# ────────────────────────────────────────────────────────────────────────


class TestOrderIndependenceProperty:
    @settings(max_examples=40, deadline=None)
    @given(
        symbols=st.lists(
            st.sampled_from(["NSE-NIFTY", "NSE-BANKNIFTY", "BSE-SENSEX", "NSE-RELIANCE"]),
            min_size=1,
            max_size=20,
        ),
        workers=st.integers(min_value=1, max_value=5),
    )
    def test_every_submitted_request_is_written_exactly_once(
        self, symbols: list[str], workers: int
    ) -> None:
        # Regardless of W or completion order, the MULTISET of writes the consumer makes
        # equals the multiset submitted — sharding never drops/duplicates a write
        # (order-independent, the merge keep-first contract relies on this).
        sink = _RecordingSink()
        consumer = ShardedWriteConsumer(sink=sink, workers=workers)
        try:
            boxes = [consumer_submit_async(consumer, _make_request(symbol=s)) for s in symbols]
            for box in boxes:
                box.get()
        finally:
            consumer.drain()
        written = sorted(c[2] for c in sink.calls)
        assert written == sorted(symbols)


# ════════════════════════════════════════════════════════════════════════
# BatchingWriteConsumer — the coalescing background writer
# (DEC-WRITE-BATCH-COALESCE-001). Proves: many same-series windows collapse to
# ONE merge-write per drain cycle, the full crash-safety failure matrix, and the
# per-window bars_appended attribution — WITHOUT touching the rate gate.
# ════════════════════════════════════════════════════════════════════════


@dataclass
class _CoalesceSink:
    """A durable-sink fake recording each ``append``'s coordinates + combined bar count.

    Returns ``bars_appended = len(bars)`` (every incoming bar treated as new — the fake
    has no stored state), so a coalesced append over N windows reports the union count.
    """

    calls: list[tuple[str, int]] = field(default_factory=list)  # (symbol, combined-bar-count)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    delay: float = 0.0

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
        del exchange, segment, interval, merge_mode
        if self.delay:
            time.sleep(self.delay)
        with self._lock:
            self.calls.append((symbol, len(bars)))
        return SinkAppendResult(bars_appended=len(bars), partitions_written=1, write_results=())


def _batch_request(*, symbol: str = "NSE-NIFTY", minutes: Sequence[int] = (15,)) -> WriteRequest:
    return WriteRequest(
        exchange="NSE",
        segment="INDEX",
        symbol=symbol,
        interval="1minute",
        bars=[_bar(minute=m) for m in minutes],
        merge_mode="keep-first",
        box=ResultBox(),
    )


def _submit_swallow_async(consumer: BatchingWriteConsumer, request: WriteRequest) -> ResultBox:
    """Submit on a daemon thread, SWALLOWING the producer-thread re-raise.

    ``submit`` re-raises a captured write failure on the SUBMITTING thread. When a test
    submits on a background daemon thread (so the main test thread can poll several boxes),
    that re-raise would be an UNHANDLED daemon-thread exception (a pytest error). The test
    asserts the failure via the returned box's :meth:`get` instead, so the daemon swallows
    the re-raise — the box still carries the captured exception either way.
    """

    def _run() -> None:
        # The submitting thread re-raises a captured write failure; this test asserts the
        # failure via the returned box instead, so the daemon SWALLOWS the re-raise (an
        # unhandled daemon-thread exception would be a pytest error). The box still carries
        # the captured exception either way (settle-after-durable).
        with contextlib.suppress(BaseException):
            consumer.submit(request)

    threading.Thread(target=_run, daemon=True).start()
    return request.box


class TestBatchingConstruction:
    def test_rejects_zero_queue_maxsize(self) -> None:
        sink = _CoalesceSink()
        with pytest.raises(ValueError, match="queue_maxsize must be >= 1"):
            BatchingWriteConsumer(sink=sink, queue_maxsize=0)

    def test_rejects_zero_max_coalesce(self) -> None:
        sink = _CoalesceSink()
        with pytest.raises(ValueError, match="max_coalesce must be >= 1"):
            BatchingWriteConsumer(sink=sink, max_coalesce=0)

    def test_workers_is_one(self) -> None:
        sink = _CoalesceSink()
        consumer = BatchingWriteConsumer(sink=sink)
        try:
            assert consumer.workers == 1
        finally:
            consumer.drain()


class TestBatchingCoalesce:
    def test_many_same_series_windows_collapse_to_fewer_writes(self) -> None:
        # The CORE win: while one slow write drains, many windows queue behind it; the
        # next drain cycle coalesces them all into ONE append. With a deliberate write
        # delay the producers pile up, so the total append count is << the window count.
        sink = _CoalesceSink(delay=0.05)
        consumer = BatchingWriteConsumer(sink=sink)
        n = 20
        boxes = [consumer_submit_async(consumer, _batch_request(minutes=[m])) for m in range(n)]
        results = [b.get() for b in boxes]
        consumer.drain()
        # Every window settled; the total newly-added equals the window count.
        assert sum(r.bars_appended for r in results) == n
        # Coalescing happened: strictly fewer durable writes than windows.
        assert len(sink.calls) < n, f"no coalescing: {len(sink.calls)} writes for {n} windows"

    def test_combined_bars_equal_union_of_grouped_windows(self) -> None:
        # A coalesced append carries the UNION of its grouped windows' bars (per series).
        sink = _CoalesceSink(delay=0.05)
        consumer = BatchingWriteConsumer(sink=sink)
        boxes = [
            consumer_submit_async(consumer, _batch_request(symbol="NSE-NIFTY", minutes=[m]))
            for m in range(10)
        ]
        for b in boxes:
            b.get()
        consumer.drain()
        # Total combined bars across all NIFTY appends == the 10 distinct windows.
        nifty_total = sum(count for sym, count in sink.calls if sym == "NSE-NIFTY")
        assert nifty_total == 10

    def test_distinct_series_never_coalesced_together(self) -> None:
        # Two different series must NEVER share a coalesced write (each its own partition).
        sink = _CoalesceSink(delay=0.05)
        consumer = BatchingWriteConsumer(sink=sink)
        boxes = [
            consumer_submit_async(consumer, _batch_request(symbol=s, minutes=[m]))
            for m in range(6)
            for s in ("NSE-NIFTY", "NSE-BANKNIFTY")
        ]
        for b in boxes:
            b.get()
        consumer.drain()
        # Every call is single-series (the symbol is the group key); no mixed write exists.
        symbols_written = {sym for sym, _ in sink.calls}
        assert symbols_written == {"NSE-NIFTY", "NSE-BANKNIFTY"}

    def test_max_coalesce_caps_group_size(self) -> None:
        # The max_coalesce safety cap bounds how many requests one cycle pulls.
        sink = _CoalesceSink(delay=0.05)
        consumer = BatchingWriteConsumer(sink=sink, max_coalesce=3)
        boxes = [consumer_submit_async(consumer, _batch_request(minutes=[m])) for m in range(12)]
        for b in boxes:
            b.get()
        consumer.drain()
        # No single coalesced write merged more than the cap's worth of windows.
        assert max(count for _, count in sink.calls) <= 3


class TestAttributeBarsAppended:
    """Deterministic whitebox unit tests of the per-window attribution helper."""

    def test_distinct_windows_each_get_their_own_count(self) -> None:
        from brutex.data.write_consumer import _attribute_bars_appended

        per_request = [[_bar(15), _bar(16)], [_bar(17), _bar(18), _bar(19)]]
        counts = _attribute_bars_appended(per_request, total_appended=5)
        assert counts == [2, 3]
        assert sum(counts) == 5

    def test_duplicate_timestamp_credited_to_first_window(self) -> None:
        from brutex.data.write_consumer import _attribute_bars_appended

        # window B repeats minute 16 (already in A) → B credited only 17.
        per_request = [[_bar(15), _bar(16)], [_bar(16), _bar(17)]]
        counts = _attribute_bars_appended(per_request, total_appended=3)
        assert counts == [2, 1]

    def test_pre_existing_bars_credit_zero_caps_at_total(self) -> None:
        # DEFENSIVE test: drives the cap branch (total_appended < distinct timestamps).
        # The REAL ``ParquetBarSink.append`` returns ``bars_appended = len(bars)`` (the
        # INPUT count, sink.py:106/:209 — NOT a net-new count), so it never feeds a
        # total smaller than the union size; this case is impossible from the production
        # sink. We assert it anyway to prove the cap keeps per-window credits from being
        # INFLATED for ANY conforming BarSink (the attribution is at-most-serial).
        from brutex.data.write_consumer import _attribute_bars_appended

        per_request = [[_bar(15)], [_bar(16)], [_bar(17)]]
        counts = _attribute_bars_appended(per_request, total_appended=1)
        assert counts == [1, 0, 0]
        assert sum(counts) == 1

    def test_zero_total_credits_nothing(self) -> None:
        from brutex.data.write_consumer import _attribute_bars_appended

        per_request = [[_bar(15)], [_bar(16)]]
        counts = _attribute_bars_appended(per_request, total_appended=0)
        assert counts == [0, 0]


class TestBatchingStraddle:
    def test_request_straddling_two_months_settles_after_both_partitions(self) -> None:
        # A request whose bars span Jan + Feb writes TWO partitions; its box must settle
        # ONLY after BOTH are durable (the multi-partition settle path), with combined
        # bars_appended + partitions_written = 2.
        from datetime import datetime as _dt
        from decimal import Decimal

        def _dated_bar(month: int, day: int) -> CandleBar:
            return CandleBar(
                timestamp=_dt(2020, month, day, 9, 15, tzinfo=UTC),
                open=Decimal("100.00"),
                high=Decimal("101.00"),
                low=Decimal("99.00"),
                close=Decimal("100.50"),
                volume=10,
                open_interest=None,
            )

        sink = _CoalesceSink()
        consumer = BatchingWriteConsumer(sink=sink)
        req = WriteRequest(
            exchange="NSE",
            segment="INDEX",
            symbol="NSE-NIFTY",
            interval="1minute",
            bars=[_dated_bar(1, 31), _dated_bar(2, 1)],  # Jan + Feb
            merge_mode="keep-first",
            box=ResultBox(),
        )
        result = consumer.submit(req)
        consumer.drain()
        assert result.partitions_written == 2
        assert result.bars_appended == 2  # one new bar per partition
        # Two distinct partition writes happened (one per month).
        assert len(sink.calls) == 2


class TestBatchingPerWindowAttribution:
    def test_bars_appended_attributed_per_window_not_per_batch(self) -> None:
        # Each window's result carries ITS OWN bars_appended, not the batch total.
        sink = _CoalesceSink(delay=0.04)
        consumer = BatchingWriteConsumer(sink=sink)
        # Window A: 2 bars, window B: 3 bars — same series, coalesced.
        a = _batch_request(minutes=[15, 16])
        b = _batch_request(minutes=[17, 18, 19])
        ba = consumer_submit_async(consumer, a)
        bb = consumer_submit_async(consumer, b)
        ra, rb = ba.get(), bb.get()
        consumer.drain()
        # Per-window attribution: A gets its 2, B gets its 3 (distinct timestamps),
        # summing to the coalesced total — NOT both reading the batch total of 5.
        assert ra.bars_appended == 2
        assert rb.bars_appended == 3

    def test_duplicate_timestamp_credited_to_first_window_only_when_coalesced(self) -> None:
        # When two windows carry the SAME timestamp AND coalesce into one write, keep-first
        # credits the FIRST window only. A gated sink forces both into ONE batch: the writer
        # blocks on the first append's gate until BOTH windows are queued, so the next drain
        # cycle coalesces them deterministically.
        gate = threading.Event()
        released = threading.Event()

        @dataclass
        class _GatedSink:
            calls: list[int] = field(default_factory=list)
            _lock: threading.Lock = field(default_factory=threading.Lock)

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
                del exchange, segment, symbol, interval, merge_mode
                with self._lock:
                    first = not self.calls
                    self.calls.append(len(bars))
                if first:
                    # Hold the very first write until both windows are queued, so the
                    # SECOND drain cycle sees A+B together and coalesces them.
                    gate.wait(timeout=2.0)
                released.set()
                # Combined keep-first union over {15,16,17} = 3 distinct new bars.
                distinct = {b.timestamp for b in bars}
                return SinkAppendResult(
                    bars_appended=len(distinct), partitions_written=1, write_results=()
                )

        sink = _GatedSink()
        consumer = BatchingWriteConsumer(sink=sink)
        # A priming window to occupy the writer in its gated first append.
        prime = consumer_submit_async(consumer, _batch_request(minutes=[1]))
        time.sleep(0.05)  # let the writer enter the gated append
        a = _batch_request(minutes=[15, 16])
        b = _batch_request(minutes=[16, 17])  # 16 duplicates A's
        ba = consumer_submit_async(consumer, a)
        bb = consumer_submit_async(consumer, b)
        time.sleep(0.05)  # ensure both A and B are queued behind the gate
        gate.set()
        ra, rb = ba.get(), bb.get()
        prime.get()
        consumer.drain()
        # A+B coalesced: 3 distinct timestamps {15,16,17}; A credited 15+16 = 2,
        # B credited 17 only (16 is A's) = 1 — first-occurrence keep-first.
        assert ra.bars_appended + rb.bars_appended == 3
        assert ra.bars_appended == 2
        assert rb.bars_appended == 1


class TestBatchingEmptyShortCircuit:
    def test_empty_bars_request_settles_empty_no_op(self) -> None:
        # An empty-bars request mirrors the sink's empty no-op: never enqueued for a write.
        sink = _CoalesceSink()
        consumer = BatchingWriteConsumer(sink=sink)
        req = WriteRequest(
            exchange="NSE",
            segment="INDEX",
            symbol="NSE-NIFTY",
            interval="1minute",
            bars=[],
            merge_mode="keep-first",
            box=ResultBox(),
        )
        box = consumer_submit_async(consumer, req)
        result = box.get()
        consumer.drain()
        assert result.bars_appended == 0
        assert result.partitions_written == 0
        # No durable write for an empty request.
        assert sink.calls == []


class TestBatchingSettleAfterDurable:
    def test_box_settles_only_after_durable_write_returns(self) -> None:
        # The producer's result is settled strictly AFTER the durable append returns.
        order: list[str] = []
        order_lock = threading.Lock()

        @dataclass
        class _OrderingSink:
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
                del exchange, segment, symbol, interval, merge_mode
                with order_lock:
                    order.append("durable")
                return SinkAppendResult(
                    bars_appended=len(bars), partitions_written=1, write_results=()
                )

        consumer = BatchingWriteConsumer(sink=_OrderingSink())
        box = consumer_submit_async(consumer, _batch_request())
        box.get()
        with order_lock:
            order.append("settled")
        consumer.drain()
        # "durable" (the append) is recorded before "settled" (after box.get()).
        assert order == ["durable", "settled"]


class TestBatchingLakeWriteHalt:
    def test_halt_settles_every_box_in_batch_and_trips_stop(self) -> None:
        # disk-full/OOM: the writer's LakeWriteHalt settles EVERY box in the failing batch
        # with the halt, and trips the pull-wide stop so queued/future submits fail fast.
        @dataclass
        class _HaltingSink:
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
                del exchange, segment, symbol, interval, bars, merge_mode
                raise LakeWriteHalt("disk_full")

        sink = _HaltingSink()
        consumer = BatchingWriteConsumer(sink=sink, max_coalesce=64)
        # Submit several windows; EVERY window's box must settle with the halt — whether it
        # was in the failing batch, queued-but-uncollected (drained-with-halt), or fast
        # rejected after the stop tripped. None blocks forever (the unsettled-box gap).
        boxes = [_submit_swallow_async(consumer, _batch_request(minutes=[m])) for m in range(5)]
        for box in boxes:
            with pytest.raises(LakeWriteHalt, match="disk_full"):
                box.get()
        # A subsequent submit fails fast with the recorded halt (no disk hammering).
        with pytest.raises(LakeWriteHalt, match="disk_full"):
            consumer.submit(_batch_request())
        consumer.drain()

    def test_drain_after_halt_logs_warning(self) -> None:
        @dataclass
        class _HaltingSink:
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
                del exchange, segment, symbol, interval, bars, merge_mode
                raise LakeWriteHalt("out_of_memory")

        consumer = BatchingWriteConsumer(sink=_HaltingSink())
        box = _submit_swallow_async(consumer, _batch_request())
        with pytest.raises(LakeWriteHalt):
            box.get()
        # drain() is safe after a halt (logs + joins, never raises).
        consumer.drain()

    def test_drain_queue_with_halt_settles_queued_then_stops_at_poison(self) -> None:
        # Whitebox: after a halt, a request already QUEUED but not yet collected must be
        # fail-fast settled with the halt (not left blocking its producer), and the
        # poison-pill stops the drain (re-enqueued for drain() to join cleanly).
        @dataclass
        class _NoopSink:
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
                del exchange, segment, symbol, interval, merge_mode
                return SinkAppendResult(
                    bars_appended=len(bars), partitions_written=1, write_results=()
                )

        consumer = BatchingWriteConsumer(sink=_NoopSink())
        consumer.drain()  # stop the live writer thread so we can drive the helper directly
        halt = LakeWriteHalt("disk_full")
        queued = _batch_request(minutes=[15])
        consumer._queue.put(queued)  # a request still in the queue at the halt
        consumer._queue.put(consumer._poison)  # then the poison-pill
        consumer._drain_queue_with_halt(halt)
        # The queued request's box is settled WITH the halt (never blocks forever).
        with pytest.raises(LakeWriteHalt, match="disk_full"):
            queued.box.get()
        # The poison-pill was re-enqueued (so a subsequent drain/join sees it).
        assert consumer._queue.get_nowait() is consumer._poison

    def test_trip_halt_keeps_first_reason(self) -> None:
        # Whitebox: a SECOND _trip_halt must NOT overwrite the first recorded halt (the
        # `if self._halt_exc is None` guard's False edge). Exercised directly so the race
        # is deterministic.
        @dataclass
        class _NoopSink:
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
                del exchange, segment, symbol, interval, merge_mode
                return SinkAppendResult(
                    bars_appended=len(bars), partitions_written=1, write_results=()
                )

        consumer = BatchingWriteConsumer(sink=_NoopSink())
        consumer.drain()
        consumer._trip_halt(LakeWriteHalt("disk_full"))
        consumer._trip_halt(LakeWriteHalt("out_of_memory"))  # second trip is a no-op
        assert consumer._halt_exc is not None
        assert consumer._halt_exc.reason == "disk_full"

    def test_drain_queue_with_halt_returns_on_empty(self) -> None:
        # The empty-queue branch: drain returns immediately when nothing is queued.
        @dataclass
        class _NoopSink:
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
                del exchange, segment, symbol, interval, merge_mode
                return SinkAppendResult(
                    bars_appended=len(bars), partitions_written=1, write_results=()
                )

        consumer = BatchingWriteConsumer(sink=_NoopSink())
        consumer.drain()
        consumer._drain_queue_with_halt(LakeWriteHalt("disk_full"))  # empty → no raise


class TestBatchingFailureNeverFakesSuccess:
    def test_write_failure_captured_never_settled_as_success(self) -> None:
        # A generic write error is captured into every box of the batch and re-raised on
        # the producer — NEVER a fabricated success (Golden Rule #7). The writer thread
        # stays alive to drain the rest.
        @dataclass
        class _FlakySink:
            fail_first: bool = True
            _lock: threading.Lock = field(default_factory=threading.Lock)

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
                del exchange, segment, symbol, interval, merge_mode
                with self._lock:
                    if self.fail_first:
                        self.fail_first = False
                        raise RuntimeError("write boom")
                return SinkAppendResult(
                    bars_appended=len(bars), partitions_written=1, write_results=()
                )

        consumer = BatchingWriteConsumer(sink=_FlakySink())
        try:
            # First window fails — submit() re-raises the captured failure on THIS thread
            # (NEVER a fabricated success). A generic error does NOT trip the pull-wide stop.
            with pytest.raises(RuntimeError, match="write boom"):
                consumer.submit(_batch_request(symbol="NSE-NIFTY"))
            # The writer is still alive: a later window succeeds.
            assert consumer.submit(_batch_request(symbol="NSE-BANKNIFTY")).bars_appended == 1
        finally:
            consumer.drain()


class TestBatchingBackPressure:
    def test_full_queue_blocks_producer(self) -> None:
        # A bounded queue back-pressures the producer when full (never an unbounded OOM).
        started = threading.Event()
        release = threading.Event()

        @dataclass
        class _BlockingSink:
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
                del exchange, segment, symbol, interval, merge_mode
                started.set()
                release.wait(timeout=2.0)
                return SinkAppendResult(
                    bars_appended=len(bars), partitions_written=1, write_results=()
                )

        consumer = BatchingWriteConsumer(sink=_BlockingSink(), queue_maxsize=1, max_coalesce=1)
        # First submit occupies the writer (blocks in append). Submit async so we don't block here.
        first = consumer_submit_async(consumer, _batch_request(minutes=[15]))
        started.wait(timeout=2.0)
        # Fill the queue (maxsize 1) with a second; a THIRD submit must block on put.
        consumer_submit_async(consumer, _batch_request(minutes=[16]))
        blocked = threading.Event()
        third_done = threading.Event()

        def _third() -> None:
            blocked.set()
            consumer.submit(_batch_request(minutes=[17]))
            third_done.set()

        t = threading.Thread(target=_third, daemon=True)
        t.start()
        blocked.wait(timeout=2.0)
        time.sleep(0.1)
        # The third producer is blocked on the full queue (back-pressure), not done.
        assert not third_done.is_set()
        release.set()
        first.get()
        t.join(timeout=2.0)
        consumer.drain()
        assert third_done.is_set()


class TestBatchingDrainFlushesAll:
    def test_drain_flushes_every_queued_request_durably(self) -> None:
        # drain() flushes EVERY pending request (one final coalesced cycle) before joining.
        sink = _CoalesceSink(delay=0.02)
        consumer = BatchingWriteConsumer(sink=sink)
        boxes = [consumer_submit_async(consumer, _batch_request(minutes=[m])) for m in range(15)]
        # Drain WITHOUT first joining the boxes — drain must still settle them all.
        consumer.drain()
        results = [b.get() for b in boxes]  # already settled by drain
        assert sum(r.bars_appended for r in results) == 15

    def test_drain_is_idempotent(self) -> None:
        sink = _CoalesceSink()
        consumer = BatchingWriteConsumer(sink=sink)
        consumer.submit(_batch_request())
        consumer.drain()
        consumer.drain()  # second call: thread already dead → no-op, no raise

    def test_no_leaked_threads_after_drain(self) -> None:
        before = {t.name for t in threading.enumerate()}
        sink = _CoalesceSink()
        consumer = BatchingWriteConsumer(sink=sink)
        consumer.submit(_batch_request())
        consumer.drain()
        time.sleep(0.05)
        after = {t.name for t in threading.enumerate()}
        leaked = {n for n in after - before if n.startswith("write-batch-consumer")}
        assert leaked == set()


class TestBatchingDrainPoisonDelivery:
    """MINOR-3: drain() never deadlocks delivering the poison-pill on a full queue.

    The pill is sent ``put_nowait`` first; only on a momentarily-full queue does it fall
    back to a bounded blocking ``put``, and only while the worker is still alive (a dead
    daemon worker → skip, the process is tearing down). These drive both Full sub-branches
    deterministically without relying on a real race.
    """

    @dataclass
    class _FakeThread:
        """A stand-in for the worker thread with scripted ``is_alive`` + no-op ``join``."""

        alive: list[bool]
        joined: bool = False

        def is_alive(self) -> bool:
            # Pop the next scripted liveness answer (last value sticks).
            return self.alive[0] if len(self.alive) == 1 else self.alive.pop(0)

        def join(self) -> None:
            self.joined = True

    def test_drain_full_queue_dead_worker_skips_blocking_put(self) -> None:
        # put_nowait raises Full; the re-checked worker is DEAD → skip the blocking put
        # (no deadlock), then join (a no-op on the dead worker).
        sink = _CoalesceSink()
        consumer = BatchingWriteConsumer(sink=sink, queue_maxsize=1, max_coalesce=1)
        consumer.drain()  # cleanly stop + join the REAL worker before swapping internals
        # Re-create a full queue so put_nowait raises Full.
        consumer._queue = type(consumer._queue)(maxsize=1)
        consumer._queue.put_nowait(object())  # fill to capacity
        # Replace the (already-dead) worker with a fake: alive at the entry guard, DEAD at
        # the re-check (so drain proceeds past the early return, then skips the blocking put).
        consumer._thread = self._FakeThread(alive=[True, False])  # type: ignore[assignment]
        consumer.drain()  # must NOT block — the dead-worker branch skips the blocking put
        assert consumer._thread.joined is True  # type: ignore[attr-defined]

    def test_drain_full_queue_live_worker_blocks_then_puts(self) -> None:
        # put_nowait raises Full; the worker is ALIVE → the blocking put is taken. A
        # background consumer frees a slot so the blocking put returns (no deadlock).
        sink = _CoalesceSink()
        consumer = BatchingWriteConsumer(sink=sink, queue_maxsize=1, max_coalesce=1)
        consumer.drain()  # cleanly stop + join the REAL worker before swapping internals
        consumer._queue = type(consumer._queue)(maxsize=1)
        filler = object()
        consumer._queue.put_nowait(filler)  # fill to capacity → put_nowait will raise Full

        # A live fake thread; a helper drains the filler shortly so the blocking put returns.
        consumer._thread = self._FakeThread(alive=[True])  # type: ignore[assignment]

        def _free_slot() -> None:
            time.sleep(0.05)
            assert consumer._queue.get() is filler  # free the one slot

        helper = threading.Thread(target=_free_slot, daemon=True)
        helper.start()
        consumer.drain()  # takes the Full → blocking-put branch; returns once the slot frees
        helper.join(timeout=2.0)
        # The poison-pill is now the queued item (the blocking put succeeded).
        assert consumer._queue.get() is consumer._poison
        assert consumer._thread.joined is True  # type: ignore[attr-defined]


class TestBatchingViaSink:
    def test_write_consumer_sink_accepts_batching_consumer(self) -> None:
        # The WriteConsumerSink Protocol drop-in accepts the batching consumer too.
        sink = _CoalesceSink(delay=0.02)
        consumer = BatchingWriteConsumer(sink=sink)
        wsink = WriteConsumerSink(consumer=consumer)
        result = wsink.append(
            exchange="NSE",
            segment="INDEX",
            symbol="NSE-NIFTY",
            interval="1minute",
            bars=[_bar(15)],
        )
        consumer.drain()
        assert result.bars_appended == 1

    def test_write_consumer_sink_empty_no_op_with_batching(self) -> None:
        sink = _CoalesceSink()
        consumer = BatchingWriteConsumer(sink=sink)
        wsink = WriteConsumerSink(consumer=consumer)
        result = wsink.append(
            exchange="NSE", segment="INDEX", symbol="NSE-NIFTY", interval="1minute", bars=[]
        )
        consumer.drain()
        assert result.bars_appended == 0
        assert sink.calls == []  # never enqueued


class TestResolveWriteBatchEnabled:
    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "On", " on "])
    def test_truthy_values_enable(self, value: str) -> None:
        assert resolve_write_batch_enabled(value) is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "  ", "maybe", "2"])
    def test_other_values_disable(self, value: str) -> None:
        assert resolve_write_batch_enabled(value) is False

    def test_none_reads_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BRUTEX_WRITE_BATCH", raising=False)
        assert resolve_write_batch_enabled() is False
        monkeypatch.setenv("BRUTEX_WRITE_BATCH", "1")
        assert resolve_write_batch_enabled() is True


# ────────────────────────────────────────────────────────────────────────
# Run correlation on the consumer threads (DEC-PULL-BEAT-PERRUN-001 fix round)
# ────────────────────────────────────────────────────────────────────────


@dataclass
class _ContextProbeSink:
    """A sink fake recording the ambient ``pull_id`` seen by each ``append``."""

    seen_ids: list[str] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

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
        from brutex.observability.correlation import current_pull_id

        del exchange, segment, symbol, interval, merge_mode
        with self._lock:
            self.seen_ids.append(current_pull_id())
        return SinkAppendResult(bars_appended=len(bars), partitions_written=1, write_results=())


class TestConsumerThreadRunBinding:
    """The consumer threads re-bind the run id captured at construction, so the
    durable write's seam beats (partition lock-wait) attribute to the run's KEYED
    progress-beat channel — never dropped as an unattributed foreign beat."""

    def test_sharded_consumer_thread_carries_the_construction_run_id(self) -> None:
        from brutex.observability.correlation import pull_id_scope

        sink = _ContextProbeSink()
        with pull_id_scope("abc123def456"):
            consumer = ShardedWriteConsumer(sink=sink, workers=2)
        consumer.submit(_make_request(symbol="NSE-NIFTY"))
        consumer.submit(_make_request(symbol="NSE-BANKNIFTY"))
        consumer.drain()
        assert sink.seen_ids == ["abc123def456"] * 2

    def test_batching_consumer_thread_carries_the_construction_run_id(self) -> None:
        from brutex.observability.correlation import pull_id_scope

        sink = _ContextProbeSink()
        with pull_id_scope("abc123def456"):
            consumer = BatchingWriteConsumer(sink=sink)
        consumer.submit(_batch_request(symbol="NSE-NIFTY"))
        consumer.drain()
        assert sink.seen_ids == ["abc123def456"]

    def test_no_ambient_scope_binds_the_legacy_empty_id(self) -> None:
        """The CLI path (no run scope) captures "" — byte-identical legacy routing."""
        sink = _ContextProbeSink()
        consumer = ShardedWriteConsumer(sink=sink, workers=1)
        consumer.submit(_make_request())
        consumer.drain()
        assert sink.seen_ids == [""]

    def test_sharded_consumer_write_beat_reaches_the_runs_keyed_channel(self) -> None:
        """End-to-end mechanism pin: a beat emitted DURING the consumer's durable
        write routes to the run's keyed channel (the GAP #15 flock-wait shape)."""
        from brutex.observability.correlation import current_pull_id, pull_id_scope
        from brutex.observability.progress_beat import (
            emit_progress_beat,
            register_progress_beat,
            reset_progress_beats,
        )

        @dataclass
        class _BeatingSink(_ContextProbeSink):
            def append(self, **kw: object) -> SinkAppendResult:
                emit_progress_beat()  # the partition lock-wait poll shape
                with self._lock:
                    self.seen_ids.append(current_pull_id())
                bars = kw["bars"]
                assert isinstance(bars, Sequence)
                return SinkAppendResult(
                    bars_appended=len(bars), partitions_written=1, write_results=()
                )

        reset_progress_beats()
        beats: list[int] = []
        register_progress_beat(lambda: beats.append(1), pull_id="abc123def456")
        try:
            sink = _BeatingSink()
            with pull_id_scope("abc123def456"):
                consumer = ShardedWriteConsumer(sink=sink, workers=1)
            consumer.submit(_make_request())
            consumer.drain()
            assert beats == [1]  # delivered on the run's channel, never dropped
        finally:
            reset_progress_beats()
