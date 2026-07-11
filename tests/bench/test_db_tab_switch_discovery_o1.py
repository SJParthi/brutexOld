"""O(1)-on-repeat benchmark for /db candle-dataset discovery (DEC-O1-BENCH-GAP-CLOSURE-001).

Gates the load-bearing claim behind DEC-DB-TAB-SWITCH-O1-001 (#896) and its
manifest-first strengthening (DEC-LAKE-MANIFEST-O1-001 / DEC-O1-CLAIMS-HONESTY-001):
a repeat ``/db`` candle-tab discovery is served in **O(dataset-rows), independent of
the lake's partition-FILE count**. The measured 2.36M-file post-prebuild lake paid a
~29.7-minute O(files) walk on the FIRST discovery; every discovery after that serves
the byte-identical dataset list from the durable write-time manifest in constant time
w.r.t. the file count.

The gate builds two real synthetic bars lakes with the SAME dataset row count but a
very different partition-FILE count (1 file/dataset vs many files/dataset). The first
:func:`~brutex.dashboard.db_view.list_candle_datasets` call over each walks the tree
AND rebuilds the manifest; every subsequent call is manifest-served. It then times
the repeat (manifest-served) discovery and asserts the latency is flat vs the
partition-file count under ``ratio_ceiling(5.0)`` (``tests/bench/_o1_tiers.py``) — a
regression back to an O(files) walk per discovery would scale with the file count and
blow past the ceiling.

This exercises the REAL production discovery (``list_candle_datasets`` +
``lake_manifest``), not the ``MoversSnapshotCache`` route memo alone (which
``tests/bench/test_movers_cache_o1.py`` already pins) — so it catches a regression
in the manifest layer that a pure memo-hit bench would miss.

HONEST envelope (Golden Rule #10): the FIRST discovery over a fresh lake still pays
the O(files) walk (this bench primes it out); the manifest read is
O(dataset-rows), not a literal O(1)-total. Best-of-N-MIN timing (DEC-CANARY-MINTIME-001).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

pytest.importorskip("polars")
import polars as pl
from brutex.dashboard.db_view import list_candle_datasets

from tests.bench._o1_tiers import ratio_ceiling

_N_DATASETS = 30


def _one_bar_frame() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "timestamp": [datetime(2024, 1, 1, 3, 45, tzinfo=UTC)],
            "open": [1.0],
            "high": [1.0],
            "low": [1.0],
            "close": [1.0],
            "volume": [1],
            "open_interest": [None],
        }
    )


def _build_bars_lake(root: Path, files_per_dataset: int) -> None:
    """Write ``_N_DATASETS`` spot datasets, each with *files_per_dataset* partition files."""
    frame = _one_bar_frame()
    for s in range(_N_DATASETS):
        base = root / "bars" / "NSE" / "CASH" / f"SYM{s:04d}" / "1minute" / "2024"
        base.mkdir(parents=True, exist_ok=True)
        for f in range(files_per_dataset):
            frame.write_parquet(str(base / f"{f + 1:02d}.parquet"))


def _primed_lake(root: Path, files_per_dataset: int) -> None:
    """Build a lake and pay the first walk (which rebuilds the manifest)."""
    _build_bars_lake(root, files_per_dataset)
    rows = list_candle_datasets(root)  # first: O(files) walk + manifest rebuild
    assert len(rows) == _N_DATASETS


class TestRepeatDiscoveryIsO1VsFileCount:
    """A repeat (manifest-served) discovery is flat vs the partition-file count."""

    def test_manifest_served_rows_are_correct(self, tmp_path: Path) -> None:
        _primed_lake(tmp_path, 4)
        rows = list_candle_datasets(tmp_path)  # manifest-served
        assert len(rows) == _N_DATASETS
        assert rows[0]["segment"] == "CASH"
        assert rows[0]["interval"] == "1minute"

    def test_repeat_discovery_flat_vs_partition_files(
        self, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        few_dir = tmp_path
        many_dir = tmp_path_factory.mktemp("many_files_lake")
        _primed_lake(few_dir, 1)  # 30 partition files total
        _primed_lake(many_dir, 150)  # 4500 partition files total (same 30 datasets)

        def _per_call(root: Path, iterations: int) -> float:
            best = float("inf")
            for _ in range(5):  # best-of-N-MIN (DEC-CANARY-MINTIME-001)
                t0 = time.perf_counter_ns()
                for _ in range(iterations):
                    list_candle_datasets(root)
                best = min(best, (time.perf_counter_ns() - t0) / iterations)
            return best

        _per_call(few_dir, 20)  # warm
        few = _per_call(few_dir, 50)
        many = _per_call(many_dir, 50)
        ratio = many / max(few, 1.0)
        assert ratio < ratio_ceiling(5.0), (
            f"repeat /db discovery ratio {ratio:.2f}× (4500 vs 30 partition files, same 30 "
            f"datasets) exceeds the 5× CI ceiling — discovery may have regressed to a "
            f"per-poll O(files) tree walk. few={few:.0f}ns many={many:.0f}ns."
        )
