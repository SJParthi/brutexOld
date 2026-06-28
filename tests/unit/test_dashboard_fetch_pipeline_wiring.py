"""Wiring tests for the dashboard FULL async fetch/process pipeline build/drain helpers.

DEC-GROWW-FETCH-ASYNC-SEPARATION-001 — every branch of the best-effort
:func:`~brutex.dashboard.pull_controller._build_fetch_pipeline` +
:func:`~brutex.dashboard.pull_controller._drain_fetch_pipeline`:

* a dry-run never builds a pipeline (no real writes);
* the pipeline is ON BY DEFAULT — an UNSET flag builds a live
  :class:`~brutex.adapters.groww.fetch_pipeline.FetchPipeline`;
* the ``BRUTEX_GROWW_FETCH_PIPELINE=off`` kill-switch falls back to the synchronous path;
* a construction failure is swallowed → ``None`` (binding it can never crash the pull);
* drain is a guarded no-op on ``None``, swallows a generic drain error, but PROPAGATES a
  captured :class:`~brutex.data.errors.LakeWriteHalt` (the graceful pull-halt path).
"""

# Whitebox test of the module-private build/drain helpers (sanctioned).
# pyright: reportPrivateUsage=none

from __future__ import annotations

from typing import Any

import pytest
from brutex.adapters.groww.fetch_pipeline import FetchPipeline
from brutex.adapters.groww.ingest import IngestReport
from brutex.dashboard.pull_controller import (
    _build_fetch_pipeline,
    _drain_fetch_pipeline,
    _pipeline_stall_alert,
    _reconcile_pairs_with_pipeline,
)
from brutex.data.errors import LakeWriteHalt


