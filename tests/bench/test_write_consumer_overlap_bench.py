"""A/B throughput bench + byte-parity oracle for the sharded write-consumer (Lever 1).

DEC-FETCH-WRITE-OVERLAP-LEVER1-001 — the HARD, PROVE-FIRST gate the operator demanded.

## VERDICT: NO-GO (the gate was REFUTED, the feature is NOT enabled at the live site)

The deep-partition bench (#910) PROVED the lake write is O(month-depth) per save
(12 ms → 213 ms over 500 → 15,000 rows) and runs serially on the fetch worker under the
per-partition lock. The hypothesis was that handing the write OFF to a background shard
worker would let the fetch lane stop idling on it. This bench DISPROVED that hypothesis
on the Track-1 hardware envelope (Golden Rule #10 — an honest NO-GO is the outcome, NOT a
fabricated win):

* **achieved_rps(W=2) and (W=4) do NOT beat achieved_rps(None)** — across repeats they
  sit within ±0.3 r/s of the synchronous path (pure measurement noise), never a
  reproducible margin (see :func:`test_write_consumer_ab_records_no_go`).
* **WHY (the root cause, measured):** the deep merge write is **~78 ms of GIL-HELD
  Python** (the ``_read_existing_bars`` zip-reconstruction loop over the stored month) +
  ~18 ms of GIL-released polars render. A pure-Python shard worker STILL holds the GIL
  for that 78 ms, so moving the merge to a shard thread does not parallelise it — Python
  threads cannot run bytecode concurrently. The write-bound ceiling
  (``1/0.096 ≈ 10 r/s`` of GIL-serialised write throughput) binds in BOTH arms.
* **AND** the #1 correctness invariant — settle-after-durable (Golden Rule #7) — REQUIRES
  the producer to block on the result box until the write is durable, so a connection
  cannot fetch ahead during its OWN write. The "overlap" of other connections fetching
  during one's wait ALREADY happens in the synchronous path (the connections are separate
  threads); the consumer reproduces it exactly, with no extra gain.

So Lever 1 is **shipped DORMANT**: the :class:`~brutex.data.write_consumer.ShardedWriteConsumer`
+ the ``write_pool`` wiring are correct, fully-tested infrastructure (byte-parity +
settle-after-durable both PROVEN below), but the production live build site does NOT
construct a pool — ``write_pool=None`` keeps the byte-identical synchronous path. The real
unlock (a process-pool / Rust write worker that escapes the GIL) is named explicit FUTURE
work, NOT claimed.

## What this bench still PROVES (the value retained)

* **byte-parity oracle** — the stored Parquet is BIT-IDENTICAL across ``None`` / ``2`` /
  ``4`` (the merge is order-independent; the decouple would change throughput, never the
  bytes). PASSES.
* **settle-after-durable** — the producer's result is the REAL durable ``WriteResult``,
  and the partition is on disk before the box settles. PASSES.
* the measured A/B curve, recorded so the NO-GO is reproducible (the #922-style
  refutation, captured not hidden).

The bench is marked ``slow`` so the default CI run can opt out; the verdict assertions run
on a deterministic, real-write scenario so the refutation is reproducible.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("polars", reason="the write-consumer A/B bench writes a real Parquet lake")

from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink, SinkAppendResult
from brutex.data.write_consumer import (
    ResultBox,
    ShardedWriteConsumer,
    WriteConsumerSink,
    WriteRequest,
)

#: The original merge gate's absolute floor (r/s) — the synchronous-path ceiling from the
#: #910 evidence (~3.2 r/s). The full gate ALSO required beating sync; the refutation below
#: shows the consumer clears this floor (~10 r/s) but does NOT beat sync, so the gate fails
#: on its second clause (a real win over the synchronous path).
_GATE_FLOOR_RPS = 3.20

#: Deep steady-state month depth: ~7,500 stored 1-minute bars (~20 trading sessions),
#: the scenario the gate is defined on.
_DEEP_ROWS = 7_500

#: Simulated per-fetch network latency. The fetch worker sleeps this BEFORE the write,
#: modelling the vendor round-trip; it is identical across all three arms.
_FETCH_LATENCY_S = 0.010

#: How many windows each arm processes (enough to amortise warm-up).
_WINDOWS = 24

#: Concurrent fetch "connections" (mirrors the production pool size 4, DEC-FETCH-WRITE-OVERLAP-001).
_CONNECTIONS = 4


def _bar(ts: datetime, *, price: float = 100.0) -> CandleBar:
    """One 1-minute bar at *ts* (close within [low, high])."""
    p = Decimal(str(price))
    return CandleBar(
        timestamp=ts,
        open=p,
        high=p + Decimal("1.00"),
        low=p - Decimal("1.00"),
        close=p,
        volume=10,
        open_interest=None,
    )


def _seed_deep_partition(writer: ParquetWriter, *, symbol: str) -> datetime:
    """Pre-fill one month partition to ~``_DEEP_ROWS`` so every write is a deep merge.

    Returns the next free timestamp so each window appends a fresh distinct bar (a real
    read-modify-write over the deep stored month, the O(month-depth) cost Lever 1 moves
    off the fetch worker).
    """
    base = datetime(2020, 1, 1, 9, 15, tzinfo=UTC)
    seed = [_bar(base + timedelta(minutes=i)) for i in range(_DEEP_ROWS)]
    writer.write(exchange="NSE", segment="CASH", symbol=symbol, interval="1minute", bars=seed)
    return base + timedelta(minutes=_DEEP_ROWS)


def _new_window_bars(next_ts: datetime, window: int) -> list[CandleBar]:
    """A small distinct bar batch for *window* — appended to the deep stored month."""
    start = next_ts + timedelta(minutes=window)
    return [_bar(start, price=200.0 + window)]


@dataclass
class _ArmResult:
    label: str
    achieved_rps: float
    windows: int


def _run_arm(
    tmp_path: Path,
    *,
    write_pool_workers: int | None,
    tag: str,
) -> _ArmResult:
    """Drive ``_WINDOWS`` fetch→write cycles over ``_CONNECTIONS`` connections; measure rps.

    Each "connection" runs a sequence of windows. A window = sleep(fetch latency) then the
    durable write. With ``write_pool_workers=None`` the write runs INLINE on the connection
    thread (today's synchronous path); with a sharded consumer the write is handed off and
    the connection only blocks on the durable result — so OTHER connections fetch during
    the wait (the overlap). ``achieved_rps`` = total windows / wall time.
    """
    lake = tmp_path / tag
    writer = ParquetWriter(lake_root=lake)
    sink = ParquetBarSink(writer=writer)
    symbol = "NSE-NIFTY"
    next_ts = _seed_deep_partition(writer, symbol=symbol)

    consumer: ShardedWriteConsumer | None = None
    write_sink: ParquetBarSink | WriteConsumerSink = sink
    if write_pool_workers is not None:
        consumer = ShardedWriteConsumer(sink=sink, workers=write_pool_workers, queue_maxsize=64)
        write_sink = WriteConsumerSink(consumer=consumer)

    # Split the windows across connections (round-robin so each connection gets a fair share).
    per_conn: list[list[int]] = [[] for _ in range(_CONNECTIONS)]
    for w in range(_WINDOWS):
        per_conn[w % _CONNECTIONS].append(w)

    def _connection(windows: list[int]) -> None:
        for w in windows:
            time.sleep(_FETCH_LATENCY_S)  # simulated vendor fetch
            write_sink.append(
                exchange="NSE",
                segment="CASH",
                symbol=symbol,
                interval="1minute",
                bars=_new_window_bars(next_ts, w),
            )

    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=_CONNECTIONS) as pool:
        list(pool.map(_connection, per_conn))
    if consumer is not None:
        consumer.drain()
    elapsed = time.perf_counter() - start

    achieved = _WINDOWS / elapsed if elapsed > 0 else float("inf")
    return _ArmResult(label=tag, achieved_rps=achieved, windows=_WINDOWS)


def _median(values: list[float]) -> float:
    """The median of *values* (robust to a single noisy run)."""
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


@pytest.mark.slow
def test_write_consumer_ab_records_no_go(tmp_path: Path) -> None:
    """A/B: achieved_rps(None) vs W=2 vs W=4 — records the NO-GO refutation.

    The HARD gate (Lever 1 would ship ONLY if BOTH held):
        achieved_rps(W>=2) > 3.20/s  AND  achieved_rps(W>=2) > achieved_rps(None).

    The MEASURED reality on the Track-1 envelope: the consumer arms sit within ±0.3 r/s
    of the synchronous path — the GIL-held deep-merge write (~78 ms of Python) binds the
    write-bound ceiling in BOTH arms, so there is NO reproducible margin. This test does
    NOT rubber-stamp a noise "win"; it ASSERTS the refutation (the consumer does not beat
    sync by the meaningful margin the gate demands) so the NO-GO is a reproducible,
    captured result — and prints the curve for the PR body / MIGRATION.md.

    A throughput regression would also be a NO-GO; this test additionally asserts the
    consumer is not WORSE than sync by more than noise (so the dormant code is at least
    harmless if ever enabled). Several repeats are taken and the MEDIAN compared, so one
    noisy run cannot flip the verdict.
    """
    repeats = 3
    none_rps = [
        _run_arm(tmp_path, write_pool_workers=None, tag=f"sync{i}").achieved_rps
        for i in range(repeats)
    ]
    w2_rps = [
        _run_arm(tmp_path, write_pool_workers=2, tag=f"w2_{i}").achieved_rps for i in range(repeats)
    ]
    w4_rps = [
        _run_arm(tmp_path, write_pool_workers=4, tag=f"w4_{i}").achieved_rps for i in range(repeats)
    ]

    none_med, w2_med, w4_med = _median(none_rps), _median(w2_rps), _median(w4_rps)
    best_med = max(w2_med, w4_med)
    curve = (
        f"\nA/B curve (deep {_DEEP_ROWS}-row month, {_CONNECTIONS} connections, "
        f"{_WINDOWS} windows, {_FETCH_LATENCY_S * 1000:.0f}ms fetch, median of {repeats}):\n"
        f"  achieved_rps(None) = {none_med:6.2f} r/s  raw={[round(x, 2) for x in none_rps]}\n"
        f"  achieved_rps(W=2)  = {w2_med:6.2f} r/s  raw={[round(x, 2) for x in w2_rps]}\n"
        f"  achieved_rps(W=4)  = {w4_med:6.2f} r/s  raw={[round(x, 2) for x in w4_rps]}\n"
        f"  VERDICT: NO-GO (best overlapped {best_med:.2f} does not beat sync "
        f"{none_med:.2f} by a meaningful margin)\n"
    )
    print(curve)  # captured by -s / on failure; the measured curve for the PR body

    # The MERGE gate (a real ≥10% win over sync) is NOT met — assert that, so the NO-GO is
    # the recorded, reproducible verdict. A 1.10× threshold filters noise: a true overlap
    # win would clear it comfortably; the GIL-bound reality does not.
    merge_gate_margin = 1.10
    assert best_med < none_med * merge_gate_margin, (
        "UNEXPECTED: the overlapped arm beat sync by >=10% — re-evaluate the NO-GO verdict "
        "before relying on it (the dormant feature may now be worth enabling)" + curve
    )
    # And it is not a REGRESSION either: the dormant code, if ever enabled, is within noise
    # of sync (never materially slower) — so shipping it dormant is harmless.
    assert best_med > none_med * 0.85, (
        "The consumer arm is materially SLOWER than sync — investigate before merging even "
        "the dormant code" + curve
    )
    # Sanity: the consumer DOES clear the original gate's absolute floor (~3.2 r/s) — it is
    # a working overlap, it simply cannot beat sync because the GIL binds the write. (So the
    # NO-GO is "no win over sync", not "broken".)
    assert best_med > _GATE_FLOOR_RPS, (
        f"the consumer did not even clear the {_GATE_FLOOR_RPS} r/s floor — unexpected" + curve
    )


def _bytes_for_arm(tmp_path: Path, *, workers: int | None, tag: str) -> bytes:
    """Write an IDENTICAL bar stream through the given arm; return the stored partition bytes.

    The byte-parity oracle: the consumer (any W) must produce the EXACT same on-disk
    Parquet as the synchronous path — sharding/concurrency changes only WHEN a write runs,
    never WHICH bytes land (the merge unions by timestamp, order-independent).
    """
    lake = tmp_path / tag
    writer = ParquetWriter(lake_root=lake)
    sink = ParquetBarSink(writer=writer)
    symbol = "NSE-NIFTY"
    base = datetime(2020, 1, 1, 9, 15, tzinfo=UTC)

    write_sink: ParquetBarSink | WriteConsumerSink = sink
    consumer: ShardedWriteConsumer | None = None
    if workers is not None:
        consumer = ShardedWriteConsumer(sink=sink, workers=workers, queue_maxsize=64)
        write_sink = WriteConsumerSink(consumer=consumer)

    # A deterministic multi-window stream over ONE month partition. Submit concurrently
    # for the consumer arms so the oracle also exercises the cross-window merge ordering.
    batches = [[_bar(base + timedelta(minutes=w), price=300.0 + w)] for w in range(30)]

    def _one(bars: Sequence[CandleBar]) -> SinkAppendResult:
        return write_sink.append(
            exchange="NSE",
            segment="CASH",
            symbol=symbol,
            interval="1minute",
            bars=list(bars),
        )

    if consumer is None:
        for batch in batches:
            _one(batch)
    else:
        with ThreadPoolExecutor(max_workers=_CONNECTIONS) as pool:
            list(pool.map(_one, batches))
        consumer.drain()

    partition = lake / "bars" / "NSE" / "CASH" / "NSE-NIFTY" / "1minute" / "2020" / "01.parquet"
    return partition.read_bytes()


def test_byte_parity_across_w_none_2_4(tmp_path: Path) -> None:
    """The stored Parquet is BIT-IDENTICAL across write_pool=None / W=2 / W=4.

    Proves the decouple changes throughput, NEVER the bytes — the merge is
    order-independent (keep-first union by timestamp), so any shard/completion order
    yields the same partition (Golden Rule #7).
    """
    sync_bytes = _bytes_for_arm(tmp_path, workers=None, tag="parity_none")
    w2_bytes = _bytes_for_arm(tmp_path, workers=2, tag="parity_w2")
    w4_bytes = _bytes_for_arm(tmp_path, workers=4, tag="parity_w4")
    assert w2_bytes == sync_bytes, "W=2 partition bytes diverged from the synchronous path"
    assert w4_bytes == sync_bytes, "W=4 partition bytes diverged from the synchronous path"


def test_settle_after_durable_in_real_write(tmp_path: Path) -> None:
    """End-to-end: the result the producer sees is the REAL durable WriteResult.

    A regression guard for the settle-after-durable invariant against a REAL write — the
    ``bars_appended`` the producer receives is the underlying sink's own count (the #919
    back-channel stays real), and the partition exists on disk before the box settles.
    """
    lake = tmp_path / "settle"
    writer = ParquetWriter(lake_root=lake)
    sink = ParquetBarSink(writer=writer)
    consumer = ShardedWriteConsumer(sink=sink, workers=2)
    request = WriteRequest(
        exchange="NSE",
        segment="CASH",
        symbol="NSE-NIFTY",
        interval="1minute",
        bars=[_bar(datetime(2020, 1, 1, 9, 15, tzinfo=UTC))],
        merge_mode="keep-first",
        box=ResultBox(),
    )
    try:
        result = consumer.submit(request)
        # The producer's result is the durable write's own count, and the file is on disk.
        assert result.bars_appended == 1
        partition = lake / "bars" / "NSE" / "CASH" / "NSE-NIFTY" / "1minute" / "2020" / "01.parquet"
        assert partition.exists()
    finally:
        consumer.drain()


def test_no_unjoined_threads_after_drain(tmp_path: Path) -> None:
    """After drain() every shard thread has exited — no leaked workers across a pull."""
    before = {t.name for t in threading.enumerate()}
    lake = tmp_path / "threads"
    sink = ParquetBarSink(writer=ParquetWriter(lake_root=lake))
    consumer = ShardedWriteConsumer(sink=sink, workers=4)
    consumer.submit(
        WriteRequest(
            exchange="NSE",
            segment="CASH",
            symbol="NSE-NIFTY",
            interval="1minute",
            bars=[_bar(datetime(2020, 1, 1, 9, 15, tzinfo=UTC))],
            merge_mode="keep-first",
            box=ResultBox(),
        )
    )
    consumer.drain()
    # Give the daemon threads a beat to fully unwind, then assert none linger.
    time.sleep(0.05)
    after = {t.name for t in threading.enumerate()}
    leaked = {n for n in after - before if n.startswith("write-consumer-")}
    assert leaked == set(), f"leaked write-consumer threads after drain: {leaked}"
