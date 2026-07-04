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
    build_option_coverage_payload,
    build_spot_coverage_payload,
    coverage_headline,
    option_coverage_headline,
    spot_coverage_headline,
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
    status: str | None = None,
) -> CoverageRecord:
    return CoverageRecord(
        exch_id=exchange,
        underlying_symbol=underlying,
        year=year,
        expiries_pulled=expiries,
        contracts_pulled=contracts,
        status=status if status is not None else classify_cell(expiries, contracts, error),
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
            "attempted": 2,
            "not_pulled": 2,
            "incomplete": 2,
            "failed": 0,
            "pending": 0,
            "expiries_only": 0,
            "missing": 2,
            "percent": 50,
            "is_complete": False,
        }

    def test_headline_expiries_only_not_counted_complete(self) -> None:
        # A Phase-1-only cell (expiries_only) must NOT inflate "complete" and
        # must NOT read as 100% (DEC-UNIVERSE-PULL-PHASED-001).
        summary = reconcile_coverage(
            [("NSE", "A"), ("NSE", "B")],
            [2024],
            [
                _record(underlying="A"),  # complete
                _record(underlying="B", expiries=3, contracts=0, status="expiries_only"),
            ],
        )
        headline = coverage_headline(summary)
        assert headline["complete"] == 1
        assert headline["expiries_only"] == 1
        assert headline["attempted"] == 2  # complete + expiries_only
        assert headline["is_complete"] is False  # expiries_only ≠ done
        assert headline["percent"] == 50

    def test_headline_attempted_and_not_pulled_are_honest(self) -> None:
        # attempted = complete + failed + pending (cells the pull has touched);
        # not_pulled = missing (queued / in-progress) — NOT an error. The only
        # real-error count is `failed`. attempted + not_pulled == total.
        summary = reconcile_coverage(
            [("NSE", "C"), ("NSE", "F"), ("NSE", "P"), ("NSE", "M1"), ("NSE", "M2")],
            [2024],
            [
                _record(underlying="C"),
                _record(underlying="F", expiries=0, contracts=0, error="boom"),
                _record(underlying="P", expiries=0, contracts=0),
            ],
        )
        headline = coverage_headline(summary)
        assert headline["attempted"] == 3  # C(complete) + F(failed) + P(pending)
        assert headline["not_pulled"] == 2  # M1, M2 never reached (== missing)
        assert headline["failed"] == 1  # the ONLY real-error count
        assert headline["attempted"] + headline["not_pulled"] == headline["total"]

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


# ── Spot coverage (DEC-COVERAGE-SPOT-001) ────────────────────────────


def _spot(symbol: str = "NSE-NIFTY", *, bars: int = 2) -> dict[str, object]:
    """One per-dataset stats dict shaped like ``summarize_spot_datasets``."""
    return {
        "exchange": "NSE",
        "segment": "CASH",
        "symbol": symbol,
        "interval": "1minute",
        "bars": bars,
        "first_date": "2024-03-01",
        "last_date": "2024-03-02",
    }


class TestSpotCoverageHeadline:
    def test_empty_is_zero(self) -> None:
        # No datasets → vacuously zero, never raises.
        assert spot_coverage_headline([]) == {"datasets": 0, "total_bars": 0}

    def test_sums_bars_across_datasets(self) -> None:
        head = spot_coverage_headline([_spot("NSE-NIFTY", bars=2), _spot("NSE-BANKNIFTY", bars=5)])
        assert head == {"datasets": 2, "total_bars": 7}


class TestBuildSpotCoveragePayload:
    def test_payload_has_headline_and_rows(self) -> None:
        payload = build_spot_coverage_payload([_spot("NSE-NIFTY", bars=2)])
        assert set(payload) == {"headline", "rows"}
        assert payload["headline"] == {"datasets": 1, "total_bars": 2}
        assert payload["rows"][0]["symbol"] == "NSE-NIFTY"

    def test_payload_empty(self) -> None:
        payload = build_spot_coverage_payload([])
        assert payload == {"headline": {"datasets": 0, "total_bars": 0}, "rows": []}

    def test_payload_accepts_iterator_once(self) -> None:
        # An iterator (single-use) must still produce both headline + rows.
        payload = build_spot_coverage_payload(iter([_spot("NSE-NIFTY", bars=3)]))
        assert payload["headline"]["total_bars"] == 3
        assert payload["rows"][0]["symbol"] == "NSE-NIFTY"

    def test_payload_passes_open_substitution_fields_through(self) -> None:
        # DEC-DAILY-OPEN-SUBSTITUTION-HONESTY-001: the ``{**row}`` annotation spread
        # carries the summarize-side substitution counts through to the /coverage/spot
        # payload (and thence the shared completeness inputs) unchanged.
        row = _spot("NSE-NIFTY")
        row["open_substituted_days"] = 4
        row["session_days"] = 9
        annotated = build_spot_coverage_payload([row])["rows"][0]
        assert annotated["open_substituted_days"] == 4
        assert annotated["session_days"] == 9


