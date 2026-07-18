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
from typing import TYPE_CHECKING

import pytest
from brutex.data.catalog import Catalog, open_catalog
from brutex.data.errors import CatalogError
from brutex.data.models import CatalogEntry

if TYPE_CHECKING:  # pragma: no cover — type-only import (module avoids heavy imports)
    import pyarrow as pa

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


# ────────────────────────────────────────────────────────────────────────
# R2N-02 / R2N-04 + NEW-1(a): corrupt / torn partition reads at get_bars.
# DEC-CATALOG-READ-INTEGRITY-001, MIGRATION.md #1959.
# ────────────────────────────────────────────────────────────────────────

_pl = pytest.importorskip("polars")
pytest.importorskip("pyarrow")

from datetime import UTC as _UTC  # noqa: E402
from decimal import Decimal as _Decimal  # noqa: E402

from brutex.data.catalog import (  # noqa: E402
    _read_partition_frame,
    _validate_partition_schema,
)
from brutex.data.models import CandleBar as _CandleBar  # noqa: E402
from brutex.data.partition import build_partition_path as _build_partition_path  # noqa: E402

_EXCH, _SEG, _SYM, _INT = "NSE", "FNO", "NSE-NIFTY", "1minute"
_WIN_START = datetime(2024, 1, 1, tzinfo=_UTC)
_WIN_END = datetime(2024, 1, 31, 23, 59, tzinfo=_UTC)


def _cbar(day: int, *, minute: int = 15, open_price: float = 22000.0) -> _CandleBar:
    return _CandleBar(
        timestamp=datetime(2024, 1, day, 9, minute, 0, tzinfo=_UTC),
        open=_Decimal(str(open_price)),
        high=_Decimal(str(open_price + 10)),
        low=_Decimal(str(open_price - 10)),
        close=_Decimal(str(open_price + 5)),
        volume=100,
        open_interest=500,
    )


def _write_and_register(lake: Path, catalog_path: Path, bars: list[_CandleBar]) -> Path:
    """Write *bars* through the REAL writer, reconcile the catalog, return the partition path."""
    from brutex.data.catalog_reconcile import reconcile_catalog_from_lake
    from brutex.data.parquet_writer import ParquetWriter

    writer = ParquetWriter(lake_root=lake)
    writer.write(exchange=_EXCH, segment=_SEG, symbol=_SYM, interval=_INT, bars=bars)
    with Catalog.open(catalog_path) as cat:
        reconcile_catalog_from_lake(
            lake_root=lake,
            catalog=cat,
            exchange=_EXCH,
            segment=_SEG,
            symbol=_SYM,
            interval=_INT,
        )
    return _build_partition_path(
        lake_root=lake, exchange=_EXCH, segment=_SEG, symbol=_SYM, interval=_INT, year=2024, month=1
    )


def _get_bars(catalog_path: Path, lake: Path, **kw: object) -> pa.Table:
    with Catalog.open(catalog_path) as cat:
        return cat.get_bars(
            lake_root=lake,
            exchange=_EXCH,
            segment=_SEG,
            symbol=_SYM,
            interval=_INT,
            start=_WIN_START,
            end=_WIN_END,
            **kw,  # type: ignore[arg-type]
        )


