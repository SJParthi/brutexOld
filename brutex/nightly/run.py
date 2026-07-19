"""Nightly composition root — locks → keep-awake → audit → chain → report → Telegram
(DEC-SWEEP-NIGHTLY-AUTOMATION-001).

Every external effect is injected through :class:`NightlySeams` (all defaults
are production), so the FULL composition — lock acquisition order, the account
lock's post-PREBUILD release, the pull-halt alert, the report/bundle-in-
``finally`` law, the Telegram emits — is unit-testable offline.

Collision law (spec §7.2; DEC-SWEEP-LOCK-PULL-CONCURRENCY-001): the nightly
single-flight lock is taken FIRST (pre-chain; contention ⇒ refusal report +
``nightly_refused`` + ``EXIT_USER_ERROR``, no chain); the COLLISION link then
acquires the ACCOUNT pull lock AND the LAKE pull lock — BOTH released after
PULL+PREBUILD (no vendor calls happen during the sweep, so the account lock
never starves a different-lake pull on the same account; a manual pull
attempted mid-PULL reads ``(holder: nightly:<id>)``) — then runs the
sweep-liveness heuristic. The SWEEP link runs with the lake lock RELEASED:
its sweep-vs-pull exclusion is owned by ``cmd_sweep_all`` itself — the G10
lake read-guard (``LOCK_SH`` on the same pull lock file, held across the bar
PRELOAD only; :mod:`brutex.engine.sweep_singleflight`) + the G6 sweep
single-flight (``sweep_run_lock``, DEC-SWEEP-UNKILLABLE-CORE-001) — so the
daily pull / crossverify can run CONCURRENTLY with the sweep's multi-day
compute phase. (The pre-G10 acquire-and-HOLD design — its "no sweep-level
flock exists" rationale — is SUPERSEDED: holding the lake lock EX through the
SWEEP link self-conflicted with cmd_sweep_all's own G10 SH guard, flock being
per open file description → ``LakeBusyError`` at every nightly sweep preload.)

``--dry-run`` is the documented post-install smoke: probes + inventory + plan
+ report ONLY — takes NO locks and mutates NOTHING (the report is RETURNED for
the CLI to print, never written; the mutating links read SKIPPED). The
pipeline is deliberately NOT invoked even with ``dry_run=True``: the dashboard
redirects a dry-run pull to an EPHEMERAL lake root precisely because the
dry-run spines still write synthetic bars — running them against the real lake
would violate the tree-oracle contract (recorded deviation from the build
spec, phase6-deviations.md).
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import time
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from brutex.data.guarded_read import read_regular_text
from brutex.nightly.backup import (
    MANIFEST_NAME,
    BackupManifest,
    backup_inventory,
    create_snapshot,
    estimate_backup_bytes,
)
from brutex.nightly.chain import (
    EXIT_SWEEP_TUPLE_FAILED,
    REPORT_SCHEMA_VERSION,
    ChainStep,
    LinkOutcome,
    LinkResult,
    LinkStatus,
    NightlyLink,
    NightlyReport,
    Tripwire,
    audit_reconcile_tripwire,
    first_breaker,
    run_chain,
)
from brutex.nightly.pipeline_binding import (
    NightlyCapabilityError,
    build_headless_pipeline,
    decompose_pipeline_report,
    execute_headless_pipeline,
)
from brutex.nightly.probes import (
    check_disk_floors,
    probe_sweep_liveness,
    resolve_sweep_fresh_seconds,
)
from brutex.nightly.report import (
    mint_nightly_id,
    nightly_audit_path,
    open_nightly_audit,
    summary_lines,
    write_bundle,
    write_report,
)
from brutex.nightly.sweep_binding import (
    probe_contract_artifacts,
    read_sweep_tail_counts,
    sweep_link_result,
    sweep_supervisor_audit_path,
)
from brutex.observability.power_keepawake import keep_awake

if TYPE_CHECKING:
    from brutex.nightly.pipeline_binding import HeadlessPipeline
    from brutex.notifications.sweep_alerts import SweepAlerter

logger = structlog.get_logger(__name__)

__all__ = ["NightlyPlan", "NightlySeams", "run_nightly"]

#: The verbatim R48 divergence sentence (spec §12 item 1 — carried on EVERY report).
R48_DIVERGENCE_NOTE = (
    "R48 decay diff NOT built — deferred to v1.1 by the explicit operator phase "
    "directive; a recorded DIVERGENCE from the living requirements (which home "
    "R48 (A) in P6; the file's v1.1 item is R49). The P3 top100.json probe is "
    "the v1.1 seam."
)

#: The verified SUPERVISOR_ACTION_COMPLETE identity (T5): total == the five parts.
_SWEEP_IDENTITY_PARTS = (
    "tuples_run",
    "skipped_cache",
    "skipped_resume",
    "tuples_skipped_no_bars",
    "tuples_failed",
)


@dataclass(frozen=True, slots=True)
class NightlyPlan:
    """The resolved, validated inputs of one nightly run (built by the CLI)."""

    lake_root: Path
    results_root: Path  # default <lake_root>/sweeps (the cmd_sweep_all law)
    backup_root: Path  # default env BRUTEX_NIGHTLY_BACKUP_ROOT else ~/.brutex/backups
    keep: int  # default 7
    from_date: str | None
    to_date: str | None
    sweep_kwargs: Mapping[str, object]  # frozen output of nightly_sweep_kwargs
    skip_backup: bool = False
    skip_sweep: bool = False
    dry_run: bool = False
    #: The pre-chain box-update leg's outcome block (DEC-BOX-AUTODEPLOY-LANE-001
    #: §5/§6 step 5 — threaded at CONSTRUCTION, the plan is frozen); ``None`` =
    #: the leg never ran (disabled kill switch pre-resolution / dry-run / no leg).
    box_update: Mapping[str, object] | None = None
    #: The §12 env-preflight block; ``None`` = the pre-flight never ran.
    env_preflight: Mapping[str, object] | None = None


def _default_keep_awake(reason: str) -> AbstractContextManager[bool]:
    """Return a no-op keep-awake context manager for the given reason."""
    return keep_awake(reason)


@dataclass(frozen=True, slots=True)
class NightlySeams:
    """Every external effect, injectable; ALL defaults are production."""

    build_pipeline: Callable[..., HeadlessPipeline] = build_headless_pipeline
    run_sweep: Callable[..., int] | None = None  # None → cli.cmd_sweep_all (call-time import)
    alerter: SweepAlerter | None = None  # None → SweepAlerter() lazy singleton
    keep_awake_cm: Callable[[str], AbstractContextManager[bool]] = _default_keep_awake
    credentials: Callable[[], Any] | None = None  # None → resolve_groww_aws_credentials
    copy_file: Callable[[Path, Path], None] | None = None
    disk_usage: Callable[[str], Any] = shutil.disk_usage
    now: Callable[[], float] = time.time


def _resolve_alerter(seams: NightlySeams) -> SweepAlerter:
    """Resolve the SweepAlerter from the nightly seams."""
    if seams.alerter is not None:
        return seams.alerter
    from brutex.notifications.sweep_alerts import SweepAlerter

    return SweepAlerter()


def _resolve_run_sweep(seams: NightlySeams) -> Callable[..., int]:
    """Resolve the sweep-runner callable from the nightly seams."""
    if seams.run_sweep is not None:
        return seams.run_sweep
    from brutex.cli import cmd_sweep_all

    return cmd_sweep_all


def _counts_from_links(
    outcomes: tuple[LinkOutcome, ...],
) -> dict[str, int | None]:
    """Fold link counts into the report's fixed count keys (absent ⇒ None)."""
    by_link: dict[NightlyLink, Mapping[str, int]] = {o.link: o.counts for o in outcomes}

    def pick(link: NightlyLink, key: str) -> int | None:
        return dict(by_link.get(link, {})).get(key)

    return {
        "pull_bars": pick(NightlyLink.PULL, "pull_bars"),
        "pull_quarantined": pick(NightlyLink.PULL, "pull_quarantined"),
        "prebuild_built": pick(NightlyLink.PREBUILD, "prebuild_built"),
        "sweep_run": pick(NightlyLink.SWEEP, "tuples_run"),
        "sweep_skipped_cache": pick(NightlyLink.SWEEP, "skipped_cache"),
        "sweep_skipped_resume": pick(NightlyLink.SWEEP, "skipped_resume"),
        "sweep_failed": pick(NightlyLink.SWEEP, "tuples_failed"),
        "backup_files": pick(NightlyLink.BACKUP, "backup_files"),
        "backup_bytes": pick(NightlyLink.BACKUP, "backup_bytes"),
    }


