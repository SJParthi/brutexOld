"""Unit tests for the economics-integrity CSV serializer (DEC-INSTRUMENT-ECONOMICS-INTEGRITY-001)."""

from __future__ import annotations

import csv
import io

from brutex.dashboard.economics_integrity_csv import economics_integrity_to_csv
from brutex.data.economics_integrity import (
    EconomicsIntegrityReport,
    LotMismatch,
    LotRevision,
)


def _parse(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text)))


def _report(
    *,
    mismatches: tuple[LotMismatch, ...] = (),
    revisions: tuple[LotRevision, ...] = (),
) -> EconomicsIntegrityReport:
    return EconomicsIntegrityReport(
        generated_at="2026-06-21T00:00:00+00:00",
        run_id="r1",
        csv_source_sha256="abc",
        total_legs=4,
        expiry_groups=2,
        lot_mismatch_expiries=len(mismatches),
        underlyings_with_lot_revisions=len(revisions),
        mismatches=mismatches,
        lot_revisions=revisions,
    )


def test_green_when_no_mismatch() -> None:
    text = economics_integrity_to_csv(_report())
    assert ["Status", "GREEN"] in _parse(text)
    assert "Economics Integrity Audit" in text


def test_red_with_mismatch_detail() -> None:
    mm = LotMismatch(exchange="NSE", underlying="RELIANCE", expiry="2026-06-25", lots=(500, 750))
    text = economics_integrity_to_csv(_report(mismatches=(mm,)))
    rows = _parse(text)
    assert ["Status", "RED"] in rows
    assert ["NSE", "RELIANCE", "2026-06-25", "500 750"] in rows


def test_revision_detail_is_informational() -> None:
    rev = LotRevision(exchange="NSE", underlying="AMBUJACEM", lots=(1050, 1200))
    text = economics_integrity_to_csv(_report(revisions=(rev,)))
    rows = _parse(text)
    assert ["Status", "GREEN"] in rows  # a revision alone does not gate
    assert ["NSE", "AMBUJACEM", "1050 1200"] in rows
