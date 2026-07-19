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
from brutex.engine.sweep_orchestrator import (
    count_sweep_tuples,
    iter_sweep_tuples,
    tuple_store_path,
)
from brutex.engine.sweep_parallel import run_parallel_grid
from brutex.orchestrator.audit import AuditWriter

from tests.unit.test_engine_sweep_parallel import _bars_for, _grid, _guards

pytest.importorskip("polars")
pytest.importorskip("lmdb")

#: D-6 traceability — test name → requirement/gap tokens.
TRACEABILITY: Final[dict[str, tuple[str, ...]]] = {
    "test_g3_writer_actor_ticks_on_progress_per_applied_tuple": ("G3",),
    "test_on_tuple_fires_per_successful_tuple_in_parallel": ("R39",),
    "test_g1_corrupt_store_quarantined_in_worker_grid_continues": ("G1",),
}


def test_g3_writer_actor_ticks_on_progress_per_applied_tuple(tmp_path: Path) -> None:
    """The hook observes one MONOTONE running snapshot per applied tuple — and
    ONLY those (the #1445 G3 exact-count law).

    Merge-fold reconcile (DEC-SWEEP-ACCEPTANCE-LIFECYCLE-001 ⇄
    DEC-JOB-COOPERATIVE-CANCEL-001): the writer-actor apply path fires ONE
    ``state="running"`` tick per applied tuple (evaluated 1..total, in
    enumeration order) and NO trailing finish tick — ``cmd_sweep_all``'s
    parallel arm publishes the terminal ``done`` snapshot to the bridge right
    after the driver returns, so the dashboard still lands on DONE (pinned by
    ``test_parallel_run_writes_sweep_progress_file``'s adapted call-site
    publish)."""
    from brutex.engine.sweep_progress import STATE_RUNNING

    grid = _grid()
    total = count_sweep_tuples(grid)
    ticks: list[tuple[str, int]] = []
    out = tmp_path / "out"
    with AuditWriter.open(tmp_path / "i.jsonl") as ing, _guards(tmp_path, grid) as guards:
        summary = run_parallel_grid(
            grid,
            bars_for=_bars_for,
            root=out,
            ingest_audit=ing,
            guards=guards,
            workers=3,
            on_progress=lambda snap: ticks.append((snap.state, snap.evaluated)),
        )
    assert summary.tuples_run == total
    # ONE running tick per applied tuple — never fewer, never a trailing finish
    # duplicate — monotone in writer-actor enumeration order (#1445 G3).
    assert len(ticks) == total
    assert [state for state, _ in ticks] == [STATE_RUNNING] * total
    assert [ev for _, ev in ticks] == list(range(1, total + 1))


def test_on_tuple_fires_per_successful_tuple_in_parallel(tmp_path: Path) -> None:
    """MAJOR (round 2): the per-tuple hook runs in the DEFAULT multi-core mode.

    The parent-side ``on_tuple`` closes the default-mode gap where TUPLE_DONE
    flight records + the R39 NaN/absurd per-tuple tripwire were absent — the
    applied summary is parent-side, so no closure crosses the spawn boundary.
    """
    grid = _grid()
    total = count_sweep_tuples(grid)
    seen: list[tuple[str, int]] = []
    out = tmp_path / "out"
    with AuditWriter.open(tmp_path / "i.jsonl") as ing, _guards(tmp_path, grid) as guards:
        summary = run_parallel_grid(
            grid,
            bars_for=_bars_for,
            root=out,
            ingest_audit=ing,
            guards=guards,
            workers=3,
            on_tuple=lambda t, s: seen.append((t.instrument, s.evaluated)),
        )
    assert summary.tuples_run == total
    assert len(seen) == total  # ONE hook call per SUCCESSFUL applied tuple
    assert all(instrument == "NIFTY" for instrument, _ev in seen)


def test_g1_corrupt_store_quarantined_in_worker_grid_continues(tmp_path: Path) -> None:
    """G1 (round 2): a corrupt tuple store is quarantined IN-WORKER, grid CONTINUES.

    Pre-corrupt one tuple's on-disk store so the worker's persist raises
    ResultsStoreSchemaError (a ResultsStoreError). Without the worker-side
    quarantine that non-SweepComboError
    would break the future and ABORT the whole grid. With it, the worker
    quarantines + retries the trio in-process (byte-identical regeneration), the
    grid finishes, and the QUARANTINE event replays into the ``on_quarantine``
    adapter (forensic parity with the sequential path).
    """
    grid = _grid()
    total = count_sweep_tuples(grid)
    out = tmp_path / "out"
    victim = next(iter_sweep_tuples(grid))
    store = tuple_store_path(out, victim, grid)
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_bytes(b"garbage - not parquet")
    quarantined: list[tuple[str, str]] = []
    with AuditWriter.open(tmp_path / "i.jsonl") as ing, _guards(tmp_path, grid) as guards:
        summary = run_parallel_grid(
            grid,
            bars_for=_bars_for,
            root=out,
            ingest_audit=ing,
            guards=guards,
            workers=3,
            on_quarantine=lambda dest_rel, reason: quarantined.append((dest_rel, reason)),
        )
    # The grid completed every tuple — the corrupt store did NOT abort it.
    assert summary.tuples_run == total
    assert summary.tuples_failed == 0
    # The trio was quarantined + the event replayed to the adapter.
    assert len(quarantined) == 1
    dest_rel, reason = quarantined[0]
    assert dest_rel.startswith(".quarantine/runtime/")
    assert "ResultsStoreSchemaError" in reason
    assert (out / ".quarantine" / "runtime").is_dir()
