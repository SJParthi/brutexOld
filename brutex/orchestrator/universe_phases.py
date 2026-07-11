"""Two-phase universe pull orchestration — pure, dependency-light core.

DEC-UNIVERSE-PULL-PHASED-001 (operator decree 2026-05-30): the universe pull
MUST complete **ALL expiry discovery first**, and only **after** that whole
phase settles may it pull **any** contracts. The prior pull (DEC-UNIVERSE-PULL-
PARALLEL-001, #319) interleaved them *per cell* (a cell's expiries → that
cell's contracts → next cell), which (a) the operator explicitly rejected and
(b) capped throughput at ~2.6 r/s because expiry discovery never ran wide.

Operator amendment (same day): the two phases are driven by **two manual
buttons under /universe** — "Fetch all expiries" (Phase 1) and "Fetch all
contracts" (Phase 2) — NOT an auto-pull on startup. Re-clicking must fetch
**only new work**: a successfully-pulled, immutable cell/expiry is skipped at
the API-call level on the next click (the incremental skip below).

## The two phases (locked ordering)

* **Phase 1 — expiries.** For every ``(exchange, underlying, year)`` cell in
  the universe×years grid, call the injected ``fetch_expiries``. All cells run
  through the injected ``run_parallel`` so the whole phase saturates the rate
  budget. The phase is "settled" when EVERY cell has been attempted (got
  expiries, returned empty, or failed after the fetcher's own retries).
* **Barrier.** :func:`run_two_phase` does not submit a single contract job
  until the Phase-1 iterable is fully drained — the ordering guarantee is
  structural, not advisory (asserted by ``test_no_contract_before_all_expiries``).
* **Phase 2 — contracts.** The ``(exchange, underlying, expiry)`` jobs are
  derived from the expiries Phase 1 discovered THIS run (handed over
  in-memory) UNIONED with the caller-injected *persisted_jobs* — a PRIOR
  run's registry-persisted expiries — then run through the same
  ``run_parallel``. The union (DEC-PHASE2-REGISTRY-REBUILD-001) is the
  re-pull path for contracts that FAILED after their Phase-1 cell settled:
  a settled past-year cell is skipped by the Phase-1 incremental skip, so
  its expiries are absent from this run's in-memory discoveries and an
  in-memory-only derivation would drop those failed jobs FOREVER. Both
  sources are vendor-discovered expiries (never invented), de-duplicated,
  and still filtered by the Phase-2 incremental skip below.

## Incremental skip (the "don't re-fetch what's already pulled" requirement)

Re-clicking a button must do only NEW work. The key insight is **immutability
by time**, not "pulled once = never again":

* **Phase 1 expiries** — a *past* year's expiry calendar is FINAL (the vendor never
  adds a 2021 expiry in 2026). :func:`cells_to_pull` skips
  ``(exchange, underlying, year)`` when ``year < current_year`` AND it is
  already settled (a complete/pending coverage row). The **current** year is
  always re-pulled (new expiries appear as the year runs); a previously
  **failed** cell is always re-pulled (it never settled).
* **Phase 2 contracts** — an *expired* expiry's contract set is FINAL.
  :func:`contract_jobs_to_pull` skips ``(exchange, underlying, expiry)`` when
  ``expiry < today`` AND already pulled; a **not-yet-expired** expiry is always
  re-pulled (new strikes get listed as the underlying moves).

Both filters are **O(1) per item** — a single ``frozenset`` membership test —
so the daily re-click is O(N) checks with an O(1) constant and issues only the
genuinely-new API calls. The persisted upsert is the second dedup line (its PK
inserts only new rows), but the skip happens BEFORE the network call so we
never spend a request on immutable data.

## Why pure (CLAUDE.md §11 COMMON + 100% testable)

The SDK/network fetchers, the thread pool, and the persistence sink are all
INJECTED:

* ``fetch_expiries(cell) -> Sequence[date]`` — may raise; the orchestrator
  records the failure and continues (one dead cell never blocks the phase).
* ``fetch_contracts(job) -> int`` — returns the persisted-contract count; may
  raise.
* ``run_parallel`` — a :class:`RunParallel` mapping *fn* over *items*
  concurrently, yielding one :class:`ItemResult` per item. Production wires a
  ``ThreadPoolExecutor`` (``concurrent.futures.as_completed``); tests inject a
  synchronous fake so the ordering + barrier + worklist logic is provable with
  zero threads, zero network, zero DuckDB.
* ``on_expiry_cell`` / ``on_contract`` — per-result callbacks (persist a
  coverage row, emit a structlog line). Side effects live in the caller; this
  module owns only the *decision* of what to pull, in what order, skipping what.

So the file imports + unit-tests with no native deps (mirrors
:mod:`brutex.data.coverage_ledger`), and gets its own strict per-module
100% line+branch gate.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Protocol

#: One Phase-1 unit of work: a ``(exchange, underlying, year)`` cell.
ExpiryCell = tuple[str, str, int]

#: One Phase-2 unit of work: a ``(exchange, underlying, expiry_date)`` leg-set
#: to discover contracts for. ``exchange`` + ``underlying`` are carried so the
#: contract fetcher needs no back-reference to the originating cell.
ContractJob = tuple[str, str, date]


@dataclass(frozen=True, slots=True)
class ItemResult[ItemT, ResultT]:
    """Outcome of mapping a fetcher over one work item via ``run_parallel``.

    Exactly one of (*ok* + *result*) or (*exc*) is meaningful: when ``ok`` is
    ``True`` the fetcher returned *result*; when ``ok`` is ``False`` it raised
    *exc* (after its own retries) and *result* is ``None``.
    """

    item: ItemT
    ok: bool
    result: ResultT | None
    exc: BaseException | None


class RunParallel(Protocol):
    """A concurrency primitive: map *fn* over *items*, one :class:`ItemResult` each.

    A :class:`~typing.Protocol` with a *generic method* so ONE instance serves
    both phases — ``fetch_expiries`` (``ExpiryCell → Sequence[date]``) and
    ``fetch_contracts`` (``ContractJob → int``) — keeping
    :func:`run_two_phase`'s signature to a single concurrency param while
    staying type-safe per call. :func:`run_two_phase` fully consumes the
    Phase-1 iterable before building Phase-2, so a generator that yields as
    futures complete still upholds the barrier.
    """

    def __call__[ItemT, ResultT](
        self,
        fn: Callable[[ItemT], ResultT],
        items: Sequence[ItemT],
    ) -> Iterable[ItemResult[ItemT, ResultT]]:
        """Map *fn* over *items*, yielding one :class:`ItemResult` each."""
        ...


@dataclass(frozen=True, slots=True)
class TwoPhaseReport:
    """Aggregate outcome of one :func:`run_two_phase` run.

    Attributes
    ----------
    expiry_cells
        Total Phase-1 cells attempted (after the incremental skip).
    expiry_failures
        Phase-1 cells whose ``fetch_expiries`` raised (after its own retries).
    contract_jobs
        Total Phase-2 ``(exchange, underlying, expiry)`` jobs attempted.
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
        Calendar years to cover (vendor historical data from 2021).

    Returns
    -------
    list[ExpiryCell]
        Sorted, de-duplicated ``(exchange, underlying, year)`` cells — sorted so
        the pull order is reproducible (CLAUDE.md §4) and tests are deterministic.
    """
    cells = [(exchange, underlying, year) for exchange, underlying in underlyings for year in years]
    return sorted(set(cells))


def cells_to_pull(
    cells: Iterable[ExpiryCell],
    *,
    settled: frozenset[ExpiryCell],
    current_year: int,
) -> list[ExpiryCell]:
    """Filter Phase-1 cells to only those needing a (re)pull — the incremental skip.

    A cell is SKIPPED (its expiry calendar is immutable + already captured) iff
    ``year < current_year`` AND it is in *settled*. Everything else is pulled:

    * the **current** year — new expiries appear as the year progresses;
    * any **未settled** past year — never successfully captured yet (missing or
      a previously *failed* cell the caller excluded from *settled*).

    O(1) per cell (one ``frozenset`` membership), so a daily re-click is O(N)
    checks issuing only genuinely-new API calls.

    Parameters
    ----------
    cells
        The full Phase-1 worklist (e.g. from :func:`phase1_cells`).
    settled
        ``(exchange, underlying, year)`` cells already pulled to a FINAL state
        (the caller builds this from the coverage ledger — complete/pending
        rows, deliberately EXCLUDING ``failed`` so failures always retry).
    current_year
        The current calendar year (injected for determinism / testability).

    Returns
    -------
    list[ExpiryCell]
        The cells to actually fetch, order preserved.
    """
    return [cell for cell in cells if not (cell[2] < current_year and cell in settled)]


def contract_jobs_from(
    expiries_by_cell: dict[ExpiryCell, Sequence[date]],
    *,
    persisted: Iterable[ContractJob] = (),
) -> list[ContractJob]:
    """Derive the Phase-2 contract worklist from Phase-1's discovered expiries.

    A contract job exists for an ``(exchange, underlying, expiry)`` ONLY when
    Phase 1 actually discovered that expiry — THIS run (*expiries_by_cell*) or a
    PRIOR run whose discovery was persisted to the registry (*persisted*,
    DEC-PHASE2-REGISTRY-REBUILD-001) — so contracts can never be pulled for an
    expiry the engine never saw (the operator's "never jump to contracts early"
    invariant, made structural for BOTH sources). The *persisted* union is the
    re-pull path for contracts that FAILED after their cell settled
    ``expiries_only``: the settled past-year cell is skipped on a re-run
    (:func:`cells_to_pull`), so its expiries never re-enter *expiries_by_cell*,
    and without the union the failed jobs would never be rebuilt.

    Each ``(exchange, underlying, expiry)`` is emitted once even if two cells
    (e.g. adjacent pull years) surfaced the same expiry date and/or it also
    appears in *persisted*, so a contract leg-set is never fetched twice in one
    run (O(1) dedup via a set).

    Parameters
    ----------
    expiries_by_cell
        This run's Phase-1 discoveries, keyed by their discovering cell.
    persisted
        Previously-persisted ``(exchange, underlying, expiry)`` triples (e.g.
        the registry's expiry rows). Empty (the default) keeps the derivation
        byte-identical to the in-memory-only behaviour.

    Returns
    -------
    list[ContractJob]
        Sorted, de-duplicated ``(exchange, underlying, expiry_date)`` jobs.
    """
    jobs: set[ContractJob] = set(persisted)
    for (exchange, underlying, _year), expiries in expiries_by_cell.items():
        for expiry in expiries:
            jobs.add((exchange, underlying, expiry))
    return sorted(jobs)


def contract_jobs_to_pull(
    jobs: Iterable[ContractJob],
    *,
    settled: frozenset[ContractJob],
    today: date,
) -> list[ContractJob]:
    """Filter Phase-2 jobs to only those needing a (re)pull — the incremental skip.

    A job is SKIPPED (its contract set is immutable + already captured) iff its
    ``expiry < today`` AND it is in *settled*. A **not-yet-expired** expiry is
    always re-pulled — new strikes get listed as the underlying moves — and any
    un-captured expired job is pulled once.

    O(1) per job (one ``frozenset`` membership).

    Parameters
    ----------
    jobs
        The full Phase-2 worklist (e.g. from :func:`contract_jobs_from`).
    settled
        ``(exchange, underlying, expiry)`` jobs already pulled (the caller
        builds this from the persisted contracts/coverage).
    today
        The current date (injected for determinism / testability).

    Returns
    -------
    list[ContractJob]
        The jobs to actually fetch, order preserved.
    """
    return [job for job in jobs if not (job[2] < today and job in settled)]


#: Coverage statuses that mean a Phase-1 cell reached a FINAL state for its
#: year — its EXPIRY calendar will not change. ``complete`` (combined pull) and
#: ``expiries_only`` (Phase-1-only — expiries done, contracts a separate phase)
#: both mean the expiries are captured; ``pending`` means it ran + found zero
#: expiries (a derivative-free year, immutable). ``failed`` is deliberately
#: EXCLUDED so a previously-errored cell is always re-attempted; ``missing`` is
#: not a persisted status (a never-attempted cell has no row at all). NOTE this
#: is the PHASE-1 (expiry) settled set — a settled past year is skipped on a
#: re-click of "Fetch all expiries"; contract completeness is Phase 2's own
#: skip (``settled_contract_jobs``), independent of this.
_SETTLED_STATUSES: frozenset[str] = frozenset({"complete", "expiries_only", "pending"})


def settled_expiry_cells(
    rows: Iterable[tuple[str, str, int, str]],
) -> frozenset[ExpiryCell]:
    """Build the Phase-1 *settled* skip-set for :func:`cells_to_pull` from coverage rows.

    Each row is ``(exchange, underlying, year, status)`` — exactly the columns
    the caller projects from ``store.list_coverage()``. A cell counts as settled
    iff its status is ``complete`` or ``pending`` (:data:`_SETTLED_STATUSES` — it
    ran and reached a final state); a ``failed`` row is EXCLUDED so failures
    always retry. Pairing this with :func:`cells_to_pull` (which only skips a
    settled *past* year) gives the operator's "re-click pulls only new work" with
    a daily current-year refresh.

    The result is a :class:`frozenset` so the downstream membership test is O(1)
    per cell (CLAUDE.md §5). Building it is one O(R) pass over R coverage rows.

    Returns
    -------
    frozenset[ExpiryCell]
        The settled ``(exchange, underlying, year)`` cells.
    """
    return frozenset(
        (exchange, underlying, year)
        for exchange, underlying, year, status in rows
        if status in _SETTLED_STATUSES
    )


def settled_contract_jobs(
    rows: Iterable[tuple[str, str, date]],
) -> frozenset[ContractJob]:
    """Build the Phase-2 *settled* skip-set for :func:`contract_jobs_to_pull`.

    Each row is ``(exchange, underlying, expiry_date)`` — the distinct
    ``(exchange, underlying_symbol, expiry_date)`` triples the caller projects
    from ``store.list_contracts()`` (a persisted contract for that expiry means
    its leg-set was already pulled). :func:`contract_jobs_to_pull` then skips a
    job only when it is BOTH in this set AND already expired, so a not-yet-
    expired expiry is always re-pulled (new strikes).

    The result is a :class:`frozenset` so the downstream membership test is O(1)
    per job (CLAUDE.md §5).

    Returns
    -------
    frozenset[ContractJob]
        The already-pulled ``(exchange, underlying, expiry)`` jobs.
    """
    return frozenset(rows)


def _reraise_if_market_hours_halt(exc: BaseException | None) -> None:
    """Re-raise *exc* iff it is a market-hours global stop (DEC-GROWW-MARKET-HOURS-GUARD-001).

    The two-phase engine captures every fetcher exception per cell (record-and-resume);
    a :class:`~brutex.data.errors.MarketHoursHalt` (the metadata phase crossed into the
    NSE trading window) is the ONE class that must instead halt the whole phase — the
    metadata twin of the ingest spines' ``_GLOBAL_STOP_ERRORS`` carve-out. Imported
    INSIDE the call so this low-level orchestrator stays import-light. ``None`` (a clean
    cell) is a no-op.
    """
    from brutex.data.errors import MarketHoursHalt

    if isinstance(exc, MarketHoursHalt):
        raise exc


def run_two_phase(
    cells: Sequence[ExpiryCell],
    *,
    fetch_expiries: Callable[[ExpiryCell], Sequence[date]],
    fetch_contracts: Callable[[ContractJob], int],
    run_parallel: RunParallel,
    on_expiry_cell: Callable[[ExpiryCell, Sequence[date], BaseException | None], None],
    on_contract: Callable[[ContractJob, int, BaseException | None], None],
    contract_skip: frozenset[ContractJob] = frozenset(),
    persisted_jobs: Iterable[ContractJob] = (),
    today: date | None = None,
) -> TwoPhaseReport:
    """Run Phase 1 (all expiries) to completion, then Phase 2 (all contracts).

    The barrier is structural: the Phase-1 ``run_parallel`` iterable is fully
    consumed — every cell's :class:`ItemResult` observed and its
    ``on_expiry_cell`` callback fired — before a single Phase-2 contract job is
    built or submitted. A failed cell (``fetch_expiries`` raised) is recorded
    and skipped for Phase 2 (no expiries ⇒ no contract jobs), so one dead cell
    never blocks the phase or the run.

    Phase-1 cell skipping is the caller's job (filter *cells* with
    :func:`cells_to_pull` before calling); Phase-2 skipping happens HERE via
    *contract_skip* + *today* / :func:`contract_jobs_to_pull`, because the job
    list is only known after Phase 1 discovers the expiries.

    Parameters
    ----------
    cells
        The Phase-1 worklist (already incrementally filtered by the caller).
    fetch_expiries
        Pulls one cell's expiry dates (wraps the SDK + retry in production).
    fetch_contracts
        Pulls + persists one job's contracts, returning the count.
    run_parallel
        Concurrency mapper used for BOTH phases (a generic-method
        :class:`RunParallel`). Production passes one pool-backed impl.
    on_expiry_cell
        Called once per Phase-1 cell with ``(cell, expiries, exc)`` — persists
        the coverage row + logs. ``expiries`` is empty when ``exc`` is set.
    on_contract
        Called once per Phase-2 job with ``(job, count, exc)``.
    contract_skip
        ``(exchange, underlying, expiry)`` jobs already pulled — combined with
        *today* to drop immutable expired jobs before the network call.
    persisted_jobs
        Previously-persisted ``(exchange, underlying, expiry)`` triples (the
        registry's expiry rows, injected by the caller — no store read here)
        UNIONED into the Phase-2 worklist alongside this run's discoveries
        (DEC-PHASE2-REGISTRY-REBUILD-001). This is the re-pull path for
        contracts that FAILED after their Phase-1 cell settled ``expiries_only``
        (the skipped cell contributes no in-memory expiries on a re-run). The
        union is de-duplicated and *contract_skip* still applies — a job whose
        contracts are already captured + expired is dropped, so a healthy
        re-run issues no extra call. Empty (the default) is byte-identical to
        the prior in-memory-only derivation.
    today
        Current date for the Phase-2 skip; defaults to ``date.today()``. When
        *contract_skip* is empty the value is immaterial (nothing is skipped).

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
    for e_outcome in run_parallel(fetch_expiries, list(cells)):
        expiry_cells += 1
        if e_outcome.ok:
            discovered = e_outcome.result or []
            expiries_by_cell[e_outcome.item] = discovered
            expiries_discovered += len(discovered)
            on_expiry_cell(e_outcome.item, discovered, None)
        else:
            expiry_failures += 1
            on_expiry_cell(e_outcome.item, [], e_outcome.exc)
            # DEC-GROWW-MARKET-HOURS-GUARD-001: a fetcher that raised MarketHoursHalt
            # (the metadata phase crossed into the NSE trading window) is a PULL-WIDE
            # global stop — re-raise it past the per-cell record-and-resume so the
            # phase halts like the ingest spines instead of fail-fasting every
            # remaining cell. The cell's outcome was already recorded above.
            _reraise_if_market_hours_halt(e_outcome.exc)
    # ── Barrier: Phase 1 fully drained above; ONLY now do we build Phase 2 ──

    # ── Phase 2 — contracts, from discovered ∪ persisted expiries ───────
    jobs = contract_jobs_from(expiries_by_cell, persisted=persisted_jobs)
    jobs = contract_jobs_to_pull(jobs, settled=contract_skip, today=today or date.today())
    contract_jobs = 0
    contract_failures = 0
    contracts_persisted = 0
    for c_outcome in run_parallel(fetch_contracts, jobs):
        contract_jobs += 1
        if c_outcome.ok:
            contracts_persisted += c_outcome.result or 0
            on_contract(c_outcome.item, c_outcome.result or 0, None)
        else:
            contract_failures += 1
            on_contract(c_outcome.item, 0, c_outcome.exc)
            # DEC-GROWW-MARKET-HOURS-GUARD-001: a Phase-2 MarketHoursHalt halts the pull.
            _reraise_if_market_hours_halt(c_outcome.exc)

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
    "cells_to_pull",
    "contract_jobs_from",
    "contract_jobs_to_pull",
    "phase1_cells",
    "run_two_phase",
    "settled_contract_jobs",
    "settled_expiry_cells",
]
