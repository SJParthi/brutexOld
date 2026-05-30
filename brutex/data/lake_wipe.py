"""Lake data-wipe core — truncate the ingested-data tables (DEC-DASHBOARD-WIPE-001).

The dashboard "Delete all ingested data" button empties the lake's DATA tables
WITHOUT closing the DuckDB connection: a ``DELETE FROM`` per table, run as ONE
:class:`~brutex.data.instrument_store.InstrumentStore` method under the store's
``_StoreMeta`` lock — so a concurrent ``/db`` read just sees fewer rows
(DuckDB is ACID/MVCC), never a use-after-free crash. The close→reopen approach
(which WOULD segfault, because ``db_view.sample_rows`` releases the store lock
between its two executes — a wipe closing the connection in that gap frees it
under a live cursor) is deliberately deferred to a separate PR (see the 4-agent
review recorded in MIGRATION.md).

Authoritative wipe set (:data:`LAKE_DATA_TABLES`): every table the ingest WRITES
rows into — INCLUDING ``coverage_ledger`` (so ``/coverage`` doesn't keep showing
stale cells after a wipe), EXCLUDING ``schema_meta`` (the schema-version marker,
not ingested data — truncating it would orphan the store's version row). It
differs from ``db_view.KNOWN_TABLES`` (the viewer allowlist) by design + intent.

Pure + injectable: the ``execute`` + ``count`` callables are injected, so every
branch (including the per-table failure path) is unit-reachable with no DuckDB.

Honest scope (Golden Rule #10): this zeroes the ROWS the operator sees; DuckDB
does NOT shrink the file on ``DELETE``, and credentials / token / audit are NOT
touched — to reclaim disk bytes + remove credentials, stop the server and run
``make wipe`` (the file-level CLI wipe, DEC-DATA-WIPE-001).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

#: The lake DATA tables the dashboard wipe truncates (the authoritative set).
#: Every table the ingest writes rows into; ``coverage_ledger`` IS included so a
#: wipe doesn't leave ``/coverage`` showing stale cells; ``schema_meta`` is NOT
#: (it is the version marker, not data). One source of truth for the wiper.
LAKE_DATA_TABLES: Final[tuple[str, ...]] = (
    "instruments",
    "instrument_runs",
    "expiries",
    "contracts",
    "coverage_ledger",
)


@dataclass(frozen=True, slots=True)
class LakeWipeReport:
    """Outcome of a :func:`truncate_lake_tables` — per-table deleted counts + failures."""

    deleted: tuple[tuple[str, int], ...]  # (table, rows_deleted)
    failed: tuple[tuple[str, str], ...]  # (table, error message)


def truncate_lake_tables(
    execute: Callable[[str], object],
    *,
    count: Callable[[str], int],
    tables: Sequence[str] = LAKE_DATA_TABLES,
) -> LakeWipeReport:
    """``DELETE FROM`` each lake DATA *table*; continue past per-table failures.

    Each table's pre-delete row count is captured (so the report can tell the
    operator what was removed), then the table is emptied. A failure on one
    table is recorded and the wipe continues — one un-truncatable table never
    aborts the rest (mirrors :func:`brutex.wipe.execute_wipe`'s discipline).

    Parameters
    ----------
    execute
        Runs one SQL statement (production: the store's ``conn.execute`` while
        the store lock is held). Table names come ONLY from
        :data:`LAKE_DATA_TABLES` — a fixed allowlist, never operator input — so
        the interpolated ``DELETE FROM {table}`` is injection-safe.
    count
        Returns a table's current row count (injected for testability).
    tables
        Tables to truncate (defaults to :data:`LAKE_DATA_TABLES`).

    Returns
    -------
    LakeWipeReport
        Per-table deleted counts (best-effort) plus any per-table failures.
    """
    deleted: list[tuple[str, int]] = []
    failed: list[tuple[str, str]] = []
    for table in tables:
        try:
            rows = count(table)
            execute(f"DELETE FROM {table}")  # noqa: S608  # nosec B608 — table from the fixed LAKE_DATA_TABLES allowlist, not user input
        except Exception as exc:
            failed.append((table, str(exc)))
            continue
        deleted.append((table, rows))
    return LakeWipeReport(tuple(deleted), tuple(failed))


__all__ = ["LAKE_DATA_TABLES", "LakeWipeReport", "truncate_lake_tables"]
