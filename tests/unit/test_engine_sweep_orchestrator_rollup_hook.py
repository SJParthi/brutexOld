"""Hook-A seam wiring — the SEQUENTIAL grid (DEC-RESULTS-TOPK-ROLLUP-001).

Mutant-killer for the ``rollup_hook`` threading through
``run_sweep_grid``: drives the REAL driver and asserts the hook fired
with the exact completed tuples AFTER each store landed (a severed seam
or a dropped kwarg fails the named test).

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
    run_sweep_grid,
    tuple_store_path,
)
from brutex.orchestrator.audit import AuditWriter


class TestSequentialGridHook:
    def test_hook_fires_once_per_successful_tuple(self, tmp_path: Path) -> None:
        from tests.unit.test_engine_sweep_orchestrator import _dedup, _grid, _trending_bars

        grid = _grid()
        bars = _trending_bars()
        seen: list[SweepTuple] = []
        with AuditWriter.open(tmp_path / "a.jsonl") as audit:
            summary = run_sweep_grid(
                grid,
                bars_for=lambda _i, _t: bars,
                store_for=make_store_for(tmp_path / "out", grid),
                dedup=_dedup(),
                audit=audit,
                rollup_hook=seen.append,
            )
        assert summary.persisted > 0
        assert seen == list(iter_sweep_tuples(grid))
        # The hook fired AFTER the store landed (the store exists for each).
        for tuple_ in seen:
            assert tuple_store_path(tmp_path / "out", tuple_, grid).exists()
