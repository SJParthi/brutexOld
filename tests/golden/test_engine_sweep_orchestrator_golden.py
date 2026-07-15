"""Golden byte-equality oracle for the sweep orchestrator.

Three reproducibility / idempotence contracts (Golden Rule #4 / #7):

* **Determinism across independent runs.** Two fresh grid runs (fresh dedup,
  distinct roots) produce byte-identical per-tuple Parquet files.
* **Idempotent lossless re-run (the hostile-review P0 fix).** Re-running a grid
  against the SAME roots + a SHARED, already-populated dedup re-flushes the
  identical finalist rows — the files stay byte-identical and are NEVER
  overwritten with an empty store. ``evaluated`` drops to 0 on the re-run (every
  mask is dedup-skipped), proving the resume path while preserving the data.
* **Audit framing is stable.** The grid emits exactly ``2 + 3·tuples_run`` audit
  lines (``GRID_SWEEP_START`` + per-tuple ``SWEEP_*`` + ``GRID_SWEEP_COMPLETE``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.data.models import CandleBar
from brutex.data.option_dedup import OptionDedupIndex
from brutex.engine.sweep_orchestrator import (
    SweepGridConfig,
    SweepGridSummary,
    Timeframe,
    iter_sweep_tuples,
    make_store_for,
    run_sweep_grid,
    tuple_store_path,
)
from brutex.engine.sweep_run import ComboDedup
from brutex.execution.fill_mode import FillMode
from brutex.orchestrator.audit import AuditWriter, verify_chain
from brutex.strategies.types import Side

_IST = timezone(timedelta(hours=5, minutes=30))


def _bar(day: int, minute: int, o: float, c: float) -> CandleBar:
    ts = (
        datetime(2026, 1, 5, 9, 16, tzinfo=_IST) + timedelta(days=day, minutes=minute)
    ).astimezone(UTC)
    hi = max(o, c) + 0.5
    lo = min(o, c) - 0.5
    return CandleBar(
        timestamp=ts,
        open=Decimal(str(round(o, 2))),
        high=Decimal(str(round(hi, 2))),
        low=Decimal(str(round(lo, 2))),
        close=Decimal(str(round(c, 2))),
        volume=100,
        open_interest=None,
    )


def _trending_bars(n_days: int = 15, per_day: int = 120) -> list[CandleBar]:
    bars: list[CandleBar] = []
    for d in range(n_days):
        price = 100.0
        for m in range(per_day):
            close = price + 0.6 if m % 3 else price - 0.3
            bars.append(_bar(d, m, price, close))
            price = close
    return bars


def _grid() -> SweepGridConfig:
    return SweepGridConfig(
        instruments=["NIFTY"],
        timeframes=[Timeframe("1minute", 1)],
        directions=[Side.BUY, Side.SELL],
        k_values=[1],
        condition_bits=[0, 1, 2, 3, 4],
        stop_losses=[80],
        take_profits=[50],
        proximities=[None],
        mode="A",
        data_snapshot_hash="snap0001",
        indicator_version="ind-v1",
        code_commit_sha="deadbeef",
        # LEGACY-MODE PIN (DEC-FILL-WORST-CASE-001): the lossless-resume
        # golden's persisted-count expectations were built under the legacy
        # exact fills — pin PRECISE explicitly.
        fill_mode=FillMode.PRECISE,
    )


def _dedup() -> ComboDedup:
    return ComboDedup(OptionDedupIndex(capacity=100_000, fpr_target=1e-7))


def _run(
    root: Path, audit_path: Path, dedup: ComboDedup, bars: list[CandleBar]
) -> SweepGridSummary:
    grid = _grid()
    with AuditWriter.open(audit_path) as audit:
        return run_sweep_grid(
            grid,
            bars_for=lambda _i, _t: bars,
            store_for=make_store_for(root, grid),
            dedup=dedup,
            audit=audit,
        )


def test_golden_determinism_across_independent_runs(tmp_path: Path) -> None:
    pytest.importorskip("polars")
    bars = _trending_bars()
    a_root, b_root = tmp_path / "a", tmp_path / "b"
    sa = _run(a_root, tmp_path / "a.jsonl", _dedup(), bars)
    sb = _run(b_root, tmp_path / "b.jsonl", _dedup(), bars)
    assert sa == sb
    grid = _grid()
    for t in iter_sweep_tuples(grid):
        pa = tuple_store_path(a_root, t, grid)
        pb = tuple_store_path(b_root, t, grid)
        assert pa.read_bytes() == pb.read_bytes()


def test_golden_idempotent_resume_is_lossless(tmp_path: Path) -> None:
    """Re-run over the SAME root → sentinel short-circuit, byte-identical files."""
    pytest.importorskip("polars")
    bars = _trending_bars()
    root = tmp_path / "results"
    grid = _grid()
    shared = _dedup()

    first = _run(root, tmp_path / "run1.jsonl", shared, bars)
    assert first.evaluated > 0
    assert first.persisted > 0
    # Snapshot the per-tuple bytes after the FIRST run.
    before = {
        tuple_store_path(root, t, grid): tuple_store_path(root, t, grid).read_bytes()
        for t in iter_sweep_tuples(grid)
    }

    # Re-run against the SAME root + the SAME (now populated) dedup.
    second = _run(root, tmp_path / "run2.jsonl", shared, bars)
    # Every tuple is short-circuited by its PAIRED completion sentinel
    # (DEC-MARK-MANY-SENTINEL-001) — nothing re-evaluated, and the dedup is
    # never consulted (its marks are advisory; pre-sentinel this re-run was
    # instead dedup-skipped with skipped_dedup == first.evaluated) …
    assert second.evaluated == 0
    assert second.skipped_dedup == 0
    # … and the files are byte-IDENTICAL, never overwritten with an empty store.
    for path, original in before.items():
        assert path.read_bytes() == original, "resume re-run must not lose results"


def test_golden_audit_line_count(tmp_path: Path) -> None:
    pytest.importorskip("polars")
    bars = _trending_bars()
    audit_path = tmp_path / "grid.jsonl"
    summary = _run(tmp_path / "r", audit_path, _dedup(), bars)
    # 2 grid lines + 3 per-tuple lines for each tuple that ran.
    assert verify_chain(audit_path) == 2 + 3 * summary.tuples_run
