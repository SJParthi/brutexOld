"""Capture completeness under the DEFAULT-ON async fetch pipeline (P-20).

DEC-AUDIT-LAKE-REPLAY-ORACLE-001 round-1 review fix (BLOCKER): in async
pipeline mode a window's durable ``sink.append`` — the point the capture sink
buffers its ``LAKE_APPEND_WRITTEN`` / L3 ``add`` records — runs on a
background pipeline CONSUMER thread, while the run's own three drain seams
run on the controller thread INSIDE :func:`run_groww_spot_ingest`. The
pipeline is only drained by the pull owner AFTERWARDS (``cmd_fetch``'s
``finally``), so records buffered late were permanently STRANDED: the
forensic log under-recorded durable, ledger-settled writes (a false replay
divergence that could never self-heal). These tests drive the REAL
:class:`~brutex.adapters.groww.fetch_pipeline.FetchPipeline` around a
capture-wrapped real sink in the exact production ordering and prove the
FOURTH seam (:func:`~brutex.adapters.groww.run_ingest.flush_capture_to_audit`,
called after the pipeline drain while the chain is still open) leaves ZERO
stranded records and a byte-complete strict replay.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from brutex.adapters.groww.fetch_pipeline import FetchPipeline
from brutex.adapters.groww.pull_pipeline import drain_fetch_pipeline
from brutex.adapters.groww.run_ingest import (
    flush_capture_to_audit,
    run_groww_spot_ingest,
)
from brutex.adapters.synthetic import SyntheticMarketAdapter, build_synthetic_index
from brutex.data.audit_capture import (
    LAKE_APPEND_ACTION,
    AuditCaptureSink,
    CaptureMode,
)
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink, SinkAppendResult
from brutex.models import Instrument
from brutex.orchestrator.audit import AuditWriter, read_entries, verify_chain
from brutex.orchestrator.lake_replay import (
    assert_lake_byte_equal,
    check_capture_anchors,
    replay_lake_from_audit,
)
from brutex.orchestrator.parallel import SerialRunParallel

_START = date(2021, 1, 4)  # a Monday NSE session
_END = date(2021, 1, 5)


class GateSink:
    """A :class:`BarSink` wrapper that blocks ``append`` until a gate opens.

    Models the pipeline-consumer backlog deterministically: the window tail's
    durable ``sink.append`` (routed through the capture wrapper) runs ONLY
    after :func:`run_groww_spot_ingest` has already returned — i.e. after the
    run's own final ``drain_remaining()`` fired. Exactly the production
    hazard shape (a slow disk / full consumer queue at run end).
    """

    def __init__(self, inner: AuditCaptureSink, gate: threading.Event) -> None:
        self._inner = inner
        self._gate = gate
        self.appends: list[str] = []

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
        assert self._gate.wait(timeout=30.0), "gate never opened — test wiring broken"
        self.appends.append(f"{symbol}:{interval}")
        return self._inner.append(
            exchange=exchange,
            segment=segment,
            symbol=symbol,
            interval=interval,
            bars=bars,
            merge_mode=merge_mode,
        )


def _nifty_instruments() -> list[Instrument]:
    return [i for i in build_synthetic_index().universe if i.symbol_name == "NIFTY"]


class TestPipelineDeferredCaptureFlush:
    """The stranded-records BLOCKER regression + the healthy async shape."""

    def test_records_deferred_past_the_run_drain_flush_at_the_fourth_seam(
        self, tmp_path: Path
    ) -> None:
        """A tail executed AFTER run_ingest's final drain is NOT stranded.

        Mirrors the production CLI ordering exactly: (1) the run returns
        while the pipeline still holds the queued tail (the gate), (2)
        ``drain_fetch_pipeline`` executes the tail — the durable lake write +
        the capture buffering, (3) the FOURTH seam flushes the records onto
        the STILL-OPEN chain. Without step (3) the chain provably carries
        ZERO capture events for a durable write (asserted mid-test — the
        exact defect shape), replay would false-diverge, and the settled
        window would never be re-captured.
        """
        lake_root = tmp_path / "lake"
        lake_root.mkdir()
        audit_path = tmp_path / "audit" / "spot-ingest.jsonl"
        audit_path.parent.mkdir(parents=True)
        capture = AuditCaptureSink(
            ParquetBarSink(writer=ParquetWriter(lake_root=lake_root)),
            lake_root=lake_root,
            mode=CaptureMode.CAPTURE,
        )
        gate = threading.Event()
        spine_sink = GateSink(capture, gate)
        pipeline = FetchPipeline(workers=1, queue_maxsize=8)
        with AuditWriter.open(audit_path) as audit:
            report = run_groww_spot_ingest(
                SyntheticMarketAdapter(),
                spine_sink,
                instruments=_nifty_instruments(),
                intervals=["1minute"],
                start=_START,
                end=_END,
                audit=audit,
                capture=capture,
                run_parallel=SerialRunParallel(),
                fetch_pipeline=pipeline,
            )
            assert report.failed_pairs == 0
            # The run returned with the tail still gated: its three in-run
            # drains flushed NOTHING (the pre-fix defect shape — a durable
            # write with zero capture events would follow).
            pre_flush = [e for e in read_entries(audit_path) if e.action == LAKE_APPEND_ACTION]
            assert pre_flush == []
            # The CLI finally's ordering: pipeline drain (executes the tail →
            # durable write + capture buffering), THEN the fourth seam.
            gate.set()
            drain_fetch_pipeline(pipeline)
            assert spine_sink.appends == ["NIFTY:1minute"]
            flushed = flush_capture_to_audit(capture, audit)
            assert flushed >= 1  # the deferred records WERE stranded until here
        # Nothing remains buffered — capture memory did not outlive the pull.
        assert capture.drain_remaining() == ()
        assert verify_chain(audit_path) > 0
        events = [e for e in read_entries(audit_path) if e.action == LAKE_APPEND_ACTION]
        assert len(events) == flushed
        # The forensic modes the records exist for now hold, strict included.
        replay_root = tmp_path / "replay"
        replay_report = replay_lake_from_audit(audit_path, replay_root=replay_root)
        assert replay_report.appends_replayed == len(events)
        assert_lake_byte_equal(
            original_root=lake_root, replay_root=replay_root, report=replay_report
        )
        anchor_report = check_capture_anchors(audit_path, lake_root=lake_root)
        assert anchor_report.anchor_mismatches == ()

    def test_ungated_pipeline_pull_is_byte_complete_after_cli_order_drains(
        self, tmp_path: Path
    ) -> None:
        """The healthy async shape: a real pipeline pull + the CLI drain
        ordering (pipeline → fourth seam) leaves zero stranded records and a
        byte-complete strict replay, wherever the tail happened to land."""
        lake_root = tmp_path / "lake"
        lake_root.mkdir()
        audit_path = tmp_path / "audit" / "spot-ingest.jsonl"
        audit_path.parent.mkdir(parents=True)
        capture = AuditCaptureSink(
            ParquetBarSink(writer=ParquetWriter(lake_root=lake_root)),
            lake_root=lake_root,
            mode=CaptureMode.CAPTURE,
        )
        pipeline = FetchPipeline(workers=1, queue_maxsize=8)
        with AuditWriter.open(audit_path) as audit:
            report = run_groww_spot_ingest(
                SyntheticMarketAdapter(),
                capture,
                instruments=_nifty_instruments(),
                intervals=["1minute"],
                start=_START,
                end=_END,
                audit=audit,
                capture=capture,
                run_parallel=SerialRunParallel(),
                fetch_pipeline=pipeline,
            )
            assert report.failed_pairs == 0
            drain_fetch_pipeline(pipeline)
            flush_capture_to_audit(capture, audit)
        assert capture.drain_remaining() == ()
        assert list((lake_root / "bars").rglob("*.parquet")) != []
        events = [e for e in read_entries(audit_path) if e.action == LAKE_APPEND_ACTION]
        assert len(events) >= 1  # every durable write is on the chain
        replay_root = tmp_path / "replay"
        replay_report = replay_lake_from_audit(audit_path, replay_root=replay_root)
        assert_lake_byte_equal(
            original_root=lake_root, replay_root=replay_root, report=replay_report
        )