def _link_verdicts(outcomes: tuple[LinkOutcome, ...]) -> dict[str, str]:
    """Map each chain link name to its verdict string."""
    verdicts: dict[str, str] = {}
    for outcome in outcomes:
        text = outcome.status.value
        if outcome.status is LinkStatus.BLOCKED and outcome.blocked_by is not None:
            text = f"blocked({outcome.blocked_by.value})"
        verdicts[outcome.link.value] = text
    return verdicts


def run_nightly(plan: NightlyPlan, *, seams: NightlySeams | None = None) -> NightlyReport:
    """Run the whole nightly chain; ALWAYS returns a :class:`NightlyReport`.

    The report + bundle are written in a ``finally`` (a raise between links
    still leaves the durable record); the exit code is
    :attr:`NightlyReport.exit_code` — the CLI's cron-visible surface.
    KeyboardInterrupt/SystemExit propagate raw AFTER the report lands.
    """
    seams = seams or NightlySeams()
    alerter = _resolve_alerter(seams)
    nightly_id = mint_nightly_id(now=seams.now)
    started = seams.now()

    if plan.dry_run:
        return _run_dry(plan, seams, nightly_id=nightly_id, started=started)

    from brutex.adapters.groww.pull_singleflight import (
        NightlyAlreadyRunningError,
        nightly_singleflight_lock,
    )

    with ExitStack() as locks:
        try:
            locks.enter_context(
                nightly_singleflight_lock(plan.lake_root, holder_note=f"nightly:{nightly_id}")
            )
        except NightlyAlreadyRunningError as exc:
            return _refusal_report(
                plan,
                seams,
                alerter,
                nightly_id=nightly_id,
                started=started,
                detail=f"nightly_already_running: {exc}",
            )
        with seams.keep_awake_cm("brutex nightly"):
            return _run_locked(plan, seams, alerter, locks, nightly_id=nightly_id, started=started)


