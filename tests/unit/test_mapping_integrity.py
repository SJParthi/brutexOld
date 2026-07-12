"""Unit tests for the mapping-integrity reconciliation (DEC-MAPPING-INTEGRITY-RECONCILE-001).

Pure, offline — 100% line+branch with tiny injected fixtures (no store/lake/network). Covers
the full / none(M&M) / partial(BAJAJ-AUTO) / not_attempted(selective-pull) / whitelisted /
empty-span / empty-universe cases, both eligible-spot input shapes, de-dup, the green/red gate,
the attempted-set distinction (DEC-MAPPING-INTEGRITY-ATTEMPTED-001), and the JSON projection.
"""

from __future__ import annotations

from datetime import UTC, datetime

from brutex.data.coverage_ledger import CoverageRecord
from brutex.data.mapping_integrity import (
    REASON_NONE,
    REASON_NOT_ATTEMPTED,
    REASON_PARTIAL,
    REASONS,
    EligibleSpot,
    MappingIntegrityReport,
    UnderPulledSpot,
    attempted_pairs_from_records,
    coverage_years_from_records,
    reconcile_mapping_integrity,
    report_to_dict,
)

_NOW = datetime(2026, 6, 21, tzinfo=UTC)
_EXPECTED = (2020, 2021, 2022)


