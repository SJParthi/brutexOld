"""Hook-A seam wiring — the SUPERVISED grid (DEC-RESULTS-TOPK-ROLLUP-001).

Mutant-killer for the ``rollup_hook`` threading through
``run_supervised_grid``: drives the REAL driver and asserts the hook
fired once per successful tuple in canonical enumeration order (a
severed seam or a dropped kwarg fails the named test).

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

from brutex.engine.sweep_orchestrator import (
    SweepTuple,
    iter_sweep_tuples,
    make_store_for,
)
from brutex.engine.sweep_supervisor import run_supervised_grid
from brutex.orchestrator.audit import AuditWriter


class TestSupervisedGridHook:
    def test_hook_fires_once_per_successful_tuple(self, tmp_path: Path) -> None:
        from tests.unit.test_engine_sweep_supervisor import (
            _dedup,
            _grid,
            _open_guards,
            _trending_bars,
        )

        grid = _grid()
        bars = _trending_bars()
        seen: list[SweepTuple] = []
        with (
            _open_guards(tmp_path, grid) as guards,
            AuditWriter.open(tmp_path / "ingest.jsonl") as ingest,
        ):
            summary = run_supervised_grid(
                grid,
                bars_for=lambda _i, _t: bars,
                store_for=make_store_for(tmp_path / "results", grid),
                dedup=_dedup(),
                ingest_audit=ingest,
                guards=guards,
                rollup_hook=seen.append,
            )
        assert summary.tuples_run == len(seen) > 0
        assert seen == list(iter_sweep_tuples(grid))
