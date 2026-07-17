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
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, Protocol

import structlog

from brutex.crossverify.canonical import CanonicalIdentity, canonical_identity, s3_key
from brutex.crossverify.day_export import (
    OBSERVE_PUBLISH_REASONS,
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
    scan_withheld_journals,
    staging_dir,
    staging_path,
    write_last_run,
)
from brutex.crossverify.owed import clear_owed, scan_owed_days
from brutex.crossverify.prebuild_leg import DeriveOutcome, run_prebuild_leg
from brutex.crossverify.pull_leg import (
    PullLegReport,
    run_pull_leg,
    run_settle_retry,
    whole_universe_spot_targets,
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

#: Verdicts that persist the durable ``last_run.json`` record (the summary
#: verdicts + ``refused``) — every MATERIAL outcome. The honest skip
#: verdicts (non-trading / Muhurat / deferred / disabled) do NOT write it
#: UNLESS the run did material catch-up work (``regated``/``recovered_prior``
#: > 0 — the R1 "nothing ships invisibly" law): a true no-op weekend skip
#: must never clobber Friday's failure record, while a weekend re-gate that
#: published real S3 objects records + summarises — the record it may
#: overwrite was resolved by that very re-gate (material-actions-only
#: writes win; documented in the runbook §3).
_LAST_RUN_VERDICTS: frozenset[str] = _SUMMARY_VERDICTS | frozenset({"refused"})

#: Verdicts that CLEAR the day's owed marker
#: (DEC-CROSSVERIFY-OWED-SENTINEL-001) — the completed-material set: the
#: day's cross-verify genuinely finished (published / rendered+staged /
#: honest nothing) or the day is honestly not-a-session (non-trading /
#: Muhurat — a marker must never wedge forever on a holiday). Deferrals,
#: refusals and every failure verdict KEEP the marker: the next run's
#: step-D3 owed catchup re-completes the day (crash-convergent — the marker
#: only clears once a run reached a completed material terminal verdict).
#: ``staged_only`` clears only on the REAL publish-disabled arm (the dry-run
#: arm never touches the store — the ``config.dry_run`` guard at both clear
#: sites): the day's render+stage completed and the journal's pending keys
#: are the recovery scan's territory from there.
_OWED_CLEAR_VERDICTS: frozenset[str] = frozenset(
    {"published", "staged_only", "nothing_to_do", "skipped_non_trading", "skipped_muhurat"}
)


def _store_read_errors() -> tuple[type[Exception], ...]:
    """The CONFIRMED error classes escaping the store-backed target
    enumeration (mirrors ``spot_drift._master_read_errors`` — the house
    degrade set): :class:`~brutex.data.errors.CatalogError` (the domain
    error ``InstrumentStore.open`` wraps every open failure into),
    ``OSError`` (the pre-guard ``mkdir``), plus ``duckdb.Error`` (raw reads)
    when duckdb is importable — absent, the store's own ``open`` raises
    ``CatalogError`` first, so the tuple is complete without it.
    """
    from brutex.data.errors import CatalogError

    errors: list[type[Exception]] = [CatalogError, OSError]
    try:
        import duckdb
    except ImportError:
        return tuple(errors)
    errors.append(duckdb.Error)
    return tuple(errors)


def _log_regate_day(
    *, run_id: str, prior: DayJournal, staged: int, published: int, dropped_nothing: int
) -> None:
    """The one per-re-gated-day INFO (step D2 observability)."""
    _LOG.info(
        "crossverify.regate_day",
        run_id=run_id,
        prior_day=prior.day.isoformat(),
        staged=staged,
        published=published,
        still_withheld=len(prior.withheld),
        dropped_nothing=dropped_nothing,
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
    enumerate_targets: Callable[[date], tuple[list[CanonicalIdentity], int]]
    run_pull: Callable[[], PullLegReport]
    run_settle_retry: Callable[[Sequence[CanonicalIdentity]], PullLegReport]
    run_prebuild: Callable[[Sequence[CanonicalIdentity]], DeriveOutcome]
    s3_client_factory: Callable[[], S3PutClient]  # lazy — dry_run / no-publish never calls it
    alerter: CrossverifyAlerter
    emit_pull_halt: Callable[[BaseException], object]
    market_hours_blocked: Callable[[], bool]
    now_ist: Callable[[], datetime]
    sleeper: Callable[[float], None]
    #: The step-D3 owed-day rerun seam (DEC-CROSSVERIFY-OWED-SENTINEL-001):
    #: complete ONE never-started owed prior day with the manual ``--day``
    #: machinery (production: a day-scoped ``run_crossverify`` over fresh
    #: day-bound default seams — ``skip_pull`` + ``catchup_days=0``). Default
    #: ``None`` (back-compat for injected test seams) DEGRADES honestly: owed
    #: days are surfaced with ONE WARN, never silently dropped.
    run_day: Callable[[date], CrossverifyReport] | None = None


@dataclass(frozen=True, slots=True)
class CrossverifyReport:
    """The one immutable record of a run (feeds the summary alert + exit code)."""

    day: date
    run_id: str
    verdict: Verdict
    published: int
    skipped_already: int
    #: Export DECISIONS admitted under the STRICT law (reason ``complete``) —
    #: decision-level, unlike the put-level ``published`` (an idempotent
    #: re-run publishes 0 while still deciding N strict publishes).
    publish_strict: int
    #: Export DECISIONS admitted under the OBSERVE-ONLY index count law
    #: (#1788 fix round — count-anomalous non-trio indices shipped with a
    #: counter, never a withhold). Decision-level, like ``withheld``.
    publish_observe: int
    withheld: int
    blocked: int
    nothing: int
    excluded: int
    publish_failed: int
    recovered_prior: int
    #: PRIOR days' previously-WITHHELD symbols published by the step-D2
    #: auto-re-gate (DEC-CROSSVERIFY-WITHHELD-REGATE-001) — put-level, like
    #: ``recovered_prior`` (the still-withheld remainder stays visible in
    #: each day's journal ``withheld`` map).
    regated: int
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
        "failed_keys",
        "publish_failed",
        "published",
        "recovered_prior",
        "regated",
        "skipped_already",
    )

    def __init__(self) -> None:
        self.published = 0
        self.skipped_already = 0
        self.excluded = 0
        self.publish_failed = 0
        self.recovered_prior = 0
        self.regated = 0
        self.derived = 0
        self.derive_failed = 0
        #: Capped sample of still-failing S3 keys across EVERY publish pass
        #: (recovery + re-gate + today) — feeds the publish_failed detail +
        #: the durable last-run record; ``publish_failed`` is the true count.
        self.failed_keys: list[str] = []

    def note_failed_keys(self, keys: tuple[str, ...]) -> None:
        """Fold a publish pass's capped failed-key sample, bounded overall."""
        from brutex.crossverify.s3_publish import FAILED_KEYS_CAP

        room = FAILED_KEYS_CAP - len(self.failed_keys)
        if room > 0:
            self.failed_keys.extend(keys[:room])


def _settle_retry_affected(exports: Mapping[str, SymbolExport]) -> list[CanonicalIdentity]:
    """The G2 belt's affected set: short/incomplete indices (spot-only #1788).

    The FUT arm (``oi_null_settling``) left with the futures legs
    (DEC-CROSSVERIFY-SPOT-ONLY-001) — a withheld non-index export is never
    settle-retried.
    """
    return [
        export.identity
        for export in exports.values()
        if export.disposition == "withheld"
        and export.identity.instrument_class == "index_spot"
        and export.reason in SETTLE_RETRY_REASONS_INDEX
    ]


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
        # The three-way publish split (the #1788-fix-round index-policy
        # ruling): observe = publish decisions carrying a gate count token
        # (only the observe-tier index law admits those); strict = the rest.
        publish_observe = sum(
            1
            for e in exports.values()
            if e.disposition == "publish" and e.reason in OBSERVE_PUBLISH_REASONS
        )
        publish_strict = (
            sum(1 for e in exports.values() if e.disposition == "publish") - publish_observe
        )
        return CrossverifyReport(
            day=config.day,
            run_id=run_id,
            verdict=verdict,
            published=state.published,
            skipped_already=state.skipped_already,
            publish_strict=publish_strict,
            publish_observe=publish_observe,
            withheld=withheld,
            blocked=blocked,
            nothing=nothing,
            excluded=state.excluded,
            publish_failed=state.publish_failed,
            recovered_prior=state.recovered_prior,
            regated=state.regated,
            derived=state.derived,
            derive_failed=state.derive_failed,
            movers_warm=movers_warm,
            detail=detail,
            exit_code=exit_code,
        )

    def _finish(report: CrossverifyReport) -> CrossverifyReport:
        """Emit the exactly-once summary for post-gate verdicts (best-effort)
        and persist the durable last-run record (the dashboard's reload-proof
        failure surface — a finished failure no longer vanishes with the
        in-memory job state).

        MATERIAL-SKIP override (the R1 "nothing ships invisibly" law): a
        skip-verdict run that re-gated or recovered prior days' keys
        published REAL S3 objects, so it summarises (WARN — the alerter's
        catch-up tier) AND writes the last-run record like any material run;
        a ``refused`` run stays summary-free (``crossverify_refused`` is its
        alert) and a true no-op skip stays silent.
        """
        if not config.dry_run and report.verdict in _OWED_CLEAR_VERDICTS:
            # DEC-CROSSVERIFY-OWED-SENTINEL-001: the day reached a completed
            # material terminal verdict — its owed marker (if any launcher
            # wrote one) is settled. Best-effort: a failed clear WARNs and
            # the day honestly re-reads OWED (idempotent re-completion).
            clear_owed(config.lake_root, config.day)
        material_skip = report.verdict not in _LAST_RUN_VERDICTS and (
            report.regated > 0 or report.recovered_prior > 0
        )
        if not config.dry_run and (report.verdict in _LAST_RUN_VERDICTS or material_skip):
            try:
                write_last_run(
                    config.lake_root,
                    {
                        "version": 1,
                        "finished_at_utc": seams.now_ist().astimezone(UTC).isoformat(),
                        "failed_keys": list(state.failed_keys),
                        "day": report.day.isoformat(),
                        "run_id": report.run_id,
                        "verdict": report.verdict,
                        "exit_code": report.exit_code,
                        "published": report.published,
                        "skipped_already": report.skipped_already,
                        "publish_strict": report.publish_strict,
                        "publish_observe": report.publish_observe,
                        "withheld": report.withheld,
                        "blocked": report.blocked,
                        "nothing": report.nothing,
                        "excluded": report.excluded,
                        "publish_failed": report.publish_failed,
                        "recovered_prior": report.recovered_prior,
                        "regated": report.regated,
                        "derived": report.derived,
                        "derive_failed": report.derive_failed,
                        "detail": report.detail,
                    },
                )
            except Exception as exc:  # best-effort VIEW write — never kills a run
                _LOG.warning(
                    "crossverify.last_run_write_failed",
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
        if report.verdict in _SUMMARY_VERDICTS or material_skip:
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

    def _regate_withheld_scan() -> CrossverifyReport | None:
        """Step D2 — the withheld-day AUTO-RE-GATE (DEC-CROSSVERIFY-WITHHELD-REGATE-001).

        For every PRIOR day inside the catchup window whose journal still
        names WITHHELD symbols, re-gate exactly those symbols from the
        (since-healed) lake — re-export → stage → publish the now-complete
        ones — and rewrite the journal's ``withheld`` map to the honest
        remainder. Closes the runbook's "manual ``--day`` re-run" remediation
        for the common heal-within-catchup case. Laws:

        * fail-closed — the completeness gate binds FIRST; an absent gate
          module skips the WHOLE scan with one WARN (never publish ungated);
        * already-published keys are NEVER re-put — the re-gate touches only
          the withheld map's symbols, and :func:`publish_staged` puts only
          journal-PENDING keys (a re-render whose sha matches a published
          entry keeps ``published=True`` — the ``mark_staged`` idempotency
          law);
        * vendor-free + pull-lock-free — reads the lake, writes only the
          day's staging/journal sidecars under that day's LEAF journal flock
          (the step-D/M lock law), then publishes over the network;
        * per-day isolation — a contended day / calendar-abstain day is
          skipped loudly; the next run's scan re-finds it. Returns a terminal
          report ONLY for the whole-run failure classes (S3 backend absent /
          a ``LakeWriteHalt`` staging halt → ``suspect``), mirroring steps
          D/K.

        HONEST envelope: each re-gated symbol pays one month-partition read
        (the ``export_symbol`` read law — O(one month of 1-minute bars) per
        (symbol, day)); the scan is O(catchup_days) journal probes + ONE
        target enumeration (lazy, first re-gated day only; a transient store
        fault there skips the scan FAIL-OPEN with one
        ``crossverify.regate_scan_failed`` WARN — the main day never sinks).
        Only WITHHELD symbols re-gate — a day whose symbol read honest
        ``nothing`` (zero bars) is never in the withheld map, so a
        later-backfilled zero-bar day still needs the manual ``--day``
        re-run (named limit). Attribution: a prior PENDING key that reaches
        S3 via THIS pass's publish (rather than the step-D recovery scan —
        e.g. a day carrying both pending keys and withheld symbols) counts
        as ``regated``, not ``recovered_prior`` — attribution only, both
        roads end published (harmless, named in MIGRATION.md #1789).
        """
        days = scan_withheld_journals(
            config.lake_root, today=config.day, lookback_days=config.catchup_days
        )
        if not days:
            return None
        try:
            classifier = seams.classifier_factory()
        except CompletenessGateUnavailable as exc:
            _LOG.warning(
                "crossverify.regate_gate_unavailable",
                run_id=run_id,
                error=str(exc),
                note="withheld re-gate skipped FAIL-CLOSED — never publish ungated",
            )
            return None
        from brutex.calendars.nse import HolidayDataUnavailableError, SessionType
        from brutex.data.atomic_io import atomic_write_via

        identities: dict[str, CanonicalIdentity] | None = None
        for prior in days:
            try:
                prior_session = seams.session_for(prior.day)
            except HolidayDataUnavailableError as exc:
                _LOG.warning(
                    "crossverify.regate_session_unavailable",
                    run_id=run_id,
                    prior_day=prior.day.isoformat(),
                    error=str(exc),
                    note="calendar abstained for the withheld day — skipped this run",
                )
                continue
            if prior_session is None or prior_session.session_type is SessionType.MUHURAT:
                # Defensive totality: a withheld map is only ever WRITTEN by
                # a run that passed the session gate (non-trading + Muhurat
                # runs skip before any journal exists) — a journal claiming
                # withheld symbols on such a day is foreign/tampered state.
                _LOG.warning(
                    "crossverify.regate_day_not_regatable",
                    run_id=run_id,
                    prior_day=prior.day.isoformat(),
                    note="non-trading/Muhurat day carries a withheld map — never re-gated",
                )
                continue
            if identities is None:
                try:
                    targets, _excluded = seams.enumerate_targets(prior.day)
                except _store_read_errors() as exc:
                    # FAIL-OPEN for the main day (R1 hardening): a transient
                    # store/catalog fault in the re-gate's OWN enumeration
                    # must never sink today's work — skip the whole re-gate
                    # scan this run (the withheld maps stay untouched; the
                    # next run's scan re-finds them). A non-store exception
                    # is a genuine code bug and still propagates.
                    _LOG.warning(
                        "crossverify.regate_scan_failed",
                        run_id=run_id,
                        prior_day=prior.day.isoformat(),
                        error_type=type(exc).__name__,
                        error=str(exc),
                        note=(
                            "target enumeration failed — withheld re-gate "
                            "skipped this run; the main day continues"
                        ),
                    )
                    return None
                identities = {identity.canonical_symbol: identity for identity in targets}
            try:
                with partition_write_lock(
                    config.lake_root, journal_path(config.lake_root, prior.day)
                ):
                    # Re-load UNDER the lock — a peer may have re-gated the
                    # day between the scan and our acquire (stale ⇒ skip).
                    current = load_journal(config.lake_root, prior.day)
                    if current is None or not current.withheld:
                        continue
                    current.run_id = run_id  # the latest run owns the journal
                    try:
                        still_withheld: dict[str, str] = {}
                        staged = 0
                        dropped_nothing = 0
                        for symbol, prior_reason in sorted(current.withheld.items()):
                            identity = identities.get(symbol)
                            if identity is None:
                                _LOG.warning(
                                    "crossverify.regate_symbol_unknown",
                                    run_id=run_id,
                                    prior_day=prior.day.isoformat(),
                                    symbol=repr(symbol),  # journal-derived — repr()'d (#907)
                                    note=(
                                        "withheld symbol no longer enumerates — kept on "
                                        "the honest withheld record, never re-gated"
                                    ),
                                )
                                still_withheld[symbol] = prior_reason
                                continue
                            export = export_symbol(
                                identity=identity,
                                day=prior.day,
                                session=prior_session,
                                reader=seams.reader,
                                classifier=classifier,
                            )
                            if (
                                export.disposition == "publish"
                                and export.csv_bytes is not None
                                and export.sha256 is not None
                            ):
                                body = export.csv_bytes
                                dest = staging_path(config.lake_root, prior.day, identity)
                                dest.parent.mkdir(parents=True, exist_ok=True)

                                def _write(tmp: Path, data: bytes = body) -> None:
                                    tmp.write_bytes(data)

                                atomic_write_via(dest, _write)
                                current.mark_staged(
                                    s3_key(
                                        prefix=config.key_prefix,
                                        day=prior.day,
                                        identity=identity,
                                    ),
                                    export.sha256,
                                    len(body),
                                )
                                staged += 1
                            elif export.disposition == "nothing":
                                # The lake now honestly holds NOTHING for the
                                # symbol (e.g. a heal removed junk rows) —
                                # drop the stale withheld record, never
                                # fabricate a file.
                                dropped_nothing += 1
                                _LOG.info(
                                    "crossverify.regate_now_nothing",
                                    run_id=run_id,
                                    prior_day=prior.day.isoformat(),
                                    symbol=repr(symbol),  # vendor-derived — repr()'d (#907)
                                    prior_reason=prior_reason,
                                    reason=export.reason,
                                )
                            else:  # withheld | blocked — still not publishable
                                still_withheld[symbol] = export.reason
                        current.withheld = still_withheld
                        current.save(config.lake_root)
                    except LakeWriteHalt as exc:
                        seams.emit_pull_halt(exc)
                        return _suspect(f"re-gate staging halt: {exc}")
                    published_now = 0
                    if current.pending_keys():
                        try:
                            outcome = publish_staged(
                                client=_client(),
                                bucket=config.bucket,
                                journal=current,
                                lake_root=config.lake_root,
                                sleeper=seams.sleeper,
                            )
                        except S3BackendUnavailable as exc:
                            state.note_failed_keys(current.pending_keys())
                            state.publish_failed += len(current.pending_keys())
                            return _finish(_report("publish_failed", 2, detail=str(exc)))
                        published_now = outcome.published
                        state.regated += outcome.published
                        state.publish_failed += outcome.failed_count
                        state.note_failed_keys(outcome.failed_keys)
                        if outcome.aborted_nonretryable:
                            # The step-D/M abort law: a broken grant/bucket
                            # fails every later day identically — stop the
                            # scan; the untouched days' withheld maps stay
                            # for the next run.
                            _LOG.error(
                                "crossverify.regate_aborted_nonretryable",
                                run_id=run_id,
                                prior_day=prior.day.isoformat(),
                                note=(
                                    "whole re-gate scan stopped — later withheld "
                                    "days left for the next run"
                                ),
                            )
                            _log_regate_day(
                                run_id=run_id,
                                prior=current,
                                staged=staged,
                                published=published_now,
                                dropped_nothing=dropped_nothing,
                            )
                            return None
                        if not current.pending_keys():
                            # The step-N cleanup, applied to the re-gated
                            # PRIOR day: fully published ⇒ its staged bytes
                            # served their purpose.
                            shutil.rmtree(
                                staging_dir(config.lake_root, prior.day), ignore_errors=True
                            )
                    _log_regate_day(
                        run_id=run_id,
                        prior=current,
                        staged=staged,
                        published=published_now,
                        dropped_nothing=dropped_nothing,
                    )
            except PartitionLockTimeout:
                # A peer holds this day's lock past the bound — it is
                # actively recovering/re-gating the day; skip it this run.
                _LOG.warning(
                    "crossverify.regate_day_contended",
                    run_id=run_id,
                    prior_day=prior.day.isoformat(),
                    note="a peer holds the day-journal lock — skipped this run",
                )
                continue
        return None

    def _owed_catchup_scan() -> CrossverifyReport | None:
        """Step D3 — NEVER-STARTED owed-day catchup (DEC-CROSSVERIFY-OWED-SENTINEL-001).

        Steps D/D2 iterate days that HAVE a journal — a day whose pull chain
        died BEFORE the crossverify tail ever started (the one-click pull
        writes today's data then dies mid-prebuild) has NO journal and was
        invisible to every catchup. The pull launchers now write a durable
        owed marker at pull-tail-commit ENTRY; this scan COMPLETES each owed
        prior day inside the catchup window with the manual ``--day``
        machinery verbatim (the ``seams.run_day`` rerun — a day-scoped
        ``run_crossverify``: full export → gate → stage → publish), which
        itself clears the day's marker on a completed material verdict. Laws:

        * idempotent vs a day that DID start (the tail-started-then-died
          shape): the rerun re-renders deterministically, ``mark_staged``'s
          sha idempotency keeps already-published entries published and
          :func:`publish_staged` puts only pending keys — ZERO double-puts;
        * folded failures: the rerun's ``publish_failed`` count folds into
          this run's state (the final verdict stays loud); a rerun
          ``suspect`` is the step-D/K whole-run halt class; a rerun
          ``publish_failed`` / ``refused`` ABORTS the scan (the step-D/D2
          abort law — later days would fail identically; markers kept);
        * a rerun ``withheld_partial`` / ``blocked`` continues the scan —
          the day's journal now carries the honest record (step D2 re-gates
          it next run once the lake heals) and the marker stays until the
          day completes;
        * ``seams.run_day`` unbound (injected test seams) degrades honestly:
          the owed days are surfaced with ONE WARN, never silently dropped.

        HONEST envelope: each rerun is a full day pass (O(universe) exports +
        one lock-trio acquire); a day owed BEFORE its bars landed reruns to
        ``withheld_partial`` first and converges the run AFTER today's pull
        heals it (the D2 twin's heal-within-catchup shape); a permanently
        incompletable day retries once per run until it ages past
        ``catchup_days``, then stays surfaced via the beyond-window WARN +
        the panel (remediation: manual ``--day``).
        """
        if config.catchup_days <= 0:
            # The recursive rerun's structural bound (catchup_days=0) — and
            # an operator zero-catchup config: no owed scan at all here (the
            # panel's names-only surface still shows every marker), so a
            # multi-day catchup's inner reruns never emit misleading
            # beyond-window WARNs about the sibling markers the OUTER scan
            # is about to complete.
            return None
        owed_days = scan_owed_days(
            config.lake_root, today=config.day, lookback_days=config.catchup_days
        )
        if not owed_days:
            return None
        if seams.run_day is None:
            _LOG.warning(
                "crossverify.owed_rerun_unavailable",
                run_id=run_id,
                days=[day.isoformat() for day in owed_days],
                note="owed prior day(s) found but no day-rerun seam is bound — "
                "surfaced only; the markers stay for the next run",
            )
            return None
        for prior_day in owed_days:
            day_report = seams.run_day(prior_day)
            _LOG.info(
                "crossverify.owed_day_rerun",
                run_id=run_id,
                prior_day=prior_day.isoformat(),
                verdict=day_report.verdict,
                published=day_report.published,
                skipped_already=day_report.skipped_already,
                withheld=day_report.withheld,
                exit_code=day_report.exit_code,
            )
            state.publish_failed += day_report.publish_failed
            if day_report.verdict == "suspect":
                # The step-D/K halt law, mirrored: a halt inside the owed
                # day's rerun is a whole-run failure class.
                return _suspect(
                    f"owed-day catchup suspect for {prior_day.isoformat()}: {day_report.detail}"
                )
            if day_report.verdict == "publish_failed":
                # The step-D/D2 abort law: a broken grant/bucket fails every
                # later day identically — stop the scan (the folded count
                # keeps this run's final verdict loud); the untouched owed
                # markers stay for the next run.
                _LOG.error(
                    "crossverify.owed_catchup_aborted",
                    run_id=run_id,
                    prior_day=prior_day.isoformat(),
                    note="owed-day publish failed — later owed days left for the next run",
                )
                return None
            if day_report.verdict == "refused":
                # Lock/gate refusal inside the rerun — every later day would
                # refuse identically; the outer run's own step F takes its
                # honest turn next (markers kept for the next run).
                _LOG.warning(
                    "crossverify.owed_day_contended",
                    run_id=run_id,
                    prior_day=prior_day.isoformat(),
                    note="owed-day rerun refused (locks/gate) — markers kept for the next run",
                )
                return None
        return None

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
                        state.note_failed_keys(current.pending_keys())
                        state.publish_failed += len(current.pending_keys())
                        return _finish(_report("publish_failed", 2, detail=str(exc)))
                    state.recovered_prior += outcome.published
                    state.publish_failed += outcome.failed_count
                    state.note_failed_keys(outcome.failed_keys)
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
            # ── D2: withheld-day AUTO-RE-GATE (the runbook's "manual --day
            # re-run" remediation, automated for the heal-within-catchup
            # case — DEC-CROSSVERIFY-WITHHELD-REGATE-001). Skipped after a
            # nonretryable recovery abort exactly like the orphan scan (a
            # broken grant fails every later publish identically).
            terminal = _regate_withheld_scan()
            if terminal is not None:
                return terminal
            # ── D3: NEVER-STARTED owed-day catchup — the durable owed
            # markers the pull launchers write at pull-tail-commit ENTRY
            # (DEC-CROSSVERIFY-OWED-SENTINEL-001). After D2 (a day both
            # owed AND withheld-journaled converges either way — both
            # passes are idempotent); skipped after a nonretryable
            # recovery abort exactly like the D2/orphan scans.
            terminal = _owed_catchup_scan()
            if terminal is not None:
                return terminal

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
        report = _report(verdict, 0, detail=detail)
        if state.regated > 0 or state.recovered_prior > 0:
            # MATERIAL skip-day run (a weekend/holiday re-gate or recovery
            # published real S3 objects) — ride _finish so the summary +
            # last-run record fire; a true no-op skip stays silent below.
            return _finish(report)
        if not config.dry_run and report.verdict in _OWED_CLEAR_VERDICTS:
            # The non-material honest-skip arm (non-trading / Muhurat) still
            # settles the day's owed marker (DEC-CROSSVERIFY-OWED-SENTINEL-001
            # — a holiday marker must never wedge forever); the material arm
            # above cleared inside _finish.
            clear_owed(config.lake_root, config.day)
        return report

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

        # ── G: priority pull (spot-only — DEC-CROSSVERIFY-SPOT-ONLY-001) ───
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
        targets, excluded = seams.enumerate_targets(config.day)
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
    # acquire. The journal is RE-LOADED under the flock (round-3 X1, the
    # step-D law applied to today — refuter-confirmed MAJOR): two same-day
    # runs interleaved K_A,K_B,M_A,M_B leave run B's IN-MEMORY journal
    # stale after A publishes + rmtrees today's staging — publishing from
    # it read EVERY key as staged_bytes_missing → a FALSE publish_failed
    # exit 2 + CRITICAL while S3 was complete. Now the span acts only on
    # the durable journal: no pending keys ⇒ converge (zero puts, the
    # published entries counted skipped_already — the normal exit law).
    if not config.dry_run and config.publish and journal.entries:
        try:
            with partition_write_lock(config.lake_root, journal_path(config.lake_root, config.day)):
                current = load_journal(config.lake_root, config.day)
                if current is None or not current.pending_keys():
                    # Converged: a clean idempotent re-run or a concurrent
                    # same-day peer already published every journaled key
                    # (or the journal vanished/corrupted since step K —
                    # nothing attributable left to publish this run; the
                    # staged bytes stay in place, the orphan-staging law).
                    state.skipped_already = 0 if current is None else len(current.entries)
                    _LOG.info(
                        "crossverify.publish_converged",
                        run_id=run_id,
                        day=config.day.isoformat(),
                        journal_present=current is not None,
                        entries=state.skipped_already,
                        note=(
                            "no pending keys under the day flock — already published "
                            "(a clean re-run or a concurrent same-day peer); zero puts"
                        ),
                    )
                    if current is not None:
                        # The step-N cleanup, idempotent — a peer that
                        # finished the publish usually already removed it.
                        shutil.rmtree(staging_dir(config.lake_root, config.day), ignore_errors=True)
                else:
                    current.run_id = run_id  # the latest run owns the journal
                    try:
                        outcome = publish_staged(
                            client=_client(),
                            bucket=config.bucket,
                            journal=current,
                            lake_root=config.lake_root,
                            sleeper=seams.sleeper,
                        )
                    except S3BackendUnavailable as exc:
                        state.note_failed_keys(current.pending_keys())
                        state.publish_failed += len(current.pending_keys())
                        return _finish(_report("publish_failed", 2, detail=str(exc)))
                    state.published = outcome.published
                    state.skipped_already = outcome.skipped_already
                    state.publish_failed += outcome.failed_count
                    state.note_failed_keys(outcome.failed_keys)
                    if outcome.aborted_nonretryable:
                        return _finish(
                            _report(
                                "publish_failed",
                                2,
                                detail="nonretryable S3 error — whole publish aborted "
                                f"(failed keys: {', '.join(outcome.failed_keys)})",
                            )
                        )
                    if not current.pending_keys():
                        # The step-N staging cleanup, INSIDE the day lock: a
                        # full publish means the staging bytes served their
                        # purpose; the journal stays the durable done-record.
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
        # The retryable-exhaustion arm gets a REAL detail (the abort arm's
        # shape): the true failing count + the capped key sample, so the
        # summary alert / CLI line / last-run panel name what to look at.
        sample = ", ".join(state.failed_keys)
        return _finish(
            _report(
                "publish_failed",
                2,
                detail=(
                    f"{state.publish_failed} key(s) still failing after retries"
                    + (f" (first: {sample})" if sample else "")
                ),
            )
        )
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
    if state.published + state.skipped_already + state.recovered_prior + state.regated > 0:
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
        """Duck-typed :class:`CrossverifyAlerter` over the ``emit_*`` module helpers."""

        def _emit(self, helper_name: str, **kwargs: Any) -> bool:
            """Fire ONE named ``sweep_alerts`` helper; absent/raising → WARN + False."""
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
            """The exactly-once end-of-run summary; returns True iff attempted."""
            return self._emit("emit_crossverify_summary_alert", run_id=run_id, report=report)

        def crossverify_refused(self, *, run_id: str, reason: str) -> bool:
            """A CRITICAL refusal (lock exhaustion / gate absent / calendar abstain)."""
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

    def _enumerate(day: date) -> tuple[list[CanonicalIdentity], int]:
        """§4.2 store-authoritative WHOLE-SPOT-UNIVERSE enumeration (#1788).

        Export enumerates exactly what the spot pull covers — the SAME
        :func:`whole_universe_spot_targets` set the pull leg pulls (every
        admitted index + every admitted stock, reference-only NTM names
        included) — plus the engine-trio STRUCTURAL FLOOR (``V1_INDICES``
        minus reference-only): a masterless lake still enumerates + BLOCKS
        on a missing brute-force index, so the export surface can never
        silently narrow below the trio. NO registry read — futures are out
        of the contract (DEC-CROSSVERIFY-SPOT-ONLY-001).
        """
        _ = day
        from brutex.models import InstrumentType
        from brutex.universe import V1_INDICES

        identities: list[CanonicalIdentity] = []
        excluded = 0
        # The engine-trio floor: every non-reference spec maps by contract.
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
        seen = {identity.canonical_symbol for identity in identities}
        store = opener(db_path)
        try:
            targets = whole_universe_spot_targets(store)
        finally:
            store.close()
        for inst in targets:
            segment = "INDEX" if inst.instrument is InstrumentType.IDX else "CASH"
            identity = canonical_identity(
                exchange=inst.exch_id.value, segment=segment, lake_symbol=inst.symbol_name
            )
            if identity is None:
                excluded += 1
                continue
            if identity.canonical_symbol in seen:
                continue  # the floor trio re-enumerated from the store
            seen.add(identity.canonical_symbol)
            identities.append(identity)
        return identities, excluded

    def _run_pull() -> PullLegReport:
        store = opener(db_path)
        try:
            return run_pull_leg(
                lake_root=lake_root,
                day=config.day,
                lookback_days=config.pull_lookback_days,
                store=store,
                session_rate=session_rate,
                market_closed=market_closed,
                spot_puller=spot_puller,
            )
        finally:
            store.close()

    def _run_settle_retry(affected: Sequence[CanonicalIdentity]) -> PullLegReport:
        store = opener(db_path)
        try:
            return run_settle_retry(
                lake_root=lake_root,
                day=config.day,
                lookback_days=config.pull_lookback_days,
                affected=affected,
                store=store,
                session_rate=session_rate,
                market_closed=market_closed,
                spot_puller=spot_puller,
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

    def _run_day(prior_day: date) -> CrossverifyReport:
        """The step-D3 owed-day rerun (DEC-CROSSVERIFY-OWED-SENTINEL-001):
        the manual ``--day`` machinery VERBATIM — the SAME ``run_crossverify``
        step machine over a day-scoped config (``skip_pull=True``: vendor-free
        — today's run's pull leg owns the vendor window, the lake is read
        as-is; ``catchup_days=0``: the rerun scans nothing — the structural
        recursion bound) over fresh day-bound default seams carrying the SAME
        injections (so the day-scoped prebuild/holder-note/pull closures bind
        the PRIOR day, never today). The rerun acquires the lock trio itself
        in its own step F — legal because step D3 runs BEFORE the outer run's
        step F (nothing is held; the day-journal-flock leaf law is per day).
        """
        from dataclasses import replace as _replace

        day_config = _replace(config, day=prior_day, skip_pull=True, catchup_days=0)
        return run_crossverify(
            day_config,
            default_seams(
                day_config,
                env=env,
                store_opener=store_opener,
                account_key_resolver=account_key_resolver,
                lease_factory=lease_factory,
                spot_puller=spot_puller,
                materialize=materialize,
                derive_daily=derive_daily,
                read_window_factory=read_window_factory,
                write_daily_factory=write_daily_factory,
                s3_client_factory=s3_client_factory,
                alerter=alerter,
                session_rate=session_rate,
            ),
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
        run_day=_run_day,
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
