"""Hook-A seam wiring — the Stage-3 one-merge-per-pass batch seam (DEC-RESULTS-TOPK-ROLLUP-001).

Mutant-killer for the ``rollup_paths_hook`` threading through
``run_stage3_pass``: ONE batch per pass carrying exactly the flushed
cell-store paths (never per cell; a severed seam or a dropped kwarg
fails the named test).

Split out of the single ``test_engine_results_rollup_wiring.py`` at the
CI-red fix round so this driver's OWN discovery-gate stem family
(``scripts/ci/coverage_gate_discovery.py``) covers its hook seam; reuses
the sibling suite's REAL fixtures (the house cross-test-import precedent
— ``tests/browser/_fixtures.py``): no duplicated grid/bars/guards.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("polars", reason="the drivers persist ResultsStore Parquet files")

from brutex.engine.sweep_orchestrator import make_store_for, run_sweep_grid
from brutex.orchestrator.audit import AuditWriter


class TestStage3PassHook:
    def test_hook_fires_once_per_pass_with_the_flushed_cell_paths(self, tmp_path: Path) -> None:
        from brutex.engine.stage3_wire import run_stage3_pass

        from tests.unit.test_engine_stage3_wire import (
            _N_DAYS,
            _SCOPE,
            _dedup,
            _grid,
            _trending_bars,
        )

        grid = _grid()
        bars = _trending_bars(_N_DAYS)  # past the lockbox window — a REAL train slice
        batches: list[list[Path]] = []

        def bars_for(_instrument: str, _tf_label: str) -> list[object]:
            return bars  # type: ignore[return-value]

        with AuditWriter.open(tmp_path / "audit.jsonl") as audit:
            run_sweep_grid(
                grid,
                bars_for=bars_for,  # type: ignore[arg-type]
                store_for=make_store_for(tmp_path, grid),
                dedup=_dedup(),
                audit=audit,
            )
            report = run_stage3_pass(
                grid,
                results_root=tmp_path,
                bars_for=bars_for,  # type: ignore[arg-type]
                audit=audit,
                exchange=_SCOPE["exchange"],
                segment=_SCOPE["segment"],
                start=_SCOPE["start"],
                end=_SCOPE["end"],
                rollup_paths_hook=lambda paths: batches.append(list(paths)),
            )
        assert report.cells  # the pass genuinely refined cells
        assert len(batches) == 1  # ONE merge per pass — never per cell
        assert batches[0] == [cell.path for cell in report.cells]
        for path in batches[0]:
            assert path.exists()
