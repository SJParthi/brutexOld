"""O-cost bench — the lake invariant scan's PER-FILE cost is flat in lake depth.

DEC-LAKE-HEAL-001. **Honest, narrow claim (Golden Rule #10):** the L3 startup
sweep (:func:`brutex.data.lake_invariants.scan_lake_invariants`) is
**O(files) TOTAL**; the per-file cost = footer + ONE key-column read and
**SCALES WITH THE FILE'S ROW COUNT** — this bench does NOT claim the scan is
O(1). What it pins:

1. the PER-FILE cost is independent of the LAKE SIZE (no accidental re-glob /
   re-scan per file — the super-linear regression class);
2. a small file's absolute per-file cost stays comfortably bounded (measured
   ~1.6 ms/scan for a one-file 400-row lake on the shared dev container,
   2026-07-03; asserted < 100 ms — generous ~×60 headroom, deliberately
   un-flaky);
3. the "at most ONE key-column read per clean file" law at bench scale (spy).

Ceilings route through :mod:`tests.bench._o1_tiers` (DEC-O1-STRICT-TIER-001 —
CLAUDE.md §11 COMMON). Throttle (``per_file_pause``) behaviour is unit-tested in
``tests/unit/test_lake_invariants.py``, not benched.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import time
from collections.abc import Sequence
from pathlib import Path

import pytest

from tests.bench._o1_tiers import ratio_ceiling

pytest.importorskip("polars")
pytest.importorskip("pyarrow")

pytestmark = pytest.mark.bench

_SHALLOW_FILES = 12
_DEEP_FILES = 96
_ROWS_PER_FILE = 60
_ABS_ROWS = 400
_ABS_ITERS = 200
_ABS_CEILING_S = 0.100  # 100 ms/file — ~×60 headroom over the measured ~1.6 ms


def _build_lake(root: Path, n_files: int, rows_per_file: int) -> None:
    """Write *n_files* identical small partitions through the REAL writer."""
    from datetime import UTC, datetime
    from decimal import Decimal

    from brutex.data.models import CandleBar
    from brutex.data.parquet_writer import ParquetWriter

    writer = ParquetWriter(lake_root=root)
    price = Decimal("100.00")
    for idx in range(n_files):
        year = 2010 + idx // 12
        month = 1 + idx % 12
        bars = [
            CandleBar(
                timestamp=datetime(year, month, 2, 4, 0, tzinfo=UTC).replace(
                    hour=4 + minute // 60, minute=minute % 60
                ),
                open=price,
                high=price,
                low=price,
                close=price,
                volume=10,
                open_interest=None,
            )
            for minute in range(rows_per_file)
        ]
        writer.write(exchange="NSE", segment="INDEX", symbol="NIFTY", interval="1minute", bars=bars)


def _per_file_scan_s(root: Path, n_files: int) -> float:
    """Best-of-3 amortised per-file scan time over the whole lake."""
    from brutex.data.lake_invariants import scan_lake_invariants

    best = float("inf")
    for _ in range(3):
        t0 = time.perf_counter()
        report = scan_lake_invariants(root)
        elapsed = time.perf_counter() - t0
        assert report.files_scanned == n_files
        assert report.findings == ()
        best = min(best, elapsed / n_files)
    return best


def test_per_file_scan_cost_flat_in_lake_depth(tmp_path: Path) -> None:
    """Per-file scan cost is O(1) in the LAKE's file count (12 vs 96 files)."""
    shallow = tmp_path / "shallow"
    deep = tmp_path / "deep"
    shallow.mkdir()
    deep.mkdir()
    _build_lake(shallow, _SHALLOW_FILES, _ROWS_PER_FILE)
    _build_lake(deep, _DEEP_FILES, _ROWS_PER_FILE)
    t_shallow = _per_file_scan_s(shallow, _SHALLOW_FILES)
    t_deep = _per_file_scan_s(deep, _DEEP_FILES)
    ratio = t_deep / max(t_shallow, 1e-9)
    assert ratio < ratio_ceiling(5.0), (
        f"per-file scan cost ratio {ratio:.2f}× (files={_DEEP_FILES} vs "
        f"{_SHALLOW_FILES}) exceeds the flat-per-file ceiling — the scanner may "
        f"have regressed to super-linear work per file. "
        f"t_shallow={t_shallow * 1e3:.3f}ms/file, t_deep={t_deep * 1e3:.3f}ms/file."
    )


