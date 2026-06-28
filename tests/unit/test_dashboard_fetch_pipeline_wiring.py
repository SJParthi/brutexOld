"""Wiring tests for the dashboard FULL async fetch/process pipeline build/drain helpers.

DEC-GROWW-FETCH-ASYNC-SEPARATION-001 — every branch of the best-effort
:func:`~brutex.dashboard.pull_controller._build_fetch_pipeline` +
:func:`~brutex.dashboard.pull_controller._drain_fetch_pipeline`:

* a dry-run never builds a pipeline (no real writes);
* the ``BRUTEX_GROWW_FETCH_PIPELINE`` flag OFF (the default) keeps the synchronous path;
* the flag ON builds a live :class:`~brutex.adapters.groww.fetch_pipeline.FetchPipeline`;
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
from brutex.dashboard.pull_controller import (
    _build_fetch_pipeline,
    _drain_fetch_pipeline,
)
from brutex.data.errors import LakeWriteHalt


class TestBuildFetchPipeline:
    def test_dry_run_never_builds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRUTEX_GROWW_FETCH_PIPELINE", "1")
        assert _build_fetch_pipeline(dry_run=True) is None

    def test_flag_off_keeps_synchronous_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BRUTEX_GROWW_FETCH_PIPELINE", raising=False)
        assert _build_fetch_pipeline(dry_run=False) is None

    def test_flag_off_explicit_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
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