def _refusal_report(
    plan: NightlyPlan,
    seams: NightlySeams,
    alerter: SweepAlerter,
    *,
    nightly_id: str,
    started: float,
    detail: str,
) -> NightlyReport:
    """The no-chain COLLISION refusal: report + instant Telegram, nothing touched."""
    ended = seams.now()
    outcomes = [
        LinkOutcome(
            link=NightlyLink.COLLISION,
            status=LinkStatus.REFUSED,
            started_utc=started,
            ended_utc=ended,
            detail=detail,
            counts={},
            error_type=None,
            blocked_by=None,
            exit_hint=None,
        )
    ]
    for link in (
        NightlyLink.DISK,
        NightlyLink.BACKUP,
        NightlyLink.PULL,
        NightlyLink.PREBUILD,
        NightlyLink.SWEEP,
    ):
        outcomes.append(
            LinkOutcome(
                link=link,
                status=LinkStatus.BLOCKED,
                started_utc=None,
                ended_utc=None,
                detail="REFUSED-BECAUSE-collision refused",
                counts={},
                error_type=None,
                blocked_by=NightlyLink.COLLISION,
                exit_hint=None,
            )
        )
    report = _assemble_report(
        plan,
        nightly_id=nightly_id,
        started=started,
        ended=ended,
        links=tuple(outcomes),
        tripwires=(),
        snapshot_id=None,
        absent_artifacts=(),
        extra_notes=(),
    )
    with contextlib.suppress(OSError):
        write_report(plan.results_root, report)
    alerter.nightly_refused(nightly_id=nightly_id, link="collision", reason=detail)
    _emit_summary(alerter, report, seams)
    return report


