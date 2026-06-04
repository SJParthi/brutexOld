"""Reconcile the fetch-coverage skip-ledger against what's actually in the lake.

DEC-LAKE-RECONCILE-001. The skip-ledger (DEC-FETCH-COVERAGE-001,
``<lake>/fetch_coverage.json``) records which fetch windows are already pulled so
a re-pull skips them (no wasted Dhan hit). But it reads ONLY its own JSON
sidecar — never the lake — so a fresh clone / lost / never-persisted ledger
re-fetches **every** window even though the Parquet bars are already stored,
producing the ``ingest.conflict`` flood the operator saw on NIFTY 1-minute.

This module makes the **lake the single source of truth** (CLAUDE.md §11 COMMON):
at pull start, every window the lake already encloses is marked complete in the
ledger, so a window whose bars are in the DB is **never re-fetched** — while a
window the lake does NOT cover (a missing 1-day dataset, a recent gap) is left
un-marked and re-pulled. Re-fetching an un-enclosed window is always safe (the
sink is idempotent + keep-first, DEC-LAKE-CONFLICT-KEEPFIRST-001), so the
reconcile is deliberately **conservative**: it marks a window complete ONLY when
the stored span fully encloses it.

## Honest envelope (Golden Rule #10)

* Enclosure is computed from each dataset's stored **min/max date span**
  (:func:`~brutex.dashboard.db_view.summarize_spot_datasets`), so an *internal*
  gap inside an enclosed span is not detected here — that finer per-window
  completeness is the windows-ledger follow-up. The conservative rule never
  causes data loss (an un-enclosed window re-pulls; keep-first makes a re-fetch a
  no-op), it only narrows the quota saving to the clearly-settled windows.
* Pure :func:`reconcile_windows` (spans injected → 100% testable) +
  :func:`reconcile_coverage_with_lake` (the one ``read_parquet`` I/O surface).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date
from pathlib import Path
from typing import Any

from brutex.adapters.dhan.ingest import _MARKET, _windows  # the canonical tiler (COMMON)
from brutex.data.fetch_coverage import FetchCoverage, WindowKey
from brutex.models import Instrument

__all__ = ["load_lake_spans", "reconcile_coverage_with_lake", "reconcile_windows"]


def _span_key(exchange: str, segment: str, symbol: str, interval: str) -> tuple[str, str, str, str]:
    """The ``(exchange, segment, symbol, interval)`` coordinate keying a lake span."""
    return (exchange, segment, symbol, interval)


def reconcile_windows(
    coverage: FetchCoverage,
    spans: Sequence[dict[str, Any]],
    *,
    instruments: Iterable[Instrument],
    intervals: Sequence[str],
    start: date,
    end: date,
) -> int:
    """Mark every planned window the lake already encloses as complete — **pure**.

    For each ``(instrument, interval)`` being pulled over ``[start, end)``, look up
    the lake's stored span (from *spans*) and mark a window complete iff the span
    fully encloses it: ``first_date <= window_start AND window_end <= last_date``
    and the dataset holds at least one bar. The window tiling is the SAME
    :func:`~brutex.adapters.dhan.ingest._windows` the ingest uses (one source of
    truth), so a marked window's key matches the key the ingest's skip probe will
    test byte-for-byte (``security_id == instrument.symbol_name``,
    ``market == "dhan"``).

    Parameters
    ----------
    coverage
        The ledger to populate (mutated in place via :meth:`FetchCoverage.mark_complete`).
    spans
        The lake's per-dataset spans — each a dict with ``exchange``, ``segment``,
        ``symbol``, ``interval``, ``bars`` (int), ``first_date`` / ``last_date``
        (``YYYY-MM-DD`` strings or ``None``), as returned by
        :func:`~brutex.dashboard.db_view.summarize_spot_datasets`.
    instruments, intervals
        The instruments + canonical intervals being pulled.
    start, end
        The half-open pull date range.

    Returns
    -------
    int
        The number of windows newly marked complete from the lake.
    """
    span_by_key: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for span in spans:
        span_by_key[
            _span_key(
                str(span["exchange"]),
                str(span["segment"]),
                str(span["symbol"]),
                str(span["interval"]),
            )
        ] = span
    marked = 0
    for instrument in instruments:
        for interval in intervals:
            entry = span_by_key.get(
                _span_key(
                    instrument.exch_id.value,
                    instrument.segment.value,
                    instrument.symbol_name,
                    interval,
                )
            )
            if entry is None:
                continue
            if int(entry["bars"]) <= 0 or entry["first_date"] is None or entry["last_date"] is None:
                continue
            first_date = date.fromisoformat(str(entry["first_date"]))
            last_date = date.fromisoformat(str(entry["last_date"]))
            for window_start, window_end in _windows(interval, start, end):
                # Conservative: the stored span must ENCLOSE the whole window.
                if first_date <= window_start and window_end <= last_date:
                    coverage.mark_complete(
                        WindowKey(
                            market=_MARKET,
                            security_id=instrument.symbol_name,
                            interval=interval,
                            start=window_start,
                            end=window_end,
                        )
                    )
                    marked += 1
    return marked


def load_lake_spans(lake_root: Path) -> list[dict[str, Any]]:  # pragma: no cover - thin DuckDB I/O
    """Read every spot dataset's stored span from the lake via an ephemeral DuckDB conn.

    The one read-only ``read_parquet`` surface (a single aggregate scalar per
    dataset — see :func:`~brutex.dashboard.db_view.summarize_spot_datasets`),
    exercised by the integration test over a real tmp lake; the conn is local +
    closed, so the pull path takes no dependency on the dashboard's store
    connection. Returns ``[]`` when DuckDB is unavailable or the lake is empty.
    """
    try:
        import duckdb
    except ImportError:
        return []
    from brutex.dashboard.db_view import summarize_spot_datasets

    conn = duckdb.connect()
    try:
        return summarize_spot_datasets(conn, lake_root)
    finally:
        conn.close()


def reconcile_coverage_with_lake(
    coverage: FetchCoverage,
    lake_root: Path,
    *,
    instruments: Iterable[Instrument],
    intervals: Sequence[str],
    start: date,
    end: date,
) -> int:
    """Load the lake spans and reconcile *coverage* against them (the wired entry point).

    Thin composition of :func:`load_lake_spans` (I/O) + :func:`reconcile_windows`
    (pure). Called once at pull start, so its cost is O(datasets) aggregate reads,
    never per-window — the steady-state per-window skip stays O(1).
    """
    spans = load_lake_spans(lake_root)
    return reconcile_windows(
        coverage,
        spans,
        instruments=instruments,
        intervals=intervals,
        start=start,
        end=end,
    )
