"""Unit tests for :mod:`brutex.data.coverage_ledger`.

Pure reconciliation core for the completeness ledger
(DEC-COVERAGE-LEDGER-001). These tests pin every line + branch of the
classification rule, the reconciliation diff, the summary properties, and
the resume worklist accessor — no DuckDB, no SDK, no network.
"""

from __future__ import annotations

from datetime import UTC, datetime

from brutex.data.coverage_ledger import (
    COVERAGE_STATUSES,
    STATUS_COMPLETE,
    STATUS_EXPIRIES_ONLY,
    STATUS_FAILED,
    STATUS_PENDING,
    CoverageRecord,
    CoverageSummary,
    cells_to_repull,
    classify_cell,
    reconcile_coverage,
)

_FETCHED_AT = datetime(2026, 5, 30, 9, 15, tzinfo=UTC)


def _record(
    *,
    exchange: str = "NSE",
    underlying: str = "NIFTY",
    year: int = 2024,
    expiries: int = 1,
    contracts: int = 10,
    status: str | None = None,
    error: str | None = None,
) -> CoverageRecord:
    resolved_status = status if status is not None else classify_cell(expiries, contracts, error)
    return CoverageRecord(
        exchange=exchange,
        underlying_symbol=underlying,
        year=year,
        expiries_pulled=expiries,
        contracts_pulled=contracts,
        status=resolved_status,
        last_run_id="run-1",
        last_fetched_at=_FETCHED_AT,
        error_message=error,
    )


class TestConstants:
    def test_statuses_tuple_ordering(self) -> None:
        assert COVERAGE_STATUSES == (
            STATUS_COMPLETE,
            STATUS_EXPIRIES_ONLY,
            STATUS_FAILED,
            STATUS_PENDING,
        )

    def test_status_string_values(self) -> None:
        assert STATUS_COMPLETE == "complete"
        assert STATUS_FAILED == "failed"
        assert STATUS_PENDING == "pending"


class TestClassifyCell:
    def test_error_dominates_even_with_expiries(self) -> None:
        assert classify_cell(5, 100, "boom") == STATUS_FAILED

    def test_error_dominates_with_zero_expiries(self) -> None:
        assert classify_cell(0, 0, "network down") == STATUS_FAILED

    def test_complete_when_expiries_pulled_and_no_error(self) -> None:
        assert classify_cell(3, 42, None) == STATUS_COMPLETE

    def test_complete_with_zero_contracts(self) -> None:
        # A derivative-free index: expiries ran, zero contracts → complete.
        assert classify_cell(1, 0, None) == STATUS_COMPLETE

    def test_pending_when_nothing_pulled_and_no_error(self) -> None:
        assert classify_cell(0, 0, None) == STATUS_PENDING

    def test_empty_string_error_is_not_failed(self) -> None:
        # Empty string is falsy → not a failure; expiries decide.
        assert classify_cell(2, 5, "") == STATUS_COMPLETE
        assert classify_cell(0, 0, "") == STATUS_PENDING


class TestCoverageSummaryProperties:
    def test_incomplete_counts_repull_cells(self) -> None:
        summary = CoverageSummary(
            total_expected=4,
            complete=1,
            failed=1,
            pending=1,
            missing=1,
            to_repull=(("NSE", "A", 2020), ("NSE", "B", 2021), ("NSE", "C", 2022)),
        )
        assert summary.incomplete == 3

    def test_is_complete_true_when_no_repull(self) -> None:
        summary = CoverageSummary(
            total_expected=2,
            complete=2,
            failed=0,
            pending=0,
            missing=0,
            to_repull=(),
        )
        assert summary.is_complete is True

    def test_is_complete_false_when_repull_present(self) -> None:
        summary = CoverageSummary(
            total_expected=1,
            complete=0,
            failed=0,
            pending=1,
            missing=0,
            to_repull=(("NSE", "A", 2020),),
        )
        assert summary.is_complete is False

    def test_complete_fraction(self) -> None:
        summary = CoverageSummary(
            total_expected=4,
            complete=3,
            failed=0,
            pending=1,
            missing=0,
            to_repull=(("NSE", "A", 2020),),
        )
        assert summary.complete_fraction == 0.75

    def test_complete_fraction_empty_grid_is_one(self) -> None:
        summary = CoverageSummary(
            total_expected=0,
            complete=0,
            failed=0,
            pending=0,
            missing=0,
            to_repull=(),
        )
        assert summary.complete_fraction == 1.0
        assert summary.is_complete is True


