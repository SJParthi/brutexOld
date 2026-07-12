"""Run orchestration: teeth gate → fenced checks → roll-up → report → exit code.

DEC-OPERATOR-ACCEPTANCE-VERIFY-001 (MIGRATION.md #1756). Any exception in a
check runner → verdict ``ERROR`` with the exception in evidence, the run
CONTINUES (the verifier failing never silently loses the other checks'
evidence) and the overall exit is 2 (SUSPECT). ``TEETH_FAILED`` withholds all
real-lake verdicts. A report write / audit failure is itself SUSPECT.
"""

from __future__ import annotations

import socket
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from brutex.acceptance import report as report_mod
from brutex.acceptance import sampling, selftest
from brutex.acceptance.checks import ALL_CHECK_IDS, CHECK_REGISTRY
from brutex.acceptance.context import AcceptanceContext
from brutex.acceptance.model import (
    SCHEMA_VERSION,
    AbstainReason,
    CheckResult,
    OverallVerdict,
    TeethVerdict,
    Verdict,
    make_check,
    make_coverage,
    make_subcheck,
    rollup_overall,
    totals_for,
    with_streak,
)

_LOG = structlog.get_logger(__name__)


@dataclass
class RunConfig:
    """One resolved invocation of ``brutex verify-acceptance`` (no prompts)."""

    lake_root: Path
    checks: tuple[str, ...] = ALL_CHECK_IDS
    seed: str | None = None
    sample_n: int = 12
    months: int = 6
    full: bool = False
    date: str | None = None
    top_n: int = 20
    latency_rounds: int = 5
    latency_budget_overrides: dict[str, float] = field(default_factory=dict)
    abstain_budget: int = 3
    max_runtime_s: float | None = None
    force_during_pull: bool = False
    self_test_mode: str = "smoke"  # "smoke" | "full" | "skipped"
    out_dir: Path | None = None
    argv: tuple[str, ...] = ()


def _error_check(check_id: str, exc: BaseException, *, seed: str) -> CheckResult:
    tail = traceback.format_exc(limit=5)
    sub = make_subcheck(
        f"{check_id}.error",
        Verdict.ERROR,
        units_checked=0,
        evidence={
            "exception": f"{type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}",
            "traceback_tail": tail[-2000:],
        },
        repro=f"brutex verify-acceptance --checks {check_id} --seed {seed}",
    )
    coverage = make_coverage(mode="none", population=0, examined=0, seed=seed)
    return make_check(check_id, [sub], coverage=coverage)


def _budget_check(check_id: str, *, seed: str) -> CheckResult:
    sub = make_subcheck(
        f"{check_id}.skipped",
        Verdict.ABSTAIN,
        units_checked=0,
        evidence={"reason": "soft runtime budget exhausted before this check ran"},
        abstain_reason=AbstainReason.TIME_BUDGET_EXHAUSTED,
        repro=f"brutex verify-acceptance --checks {check_id} --seed {seed}",
    )
    coverage = make_coverage(mode="none", population=0, examined=0, seed=seed)
    return make_check(check_id, [sub], coverage=coverage)


def _box_block(ctx: AcceptanceContext) -> dict[str, Any]:
    import brutex

    try:
        from brutex.config.hardware_envelope import hardware_key

        hw = hardware_key()
    except Exception:
        hw = None
    return {
        "hostname": socket.gethostname(),
        "brutex_version": getattr(brutex, "__version__", None),
        "code_commit": None,
        "hardware_key": hw,
        "pull_active": ctx.pull_active(),
    }