def _run_locked(
    # small named closure and the branch surface is pinned by the e2e suite.
    plan: NightlyPlan,
    seams: NightlySeams,
    alerter: SweepAlerter,
    locks: ExitStack,
    *,
    nightly_id: str,
    started: float,
) -> NightlyReport:
    """Run the nightly chain while holding the required single-flight locks."""
    from brutex.adapters.groww.pull_singleflight import pull_singleflight_lock
    from brutex.dashboard.pull_jobs import is_pull_halt
    from brutex.notifications.sweep_alerts import emit_pull_halt_alert

    audit = open_nightly_audit(plan.results_root)
    locks.callback(audit.close)
    audit.append(
        action="NIGHTLY_START",
        subject=f"nightly:{nightly_id}",
        payload={"lake_root": str(plan.lake_root), "dry_run": plan.dry_run},
    )

    extra_tripwires: list[Tripwire] = []
    account_stack = ExitStack()
    locks.callback(account_stack.close)  # backstop: released even if PULL never runs
    # The LAKE pull lock rides the SAME post-PULL+PREBUILD release discipline
    # as the account lock (DEC-SWEEP-LOCK-PULL-CONCURRENCY-001): held through
    # PULL+PREBUILD only, closed in pull_fn's ``finally``; the SWEEP leg's
    # sweep-vs-pull exclusion is cmd_sweep_all's own G10 (preload-only SH on
    # the same file — an EX still held here would self-conflict it) + G6.
    lake_stack = ExitStack()
    locks.callback(lake_stack.close)  # backstop: released even if PULL never runs
    inventory_bytes = 0
    snapshot_id: str | None = None
    absent_artifacts: tuple[str, ...] = ()
    extra_notes: list[str] = []

    def on_event(event: str, payload: Mapping[str, object]) -> None:
        # An audit-append failure (SweepAuditWriteError on a mid-chain full
        # disk / closed writer) must NEVER escape: ``run_chain`` emits these
        # events OUTSIDE its per-link try, so an unguarded raise aborted the
        # chain with EMPTY outcomes and the ``finally`` assembled a fabricated
        # all-green "✅ OK" morning Telegram (review round 6, MAJOR — Golden
        # Rule #1). The lost line degrades to SUSPECT instead: ONE direct
        # ``audit_reconcile`` tripwire per failed append (the T2 disk
        # read-back is subset-based on LINK_START, so a lost START alone
        # would otherwise reconcile clean) — and T2 still catches lost
        # DONE/BLOCKED lines independently.
        try:
            audit.append(action=event, subject=f"nightly:{nightly_id}", payload=dict(payload))
        except Exception as exc:
            logger.warning(
                "nightly.audit_append_failed",
                audit_event=event,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            extra_tripwires.append(
                Tripwire(
                    name="audit_reconcile",
                    link=None,
                    detail=f"audit append failed for {event}: {str(exc)[:200]}",
                )
            )
        # Instant refusal signal at the collision/disk gates (spec §9).
        if (
            event == "LINK_DONE"
            and payload.get("status") == "refused"
            and payload.get("link") in ("collision", "disk")
        ):
            alerter.nightly_refused(
                nightly_id=nightly_id,
                link=str(payload.get("link")),
                reason=str(payload.get("detail", "")),
            )

    def collision_fn() -> LinkResult:
        from brutex.adapters.groww.pull_singleflight import PullAlreadyRunningError
        from brutex.config.credentials_types import CredentialsError

        locks_taken = 1  # the nightly lock (already held)
        try:
            creds = seams.credentials() if seams.credentials is not None else _resolve_credentials()
        except CredentialsError as exc:
            logger.warning("nightly.account_lock_skipped", reason=str(exc))
        else:
            from brutex.adapters.groww.pull_singleflight import account_pull_singleflight_lock

            try:
                account_stack.enter_context(account_pull_singleflight_lock(creds.api_key))
            except PullAlreadyRunningError as exc:
                return LinkResult(status=LinkStatus.REFUSED, detail=f"pull_in_flight: {exc}")
            locks_taken += 1
        try:
            lake_stack.enter_context(
                pull_singleflight_lock(plan.lake_root, holder_note=f"nightly:{nightly_id}")
            )
        except PullAlreadyRunningError as exc:
            return LinkResult(status=LinkStatus.REFUSED, detail=f"pull_in_flight: {exc}")
        locks_taken += 1
        liveness = probe_sweep_liveness(
            plan.results_root, fresh_seconds=resolve_sweep_fresh_seconds(), now=seams.now
        )
        if liveness.live:
            return LinkResult(status=LinkStatus.REFUSED, detail=f"sweep_alive({liveness.evidence})")
        return LinkResult(
            status=LinkStatus.OK,
            detail="locks held; no live sweep detected (heuristic)",
            counts={"locks": locks_taken},
        )

    def disk_fn() -> LinkResult:
        nonlocal inventory_bytes
        items, _skipped = backup_inventory(
            plan.lake_root, plan.results_root, exclude_nightly_id=nightly_id
        )
        inventory_bytes = estimate_backup_bytes(items)
        verdict = check_disk_floors(
            lake_root=plan.lake_root,
            backup_root=plan.backup_root,
            inventory_bytes=inventory_bytes,
            disk_usage=seams.disk_usage,
        )
        counts = {"inventory_bytes": inventory_bytes, "backup_required": verdict.backup_required}
        if not verdict.ok:
            return LinkResult(status=LinkStatus.REFUSED, detail=verdict.detail, counts=counts)
        return LinkResult(status=LinkStatus.OK, detail=verdict.detail, counts=counts)

    def backup_fn() -> LinkResult:
        nonlocal snapshot_id
        if plan.skip_backup:
            return LinkResult(status=LinkStatus.SKIPPED, detail="operator --skip-backup")
        result = create_snapshot(
            lake_root=plan.lake_root,
            results_root=plan.results_root,
            backup_root=plan.backup_root,
            snapshot_id=nightly_id,
            nightly_id=nightly_id,
            copy_file=seams.copy_file,
            keep=plan.keep,
            now=seams.now,
        )
        snapshot_id = result.snapshot_id
        audit.append(
            action="BACKUP_SNAPSHOT",
            subject=f"nightly:{nightly_id}",
            payload={"snapshot": str(result.snapshot_path), "files": result.file_count},
        )
        # T4 snapshot_unreadable: re-read + parse the sealed manifest, re-stat
        # the item count (the FULL hash verify already ran pre-seal). Exclude
        # ONLY the snapshot's OWN top-level manifest — NESTED manifest.json
        # files (results/cascade/, results/runs/<id>/) are REAL backup entries
        # counted in item_count; excluding them by basename undercounted the
        # disk and fired a FALSE tripwire every healthy night (review round 2).
        try:
            own_manifest = result.snapshot_path / MANIFEST_NAME
            manifest = BackupManifest.from_dict(json.loads(read_regular_text(own_manifest)))
            on_disk = sum(
                1 for p in result.snapshot_path.rglob("*") if p.is_file() and p != own_manifest
            )
            if manifest.item_count != on_disk:
                extra_tripwires.append(
                    Tripwire(
                        name="snapshot_unreadable",
                        link=NightlyLink.BACKUP,
                        detail=(
                            f"sealed snapshot re-stat: manifest says "
                            f"{manifest.item_count} file(s), disk has {on_disk}"
                        ),
                    )
                )
        except Exception as exc:
            extra_tripwires.append(
                Tripwire(
                    name="snapshot_unreadable",
                    link=NightlyLink.BACKUP,
                    detail=f"sealed manifest re-read failed: {exc}",
                )
            )
        return LinkResult(
            status=LinkStatus.OK,
            detail=f"snapshot {result.snapshot_id} sealed"
            + (f"; pruned {len(result.pruned)}" if result.pruned else ""),
            counts={"backup_files": result.file_count, "backup_bytes": result.total_bytes},
        )

    def pull_fn() -> LinkResult:
        try:
            pipeline = seams.build_pipeline(
                lake_root=plan.lake_root,
                dry_run=plan.dry_run,
                from_date=plan.from_date,
                to_date=plan.to_date,
            )
        except NightlyCapabilityError as exc:
            account_stack.close()
            lake_stack.close()
            return LinkResult(status=LinkStatus.REFUSED, detail=str(exc))
        phase_started = seams.now()
        try:
            pipe_report = execute_headless_pipeline(pipeline)
        except BaseException as exc:
            if isinstance(exc, Exception) and is_pull_halt(exc):
                # The dedicated CRITICAL halt alert fires BEFORE the chain's
                # FAILED conversion (the _one_fetch_pass pattern).
                emit_pull_halt_alert(exc)
            raise
        finally:
            # BOTH pull locks release after PULL+PREBUILD (spec §7.2 +
            # DEC-SWEEP-LOCK-PULL-CONCURRENCY-001): no vendor calls happen
            # during the sweep, and the SWEEP leg's sweep-vs-pull exclusion is
            # owned by cmd_sweep_all's own G10 preload read-guard (an EX still
            # held HERE would self-conflict its SH acquire) + the G6 sweep
            # single-flight — so a pull/crossverify can run concurrently with
            # the sweep's compute phase.
            account_stack.close()
            lake_stack.close()
            pipeline.close()
        result, t3 = decompose_pipeline_report(
            pipe_report, started_utc=phase_started, ended_utc=seams.now()
        )
        extra_tripwires.extend(t3)
        return result

    def sweep_fn() -> LinkResult:
        if plan.skip_sweep:
            return LinkResult(status=LinkStatus.SKIPPED, detail="operator --skip-sweep")
        nonlocal absent_artifacts
        run_sweep = _resolve_run_sweep(seams)
        since = seams.now()
        # The counts live in the SUPERVISOR audit chain (SweepGuards.audit →
        # sweep-all-supervisor.jsonl) — NEVER the ingest sweep-all.jsonl,
        # which carries no completion payload (review round 2, finding 1).
        # That chain is APPEND-across-runs, so the read is freshness-gated on
        # the pre-sweep `since` (the same guard the contract probe already
        # rode): a refused/aborted/crashed sweep writes no NEW completion and
        # must read honest-None, never a PRIOR night's tally presented as
        # tonight's (review round 3, findings 1+2).
        audit_path = sweep_supervisor_audit_path(plan.results_root)
        had_prior_audit = audit_path.exists()
        code = int(run_sweep(**dict(plan.sweep_kwargs)))
        counts = read_sweep_tail_counts(audit_path, since_utc=since)
        probe = probe_contract_artifacts(plan.results_root, since_utc=since)
        absent_artifacts = probe.absent
        if "run_manifest" in probe.found:
            with contextlib.suppress(OSError, ValueError, TypeError, KeyError):
                raw_manifest = read_regular_text(Path(probe.paths["run_manifest"]))
                manifest = json.loads(raw_manifest)
                dedup = manifest["dedup_skips"]
                if isinstance(dedup, int) and counts is not None:
                    counts = {**counts, "skipped_dedup_manifest": dedup}
        if counts is None:
            extra_notes.append(
                "sweep counts unknown — no fresh SUPERVISOR_COMPLETE payload for "
                "THIS run readable (a prior run's tail tally is never reported "
                "as tonight's)"
            )
        else:
            _check_sweep_counts(code, counts)
            if (
                had_prior_audit
                and counts.get("skipped_cache") == 0
                and counts.get("tuples_total", 0) > 0
            ):
                extra_notes.append(
                    "skipped_cache == 0 with a prior sweep audit present — possible "
                    "commit re-key (expected after a deploy; the tuple identity "
                    "folds code_commit_sha)"
                )
        return sweep_link_result(code, audit_path=audit_path, counts=counts)

    def _check_sweep_counts(code: int, counts: Mapping[str, int]) -> None:
        """T5 sweep_counts_incoherent — only when the payload is readable."""
        if "tuples_total" in counts and all(part in counts for part in _SWEEP_IDENTITY_PARTS):
            total = sum(counts[part] for part in _SWEEP_IDENTITY_PARTS)
            if counts["tuples_total"] != total:
                extra_tripwires.append(
                    Tripwire(
                        name="sweep_counts_incoherent",
                        link=NightlyLink.SWEEP,
                        detail=f"tuples_total {counts['tuples_total']} != parts sum {total}",
                    )
                )
        failed = counts.get("tuples_failed")
        if failed is not None and ((code == EXIT_SWEEP_TUPLE_FAILED) != (failed > 0)):
            extra_tripwires.append(
                Tripwire(
                    name="sweep_counts_incoherent",
                    link=NightlyLink.SWEEP,
                    detail=f"exit=={code} but tuples_failed=={failed} (6 iff failed>0)",
                )
            )

    steps = [
        ChainStep(link=NightlyLink.COLLISION, fn=collision_fn),
        ChainStep(link=NightlyLink.DISK, fn=disk_fn),
        ChainStep(link=NightlyLink.BACKUP, fn=backup_fn),
        ChainStep(link=NightlyLink.PULL, fn=pull_fn, also_declares=(NightlyLink.PREBUILD,)),
        ChainStep(link=NightlyLink.SWEEP, fn=sweep_fn),
    ]

    outcomes: tuple[LinkOutcome, ...] = ()
    tripwires: tuple[Tripwire, ...] = ()
    try:
        try:
            outcomes, tripwires = run_chain(
                nightly_id=nightly_id, steps=steps, on_event=on_event, now=seams.now
            )
        except Exception as exc:
            # Belt: run_chain's contract is never-raise (only KeyboardInterrupt
            # / SystemExit propagate raw). A raise here means the composition
            # itself is defective and the chain state is UNKNOWN — record a
            # tripwire so the ``finally``'s report reads SUSPECT (exit >= 2),
            # never the vacuous all-green "OK" the empty initial tuples would
            # otherwise assemble, and RETURN that report (the run_nightly
            # "ALWAYS returns a NightlyReport" contract) instead of escaping
            # past ``cmd_nightly_run`` (review round 6, MAJOR).
            logger.error("nightly.chain_aborted", error_type=type(exc).__name__, error=str(exc))
            extra_tripwires.append(
                Tripwire(
                    name="link_transition",
                    link=None,
                    detail=(
                        f"run_chain raised {type(exc).__name__}: {str(exc)[:200]} — "
                        "chain state unknown; no link outcomes recorded"
                    ),
                )
            )
        else:
            # T2 reconciles the DURABLE audit (read back from disk — a lost/torn
            # append is exactly what it exists to catch), never an in-memory echo.
            t2 = audit_reconcile_tripwire(
                nightly_id=nightly_id,
                events=_own_audit_events(nightly_audit_path(plan.results_root), nightly_id),
                outcomes=outcomes,
            )
            if t2 is not None:
                extra_tripwires.append(t2)
    finally:
        if not outcomes and not tripwires and not extra_tripwires:
            # Belt: an aborted chain (KeyboardInterrupt/SystemExit mid-link, or
            # any future escape) must never assemble the empty initial tuples
            # into a vacuous all-green OK report (Golden Rule #1).
            extra_tripwires.append(
                Tripwire(
                    name="link_transition",
                    link=None,
                    detail="chain aborted before any link outcome was recorded",
                )
            )
        all_tripwires = tuple(tripwires) + tuple(extra_tripwires)
        for wire in extra_tripwires:
            with contextlib.suppress(Exception):
                audit.append(
                    action="TRIPWIRE", subject=f"nightly:{nightly_id}", payload=wire.to_dict()
                )
        report = _assemble_report(
            plan,
            nightly_id=nightly_id,
            started=started,
            ended=seams.now(),
            links=outcomes,
            tripwires=all_tripwires,
            snapshot_id=snapshot_id,
            absent_artifacts=absent_artifacts,
            extra_notes=tuple(extra_notes),
        )
        with contextlib.suppress(OSError):
            write_report(plan.results_root, report)
            write_bundle(
                plan.results_root,
                report,
                # The forensic tail the report's own details reference — the
                # SUPERVISOR chain (TUPLE_FAILED lines + completion tally),
                # not the ingest sweep-all.jsonl (review round 2).
                sweep_audit_path=sweep_supervisor_audit_path(plan.results_root),
                nightly_audit_path=nightly_audit_path(plan.results_root),
                progress_path=plan.results_root / "progress" / "sweep-progress.json",
                env=_bundle_env(),
            )
        with contextlib.suppress(Exception):
            audit.append(
                action="NIGHTLY_DONE",
                subject=f"nightly:{nightly_id}",
                payload={"exit_code": report.exit_code, "ok": report.ok},
            )
        _emit_summary(alerter, report, seams)
    return report


def _own_audit_events(path: Path, nightly_id: str) -> list[tuple[str, str | None]]:
    """Read THIS run's audit events back from disk for the T2 reconcile.

    Best-effort parse (a torn line is skipped — the reconcile then honestly
    mismatches); only entries whose subject is this ``nightly:<id>`` count.
    """
    events: list[tuple[str, str | None]] = []
    with contextlib.suppress(OSError):
        for line in read_regular_text(path).splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict) or entry.get("subject") != f"nightly:{nightly_id}":
                continue
            payload = entry.get("payload")
            link = payload.get("link") if isinstance(payload, dict) else None
            events.append((str(entry.get("action")), str(link) if isinstance(link, str) else None))
    return events