class TestSpotCompleteness:
    """DEC-SPOT-COVERAGE-COMPLETENESS-001 — the honest-state chip ladder.

    The green state is "settled" (the fetch ledger has no outstanding work for
    planner-planned ranges), deliberately NOT "complete" — a trading-day gap
    audit is a separate follow-up and is never implied.
    """

    def _row(self, payload: dict[str, object]) -> dict[str, object]:
        rows = payload["rows"]
        assert isinstance(rows, list)
        return rows[0]  # type: ignore[no-any-return]

    def test_rollup_omitted_is_no_ledger_record(self) -> None:
        row = self._row(build_spot_coverage_payload([_spot("NIFTY")]))
        assert row["completeness"] == "no_ledger_record"
        # None, never 0 — 0 would assert "ledger consulted, found nothing".
        assert row["windows_settled"] is None
        assert row["windows_pending"] is None
        assert row["windows_failed"] is None

    def test_settled_when_no_outstanding_windows(self) -> None:
        row = self._row(
            build_spot_coverage_payload([_spot("NIFTY")], {("NIFTY", "1minute"): (21, 0, 0)})
        )
        assert row["completeness"] == "settled"
        assert row["windows_settled"] == 21
        assert row["windows_pending"] == 0
        assert row["windows_failed"] == 0

    def test_pending_when_outstanding_windows(self) -> None:
        row = self._row(
            build_spot_coverage_payload([_spot("NIFTY")], {("NIFTY", "1minute"): (20, 3, 0)})
        )
        assert row["completeness"] == "pending"
        assert row["windows_pending"] == 3

    def test_failed_outranks_pending(self) -> None:
        # FAILED is the actionable state — it wins even with pending present.
        row = self._row(
            build_spot_coverage_payload([_spot("NIFTY")], {("NIFTY", "1minute"): (20, 3, 2)})
        )
        assert row["completeness"] == "failed"
        assert row["windows_failed"] == 2

    def test_resampled_interval_is_derived(self) -> None:
        # A 5minute partition is RESAMPLED from 1minute — the fetch ledger does
        # not apply, so it is "derived" (never "no_ledger_record" as if suspect,
        # never inheriting the 1minute state — materialisation is manual and can
        # be stale relative to a later 1minute pull).
        spot = dict(_spot("NIFTY"), interval="5minute")
        row = self._row(build_spot_coverage_payload([spot], {("NIFTY", "1minute"): (21, 0, 0)}))
        assert row["completeness"] == "derived"
        assert row["windows_settled"] is None

    def test_fetch_interval_without_ledger_rows_is_no_record(self) -> None:
        row = self._row(
            build_spot_coverage_payload([_spot("NIFTY")], {("OTHER", "1minute"): (1, 0, 0)})
        )
        assert row["completeness"] == "no_ledger_record"

    def test_duplicate_symbol_interval_is_ambiguous(self) -> None:
        # The ledger key has no exchange/segment — the SAME (symbol, interval)
        # under two directories cannot be attributed without guessing
        # (hostile C4): both rows degrade to an explicit ambiguous state.
        nse = _spot("NIFTY")
        bse = dict(_spot("NIFTY"), exchange="BSE", segment="INDEX")
        payload = build_spot_coverage_payload([nse, bse], {("NIFTY", "1minute"): (21, 0, 0)})
        rows = payload["rows"]
        assert isinstance(rows, list)
        assert [r["completeness"] for r in rows] == ["ambiguous", "ambiguous"]
        assert all(r["windows_settled"] is None for r in rows)

    def test_headline_is_unchanged_by_annotation(self) -> None:
        # The hub's pinned headline shape never changes (hostile C1/F10).
        payload = build_spot_coverage_payload(
            [_spot("NIFTY", bars=4)], {("NIFTY", "1minute"): (1, 0, 0)}
        )
        assert payload["headline"] == {"datasets": 1, "total_bars": 4}


def _option(underlying: str, *, series: int, bars: int) -> dict[str, object]:
    """One per-underlying expired-options coverage row (summarize_option_datasets shape)."""
    return {
        "underlying": underlying,
        "exchange": "NSE",
        "series": series,
        "bars": bars,
        "first_date": "2024-03-01",
        "last_date": "2024-03-02",
    }


class TestOptionCoverageHeadline:
    def test_empty_is_zero(self) -> None:
        # No underlyings → vacuously zero, never raises.
        assert option_coverage_headline([]) == {
            "underlyings": 0,
            "series": 0,
            "total_bars": 0,
        }

    def test_sums_series_and_bars_across_underlyings(self) -> None:
        head = option_coverage_headline(
            [_option("NIFTY", series=2, bars=4), _option("BANKNIFTY", series=3, bars=9)]
        )
        assert head == {"underlyings": 2, "series": 5, "total_bars": 13}


class TestBuildOptionCoveragePayload:
    def test_payload_has_headline_and_rows(self) -> None:
        payload = build_option_coverage_payload([_option("NIFTY", series=2, bars=4)])
        assert set(payload) == {"headline", "rows"}
        assert payload["headline"] == {"underlyings": 1, "series": 2, "total_bars": 4}
        assert payload["rows"][0]["underlying"] == "NIFTY"

    def test_payload_empty(self) -> None:
        payload = build_option_coverage_payload([])
        assert payload == {
            "headline": {"underlyings": 0, "series": 0, "total_bars": 0},
            "rows": [],
        }

    def test_payload_accepts_iterator_once(self) -> None:
        # An iterator (single-use) must still produce both headline + rows.
        payload = build_option_coverage_payload(iter([_option("NIFTY", series=1, bars=3)]))
        assert payload["headline"]["total_bars"] == 3
        assert payload["rows"][0]["underlying"] == "NIFTY"
