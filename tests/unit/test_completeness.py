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
    per_symbol_completeness,
    scoreboard_to_csv,
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
    symbol: str,
    interval: str,
    completeness: str,
    *,
    exchange: str = "NSE",
    first_date: str | None = None,
    last_date: str | None = None,
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "exchange": exchange,
        "interval": interval,
        "completeness": completeness,
        "first_date": first_date,
        "last_date": last_date,
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
    cats: dict[str, str] = dict.fromkeys(("A", "B", "C", "D"), CATEGORY_EQUITY)
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


def test_categorize_instruments_reference_wins_over_by_type_index() -> None:
    # DEC-COMPLETENESS-INDEX-CATEGORY-001: with the by-TYPE is_index predicate a reference-only
    # index (India VIX) satisfies BOTH is_index AND is_reference — reference is checked FIRST so
    # VIX stays REFERENCE, never INDEX. A non-brute-forced index (FINNIFTY) that is is_index but
    # NOT reference correctly lands in INDEX (the exact bug this fixes: it was bucketed "equity").
    instruments = [_Inst("NSE-FINNIFTY"), _Inst("NSE-INDIAVIX"), _Inst("NSE-RELIANCE")]
    cats = categorize_instruments(
        instruments,
        # by-type: FINNIFTY + INDIAVIX are both IDX; RELIANCE is EQ.
        is_index=lambda i: i.symbol_name in {"NSE-FINNIFTY", "NSE-INDIAVIX"},
        is_reference=lambda i: i.symbol_name == "NSE-INDIAVIX",
    )
    assert cats == {
        "NSE-FINNIFTY": CATEGORY_INDEX,  # real index, not reference → INDEX (was wrongly equity)
        "NSE-INDIAVIX": CATEGORY_REFERENCE,  # reference precedence beats the by-type index test
        "NSE-RELIANCE": CATEGORY_EQUITY,
    }


# ── per-symbol completeness WITH dates (DEC-COMPLETENESS-UNIVERSE-001) ─────────────────────


def test_per_symbol_both_intervals_with_dates_from_2020() -> None:
    """Both intervals settled from Jan-2020 → full + from_2020 True + explicit dates carried."""
    rows = [
        _spot_row(
            "NSE-NIFTY",
            "1day",
            COMPLETENESS_SETTLED,
            first_date="2020-01-01",
            last_date="2026-06-18",
        ),
        _spot_row(
            "NSE-NIFTY",
            "1minute",
            COMPLETENESS_SETTLED,
            first_date="2020-01-01",
            last_date="2026-06-18",
        ),
    ]
    out = per_symbol_completeness(rows, {"NSE-NIFTY": CATEGORY_INDEX}, [], {})
    entry = out["NSE-NIFTY"]
    assert entry["category"] == CATEGORY_INDEX and entry["exchange"] == "NSE"
    day = entry["spot"]["1day"]
    assert day["status"] == DIM_FULL and day["from_2020"] is True
    assert day["first_date"] == "2020-01-01" and day["last_date"] == "2026-06-18"
    assert entry["spot"]["overall"] == DIM_FULL
    # the absent option side gets the honest defaults.
    assert entry["options"]["status"] == OPT_NONE and entry["options"]["first_year"] is None


def test_per_symbol_from_2020_false_when_data_starts_2021() -> None:
    """A 1-minute series that only starts 2021 → from_2020 False (a late start, flagged)."""
    rows = [
        _spot_row("NSE-X", "1day", COMPLETENESS_SETTLED, first_date="2020-01-01"),
        _spot_row("NSE-X", "1minute", COMPLETENESS_PENDING, first_date="2021-03-04"),
    ]
    out = per_symbol_completeness(rows, {"NSE-X": CATEGORY_EQUITY}, [], {})
    spot = out["NSE-X"]["spot"]
    assert spot["1day"]["from_2020"] is True
    assert spot["1minute"]["from_2020"] is False and spot["1minute"]["status"] == DIM_PARTIAL
    assert spot["overall"] == DIM_PARTIAL  # one full, one pending


def test_per_symbol_first_date_present_but_last_date_missing() -> None:
    """A dimension with a first bar but no last_date keeps first_date + leaves last_date None.

    Mirrors the page's ``first→?`` render path: the strip shows the known start even when the
    last bar date is absent — first_date drives the from_2020 flag, last_date stays None.
    """
    rows = [
        _spot_row("NSE-Z", "1day", COMPLETENESS_SETTLED, first_date="2020-01-01", last_date=None)
    ]
    out = per_symbol_completeness(rows, {"NSE-Z": CATEGORY_EQUITY}, [], {})
    day = out["NSE-Z"]["spot"]["1day"]
    assert day["first_date"] == "2020-01-01" and day["last_date"] is None
    assert day["from_2020"] is True and day["status"] == DIM_FULL


