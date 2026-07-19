"""Unit tests for the Data Completeness Scoreboard (DEC-COMPLETENESS-SCOREBOARD-001).

Pure re-aggregation surface — 100% line+branch offline (no lake / store / network). Covers
the per-symbol spot rollup (1-minute verdict = overall; 1-day is DERIVED from 1-minute so it
is NOT scored — DEC-1MINUTE-ONLY-DERIVE-1DAY-001), the honest option
stable/growing/partial/failed fold, the aggregate count tables, the category/derived-interval
skips, sorting, and the cell-status drift guard against ``coverage_ledger``.
"""

from __future__ import annotations

import math
import threading
from datetime import UTC, date, datetime, timedelta
from typing import Any

from brutex.calendars import trading_days_between
from brutex.dashboard.completeness import (
    CATEGORY_EQUITY,
    CATEGORY_INDEX,
    CATEGORY_REFERENCE,
    DIM_FAILED,
    DIM_FULL,
    DIM_NONE,
    DIM_PARTIAL,
    DIM_SETTLED_PARTIAL_RANGE,
    OPT_CONTRACTS_BY_DESIGN_NOTE,
    OPT_FAILED,
    OPT_GROWING,
    OPT_NONE,
    OPT_PARTIAL,
    OPT_STABLE,
    RECONCILE_MISMATCH_EVENT,
    SPOT_SESSION_RATIO_FLOOR,
    SYMBOLS_NOT_YET_PULLED_EVENT,
    ReconcileLogGate,
    SymbolReconcileReport,
    _aggregate_options,
    _contracts_note,
    _option_verdict,
    _overall_spot,
    _resolve_spot_category,
    _session_days_of,
    _session_range_audit,
    _spot_cell_supersedes,
    _spot_dimension_verdict_with_floor,
    build_completeness_scoreboard,
    categorize_instruments,
    log_symbol_reconcile_from_board,
    per_symbol_completeness,
    reconcile_symbol_coverage,
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
from structlog.testing import capture_logs


def _spot_row(
    symbol: str,
    interval: str,
    completeness: str,
    *,
    exchange: str = "NSE",
    segment: str = "CASH",
    first_date: str | None = None,
    last_date: str | None = None,
    session_days: int | None = None,
) -> dict[str, Any]:
    # Production rows from ``summarize_spot_datasets`` always carry ``segment``; the
    # reconciliation diagnostic filters on it (spot segments only — F&O ``1minute``
    # contract partitions are NOT scored underlyings). Default ``CASH`` keeps every
    # pre-existing row a scored spot symbol. ``session_days`` (the
    # DEC-SPOT-PARTIAL-RANGE-CHIP-001 lake scalar) defaults ``None`` so pre-scalar
    # rows exercise the session-audit ABSTAIN path.
    return {
        "symbol": symbol,
        "exchange": exchange,
        "segment": segment,
        "interval": interval,
        "completeness": completeness,
        "first_date": first_date,
        "last_date": last_date,
        "session_days": session_days,
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


def test_symbol_full_when_one_minute_settled() -> None:
    # 1-day is DERIVED from 1-minute so it is NOT scored — only the fetched 1-minute is, and a
    # stray 1-day row is ignored (covers the SPOT_DIMENSIONS skip for 1-day). The 1-minute series
    # starts at the 2020 floor → genuinely ``full`` (DEC-SPOT-COMPLETENESS-2020-FLOOR-001).
    rows = [
        _spot_row("NSE-NIFTY", "1day", COMPLETENESS_SETTLED),  # derived 1-day → skipped
        _spot_row("NSE-NIFTY", "1minute", COMPLETENESS_SETTLED, first_date="2020-01-01"),
    ]
    board = build_completeness_scoreboard(rows, {"NSE-NIFTY": CATEGORY_INDEX}, [], {})
    [row] = board["spot"]["per_symbol"]
    assert row["1minute"] == DIM_FULL and row["overall"] == DIM_FULL
    assert "1day" not in row  # the 1-day column is gone from the per-symbol view
    assert board["spot"]["by_category"][CATEGORY_INDEX] == {
        "total": 1,
        "full_1minute": 1,
        # Re-baselined for DEC-DAILY-OPEN-SUBSTITUTION-HONESTY-001: hand-built rows
        # carry no substitution counts -> the honest 0 default.
        "open_substituted_days": 0,
    }


def test_symbol_partial_when_one_minute_pending() -> None:
    rows = [_spot_row("NSE-RELIANCE", "1minute", COMPLETENESS_PENDING)]
    board = build_completeness_scoreboard(rows, {"NSE-RELIANCE": CATEGORY_EQUITY}, [], {})
    [row] = board["spot"]["per_symbol"]
    assert row["1minute"] == DIM_PARTIAL and row["overall"] == DIM_PARTIAL
    agg = board["spot"]["by_category"][CATEGORY_EQUITY]
    assert agg == {"total": 1, "full_1minute": 0, "open_substituted_days": 0}


def test_symbol_failed_when_one_minute_failed() -> None:
    rows = [_spot_row("NSE-X", "1minute", COMPLETENESS_FAILED)]
    board = build_completeness_scoreboard(rows, {"NSE-X": CATEGORY_EQUITY}, [], {})
    [row] = board["spot"]["per_symbol"]
    assert row["1minute"] == DIM_FAILED and row["overall"] == DIM_FAILED


def test_symbol_none_when_no_ledger_record() -> None:
    rows = [_spot_row("NSE-Y", "1minute", COMPLETENESS_NO_RECORD)]
    board = build_completeness_scoreboard(rows, {"NSE-Y": CATEGORY_EQUITY}, [], {})
    [row] = board["spot"]["per_symbol"]
    assert row["1minute"] == DIM_NONE and row["overall"] == DIM_NONE


def test_symbol_partial_when_settled_but_data_starts_after_2020() -> None:
    """DEC-SPOT-COMPLETENESS-2020-FLOOR-001 — a 1-minute series whose windows are all settled
    but whose earliest bar is LATE (a late listing / late data, e.g. JIOFIN 2023-08-21) does
    NOT hold the solid 2020-floor history the deep-drill brute-force backtest needs and is
    excluded from the FULL 1-minute count. Since DEC-SPOT-PARTIAL-RANGE-CHIP-001 the downgrade
    target is the distinct AMBER ``settled_partial_range`` (settled — nothing re-pulls — but
    the range is honestly incomplete), no longer the pending-windows ``partial``."""
    rows = [_spot_row("NSE-JIOFIN", "1minute", COMPLETENESS_SETTLED, first_date="2023-08-21")]
    board = build_completeness_scoreboard(rows, {"NSE-JIOFIN": CATEGORY_EQUITY}, [], {})
    [row] = board["spot"]["per_symbol"]
    assert row["1minute"] == DIM_SETTLED_PARTIAL_RANGE
    assert row["overall"] == DIM_SETTLED_PARTIAL_RANGE
    # the actually-covered start is carried so the operator SEES why it is partial.
    assert row["1minute_start"] == "2023-08-21"
    # a late-start symbol does NOT inflate the FULL 1-minute count.
    assert board["spot"]["by_category"][CATEGORY_EQUITY] == {
        "total": 1,
        "full_1minute": 0,
        "open_substituted_days": 0,
    }


def test_symbol_partial_when_settled_but_no_first_date() -> None:
    """A settled 1-minute dimension with NO bars at all (every planned window resolved empty →
    no first_date) cannot claim solid 2020 history → ``partial``, never ``full``
    (DEC-SPOT-COMPLETENESS-2020-FLOOR-001 — the ``from_2020`` False / first_date None edge)."""
    rows = [_spot_row("NSE-EMPTY", "1minute", COMPLETENESS_SETTLED)]  # first_date defaults None
    board = build_completeness_scoreboard(rows, {"NSE-EMPTY": CATEGORY_EQUITY}, [], {})
    [row] = board["spot"]["per_symbol"]
    assert row["1minute"] == DIM_SETTLED_PARTIAL_RANGE
    assert row["overall"] == DIM_SETTLED_PARTIAL_RANGE


def test_within_2020_late_start_is_partial_january_floor() -> None:
    """DEC-SPOT-COMPLETENESS-2020-FLOOR-001 (operator decree "must have Jan-2020 data") — a symbol
    whose first bar is LATER within 2020 (TATACONSUM 2020-02-27, MAXHEALTH 2020-08-21) is missing
    the START of 2020 → ``partial``, NOT ``full``. Only a January-2020 start counts as solid."""
    rows = [
        _spot_row("NSE-TATACONSUM", "1minute", COMPLETENESS_SETTLED, first_date="2020-02-27"),
        _spot_row("NSE-MAXHEALTH", "1minute", COMPLETENESS_SETTLED, first_date="2020-08-21"),
    ]
    cats = {"NSE-TATACONSUM": CATEGORY_EQUITY, "NSE-MAXHEALTH": CATEGORY_EQUITY}
    board = build_completeness_scoreboard(rows, cats, [], {})
    verdicts = {r["symbol"]: r["1minute"] for r in board["spot"]["per_symbol"]}
    assert verdicts == {
        "NSE-TATACONSUM": DIM_SETTLED_PARTIAL_RANGE,
        "NSE-MAXHEALTH": DIM_SETTLED_PARTIAL_RANGE,
    }
    # neither within-2020-late symbol counts toward the FULL 1-minute total.
    assert board["spot"]["by_category"][CATEGORY_EQUITY] == {
        "total": 2,
        "full_1minute": 0,
        "open_substituted_days": 0,
    }


def test_january_2020_boundary_full_vs_partial() -> None:
    """The January-2020 floor boundary (DEC-SPOT-COMPLETENESS-2020-FLOOR-001): a last-January-day
    start (2020-01-31) is still solid → ``full``; the very next day (2020-02-01, the exclusive
    upper bound) is already missing the start of 2020 → ``partial``."""
    rows = [
        _spot_row("NSE-JANEND", "1minute", COMPLETENESS_SETTLED, first_date="2020-01-31"),
        _spot_row("NSE-FEB1", "1minute", COMPLETENESS_SETTLED, first_date="2020-02-01"),
    ]
    cats = {"NSE-JANEND": CATEGORY_EQUITY, "NSE-FEB1": CATEGORY_EQUITY}
    board = build_completeness_scoreboard(rows, cats, [], {})
    verdicts = {r["symbol"]: r["1minute"] for r in board["spot"]["per_symbol"]}
    assert verdicts == {"NSE-JANEND": DIM_FULL, "NSE-FEB1": DIM_SETTLED_PARTIAL_RANGE}


def test_per_symbol_detail_downgrades_late_start_to_partial() -> None:
    """The per-symbol DETAIL map (the /universe strip) applies the SAME 2020-floor downgrade
    (DEC-SPOT-COMPLETENESS-2020-FLOOR-001): a settled-but-late-start dimension reads ``partial``
    with from_2020 False, while the explicit first/last dates disambiguate the reason."""
    rows = [
        _spot_row(
            "NSE-TMPV",
            "1minute",
            COMPLETENESS_SETTLED,
            first_date="2025-10-24",
            last_date="2026-06-19",
        )
    ]
    out = per_symbol_completeness(rows, {"NSE-TMPV": CATEGORY_EQUITY}, [], {})
    minute = out["NSE-TMPV"]["spot"]["1minute"]
    assert minute["status"] == DIM_SETTLED_PARTIAL_RANGE and minute["from_2020"] is False
    assert minute["first_date"] == "2025-10-24" and minute["last_date"] == "2026-06-19"
    assert out["NSE-TMPV"]["spot"]["overall"] == DIM_SETTLED_PARTIAL_RANGE


# ── settled_partial_range — the session-density arm (DEC-SPOT-PARTIAL-RANGE-CHIP-001) ──


def _span_with_sessions(n: int) -> tuple[date, date]:
    """The shortest span from 2024-01-01 holding exactly *n* NSE trading sessions."""
    start = date(2024, 1, 1)
    end = start
    while len(trading_days_between(start, end)) < n:
        end += timedelta(days=1)
    return start, end


def test_session_range_audit_full_density_ratio_one() -> None:
    start, end = _span_with_sessions(20)
    expected, ratio = _session_range_audit(start.isoformat(), end.isoformat(), 20)
    assert expected == 20 and ratio == 1.0


def test_session_ratio_exactly_at_floor_stays_full() -> None:
    # 19/20 = 0.95 exactly — NOT below the floor → the settled verdict stays full.
    start, end = _span_with_sessions(20)
    verdict = _spot_dimension_verdict_with_floor(
        COMPLETENESS_SETTLED,
        "2020-01-01",
        last_date="2020-01-31",
        session_days=None,
    )
    assert verdict == DIM_FULL  # abstain baseline (no scalar)
    expected = len(trading_days_between(date(2020, 1, 1), date(2020, 1, 31)))
    at_floor = math.ceil(SPOT_SESSION_RATIO_FLOOR * expected)
    assert (
        _spot_dimension_verdict_with_floor(
            COMPLETENESS_SETTLED,
            "2020-01-01",
            last_date="2020-01-31",
            session_days=at_floor,
        )
        == DIM_FULL
    )
    # One session fewer breaches the floor → the AMBER settled_partial_range.
    assert (
        _spot_dimension_verdict_with_floor(
            COMPLETENESS_SETTLED,
            "2020-01-01",
            last_date="2020-01-31",
            session_days=at_floor - 1,
        )
        == DIM_SETTLED_PARTIAL_RANGE
    )
    # The exact 0.95 boundary on a clean 20-session span: 19/20 full, 18/20 amber.
    _exp2, ratio_at = _session_range_audit(start.isoformat(), end.isoformat(), 19)
    assert ratio_at == SPOT_SESSION_RATIO_FLOOR
    _exp3, ratio_below = _session_range_audit(start.isoformat(), end.isoformat(), 18)
    assert ratio_below is not None and ratio_below < SPOT_SESSION_RATIO_FLOOR


def test_floor_fail_now_reads_amber_not_partial() -> None:
    # DEC-SPOT-PARTIAL-RANGE-CHIP-001: the Jan-2020-floor fail is a SETTLED range
    # problem, so its downgrade target is the amber verdict, no longer DIM_PARTIAL.
    verdict = _spot_dimension_verdict_with_floor(COMPLETENESS_SETTLED, "2023-08-21")
    assert verdict == DIM_SETTLED_PARTIAL_RANGE
    assert verdict != DIM_PARTIAL


def test_session_audit_abstains_keep_full() -> None:
    # Every abstain shape keeps the settled verdict FULL (never a guessed amber):
    base = {"completeness": COMPLETENESS_SETTLED}
    # (a) no lake scalar;
    assert (
        _spot_dimension_verdict_with_floor(
            base["completeness"], "2020-01-01", last_date="2026-01-30", session_days=None
        )
        == DIM_FULL
    )
    # (b) missing last_date;
    assert (
        _spot_dimension_verdict_with_floor(
            base["completeness"], "2020-01-01", last_date=None, session_days=1
        )
        == DIM_FULL
    )
    # (c) unparseable last_date;
    assert (
        _spot_dimension_verdict_with_floor(
            base["completeness"], "2020-01-01", last_date="not-a-date", session_days=1
        )
        == DIM_FULL
    )
    # (d) span exits the published holiday coverage (out-of-calendar ABSTAIN);
    assert (
        _spot_dimension_verdict_with_floor(
            base["completeness"], "2020-01-01", last_date="2031-01-05", session_days=1
        )
        == DIM_FULL
    )
    # (e) reversed span.
    assert (
        _spot_dimension_verdict_with_floor(
            base["completeness"], "2020-01-15", last_date="2020-01-02", session_days=1
        )
        == DIM_FULL
    )


def test_session_range_audit_zero_expected_abstains() -> None:
    # A weekend-only span has ZERO trading sessions → (None, None), never a divide.
    assert _session_range_audit("2024-01-06", "2024-01-07", 0) == (None, None)


def test_session_days_of_row_helper() -> None:
    assert _session_days_of({"session_days": 3}) == 3
    assert _session_days_of({"session_days": 0}) == 0
    assert _session_days_of({}) is None
    assert _session_days_of({"session_days": None}) is None


def test_scoreboard_sparse_settled_range_reads_amber() -> None:
    # End-to-end through the scoreboard fold: a settled Jan-2020-anchored symbol whose
    # lake holds only 3 session-days over a multi-month span reads the amber
    # settled_partial_range — and is excluded from the FULL 1-minute count.
    rows = [
        _spot_row(
            "NSE-SPARSE",
            "1minute",
            COMPLETENESS_SETTLED,
            first_date="2020-01-01",
            last_date="2020-03-31",
            session_days=3,
        )
    ]
    board = build_completeness_scoreboard(rows, {"NSE-SPARSE": CATEGORY_EQUITY}, [], {})
    [row] = board["spot"]["per_symbol"]
    assert row["1minute"] == DIM_SETTLED_PARTIAL_RANGE
    # ``open_substituted_days`` rides every by_category bucket since the merged
    # DEC-DAILY-OPEN-SUBSTITUTION-HONESTY-001 (0 here — no substitution observed).
    assert board["spot"]["by_category"][CATEGORY_EQUITY] == {
        "total": 1,
        "full_1minute": 0,
        "open_substituted_days": 0,
    }


def test_per_symbol_detail_exposes_session_honesty_fields() -> None:
    # The /universe per-symbol map carries session_days / expected_sessions /
    # session_ratio so the operator SEES the density behind the amber verdict.
    rows = [
        _spot_row(
            "NSE-SPARSE",
            "1minute",
            COMPLETENESS_SETTLED,
            first_date="2020-01-01",
            last_date="2020-03-31",
            session_days=3,
        )
    ]
    out = per_symbol_completeness(rows, {"NSE-SPARSE": CATEGORY_EQUITY}, [], {})
    minute = out["NSE-SPARSE"]["spot"]["1minute"]
    assert minute["status"] == DIM_SETTLED_PARTIAL_RANGE
    assert minute["session_days"] == 3
    expected = len(trading_days_between(date(2020, 1, 1), date(2020, 3, 31)))
    assert minute["expected_sessions"] == expected
    assert minute["session_ratio"] == 3 / expected
    # An abstaining row (no scalar) exposes honest Nones, never fabricated zeros.
    rows2 = [_spot_row("NSE-OK", "1minute", COMPLETENESS_SETTLED, first_date="2020-01-01")]
    out2 = per_symbol_completeness(rows2, {"NSE-OK": CATEGORY_EQUITY}, [], {})
    minute2 = out2["NSE-OK"]["spot"]["1minute"]
    assert minute2["status"] == DIM_FULL
    assert minute2["expected_sessions"] is None and minute2["session_ratio"] is None


def test_amber_rank_sits_between_partial_and_full() -> None:
    # _spot_cell_supersedes rank regression (order-independence with the new verdict):
    # full > settled_partial_range > partial > failed > none.
    assert _spot_cell_supersedes(DIM_FULL, "2020-01-01", DIM_SETTLED_PARTIAL_RANGE, "2020-01-01")
    assert not _spot_cell_supersedes(
        DIM_SETTLED_PARTIAL_RANGE, "2020-01-01", DIM_FULL, "2020-01-01"
    )
    assert _spot_cell_supersedes(DIM_SETTLED_PARTIAL_RANGE, "2020-01-01", DIM_PARTIAL, "2020-01-01")
    assert not _spot_cell_supersedes(
        DIM_PARTIAL, "2020-01-01", DIM_SETTLED_PARTIAL_RANGE, "2020-01-01"
    )
    assert _spot_cell_supersedes(DIM_SETTLED_PARTIAL_RANGE, "2020-01-01", DIM_FAILED, "2020-01-01")


def test_unmapped_symbol_and_derived_interval_skipped() -> None:
    rows = [
        _spot_row("NSE-NIFTY", "1minute", COMPLETENESS_SETTLED),
        _spot_row("NSE-STRAY", "1minute", COMPLETENESS_SETTLED),  # not in categories → skipped
        _spot_row("NSE-NIFTY", "5minute", COMPLETENESS_DERIVED),  # derived TF → skipped
        _spot_row("NSE-NIFTY", "1day", COMPLETENESS_SETTLED),  # derived 1-day → skipped
    ]
    board = build_completeness_scoreboard(rows, {"NSE-NIFTY": CATEGORY_INDEX}, [], {})
    syms = [r["symbol"] for r in board["spot"]["per_symbol"]]
    assert syms == ["NSE-NIFTY"]  # stray excluded, derived intervals ignored


def test_per_symbol_sorted_by_symbol_then_exchange() -> None:
    rows = [
        _spot_row("NSE-B", "1minute", COMPLETENESS_SETTLED),
        _spot_row("NSE-A", "1minute", COMPLETENESS_SETTLED),
    ]
    board = build_completeness_scoreboard(
        rows, {"NSE-A": CATEGORY_EQUITY, "NSE-B": CATEGORY_EQUITY}, [], {}
    )
    assert [r["symbol"] for r in board["spot"]["per_symbol"]] == ["NSE-A", "NSE-B"]


def test_overall_spot_is_the_one_minute_verdict() -> None:
    # The overall spot verdict IS the 1-minute verdict alone — no second dimension to AND
    # against (DEC-1MINUTE-ONLY-DERIVE-1DAY-001).
    assert _overall_spot(DIM_FULL) == DIM_FULL
    assert _overall_spot(DIM_PARTIAL) == DIM_PARTIAL
    assert _overall_spot(DIM_FAILED) == DIM_FAILED
    assert _overall_spot(DIM_NONE) == DIM_NONE


# ── option verdicts ───────────────────────────────────────────────────────────────────


def test_option_verdict_all_states() -> None:
    # Cells are per-year (status, expiries_pulled); only expiries>0 years are real history
    # (DEC-OPTIONS-COMPLETENESS-REAL-YEARS-001).
    assert _option_verdict([]) == OPT_NONE
    # any failed wins (even if that year had no expiries)
    assert _option_verdict([(STATUS_COMPLETE, 50), (STATUS_FAILED, 0)]) == OPT_FAILED
    # DEFENSIVE / forward-compat branch (UNREACHABLE in production — the ledger invariant is
    # pending ⟺ expiries_pulled==0, so a (pending, n>0) cell is never emitted; hostile-review
    # #859). Synthetic input exercises the retained `growing` fold, NOT a real pipeline state.
    assert _option_verdict([(STATUS_COMPLETE, 50), (STATUS_PENDING, 20)]) == OPT_GROWING
    # an expiries_only year (with expiries) → partial
    assert _option_verdict([(STATUS_COMPLETE, 50), (STATUS_EXPIRIES_ONLY, 12)]) == OPT_PARTIAL
    # every requested year complete AND non-empty → stable
    assert _option_verdict([(STATUS_COMPLETE, 50), (STATUS_COMPLETE, 92)]) == OPT_STABLE
    # late-listed underlying: a pre-listing year has ZERO expiries (pending) → NOT stuck
    # growing; the real years are complete → partial (honest late-start)
    assert _option_verdict([(STATUS_PENDING, 0), (STATUS_COMPLETE, 92)]) == OPT_PARTIAL
    # every requested year returned zero expiries → none (no option history at all)
    assert _option_verdict([(STATUS_PENDING, 0), (STATUS_PENDING, 0)]) == OPT_NONE


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


def test_late_listed_underlying_shows_real_years_and_partial_not_growing() -> None:
    """A late-listed underlying shows ONLY its real expiry years + ``partial`` (DEC-OPTIONS-
    COMPLETENESS-REAL-YEARS-001) — not a fake 2020→2026 span nor a perpetual ``growing``.

    JIOFIN's options began ~2023: the pre-listing years 2020-2022 returned ZERO expiries and
    are recorded ``pending``. Before the fix they (a) inflated ``years`` to the full requested
    span and (b) forced ``growing`` forever. Now: the empty years are excluded from ``years``
    and the verdict is ``partial`` (honest late-start), while the real years are complete.
    """
    records = [
        _cov("JIOFIN", 2020, STATUS_PENDING, expiries=0),  # pre-listing — no expiries
        _cov("JIOFIN", 2021, STATUS_PENDING, expiries=0),  # pre-listing — no expiries
        _cov("JIOFIN", 2022, STATUS_PENDING, expiries=0),  # pre-listing — no expiries
        _cov("JIOFIN", 2023, STATUS_COMPLETE, expiries=10, contracts=400),
        _cov("JIOFIN", 2024, STATUS_COMPLETE, expiries=12, contracts=600),
    ]
    board = build_completeness_scoreboard([], {}, records, {"JIOFIN": CATEGORY_EQUITY})
    [row] = board["options"]["per_underlying"]
    # YEARS shows ONLY the real expiry years — never the empty pre-listing years.
    assert row["years"] == [2023, 2024]
    # Honest verdict: partial (late start), NOT stuck growing.
    assert row["status"] == OPT_PARTIAL
    # Counts still sum across all folded cells (empty years contribute 0).
    assert row["expiries_pulled"] == 22 and row["contracts_pulled"] == 1000
    # The per-symbol view agrees (same shared rollup).
    out = per_symbol_completeness([], {}, records, {"JIOFIN": CATEGORY_EQUITY})
    opt = out["JIOFIN"]["options"]
    assert opt["years"] == [2023, 2024] and opt["status"] == OPT_PARTIAL
    assert opt["first_year"] == 2023 and opt["last_year"] == 2024


# ── honest "0 contracts = metadata-only by design" label (DEC-METADATA-LIVE-PROGRESS-001) ──


def test_contracts_note_only_for_expiries_only_zero_contracts() -> None:
    """The by-design note attaches ONLY to an ``expiries_only`` verdict with 0 contracts.

    Any other verdict, or a row that DID persist contracts, gets ``""`` — so the page
    can never attach a false "metadata-only by design" claim (DEC-METADATA-LIVE-PROGRESS-001).
    """
    # The one honest by-design case: expiries captured, legs intentionally discarded.
    assert _contracts_note(OPT_PARTIAL, 0) == OPT_CONTRACTS_BY_DESIGN_NOTE
    # A partial row that somehow DID persist contracts is NOT "0 by design".
    assert _contracts_note(OPT_PARTIAL, 5) == ""
    # Every other verdict (even at 0 contracts) is not the metadata-only state.
    assert _contracts_note(OPT_STABLE, 0) == ""
    assert _contracts_note(OPT_GROWING, 0) == ""
    assert _contracts_note(OPT_FAILED, 0) == ""
    assert _contracts_note(OPT_NONE, 0) == ""


def test_per_underlying_options_annotates_metadata_only_by_design() -> None:
    """An expiries_only/0-contract underlying carries the honest by-design note in the payload.

    This is the misleading ``contracts pulled = 0`` made HONEST: it is metadata-only by
    design (legs discarded — DEC-INSTRUMENT-UNIVERSE-FAST-001), NOT a broken pull.
    """
    records = [_cov("NIFTY", 2026, STATUS_EXPIRIES_ONLY, expiries=42, contracts=0)]
    board = build_completeness_scoreboard([], {}, records, {"NIFTY": CATEGORY_INDEX})
    [row] = board["options"]["per_underlying"]
    assert row["status"] == OPT_PARTIAL
    assert row["contracts_pulled"] == 0
    assert row["contracts_note"] == OPT_CONTRACTS_BY_DESIGN_NOTE
    assert "by design" in row["contracts_note"]
    assert "DEC-INSTRUMENT-UNIVERSE-FAST-001" in row["contracts_note"]


def test_per_underlying_options_no_note_when_contracts_present() -> None:
    """A stable underlying WITH persisted contracts gets an empty note (no false claim)."""
    records = [_cov("NIFTY", 2025, STATUS_COMPLETE, expiries=50, contracts=10_000)]
    board = build_completeness_scoreboard([], {}, records, {"NIFTY": CATEGORY_INDEX})
    [row] = board["options"]["per_underlying"]
    assert row["status"] == OPT_STABLE
    assert row["contracts_note"] == ""


def test_per_symbol_options_carries_by_design_note() -> None:
    """The per-symbol ``/universe`` strip also annotates the by-design 0-contract state."""
    records = [_cov("NIFTY", 2026, STATUS_EXPIRIES_ONLY, expiries=12, contracts=0)]
    out = per_symbol_completeness([], {}, records, {"NIFTY": CATEGORY_INDEX})
    opt = out["NIFTY"]["options"]
    assert opt["status"] == OPT_PARTIAL
    assert opt["contracts_note"] == OPT_CONTRACTS_BY_DESIGN_NOTE


def test_per_symbol_options_default_has_empty_note() -> None:
    """A symbol with NO option records gets the OPT_NONE default + an empty note."""
    spot_rows = [_spot_row("NSE-NIFTY", "1minute", COMPLETENESS_SETTLED)]
    out = per_symbol_completeness(spot_rows, {"NSE-NIFTY": CATEGORY_INDEX}, [], {})
    opt = out["NSE-NIFTY"]["options"]
    assert opt["status"] == OPT_NONE
    assert opt["contracts_note"] == ""


def test_scoreboard_csv_options_detail_has_by_design_note() -> None:
    """The downloadable CSV's options detail surfaces the honest by-design note too."""
    records = [_cov("NIFTY", 2026, STATUS_EXPIRIES_ONLY, expiries=7, contracts=0)]
    board = build_completeness_scoreboard([], {}, records, {"NIFTY": CATEGORY_INDEX})
    csv_text = scoreboard_to_csv(board)
    assert "Notes" in csv_text  # the new options-detail column header
    assert "metadata-only by design" in csv_text


def test_option_aggregate_counts_each_status() -> None:
    records = [
        _cov("A", 2025, STATUS_COMPLETE, expiries=50),  # stable
        # DEFENSIVE / forward-compat: a (pending, n>0) cell is unreachable in production
        # (pending ⟺ 0 expiries; hostile-review #859) — synthetic, exercises the `growing` fold.
        _cov("B", 2025, STATUS_PENDING, expiries=20),  # → growing (defensive branch)
        _cov("C", 2025, STATUS_EXPIRIES_ONLY, expiries=12),  # partial
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


def test_per_symbol_one_minute_with_dates_from_2020() -> None:
    """1-minute settled from Jan-2020 → full + from_2020 True + explicit dates carried.

    A stray derived 1-day row is ignored (only 1-minute is scored — no 1-day sub-dict exists).
    """
    rows = [
        _spot_row(
            "NSE-NIFTY",
            "1day",  # derived 1-day → skipped (not in SPOT_DIMENSIONS)
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
    assert "1day" not in entry["spot"]  # 1-day sub-dict dropped
    minute = entry["spot"]["1minute"]
    assert minute["status"] == DIM_FULL and minute["from_2020"] is True
    assert minute["first_date"] == "2020-01-01" and minute["last_date"] == "2026-06-18"
    assert entry["spot"]["overall"] == DIM_FULL
    # the absent option side gets the honest defaults.
    assert entry["options"]["status"] == OPT_NONE and entry["options"]["first_year"] is None


def test_per_symbol_from_2020_false_when_data_starts_2021() -> None:
    """A 1-minute series that only starts 2021 → from_2020 False (a late start, flagged)."""
    rows = [_spot_row("NSE-X", "1minute", COMPLETENESS_PENDING, first_date="2021-03-04")]
    out = per_symbol_completeness(rows, {"NSE-X": CATEGORY_EQUITY}, [], {})
    spot = out["NSE-X"]["spot"]
    assert spot["1minute"]["from_2020"] is False and spot["1minute"]["status"] == DIM_PARTIAL
    assert spot["overall"] == DIM_PARTIAL


def test_per_symbol_first_date_present_but_last_date_missing() -> None:
    """A dimension with a first bar but no last_date keeps first_date + leaves last_date None.

    Mirrors the page's ``first→?`` render path: the strip shows the known start even when the
    last bar date is absent — first_date drives the from_2020 flag, last_date stays None.
    """
    rows = [
        _spot_row("NSE-Z", "1minute", COMPLETENESS_SETTLED, first_date="2020-01-01", last_date=None)
    ]
    out = per_symbol_completeness(rows, {"NSE-Z": CATEGORY_EQUITY}, [], {})
    minute = out["NSE-Z"]["spot"]["1minute"]
    assert minute["first_date"] == "2020-01-01" and minute["last_date"] is None
    assert minute["from_2020"] is True and minute["status"] == DIM_FULL


def test_per_symbol_missing_dates_and_no_record_default_none() -> None:
    """A no-record 1-minute row → status none, dates None, from_2020 False."""
    rows = [_spot_row("NSE-Y", "1minute", COMPLETENESS_NO_RECORD)]  # first_date/last_date None
    out = per_symbol_completeness(rows, {"NSE-Y": CATEGORY_EQUITY}, [], {})
    spot = out["NSE-Y"]["spot"]
    assert spot["1minute"]["status"] == DIM_NONE and spot["1minute"]["first_date"] is None
    assert spot["1minute"]["from_2020"] is False
    assert spot["overall"] == DIM_NONE


def test_per_symbol_options_only_symbol_spot_defaults_to_none() -> None:
    """An options-only underlying (no spot row at all) → spot side is the all-none default."""
    out = per_symbol_completeness(
        [], {}, [_cov("NSE-NIFTY", 2025, STATUS_PENDING)], {"NSE-NIFTY": CATEGORY_INDEX}
    )
    spot = out["NSE-NIFTY"]["spot"]
    assert spot["1minute"]["status"] == DIM_NONE and spot["overall"] == DIM_NONE


def test_per_symbol_unmapped_symbol_and_derived_interval_skipped() -> None:
    rows = [
        _spot_row("NSE-NIFTY", "1minute", COMPLETENESS_SETTLED, first_date="2020-01-01"),
        _spot_row("NSE-STRAY", "1minute", COMPLETENESS_SETTLED),  # not in categories → skipped
        _spot_row("NSE-NIFTY", "5minute", COMPLETENESS_DERIVED),  # derived TF → skipped
        _spot_row("NSE-NIFTY", "1day", COMPLETENESS_SETTLED),  # derived 1-day → skipped
    ]
    out = per_symbol_completeness(rows, {"NSE-NIFTY": CATEGORY_INDEX}, [], {})
    assert set(out) == {"NSE-NIFTY"}  # stray excluded; only the 1-minute dimension considered


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
    assert out["NSE-NIFTY"]["spot"]["1minute"]["status"] == DIM_NONE


def test_per_symbol_options_unmapped_underlying_skipped() -> None:
    out = per_symbol_completeness([], {}, [_cov("STRAY", 2025, STATUS_COMPLETE)], {})
    assert out == {}


# ── spot key-drift recovery (DEC-COMPLETENESS-SPOT-KEY-RECOVER-001) ───────────────────────


def test_resolve_spot_category_exact_match_is_byte_identical() -> None:
    """The clean steady state: an exactly-scored symbol resolves on the FIRST lookup,
    returning itself + its category (no normalization, no behaviour change)."""
    cats = {"M&M": CATEGORY_EQUITY}
    assert _resolve_spot_category("M&M", "NSE", cats) == ("M&M", CATEGORY_EQUITY)


def test_resolve_spot_category_recovers_trailing_whitespace_drift() -> None:
    """A legacy partition keyed ``"M&M "`` (write side strips, so this is foreign/legacy) is
    recovered to the canonical scored ``"M&M"`` so its real data is not silently dropped."""
    cats = {"M&M": CATEGORY_EQUITY}
    assert _resolve_spot_category("M&M ", "NSE", cats) == ("M&M", CATEGORY_EQUITY)


def test_resolve_spot_category_recovers_exchange_prefix_drift() -> None:
    """A partition keyed by the groww_symbol ``"NSE-M&M"`` is recovered to the bare scored
    ``"M&M"`` by stripping the row's OWN exchange prefix (the documented #907 drift class)."""
    cats = {"M&M": CATEGORY_EQUITY}
    assert _resolve_spot_category("NSE-M&M", "NSE", cats) == ("M&M", CATEGORY_EQUITY)


def test_resolve_spot_category_does_not_overstrip_hyphenated_symbol() -> None:
    """A legitimately hyphenated symbol (``BAJAJ-AUTO``) matches exactly and is NEVER split on
    its internal hyphen — the prefix strip keys on ``"<exchange>-"``, not any hyphen."""
    cats = {"BAJAJ-AUTO": CATEGORY_EQUITY}
    assert _resolve_spot_category("BAJAJ-AUTO", "NSE", cats) == ("BAJAJ-AUTO", CATEGORY_EQUITY)
    # …and a prefixed hyphenated symbol still recovers correctly (strip only the exch prefix).
    assert _resolve_spot_category("NSE-BAJAJ-AUTO", "NSE", cats) == (
        "BAJAJ-AUTO",
        CATEGORY_EQUITY,
    )


def test_resolve_spot_category_unscored_stray_returns_none() -> None:
    """A genuinely unscored partition (no exact / whitespace / prefix class matches) returns
    None and STAYS dropped — never fabricated onto the board (Golden Rule #1)."""
    cats = {"M&M": CATEGORY_EQUITY}
    assert _resolve_spot_category("FOOBAR", "NSE", cats) is None
    # the prefix is present but the de-prefixed candidate is still not scored → None.
    assert _resolve_spot_category("NSE-FOOBAR", "NSE", cats) is None


def test_resolve_spot_category_blank_exchange_skips_prefix_strip() -> None:
    """An empty exchange means there is no ``"<exch>-"`` prefix to strip — the prefix branch is
    guarded off and an unmatched key returns None (no degenerate ``"-"`` prefix logic)."""
    cats = {"M&M": CATEGORY_EQUITY}
    assert _resolve_spot_category("NSE-M&M", "", cats) is None


def test_resolve_spot_category_whitespace_strip_still_unscored_falls_through() -> None:
    """A whitespace-only drift whose stripped form is STILL unscored falls through to the
    prefix branch and then to None (exercises the stripped-but-unmatched path)."""
    cats = {"M&M": CATEGORY_EQUITY}
    assert _resolve_spot_category("OTHER ", "NSE", cats) is None


def test_per_symbol_spot_groups_recovered_drift_under_canonical_symbol() -> None:
    """End-to-end through the board: a ``NSE-M&M`` partition surfaces under the canonical
    ``M&M`` with its real verdict + the partition's exchange — the read-side FIX of #907."""
    rows = [_spot_row("NSE-M&M", "1minute", COMPLETENESS_SETTLED, first_date="2020-01-01")]
    out = per_symbol_completeness(rows, {"M&M": CATEGORY_EQUITY}, [], {})
    assert set(out) == {"M&M"}  # grouped under the clean scored key, not "NSE-M&M"
    entry = out["M&M"]
    assert entry["spot"]["1minute"]["status"] == DIM_FULL  # the real on-disk data is scored
    assert entry["exchange"] == "NSE"


def test_spot_cell_supersedes_ranks_verdicts_and_breaks_ties_by_start() -> None:
    """The rank-merge contract: a strictly-better verdict always wins; on a verdict TIE the
    earlier start (wider coverage) wins; a ``None`` start ranks worst — so the merged cell is
    order-independent (DEC-COMPLETENESS-SPOT-KEY-RECOVER-001)."""
    # strictly-better verdict replaces — both directions of the rank comparison.
    assert _spot_cell_supersedes(DIM_FULL, "2020-01-01", DIM_NONE, None) is True
    assert _spot_cell_supersedes(DIM_NONE, "2024-01-01", DIM_FULL, "2020-01-01") is False
    # tie on verdict → the earlier start wins (both directions of the ``<`` comparison).
    assert _spot_cell_supersedes(DIM_FULL, "2020-01-01", DIM_FULL, "2021-01-01") is True
    assert _spot_cell_supersedes(DIM_FULL, "2021-01-01", DIM_FULL, "2020-01-01") is False
    # tie on verdict with None starts: a None new never supersedes; a None current is superseded.
    assert _spot_cell_supersedes(DIM_PARTIAL, None, DIM_PARTIAL, "2020-01-01") is False
    assert _spot_cell_supersedes(DIM_PARTIAL, "2020-01-01", DIM_PARTIAL, None) is True


def test_spot_key_recovery_collision_keeps_clean_full_over_drifted_fragment() -> None:
    """The hostile-review BLOCKER regression: a clean FULL ``M&M`` partition coexisting with a
    drifted ``NSE-M&M`` fragment (no ledger → ``none``) — both recover to canonical ``M&M`` —
    keeps the real FULL data and NEVER lets the stale fragment clobber it, in BOTH scan orders
    AND across BOTH read paths (scoreboard + universe strip)."""
    clean = _spot_row("M&M", "1minute", COMPLETENESS_SETTLED, first_date="2020-01-01")
    drifted = _spot_row("NSE-M&M", "1minute", COMPLETENESS_NO_RECORD, first_date="2024-03-01")
    cats = {"M&M": CATEGORY_EQUITY}
    for rows in ([clean, drifted], [drifted, clean]):  # order-INDEPENDENT
        board = build_completeness_scoreboard(rows, cats, [], {})
        per = {r["symbol"]: r for r in board["spot"]["per_symbol"]}
        assert set(per) == {"M&M"}  # one canonical row, the drifted key never shown
        assert per["M&M"]["1minute"] == DIM_FULL  # real FULL kept, not clobbered by `none`
        assert per["M&M"]["1minute_start"] == "2020-01-01"  # the real 2020-floor start kept
        # the /universe per-row strip holds the SAME invariant (the second wired call site).
        out = per_symbol_completeness(rows, cats, [], {})
        assert out["M&M"]["spot"]["1minute"]["status"] == DIM_FULL
        assert out["M&M"]["spot"]["1minute"]["first_date"] == "2020-01-01"


def test_per_symbol_spot_and_options_merge_on_same_symbol() -> None:
    """A symbol with BOTH spot rows and option records merges into one entry (no duplication)."""
    rows = [_spot_row("NSE-NIFTY", "1minute", COMPLETENESS_SETTLED, first_date="2020-01-01")]
    # DEFENSIVE / forward-compat: (pending, n>0) is unreachable in production (pending ⟺ 0
    # expiries; hostile-review #859) — synthetic input exercises the retained `growing` fold.
    records = [_cov("NSE-NIFTY", 2025, STATUS_PENDING, expiries=10)]
    cats = {"NSE-NIFTY": CATEGORY_INDEX}
    out = per_symbol_completeness(rows, cats, records, cats)
    [entry] = out.values()
    assert entry["spot"]["1minute"]["status"] == DIM_FULL
    assert entry["options"]["status"] == OPT_GROWING  # a pending year WITH expiries → growing


# ── CSV report export (DEC-COMPLETENESS-PAGE-001) ─────────────────────────────────────────


def test_scoreboard_csv_full_board_has_all_sections_and_rows() -> None:
    """A populated board renders the four labelled sections + summary + per-row detail."""
    spot_rows = [
        _spot_row("NSE-NIFTY", "1day", COMPLETENESS_SETTLED),  # derived → skipped
        # settled FROM the 2020 floor → genuinely full (DEC-SPOT-COMPLETENESS-2020-FLOOR-001).
        _spot_row("NSE-NIFTY", "1minute", COMPLETENESS_SETTLED, first_date="2020-01-01"),
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
    # The 1-day columns are GONE from the spot CSV (view-only removal).
    assert "Full 1-day" not in csv_text and "1-day complete?" not in csv_text
    # The spot summary carries the exact per-category FULL 1-minute count (index: full).
    assert f"{CATEGORY_INDEX},1,1" in lines
    # equity: 1-minute pending → full_1minute=0.
    assert f"{CATEGORY_EQUITY},1,0" in lines
    # The per-symbol detail rows are present with their 1-minute + overall verdicts —
    # plus the informational open-substituted-days column (0 for these hand-built rows;
    # DEC-DAILY-OPEN-SUBSTITUTION-HONESTY-001).
    assert f"{CATEGORY_INDEX},NSE-NIFTY,NSE,{DIM_FULL},0,{DIM_FULL}" in lines
    assert f"{CATEGORY_EQUITY},NSE-RELIANCE,NSE,{DIM_PARTIAL},0,{DIM_PARTIAL}" in lines
    # The per-underlying option detail row carries status + summed counts + the years +
    # the (empty, for a stable/has-contracts row) by-design Notes column.
    assert f"{CATEGORY_INDEX},NSE-NIFTY,NSE,{OPT_STABLE},50,10000,2024," in lines


def test_scoreboard_csv_empty_board_keeps_section_skeleton() -> None:
    """An empty board still renders every section header + a zeroed per-category summary."""
    csv_text = scoreboard_to_csv(build_completeness_scoreboard([], {}, [], {}))
    lines = csv_text.splitlines()
    assert any(line.startswith("DETAIL — spot") for line in lines)
    assert any(line.startswith("DETAIL — options") for line in lines)
    # Each category still gets a zeroed summary row (the loops over CATEGORIES always run):
    # spot is now ``cat,0,0`` (total + full_1minute); options stays ``cat,0,0,0,0``.
    for cat in (CATEGORY_INDEX, CATEGORY_EQUITY, CATEGORY_REFERENCE):
        assert f"{cat},0,0" in lines  # the zeroed spot summary row (3 columns)
        assert f"{cat},0,0,0,0" in lines  # the zeroed options summary row (5 columns)


def test_scoreboard_csv_escapes_comma_in_symbol() -> None:
    """A symbol carrying a comma is RFC-4180 quoted, never splitting the row (extreme input)."""
    rows = [_spot_row("NSE-A,B", "1minute", COMPLETENESS_SETTLED)]
    board = build_completeness_scoreboard(rows, {"NSE-A,B": CATEGORY_EQUITY}, [], {})
    csv_text = scoreboard_to_csv(board)
    assert '"NSE-A,B"' in csv_text  # the comma-bearing symbol is quoted, not field-split

    # And it round-trips through a strict CSV reader back to the original symbol.
    import csv as _csv
    from io import StringIO

    detail = [
        r
        for r in _csv.reader(StringIO(csv_text))
        # the per-symbol detail row (now 6 columns — the open-substituted column was
        # added by DEC-DAILY-OPEN-SUBSTITUTION-HONESTY-001)
        if len(r) == 6 and r[1] == "NSE-A,B"
    ]
    assert detail and detail[0][1] == "NSE-A,B"


def test_scoreboard_csv_prefixes_formula_injection_symbol() -> None:
    """A symbol whose text starts with '=' is quoted (') in the rendered CSV so a spreadsheet
    app renders it as literal text, never executes it as a formula (DEC-CSV-FORMULA-INJECTION-001)."""
    evil = "=HYPERLINK(0)"  # no comma → a single cell; starts with a formula lead-in
    rows = [_spot_row(evil, "1minute", COMPLETENESS_SETTLED)]
    board = build_completeness_scoreboard(rows, {evil: CATEGORY_EQUITY}, [], {})
    csv_text = scoreboard_to_csv(board)
    assert "'=HYPERLINK(0)" in csv_text  # neutralised — prefixed, never bare "=HYPERLINK(0)"

    import csv as _csv
    from io import StringIO

    detail = [
        r for r in _csv.reader(StringIO(csv_text)) if len(r) == 6 and r[1].endswith("HYPERLINK(0)")
    ]
    assert detail and detail[0][1] == "'=HYPERLINK(0)"  # the safe (quoted) value round-trips


def test_scoreboard_csv_with_per_symbol_enriches_spot_detail_with_dates() -> None:
    """DEC-COMPLETENESS-CSV-READABLE-001: a per_symbol map enriches the spot detail with the
    explicit first→last bar dates + from_2020 flag for the 1-minute dimension (the SAME rich
    info the /universe strip shows), so the CSV is never less informative than the page."""
    import csv as _csv
    from io import StringIO

    spot_rows = [
        _spot_row(
            "NSE-NIFTY",
            "1minute",
            COMPLETENESS_SETTLED,
            first_date="2020-01-01",
            last_date="2026-06-18",
        ),
        # FINNIFTY: 1-minute NEVER pulled (no-record → empty date cells + from_2020 = no).
        _spot_row("NSE-FINNIFTY", "1minute", COMPLETENESS_NO_RECORD),
    ]
    cats = {"NSE-NIFTY": CATEGORY_INDEX, "NSE-FINNIFTY": CATEGORY_INDEX}
    board = build_completeness_scoreboard(spot_rows, cats, [], {})
    psmap = per_symbol_completeness(spot_rows, cats, [], {})

    csv_text = scoreboard_to_csv(board, per_symbol=psmap)
    lines = csv_text.splitlines()
    # The enriched section title + the new date / from-2020 column headers are present.
    assert any(line.startswith("DETAIL — spot (per symbol with first") for line in lines)
    assert "1-minute first bar" in csv_text and "1-minute from 2020?" in csv_text
    # The dropped 1-day headers are gone.
    assert "1-day first bar" not in csv_text and "1-day from 2020?" not in csv_text

    # Enriched spot detail rows now have 10 columns: cat, symbol, exch, 1m verdict, 1m first,
    # 1m last, 1m from-2020, 1m open-substituted days, 1m sessions, overall
    # (the two substitution columns added by DEC-DAILY-OPEN-SUBSTITUTION-HONESTY-001).
    assert "1-minute open-substituted days" in csv_text and "1-minute sessions" in csv_text
    rows = [r for r in _csv.reader(StringIO(csv_text)) if len(r) == 10]
    by_symbol = {r[1]: r for r in rows}
    # NIFTY: dated 2020→2026, from_2020 = yes (covers _csv_date truthy + _csv_yes_no True).
    nifty = by_symbol["NSE-NIFTY"]
    assert nifty[4] == "2020-01-01" and nifty[5] == "2026-06-18" and nifty[6] == "yes"
    assert nifty[7] == "0" and nifty[8] == "0"  # hand-built rows carry no counts → honest 0/0
    assert nifty[9] == DIM_FULL  # overall = 1-minute verdict
    # FINNIFTY: 1-minute no-record → EMPTY date cells + no (covers _csv_date falsy +
    # _csv_yes_no False).
    finnifty = by_symbol["NSE-FINNIFTY"]
    assert finnifty[4] == "" and finnifty[5] == "" and finnifty[6] == "no"


# ---------------------------------------------------------------------------
# DEC-DAILY-OPEN-SUBSTITUTION-HONESTY-001 — the per-day open-substitution count:
# how many stored 1-minute sessions have their FIRST present minute strictly after
# the 09:15 IST canonical open, so the DERIVED 1-day open (tf_resampler `.first()`)
# is a substituted first-present-minute price. INFORMATIONAL only — verdict stays
# ``full``; the count merely travels through every completeness projection.
# ---------------------------------------------------------------------------


def _sub_row(
    symbol: str,
    completeness: str,
    *,
    first_date: str | None = "2020-01-01",
    open_substituted_days: int = 0,
    session_days: int = 0,
) -> dict[str, Any]:
    """A ``1minute`` spot row carrying the summarize-side substitution counts."""
    row = _spot_row(symbol, "1minute", completeness, first_date=first_date)
    row["open_substituted_days"] = open_substituted_days
    row["session_days"] = session_days
    return row


def test_per_symbol_spot_row_carries_open_substitution_counts_regression() -> None:
    # FAILS-BEFORE / PASSES-AFTER: pre-fix the scoreboard row had NO substitution keys
    # (KeyError below), so a missing-09:15 session's substituted derived daily open was
    # invisible on the completeness surface.
    rows = [_sub_row("NSE-NIFTY", COMPLETENESS_SETTLED, open_substituted_days=3, session_days=100)]
    board = build_completeness_scoreboard(rows, {"NSE-NIFTY": CATEGORY_INDEX}, [], {})
    [row] = board["spot"]["per_symbol"]
    assert row["open_substituted_days"] == 3
    assert row["session_days"] == 100


def test_open_substitution_is_informational_verdict_stays_full() -> None:
    # The design's explicit invariant: a substitution NEVER downgrades the verdict —
    # a settled, Jan-2020-floor symbol with substituted opens still reads ``full``
    # (the data exists; only its derived daily open is a substitute).
    rows = [_sub_row("NSE-NIFTY", COMPLETENESS_SETTLED, open_substituted_days=5, session_days=50)]
    board = build_completeness_scoreboard(rows, {"NSE-NIFTY": CATEGORY_INDEX}, [], {})
    [row] = board["spot"]["per_symbol"]
    assert row["1minute"] == DIM_FULL and row["overall"] == DIM_FULL
    assert row["open_substituted_days"] == 5


def test_open_substitution_counts_travel_with_rank_merge_winner() -> None:
    # REGRESSION (the design's rank-merge trap): when a clean partition and a stale
    # drifted fragment both resolve to one canonical symbol, the substitution count
    # must follow the rank-merge WINNING cell — a later-scanned WORSE fragment must
    # NOT clobber the winner's count. Order-independent (both scan orders).
    winner = _sub_row("NSE-NIFTY", COMPLETENESS_SETTLED, open_substituted_days=2, session_days=50)
    loser = _sub_row(
        "NSE-NIFTY",
        COMPLETENESS_NO_RECORD,
        first_date=None,
        open_substituted_days=9,
        session_days=9,
    )
    for ordering in ([winner, loser], [loser, winner]):
        board = build_completeness_scoreboard(ordering, {"NSE-NIFTY": CATEGORY_INDEX}, [], {})
        [row] = board["spot"]["per_symbol"]
        assert row["1minute"] == DIM_FULL
        assert row["open_substituted_days"] == 2  # the WINNING cell's count, both orders
        assert row["session_days"] == 50


def test_per_symbol_spot_defaults_zero_counts_for_legacy_rows() -> None:
    # A hand-built / legacy row WITHOUT the summarize-side keys defaults to the honest
    # 0/0 — never a KeyError, never a fabricated count.
    rows = [_spot_row("NSE-NIFTY", "1minute", COMPLETENESS_SETTLED, first_date="2020-01-01")]
    board = build_completeness_scoreboard(rows, {"NSE-NIFTY": CATEGORY_INDEX}, [], {})
    [row] = board["spot"]["per_symbol"]
    assert row["open_substituted_days"] == 0
    assert row["session_days"] == 0


def test_aggregate_spot_sums_open_substituted_days_per_category() -> None:
    # FAILS-BEFORE (key absent): the by_category bucket sums the per-symbol counts —
    # the informational category-level tally, never a verdict input.
    rows = [
        _sub_row("NSE-A", COMPLETENESS_SETTLED, open_substituted_days=2, session_days=10),
        _sub_row("NSE-B", COMPLETENESS_SETTLED, open_substituted_days=3, session_days=10),
        _sub_row("NSE-NIFTY", COMPLETENESS_SETTLED, open_substituted_days=1, session_days=10),
    ]
    cats = {"NSE-A": CATEGORY_EQUITY, "NSE-B": CATEGORY_EQUITY, "NSE-NIFTY": CATEGORY_INDEX}
    board = build_completeness_scoreboard(rows, cats, [], {})
    assert board["spot"]["by_category"][CATEGORY_EQUITY]["open_substituted_days"] == 5
    assert board["spot"]["by_category"][CATEGORY_INDEX]["open_substituted_days"] == 1


def test_per_symbol_detail_carries_open_substitution_fields() -> None:
    # FAILS-BEFORE (keys absent): the /universe per-symbol detail carries the counts
    # + the ``open_substituted`` flag (days > 0) for the 1-minute dimension.
    rows = [_sub_row("NSE-NIFTY", COMPLETENESS_SETTLED, open_substituted_days=4, session_days=80)]
    detail = per_symbol_completeness(rows, {"NSE-NIFTY": CATEGORY_INDEX}, [], {})
    one_min = detail["NSE-NIFTY"]["spot"]["1minute"]
    assert one_min["open_substituted_days"] == 4
    assert one_min["session_days"] == 80
    assert one_min["open_substituted"] is True
    # …and the verdict is NOT downgraded by the substitution (informational only).
    assert one_min["status"] == DIM_FULL


def test_per_symbol_detail_zero_counts_flag_false() -> None:
    # The flag is ``days > 0`` — a clean symbol reads 0/M with ``open_substituted`` False.
    rows = [_sub_row("NSE-NIFTY", COMPLETENESS_SETTLED, open_substituted_days=0, session_days=80)]
    detail = per_symbol_completeness(rows, {"NSE-NIFTY": CATEGORY_INDEX}, [], {})
    one_min = detail["NSE-NIFTY"]["spot"]["1minute"]
    assert one_min["open_substituted_days"] == 0
    assert one_min["session_days"] == 80
    assert one_min["open_substituted"] is False


def test_new_per_symbol_entry_seeds_open_substitution_defaults() -> None:
    # An options-only symbol (no spot row at all) gets the honest no-data defaults —
    # 0/0/False — from the ``_new_per_symbol_entry`` seed.
    records = [_cov("RELIANCE", 2024, STATUS_COMPLETE, expiries=4, contracts=100)]
    detail = per_symbol_completeness([], {}, records, {"RELIANCE": CATEGORY_EQUITY})
    one_min = detail["RELIANCE"]["spot"]["1minute"]
    assert one_min["open_substituted_days"] == 0
    assert one_min["session_days"] == 0
    assert one_min["open_substituted"] is False


def test_per_symbol_detail_rank_merge_keeps_winner_counts() -> None:
    # The /universe detail applies the SAME rank-merge: a stale drifted fragment's
    # counts never clobber the winning cell's (order-independent).
    winner = _sub_row("NSE-NIFTY", COMPLETENESS_SETTLED, open_substituted_days=1, session_days=40)
    loser = _sub_row(
        "NSE-NIFTY",
        COMPLETENESS_FAILED,
        first_date=None,
        open_substituted_days=7,
        session_days=7,
    )
    for ordering in ([winner, loser], [loser, winner]):
        detail = per_symbol_completeness(ordering, {"NSE-NIFTY": CATEGORY_INDEX}, [], {})
        one_min = detail["NSE-NIFTY"]["spot"]["1minute"]
        assert one_min["status"] == DIM_FULL
        assert one_min["open_substituted_days"] == 1
        assert one_min["session_days"] == 40


def test_scoreboard_csv_compact_detail_carries_open_substituted_column() -> None:
    # FAILS-BEFORE (header absent): the compact (per_symbol=None) DETAIL-spot section
    # carries the informational count column from the scoreboard row itself.
    rows = [_sub_row("NSE-NIFTY", COMPLETENESS_SETTLED, open_substituted_days=3, session_days=60)]
    board = build_completeness_scoreboard(rows, {"NSE-NIFTY": CATEGORY_INDEX}, [], {})
    csv_text = scoreboard_to_csv(board)
    assert "1-minute open-substituted days" in csv_text
    assert f"{CATEGORY_INDEX},NSE-NIFTY,NSE,{DIM_FULL},3,{DIM_FULL}" in csv_text.splitlines()


def test_scoreboard_csv_enriched_carries_real_substitution_counts() -> None:
    # FAILS-BEFORE (headers absent): the enriched DETAIL-spot section carries BOTH the
    # "N substituted" and "M sessions" columns from the per-symbol detail map.
    import csv as _csv
    from io import StringIO

    rows = [_sub_row("NSE-NIFTY", COMPLETENESS_SETTLED, open_substituted_days=3, session_days=60)]
    cats = {"NSE-NIFTY": CATEGORY_INDEX}
    board = build_completeness_scoreboard(rows, cats, [], {})
    psmap = per_symbol_completeness(rows, cats, [], {})
    csv_text = scoreboard_to_csv(board, per_symbol=psmap)
    assert "1-minute open-substituted days" in csv_text and "1-minute sessions" in csv_text
    detail = {r[1]: r for r in _csv.reader(StringIO(csv_text)) if len(r) == 10}
    nifty = detail["NSE-NIFTY"]
    assert nifty[7] == "3" and nifty[8] == "60"  # N substituted / M sessions
    assert nifty[9] == DIM_FULL  # verdict stays full — informational only


# ---------------------------------------------------------------------------
# DEC-COMPLETENESS-RECONCILE-DIAG-001 — the OBSERVE-ONLY lake↔scored reconciliation
# diagnostic that PINPOINTS why a symbol like ``M&M`` reads ``none`` despite having lake
# data (a stored lake/ledger key that diverges from the scored ``symbol_name`` it is looked
# up by). It logs ONE structured WARNING and changes NO scoreboard output.
# ---------------------------------------------------------------------------


def test_reconcile_clean_when_keys_match() -> None:
    """No divergence → an empty report with ``has_mismatch()`` False (the healthy state)."""
    report = reconcile_symbol_coverage(
        lake_symbols=["M&M", "NSE-NIFTY"], scored_symbols=["M&M", "NSE-NIFTY"]
    )
    assert report == SymbolReconcileReport(
        stray_lake_symbols=(),
        stray_lake_count=0,
        unmatched_scored_symbols=(),
        unmatched_scored_count=0,
    )
    assert report.has_mismatch() is False


def test_reconcile_surfaces_both_sides_of_a_trailing_space_divergence() -> None:
    """The CONFIRMED ``M&M`` failure: the lake key carries a trailing space (``'M&M '``) so the
    scored ``'M&M'`` matches no lake row (reads ``none``) AND the lake key is unscored — BOTH
    sides are surfaced, with duplicates collapsed and the sample SORTED."""
    report = reconcile_symbol_coverage(
        # A divergent ``"M&M "`` (trailing space) + a clean match + a duplicate stray.
        lake_symbols=["M&M ", "M&M ", "NSE-NIFTY"],
        scored_symbols=["M&M", "NSE-NIFTY"],
    )
    assert report.stray_lake_symbols == ("M&M ",)  # NSE-NIFTY matched → not stray
    assert report.stray_lake_count == 1
    assert report.unmatched_scored_symbols == ("M&M",)  # the scored M&M has no lake key
    assert report.unmatched_scored_count == 1
    assert report.has_mismatch() is True


def test_reconcile_caps_samples_but_keeps_full_counts() -> None:
    """A flood of stray / unmatched keys truncates the SAMPLE lists to the cap while the COUNTS
    stay the full totals (so one badly-diverged master can never bloat the log line)."""
    # 12 stray lake keys (none scored) and 15 scored keys (none in the lake) — both exceed the
    # 10-entry sample cap, proving the counts are full but the samples truncated + sorted.
    lake = [f"L{i:02d}" for i in range(12)]
    scored = [f"S{i:02d}" for i in range(15)]
    report = reconcile_symbol_coverage(lake_symbols=lake, scored_symbols=scored)
    assert report.stray_lake_count == 12
    assert report.unmatched_scored_count == 15
    assert len(report.stray_lake_symbols) == 10
    assert len(report.unmatched_scored_symbols) == 10
    # Sorted + capped: the first ten of each sorted set.
    assert report.stray_lake_symbols == tuple(sorted(lake)[:10])
    assert report.unmatched_scored_symbols == tuple(sorted(scored)[:10])


def test_log_reconcile_from_board_fires_warning_naming_both_repr_strings() -> None:
    """The build-path wiring: a scored ``M&M`` + a lake row keyed on the DIVERGENT ``'M&M '``
    (trailing space) → ``M&M`` reads ``none`` on the board AND the diagnostic logs ONE WARNING
    naming BOTH ``repr()`` strings, so the invisible trailing space is visible. This is the
    regression that does NOT log on origin/main (no diagnostic) and DOES with this change."""
    spot_rows = [
        # The lake partition's key — a TRAILING SPACE the scored map lacks.
        _spot_row("M&M ", "1minute", COMPLETENESS_SETTLED, first_date="2020-01-01"),
        _spot_row("NSE-NIFTY", "1minute", COMPLETENESS_SETTLED, first_date="2020-01-01"),
    ]
    # The scored universe keys on the CLEAN ``M&M`` (the engine ``symbol_name``).
    cats = {"M&M": CATEGORY_EQUITY, "NSE-NIFTY": CATEGORY_INDEX}

    with capture_logs() as logs:
        report = log_symbol_reconcile_from_board(spot_rows, cats)

    assert report.has_mismatch() is True
    warnings = [e for e in logs if e.get("event") == RECONCILE_MISMATCH_EVENT]
    assert len(warnings) == 1
    entry = warnings[0]
    assert entry["log_level"] == "warning"
    assert entry["stray_lake_count"] == 1
    assert entry["unmatched_scored_count"] == 1
    # repr() makes the trailing space VISIBLE on BOTH sides — the exact divergence.
    assert entry["stray_lake_symbols"] == [repr("M&M ")]
    assert entry["unmatched_scored_symbols"] == [repr("M&M")]
    # repr() literally renders the trailing-space form (the pinpointed key drift).
    assert "'M&M '" in entry["stray_lake_symbols"][0]
    assert entry["unmatched_scored_symbols"][0] == "'M&M'"


def test_log_reconcile_from_board_silent_and_scoreboard_unchanged_when_clean() -> None:
    """The clean case: a CONSISTENT ``M&M`` on both the lake row AND the scored map reads
    normally and fires NO warning — AND the scoreboard payload is byte-identical with vs
    without the diagnostic (observability ONLY, no row added / removed / altered)."""
    spot_rows = [
        _spot_row("M&M", "1minute", COMPLETENESS_SETTLED, first_date="2020-01-01"),
        _spot_row("NSE-NIFTY", "1minute", COMPLETENESS_SETTLED, first_date="2020-01-01"),
        # A DERIVED intraday TF row for the SAME symbol — NOT a distinct lake symbol, so it
        # must NOT be treated as a stray key (covers the SPOT_DIMENSIONS interval filter).
        _spot_row("M&M", "5minute", COMPLETENESS_SETTLED, first_date="2020-01-01"),
    ]
    cats = {"M&M": CATEGORY_EQUITY, "NSE-NIFTY": CATEGORY_INDEX}

    # Build the board BEFORE running the diagnostic over the SAME mutable inputs ...
    board_before = build_completeness_scoreboard(spot_rows, cats, [], {})

    # The diagnostic stays SILENT — every scored symbol is backed by a lake key and vice versa.
    with capture_logs() as logs:
        report = log_symbol_reconcile_from_board(spot_rows, cats)
    assert report.has_mismatch() is False
    assert [e for e in logs if e.get("event") == RECONCILE_MISMATCH_EVENT] == []

    # ... then build AGAIN from those SAME inputs: byte-identical proves the diagnostic
    # mutated NEITHER ``spot_rows`` NOR ``cats`` (true non-interference, not just determinism).
    board_after = build_completeness_scoreboard(spot_rows, cats, [], {})
    assert board_after == board_before
    # The scoreboard scores ``M&M`` normally (DIM_FULL, not ``none``) — the consistent case.
    per_symbol = {row["symbol"]: row for row in board_after["spot"]["per_symbol"]}
    assert per_symbol["M&M"]["1minute"] == DIM_FULL


def test_log_reconcile_does_not_log_when_a_derived_row_is_the_only_extra() -> None:
    """A derived intraday-TF row for a scored symbol is filtered out by SPOT_DIMENSIONS, so it
    is NOT counted as a stray lake key — the diagnostic stays silent (covers the interval-skip
    branch of the lake-symbol comprehension)."""
    spot_rows = [
        _spot_row("NSE-NIFTY", "1minute", COMPLETENESS_SETTLED, first_date="2020-01-01"),
        # Only a derived 1-day row exists for FINNIFTY — NOT the fetched 1-minute dimension,
        # so FINNIFTY is neither a stray lake key (filtered) nor scored.
        _spot_row("NSE-FINNIFTY", "1day", COMPLETENESS_SETTLED, first_date="2020-01-01"),
    ]
    cats = {"NSE-NIFTY": CATEGORY_INDEX}
    with capture_logs() as logs:
        report = log_symbol_reconcile_from_board(spot_rows, cats)
    assert report.has_mismatch() is False
    assert [e for e in logs if e.get("event") == RECONCILE_MISMATCH_EVENT] == []


def test_log_reconcile_excludes_fno_contract_partitions() -> None:
    """An F&O contract partition is ALSO stored at ``1minute`` but is NEVER a scored spot
    underlying — the lake-symbol set is filtered to spot segments (``SPOT_PROBE_SEGMENTS`` =
    INDEX/CASH), so a ``segment="FNO"`` 1-minute partition is NOT counted as a stray. Without
    this filter every backfilled F&O contract would fire the WARNING on EVERY load and drown
    the real signal; the diagnostic must stay SILENT here (covers the segment-filter False
    branch)."""
    spot_rows = [
        _spot_row(
            "NSE-NIFTY", "1minute", COMPLETENESS_SETTLED, segment="INDEX", first_date="2020-01-01"
        ),
        # An F&O contract partition at 1minute — present on disk, NOT a scored underlying.
        _spot_row(
            "NSE-NIFTY25JAN24000CE",
            "1minute",
            COMPLETENESS_SETTLED,
            segment="FNO",
            first_date="2025-01-01",
        ),
    ]
    cats = {"NSE-NIFTY": CATEGORY_INDEX}
    with capture_logs() as logs:
        report = log_symbol_reconcile_from_board(spot_rows, cats)
    # The FNO partition is excluded by the spot-segment filter → no stray, no mismatch, silent.
    assert report.stray_lake_count == 0
    assert report.has_mismatch() is False
    assert [e for e in logs if e.get("event") == RECONCILE_MISMATCH_EVENT] == []


def test_log_reconcile_from_board_is_guarded_against_a_malformed_row() -> None:
    """OBSERVE-ONLY must NEVER break the page: a malformed row (missing ``symbol``) raises
    inside the comprehension; the body is wrapped so the error is caught, logged ONCE as
    ``completeness.symbol_reconcile_error``, and an empty report returned — no raise (covers the
    defensive except branch)."""
    spot_rows = [{"interval": "1minute", "segment": "CASH"}]  # no "symbol" → KeyError
    with capture_logs() as logs:
        report = log_symbol_reconcile_from_board(spot_rows, {"NSE-NIFTY": CATEGORY_INDEX})
    assert report.has_mismatch() is False
    assert report.stray_lake_count == 0
    assert report.unmatched_scored_count == 0
    errors = [e for e in logs if e.get("event") == "completeness.symbol_reconcile_error"]
    assert len(errors) == 1
    assert errors[0]["log_level"] == "warning"


# ---------------------------------------------------------------------------
# DEC-TELEMETRY-RECONCILE-LEDGERSAVE-001 — severity split + fingerprint gate.
# ---------------------------------------------------------------------------


def test_partial_lake_shape_logs_counts_only_info_never_the_warning() -> None:
    """The 953x/night STORM shape: ZERO stray lake keys (every partition scored) while some
    scored symbols simply have NO lake data yet (a deliberately-partial lake mid-backfill).
    That is NOT a key drift — it must log the counts-only ``completeness.symbols_not_yet_pulled``
    INFO, never the ``RECONCILE_MISMATCH_EVENT`` WARNING (fails-before: pre-change this shape
    WARNed on every scoreboard build)."""
    spot_rows = [
        _spot_row("NSE-NIFTY", "1minute", COMPLETENESS_SETTLED, first_date="2020-01-01"),
    ]
    cats = {"NSE-NIFTY": CATEGORY_INDEX, "RELIANCE": CATEGORY_EQUITY, "TCS": CATEGORY_EQUITY}

    with capture_logs() as logs:
        report = log_symbol_reconcile_from_board(spot_rows, cats)

    # The report itself is UNCHANGED (parity: same return values as the pure reconcile).
    assert report == reconcile_symbol_coverage(
        lake_symbols=["NSE-NIFTY"], scored_symbols=cats.keys()
    )
    assert report.stray_lake_count == 0
    assert report.unmatched_scored_count == 2
    # No WARNING — the storm shape is downgraded.
    assert [e for e in logs if e.get("event") == RECONCILE_MISMATCH_EVENT] == []
    infos = [e for e in logs if e.get("event") == SYMBOLS_NOT_YET_PULLED_EVENT]
    assert len(infos) == 1
    entry = infos[0]
    assert entry["log_level"] == "info"
    assert entry["unmatched_scored_count"] == 2
    assert entry["suppressed_repeats"] == 0
    # Counts-only: NO symbol sample lists on the INFO shape.
    assert "stray_lake_symbols" not in entry
    assert "unmatched_scored_symbols" not in entry


def test_partial_lake_shape_leaves_scoreboard_payload_byte_identical() -> None:
    """Parity pin (Session-B contract): the INFO downgrade is logging-ONLY — the scoreboard
    built from the SAME inputs before vs after the diagnostic runs is byte-identical (no row
    added / removed / altered, no input mutated)."""
    spot_rows = [
        _spot_row("NSE-NIFTY", "1minute", COMPLETENESS_SETTLED, first_date="2020-01-01"),
    ]
    cats = {"NSE-NIFTY": CATEGORY_INDEX, "RELIANCE": CATEGORY_EQUITY}
    board_before = build_completeness_scoreboard(spot_rows, cats, [], {})
    log_symbol_reconcile_from_board(spot_rows, cats, gate=ReconcileLogGate())
    board_after = build_completeness_scoreboard(spot_rows, cats, [], {})
    assert board_after == board_before


def test_warning_shape_carries_suppressed_repeats_zero_when_ungated() -> None:
    """The stray>0 WARNING keeps its full payload; ungated (``gate=None``) every call logs —
    the pre-gate control flow — and ``suppressed_repeats`` reads an honest 0."""
    spot_rows = [
        _spot_row("M&M ", "1minute", COMPLETENESS_SETTLED, first_date="2020-01-01"),
    ]
    cats = {"M&M": CATEGORY_EQUITY}
    with capture_logs() as logs:
        log_symbol_reconcile_from_board(spot_rows, cats)
        log_symbol_reconcile_from_board(spot_rows, cats)
    warnings = [e for e in logs if e.get("event") == RECONCILE_MISMATCH_EVENT]
    # Ungated = byte-identical legacy cadence: BOTH calls log.
    assert len(warnings) == 2
    assert all(e["suppressed_repeats"] == 0 for e in warnings)
    assert warnings[0]["stray_lake_symbols"] == [repr("M&M ")]


class TestReconcileLogGate:
    """The thread-safe fingerprint gate primitive."""

    def test_first_observation_logs_with_zero_carried(self) -> None:
        gate = ReconcileLogGate()
        assert gate.observe(("warn", 1)) == (True, 0)

    def test_byte_identical_repeats_are_suppressed(self) -> None:
        gate = ReconcileLogGate()
        assert gate.observe("fp-a") == (True, 0)
        assert gate.observe("fp-a") == (False, 0)
        assert gate.observe("fp-a") == (False, 0)

    def test_fingerprint_change_logs_and_carries_suppressed_count(self) -> None:
        gate = ReconcileLogGate()
        gate.observe("fp-a")
        for _ in range(3):
            gate.observe("fp-a")  # 3 suppressed repeats
        assert gate.observe("fp-b") == (True, 3)
        # The tally reset with the new fingerprint.
        assert gate.observe("fp-c") == (True, 0)

    def test_flush_returns_suppressed_count_and_rearms(self) -> None:
        gate = ReconcileLogGate()
        gate.observe("fp-a")
        gate.observe("fp-a")
        gate.observe("fp-a")
        assert gate.flush() == 2
        # After a flush the SAME fingerprint logs again (cleared), carrying 0 (consumed).
        assert gate.observe("fp-a") == (True, 0)

    def test_flush_on_a_fresh_gate_is_zero(self) -> None:
        assert ReconcileLogGate().flush() == 0

    def test_concurrent_observations_of_one_fingerprint_log_exactly_once(self) -> None:
        """Lock contention: N threads racing the SAME fingerprint through one gate — EXACTLY
        one wins ``should_log``; the rest are suppressed. The follow-up fingerprint change
        then carries exactly N-1 suppressed repeats (no lost/double counts under the lock)."""
        gate = ReconcileLogGate()
        n = 16
        barrier = threading.Barrier(n)
        verdicts: list[tuple[bool, int]] = []
        verdicts_lock = threading.Lock()

        def _observe() -> None:
            barrier.wait()
            verdict = gate.observe("fp-race")
            with verdicts_lock:
                verdicts.append(verdict)

        threads = [threading.Thread(target=_observe) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sum(1 for should_log, _ in verdicts if should_log) == 1
        assert gate.observe("fp-next") == (True, n - 1)


def _stray_rows() -> list[dict[str, Any]]:
    """A fresh stray-drift fixture (the WARNING shape) per call — never shared-mutable."""
    return [_spot_row("M&M ", "1minute", COMPLETENESS_SETTLED, first_date="2020-01-01")]


def _stray_cats() -> dict[str, str]:
    return {"M&M": CATEGORY_EQUITY}


class TestReconcileGateWiring:
    """The gate seam through ``log_symbol_reconcile_from_board`` (the production wiring)."""

    def test_stable_state_logs_once_not_per_poll(self) -> None:
        """The headline fix: 953 identical scoreboard builds against ONE gate log ONE line —
        and every call still RETURNS the identical report (suppression is logging-only)."""
        gate = ReconcileLogGate()
        reports = []
        with capture_logs() as logs:
            for _ in range(5):
                reports.append(
                    log_symbol_reconcile_from_board(_stray_rows(), _stray_cats(), gate=gate)
                )
        warnings = [e for e in logs if e.get("event") == RECONCILE_MISMATCH_EVENT]
        assert len(warnings) == 1
        assert warnings[0]["suppressed_repeats"] == 0
        assert len(set(reports)) == 1  # every call returned the SAME frozen report

    def test_fingerprint_change_relogs_and_names_suppressed_repeats(self) -> None:
        """A real STATE change (a new stray key) re-logs immediately, carrying the count of
        identical lines the gate swallowed since the last change."""
        gate = ReconcileLogGate()
        changed_rows = [*_stray_rows()]
        changed_rows.append(
            {
                "symbol": "NSE-TCS",
                "exchange": "NSE",
                "segment": "CASH",
                "interval": "1minute",
                "completeness": COMPLETENESS_SETTLED,
                "first_date": "2020-01-01",
                "last_date": None,
            }
        )
        with capture_logs() as logs:
            for _ in range(4):
                log_symbol_reconcile_from_board(_stray_rows(), _stray_cats(), gate=gate)
            log_symbol_reconcile_from_board(changed_rows, _stray_cats(), gate=gate)
        warnings = [e for e in logs if e.get("event") == RECONCILE_MISMATCH_EVENT]
        assert len(warnings) == 2
        assert warnings[0]["suppressed_repeats"] == 0
        assert warnings[1]["suppressed_repeats"] == 3  # the 3 swallowed repeats, named
        assert warnings[1]["stray_lake_count"] == 2

    def test_one_gate_covers_both_shapes_warn_then_info(self) -> None:
        """ONE gate tracks BOTH log shapes: a stray-drift WARNING followed by the partial-lake
        INFO is a fingerprint change (logged), and flipping back re-logs the WARNING — the two
        shapes never suppress across a genuine state change."""
        gate = ReconcileLogGate()
        info_rows = [
            {
                "symbol": "NSE-NIFTY",
                "exchange": "NSE",
                "segment": "INDEX",
                "interval": "1minute",
                "completeness": COMPLETENESS_SETTLED,
                "first_date": "2020-01-01",
                "last_date": None,
            }
        ]
        info_cats = {"NSE-NIFTY": CATEGORY_INDEX, "RELIANCE": CATEGORY_EQUITY}
        with capture_logs() as logs:
            log_symbol_reconcile_from_board(_stray_rows(), _stray_cats(), gate=gate)  # warn
            log_symbol_reconcile_from_board(_stray_rows(), _stray_cats(), gate=gate)  # repeat
            log_symbol_reconcile_from_board(info_rows, info_cats, gate=gate)  # shape flip
            log_symbol_reconcile_from_board(_stray_rows(), _stray_cats(), gate=gate)  # flip back
        warnings = [e for e in logs if e.get("event") == RECONCILE_MISMATCH_EVENT]
        infos = [e for e in logs if e.get("event") == SYMBOLS_NOT_YET_PULLED_EVENT]
        assert len(warnings) == 2
        assert len(infos) == 1
        assert infos[0]["suppressed_repeats"] == 1  # the one swallowed WARNING repeat, named
        assert warnings[1]["suppressed_repeats"] == 0

    def test_clean_interlude_rearms_the_gate(self) -> None:
        """mismatch → clean → the SAME mismatch again: the clean state is a fingerprint change
        (silent by design — the existing clean-is-silent pin), so the RE-appearing mismatch
        logs again instead of being suppressed as a stale repeat."""
        gate = ReconcileLogGate()
        clean_rows = [
            {
                "symbol": "M&M",
                "exchange": "NSE",
                "segment": "CASH",
                "interval": "1minute",
                "completeness": COMPLETENESS_SETTLED,
                "first_date": "2020-01-01",
                "last_date": None,
            }
        ]
        with capture_logs() as logs:
            log_symbol_reconcile_from_board(_stray_rows(), _stray_cats(), gate=gate)  # logs
            log_symbol_reconcile_from_board(clean_rows, _stray_cats(), gate=gate)  # silent, re-arms
            log_symbol_reconcile_from_board(_stray_rows(), _stray_cats(), gate=gate)  # logs again
        warnings = [e for e in logs if e.get("event") == RECONCILE_MISMATCH_EVENT]
        assert len(warnings) == 2
        assert [e for e in logs if e.get("event") == SYMBOLS_NOT_YET_PULLED_EVENT] == []


class TestScoreboardScopeExcludedEcho:
    """The R2 emit-on-nonzero census on the scoreboard (DEC-UNIVERSE-INDEX-ONLY-001, #1934)."""

    def test_default_zero_is_byte_identical(self) -> None:
        base = build_completeness_scoreboard([], {}, [], {})
        assert build_completeness_scoreboard([], {}, [], {}, scope_excluded=0) == base
        assert "scope_excluded" not in base

    def test_emits_on_nonzero(self) -> None:
        board = build_completeness_scoreboard([], {}, [], {}, scope_excluded=4)
        assert board["scope_excluded"] == 4
        # The named sections are untouched by the echo.
        assert set(board) == {"spot", "options", "scope_excluded"}

    def test_csv_render_tolerates_the_echo(self) -> None:
        # scoreboard_to_csv reads the named sections only — the additive echo
        # can never break the download (structural tolerance pin).
        board = build_completeness_scoreboard([], {}, [], {}, scope_excluded=4)
        text = scoreboard_to_csv(board)
        assert "Data Completeness" in text or text  # renders, never raises