def _reconcile(
    eligible, covered, *, expected=_EXPECTED, attempted=None, whitelist=(), key_forms=None
):
    # ``key_forms`` is threaded ONLY when given, so every pre-existing call exercises
    # the OMITTED-kwarg default path (the DEC-MAPPING-INTEGRITY-FORM-KEY-001
    # default-flip mutant killer); the explicit-``None`` arm is pinned separately in
    # ``test_key_forms_none_is_byte_identical``.
    extra = {} if key_forms is None else {"key_forms": key_forms}
    return reconcile_mapping_integrity(
        eligible_spots=eligible,
        covered_years=covered,
        expected_years=expected,
        attempted=attempted,
        whitelist=whitelist,
        now=_NOW,
        **extra,
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
    """Drift guard: the public REASONS tuple stays the three known reasons."""
    assert REASONS == (REASON_NONE, REASON_PARTIAL, REASON_NOT_ATTEMPTED)
    # the dataclasses are importable and frozen (slots) — construct one for coverage.
    spot = UnderPulledSpot("NSE", "X", REASON_NONE, (), (2020,), False)
    assert isinstance(spot, UnderPulledSpot)
    assert isinstance(MappingIntegrityReport("t", 0, 0, 0, (), (), ()), MappingIntegrityReport)


# ── DEC-MAPPING-INTEGRITY-ATTEMPTED-001: attempted vs never-attempted ─────────


def test_never_attempted_spot_is_neutral_not_gating() -> None:
    """Defect 1: an eligible spot with NO coverage row (never pulled) is not_attempted, NOT red."""
    report = _reconcile([("NSE", "M&M")], {}, attempted=set())
    assert report.is_green is True  # never attempted ⇒ never gates
    [u] = report.under_pulled
    assert u.reason == REASON_NOT_ATTEMPTED and u.whitelisted is False
    assert u.covered_years == () and u.missing_years == _EXPECTED
    assert report.gating_failures == ()
    assert report.not_attempted == 1 and report.fully_covered == 0


def test_attempted_zero_spot_is_gating_red() -> None:
    """Defect 2: an attempted-and-EMPTY spot (a coverage row, zero years) gates red — M&M."""
    report = _reconcile([("NSE", "M&M")], {}, attempted={("NSE", "M&M")})
    assert report.is_green is False
    [u] = report.under_pulled
    assert u.reason == REASON_NONE
    assert len(report.gating_failures) == 1
    assert report.not_attempted == 0


def test_green_on_clean_selective_pull() -> None:
    """The headline fix: a selective pull (only NIFTY attempted+covered) reads GREEN.

    The other eligible names were never attempted → not_attempted, never red — so the banner
    no longer falsely brands a whole 750-name universe "pulled ZERO expiries".
    """
    eligible = [("NSE", "NIFTY"), ("NSE", "M&M"), ("NSE", "RELIANCE"), ("NSE", "SBIN")]
    report = _reconcile(
        eligible,
        {("NSE", "NIFTY"): [2020, 2021, 2022]},
        attempted={("NSE", "NIFTY")},
    )
    assert report.is_green is True
    assert report.fully_covered == 1
    assert report.not_attempted == 3  # M&M, RELIANCE, SBIN — never pulled, neutral
    assert report.gating_failures == ()
    assert {u.reason for u in report.under_pulled} == {REASON_NOT_ATTEMPTED}


def test_mixed_attempted_and_not_attempted() -> None:
    """A real failure (attempted M&M) stays catchable AMONG never-attempted names."""
    eligible = [("NSE", "M&M"), ("NSE", "RELIANCE"), ("NSE", "SBIN")]
    report = _reconcile(
        eligible,
        {("NSE", "RELIANCE"): [2020, 2021, 2022]},
        attempted={("NSE", "RELIANCE"), ("NSE", "M&M")},  # SBIN never attempted
    )
    assert report.is_green is False  # M&M attempted-and-empty still flags
    by_sym = {u.symbol: u.reason for u in report.under_pulled}
    assert by_sym == {"M&M": REASON_NONE, "SBIN": REASON_NOT_ATTEMPTED}
    assert [u.symbol for u in report.gating_failures] == ["M&M"]
    assert report.not_attempted == 1 and report.fully_covered == 1


def test_attempted_none_defaults_to_all_attempted_back_compat() -> None:
    """``attempted=None`` (the back-compat default) treats EVERY eligible spot as attempted."""
    report = _reconcile([("NSE", "M&M")], {}, attempted=None)
    assert report.is_green is False  # behaves like the pre-attempted reconciler
    [u] = report.under_pulled
    assert u.reason == REASON_NONE
    assert report.not_attempted == 0


def test_attempted_pairs_from_records_keeps_any_row() -> None:
    """attempted_pairs_from_records keeps a pair for ANY row — even a zero-expiry attempt."""
    records = [
        _cov("RELIANCE", 2020, expiries=50),
        _cov("M&M", 2020, expiries=0),  # attempted-and-empty — STILL counts as attempted
    ]
    assert attempted_pairs_from_records(records) == {("NSE", "RELIANCE"), ("NSE", "M&M")}
    # …whereas coverage_years_from_records drops the zero-expiry M&M cell.
    assert ("NSE", "M&M") not in coverage_years_from_records(records)


def test_report_to_dict_carries_not_attempted() -> None:
    """The JSON projection exposes the not_attempted tally for the dashboard banner."""
    report = _reconcile(
        [("NSE", "NIFTY"), ("NSE", "SBIN")],
        {("NSE", "NIFTY"): [2020, 2021, 2022]},
        attempted={("NSE", "NIFTY")},
    )
    payload = report_to_dict(report)
    assert payload["not_attempted"] == 1
    assert payload["is_green"] is True
    assert payload["gating_failures"] == 0


# ── DEC-MAPPING-INTEGRITY-FORM-KEY-001: the key_forms keyspace crossing ───────


def test_key_forms_none_is_byte_identical() -> None:
    """``key_forms=None`` (the default) is byte-identical to the exact-key compare.

    The default IS the mutant killer for a flipped default: the same fixture with and
    without an explicit ``key_forms=None`` serialises identically (and the 20 prior
    tests above pass unmodified).
    """
    eligible = [("NSE", "M&M"), ("NSE", "BAJAJ-AUTO"), ("NSE", "RELIANCE")]
    covered = {("NSE", "BAJAJ-AUTO"): [2022], ("NSE", "RELIANCE"): [2020, 2021, 2022]}
    attempted = {("NSE", "M&M"), ("NSE", "BAJAJ-AUTO"), ("NSE", "RELIANCE")}
    without = _reconcile(eligible, covered, attempted=attempted)
    with_none = reconcile_mapping_integrity(
        eligible_spots=eligible,
        covered_years=covered,
        expected_years=_EXPECTED,
        attempted=attempted,
        whitelist=(),
        now=_NOW,
        key_forms=None,
    )
    assert report_to_dict(without) == report_to_dict(with_none)


def test_key_forms_union_across_forms() -> None:
    """Covered years UNION across the forms superset — never primary-only, never last-wins.

    Legacy raw-era 2021 (``BAJAJ-AUTO``) + primary-era 2020+2022 (``BAJAJAUTO``) rows
    union into ONE fully-covered verdict (the DEC-PULL-FORENSIC-WAVE2-001
    legacy-key-era law; kills the union→last-wins and forms``[:1]`` mutants).
    """
    report = _reconcile(
        [("NSE", "BAJAJ-AUTO")],
        {("NSE", "BAJAJ-AUTO"): [2021], ("NSE", "BAJAJAUTO"): [2020, 2022]},
        key_forms={"BAJAJ-AUTO": ("BAJAJAUTO", "BAJAJ-AUTO")},
    )
    assert report.fully_covered == 1
    assert report.under_pulled == ()
    # And a split leaving a hole reads ONE partial row with the UNION of years.
    partial = _reconcile(
        [("NSE", "BAJAJ-AUTO")],
        {("NSE", "BAJAJ-AUTO"): [2021], ("NSE", "BAJAJAUTO"): [2022]},
        key_forms={"BAJAJ-AUTO": ("BAJAJAUTO", "BAJAJ-AUTO")},
    )
    [u] = partial.under_pulled
    assert u.reason == REASON_PARTIAL
    assert u.covered_years == (2021, 2022)  # the cross-era union
    assert u.missing_years == (2020,)


def test_key_forms_attempted_any_form() -> None:
    """Attempted means ANY form has a coverage row — the silent-vanish alarm can fire.

    The attempted set holds only the PRIMARY form (the writer's keyspace); the
    eligible SPOT spelling must still read attempted-and-zero → the gating ``none``,
    never ``not_attempted`` (kills the primary-only membership and ``any``→``all``
    mutants — with ``all``, the raw-spelling miss would flip it back).
    """
    report = _reconcile(
        [("NSE", "BAJAJ-AUTO")],
        {},
        attempted={("NSE", "BAJAJAUTO")},
        key_forms={"BAJAJ-AUTO": ("BAJAJAUTO", "BAJAJ-AUTO")},
    )
    assert report.is_green is False
    [u] = report.under_pulled
    assert u.symbol == "BAJAJ-AUTO"  # display stays the SPOT spelling
    assert u.reason == REASON_NONE
    assert report.not_attempted == 0


def test_key_forms_symbol_absent_degrades_to_exact_key() -> None:
    """A symbol missing from the map degrades to today's exact-key compare."""
    report = _reconcile(
        [("NSE", "RELIANCE")],
        {("NSE", "RELIANCE"): [2020, 2021, 2022]},
        key_forms={"BAJAJ-AUTO": ("BAJAJAUTO", "BAJAJ-AUTO")},
    )
    assert report.fully_covered == 1
    assert report.under_pulled == ()


def test_key_forms_empty_forms_degrades_to_exact_key() -> None:
    """The BUG-3 interaction pin: an EMPTY forms tuple degrades to the exact raw key.

    ``fno_underlying_forms("") == ()`` (DEC-FNO-UNDERLYING-EMPTY-GUARD-001), so the
    builder map may hold ``{"": ()}`` — the ``or (symbol,)`` degrade probes the exact
    ``("NSE", "")`` key, matches nothing, and reads the honest ``not_attempted``;
    never a raise (kills the ``or (symbol,)`` removal mutant — without it the years
    generator yields nothing AND ``any`` over no forms reads never-attempted even
    when the raw key was attempted).
    """
    report = _reconcile(
        [("NSE", "")],
        {},
        attempted=set(),
        key_forms={"": ()},
    )
    [u] = report.under_pulled
    assert u.symbol == "" and u.reason == REASON_NOT_ATTEMPTED
    # The exact-raw-key degrade has TEETH: an attempted raw key still reads through.
    attempted_raw = _reconcile(
        [("NSE", "")],
        {},
        attempted={("NSE", "")},
        key_forms={"": ()},
    )
    [u2] = attempted_raw.under_pulled
    assert u2.reason == REASON_NONE


def test_key_forms_alias_form_resolves() -> None:
    """The dormant index-alias class is covered for free: NIFTYJR reads NIFTYNXT50 coverage."""
    report = _reconcile(
        [("NSE", "NIFTYJR")],
        {("NSE", "NIFTYNXT50"): [2020, 2021, 2022]},
        key_forms={"NIFTYJR": ("NIFTYNXT50", "NIFTYJR")},
    )
    assert report.fully_covered == 1
    assert report.under_pulled == ()


def test_with_legs_dual_spelling_no_dedup() -> None:
    """The no-dedup choice pinned: dual spellings BOTH resolve, ``total_eligible`` counts 2.

    On a full-with-legs store the arm(a) ``BAJAJAUTO`` + arm(b) ``BAJAJ-AUTO``
    eligible rows both resolve to the SAME coverage (count semantics unchanged; a
    vanish would flag BOTH rows red — louder, never quieter).
    """
    report = _reconcile(
        [("NSE", "BAJAJAUTO"), ("NSE", "BAJAJ-AUTO")],
        {("NSE", "BAJAJAUTO"): [2020, 2021, 2022]},
        key_forms={
            "BAJAJAUTO": ("BAJAJAUTO",),
            "BAJAJ-AUTO": ("BAJAJAUTO", "BAJAJ-AUTO"),
        },
    )
    assert report.total_eligible == 2
    assert report.fully_covered == 2
    assert report.under_pulled == ()


def test_whitelist_matches_display_spelling() -> None:
    """The whitelist matches the SPOT spelling — it never needs the F&O form."""
    report = _reconcile(
        [("NSE", "BAJAJ-AUTO")],
        {},
        attempted={("NSE", "BAJAJAUTO")},
        whitelist=["BAJAJ-AUTO"],
        key_forms={"BAJAJ-AUTO": ("BAJAJAUTO", "BAJAJ-AUTO")},
    )
    [u] = report.under_pulled
    assert u.reason == REASON_NONE and u.whitelisted is True
    assert report.gating_failures == ()
    assert report.is_green is True
