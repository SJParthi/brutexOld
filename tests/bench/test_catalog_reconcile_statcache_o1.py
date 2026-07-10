"""Bench — the WARM (2nd-launch) reconcile is stat-only: O(files) stats, O(bytes)-free.

DEC-SWEEP-RECONCILE-STATCACHE-001. The ``brutex sweep-all`` launcher reconciles
with ``stat_cache=True``, so a byte-unchanged lake's SECOND launch skips the
full-bytes ``sha256`` + timestamp re-read of every partition on a stat-only
``(mtime_ns, size, ino)`` token match. Two gates pin the honest envelope:

1. the warm per-partition cost is flat vs the partition COUNT (the ``stat`` set
   is O(files), never re-globbing/re-scanning the whole lake per partition);
2. the warm cost is INVARIANT to the per-file bar COUNT (bytes) — it does not
   read the parquet bytes at all, so a heavy-bar lake warms in the same time as
   a one-bar lake (the sha256-avoidance the whole change turns on).

Honest scope (Golden Rule #10): the COLD first launch stays O(P) over P
partitions (its own gate is ``test_catalog_reconcile_o1.py``); this gates the
WARM launch only. Not an O(1)-total claim.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from tests.bench._o1_tiers import cache_tier_ceiling, ratio_ceiling

pytest.importorskip("duckdb")
pytest.importorskip("polars")

_SMALL = 10
_LARGE = 100


def _build_lake(root: Path, *, n_partitions: int, bars_per_file: int) -> None:
    from datetime import UTC, datetime, timedelta
    from decimal import Decimal

    from brutex.data.models import CandleBar
    from brutex.data.parquet_writer import ParquetWriter

    writer = ParquetWriter(lake_root=root)
    for idx in range(n_partitions):
        year = 2005 + idx // 12
        month = 1 + idx % 12
        base = datetime(year, month, 1, 9, 15, tzinfo=UTC)
        bars = [
            CandleBar(
                timestamp=base + timedelta(minutes=j),
                open=Decimal("100"),
                high=Decimal("110"),
                low=Decimal("90"),
                close=Decimal("105"),
                volume=1,
                open_interest=None,
            )
            for j in range(bars_per_file)
        ]
        writer.write(exchange="NSE", segment="INDEX", symbol="NIFTY", interval="1minute", bars=bars)


def _warm_per_partition_ns(tmp: Path, *, n: int, bars_per_file: int) -> float:
    from brutex.data.catalog import Catalog
    from brutex.data.catalog_reconcile import reconcile_catalog_from_lake

    lake = tmp / f"lake_{n}_{bars_per_file}"
    lake.mkdir()
    _build_lake(lake, n_partitions=n, bars_per_file=bars_per_file)
    with Catalog.open(lake / "catalog.duckdb") as cat:
        # Cold pass warms the durable stat tokens (not timed).
        cold = reconcile_catalog_from_lake(lake_root=lake, catalog=cat, stat_cache=True)
        assert cold.partitions_reconciled == n and cold.stat_cache_hits == 0
        # The whole point: the 2nd launch is stat-only — zero reconciles.
        warm = reconcile_catalog_from_lake(lake_root=lake, catalog=cat, stat_cache=True)
        assert warm.partitions_reconciled == 0 and warm.stat_cache_hits == n
        best = float("inf")
        for _ in range(3):
            t0 = time.perf_counter_ns()
            reconcile_catalog_from_lake(lake_root=lake, catalog=cat, stat_cache=True)
            best = min(best, (time.perf_counter_ns() - t0) / n)
    return best


def test_warm_per_partition_is_flat_in_partition_count(tmp_path: Path) -> None:
    t_small = _warm_per_partition_ns(tmp_path, n=_SMALL, bars_per_file=1)
    t_large = _warm_per_partition_ns(tmp_path, n=_LARGE, bars_per_file=1)
    ratio = t_large / max(t_small, 1.0)
    assert ratio < ratio_ceiling(3.0), (
        f"warm reconcile per-partition ratio {ratio:.2f}× (P=100 vs P=10) exceeds "
        f"the O(1)-per-partition ceiling — the stat-only path may have regressed. "
        f"t_10={t_small:.0f}ns, t_100={t_large:.0f}ns."
    )


def test_warm_cost_is_invariant_to_bar_count(tmp_path: Path) -> None:
    # Same partition COUNT, wildly different per-file byte size: the warm path
    # never opens the parquet bytes, so the times track within a cache-tier
    # allowance (a regression that re-read/re-hashed would scale with bytes).
    t_light = _warm_per_partition_ns(tmp_path, n=_SMALL, bars_per_file=1)
    t_heavy = _warm_per_partition_ns(tmp_path, n=_SMALL, bars_per_file=200)
    ratio = max(t_light, t_heavy) / max(min(t_light, t_heavy), 1.0)
    assert ratio < cache_tier_ceiling(3.0), (
        f"warm reconcile cost ratio {ratio:.2f}× (200 bars/file vs 1 bar/file) "
        f"exceeds the O(bytes)-free ceiling — the warm path may be reading file "
        f"bytes. t_light={t_light:.0f}ns, t_heavy={t_heavy:.0f}ns."
    )
