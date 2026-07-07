"""Bench — the replay-capture sink's measured overhead (§1.4 estimates → numbers).

DEC-AUDIT-LAKE-REPLAY-ORACLE-001. Coverage-excluded, NON-GATING by design:
this bench turns the design's §1.4 fsync/lock-serialization ESTIMATES into
measured numbers on whatever box runs it. It records and prints:

1. **anchor-mode append overhead** — wrapped vs bare ``sink.append`` wall
   time over the same batch schedule (the capture lock + anchor projection
   + ``input_blake3`` cost, dominated by the Parquet render);
2. **cross-partition lock serialization** — two threads appending DISJOINT
   series through ONE capture sink (the wired-mode cost the design states
   honestly: while wired, ALL lake appends serialize through one lock);
3. **drain fsync rate** — records/second through a real hash-chained
   :class:`AuditWriter` (one fsync per entry).

No wall-clock RATIO assertion runs on shared CI — the A/B comparison class
is exactly what DEC-P0-020 declares un-runnable on shared GHA runners; the
absolute-throughput assertions are gated behind ``BRUTEX_M4PRO_ABS=1`` (the
house convention for every sibling A/B wall-clock bench). Correctness
(delegation transparency, buffered-event counts) is asserted unconditionally.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Final

import pytest
from brutex.data.audit_capture import AuditCaptureSink, CaptureMode
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink
from brutex.orchestrator.audit import AuditWriter

_ENV_M4PRO_ABS: Final[str] = "BRUTEX_M4PRO_ABS"

#: Bounded schedule — enough appends to average out noise, small enough to
#: keep the bench job fast.
_APPENDS: Final[int] = 12
_BARS_PER_APPEND: Final[int] = 60


def _m4pro_abs_enabled() -> bool:
    """Return True iff ``BRUTEX_M4PRO_ABS=1`` is set (the house bench gate)."""
    return os.environ.get(_ENV_M4PRO_ABS, "").strip() == "1"


def _bars(*, day: int, start_minute: int, count: int, base: str) -> list[CandleBar]:
    """*count* consecutive whole-second 1-minute bars starting at *start_minute*."""
    price = Decimal(base)
    t0 = datetime(2021, 1, day, 4, 0, tzinfo=UTC) + timedelta(minutes=start_minute)
    return [
        CandleBar(
            timestamp=t0 + timedelta(minutes=i),
            open=price,
            high=price,
            low=price,
            close=price,
            volume=100 + i,
            open_interest=None,
        )
        for i in range(count)
    ]


def _warm_up(sink: ParquetBarSink | AuditCaptureSink) -> None:
    """TWO untimed throwaway appends through *sink* before its timed arm starts.

    Round-2 review fix: the bare arm previously ran FIRST in a fresh bench
    process, so the one-time initialization cost of the process's first
    Parquet writes landed entirely inside ``bare_s`` — systematically
    understating (even negating) the printed wrapped-vs-bare per-append delta
    and flattering the ``BRUTEX_M4PRO_ABS`` assertion in the passing
    direction. Each arm now pays untimed warm-up appends through its OWN sink
    covering BOTH one-time paths (measured on this box: ~390 ms first FRESH
    render — polars/pyarrow/zstd init — and ~250 ms first MERGE write — the
    read-modify-write's first Parquet READ), on a distinct WARMUP partition
    that never collides with the timed schedule, so the timed numbers measure
    steady-state appends only. A warmed capture sink's buffered warm-up
    records are drained immediately so the timed-arm record-count assertions
    stay exact.
    """
    for start_minute in (0, 1):  # 2nd append = the MERGE (read-modify-write) path
        sink.append(
            exchange="NSE",
            segment="INDEX",
            symbol="WARMUP",
            interval="1minute",
            bars=_bars(day=1, start_minute=start_minute, count=1, base="99.00"),
        )
    if isinstance(sink, AuditCaptureSink):
        sink.drain_pair("WARMUP", "1minute")


def _run_schedule(sink: ParquetBarSink | AuditCaptureSink, *, symbol: str) -> float:
    """Append the fixed schedule through *sink*; return the wall seconds."""
    started = time.perf_counter()
    for i in range(_APPENDS):
        sink.append(
            exchange="NSE",
            segment="INDEX",
            symbol=symbol,
            interval="1minute",
            bars=_bars(
                day=4 + (i % 5),
                start_minute=i * _BARS_PER_APPEND,
                count=_BARS_PER_APPEND,
                base="150.25",
            ),
        )
    return time.perf_counter() - started


def test_anchor_mode_append_overhead(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Measure wrapped-vs-bare append wall time (anchor mode, no journal)."""
    bare_sink = ParquetBarSink(writer=ParquetWriter(lake_root=tmp_path / "bare"))
    _warm_up(bare_sink)
    bare_s = _run_schedule(bare_sink, symbol="NIFTY")

    wrapped_root = tmp_path / "wrapped"
    capture = AuditCaptureSink(
        ParquetBarSink(writer=ParquetWriter(lake_root=wrapped_root)),
        lake_root=wrapped_root,
        mode=CaptureMode.ANCHOR,
    )
    _warm_up(capture)
    wrapped_s = _run_schedule(capture, symbol="NIFTY")
    records = capture.drain_remaining()
    assert len(records) == _APPENDS  # one anchor event per non-empty append

    per_append_delta_ms = (wrapped_s - bare_s) / _APPENDS * 1000.0
    with capsys.disabled():
        print(
            f"\n[bench audit_capture] anchor-mode overhead: bare={bare_s * 1000:.1f} ms "
            f"wrapped={wrapped_s * 1000:.1f} ms delta/append={per_append_delta_ms:+.2f} ms "
            f"({_APPENDS} appends x {_BARS_PER_APPEND} bars)"
        )
    if _m4pro_abs_enabled():  # pragma: no cover — M4 Pro absolute gate only
        # The anchor projection must stay dominated by the Parquet render.
        assert wrapped_s <= bare_s * 2.0


def test_cross_partition_lock_serialization(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Measure the wired-mode cross-partition serialization cost (2 threads)."""
    import threading

    def _timed_pair(sink_factory: str) -> float:
        root = tmp_path / sink_factory
        base = ParquetBarSink(writer=ParquetWriter(lake_root=root))
        sink: ParquetBarSink | AuditCaptureSink
        if sink_factory == "wrapped":
            sink = AuditCaptureSink(base, lake_root=root, mode=CaptureMode.ANCHOR)
        else:
            sink = base
        _warm_up(sink)
        threads = [
            threading.Thread(target=_run_schedule, args=(sink,), kwargs={"symbol": symbol})
            for symbol in ("NIFTY", "BANKNIFTY")
        ]
        started = time.perf_counter()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return time.perf_counter() - started

    bare_s = _timed_pair("bare")
    wrapped_s = _timed_pair("wrapped")
    with capsys.disabled():
        print(
            f"\n[bench audit_capture] cross-partition 2-thread schedule: "
            f"bare={bare_s * 1000:.1f} ms wrapped(serialized)={wrapped_s * 1000:.1f} ms "
            f"ratio={wrapped_s / bare_s:.2f}x"
        )
    if _m4pro_abs_enabled():  # pragma: no cover — M4 Pro absolute gate only
        # Full serialization bounds the wired cost at ~2x the overlapped run.
        assert wrapped_s <= bare_s * 3.0


def test_drain_fsync_rate(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Measure the controller-drain append rate through a real hash chain."""
    root = tmp_path / "lake"
    capture = AuditCaptureSink(
        ParquetBarSink(writer=ParquetWriter(lake_root=root)),
        lake_root=root,
        mode=CaptureMode.ANCHOR,
    )
    _run_schedule(capture, symbol="NIFTY")
    records = capture.drain_remaining()
    assert len(records) == _APPENDS
    audit_path = tmp_path / "audit" / "spot-ingest.jsonl"
    audit_path.parent.mkdir(parents=True)
    started = time.perf_counter()
    with AuditWriter.open(audit_path) as audit:
        for record in records:
            audit.append(action=record.action, subject=record.subject, payload=record.payload)
    elapsed = time.perf_counter() - started
    rate = len(records) / elapsed if elapsed > 0 else float("inf")
    with capsys.disabled():
        print(
            f"\n[bench audit_capture] drain fsync rate: {len(records)} records in "
            f"{elapsed * 1000:.1f} ms -> {rate:.0f} records/s (one fsync per entry)"
        )
