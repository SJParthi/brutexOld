"""Completeness-ledger view helpers — pure, JSON-friendly projection.

Powers the ``/coverage`` dashboard page (DEC-COVERAGE-LEDGER-001). Per
CLAUDE.md §11 (COMMON) the reconciliation rule lives in exactly ONE place
— :mod:`brutex.data.coverage_ledger` — and this module *reuses* it to
turn the persisted :class:`~brutex.data.coverage_ledger.CoverageRecord`
rows (plus the expected universe×years grid) into the deterministic,
JSON-serialisable shapes the FastAPI route + HTML template render. The
route in :mod:`brutex.dashboard.app` (coverage-omit, FastAPI-dependent)
stays a thin wrapper that delegates the real logic here so it is 100%
unit-testable offline.

Two outputs:

* :func:`build_coverage_rows` — one JSON row per expected
  ``(exchange, underlying, year)`` cell, including cells that have NO
  persisted ledger row yet (rendered as ``status="missing"``). Sorted
  deterministically so the table is stable across renders.
* :func:`coverage_headline` — the ``X / Y complete`` headline dict for
  the KPI strip, derived from a
  :class:`~brutex.data.coverage_ledger.CoverageSummary`.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any, Final

from brutex.data.coverage_ledger import (
    CoverageRecord,
    CoverageSummary,
    reconcile_coverage,
)

#: Status shown for an expected cell that has no persisted ledger row yet.
STATUS_MISSING: Final[str] = "missing"


def build_coverage_rows(
    records: Iterable[CoverageRecord],
    expected_underlyings: Iterable[tuple[str, str]],
    years: Sequence[int],
) -> list[dict[str, Any]]:
    """Return one JSON-friendly row per expected ``(exchange, underlying, year)`` cell.

    Every cell in the *expected_underlyings* × *years* grid produces a row,
    so the ``/coverage`` table shows the gaps too: a cell with no persisted
    :class:`CoverageRecord` is rendered with ``status="missing"`` and zero
    counts. A persisted cell carries its real counts, status, last-fetched
    timestamp, and error.

    Parameters
    ----------
    records
        The persisted ledger rows (e.g. ``store.list_coverage()``).
    expected_underlyings
        ``(exchange, underlying_symbol)`` pairs that SHOULD have coverage.
    years
        Calendar years that SHOULD have coverage for every underlying.

    Returns
    -------
    list[dict[str, Any]]
        Rows sorted by ``(exchange, underlying, year)``. Each row has keys:
        ``underlying``, ``exchange``, ``year``, ``status``,
        ``expiries_pulled``, ``contracts_pulled``, ``last_fetched_at``
        (ISO-8601 string or ``None``), and ``error`` (string or ``None``).
    """
    by_cell: dict[tuple[str, str, int], CoverageRecord] = {
        (r.exchange, r.underlying_symbol, r.year): r for r in records
    }

    expected: set[tuple[str, str, int]] = set()
    for exchange, underlying in expected_underlyings:
        for year in years:
            expected.add((exchange, underlying, year))

    rows: list[dict[str, Any]] = []
    for cell in sorted(expected):
        exchange, underlying, year = cell
        record = by_cell.get(cell)
        if record is None:
            rows.append(
                {
                    "underlying": underlying,
                    "exchange": exchange,
                    "year": year,
                    "status": STATUS_MISSING,
                    "expiries_pulled": 0,
                    "contracts_pulled": 0,
                    "last_fetched_at": None,
                    "error": None,
                }
            )
            continue
        rows.append(
            {
                "underlying": record.underlying_symbol,
                "exchange": record.exchange,
                "year": record.year,
                "status": record.status,
                "expiries_pulled": record.expiries_pulled,
                "contracts_pulled": record.contracts_pulled,
                "last_fetched_at": record.last_fetched_at.isoformat(),
                "error": record.error_message,
            }
        )
    return rows


def coverage_headline(summary: CoverageSummary) -> dict[str, Any]:
    """Return the ``X / Y complete`` headline dict for the KPI strip.

    Parameters
    ----------
    summary
        The reconciliation result from
        :func:`brutex.data.coverage_ledger.reconcile_coverage`.

    Returns
    -------
    dict[str, Any]
        ``{"complete", "total", "attempted", "not_pulled", "incomplete",
        "failed", "pending", "missing", "percent", "is_complete"}``.

        ``percent`` is the integer-rounded completion percentage (``100``
        when nothing is expected, since an empty grid is vacuously
        complete).

        **Honesty (the "894 missing looks like errors" fix):** a cell is
        ``missing`` only because the pull has *not reached it yet* — it is
        NOT an error. The two added fields name this plainly so the
        dashboard can lead with progress instead of a scary gap count:

        * ``attempted`` = ``complete + failed + pending + expiries_only`` —
          cells that have a persisted ledger row (the pull has touched them).
          This is the honest "how far has the run got" numerator.
        * ``not_pulled`` = ``missing`` — cells with no row yet (queued /
          in-progress), an alias named so the UI never implies an error.

        ``failed`` remains the ONLY count that means a real, recorded error
        (per :func:`~brutex.data.coverage_ledger.classify_cell`); a cell
        that ran and found zero expiries is ``pending`` (legitimately
        derivative-free that year), not a failure. ``expiries_only`` (Phase 1
        done, contracts pending — DEC-UNIVERSE-PULL-PHASED-001) is reported
        separately so a 0-contract F&O cell is NEVER counted as ``complete``.
    """
    attempted = summary.complete + summary.failed + summary.pending + summary.expiries_only
    return {
        "complete": summary.complete,
        "total": summary.total_expected,
        "attempted": attempted,
        "not_pulled": summary.missing,
        "incomplete": summary.incomplete,
        "failed": summary.failed,
        "pending": summary.pending,
        "expiries_only": summary.expiries_only,
        "missing": summary.missing,
        "percent": round(summary.complete_fraction * 100),
        "is_complete": summary.is_complete,
    }


def build_coverage_payload(
    records: Iterable[CoverageRecord],
    expected_underlyings: Iterable[tuple[str, str]],
    years: Sequence[int],
) -> dict[str, Any]:
    """Return the full ``/api/v1/coverage`` payload — headline + rows.

    Convenience composer so the FastAPI route is a single delegating call:
    materialises *records* once, reconciles for the headline, and builds the
    per-cell rows off the same inputs.

    Returns
    -------
    dict[str, Any]
        ``{"headline": {...}, "rows": [...]}``.
    """
    materialised = list(records)
    summary = reconcile_coverage(expected_underlyings, years, materialised)
    return {
        "headline": coverage_headline(summary),
        "rows": build_coverage_rows(materialised, expected_underlyings, years),
    }


def spot_coverage_headline(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Return the spot-coverage KPI headline — dataset + bar totals.

    Pure derivation off the per-dataset stats from
    :func:`brutex.dashboard.db_view.summarize_spot_datasets`.

    **Honest envelope (Golden Rule #10):** a dataset PRESENT in the lake counts
    as *pulled*. This headline reports HOW MANY spot datasets exist and their
    total bar count; it does NOT assert any date range is complete —
    partial-date-range detection is an explicit follow-up (DEC-COVERAGE-SPOT-001).

    Returns
    -------
    dict[str, Any]
        ``{"datasets": <int>, "total_bars": <int>}``.
    """
    return {
        "datasets": len(rows),
        "total_bars": sum(int(row["bars"]) for row in rows),
    }


def build_spot_coverage_payload(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Return the full ``/api/v1/coverage/spot`` payload — headline + rows.

    Composer mirroring :func:`build_coverage_payload` for the SPOT section of
    the ``/coverage`` page (DEC-COVERAGE-SPOT-001). *rows* are the per-dataset
    stats from :func:`brutex.dashboard.db_view.summarize_spot_datasets`
    (``exchange`` · ``segment`` · ``symbol`` · ``interval`` · ``bars`` ·
    ``first_date`` · ``last_date``). Pure projection: materialise once, derive
    the headline, pass the rows through unchanged — so the FastAPI route stays a
    thin delegating wrapper and this surface is 100% unit-testable offline.

    Returns
    -------
    dict[str, Any]
        ``{"headline": {"datasets", "total_bars"}, "rows": [...]}``.
    """
    materialised = list(rows)
    return {
        "headline": spot_coverage_headline(materialised),
        "rows": materialised,
    }


__all__ = [
    "STATUS_MISSING",
    "build_coverage_payload",
    "build_coverage_rows",
    "build_spot_coverage_payload",
    "coverage_headline",
    "spot_coverage_headline",
]
