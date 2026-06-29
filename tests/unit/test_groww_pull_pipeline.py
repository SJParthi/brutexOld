"""Dedicated unit tests for the COMMON async fetch/write-pipeline lifecycle helpers.

DEC-CLI-FETCH-ISOLATION-001 (MIGRATION.md #1001) — :mod:`brutex.adapters.groww.pull_pipeline`
is the single source of truth for BUILDING + DRAINING the async fetch/process separation
pool (:class:`~brutex.adapters.groww.fetch_pipeline.FetchPipeline`) and the coalescing
batched write consumer (:class:`~brutex.data.write_consumer.BatchingWriteConsumer`) that
wrap a Groww spot pull so internal processing can never block or slow the fetch lane. The
helpers were RELOCATED VERBATIM out of :mod:`brutex.dashboard.pull_controller` so the
dashboard, the F&O ``historical_pull`` build site, AND the CLI ``brutex fetch`` build site
share ONE implementation (CLAUDE.md §11 COMMON, no copy-paste).

This module's own dedicated test (the per-file coverage-gate requirement for a NEW module,
``scripts/ci/coverage_gate_discovery.py``): it imports
:mod:`brutex.adapters.groww.pull_pipeline` DIRECTLY and exercises EVERY line + branch of the
five public helpers + the private stall hook by itself —

* :func:`~brutex.adapters.groww.pull_pipeline.build_write_consumer` — dry-run / flag-off /
  flag-on builds / construction failure swallowed → ``None``;
* :func:`~brutex.adapters.groww.pull_pipeline.drain_write_consumer` — ``None`` no-op / drains
  a consumer / swallows a drain error;
* :func:`~brutex.adapters.groww.pull_pipeline.build_fetch_pipeline` — dry-run / default-ON
  builds / size-``None`` (kill-switch) / construction failure swallowed → ``None``;
* :func:`~brutex.adapters.groww.pull_pipeline.reconcile_pairs_with_pipeline` — ``None``
  pipeline / empty per-series / a key with NO folded total kept / a key WITH a total
  replaced;
* :func:`~brutex.adapters.groww.pull_pipeline._pipeline_stall_alert` — the success route +
  the best-effort error-swallow route;
* :func:`~brutex.adapters.groww.pull_pipeline.drain_fetch_pipeline` — ``None`` no-op / drains
  / PROPAGATES a captured :class:`~brutex.data.errors.LakeWriteHalt` (the graceful pull-halt
  path) / swallows any OTHER drain error.
"""

# Whitebox test of the module-private stall hook (sanctioned).
# pyright: reportPrivateUsage=none

from __future__ import annotations

from typing import Any

import pytest
from brutex.adapters.groww.fetch_pipeline import FetchPipeline, SeriesTotals
from brutex.adapters.groww.ingest import IngestReport
from brutex.adapters.groww.pull_pipeline import (
    _pipeline_stall_alert,
    build_fetch_pipeline,
    build_write_consumer,
    drain_fetch_pipeline,
    drain_write_consumer,
    reconcile_pairs_with_pipeline,
)
from brutex.data.errors import LakeWriteHalt
from brutex.data.sink import NullBarSink
from brutex.data.write_consumer import BatchingWriteConsumer


def _report(bars: int, *, windows: int = 2) -> IngestReport:
    """A producer per-pair report — the async path defers ``bars_appended`` to ``0``."""
    return IngestReport(
        windows=windows,
        bars_appended=bars,
        transient_failures=0,
        skipped_windows=0,
        conflicts=0,
        retried_windows=0,
        still_failed_windows=0,
    )


class _FakePipeline:
    """A minimal stand-in exposing only what the helpers touch: per_series_totals + drain."""

    def __init__(
        self,
        per_series: dict[tuple[str, str], SeriesTotals] | None = None,
        *,
        drain_error: BaseException | None = None,
    ) -> None:
        self._per_series = per_series or {}
        self._drain_error = drain_error
        self.drained = False

    def per_series_totals(self) -> dict[tuple[str, str], SeriesTotals]:
        return self._per_series

    def drain(self) -> None:
        self.drained = True
        if self._drain_error is not None:
            raise self._drain_error


