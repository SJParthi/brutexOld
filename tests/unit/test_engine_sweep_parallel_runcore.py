"""Parallel-spine G3 live-progress coverage (DEC-SWEEP-UNKILLABLE-CORE-001 WP-6).

Stem-matching supplement so the touched-module discovery gate proves
``brutex.engine.sweep_parallel`` at 100% over its OWN suite: the WP-5 G3
addition — the writer-actor ``on_progress`` tick — was previously exercised
only by the run-core integration suite (a foreign stem the gate cannot map).
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import pytest
from brutex.engine.sweep_orchestrator import count_sweep_tuples
from brutex.engine.sweep_parallel import run_parallel_grid
from brutex.orchestrator.audit import AuditWriter

from tests.unit.test_engine_sweep_parallel import _bars_for, _grid, _guards

pytest.importorskip("polars")
pytest.importorskip("lmdb")

#: D-6 traceability — test name → requirement/gap tokens.
TRACEABILITY: Final[dict[str, tuple[str, ...]]] = {
    "test_g3_writer_actor_ticks_on_progress_per_applied_tuple": ("G3",),
}


def test_g3_writer_actor_ticks_on_progress_per_applied_tuple(tmp_path: Path) -> None:
    """The registered hook observes one MONOTONE snapshot per applied tuple."""
    grid = _grid()
    total = count_sweep_tuples(grid)
    evaluated: list[int] = []
    out = tmp_path / "out"
    with AuditWriter.open(tmp_path / "i.jsonl") as ing, _guards(tmp_path, grid) as guards:
        summary = run_parallel_grid(
            grid,
            bars_for=_bars_for,
            root=out,
            ingest_audit=ing,
            guards=guards,
            workers=3,
            on_progress=lambda snap: evaluated.append(snap.evaluated),
        )
    assert summary.tuples_run == total
    assert len(evaluated) == total  # ONE tick per applied tuple — never fewer
    assert evaluated == sorted(evaluated)  # writer-actor ordering ⇒ monotone
    assert evaluated[-1] == total