def test_per_symbol_missing_dates_and_no_record_default_none() -> None:
    """A no-record row → status none, dates None, from_2020 False; 1-minute defaults to none."""
    rows = [_spot_row("NSE-Y", "1day", COMPLETENESS_NO_RECORD)]  # first_date/last_date None
    out = per_symbol_completeness(rows, {"NSE-Y": CATEGORY_EQUITY}, [], {})
    spot = out["NSE-Y"]["spot"]
    assert spot["1day"]["status"] == DIM_NONE and spot["1day"]["first_date"] is None
    assert spot["1day"]["from_2020"] is False
    assert spot["1minute"]["status"] == DIM_NONE  # never present → default
    assert spot["overall"] == DIM_NONE


def test_per_symbol_unmapped_symbol_and_derived_interval_skipped() -> None:
    rows = [
        _spot_row("NSE-NIFTY", "1day", COMPLETENESS_SETTLED, first_date="2020-01-01"),
        _spot_row("NSE-STRAY", "1day", COMPLETENESS_SETTLED),  # not in categories → skipped
        _spot_row("NSE-NIFTY", "5minute", COMPLETENESS_DERIVED),  # derived TF → skipped
    ]
    out = per_symbol_completeness(rows, {"NSE-NIFTY": CATEGORY_INDEX}, [], {})
    assert set(out) == {"NSE-NIFTY"}  # stray excluded; only the two real dimensions considered


def test_per_symbol_options_folded_with_year_span() -> None:
    """Option records fold per underlying → status + summed counts + sorted year span."""
    records = [
        _cov("NSE-NIFTY", 2025, STATUS_COMPLETE, expiries=92, contracts=28_400),
        _cov("NSE-NIFTY", 2024, STATUS_COMPLETE, expiries=50, contracts=10_000),
    ]
    out = per_symbol_completeness([], {}, records, {"NSE-NIFTY": CATEGORY_INDEX})
    opt = out["NSE-NIFTY"]["options"]
    assert opt["status"] == OPT_STABLE and opt["years"] == [2024, 2025]
    assert opt["expiries_pulled"] == 142 and opt["contracts_pulled"] == 38_400
    assert opt["first_year"] == 2024 and opt["last_year"] == 2025
    # options-only underlying → the spot side is the honest all-none default.
    assert out["NSE-NIFTY"]["spot"]["overall"] == DIM_NONE
    assert out["NSE-NIFTY"]["spot"]["1day"]["status"] == DIM_NONE


def test_per_symbol_options_unmapped_underlying_skipped() -> None:
    out = per_symbol_completeness([], {}, [_cov("STRAY", 2025, STATUS_COMPLETE)], {})
    assert out == {}


def test_per_symbol_spot_and_options_merge_on_same_symbol() -> None:
    """A symbol with BOTH spot rows and option records merges into one entry (no duplication)."""
    rows = [_spot_row("NSE-NIFTY", "1day", COMPLETENESS_SETTLED, first_date="2020-01-01")]
    records = [_cov("NSE-NIFTY", 2025, STATUS_PENDING)]
    cats = {"NSE-NIFTY": CATEGORY_INDEX}
    out = per_symbol_completeness(rows, cats, records, cats)
    [entry] = out.values()
    assert entry["spot"]["1day"]["status"] == DIM_FULL
    assert entry["options"]["status"] == OPT_GROWING  # the pending year → growing


# ── CSV report export (DEC-COMPLETENESS-PAGE-001) ─────────────────────────────────────────


def test_scoreboard_csv_full_board_has_all_sections_and_rows() -> None:
    """A populated board renders the four labelled sections + summary + per-row detail."""
    spot_rows = [
        _spot_row("NSE-NIFTY", "1day", COMPLETENESS_SETTLED),
        _spot_row("NSE-NIFTY", "1minute", COMPLETENESS_SETTLED),
        _spot_row("NSE-RELIANCE", "1day", COMPLETENESS_SETTLED),
        _spot_row("NSE-RELIANCE", "1minute", COMPLETENESS_PENDING),
    ]
    cats = {"NSE-NIFTY": CATEGORY_INDEX, "NSE-RELIANCE": CATEGORY_EQUITY}
    records = [_cov("NSE-NIFTY", 2024, STATUS_COMPLETE, expiries=50, contracts=10_000)]
    board = build_completeness_scoreboard(spot_rows, cats, records, cats)

    csv_text = scoreboard_to_csv(board)
    lines = csv_text.splitlines()

    # The four self-describing section titles are all present.
    assert "BruteX — Data Completeness Scoreboard" in lines[0]
    assert any(line.startswith("SUMMARY — spot") for line in lines)
    assert any(line.startswith("SUMMARY — options") for line in lines)
    assert any(line.startswith("DETAIL — spot") for line in lines)
    assert any(line.startswith("DETAIL — options") for line in lines)
    # The spot summary carries the exact per-category FULL counts (index: both full).
    assert f"{CATEGORY_INDEX},1,1,1,1" in lines
    # equity: 1-day full, 1-minute pending → full_1minute=0, full_both=0.
    assert f"{CATEGORY_EQUITY},1,1,0,0" in lines
    # The per-symbol detail rows are present with their verdicts.
    assert f"{CATEGORY_INDEX},NSE-NIFTY,NSE,{DIM_FULL},{DIM_FULL},{DIM_FULL}" in lines
    assert f"{CATEGORY_EQUITY},NSE-RELIANCE,NSE,{DIM_FULL},{DIM_PARTIAL},{DIM_PARTIAL}" in lines
    # The per-underlying option detail row carries status + summed counts + the years.
    assert f"{CATEGORY_INDEX},NSE-NIFTY,NSE,{OPT_STABLE},50,10000,2024" in lines


