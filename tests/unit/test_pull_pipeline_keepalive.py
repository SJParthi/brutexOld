"""Unit tests for the volume-keepalive wiring in ``build_fetch_pipeline`` (DEC-VOLUME-KEEPALIVE-001).

The ONE COMMON build site (:mod:`brutex.adapters.groww.pull_pipeline` — both the
CLI ``brutex fetch`` and the dashboard/historical pull route through it) wires a
STARTED :class:`~brutex.data.volume_keepalive.VolumeKeepalive` into the
:class:`~brutex.adapters.groww.fetch_pipeline.FetchPipeline` iff a *lake_root*
is given and the ``BRUTEX_VOLUME_KEEPALIVE`` kill-switch is off; a dry-run /
kill-switched / lake-root-less build spins NO daemon; a keepalive construction
error degrades to ``None`` (the pipeline still builds — never blocks a pull);
and the pipeline's drain leaves no live ``volume-keepalive`` thread (the
thread-leak oracle).
"""

# Whitebox assertions on the wired private keepalive (sanctioned).
# pyright: reportPrivateUsage=none

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest
from brutex.adapters.groww.fetch_pipeline import FetchPipeline
from brutex.adapters.groww.pull_pipeline import (
    _build_volume_keepalive,
    build_fetch_pipeline,
    drain_fetch_pipeline,
)
from brutex.data.volume_keepalive import (
    KEEPALIVE_ENABLED_ENV,
    PROBE_FILENAME,
    VolumeKeepalive,
)
from structlog.testing import capture_logs


def _keepalive_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == "volume-keepalive"]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BRUTEX_GROWW_FETCH_PIPELINE", raising=False)
    monkeypatch.delenv("BRUTEX_GROWW_FETCH_PIPELINE_SIZE", raising=False)
    monkeypatch.delenv(KEEPALIVE_ENABLED_ENV, raising=False)


class TestBuildWiresKeepalive:
    def test_lake_root_wires_started_keepalive_and_drain_stops_it(self, tmp_path: Path) -> None:
        """The production shape: keepalive built + started under lake_root; drain stops it."""
        baseline = len(_keepalive_threads())
        pipeline = build_fetch_pipeline(dry_run=False, lake_root=tmp_path)
        try:
            assert isinstance(pipeline, FetchPipeline)
            keepalive = pipeline._keepalive
            assert keepalive is not None
            assert keepalive.probe_path == tmp_path / PROBE_FILENAME
            assert len(_keepalive_threads()) == baseline + 1
        finally:
            drain_fetch_pipeline(pipeline)
        # The thread-leak oracle: the drain left no live volume-keepalive thread.
        assert len(_keepalive_threads()) == baseline

    def test_no_lake_root_builds_pipeline_without_keepalive(self) -> None:
        pipeline = build_fetch_pipeline(dry_run=False, lake_root=None)
        try:
            assert isinstance(pipeline, FetchPipeline)
            assert pipeline._keepalive is None
        finally:
            drain_fetch_pipeline(pipeline)

    def test_dry_run_spins_no_daemon(self, tmp_path: Path) -> None:
        baseline = len(_keepalive_threads())
        assert build_fetch_pipeline(dry_run=True, lake_root=tmp_path) is None
        assert len(_keepalive_threads()) == baseline

    def test_fetch_pipeline_kill_switch_spins_no_daemon(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The sync kill-switch path has no spill (its inline tail is 90 s-bounded)."""
        monkeypatch.setenv("BRUTEX_GROWW_FETCH_PIPELINE", "off")
        baseline = len(_keepalive_threads())
        assert build_fetch_pipeline(dry_run=False, lake_root=tmp_path) is None
        assert len(_keepalive_threads()) == baseline

    def test_keepalive_kill_switch_builds_pipeline_without_keepalive(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(KEEPALIVE_ENABLED_ENV, "off")
        pipeline = build_fetch_pipeline(dry_run=False, lake_root=tmp_path)
        try:
            assert isinstance(pipeline, FetchPipeline)
            assert pipeline._keepalive is None
        finally:
            drain_fetch_pipeline(pipeline)

    def test_keepalive_build_failure_degrades_never_blocks_pull(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A keepalive construction error → logged + None; the pipeline STILL builds."""

        def _boom(_lake_root: Path) -> VolumeKeepalive:
            raise RuntimeError("keepalive boom")

        monkeypatch.setattr("brutex.data.volume_keepalive.VolumeKeepalive.under_lake_root", _boom)
        with capture_logs() as logs:
            pipeline = build_fetch_pipeline(dry_run=False, lake_root=tmp_path)
        try:
            assert isinstance(pipeline, FetchPipeline)
            assert pipeline._keepalive is None
            assert any(e["event"] == "pull.volume_keepalive.build_failed" for e in logs)
        finally:
            drain_fetch_pipeline(pipeline)

    def test_pipeline_construction_failure_stops_started_keepalive(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pipeline build failure AFTER the keepalive started leaks no daemon."""

        def _boom(*_a: Any, **_k: Any) -> FetchPipeline:
            raise RuntimeError("pipeline boom")

        monkeypatch.setattr("brutex.adapters.groww.fetch_pipeline.FetchPipeline", _boom)
        baseline = len(_keepalive_threads())
        assert build_fetch_pipeline(dry_run=False, lake_root=tmp_path) is None
        assert len(_keepalive_threads()) == baseline


class TestBuildVolumeKeepaliveHelper:
    def test_none_lake_root_returns_none(self) -> None:
        assert _build_volume_keepalive(None) is None

    def test_builds_and_starts_under_lake_root(self, tmp_path: Path) -> None:
        keepalive = _build_volume_keepalive(tmp_path)
        assert keepalive is not None
        try:
            assert keepalive.probe_path == tmp_path / PROBE_FILENAME
            thread = keepalive._thread
            assert thread is not None
            assert thread.is_alive()
        finally:
            keepalive.stop()

    def test_kill_switch_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(KEEPALIVE_ENABLED_ENV, "0")
        assert _build_volume_keepalive(tmp_path) is None

    def test_start_failure_is_swallowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(_self: VolumeKeepalive) -> None:
            raise RuntimeError("start boom")

        monkeypatch.setattr(VolumeKeepalive, "start", _boom)
        with capture_logs() as logs:
            assert _build_volume_keepalive(tmp_path) is None
        assert any(e["event"] == "pull.volume_keepalive.build_failed" for e in logs)
