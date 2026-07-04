"""Edge-case + defensive-path coverage for :mod:`brutex.data.catalog`.

The happy paths of :class:`~brutex.data.catalog.Catalog` (upsert / get /
count / get_bars / data_snapshot_hash over a real Parquet+DuckDB lake)
are exercised by :mod:`tests.integration.test_data_lake_real`. This
companion module drives the remaining guard / error / defensive branches
that the end-to-end suite never reaches, lifting ``catalog.py`` to
**100% line + branch** so it can be un-omitted from the coverage gate and
locked by a strict per-module gate (DEC-COVERAGE-CATALOG-001, mirroring
DEC-COVERAGE-DRIVER-001).

Covered branches (measured ``--cov`` miss list before this file):

* ``open()`` ``ImportError`` when ``duckdb`` is absent (77-78)
* ``open()`` ``require_exists=True`` missing-path refusal (112-115,
  DEC-CLI-USER-ERROR-HYGIENE-001 — the READ-ONLY caller guard)
* the :pyattr:`Catalog.path` property getter (94)
* the ``upsert`` ``BEGIN → … → ROLLBACK → raise`` rollback path (145-147)
* the ``list_partitions`` closed-catalog guard (209)
* the ``start is None`` / ``end is None`` clause-skip partials (222->225,
  225->234)
* the ``get_bars`` closed-catalog guard (314) and its ``polars`` /
  ``pyarrow`` ``ImportError`` path (323-324)
* the ``data_snapshot_hash`` closed-catalog guard (407) and inverted-window
  guard (411)
* the :func:`~brutex.data.catalog.open_catalog` context-manager sugar
  (462-466)
* the :func:`_require_tz_aware` naive-datetime guard (472)

These tests need only ``duckdb`` (for a real on-disk catalog) plus light
fakes / ``sys.modules`` monkeypatching; ``polars`` / ``pyarrow`` are
deliberately *not* imported at module scope so the absent-dependency path
can be simulated.
"""

from __future__ import annotations

import hashlib
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from brutex.data.catalog import Catalog, open_catalog
from brutex.data.errors import CatalogError
from brutex.data.models import CatalogEntry

# A real DuckDB catalog underpins most of these tests; skip the module
# when the P2 [data] extra is missing (same guard as test_data_lake_real).
pytest.importorskip("duckdb")


# ────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────


def _entry(*, symbol: str = "NSE-NIFTY", month: int = 1, year: int = 2024) -> CatalogEntry:
    """Build a deterministic :class:`CatalogEntry` for the given coordinates."""
    return CatalogEntry(
        exchange="NSE",
        segment="FNO",
        symbol=symbol,
        interval="1minute",
        year=year,
        month=month,
        raw_hash=hashlib.sha256(f"{symbol}:{year}:{month}".encode()).hexdigest(),
        row_count=42,
        first_ts=datetime(year, month, 1, 9, 15, tzinfo=UTC),
        last_ts=datetime(year, month, 28, 15, 30, tzinfo=UTC),
        fetched_at=datetime(year, 1, 1, 12, tzinfo=UTC),
    )


class _RollbackConn:
    """Fake DuckDB connection whose ``INSERT`` fails after ``BEGIN``/``DELETE``.

    Used to drive :meth:`Catalog.upsert`'s ``except → ROLLBACK → raise``
    path without corrupting a real database. Every statement is recorded
    in :pyattr:`calls` so the test can assert that ``ROLLBACK`` ran.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def execute(self, sql: str, params: object = None) -> _RollbackConn:
        """Record *sql* and raise on the ``INSERT`` statement only."""
        verb = sql.strip().split(None, 1)[0].upper() if sql.strip() else sql
        self.calls.append(verb)
        if "INSERT" in sql.upper():
            raise RuntimeError("simulated INSERT failure")
        return self


# ────────────────────────────────────────────────────────────────────────
# Catalog.open — missing-duckdb guard (77-78)
# ────────────────────────────────────────────────────────────────────────


class TestOpenWithoutDuckDB:
    def test_open_raises_catalog_error_when_duckdb_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``Catalog.open`` surfaces a friendly ``CatalogError`` if duckdb is missing."""
        # A ``None`` entry in sys.modules makes ``import duckdb`` raise
        # ImportError — simulating the [data] extra not being installed.
        monkeypatch.setitem(sys.modules, "duckdb", None)
        with pytest.raises(CatalogError, match="duckdb"):
            Catalog.open(tmp_path / "catalog.duckdb")


