"""Two-phase universe pull orchestration — pure, dependency-light core.

DEC-UNIVERSE-PULL-PHASED-001 (operator decree 2026-05-30): the universe pull
MUST complete **ALL expiry discovery first**, and only **after** that whole
phase settles may it pull **any** contracts. The prior pull (DEC-UNIVERSE-PULL-
PARALLEL-001, #319) interleaved them *per cell* (a cell's expiries → that
cell's contracts → next cell), which (a) the operator explicitly rejected and
(b) capped throughput at ~2.6 r/s because expiry discovery never ran wide.

## The two phases (locked ordering)

* **Phase 1 — expiries.** For every ``(exchange, underlying, year)`` cell in
  the universe×years grid, call the injected ``fetch_expiries``. All cells run
  through the injected ``run_parallel`` so the whole phase saturates the rate
  budget. The phase is "settled" when EVERY cell has been attempted (got
  expiries, returned empty, or failed after the fetcher's own retries).
* **Barrier.** :func:`run_two_phase` does not submit a single contract job
  until the Phase-1 iterator is fully drained — the ordering guarantee is
  structural, not advisory (asserted by ``test_no_contract_before_all_expiries``).
* **Phase 2 — contracts.** The ``(underlying, exchange, expiry)`` jobs are
  derived ONLY from the expiries Phase 1 actually discovered (handed over
  in-memory — no store read-back), then run through the same ``run_parallel``.

## Why pure (CLAUDE.md §11 COMMON + 100% testable)

The SDK/network fetchers, the thread pool, and the persistence sink are all
INJECTED:

* ``fetch_expiries(cell) -> Sequence[date]`` — may raise; the orchestrator
  records the failure and continues (one dead cell never blocks the phase).
* ``fetch_contracts(job) -> int`` — returns the persisted-contract count; may
  raise.
* ``run_parallel(fn, items) -> Iterable[ItemResult]`` — maps *fn* over *items*
  concurrently, yielding one :class:`ItemResult` per item. Production wires a
  ``ThreadPoolExecutor`` (``concurrent.futures.as_completed``); tests inject a
  synchronous fake so the ordering + barrier + worklist logic is provable with
  zero threads, zero network, zero DuckDB.
* ``on_expiry_cell`` / ``on_contract`` — per-result callbacks (persist a
  coverage row, emit a structlog line). Side effects live in the caller; this
  module owns only the *decision* of what to pull and in what order.

So the file imports + unit-tests with no native deps (mirrors
:mod:`brutex.data.coverage_ledger`), and gets its own strict per-module
100% line+branch gate.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Generic, TypeVar

#: One Phase-1 unit of work: a ``(exchange, underlying, year)`` cell.
ExpiryCell = tuple[str, str, int]

#: One Phase-2 unit of work: a ``(exchange, underlying, expiry_date)`` leg-set
#: to discover contracts for. ``exchange`` + ``underlying`` are carried so the
#: contract fetcher needs no back-reference to the originating cell.
ContractJob = tuple[str, str, date]

_ItemT = TypeVar("_ItemT")
_ResultT = TypeVar("_ResultT")


@dataclass(frozen=True, slots=True)
class ItemResult(Generic[_ItemT, _ResultT]):
    """Outcome of mapping a fetcher over one work item via ``run_parallel``.

    Exactly one of (*ok* + *result*) or (*exc*) is meaningful: when ``ok`` is
    ``True`` the fetcher returned *result*; when ``ok`` is ``False`` it raised
    *exc* (after its own retries) and *result* is ``None``.
    """

    item: _ItemT
    ok: bool
    result: _ResultT | None
    exc: BaseException | None


#: A concurrency primitive: map *fn* over *items*, yielding one
#: :class:`ItemResult` per item (order-independent). The orchestrator fully
#: consumes the Phase-1 iterator before building Phase-2, so a generator that
#: yields as futures complete still upholds the barrier.
RunParallel = Callable[
    [Callable[[_ItemT], _ResultT], Sequence[_ItemT]],
    Iterable[ItemResult[_ItemT, _ResultT]],
]


@dataclass(frozen=True, slots=True)
class TwoPhaseReport:
    """Aggregate outcome of one :func:`run_two_phase` run.

    Attributes
    ----------
    expiry_cells
        Total Phase-1 cells attempted.
    expiry_failures
        Phase-1 cells whose ``fetch_expiries`` raised (after its own retries).
    contracts_jobs
        Total Phase-2 ``(underlying, expiry)`` jobs attempted.
    contract_failures
        Phase-2 jobs whose ``fetch_contracts`` raised.
    expiries_discovered
        Sum of expiry dates discovered across all Phase-1 cells.
    contracts_persisted
        Sum of the per-job contract counts returned by ``fetch_contracts``.
    """

    expiry_cells: int
    expiry_failures: int
    contract_jobs: int
    contract_failures: int
    expiries_discovered: int
    contracts_persisted: int


def phase1_cells(
    underlyings: Iterable[tuple[str, str]],
    years: Iterable[int],
) -> list[ExpiryCell]:
    """Build the deterministic Phase-1 worklist: every ``(exchange, underlying, year)``.

    Parameters
    ----------
    underlyings
        ``(exchange, underlying_symbol)`` pairs in the brute-force/lake universe.
    years
        Calendar years to cover (Groww serves F&O from 2020).

    Returns
    -------
    list[ExpiryCell]
        Sorted ``(exchange, underlying, year)`` cells — sorted so the pull
        order is reproducible (CLAUDE.md §4 reproducibility) and tests are
        deterministic.
    """
    cells = [(exchange, underlying, year) for exchange, underlying in underlyings for year in years]
    return sorted(set(cells))


def contract_jobs_from(expiries_by_cell: dict[ExpiryCell, Sequence[date]]) -> list[ContractJob]:
    """Derive the Phase-2 contract worklist from Phase-1's discovered expiries.

    A contract job exists for an ``(exchange, underlying, expiry)`` ONLY when
    Phase 1 actually discovered that expiry — so contracts can never be pulled
    for an expiry the engine never saw (the operator's "never jump to contracts
    early" invariant, made structural).

    Each ``(exchange, underlying, expiry)`` is emitted once even if two cells
    (e.g. adjacent pull years) surfaced the same expiry date, so a contract
    leg-set is never fetched twice in one run (O(1) dedup via a set).

    Returns
    -------
    list[ContractJob]
        Sorted, de-duplicated ``(exchange, underlying, expiry_date)`` jobs.
    """
    jobs: set[ContractJob] = set()
    for (exchange, underlying, _year), expiries in expiries_by_cell.items():
        for expiry in expiries:
            jobs.add((exchange, underlying, expiry))
    return sorted(jobs)


def run_two_phase(
    cells: Sequence[ExpiryCell],
    *,
    fetch_expiries: Callable[[ExpiryCell], Sequence[date]],
    fetch_contracts: Callable[[ContractJob], int],
    run_parallel: RunParallel[ExpiryCell, Sequence[date]],
    run_parallel_contracts: RunParallel[ContractJob, int],
    on_expiry_cell: Callable[[ExpiryCell, Sequence[date], BaseException | None], None],
    on_contract: Callable[[ContractJob, int, BaseException | None], None],
) -> TwoPhaseReport:
    """Run Phase 1 (all expiries) to completion, then Phase 2 (all contracts).

    The barrier is structural: the Phase-1 ``run_parallel`` iterator is fully
    consumed — every cell's :class:`ItemResult` observed and its
    ``on_expiry_cell`` callback fired — before a single Phase-2 contract job is
    built or submitted. A failed cell (``fetch_expiries`` raised) is recorded
    and skipped for Phase 2 (no expiries ⇒ no contract jobs), so one dead cell
    never blocks the phase or the run.

    Parameters
    ----------
    cells
        The Phase-1 worklist from :func:`phase1_cells`.
    fetch_expiries
        Pulls one cell's expiry dates (wraps the SDK + retry in production).
    fetch_contracts
        Pulls + persists one job's contracts, returning the count.
    run_parallel, run_parallel_contracts
        Concurrency mappers for each phase (separate so the two element types
        stay type-safe; production passes the same pool-backed impl twice).
    on_expiry_cell
        Called once per Phase-1 cell with ``(cell, expiries, exc)`` — persists
        the coverage row + logs. ``expiries`` is empty when ``exc`` is set.
    on_contract
        Called once per Phase-2 job with ``(job, count, exc)``.

    Returns
    -------
    TwoPhaseReport
        Per-phase attempted / failure / discovered counts.
    """
    # ── Phase 1 — ALL expiries first ────────────────────────────────────
    expiries_by_cell: dict[ExpiryCell, Sequence[date]] = {}
    expiry_cells = 0
    expiry_failures = 0
    expiries_discovered = 0
    for outcome in run_parallel(fetch_expiries, list(cells)):
        expiry_cells += 1
        if outcome.ok:
            discovered = outcome.result or []
            expiries_by_cell[outcome.item] = discovered
            expiries_discovered += len(discovered)
            on_expiry_cell(outcome.item, discovered, None)
        else:
            expiry_failures += 1
            on_expiry_cell(outcome.item, [], outcome.exc)
    # ── Barrier: Phase 1 fully drained above; ONLY now do we build Phase 2 ──

    # ── Phase 2 — contracts, derived solely from discovered expiries ────
    jobs = contract_jobs_from(expiries_by_cell)
    contract_jobs = 0
    contract_failures = 0
    contracts_persisted = 0
    for outcome in run_parallel_contracts(fetch_contracts, jobs):
        contract_jobs += 1
        if outcome.ok:
            contracts_persisted += outcome.result or 0
            on_contract(outcome.item, outcome.result or 0, None)
        else:
            contract_failures += 1
            on_contract(outcome.item, 0, outcome.exc)

    return TwoPhaseReport(
        expiry_cells=expiry_cells,
        expiry_failures=expiry_failures,
        contract_jobs=contract_jobs,
        contract_failures=contract_failures,
        expiries_discovered=expiries_discovered,
        contracts_persisted=contracts_persisted,
    )


__all__ = [
    "ContractJob",
    "ExpiryCell",
    "ItemResult",
    "RunParallel",
    "TwoPhaseReport",
    "contract_jobs_from",
    "phase1_cells",
    "run_two_phase",
]
