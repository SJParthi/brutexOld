"""Unit tests for :mod:`brutex.data.lake_wipe` — the dashboard truncate core.

Pure + injectable (no DuckDB): covers the happy path (every data table emptied,
counts reported, ``coverage_ledger`` included + ``schema_meta`` excluded) and the
per-table failure path (one table errors, the rest still truncate), at 100%
line + branch.
"""

from __future__ import annotations

from brutex.data.lake_wipe import LAKE_DATA_TABLES, truncate_lake_tables


def test_lake_data_tables_set() -> None:
    """The wipe set is the 5 data tables — coverage_ledger IN, schema_meta OUT."""
    assert LAKE_DATA_TABLES == (
        "instruments",
        "instrument_runs",
        "expiries",
        "contracts",
        "coverage_ledger",
    )
    assert "coverage_ledger" in LAKE_DATA_TABLES  # else /coverage keeps stale cells
    assert "schema_meta" not in LAKE_DATA_TABLES  # the version marker, not data


def test_truncate_all_tables_deleted() -> None:
    """Every table is DELETE-d, in order, with its pre-delete count reported."""
    counts = {
        "instruments": 25,
        "instrument_runs": 4,
        "expiries": 6053,
        "contracts": 249409,
        "coverage_ledger": 1652,
    }
    calls: list[str] = []
    report = truncate_lake_tables(
        calls.append,
        count=lambda table: counts[table],
    )
    assert report.failed == ()
    assert dict(report.deleted) == counts
    assert len(calls) == len(LAKE_DATA_TABLES)
    assert all(table in sql for table, sql in zip(LAKE_DATA_TABLES, calls, strict=True))


def test_truncate_continues_past_per_table_failure() -> None:
    """A failing table is recorded in ``failed``; the others still truncate."""

    def _execute(sql: str) -> None:
        if "contracts" in sql:
            raise RuntimeError("table locked")

    report = truncate_lake_tables(_execute, count=lambda _table: 7)
    deleted_tables = [table for table, _ in report.deleted]
    failed = dict(report.failed)
    assert failed == {"contracts": "table locked"}
    assert "contracts" not in deleted_tables
    assert len(deleted_tables) == len(LAKE_DATA_TABLES) - 1  # the other 4 succeeded


def test_truncate_honours_explicit_table_subset() -> None:
    """An explicit ``tables`` arg overrides the default set."""
    calls: list[str] = []
    report = truncate_lake_tables(
        calls.append,
        count=lambda _table: 0,
        tables=("contracts",),
    )
    assert calls == ["DELETE FROM contracts"]
    assert report.deleted == (("contracts", 0),)