def test_scoreboard_csv_empty_board_keeps_section_skeleton() -> None:
    """An empty board still renders every section header + a zeroed per-category summary."""
    csv_text = scoreboard_to_csv(build_completeness_scoreboard([], {}, [], {}))
    lines = csv_text.splitlines()
    assert any(line.startswith("DETAIL — spot") for line in lines)
    assert any(line.startswith("DETAIL — options") for line in lines)
    # Each category still gets a zeroed summary row (the loops over CATEGORIES always run).
    for cat in (CATEGORY_INDEX, CATEGORY_EQUITY, CATEGORY_REFERENCE):
        assert f"{cat},0,0,0,0" in lines  # both spot + options zero rows share this shape


def test_scoreboard_csv_escapes_comma_in_symbol() -> None:
    """A symbol carrying a comma is RFC-4180 quoted, never splitting the row (extreme input)."""
    rows = [_spot_row("NSE-A,B", "1day", COMPLETENESS_SETTLED)]
    board = build_completeness_scoreboard(rows, {"NSE-A,B": CATEGORY_EQUITY}, [], {})
    csv_text = scoreboard_to_csv(board)
    assert '"NSE-A,B"' in csv_text  # the comma-bearing symbol is quoted, not field-split

    # And it round-trips through a strict CSV reader back to the original symbol.
    import csv as _csv
    from io import StringIO

    detail = [
        r
        for r in _csv.reader(StringIO(csv_text))
        if len(r) == 6 and r[1] == "NSE-A,B"  # the per-symbol detail row
    ]
    assert detail and detail[0][1] == "NSE-A,B"


def test_scoreboard_csv_with_per_symbol_enriches_spot_detail_with_dates() -> None:
    """DEC-COMPLETENESS-CSV-READABLE-001: a per_symbol map enriches the spot detail with the
    explicit first→last bar dates + from_2020 flag per dimension (the SAME rich info the
    /universe strip shows), so the CSV is never less informative than the page."""
    import csv as _csv
    from io import StringIO

    spot_rows = [
        _spot_row(
            "NSE-NIFTY",
            "1day",
            COMPLETENESS_SETTLED,
            first_date="2020-01-01",
            last_date="2026-06-18",
        ),
        _spot_row(
            "NSE-NIFTY",
            "1minute",
            COMPLETENESS_SETTLED,
            first_date="2020-01-01",
            last_date="2026-06-18",
        ),
        # FINNIFTY: 1-day starts 2025 (from_2020 = no); 1-minute NEVER pulled (no dates → empty cells).
        _spot_row(
            "NSE-FINNIFTY",
            "1day",
            COMPLETENESS_SETTLED,
            first_date="2025-01-01",
            last_date="2026-06-18",
        ),
        _spot_row("NSE-FINNIFTY", "1minute", COMPLETENESS_NO_RECORD),
    ]
    cats = {"NSE-NIFTY": CATEGORY_INDEX, "NSE-FINNIFTY": CATEGORY_INDEX}
    board = build_completeness_scoreboard(spot_rows, cats, [], {})
    psmap = per_symbol_completeness(spot_rows, cats, [], {})

    csv_text = scoreboard_to_csv(board, per_symbol=psmap)
    lines = csv_text.splitlines()
    # The enriched section title + the new date / from-2020 column headers are present.
    assert any(line.startswith("DETAIL — spot (per symbol with first") for line in lines)
    assert "1-day first bar" in csv_text and "1-day from 2020?" in csv_text

    rows = [r for r in _csv.reader(StringIO(csv_text)) if len(r) == 12]
    by_symbol = {r[1]: r for r in rows}
    # NIFTY: dated 2020→2026 on BOTH dimensions, from_2020 = yes (covers _csv_date truthy + _csv_yes_no True).
    nifty = by_symbol["NSE-NIFTY"]
    assert nifty[4] == "2020-01-01" and nifty[5] == "2026-06-18" and nifty[6] == "yes"
    assert nifty[8] == "2020-01-01" and nifty[9] == "2026-06-18" and nifty[10] == "yes"
    # FINNIFTY: 1-day starts 2025 → from_2020 = no; 1-minute no-record → EMPTY date cells + no
    # (covers _csv_date falsy + _csv_yes_no False).
    finnifty = by_symbol["NSE-FINNIFTY"]
    assert finnifty[4] == "2025-01-01" and finnifty[6] == "no"
    assert finnifty[8] == "" and finnifty[9] == "" and finnifty[10] == "no"
