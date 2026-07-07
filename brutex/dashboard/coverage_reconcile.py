"""Read the lake's per-dataset spans — the cold-start source for the window ledger.

DEC-WINDOW-LEDGER-001 (Stage d). This module once held the fetch-coverage
reconcile *band-aids* (``reconcile_windows`` / ``reconcile_coverage_with_lake`` /
``reconcile_empty_terminal`` / ``reconcile_failed_windows_for_repull``) that
patched the COARSE ``FetchCoverage`` span-set so
its #598 internal-gap mask did less harm. Now that the precise per-window
:class:`~brutex.data.window_ledger.WindowLedger` is the SOLE skip authority
(Stages b3 / c), those band-aids are dead and have been removed — the ledger
records the exact per-window state and never needs a coarse set patched every
pull.

The single survivor is :func:`load_lake_spans`: the read-only ``read_parquet``
surface the ledger's cold-start seed
(:func:`~brutex.data.window_ledger_store.reconcile_ledger_with_lake`) consumes to
discover which windows the lake already holds. O(datasets) aggregate reads at
pull start, never per-window.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

__all__ = ["load_lake_spans"]


def load_lake_spans(
    lake_root: Path,
    *,
    session_date_segments: frozenset[str] | None = None,
) -> list[dict[str, Any]]:  # pragma: no cover - thin DuckDB I/O
    """Read every spot dataset's stored span from the lake via an ephemeral DuckDB conn.

    The one read-only ``read_parquet`` surface (a single aggregate scalar per
    dataset — see :func:`~brutex.dashboard.db_view.summarize_spot_datasets`),
    exercised by the integration test over a real tmp lake; the conn is local +
    closed, so the pull path takes no dependency on the dashboard's store
    connection. Returns ``[]`` when DuckDB is unavailable or the lake is empty.

    Parameters
    ----------
    lake_root
        Top-level lake directory (the parent of ``bars/``).
    session_date_segments
        Forwarded verbatim to
        :func:`~brutex.dashboard.db_view.summarize_spot_datasets` — the SPOT
        ledger cold-start passes ``{"INDEX"}`` so the dense index ``1minute``
        spans carry ``session_dates`` for the interior-gap density cross-check
        (DEC-COLDSTART-SEED-INTERIOR-GAP-001). The default ``None`` keeps every
        other caller (the F&O reconcile included) byte-identical.
    """
    try:
        import duckdb
    except ImportError:
        return []
    from brutex.config.hardware_envelope import apply_duckdb_thread_bound
    from brutex.dashboard.db_view import summarize_spot_datasets

    conn = duckdb.connect()
    try:
        # DEC-THREAD-BUDGET-BOUNDS-001 — bound this ephemeral connection's
        # thread pool (DuckDB defaults to ALL logical cores per connection).
        # Honest tradeoff: an ISOLATED cold-start scan on an idle box now runs
        # at physical//2 — accepted, because this read fires alongside pull
        # activity in production and is a bounded footer-aggregate read.
        apply_duckdb_thread_bound(conn)
        return summarize_spot_datasets(conn, lake_root, session_date_segments=session_date_segments)
    finally:
        conn.close()