def _resolve_credentials() -> Any:
    """Resolve Groww credentials for the nightly pull, if configured."""
    from brutex.config.credentials import resolve_groww_aws_credentials

    return resolve_groww_aws_credentials()


def _bundle_env() -> Mapping[str, str]:
    """Return the allowlisted environment snapshot for the run bundle."""
    return dict(os.environ)


def _assemble_report(
    plan: NightlyPlan,
    *,
    nightly_id: str,
    started: float,
    ended: float,
    links: tuple[LinkOutcome, ...],
    tripwires: tuple[Tripwire, ...],
    snapshot_id: str | None,
    absent_artifacts: tuple[str, ...],
    extra_notes: tuple[str, ...],
) -> NightlyReport:
    """Assemble the NightlyReport from the collected link outcomes.

    The box-update / env-preflight blocks ride the PLAN (construction-time
    threading, DEC-BOX-AUTODEPLOY-LANE-001 §6 step 5) into the report; a
    box-update status outside ``{up_to_date, disabled}`` appends the ONE
    §5 summary line via the honesty-notes mechanism (the cheapest carrier —
    zero new alert methods).
    """
    notes = [R48_DIVERGENCE_NOTE, *extra_notes]
    box_note = _box_update_note(plan.box_update)
    if box_note is not None:
        notes.append(box_note)
    return NightlyReport(
        schema_version=REPORT_SCHEMA_VERSION,
        nightly_id=nightly_id,
        lake_root=str(plan.lake_root),
        results_root=str(plan.results_root),
        backup_root=str(plan.backup_root),
        started_utc=started,
        ended_utc=ended,
        links=links,
        tripwires=tripwires,
        counts=_counts_from_links(links),
        snapshot_id=snapshot_id,
        absent_artifacts=absent_artifacts,
        honesty_notes=tuple(notes),
        dry_run=plan.dry_run,
        box_update=plan.box_update,
        env_preflight=plan.env_preflight,
    )


