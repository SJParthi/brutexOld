"""Run orchestration: teeth gate → fenced checks → roll-up → report → exit code.

DEC-OPERATOR-ACCEPTANCE-VERIFY-001 (MIGRATION.md #1797). Any exception in a
check runner → verdict ``ERROR`` with the exception in evidence, the run
CONTINUES (the verifier failing never silently loses the other checks'
evidence) and the overall exit is 2 (SUSPECT). ``TEETH_FAILED`` withholds all
real-lake verdicts. A report write / audit failure is itself SUSPECT.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
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

#: The acceptance run's OWN single-flight lock file name under ``.locks/``.
ACCEPTANCE_SINGLEFLIGHT_LOCK_NAME = "acceptance_singleflight.lock"


def acceptance_lock_path(lake_root: Path) -> Path:
    """``<lake_root>/.locks/acceptance_singleflight.lock`` (never created here).

    Deliberately OUTSIDE ``<lake>/acceptance/`` so the lock is usable even
    when the evidence tree itself is refused (symlink) — the house
    ``.locks/`` sidecar dir the pull/nightly locks share.
    """
    return lake_root / ".locks" / ACCEPTANCE_SINGLEFLIGHT_LOCK_NAME


class AcceptanceLockHeldError(RuntimeError):
    """A second concurrent ``verify-acceptance`` run was refused (B4-1)."""

    def __init__(self, lock_path: Path, holder: str) -> None:
        suffix = f" (holder: {holder})" if holder else ""
        super().__init__(
            "another verify-acceptance run is already writing this lake's "
            f"acceptance evidence (single-flight lock held at {lock_path}){suffix}; "
            "refusing to fork the audit hash chain"
        )


@dataclass
class _AcceptanceRunLock:
    """The acceptance run's cross-process single-flight flock (round-4 B4-1).

    The ``_flock_singleflight`` shape (:mod:`brutex.adapters.groww.\
pull_singleflight` — the pull/nightly/backup precedent): non-blocking
    exclusive ``flock`` on a stable sidecar file, kernel-released on fd
    close / process death (no pid files, no stale-lock state). Two
    overlapping runs previously interleaved appends into the ONE
    ``acceptance.jsonl`` hash chain (``SweepAuditWriter`` is single-writer),
    corrupting it PERMANENTLY — every later ``brutex audit verify`` failed
    and the next run resumed from the torn tail. The second run now REFUSES
    loudly (SUSPECT, exit 2 — documented in the runbook §4) naming the
    holder note the first run wrote.
    """

    fd: int

    @classmethod
    def acquire(cls, lake_root: Path, *, holder_note: str) -> _AcceptanceRunLock:
        """Take the lock or raise (contention → :class:`AcceptanceLockHeldError`)."""
        path = acceptance_lock_path(lake_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        # 0o600 — owner-only private coordination sidecar (house convention).
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            holder = ""
            with contextlib.suppress(OSError):
                holder = path.read_text(encoding="utf-8").strip()
            os.close(fd)
            raise AcceptanceLockHeldError(path, holder) from exc
        except BaseException:
            os.close(fd)
            raise
        with contextlib.suppress(OSError):
            # Best-effort holder note for the contended peer's refusal line
            # (advisory only — a write failure never blocks the acquired lock).
            os.ftruncate(fd, 0)
            os.write(fd, holder_note.encode("utf-8"))
        return cls(fd=fd)

    def release(self) -> None:
        """Close the fd — the kernel drops the advisory lock in the same step."""
        os.close(self.fd)


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
    #: HARD per-check wall-clock deadline (round-5 F1 defense-in-depth). A check
    #: that has not returned within this many seconds is ABANDONED on a daemon
    #: worker thread and recorded ``ABSTAIN(check_timed_out)`` so the run always
    #: finishes + releases the single-flight flock — never an unbounded hang.
    #: ``None`` disables the watchdog (tests that inject a fake client). The
    #: default is deliberately generous: every real-lake check is sub-second, so
    #: a breach is pathological (a wedged store / vendor volume), not slowness.
    check_timeout_s: float | None = 120.0
    force_during_pull: bool = False
    self_test_mode: str = "smoke"  # "smoke" | "full" | "skipped"
    out_dir: Path | None = None
    argv: tuple[str, ...] = ()


def _error_check(check_id: str, exc: BaseException, *, seed: str) -> CheckResult:
    """Build an ERROR ``CheckResult`` from an exception raised by a check.

    Parameters
    ----------
    check_id
        The check identifier that raised.
    exc
        The exception caught while running the check.
    seed
        The run seed, echoed into the reproduce command.

    Returns
    -------
    CheckResult
        A single-subcheck ERROR result carrying the exception summary and a
        bounded traceback tail.
    """
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
    """Build an ABSTAIN ``CheckResult`` for a check skipped on budget.

    Parameters
    ----------
    check_id
        The check identifier that did not run.
    seed
        The run seed, echoed into the reproduce command.

    Returns
    -------
    CheckResult
        A single-subcheck ABSTAIN result with reason
        ``TIME_BUDGET_EXHAUSTED``.
    """
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


def _timeout_check(check_id: str, *, timeout_s: float, seed: str) -> CheckResult:
    """A hung check → ERROR(check_timed_out) → SUSPECT (exit 2), run continues.

    Round-5 F1 defense-in-depth: the check was ABANDONED on a daemon worker
    thread after *timeout_s* so the run always finishes + releases the
    single-flight flock — never an unbounded hang that self-bricks the command.
    ERROR (not ABSTAIN) so the exit is the truthful SUSPECT floor.
    """
    sub = make_subcheck(
        f"{check_id}.timed_out",
        Verdict.ERROR,
        units_checked=0,
        evidence={
            "reason": "check_timed_out",
            "timeout_s": timeout_s,
            "detail": "the check exceeded its hard per-check wall-clock deadline "
            "and was abandoned on a daemon thread so the run could finish + "
            "release the single-flight lock (a wedged store / stalled volume)",
        },
        repro=f"brutex verify-acceptance --checks {check_id} --seed {seed}",
    )
    coverage = make_coverage(mode="none", population=0, examined=0, seed=seed)
    return make_check(check_id, [sub], coverage=coverage)


def _run_one_check(
    check_id: str,
    ctx: AcceptanceContext,
    *,
    seed: str,
    timeout_s: float | None,
) -> CheckResult:
    """Run one check under the HARD per-check watchdog (round-5 F1).

    Runs the registry runner on a daemon worker thread and joins for at most
    *timeout_s*. A breach records :func:`_timeout_check` and returns so the
    outer loop advances (the abandoned thread dies at interpreter exit).
    ``MemoryError`` re-raises raw (house law N3); any other exception is folded
    to an ERROR check by the caller. ``timeout_s=None`` runs inline (no thread).
    """
    if timeout_s is None:
        return CHECK_REGISTRY[check_id](ctx)
    import threading

    box: dict[str, Any] = {}

    def _worker() -> None:
        try:
            box["result"] = CHECK_REGISTRY[check_id](ctx)
        except Exception as exc:  # captured; re-raised on the caller thread
            box["exc"] = exc

    thread = threading.Thread(target=_worker, name=f"acceptance-check-{check_id}", daemon=True)
    thread.start()
    thread.join(timeout_s)
    if thread.is_alive():
        _LOG.error("acceptance.check_timed_out", check_id=check_id, timeout_s=timeout_s)
        return _timeout_check(check_id, timeout_s=timeout_s, seed=seed)
    exc = box.get("exc")
    if exc is not None:
        raise exc
    return box["result"]  # type: ignore[no-any-return]


def _box_block(ctx: AcceptanceContext) -> dict[str, Any]:
    """Build the report's ``box`` provenance block.

    Parameters
    ----------
    ctx
        The acceptance context, queried for pull-active state.

    Returns
    -------
    dict[str, Any]
        Hostname, BruteX version, hardware key (or ``None`` on failure), and
        the current pull-active flag.
    """
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
    """Mint the run id and run the acceptance pass under the run lock.

    Returns
    -------
    tuple[int, dict[str, Any]]
        The process exit code and the machine report payload.
    """
    run_id = report_mod.mint_run_id(clock())

    # Cross-process single-flight (round-4 fix B4-1): two overlapping runs
    # interleaved appends into the ONE acceptance.jsonl hash chain and
    # corrupted it PERMANENTLY. The second run refuses loudly (SUSPECT,
    # exit 2) naming the holder.
    holder_note = f"acceptance:{run_id} pid={os.getpid()} host={socket.gethostname()}"
    try:
        lock = _AcceptanceRunLock.acquire(cfg.lake_root, holder_note=holder_note)
    except AcceptanceLockHeldError as exc:
        _LOG.error("acceptance.singleflight_refused", error=str(exc))
        return 2, {"error": str(exc), "run_id": run_id}
    except OSError as exc:
        _LOG.error("acceptance.singleflight_unavailable", error=str(exc))
        return 2, {"error": f"acceptance single-flight lock unavailable: {exc}", "run_id": run_id}
    try:
        return _run_locked(
            cfg,
            ctx,
            run_id=run_id,
            clock=clock,
            seed_mode=seed_mode,
            started=started,
            selftest_runner=selftest_runner,
        )
    finally:
        lock.release()


def _run_locked(
    cfg: RunConfig,
    ctx: AcceptanceContext,
    *,
    run_id: str,
    clock: Callable[[], datetime],
    seed_mode: str,
    started: float,
    selftest_runner: Callable[..., selftest.SelfTestResult] | None,
) -> tuple[int, dict[str, Any]]:
    """Run the acceptance pass with the evidence-tree symlink refusal held.

    Returns
    -------
    tuple[int, dict[str, Any]]
        The process exit code and the machine report payload.
    """
    # ONE evidence-tree symlink refusal covering EVERY write (round-3 fix
    # B-NEW-1): the round-2 guard sat at report-write time, so the audit
    # chain (and the coverage ledger's warn-only refusal) had already been
    # reached with <lake>/acceptance a symlink — the audit JSONL was written
    # THROUGH the link into the target dir. Hoisted here, BEFORE any write
    # (house .quarantine precedent — refuse the whole run loudly, SUSPECT).
    # Round-4 fix (A-F1): the refusal covers every FIXED, predictable child
    # path the run writes THROUGH too — a symlinked <lake>/acceptance/audit
    # dir (or the chain file itself) previously redirected the hash-chained
    # audit JSONL off-lake while the top-level guard read clean. The
    # <run_id> dir is nonce-named (unplantable) and LATEST / the coverage
    # ledger are atomic os.replace targets (the link is REPLACED, never
    # followed), so the fixed write-THROUGH set is exactly these three.
    base = report_mod.acceptance_dir(cfg.lake_root)
    audit_file = report_mod.audit_path(cfg.lake_root)
    for probe in (base, audit_file.parent, audit_file):
        if probe.is_symlink():
            _LOG.error("acceptance.evidence_tree_symlink_refused", path=str(probe))
            return 2, {
                "error": f"{probe} is a symlink — refusing to write the evidence tree through it",
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
    """Run the teeth self-test then the composed checks under the audit chain.

    Returns
    -------
    tuple[int, dict[str, Any]]
        The process exit code and the machine report payload.
    """
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
                result = _run_one_check(check_id, ctx, seed=ctx.seed, timeout_s=cfg.check_timeout_s)
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

    # Round-4 fix (B4-4): sub-checks that did NOT run this run (a --checks
    # subset, or a TEETH_FAILED run's withheld checks) carry their previous
    # (verdict, streak) forward VERBATIM — an interleaved subset/teeth-failed
    # run can no longer silently reset every other sub-check's abstain
    # streak and suppress the streak-≥3 ⇒ exit-4 law indefinitely.
    streak_carry = report_mod.streak_carry_block(prev_index, checks)

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
        "streak_carry": streak_carry,
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
