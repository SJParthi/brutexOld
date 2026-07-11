"""Check 1 — uniqueness / dedup / foreign / overlap census (composed, never re-read).

DEC-OPERATOR-ACCEPTANCE-VERIFY-001 (MIGRATION.md #1756). Verbatim reuse of the
``scan_lake_invariants`` scanner the ``brutex lake audit-unique`` CLI + doctor +
``/universe`` chip share. PASS only when the scope is COMPLETE and clean — any
finding OR any skipped/unreadable file FAILs (DEC-AUDIT-CENSUS-HONESTY-001:
incompleteness never certifies clean). Default (non ``--full``) scope is the
SAMPLED datasets × the last ``--months`` month cells via per-coordinate bounded
scans (+ the whole ``options/`` tree, filename-bounded) — never a whole-lake
``bars/`` tree enumeration (review round 1 complexity MAJOR-3); ``--full`` is
the sanctioned whole-lake single scan.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from brutex.acceptance.checks import run_fingerprinted_check
from brutex.acceptance.context import AcceptanceContext
from brutex.acceptance.model import (
    AbstainReason,
    CheckResult,
    Verdict,
    make_check,
    make_coverage,
    make_subcheck,
)
from brutex.acceptance.sampling import (
    month_bounded_files,
    recent_months,
    sample_datasets,
    sample_fno_contracts,
)

CHECK_ID = "unique"

#: Offender-path samples are capped at this many entries (named in evidence).
OFFENDER_CAP = 20

#: Sampled F&O contract dirs scanned per non-``--full`` run (mirrors tf).
FNO_SAMPLE_N = 6


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


def _symbol_dir_files(symbol_dir: Path, months: set[tuple[int, int]] | None) -> list[Path]:
    """Every month-bounded partition file under one symbol dir (all intervals)."""
    if not symbol_dir.is_dir():
        return []
    out: list[Path] = []
    for interval_dir in sorted(p for p in symbol_dir.iterdir() if p.is_dir()):
        out.extend(month_bounded_files(interval_dir, months))
    return out


def run(ctx: AcceptanceContext) -> CheckResult:
    """Run the uniqueness census over the engine set + the sampled bounded rest."""
    from brutex.data.lake_invariants import ENGINE_INDEX_COORDS

    if not (ctx.lake_root / "bars").is_dir():
        return _abstain_check(ctx, AbstainReason.NO_LAKE)
    if ctx.pull_active() and not ctx.force_during_pull:
        return _abstain_check(ctx, AbstainReason.PULL_IN_PROGRESS)

    plan = sample_datasets(ctx.spot_datasets(), seed=ctx.seed, sample_n=ctx.sample_n, full=ctx.full)
    contracts_all = ctx.fno_contracts()
    # Seed-ROTATED contract fill (round 2 fix 8) — the lexicographic head-6
    # never rotated, so a poisoned contract outside it was invisible forever.
    contracts = (
        contracts_all
        if ctx.full
        else sample_fno_contracts(contracts_all, seed=ctx.seed, sample_n=FNO_SAMPLE_N)
    )
    months = None if ctx.full else set(recent_months(ctx.run_date(), ctx.months))

    def _paths() -> list[Path]:
        paths: list[Path] = []
        if ctx.full:
            paths.extend((ctx.lake_root / "bars").rglob("*.parquet"))
        else:
            for exchange, segment, symbol in ENGINE_INDEX_COORDS:
                # Engine coords are scanned FULL-history — fingerprint them so.
                paths.extend(
                    _symbol_dir_files(ctx.lake_root / "bars" / exchange / segment / symbol, None)
                )
            for exchange, segment, symbol in plan.selected:
                paths.extend(
                    _symbol_dir_files(ctx.lake_root / "bars" / exchange / segment / symbol, months)
                )
            for contract in contracts:
                paths.extend(
                    _symbol_dir_files(ctx.lake_root / "bars" / "NSE" / "FNO" / contract, months)
                )
        options_tree = ctx.lake_root / "options"
        if options_tree.is_dir():
            paths.extend(options_tree.rglob("*.parquet"))
        return sorted(set(paths))

    return run_fingerprinted_check(
        ctx,
        check_id=CHECK_ID,
        sub_id="unique.census",
        paths_fn=_paths,
        body=lambda: _run_body(ctx, plan=plan, contracts=contracts, months=months),
    )


def _run_body(
    ctx: AcceptanceContext,
    *,
    plan: Any,
    contracts: list[str],
    months: set[tuple[int, int]] | None,
) -> CheckResult:
    from brutex.data.lake_invariants import (
        ENGINE_INDEX_COORDS,
        build_lake_census,
        census_to_dict,
        scan_lake_invariants,
    )

    census_evidence: dict[str, Any] = {}
    if ctx.full:
        report = scan_lake_invariants(ctx.lake_root, months=None, include_options=True)
        reports = [report]
        census = build_lake_census(report)
        census_evidence["census"] = census_to_dict(census, mode="acceptance")
    else:
        # Per-coordinate BOUNDED scans (the symbol filter prunes the glob) —
        # the census never enumerates the whole derived F&O tree by default.
        reports = [
            scan_lake_invariants(
                ctx.lake_root,
                exchange=exchange,
                segment=segment,
                symbol=symbol,
                months=months,
                include_options=False,
            )
            for exchange, segment, symbol in plan.selected
        ]
        reports.extend(
            scan_lake_invariants(
                ctx.lake_root,
                exchange="NSE",
                segment="FNO",
                symbol=contract,
                months=months,
                include_options=False,
            )
            for contract in contracts
        )
        # The legacy expired-options tree: ONE filename-bounded options-only scan.
        reports.append(
            scan_lake_invariants(
                ctx.lake_root, months=months, include_bars=False, include_options=True
            )
        )
        census_evidence["finding_kinds"] = dict(
            Counter(type(f).__name__ for r in reports for f in r.findings)
        )

    findings = [f for r in reports for f in r.findings]
    offenders = [str(getattr(f, "path", repr(f))) for f in findings]
    skipped = sum(r.files_skipped + r.option_files_skipped for r in reports)
    units = sum(r.files_scanned + r.option_files_scanned for r in reports)
    first_skipped = next(
        (str(r.first_skipped_path) for r in reports if r.first_skipped_path is not None), None
    )
    census_verdict = Verdict.PASS if not findings and skipped == 0 else Verdict.FAIL
    census_sub = make_subcheck(
        "unique.census",
        census_verdict,
        units_checked=units,
        evidence={
            **census_evidence,
            "findings_total": len(findings),
            "files_scanned": units,
            "files_skipped": skipped,
            "first_skipped_path": first_skipped,
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
        census = build_lake_census(reports[0])
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
        population=plan.population + len(ctx.fno_contracts()),
        examined=len(plan.selected) + len(contracts),
        seed=ctx.seed,
        selection=(
            ["whole-lake"]
            if ctx.full
            else [*plan.selection_keys, *(f"NSE/FNO/{c}" for c in contracts)]
        ),
    )
    return make_check(
        CHECK_ID,
        [census_sub, engine_sub],
        coverage=coverage,
        repro=_repro(ctx),
        limits=(
            []
            if ctx.full
            else [
                f"non-engine census scope = sampled datasets x last {ctx.months} months "
                "+ the whole options tree (--full lifts to the whole-lake scan)"
            ]
        ),
    )
