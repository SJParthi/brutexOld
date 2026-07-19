"""Capture-order concurrency + wrapping-order integration (P-20 hazards).

DEC-AUDIT-LAKE-REPLAY-ORACLE-001. Three structural hazards the design's §4
demands regression pins for:

1. **The parallel-window same-partition conflict race (the judge-demanded
   pin, ground truth G4):** a SINGLE-pair pull hands its windows the full
   outer pool, so two parallel windows of one pair CAN append conflicting
   same-timestamp batches to ONE month partition concurrently. The capture
   sink's ONE lock spans the durable inner append AND the ``capture_seq``
   stamp, so stamp order IS lake-apply order — every interleaving's replay
   must be byte-equal with matching per-event ``raw_hash`` anchors. Two
   barrier-started threads race conflicting batches for ~20 bounded
   iterations to sample both keep-first interleavings.

2. **Wrapping order (G3):** the capture wrapper must sit INSIDE the write
   pool (the pool owns the base sink), so shard-WORKER-thread appends route
   through it. Proven against the real :class:`ShardedWriteConsumer` +
   :class:`WriteConsumerSink` and against the production
   :func:`build_write_consumer` (``BRUTEX_WRITE_BATCH`` coalescing) factory.

3. **F&O capture mechanics (§2.3):** a ``segment="FNO"`` batch (real
   ``open_interest`` ints) captures → replays byte-equal — the F&O spine's
   production chain wiring is follow-up ``-002``, but the sink is
   spine-agnostic and the mechanics are proven here.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.data.audit_capture import (
    LAKE_APPEND_ACTION,
    AuditCaptureSink,
    CaptureMode,
)
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink
from brutex.data.write_consumer import (
    ShardedWriteConsumer,
    WriteConsumerSink,
)
from brutex.orchestrator.audit import AuditWriter, read_entries
from brutex.orchestrator.lake_replay import (
    assert_lake_byte_equal,
    check_capture_anchors,
    replay_lake_from_audit,
)

#: Bounded race sampling — enough iterations to see both keep-first
#: interleavings with overwhelming probability, small enough to stay fast.
_RACE_ITERATIONS = 20


def _bar(minute: int, price: str, *, oi: int | None = None) -> CandleBar:
    """One whole-second UTC 1-minute bar with a flat OHLC at *price*."""
    value = Decimal(price)
    return CandleBar(
        timestamp=datetime(2021, 1, 4, 4, minute, tzinfo=UTC),
        open=value,
        high=value,
        low=value,
        close=value,
        volume=100 + minute,
        open_interest=oi,
    )


def _capture_sink(lake_root: Path) -> AuditCaptureSink:
    """A CAPTURE-mode sink over a REAL Parquet writer stack."""
    return AuditCaptureSink(
        ParquetBarSink(writer=ParquetWriter(lake_root=lake_root)),
        lake_root=lake_root,
        mode=CaptureMode.CAPTURE,
    )


def _drain_to_chain(capture: AuditCaptureSink, audit_path: Path) -> None:
    """Drain every buffered record onto a REAL hash-chained AuditWriter."""
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with AuditWriter.open(audit_path, resume=audit_path.exists()) as audit:
        for record in capture.drain_remaining():
            audit.append(action=record.action, subject=record.subject, payload=record.payload)


def _replay_and_assert_byte_equal(audit_path: Path, original: Path, replayed: Path) -> None:
    """Replay the chain into a fresh root; anchors + full bytes must match."""
    report = replay_lake_from_audit(audit_path, replay_root=replayed)
    assert report.anchor_mismatches == ()
    assert_lake_byte_equal(original_root=original, replay_root=replayed, report=report)
    anchor_report = check_capture_anchors(audit_path, lake_root=original)
    assert anchor_report.anchor_mismatches == ()


class TestParallelSamePartitionConflictRace:
    """G4: racing conflicting same-timestamp batches never breaks replay."""

    def test_every_interleaving_replays_byte_equal(self, tmp_path: Path) -> None:
        """Two barrier-started threads race a keep-first conflict into ONE
        partition through one shared capture sink; EVERY iteration's replay
        is byte-equal and every per-event anchor matches (stamp order IS
        lake-apply order — the one-lock contract, closed structurally)."""
        for iteration in range(_RACE_ITERATIONS):
            root = tmp_path / f"iter-{iteration:02d}"
            original = root / "lake-a"
            replayed = root / "lake-b"
            audit_path = root / "audit" / "spot-ingest.jsonl"
            capture = _capture_sink(original)
            barrier = threading.Barrier(2)
            errors: list[BaseException] = []

            def _race(
                price: str,
                *,
                cap: AuditCaptureSink,
                gate: threading.Barrier,
                sink_errors: list[BaseException],
            ) -> None:
                # SAME timestamps, DIFFERENT values — a genuine keep-first
                # conflict whose stored winner depends on apply order.
                bars = [_bar(0, price), _bar(1, price)]
                gate.wait()
                try:
                    cap.append(
                        exchange="NSE",
                        segment="INDEX",
                        symbol="NIFTY",
                        interval="1minute",
                        bars=bars,
                    )
                except Exception as exc:  # recorded, re-asserted on the main thread
                    sink_errors.append(exc)  # pragma: no cover — fail loud below

            threads = [
                threading.Thread(
                    target=_race,
                    args=(price,),
                    kwargs={"cap": capture, "gate": barrier, "sink_errors": errors},
                )
                for price in ("101.00", "202.00")
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            assert errors == []
            _drain_to_chain(capture, audit_path)
            events = [e for e in read_entries(audit_path) if e.action == LAKE_APPEND_ACTION]
            assert len(events) == 2
            # The two events carry distinct, gap-free capture_seq stamps.
            assert sorted(int(str(e.payload["capture_seq"])) for e in events) == [0, 1]
            _replay_and_assert_byte_equal(audit_path, original, replayed)


class TestWrapperInsideWritePool:
    """G3: the capture wrapper sits INSIDE the pool — shard appends captured."""

    def test_sharded_consumer_worker_appends_route_through_capture(self, tmp_path: Path) -> None:
        """Appends submitted through WriteConsumerSink run on the pool's
        SHARD WORKER threads — and still land in the capture buffer, because
        the pool was built OVER the capture-wrapped base sink."""
        original = tmp_path / "lake-a"
        replayed = tmp_path / "lake-b"
        audit_path = tmp_path / "audit" / "spot-ingest.jsonl"
        capture = _capture_sink(original)
        pool = ShardedWriteConsumer(sink=capture, workers=2)
        try:
            pooled_sink = WriteConsumerSink(consumer=pool)
            pooled_sink.append(
                exchange="NSE",
                segment="INDEX",
                symbol="NIFTY",
                interval="1minute",
                bars=[_bar(0, "150.25"), _bar(1, "151.10")],
            )
            pooled_sink.append(
                exchange="NSE",
                segment="INDEX",
                symbol="BANKNIFTY",
                interval="1minute",
                bars=[_bar(0, "350.40")],
            )
        finally:
            pool.drain()
        _drain_to_chain(capture, audit_path)
        events = [e for e in read_entries(audit_path) if e.action == LAKE_APPEND_ACTION]
        assert {str(e.subject) for e in events} == {
            "NSE:INDEX:NIFTY:1minute",
            "NSE:INDEX:BANKNIFTY:1minute",
        }
        _replay_and_assert_byte_equal(audit_path, original, replayed)

    def test_build_write_consumer_over_capture_sink_is_captured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The PRODUCTION pool factory (``BRUTEX_WRITE_BATCH`` coalescing
        consumer) built over the capture-wrapped sink — the exact CLI
        wrapping order — still routes every durable append through capture."""
        from brutex.adapters.groww.pull_pipeline import (
            build_write_consumer,
            drain_write_consumer,
        )

        monkeypatch.setenv("BRUTEX_WRITE_BATCH", "1")
        original = tmp_path / "lake-a"
        replayed = tmp_path / "lake-b"
        audit_path = tmp_path / "audit" / "spot-ingest.jsonl"
        capture = _capture_sink(original)
        consumer = build_write_consumer(capture, dry_run=False)
        assert consumer is not None
        try:
            pooled_sink = WriteConsumerSink(consumer=consumer)
            result = pooled_sink.append(
                exchange="NSE",
                segment="INDEX",
                symbol="NIFTY",
                interval="1minute",
                bars=[_bar(0, "150.25")],
            )
            assert result.bars_appended == 1
        finally:
            drain_write_consumer(consumer)
        _drain_to_chain(capture, audit_path)
        events = [e for e in read_entries(audit_path) if e.action == LAKE_APPEND_ACTION]
        assert [str(e.subject) for e in events] == ["NSE:INDEX:NIFTY:1minute"]
        _replay_and_assert_byte_equal(audit_path, original, replayed)


