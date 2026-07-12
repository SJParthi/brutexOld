"""The cross-verify pipeline runner — step machine, seams, report/exit law.

DEC-CROSSVERIFY-PRODUCER-001 §4. One zero-touch run (run_id
``cv-<YYYYMMDD>-<hhmmss>-<pid>``): pull → completeness gate → incremental
prebuild → movers (revision propagation) → export → publish, with the
never-publish-partial law held by three concentric structural belts
(``export_symbol`` is the only ``csv_bytes`` constructor; the render belts;
``publish_staged`` puts only journal-staged keys).

Exit-code law (the ``cli.py`` convention): **0** published / staged_only /
nothing / skips / disabled / deferred · **1** lock refusal /
gate-module-absent · **2** pull halt / blocked index / withheld-final /
publish failed / deadline suspect / calendar abstain (cron-red). Final
verdict precedence (all exit 2 + CRITICAL): ``suspect`` > ``publish_failed``
> ``blocked`` > ``withheld_partial``.

Every I/O rides :class:`CrossverifySeams` — unit tests drive the whole
matrix with fakes; :func:`default_seams` is the ONLY function importing the
real machinery (stores, registry, pull legs, market-hours guard, gate
binding, S3 factory, alert helpers), each dependency injectable.
"""

from __future__ import annotations

import os
import shutil
import time
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, Protocol

import structlog

from brutex.crossverify.canonical import CanonicalIdentity, canonical_identity, s3_key
from brutex.crossverify.day_export import (
    SETTLE_RETRY_REASONS_FUT,
    SETTLE_RETRY_REASONS_INDEX,
    DayFrameReader,
    SymbolExport,
    export_symbol,
    make_day_frame_reader,
)
from brutex.crossverify.gate_seam import (
    CompletenessGateUnavailable,
    TodayCompletenessClassifier,
    bind_production_classifier,
)
from brutex.crossverify.journal import (
    DayJournal,
    journal_path,
    load_journal,
    scan_pending_journals,
    staging_dir,
    staging_path,
)
from brutex.crossverify.prebuild_leg import DeriveOutcome, run_prebuild_leg
from brutex.crossverify.pull_leg import (
    PullLegReport,
    live_fut_rows,
    run_pull_leg,
    run_settle_retry,
)
from brutex.crossverify.s3_publish import (
    JOURNAL_SAVE_EVERY,
    S3BackendUnavailable,
    S3PutClient,
    build_real_s3_client,
    publish_staged,
)
from brutex.data.errors import LakeWriteHalt, MarketHoursHalt, PartitionLockTimeout
from brutex.data.partition_lock import partition_write_lock
from brutex.data.session import IST

if TYPE_CHECKING:  # pragma: no cover — typing-only imports
    from collections.abc import Callable, Mapping, Sequence
    from pathlib import Path

    from brutex.calendars.nse import TradingSession
    from brutex.crossverify.config import CrossverifyConfig

_LOG = structlog.get_logger("brutex.crossverify.run")

#: The one movers statement this headless producer can honestly make: the
#: pull/derive moved the window-ledger + lake-revision tokens, so a running
#: dashboard's caches re-key on their next poll (DEC-HARDENING-LEFTOVERS-001).
MOVERS_WARM_NOTE: str = "revision-propagated (headless)"

#: The `crossverify.lock_contended` log is rate-limited to ≤1/min.
_CONTENDED_LOG_INTERVAL_S: float = 60.0

Verdict = Literal[
    "published",
    "staged_only",
    "nothing_to_do",
    "skipped_non_trading",
    "skipped_muhurat",
    "disabled",
    "deferred_market_hours",
    "refused",
    "withheld_partial",
    "blocked",
    "publish_failed",
    "suspect",
]

#: Verdicts that emit the ONE per-run Telegram summary (severity is the
#: alerter's law: INFO all-published / WARN anomalies / CRITICAL failures).
_SUMMARY_VERDICTS: frozenset[str] = frozenset(
    {
        "published",
        "staged_only",
        "nothing_to_do",
        "blocked",
        "withheld_partial",
        "publish_failed",
        "suspect",
    }
)


class CrossverifyAlerter(Protocol):
    """The two producer alerts (bound to ``SweepAlerter`` methods at merge)."""

    def crossverify_summary(self, *, run_id: str, report: CrossverifyReport) -> bool:
        """The exactly-once end-of-run summary; returns True iff attempted."""
        ...

    def crossverify_refused(self, *, run_id: str, reason: str) -> bool:
        """A CRITICAL refusal (lock exhaustion / gate absent / calendar abstain)."""
        ...


