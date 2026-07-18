"""Golden intactness under the FULL run core (DEC-SWEEP-UNKILLABLE-CORE-001 WP-5).

Reproduces the two hand-pinned oracles of
``tests/golden/test_engine_sweep_run_golden.py`` — ``_GOLDEN_RUN_ID`` and
``_GOLDEN_CONTENT_HASH`` — through the WIRED stack with EVERY Phase-2 default
ON: the run-core composition (recorder + tripwires + manifest + composed
hooks), the G1 quarantine seam, AND the R45 diffcheck hook genuinely engaged.
The grid is constructed so its single tuple's ``SweepRunConfig`` is
byte-identical to the golden fixture's, so any Phase-2 leak into enumeration,
simulation, ranking, or store bytes moves the pinned literals (Golden Rule #4).
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from brutex.engine.sweep_orchestrator import (
    SweepGridConfig,
    Timeframe,
    make_store_for,
    tuple_store_path,
)
from brutex.engine.sweep_run_core import sweep_run_core
from brutex.engine.sweep_supervisor import grid_identity
from brutex.notifications.sweep_alerts import SweepAlerter
from brutex.observability.flight_recorder import iter_run_records
from brutex.observability.run_tripwires import TripwireConfig
from brutex.strategies.types import Side

from tests.golden.test_engine_sweep_run_golden import (
    _CTX,
    _GOLDEN_CONTENT_HASH,
    _GOLDEN_RUN_ID,
    _PARAMS,
    _bars,
)


def _golden_grid() -> SweepGridConfig:
    """The 1-tuple grid whose tuple's run config equals the golden fixture's."""
    return SweepGridConfig(
        instruments=["NIFTY"],
        timeframes=[Timeframe("1m", 1)],
        directions=[Side.BUY],
        k_values=[1],
        condition_bits=[0, 1, 2, 3, 4],
        stop_losses=[80],
        take_profits=[50],
        proximities=[None],
        mode="A",
        data_snapshot_hash="snap0001",
        indicator_version="ind-v1",
        code_commit_sha="deadbeef",
        # DEC-FILL-WORST-CASE-ONLY-001: worst-case is the ONLY fill law — the
        # golden pair was RE-LOCKED on the rankable gap-up fixture (see the
        # source module's re-lock note); the grid carries no mode field.
    )


def test_golden_run_id_reproduced_under_phase2() -> None:
    from brutex.engine.sweep_run import mask_run_id

    assert mask_run_id(mask=0b101, params=_PARAMS, **_CTX) == _GOLDEN_RUN_ID


def test_golden_content_hash_reproduced_with_all_phase2_defaults_on(tmp_path: Path) -> None:
    pytest.importorskip("polars")
    from brutex.data.option_dedup import OptionDedupIndex
    from brutex.engine.sweep_orchestrator import iter_sweep_tuples, run_one_tuple
    from brutex.engine.sweep_run import ComboDedup, SweepRunSummary
    from brutex.orchestrator.audit import AuditWriter

    grid = _golden_grid()
    bars = _bars()
    progress_path = tmp_path / "progress" / "sweep-progress.json"
    chain_path = tmp_path / "audit" / "sweep-all.jsonl"
    summary: SweepRunSummary | None = None
    with sweep_run_core(
        results_root=tmp_path,
        grid=grid,
        alerter=SweepAlerter(),
        entry_label="golden intact",
        progress_path=progress_path,
        cli_chain_path=chain_path,
        tripwire_config=TripwireConfig(),
        diffcheck_k=8,  # R45 genuinely ON — the rehearsal-sized budget
    ) as core:
        core.stage("sweep")
        assert core.diffcheck is not None
        with AuditWriter.open(chain_path, resume=chain_path.exists()) as audit:
            from brutex.engine.sweep_kernel import ResidentKernelCache

            tuple_ = next(iter_sweep_tuples(grid))
            summary = run_one_tuple(
                grid,
                tuple_,
                bars_for=lambda _i, _t: bars,
                store_for=make_store_for(tmp_path, grid),
                dedup=ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6)),
                audit=audit,
                kernel_cache=ResidentKernelCache(capacity=1),
                diffcheck=core.diffcheck,
                store_quarantine=core.store_quarantine,
            )
        core.set_exit_code(0)
        run_id = core.run_id
        store_path = tuple_store_path(tmp_path, tuple_, grid)

    # THE pin: the persisted store's content hash is the golden literal —
    # diffcheck + quarantine seam + recorder all engaged, zero byte drift.
    assert summary is not None
    assert summary.store_content_hash == _GOLDEN_CONTENT_HASH
    assert store_path.exists()

    # The diffcheck genuinely RAN (engaged, not vacuously skipped) and stayed
    # clean; the run never latched SUSPECT (I-4 never-adopt on a clean run).
    records = list(iter_run_records(tmp_path, run_id))
    ok = [r for r in records if r["kind"] == "DIFFCHECK_OK"]
    assert len(ok) == 1
    # The 1-bit-per-mask golden grid enumerates exactly C(5,1)=5 masks, so the
    # K=8 budget clamps to the enumerable 5 (sample_masks samples min(k, n)).
    assert cast("dict[str, object]", ok[0]["payload"])["checked"] == 5
    assert cast("dict[str, object]", ok[0]["payload"])["mismatches"] == 0
    assert not [r for r in records if r["kind"] == "SUSPECT"]
    assert run_id == grid_identity(grid)


def test_golden_store_bytes_identical_run_core_on_vs_off(tmp_path: Path) -> None:
    pytest.importorskip("polars")
    from brutex.data.option_dedup import OptionDedupIndex
    from brutex.engine.sweep_kernel import ResidentKernelCache
    from brutex.engine.sweep_orchestrator import iter_sweep_tuples, run_one_tuple
    from brutex.engine.sweep_run import ComboDedup
    from brutex.orchestrator.audit import AuditWriter

    grid = _golden_grid()
    bars = _bars()
    tuple_ = next(iter_sweep_tuples(grid))

    def _run(root: Path, *, core_on: bool) -> bytes:
        with AuditWriter.open(root / "audit.jsonl") as audit:
            if core_on:
                with sweep_run_core(
                    results_root=root,
                    grid=grid,
                    alerter=SweepAlerter(),
                    entry_label="golden A/B",
                    tripwire_config=TripwireConfig(),
                    diffcheck_k=8,
                ) as core:
                    run_one_tuple(
                        grid,
                        tuple_,
                        bars_for=lambda _i, _t: bars,
                        store_for=make_store_for(root, grid),
                        dedup=ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6)),
                        audit=audit,
                        kernel_cache=ResidentKernelCache(capacity=1),
                        diffcheck=core.diffcheck,
                        store_quarantine=core.store_quarantine,
                    )
            else:
                run_one_tuple(
                    grid,
                    tuple_,
                    bars_for=lambda _i, _t: bars,
                    store_for=make_store_for(root, grid),
                    dedup=ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6)),
                    audit=audit,
                    kernel_cache=ResidentKernelCache(capacity=1),
                )
        return tuple_store_path(root, tuple_, grid).read_bytes()

    # Hoisted out of the assert (the fills-track side-effecting-assert NIT:
    # under `python -O` a bare `assert _run(...) == _run(...)` strips BOTH
    # sweep executions, silently voiding the byte-identity gate).
    on_bytes = _run(tmp_path / "on", core_on=True)
    off_bytes = _run(tmp_path / "off", core_on=False)
    assert on_bytes == off_bytes
