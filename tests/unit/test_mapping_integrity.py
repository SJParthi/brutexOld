"""Unit tests for the mapping-integrity reconciliation (DEC-MAPPING-INTEGRITY-RECONCILE-001).

Pure, offline — 100% line+branch with tiny injected fixtures (no store/lake/network). Covers
the full / none(M&M) / partial(BAJAJ-AUTO) / whitelisted / empty-span / empty-universe cases,
both eligible-spot input shapes, de-dup, the green/red gate, and the JSON projection.
"""

from __future__ import annotations

from datetime import UTC, datetime

from brutex.data.coverage_ledger import CoverageRecord
from brutex.data.mapping_integrity import (
    REASON_NONE,
    REASON_PARTIAL,
    REASONS,
    EligibleSpot,
    MappingIntegrityReport,
    UnderPulledSpot,
    coverage_years_from_records,
    reconcile_mapping_integrity,
    report_to_dict,
)

_NOW = datetime(2026, 6, 21, tzinfo=UTC)
_EXPECTED = (2020, 2021, 2022)


def _reconcile(eligible, covered, *, expected=_EXPECTED, whitelist=()):
    return reconcile_mapping_integrity(
        eligible_spots=eligible,
        covered_years=covered,
        expected_years=expected,
        whitelist=whitelist,
        now=_NOW,
    )


def test_fully_covered_is_green() -> None:
    """An eligible spot covering every expected year → fully_covered, green, no under-pulled."""
    report = _reconcile(
        [EligibleSpot("NSE", "RELIANCE")],
        {("NSE", "RELIANCE"): [2020, 2021, 2022]},
    )
    assert report.is_green is True
    assert report.total_eligible == 1 and report.fully_covered == 1
    assert report.under_pulled == ()
    assert report.generated_at == _NOW.isoformat()


def test_zero_coverage_is_gating_red() -> None:
    """M&M-style: eligible but ZERO expiries pulled → reason none, gating, red."""
    report = _reconcile([("NSE", "M&M")], {})
    assert report.is_green is False
    [u] = report.under_pulled
    assert u.reason == REASON_NONE and u.whitelisted is False
    assert u.covered_years == () and u.missing_years == _EXPECTED
    assert len(report.gating_failures) == 1
    assert report.fully_covered == 0 and report.total_eligible == 1


def test_partial_coverage_is_amber_not_gating() -> None:
    """BAJAJ-AUTO-style: covered some years, missing the early span → partial, amber (still green)."""
    report = _reconcile(
        [EligibleSpot("NSE", "BAJAJ-AUTO")],
        {("NSE", "BAJAJ-AUTO"): [2022]},
    )
    assert report.is_green is True  # partial does NOT gate
    [u] = report.under_pulled
    assert u.reason == REASON_PARTIAL
    assert u.covered_years == (2022,) and u.missing_years == (2020, 2021)
    assert report.gating_failures == ()


def test_whitelisted_zero_coverage_does_not_gate() -> None:
    """A whitelisted genuine late listing with zero coverage is reported but never gates."""
    report = _reconcile([("NSE", "JIOFIN")], {}, whitelist=["JIOFIN"])
    assert report.is_green is True
    [u] = report.under_pulled
    assert u.reason == REASON_NONE and u.whitelisted is True
    assert report.gating_failures == ()
    assert report.whitelist == ("JIOFIN",)


def test_empty_expected_span_disables_partial_check() -> None:
    """With no expected span, a spot that pulled ANYTHING is fully covered (only none can fail)."""
    report = _reconcile(
        [("NSE", "RELIANCE"), ("NSE", "M&M")],
        {("NSE", "RELIANCE"): [2024]},
        expected=(),
    )
    assert report.expected_years == ()
    assert report.fully_covered == 1  # RELIANCE — one year is enough when no span expected
    [u] = report.under_pulled
    assert u.symbol == "M&M" and u.reason == REASON_NONE
    assert report.is_green is False


def test_covered_superset_of_expected_is_full() -> None:
    """Covering MORE than the expected span still counts as fully covered (subset check)."""
    report = _reconcile(
        [("NSE", "INFY")],
        {("NSE", "INFY"): [2019, 2020, 2021, 2022, 2023]},
    )
    assert report.fully_covered == 1 and report.under_pulled == ()


def test_empty_universe_is_vacuously_green() -> None:
    """No eligible spots → green, all counts zero, no crash."""
    report = _reconcile([], {})
    assert report.is_green is True
    assert report.total_eligible == 0 and report.fully_covered == 0
    assert report.under_pulled == ()


