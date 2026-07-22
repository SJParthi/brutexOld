"""The CLI Hook-B rollup reconcile fence (DEC-RESULTS-TOPK-ROLLUP-001).

Drives ``brutex.cli._run_results_rollup_reconcile`` — the fenced
``cmd_sweep_all`` run-end reconcile (a rollup failure NEVER fails the sweep;
KeyboardInterrupt/SystemExit/MemoryError re-raise raw). Moved out of
``tests/unit/test_engine_results_rollup.py`` at the CI-red fix round so the
fence arms live in ``brutex/cli.py``'s OWN ``test_cli*`` discovery-gate stem
family (``scripts/ci/coverage_gate_discovery.py`` measures cli.py over that
family only — the fence arms were its sole <100% residue); reuses the rollup
suite's REAL fixtures (the house cross-test-import precedent —
``tests/browser/_fixtures.py``): no duplicated store/tree builders.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("polars", reason="the rollup persists ResultsStore Parquet files")

import brutex.engine.results_rollup as rr
import structlog.testing

from tests.unit.test_engine_results_rollup import _seed_tree


@pytest.fixture
def sweeps_root(tmp_path: Path) -> Path:
    """A bare ``<lake>/sweeps`` root (the rollup suite's fixture shape)."""
    root = tmp_path / "sweeps"
    root.mkdir()
    return root


class TestCliReconcile:
    def test_success_prints_reconciled(
        self, sweeps_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from brutex.cli import _run_results_rollup_reconcile

        _seed_tree(sweeps_root)
        assert _run_results_rollup_reconcile(out_root=sweeps_root) is True
        assert "reconciled" in capsys.readouterr().out
        assert rr.rollup_parquet_path(sweeps_root).exists()

    def test_zero_store_prints_skipped(
        self, sweeps_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from brutex.cli import _run_results_rollup_reconcile

        assert _run_results_rollup_reconcile(out_root=sweeps_root) is False
        assert "skipped (no stores)" in capsys.readouterr().out

    def test_failure_is_fenced_and_loud(
        self, sweeps_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from brutex.cli import _run_results_rollup_reconcile

        def _boom(root: Path) -> bool:
            raise OSError("disk gone")

        with structlog.testing.capture_logs() as logs:
            assert _run_results_rollup_reconcile(out_root=sweeps_root, rebuild=_boom) is False
        assert "rollup reconcile FAILED" in capsys.readouterr().err
        assert any(e["event"] == "cli.results_rollup.failed" for e in logs)

    def test_carve_out_propagates(self, sweeps_root: Path) -> None:
        from brutex.cli import _run_results_rollup_reconcile

        def _interrupt(root: Path) -> bool:
            raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            _run_results_rollup_reconcile(out_root=sweeps_root, rebuild=_interrupt)
