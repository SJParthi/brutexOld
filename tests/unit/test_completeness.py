"""Unit tests for the Data Completeness Scoreboard (DEC-COMPLETENESS-SCOREBOARD-001).

Pure re-aggregation surface — 100% line+branch offline (no lake / store / network). Covers
the per-symbol spot rollup (1-day + 1-minute verdicts + overall), the honest option
stable/growing/partial/failed fold, the aggregate count tables, the category/derived-interval
skips, sorting, and the cell-status drift guard against ``coverage_ledger``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from brutex.dashboard.completeness import (
    CATEGORY_EQUITY,
    CATEGORY_INDEX,
    CATEGORY_REFERENCE,
    DIM_FAILED,
    DIM_FULL,
    DIM_NONE,
    DIM_PARTIAL,
    OPT_FAILED,
    OPT_GROWING,
    OPT_NONE,
    OPT_PARTIAL,
    OPT_STABLE,
    _aggregate_options,
    _option_verdict,
    _overall_spot,
    build_completeness_scoreboard,
    categorize_instruments,
)
from brutex.dashboard.coverage_view import (
    COMPLETENESS_DERIVED,
    COMPLETENESS_FAILED,
    COMPLETENESS_NO_RECORD,
    COMPLETENESS_PENDING,
    COMPLETENESS_SETTLED,
)
from brutex.data.coverage_ledger import (
    STATUS_COMPLETE,
    STATUS_EXPIRIES_ONLY,
    STATUS_FAILED,
    STATUS_PENDING,
    CoverageRecord,
)


def _spot_row(
    symbol: str, interval: str, completeness: str, *, exchange: str = "NSE"
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "exchange": exchange,
        "interval": interval,
        "completeness": completeness,
    }


def _cov(
    underlying: str,
    year: int,
    status: str,
    *,
    expiries: int = 0,
    contracts: int = 0,
    exch: str = "NSE",
) -> CoverageRecord:
    return CoverageRecord(
        exch_id=exch,
        underlying_symbol=underlying,
        year=year,
        expiries_pulled=expiries,
        contracts_pulled=contracts,
        status=status,
        last_run_id="r1",
        last_fetched_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


# ── per-symbol spot verdicts ──────────────────────────────────────────────────────────


def test_symbol_full_when_both_intervals_settled() -> None:
    rows = [
        _spot_row("NSE-NIFTY", "1day", COMPLETENESS_SETTLED),
        _spot_row("NSE-NIFTY", "1minute", COMPLETENESS_SETTLED),
    ]
    board = build_completeness_scoreboard(rows, {"NSE-NIFTY": CATEGORY_INDEX}, [], {})
    [row] = board["spot"]["per_symbol"]
    assert row["1day"] == DIM_FULL and row["1minute"] == DIM_FULL and row["overall"] == DIM_FULL
    assert board["spot"]["by_category"][CATEGORY_INDEX] == {
        "total": 1,
        "full_1day": 1,
        "full_1minute": 1,
        "full_both": 1,
    }


def test_symbol_partial_when_one_minute_pending() -> None:
    rows = [
        _spot_row("NSE-RELIANCE", "1day", COMPLETENESS_SETTLED),
        _spot_row("NSE-RELIANCE", "1minute", COMPLETENESS_PENDING),
    ]
    board = build_completeness_scoreboard(rows, {"NSE-RELIANCE": CATEGORY_EQUITY}, [], {})
    [row] = board["spot"]["per_symbol"]
    assert (
        row["1day"] == DIM_FULL and row["1minute"] == DIM_PARTIAL and row["overall"] == DIM_PARTIAL
    )
    agg = board["spot"]["by_category"][CATEGORY_EQUITY]
    assert agg["full_1day"] == 1 and agg["full_1minute"] == 0 and agg["full_both"] == 0


def test_symbol_failed_when_any_dimension_failed() -> None:
    rows = [
        _spot_row("NSE-X", "1day", COMPLETENESS_FAILED),
        _spot_row("NSE-X", "1minute", COMPLETENESS_SETTLED),
    ]
    board = build_completeness_scoreboard(rows, {"NSE-X": CATEGORY_EQUITY}, [], {})
    [row] = board["spot"]["per_symbol"]
    assert row["1day"] == DIM_FAILED and row["overall"] == DIM_FAILED


def test_symbol_none_when_no_ledger_record() -> None:
    rows = [_spot_row("NSE-Y", "1day", COMPLETENESS_NO_RECORD)]
    board = build_completeness_scoreboard(rows, {"NSE-Y": CATEGORY_EQUITY}, [], {})
    [row] = board["spot"]["per_symbol"]
    assert row["1day"] == DIM_NONE and row["1minute"] == DIM_NONE and row["overall"] == DIM_NONE


def test_unmapped_symbol_and_derived_interval_skipped() -> None:
    rows = [
        _spot_row("NSE-NIFTY", "1day", COMPLETENESS_SETTLED),
        _spot_row("NSE-STRAY", "1day", COMPLETENESS_SETTLED),  # not in categories → skipped
        _spot_row("NSE-NIFTY", "5minute", COMPLETENESS_DERIVED),  # derived TF → skipped
    ]
    board = build_completeness_scoreboard(rows, {"NSE-NIFTY": CATEGORY_INDEX}, [], {})
    syms = [r["symbol"] for r in board["spot"]["per_symbol"]]
    assert syms == ["NSE-NIFTY"]  # stray excluded, derived interval ignored


def test_per_symbol_sorted_by_symbol_then_exchange() -> None:
    rows = [
        _spot_row("NSE-B", "1day", COMPLETENESS_SETTLED),
        _spot_row("NSE-A", "1day", COMPLETENESS_SETTLED),
    ]
    board = build_completeness_scoreboard(
        rows, {"NSE-A": CATEGORY_EQUITY, "NSE-B": CATEGORY_EQUITY}, [], {}
    )
    assert [r["symbol"] for r in board["spot"]["per_symbol"]] == ["NSE-A", "NSE-B"]


def test_overall_spot_direct_branches() -> None:
    assert _overall_spot(DIM_FULL, DIM_FULL) == DIM_FULL
    assert _overall_spot(DIM_FAILED, DIM_NONE) == DIM_FAILED
    assert _overall_spot(DIM_FULL, DIM_NONE) == DIM_PARTIAL  # one full, one missing → partial
    assert _overall_spot(DIM_PARTIAL, DIM_NONE) == DIM_PARTIAL
    assert _overall_spot(DIM_NONE, DIM_NONE) == DIM_NONE


# ── option verdicts ───────────────────────────────────────────────────────────────────


def test_option_verdict_all_states() -> None:
    assert _option_verdict([]) == OPT_NONE
    assert _option_verdict([STATUS_COMPLETE, STATUS_FAILED]) == OPT_FAILED  # any failed wins
    assert (
        _option_verdict([STATUS_COMPLETE, STATUS_PENDING]) == OPT_GROWING
    )  # any pending → growing
    assert _option_verdict([STATUS_COMPLETE, STATUS_EXPIRIES_ONLY]) == OPT_PARTIAL
    assert (
        _option_verdict([STATUS_COMPLETE, STATUS_COMPLETE]) == OPT_STABLE
    )  # all complete → stable


def test_option_rollup_sums_years_and_folds_status() -> None:
    records = [
        _cov("NIFTY", 2024, STATUS_COMPLETE, expiries=50, contracts=10_000),
        _cov("NIFTY", 2025, STATUS_COMPLETE, expiries=92, contracts=28_400),
    ]
    board = build_completeness_scoreboard([], {}, records, {"NIFTY": CATEGORY_INDEX})
    [row] = board["options"]["per_underlying"]
    assert row["expiries_pulled"] == 142 and row["contracts_pulled"] == 38_400
    assert row["years"] == [2024, 2025] and row["status"] == OPT_STABLE
    assert board["options"]["by_category"][CATEGORY_INDEX]["stable"] == 1


def test_option_unmapped_underlying_skipped() -> None:
    records = [_cov("STRAY", 2025, STATUS_COMPLETE)]
    board = build_completeness_scoreboard([], {}, records, {"NIFTY": CATEGORY_INDEX})
    assert board["options"]["per_underlying"] == []


def test_option_aggregate_counts_each_status() -> None:
    records = [
        _cov("A", 2025, STATUS_COMPLETE),  # stable
        _cov("B", 2025, STATUS_PENDING),  # growing
        _cov("C", 2025, STATUS_EXPIRIES_ONLY),  # partial
        _cov("D", 2025, STATUS_FAILED),  # failed
    ]
    cats = dict.fromkeys(("A", "B", "C", "D"), CATEGORY_EQUITY)
    board = build_completeness_scoreboard([], {}, records, cats)
    agg = board["options"]["by_category"][CATEGORY_EQUITY]
    assert agg == {"total": 4, "stable": 1, "growing_or_partial": 2, "failed": 1}


def test_aggregate_options_ignores_none_status_directly() -> None:
    """A defensive OPT_NONE row (never produced by the rollup) counts in total only."""
    agg = _aggregate_options([{"category": CATEGORY_EQUITY, "status": OPT_NONE}])
    assert agg[CATEGORY_EQUITY] == {"total": 1, "stable": 0, "growing_or_partial": 0, "failed": 0}


# ── shape + drift guard ───────────────────────────────────────────────────────────────


def test_scoreboard_shape_and_empty_categories_present() -> None:
    board = build_completeness_scoreboard([], {}, [], {})
    assert set(board) == {"spot", "options"}
    # every category bucket is present even with zero data (a stable table for the UI).
    for cat in (CATEGORY_INDEX, CATEGORY_EQUITY, CATEGORY_REFERENCE):
        assert board["spot"]["by_category"][cat]["total"] == 0
        assert board["options"]["by_category"][cat]["total"] == 0
    assert board["spot"]["per_symbol"] == [] and board["options"]["per_underlying"] == []


def test_cell_status_constants_do_not_drift_from_coverage_ledger() -> None:
    """The duplicated _CELL_* strings must equal the real coverage_ledger statuses."""
    from brutex.dashboard import completeness as c

    assert c._CELL_COMPLETE == STATUS_COMPLETE
    assert c._CELL_EXPIRIES_ONLY == STATUS_EXPIRIES_ONLY
    assert c._CELL_PENDING == STATUS_PENDING
    assert c._CELL_FAILED == STATUS_FAILED


class _Inst:
    """Minimal instrument stand-in (only the attribute categorize_instruments touches)."""

    def __init__(self, symbol_name: str) -> None:
        self.symbol_name = symbol_name


def test_categorize_instruments_all_three_buckets() -> None:
    instruments = [_Inst("NSE-NIFTY"), _Inst("NSE-INDIAVIX"), _Inst("NSE-RELIANCE")]
    cats = categorize_instruments(
        instruments,
        is_index=lambda i: i.symbol_name == "NSE-NIFTY",
        is_reference=lambda i: i.symbol_name == "NSE-INDIAVIX",
    )
    assert cats == {
        "NSE-NIFTY": CATEGORY_INDEX,
        "NSE-INDIAVIX": CATEGORY_REFERENCE,
        "NSE-RELIANCE": CATEGORY_EQUITY,
    }
