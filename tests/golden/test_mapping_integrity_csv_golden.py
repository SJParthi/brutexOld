"""Golden byte-equality oracle for the mapping-integrity CSV (DEC-MAPPING-INTEGRITY-EXPORT-001).

Locks the EXACT bytes of :func:`brutex.dashboard.mapping_integrity_csv.mapping_integrity_to_csv`
for a hand-fixed report (one gating ``none`` = M&M, one ``partial`` = BAJAJ-AUTO, one
whitelisted ``none`` = JIOFIN, one neutral ``not_attempted`` = SBIN —
DEC-MAPPING-INTEGRITY-ATTEMPTED-001). Any drift in the serializer — a reordered section, a
renamed header, a changed cell, a broken comma-escape or formula-injection guard — flips the
SHA-256 and fails loudly (Golden Rule #4). The operator then decides: intentional (update the
digest + the corresponding MIGRATION entry) or a regression (fix the code).

The report is hand-built (no store/lake/network) so the oracle is fully deterministic.
"""

from __future__ import annotations

import hashlib

from brutex.dashboard.mapping_integrity_csv import mapping_integrity_to_csv
from brutex.data.mapping_integrity import (
    REASON_NONE,
    REASON_NOT_ATTEMPTED,
    REASON_PARTIAL,
    MappingIntegrityReport,
    UnderPulledSpot,
)

#: Locked golden digest. Regenerate ONLY on a deliberate report-format change; document it in
#: the corresponding MIGRATION.md DEC entry.
_GOLDEN_SHA256 = "be226894e138c90dc3185609a1842b9da61a95b56b73a1c5a3482b6f69822309"

_GOLDEN_CSV = (
    "BruteX — Mapping-Integrity Audit (spot ↔ options)\n"
    "Every options-eligible spot underlying must have pulled its option expiries; "
    "a 'none' row (zero pulled) is the GATING failure that turns the verdict RED\n"
    "\n"
    "VERDICT\n"
    "Status,RED\n"
    "Generated at,2026-06-21T00:00:00+00:00\n"
    "Expected years,2020 2021 2022 2023 2024\n"
    "\n"
    "SUMMARY\n"
    "Total eligible,5\n"
    "Fully covered,1\n"
    "Not attempted (no pull run yet),1\n"
    "Under-pulled,4\n"
    '"Gating failures (none, not whitelisted)",1\n'
    "Whitelist,JIOFIN\n"
    "\n"
    "DETAIL — under-pulled spots\n"
    "Exchange,Symbol,Reason,Gating?,Covered years,Missing years,Whitelisted\n"
    "NSE,M&M,none,yes,,2020 2021 2022,no\n"
    "NSE,BAJAJ-AUTO,partial,no,2024,2020 2021 2022 2023,no\n"
    "NSE,JIOFIN,none,no,,2020 2021 2022,yes\n"
    "NSE,SBIN,not_attempted,no,,2020 2021 2022,no\n"
)


def _golden_report() -> MappingIntegrityReport:
    return MappingIntegrityReport(
        generated_at="2026-06-21T00:00:00+00:00",
        total_eligible=5,
        fully_covered=1,
        not_attempted=1,
        under_pulled=(
            UnderPulledSpot(
                exchange="NSE",
                symbol="M&M",
                reason=REASON_NONE,
                covered_years=(),
                missing_years=(2020, 2021, 2022),
                whitelisted=False,
            ),
            UnderPulledSpot(
                exchange="NSE",
                symbol="BAJAJ-AUTO",
                reason=REASON_PARTIAL,
                covered_years=(2024,),
                missing_years=(2020, 2021, 2022, 2023),
                whitelisted=False,
            ),
            UnderPulledSpot(
                exchange="NSE",
                symbol="JIOFIN",
                reason=REASON_NONE,
                covered_years=(),
                missing_years=(2020, 2021, 2022),
                whitelisted=True,
            ),
            UnderPulledSpot(
                exchange="NSE",
                symbol="SBIN",
                reason=REASON_NOT_ATTEMPTED,
                covered_years=(),
                missing_years=(2020, 2021, 2022),
                whitelisted=False,
            ),
        ),
        expected_years=(2020, 2021, 2022, 2023, 2024),
        whitelist=("JIOFIN",),
    )


def test_mapping_integrity_csv_byte_stable() -> None:
    rendered = mapping_integrity_to_csv(_golden_report())
    assert rendered == _GOLDEN_CSV


def test_mapping_integrity_csv_sha256_pinned() -> None:
    rendered = mapping_integrity_to_csv(_golden_report())
    assert hashlib.sha256(rendered.encode("utf-8")).hexdigest() == _GOLDEN_SHA256
