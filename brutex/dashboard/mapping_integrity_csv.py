"""Mapping-integrity audit → a downloadable, human-readable CSV report.

DEC-MAPPING-INTEGRITY-EXPORT-001 — the "open it in Excel and SEE the proof" download
behind ``GET /api/v1/universe/mapping-integrity.csv``, the file companion to the live
green/red ``/universe`` panel + JSON route of DEC-MAPPING-INTEGRITY-RECONCILE-001 (#870).

A single PURE string-builder over an already-reconciled
:class:`~brutex.data.mapping_integrity.MappingIntegrityReport` — NO store / lake / network,
no new derivation (it only re-serialises the engine's own output, CLAUDE.md §11 COMMON), so
it is 100% offline-unit-testable and byte-stable for a golden oracle.

The file is a **three-section human report** (each a self-describing title row + its own
header, blank-line separated):

1. ``VERDICT`` — the one-line ``GREEN`` / ``RED`` answer + the as-of timestamp + the
   expected year span every eligible spot is checked against.
2. ``SUMMARY`` — total eligible, fully covered, under-pulled, gating failures, whitelist.
3. ``DETAIL — under-pulled spots`` — one row per shortfall: exchange, symbol, reason
   (``none`` attempted-and-zero gating / ``partial`` amber / ``not_attempted`` never-pulled
   neutral — DEC-MAPPING-INTEGRITY-ATTEMPTED-001), covered years, missing years, whitelisted.

Uses :mod:`csv` so any symbol with a comma / quote (e.g. ``M&M``) is escaped correctly, and
every cell is run through the SHARED :class:`~brutex.dashboard._csv_utils.FormulaSafeWriter`
so a value starting with ``= + - @`` (or a leading TAB / CR) can never execute as a
spreadsheet formula (OWASP "CSV injection" — DEC-CSV-FORMULA-INJECTION-001). ``\\n`` line
terminator for deterministic, platform-stable output.
"""

from __future__ import annotations

import csv
import io

from brutex.dashboard._csv_utils import FormulaSafeWriter
from brutex.data.mapping_integrity import REASON_NONE, MappingIntegrityReport


def _years_cell(years: tuple[int, ...]) -> str:
    """Render a year tuple as a space-separated string (``"2020 2021 2022"``; ``""`` if empty)."""
    return " ".join(str(year) for year in years)


def mapping_integrity_to_csv(
    report: MappingIntegrityReport, *, revision_probe: str | None = None
) -> str:
    """Render a :class:`MappingIntegrityReport` as a downloadable three-section CSV report.

    Pure over *report* — no I/O, no derivation. The verdict, summary, and per-spot drill-down
    mirror exactly what :func:`~brutex.data.mapping_integrity.report_to_dict` exposes to the
    JSON route, so the CSV is never less informative than the live panel (Golden Rule #1 — it
    re-serialises, it does not re-judge). Byte-stable for the golden oracle.

    Parameters
    ----------
    report
        The reconciled mapping-integrity report (from
        :func:`~brutex.data.mapping_integrity.reconcile_mapping_integrity`).
    revision_probe
        Optional staleness marker (DEC-CACHE-SERVE-LAST-KNOWN-001 review fold —
        the CSV twin of the JSON routes' additive ``revision_probe`` field).
        When set, ONE informational ``# revision_probe: …`` row is prepended
        through the SAME :class:`FormulaSafeWriter` so a last-known report
        served during a store outage carries an IN-BAND staleness signal
        (Golden Rule #1 — a stale serve is never silent, even in a file with
        no JSON marker surface). ``None`` (the default, every healthy serve)
        is byte-identical to the pre-fold output — golden-pinned.

    Returns
    -------
    str
        The full CSV document.
    """
    buf = io.StringIO()
    writer = FormulaSafeWriter(csv.writer(buf, lineterminator="\n"))

    if revision_probe is not None:
        writer.writerow([f"# revision_probe: {revision_probe}"])
    writer.writerow(["BruteX — Mapping-Integrity Audit (spot ↔ options)"])
    writer.writerow(
        [
            "Every options-eligible spot underlying must have pulled its option expiries; "
            "a 'none' row (zero pulled) is the GATING failure that turns the verdict RED"
        ]
    )

    writer.writerow([])
    writer.writerow(["VERDICT"])
    writer.writerow(["Status", "GREEN" if report.is_green else "RED"])
    writer.writerow(["Generated at", report.generated_at])
    writer.writerow(["Expected years", _years_cell(report.expected_years)])

    writer.writerow([])
    writer.writerow(["SUMMARY"])
    writer.writerow(["Total eligible", report.total_eligible])
    writer.writerow(["Fully covered", report.fully_covered])
    writer.writerow(["Not attempted (no pull run yet)", report.not_attempted])
    writer.writerow(["Under-pulled", len(report.under_pulled)])
    writer.writerow(["Gating failures (none, not whitelisted)", len(report.gating_failures)])
    writer.writerow(["Whitelist", " ".join(report.whitelist)])

    writer.writerow([])
    writer.writerow(["DETAIL — under-pulled spots"])
    writer.writerow(
        [
            "Exchange",
            "Symbol",
            "Reason",
            "Gating?",
            "Covered years",
            "Missing years",
            "Whitelisted",
        ]
    )
    for spot in report.under_pulled:
        gating = spot.reason == REASON_NONE and not spot.whitelisted
        writer.writerow(
            [
                spot.exchange,
                spot.symbol,
                spot.reason,
                "yes" if gating else "no",
                _years_cell(spot.covered_years),
                _years_cell(spot.missing_years),
                "yes" if spot.whitelisted else "no",
            ]
        )

    return buf.getvalue()


__all__ = ["mapping_integrity_to_csv"]
