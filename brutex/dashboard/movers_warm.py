"""Post-pull movers warm — precompute the default movers snapshots after a clean pull.

DEC-PULL-MOVERS-WARM-001 — the gated phase-4 binding for
:func:`brutex.dashboard.pipeline_pull.run_full_pipeline`. After a fully-successful fetch
(spot + metadata + historical all passed their gates), warm the DEFAULT movers snapshots
(``1day`` · the LATEST available day · all-exchange · spot/futures/options) into the
:class:`~brutex.dashboard.movers_cache.MoversSnapshotCache` at the current write
generation, so the operator's first ``/movers`` open is an O(1) cache hit instead of a
cold lake scan.

## No illusion (Golden Rule #1)

The warm targets the **latest day that actually has data** (``latest_movers_date`` — the
MAX spot ``1day`` bar's IST date), NEVER "today": the just-finished historical pull fills
the lake only to the PREVIOUS trading day, so warming today's coordinate would cache
EMPTY snapshots (`compute_spot_candidates` reads the exact requested date — no fallback).
If the lake has no spot ``1day`` data yet (`latest_movers_date` is ``None``), nothing is
warmed and ``0`` is returned — an honest "warmed nothing", never a fabricated snapshot.

## Best-effort (the pull's success is never compromised)

The warm is a pure read-only accelerator over the just-filled lake. A warmer that raises
(a transient lake read race) is logged and counted as ``0`` for that asset — it NEVER
propagates to fail the already-successful pull (Correctness of the pull > the warm's
latency benefit). The cached value (even an empty candidate set) is still valid for the
generation, so the next ``/movers`` open is correct regardless.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

import structlog

__all__ = ["latest_movers_date", "warm_movers_snapshots"]

_LOG = structlog.get_logger(__name__)

#: The spot segments the movers default snapshot reads (INDEX + CASH 1day partitions).
_SPOT_SEGMENTS: tuple[str, ...] = ("INDEX", "CASH")
_DAILY_INTERVAL = "1day"


def latest_movers_date(lake_root: Path) -> str | None:
    """Return the IST date (ISO ``YYYY-MM-DD``) of the most recent spot ``1day`` bar, or ``None``.

    Scans the spot ``1day`` partitions (INDEX + CASH) for ``MAX("timestamp")`` and converts
    that instant to its IST calendar date — the latest day the movers default snapshot can
    show WITH DATA. ``None`` when the lake has no spot ``1day`` files yet (so the caller
    warms nothing rather than an empty "today" snapshot). Opens its own short-lived DuckDB
    connection (self-contained, like the ``compute_*_candidates`` readers).

    The read is bounded to the ``1day`` partitions (intrinsically tiny — one row per trading
    day per symbol), never the multi-year 1-minute lake.

    Parameters
    ----------
    lake_root
        Top-level lake directory (parent of ``bars/``).

    Returns
    -------
    str | None
        The latest spot ``1day`` IST date as ``YYYY-MM-DD``, or ``None`` if absent.
    """
    import duckdb

    # Only query the segments that actually have at least one file — read_parquet raises
    # "No files found" on an empty glob, which we treat as "no data" (None), not an error.
    present = [
        (lake_root / "bars" / "*" / seg / "*" / _DAILY_INTERVAL).as_posix() + "/**/*.parquet"
        for seg in _SPOT_SEGMENTS
        if (lake_root / "bars").is_dir()
        and any((lake_root / "bars").glob(f"*/{seg}/*/{_DAILY_INTERVAL}/**/*.parquet"))
    ]
    if not present:
        return None
    # MAX over the present spot 1day globs → the latest instant; convert to its IST date.
    # `present` is non-empty so the aggregate has rows; the None-guard is a single-line
    # ternary (a defensive narrow for the type checker, never a partial-branch miss).
    #
    # GAP #4 (resilience): a single corrupt / unreadable 1day Parquet under the glob makes
    # DuckDB raise a `duckdb.Error` (an `InvalidInputException` — "No magic bytes …") or an
    # `OSError` fault; this read backs the DEFAULT `/movers` poll, so an unguarded raise
    # would 500 the whole page on every open. Treat an unreadable lake the same as an empty
    # one — return `None` so the route degrades to an honest empty/today payload instead of
    # crashing (Golden Rule #1 / #10). NARROW: a code bug still propagates.
    try:
        with duckdb.connect() as conn:
            row = conn.execute(
                "SELECT MAX((\"timestamp\" AT TIME ZONE 'Asia/Kolkata')::DATE) "
                "FROM read_parquet(?, union_by_name=true)",  # nosec B608 - bound glob-list parameter
                [present],
            ).fetchone()
    except (duckdb.Error, OSError) as exc:
        _LOG.warning("movers.latest_date.unreadable_lake", error=str(exc))
        return None
    return str(row[0]) if row is not None and row[0] is not None else None


def warm_movers_snapshots(jobs: Sequence[Callable[[], int]]) -> int:
    """Run each warm *job* (compute+cache one default snapshot) and return how many warmed NON-empty.

    Each job computes one asset's default snapshot via ``MoversSnapshotCache.get_or_compute``
    (so it is stored at the current generation) and returns that snapshot's candidate count.
    A job that produces ≥ 1 candidate counts toward the returned total; a job that warms an
    empty snapshot counts ``0`` (honest). A job that RAISES (a transient lake-read race) is
    logged and counted ``0`` — the warm is best-effort and NEVER fails the already-successful
    pull that triggered it (the cached value, if any, is still valid for the generation).

    Parameters
    ----------
    jobs
        One zero-arg callable per default asset snapshot; each computes+caches its snapshot
        and returns its candidate count (``len(snapshots)`` / ``len(movers)``).

    Returns
    -------
    int
        The number of jobs that warmed a NON-empty snapshot.
    """
    warmed = 0
    for job in jobs:
        try:
            if job() > 0:
                warmed += 1
        except Exception as exc:  # pylint: disable=broad-except - best-effort warm; logged, never re-raised
            _LOG.warning("movers.warm.job_failed", error=str(exc))
    return warmed