@dataclass(frozen=True, slots=True)
class CrossverifySeams:
    """Every I/O surface of one run, injected (all-fakes in unit tests)."""

    acquire_locks: Callable[[ExitStack], bool]  # ONE trio attempt; runner owns the retry loop
    session_for: Callable[[date], TradingSession | None]
    classifier_factory: Callable[[], TodayCompletenessClassifier]
    reader: DayFrameReader
    enumerate_targets: Callable[[date, PullLegReport | None], tuple[list[CanonicalIdentity], int]]
    run_pull: Callable[[], PullLegReport]
    run_settle_retry: Callable[[Sequence[CanonicalIdentity]], PullLegReport]
    run_prebuild: Callable[[Sequence[CanonicalIdentity]], DeriveOutcome]
    s3_client_factory: Callable[[], S3PutClient]  # lazy — dry_run / no-publish never calls it
    alerter: CrossverifyAlerter
    emit_pull_halt: Callable[[BaseException], object]
    market_hours_blocked: Callable[[], bool]
    now_ist: Callable[[], datetime]
    sleeper: Callable[[float], None]


@dataclass(frozen=True, slots=True)
class CrossverifyReport:
    """The one immutable record of a run (feeds the summary alert + exit code)."""

    day: date
    run_id: str
    verdict: Verdict
    published: int
    skipped_already: int
    withheld: int
    blocked: int
    nothing: int
    excluded: int
    publish_failed: int
    recovered_prior: int
    derived: int
    derive_failed: int
    movers_warm: str
    detail: str
    exit_code: int  # 0 | 1 | 2


class _RunState:
    """Mutable per-run accumulator (kept private to the step machine)."""

    __slots__ = (
        "derive_failed",
        "derived",
        "excluded",
        "publish_failed",
        "published",
        "recovered_prior",
        "skipped_already",
    )

    def __init__(self) -> None:
        self.published = 0
        self.skipped_already = 0
        self.excluded = 0
        self.publish_failed = 0
        self.recovered_prior = 0
        self.derived = 0
        self.derive_failed = 0


def _settle_retry_affected(exports: Mapping[str, SymbolExport]) -> list[CanonicalIdentity]:
    """The G2 belt's affected set: settling FUTs + short/incomplete indices."""
    affected: list[CanonicalIdentity] = []
    for export in exports.values():
        if export.disposition != "withheld":
            continue
        cls = export.identity.instrument_class
        if (cls == "index_spot" and export.reason in SETTLE_RETRY_REASONS_INDEX) or (
            cls == "fut" and export.reason in SETTLE_RETRY_REASONS_FUT
        ):
            affected.append(export.identity)
    return affected