class TestFnoBatchCaptureMechanics:
    """§2.3: F&O-shaped (segment="FNO", real OI) capture → replay mechanics."""

    def test_fno_batch_captures_and_replays_byte_equal(self, tmp_path: Path) -> None:
        original = tmp_path / "lake-a"
        replayed = tmp_path / "lake-b"
        audit_path = tmp_path / "audit" / "fno-ingest.jsonl"
        capture = _capture_sink(original)
        capture.append(
            exchange="NSE",
            segment="FNO",
            symbol="NSE-NIFTY-25Feb21-14000-CE",
            interval="1minute",
            bars=[_bar(0, "210.55", oi=7500), _bar(1, "212.10", oi=7625)],
        )
        _drain_to_chain(capture, audit_path)
        events = [e for e in read_entries(audit_path) if e.action == LAKE_APPEND_ACTION]
        assert [str(e.subject) for e in events] == ["NSE:FNO:NSE-NIFTY-25Feb21-14000-CE:1minute"]
        # The captured rows carry the OI ints verbatim (never nulled).
        bars_payload = events[0].payload["bars"]
        assert isinstance(bars_payload, list)
        assert [row[6] for row in bars_payload] == [7500, 7625]
        _replay_and_assert_byte_equal(audit_path, original, replayed)
