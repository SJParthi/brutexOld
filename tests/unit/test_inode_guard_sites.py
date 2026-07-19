# pyright: reportPrivateUsage=none
"""Per-site planted-inode regressions (DEC-LAKE-READ-INODE-GUARD-001).

A REPRESENTATIVE subset of the wired sites, one per consumer family: a FIFO
planted at the site's file previously blocked the reader FOREVER (the open(2)
of a FIFO waits for a writer); each site now folds the typed
``NotRegularFileError`` (an ``OSError`` subclass) into its EXISTING degrade
arm in bounded time. Every case runs under the house SIGALRM hang watchdog
(the #1614 round-6 idiom) so a regression fails LOUDLY instead of hanging the
suite. Regular-file behavior at each site is pinned unchanged by the sites'
own pre-existing suites (green unmodified).
"""

from __future__ import annotations

import os
import signal
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import pytest
from brutex.dashboard.health_hub import _read_report_json
from brutex.dashboard.oneclick_artifacts import load_manifest
from brutex.data.window_ledger_store import deltas_dir, load_window_ledger
from brutex.engine.sweep_checkpoint import SweepCheckpointError, SweepCheckpointStore
from structlog.testing import capture_logs


@contextmanager
def _hang_watchdog(seconds: int = 5) -> Generator[None]:
    """Fail LOUDLY if the guarded site blocks — the fails-before shape is a hang."""

    def _boom(_signum: int, _frame: object) -> None:
        raise AssertionError(f"site read blocked > {seconds}s — the inode-guard hang is back")

    previous = signal.signal(signal.SIGALRM, _boom)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


class TestSweepCheckpointSite:
    """TIER 1 — the load runs while the lake-root pull single-flight is HELD."""

    def test_fifo_checkpoint_raises_typed_not_hangs(self, tmp_path: Path) -> None:
        fifo = tmp_path / "checkpoint.json"
        os.mkfifo(fifo)
        store = SweepCheckpointStore(path=fifo, sweep_id="s1", total_combos=10)
        with _hang_watchdog(), pytest.raises(SweepCheckpointError, match="Failed to read"):
            store.load()


class TestWindowLedgerSite:
    """The ledger fold — a FIFO delta is SKIPPED loudly (the never-raises contract)."""

    def test_fifo_delta_is_skipped_loudly_not_hung(self, tmp_path: Path) -> None:
        deltas = deltas_dir(tmp_path)
        deltas.mkdir(parents=True)
        os.mkfifo(deltas / "000001.parquet")
        with _hang_watchdog(), capture_logs() as logs:
            ledger = load_window_ledger(tmp_path)
        assert not ledger.load_failed  # only a failed BASE read sets the flag
        skips = [e for e in logs if e["event"] == "window_ledger.delta_load_failed"]
        assert len(skips) == 1


class TestHealthHubSite:
    """The nightly report lane — a FIFO reads the NAMED unreadable reason."""

    def test_fifo_report_reads_named_unreadable(self, tmp_path: Path) -> None:
        fifo = tmp_path / "report.json"
        os.mkfifo(fifo)
        with _hang_watchdog():
            verdict = _read_report_json(fifo)
        assert verdict == "report.json unreadable: NotRegularFileError"


class TestOneclickArtifactSite:
    """The oneclick reader — a FIFO manifest folds into the NAMED-corrupt arm."""

    def test_fifo_manifest_reads_named_corrupt(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "sweep_runs" / "run1"
        run_dir.mkdir(parents=True)
        os.mkfifo(run_dir / "manifest.json")
        with _hang_watchdog():
            result = load_manifest(run_dir)
        assert result.status == "corrupt"
        assert "NotRegularFileError" in result.detail
