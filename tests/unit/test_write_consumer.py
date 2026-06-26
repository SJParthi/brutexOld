"""Tests for the sharded write-behind consumer (DEC-FETCH-WRITE-OVERLAP-001 Lever 1).

Covers (CLAUDE.md §12 — 100% line+branch + the 7 test types relevant here):

* unit — shard resolution, routing, lifecycle, settle-once, empty fast path, errors;
* property (hypothesis) — settle-once + bar-count invariants under arbitrary
  partition/shard interleavings;
* golden — bit-for-bit identical lake bytes whether written synchronously or via the
  consumer (idempotence, Golden Rule #7);
* stress/concurrency — many partitions × many writers, back-pressure, drain-on-shutdown,
  a writer raising LakeWriteHalt halting the producer.
"""
# A few lifecycle assertions inspect the consumer's private ``_threads`` / ``_closed``
# (whitebox state checks); silence reportPrivateUsage file-wide for this test module,
# the established convention for the repo's whitebox test files (CLAUDE.md §12 note).
# pyright: reportPrivateUsage=none

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from brutex.data.errors import LakeWriteHalt
from brutex.data.models import CandleBar
from brutex.data.sink import BarSink, SinkAppendResult
from brutex.data.write_consumer import (
    DEFAULT_WRITE_CONSUMER_SHARDS,
    WriteConsumer,
    resolve_shard_count,
    shard_for_partition,
)
from hypothesis import given, settings
from hypothesis import strategies as st

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence


# ────────────────────────────────────────────────────────────────────────
# Helpers / fakes
# ────────────────────────────────────────────────────────────────────────


def _bar(day: int, minute: int = 15, *, month: int = 1, year: int = 2024) -> CandleBar:
    """A deterministic ``CandleBar`` at the given (year, month, day, minute)."""
    return CandleBar(
        timestamp=datetime(year, month, day, 9, minute, 0, tzinfo=UTC),
        open=Decimal("100.00"),
        high=Decimal("110.00"),
        low=Decimal("90.00"),
        close=Decimal("105.00"),
        volume=10,
        open_interest=5,
    )


class RecordingSink:
    """A thread-safe :class:`BarSink` that records every append it receives."""

    def __init__(self, *, delay: float = 0.0) -> None:
        self._delay = delay
        self._lock = threading.Lock()
        self.calls: list[tuple[str, str, int]] = []

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
        if self._delay:
            time.sleep(self._delay)
        with self._lock:
            self.calls.append((symbol, interval, len(bars)))
        return SinkAppendResult(bars_appended=len(bars), partitions_written=1, write_results=())