class TestBuildFetchPipeline:
    def test_dry_run_never_builds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRUTEX_GROWW_FETCH_PIPELINE", "1")
        assert _build_fetch_pipeline(dry_run=True) is None

    def test_default_unset_builds_pipeline(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """DEFAULT ON: with NO env flag set, the production build site constructs the pipeline."""
        monkeypatch.delenv("BRUTEX_GROWW_FETCH_PIPELINE", raising=False)
        monkeypatch.delenv("BRUTEX_GROWW_FETCH_PIPELINE_SIZE", raising=False)
        pipeline = _build_fetch_pipeline(dry_run=False)
        try:
            assert isinstance(pipeline, FetchPipeline)
            assert pipeline.workers == 4  # the audited default size
            # MINOR-5: the watchdog stall hook is wired to the pull-health stalled alert.
            assert pipeline._on_stall is _pipeline_stall_alert  # pyright: ignore[reportPrivateUsage]
        finally:
            _drain_fetch_pipeline(pipeline)

    def test_kill_switch_off_keeps_synchronous_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """KILL-SWITCH: ``=off`` disables the pipeline → the byte-identical synchronous path."""
        monkeypatch.setenv("BRUTEX_GROWW_FETCH_PIPELINE", "off")
        assert _build_fetch_pipeline(dry_run=False) is None

    def test_kill_switch_explicit_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRUTEX_GROWW_FETCH_PIPELINE", "0")
        assert _build_fetch_pipeline(dry_run=False) is None

    def test_flag_on_builds_pipeline(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRUTEX_GROWW_FETCH_PIPELINE", "1")
        monkeypatch.setenv("BRUTEX_GROWW_FETCH_PIPELINE_SIZE", "2")
        pipeline = _build_fetch_pipeline(dry_run=False)
        try:
            assert isinstance(pipeline, FetchPipeline)
            assert pipeline.workers == 2
        finally:
            _drain_fetch_pipeline(pipeline)

    def test_construction_failure_is_swallowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRUTEX_GROWW_FETCH_PIPELINE", "1")

        def _boom(*_a: Any, **_k: Any) -> FetchPipeline:
            raise RuntimeError("construction boom")

        monkeypatch.setattr("brutex.adapters.groww.fetch_pipeline.FetchPipeline", _boom)
        assert _build_fetch_pipeline(dry_run=False) is None


class TestReconcilePairsWithPipeline:
    """The completion-count fix: fold the pipeline's post-drain per-series totals into the reports."""

    def _report(self, **kw: Any) -> IngestReport:
        base: dict[str, Any] = {
            "windows": 5,
            "bars_appended": 0,
            "transient_failures": 0,
            "skipped_windows": 1,
            "conflicts": 0,
            "retried_windows": 0,
            "still_failed_windows": 0,
        }
        base.update(kw)
        return IngestReport(**base)

    def test_none_pipeline_returns_unchanged(self) -> None:
        """Synchronous path (no pipeline) → the per-pair reports pass through untouched."""
        pairs = {("13", "1day"): self._report(bars_appended=42)}
        assert _reconcile_pairs_with_pipeline(pairs, None) is pairs

    def test_empty_per_series_returns_unchanged(self) -> None:
        """A pipeline that folded nothing (no consumer ran) → reports pass through untouched."""
        pipeline = FetchPipeline(workers=1)
        try:
            pairs = {("13", "1day"): self._report(bars_appended=0)}
            assert _reconcile_pairs_with_pipeline(pairs, pipeline) is pairs
        finally:
            pipeline.drain()

    def test_folds_matching_series_overrides_bars(self) -> None:
        """A deferred 0-bar report is OVERRIDDEN with the consumer's real per-series totals."""
        pipeline = FetchPipeline(workers=1)
        try:
            pipeline.fold_totals(bars_appended=30, conflicts=2, series_key=("13", "1day"))
            pipeline.fold_tail_transient(("13", "1day"))
            pairs = {
                ("13", "1day"): self._report(bars_appended=0, windows=5, skipped_windows=1),
                # A series with NO folded total (every window a producer skip) stays as-is.
                ("99", "1minute"): self._report(bars_appended=0, windows=2),
            }
            out = _reconcile_pairs_with_pipeline(pairs, pipeline)
            folded = out[("13", "1day")]
            assert folded.bars_appended == 30
            assert folded.conflicts == 2
            assert folded.still_failed_windows == 1
            assert folded.transient_failures == 1
            # Producer-resolved fields preserved (NOT overridden by the fold).
            assert folded.windows == 5 and folded.skipped_windows == 1
            # The unmatched series keeps its producer report verbatim.
            assert out[("99", "1minute")] is pairs[("99", "1minute")]
        finally:
            pipeline.drain()


class TestPipelineStallAlert:
    """MINOR-5: the watchdog ``on_stall`` hook routes to the pull-health ``pull.stalled`` alert."""

    def test_routes_to_pull_degraded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[dict[str, Any]] = []

        class _FakeAlerter:
            def pull_degraded(self, **kw: Any) -> bool:
                calls.append(kw)
                return True

        monkeypatch.setattr(
            "brutex.notifications.sweep_alerts.SweepAlerter", lambda *a, **k: _FakeAlerter()
        )
        _pipeline_stall_alert(123.4)
        assert len(calls) == 1
        assert calls[0]["rule"] == "pull.stalled"
        assert calls[0]["achieved"] == 0.0
        assert "wedged" in calls[0]["detail"]

    def test_alert_error_is_swallowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A throwing alerter can NEVER crash the pull — the hook swallows + logs."""

        def _boom(*_a: Any, **_k: Any) -> Any:
            raise RuntimeError("alert boom")

        monkeypatch.setattr("brutex.notifications.sweep_alerts.SweepAlerter", _boom)
        _pipeline_stall_alert(5.0)  # no raise


class TestDrainFetchPipeline:
    def test_drain_none_is_noop(self) -> None:
        _drain_fetch_pipeline(None)  # no raise

    def test_drain_calls_pipeline_drain(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRUTEX_GROWW_FETCH_PIPELINE", "1")
        pipeline = _build_fetch_pipeline(dry_run=False)
        assert pipeline is not None
        _drain_fetch_pipeline(pipeline)
        # A second drain is idempotent (the threads are already dead).
        _drain_fetch_pipeline(pipeline)

    def test_generic_drain_error_is_swallowed(self) -> None:
        class _ExplodingPipeline:
            def drain(self) -> None:
                raise RuntimeError("drain boom")

        # The guarded drain logs + swallows a NON-halt error — never re-raised into the finally.
        _drain_fetch_pipeline(_ExplodingPipeline())  # type: ignore[arg-type]

    def test_lake_write_halt_propagates(self) -> None:
        class _HaltingPipeline:
            def drain(self) -> None:
                raise LakeWriteHalt("disk_full")

        # A captured disk-full halt MUST propagate (the graceful pull-halt path), NOT be
        # swallowed — the caller's halt handling + the CRITICAL Telegram alert fire on it.
        with pytest.raises(LakeWriteHalt):
            _drain_fetch_pipeline(_HaltingPipeline())  # type: ignore[arg-type]
