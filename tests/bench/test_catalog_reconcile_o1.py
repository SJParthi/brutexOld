"""O(1) bench — the lake→catalog reconcile cost is flat PER PARTITION.

:func:`brutex.data.catalog_reconcile.reconcile_catalog_from_lake` does one
bounded unit of work per Parquet partition (glob hit → parse path → read the
``timestamp`` column → ``sha256`` the file → one ``upsert``), so the **per-
partition** cost is O(1) in the partition COUNT. This gate asserts the
amortised per-partition time over a 100-partition lake stays within a small
multiple of the per-partition time over a 10-partition lake — a regression to
super-linear behaviour (e.g. re-globbing or re-scanning the whole lake per
partition) would blow the ratio.

Honest scope (Golden Rule #10): a full reconcile is inherently **O(P)** over P
partitions — this gates the per-partition cost in P, NOT an O(1)-total claim,
which is physically impossible for a P-partition scan.

The reconciler needs the ``[data]`` extra (polars + duckdb); the CI bench job
installs only ``[dev]``, so this module ``importorskip``s those FIRST and
imports the data-layer symbols lazily inside the helpers — collection never
fails when ``[data]`` is absent, it just skips (mirrors the other data-backed
benches).
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from tests.bench._o1_tiers import ratio_ceiling

pytest.importorskip("duckdb")
pytest.importorskip("polars")

_SMALL = 10
_LARGE = 100


def _build_lake(root: Path, n_partitions: int) -> None:
    """Write *n_partitions* one-bar partitions across distinct months/years."""
    from datetime import UTC, datetime
    from decimal import Decimal

    from brutex.data.models import CandleBar
    from brutex.data.parquet_writer import ParquetWriter

    writer = ParquetWriter(lake_root=root)
    for idx in range(n_partitions):
        year = 2010 + idx // 12
        month = 1 + idx % 12
        writer.write(
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            interval="1minute",
            bars=[
                CandleBar(
                    timestamp=datetime(year, month, 1, 9, 15, tzinfo=UTC),
                    open=Decimal("100"),
                    high=Decimal("110"),
                    low=Decimal("90"),
                    close=Decimal("105"),
                    volume=1,
                    open_interest=None,
                )
            ],
        )


def _per_partition_ns(tmp: Path, n: int) -> float:
    from brutex.data.catalog import Catalog
    from brutex.data.catalog_reconcile import reconcile_catalog_from_lake

    lake = tmp / f"lake_{n}"
    lake.mkdir()
    _build_lake(lake, n)
    with Catalog.open(lake / "catalog.duckdb") as cat:
        best = float("inf")
        for _ in range(3):
            t0 = time.perf_counter_ns()
            reconcile_catalog_from_lake(lake_root=lake, catalog=cat)
            best = min(best, (time.perf_counter_ns() - t0) / n)
    return best


def test_reconcile_per_partition_is_o1_in_partition_count(tmp_path: Path) -> None:
    t_small = _per_partition_ns(tmp_path, _SMALL)
    t_large = _per_partition_ns(tmp_path, _LARGE)
    ratio = t_large / max(t_small, 1.0)
    assert ratio < ratio_ceiling(3.0), (
        f"reconcile per-partition time ratio {ratio:.2f}× (P=100 vs P=10) "
        f"exceeds the O(1)-per-partition ceiling — reconcile may have "
        f"regressed to super-linear work. "
        f"t_10={t_small:.0f}ns, t_100={t_large:.0f}ns."
    )
