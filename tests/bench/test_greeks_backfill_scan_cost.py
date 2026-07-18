"""O-cost bench — the Greeks-backfill scan/checkpoint envelope is machine-pinned.

DEC-GREEKS-BULK-REENRICH-001 fix round 4 (refuter-confirmed MINOR): the module
borrows the lake_heal #1175 "O(files) NEVER O(1)" wording, but #1175 backed
that wording with a real bench pin (``test_lake_invariants_scan_cost.py``)
while the backfill shipped its complexity claims verified only by code
reading — a probe regression to full-table reads or a checkpoint degrading to
O(units²) bytes would have passed CI silently. **Honest, narrow claims
(Golden Rule #10)** — the scan is **O(files) TOTAL**, never O(1); what this
bench pins:

1. the PER-FILE probe cost is independent of the LAKE's file count (no
   accidental re-glob / re-scan per file — the super-linear regression class);
2. the probe reads ONLY the ``greeks_provenance_id`` column on a clean file
   (spy — kills the full-table-read regression the flat-ratio test alone
   cannot see);
3. the per-batch checkpoint record's DURABLE byte cost is O(1) in the plan
   size (a 100× larger plan writes the same few-hundred-byte sidecar — kills
   an O(units²) checkpoint-bytes regression).

Ceilings route through :mod:`tests.bench._o1_tiers` (DEC-O1-STRICT-TIER-001 —
CLAUDE.md §11 COMMON).
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import time
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from tests.bench._o1_tiers import ratio_ceiling

pytest.importorskip("trading_core", reason="greeks enrichment needs the trading_core wheel")
pytest.importorskip("polars")

import polars as pl
from brutex.data import greeks_backfill
from brutex.data.errors import LakeWriteHalt
from brutex.data.greeks_backfill import (
    BackfillScan,
    checkpoint_path,
    plan_greeks_backfill,
    run_greeks_backfill,
    scan_stale_greeks_targets,
)
from brutex.data.greeks_enrich import (
    RATE_SOURCE_ID_FIXED,
    STATUS_OK,
    STYLE_EUROPEAN_EXACT,
    GreeksEnrichReport,
    GreeksEnrichTarget,
    pack_greeks_provenance,
)
from brutex.options.risk_free import FixedRateSource

pytestmark = pytest.mark.bench

_RATE = FixedRateSource()
_CURRENT_PID = pack_greeks_provenance(
    rate_source_id=RATE_SOURCE_ID_FIXED, style_flag=STYLE_EUROPEAN_EXACT, status=STATUS_OK
)
_ROWS_PER_FILE = 200
_SHALLOW_CONTRACTS = 1  # × 12 months = 12 files
_DEEP_CONTRACTS = 8  # × 12 months = 96 files
_MONTHS = 12


def _build_lake(root: Path, n_contracts: int) -> int:
    """Write *n_contracts* fully-CURRENT option contracts × 12 month files each."""
    n_files = 0
    for idx in range(n_contracts):
        symbol = f"NSE-NIFTY-25Jan24-{21000 + idx * 100}-CE"
        for month in range(1, _MONTHS + 1):
            dest = (
                root / "bars" / "NSE" / "FNO" / symbol / "1minute" / "2024" / f"{month:02d}.parquet"
            )
            dest.parent.mkdir(parents=True, exist_ok=True)
            pl.DataFrame(
                {
                    "open": pl.Series([100.0] * _ROWS_PER_FILE, dtype=pl.Float64),
                    "high": pl.Series([101.0] * _ROWS_PER_FILE, dtype=pl.Float64),
                    "low": pl.Series([99.0] * _ROWS_PER_FILE, dtype=pl.Float64),
                    "close": pl.Series([100.5] * _ROWS_PER_FILE, dtype=pl.Float64),
                    "volume": pl.Series([10] * _ROWS_PER_FILE, dtype=pl.Int64),
                    "greeks_provenance_id": pl.Series(
                        [_CURRENT_PID] * _ROWS_PER_FILE, dtype=pl.Int32
                    ),
                }
            ).write_parquet(dest)
            n_files += 1
    return n_files


def _per_file_scan_s(root: Path, n_files: int) -> float:
    """Best-of-3 amortised per-file scan time over the whole lake."""
    best = float("inf")
    for _ in range(3):
        t0 = time.perf_counter()
        scan = scan_stale_greeks_targets(root, rate_source=_RATE)
        elapsed = time.perf_counter() - t0
        assert scan.files_probed == n_files  # all-current → every file probed
        assert scan.targets == ()
        best = min(best, elapsed / n_files)
    return best


def test_per_file_probe_cost_flat_in_lake_depth(tmp_path: Path) -> None:
    """Per-file probe cost is O(1) in the LAKE's file count (12 vs 96 files)."""
    shallow = tmp_path / "shallow"
    deep = tmp_path / "deep"
    shallow.mkdir()
    deep.mkdir()
    n_shallow = _build_lake(shallow, _SHALLOW_CONTRACTS)
    n_deep = _build_lake(deep, _DEEP_CONTRACTS)
    t_shallow = _per_file_scan_s(shallow, n_shallow)
    t_deep = _per_file_scan_s(deep, n_deep)
    ratio = t_deep / max(t_shallow, 1e-9)
    assert ratio < ratio_ceiling(5.0), (
        f"per-file probe cost ratio {ratio:.2f}× (files={n_deep} vs {n_shallow}) "
        f"exceeds the flat-per-file ceiling — the scanner may have regressed to "
        f"super-linear work per file. "
        f"t_shallow={t_shallow * 1e3:.3f}ms/file, t_deep={t_deep * 1e3:.3f}ms/file."
    )