def test_small_file_absolute_cost_bound(tmp_path: Path) -> None:
    """A ~400-row file scans in well under 100 ms/file (measured ~1.6 ms)."""
    from brutex.data.lake_invariants import scan_lake_invariants

    _build_lake(tmp_path, 1, _ABS_ROWS)
    # Warm-up (imports, page cache) then time the full scan loop.
    scan_lake_invariants(tmp_path)
    t0 = time.perf_counter()
    for _ in range(_ABS_ITERS):
        report = scan_lake_invariants(tmp_path)
        assert report.files_scanned == 1
    mean_s = (time.perf_counter() - t0) / _ABS_ITERS
    assert mean_s < _ABS_CEILING_S, (
        f"one ~{_ABS_ROWS}-row file took {mean_s * 1e3:.2f} ms/scan on average "
        f"(ceiling {_ABS_CEILING_S * 1e3:.0f} ms) — the per-file cost regressed."
    )


def test_scan_read_calls_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """At bench scale a CLEAN lake pays exactly ONE key-column read per file."""
    import polars as pl
    from brutex.data import lake_invariants as li

    _build_lake(tmp_path, _DEEP_FILES, _ROWS_PER_FILE)
    calls: list[Path] = []
    real = li._read_key_columns

    def spy(path: Path, columns: Sequence[str]) -> pl.DataFrame:
        calls.append(path)
        return real(path, columns)

    monkeypatch.setattr(li, "_read_key_columns", spy)
    report = li.scan_lake_invariants(tmp_path)
    assert report.files_scanned == _DEEP_FILES
    assert len(calls) == _DEEP_FILES
    assert len(set(calls)) == _DEEP_FILES


def _build_option_tree(root: Path, n_files: int, rows_per_file: int) -> None:
    """Write *n_files* clean single-month option partitions with raw pyarrow.

    Raw pyarrow keeps the bench fixture cheap (the scanner reads only the two
    key columns anyway); rows stay wholly inside each file's own UTC month so
    the clean fast path is what gets measured
    (DEC-LAKE-HEAL-OPTIONS-FOREIGN-001).
    """
    from datetime import UTC, datetime

    import pyarrow as pa
    import pyarrow.parquet as pq

    for idx in range(n_files):
        year = 2010 + idx // 12
        month = 1 + idx % 12
        dest = root / "options" / "SERIES" / f"{year:04d}-{month:02d}.parquet"
        dest.parent.mkdir(parents=True, exist_ok=True)
        stamps = [
            datetime(year, month, 2, 4 + minute // 60, minute % 60, tzinfo=UTC)
            for minute in range(rows_per_file)
        ]
        table = pa.table(
            {
                "side": pa.array(["CE"] * rows_per_file, type=pa.string()),
                "timestamp": pa.array(stamps, type=pa.timestamp("us", tz="UTC")),
            }
        )
        pq.write_table(table, dest)


def test_option_scan_read_calls_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A CLEAN options tree pays exactly ONE key read per file — no sibling reads.

    The foreign classifier's min/max fast path means a clean option file adds
    ZERO reads over the pre-DEC-LAKE-HEAL-OPTIONS-FOREIGN-001 scan (the §6.1
    unit spy is the primary oracle; this pins the shape at bench scale).
    """
    import polars as pl
    from brutex.data import lake_invariants as li

    _build_lake(tmp_path, _SHALLOW_FILES, _ROWS_PER_FILE)
    _build_option_tree(tmp_path, _DEEP_FILES, _ROWS_PER_FILE)
    calls: list[Path] = []
    real = li._read_key_columns

    def spy(path: Path, columns: Sequence[str]) -> pl.DataFrame:
        calls.append(path)
        return real(path, columns)

    monkeypatch.setattr(li, "_read_key_columns", spy)
    report = li.scan_lake_invariants(tmp_path)
    assert report.findings == ()
    assert report.files_scanned == _SHALLOW_FILES
    assert report.option_files_scanned == _DEEP_FILES
    assert len(calls) == _SHALLOW_FILES + _DEEP_FILES
    assert len(set(calls)) == _SHALLOW_FILES + _DEEP_FILES
