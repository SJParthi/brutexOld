"""Unit tests for :mod:`brutex.dashboard.coverage_view`.

Pure JSON-projection helpers for the ``/coverage`` page
(DEC-COVERAGE-LEDGER-001). Pins every line + branch of the row builder
(including the missing-cell path), the headline dict, and the payload
composer — no FastAPI, no DuckDB, no network.
"""

from __future__ import annotations

from datetime import UTC, datetime

from brutex.dashboard.coverage_view import (
    STATUS_MISSING,
    build_coverage_payload,
    build_coverage_rows,
    coverage_headline,
)
from brutex.data.coverage_ledger import (
    CoverageRecord,
    classify_cell,
    reconcile_coverage,
)

_FETCHED_AT = datetime(2026, 5, 30, 9, 15, tzinfo=UTC)


def _record(
    *,
    exchange: str = "NSE",
    underlying: str = "NIFTY",
    year: int = 2024,
    expiries: int = 2,
    contracts: int = 20,
    error: str | None = None,
) -> CoverageRecord:
    return CoverageRecord(
        exchange=exchange,
        underlying_symbol=underlying,
        year=year,
        expiries_pulled=expiries,
        contracts_pulled=contracts,
        status=classify_cell(expiries, contracts, error),
        last_run_id="run-1",
        last_fetched_at=_FETCHED_AT,
        error_message=error,
    )


class TestBuildCoverageRows:
    def test_persisted_cell_row_shape(self) -> None:
        rows = build_coverage_rows([_record()], [("NSE", "NIFTY")], [2024])
        assert rows == [
            {
                "underlying": "NIFTY",
                "exchange": "NSE",
                "year": 2024,
                "status": "complete",
                "expiries_pulled": 2,
                "contracts_pulled": 20,
                "last_fetched_at": _FETCHED_AT.isoformat(),
                "error": None,
            }
        ]

    def test_failed_cell_carries_error(self) -> None:
        rec = _record(expiries=0, contracts=0, error="network down")
        rows = build_coverage_rows([rec], [("NSE", "NIFTY")], [2024])
        assert rows[0]["status"] == "failed"
        assert rows[0]["error"] == "network down"

    def test_missing_cell_rendered(self) -> None:
        # Expected grid has a cell with no persisted record.
        rows = build_coverage_rows([], [("NSE", "NIFTY")], [2024])
        assert rows == [
            {
                "underlying": "NIFTY",
                "exchange": "NSE",
                "year": 2024,
                "status": STATUS_MISSING,
                "expiries_pulled": 0,
                "contracts_pulled": 0,
                "last_fetched_at": None,
                "error": None,
            }
        ]

    def test_mix_of_present_and_missing_sorted(self) -> None:
        rows = build_coverage_rows(
            [_record(underlying="NIFTY", year=2024)],
            [("NSE", "NIFTY"), ("BSE", "SENSEX")],
            [2024],
        )
        keys = [(r["exchange"], r["underlying"], r["year"]) for r in rows]
        # Sorted: BSE first, then NSE.
        assert keys == [("BSE", "SENSEX", 2024), ("NSE", "NIFTY", 2024)]
        assert rows[0]["status"] == STATUS_MISSING
        assert rows[1]["status"] == "complete"

    def test_extra_record_outside_grid_is_dropped(self) -> None:
        rows = build_coverage_rows(
            [_record(underlying="NIFTY", year=2024), _record(underlying="RELIANCE")],
            [("NSE", "NIFTY")],
            [2024],
        )
        assert len(rows) == 1
        assert rows[0]["underlying"] == "NIFTY"

    def test_empty_grid_returns_empty(self) -> None:
        assert build_coverage_rows([_record()], [], [2024]) == []


class TestCoverageHeadline:
    def test_partial_headline(self) -> None:
        summary = reconcile_coverage(
            [("NSE", "A"), ("NSE", "B"), ("NSE", "C"), ("NSE", "D")],
            [2024],
            [_record(underlying="A"), _record(underlying="B")],
        )
        headline = coverage_headline(summary)
        assert headline == {
            "complete": 2,
            "total": 4,
            "incomplete": 2,
            "failed": 0,
            "pending": 0,
            "missing": 2,
            "percent": 50,
            "is_complete": False,
        }

    def test_all_complete_headline(self) -> None:
        summary = reconcile_coverage([("NSE", "A")], [2024], [_record(underlying="A")])
        headline = coverage_headline(summary)
        assert headline["percent"] == 100
        assert headline["is_complete"] is True

    def test_empty_grid_headline_is_100_percent(self) -> None:
        summary = reconcile_coverage([], [2024], [])
        headline = coverage_headline(summary)
        assert headline["percent"] == 100
        assert headline["total"] == 0
        assert headline["is_complete"] is True

    def test_failed_and_pending_counts(self) -> None:
        summary = reconcile_coverage(
            [("NSE", "F"), ("NSE", "P")],
            [2024],
            [
                _record(underlying="F", expiries=0, contracts=0, error="boom"),
                _record(underlying="P", expiries=0, contracts=0),
            ],
        )
        headline = coverage_headline(summary)
        assert headline["failed"] == 1
        assert headline["pending"] == 1
        assert headline["percent"] == 0


class TestBuildCoveragePayload:
    def test_payload_has_headline_and_rows(self) -> None:
        payload = build_coverage_payload(
            [_record(underlying="NIFTY", year=2024)],
            [("NSE", "NIFTY"), ("BSE", "SENSEX")],
            [2024],
        )
        assert set(payload) == {"headline", "rows"}
        assert payload["headline"]["total"] == 2
        assert payload["headline"]["complete"] == 1
        assert len(payload["rows"]) == 2

    def test_payload_accepts_iterator_once(self) -> None:
        # An iterator (single-use) must still produce both headline + rows.
        records = iter([_record(underlying="NIFTY", year=2024)])
        payload = build_coverage_payload(records, [("NSE", "NIFTY")], [2024])
        assert payload["headline"]["complete"] == 1
        assert payload["rows"][0]["status"] == "complete"
