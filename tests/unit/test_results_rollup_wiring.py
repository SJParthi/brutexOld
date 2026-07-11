"""Hook-A seam wiring across ALL FOUR drivers (DEC-RESULTS-TOPK-ROLLUP-001).

Mutant-killers for the ``rollup_hook`` / ``rollup_paths_hook`` threading: the
sequential grid (covered again here beside its siblings), the SUPERVISED grid,
the PARALLEL parent's contiguous apply point, and the Stage-3 one-merge-per-pass
batch seam — each drives the REAL driver and asserts the hook fired with the
exact completed tuple(s)/paths (a severed seam or a dropped kwarg fails a named
test). Reuses the sibling suites' REAL fixtures (the house cross-test-import
precedent — ``tests/browser/_fixtures.py``): no duplicated grid/bars/guards.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("polars", reason="the drivers persist ResultsStore Parquet files")

import brutex.engine.sweep_parallel as parallel_mod
from brutex.engine.sweep_orchestrator import (
    SweepTuple,
    iter_sweep_tuples,
    make_store_for,
    run_sweep_grid,
    tuple_store_path,
)
from brutex.engine.sweep_supervisor import run_supervised_grid
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
