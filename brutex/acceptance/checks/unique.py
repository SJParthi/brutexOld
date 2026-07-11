"""Check 1 — uniqueness / dedup / foreign / overlap census (composed, never re-read).

DEC-OPERATOR-ACCEPTANCE-VERIFY-001 (MIGRATION.md #1756). Verbatim reuse of the
``scan_lake_invariants → build_lake_census → census_to_dict`` fold the
``brutex lake audit-unique`` CLI + doctor + ``/universe`` chip share. PASS only
when the scope is COMPLETE and clean — any finding OR any skipped/unreadable
file FAILs (DEC-AUDIT-CENSUS-HONESTY-001: incompleteness never certifies clean).
"""

from __future__ import annotations

from datetime import date
from typing import Any

from brutex.acceptance.context import AcceptanceContext
from brutex.acceptance.model import (
    AbstainReason,
    CheckResult,
    Verdict,
    make_check,
    make_coverage,
    make_subcheck,
)

CHECK_ID = "unique"

#: Offender-path samples are capped at this many entries (named in evidence).
OFFENDER_CAP = 20


def recent_months(run_date: date, months: int) -> list[tuple[int, int]]:
    """The last *months* calendar (year, month) cells ending at *run_date*."""
    out: list[tuple[int, int]] = []
    year, month = run_date.year, run_date.month
    for _ in range(months):
        out.append((year, month))
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return sorted(out)


def _repro(ctx: AcceptanceContext) -> str:
    return f"brutex verify-acceptance --checks {CHECK_ID} --seed {ctx.seed}"


def _abstain_check(ctx: AcceptanceContext, reason: AbstainReason) -> CheckResult:
    sub = make_subcheck(
        "unique.census",
        Verdict.ABSTAIN,
        units_checked=0,
        evidence={"reason": str(reason)},
        abstain_reason=reason,
        repro=_repro(ctx),
    )
    coverage = make_coverage(mode="none", population=0, examined=0, seed=ctx.seed)
    return make_check(CHECK_ID, [sub], coverage=coverage, repro=_repro(ctx))


def run(ctx: AcceptanceContext) -> CheckResult:
    """Run the uniqueness census over the engine set + the bounded rest."""
    from brutex.data.lake_invariants import (
        ENGINE_INDEX_COORDS,
        build_lake_census,
        census_to_dict,
        scan_lake_invariants,
    )

    if not (ctx.lake_root / "bars").is_dir():
        return _abstain_check(ctx, AbstainReason.NO_LAKE)
    if ctx.pull_active() and not ctx.force_during_pull:
        return _abstain_check(ctx, AbstainReason.PULL_IN_PROGRESS)

    months = None if ctx.full else recent_months(ctx.run_date(), ctx.months)
    report = scan_lake_invariants(ctx.lake_root, months=months, include_options=True)
    census = build_lake_census(report)
    census_dict = census_to_dict(census, mode="acceptance")

    offenders = [str(getattr(f, "path", repr(f))) for f in report.findings]
    skipped = report.files_skipped + report.option_files_skipped
    units = report.files_scanned + report.option_files_scanned
    census_verdict = Verdict.PASS if census.findings_total == 0 and skipped == 0 else Verdict.FAIL
    census_sub = make_subcheck(
        "unique.census",
        census_verdict,
        units_checked=units,
        evidence={
            "census": census_dict,
            "findings_total": census.findings_total,
            "files_scanned": units,
            "files_skipped": skipped,
            "first_skipped_path": (
                None if report.first_skipped_path is None else str(report.first_skipped_path)
            ),
            "offenders": offenders[:OFFENDER_CAP],
            "offenders_total": len(offenders),
            "offenders_truncated": len(offenders) > OFFENDER_CAP,
        },
        limits=["a skip is a FAIL — incompleteness never certifies clean"],
        repro=_repro(ctx),
    )

    # The operator's headline row: FULL-history engine scans regardless of the
    # month bound (the engine set is never left to the sampling window).
    engine_evidence: dict[str, Any] = {}
    engine_units = 0
    engine_clean = True
    if ctx.full:
        engine_clean = census.engine_clean
        engine_evidence["engine_detail"] = census.engine_detail
        engine_units = census.files_scanned
    else:
        details: dict[str, str] = {}
        for exchange, segment, symbol in ENGINE_INDEX_COORDS:
            sub_report = scan_lake_invariants(
                ctx.lake_root,
                exchange=exchange,
                segment=segment,
                symbol=symbol,
                include_options=False,
            )
            sub_census = build_lake_census(sub_report)
            engine_units += sub_report.files_scanned
            clean = sub_census.findings_total == 0 and sub_report.files_skipped == 0
            engine_clean = engine_clean and clean
            details[symbol] = "CLEAN" if clean else "POISONED-or-UNREADABLE"
        engine_evidence["engine_detail"] = details
    engine_sub = make_subcheck(
        "unique.engine",
        Verdict.PASS if engine_clean else Verdict.FAIL,
        units_checked=engine_units,
        evidence=engine_evidence,
        limits=["full stored history — never month-bounded"],
        repro=_repro(ctx),
    )

    coverage = make_coverage(
        mode="full" if ctx.full else "bounded",
        population=units,
        examined=units,
        seed=ctx.seed,
        selection=[f"months={ctx.months}" if not ctx.full else "whole-lake"],
    )
    return make_check(
        CHECK_ID,
        [census_sub, engine_sub],
        coverage=coverage,
        repro=_repro(ctx),
        limits=(
            []
            if ctx.full
            else [f"non-engine scope bounded to the last {ctx.months} months (--full lifts)"]
        ),
    )
