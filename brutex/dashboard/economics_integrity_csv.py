"""Economics-integrity audit → a downloadable, human-readable CSV report.

DEC-INSTRUMENT-ECONOMICS-INTEGRITY-001 — the file companion to the green/red ``/universe``
economics panel + JSON route. A single PURE string-builder over an already-computed
:class:`~brutex.data.economics_integrity.EconomicsIntegrityReport` (no store/lake/network, no
re-derivation — CLAUDE.md §11 COMMON), byte-stable for a golden oracle.

Three sections: VERDICT (GREEN/RED + run/sha/as-of), SUMMARY (legs, expiry groups, gated
within-expiry lot mismatches, informational cross-expiry revisions), DETAIL — the capped
mismatch sample THEN the informational lot-revision sample. Every cell is run through the
SHARED :class:`~brutex.dashboard._csv_utils.FormulaSafeWriter` (OWASP CSV-injection safety).
"""

from __future__ import annotations

import csv
import io

from brutex.dashboard._csv_utils import FormulaSafeWriter
from brutex.data.economics_integrity import EconomicsIntegrityReport


def economics_integrity_to_csv(report: EconomicsIntegrityReport) -> str:
    """Render an :class:`EconomicsIntegrityReport` as a downloadable CSV report (pure, stable)."""
    buf = io.StringIO()
    writer = FormulaSafeWriter(csv.writer(buf, lineterminator="\n"))

    writer.writerow(["BruteX — Instrument-Master Economics Integrity Audit (lot_size per chain)"])
    writer.writerow(
        [
            "lot_size must be uniform PER (underlying, expiry); a within-expiry mismatch is RED. "
            "Cross-expiry lot revisions are LEGITIMATE (NSE quarterly) and shown informational"
        ]
    )

    writer.writerow([])
    writer.writerow(["VERDICT"])
    writer.writerow(["Status", "GREEN" if report.is_green else "RED"])
    writer.writerow(["Generated at", report.generated_at])
    writer.writerow(["Run id", report.run_id])
    writer.writerow(["CSV sha256", report.csv_source_sha256])

    writer.writerow([])
    writer.writerow(["SUMMARY"])
    writer.writerow(["Total legs", report.total_legs])
    writer.writerow(["(underlying, expiry) groups", report.expiry_groups])
    writer.writerow(["Within-expiry lot mismatches (RED)", report.lot_mismatch_expiries])
    writer.writerow(
        [
            "Underlyings with cross-expiry lot revisions (informational)",
            report.underlyings_with_lot_revisions,
        ]
    )

    writer.writerow([])
    writer.writerow(["DETAIL — within-expiry lot mismatches (gating, capped sample)"])
    writer.writerow(["Exchange", "Underlying", "Expiry", "Distinct lots"])
    for m in report.mismatches:
        writer.writerow([m.exchange, m.underlying, m.expiry, " ".join(str(lot) for lot in m.lots)])

    writer.writerow([])
    writer.writerow(["DETAIL — cross-expiry lot revisions (informational, capped sample)"])
    writer.writerow(["Exchange", "Underlying", "Lots seen"])
    for r in report.lot_revisions:
        writer.writerow([r.exchange, r.underlying, " ".join(str(lot) for lot in r.lots)])

    return buf.getvalue()


__all__ = ["economics_integrity_to_csv"]