# ────────────────────────────────────────────────────────────────────────
# Catalog.open — require_exists guard (DEC-CLI-USER-ERROR-HYGIENE-001)
# ────────────────────────────────────────────────────────────────────────


class TestOpenRequireExists:
    """The READ-ONLY caller guard: never conjure a fresh catalog from a typo."""

    def test_missing_path_refused_when_require_exists(self, tmp_path: Path) -> None:
        """``require_exists=True`` on an absent path raises instead of creating."""
        missing = tmp_path / "nope" / "catalog.duckdb"
        with pytest.raises(CatalogError, match="catalog not found"):
            Catalog.open(missing, require_exists=True)
        # The guard must refuse WITHOUT side effects — no file, no parent dir.
        assert not missing.exists()
        assert not missing.parent.exists()

    def test_existing_path_opens_when_require_exists(self, tmp_path: Path) -> None:
        """``require_exists=True`` on a real catalog opens it normally."""
        db_path = tmp_path / "catalog.duckdb"
        with Catalog.open(db_path) as cat:  # create first (default behaviour)
            cat.upsert(_entry())
        with Catalog.open(db_path, require_exists=True) as reopened:
            assert reopened.count() == 1


# ────────────────────────────────────────────────────────────────────────
# Catalog.path property getter (94)
# ────────────────────────────────────────────────────────────────────────


class TestPathProperty:
    def test_path_returns_on_disk_location(self, tmp_path: Path) -> None:
        """The ``path`` property echoes the file the catalog was opened at."""
        db_path = tmp_path / "nested" / "catalog.duckdb"
        with Catalog.open(db_path) as cat:
            assert cat.path == db_path


# ────────────────────────────────────────────────────────────────────────
# Catalog.upsert — transactional rollback (145-147)
# ────────────────────────────────────────────────────────────────────────


class TestUpsertRollback:
    def test_failed_insert_rolls_back_and_reraises(self, tmp_path: Path) -> None:
        """A mid-transaction failure rolls back and propagates the error."""
        conn = _RollbackConn()
        cat = Catalog(conn=conn, path=tmp_path / "catalog.duckdb")
        with pytest.raises(RuntimeError, match="simulated INSERT failure"):
            cat.upsert(_entry())
        # The transaction must have been opened, the insert attempted, and
        # then explicitly rolled back (never committed).
        assert conn.calls == ["BEGIN", "DELETE", "INSERT", "ROLLBACK"]
        assert "COMMIT" not in conn.calls


# ────────────────────────────────────────────────────────────────────────
# Catalog.list_partitions — closed guard, unbounded window, naive tz
# (209, 222->225, 225->234, 472)
# ────────────────────────────────────────────────────────────────────────


