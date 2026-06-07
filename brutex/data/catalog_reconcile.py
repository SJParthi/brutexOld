"""Rebuild the DuckDB catalog ``partitions`` table from the on-disk Parquet lake.

DEC-CATALOG-RECONCILE-WIRE-001. The production ingest sinks historically
constructed :class:`~brutex.data.sink.ParquetBarSink` WITHOUT a catalog (the
``catalog=None`` default), so a real ``fetch`` / dashboard pull wrote Parquet
partitions to disk but never recorded them in the DuckDB ``partitions`` table.
:meth:`~brutex.data.catalog.Catalog.get_bars` reads bars *only* through that
table, so ``brutex sweep-all`` found zero bars after any real pull.

This module is the READ-side safety net + recovery tool: it scans the lake's
Parquet partition tree (the source of truth on disk) and upserts one
:class:`~brutex.data.models.CatalogEntry` per partition, so an existing lake
becomes sweep-able with **no re-pull**. The write-side fix (wiring a catalog
into the sinks) makes *future* pulls self-register; this reconciler repairs
*already-pulled* lakes and is invoked automatically at sweep start.

Honest envelope (CLAUDE.md Golden Rule #10): reconciliation is **O(P) one-time
work** over the P partition files of the requested coordinates — each file is
opened once to read only its ``timestamp`` column (O(rows-in-file) I/O) plus an
O(1) ``sha256`` over the file bytes. It is NOT in the per-combo sweep hot loop
and makes no O(1)-total claim. It is additive (upsert-only): partitions whose
Parquet file was deleted out from under the lake are left as stale catalog rows
(this append-only lake does not delete partitions); callers reading a missing
file get the catalog's existing missing-file error, unchanged.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import polars as pl

from brutex.data.catalog import Catalog
from brutex.data.errors import PartitionError
from brutex.data.models import CatalogEntry
from brutex.data.partition import BAR_DIR_NAME, parse_partition_path

__all__ = ["ReconcileResult", "reconcile_catalog_from_lake"]


@dataclass(frozen=True)
class ReconcileResult:
    """Outcome of a :func:`reconcile_catalog_from_lake` pass.

    Attributes
    ----------
    partitions_reconciled
        Number of Parquet partitions upserted into the catalog.
    bars_total
        Total bars across every reconciled partition.
    skipped
        Partition files skipped because they could not be parsed as a
        valid partition path or held zero rows.
    """

    partitions_reconciled: int
    bars_total: int
    skipped: int


def _partition_files(
    lake_root: Path,
    *,
    exchange: str | None,
    segment: str | None,
    symbol: str | None,
    interval: str | None,
) -> list[Path]:
    """Return every ``<year>/<month>.parquet`` partition file matching the coords.

    ``None`` for a coordinate means "any value" (a ``*`` glob segment). The
    six-segment pattern (``exchange/segment/symbol/interval/year/month.parquet``)
    matches exactly the spot bar layout under ``bars/`` and never the option
    lake's deeper tree, so reconcile only ever touches index/spot partitions.
    """
    base = lake_root / BAR_DIR_NAME
    if not base.exists():
        return []
    pattern = f"{exchange or '*'}/{segment or '*'}/{symbol or '*'}/{interval or '*'}/*/*.parquet"
    return sorted(base.glob(pattern))


def reconcile_catalog_from_lake(
    *,
    lake_root: Path,
    catalog: Catalog,
    exchange: str | None = None,
    segment: str | None = None,
    symbol: str | None = None,
    interval: str | None = None,
) -> ReconcileResult:
    """Upsert one catalog row per on-disk Parquet partition under *lake_root*.

    Scans ``<lake_root>/bars/<exchange>/<segment>/<symbol>/<interval>/<year>/
    <month>.parquet`` (each unset coordinate widening to a ``*`` glob) and, for
    every partition file, records a :class:`~brutex.data.models.CatalogEntry`
    via :meth:`~brutex.data.catalog.Catalog.upsert`. ``raw_hash`` is the sha256
    of the Parquet file bytes — bit-identical to what
    :class:`~brutex.data.parquet_writer.ParquetWriter` records at write time, so
    reconciling an already-ingested partition is a no-op upsert. ``fetched_at``
    is the file's modification time (UTC) so repeated reconciles are
    deterministic.

    Parameters
    ----------
    lake_root
        Top-level lake directory (the parent of ``bars/``).
    catalog
        Open :class:`~brutex.data.catalog.Catalog` to upsert into. The
        catalog's internal lock serialises the writes.
    exchange, segment, symbol, interval
        Optional coordinate filters; ``None`` reconciles every value for
        that coordinate.

    Returns
    -------
    ReconcileResult
        Counts of reconciled partitions, total bars, and skipped files.
    """
    files = _partition_files(
        lake_root,
        exchange=exchange,
        segment=segment,
        symbol=symbol,
        interval=interval,
    )
    reconciled = 0
    bars_total = 0
    skipped = 0
    for path in files:
        try:
            coords = parse_partition_path(path, lake_root=lake_root)
        except PartitionError:
            skipped += 1
            continue
        timestamps = pl.read_parquet(path, columns=["timestamp"]).get_column("timestamp")
        row_count = timestamps.len()
        if row_count == 0:
            skipped += 1
            continue
        first_ts = timestamps.min()
        last_ts = timestamps.max()
        entry = CatalogEntry(
            exchange=cast(str, coords["exchange"]),
            segment=cast(str, coords["segment"]),
            symbol=cast(str, coords["symbol"]),
            interval=cast(str, coords["interval"]),
            year=cast(int, coords["year"]),
            month=cast(int, coords["month"]),
            raw_hash=hashlib.sha256(path.read_bytes()).hexdigest(),
            row_count=row_count,
            first_ts=_as_utc(first_ts),
            last_ts=_as_utc(last_ts),
            fetched_at=datetime.fromtimestamp(path.stat().st_mtime, tz=UTC),
        )
        catalog.upsert(entry)
        reconciled += 1
        bars_total += row_count
    return ReconcileResult(
        partitions_reconciled=reconciled,
        bars_total=bars_total,
        skipped=skipped,
    )


def _as_utc(value: object) -> datetime:
    """Coerce a polars min/max timestamp to a timezone-aware UTC datetime.

    Bars are stored as ``timestamp[us, tz=UTC]`` so polars already yields a
    tz-aware ``datetime``; a tz-naive value (defensive) is stamped UTC.
    """
    if not isinstance(value, datetime):  # pragma: no cover — schema guarantees datetime
        raise PartitionError(f"timestamp column yielded a non-datetime: {value!r}")
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