def test_probe_reads_only_the_stamp_column_on_a_clean_lake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The clean-file probe is a SINGLE stamp-column projection per month file.

    The flat-ratio gate above cannot see a full-table-read regression (it is
    per-file too); this spy can: every parquet read the scan issues against
    an option month partition of a fully-CURRENT lake must project EXACTLY
    ``["greeks_provenance_id"]`` — once per file (the fixability read is paid
    ONLY on the null-stamp branch, absent here). The spy pins the module's
    GUARDED read seam (``greeks_backfill.read_parquet_regular`` —
    DEC-LAKE-READ-INODE-GUARD-001 hands polars the fd-validated OPEN handle,
    so a ``pl.read_parquet`` spy filtering on path strings goes blind:
    ``str(source)`` is a ``FileIO`` repr, never the ``.parquet`` path).
    """
    n_files = _build_lake(tmp_path, _SHALLOW_CONTRACTS)
    real_read = greeks_backfill.read_parquet_regular
    month_reads: list[tuple[str, Any]] = []

    def _spy(path: Any, *, columns: Any = None) -> Any:
        text = str(path)
        if "/FNO/" in text and text.endswith(".parquet"):
            month_reads.append((text, columns))
        return real_read(path, columns=columns)

    monkeypatch.setattr(greeks_backfill, "read_parquet_regular", _spy)
    scan = scan_stale_greeks_targets(tmp_path, rate_source=_RATE)
    assert scan.files_probed == n_files
    assert len(month_reads) == n_files  # exactly ONE read per month file
    assert all(columns == ["greeks_provenance_id"] for _, columns in month_reads), (
        f"the probe lost its stamp-column projection: {month_reads[:3]}"
    )


def _checkpoint_bytes_for_plan(tmp_path: Path, name: str, n_targets: int) -> int:
    """Drive ONE recorded batch through the REAL runner; return the sidecar size."""
    lake = tmp_path / name
    lake.mkdir()
    targets = tuple(
        GreeksEnrichTarget(
            exchange="NSE",
            symbol=f"NSE-NIFTY-25Jan24-{21000 + i}-CE",
            start=date(2024, 1, 1),
            end=date(2024, 2, 2),
        )
        for i in range(n_targets)
    )
    plan = plan_greeks_backfill(
        BackfillScan(targets=targets), rate_source=_RATE, force=False, expired_only=False
    )
    calls = {"n": 0}

    def _enrich(**kwargs: Any) -> GreeksEnrichReport:
        calls["n"] += 1
        if calls["n"] == 2:  # crash AFTER batch 1's record → the sidecar survives
            raise LakeWriteHalt("disk_full")
        return GreeksEnrichReport(
            enriched=len(kwargs["targets"]),
            already_current=0,
            futures_skipped=0,
            undecodable=0,
            no_bars=0,
            errored=0,
            bars_written=1,
            bars_solved=1,
            bars_null=0,
            bars_reused=0,
            spot_reads=0,
            spot_hits=0,
            bars_skipped=0,
        )

    with pytest.raises(LakeWriteHalt):
        run_greeks_backfill(lake, plan=plan, rate_source=_RATE, batch_size=10, enrich=_enrich)
    return checkpoint_path(lake).stat().st_size


def test_checkpoint_bytes_constant_in_plan_size(tmp_path: Path) -> None:
    """The durable per-batch checkpoint is O(1) BYTES in the plan size.

    A 100× larger plan (20 → 2,000 targets) writes the SAME few-hundred-byte
    sidecar (the record is ``sweep_id + total + high-water index``, never a
    per-unit ledger) — kills an O(units)/O(units²) checkpoint-bytes regression
    that would turn the per-batch record into a growing rewrite.
    """
    small = _checkpoint_bytes_for_plan(tmp_path, "small", 20)
    big = _checkpoint_bytes_for_plan(tmp_path, "big", 2_000)
    assert small < 1_024 and big < 1_024  # absolute: a tiny JSON sidecar
    assert big <= small + 16, (  # digits-width slack only, never per-unit growth
        f"checkpoint grew with the plan: {small}B @ 20 targets → {big}B @ 2000 targets"
    )