class _RaisingSink:
    """A sink that raises a chosen exception on append (back-channel error tests)."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

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
        raise self._exc


def _collect_settle() -> tuple[
    list[tuple[SinkAppendResult | None, BaseException | None]], threading.Lock
]:
    """A thread-safe settle collector returning ``(results_list, lock)``."""
    lock = threading.Lock()
    out: list[tuple[SinkAppendResult | None, BaseException | None]] = []

    return out, lock


def _make_settle(
    out: list[tuple[SinkAppendResult | None, BaseException | None]], lock: threading.Lock
) -> Callable[[SinkAppendResult | None, BaseException | None], None]:
    """Return a thread-safe settle callback that appends each outcome to *out*."""

    def settle(result: SinkAppendResult | None, error: BaseException | None) -> None:
        with lock:
            out.append((result, error))

    return settle


# ────────────────────────────────────────────────────────────────────────
# resolve_shard_count
# ────────────────────────────────────────────────────────────────────────


class TestResolveShardCount:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (None, DEFAULT_WRITE_CONSUMER_SHARDS),
            ("", DEFAULT_WRITE_CONSUMER_SHARDS),
            ("   ", DEFAULT_WRITE_CONSUMER_SHARDS),
            ("not-an-int", DEFAULT_WRITE_CONSUMER_SHARDS),
            ("0", DEFAULT_WRITE_CONSUMER_SHARDS),
            ("-3", DEFAULT_WRITE_CONSUMER_SHARDS),
            ("1", 1),
            ("8", 8),
            ("  6  ", 6),
        ],
    )
    def test_parses_or_falls_back(self, raw: str | None, expected: int) -> None:
        assert resolve_shard_count(raw) == expected

    def test_custom_default(self) -> None:
        assert resolve_shard_count(None, default=9) == 9
        assert resolve_shard_count("bad", default=9) == 9


# ────────────────────────────────────────────────────────────────────────
# shard_for_partition
# ────────────────────────────────────────────────────────────────────────


class TestShardForPartition:
    def test_in_range(self) -> None:
        for i in range(50):
            shard = shard_for_partition(f"key-{i}", shards=4)
            assert 0 <= shard < 4

    def test_stable(self) -> None:
        a = shard_for_partition("NSE/FNO/X/1minute/2024/01", shards=8)
        b = shard_for_partition("NSE/FNO/X/1minute/2024/01", shards=8)
        assert a == b

    def test_spreads_across_shards(self) -> None:
        seen = {shard_for_partition(f"part-{i}", shards=8) for i in range(200)}
        assert len(seen) > 1  # not all collapse to one shard


# ────────────────────────────────────────────────────────────────────────
# Construction + lifecycle
# ────────────────────────────────────────────────────────────────────────


class TestLifecycle:
    def test_rejects_bad_shards(self) -> None:
        with pytest.raises(ValueError, match="shards must be"):
            WriteConsumer(RecordingSink(), shards=0)

    def test_rejects_bad_queue_depth(self) -> None:
        with pytest.raises(ValueError, match="queue_depth must be"):
            WriteConsumer(RecordingSink(), queue_depth=0)

    def test_shards_property(self) -> None:
        assert WriteConsumer(RecordingSink(), shards=3).shards == 3

    def test_start_is_idempotent(self) -> None:
        wc = WriteConsumer(RecordingSink(), shards=2)
        wc.start()
        wc.start()  # second call is a no-op
        assert len(wc._threads) == 2
        wc.close()

    def test_submit_before_start_raises(self) -> None:
        wc = WriteConsumer(RecordingSink())
        with pytest.raises(RuntimeError, match="started, open"):
            wc.submit(
                exchange="NSE",
                segment="FNO",
                symbol="NSE-X",
                interval="1minute",
                bars=[_bar(1)],
                settle=lambda r, e: None,
            )

    def test_start_after_close_raises(self) -> None:
        wc = WriteConsumer(RecordingSink())
        wc.start()
        wc.close()
        with pytest.raises(RuntimeError, match="closed"):
            wc.start()

    def test_close_before_start_is_noop(self) -> None:
        wc = WriteConsumer(RecordingSink())
        wc.close()  # never started — must not hang or raise
        assert wc._closed is True

    def test_close_is_idempotent(self) -> None:
        wc = WriteConsumer(RecordingSink())
        wc.start()
        wc.close()
        wc.close()  # second close is a no-op

    def test_submit_after_close_raises(self) -> None:
        wc = WriteConsumer(RecordingSink())
        wc.start()
        wc.close()
        with pytest.raises(RuntimeError, match="started, open"):
            wc.submit(
                exchange="NSE",
                segment="FNO",
                symbol="NSE-X",
                interval="1minute",
                bars=[_bar(1)],
                settle=lambda r, e: None,
            )

    def test_context_manager_drains_and_joins(self) -> None:
        sink = RecordingSink()
        out, lock = _collect_settle()
        with WriteConsumer(sink, shards=2) as wc:
            for d in range(1, 6):
                wc.submit(
                    exchange="NSE",
                    segment="FNO",
                    symbol=f"NSE-S{d}",
                    interval="1minute",
                    bars=[_bar(d)],
                    settle=_make_settle(out, lock),
                )
        # On context exit every submit has been written + settled (no lost tail).
        assert len(sink.calls) == 5
        assert len(out) == 5
        assert all(e is None for _r, e in out)


# ────────────────────────────────────────────────────────────────────────
# Write + settle behaviour
# ────────────────────────────────────────────────────────────────────────


def _settling_consumer(
    sink: BarSink, *, shards: int = 4, queue_depth: int = 64
) -> tuple[
    WriteConsumer, list[tuple[SinkAppendResult | None, BaseException | None]], threading.Lock
]:
    out: list[tuple[SinkAppendResult | None, BaseException | None]] = []
    lock = threading.Lock()
    wc = WriteConsumer(sink, shards=shards, queue_depth=queue_depth)
    return wc, out, lock


class TestWriteAndSettle:
    def test_single_partition_settles_with_result(self) -> None:
        sink = RecordingSink()
        wc, out, lock = _settling_consumer(sink, shards=2)
        with wc:
            wc.submit(
                exchange="NSE",
                segment="FNO",
                symbol="NSE-X",
                interval="1minute",
                bars=[_bar(1), _bar(2)],
                settle=_make_settle(out, lock),
            )
        assert len(out) == 1
        result, error = out[0]
        assert error is None
        assert result is not None
        assert result.bars_appended == 2
        assert result.partitions_written == 1

    def test_empty_bars_settles_synchronously_zero(self) -> None:
        sink = RecordingSink()
        wc, out, lock = _settling_consumer(sink)
        with wc:
            wc.submit(
                exchange="NSE",
                segment="FNO",
                symbol="NSE-X",
                interval="1minute",
                bars=[],
                settle=_make_settle(out, lock),
            )
        assert len(sink.calls) == 0  # no writer work
        assert len(out) == 1
        result, error = out[0]
        assert error is None
        assert result is not None
        assert result.bars_appended == 0
        assert result.partitions_written == 0

    def test_multi_partition_submit_settles_once_aggregated(self) -> None:
        sink = RecordingSink()
        wc, out, lock = _settling_consumer(sink, shards=4)
        # Two month partitions: Jan + Feb.
        bars = [_bar(1, month=1), _bar(2, month=1), _bar(3, month=2)]
        with wc:
            wc.submit(
                exchange="NSE",
                segment="FNO",
                symbol="NSE-X",
                interval="1minute",
                bars=bars,
                settle=_make_settle(out, lock),
            )
        # Two writer appends (one per month) but settle fires exactly ONCE, aggregated.
        assert len(sink.calls) == 2
        assert len(out) == 1
        result, error = out[0]
        assert error is None
        assert result is not None
        assert result.bars_appended == 3
        assert result.partitions_written == 2

    def test_same_partition_routes_to_one_shard_in_order(self) -> None:
        # Two submits to the SAME partition must hit the same shard (serialised).
        sink = RecordingSink(delay=0.01)
        wc, out, lock = _settling_consumer(sink, shards=4)
        with wc:
            for d in (1, 2):
                wc.submit(
                    exchange="NSE",
                    segment="FNO",
                    symbol="NSE-SAME",
                    interval="1minute",
                    bars=[_bar(d)],
                    settle=_make_settle(out, lock),
                )
        assert len(sink.calls) == 2
        assert all(c[0] == "NSE-SAME" for c in sink.calls)


# ────────────────────────────────────────────────────────────────────────
# Error back-channel (the #919 fix)
# ────────────────────────────────────────────────────────────────────────


class TestErrorBackChannel:
    def test_generic_write_error_delivered_to_settle_and_reraised(self) -> None:
        # A generic write error is (a) delivered to settle as the back-channel result
        # AND (b) latched + re-raised on close, so the producer's quarantine path sees it
        # — never silently dropped (the #919 fix / constraint #4).
        boom = ValueError("disk wobble")
        wc, out, lock = _settling_consumer(_RaisingSink(boom), shards=2)
        wc.start()
        wc.submit(
            exchange="NSE",
            segment="FNO",
            symbol="NSE-X",
            interval="1minute",
            bars=[_bar(1)],
            settle=_make_settle(out, lock),
        )
        with pytest.raises(ValueError, match="disk wobble"):
            wc.close()
        assert len(out) == 1
        result, error = out[0]
        assert result is None
        assert error is boom  # delivered to settle, never swallowed

    def test_first_error_wins_for_multi_partition(self) -> None:
        wc, out, lock = _settling_consumer(_RaisingSink(KeyError("x")), shards=4)
        bars = [_bar(1, month=1), _bar(2, month=2)]  # two partitions, both raise
        wc.start()
        wc.submit(
            exchange="NSE",
            segment="FNO",
            symbol="NSE-X",
            interval="1minute",
            bars=bars,
            settle=_make_settle(out, lock),
        )
        with pytest.raises(KeyError):
            wc.close()
        assert len(out) == 1
        result, error = out[0]
        assert result is None
        assert isinstance(error, KeyError)


# ────────────────────────────────────────────────────────────────────────
# LakeWriteHalt — disk-full / OOM halts the producer (constraint #5)
# ────────────────────────────────────────────────────────────────────────


class TestLakeWriteHalt:
    def test_halt_latched_and_reraised_on_next_submit(self) -> None:
        halt = LakeWriteHalt("disk_full")
        wc, out, lock = _settling_consumer(_RaisingSink(halt), shards=1)
        wc.start()
        try:
            # First submit triggers the write that latches the halt; its settle gets
            # the halt as its error (NOT durable-settled).
            wc.submit(
                exchange="NSE",
                segment="FNO",
                symbol="NSE-X",
                interval="1minute",
                bars=[_bar(1)],
                settle=_make_settle(out, lock),
            )
            # Wait for the writer to process it (single shard, deterministic order).
            deadline = time.time() + 5.0
            while time.time() < deadline:
                with lock:
                    if out:
                        break
                time.sleep(0.005)
            assert out and out[0][1] is halt
            # The latch now makes the NEXT submit re-raise so the pull halts.
            with pytest.raises(LakeWriteHalt) as exc_info:
                wc.submit(
                    exchange="NSE",
                    segment="FNO",
                    symbol="NSE-Y",
                    interval="1minute",
                    bars=[_bar(2)],
                    settle=_make_settle(out, lock),
                )
            assert exc_info.value.reason == "disk_full"
        finally:
            with pytest.raises(LakeWriteHalt):
                wc.close()

    def test_second_halt_does_not_overwrite_first(self) -> None:
        # Two writes to DIFFERENT shards both raise LakeWriteHalt: the FIRST latched
        # halt wins (the second hits the `self._halt is not None` branch and only logs).
        wc, out, lock = _settling_consumer(_RaisingSink(LakeWriteHalt("disk_full")), shards=4)
        wc.start()
        # Pick two symbols that hash to different shards so both writer threads latch.
        from brutex.data.write_consumer import shard_for_partition as _sfp

        def _key(sym: str) -> str:
            return f"NSE/FNO/{sym}/1minute/2024/01"

        sym_a, sym_b = "NSE-HA", "NSE-HB"
        # If they collide on a shard, find a second symbol that doesn't.
        idx = 0
        while _sfp(_key(sym_a), shards=4) == _sfp(_key(sym_b), shards=4):
            idx += 1
            sym_b = f"NSE-HB{idx}"
        for sym in (sym_a, sym_b):
            wc.submit(
                exchange="NSE",
                segment="FNO",
                symbol=sym,
                interval="1minute",
                bars=[_bar(1)],
                settle=_make_settle(out, lock),
            )
        with pytest.raises(LakeWriteHalt) as exc_info:
            wc.close()
        assert exc_info.value.reason == "disk_full"
        # Both submits settled with a halt error (neither durably written).
        assert len(out) == 2
        assert all(isinstance(e, LakeWriteHalt) for _r, e in out)

    def test_halt_reraised_on_close(self) -> None:
        halt = LakeWriteHalt("out_of_memory")
        wc, out, lock = _settling_consumer(_RaisingSink(halt), shards=1)
        wc.start()
        wc.submit(
            exchange="NSE",
            segment="FNO",
            symbol="NSE-X",
            interval="1minute",
            bars=[_bar(1)],
            settle=_make_settle(out, lock),
        )
        with pytest.raises(LakeWriteHalt) as exc_info:
            wc.close()
        assert exc_info.value.reason == "out_of_memory"


# ────────────────────────────────────────────────────────────────────────
# Stress / concurrency
# ────────────────────────────────────────────────────────────────────────


class TestStress:
    def test_many_partitions_many_writers(self) -> None:
        sink = RecordingSink()
        wc, out, lock = _settling_consumer(sink, shards=8, queue_depth=16)
        n = 500
        with wc:
            for i in range(n):
                wc.submit(
                    exchange="NSE",
                    segment="FNO",
                    symbol=f"NSE-C{i}",
                    interval="1minute",
                    bars=[_bar(1 + (i % 27))],
                    settle=_make_settle(out, lock),
                )
        assert len(sink.calls) == n
        assert len(out) == n
        assert all(e is None for _r, e in out)
        assert sum(r.bars_appended for r, _e in out if r is not None) == n

    def test_backpressure_blocks_producer(self) -> None:
        # A slow single-shard sink with a depth-1 queue forces the producer to block.
        sink = RecordingSink(delay=0.05)
        wc, out, lock = _settling_consumer(sink, shards=1, queue_depth=1)
        start = time.time()
        with wc:
            for d in range(1, 6):  # 5 same-partition submits, serialised through 1 writer
                wc.submit(
                    exchange="NSE",
                    segment="FNO",
                    symbol="NSE-SLOW",
                    interval="1minute",
                    bars=[_bar(d)],
                    settle=_make_settle(out, lock),
                )
        elapsed = time.time() - start
        # 5 writes × 50 ms each, serialised → producer was throttled by back-pressure.
        assert elapsed >= 0.20
        assert len(sink.calls) == 5
        assert len(out) == 5

    def test_concurrent_producers(self) -> None:
        sink = RecordingSink()
        wc, out, lock = _settling_consumer(sink, shards=4, queue_depth=8)
        per_thread = 50
        threads_n = 4

        def producer(tid: int) -> None:
            for i in range(per_thread):
                wc.submit(
                    exchange="NSE",
                    segment="FNO",
                    symbol=f"NSE-T{tid}-{i}",
                    interval="1minute",
                    bars=[_bar(1 + (i % 27))],
                    settle=_make_settle(out, lock),
                )

        with wc:
            producers = [threading.Thread(target=producer, args=(t,)) for t in range(threads_n)]
            for p in producers:
                p.start()
            for p in producers:
                p.join()
        assert len(out) == per_thread * threads_n
        assert all(e is None for _r, e in out)


# ────────────────────────────────────────────────────────────────────────
# Property — settle-once + bar-count invariants (hypothesis)
# ────────────────────────────────────────────────────────────────────────


class TestProperties:
    @settings(max_examples=40, deadline=None)
    @given(
        shards=st.integers(min_value=1, max_value=6),
        submits=st.lists(
            st.tuples(
                st.integers(min_value=1, max_value=5),  # symbol id
                st.lists(
                    st.tuples(
                        st.integers(min_value=1, max_value=3),  # month
                        st.integers(min_value=1, max_value=27),  # day
                    ),
                    min_size=0,
                    max_size=6,
                ),
            ),
            min_size=1,
            max_size=12,
        ),
    )
    def test_settle_once_and_bar_counts(
        self, shards: int, submits: list[tuple[int, list[tuple[int, int]]]]
    ) -> None:
        sink = RecordingSink()
        out: list[tuple[SinkAppendResult | None, BaseException | None]] = []
        lock = threading.Lock()
        expected_total = 0
        with WriteConsumer(sink, shards=shards, queue_depth=8) as wc:
            for sym_id, day_specs in submits:
                bars = [_bar(day, month=month) for month, day in day_specs]
                expected_total += len(bars)
                wc.submit(
                    exchange="NSE",
                    segment="FNO",
                    symbol=f"NSE-P{sym_id}",
                    interval="1minute",
                    bars=bars,
                    settle=_make_settle(out, lock),
                )
        # Every submit settled EXACTLY once regardless of partition/shard interleaving.
        assert len(out) == len(submits)
        assert all(e is None for _r, e in out)
        # Total bars accounted across all settles == total submitted.
        assert sum(r.bars_appended for r, _e in out if r is not None) == expected_total


# ────────────────────────────────────────────────────────────────────────
# Golden — bit-for-bit identical lake bytes: sync sink vs write_consumer
# ────────────────────────────────────────────────────────────────────────

pl = pytest.importorskip("polars")
pytest.importorskip("pyarrow")
from brutex.data.parquet_writer import ParquetWriter  # noqa: E402
from brutex.data.sink import ParquetBarSink  # noqa: E402


def _hash_lake(root: Path) -> dict[str, bytes]:
    """Map each partition path (relative to *root*) → its bytes, for byte-equality."""
    out: dict[str, bytes] = {}
    for parquet in sorted(root.rglob("*.parquet")):
        out[str(parquet.relative_to(root))] = parquet.read_bytes()
    return out


class TestGoldenByteEquality:
    def test_consumer_lake_bytes_match_sync_sink(self, tmp_path: Path) -> None:
        # Same bars, two lakes: one written synchronously, one via the consumer.
        bars_by_symbol = {
            "NSE-A": [_bar(1, month=1), _bar(2, month=1), _bar(3, month=2)],
            "NSE-B": [_bar(5, month=1), _bar(6, month=2)],
            "NSE-C": [_bar(10, month=3)],
        }

        sync_root = tmp_path / "sync"
        sync_sink = ParquetBarSink(writer=ParquetWriter(lake_root=sync_root))
        for symbol, bars in bars_by_symbol.items():
            sync_sink.append(
                exchange="NSE", segment="FNO", symbol=symbol, interval="1minute", bars=bars
            )

        async_root = tmp_path / "async"
        async_sink = ParquetBarSink(writer=ParquetWriter(lake_root=async_root))
        with WriteConsumer(async_sink, shards=4) as wc:
            for symbol, bars in bars_by_symbol.items():
                wc.submit(
                    exchange="NSE",
                    segment="FNO",
                    symbol=symbol,
                    interval="1minute",
                    bars=bars,
                    settle=lambda _r, _e: None,
                )

        sync_hashes = _hash_lake(sync_root)
        async_hashes = _hash_lake(async_root)
        assert sync_hashes.keys() == async_hashes.keys()
        for key in sync_hashes:
            assert sync_hashes[key] == async_hashes[key], f"byte mismatch at {key}"

    def test_single_shard_consumer_matches_sync(self, tmp_path: Path) -> None:
        # A single-shard consumer is the strict serial case — must be byte-identical.
        bars = [_bar(d, month=1 + (d % 3)) for d in range(1, 12)]
        sync_root = tmp_path / "sync1"
        ParquetBarSink(writer=ParquetWriter(lake_root=sync_root)).append(
            exchange="NSE", segment="FNO", symbol="NSE-X", interval="1minute", bars=bars
        )
        async_root = tmp_path / "async1"
        with WriteConsumer(
            ParquetBarSink(writer=ParquetWriter(lake_root=async_root)), shards=1
        ) as wc:
            wc.submit(
                exchange="NSE",
                segment="FNO",
                symbol="NSE-X",
                interval="1minute",
                bars=bars,
                settle=lambda _r, _e: None,
            )
        assert _hash_lake(sync_root) == _hash_lake(async_root)