def run_crossverify(config: CrossverifyConfig, seams: CrossverifySeams) -> CrossverifyReport:
    """Execute one producer run — the §4 step machine (A…N). Never raises for
    a data/vendor/publish problem (halts fold into ``suspect``); a genuine
    code bug still propagates loudly."""
    started = seams.now_ist()
    run_id = f"cv-{config.day:%Y%m%d}-{started:%H%M%S}-{os.getpid()}"
    state = _RunState()
    exports: dict[str, SymbolExport] = {}
    movers_warm = ""

    def _report(verdict: Verdict, exit_code: int, *, detail: str = "") -> CrossverifyReport:
        withheld = sum(1 for e in exports.values() if e.disposition == "withheld")
        blocked = sum(1 for e in exports.values() if e.disposition == "blocked")
        nothing = sum(1 for e in exports.values() if e.disposition == "nothing")
        return CrossverifyReport(
            day=config.day,
            run_id=run_id,
            verdict=verdict,
            published=state.published,
            skipped_already=state.skipped_already,
            withheld=withheld,
            blocked=blocked,
            nothing=nothing,
            excluded=state.excluded,
            publish_failed=state.publish_failed,
            recovered_prior=state.recovered_prior,
            derived=state.derived,
            derive_failed=state.derive_failed,
            movers_warm=movers_warm,
            detail=detail,
            exit_code=exit_code,
        )

    def _finish(report: CrossverifyReport) -> CrossverifyReport:
        """Emit the exactly-once summary for post-gate verdicts (best-effort)."""
        if report.verdict in _SUMMARY_VERDICTS:
            try:
                seams.alerter.crossverify_summary(run_id=run_id, report=report)
            except Exception as exc:  # fire-and-forget — an alert never kills a run
                _LOG.warning(
                    "crossverify.summary_alert_failed",
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
        _LOG.info(
            "crossverify.run_finished",
            run_id=run_id,
            day=config.day.isoformat(),
            verdict=report.verdict,
            exit_code=report.exit_code,
            detail=report.detail,
        )
        return report

    def _refused(reason: str, exit_code: int) -> CrossverifyReport:
        try:
            seams.alerter.crossverify_refused(run_id=run_id, reason=reason)
        except Exception as exc:  # fire-and-forget
            _LOG.warning(
                "crossverify.refused_alert_failed",
                error_type=type(exc).__name__,
                error=str(exc),
            )
        return _finish(_report("refused", exit_code, detail=reason))

    def _over_deadline() -> bool:
        return (seams.now_ist() - started).total_seconds() > config.deadline_s

    def _suspect(detail: str) -> CrossverifyReport:
        return _finish(_report("suspect", 2, detail=detail))

    # ── A: kill switch ─────────────────────────────────────────────────────
    if not config.enabled:
        _LOG.info("crossverify.disabled", run_id=run_id, day=config.day.isoformat())
        return _report("disabled", 0, detail="BRUTEX_CROSSVERIFY=off")

    # Lazy, memoised S3 client (dry-run / no-publish paths never build it).
    client_holder: list[S3PutClient] = []

    def _client() -> S3PutClient:
        if not client_holder:
            client_holder.append(seams.s3_client_factory())
        return client_holder[0]

    # ── D: recovery scan — re-publish prior pending STAGED bytes (no re-gate).
    # Deliberately BEFORE the session gate (round-1 hostile review): a
    # weekend/holiday run must still republish a PRIOR trading day's pending
    # keys — recovery needs no vendor pull, no session, and NO pull locks
    # (publish is pure network + journal/staging sidecar writes, exactly like
    # steps M/N which also run lock-free). Each prior day's
    # journal-reload → publish → save → cleanup runs under that day's
    # cross-process journal flock (round-2 hostile review S2a — the COMMON
    # partition_write_lock on the day's journal path), so a concurrent run's
    # step-N/step-D rmtree can never race THIS read into a spurious
    # publish_failed, and two recoveries of one day serialise. Lock-order
    # law: a day-journal flock is a LEAF — nothing (in particular never the
    # account/lake/lease pull locks) is acquired while it is held, and step D
    # releases each day's flock before step F takes the pull trio.
    if config.publish and not config.dry_run:
        aborted_recovery = False
        for prior in scan_pending_journals(
            config.lake_root, today=config.day, lookback_days=config.catchup_days
        ):
            try:
                with partition_write_lock(
                    config.lake_root, journal_path(config.lake_root, prior.day)
                ):
                    # Re-load UNDER the lock — a peer may have recovered the
                    # day between the scan and our acquire (stale ⇒ skip).
                    current = load_journal(config.lake_root, prior.day)
                    if current is None or not current.pending_keys():
                        continue
                    try:
                        outcome = publish_staged(
                            client=_client(),
                            bucket=config.bucket,
                            journal=current,
                            lake_root=config.lake_root,
                            sleeper=seams.sleeper,
                        )
                    except S3BackendUnavailable as exc:
                        state.publish_failed += len(current.pending_keys())
                        return _finish(_report("publish_failed", 2, detail=str(exc)))
                    state.recovered_prior += outcome.published
                    state.publish_failed += outcome.failed_count
                    _LOG.info(
                        "crossverify.recovery_scan",
                        run_id=run_id,
                        prior_day=prior.day.isoformat(),
                        recovered=outcome.published,
                        still_failing=outcome.failed_count,
                    )
                    if outcome.aborted_nonretryable:
                        # The step-M abort law, mirrored (round-2 S3e): a
                        # broken grant/bucket fails every later day
                        # identically — stop scanning; the untouched days'
                        # keys stay pending for the next run.
                        _LOG.error(
                            "crossverify.recovery_aborted_nonretryable",
                            run_id=run_id,
                            prior_day=prior.day.isoformat(),
                            note="whole recovery scan stopped — later prior days left pending",
                        )
                        aborted_recovery = True
                        break
                    if not current.pending_keys():
                        # The recovered day is fully published — its staged
                        # bytes served their purpose (the step-N cleanup,
                        # applied to the PRIOR day so staging never
                        # accumulates unbounded). A still-pending
                        # (failed-again) day's staging is KEPT.
                        shutil.rmtree(staging_dir(config.lake_root, prior.day), ignore_errors=True)
            except PartitionLockTimeout:
                # A peer holds this day's lock past the bound — it is
                # actively recovering/publishing the day; skip it this run
                # (the next scan re-finds anything still pending).
                _LOG.warning(
                    "crossverify.recovery_day_contended",
                    run_id=run_id,
                    prior_day=prior.day.isoformat(),
                    note="a peer holds the day-journal lock — skipped this run",
                )
                continue
        # Orphaned staging (round-2 S2b): a staging dir whose journal was
        # wiped / loads corrupt-None is INVISIBLE to the pending scan — name
        # it loudly, leave the bytes in place (remediation: `brutex
        # crossverify run --day <day>` re-runs the day idempotently).
        if not aborted_recovery:
            for offset in range(config.catchup_days, 0, -1):
                orphan_day = config.day - timedelta(days=offset)
                orphan = staging_dir(config.lake_root, orphan_day)
                if orphan.is_dir() and load_journal(config.lake_root, orphan_day) is None:
                    _LOG.warning(
                        "crossverify.staging_orphaned",
                        run_id=run_id,
                        day=orphan_day.isoformat(),
                        path=str(orphan),
                        remediation=(
                            f"brutex crossverify run --day {orphan_day.isoformat()} "
                            "re-runs the day idempotently"
                        ),
                    )

    def _early_ok(
        verdict: Verdict, *, detail: str = "", log_event: str | None = None
    ) -> CrossverifyReport:
        """An exit-0 early return — honest about recovery soft-failures.

        Round-2 hostile review S1: EVERY would-be-green early return (the
        non-trading/Muhurat skips AND the three market-hours deferral arms)
        upgrades to ``publish_failed`` exit 2 when the step-D recovery scan
        left prior keys still failing — a cron-green exit must never mask a
        recovery failure.
        """
        if state.publish_failed > 0:
            return _finish(
                _report("publish_failed", 2, detail="recovery scan left prior keys still failing")
            )
        if log_event is not None:
            _LOG.info(log_event, run_id=run_id, day=config.day.isoformat())
        return _report(verdict, 0, detail=detail)

    # ── B: day + session gate (never fabricates a session) ────────────────
    from brutex.calendars.nse import HolidayDataUnavailableError, SessionType

    try:
        session = seams.session_for(config.day)
    except HolidayDataUnavailableError as exc:
        return _refused(f"calendar coverage exhausted for {config.day.isoformat()}: {exc}", 2)
    if session is None:
        return _early_ok("skipped_non_trading", log_event="crossverify.skipped_non_trading")
    if session.session_type is SessionType.MUHURAT:
        # Coordinator ruling 2: Muhurat = whole-run skip, NO files, one INFO.
        return _early_ok("skipped_muhurat", log_event="crossverify.skipped_muhurat")

    # ── C: gate presence — FAIL-CLOSED ─────────────────────────────────────
    try:
        classifier = seams.classifier_factory()
    except CompletenessGateUnavailable as exc:
        return _refused(f"completeness gate module absent — refusing to publish: {exc}", 1)

    # ── E: market-hours START gate (vendor-calling runs only; NEVER exempt) —
    if not (config.dry_run or config.skip_pull) and seams.market_hours_blocked():
        return _early_ok(
            "deferred_market_hours",
            detail="market-hours window — deferred",
            log_event="crossverify.deferred_market_hours",
        )

    journal = load_journal(config.lake_root, config.day)
    if journal is None:
        journal = DayJournal(day=config.day, run_id=run_id)
    else:
        journal.run_id = run_id  # the latest run owns the journal

    with ExitStack() as stack:
        # ── F: locks, bounded wait-retry (decision 4 — NO preemption) ──────
        if not config.dry_run:
            wait_started = seams.now_ist()
            last_contended_log: datetime | None = None
            acquired = False
            while True:
                if seams.acquire_locks(stack):
                    acquired = True
                    break
                now = seams.now_ist()
                if (now - wait_started).total_seconds() >= config.lock_wait_s:
                    break
                if (
                    last_contended_log is None
                    or (now - last_contended_log).total_seconds() >= _CONTENDED_LOG_INTERVAL_S
                ):
                    _LOG.info(
                        "crossverify.lock_contended",
                        run_id=run_id,
                        waited_s=(now - wait_started).total_seconds(),
                        budget_s=config.lock_wait_s,
                    )
                    last_contended_log = now
                seams.sleeper(config.lock_retry_s)
            if not acquired:
                return _refused(
                    f"pull locks still held by a peer after {config.lock_wait_s:.0f}s "
                    "wait budget — deferring the day (no preemption)",
                    1,
                )

        # ── G: priority pull ───────────────────────────────────────────────
        pull_report: PullLegReport | None = None
        if not (config.dry_run or config.skip_pull):
            try:
                pull_report = seams.run_pull()
            except MarketHoursHalt as exc:
                # Defensive belt — the leg normally folds this into `halted`.
                _LOG.warning("crossverify.pull_deferred_market_hours", detail=str(exc))
                return _early_ok("deferred_market_hours", detail=str(exc))
            except Exception as exc:
                from brutex.dashboard.pull_jobs import is_pull_halt

                if is_pull_halt(exc):
                    # INTENTIONAL double CRITICAL on a halt (here + the
                    # prebuild/staging halt arms below): the SPECIFIC
                    # pull-halt alert (emit_pull_halt_alert — names the halt
                    # class + remediation) AND the one-per-run summary
                    # (crossverify_summary on the `suspect` verdict) both
                    # fire under DISTINCT dedupe keys — specific + summary,
                    # never two copies of the same message.
                    seams.emit_pull_halt(exc)
                    return _suspect(f"pull halt: {type(exc).__name__}: {exc}")
                raise
            if pull_report.halted:
                return _early_ok(
                    "deferred_market_hours",
                    detail=f"pull halted: {pull_report.halt_reason}",
                )
        if _over_deadline():
            return _suspect("hard deadline exceeded after the pull leg")

        # ── H: completeness gate + export decisions ────────────────────────
        targets, excluded = seams.enumerate_targets(config.day, pull_report)
        state.excluded = excluded
        for identity in targets:
            exports[identity.canonical_symbol] = export_symbol(
                identity=identity,
                day=config.day,
                session=session,
                reader=seams.reader,
                classifier=classifier,
            )
        if _over_deadline():
            return _suspect("hard deadline exceeded after the export pass")

        # ── G2: settle-retry belt (affected identities only) ───────────────
        if not (config.dry_run or config.skip_pull):
            for _round in range(config.settle_retry_rounds):
                affected = _settle_retry_affected(exports)
                if not affected or _over_deadline():
                    break
                seams.sleeper(config.settle_retry_delay_s)
                seams.run_settle_retry(affected)
                for identity in affected:
                    exports[identity.canonical_symbol] = export_symbol(
                        identity=identity,
                        day=config.day,
                        session=session,
                        reader=seams.reader,
                        classifier=classifier,
                    )

        # ── I: incremental prebuild (never blocks the export) ──────────────
        if not config.dry_run:
            coordinates = [
                e.identity for e in exports.values() if e.disposition in ("publish", "withheld")
            ]
            try:
                derive = seams.run_prebuild(coordinates)
            except LakeWriteHalt as exc:
                seams.emit_pull_halt(exc)
                return _suspect(f"prebuild halt: {exc}")
            state.derived = derive.derived
            state.derive_failed = derive.failed
        if _over_deadline():
            return _suspect("hard deadline exceeded after the prebuild leg")

        # ── J: movers warm — honest revision propagation only ──────────────
        movers_warm = MOVERS_WARM_NOTE

        # ── K: stage + journal ─────────────────────────────────────────────
        if not config.dry_run:
            # The comprehension's non-None narrowing is structural: the
            # never-partial law (`csv_bytes is not None ⇔ publish`) is pinned
            # at export_symbol — the filter narrows types, it drops nothing.
            publishable = sorted(
                (
                    (e.identity, e.csv_bytes, e.sha256)
                    for e in exports.values()
                    if e.disposition == "publish"
                    and e.csv_bytes is not None
                    and e.sha256 is not None
                ),
                key=lambda item: (item[0].segment_token, item[0].canonical_symbol),
            )
            from brutex.data.atomic_io import atomic_write_via

            try:
                # The stage+journal span runs under today's day-journal flock
                # (round-2 S2a): two same-day runs serialise their staging
                # writes + journal saves instead of last-writer-wins racing.
                # Acquired strictly AFTER the pull trio (step F) and released
                # at the end of this block — a day-journal flock is a LEAF:
                # never held while acquiring the pull locks.
                with partition_write_lock(
                    config.lake_root, journal_path(config.lake_root, config.day)
                ):
                    for staged, (identity, body, sha) in enumerate(publishable, start=1):
                        dest = staging_path(config.lake_root, config.day, identity)
                        dest.parent.mkdir(parents=True, exist_ok=True)

                        def _write(tmp: Path, data: bytes = body) -> None:
                            tmp.write_bytes(data)

                        atomic_write_via(dest, _write)
                        journal.mark_staged(
                            s3_key(prefix=config.key_prefix, day=config.day, identity=identity),
                            sha,
                            len(body),
                        )
                        if staged % JOURNAL_SAVE_EVERY == 0:
                            journal.save(config.lake_root)
                    journal.withheld = {
                        symbol: e.reason
                        for symbol, e in sorted(exports.items())
                        if e.disposition == "withheld"
                    }
                    journal.save(config.lake_root)
            except LakeWriteHalt as exc:
                seams.emit_pull_halt(exc)
                return _suspect(f"staging halt: {exc}")
            except PartitionLockTimeout:
                return _refused(
                    "today's journal/staging lock held by a peer past the bound — "
                    "a concurrent same-day run is active; deferring (no preemption)",
                    1,
                )

    # ── L: locks released (ExitStack closed) — publish is pure network ─────
    if _over_deadline():
        return _suspect("hard deadline exceeded before publish")

    # ── M: publish (frozen staged bytes) — under today's day-journal flock
    # (round-2 S2a): the staged-bytes read → PUT → journal save → step-N
    # staging cleanup are one exclusive span, so a peer's rmtree can never
    # race THIS run's read into a spurious publish_failed. Acquired only
    # AFTER the pull trio released (step L) — never held across a pull-lock
    # acquire.
    if not config.dry_run and config.publish and journal.entries:
        try:
            with partition_write_lock(config.lake_root, journal_path(config.lake_root, config.day)):
                try:
                    outcome = publish_staged(
                        client=_client(),
                        bucket=config.bucket,
                        journal=journal,
                        lake_root=config.lake_root,
                        sleeper=seams.sleeper,
                    )
                except S3BackendUnavailable as exc:
                    state.publish_failed += len(journal.pending_keys())
                    return _finish(_report("publish_failed", 2, detail=str(exc)))
                state.published = outcome.published
                state.skipped_already = outcome.skipped_already
                state.publish_failed += outcome.failed_count
                if outcome.aborted_nonretryable:
                    return _finish(
                        _report(
                            "publish_failed",
                            2,
                            detail="nonretryable S3 error — whole publish aborted "
                            f"(failed keys: {', '.join(outcome.failed_keys)})",
                        )
                    )
                if not journal.pending_keys():
                    # The step-N staging cleanup, INSIDE the day lock: a full
                    # publish means the staging bytes served their purpose;
                    # the journal is retained as the durable done-record.
                    shutil.rmtree(staging_dir(config.lake_root, config.day), ignore_errors=True)
        except PartitionLockTimeout:
            return _refused(
                "today's journal/staging lock held by a peer past the bound — "
                "a concurrent same-day run is active; deferring (no preemption)",
                1,
            )

    # ── N: final verdict ────────────────────────────────────────────────────
    blocked = sum(1 for e in exports.values() if e.disposition == "blocked")
    withheld = sum(1 for e in exports.values() if e.disposition == "withheld")
    if state.publish_failed > 0:
        return _finish(_report("publish_failed", 2))
    if blocked > 0:
        return _finish(_report("blocked", 2))
    if withheld > 0:
        return _finish(_report("withheld_partial", 2))
    if config.dry_run:
        return _finish(
            _report("staged_only", 0, detail="dry-run — rendered in memory, nothing staged")
        )
    if not config.publish:
        return _finish(_report("staged_only", 0, detail="publish disabled — staged only"))
    if state.published + state.skipped_already + state.recovered_prior > 0:
        return _finish(_report("published", 0))
    return _finish(_report("nothing_to_do", 0))


def _default_alerter() -> CrossverifyAlerter:
    """The production alerter — lazy, tolerant binding to ``sweep_alerts``.

    Phase B lands ``emit_crossverify_summary_alert`` /
    ``emit_crossverify_refused_alert`` module helpers (the
    ``emit_pull_halt_alert`` shape); until then a missing helper degrades to
    ONE WARN + ``False`` — an alert is fire-and-forget, never load-bearing.
    """

    class _Adapter:
        def _emit(self, helper_name: str, **kwargs: Any) -> bool:
            try:
                import importlib

                module = importlib.import_module("brutex.notifications.sweep_alerts")
                helper = getattr(module, helper_name, None)
                if helper is None:
                    _LOG.warning(
                        "crossverify.alert_helper_missing",
                        helper=helper_name,
                        note="sweep_alerts helper not yet merged — alert skipped",
                    )
                    return False
                return bool(helper(**kwargs))
            except Exception as exc:  # blanket fire-and-forget (the emit_* shape)
                _LOG.warning(
                    "crossverify.alert_emit_failed",
                    helper=helper_name,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                return False

        def crossverify_summary(self, *, run_id: str, report: CrossverifyReport) -> bool:
            return self._emit("emit_crossverify_summary_alert", run_id=run_id, report=report)

        def crossverify_refused(self, *, run_id: str, reason: str) -> bool:
            return self._emit("emit_crossverify_refused_alert", run_id=run_id, reason=reason)

    return _Adapter()


def _default_emit_pull_halt(exc: BaseException) -> object:
    """Best-effort pull-halt CRITICAL alert (the existing COMMON helper)."""
    try:
        from brutex.notifications.sweep_alerts import emit_pull_halt_alert

        if isinstance(exc, Exception):  # the helper's contract; halts always are
            emit_pull_halt_alert(exc)
            return True
        return False
    except Exception as emit_exc:  # blanket fire-and-forget
        _LOG.warning(
            "crossverify.pull_halt_alert_failed",
            error_type=type(emit_exc).__name__,
            error=str(emit_exc),
        )
        return False


def default_seams(
    config: CrossverifyConfig,
    *,
    env: Mapping[str, str] | None = None,
    store_opener: Callable[[Path], Any] | None = None,
    account_key_resolver: Callable[[], str | None] | None = None,
    lease_factory: Callable[[Path], Any] | None = None,
    spot_puller: Callable[..., Any] | None = None,
    metadata_puller: Callable[..., Any] | None = None,
    candle_puller: Callable[..., Any] | None = None,
    materialize: Callable[..., Any] | None = None,
    derive_daily: Callable[..., Any] | None = None,
    read_window_factory: Callable[..., Any] | None = None,
    write_daily_factory: Callable[..., Any] | None = None,
    s3_client_factory: Callable[[], S3PutClient] | None = None,
    alerter: CrossverifyAlerter | None = None,
    session_rate: Any | None = None,
) -> CrossverifySeams:
    """Bind the production seams (the ONLY real-machinery importer besides the
    legs). Every heavy dependency opens lazily INSIDE its closure (per call);
    each is injectable so the closures are fully unit-coverable.

    The lock seam wraps the ``cli._enter_pull_singleflight`` trio ORDER
    (account flock → lake flock → cross-host lease, released LIFO), one
    attempt per call — the runner owns the bounded wait-retry loop.
    """
    from brutex.calendars.market_hours import guard_active_default, resolve_market_closed
    from brutex.calendars.nse import get_session_checked
    from brutex.data.instrument_store import DEFAULT_DB_NAME, InstrumentStore

    lake_root = config.lake_root
    db_path = lake_root / DEFAULT_DB_NAME
    registry_path = lake_root / "registry.duckdb"
    holder_note = f"crossverify:{config.day.isoformat()}:{os.getpid()}"
    opener = InstrumentStore.open if store_opener is None else store_opener
    # Resolved ONCE at seam build (fail-loud on junk env); a dry-run threads
    # None — no vendor calls to gate (the nightly binding precedent).
    market_closed = None if config.dry_run else resolve_market_closed(env=env)

    if session_rate is None:
        from brutex.dashboard.pull_controller import SessionRateController

        session_rate = SessionRateController()

    def _account_key() -> str | None:
        if account_key_resolver is not None:
            return account_key_resolver()
        from brutex.config.credentials import resolve_groww_aws_credentials
        from brutex.config.credentials_types import CredentialsError

        try:
            return resolve_groww_aws_credentials().api_key
        except CredentialsError as exc:
            _LOG.warning(
                "crossverify.account_lock_skipped",
                reason="credentials unavailable — account-keyed single-flight not taken",
                error=str(exc),
            )
            return None

    def _acquire_locks(stack: ExitStack) -> bool:
        from brutex.adapters.groww.pull_singleflight import (
            PullAlreadyRunningError,
            account_pull_singleflight_lock,
            pull_lease_guard,
            pull_singleflight_lock,
        )

        lease = pull_lease_guard if lease_factory is None else lease_factory
        inner = ExitStack()
        try:
            key = _account_key()
            if key is not None:
                inner.enter_context(account_pull_singleflight_lock(key))
            inner.enter_context(pull_singleflight_lock(lake_root, holder_note=holder_note))
            inner.enter_context(lease(lake_root))
        except PullAlreadyRunningError as exc:
            inner.close()
            _LOG.info("crossverify.lock_attempt_refused", holder_detail=str(exc))
            return False
        except BaseException:
            inner.close()
            raise
        stack.enter_context(inner.pop_all())
        return True

    def _enumerate(
        day: date, pull_report: PullLegReport | None
    ) -> tuple[list[CanonicalIdentity], int]:
        """§4.2 store+registry-authoritative target enumeration."""
        from brutex.models import InstrumentType
        from brutex.universe import V1_INDICES

        identities: list[CanonicalIdentity] = []
        excluded = 0
        # The mapper mirrors V1_INDICES minus reference-only by construction
        # (EXPORT_INDEX_COORDS derives from it), so every non-reference spec
        # maps — the comprehension filter is the structural narrowing.
        identities.extend(
            identity
            for spec in V1_INDICES
            if not spec.reference_only
            and (
                identity := canonical_identity(
                    exchange=spec.exchange.value,
                    segment="INDEX",
                    lake_symbol=spec.underlying_symbol,
                )
            )
            is not None
        )
        store = opener(db_path)
        try:
            stock_symbols = sorted(
                {
                    inst.symbol_name
                    for inst in store.list_index_equity_instruments()
                    if inst.instrument is InstrumentType.EQ and inst.exch_id.value == "NSE"
                }
            )
        finally:
            store.close()
        for symbol in stock_symbols:
            stock_identity = canonical_identity(exchange="NSE", segment="CASH", lake_symbol=symbol)
            if stock_identity is None:
                excluded += 1
                continue
            identities.append(stock_identity)
        if pull_report is not None:
            fut_rows = sorted(pull_report.fut_contracts, key=lambda r: r.contract_symbol)
        elif registry_path.exists():
            from brutex.data.registry_store import RegistryStore

            with RegistryStore.open(registry_path) as registry:
                fut_rows = live_fut_rows(registry, day=day)
        else:
            _LOG.info("crossverify.no_registry", path=str(registry_path))
            fut_rows = []
        for row in fut_rows:
            fut_identity = canonical_identity(
                exchange=row.exch, segment="FNO", lake_symbol=row.contract_symbol
            )
            if fut_identity is None:
                excluded += 1
                continue
            identities.append(fut_identity)
        return identities, excluded

    def _run_pull() -> PullLegReport:
        from brutex.data.registry_store import RegistryStore

        store = opener(db_path)
        try:
            with RegistryStore.open(registry_path) as registry:
                return run_pull_leg(
                    lake_root=lake_root,
                    day=config.day,
                    lookback_days=config.pull_lookback_days,
                    store=store,
                    registry=registry,
                    session_rate=session_rate,
                    market_closed=market_closed,
                    spot_puller=spot_puller,
                    metadata_puller=metadata_puller,
                    candle_puller=candle_puller,
                )
        finally:
            store.close()

    def _run_settle_retry(affected: Sequence[CanonicalIdentity]) -> PullLegReport:
        from brutex.data.registry_store import RegistryStore

        store = opener(db_path)
        try:
            with RegistryStore.open(registry_path) as registry:
                return run_settle_retry(
                    lake_root=lake_root,
                    day=config.day,
                    lookback_days=config.pull_lookback_days,
                    affected=affected,
                    store=store,
                    registry=registry,
                    session_rate=session_rate,
                    market_closed=market_closed,
                    spot_puller=spot_puller,
                    candle_puller=candle_puller,
                )
        finally:
            store.close()

    def _run_prebuild(coordinates: Sequence[CanonicalIdentity]) -> DeriveOutcome:
        return run_prebuild_leg(
            lake_root=lake_root,
            day=config.day,
            coordinates=coordinates,
            now_ist=lambda: datetime.now(tz=IST),
            materialize=materialize,
            derive_daily=derive_daily,
            read_window_factory=read_window_factory,
            write_daily_factory=write_daily_factory,
        )

    return CrossverifySeams(
        acquire_locks=_acquire_locks,
        session_for=get_session_checked,
        classifier_factory=bind_production_classifier,
        reader=make_day_frame_reader(lake_root),
        enumerate_targets=_enumerate,
        run_pull=_run_pull,
        run_settle_retry=_run_settle_retry,
        run_prebuild=_run_prebuild,
        s3_client_factory=(
            (lambda: build_real_s3_client(config.region))
            if s3_client_factory is None
            else s3_client_factory
        ),
        alerter=_default_alerter() if alerter is None else alerter,
        emit_pull_halt=_default_emit_pull_halt,
        market_hours_blocked=lambda: guard_active_default(env=env),
        now_ist=lambda: datetime.now(tz=IST),
        sleeper=time.sleep,
    )


__all__ = [
    "MOVERS_WARM_NOTE",
    "CrossverifyAlerter",
    "CrossverifyReport",
    "CrossverifySeams",
    "Verdict",
    "default_seams",
    "run_crossverify",
]