# --------------------------------------------------------------------------- #
# build_write_consumer
# --------------------------------------------------------------------------- #
class TestBuildWriteConsumer:
    def test_dry_run_never_builds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Even with the flag ON, a dry-run builds NO consumer (no real writes happen).
        monkeypatch.setenv("BRUTEX_WRITE_BATCH", "1")
        assert build_write_consumer(NullBarSink(), dry_run=True) is None

    def test_flag_off_default_keeps_synchronous_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BRUTEX_WRITE_BATCH", raising=False)
        assert build_write_consumer(NullBarSink(), dry_run=False) is None

    def test_flag_on_builds_batching_consumer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRUTEX_WRITE_BATCH", "1")
        consumer = build_write_consumer(NullBarSink(), dry_run=False)
        try:
            assert isinstance(consumer, BatchingWriteConsumer)
        finally:
            drain_write_consumer(consumer)

    def test_construction_failure_is_swallowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRUTEX_WRITE_BATCH", "1")

        def _boom(*_a: Any, **_k: Any) -> BatchingWriteConsumer:
            raise RuntimeError("construction boom")

        monkeypatch.setattr("brutex.data.write_consumer.BatchingWriteConsumer", _boom)
        assert build_write_consumer(NullBarSink(), dry_run=False) is None


# --------------------------------------------------------------------------- #
# drain_write_consumer
# --------------------------------------------------------------------------- #
class TestDrainWriteConsumer:
    def test_drain_none_is_noop(self) -> None:
        drain_write_consumer(None)  # no raise

    def test_drain_calls_consumer_drain(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRUTEX_WRITE_BATCH", "1")
        consumer = build_write_consumer(NullBarSink(), dry_run=False)
        assert consumer is not None
        drain_write_consumer(consumer)
        # A second drain is idempotent (the thread is already dead).
        drain_write_consumer(consumer)

    def test_drain_error_is_swallowed(self) -> None:
        class _ExplodingConsumer:
            def drain(self) -> None:
                raise RuntimeError("drain boom")

        # The guarded drain logs + swallows — it must NEVER re-raise into the pull finally.
        drain_write_consumer(_ExplodingConsumer())  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# build_fetch_pipeline
# --------------------------------------------------------------------------- #
class TestBuildFetchPipeline:
    def test_dry_run_never_builds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRUTEX_GROWW_FETCH_PIPELINE", "1")
        assert build_fetch_pipeline(dry_run=True) is None

    def test_default_unset_builds_pipeline(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """DEFAULT ON: with NO env flag, the build site constructs a live pipeline."""
        monkeypatch.delenv("BRUTEX_GROWW_FETCH_PIPELINE", raising=False)
        monkeypatch.delenv("BRUTEX_GROWW_FETCH_PIPELINE_SIZE", raising=False)
        pipeline = build_fetch_pipeline(dry_run=False)
        try:
            assert isinstance(pipeline, FetchPipeline)
            # The watchdog stall hook is wired to the pull-health stalled alert.
            assert pipeline._on_stall is _pipeline_stall_alert
        finally:
            drain_fetch_pipeline(pipeline)

    def test_kill_switch_off_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``BRUTEX_GROWW_FETCH_PIPELINE=off`` → size ``None`` → the synchronous path."""
        monkeypatch.setenv("BRUTEX_GROWW_FETCH_PIPELINE", "off")
        assert build_fetch_pipeline(dry_run=False) is None

    def test_construction_failure_is_swallowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BRUTEX_GROWW_FETCH_PIPELINE", raising=False)

        def _boom(*_a: Any, **_k: Any) -> FetchPipeline:
            raise RuntimeError("pipeline boom")

        # The helper resolves FetchPipeline lazily inside the guarded try.
        monkeypatch.setattr("brutex.adapters.groww.fetch_pipeline.FetchPipeline", _boom)
        assert build_fetch_pipeline(dry_run=False) is None


# --------------------------------------------------------------------------- #
# reconcile_pairs_with_pipeline
# --------------------------------------------------------------------------- #
class TestReconcilePairsWithPipeline:
    def test_none_pipeline_returns_unchanged(self) -> None:
        pairs = {("NSE-NIFTY", "1minute"): _report(0)}
        assert reconcile_pairs_with_pipeline(pairs, None) is pairs

    def test_empty_per_series_returns_unchanged(self) -> None:
        pairs = {("NSE-NIFTY", "1minute"): _report(0)}
        out = reconcile_pairs_with_pipeline(pairs, _FakePipeline({}))  # type: ignore[arg-type]
        assert out is pairs

    def test_key_with_no_total_is_kept(self) -> None:
        """A series whose every window was a producer skip/transient keeps its report."""
        key = ("NSE-NIFTY", "1minute")
        other = ("NSE-BANKNIFTY", "1minute")
        pairs = {key: _report(0), other: _report(0)}
        # Totals folded for `other` only — `key` reached no consumer.
        per_series = {other: SeriesTotals(bars_appended=42, conflicts=1, transient=0)}
        out = reconcile_pairs_with_pipeline(pairs, _FakePipeline(per_series))  # type: ignore[arg-type]
        # key untouched (the deferred producer report, bars 0)
        assert out[key] is pairs[key]
        # other replaced with the consumer's real totals
        assert out[other].bars_appended == 42
        assert out[other].conflicts == 1

    def test_key_with_total_is_replaced(self) -> None:
        """The deferred ``bars_appended==0`` is OVERRIDDEN by the consumer's real totals."""
        key = ("NSE-NIFTY", "1minute")
        pairs = {key: _report(0, windows=3)}
        per_series = {key: SeriesTotals(bars_appended=375, conflicts=2, transient=1)}
        out = reconcile_pairs_with_pipeline(pairs, _FakePipeline(per_series))  # type: ignore[arg-type]
        folded = out[key]
        assert folded.bars_appended == 375
        assert folded.conflicts == 2
        assert folded.transient_failures == 1
        assert folded.still_failed_windows == 1
        # producer-resolved counts are LEFT untouched
        assert folded.windows == 3
        assert folded.skipped_windows == 0
        assert folded.retried_windows == 0


# --------------------------------------------------------------------------- #
# _pipeline_stall_alert
# --------------------------------------------------------------------------- #
class TestPipelineStallAlert:
    def test_routes_to_pull_degraded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, Any] = {}

        class _FakeAlerter:
            def pull_degraded(self, **kwargs: Any) -> None:
                captured.update(kwargs)

        monkeypatch.setattr("brutex.notifications.sweep_alerts.SweepAlerter", _FakeAlerter)
        _pipeline_stall_alert(12.0)
        assert captured["rule"] == "pull.stalled"
        assert captured["achieved"] == 0.0
        assert captured["allowed"] == 0.0
        assert "12s" in captured["detail"]

    def test_error_is_swallowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(*_a: Any, **_k: Any) -> Any:
            raise RuntimeError("alerter boom")

        # The best-effort hook must NEVER raise into the watchdog thread.
        monkeypatch.setattr("brutex.notifications.sweep_alerts.SweepAlerter", _boom)
        _pipeline_stall_alert(5.0)  # no raise


# --------------------------------------------------------------------------- #
# drain_fetch_pipeline
# --------------------------------------------------------------------------- #
class TestDrainFetchPipeline:
    def test_drain_none_is_noop(self) -> None:
        drain_fetch_pipeline(None)  # no raise

    def test_drain_calls_pipeline_drain(self) -> None:
        pipeline = _FakePipeline()
        drain_fetch_pipeline(pipeline)  # type: ignore[arg-type]
        assert pipeline.drained is True

    def test_lake_write_halt_propagates(self) -> None:
        """A consumer disk-full / OOM halt is the graceful pull-stop — it must PROPAGATE."""
        pipeline = _FakePipeline(drain_error=LakeWriteHalt("disk_full"))
        with pytest.raises(LakeWriteHalt):
            drain_fetch_pipeline(pipeline)  # type: ignore[arg-type]

    def test_other_drain_error_is_swallowed(self) -> None:
        # Any NON-halt drain error is logged + swallowed so the ledger persist still runs.
        pipeline = _FakePipeline(drain_error=RuntimeError("drain boom"))
        drain_fetch_pipeline(pipeline)  # type: ignore[arg-type]  # no raise
        assert pipeline.drained is True
