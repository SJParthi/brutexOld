"""Unit tests for the mapping-integrity CSV serializer (DEC-MAPPING-INTEGRITY-EXPORT-001).

Pure, offline, over a hand-built :class:`MappingIntegrityReport` (no store/lake/network).
100% line + branch over ``mapping_integrity_to_csv``, covering green/red verdicts, every
``UnderPulledSpot`` reason, the whitelist, empties, and CSV/formula-injection safety on a
hostile symbol.
"""

from __future__ import annotations

import csv
import io

from brutex.dashboard.mapping_integrity_csv import mapping_integrity_to_csv
from brutex.data.mapping_integrity import (
    REASON_NONE,
    REASON_NOT_ATTEMPTED,
    REASON_PARTIAL,
    MappingIntegrityReport,
    UnderPulledSpot,
)


def _parse(text: str) -> list[list[str]]:
    """Parse the CSV back to rows (proves it is well-formed + escaped correctly)."""
    return list(csv.reader(io.StringIO(text)))


def _report(
    *,
    under: tuple[UnderPulledSpot, ...] = (),
    total: int = 1,
    fully: int = 1,
    not_attempted: int = 0,
    expected: tuple[int, ...] = (2020, 2021),
    whitelist: tuple[str, ...] = (),
) -> MappingIntegrityReport:
    return MappingIntegrityReport(
        generated_at="2026-06-21T00:00:00+00:00",
        total_eligible=total,
        fully_covered=fully,
        not_attempted=not_attempted,
        under_pulled=under,
        expected_years=expected,
        whitelist=whitelist,
    )


def test_green_verdict_when_no_under_pulled() -> None:
    text = mapping_integrity_to_csv(_report())
    assert ["Status", "GREEN"] in _parse(text)
    assert "BruteX — Mapping-Integrity Audit (spot ↔ options)" in text


def test_red_verdict_with_gating_none_row() -> None:
    mm = UnderPulledSpot(
        exchange="NSE",
        symbol="M&M",
        reason=REASON_NONE,
        covered_years=(),
        missing_years=(2020, 2021),
        whitelisted=False,
    )
    text = mapping_integrity_to_csv(_report(under=(mm,), total=2, fully=1))
    rows = _parse(text)
    assert ["Status", "RED"] in rows
    # The M&M row is present, marked gating=yes, with its missing years — and the `&` symbol
    # survives CSV round-trip intact (comma-safe escaping).
    mm_row = [r for r in rows if len(r) > 1 and r[1] == "M&M"]
    assert mm_row, "M&M detail row missing"
    assert mm_row[0] == ["NSE", "M&M", "none", "yes", "", "2020 2021", "no"]


def test_partial_row_is_not_gating() -> None:
    bajaj = UnderPulledSpot(
        exchange="NSE",
        symbol="BAJAJ-AUTO",
        reason=REASON_PARTIAL,
        covered_years=(2024,),
        missing_years=(2020, 2021),
        whitelisted=False,
    )
    text = mapping_integrity_to_csv(_report(under=(bajaj,), total=2, fully=1))
    rows = _parse(text)
    # partial → verdict stays GREEN (gating_failures is empty) and the row reads gating=no.
    assert ["Status", "GREEN"] in rows
    row = next(r for r in rows if r[:2] == ["NSE", "BAJAJ-AUTO"])
    assert row == ["NSE", "BAJAJ-AUTO", "partial", "no", "2024", "2020 2021", "no"]


def test_not_attempted_row_is_not_gating_and_counted_in_summary() -> None:
    """A never-attempted spot reads ``not_attempted``, gating=no, and a SUMMARY tally row."""
    sbin = UnderPulledSpot(
        exchange="NSE",
        symbol="SBIN",
        reason=REASON_NOT_ATTEMPTED,
        covered_years=(),
        missing_years=(2020, 2021),
        whitelisted=False,
    )
    text = mapping_integrity_to_csv(_report(under=(sbin,), total=2, fully=1, not_attempted=1))
    rows = _parse(text)
    assert ["Status", "GREEN"] in rows  # not_attempted never gates
    row = next(r for r in rows if r[:2] == ["NSE", "SBIN"])
    assert row == ["NSE", "SBIN", "not_attempted", "no", "", "2020 2021", "no"]
    assert ["Not attempted (no pull run yet)", "1"] in rows


def test_whitelisted_none_row_is_not_gating() -> None:
    jio = UnderPulledSpot(
        exchange="NSE",
        symbol="JIOFIN",
        reason=REASON_NONE,
        covered_years=(),
        missing_years=(2020, 2021),
        whitelisted=True,
    )
    text = mapping_integrity_to_csv(_report(under=(jio,), total=2, fully=1, whitelist=("JIOFIN",)))
    rows = _parse(text)
    assert ["Status", "GREEN"] in rows  # whitelisted none does not gate
    row = next(r for r in rows if r[:2] == ["NSE", "JIOFIN"])
    assert row == ["NSE", "JIOFIN", "none", "no", "", "2020 2021", "yes"]
    # The whitelist is echoed in the SUMMARY section.
    assert ["Whitelist", "JIOFIN"] in rows


def test_empty_expected_years_and_empty_whitelist_render_blank() -> None:
    text = mapping_integrity_to_csv(_report(expected=(), whitelist=()))
    rows = _parse(text)
    assert ["Expected years", ""] in rows
    assert ["Whitelist", ""] in rows


def test_formula_injection_symbol_is_neutralised() -> None:
    evil = UnderPulledSpot(
        exchange="NSE",
        symbol="=HYPERLINK(0)",
        reason=REASON_NONE,
        covered_years=(),
        missing_years=(),
        whitelisted=False,
    )
    text = mapping_integrity_to_csv(_report(under=(evil,), total=1, fully=0))
    # The leading '=' is defused with a single quote so a spreadsheet never executes it.
    assert "'=HYPERLINK(0)" in text


def test_revision_probe_marker_row_prepended() -> None:
    # Review fold (DEC-CACHE-SERVE-LAST-KNOWN-001): a last-known report served
    # during a store outage carries ONE in-band informational marker row.
    marker = "outage — serving last-known (store unreachable)"
    text = mapping_integrity_to_csv(_report(), revision_probe=marker)
    assert text.startswith(f"# revision_probe: {marker}\n")
    rows = _parse(text)
    assert rows[0] == [f"# revision_probe: {marker}"]
    # exactly ONE marker row, prepended — the report body is untouched.
    assert text.count("# revision_probe") == 1


def test_revision_probe_default_none_is_byte_identical() -> None:
    # Regression pin: the healthy serve (revision_probe=None) is byte-identical
    # to the pre-fold output — the golden oracle stays valid.
    assert mapping_integrity_to_csv(_report()) == mapping_integrity_to_csv(
        _report(), revision_probe=None
    )
    assert "# revision_probe" not in mapping_integrity_to_csv(_report())
