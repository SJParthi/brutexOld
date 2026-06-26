"""At-scale Groww pull harness — REAL controller/pool/ingest/sink, FAKE SDK.

DEC-PULL-SCALE-HARNESS-001 (MIGRATION.md #929). A tests-only benchmark harness that
drives the REAL production pull stack — the adaptive
:class:`~brutex.adapters.rate_common.RateController` (ceiling 8 r/s + 500/min governor),
the multi-connection :class:`~brutex.adapters.groww.pool.ClientPool` (pool size 4) over
ONE shared :class:`~brutex.adapters.call_meter.CallMeter`, the real
:func:`~brutex.adapters.groww.ingest.ingest_spot` + :class:`~brutex.orchestrator.\
parallel.PoolRunParallel`, and the real :class:`~brutex.data.sink.ParquetBarSink` /
:class:`~brutex.data.parquet_writer.ParquetWriter` to a tmp lake — against a synthetic,
deterministic, network-free :class:`~tests.fakes.fake_groww_sdk.FakeGrowwSDK`.

The harness samples the shared meter's ``snapshot()`` via the existing per-window
``on_window`` hook and produces a frozen :class:`PullHarnessReport` of the honest
achieved-vs-allowed throughput, so the slack/idle CI gate
(``tests/bench/test_pull_at_scale_gate.py``) can assert the pull saturates UP TO the
allowed rate (slack < 15 %, pool idle ≤ 25 %) instead of trusting the controller's
self-report. ENGINE UNCHANGED — this is a test-only observability harness over the
existing pull stack (no ``brutex/`` change).
"""

from __future__ import annotations

from tests.bench.pull_harness.report import PullHarnessReport
from tests.bench.pull_harness.runner import run_pull_harness
from tests.bench.pull_harness.scenario import PullScenario

__all__ = ["PullHarnessReport", "PullScenario", "run_pull_harness"]