class TestListPartitions:
    def test_closed_catalog_raises(self, tmp_path: Path) -> None:
        """``list_partitions`` rejects a closed catalog (209)."""
        cat = Catalog.open(tmp_path / "catalog.duckdb")
        cat.close()
        with pytest.raises(CatalogError, match="closed"):
            cat.list_partitions(
                exchange="NSE", segment="FNO", symbol="NSE-NIFTY", interval="1minute"
            )

    def test_unbounded_window_skips_both_clauses(self, tmp_path: Path) -> None:
        """``start=None`` and ``end=None`` skip both range clauses (222->225, 225->234)."""
        with Catalog.open(tmp_path / "catalog.duckdb") as cat:
            cat.upsert(_entry(month=1))
            cat.upsert(_entry(month=3))
            cat.upsert(_entry(month=2))
            rows = cat.list_partitions(
                exchange="NSE",
                segment="FNO",
                symbol="NSE-NIFTY",
                interval="1minute",
                start=None,
                end=None,
            )
        # Every partition returned, sorted ascending by (year, month).
        assert [r.month for r in rows] == [1, 2, 3]

    def test_start_only_keeps_lower_bound(self, tmp_path: Path) -> None:
        """A lower bound alone (``end=None``) filters on ``last_ts >= start``."""
        with Catalog.open(tmp_path / "catalog.duckdb") as cat:
            cat.upsert(_entry(month=1))
            cat.upsert(_entry(month=6))
            rows = cat.list_partitions(
                exchange="NSE",
                segment="FNO",
                symbol="NSE-NIFTY",
                interval="1minute",
                start=datetime(2024, 5, 1, tzinfo=UTC),
                end=None,
            )
        assert [r.month for r in rows] == [6]

    def test_end_only_keeps_upper_bound(self, tmp_path: Path) -> None:
        """An upper bound alone (``start=None``) filters on ``first_ts <= end``."""
        with Catalog.open(tmp_path / "catalog.duckdb") as cat:
            cat.upsert(_entry(month=1))
            cat.upsert(_entry(month=6))
            rows = cat.list_partitions(
                exchange="NSE",
                segment="FNO",
                symbol="NSE-NIFTY",
                interval="1minute",
                start=None,
                end=datetime(2024, 2, 1, tzinfo=UTC),
            )
        assert [r.month for r in rows] == [1]

    def test_naive_start_rejected(self, tmp_path: Path) -> None:
        """A naive ``start`` trips the tz-aware guard (472)."""
        with (
            Catalog.open(tmp_path / "catalog.duckdb") as cat,
            pytest.raises(CatalogError, match="timezone-aware"),
        ):
            cat.list_partitions(
                exchange="NSE",
                segment="FNO",
                symbol="NSE-NIFTY",
                interval="1minute",
                start=datetime(2024, 1, 1),  # naive — no tzinfo
            )


# ────────────────────────────────────────────────────────────────────────
# Catalog.get_bars — closed guard (314) + missing polars/pyarrow (323-324)
# ────────────────────────────────────────────────────────────────────────


