"""Completeness-ledger reconciliation — pure, dependency-light core.

This module makes the operator question *"did we pull ALL expiries +
contracts from 2020 for every underlying?"* **provable** and
**recoverable**. It is the testable heart of DEC-COVERAGE-LEDGER-001: it
holds the per-``(exchange, underlying, year)`` cell record, the rule that
classifies a cell as ``complete`` / ``failed`` / ``pending``, and the
reconciliation that diffs the *expected* universe×years grid against the
*actual* persisted ledger rows to produce a re-pull worklist.

## Why this is a separate, pure module (CLAUDE.md §11 COMMON + DYNAMIC)

The DuckDB ``coverage_ledger`` table (persistence) lives in
:mod:`brutex.data.instrument_store`; the live Groww pull glue lives in
:mod:`brutex.cli` (``# pragma: no cover`` — needs creds + SDK + network);
the dashboard routes live in :mod:`brutex.dashboard.app` (FastAPI /
coverage-omit). All three delegate the *decisions* here so the
classification + reconciliation rule lives in exactly ONE place and is
100% line+branch unit-testable offline — no DuckDB, no SDK, no network.

It lives under :mod:`brutex.data` (not :mod:`brutex.orchestrator`)
deliberately: the ``brutex.orchestrator`` package ``__init__`` eagerly
imports the ``fastbloom_rs`` PyO3 wheel (an abi3-py38 single-init
extension), which makes a *standalone* ``--cov`` branch-coverage gate on
any orchestrator submodule impossible (the same documented limit that
keeps ``brutex.orchestrator.replay`` / ``.retry`` off the strict gate).
``brutex.data``'s package init is light, so this module gets its own
strict per-module 100% gate (DEC-COVERAGE-LEDGER-001).

## The classification rule (honest + explicit)

:func:`classify_cell` is intentionally simple and total:

* an **error** message set (non-``None``, non-empty)  → ``"failed"``
  (a recorded exception always dominates — even if some expiries were
  pulled before it raised, the cell is not trustworthy as complete);
* otherwise, **at least one expiry pulled** (``expiries_pulled > 0``)
  → ``"complete"``;
* otherwise (no error, zero expiries)                  → ``"pending"``.

The rule keys completeness off ``expiries_pulled`` (not
``contracts_pulled``) deliberately: a non-F&O underlying (e.g. a sectoral
index) legitimately has **zero** contracts but is still "complete" once
its expiry discovery ran and returned its (possibly empty after the first
non-empty) set. A cell with zero expiries and no error has simply not run
yet → ``"pending"``. This is an **honest** completeness signal, not a
guarantee that every contract on the chain was enriched; that deeper
guarantee is tracked separately (it would need an expected-contract count
the discovery API does not expose up-front).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

#: The three terminal states a coverage cell can hold.
STATUS_COMPLETE: Final[str] = "complete"
STATUS_FAILED: Final[str] = "failed"
STATUS_PENDING: Final[str] = "pending"

#: Every admissible status, in display order (complete first).
COVERAGE_STATUSES: Final[tuple[str, ...]] = (
    STATUS_COMPLETE,
    STATUS_FAILED,
    STATUS_PENDING,
)


@dataclass(frozen=True, slots=True)
class CoverageRecord:
    """One persisted coverage-ledger cell, mirroring the DuckDB table.

    A cell is uniquely keyed by ``(exchange, underlying_symbol, year)`` —
    the same primary key as the ``coverage_ledger`` table. It records how
    many expiries + contracts were pulled for that underlying in that
    calendar year, the cell's :data:`COVERAGE_STATUSES` status, the run id
    + timestamp of the last attempt, and the error message of the last
    failure (``None`` when the cell never failed).

    Attributes
    ----------
    exchange
        Exchange token (e.g. ``"NSE"`` / ``"BSE"``).
    underlying_symbol
        The underlying the expiries/contracts were pulled for.
    year
        Calendar year of the pull (Groww serves F&O from 2020).
    expiries_pulled
        Count of expiry dates discovered for ``(underlying, year)``.
    contracts_pulled
        Count of contract legs discovered across those expiries.
    status
        One of :data:`COVERAGE_STATUSES`. Callers should derive it from
        :func:`classify_cell` rather than setting it ad hoc.
    last_run_id
        The ingest run id that last touched this cell.
    last_fetched_at
        When the cell was last attempted (timezone-aware).
    error_message
        The last failure's message, or ``None`` if the cell never failed.
    """

    exchange: str
    underlying_symbol: str
    year: int
    expiries_pulled: int
    contracts_pulled: int
    status: str
    last_run_id: str
    last_fetched_at: datetime
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class CoverageSummary:
    """Reconciliation outcome — the headline + the re-pull worklist.

    Attributes
    ----------
    total_expected
        ``len(expected_underlyings) × len(years)`` — every cell that
        SHOULD exist for the universe to be provably complete.
    complete
        Count of cells whose status is ``"complete"``.
    failed
        Count of cells whose status is ``"failed"``.
    pending
        Count of cells whose status is ``"pending"``.
    missing
        Count of expected cells that have NO persisted ledger row at all
        (never attempted). These are a subset of "incomplete" and are
        always included in :attr:`to_repull`.
    to_repull
        The deterministic, sorted list of ``(exchange, underlying, year)``
        cells that are not ``"complete"`` — every missing, pending, or
        failed cell — i.e. exactly what a resume pass must re-pull.
    """

    total_expected: int
    complete: int
    failed: int
    pending: int
    missing: int
    to_repull: tuple[tuple[str, str, int], ...]

    @property
    def incomplete(self) -> int:
        """Return the number of expected cells that are not ``"complete"``."""
        return len(self.to_repull)

    @property
    def is_complete(self) -> bool:
        """Return ``True`` iff every expected cell is ``"complete"``."""
        return not self.to_repull

    @property
    def complete_fraction(self) -> float:
        """Return ``complete / total_expected`` (``1.0`` when nothing is expected).

        An empty expected grid is vacuously complete, so the fraction is
        ``1.0`` to avoid a divide-by-zero and to read sensibly in the
        dashboard headline.
        """
        if self.total_expected == 0:
            return 1.0
        return self.complete / self.total_expected


def classify_cell(
    expiries_pulled: int,
    contracts_pulled: int,
    error: str | None,
) -> str:
    """Classify one coverage cell into a :data:`COVERAGE_STATUSES` status.

    The rule (documented honestly in the module docstring):

    * a non-empty *error* → ``"failed"`` (a recorded exception dominates);
    * else ``expiries_pulled > 0`` → ``"complete"``;
    * else → ``"pending"`` (never ran, or ran but discovered nothing).

    ``contracts_pulled`` is accepted for signature symmetry with the table
    columns but does NOT affect completeness — a legitimately
    derivative-free underlying (e.g. a sectoral index) has zero contracts
    yet is complete once its expiry discovery ran.

    Parameters
    ----------
    expiries_pulled
        Count of expiry dates discovered for the cell.
    contracts_pulled
        Count of contract legs discovered (not used by the rule).
    error
        The last failure's message, or ``None`` / empty when the cell did
        not fail.

    Returns
    -------
    str
        One of :data:`STATUS_COMPLETE`, :data:`STATUS_FAILED`,
        :data:`STATUS_PENDING`.
    """
    del contracts_pulled  # accepted for symmetry; not part of the rule
    if error:
        return STATUS_FAILED
    if expiries_pulled > 0:
        return STATUS_COMPLETE
    return STATUS_PENDING


def reconcile_coverage(
    expected_underlyings: Iterable[tuple[str, str]],
    years: Sequence[int],
    actual: Iterable[CoverageRecord],
) -> CoverageSummary:
    """Diff the expected universe×years grid against the actual ledger rows.

    Builds the full expected set of ``(exchange, underlying, year)`` cells
    from *expected_underlyings* × *years*, then walks *actual* once to tally
    each expected cell's status. A cell with no persisted row is *missing*
    (counted + always re-pulled); a persisted cell that is not
    ``"complete"`` (``"pending"`` / ``"failed"``) is also re-pulled. Rows
    in *actual* that are not in the expected grid are ignored (they belong
    to a different universe/year scope and are not this pass's concern).

    Parameters
    ----------
    expected_underlyings
        ``(exchange, underlying_symbol)`` pairs that SHOULD have coverage.
    years
        Calendar years that SHOULD have coverage for every underlying.
    actual
        The persisted :class:`CoverageRecord` rows (e.g. from
        ``store.list_coverage()``).

    Returns
    -------
    CoverageSummary
        Headline counts + the deterministic sorted re-pull worklist.
    """
    expected: set[tuple[str, str, int]] = set()
    for exchange, underlying in expected_underlyings:
        for year in years:
            expected.add((exchange, underlying, year))

    by_cell: dict[tuple[str, str, int], CoverageRecord] = {}
    for record in actual:
        key = (record.exchange, record.underlying_symbol, record.year)
        if key in expected:
            by_cell[key] = record

    complete = 0
    failed = 0
    pending = 0
    missing = 0
    repull: list[tuple[str, str, int]] = []
    for cell in expected:
        persisted = by_cell.get(cell)
        if persisted is None:
            missing += 1
            repull.append(cell)
            continue
        if persisted.status == STATUS_COMPLETE:
            complete += 1
            continue
        if persisted.status == STATUS_FAILED:
            failed += 1
        else:
            pending += 1
        repull.append(cell)

    return CoverageSummary(
        total_expected=len(expected),
        complete=complete,
        failed=failed,
        pending=pending,
        missing=missing,
        to_repull=tuple(sorted(repull)),
    )


def cells_to_repull(summary: CoverageSummary) -> tuple[tuple[str, str, int], ...]:
    """Return only the incomplete cells a resume pass must re-pull.

    Thin, intention-revealing accessor over
    :attr:`CoverageSummary.to_repull` so the resumable live path
    (:mod:`brutex.cli`, ``# pragma: no cover``) reads as a single named
    call. The decision (which cells) is the reconciliation's; this keeps
    the cli glue thin.

    Parameters
    ----------
    summary
        The output of :func:`reconcile_coverage`.

    Returns
    -------
    tuple[tuple[str, str, int], ...]
        The sorted ``(exchange, underlying, year)`` cells to re-pull.
    """
    return summary.to_repull


__all__ = [
    "COVERAGE_STATUSES",
    "STATUS_COMPLETE",
    "STATUS_FAILED",
    "STATUS_PENDING",
    "CoverageRecord",
    "CoverageSummary",
    "cells_to_repull",
    "classify_cell",
    "reconcile_coverage",
]