def run_acceptance(
    cfg: RunConfig,
    *,
    now_utc: Callable[[], datetime] | None = None,
    client_factory: Callable[[], Any] | None = None,
    perf_timer: Callable[[], float] | None = None,
    selftest_runner: Callable[..., selftest.SelfTestResult] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Execute one acceptance run end-to-end; returns ``(exit_code, report_dict)``.

    Injection points (*now_utc* / *client_factory* / *perf_timer* /
    *selftest_runner*) exist for tests; production binds the real clock,
    dashboard client, ``perf_counter`` and the real self-test.
    """
    clock = now_utc or (lambda: datetime.now(UTC))
    started = time.monotonic()
    if not cfg.checks:
        # Design LAW 2 at run level (review round 1 MINOR): an empty check
        # list can never read as a clean exit-0 run — usage error in the CORE,
        # not just the CLI guard.
        return 1, {"error": "no checks requested — refusing a zero-work PASS run"}
    if cfg.sample_n < 0:
        # Round-3 NIT (C-F5) at the core too: a negative N is a Python slice
        # ([:-1] = population−1 datasets) — refused for in-process callers.
        return 1, {"error": "sample_n must be >= 0 — a negative sample is a slice surprise"}
    ctx = AcceptanceContext(
        lake_root=cfg.lake_root,
        seed=cfg.seed or "",
        sample_n=cfg.sample_n,
        months=cfg.months,
        full=cfg.full,
        date=cfg.date,
        top_n=cfg.top_n,
        latency_rounds=cfg.latency_rounds,
        latency_budget_overrides=cfg.latency_budget_overrides,
        force_during_pull=cfg.force_during_pull,
        now_utc=clock,
        client_factory=client_factory,
    )
    if perf_timer is not None:
        ctx.perf_timer = perf_timer
    if not ctx.seed:
        ctx.seed = ctx.run_date().isoformat()
        seed_mode = "rotated"
    else:
        seed_mode = "fixed"
    try:
        return _run_with_ctx(
            cfg,
            ctx,
            clock=clock,
            seed_mode=seed_mode,
            started=started,
            selftest_runner=selftest_runner,
        )
    finally:
        # Run-scoped teardown (MN1): restores any BRUTEX_LAKE_ROOT override
        # and closes the shared client + read-only store handle.
        ctx.close()


def _run_with_ctx(
    cfg: RunConfig,
    ctx: AcceptanceContext,
    *,
    clock: Callable[[], datetime],
    seed_mode: str,
    started: float,
    selftest_runner: Callable[..., selftest.SelfTestResult] | None,
) -> tuple[int, dict[str, Any]]:
    run_id = report_mod.mint_run_id(clock())

    # ONE evidence-tree symlink refusal covering EVERY write (round-3 fix
    # B-NEW-1): the round-2 guard sat at report-write time, so the audit
    # chain (and the coverage ledger's warn-only refusal) had already been
    # reached with <lake>/acceptance a symlink — the audit JSONL was written
    # THROUGH the link into the target dir. Hoisted here, BEFORE any write
    # (house .quarantine precedent — refuse the whole run loudly, SUSPECT).
    base = report_mod.acceptance_dir(cfg.lake_root)
    if base.is_symlink():
        _LOG.error("acceptance.evidence_tree_symlink_refused", path=str(base))
        return 2, {
            "error": f"{base} is a symlink — refusing to write the evidence tree through it",
            "run_id": run_id,
        }

    audit: report_mod.AcceptanceAudit | None
    try:
        audit = report_mod.AcceptanceAudit(cfg.lake_root)
        audit.run_start(run_id=run_id, argv=cfg.argv, seed=ctx.seed)
    except OSError as exc:
        _LOG.error("acceptance.audit_open_failed", error=str(exc))
        return 2, {"error": f"audit chain unavailable: {exc}", "run_id": run_id}
    try:
        return _run_audited(
            cfg,
            ctx,
            audit=audit,
            run_id=run_id,
            clock=clock,
            seed_mode=seed_mode,
            started=started,
            selftest_runner=selftest_runner,
        )
    finally:
        audit.close()  # idempotent — a raising run never leaks the chain fd


def _run_audited(
    cfg: RunConfig,
    ctx: AcceptanceContext,
    *,
    audit: report_mod.AcceptanceAudit,
    run_id: str,
    clock: Callable[[], datetime],
    seed_mode: str,
    started: float,
    selftest_runner: Callable[..., selftest.SelfTestResult] | None,
) -> tuple[int, dict[str, Any]]:
    # ── teeth before trust ─────────────────────────────────────────────
    if cfg.self_test_mode == "skipped":
        teeth = selftest.skipped_selftest()
    else:
        runner = selftest_runner or (
            selftest.run_full_battery if cfg.self_test_mode == "full" else selftest.run_smoke
        )
        try:
            teeth = runner(now_utc=clock())
        except MemoryError:
            raise  # house law (N3): OOM re-raises raw, never a soft verdict
        except Exception as exc:
            _LOG.error("acceptance.selftest_crashed", error=str(exc))
            teeth = selftest.SelfTestResult(
                mode=cfg.self_test_mode,
                poisons_planted=0,
                poisons_detected=0,
                false_fails_on_clean=0,
                verdict=TeethVerdict.TEETH_FAILED,
                duration_s=0.0,
            )

    checks: list[CheckResult] = []
    if teeth.verdict is not TeethVerdict.TEETH_FAILED:
        for check_id in cfg.checks:
            if cfg.max_runtime_s is not None and (time.monotonic() - started) > cfg.max_runtime_s:
                checks.append(_budget_check(check_id, seed=ctx.seed))
                continue
            check_started = time.monotonic()
            try:
                result = CHECK_REGISTRY[check_id](ctx)
            except MemoryError:
                raise  # house law (N3): OOM re-raises raw, never an ERROR verdict
            except Exception as exc:
                _LOG.error("acceptance.check_errored", check_id=check_id, error=str(exc))
                result = _error_check(check_id, exc, seed=ctx.seed)
            from dataclasses import replace

            result = replace(result, duration_s=round(time.monotonic() - check_started, 3))
            checks.append(result)
            try:
                audit.check(run_id=run_id, check=result)
            except OSError as exc:
                _LOG.error("acceptance.audit_append_failed", error=str(exc))
                audit.close()
                return 2, {"error": f"audit append failed: {exc}", "run_id": run_id}

    # ── streak carry from the previous report chain ────────────────────
    previous = report_mod.load_previous(cfg.lake_root)
    prev_index = report_mod.previous_subcheck_index(previous)
    stamped: list[CheckResult] = []
    for check in checks:
        from dataclasses import replace

        stamped.append(
            replace(
                check,
                sub_checks=tuple(
                    with_streak(sub, report_mod.streak_for(sub.id, sub.verdict, prev_index))
                    for sub in check.sub_checks
                ),
            )
        )
    checks = stamped

    time_truncated = any(
        sub.abstain_reason is AbstainReason.TIME_BUDGET_EXHAUSTED
        for check in checks
        for sub in check.sub_checks
    )
    overall = rollup_overall(
        checks,
        teeth=teeth.verdict,
        abstain_budget=cfg.abstain_budget,
        time_truncated=time_truncated,
    )

    # ── coverage ledger accumulation (visibility aid) ──────────────────
    population = ctx.spot_datasets()
    population_keys = [sampling.dataset_key(c) for c in population]
    # F&O contracts JOIN the ledger population (round 2 fix 8 — the sampled
    # contract rotation is now visible to oldest_unsampled), CAPPED because a
    # mature options lake holds ~10^5 contract dirs and the ledger is a small
    # JSON visibility aid (truncation disclosed below).
    fno_all = ctx.fno_contracts()
    fno_keys = [f"NSE/FNO/{c}" for c in sorted(fno_all)[: sampling.FNO_LEDGER_POPULATION_CAP]]
    fno_truncated = max(0, len(fno_all) - sampling.FNO_LEDGER_POPULATION_CAP)
    population_keys.extend(fno_keys)
    ledger_path = sampling.coverage_ledger_path(cfg.lake_root)
    examined_keys = sorted(
        {key for check in checks for key in check.coverage.selection if "/" in key}
    )
    if examined_keys:
        sampling.update_coverage_ledger(
            ledger_path,
            check_id="run",
            dataset_keys=examined_keys,
            run_date=ctx.run_date(),
            # Prune-on-save (round 2 fix 7): keys absent from the CURRENT
            # dataset population (expired contracts, deleted datasets) no
            # longer accumulate forever.
            population_keys=population_keys,
        )
    ledger_summary = sampling.coverage_ledger_summary(
        sampling.load_coverage_ledger(ledger_path),
        population_keys=population_keys,
        run_date=ctx.run_date(),
    )
    ledger_summary["fno_population_truncated"] = fno_truncated

    report: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "run_id": run_id,
        "generated_utc": clock().isoformat(),
        "duration_s": round(time.monotonic() - started, 3),
        "box": _box_block(ctx),
        "inputs": {
            "lake_root": str(cfg.lake_root),
            "checks_requested": list(cfg.checks),
            "seed": ctx.seed,
            "seed_mode": seed_mode,
            "full": cfg.full,
            "months_bound": cfg.months,
            "sample_n": cfg.sample_n,
            "latency_budget_overrides": dict(cfg.latency_budget_overrides),
            "argv": list(cfg.argv),
        },
        "self_test": teeth.to_dict(),
        "overall": overall.to_dict(),
        "checks": [c.to_dict() for c in checks],
        "totals": totals_for(checks),
        "coverage_ledger": ledger_summary,
        "previous": (
            None
            if previous is None
            else {
                "run_id": previous.get("run_id"),
                "generated_utc": previous.get("generated_utc"),
                "overall_verdict": (previous.get("overall") or {}).get("verdict"),
            }
        ),
        "deltas": report_mod.compute_deltas(previous, checks),
    }
    try:
        report_path, sha = report_mod.write_report_files(
            report, lake_root=cfg.lake_root, out_dir=cfg.out_dir
        )
        audit.run_complete(
            run_id=run_id,
            overall=str(overall.verdict),
            report_sha256=sha,
            report_path=str(report_path),
        )
        audit.close()
    except (report_mod.ReportWriteError, OSError) as exc:
        _LOG.error("acceptance.report_write_failed", error=str(exc))
        audit.close()
        # Round-3 fix (C-F4): the in-memory report still carried the
        # data-verdict exit code, so the printed table ended "→ exit 0"
        # while the PROCESS exited 2 — restamp overall to the truthful
        # SUSPECT and carry the failure for the CLI's loud stderr line.
        report["report_write_error"] = str(exc)
        report["overall"] = {
            **report["overall"],
            "verdict": str(OverallVerdict.SUSPECT),
            "exit_code": 2,
        }
        return 2, report
    exit_code = overall.exit_code
    _LOG.info(
        "acceptance.run_complete",
        run_id=run_id,
        overall=str(overall.verdict),
        exit_code=exit_code,
        teeth=str(teeth.verdict),
    )
    return exit_code, report


def repro_command(check_id: str, *, seed: str | None = None) -> str:
    """The exact single-check repro command (``--repro CHECK_ID``)."""
    suffix = f" --seed {seed}" if seed else ""
    return f"brutex verify-acceptance --checks {check_id}{suffix}"