class TestGetBarsGuards:
    def test_closed_catalog_raises(self, tmp_path: Path) -> None:
        """``get_bars`` rejects a closed catalog before any I/O (314)."""
        cat = Catalog.open(tmp_path / "catalog.duckdb")
        cat.close()
        with pytest.raises(CatalogError, match="closed"):
            cat.get_bars(
                lake_root=tmp_path / "lake",
                exchange="NSE",
                segment="FNO",
                symbol="NSE-NIFTY",
                interval="1minute",
                start=datetime(2024, 1, 1, tzinfo=UTC),
                end=datetime(2024, 1, 31, tzinfo=UTC),
            )

    def test_missing_polars_raises_friendly_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``get_bars`` surfaces a ``CatalogError`` when polars/pyarrow are absent."""
        # Pass the closed/tz/window guards, then force the lazy import to fail.
        monkeypatch.setitem(sys.modules, "polars", None)
        with (
            Catalog.open(tmp_path / "catalog.duckdb") as cat,
            pytest.raises(CatalogError, match="polars"),
        ):
            cat.get_bars(
                lake_root=tmp_path / "lake",
                exchange="NSE",
                segment="FNO",
                symbol="NSE-NIFTY",
                interval="1minute",
                start=datetime(2024, 1, 1, tzinfo=UTC),
                end=datetime(2024, 1, 31, tzinfo=UTC),
            )


# ────────────────────────────────────────────────────────────────────────
# Catalog.data_snapshot_hash — closed guard (407) + inverted window (411)
# ────────────────────────────────────────────────────────────────────────


class TestDataSnapshotHashGuards:
    def test_closed_catalog_raises(self, tmp_path: Path) -> None:
        """``data_snapshot_hash`` rejects a closed catalog (407)."""
        cat = Catalog.open(tmp_path / "catalog.duckdb")
        cat.close()
        with pytest.raises(CatalogError, match="closed"):
            cat.data_snapshot_hash(
                exchange="NSE",
                segment="FNO",
                symbols=["NSE-NIFTY"],
                interval="1minute",
                start=datetime(2024, 1, 1, tzinfo=UTC),
                end=datetime(2024, 1, 31, tzinfo=UTC),
            )

    def test_inverted_window_rejected(self, tmp_path: Path) -> None:
        """``end < start`` is rejected with a ``>=`` message (411)."""
        with (
            Catalog.open(tmp_path / "catalog.duckdb") as cat,
            pytest.raises(CatalogError, match=">="),
        ):
            cat.data_snapshot_hash(
                exchange="NSE",
                segment="FNO",
                symbols=["NSE-NIFTY"],
                interval="1minute",
                start=datetime(2024, 12, 31, tzinfo=UTC),
                end=datetime(2024, 1, 1, tzinfo=UTC),
            )


# ────────────────────────────────────────────────────────────────────────
# open_catalog context-manager sugar (462-466)
# ────────────────────────────────────────────────────────────────────────


class TestOpenCatalogContextManager:
    def test_yields_usable_catalog_and_closes_on_exit(self, tmp_path: Path) -> None:
        """``open_catalog`` yields a live catalog and closes it on block exit."""
        db_path = tmp_path / "catalog.duckdb"
        with open_catalog(db_path) as cat:
            cat.upsert(_entry())
            assert cat.count() == 1
            captured = cat
        # The finally-clause must have closed the catalog: any operation now
        # raises the closed-guard error.
        with pytest.raises(CatalogError, match="closed"):
            captured.count()


# ────────────────────────────────────────────────────────────────────────
# delete_partition (DEC-LAKE-HEAL-001)
# ────────────────────────────────────────────────────────────────────────


class TestDeletePartition:
    """``Catalog.delete_partition`` — the healer's dangling-row eraser."""

    def test_delete_partition_removes_row_and_is_idempotent(self, tmp_path: Path) -> None:
        """A present row deletes (True); a repeat delete is a False no-op; the
        sibling row survives (a heal that quarantine-MOVES one file never
        touches the others' rows)."""
        with Catalog.open(tmp_path / "catalog.duckdb") as cat:
            cat.upsert(_entry(month=1))
            cat.upsert(_entry(month=2))
            assert cat.count() == 2
            deleted = cat.delete_partition(
                exchange="NSE",
                segment="FNO",
                symbol="NSE-NIFTY",
                interval="1minute",
                year=2024,
                month=1,
            )
            assert deleted is True
            assert cat.count() == 1
            assert (
                cat.get(
                    exchange="NSE",
                    segment="FNO",
                    symbol="NSE-NIFTY",
                    interval="1minute",
                    year=2024,
                    month=1,
                )
                is None
            )
            again = cat.delete_partition(
                exchange="NSE",
                segment="FNO",
                symbol="NSE-NIFTY",
                interval="1minute",
                year=2024,
                month=1,
            )
            assert again is False
            assert cat.count() == 1  # the February row is untouched

    def test_delete_partition_closed_catalog_raises(self, tmp_path: Path) -> None:
        cat = Catalog.open(tmp_path / "catalog.duckdb")
        cat.close()
        with pytest.raises(CatalogError, match="closed"):
            cat.delete_partition(
                exchange="NSE",
                segment="FNO",
                symbol="NSE-NIFTY",
                interval="1minute",
                year=2024,
                month=1,
            )


# ────────────────────────────────────────────────────────────────────────
# Real-lake happy paths — get_bars / data_snapshot_hash / closed guards
# (DEC-LAKE-READ-UNIQUE-001: the per-touched-file coverage gate maps
# catalog.py to THIS stem suite, so the happy paths formerly exercised only
# by test_data_lake_real.py are covered here too — self-sufficient at 100%.)
# ────────────────────────────────────────────────────────────────────────


