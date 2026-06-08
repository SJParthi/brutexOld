"""O(1) benchmark for the raw Parquet lake-write append path.

DEC-O1-BENCH-GAPS-001 (G1 of the 4-gap deep audit). CLAUDE.md Golden Rule #5
names ``result append`` as an O(1) hot path. The in-memory results-store append
already has a gate (``test_engine_results_store_o1.py``), but the deep audit
flagged the **raw lake write** (``ParquetWriter.write`` → a real Parquet file)
as a YELLOW gap — never benched for O(1).

The load-bearing O(1) property here is *write latency independent of how many
partition files the lake already holds*. ``ParquetWriter.write`` resolves a
single canonical partition path and writes (or merge-writes) only that one file
under a per-path lock (``parquet_writer.py:280-312``); it never enumerates or
scans the rest of the lake. So writing the 10,000th partition must cost the same
as the 1st. A regression that walked the lake (e.g. a global index rebuild on
every write) would scale with lake size and blow the ratio.

Each timed write targets a UNIQUE ``symbol`` → a fresh partition file (no merge
path), isolating the create-write cost. File I/O is noisier than pure CPU, so
the shared-CI ceiling matches the other I/O-touching gates (``< 8×``); the
M4-Pro strict tier tightens it to the decreed ``1.1×`` via :func:`ratio_ceiling`.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from brutex.data import CandleBar
from brutex.data.parquet_writer import ParquetWriter

from tests.bench._o1_tiers import ratio_ceiling

#: Existing-partition counts the fresh-write cost must be independent of.
_SMALL = 50
_LARGE = 500
#: How many fresh writes to time at each scale (averaged).
_WRITES = 50


def _bar() -> CandleBar:
    """A single deterministic bar (one partition = one (year, month))."""
    return CandleBar(
        timestamp=datetime(2024, 1, 25, 9, 15, 0, tzinfo=UTC),
        open=Decimal("22000.00"),
        high=Decimal("22010.00"),
        low=Decimal("21990.00"),
        close=Decimal("22005.00"),
        volume=100,
        open_interest=500,
    )


def _write(writer: ParquetWriter, sym: int) -> None:
    """Write one bar to a UNIQUE symbol → a brand-new partition file."""
    writer.write(
        exchange="NSE",
        segment="FNO",
        symbol=f"SYM{sym:06d}",
        interval="1minute",
        bars=[_bar()],
    )


def _prefill(writer: ParquetWriter, n: int) -> None:
    for i in range(n):
        _write(writer, i)


class TestLakeWriteIsO1:
    """``ParquetWriter.write`` cost is independent of existing lake size."""

    def test_write_at_small_lake(self, benchmark, tmp_path: Path) -> None:
        writer = ParquetWriter(lake_root=tmp_path / "s")
        _prefill(writer, _SMALL)
        counter = iter(range(_SMALL, _SMALL + 10_000))
        benchmark(lambda: _write(writer, next(counter)))

    def test_write_at_large_lake(self, benchmark, tmp_path: Path) -> None:
        writer = ParquetWriter(lake_root=tmp_path / "l")
        _prefill(writer, _LARGE)
        counter = iter(range(_LARGE, _LARGE + 10_000))
        benchmark(lambda: _write(writer, next(counter)))

    def test_write_ratio_below_8x(self, tmp_path: Path) -> None:
        """Fresh-write latency at 500 / 50 existing partitions stays O(1) (< 8×)."""

        def _time(root: Path, prefill: int) -> float:
            writer = ParquetWriter(lake_root=root)
            _prefill(writer, prefill)
            base = prefill
            # warm
            for j in range(5):
                _write(writer, base + j)
            base += 5
            t0 = time.perf_counter_ns()
            for j in range(_WRITES):
                _write(writer, base + j)
            t1 = time.perf_counter_ns()
            return (t1 - t0) / _WRITES

        t_small = _time(tmp_path / "rs", _SMALL)
        t_large = _time(tmp_path / "rl", _LARGE)
        ratio = t_large / max(t_small, 1.0)
        assert ratio < ratio_ceiling(8.0), (
            f"ParquetWriter.write ratio {ratio:.2f}× (500 vs 50 existing "
            f"partitions) exceeds 8× — the writer may have started scanning the "
            f"lake (O(n_partitions) regression). "
            f"t_50={t_small:.0f}ns, t_500={t_large:.0f}ns."
        )