def _box_update_note(block: Mapping[str, object] | None) -> str | None:
    """The §5 one-line box-update summary (silent on up_to_date/disabled/absent)."""
    if block is None:
        return None
    status = str(block.get("status", "unknown"))
    if status in ("up_to_date", "disabled"):
        return None

    def _sha7(key: str) -> str:
        value = block.get(key)
        return str(value)[:7] if isinstance(value, str) and value else "?"

    wheels = block.get("wheels")
    wheel_list = wheels if isinstance(wheels, list) else []
    ok_count = sum(1 for w in wheel_list if isinstance(w, dict) and w.get("result") == "ok")
    gate = str(block.get("gate", "skipped"))
    return (
        f"📦 box-update: {status} {_sha7('from_sha')}→{_sha7('to_sha')} · "
        f"wheels ok {ok_count}/{len(wheel_list)} · gate {gate}"
    )


def _emit_summary(alerter: SweepAlerter, report: NightlyReport, seams: NightlySeams) -> None:
    """Unconditional chain-end summary (the complete record; refusals also
    already fired the instant ``nightly_refused``)."""
    _lines, verdict = summary_lines(report)
    breaker = first_breaker(report.links)
    counts = report.counts
    with contextlib.suppress(Exception):
        alerter.nightly_summary(
            nightly_id=report.nightly_id,
            verdict=verdict,
            link_verdicts=_link_verdicts(report.links),
            pull_bars=counts.get("pull_bars"),
            prebuild_built=counts.get("prebuild_built"),
            sweep_run=counts.get("sweep_run"),
            sweep_skipped_cache=counts.get("sweep_skipped_cache"),
            sweep_failed=counts.get("sweep_failed"),
            backup_files=counts.get("backup_files"),
            blocker=None if breaker is None else breaker.link.value,
            duration_seconds=max(0.0, report.ended_utc - report.started_utc),
        )


