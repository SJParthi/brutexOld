"""Instrument-master integrity audit → a downloadable, human-readable CSV report.

DEC-INSTRUMENT-MASTER-INTEGRITY-001 — the "open it in Excel and SEE the mapping proof"
download behind ``GET /api/v1/universe/master-integrity.csv``, the file companion to the
green/red ``/universe`` panel + JSON route.

A single PURE string-builder over an already-computed
:class:`~brutex.data.master_integrity.MasterIntegrityReport` — NO store / lake / network, no
re-derivation (it re-serialises the audit's own output, CLAUDE.md §11 COMMON), so it is 100%
offline-unit-testable and byte-stable for a golden oracle.

Three sections: VERDICT (GREEN/RED + run/sha/as-of), SUMMARY (per-type counts + resolution
buckets + dedup + spots-with-no-legs), DETAIL — the capped orphan/ambiguous/disagreement leg
sample. Every cell is run through the SHARED
:class:`~brutex.dashboard._csv_utils.FormulaSafeWriter` so a special-char underlying
(``M&M`` / ``BAJAJ-AUTO``) or a ``=``-leading symbol can never execute as a spreadsheet
formula (OWASP "CSV injection" — DEC-CSV-FORMULA-INJECTION-001). ``\\n`` line terminator for
deterministic, platform-stable output.
"""

from __future__ import annotations

import csv
import io

from brutex.dashboard._csv_utils import FormulaSafeWriter
from brutex.data.master_integrity import MasterIntegrityReport


def mapping_or_none(value: int | None) -> str:
    """Render an optional security id as a string (``""`` for ``None``)."""
    return "" if value is None else str(value)


def master_integrity_to_csv(report: MasterIntegrityReport) -> str:
    """Render a :class:`MasterIntegrityReport` as a downloadable three-section CSV report.

    Pure over *report* — no I/O, no re-derivation. Byte-stable for the golden oracle.
    """
    buf = io.StringIO()
    writer = FormulaSafeWriter(csv.writer(buf, lineterminator="\n"))

    writer.writerow(["BruteX — Instrument-Master Integrity Audit (every leg ↔ one spot)"])
    writer.writerow(
        [
            "Every FUT/CE/PE leg must map to exactly ONE IDX/EQ spot (by symbol AND numeric "
            "token); an orphan / ambiguous / disagreement / empty-underlying leg turns it RED"
        ]
    )

    writer.writerow([])
    writer.writerow(["VERDICT"])
    writer.writerow(["Status", "GREEN" if report.is_green else "RED"])
    writer.writerow(["Generated at", report.generated_at])
    writer.writerow(["Run id", report.run_id])
    writer.writerow(["CSV sha256", report.csv_source_sha256])

    tc = report.type_counts
    writer.writerow([])
    writer.writerow(["SUMMARY"])
    writer.writerow(["Total rows", report.total_rows])
    writer.writerow(["Indices (IDX)", tc.idx])
    writer.writerow(["Equities (EQ)", tc.eq])
    writer.writerow(["Futures (FUT)", tc.fut])
    writer.writerow(["Calls (CE)", tc.ce])
    writer.writerow(["Puts (PE)", tc.pe])
    writer.writerow(["Spots (IDX+EQ)", tc.spots])
    writer.writerow(["Legs (FUT+CE+PE)", tc.legs])
    writer.writerow(["Resolved both (symbol+token)", report.resolved_both])
    writer.writerow(["Resolved symbol only", report.resolved_symbol_only])
    writer.writerow(["Resolved token only", report.resolved_token_only])
    writer.writerow(["Orphan legs", report.orphan_count])
    writer.writerow(["Ambiguous legs", report.ambiguous_count])
    writer.writerow(["Symbol/token disagreement", report.disagreement_count])
    writer.writerow(["Empty-underlying legs", report.empty_underlying_count])
    writer.writerow(["Gating failures (red)", report.gating_failures])
    writer.writerow(["Duplicate rows", report.dedup_collisions])
    writer.writerow(["Spots with no legs", report.spots_with_no_legs])
    writer.writerow(["Exchange test legs (excluded, non-gating)", report.test_instrument_count])
    writer.writerow(["Special-char underlyings (count)", report.special_char_count])
    writer.writerow(
        ["Special-char underlyings (sample)", " ".join(report.special_char_underlyings)]
    )

    writer.writerow([])
    writer.writerow(["DETAIL — failing legs (capped sample)"])
    writer.writerow(
        [
            "Exchange",
            "Leg symbol",
            "Type",
            "Underlying symbol",
            "Underlying security id",
            "Reason",
        ]
    )
    for leg in report.orphan_legs:
        writer.writerow(
            [
                leg.exchange,
                leg.symbol,
                leg.instrument_type,
                leg.underlying_symbol or "",
                mapping_or_none(leg.underlying_security_id),
                leg.reason,
            ]
        )

    return buf.getvalue()


__all__ = ["master_integrity_to_csv"]
