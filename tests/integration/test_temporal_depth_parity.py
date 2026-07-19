"""Depth-parity tooth (DEC-SWEEP-TEMPORAL-ROBUSTNESS-001, graft 10).

The results-depth sidecar's ``pnl_by_year_json`` (DEC-SWEEP-RESULTS-DEPTH-001,
oneclick zone) and the store's ``stats_by_year_json`` cells (this DEC, /results
zone) are DELIBERATELY not deduplicated — different zones, lifecycles and
consumers. This test turns the redundancy into a DRIFT DETECTOR: over REAL
artifacts (a real sweep with temporal capture ON, then the real run-end depth
pass), every shared ``run_id``'s sidecar year-pnl map must equal the stored
year cells' pnl exactly — both are pure functions of the same trades, so any
divergence is a fold bug, never a tolerance.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("polars", reason="stores + sidecars read through polars")
import polars as pl
from brutex.data.models import CandleBar
from brutex.data.option_dedup import OptionDedupIndex
from brutex.engine.period_stats import decode_period_stats
from brutex.engine.results_depth_pass import run_results_depth_pass
from brutex.engine.results_store import ResultsStore
from brutex.engine.sweep_orchestrator import (
    SweepGridConfig,
    Timeframe,
    iter_sweep_tuples,
    load_or_create_store,
    tuple_store_path,
)
from brutex.engine.sweep_run import ComboDedup, run_persisted_sweep
from brutex.engine.sweep_supervisor import grid_identity
from brutex.engine.trade_store import sidecar_paths_for
from brutex.orchestrator.audit import AuditWriter
from brutex.ranking.temporal import TemporalRobustnessConfig
from brutex.strategies.types import Side

_IST = timezone(timedelta(hours=5, minutes=30))


def _bars() -> list[CandleBar]:
    """The results-depth end-to-end fixture's worst-case-rankable bars, verbatim.

    Re-copied 2026-07-19 (the ledger-#1848 temporal-golden re-lock): the donor
    (``test_results_depth_end_to_end.py::_bars`` — the 12-day gap-up drift of
    DEC-FILL-WORST-CASE-ONLY-001) was re-crafted when #1825 landed, but this
    copy had rotted on the pre-#1825 upward-drift geometry whose every trade
    LOSES under the adverse-max fill law — the observe-mode sweep persisted 0
    finalists and the parity walk below compared nothing. The test's own
    non-empty asserts (``stored_year_pnl`` + ``shared >= 1``) are the
    anti-rot tooth that caught it (the #1553 fixture-rot repair pattern).
    """
    out: list[CandleBar] = []
    cycle = 0
    for d in range(12):
        price = 100.0
        for m in range(120):
            k = m % 7
            if k == 0:
                cycle += 1
            if k == 3:  # shallow bearish bar
                o = price
                c, h, lo = o - 0.3, o + 0.1, o - 0.4
            elif k == 4 and cycle % 4 == 0:  # deep bearish loss carrier
                o = price
                c, h, lo = o - 0.65, o + 0.1, o - 0.75
            else:  # narrow gap-up win bar
                o = price + 3.0
                c, h, lo = o + 0.3, o + 0.4, o - 0.1
            ts = (
                datetime(2026, 1, 5, 9, 16, tzinfo=_IST) + timedelta(days=d, minutes=m)
            ).astimezone(UTC)
            out.append(
                CandleBar(
                    timestamp=ts,
                    open=Decimal(str(round(o, 2))),
                    high=Decimal(str(round(h, 2))),
                    low=Decimal(str(round(lo, 2))),
                    close=Decimal(str(round(c, 2))),
                    volume=100,
                    open_interest=None,
                )
            )
            price = c
    return out


def _grid() -> SweepGridConfig:
    return SweepGridConfig(
        instruments=["NIFTY"],
        timeframes=[Timeframe(label="1minute", minutes=1)],
        directions=[Side.BUY],
        k_values=[1],
        condition_bits=[0, 1, 2],
        stop_losses=[80],
        take_profits=[50],
        proximities=[None],
        mode="A",
        data_snapshot_hash="snap0001",
        indicator_version="ind-v1",
        code_commit_sha="deadbeef",
        # Temporal CAPTURE on (observe filters nothing — every finalist row
        # gains the stored per-period columns; the one-year fixture is judged
        # thanks to the 1-session edge floor).
        temporal=TemporalRobustnessConfig(mode="observe", min_edge_year_sessions=1),
    )


def test_sidecar_year_pnl_equals_stored_year_cells(tmp_path: Path) -> None:
    """REAL artifacts: sweep (temporal ON) → depth pass → exact per-run_id parity."""
    out_root = tmp_path / "sweeps"
    out_root.mkdir()
    grid = _grid()
    bars = _bars()
    audit_path = out_root / "audit" / "sweep-all.jsonl"
    audit_path.parent.mkdir(parents=True)
    tuple_ = next(iter_sweep_tuples(grid))
    store_path = tuple_store_path(out_root, tuple_, grid)
    with AuditWriter(path=audit_path) as audit:
        store = load_or_create_store(store_path)
        dedup = ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6))
        run_persisted_sweep(bars, tuple_.to_run_config(grid), store=store, dedup=dedup, audit=audit)

    report = run_results_depth_pass(
        out_root=out_root,
        grid=grid,
        bars_for=lambda _instrument, _tf: bars,
        minute_bars_for=lambda _instrument: bars,
        vix_bars=None,
    )
    assert report.tuples_processed >= 1

    # The stored side: every traded finalist carries the temporal columns.
    stored = ResultsStore.load(store_path)
    stored_year_pnl: dict[str, dict[str, int]] = {}
    for row in stored.rows():
        if row.stats_by_year_json is None:
            continue
        cells = decode_period_stats(row.stats_by_year_json, kind="year")
        stored_year_pnl[row.run_id] = {year: cell[1] for year, cell in cells.items()}
    assert stored_year_pnl, "the temporal-ON sweep must have stored year cells"

    # The sidecar side: the depth pass's own independent per-year fold.
    paths = sidecar_paths_for(store_path, out_root, run_id=grid_identity(grid))
    aggs = pl.read_parquet(paths.aggs)
    shared = 0
    for record in aggs.iter_rows(named=True):
        run_id = record["run_id"]
        if run_id not in stored_year_pnl:
            continue
        shared += 1
        sidecar_map = json.loads(record["pnl_by_year_json"])
        assert sidecar_map == stored_year_pnl[run_id], (
            f"depth-parity drift for run_id {run_id}: sidecar {sidecar_map!r} "
            f"!= stored {stored_year_pnl[run_id]!r}"
        )
    assert shared >= 1, "at least one run_id must be shared between store and sidecar"