def _run_dry(
    plan: NightlyPlan, seams: NightlySeams, *, nightly_id: str, started: float
) -> NightlyReport:
    """The documented post-install smoke: probes + inventory + plan, ZERO mutation.

    Takes NO locks, writes NO report/audit/bundle (the report is RETURNED for
    the CLI to print — the tree-oracle contract), and SKIPS every mutating
    link. The liveness + disk probes run for real (read-only).
    """

    def collision_fn() -> LinkResult:
        liveness = probe_sweep_liveness(
            plan.results_root, fresh_seconds=resolve_sweep_fresh_seconds(), now=seams.now
        )
        if liveness.live:
            return LinkResult(status=LinkStatus.REFUSED, detail=f"sweep_alive({liveness.evidence})")
        return LinkResult(
            status=LinkStatus.OK, detail="dry-run — locks not taken; no live sweep detected"
        )

    inventory_bytes = 0

    def disk_fn() -> LinkResult:
        nonlocal inventory_bytes
        items, _skipped = backup_inventory(
            plan.lake_root, plan.results_root, exclude_nightly_id=nightly_id
        )
        inventory_bytes = estimate_backup_bytes(items)
        verdict = check_disk_floors(
            lake_root=plan.lake_root,
            backup_root=plan.backup_root,
            inventory_bytes=inventory_bytes,
            disk_usage=seams.disk_usage,
        )
        counts = {"inventory_bytes": inventory_bytes, "backup_required": verdict.backup_required}
        if not verdict.ok:
            return LinkResult(status=LinkStatus.REFUSED, detail=verdict.detail, counts=counts)
        return LinkResult(status=LinkStatus.OK, detail=verdict.detail, counts=counts)

    def skipped(detail: str, *, extras: tuple[NightlyLink, ...] = ()) -> Callable[[], LinkResult]:
        def fn() -> LinkResult:
            extra_outcomes = tuple(
                LinkOutcome(
                    link=link,
                    status=LinkStatus.SKIPPED,
                    started_utc=seams.now(),
                    ended_utc=seams.now(),
                    detail=detail,
                    counts={},
                    error_type=None,
                    blocked_by=None,
                    exit_hint=None,
                )
                for link in extras
            )
            return LinkResult(
                status=LinkStatus.SKIPPED, detail=detail, extra_outcomes=extra_outcomes
            )

        return fn

    steps = [
        ChainStep(link=NightlyLink.COLLISION, fn=collision_fn),
        ChainStep(link=NightlyLink.DISK, fn=disk_fn),
        ChainStep(link=NightlyLink.BACKUP, fn=skipped("dry-run")),
        ChainStep(
            link=NightlyLink.PULL,
            fn=skipped("dry-run", extras=(NightlyLink.PREBUILD,)),
            also_declares=(NightlyLink.PREBUILD,),
        ),
        ChainStep(link=NightlyLink.SWEEP, fn=skipped("dry-run")),
    ]
    outcomes, tripwires = run_chain(nightly_id=nightly_id, steps=steps, now=seams.now)
    report = _assemble_report(
        plan,
        nightly_id=nightly_id,
        started=started,
        ended=seams.now(),
        links=outcomes,
        tripwires=tripwires,
        snapshot_id=None,
        absent_artifacts=(),
        extra_notes=(f"dry-run: inventory {inventory_bytes} bytes; nothing mutated",),
    )
    logger.info("nightly.dry_run.complete", nightly_id=nightly_id, exit_code=report.exit_code)
    return report