def test_duplicate_eligible_spot_counted_once() -> None:
    """A repeated eligible spot (both input shapes) is de-duplicated to one row."""
    report = _reconcile(
        [EligibleSpot("NSE", "RELIANCE"), ("NSE", "RELIANCE")],
        {("NSE", "RELIANCE"): [2020, 2021, 2022]},
    )
    assert report.total_eligible == 1 and report.fully_covered == 1


def test_under_pulled_sorted_and_mixed_gate() -> None:
    """Mixed bag: a non-wl none gates; a wl none + a partial do not; rows sorted by (exch, sym)."""
    report = _reconcile(
        [("NSE", "M&M"), ("NSE", "JIOFIN"), ("NSE", "BAJAJ-AUTO")],
        {("NSE", "BAJAJ-AUTO"): [2022]},
        whitelist=["JIOFIN"],
    )
    assert [u.symbol for u in report.under_pulled] == ["BAJAJ-AUTO", "JIOFIN", "M&M"]
    assert report.is_green is False
    assert [u.symbol for u in report.gating_failures] == ["M&M"]


def test_report_to_dict_round_trip() -> None:
    """report_to_dict carries the verdict, counts, span, whitelist, and the drill-down."""
    report = _reconcile(
        [("NSE", "M&M"), ("NSE", "BAJAJ-AUTO")],
        {("NSE", "BAJAJ-AUTO"): [2022]},
    )
    payload = report_to_dict(report)
    assert payload["is_green"] is False
    assert payload["total_eligible"] == 2 and payload["fully_covered"] == 0
    assert payload["under_pulled_count"] == 2 and payload["gating_failures"] == 1
    assert payload["expected_years"] == [2020, 2021, 2022]
    assert payload["whitelist"] == []
    rows = payload["under_pulled"]
    assert {r["symbol"] for r in rows} == {"M&M", "BAJAJ-AUTO"}
    bajaj = next(r for r in rows if r["symbol"] == "BAJAJ-AUTO")
    assert bajaj == {
        "exchange": "NSE",
        "symbol": "BAJAJ-AUTO",
        "reason": REASON_PARTIAL,
        "covered_years": [2022],
        "missing_years": [2020, 2021],
        "whitelisted": False,
    }


def _cov(underlying: str, year: int, *, expiries: int, exch: str = "NSE") -> CoverageRecord:
    return CoverageRecord(
        exch_id=exch,
        underlying_symbol=underlying,
        year=year,
        expiries_pulled=expiries,
        contracts_pulled=0,
        status="complete",
        last_run_id="r1",
        last_fetched_at=_NOW,
    )


def test_coverage_years_from_records_groups_nonzero_only() -> None:
    """Only cells with expiries_pulled>0 count; a 0-expiry cell (M&M/pre-listing) is excluded."""
    records = [
        _cov("RELIANCE", 2020, expiries=50),
        _cov("RELIANCE", 2021, expiries=49),
        _cov("M&M", 2020, expiries=0),  # zero-expiry → NOT coverage
        _cov("BAJAJ-AUTO", 2024, expiries=26),
    ]
    covered = coverage_years_from_records(records)
    assert covered == {("NSE", "RELIANCE"): {2020, 2021}, ("NSE", "BAJAJ-AUTO"): {2024}}
    assert ("NSE", "M&M") not in covered  # the zero-expiry cell is dropped


def test_coverage_years_feeds_reconcile_end_to_end() -> None:
    """The helper output plugs straight into reconcile — M&M red, RELIANCE green, BAJAJ partial."""
    covered = coverage_years_from_records(
        [
            _cov("RELIANCE", 2020, expiries=1),
            _cov("RELIANCE", 2021, expiries=1),
            _cov("RELIANCE", 2022, expiries=1),
            _cov("M&M", 2020, expiries=0),
            _cov("BAJAJ-AUTO", 2022, expiries=1),
        ]
    )
    report = _reconcile(
        [("NSE", "RELIANCE"), ("NSE", "M&M"), ("NSE", "BAJAJ-AUTO")],
        covered,
    )
    by_sym = {u.symbol: u.reason for u in report.under_pulled}
    assert by_sym == {"M&M": REASON_NONE, "BAJAJ-AUTO": REASON_PARTIAL}
    assert report.is_green is False and report.fully_covered == 1


def test_reasons_constant_pins_vocabulary() -> None:
    """Drift guard: the public REASONS tuple stays the two known reasons."""
    assert REASONS == (REASON_NONE, REASON_PARTIAL)
    # the dataclasses are importable and frozen (slots) — construct one for coverage.
    spot = UnderPulledSpot("NSE", "X", REASON_NONE, (), (2020,), False)
    assert isinstance(spot, UnderPulledSpot)
    assert isinstance(MappingIntegrityReport("t", 0, 0, (), (), ()), MappingIntegrityReport)