class TestGetBarsReadIntegrity:
    def test_corrupt_partition_raises_unreadable_naming_file(self, tmp_path: Path) -> None:
        """C1/C2: a garbage parquet raises CatalogError 'UNREADABLE' naming the month file."""
        lake, catalog_path = tmp_path / "lake", tmp_path / "catalog.duckdb"
        part = _write_and_register(lake, catalog_path, [_cbar(2)])
        part.write_bytes(b"CORRUPT\x00\x01" * 40)
        with pytest.raises(CatalogError, match="UNREADABLE") as excinfo:
            _get_bars(catalog_path, lake, retry_sleep=lambda _s: None)
        assert str(part) in str(excinfo.value)

    def test_foreign_schema_missing_ohlc_raises(self, tmp_path: Path) -> None:
        """C3: a timestamp-only foreign parquet is rejected naming the missing columns."""
        lake, catalog_path = tmp_path / "lake", tmp_path / "catalog.duckdb"
        part = _write_and_register(lake, catalog_path, [_cbar(2)])
        _pl.DataFrame({"timestamp": [datetime(2024, 1, 2, 9, 15, tzinfo=_UTC)]}).write_parquet(part)
        with pytest.raises(CatalogError, match="missing required bar column") as excinfo:
            _get_bars(catalog_path, lake)
        assert "open" in str(excinfo.value)

    def test_utf8_close_dtype_raises(self, tmp_path: Path) -> None:
        """C4: a full-schema parquet with close stored as Utf8 is a dtype refusal."""
        lake, catalog_path = tmp_path / "lake", tmp_path / "catalog.duckdb"
        part = _write_and_register(lake, catalog_path, [_cbar(2)])
        good = _pl.read_parquet(part)
        poisoned = good.with_columns(_pl.col("close").cast(_pl.Utf8))
        poisoned.write_parquet(part)
        with pytest.raises(CatalogError, match="wrong column dtype") as excinfo:
            _get_bars(catalog_path, lake)
        assert "close" in str(excinfo.value)

    def test_non_temporal_timestamp_dtype_raises(self, tmp_path: Path) -> None:
        """A timestamp stored as Int64 (raw epoch) is a dtype refusal naming timestamp."""
        lake, catalog_path = tmp_path / "lake", tmp_path / "catalog.duckdb"
        part = _write_and_register(lake, catalog_path, [_cbar(2)])
        good = _pl.read_parquet(part)
        poisoned = good.with_columns(_pl.col("timestamp").cast(_pl.Int64))
        poisoned.write_parquet(part)
        with pytest.raises(CatalogError, match="wrong column dtype") as excinfo:
            _get_bars(catalog_path, lake)
        assert "timestamp" in str(excinfo.value)

    def test_non_integer_volume_dtype_raises(self, tmp_path: Path) -> None:
        """A volume stored as Utf8 is a dtype refusal naming volume."""
        lake, catalog_path = tmp_path / "lake", tmp_path / "catalog.duckdb"
        part = _write_and_register(lake, catalog_path, [_cbar(2)])
        good = _pl.read_parquet(part)
        poisoned = good.with_columns(_pl.col("volume").cast(_pl.Utf8))
        poisoned.write_parquet(part)
        with pytest.raises(CatalogError, match="wrong column dtype") as excinfo:
            _get_bars(catalog_path, lake)
        assert "volume" in str(excinfo.value)

    def test_torn_read_retry_once_heals(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """R2N-04: a torn read (raises once) is retried ONCE and the real frame returned."""
        lake, catalog_path = tmp_path / "lake", tmp_path / "catalog.duckdb"
        _write_and_register(lake, catalog_path, [_cbar(2), _cbar(3)])

        real_read = _pl.read_parquet
        calls = {"n": 0}

        def _tear_once(path: object, *a: object, **k: object) -> object:
            calls["n"] += 1
            if calls["n"] == 1:
                raise _pl.exceptions.ComputeError("simulated torn read")
            return real_read(path, *a, **k)

        monkeypatch.setattr(_pl, "read_parquet", _tear_once)
        sleeps: list[float] = []
        from structlog.testing import capture_logs

        with capture_logs() as logs:
            table = _get_bars(catalog_path, lake, retry_sleep=sleeps.append)
        assert table.num_rows == 2  # the real frame, healed after one retry
        assert calls["n"] == 2
        assert len(sleeps) == 1  # slept exactly once
        assert sum(1 for e in logs if e.get("event") == "catalog.read.parquet_retry") == 1

    def test_torn_read_both_attempts_fail_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """R2N-04: a persistent tear (both attempts raise) fails LOUD as UNREADABLE."""
        lake, catalog_path = tmp_path / "lake", tmp_path / "catalog.duckdb"
        _write_and_register(lake, catalog_path, [_cbar(2)])

        def _always_tear(_path: object, *a: object, **k: object) -> object:
            raise _pl.exceptions.ComputeError("persistent tear")

        monkeypatch.setattr(_pl, "read_parquet", _always_tear)
        sleeps: list[float] = []
        with pytest.raises(CatalogError, match="UNREADABLE"):
            _get_bars(catalog_path, lake, retry_sleep=sleeps.append)
        assert len(sleeps) == 1  # retried exactly once before failing

    def test_clean_read_no_retry_no_warn_no_sleep(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A clean partition reads first try — zero retries, zero WARNs, zero sleeps."""
        lake, catalog_path = tmp_path / "lake", tmp_path / "catalog.duckdb"
        _write_and_register(lake, catalog_path, [_cbar(2), _cbar(3)])

        def _must_not_sleep(_s: float) -> None:  # pragma: no cover - asserts by never running
            raise AssertionError("clean read must never sleep/retry")

        from structlog.testing import capture_logs

        with capture_logs() as logs:
            table = _get_bars(catalog_path, lake, retry_sleep=_must_not_sleep)
        assert table.num_rows == 2
        assert not any(e.get("event") == "catalog.read.parquet_retry" for e in logs)

    def test_clean_lake_byte_parity_output_unchanged(self, tmp_path: Path) -> None:
        """Clean-data neutrality: the guard never mutates a clean read's rows."""
        lake, catalog_path = tmp_path / "lake", tmp_path / "catalog.duckdb"
        part = _write_and_register(lake, catalog_path, [_cbar(2), _cbar(3)])
        table = _get_bars(catalog_path, lake)
        # The guarded read equals a direct read+filter (no mutation, no dropped rows).
        direct = (
            _pl.read_parquet(part)
            .filter((_pl.col("timestamp") >= _WIN_START) & (_pl.col("timestamp") <= _WIN_END))
            .sort("timestamp")
        )
        assert table.num_rows == direct.height == 2
        assert table.column("close").to_pylist() == direct["close"].to_list()

    def test_get_bars_is_full_or_raises_never_a_subset(self, tmp_path: Path) -> None:
        """No-subset property: get_bars returns ALL in-window rows OR raises — never partial."""
        lake, catalog_path = tmp_path / "lake", tmp_path / "catalog.duckdb"
        part = _write_and_register(lake, catalog_path, [_cbar(2), _cbar(3), _cbar(4)])
        # Clean → all 3 rows.
        assert _get_bars(catalog_path, lake).num_rows == 3
        # Corrupt → raises (never a truncated 0/1/2-row subset).
        part.write_bytes(b"GARBAGE" * 40)
        with pytest.raises(CatalogError):
            _get_bars(catalog_path, lake, retry_sleep=lambda _s: None)

    def test_validate_partition_schema_accepts_canonical_frame(self, tmp_path: Path) -> None:
        """A canonical frame passes the schema gate silently (the happy branch)."""
        lake, catalog_path = tmp_path / "lake", tmp_path / "catalog.duckdb"
        part = _write_and_register(lake, catalog_path, [_cbar(2)])
        # Direct helper call: no raise on the canonical writer output.
        _validate_partition_schema(_pl.read_parquet(part), part)

    def test_read_partition_frame_default_sleep_on_clean_frame(self, tmp_path: Path) -> None:
        """_read_partition_frame with no injected sleep still reads a clean frame fine."""
        lake, catalog_path = tmp_path / "lake", tmp_path / "catalog.duckdb"
        part = _write_and_register(lake, catalog_path, [_cbar(2)])
        frame = _read_partition_frame(part)  # retry_sleep=None → time.sleep, never reached
        assert frame.height == 1

    def test_concurrent_writer_no_raw_polars_error_escapes(self, tmp_path: Path) -> None:
        """R2N-04 concurrency: reads under a live atomic-replace writer never leak a PolarsError."""
        import threading

        from brutex.data.parquet_writer import ParquetWriter

        lake, catalog_path = tmp_path / "lake", tmp_path / "catalog.duckdb"
        _write_and_register(lake, catalog_path, [_cbar(2), _cbar(3)])
        writer = ParquetWriter(lake_root=lake)
        stop = threading.Event()

        def _write_loop() -> None:
            i = 0
            while not stop.is_set() and i < 40:
                writer.write(
                    exchange=_EXCH,
                    segment=_SEG,
                    symbol=_SYM,
                    interval=_INT,
                    bars=[_cbar(2), _cbar(3, open_price=22000.0 + i)],
                )
                i += 1

        t = threading.Thread(target=_write_loop)
        t.start()
        try:
            with Catalog.open(catalog_path) as cat:
                for _ in range(40):
                    # Any transient tear is absorbed by the retry; a genuine
                    # PolarsError must NEVER escape get_bars (only a typed
                    # CatalogError could, and an atomic-replace lake yields none).
                    table = cat.get_bars(
                        lake_root=lake,
                        exchange=_EXCH,
                        segment=_SEG,
                        symbol=_SYM,
                        interval=_INT,
                        start=_WIN_START,
                        end=_WIN_END,
                        retry_sleep=lambda _s: None,
                    )
                    assert table.num_rows >= 1
        except _pl.exceptions.PolarsError as exc:  # pragma: no cover - the bug this pins
            raise AssertionError(f"raw PolarsError escaped get_bars: {exc!r}") from exc
        finally:
            stop.set()
            t.join()


# ────────────────────────────────────────────────────────────────────────
# Glob-metachar symbol dir refused at the ENGINE feed mouth (get_bars).
# DEC-SPOT-DERIVE-TS-RANGE-HARDENING-001 (#1594 round-15 metachar law).
# ────────────────────────────────────────────────────────────────────────
class TestGlobMetacharSymbolRefused:
    """DEC-SPOT-DERIVE-TS-RANGE-HARDENING-001 (the #1594 round-15 metachar law
    on the ENGINE feed mouth) — FAILS-BEFORE (executed repro): a registered
    partition whose symbol dir is ``NSE-X[12]`` beside real ``NSE-X1``/
    ``NSE-X2`` siblings made ``get_bars``'s ``pl.read_parquet(str(path))``
    glob-interpret the path and serve the SIBLINGS' bars AS this symbol
    (rows=3, close 100.11 — silent cross-symbol corruption on the sweep feed).
    Now the bracket symbol is refused at ``build_partition_path`` (the
    forbidden-chars law, typed ``PartitionError``) BEFORE any read, and the
    explicit-path read itself is ``glob=False`` (defense-in-depth)."""

    def test_bracket_symbol_get_bars_refused_never_reads_siblings(self, tmp_path: Path) -> None:
        import polars as pl
        from brutex.data.errors import PartitionError

        def _plant_raw(symbol: str, close: float) -> None:
            dest = tmp_path / "bars" / "NSE" / "CASH" / symbol / "1minute" / "2024" / "01.parquet"
            dest.parent.mkdir(parents=True, exist_ok=True)
            stamps = [datetime(2024, 1, 3, 4, i, tzinfo=UTC) for i in range(3)]
            pl.DataFrame(
                {
                    "timestamp": stamps,
                    "open": [close] * 3,
                    "high": [close + 1] * 3,
                    "low": [close - 1] * 3,
                    "close": [close] * 3,
                    "volume": [10] * 3,
                    "open_interest": [None] * 3,
                }
            ).with_columns(pl.col("open_interest").cast(pl.Int64)).write_parquet(dest)

        _plant_raw("NSE-X1", 100.11)
        _plant_raw("NSE-X2", 100.22)
        _plant_raw("NSE-X[12]", 100.55)
        entry = CatalogEntry(
            exchange="NSE",
            segment="CASH",
            symbol="NSE-X[12]",
            interval="1minute",
            year=2024,
            month=1,
            raw_hash="0" * 64,
            row_count=3,
            first_ts=datetime(2024, 1, 3, 4, 0, tzinfo=UTC),
            last_ts=datetime(2024, 1, 3, 4, 2, tzinfo=UTC),
            fetched_at=datetime(2024, 1, 3, 12, tzinfo=UTC),
        )
        with Catalog.open(tmp_path / "catalog.duckdb") as cat:
            cat.upsert(entry)
            with pytest.raises(PartitionError, match="forbidden"):
                cat.get_bars(
                    lake_root=tmp_path,
                    exchange="NSE",
                    segment="CASH",
                    symbol="NSE-X[12]",
                    interval="1minute",
                    start=datetime(2024, 1, 1, tzinfo=UTC),
                    end=datetime(2024, 2, 1, tzinfo=UTC),
                )
