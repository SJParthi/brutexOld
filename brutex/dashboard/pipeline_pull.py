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
from typing import Any

from brutex.dashboard.composite_pull import CompositePullReport
from brutex.dashboard.metadata_pull import MetadataPullReport
from brutex.dashboard.pull_controller import BulkSpotReport

#: The ordered pipeline phases (used for ``stopped_after`` + audit labels).
PHASE_SPOT = "spot"
PHASE_METADATA = "metadata"
PHASE_HISTORICAL = "historical"
#: The OPTIONAL gated post-pull phase (DEC-PULL-SELECTED-PIPELINE-001): warm the
#: movers snapshot ONLY after every fetch phase succeeds (never on a partial/broken
#: pull — the operator's "until/unless successful fetches happen, never run movers").
PHASE_MOVERS = "movers"
#: The OPTIONAL gated FINAL phase (DEC-UNIVERSE-PULL-CONSOLIDATE-001): pre-build the
#: derived intraday timeframes ONLY after every fetch phase succeeds — the operator's
#: "pre-build runs as the pipeline's final stage automatically". Like movers, it is a
#: read-of-the-just-filled-lake accelerator that is gated on a fully-successful fetch.
PHASE_PREBUILD = "prebuild"

#: The three PER-SEGMENT movers-warm labels (DEC-PIPELINE-SEGMENT-MOVERS-001): each
#: segment's movers are computed AS THAT SEGMENT FINISHES — SPOT the moment the spot
#: phase succeeds, FUTURES + OPTIONS the moment the historical phase succeeds (the
#: historical phase pulls futures-before-options, DEC-FNO-PULL-ORDER-LOOKBACK-001, but
#: its combined :class:`~brutex.dashboard.historical_pull.HistoricalPullReport` does NOT
#: split FUT vs CE/PE counts, so both gate honestly on the same historical success —
#: claiming a separate futures-only verdict the report cannot prove would fabricate one,
#: Golden Rule #1). The labels are audit keys for the per-segment ``movers_warmed`` map.
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
        ran at all. DEC-PIPELINE-SEGMENT-MOVERS-001 supersedes the single post-pull
        warm of DEC-PULL-SELECTED-PIPELINE-001: movers are now warmed PER SEGMENT the
        moment that segment fully succeeds (SPOT after the spot phase, FUTURES +
        OPTIONS after the historical phase). ``0`` is an honest "a segment ran but
        warmed nothing" (e.g. an empty lake), distinct from ``None`` "no segment ran".
    segment_movers_warmed
        The per-segment warmed-count map (:data:`SEGMENT_SPOT` / :data:`SEGMENT_FUTURES`
        / :data:`SEGMENT_OPTIONS` → count) for every segment whose warmer was injected
        AND whose own segment gate passed. A segment that did NOT fully succeed is
        ABSENT from the map (never a fabricated count — the operator's "a segment that
        did not fully succeed must NOT warm its movers", Golden Rule #1). Empty when no
        segment warmed.
    prebuild_built
        The number of symbols whose derived intraday timeframes were pre-built by the
        OPTIONAL gated FINAL phase (DEC-UNIVERSE-PULL-CONSOLIDATE-001), or ``None``
        when that phase did NOT run — either no ``run_prebuild`` was injected, OR the
        historical fetch did not pass its gate (the SAME hard rule as movers:
        pre-build runs ONLY after a fully-successful pull). ``0`` is an honest "ran,
        built nothing" (e.g. an empty lake), distinct from ``None`` "did not run".
    """

    spot: BulkSpotReport | None
    metadata: MetadataPullReport | None
    historical: CompositePullReport | None
    stopped_after: str | None
    note: str
    movers_warmed: int | None = None
    segment_movers_warmed: dict[str, int] = field(default_factory=dict)
    prebuild_built: int | None = None

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
            "prebuild_built": self.prebuild_built,
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


def _movers_note_clause(segment_movers_warmed: Mapping[str, int]) -> str:
    """Build the ``, movers …`` note clause from the per-segment warmed-count map.

    Empty when no segment warmed (no clause). Otherwise lists each warmed segment
    in the fixed :data:`SEGMENT_SPOT` → :data:`SEGMENT_FUTURES` → :data:`SEGMENT_OPTIONS`
    order (e.g. ``", movers spot 3 + futures 1 snapshot(s) warmed"``), so the note
    honestly distinguishes WHICH segments warmed — a segment whose gate failed (or
    whose warmer was not injected) is absent from the map and from the clause.
    """
    ordered = [
        (segment, segment_movers_warmed[segment])
        for segment in (SEGMENT_SPOT, SEGMENT_FUTURES, SEGMENT_OPTIONS)
        if segment in segment_movers_warmed
    ]
    if not ordered:
        return ""
    parts = " + ".join(f"{segment} {count}" for segment, count in ordered)
    return f", movers {parts} snapshot(s) warmed"


def _complete_note(
    spot: BulkSpotReport,
    metadata: MetadataPullReport,
    historical: CompositePullReport,
    segment_movers_warmed: Mapping[str, int],
    prebuild_built: int | None = None,
) -> str:
    """Build the honest summary note for a fully-run pipeline.

    Appends a per-segment movers-warm clause ONLY for the segments that actually
    warmed (:func:`_movers_note_clause`) — honestly naming "spot 3 + futures 1
    warmed" vs a run where no segment warmed (no clause: warmer not injected, or
    every segment's own gate failed) — AND a pre-build clause ONLY when the gated
    FINAL pre-build phase ran (``prebuild_built`` is not ``None``), honestly
    distinguishing "built N" from a run where it was skipped (no callable injected)
    or gated-out (historical not clean).
    """
    hist = historical.historical
    bars = hist.bars_appended if hist is not None else 0
    contracts = hist.contracts_completed if hist is not None else 0
    prebuild = (
        f", timeframes pre-built for {prebuild_built} symbol(s)"
        if prebuild_built is not None
        else ""
    )
    return (
        f"pipeline complete — spot {spot.verdict.kind} "
        f"({spot.total_completed} pair(s)), metadata "
        f"{metadata.expiries_persisted} expiries + {metadata.contracts_persisted} "
        f"contracts, historical {bars} bars over {contracts} contract(s)"
        f"{_movers_note_clause(segment_movers_warmed)}{prebuild}"
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
    warm_spot_movers: Callable[[], int] | None = None,
    warm_futures_movers: Callable[[], int] | None = None,
    warm_options_movers: Callable[[], int] | None = None,
    run_prebuild: Callable[[], int] | None = None,
) -> PipelinePullReport:
    """Run the gated spot→metadata→historical pipeline, warming movers PER SEGMENT.

    Calls ``run_spot`` first; if :func:`spot_phase_ok` fails the chain STOPS (no
    vendor call for the later phases) and the report carries ``stopped_after =
    "spot"`` with metadata + historical ``None``. Otherwise ``run_metadata`` runs;
    if :func:`metadata_phase_ok` fails the chain stops (``stopped_after =
    "metadata"``, historical ``None``). Otherwise ``run_historical`` runs and the
    report carries all three with ``stopped_after = None``.

    **Per-segment movers warm (DEC-PIPELINE-SEGMENT-MOVERS-001):** each segment's
    movers are computed AS THAT SEGMENT FINISHES — the operator's "the moment the
    SPOT stage fully succeeds compute SPOT movers; when FUTURES succeeds → FUTURES
    movers; when OPTIONS succeeds → OPTIONS movers":

    * ``warm_spot_movers`` (if injected) fires the moment :func:`spot_phase_ok`
      passes — BEFORE the metadata phase even runs. So a clean spot pull warms its
      movers even if a LATER phase (metadata / historical) then fails: the spot data
      is already clean + usable, so its movers are honest (this is the headline new
      behaviour over the single post-historical warm of DEC-PULL-SELECTED-PIPELINE-001).
    * ``warm_futures_movers`` + ``warm_options_movers`` (if injected) fire the moment
      :func:`historical_phase_ok` passes. The historical phase pulls futures-before-
      options (DEC-FNO-PULL-ORDER-LOOKBACK-001), but its combined
      :class:`~brutex.dashboard.historical_pull.HistoricalPullReport` does NOT split
      FUT vs CE/PE success, so BOTH gate on the SAME historical success — claiming a
      futures-only verdict the report cannot prove would fabricate one (Golden Rule #1).

    A segment whose own gate did NOT pass is NEVER warmed (the hard rule "a segment
    that did not fully succeed must NOT warm its movers") and is ABSENT from
    :attr:`PipelinePullReport.segment_movers_warmed`. A warmed segment records its
    count there (``0`` is an honest "ran, warmed nothing", e.g. an empty lake);
    :attr:`PipelinePullReport.movers_warmed` is the SUM (``None`` only when no
    segment warmed). The pull's own success (``stopped_after`` / ``ok``) is NEVER
    affected by any warm — each is a read-only post-success accelerator over the
    just-filled lake (the warmer is best-effort at its own call site).

    **Gated FINAL pre-build (DEC-UNIVERSE-PULL-CONSOLIDATE-001):** if ``run_prebuild``
    is injected, it runs as the pipeline's FINAL stage, gated by the SAME
    fully-successful-fetch rule as movers (:func:`historical_phase_ok` passes). This
    is the operator's "pre-build runs as the pipeline's final stage automatically":
    a completed-with-failures historical fetch leaves ``prebuild_built = None`` (NOT
    pre-built). A clean run records the count ``run_prebuild`` returns (``0`` is an
    honest "built nothing", distinct from ``None`` "did not run"). Like movers, the
    pre-build NEVER affects the pull's own success — it is a post-success accelerator
    that materialises the derived intraday timeframes over the just-filled lake.

    warmers + the timeframe materialiser). The sequencer itself is O(1) — the phase
    calls + the gate checks + the per-segment warms + the final pre-build; the WORK it
    triggers is the real multi-hour vendor fill (no throughput claim).

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
        OPTIONAL zero-arg callable that warms the SPOT movers snapshot over the
        just-filled lake and returns how many were warmed; fired ONLY after the spot
        phase passes its gate (before metadata). ``None`` skips the spot warm.
    warm_futures_movers
        OPTIONAL zero-arg callable that warms the FUTURES movers snapshot; fired ONLY
        after the historical phase passes its gate. ``None`` skips the futures warm.
    warm_options_movers
        OPTIONAL zero-arg callable that warms the OPTIONS movers snapshot; fired ONLY
        after the historical phase passes its gate. ``None`` skips the options warm.
        (All three ``None`` ⇒ no movers phase — backward-compatible with the
        whole-universe pipeline.)
    run_prebuild
        OPTIONAL zero-arg callable that pre-builds the derived intraday timeframes
        over the just-filled lake and returns how many symbols were built; the
        pipeline's FINAL stage, called ONLY after a fully-successful fetch (gated).
        ``None`` (default) skips it entirely.

    Returns
    -------
    PipelinePullReport
        The combined, auditable outcome of every phase that ran, with the
        per-segment movers-warm map.
    """
    segment_movers_warmed: dict[str, int] = {}

    spot = run_spot()
    if not spot_phase_ok(spot):
        return PipelinePullReport(
            spot=spot,
            metadata=None,
            historical=None,
            stopped_after=PHASE_SPOT,
            note=_spot_stop_note(spot),
        )
    # Spot succeeded → warm SPOT movers NOW (the moment the segment finishes), even
    # if a later phase fails: the spot data is already clean + usable.
    if warm_spot_movers is not None:
        segment_movers_warmed[SEGMENT_SPOT] = warm_spot_movers()

    metadata = run_metadata()
    if not metadata_phase_ok(metadata):
        return PipelinePullReport(
            spot=spot,
            metadata=metadata,
            historical=None,
            stopped_after=PHASE_METADATA,
            note=_metadata_stop_note(metadata),
            movers_warmed=_total_warmed(segment_movers_warmed),
            segment_movers_warmed=segment_movers_warmed,
        )

    historical = run_historical()
    # Post-fetch phases (gated, optional): warm the FUTURES + OPTIONS movers
    # (futures-before-options was the pull order; both ride the SAME combined
    # historical success gate) + pre-build the derived timeframes — ONLY on a
    # fully-successful fetch. Evaluate the gate ONCE so every post-fetch phase
    # shares the same fully-successful-fetch decision.
    fetch_clean = historical_phase_ok(historical)
    if fetch_clean:
        if warm_futures_movers is not None:
            segment_movers_warmed[SEGMENT_FUTURES] = warm_futures_movers()
        if warm_options_movers is not None:
            segment_movers_warmed[SEGMENT_OPTIONS] = warm_options_movers()
    prebuild_built: int | None = None
    if run_prebuild is not None and fetch_clean:
        prebuild_built = run_prebuild()
    return PipelinePullReport(
        spot=spot,
        metadata=metadata,
        historical=historical,
        stopped_after=None,
        note=_complete_note(spot, metadata, historical, segment_movers_warmed, prebuild_built),
        movers_warmed=_total_warmed(segment_movers_warmed),
        segment_movers_warmed=segment_movers_warmed,
        prebuild_built=prebuild_built,
    )
