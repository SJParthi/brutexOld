"""Catalog stat-cache column migration + the two dedicated token methods.

DEC-SWEEP-RECONCILE-STATCACHE-001. The reconcile stat-cache persists a
partition file's ``(st_mtime_ns, st_size, st_ino)`` freshness token OUTSIDE the
frozen 11-field :class:`~brutex.data.models.CatalogEntry` model and the shared
``upsert``/``get``/``list_partitions`` 11-column SELECTs, via two dedicated
methods (:meth:`Catalog.get_partition_stat_tokens` + :meth:`Catalog.set_partition_stat`)
over three additive ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS`` columns.

Pinned here (together with ``test_catalog_reconcile_statcache.py`` these hold
``brutex/data/catalog.py`` + ``catalog_reconcile.py`` at 100% line + branch):

* a legacy 11-column ``partitions`` table gains the three stat columns on open
  and its pre-existing rows read a ``None`` token (NULL, never a match);
* an empty series / an unstamped row / a stamped row round-trip;
* the closed-catalog guard on both methods;
* a DuckDB fault on the token write is wrapped into ``CatalogError``.
"""
# pyright: reportPrivateUsage=none

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("duckdb")

from brutex.data.catalog import _DDL, _STAT_CACHE_COLUMNS, Catalog
from brutex.data.errors import CatalogError

_TS = datetime(2021, 1, 1, 9, 15, tzinfo=UTC)
_COORDS = {
    "exchange": "NSE",
    "segment": "INDEX",
    "symbol": "NIFTY",
    "interval": "1minute",
}


def _legacy_catalog(path: Path) -> None:
    """Create a legacy 11-column ``partitions`` table (pre-migration) + one row."""
    import duckdb

    conn = duckdb.connect(str(path))
    conn.execute(_DDL)  # the 11-column table — the stat columns are ALTER-added
    conn.execute(
        """
        INSERT INTO partitions
        (exchange, segment, symbol, interval, year, month,
         raw_hash, row_count, first_ts, last_ts, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ["NSE", "INDEX", "NIFTY", "1minute", 2021, 1, "0" * 64, 5, _TS, _TS, _TS],
    )
    conn.close()


class TestMigration:
    def test_open_adds_stat_columns_to_legacy_catalog(self, tmp_path: Path) -> None:
        path = tmp_path / "catalog.duckdb"
        _legacy_catalog(path)
        with Catalog.open(path) as cat:
            cols = {
                row[1]
                for row in cat._conn.execute(  # type: ignore[attr-defined]
                    "PRAGMA table_info('partitions')"
                ).fetchall()
            }
        assert set(_STAT_CACHE_COLUMNS) <= cols

    def test_legacy_row_reads_none_token(self, tmp_path: Path) -> None:
        # A pre-migration row has NULL stat columns → an explicit MISS (None),
        # never a fabricated match (no COALESCE-to-0).
        path = tmp_path / "catalog.duckdb"
        _legacy_catalog(path)
        with Catalog.open(path) as cat:
            tokens = cat.get_partition_stat_tokens(**_COORDS)
        assert tokens == {(2021, 1): None}

    def test_reopen_is_idempotent(self, tmp_path: Path) -> None:
        # ADD COLUMN IF NOT EXISTS runs on every open; re-opening a migrated
        # catalog is a clean no-op.
        path = tmp_path / "catalog.duckdb"
        with Catalog.open(path):
            pass
        with Catalog.open(path) as cat:
            assert cat.get_partition_stat_tokens(**_COORDS) == {}


class TestGetPartitionStatTokens:
    def test_empty_series_is_empty_dict(self, tmp_path: Path) -> None:
        with Catalog.open(tmp_path / "catalog.duckdb") as cat:
            assert cat.get_partition_stat_tokens(**_COORDS) == {}

    def test_closed_raises(self, tmp_path: Path) -> None:
        cat = Catalog.open(tmp_path / "catalog.duckdb")
        cat.close()
        with pytest.raises(CatalogError, match="closed"):
            cat.get_partition_stat_tokens(**_COORDS)


class TestSetPartitionStat:
    def test_roundtrip(self, tmp_path: Path) -> None:
        _legacy_catalog(tmp_path / "catalog.duckdb")
        with Catalog.open(tmp_path / "catalog.duckdb") as cat:
            cat.set_partition_stat(
                **_COORDS,
                year=2021,
                month=1,
                file_mtime_ns=1_700_000_000_123_456_789,
                file_size=4096,
                file_ino=987_654_321,
            )
            tokens = cat.get_partition_stat_tokens(**_COORDS)
        assert tokens == {(2021, 1): (1_700_000_000_123_456_789, 4096, 987_654_321)}

    def test_closed_raises(self, tmp_path: Path) -> None:
        cat = Catalog.open(tmp_path / "catalog.duckdb")
        cat.close()
        with pytest.raises(CatalogError, match="closed"):
            cat.set_partition_stat(
                **_COORDS, year=2021, month=1, file_mtime_ns=1, file_size=1, file_ino=1
            )

    def test_duckdb_fault_is_wrapped(self, tmp_path: Path) -> None:
        # A DuckDB fault during the UPDATE (e.g. ENOSPC) is wrapped into
        # CatalogError so the reconcile best-effort caller can swallow it and
        # recompute next launch — never a raw crash, never a NEW uncaught path.
        class _RaisingConn:
            def execute(self, *args: Any, **kwargs: Any) -> Any:
                raise RuntimeError("simulated disk-full during UPDATE")

        cat = Catalog(conn=_RaisingConn(), path=tmp_path / "catalog.duckdb")
        with pytest.raises(CatalogError, match="stat cache"):
            cat.set_partition_stat(
                **_COORDS, year=2021, month=1, file_mtime_ns=1, file_size=1, file_ino=1
            )
