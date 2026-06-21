"""Unit tests for the instrument-master integrity CSV serializer (DEC-INSTRUMENT-MASTER-INTEGRITY-001).

Pure, offline, over hand-built :class:`MasterIntegrityReport` objects. 100% line + branch
over ``master_integrity_to_csv``, covering green/red, the leg-detail rows, None underlying id,
special chars, and formula-injection safety.
"""

from __future__ import annotations

import csv
import io

from brutex.dashboard.master_integrity_csv import master_integrity_to_csv
from brutex.data.master_integrity import (
    REASON_ORPHAN,
    MasterIntegrityReport,
    OrphanLeg,
    TypeCounts,
)


def _parse(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text)))


def _report(
    *,
    legs: tuple[OrphanLeg, ...] = (),
    idx: int = 1,
    eq: int = 1,
    fut: int = 0,
    ce: int = 0,
    pe: int = 0,
    special: tuple[str, ...] = (),
) -> MasterIntegrityReport:
    return MasterIntegrityReport(
        generated_at="2026-06-21T00:00:00+00:00",
        run_id="run-1",
        csv_source_sha256="abc",
        total_rows=idx + eq + fut + ce + pe,
        type_counts=TypeCounts(idx=idx, eq=eq, fut=fut, ce=ce, pe=pe),
        resolved_both=0,
        resolved_symbol_only=0,
        resolved_token_only=0,
        orphan_count=len([leg for leg in legs if leg.reason == REASON_ORPHAN]),
        ambiguous_count=0,
        disagreement_count=0,
        empty_underlying_count=0,
        dedup_collisions=0,
        spots_with_no_legs=0,
        special_char_underlyings=special,
        orphan_legs=legs,
    )


def test_green_when_no_failing_legs() -> None:
    text = master_integrity_to_csv(_report())
    assert ["Status", "GREEN"] in _parse(text)
    assert "Instrument-Master Integrity Audit" in text


def test_red_with_orphan_detail_row() -> None:
    leg = OrphanLeg(
        exchange="NSE",
        symbol="NOPECE",
        instrument_type="CE",
        underlying_symbol="NOPE",
        underlying_security_id=None,
        reason=REASON_ORPHAN,
    )
    text = master_integrity_to_csv(_report(legs=(leg,), ce=1))
    rows = _parse(text)
    assert ["Status", "RED"] in rows
    # None underlying id renders as an empty cell; the detail row round-trips.
    detail = next(r for r in rows if r[:2] == ["NSE", "NOPECE"])
    assert detail == ["NSE", "NOPECE", "CE", "NOPE", "", "orphan"]


def test_detail_row_with_numeric_underlying_id() -> None:
    leg = OrphanLeg(
        exchange="NSE",
        symbol="X24CE",
        instrument_type="CE",
        underlying_symbol="AAA",
        underlying_security_id=2031,
        reason=REASON_ORPHAN,
    )
    text = master_integrity_to_csv(_report(legs=(leg,), ce=1))
    detail = next(r for r in _parse(text) if r[:2] == ["NSE", "X24CE"])
    assert detail == ["NSE", "X24CE", "CE", "AAA", "2031", "orphan"]


def test_special_char_underlyings_rendered() -> None:
    text = master_integrity_to_csv(_report(special=("BAJAJ-AUTO", "M&M")))
    rows = _parse(text)
    assert ["Special-char underlyings", "BAJAJ-AUTO M&M"] in rows


def test_formula_injection_symbol_is_neutralised() -> None:
    leg = OrphanLeg(
        exchange="NSE",
        symbol="=HACK()",
        instrument_type="CE",
        underlying_symbol="=DANGER",
        underlying_security_id=None,
        reason=REASON_ORPHAN,
    )
    text = master_integrity_to_csv(_report(legs=(leg,), ce=1))
    assert "'=HACK()" in text
    assert "'=DANGER" in text
