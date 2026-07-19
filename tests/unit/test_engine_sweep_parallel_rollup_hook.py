"""Hook-A seam wiring — the PARALLEL parent's apply point (DEC-RESULTS-TOPK-ROLLUP-001).

Mutant-killers for the ``rollup_hook`` threading through
``ParallelGridDriver._apply``: the hook fires PARENT-side in canonical
apply order for successful tuples ONLY (a failed tuple never reaches
the rollup; a severed seam or a dropped kwarg fails a named test).

Split out of the single ``test_engine_results_rollup_wiring.py`` at the
CI-red fix round so this driver's OWN discovery-gate stem family
(``scripts/ci/coverage_gate_discovery.py``) covers its hook seam; reuses
the sibling suite's REAL fixtures (the house cross-test-import precedent
— ``tests/browser/_fixtures.py``): no duplicated grid/bars/guards.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest

pytest.importorskip("polars", reason="the drivers persist ResultsStore Parquet files")

import brutex.engine.sweep_parallel as parallel_mod
from brutex.engine.sweep_orchestrator import SweepTuple, iter_sweep_tuples
from brutex.observability.logging import reset_configuration
from brutex.orchestrator.audit import AuditWriter


@pytest.fixture(autouse=True)
def _reset_logging_state() -> Generator[None, None, None]:
    """``_worker_init`` binds process logging (DEC-SWEEP-LOG-VOLUME-GUARD-001
    R4 fold) — the imported ``_FakePool`` calls ``_worker_init`` IN-PROCESS,
    which configures GLOBAL structlog state (cached loggers included), so
    reset after each test to keep the suite leak-free. Fixtures do NOT travel
    with helper imports from the sibling suite, hence this file-local twin of
    ``test_engine_sweep_parallel._reset_logging_state``."""
    yield
    reset_configuration()


class TestParallelApplyHook:
    def test_hook_fires_parent_side_in_canonical_apply_order(self, tmp_path: Path) -> None:
        from tests.unit.test_engine_sweep_parallel import _bars_for, _FakePool, _grid, _guards

        grid = _grid()
        tuples = list(iter_sweep_tuples(grid))
        seen: list[SweepTuple] = []
        with (
            AuditWriter.open(tmp_path / "i.jsonl") as ingest,
            _guards(tmp_path, grid) as guards,
        ):
            driver = parallel_mod.ParallelGridDriver(
                grid,
                bars_for=_bars_for,
                root=tmp_path / "out",
                ingest_audit=ingest,
                guards=guards,
                workers=4,
                stall_seconds=300.0,
                rollup_hook=seen.append,
            )
            guards.checkpoint.load()
            acc = parallel_mod._Accumulator()
            fake = _FakePool(parallel_mod._WorkerContext(grid=grid, root=tmp_path / "out"))
            driver._drive(fake, 0, 2, 4, acc)  # type: ignore[arg-type]
        assert acc.tuples_run == len(tuples)
        assert seen == tuples  # canonical enumeration order — the apply point

    def test_hook_not_fired_for_a_failed_tuple(self, tmp_path: Path) -> None:
        from tests.unit.test_engine_sweep_parallel import _bars_for, _FakePool, _grid, _guards

        grid = _grid()
        tuples = list(iter_sweep_tuples(grid))
        failed = parallel_mod.TupleWorkResult(
            index=0, summary=None, failed_error="boom", audit_records=()
        )
        seen: list[SweepTuple] = []
        with (
            AuditWriter.open(tmp_path / "i.jsonl") as ingest,
            _guards(tmp_path, grid) as guards,
        ):
            driver = parallel_mod.ParallelGridDriver(
                grid,
                bars_for=_bars_for,
                root=tmp_path / "out",
                ingest_audit=ingest,
                guards=guards,
                workers=4,
                stall_seconds=300.0,
                rollup_hook=seen.append,
            )
            guards.checkpoint.load()
            acc = parallel_mod._Accumulator()
            fake = _FakePool(
                parallel_mod._WorkerContext(grid=grid, root=tmp_path / "out"),
                result_for={0: failed},
            )
            driver._drive(fake, 0, 2, 4, acc)  # type: ignore[arg-type]
        assert acc.tuples_failed == 1
        assert tuples[0] not in seen  # only SUCCESSFUL tuples reach the rollup
        assert seen == tuples[1:]
