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
    contract_jobs_from,
    phase1_cells,
    run_two_phase,
)

# ────────────────────────────────────────────────────────────────────────
# A synchronous run_parallel fake — preserves submit order, no threads.
# ────────────────────────────────────────────────────────────────────────


def _sync_run_parallel(
    fn: Callable[[object], object],
    items: Sequence[object],
) -> list[ItemResult[object, object]]:
    """Map *fn* over *items* in order, capturing each result/exception."""
    out: list[ItemResult[object, object]] = []
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
            run_parallel_contracts=_sync_run_parallel,
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
            run_parallel_contracts=_sync_run_parallel,
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
            run_parallel_contracts=_sync_run_parallel,
            on_expiry_cell=lambda _c, e, x: captured.append((len(e), x is not None)),
            on_contract=lambda *_: None,
        )
        assert captured == [(0, True)]  # empty expiries + exc present


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


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
