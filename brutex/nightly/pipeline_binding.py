"""Headless binding of the dashboard pull pipeline (DEC-SWEEP-NIGHTLY-AUTOMATION-001).

Mirrors the ``/pull-pipeline`` route's closures (``brutex/dashboard/app.py``
``_work`` body, app.py:3271-3742 at spec time) onto EXISTING public entry
points, minus the dashboard-only surfaces — each omission justified:

* ``RatePanelSampler`` / ``make_rate_observer`` / the ``.rate`` bridge publish:
  observe-only widget feeds (DEC-PULL-RATE-WIDGET-VISIBILITY-001 — "touches no
  pacing"); pacing binds identically via the shared
  :class:`~brutex.dashboard.pull_controller.SessionRateController` + the
  500/min governor. A concurrently-open dashboard's rate widget reads
  unwired/idle during a nightly (honest runbook note).
* ``BackgroundPullJob`` / ``set_phase`` / progress hooks: dashboard
  single-flight + live-progress UI. The nightly holds the pull locks itself
  and runs foreground under launchd; progress = report + audit + Telegram.
  Every hook is optional-default-``None`` in every spine signature.
* ``PrebuildGate`` / movers warm / cache ``mark_dirty``: process-local
  dashboard caches. A separately-running dashboard self-invalidates via the
  window-ledger content token (``lake_revision``, DEC-HARDENING-LEFTOVERS-001
  #1149) — cross-process invalidation is already the law.
* ``_greeks_health.observe``: dashboard health-streak UI; the enrich pass runs
  inside the composite spine regardless.

Mid-pull durable ledger checkpointing needs NO caller wiring:
``pull_all_spot`` internally builds ``make_live_ledger_flush(ledger,
lake_root)`` (pull_controller.py:1819) — the headless binding inherits the
durable mid-pull checkpoint; the CLI ``cmd_fetch`` wires it manually only
because it drives the lower-level ingest spine directly.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from brutex.models import BruteXError
from brutex.nightly.chain import LinkOutcome, LinkResult, LinkStatus, NightlyLink, Tripwire

if TYPE_CHECKING:
    from brutex.dashboard.composite_pull import CompositePullReport
    from brutex.dashboard.metadata_pull import MetadataPullReport
    from brutex.dashboard.pipeline_prebuild import PrebuildOutcome
    from brutex.dashboard.pipeline_pull import PipelinePullReport, PriorStepsVerdict
    from brutex.dashboard.pull_controller import BulkSpotReport, SessionRateController
    from brutex.data.instrument_store import InstrumentStore

logger = structlog.get_logger(__name__)

__all__ = [
    "HeadlessPipeline",
    "NightlyCapabilityError",
    "build_headless_pipeline",
    "decompose_pipeline_report",
    "execute_headless_pipeline",
    "nightly_metadata_years",
]

#: The metadata year-grid floor when no ``from_date`` is given — the SAME 2020
#: floor the dashboard's ``_resolve_metadata_years`` uses (GROWW_HISTORY_FLOOR
#: year; parity-pinned in tests).
_METADATA_YEAR_FLOOR = 2020


class NightlyCapabilityError(BruteXError):
    """The lake cannot run a pull (no instrument master) — an honest REFUSAL.

    The PULL link wrapper maps this to ``REFUSED`` (capability gate), never
    ``FAILED``: the remediation is operator action, not a retry.
    """


def _utcnow() -> datetime:
    """Return the current timezone-aware UTC timestamp."""
    return datetime.now(UTC)


def nightly_metadata_years(
    from_date: str | None,
    to_date: str | None,
    now: Callable[[], datetime] = _utcnow,
) -> list[int]:
    """The inclusive metadata year grid — the headless mirror of the dashboard law.

    Floor = ``year(from_date)`` when given else 2020; ceiling = ``year(to_date)``
    when given else ``now().year``; a reversed range raises :class:`ValueError`.
    Parity with ``app._resolve_metadata_years`` is PINNED in tests over a
    permutation grid (the dashboard helper is module-level and importable).
    """
    floor = date.fromisoformat(from_date).year if from_date else _METADATA_YEAR_FLOOR
    ceiling = date.fromisoformat(to_date).year if to_date else now().year
    if floor > ceiling:
        raise ValueError(
            f"from_date year {floor} is after to_date year {ceiling} — empty year range"
        )
    return list(range(floor, ceiling + 1))


@dataclass(frozen=True, slots=True)
class HeadlessPipeline:
    """The four bound phase callables + metadata for the PULL link."""

    run_spot: Callable[[], BulkSpotReport]
    run_metadata: Callable[[], MetadataPullReport]
    run_historical: Callable[[PriorStepsVerdict], CompositePullReport]
    run_prebuild: Callable[[frozenset[str]], PrebuildOutcome]
    strict_gate: bool
    targets: int  # count, for the report/log line
    close: Callable[[], None]  # closes the InstrumentStore
    #: Market-hours pull guard (DEC-GROWW-MARKET-HOURS-GUARD-001): the live-clock
    #: predicate threaded into ``run_full_pipeline`` so a headless pull that reaches
    #: the sequencer inside the NSE market-hours window skips the whole vendor chain.
    #: ``None`` (a dry-run build, or a directly-constructed test double) never gates.
    market_closed: Callable[[], bool] | None = None


def build_headless_pipeline(
    *,
    lake_root: Path,
    dry_run: bool = False,
    from_date: str | None = None,
    to_date: str | None = None,
    env: Mapping[str, str] | None = None,
    store_opener: Callable[[Path], InstrumentStore] | None = None,
    session_rate: SessionRateController | None = None,
    now: Callable[[], datetime] = _utcnow,
) -> HeadlessPipeline:
    """Bind the four pipeline phases headlessly (each item mirrors app.py).

    Parameters mirror the dashboard route's load-bearing inputs; every heavy
    dependency is injected or imported call-time (the cli.py house pattern).
    ``store_opener`` defaults to :meth:`InstrumentStore.open`.

    Raises
    ------
    NightlyCapabilityError
        No instrument master at ``<lake_root>/instruments.duckdb`` (or an
        empty one) — the honest capability refusal ("open /universe and load
        instruments once, then re-run").
    ValueError
        A junk ``BRUTEX_PIPELINE_STRICT_GATE`` value (fail-loud, resolved ONCE
        at build — the pre-chain refusal), or junk ISO dates.
    """
    from brutex.adapters.groww.expiry_scope import normalize_expiry_scope
    from brutex.calendars.market_hours import resolve_market_closed
    from brutex.dashboard.composite_pull import pull_historical_for_underlyings
    from brutex.dashboard.fno_cascade import build_fno_cascade
    from brutex.dashboard.metadata_pull import pull_expiries_contracts
    from brutex.dashboard.pipeline_prebuild import PrebuildTarget, run_pipeline_prebuild_guarded
    from brutex.dashboard.pipeline_pull import strict_gate_enabled
    from brutex.dashboard.prebuild_parallel import resolve_prebuild_workers
    from brutex.dashboard.pull_controller import (
        GROWW_HISTORY_FLOOR,
        SessionRateController,
        default_date_range,
        pull_all_spot,
        resolve_fetch_intervals,
    )
    from brutex.dashboard.universe_pull import select_pull_instruments
    from brutex.data.instrument_store import DEFAULT_DB_NAME, InstrumentStore
    from brutex.data.lake_footprint import make_disk_guard, record_lake_footprint
    from brutex.data.registry_store import RegistryStore
    from brutex.data.timeframe_materialize import MaterializeResult, materialize_lake_timeframes

    strict_gate = strict_gate_enabled(env)  # fail-loud, once at build

    db_path = lake_root / DEFAULT_DB_NAME
    if not db_path.exists():
        raise NightlyCapabilityError(
            f"no instrument master at {db_path} — open /universe and load "
            "instruments once, then re-run"
        )
    opener = InstrumentStore.open if store_opener is None else store_opener
    store = opener(db_path)
    try:
        # The exact whole-universe selection the dashboard's _select_bulk_targets
        # closure performs (route-nested, not importable — mirrored here).
        eligible = store.fno_eligible_underlyings()
        candidates = store.list_index_equity_instruments()
        targets = select_pull_instruments(
            candidates, kind="index", fno_eligible=eligible
        ) + select_pull_instruments(candidates, kind="stock", fno_eligible=eligible)
        if not targets:
            raise NightlyCapabilityError(
                f"instrument master at {db_path} is EMPTY — open /universe and "
                "load instruments once, then re-run"
            )
        reference_only_ids = store.reference_only_security_ids()
        tradable = [inst for inst in targets if inst.security_id not in reference_only_ids]
        years = nightly_metadata_years(from_date, to_date, now)
    except BaseException:
        store.close()
        raise

    fetch_intervals = resolve_fetch_intervals(None)  # ("1minute",)
    registry_path = lake_root / "registry.duckdb"
    rate = SessionRateController() if session_rate is None else session_rate

    # Market-hours pull guard (DEC-GROWW-MARKET-HOURS-GUARD-001): the nightly 01:30 IST
    # headless pull is the MOTIVATING case — it must STOP gracefully at 08:45 if it runs
    # long. The env kill switch is parsed ONCE here at build (fail-loud on junk); the
    # bound predicate then reads the live clock only (no per-window env re-parse → a junk
    # env set mid-pull can never crash the long nightly run). A dry-run threads ``None``
    # (no vendor calls to gate), so nightly dry-run tests are byte-identical.
    _market_closed = None if dry_run else resolve_market_closed(env=env)

    # F&O expiry scope with the LIVE-edge normalization the dashboard applies
    # (DEC-EXPIRY-SCOPE-LIVE-EDGE-001): a default-shaped bound is lifted to
    # None so live/unexpired contracts are never scoped out.
    fno_scope_start, fno_scope_end = normalize_expiry_scope(
        date.fromisoformat(from_date) if from_date else None,
        date.fromisoformat(to_date) if to_date else None,
        envelope_end=date.fromisoformat(default_date_range()[1]),
        floor=GROWW_HISTORY_FLOOR,
    )

    def _run_spot() -> BulkSpotReport:
        # app.py:3282-3297 minus the dashboard-only kwargs (on_progress,
        # rate_observer, on_ledger_sync). Blank dates ⇒ the canonical
        # [2020-01-01, prev-day] default inside resolve_candle_range.
        return pull_all_spot(
            targets,
            lake_root=lake_root,
            dry_run=dry_run,
            from_date=from_date,
            to_date=to_date,
            year=None,
            intervals=fetch_intervals,
            session_rate=rate,
            auto_retry=True,
            until_clean=True,
            floor_store=store,
            market_closed=_market_closed,
        )

    def _run_metadata() -> MetadataPullReport:
        # app.py:3299-3314 minus on_progress.
        with RegistryStore.open(registry_path) as registry:
            return pull_expiries_contracts(
                tradable,
                registry=registry,
                coverage_store=store,
                years=years,
                dry_run=dry_run,
                session_rate=rate,
                # DEC-GROWW-MARKET-HOURS-GUARD-001: a metadata phase CROSSING 08:45
                # halts mid-phase (per-cell belt), like the spot/historical spines.
                market_closed=_market_closed,
            )

    def _run_historical(prior_steps: PriorStepsVerdict) -> CompositePullReport:
        # app.py:3347-3365 minus on_progress/on_bars/on_ledger_sync (all
        # optional UI-tally hooks); prior_steps threading preserved so the
        # strict gate's Greeks-withhold seam works headlessly unchanged;
        # heal-before-enrich lives INSIDE this spine.
        with RegistryStore.open(registry_path) as registry:
            return pull_historical_for_underlyings(
                targets,
                reference_only_ids=reference_only_ids,
                registry=registry,
                coverage_store=store,
                lake_root=lake_root,
                dry_run=dry_run,
                intervals=("1minute",),
                years=years,
                session_rate=rate,
                until_clean=True,
                scope_start=fno_scope_start,
                scope_end=fno_scope_end,
                prior_steps=prior_steps,
            )

    def _run_prebuild(exclude_symbols: frozenset[str]) -> PrebuildOutcome:
        # The app.py:3662-3718 closure mirrored: the per-target _materialize
        # closure (NEVER materialize_lake_timeframes passed raw), one cutoff
        # resolved per run so a midnight-IST straddle derives one 1day set.
        from brutex.calendars.nse import IST
        from brutex.dashboard.db_view import list_fno_candle_datasets

        prebuild_cutoff = now().astimezone(IST).date()

        def _materialize(target: PrebuildTarget) -> MaterializeResult:
            return materialize_lake_timeframes(
                lake_root=lake_root,
                exchange=target.exchange,
                segment=target.segment,
                symbol=target.symbol,
                kind=target.kind,
                include_daily=True,
                daily_before=prebuild_cutoff,
                dense_symbol=target.dense,
            )

        outcome = run_pipeline_prebuild_guarded(
            instruments=targets,
            cascade_or_error=lambda: build_fno_cascade(list_fno_candle_datasets(lake_root)),
            materialize=_materialize,
            source="nightly",
            exclude_symbols=exclude_symbols,
            lake_root=lake_root,
            workers=resolve_prebuild_workers(target_count=len(targets)),
            daily_before=prebuild_cutoff,
            disk_guard=make_disk_guard(lake_root, store.get_meta),
        )
        if not outcome.disk_guard_blocked:
            # Best-effort observed-multiplier persistence (app.py:3717 law).
            record_lake_footprint(lake_root, store.set_meta)
        return outcome

    logger.info(
        "nightly.pipeline.bound",
        targets=len(targets),
        tradable=len(tradable),
        years=years,
        dry_run=dry_run,
        strict_gate=strict_gate,
    )
    return HeadlessPipeline(
        run_spot=_run_spot,
        run_metadata=_run_metadata,
        run_historical=_run_historical,
        run_prebuild=_run_prebuild,
        strict_gate=strict_gate,
        targets=len(targets),
        close=store.close,
        market_closed=_market_closed,
    )


def execute_headless_pipeline(hp: HeadlessPipeline) -> PipelinePullReport:
    """Run the ONE pipeline sequencer over the bound phases (movers warm None)."""
    from brutex.dashboard.pipeline_pull import run_full_pipeline

    return run_full_pipeline(
        run_spot=hp.run_spot,
        run_metadata=hp.run_metadata,
        run_historical=hp.run_historical,
        run_prebuild=hp.run_prebuild,
        strict_gate=hp.strict_gate,
        market_closed=hp.market_closed,
    )


def decompose_pipeline_report(
    report: PipelinePullReport,
    *,
    started_utc: float,
    ended_utc: float,
) -> tuple[LinkResult, tuple[Tripwire, ...]]:
    """Map ONE ``PipelinePullReport`` onto the PULL + PREBUILD links (spec §3.4).

    The two reported links ride one execution: the returned :class:`LinkResult`
    is the PULL link's; when the prebuild phase RAN, its outcome travels in
    ``extra_outcomes`` as a fully-built PREBUILD :class:`LinkOutcome` (a
    *derivation* from the same report, never a second execution). When the
    pipeline stopped BEFORE the prebuild, no extra outcome is emitted — the
    chain sequencer's BLOCKED synthesis applies (``blocked_by=PULL``).

    Tripwire **T3 (pull_report_coherence)**: negative counts on the report mark
    the run SUSPECT (the ``ok``/``stopped_after`` cross-check is structurally
    impossible — ``ok`` is a property derived from ``stopped_after`` — so only
    the reachable incoherences are wired).

    Defense in depth: even a mis-read prebuild verdict cannot poison the sweep
    — ``cmd_sweep_all``'s own no-escape derived-freshness gate (preflight gate
    6) refuses stale TFs regardless.
    """
    tripwires: list[Tripwire] = []

    counts: dict[str, int] = {"pull_quarantined": report.quarantined_count}
    historical = report.historical
    if historical is not None and historical.historical is not None:
        counts["pull_bars"] = historical.historical.bars_appended
        if historical.historical.bars_appended < 0:
            tripwires.append(
                Tripwire(
                    name="pull_report_coherence",
                    link=NightlyLink.PULL,
                    detail=f"negative bars_appended {historical.historical.bars_appended}",
                )
            )
    if report.quarantined_count < 0:
        tripwires.append(
            Tripwire(
                name="pull_report_coherence",
                link=NightlyLink.PULL,
                detail=f"negative quarantined_count {report.quarantined_count}",
            )
        )
    if report.prebuild_built is not None and report.prebuild_built < 0:
        tripwires.append(
            Tripwire(
                name="pull_report_coherence",
                link=NightlyLink.PREBUILD,
                detail=f"negative prebuild_built {report.prebuild_built}",
            )
        )

    extras: tuple[LinkOutcome, ...] = ()
    prebuild_ran = (
        report.prebuild_built is not None
        or report.prebuild_crashed is not None
        or report.prebuild_disk_guarded
    )
    if report.post_steps_strict_skipped:
        extras = (
            LinkOutcome(
                link=NightlyLink.PREBUILD,
                status=LinkStatus.SKIPPED,
                started_utc=started_utc,
                ended_utc=ended_utc,
                detail="withheld (strict gate) — re-derived next pull",
                counts={},
                error_type=None,
                blocked_by=None,
                exit_hint=None,
            ),
        )
    elif prebuild_ran:
        prebuild_failed = (
            report.prebuild_crashed is not None
            or report.prebuild_disk_guarded
            or bool(report.prebuild_failed)
        )
        detail_parts: list[str] = []
        if report.prebuild_crashed is not None:
            detail_parts.append(f"prebuild CRASHED ({report.prebuild_crashed})")
        if report.prebuild_disk_guarded:
            detail_parts.append("prebuild skipped by the disk guard")
        if report.prebuild_failed:
            detail_parts.append("failed symbols: " + ", ".join(report.prebuild_failed))
        prebuild_counts: dict[str, int] = {"prebuild_excluded": report.quarantined_count}
        if report.prebuild_built is not None:
            prebuild_counts["prebuild_built"] = report.prebuild_built
        extras = (
            LinkOutcome(
                link=NightlyLink.PREBUILD,
                status=LinkStatus.FAILED if prebuild_failed else LinkStatus.OK,
                started_utc=started_utc,
                ended_utc=ended_utc,
                detail="; ".join(detail_parts) if detail_parts else "derived timeframes built",
                counts=prebuild_counts,
                error_type=None,
                blocked_by=None,
                exit_hint=None,
            ),
        )

    if report.ok:
        pull_status = LinkStatus.OK
        detail = report.note
    else:
        pull_status = LinkStatus.FAILED
        stopped = f"stopped_after={report.stopped_after}; " if report.stopped_after else ""
        detail = f"{stopped}{report.note}"

    return (
        LinkResult(
            status=pull_status,
            detail=detail[:500],
            counts=counts,
            error_type=None,
            exit_hint=None,
            extra_outcomes=extras,
        ),
        tuple(tripwires),
    )
