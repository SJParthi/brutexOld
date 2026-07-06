"""Shared lake-poison test fixtures (DEC-LAKE-HEAL-001 / DEC-CLI-LAKE-HEAL-L2-001).

The ONE home for the raw-pyarrow partition poisoner + the stored-timestamp
reader the L2 heal-seam suites share (CLAUDE.md §11 COMMON — moved verbatim
from ``tests/integration/test_lake_heal_pull_seam.py``, which now imports
them, so the dashboard-seam and CLI-seam suites can never drift apart).
Poison is planted with raw pyarrow (the #1067 convention — the fixed writer
refuses duplicates, so a poisoned partition can only be fabricated below the
production write path).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path


def poison_partition(dest: Path, rows: Sequence[tuple[datetime, float, int]]) -> None:
    """Write a RAW pyarrow partition with VERBATIM duplicate timestamps.

    Parameters
    ----------
    dest
        The partition file to (over)write; parent directories are created.
    rows
        ``(timestamp, price, volume)`` triples written verbatim — including
        any duplicate timestamps the production writer would refuse. The one
        price is stamped into all four OHLC columns; ``open_interest`` is
        null (a spot partition).
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    dest.parent.mkdir(parents=True, exist_ok=True)
    prices = [price for _, price, _ in rows]
    table = pa.table(
        {
            "timestamp": pa.array([ts for ts, _, _ in rows], type=pa.timestamp("us", tz="UTC")),
            "open": pa.array(prices, type=pa.float64()),
            "high": pa.array(prices, type=pa.float64()),
            "low": pa.array(prices, type=pa.float64()),
            "close": pa.array(prices, type=pa.float64()),
            "volume": pa.array([vol for _, _, vol in rows], type=pa.int64()),
            "open_interest": pa.array([None] * len(rows), type=pa.int64()),
        }
    )
    pq.write_table(table, dest)


def stored_timestamps(dest: Path) -> list[datetime]:
    """Return the ``timestamp`` column of the partition at *dest*, in file order.

    Parameters
    ----------
    dest
        The Parquet partition file to read.

    Returns
    -------
    list[datetime]
        The stored timestamps — the keep-FIRST heal oracle the seam suites
        assert against.
    """
    import polars as pl

    return list(pl.read_parquet(dest, columns=["timestamp"])["timestamp"])
