"""Unit tests for brutex.orchestrator.universe_phases.

Proves the two-phase ordering guarantee (DEC-UNIVERSE-PULL-PHASED-001): ALL
expiries are fetched + observed before ANY contract is fetched. Uses a
synchronous ``run_parallel`` fake + a call-order recorder, so the barrier is
provable with zero threads / SDK / network.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date

import pytest
from brutex.orchestrator.universe_phases import (
    ContractJob,
    ExpiryCell,
    ItemResult,
    TwoPhaseReport,
    cells_to_pull,
    contract_jobs_from,
    contract_jobs_to_pull,
    phase1_cells,
    run_two_phase,
    settled_contract_jobs,
    settled_expiry_cells,
)

# ────────────────────────────────────────────────────────────────────────
# A synchronous run_parallel fake — preserves submit order, no threads.
# ────────────────────────────────────────────────────────────────────────


def _sync_run_parallel[ItemT, ResultT](
    fn: Callable[[ItemT], ResultT],
    items: Sequence[ItemT],
) -> list[ItemResult[ItemT, ResultT]]:
    """Map *fn* over *items* in order, capturing each result/exception.

    Generic (PEP 695) so it structurally satisfies the generic-method
    :class:`~brutex.orchestrator.universe_phases.RunParallel` Protocol that
    ``run_two_phase`` expects — both under mypy --strict and pyright.
    """
    out: list[ItemResult[ItemT, ResultT]] = []
    for item in items:
        try:
            result = fn(item)
        except Exception as exc:
            out.append(ItemResult(item=item, ok=False, result=None, exc=exc))
        else:
            out.append(ItemResult(item=item, ok=True, result=result, exc=None))
    return out


_D = date  # terse alias for the fixtures below


# ────────────────────────────────────────────────────────────────────────
# phase1_cells
# ────────────────────────────────────────────────────────────────────────


class TestPhase1Cells:
    def test_full_grid(self) -> None:
        cells = phase1_cells([("NSE", "NIFTY"), ("BSE", "SENSEX")], [2024, 2025])
        assert cells == [
            ("BSE", "SENSEX", 2024),
            ("BSE", "SENSEX", 2025),
            ("NSE", "NIFTY", 2024),
            ("NSE", "NIFTY", 2025),
        ]

    def test_empty_underlyings(self) -> None:
        assert phase1_cells([], [2024]) == []

    def test_empty_years(self) -> None:
        assert phase1_cells([("NSE", "NIFTY")], []) == []

    def test_deduped_and_sorted(self) -> None:
        # Duplicate (exchange, underlying) collapses; output is sorted + unique.
        cells = phase1_cells([("NSE", "NIFTY"), ("NSE", "NIFTY")], [2025, 2024])
        assert cells == [("NSE", "NIFTY", 2024), ("NSE", "NIFTY", 2025)]


# ────────────────────────────────────────────────────────────────────────
# contract_jobs_from
# ────────────────────────────────────────────────────────────────────────


class TestContractJobsFrom:
    def test_one_cell_many_expiries(self) -> None:
        by_cell: dict[ExpiryCell, Sequence[date]] = {
            ("NSE", "NIFTY", 2024): [_D(2024, 1, 25), _D(2024, 2, 29)],
        }
        assert contract_jobs_from(by_cell) == [
            ("NSE", "NIFTY", _D(2024, 1, 25)),
            ("NSE", "NIFTY", _D(2024, 2, 29)),
        ]

    def test_same_expiry_across_cells_deduped(self) -> None:
        # Two pull-years that surface the SAME expiry date for one underlying
        # must yield ONE contract job (O(1) set dedup) — never fetched twice.
        shared = _D(2024, 12, 26)
        by_cell: dict[ExpiryCell, Sequence[date]] = {
            ("NSE", "NIFTY", 2024): [shared],
            ("NSE", "NIFTY", 2025): [shared],
        }
        assert contract_jobs_from(by_cell) == [("NSE", "NIFTY", shared)]

    def test_cell_with_zero_expiries_yields_no_jobs(self) -> None:
        by_cell: dict[ExpiryCell, Sequence[date]] = {("NSE", "ABC", 2020): []}
        assert contract_jobs_from(by_cell) == []

    def test_empty(self) -> None:
        assert contract_jobs_from({}) == []

    def test_sorted_across_underlyings(self) -> None:
        by_cell: dict[ExpiryCell, Sequence[date]] = {
            ("NSE", "ZEEL", 2024): [_D(2024, 1, 25)],
            ("BSE", "AAA", 2024): [_D(2024, 1, 25)],
        }
        jobs = contract_jobs_from(by_cell)
        assert jobs[0][0] == "BSE"  # sorted: BSE before NSE


# ────────────────────────────────────────────────────────────────────────
# run_two_phase — the ordering guarantee + counts
# ────────────────────────────────────────────────────────────────────────


class TestRunTwoPhase:
    def _run(
        self,
        cells: list[ExpiryCell],
        expiries: dict[ExpiryCell, list[date]],
        *,
        contract_count: int = 3,
        fail_expiry: set[ExpiryCell] | None = None,
        fail_contract: set[ContractJob] | None = None,
        order_log: list[str] | None = None,
    ) -> TwoPhaseReport:
        fail_expiry = fail_expiry or set()
        fail_contract = fail_contract or set()

        def fetch_expiries(cell: ExpiryCell) -> Sequence[date]:
            if order_log is not None:
                order_log.append(f"E:{cell[1]}:{cell[2]}")
            if cell in fail_expiry:
                raise RuntimeError(f"expiry boom {cell}")
            return expiries.get(cell, [])

        def fetch_contracts(job: ContractJob) -> int:
            if order_log is not None:
                order_log.append(f"C:{job[1]}:{job[2].isoformat()}")
            if job in fail_contract:
                raise RuntimeError(f"contract boom {job}")
            return contract_count

        return run_two_phase(
            cells,
            fetch_expiries=fetch_expiries,
            fetch_contracts=fetch_contracts,
            run_parallel=_sync_run_parallel,
            on_expiry_cell=lambda *_: None,
            on_contract=lambda *_: None,
        )

    def test_no_contract_before_all_expiries(self) -> None:
        """THE invariant: every Phase-1 (E:) call precedes every Phase-2 (C:)."""
        cells = [("NSE", "A", 2024), ("NSE", "B", 2024)]
        expiries = {
            ("NSE", "A", 2024): [_D(2024, 1, 25)],
            ("NSE", "B", 2024): [_D(2024, 2, 29)],
        }
        order: list[str] = []
        self._run(cells, expiries, order_log=order)
        last_expiry_idx = max(i for i, s in enumerate(order) if s.startswith("E:"))
        first_contract_idx = min(i for i, s in enumerate(order) if s.startswith("C:"))
        assert last_expiry_idx < first_contract_idx, order

    def test_happy_path_counts(self) -> None:
        cells = [("NSE", "A", 2024), ("NSE", "B", 2024)]
        expiries = {
            ("NSE", "A", 2024): [_D(2024, 1, 25), _D(2024, 2, 29)],
            ("NSE", "B", 2024): [_D(2024, 3, 28)],
        }
        report = self._run(cells, expiries, contract_count=5)
        assert report.expiry_cells == 2
        assert report.expiry_failures == 0
        assert report.expiries_discovered == 3
        assert report.contract_jobs == 3  # 2 + 1 expiries
        assert report.contract_failures == 0
        assert report.contracts_persisted == 15  # 3 jobs × 5

    def test_expiry_failure_recorded_and_skipped_in_phase2(self) -> None:
        cells = [("NSE", "A", 2024), ("NSE", "B", 2024)]
        expiries = {("NSE", "B", 2024): [_D(2024, 3, 28)]}
        report = self._run(cells, expiries, fail_expiry={("NSE", "A", 2024)}, contract_count=2)
        assert report.expiry_cells == 2
        assert report.expiry_failures == 1
        # Only B's single expiry becomes a contract job; A (failed) yields none.
        assert report.contract_jobs == 1
        assert report.contracts_persisted == 2

    def test_contract_failure_recorded(self) -> None:
        cells = [("NSE", "A", 2024)]
        job = ("NSE", "A", _D(2024, 1, 25))
        expiries = {("NSE", "A", 2024): [_D(2024, 1, 25)]}
        report = self._run(cells, expiries, fail_contract={job})
        assert report.contract_jobs == 1
        assert report.contract_failures == 1
        assert report.contracts_persisted == 0

    def test_cell_with_zero_expiries_no_contracts(self) -> None:
        cells = [("NSE", "NOFNO", 2020)]
        report = self._run(cells, {("NSE", "NOFNO", 2020): []})
        assert report.expiry_cells == 1
        assert report.expiry_failures == 0
        assert report.contract_jobs == 0

    def test_phase1_market_hours_halt_reraised_not_swallowed(self) -> None:
        """DEC-GROWW-MARKET-HOURS-GUARD-001: a Phase-1 fetcher's MarketHoursHalt is the
        ONE captured-per-cell exception that HALTS the whole phase (re-raised), instead
        of being recorded-and-resumed like an ordinary fetch failure."""
        from brutex.data.errors import MarketHoursHalt

        cells = [("NSE", "A", 2024)]

        def fetch_expiries(cell: ExpiryCell) -> Sequence[date]:
            raise MarketHoursHalt()

        def fetch_contracts(job: ContractJob) -> int:  # pragma: no cover — never reached
            return 1

        with pytest.raises(MarketHoursHalt):
            run_two_phase(
                cells,
                fetch_expiries=fetch_expiries,
                fetch_contracts=fetch_contracts,
                run_parallel=_sync_run_parallel,
                on_expiry_cell=lambda *_: None,
                on_contract=lambda *_: None,
            )

    def test_phase2_market_hours_halt_reraised_not_swallowed(self) -> None:
        """DEC-GROWW-MARKET-HOURS-GUARD-001: a Phase-2 (contract) fetcher's
        MarketHoursHalt likewise halts the whole phase (re-raised)."""
        from brutex.data.errors import MarketHoursHalt

        cells = [("NSE", "A", 2024)]
        expiries = {("NSE", "A", 2024): [_D(2024, 1, 25)]}

        def fetch_expiries(cell: ExpiryCell) -> Sequence[date]:
            return expiries[cell]

        def fetch_contracts(job: ContractJob) -> int:
            raise MarketHoursHalt()

        with pytest.raises(MarketHoursHalt):
            run_two_phase(
                cells,
                fetch_expiries=fetch_expiries,
                fetch_contracts=fetch_contracts,
                run_parallel=_sync_run_parallel,
                on_expiry_cell=lambda *_: None,
                on_contract=lambda *_: None,
            )

    def test_empty_universe(self) -> None:
        report = self._run([], {})
        assert report == TwoPhaseReport(0, 0, 0, 0, 0, 0)

    def test_callbacks_fire_per_item(self) -> None:
        cells = [("NSE", "A", 2024)]
        expiries = {("NSE", "A", 2024): [_D(2024, 1, 25)]}
        seen_expiry: list[tuple[ExpiryCell, int, bool]] = []
        seen_contract: list[tuple[ContractJob, int, bool]] = []

        def fetch_expiries(cell: ExpiryCell) -> Sequence[date]:
            return expiries[cell]

        def fetch_contracts(job: ContractJob) -> int:
            return 7

        run_two_phase(
            cells,
            fetch_expiries=fetch_expiries,
            fetch_contracts=fetch_contracts,
            run_parallel=_sync_run_parallel,
            on_expiry_cell=lambda c, e, x: seen_expiry.append((c, len(e), x is None)),
            on_contract=lambda j, n, x: seen_contract.append((j, n, x is None)),
        )
        assert seen_expiry == [(("NSE", "A", 2024), 1, True)]
        assert seen_contract == [(("NSE", "A", _D(2024, 1, 25)), 7, True)]

    def test_failed_cell_callback_gets_empty_and_exc(self) -> None:
        cells = [("NSE", "A", 2024)]
        captured: list[tuple[int, bool]] = []
        run_two_phase(
            cells,
            fetch_expiries=lambda _c: (_ for _ in ()).throw(RuntimeError("boom")),
            fetch_contracts=lambda _j: 0,
            run_parallel=_sync_run_parallel,
            on_expiry_cell=lambda _c, e, x: captured.append((len(e), x is not None)),
            on_contract=lambda *_: None,
        )
        assert captured == [(0, True)]  # empty expiries + exc present


# ────────────────────────────────────────────────────────────────────────
# cells_to_pull — Phase-1 incremental skip (immutable past years)
# ────────────────────────────────────────────────────────────────────────


class TestCellsToPull:
    def test_skips_settled_past_year(self) -> None:
        cells = [("NSE", "NIFTY", 2023), ("NSE", "NIFTY", 2025)]
        settled = frozenset({("NSE", "NIFTY", 2023)})
        # 2023 settled + past → skipped; 2025 (current) always pulled.
        assert cells_to_pull(cells, settled=settled, current_year=2025) == [("NSE", "NIFTY", 2025)]

    def test_current_year_always_pulled_even_if_settled(self) -> None:
        cells = [("NSE", "NIFTY", 2025)]
        settled = frozenset({("NSE", "NIFTY", 2025)})
        # New expiries appear as the year runs → never skip the current year.
        assert cells_to_pull(cells, settled=settled, current_year=2025) == cells

    def test_unsettled_past_year_pulled(self) -> None:
        cells = [("NSE", "NIFTY", 2022)]
        # Not in settled (e.g. missing, or a failed cell the caller excluded).
        assert cells_to_pull(cells, settled=frozenset(), current_year=2025) == cells

    def test_order_preserved(self) -> None:
        cells = [("NSE", "Z", 2025), ("NSE", "A", 2025)]
        assert cells_to_pull(cells, settled=frozenset(), current_year=2025) == cells

    def test_empty(self) -> None:
        assert cells_to_pull([], settled=frozenset(), current_year=2025) == []


# ────────────────────────────────────────────────────────────────────────
# contract_jobs_to_pull — Phase-2 incremental skip (immutable expired expiries)
# ────────────────────────────────────────────────────────────────────────


class TestContractJobsToPull:
    def test_skips_settled_expired(self) -> None:
        past = ("NSE", "NIFTY", _D(2024, 1, 25))
        future = ("NSE", "NIFTY", _D(2026, 12, 31))
        settled = frozenset({past, future})
        # past expiry settled → skipped; future expiry always re-pulled.
        assert contract_jobs_to_pull([past, future], settled=settled, today=_D(2026, 5, 30)) == [
            future
        ]

    def test_unsettled_expired_pulled_once(self) -> None:
        past = ("NSE", "NIFTY", _D(2024, 1, 25))
        # Expired but never captured → must be pulled.
        assert contract_jobs_to_pull([past], settled=frozenset(), today=_D(2026, 5, 30)) == [past]

    def test_future_expiry_always_pulled_even_if_settled(self) -> None:
        fut = ("NSE", "NIFTY", _D(2026, 6, 26))
        settled = frozenset({fut})
        # New strikes get listed as the underlying moves → never skip live edge.
        assert contract_jobs_to_pull([fut], settled=settled, today=_D(2026, 5, 30)) == [fut]

    def test_today_exactly_on_expiry_is_live(self) -> None:
        # expiry == today is NOT < today → still live, pulled even if settled.
        same = ("NSE", "NIFTY", _D(2026, 5, 30))
        settled = frozenset({same})
        assert contract_jobs_to_pull([same], settled=settled, today=_D(2026, 5, 30)) == [same]

    def test_empty(self) -> None:
        assert contract_jobs_to_pull([], settled=frozenset(), today=_D(2026, 5, 30)) == []


class TestRunTwoPhaseContractSkip:
    """The Phase-2 skip wired through run_two_phase (contract_skip + today)."""

    def test_contract_skip_drops_expired_settled_job(self) -> None:
        cells = [("NSE", "A", 2024)]
        expiries = {("NSE", "A", 2024): [_D(2024, 1, 25)]}
        pulled: list[ContractJob] = []

        run_two_phase(
            cells,
            fetch_expiries=lambda c: expiries[c],
            fetch_contracts=lambda j: (pulled.append(j), 9)[1],
            run_parallel=_sync_run_parallel,
            on_expiry_cell=lambda *_: None,
            on_contract=lambda *_: None,
            contract_skip=frozenset({("NSE", "A", _D(2024, 1, 25))}),
            today=_D(2026, 5, 30),
        )
        # The single discovered expiry is expired + settled → never fetched.
        assert pulled == []

    def test_no_skip_when_contract_skip_empty(self) -> None:
        cells = [("NSE", "A", 2024)]
        expiries = {("NSE", "A", 2024): [_D(2024, 1, 25)]}
        report = run_two_phase(
            cells,
            fetch_expiries=lambda c: expiries[c],
            fetch_contracts=lambda _j: 4,
            run_parallel=_sync_run_parallel,
            on_expiry_cell=lambda *_: None,
            on_contract=lambda *_: None,
        )
        assert report.contract_jobs == 1
        assert report.contracts_persisted == 4


# ────────────────────────────────────────────────────────────────────────
# ItemResult shape
# ────────────────────────────────────────────────────────────────────────


class TestItemResult:
    def test_ok_result(self) -> None:
        r: ItemResult[str, int] = ItemResult(item="x", ok=True, result=5, exc=None)
        assert r.ok and r.result == 5 and r.exc is None

    def test_err_result(self) -> None:
        exc = RuntimeError("x")
        r: ItemResult[str, int] = ItemResult(item="x", ok=False, result=None, exc=exc)
        assert not r.ok and r.exc is exc


# ────────────────────────────────────────────────────────────────────────
# settled_expiry_cells — build the Phase-1 skip-set from coverage rows
# ────────────────────────────────────────────────────────────────────────


class TestSettledExpiryCells:
    def test_complete_and_pending_are_settled(self) -> None:
        rows = [
            ("NSE", "A", 2023, "complete"),
            ("NSE", "B", 2023, "pending"),
        ]
        assert settled_expiry_cells(rows) == frozenset({("NSE", "A", 2023), ("NSE", "B", 2023)})

    def test_failed_is_excluded(self) -> None:
        # A failed cell must NOT be settled → it always retries.
        rows = [("NSE", "C", 2023, "failed")]
        assert settled_expiry_cells(rows) == frozenset()

    def test_unknown_status_excluded(self) -> None:
        assert settled_expiry_cells([("NSE", "A", 2023, "missing")]) == frozenset()

    def test_empty(self) -> None:
        assert settled_expiry_cells([]) == frozenset()

    def test_result_is_frozenset(self) -> None:
        # O(1) membership downstream requires a frozenset (hashable, immutable).
        out = settled_expiry_cells([("NSE", "A", 2023, "complete")])
        assert isinstance(out, frozenset)

    def test_feeds_cells_to_pull(self) -> None:
        # End-to-end: the builder's output drives the Phase-1 skip.
        settled = settled_expiry_cells([("NSE", "A", 2023, "complete")])
        cells = [("NSE", "A", 2023), ("NSE", "A", 2025)]
        assert cells_to_pull(cells, settled=settled, current_year=2025) == [("NSE", "A", 2025)]


# ────────────────────────────────────────────────────────────────────────
# settled_contract_jobs — build the Phase-2 skip-set from contract rows
# ────────────────────────────────────────────────────────────────────────


class TestSettledContractJobs:
    def test_builds_triple_set(self) -> None:
        rows = [
            ("NSE", "A", _D(2024, 1, 25)),
            ("NSE", "A", _D(2024, 2, 29)),
        ]
        assert settled_contract_jobs(rows) == frozenset(
            {("NSE", "A", _D(2024, 1, 25)), ("NSE", "A", _D(2024, 2, 29))}
        )

    def test_dedupes_repeated_triples(self) -> None:
        # Many contract legs share one (exchange, underlying, expiry) → one entry.
        same = ("NSE", "A", _D(2024, 1, 25))
        assert settled_contract_jobs([same, same, same]) == frozenset({same})

    def test_empty(self) -> None:
        assert settled_contract_jobs([]) == frozenset()

    def test_result_is_frozenset(self) -> None:
        out = settled_contract_jobs([("NSE", "A", _D(2024, 1, 25))])
        assert isinstance(out, frozenset)

    def test_feeds_contract_jobs_to_pull(self) -> None:
        # End-to-end: expired + settled → skipped; future + settled → re-pulled.
        past = ("NSE", "A", _D(2024, 1, 25))
        future = ("NSE", "A", _D(2026, 12, 31))
        settled = settled_contract_jobs([past, future])
        assert contract_jobs_to_pull([past, future], settled=settled, today=_D(2026, 5, 30)) == [
            future
        ]


# ────────────────────────────────────────────────────────────────────────
# persisted-expiry union (DEC-PHASE2-REGISTRY-REBUILD-001)
# ────────────────────────────────────────────────────────────────────────


class TestContractJobsFromPersisted:
    def test_persisted_only_yields_jobs(self) -> None:
        # A prior run's registry-persisted expiry rebuilds a job with NO in-memory cell.
        persisted = [("NSE", "NIFTY", _D(2024, 6, 27))]
        assert contract_jobs_from({}, persisted=persisted) == persisted

    def test_persisted_deduped_against_discovered(self) -> None:
        # The same expiry in BOTH sources yields ONE job (never fetched twice).
        shared = _D(2026, 6, 25)
        by_cell: dict[ExpiryCell, Sequence[date]] = {("NSE", "NIFTY", 2026): [shared]}
        jobs = contract_jobs_from(by_cell, persisted=[("NSE", "NIFTY", shared)])
        assert jobs == [("NSE", "NIFTY", shared)]

    def test_union_is_sorted_across_sources(self) -> None:
        by_cell: dict[ExpiryCell, Sequence[date]] = {("NSE", "ZEEL", 2024): [_D(2024, 1, 25)]}
        jobs = contract_jobs_from(by_cell, persisted=[("BSE", "AAA", _D(2024, 1, 25))])
        assert jobs == [
            ("BSE", "AAA", _D(2024, 1, 25)),
            ("NSE", "ZEEL", _D(2024, 1, 25)),
        ]

    def test_default_empty_persisted_unchanged(self) -> None:
        # The default is byte-identical to the prior in-memory-only derivation.
        by_cell: dict[ExpiryCell, Sequence[date]] = {("NSE", "NIFTY", 2024): [_D(2024, 1, 25)]}
        assert contract_jobs_from(by_cell) == contract_jobs_from(by_cell, persisted=())


class TestRunTwoPhasePersistedJobs:
    """The registry-rebuilt Phase-2 worklist wired through run_two_phase."""

    def test_persisted_job_runs_in_phase2_without_a_cell(self) -> None:
        """THE re-pull path: a persisted expiry with NO Phase-1 cell is still fetched.

        Run-1's cell settled ``expiries_only`` and its contract job FAILED; on the
        re-run the cell is skipped (zero cells here), so before the fix no contract
        job was ever rebuilt — the failed contracts had no re-pull path at all.
        """
        pulled: list[ContractJob] = []
        job = ("NSE", "NIFTY", _D(2024, 6, 27))
        report = run_two_phase(
            [],  # the settled cell is skipped — Phase 1 has nothing to do
            fetch_expiries=lambda _c: [],
            fetch_contracts=lambda j: (pulled.append(j), 2)[1],
            run_parallel=_sync_run_parallel,
            on_expiry_cell=lambda *_: None,
            on_contract=lambda *_: None,
            persisted_jobs=[job],
            today=_D(2026, 6, 14),
        )
        assert pulled == [job]
        assert report.contract_jobs == 1
        assert report.contracts_persisted == 2

    def test_persisted_job_still_skipped_when_settled_and_expired(self) -> None:
        # Contracts already captured + expiry expired → the union adds no extra call.
        pulled: list[ContractJob] = []
        job = ("NSE", "NIFTY", _D(2024, 6, 27))
        report = run_two_phase(
            [],
            fetch_expiries=lambda _c: [],
            fetch_contracts=lambda j: (pulled.append(j), 2)[1],
            run_parallel=_sync_run_parallel,
            on_expiry_cell=lambda *_: None,
            on_contract=lambda *_: None,
            contract_skip=frozenset({job}),
            persisted_jobs=[job],
            today=_D(2026, 6, 14),
        )
        assert pulled == []
        assert report.contract_jobs == 0

    def test_persisted_job_deduped_against_discovered(self) -> None:
        # The same expiry discovered THIS run and persisted earlier → ONE fetch.
        shared = _D(2026, 6, 25)
        pulled: list[ContractJob] = []
        run_two_phase(
            [("NSE", "NIFTY", 2026)],
            fetch_expiries=lambda _c: [shared],
            fetch_contracts=lambda j: (pulled.append(j), 1)[1],
            run_parallel=_sync_run_parallel,
            on_expiry_cell=lambda *_: None,
            on_contract=lambda *_: None,
            persisted_jobs=[("NSE", "NIFTY", shared)],
            today=_D(2026, 6, 14),
        )
        assert pulled == [("NSE", "NIFTY", shared)]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
