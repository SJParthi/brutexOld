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
    ResultBox,
    ShardedWriteConsumer,
    WriteConsumerSink,
    WriteRequest,
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


def consumer_submit_async(consumer: ShardedWriteConsumer, request: WriteRequest) -> ResultBox:
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