def _write_month_partition(
    lake_root: Path,
    *,
    entry: CatalogEntry,
    closes: list[float],
    duplicate_first_minute: bool = False,
) -> None:
    """Write the entry's month Parquet directly (polars — bypasses the writer).

    One 1-minute bar per element of *closes*, starting at the entry's
    ``first_ts``. With ``duplicate_first_minute`` a CONFLICTING duplicate of
    the first instant is appended LAST (a poisoned-era partition — the fixed
    writer refuses to store one, so the poison is planted raw).
    """
    import polars as pl
    from brutex.data.partition import build_partition_path

    stamps = [entry.first_ts + timedelta(minutes=i) for i in range(len(closes))]
    prices = list(closes)
    if duplicate_first_minute:
        stamps.append(entry.first_ts)
        prices.append(999_999.0)
    dest = build_partition_path(
        lake_root=lake_root,
        exchange=entry.exchange,
        segment=entry.segment,
        symbol=entry.symbol,
        interval=entry.interval,
        year=entry.year,
        month=entry.month,
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(
        {
            "timestamp": stamps,
            "open": prices,
            "high": [p + 1 for p in prices],
            "low": [p - 1 for p in prices],
            "close": prices,
            "volume": [10] * len(prices),
            "open_interest": [None] * len(prices),
        }
    ).with_columns(pl.col("open_interest").cast(pl.Int64)).write_parquet(dest)


class TestGetBarsRealLake:
    """``get_bars`` over a real Parquet lake — happy path + the read dedup gate."""

    def test_happy_path_returns_sorted_window(self, tmp_path: Path) -> None:
        entry = _entry(month=1, year=2024)
        _write_month_partition(tmp_path, entry=entry, closes=[100.0, 101.0, 102.0])
        with Catalog.open(tmp_path / "catalog.duckdb") as cat:
            cat.upsert(entry)
            table = cat.get_bars(
                lake_root=tmp_path,
                exchange=entry.exchange,
                segment=entry.segment,
                symbol=entry.symbol,
                interval=entry.interval,
                start=entry.first_ts,
                end=entry.first_ts + timedelta(minutes=1),
            )
        # Window-filtered (2 of 3 bars), ascending, canonical columns.
        assert table.column("close").to_pylist() == [100.0, 101.0]
        timestamps = table.column("timestamp").to_pylist()
        assert timestamps == sorted(timestamps)

    def test_poisoned_partition_collapses_keep_first(self, tmp_path: Path) -> None:
        """DEC-LAKE-READ-UNIQUE-001: the read gate drops the 999999 re-statement."""
        entry = _entry(month=1, year=2024)
        _write_month_partition(
            tmp_path, entry=entry, closes=[100.0, 101.0], duplicate_first_minute=True
        )
        with Catalog.open(tmp_path / "catalog.duckdb") as cat:
            cat.upsert(entry)
            table = cat.get_bars(
                lake_root=tmp_path,
                exchange=entry.exchange,
                segment=entry.segment,
                symbol=entry.symbol,
                interval=entry.interval,
                start=entry.first_ts,
                end=entry.first_ts + timedelta(minutes=5),
            )
        assert table.column("close").to_pylist() == [100.0, 101.0]  # keep-FIRST won

    def test_no_matching_partition_returns_canonical_empty_table(self, tmp_path: Path) -> None:
        with Catalog.open(tmp_path / "catalog.duckdb") as cat:
            table = cat.get_bars(
                lake_root=tmp_path,
                exchange="NSE",
                segment="FNO",
                symbol="NSE-NIFTY",
                interval="1minute",
                start=datetime(2024, 1, 1, tzinfo=UTC),
                end=datetime(2024, 1, 2, tzinfo=UTC),
            )
        assert table.num_rows == 0
        assert table.column_names == [
            "timestamp",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "open_interest",
        ]

    def test_missing_partition_file_raises(self, tmp_path: Path) -> None:
        entry = _entry(month=1, year=2024)  # catalog row, but NO Parquet on disk
        with Catalog.open(tmp_path / "catalog.duckdb") as cat:
            cat.upsert(entry)
            with pytest.raises(CatalogError, match="missing partition file"):
                cat.get_bars(
                    lake_root=tmp_path,
                    exchange=entry.exchange,
                    segment=entry.segment,
                    symbol=entry.symbol,
                    interval=entry.interval,
                    start=entry.first_ts,
                    end=entry.last_ts,
                )

    def test_naive_bounds_rejected(self, tmp_path: Path) -> None:
        with (
            Catalog.open(tmp_path / "catalog.duckdb") as cat,
            pytest.raises(CatalogError, match="timezone-aware"),
        ):
            cat.get_bars(
                lake_root=tmp_path,
                exchange="NSE",
                segment="FNO",
                symbol="NSE-NIFTY",
                interval="1minute",
                start=datetime(2024, 1, 1),  # naive
                end=datetime(2024, 1, 2, tzinfo=UTC),
            )

    def test_inverted_window_rejected(self, tmp_path: Path) -> None:
        with (
            Catalog.open(tmp_path / "catalog.duckdb") as cat,
            pytest.raises(CatalogError, match=">="),
        ):
            cat.get_bars(
                lake_root=tmp_path,
                exchange="NSE",
                segment="FNO",
                symbol="NSE-NIFTY",
                interval="1minute",
                start=datetime(2024, 1, 2, tzinfo=UTC),
                end=datetime(2024, 1, 1, tzinfo=UTC),
            )


class TestDataSnapshotHashRealRollup:
    """``data_snapshot_hash`` over real rows — deterministic sha256 rollup."""

    def test_rollup_matches_manual_sha256(self, tmp_path: Path) -> None:
        e1 = _entry(symbol="NSE-NIFTY", month=1)
        e2 = _entry(symbol="NSE-BANKNIFTY", month=1)
        with Catalog.open(tmp_path / "catalog.duckdb") as cat:
            cat.upsert(e1)
            cat.upsert(e2)
            digest = cat.data_snapshot_hash(
                exchange="NSE",
                segment="FNO",
                symbols=["NSE-NIFTY", "NSE-BANKNIFTY", "NSE-NIFTY"],  # dupes de-duped
                interval="1minute",
                start=e1.first_ts,
                end=e1.last_ts,
            )
        # Canonical order = sorted symbols → BANKNIFTY then NIFTY.
        expected = hashlib.sha256((e2.raw_hash + e1.raw_hash).encode("ascii")).hexdigest()
        assert digest == expected

    def test_empty_slice_is_sha256_of_empty_string(self, tmp_path: Path) -> None:
        with Catalog.open(tmp_path / "catalog.duckdb") as cat:
            digest = cat.data_snapshot_hash(
                exchange="NSE",
                segment="FNO",
                symbols=[],
                interval="1minute",
                start=datetime(2024, 1, 1, tzinfo=UTC),
                end=datetime(2024, 1, 2, tzinfo=UTC),
            )
        assert digest == hashlib.sha256(b"").hexdigest()


class TestClosedGuardsAndIdempotentClose:
    """The remaining closed-catalog guards + double-close idempotence."""

    def test_upsert_on_closed_catalog_raises(self, tmp_path: Path) -> None:
        cat = Catalog.open(tmp_path / "catalog.duckdb")
        cat.close()
        with pytest.raises(CatalogError, match="closed"):
            cat.upsert(_entry())

    def test_get_on_closed_catalog_raises(self, tmp_path: Path) -> None:
        cat = Catalog.open(tmp_path / "catalog.duckdb")
        cat.close()
        with pytest.raises(CatalogError, match="closed"):
            cat.get(
                exchange="NSE",
                segment="FNO",
                symbol="NSE-NIFTY",
                interval="1minute",
                year=2024,
                month=1,
            )

    def test_get_roundtrip_found_and_absent(self, tmp_path: Path) -> None:
        entry = _entry(month=2)
        with Catalog.open(tmp_path / "catalog.duckdb") as cat:
            cat.upsert(entry)
            found = cat.get(
                exchange=entry.exchange,
                segment=entry.segment,
                symbol=entry.symbol,
                interval=entry.interval,
                year=entry.year,
                month=entry.month,
            )
            absent = cat.get(
                exchange=entry.exchange,
                segment=entry.segment,
                symbol=entry.symbol,
                interval=entry.interval,
                year=entry.year,
                month=12,
            )
        assert found is not None
        assert found.raw_hash == entry.raw_hash
        assert absent is None

    def test_close_twice_is_idempotent(self, tmp_path: Path) -> None:
        cat = Catalog.open(tmp_path / "catalog.duckdb")
        cat.close()
        cat.close()  # the early-return branch — no raise, still closed
        with pytest.raises(CatalogError, match="closed"):
            cat.count()

    def test_snapshot_hash_naive_bounds_rejected(self, tmp_path: Path) -> None:
        with (
            Catalog.open(tmp_path / "catalog.duckdb") as cat,
            pytest.raises(CatalogError, match="timezone-aware"),
        ):
            cat.data_snapshot_hash(
                exchange="NSE",
                segment="FNO",
                symbols=["NSE-NIFTY"],
                interval="1minute",
                start=datetime(2024, 1, 1),  # naive
                end=datetime(2024, 1, 2, tzinfo=UTC),
            )