class TestReconcileCoverage:
    def test_all_complete(self) -> None:
        expected = [("NSE", "NIFTY"), ("BSE", "SENSEX")]
        years = [2023, 2024]
        actual = [
            _record(exchange="NSE", underlying="NIFTY", year=2023),
            _record(exchange="NSE", underlying="NIFTY", year=2024),
            _record(exchange="BSE", underlying="SENSEX", year=2023),
            _record(exchange="BSE", underlying="SENSEX", year=2024),
        ]
        summary = reconcile_coverage(expected, years, actual)
        assert summary.total_expected == 4
        assert summary.complete == 4
        assert summary.failed == 0
        assert summary.pending == 0
        assert summary.missing == 0
        assert summary.to_repull == ()
        assert summary.is_complete is True

    def test_missing_cell_is_repulled(self) -> None:
        expected = [("NSE", "NIFTY")]
        years = [2023, 2024]
        # Only 2023 persisted → 2024 is missing.
        actual = [_record(year=2023)]
        summary = reconcile_coverage(expected, years, actual)
        assert summary.total_expected == 2
        assert summary.complete == 1
        assert summary.missing == 1
        assert summary.to_repull == (("NSE", "NIFTY", 2024),)

    def test_failed_and_pending_are_repulled(self) -> None:
        expected = [("NSE", "A"), ("NSE", "B"), ("NSE", "C")]
        years = [2024]
        actual = [
            _record(underlying="A", year=2024),  # complete
            _record(underlying="B", year=2024, expiries=0, contracts=0, error="boom"),  # failed
            _record(underlying="C", year=2024, expiries=0, contracts=0),  # pending
        ]
        summary = reconcile_coverage(expected, years, actual)
        assert summary.complete == 1
        assert summary.failed == 1
        assert summary.pending == 1
        assert summary.missing == 0
        assert summary.to_repull == (("NSE", "B", 2024), ("NSE", "C", 2024))

    def test_expiries_only_counted_separately_and_not_repulled(self) -> None:
        # DEC-UNIVERSE-PULL-PHASED-001: an expiries_only cell (Phase 1 done,
        # contracts pending) is NOT complete, NOT re-pulled by a Phase-1 resume
        # (its expiry calendar is captured), and counted in its own bucket.
        expected = [("NSE", "A"), ("NSE", "B")]
        years = [2024]
        actual = [
            _record(underlying="A", year=2024),  # complete
            _record(underlying="B", year=2024, status="expiries_only"),
        ]
        summary = reconcile_coverage(expected, years, actual)
        assert summary.complete == 1
        assert summary.expiries_only == 1
        assert summary.pending == 0
        assert summary.failed == 0
        assert summary.to_repull == ()  # expiries_only is NOT re-pulled
        assert summary.is_complete is False  # but the universe isn't "complete"

    def test_extra_actual_rows_outside_expected_are_ignored(self) -> None:
        expected = [("NSE", "NIFTY")]
        years = [2024]
        actual = [
            _record(underlying="NIFTY", year=2024),
            # Out-of-scope rows: different underlying / year — ignored.
            _record(underlying="RELIANCE", year=2024),
            _record(underlying="NIFTY", year=2099),
        ]
        summary = reconcile_coverage(expected, years, actual)
        assert summary.total_expected == 1
        assert summary.complete == 1
        assert summary.to_repull == ()

    def test_empty_actual_all_missing(self) -> None:
        expected = [("NSE", "NIFTY"), ("BSE", "SENSEX")]
        years = [2024]
        summary = reconcile_coverage(expected, years, [])
        assert summary.total_expected == 2
        assert summary.missing == 2
        assert summary.to_repull == (("BSE", "SENSEX", 2024), ("NSE", "NIFTY", 2024))

    def test_repull_is_sorted_deterministically(self) -> None:
        expected = [("NSE", "ZED"), ("NSE", "ABLE"), ("BSE", "SENSEX")]
        years = [2024]
        summary = reconcile_coverage(expected, years, [])
        # Sorted by (exchange, underlying, year): BSE before NSE; ABLE before ZED.
        assert summary.to_repull == (
            ("BSE", "SENSEX", 2024),
            ("NSE", "ABLE", 2024),
            ("NSE", "ZED", 2024),
        )

    def test_empty_expected_grid(self) -> None:
        summary = reconcile_coverage([], [2024], [_record()])
        assert summary.total_expected == 0
        assert summary.to_repull == ()
        assert summary.is_complete is True


class TestCellsToRepull:
    def test_returns_summary_worklist(self) -> None:
        expected = [("NSE", "NIFTY")]
        years = [2024]
        summary = reconcile_coverage(expected, years, [])
        assert cells_to_repull(summary) == (("NSE", "NIFTY", 2024),)

    def test_empty_when_complete(self) -> None:
        summary = reconcile_coverage([("NSE", "NIFTY")], [2024], [_record(year=2024)])
        assert cells_to_repull(summary) == ()
