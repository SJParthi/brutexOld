"""One-CLICK full-universe pull pipeline — spot → metadata → historical, gated.

DEC-ONE-CLICK-PIPELINE-001 (the operator's "one button does the whole thing"
request, 2026-06-21). Today the ``/universe`` page exposes the data pull as THREE
separate buttons the operator clicks one after another, each waiting on the prior
to finish:

1. **Pull spot** — :func:`~brutex.dashboard.pull_controller.pull_all_spot`
   (every index + stock spot underlying, full ``[2020-01-01, previous day]``
   1-minute history; 1-day DERIVED after).
2. **Pull expiries + contracts** —
   :func:`~brutex.dashboard.metadata_pull.pull_expiries_contracts` (the F&O chain
   SHAPE — every expiry DATE + contract SYMBOL, ZERO candles).
3. **Pull historical** —
   :func:`~brutex.dashboard.composite_pull.pull_historical_for_underlyings` (the
   FUT + CE/PE candle fill over the freshly-cached registry rows).

This module is the **thin sequencer** that chains those three EXISTING,
separately-tested spines into one click — exactly the operator's wording: "start
from pull spot, once it is successfully completed then pull expiries and contracts,
and when it is successfully completed then pull historical". It is a SEQUENCER, not
a new engine: it owns no network call, no SDK, no sink — it only calls three
INJECTED callables in order and applies a between-phase GATE so a failed earlier
phase never silently kicks off a doomed later one.

## Why injected callables (the 100%-testable seam)

The route (``POST /api/v1/universe/pull-pipeline``) binds the three callables to
the real pull functions (with all their store / registry / lake / client
dependencies). This module receives them as plain ``Callable[[], Report]`` so the
ENTIRE control-flow — the gates, the stop-early branches, the note synthesis — is
unit-testable with trivial fakes and ZERO network (CLAUDE.md §11 COMMON +
AUTOMATED; Golden Rule #1 — no fabricated data, the fakes return the real report
dataclasses).

## The gate (the operator's "once it is successfully completed" rule)

Each phase has a pure, concrete-typed gate that inspects ITS report's FAILURE
signals (not "did work happen" — a zero-call no-op is not a failure):

* :func:`spot_phase_ok` — the spot pull neither died on auth, paused on the daily
  quota, nor left any window failing after the in-run retry budget.
* :func:`metadata_phase_ok` — the chain enumeration raised on ZERO expiry cells
  AND ZERO contract jobs.
* :func:`historical_phase_ok` — the candle pull neither died on auth nor left any
  window / contract-pair failing (a ``None`` candle sub-report — no tradable
  contracts, e.g. a dry-run or all-reference-only selection — is a no-op, NOT a
  failure).

:func:`run_full_pipeline` runs phase 1; if its gate fails it STOPS (``stopped_after
= "spot"``) and never touches the vendor again. Same between phase 2 and phase 3.
A clean run reaches all three and reports ``stopped_after = None``.

## Honest envelope (Golden Rule #1 + #10)

* **No re-implementation** — the three pull spines are CALLED, never duplicated;
  this module adds only the gate + sequencing. Phase 3's composite re-runs the
  metadata enumeration internally, but incrementally (it skips all but the
  current-year / still-open cells phase 2 already settled — idempotent, Golden
  Rule #7), so the separate phase-2 gate is preserved without a wasteful full
  re-pull. The residual open-cell re-run is itself gated:
  :func:`historical_phase_ok` also checks the composite's inner metadata
  sub-report, so a transient enumeration failure during phase 3 never reads as a
  clean run.
* **Network-bound, not O(1)-total** — this is a full-history pull; the sequencer
  itself is O(1) (three calls + three gate checks), but the work it triggers is
  the real multi-hour vendor fill. No throughput claim is made here.
* **Fail-CLOSED** — a failed earlier phase stops the chain; the later phases are
  reported ``None`` (never silently skipped, never falsely "complete").
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

import structlog

from brutex.dashboard.composite_pull import CompositePullReport
from brutex.dashboard.metadata_pull import MetadataPullReport
from brutex.dashboard.movers_warm import MoversWarmResult
from brutex.dashboard.pipeline_prebuild import PrebuildOutcome
from brutex.dashboard.pull_controller import BulkSpotReport
from brutex.data.fno_symbol import parse_fno_symbol

_LOG = structlog.get_logger(__name__)

#: The ordered pipeline phases (used for ``stopped_after`` + audit labels).
PHASE_SPOT = "spot"
PHASE_METADATA = "metadata"
PHASE_HISTORICAL = "historical"
#: The OPTIONAL gated movers phase: warm the movers snapshots ONLY after every
#: fetch phase succeeds AND the prebuild SUCCEEDED (``PrebuildOutcome.ok`` —
#: DEC-PIPELINE-AUTOPREBUILD-001 chunk 2; movers read the DERIVED timeframes,
#: which only the prebuild writes now, so a pre-prebuild or failed-prebuild
#: warm would read stale/absent partitions).
PHASE_MOVERS = "movers"
#: The gated PREBUILD phase (DEC-PIPELINE-AUTOPREBUILD-001, superseding the
#: DEC-UNIVERSE-PULL-CONSOLIDATE-001 shape): pre-build ALL derived timeframes
#: (19 intraday + the derived ``1day``) for SPOT + FUTURES + OPTIONS ONLY after
#: every fetch phase succeeds — the pipeline's FINAL data stage, run BEFORE the
#: movers warm.
PHASE_PREBUILD = "prebuild"

#: The three PER-SEGMENT movers-warm labels. DEC-PIPELINE-AUTOPREBUILD-001
#: SUPERSEDES the early-warm arm of DEC-PIPELINE-SEGMENT-MOVERS-001 ("SPOT
#: warms the moment the spot phase succeeds"): derived data is now written ONLY
#: by the prebuild phase, so an early spot warm would read stale/absent derived
#: partitions — ALL THREE segments warm together AFTER the prebuild. The labels
#: remain the audit keys for the per-segment ``movers_warmed`` map.
SEGMENT_SPOT = "spot"
SEGMENT_FUTURES = "futures"
SEGMENT_OPTIONS = "options"


def spot_phase_ok(report: BulkSpotReport) -> bool:
    """Return ``True`` iff the SPOT phase finished without a blocking failure.

    The spot pull may proceed to the metadata phase only when it neither (a) died
    on a dead Groww token mid-pull (``auth_aborted`` — the loud ``auth_expired``
    verdict), (b) paused on the daily vendor quota (``quota_exhausted`` — resume
    after the IST-midnight reset), nor (c) left any window still failing after the
    in-run retry budget (``still_failed_windows``). A dry-run with zero bars is NOT
    a failure (no creds is reported separately); the gate detects FAILURE signals,
    not absence of work.

    Parameters
    ----------
    report
        The :class:`~brutex.dashboard.pull_controller.BulkSpotReport` from
        :func:`~brutex.dashboard.pull_controller.pull_all_spot`.

    Returns
    -------
    bool
        ``True`` when the phase is clean enough to proceed to metadata.
    """
    return (
        not report.auth_aborted and not report.quota_exhausted and report.still_failed_windows == 0
    )


def metadata_phase_ok(report: MetadataPullReport) -> bool:
    """Return ``True`` iff the METADATA phase enumerated the chain cleanly.

    The metadata enumeration may proceed to the historical candle phase only when
    NO Phase-1 expiry cell raised (``expiry_failures``) AND NO Phase-2 contract
    job raised (``contract_failures``). A dry-run that honestly persists an EMPTY
    chain (no creds) raises nothing, so it passes the gate (the historical phase
    then finds no tradable contracts — a reported no-op, never a fabricated pull).

    Parameters
    ----------
    report
        The :class:`~brutex.dashboard.metadata_pull.MetadataPullReport` from
        :func:`~brutex.dashboard.metadata_pull.pull_expiries_contracts`.

    Returns
    -------
    bool
        ``True`` when the chain enumeration is clean enough to proceed to candles.
    """
    return report.expiry_failures == 0 and report.contract_failures == 0


def historical_phase_ok(report: CompositePullReport) -> bool:
    """Return ``True`` iff the HISTORICAL candle phase finished without a failure.

    The composite delegates to BOTH spines internally — it RE-RUNS the chain
    enumeration (phase a, incrementally — only the not-yet-settled current-year /
    open cells actually re-fetch) THEN pulls candles (phase b). This gate therefore
    inspects BOTH sub-reports:

    * the inner **metadata** sub-report (``report.metadata``) must itself pass
      :func:`metadata_phase_ok` — a transient enumeration failure on an open cell
      that the incremental skip re-ran during phase 3 must NOT read as clean even
      though the explicit phase-2 gate already passed (``None`` when the composite
      enumerated nothing — a no-op, not a failure);
    * the inner **candle** sub-report
      (:class:`~brutex.dashboard.historical_pull.HistoricalPullReport`, ``None``
      when there was no tradable contract to pull — a dry-run / all-reference-only
      selection — a zero-call no-op, NOT a failure) is clean only when it neither
      died on auth (``auth_aborted``), left any window failing
      (``still_failed_windows``), nor quarantined any contract pair on a terminal
      error (``failed_pairs``).

    Parameters
    ----------
    report
        The :class:`~brutex.dashboard.composite_pull.CompositePullReport` from
        :func:`~brutex.dashboard.composite_pull.pull_historical_for_underlyings`.

    Returns
    -------
    bool
        ``True`` when BOTH inner phases are clean (or were reported no-ops).
    """
    meta = report.metadata
    if meta is not None and not metadata_phase_ok(meta):
        return False
    hist = report.historical
    if hist is None:
        return True
    return not hist.auth_aborted and hist.still_failed_windows == 0 and hist.failed_pairs == 0


#: How many quarantined symbol NAMES :attr:`PipelinePullReport.quarantined_symbols`
#: carries (and the note shows) — the TRUE total always rides
#: :attr:`PipelinePullReport.quarantined_count`, and the note appends an honest
#: ``(+K more)`` remainder computed from it (Golden Rule #1 — the cap never
#: hides the real count).
MAX_QUARANTINED_NAMES: Final[int] = 10


def derive_quarantined_symbols(
    spot: BulkSpotReport | None, historical: CompositePullReport | None
) -> tuple[tuple[str, ...], int]:
    """The deduped, sorted quarantined-symbol names + the TRUE total — pure.

    DEC-PIPELINE-AUTOPREBUILD-001 chunk 3 (headline honesty): a per-symbol
    quarantine does NOT stop the pipeline (the phase gates check pull-wide
    signals), so a completed run could read a bare "pipeline complete" while
    symbols silently failed. This derives the NAMES from the phase reports:

    * **spot** — every :class:`~brutex.dashboard.pull_controller.SymbolPullResult`
      with ``failed > 0`` (a terminally-quarantined ``(symbol, interval)`` pair;
      a pull-wide ``still_failed_windows`` residual stops the chain at the gate
      and never reaches the complete note).
    * **historical** — every in-window contract that did NOT land clean
      (``start_dates`` minus ``phase1_complete`` — quarantined pairs AND
      still-failing transients), folded to its UNDERLYING via the shared
      :func:`~brutex.data.fno_symbol.parse_fno_symbol` decode (an undecodable
      symbol keeps its raw name — reported, never dropped).

    Returns ``(names, true_count)``: *names* deduped + sorted, capped at
    :data:`MAX_QUARANTINED_NAMES`; *true_count* is the UNCAPPED total so the
    note's ``(+K more)`` remainder is honest. ``((), 0)`` for a clean run.
    """
    names: set[str] = set()
    if spot is not None:
        names.update(s.symbol for s in spot.symbols if s.failed > 0)
    hist = historical.historical if historical is not None else None
    if hist is not None:
        for symbol in set(hist.start_dates) - set(hist.phase1_complete):
            parts = parse_fno_symbol(symbol)
            names.add(symbol if parts is None else parts.underlying)
    ordered = tuple(sorted(names))
    return ordered[:MAX_QUARANTINED_NAMES], len(ordered)


@dataclass(slots=True)
class PipelinePullReport:
    """The honest outcome of one one-click spot→metadata→historical pipeline run.

    Carries each phase's own report (or ``None`` if the phase never ran because an
    earlier gate stopped the chain) plus ``stopped_after`` — the phase whose gate
    failed (``None`` when all three ran). Every phase is auditable from one object.

    Attributes
    ----------
    spot
        The phase-1 :class:`~brutex.dashboard.pull_controller.BulkSpotReport`
        (always present — phase 1 always runs).
    metadata
        The phase-2 :class:`~brutex.dashboard.metadata_pull.MetadataPullReport`
        (``None`` when phase 1's gate stopped the chain).
    historical
        The phase-3
        :class:`~brutex.dashboard.composite_pull.CompositePullReport` (``None``
        when phase 1 or phase 2's gate stopped the chain).
    stopped_after
        The phase whose gate failed and halted the chain — :data:`PHASE_SPOT` or
        :data:`PHASE_METADATA`; ``None`` when all three phases ran.
    note
        An honest one-line outcome summary.
    movers_warmed
        The TOTAL number of movers snapshots warmed across every segment that warmed
        (the sum of :attr:`segment_movers_warmed`), or ``None`` when NO segment warmer
        ran at all. DEC-PIPELINE-AUTOPREBUILD-001 supersedes the early-warm arm of
        DEC-PIPELINE-SEGMENT-MOVERS-001: derived data is written ONLY by the prebuild
        phase now, so ALL segments warm together AFTER the prebuild (an early spot
        warm would read stale/absent derived partitions). ``0`` is an honest "a
        segment ran but warmed nothing" (e.g. an empty lake), distinct from ``None``
        "no segment ran".
    segment_movers_warmed
        The per-segment warmed-count map (:data:`SEGMENT_SPOT` / :data:`SEGMENT_FUTURES`
        / :data:`SEGMENT_OPTIONS` → count) for every segment whose warmer was injected
        AND whose warm gate passed (a clean fetch AND a SUCCESSFUL prebuild —
        ``PrebuildOutcome.ok``, the chunk-2 tightening). A segment that
        did not warm is ABSENT from the map (never a fabricated count, Golden Rule #1).
        Empty when no segment warmed.
    prebuild_built
        The number of symbols (underlyings + contracts) whose derived timeframes were
        pre-built by the gated prebuild phase (DEC-PIPELINE-AUTOPREBUILD-001 —
        :attr:`~brutex.dashboard.pipeline_prebuild.PrebuildOutcome.symbols_built`), or
        ``None`` when that phase did NOT run — either no ``run_prebuild`` was injected,
        OR the historical fetch did not pass its gate (pre-build runs ONLY after a
        fully-successful pull). ``0`` is an honest "ran, built nothing", distinct from
        ``None`` "did not run".
    prebuild_total_bars
        Total derived bars the prebuild phase wrote (intraday + ``1day``), or ``None``
        when the phase did not run.
    prebuild_failed
        The prebuild's failing symbol names (capped at 10 by the producer —
        :data:`~brutex.dashboard.pipeline_prebuild.MAX_REPORTED_FAILURES`); empty when
        the phase did not run or had no failures.
    prebuild_crashed
        The error TYPE NAME (e.g. ``"ValueError"``) when the guarded prebuild
        CRASHED whole-run instead of completing
        (:attr:`~brutex.dashboard.pipeline_prebuild.PrebuildOutcome.crashed`);
        ``None`` when the phase completed (even with per-symbol failures) or
        did not run. The note renders ``", prebuild CRASHED (ErrorType)"``
        instead of the benign "pre-built for 0 symbol(s)" clause, so a crash
        is never mistakable for "ran, built 0" (Golden Rule #1).
    fs_allocation_note
        The lake-root allocation probe's one-line COARSE warning
        (DEC-LAKE-FS-PROBE-001 — ``PrebuildOutcome.fs_note``): the prebuild
        writes ~0.5-1M tiny Parquet files, so an exFAT-class cluster wastes
        30-300 GB of pure slack; named here AND in the note so the operator
        sees it before the disk fills. ``None`` when the filesystem is
        fine-grained, the probe was unavailable, or the phase did not run.
    quarantined_symbols
        The deduped, sorted names of symbols a phase QUARANTINED per-symbol
        without stopping the chain (:func:`derive_quarantined_symbols` — spot
        symbols with terminally-failed pairs + historical underlyings whose
        contracts did not land clean), capped at
        :data:`MAX_QUARANTINED_NAMES`. A quarantined symbol is ALWAYS named in
        the note — never hidden behind a bare "pipeline complete"
        (DEC-PIPELINE-AUTOPREBUILD-001 chunk 3).
    quarantined_count
        The TRUE (uncapped) quarantined-symbol total — the note's ``(+K more)``
        remainder derives from it, so the cap never lies (Golden Rule #1).
    """

    spot: BulkSpotReport | None
    metadata: MetadataPullReport | None
    historical: CompositePullReport | None
    stopped_after: str | None
    note: str
    movers_warmed: int | None = None
    segment_movers_warmed: dict[str, int] = field(default_factory=dict)
    segment_movers_failed: dict[str, int] = field(default_factory=dict)
    prebuild_built: int | None = None
    prebuild_total_bars: int | None = None
    prebuild_failed: tuple[str, ...] = ()
    prebuild_crashed: str | None = None
    fs_allocation_note: str | None = None
    quarantined_symbols: tuple[str, ...] = ()
    quarantined_count: int = 0

    @property
    def ok(self) -> bool:
        """Return ``True`` iff all three phases ran AND each phase gate passed.

        A short-circuited run (``stopped_after`` set) is never ``ok``; a full run
        is ``ok`` only when the spot, metadata, and historical gates all pass.
        """
        return (
            self.stopped_after is None
            and self.spot is not None
            and spot_phase_ok(self.spot)
            and self.metadata is not None
            and metadata_phase_ok(self.metadata)
            and self.historical is not None
            and historical_phase_ok(self.historical)
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly dict for the ``/pull-pipeline`` route + panel."""
        return {
            "ok": self.ok,
            "stopped_after": self.stopped_after,
            "note": self.note,
            "movers_warmed": self.movers_warmed,
            "segment_movers_warmed": dict(self.segment_movers_warmed),
            "segment_movers_failed": dict(self.segment_movers_failed),
            "prebuild_built": self.prebuild_built,
            "prebuild_total_bars": self.prebuild_total_bars,
            "prebuild_failed": list(self.prebuild_failed),
            "prebuild_crashed": self.prebuild_crashed,
            "fs_allocation_note": self.fs_allocation_note,
            "quarantined_symbols": list(self.quarantined_symbols),
            "quarantined_count": self.quarantined_count,
            "spot": self.spot.to_dict() if self.spot is not None else None,
            "metadata": self.metadata.to_dict() if self.metadata is not None else None,
            "historical": (self.historical.to_dict() if self.historical is not None else None),
        }


def _spot_stop_note(report: BulkSpotReport) -> str:
    """Build the honest stop-note for a spot-phase gate failure."""
    return (
        f"stopped after spot — {report.verdict.kind} "
        f"({report.still_failed_windows} window(s) still failing); "
        "metadata + historical NOT started"
    )


def _metadata_stop_note(report: MetadataPullReport) -> str:
    """Build the honest stop-note for a metadata-phase gate failure."""
    return (
        f"stopped after metadata — {report.expiry_failures} expiry + "
        f"{report.contract_failures} contract failure(s); historical NOT started"
    )


def _movers_note_clause(
    segment_movers_warmed: Mapping[str, int],
    segment_movers_failed: Mapping[str, int] | None = None,
) -> str:
    """Build the ``, movers …`` note clause from the per-segment warmed-count map.

    Empty when no segment warmed (no clause). Otherwise lists each warmed segment
    in the fixed :data:`SEGMENT_SPOT` → :data:`SEGMENT_FUTURES` → :data:`SEGMENT_OPTIONS`
    order (e.g. ``", movers spot 3 + futures 1 snapshot(s) warmed"``), so the note
    honestly distinguishes WHICH segments warmed — a segment whose warm gate failed
    (or whose warmer was not injected) is absent from the map and from the clause.
    When any warm JOB failed (DEC-MOVERS-WARM-PROGRESS-001 — the per-coordinate
    tally :class:`~brutex.dashboard.movers_warm.MoversWarmResult` now carries it),
    the clause appends an honest ``" (N warm job(s) FAILED)"`` so a swallowed
    best-effort failure is never hidden behind the warmed counts (Golden Rule #1).
    """
    ordered = [
        (segment, segment_movers_warmed[segment])
        for segment in (SEGMENT_SPOT, SEGMENT_FUTURES, SEGMENT_OPTIONS)
        if segment in segment_movers_warmed
    ]
    if not ordered:
        return ""
    parts = " + ".join(f"{segment} {count}" for segment, count in ordered)
    failed_total = sum((segment_movers_failed or {}).values())
    failed_clause = f" ({failed_total} warm job(s) FAILED)" if failed_total else ""
    return f", movers {parts} snapshot(s) warmed{failed_clause}"


#: How many failure NAMES the prebuild note clause shows before "(+K more)".
_NOTE_FAILURE_NAMES = 3


def _prebuild_note_clause(prebuild: PrebuildOutcome | None) -> str:
    """Build the ``, timeframes pre-built …`` note clause from the prebuild outcome.

    Empty when the prebuild phase did not run (``None``). A guarded whole-run
    CRASH (:attr:`PrebuildOutcome.crashed` set) renders the honest
    ``, prebuild CRASHED (ErrorType)`` — never the benign "pre-built for 0
    symbol(s)" a crash would otherwise masquerade as (Golden Rule #1).
    Otherwise names the built-symbol + bar counts, and — when the phase had
    failures — appends an honest ``, prebuild failures: A, B (+K more)``
    clause naming up to :data:`_NOTE_FAILURE_NAMES` symbols with the TRUE
    remainder count (``failed_count`` — the producer caps
    :attr:`PrebuildOutcome.failed` at 10 names, so the remainder uses the
    uncapped total; Golden Rule #1).
    """
    if prebuild is None:
        return ""
    if prebuild.crashed is not None:
        return f", prebuild CRASHED ({prebuild.crashed})"
    clause = (
        f", timeframes pre-built for {prebuild.symbols_built} symbol(s) "
        f"({prebuild.total_bars} bars)"
    )
    if prebuild.unchanged_skips:
        # DEC-PREBUILD-INCREMENTAL-001: name the incremental saving honestly —
        # these symbols were verified UNCHANGED and skipped, not built-with-0.
        clause += f", {prebuild.unchanged_skips} unchanged symbol(s) skipped (incremental)"
    if prebuild.failed:
        shown = ", ".join(prebuild.failed[:_NOTE_FAILURE_NAMES])
        more = prebuild.failed_count - min(_NOTE_FAILURE_NAMES, len(prebuild.failed))
        suffix = f" (+{more} more)" if more > 0 else ""
        clause += f", prebuild failures: {shown}{suffix}"
    if prebuild.fs_note is not None:
        # DEC-LAKE-FS-PROBE-001: a COARSE-allocation lake volume is named in the
        # headline too — the slack is spent by exactly this phase's tiny files.
        clause += f", ⚠ {prebuild.fs_note}"
    return clause


def _quarantine_clause(quarantined: tuple[str, ...], quarantined_count: int) -> str:
    """The ``"N symbol(s) quarantined: A, B (+K more) — "`` headline clause.

    Empty for a clean run (the complete note stays byte-identical). Otherwise
    names every capped symbol and appends the honest ``(+K more)`` remainder
    from the TRUE total, so a quarantined symbol can never hide behind a bare
    "pipeline complete" (DEC-PIPELINE-AUTOPREBUILD-001 chunk 3).
    """
    if not quarantined_count:
        return ""
    more = quarantined_count - len(quarantined)
    suffix = f" (+{more} more)" if more > 0 else ""
    return f"{quarantined_count} symbol(s) quarantined: {', '.join(quarantined)}{suffix} — "


def _complete_note(
    spot: BulkSpotReport,
    metadata: MetadataPullReport,
    historical: CompositePullReport,
    segment_movers_warmed: Mapping[str, int],
    prebuild: PrebuildOutcome | None = None,
    quarantined: tuple[str, ...] = (),
    quarantined_count: int = 0,
    segment_movers_failed: Mapping[str, int] | None = None,
) -> str:
    """Build the honest summary note for a fully-run pipeline.

    A non-empty quarantine set leads the note ("pipeline complete — N symbol(s)
    quarantined: … — …", :func:`_quarantine_clause`) so a per-symbol failure is
    NEVER hidden behind a bare "pipeline complete"; a fully-clean pull keeps
    the prior wording byte-identically. A WHOLE-pass Greeks-enrichment failure
    on the candle sub-report appends ", Greeks enrichment FAILED (<ErrorType>)
    — overlay re-attempted next pull" (DEC-GREEKS-ENRICH-OBSERVE-001; absent when the
    pass completed, never ran, or there was no candle sub-report). Appends a
    pre-build clause ONLY when the gated prebuild phase ran
    (:func:`_prebuild_note_clause` — built count + bars + capped failure
    names), THEN a per-segment movers-warm clause ONLY for the segments that
    actually warmed (:func:`_movers_note_clause`) — the prebuild-then-movers
    order mirrors the execution order (DEC-PIPELINE-AUTOPREBUILD-001).
    """
    hist = historical.historical
    bars = hist.bars_appended if hist is not None else 0
    contracts = hist.contracts_completed if hist is not None else 0
    # LOUD ledger-reload failure + VISIBLE settled-window skips
    # (DEC-WINDOW-LEDGER-RESCUE-001): the /universe pipeline note is the operator's
    # primary pull surface, so a lost cross-run memory is ANNOUNCED up front and the
    # no-vendor-hit skip count (spot + F&O phases summed) is shown when non-zero.
    ledger_failed = spot.ledger_load_failed or (hist is not None and hist.ledger_load_failed)
    prefix = (
        "window ledger reload FAILED — running full re-verify pull (no skip); "
        if ledger_failed
        else ""
    )
    skipped = spot.windows_skipped + (hist.windows_skipped if hist is not None else 0)
    skipped_clause = (
        f", skipped {skipped} already-pulled window(s) (no vendor hit)" if skipped else ""
    )
    # Expiry scoping visibility (DEC-FNO-EXPIRY-SCOPE-001): a scoped-out expiry is
    # NAMED in the pipeline note, never a silent shrink of the historical pull.
    scope_clause = (
        f", {hist.expiries_out_of_scope} expiries out of scope skipped"
        if hist is not None and hist.expiries_out_of_scope
        else ""
    )
    # Greeks-enrichment honesty (DEC-GREEKS-ENRICH-OBSERVE-001): a WHOLE-pass
    # enrichment failure is NAMED in the pipeline note (the overlay is re-derivable
    # — the next pull re-attempts it), never a swallowed log line. `hist is None`
    # (a metadata-only / no-candle run) honestly carries no clause. The clause
    # carries the same <ErrorType> class as the historical note (hostile-review
    # fold — one uniform operator-facing string).
    greeks_clause = (
        f", Greeks enrichment FAILED "
        f"({(hist.greeks_enrich_error or 'error').split(':', 1)[0]}) "
        "— overlay re-attempted next pull"
        if hist is not None and hist.greeks_enrich_ok is False
        else ""
    )
    return (
        f"{prefix}pipeline complete — "
        f"{_quarantine_clause(quarantined, quarantined_count)}spot {spot.verdict.kind} "
        f"({spot.total_completed} pair(s)), metadata "
        f"{metadata.expiries_persisted} expiries + {metadata.contracts_persisted} "
        f"contracts, historical {bars} bars over {contracts} contract(s)"
        f"{scope_clause}{skipped_clause}{greeks_clause}{_prebuild_note_clause(prebuild)}"
        f"{_movers_note_clause(segment_movers_warmed, segment_movers_failed)}"
    )


def _total_warmed(segment_movers_warmed: Mapping[str, int]) -> int | None:
    """Sum the per-segment warmed counts, or ``None`` when no segment warmed.

    ``None`` (no segment ran a warmer) is honestly distinct from ``0`` (a segment
    ran but warmed nothing — an empty lake), so :attr:`PipelinePullReport.movers_warmed`
    never conflates "did not run" with "warmed nothing" (Golden Rule #1).
    """
    if not segment_movers_warmed:
        return None
    return sum(segment_movers_warmed.values())


def run_full_pipeline(
    *,
    run_spot: Callable[[], BulkSpotReport],
    run_metadata: Callable[[], MetadataPullReport],
    run_historical: Callable[[], CompositePullReport],
    warm_spot_movers: Callable[[], MoversWarmResult] | None = None,
    warm_futures_movers: Callable[[], MoversWarmResult] | None = None,
    warm_options_movers: Callable[[], MoversWarmResult] | None = None,
    run_prebuild: Callable[[], PrebuildOutcome] | None = None,
) -> PipelinePullReport:
    """Run the gated spot→metadata→historical→prebuild→movers pipeline.

    Calls ``run_spot`` first; if :func:`spot_phase_ok` fails the chain STOPS (no
    vendor call for the later phases) and the report carries ``stopped_after =
    "spot"`` with metadata + historical ``None``. Otherwise ``run_metadata`` runs;
    if :func:`metadata_phase_ok` fails the chain stops (``stopped_after =
    "metadata"``, historical ``None``). Otherwise ``run_historical`` runs and the
    report carries all three with ``stopped_after = None``.

    **Gated PREBUILD, then movers (DEC-PIPELINE-AUTOPREBUILD-001):** after the
    fetch phases, the FINAL data stage is the timeframe prebuild — if
    ``run_prebuild`` is injected it fires ONLY when :func:`historical_phase_ok`
    passes (a completed-with-failures fetch leaves ``prebuild_built = None``),
    and its :class:`~brutex.dashboard.pipeline_prebuild.PrebuildOutcome` is
    folded into the report (built symbols, total bars, capped failure names,
    the ``crashed`` error-type marker — an ``ok=False`` outcome is still
    recorded honestly, never hidden, and a guarded whole-run crash reads
    ``", prebuild CRASHED (ErrorType)"`` in the note, never a benign
    "pre-built for 0 symbol(s)"). ONLY
    AFTER the prebuild do the movers warmers fire — movers read the DERIVED
    timeframes, which the prebuild alone writes now, so warming earlier would
    read stale/absent partitions. This SUPERSEDES the early spot-segment warm
    of DEC-PIPELINE-SEGMENT-MOVERS-001 (removed): all three segment warmers
    fire together, gated on ``fetch_clean AND the prebuild SUCCEEDED``
    (``PrebuildOutcome.ok`` — the chunk-2 tightening of the chunk-1
    "prebuild ran" gate: an ``ok=False`` prebuild is still RECORDED in the
    report but warms NOTHING, since its derived partitions may be stale or
    absent) — with no ``run_prebuild`` injected NO warmer fires (there is no
    fresh derived data to warm over). A warmed segment records its count in
    :attr:`PipelinePullReport.segment_movers_warmed` (``0`` is an honest "ran,
    warmed nothing"); :attr:`PipelinePullReport.movers_warmed` is the SUM
    (``None`` when no segment warmed). Neither the prebuild nor any warm ever
    affects the pull's own success (``stopped_after`` / ``ok``) — both are
    post-success stages over the just-filled lake, and the route binds
    ``run_prebuild`` crash-guarded
    (:func:`~brutex.dashboard.pipeline_prebuild.run_pipeline_prebuild_guarded`;
    a pull-wide GLOBAL STOP — disk-full/OOM ``LakeWriteHalt``, quota — is the
    deliberate exception: the guard RE-RAISES it, so it propagates out of this
    sequencer BEFORE any movers warm and reaches the job layer's CRITICAL
    pull-halt alert).

    The sequencer itself is O(1) — the phase calls + the gate checks; the WORK
    it triggers is the real multi-hour vendor fill + the O(symbols × bars)
    prebuild (no throughput claim, Golden Rule #10).

    Parameters
    ----------
    run_spot
        Zero-arg callable returning a
        :class:`~brutex.dashboard.pull_controller.BulkSpotReport` (phase 1).
    run_metadata
        Zero-arg callable returning a
        :class:`~brutex.dashboard.metadata_pull.MetadataPullReport` (phase 2).
    run_historical
        Zero-arg callable returning a
        :class:`~brutex.dashboard.composite_pull.CompositePullReport` (phase 3).
    warm_spot_movers
        OPTIONAL zero-arg callable that warms the SPOT movers snapshots and
        returns its :class:`~brutex.dashboard.movers_warm.MoversWarmResult`
        (warmed + failed + total — DEC-MOVERS-WARM-PROGRESS-001); fired ONLY
        after a clean fetch AND a SUCCESSFUL prebuild (``PrebuildOutcome.ok``).
        ``None`` skips the spot warm.
    warm_futures_movers
        OPTIONAL zero-arg callable warming the FUTURES movers snapshots; same
        gate. ``None`` skips the futures warm.
    warm_options_movers
        OPTIONAL zero-arg callable warming the OPTIONS movers snapshots; same
        gate. ``None`` skips the options warm.
    run_prebuild
        OPTIONAL zero-arg callable running the timeframe prebuild
        (:func:`~brutex.dashboard.pipeline_prebuild.run_pipeline_prebuild_guarded`
        at the route) and returning its
        :class:`~brutex.dashboard.pipeline_prebuild.PrebuildOutcome`; fired ONLY
        after a fully-successful fetch. ``None`` (default) skips the prebuild
        AND the movers warm (movers gate on the prebuild having run).

    Returns
    -------
    PipelinePullReport
        The combined, auditable outcome of every phase that ran.
    """
    spot = run_spot()
    if not spot_phase_ok(spot):
        quarantined, quarantined_count = derive_quarantined_symbols(spot, None)
        return PipelinePullReport(
            spot=spot,
            metadata=None,
            historical=None,
            stopped_after=PHASE_SPOT,
            note=_spot_stop_note(spot),
            quarantined_symbols=quarantined,
            quarantined_count=quarantined_count,
        )

    metadata = run_metadata()
    if not metadata_phase_ok(metadata):
        quarantined, quarantined_count = derive_quarantined_symbols(spot, None)
        return PipelinePullReport(
            spot=spot,
            metadata=metadata,
            historical=None,
            stopped_after=PHASE_METADATA,
            note=_metadata_stop_note(metadata),
            quarantined_symbols=quarantined,
            quarantined_count=quarantined_count,
        )

    historical = run_historical()
    # Post-fetch stages (gated, optional) — evaluate the fully-successful-fetch
    # gate ONCE so both post-fetch stages share the same decision.
    fetch_clean = historical_phase_ok(historical)
    # 1) PREBUILD — the FINAL data stage: derived timeframes are written ONLY
    #    here (DEC-PIPELINE-AUTOPREBUILD-001), never mid-pull.
    prebuild: PrebuildOutcome | None = None
    if run_prebuild is not None and fetch_clean:
        prebuild = run_prebuild()
    # 2) MOVERS WARM — strictly AFTER the prebuild (movers read the derived
    #    timeframes the prebuild just wrote); gated on fetch_clean AND the
    #    prebuild having SUCCEEDED (`PrebuildOutcome.ok` — chunk 2 of
    #    DEC-PIPELINE-AUTOPREBUILD-001 tightened the chunk-1 "prebuild ran"
    #    gate: a failed/partial prebuild leaves stale/absent derived
    #    partitions, so warming over them would cache a wrong snapshot).
    segment_movers_warmed: dict[str, int] = {}
    segment_movers_failed: dict[str, int] = {}
    if fetch_clean and prebuild is not None and prebuild.ok:
        warmers: tuple[tuple[str, Callable[[], MoversWarmResult] | None], ...] = (
            (SEGMENT_SPOT, warm_spot_movers),
            (SEGMENT_FUTURES, warm_futures_movers),
            (SEGMENT_OPTIONS, warm_options_movers),
        )
        injected = [segment for segment, warm in warmers if warm is not None]
        if injected:
            _LOG.info("pipeline.movers_warm.started", segments=injected)
            for segment, warm in warmers:
                if warm is not None:
                    result = warm()
                    segment_movers_warmed[segment] = result.warmed
                    # A best-effort warm job that raised was swallowed + logged
                    # inside warm_movers_snapshots; carry the count so the note
                    # can NAME it (DEC-MOVERS-WARM-PROGRESS-001, Golden Rule #1).
                    if result.failed:
                        segment_movers_failed[segment] = result.failed
            _LOG.info(
                "pipeline.movers_warm.done",
                warmed=dict(segment_movers_warmed),
                failed=dict(segment_movers_failed),
            )
    # Headline honesty (chunk 3): a per-symbol quarantine passes the phase gates
    # (they check pull-wide signals), so it must be NAMED in the complete note —
    # never hidden behind a bare "pipeline complete".
    quarantined, quarantined_count = derive_quarantined_symbols(spot, historical)
    return PipelinePullReport(
        spot=spot,
        metadata=metadata,
        historical=historical,
        stopped_after=None,
        note=_complete_note(
            spot,
            metadata,
            historical,
            segment_movers_warmed,
            prebuild,
            quarantined,
            quarantined_count,
            segment_movers_failed,
        ),
        movers_warmed=_total_warmed(segment_movers_warmed),
        segment_movers_warmed=segment_movers_warmed,
        segment_movers_failed=segment_movers_failed,
        prebuild_built=prebuild.symbols_built if prebuild is not None else None,
        prebuild_total_bars=prebuild.total_bars if prebuild is not None else None,
        prebuild_failed=prebuild.failed if prebuild is not None else (),
        prebuild_crashed=prebuild.crashed if prebuild is not None else None,
        fs_allocation_note=prebuild.fs_note if prebuild is not None else None,
        quarantined_symbols=quarantined,
        quarantined_count=quarantined_count,
    )
