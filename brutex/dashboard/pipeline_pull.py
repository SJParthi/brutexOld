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

import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Final

import structlog

from brutex.dashboard.composite_pull import CompositePullReport
from brutex.dashboard.metadata_pull import MetadataPullReport
from brutex.dashboard.movers_warm import MoversWarmResult
from brutex.dashboard.pipeline_prebuild import PrebuildOutcome

# The verdict dataclass lives in the LEAF brutex.dashboard.pipeline_verdict
# (DEC-PULL-SIX-STEP-ORDER-001): its consumers (composite_pull/historical_pull)
# sit BELOW this sequencer in the module import graph, so housing it here made
# every edge of that chain a module-level import cycle. Imported — and thereby
# RE-EXPORTED — so existing ``from brutex.dashboard.pipeline_pull import
# PriorStepsVerdict`` callers (the route wiring + the suites) stay byte-valid.
# The ``as`` self-alias is the explicit-re-export idiom mypy strict's
# no-implicit-reexport demands; ruff PLC0414 flags exactly that idiom, hence
# the per-line noqa (the repo's per-line-noqa-with-rationale house style).
from brutex.dashboard.pipeline_verdict import (
    PriorStepsVerdict as PriorStepsVerdict,  # noqa: PLC0414
)
from brutex.dashboard.pull_controller import BulkSpotReport
from brutex.data.errors import MarketHoursHalt
from brutex.data.fno_symbol import parse_fno_symbol
from brutex.data.parquet_writer import fold_max_close_delta
from brutex.data.window_ledger_store import shared_pull_ledger_scope
from brutex.money import format_inr, round_money
from brutex.notifications.sweep_alerts import emit_vendor_conflict_alert

_LOG = structlog.get_logger(__name__)

#: The ordered pipeline phases (used for ``stopped_after`` + audit labels).
PHASE_SPOT = "spot"
PHASE_METADATA = "metadata"
PHASE_HISTORICAL = "historical"
#: The DEFERRED-before-any-vendor-phase marker (DEC-GROWW-MARKET-HOURS-GUARD-001):
#: the whole pipeline was skipped because the clock is inside the NSE market-hours
#: window ``[08:45, 15:35)`` IST — no vendor call was issued (spot / metadata /
#: historical all ``None``), keeping the shared Groww account free for TickVault's
#: live feed. The non-vendor prebuild / movers stages are inapplicable (there was no
#: pull to derive from) and also do not run.
PHASE_MARKET_HOURS = "market_hours"
#: The OPTIONAL gated movers phase: warm the movers snapshots ONLY after the
#: prebuild SUCCEEDED over the clean symbol set (``PrebuildOutcome.ok`` —
#: DEC-PREBUILD-CLEANSET-001 dropped the ``fetch_clean`` arm of the
#: DEC-PIPELINE-AUTOPREBUILD-001 chunk-2 gate; movers read the DERIVED
#: timeframes, which only the prebuild writes now, so a pre-prebuild or
#: failed-prebuild warm would read stale/absent partitions).
PHASE_MOVERS = "movers"
#: The PREBUILD phase (DEC-PIPELINE-AUTOPREBUILD-001, superseding the
#: DEC-UNIVERSE-PULL-CONSOLIDATE-001 shape): pre-build ALL derived timeframes
#: (19 intraday + the derived ``1day``) for SPOT + FUTURES + OPTIONS — the
#: pipeline's FINAL data stage, run BEFORE the movers warm. Since
#: DEC-PREBUILD-CLEANSET-001 (supersedes the #1145 fetch_clean all-or-nothing
#: gate) it ALWAYS runs on the completed-chain path, over the CLEAN symbol set
#: (every target minus the UNCAPPED quarantined names, which it receives as an
#: exclusion set) — one quarantined symbol no longer starves the other ~N−1 of
#: derived timeframes.
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
    after the IST-midnight reset), (c) left any window still failing after the
    in-run retry budget (``still_failed_windows``), nor (d) HALTED on the
    mass-empty circuit breaker (``mass_empty_cause`` — the needs-operator
    ``mass_empty_halt`` verdict; DEC-EMPTY-WINDOW-CIRCUIT-BREAKER-001). The (d)
    arm is a DEC-PULL-SIX-STEP-ORDER-001 review fix: the aborted pass never
    appends, so a mass-empty halt left ``still_failed_windows == 0`` and this
    gate read a HALTED spot step as clean — the chain proceeded to metadata +
    F&O vendor calls and, under ``BRUTEX_PIPELINE_STRICT_GATE=1``, the strict
    all-or-nothing barrier fired the post-steps after a pull-wide halt. A
    dry-run with zero bars is NOT a failure (no creds is reported separately);
    the gate detects FAILURE signals, not absence of work.

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
        not report.auth_aborted
        and not report.quota_exhausted
        and report.still_failed_windows == 0
        and report.mass_empty_cause is None
    )


def metadata_phase_ok(report: MetadataPullReport) -> bool:
    """Return ``True`` iff the METADATA phase enumerated the chain cleanly.

    The metadata enumeration may proceed to the historical candle phase only when
    NO Phase-1 expiry cell raised (``expiry_failures``), NO Phase-2 contract
    job raised (``contract_failures``), AND NO registry/coverage write batch was
    deferred by the store-write wall-clock deadline (``store_write_deferrals`` —
    DEC-STORE-WRITE-DEADLINE-001: a wedged store volume stops the chain
    GRACEFULLY after metadata via the existing ``stopped_after`` verdict, never a
    crash, never a pull-wide halt; the deferred units re-attempt next pull). A
    dry-run that honestly persists an EMPTY chain (no creds) raises nothing, so
    it passes the gate (the historical phase then finds no tradable contracts —
    a reported no-op, never a fabricated pull).

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
    return (
        report.expiry_failures == 0
        and report.contract_failures == 0
        and report.store_write_deferrals == 0
    )


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
      (``still_failed_windows``), quarantined any contract pair on a terminal
      error (``failed_pairs``), nor HALTED on the F&O mass-empty circuit breaker
      (``mass_empty_cause`` — the DEC-PULL-SIX-STEP-ORDER-001 review fix, the
      symmetric twin of the :func:`spot_phase_ok` (d) arm: a halted phase must
      never read settled to the post-step barrier).

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
    return (
        not hist.auth_aborted
        and hist.still_failed_windows == 0
        and hist.failed_pairs == 0
        and hist.mass_empty_cause is None
    )


#: The strict post-step barrier's opt-in env knob (DEC-PULL-SIX-STEP-ORDER-001).
#: Unset / blank / ``"0"`` ⇒ the cleanset default; ``"1"`` ⇒ the literal
#: all-or-nothing barrier; anything else ⇒ ``ValueError`` (fail-LOUD — a broken
#: knob must never silently fall back, the ``hardware_envelope`` env convention).
STRICT_GATE_ENV: Final[str] = "BRUTEX_PIPELINE_STRICT_GATE"


def strict_gate_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Parse the :data:`STRICT_GATE_ENV` strict post-step gate knob — fail-loud.

    DEC-PULL-SIX-STEP-ORDER-001: resolved ONCE, eagerly, at pipeline LAUNCH
    (``_launch_pipeline`` wraps this in its validation ``try`` so garbage is a
    clean 400 at launch, never a mid-pull 500). Unset / blank / ``"0"`` return
    ``False`` (the DEC-PREBUILD-CLEANSET-001 cleanset default, byte-identical);
    ``"1"`` returns ``True``; any other value raises ``ValueError`` — a broken
    knob must never silently fall back (Golden Rule #1).

    Parameters
    ----------
    env
        Injectable environment mapping (tests); ``None`` reads ``os.environ``.

    Returns
    -------
    bool
        Whether the strict all-or-nothing post-step barrier is enabled.

    Raises
    ------
    ValueError
        When the variable holds anything other than ``""``/``"0"``/``"1"``.
    """
    source = os.environ if env is None else env
    raw = source.get(STRICT_GATE_ENV, "").strip()
    if raw in ("", "0"):
        return False
    if raw == "1":
        return True
    raise ValueError(f"{STRICT_GATE_ENV} must be unset, '', '0' or '1'; got {raw!r}")


@dataclass(frozen=True, slots=True)
class StepSettleVerdict:
    """The pure post-step barrier verdict (DEC-PULL-SIX-STEP-ORDER-001).

    Attributes
    ----------
    settled
        Every applicable step settled (all three phase gates passed) — the
        CLEANSET barrier. Settle-based, never a ``bars > 0`` gate: a 0-bar
        EMPTY / session-filtered / far-expiry-all-skipped step is settled.
    clean
        :attr:`settled` AND zero quarantined symbols — the STRICT barrier
        (the literal all-or-nothing law: a per-symbol quarantine withholds
        the post-steps under ``BRUTEX_PIPELINE_STRICT_GATE=1``).
    blocking
        Human-readable labels of the blocking signals, for the note + the
        ``pipeline.post_steps.strict_skipped`` log. Empty iff :attr:`clean`.
    """

    settled: bool
    clean: bool
    blocking: tuple[str, ...]


def step_settle_verdict(
    *,
    spot_ok: bool,
    metadata_ok: bool,
    historical_ok: bool,
    quarantined: frozenset[str],
) -> StepSettleVerdict:
    """Fold the three phase gates + the quarantine set into one pure verdict.

    DEC-PULL-SIX-STEP-ORDER-001 — the ONE place the post-step barrier
    predicate lives (pure + testable over ALL input combinations; at the
    sequencer call site only the reachable outcomes occur — spot/metadata
    failures already early-returned). ``settled`` is the cleanset barrier
    (all gates ok); ``clean`` additionally demands ZERO quarantined symbols
    (the strict barrier); ``blocking`` names every failing signal.
    """
    blocking: list[str] = []
    if not spot_ok:
        blocking.append("spot unclean")
    if not metadata_ok:
        blocking.append("metadata unclean")
    if not historical_ok:
        blocking.append("historical unclean")
    settled = not blocking
    if quarantined:
        blocking.append(f"{len(quarantined)} quarantined symbol(s)")
    return StepSettleVerdict(
        settled=settled, clean=settled and not quarantined, blocking=tuple(blocking)
    )


#: How many quarantined symbol NAMES :attr:`PipelinePullReport.quarantined_symbols`
#: carries (and the note shows) — the TRUE total always rides
#: :attr:`PipelinePullReport.quarantined_count`, and the note appends an honest
#: ``(+K more)`` remainder computed from it (Golden Rule #1 — the cap never
#: hides the real count).
MAX_QUARANTINED_NAMES: Final[int] = 10


def _spot_quarantine_names(spot: BulkSpotReport | None) -> frozenset[str]:
    """The SPOT arm of :func:`derive_quarantined_name_set` — pure.

    Extracted (DEC-PULL-SIX-STEP-ORDER-001) so the sequencer's
    :class:`PriorStepsVerdict` can judge spot cleanliness BEFORE the
    historical phase runs, with the SAME predicate the quarantine headline
    uses (CLAUDE.md §11 COMMON — one source of truth, no duplication): every
    :class:`~brutex.dashboard.pull_controller.SymbolPullResult` with
    ``failed > 0`` (a terminally-quarantined ``(symbol, interval)`` pair).
    ``None`` / a clean report ⇒ the empty frozenset.
    """
    if spot is None:
        return frozenset()
    return frozenset(s.symbol for s in spot.symbols if s.failed > 0)


def derive_quarantined_name_set(
    spot: BulkSpotReport | None, historical: CompositePullReport | None
) -> frozenset[str]:
    """The UNCAPPED, deduped quarantined-symbol name set — pure.

    DEC-PREBUILD-CLEANSET-001: this FULL set is the prebuild's EXCLUSION set —
    it is NEVER capped (the 10-name :data:`MAX_QUARANTINED_NAMES` cap of
    :func:`derive_quarantined_symbols` is a DISPLAY concern; excluding by the
    capped tuple would silently prebuild the 11th+ quarantined symbol over
    dirty/absent 1-minute data). The names come from the phase reports:

    * **spot** — every :class:`~brutex.dashboard.pull_controller.SymbolPullResult`
      with ``failed > 0`` (a terminally-quarantined ``(symbol, interval)`` pair;
      a pull-wide ``still_failed_windows`` residual stops the chain at the gate
      and never reaches the complete note).
    * **historical** — every in-window contract that did NOT land clean
      (``start_dates`` minus ``phase1_complete`` — quarantined pairs AND
      still-failing transients), folded to its UNDERLYING via the shared
      :func:`~brutex.data.fno_symbol.parse_fno_symbol` decode (an undecodable
      symbol keeps its raw name — reported, never dropped).

    Returns the empty frozenset for a clean run.
    """
    names: set[str] = set(_spot_quarantine_names(spot))
    hist = historical.historical if historical is not None else None
    if hist is not None:
        for symbol in set(hist.start_dates) - set(hist.phase1_complete):
            parts = parse_fno_symbol(symbol)
            names.add(symbol if parts is None else parts.underlying)
    return frozenset(names)


def _cap_quarantined(names: frozenset[str]) -> tuple[tuple[str, ...], int]:
    """Sort + cap *names* for DISPLAY: ``(capped tuple, TRUE uncapped total)``.

    The ONE place the :data:`MAX_QUARANTINED_NAMES` cap is applied (so the
    display cap and the uncapped exclusion set can never drift apart —
    DEC-PREBUILD-CLEANSET-001; CLAUDE.md §11 COMMON).
    """
    ordered = tuple(sorted(names))
    return ordered[:MAX_QUARANTINED_NAMES], len(ordered)


def derive_quarantined_symbols(
    spot: BulkSpotReport | None, historical: CompositePullReport | None
) -> tuple[tuple[str, ...], int]:
    """The deduped, sorted quarantined-symbol names + the TRUE total — pure.

    DEC-PIPELINE-AUTOPREBUILD-001 chunk 3 (headline honesty): a per-symbol
    quarantine does NOT stop the pipeline (the phase gates check pull-wide
    signals), so a completed run could read a bare "pipeline complete" while
    symbols silently failed. The names are derived by
    :func:`derive_quarantined_name_set` (the ONE source of truth — the
    UNCAPPED twin that feeds the prebuild's exclusion set,
    DEC-PREBUILD-CLEANSET-001); this function only sorts + caps for display.

    Returns ``(names, true_count)``: *names* deduped + sorted, capped at
    :data:`MAX_QUARANTINED_NAMES`; *true_count* is the UNCAPPED total so the
    note's ``(+K more)`` remainder is honest. ``((), 0)`` for a clean run.
    """
    return _cap_quarantined(derive_quarantined_name_set(spot, historical))


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
        AND whose warm gate passed (a SUCCESSFUL prebuild over the clean symbol
        set — ``PrebuildOutcome.ok``; DEC-PREBUILD-CLEANSET-001 dropped the
        ``fetch_clean`` arm of the chunk-2 gate). A segment that
        did not warm is ABSENT from the map (never a fabricated count, Golden Rule #1).
        Empty when no segment warmed.
    prebuild_built
        The number of symbols (underlyings + contracts) whose derived timeframes were
        pre-built by the prebuild phase (DEC-PIPELINE-AUTOPREBUILD-001 —
        :attr:`~brutex.dashboard.pipeline_prebuild.PrebuildOutcome.symbols_built`), or
        ``None`` when that phase did NOT run — no ``run_prebuild`` was injected, or an
        earlier gate stopped the chain before the historical phase (on the
        completed-chain path the prebuild ALWAYS runs, over the CLEAN symbol set —
        quarantined symbols excluded; DEC-PREBUILD-CLEANSET-001, superseding the
        #1145 fully-successful-pull gate). ``0`` is an honest "ran, built nothing",
        distinct from ``None`` "did not run".
    prebuild_total_bars
        Total derived bars the prebuild phase wrote (intraday + ``1day``), or ``None``
        when the phase did not run.
    prebuild_failed
        The prebuild's failing symbol names (capped at 10 by the producer —
        :data:`~brutex.dashboard.pipeline_prebuild.MAX_REPORTED_FAILURES`); empty when
        the phase did not run or had no failures.
    prebuild_no_source
        The prebuild's NO-SOURCE spot-target names — ZERO stored 1-minute
        bars at derive time, nothing to derive (vendor-empty or never pulled;
        DEC-SPOTONLY-PREBUILD-EMPTY-EXCLUDE-001) — capped at 10 by the
        producer, exactly the ``quarantined_symbols``/``quarantined_count``
        template; empty when the phase did not run or every target had
        stored data. NOT failures: the movers warm still fires, and each
        no-source symbol is structurally absent from the warmed snapshots
        (no parquet ⇒ no discovery row) — counted + NAMED in the note
        instead of silently vanishing (Golden Rule #1).
    prebuild_no_source_count
        The TRUE (uncapped) no-source total — the note's ``(+K more)``
        remainder derives from it, so the cap never lies.
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
    prebuild_opens_substituted
        The prebuild's observe-only daily-open substitution total
        (DEC-CALENDAR-EXPECTED-BARS-DAILY-OPEN-001 —
        :attr:`~brutex.dashboard.pipeline_prebuild.PrebuildOutcome.opens_substituted`):
        (symbol, date) SESSIONS whose derived daily open silently substituted
        a non-09:15 first minute, summed over the prebuild's SPOT targets
        (F&O contracts are never scanned — spot-only by decision). ``None``
        when the prebuild did not run. HONEST semantics: a FULL-HISTORY
        count recomputed per run over each re-derived symbol's whole
        1-minute frame — a permanent historical gap re-reports on every run
        that re-derives the symbol (an unchanged-source incremental skip
        contributes 0), so the number is honest volume, not "new this pull".
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
    post_steps_strict_skipped
        ``True`` when the OPT-IN strict barrier
        (``BRUTEX_PIPELINE_STRICT_GATE=1`` — DEC-PULL-SIX-STEP-ORDER-001)
        withheld the post-steps (prebuild + movers warm) on a completed chain
        because :attr:`StepSettleVerdict.clean` was ``False`` (any unclean
        step OR any per-symbol quarantine). The note names the blocking
        signals; nothing is lost — the next pull re-derives. ``False`` on the
        cleanset default (byte-identical behavior) and on every
        gate-stopped run (the post-steps were never reachable there).
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
    prebuild_opens_substituted: int | None = None
    quarantined_symbols: tuple[str, ...] = ()
    quarantined_count: int = 0
    post_steps_strict_skipped: bool = False
    #: True when the PRE-RUN disk guard skipped the whole prebuild
    #: (DEC-LAKE-FOOTPRINT-MEASURED-001) — the note carries the guard's honest
    #: numbers, and the movers warm was consequently skipped (gated on
    #: ``PrebuildOutcome.ok``).
    prebuild_disk_guarded: bool = False
    #: The no-source spot targets (capped names + TRUE count — the
    #: quarantined_symbols/quarantined_count template;
    #: DEC-SPOTONLY-PREBUILD-EMPTY-EXCLUDE-001, see the docstring above).
    prebuild_no_source: tuple[str, ...] = ()
    prebuild_no_source_count: int = 0

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
        payload: dict[str, Any] = {
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
            "prebuild_disk_guarded": self.prebuild_disk_guarded,
            "prebuild_opens_substituted": self.prebuild_opens_substituted,
            "quarantined_symbols": list(self.quarantined_symbols),
            "quarantined_count": self.quarantined_count,
            "post_steps_strict_skipped": self.post_steps_strict_skipped,
            "spot": self.spot.to_dict() if self.spot is not None else None,
            "metadata": self.metadata.to_dict() if self.metadata is not None else None,
            "historical": (self.historical.to_dict() if self.historical is not None else None),
        }
        if self.prebuild_no_source_count:
            # EMIT-ON-NONZERO (DEC-SPOTONLY-PREBUILD-EMPTY-EXCLUDE-001 — the
            # #1539/#1649b emit-on-non-default precedent): the default
            # all-stored-data payload stays byte-identical.
            payload["prebuild_no_source"] = list(self.prebuild_no_source)
            payload["prebuild_no_source_count"] = self.prebuild_no_source_count
        return payload


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


def _market_hours_stop_note() -> str:
    """Build the honest stop-note for a market-hours deferral (no vendor phase ran)."""
    return (
        "deferred — inside NSE market-hours window (08:45-15:35 IST); no vendor pull "
        "issued (spot / metadata / historical skipped to keep the shared Groww account "
        "free for the live feed); resume on the next out-of-hours run"
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


def format_capped_names(
    names: Sequence[str], true_count: int, cap: int = _NOTE_FAILURE_NAMES
) -> str:
    """``"A, B, C (+K more)"`` — capped names + the honest remainder from the TRUE count.

    The ONE capped-names note formatter (CLAUDE.md §11 COMMON,
    DEC-SPOTONLY-PREBUILD-EMPTY-EXCLUDE-001): shows up to *cap* of *names* and
    appends ``(+K more)`` from *true_count* — the producer may have already
    capped *names* (e.g. :data:`~brutex.dashboard.pipeline_prebuild.MAX_REPORTED_FAILURES`),
    so the remainder derives from the UNCAPPED total, never the shown tuple
    (Golden Rule #1). Shared by the prebuild-failures clause, the no-source
    clause, and the ``brutex pull-all-spot`` stdout line — zero drift.
    """
    shown = ", ".join(names[:cap])
    more = true_count - min(cap, len(names))
    return f"{shown} (+{more} more)" if more > 0 else shown


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
    uncapped total; Golden Rule #1). A non-zero ``no_source_count``
    (vendor-empty / never-pulled spot targets,
    DEC-SPOTONLY-PREBUILD-EMPTY-EXCLUDE-001) renders its own DISTINCT
    counted+named clause BEFORE the failures clause (benign class first) —
    both via the ONE :func:`format_capped_names` formatter.
    """
    if prebuild is None:
        return ""
    if prebuild.disk_guard_blocked:
        # DEC-LAKE-FOOTPRINT-MEASURED-001: the pre-run disk guard skipped the
        # whole prebuild — the headline carries the guard's honest numbers
        # (est. need, multiplier + provenance, free space, the env override),
        # never a benign "pre-built for 0 symbol(s)".
        return f", prebuild SKIPPED — {prebuild.disk_guard_detail}"
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
    if prebuild.opens_substituted:
        # DEC-CALENDAR-EXPECTED-BARS-DAILY-OPEN-001: the observe-only daily-open
        # substitution total is named in the headline (sessions whose derived
        # daily open fell back to a non-09:15 first minute — full-history,
        # recomputed per run). Zero keeps the note byte-identical.
        clause += f", {prebuild.opens_substituted} daily open(s) substituted"
    if prebuild.no_source_count:
        # DEC-SPOTONLY-PREBUILD-EMPTY-EXCLUDE-001: the benign no-source class
        # renders BEFORE the failure class — counted + named (capped), wording
        # deliberately distinct from "quarantined" (healthy vendor behavior,
        # not a failure) and from "prebuild failures" (nothing failed), and
        # naming BOTH causes honestly (the probe cannot prove the vendor arm).
        # Zero keeps the note byte-identical.
        clause += (
            f", {prebuild.no_source_count} symbol(s) skipped — no stored 1-minute data "
            "(vendor-empty or never pulled), excluded from timeframe build + movers warm: "
            f"{format_capped_names(prebuild.no_source, prebuild.no_source_count)}"
        )
    if prebuild.failed:
        clause += (
            f", prebuild failures: {format_capped_names(prebuild.failed, prebuild.failed_count)}"
        )
    if prebuild.fs_note is not None:
        # DEC-LAKE-FS-PROBE-001: a COARSE-allocation lake volume is named in the
        # headline too — the slack is spent by exactly this phase's tiny files.
        clause += f", ⚠ {prebuild.fs_note}"
    return clause


def vendor_surface_totals(
    spot: BulkSpotReport | None, historical: CompositePullReport | None
) -> tuple[int, int, Decimal | None]:
    """Fold the pull's vendor-behaviour counters across the spot + F&O phases — pure.

    DEC-VENDOR-CONFLICT-SURFACE-001: returns ``(conflicts, vendor_dup_rows_dropped,
    max_close_delta)`` — conflicts + duplicate drops SUM across the two fetch
    phases, the close delta MAX-folds (exact Decimal, ``None`` identity via the
    COMMON :func:`~brutex.data.parquet_writer.fold_max_close_delta`). A phase
    that never ran contributes zeros/``None`` (never fabricated counts).
    """
    conflicts = dup_rows = 0
    delta: Decimal | None = None
    if spot is not None:
        conflicts += spot.conflicts
        dup_rows += spot.vendor_dup_rows_dropped
        delta = fold_max_close_delta(delta, spot.max_close_delta)
    hist = historical.historical if historical is not None else None
    if hist is not None:
        conflicts += hist.conflicts
        dup_rows += hist.vendor_dup_rows_dropped
        delta = fold_max_close_delta(delta, hist.max_close_delta)
    return conflicts, dup_rows, delta


def _conflict_clause(conflicts: int, dup_rows: int, max_close_delta: Decimal | None) -> str:
    """The honest ``", vendor conflicts: …"`` headline clause (empty when all-zero).

    DEC-VENDOR-CONFLICT-SURFACE-001: 25 conflict summaries + 9,141 dropped
    duplicate rows previously lived ONLY in log lines — the completion headline
    now names them. The delta renders through ``brutex.money``
    (DEC-MONEY-2DP-001 — the display is 2dp-snapped; the exact Decimal is what
    the alert threshold compared). Bounded length (two counters + one amount)
    — never a flooded note. A clean pull renders NOTHING (byte-identical note).
    """
    parts: list[str] = []
    if conflicts:
        delta_part = (
            f" (max close delta {format_inr(int(round_money(max_close_delta) * 100))})"
            if max_close_delta is not None
            else ""
        )
        parts.append(f"vendor conflicts: {conflicts}{delta_part}")
    if dup_rows:
        parts.append(f"duplicate vendor rows dropped: {dup_rows}")
    return f", {'; '.join(parts)}" if parts else ""


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
    strict_blocking: tuple[str, ...] | None = None,
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

    *strict_blocking* (DEC-PULL-SIX-STEP-ORDER-001): non-``None`` when the
    opt-in strict barrier withheld the post-steps — appends the LOUD
    ``", post-steps SKIPPED (strict gate: <blocking…>)"`` clause naming every
    blocking signal (:attr:`StepSettleVerdict.blocking`). ``None`` (the
    cleanset default) renders byte-identically to before.
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
    # Vendor-behaviour honesty (DEC-VENDOR-CONFLICT-SURFACE-001): keep-first
    # conflicts + duplicate-row drops are NAMED in the headline when non-zero.
    conflicts, dup_rows, max_delta = vendor_surface_totals(spot, historical)
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
    # Strict-barrier honesty (DEC-PULL-SIX-STEP-ORDER-001): a strict-withheld
    # post-step stage is NAMED loudly — never a silent absence of the prebuild
    # + movers clauses. None (the cleanset default) renders nothing.
    strict_clause = (
        f", post-steps SKIPPED (strict gate: {'; '.join(strict_blocking)})"
        if strict_blocking is not None
        else ""
    )
    return (
        f"{prefix}pipeline complete — "
        f"{_quarantine_clause(quarantined, quarantined_count)}spot {spot.verdict.kind} "
        f"({spot.total_completed} pair(s)), metadata "
        f"{metadata.expiries_persisted} expiries + {metadata.contracts_persisted} "
        f"contracts, historical {bars} bars over {contracts} contract(s)"
        f"{scope_clause}{skipped_clause}{greeks_clause}"
        f"{_conflict_clause(conflicts, dup_rows, max_delta)}"
        f"{_prebuild_note_clause(prebuild)}"
        f"{_movers_note_clause(segment_movers_warmed, segment_movers_failed)}"
        f"{strict_clause}"
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
    run_historical: Callable[[PriorStepsVerdict], CompositePullReport],
    warm_spot_movers: Callable[[], MoversWarmResult] | None = None,
    warm_futures_movers: Callable[[], MoversWarmResult] | None = None,
    warm_options_movers: Callable[[], MoversWarmResult] | None = None,
    run_prebuild: Callable[[frozenset[str]], PrebuildOutcome] | None = None,
    strict_gate: bool = False,
    market_closed: Callable[[], bool] | None = None,
) -> PipelinePullReport:
    """Run the gated spot→metadata→historical→prebuild→movers pipeline.

    Calls ``run_spot`` first; if :func:`spot_phase_ok` fails the chain STOPS (no
    vendor call for the later phases) and the report carries ``stopped_after =
    "spot"`` with metadata + historical ``None``. Otherwise ``run_metadata`` runs;
    if :func:`metadata_phase_ok` fails the chain stops (``stopped_after =
    "metadata"``, historical ``None``). Otherwise ``run_historical`` runs —
    receiving the :class:`PriorStepsVerdict` (spot + metadata cleanliness +
    the strict flag, DEC-PULL-SIX-STEP-ORDER-001) so the Greeks seam at the
    historical tail sees the prior steps' state under the strict barrier —
    and the report carries all three with ``stopped_after = None``.

    **Strict post-step barrier (opt-in — DEC-PULL-SIX-STEP-ORDER-001):** with
    ``strict_gate=True`` (``BRUTEX_PIPELINE_STRICT_GATE=1``, resolved once at
    launch) the post-steps are literal all-or-nothing: after the historical
    phase the pure :func:`step_settle_verdict` folds the three phase gates +
    the UNCAPPED quarantine set — ANY unclean step or ANY per-symbol
    quarantine (``verdict.clean`` False) withholds the prebuild (never
    called) AND the movers warm (already gated on the prebuild), logs ONE
    ``pipeline.post_steps.strict_skipped`` WARNING naming the blocking
    signals, sets :attr:`PipelinePullReport.post_steps_strict_skipped`, and
    appends the loud ``", post-steps SKIPPED (strict gate: …)"`` note clause.
    Nothing is lost — derived timeframes re-derive on the next clean pull.
    ``strict_gate=False`` (the default) is the DEC-PREBUILD-CLEANSET-001
    cleanset path, byte-identical to before.

    **Clean-set PREBUILD, then movers (DEC-PREBUILD-CLEANSET-001, superseding
    the fetch_clean all-or-nothing gate arm of DEC-PIPELINE-AUTOPREBUILD-001
    #1145):** after the fetch phases, the FINAL data stage is the timeframe
    prebuild — if ``run_prebuild`` is injected it ALWAYS fires on the
    completed-chain path (spot + metadata gates passed and the historical
    phase RAN — an earlier stop still skips it: there is no fresh data to
    derive over), receiving the UNCAPPED quarantined-symbol name set
    (:func:`derive_quarantined_name_set`) as its EXCLUSION set, so the
    prebuild covers the CLEAN symbols (every target minus the quarantined
    ones) instead of being starved whole by one bad symbol out of ~750. Its
    :class:`~brutex.dashboard.pipeline_prebuild.PrebuildOutcome` is
    folded into the report (built symbols, total bars, capped failure names,
    the ``crashed`` error-type marker — an ``ok=False`` outcome is still
    recorded honestly, never hidden, and a guarded whole-run crash reads
    ``", prebuild CRASHED (ErrorType)"`` in the note, never a benign
    "pre-built for 0 symbol(s)"). ONLY
    AFTER the prebuild do the movers warmers fire — movers read the DERIVED
    timeframes, which the prebuild alone writes now, so warming earlier would
    read stale/absent partitions. This SUPERSEDES the early spot-segment warm
    of DEC-PIPELINE-SEGMENT-MOVERS-001 (removed): all three segment warmers
    fire together, gated on ``the prebuild SUCCEEDED over that clean set``
    (``PrebuildOutcome.ok`` — DEC-PREBUILD-CLEANSET-001 drops the
    ``fetch_clean`` arm of the chunk-2 gate: on a dirty-fetch day the movers
    now warm over the freshly-derived CLEAN symbols, honestly omitting the
    quarantined ones the note names; an ``ok=False`` prebuild is still
    RECORDED in the report but warms NOTHING, since its derived partitions
    may be stale or absent) — with no ``run_prebuild`` injected NO warmer
    fires (there is no fresh derived data to warm over). A warmed segment
    records its count in
    :attr:`PipelinePullReport.segment_movers_warmed` (``0`` is an honest "ran,
    warmed nothing"); :attr:`PipelinePullReport.movers_warmed` is the SUM
    (``None`` when no segment warmed). Neither the prebuild nor any warm ever
    affects the pull's own success (``stopped_after`` / ``ok``) — both are
    post-fetch stages over the just-filled lake, and the route binds
    ``run_prebuild`` crash-guarded
    (:func:`~brutex.dashboard.pipeline_prebuild.run_pipeline_prebuild_guarded`;
    a pull-wide GLOBAL STOP — disk-full/OOM ``LakeWriteHalt``, quota — is the
    deliberate exception: the guard RE-RAISES it, so it propagates out of this
    sequencer BEFORE any movers warm and reaches the job layer's CRITICAL
    pull-halt alert).

    The sequencer itself is O(1) — the phase calls + the gate checks; the WORK
    it triggers is the real multi-hour vendor fill + the O(symbols × bars)
    prebuild (no throughput claim, Golden Rule #10).

    **One ledger load per run (DEC-LEDGER-LOAD-SPACE-V1-001):** the fetch
    phases run inside ONE
    :func:`~brutex.data.window_ledger_store.shared_pull_ledger_scope`, so the
    spot phase's full window-ledger materialisation is REUSED by the
    historical phase (previously each phase loaded the whole O(rows) ledger —
    two full loads + two co-resident copies per pipeline run). Per-phase saves
    are unchanged; the scope closes before the prebuild/movers post-steps so
    the ledger is released, not held through the derive.

    Parameters
    ----------
    run_spot
        Zero-arg callable returning a
        :class:`~brutex.dashboard.pull_controller.BulkSpotReport` (phase 1).
    run_metadata
        Zero-arg callable returning a
        :class:`~brutex.dashboard.metadata_pull.MetadataPullReport` (phase 2).
    run_historical
        Callable receiving the sequencer's :class:`PriorStepsVerdict`
        (DEC-PULL-SIX-STEP-ORDER-001 — the route threads it as
        ``prior_steps=`` into the composite → ``pull_contract_candles`` so
        the Greeks seam sees spot + metadata cleanliness under the strict
        barrier) and returning a
        :class:`~brutex.dashboard.composite_pull.CompositePullReport`
        (phase 3).
    warm_spot_movers
        OPTIONAL zero-arg callable that warms the SPOT movers snapshots and
        returns its :class:`~brutex.dashboard.movers_warm.MoversWarmResult`
        (warmed + failed + total — DEC-MOVERS-WARM-PROGRESS-001); fired ONLY
        after a SUCCESSFUL prebuild over the clean symbol set
        (``PrebuildOutcome.ok`` — DEC-PREBUILD-CLEANSET-001). ``None`` skips
        the spot warm.
    warm_futures_movers
        OPTIONAL zero-arg callable warming the FUTURES movers snapshots; same
        gate. ``None`` skips the futures warm.
    warm_options_movers
        OPTIONAL zero-arg callable warming the OPTIONS movers snapshots; same
        gate. ``None`` skips the options warm.
    run_prebuild
        OPTIONAL callable running the timeframe prebuild
        (:func:`~brutex.dashboard.pipeline_prebuild.run_pipeline_prebuild_guarded`
        at the route) over the CLEAN symbol set and returning its
        :class:`~brutex.dashboard.pipeline_prebuild.PrebuildOutcome`. It
        receives the UNCAPPED quarantined-symbol name set as its exclusion
        set and fires on EVERY completed-chain run — clean or dirty
        (DEC-PREBUILD-CLEANSET-001). ``None`` (default) skips the prebuild
        AND the movers warm (movers gate on the prebuild having run).
    strict_gate
        The resolved :func:`strict_gate_enabled` flag
        (DEC-PULL-SIX-STEP-ORDER-001). ``True`` makes the post-step barrier
        literal all-or-nothing (see above); ``False`` (default) keeps the
        cleanset path byte-identical.

    Returns
    -------
    PipelinePullReport
        The combined, auditable outcome of every phase that ran.
    """
    # Market-hours belt (DEC-GROWW-MARKET-HOURS-GUARD-001): if the clock is inside the
    # NSE market-hours window ``[08:45, 15:35)`` IST, skip the ENTIRE vendor chain
    # (spot / metadata / historical) so a pipeline pull that reached this call (e.g. a
    # pull already dispatched as the market opened) never issues a vendor call that
    # would compete with TickVault's live feed for the shared Groww account. The
    # non-vendor prebuild / movers stages are inapplicable here (there is no pull to
    # derive from) and also do not run. The mid-run per-window ``MarketHoursHalt`` is
    # the belt for a pull already in flight; this is the belt for one about to start.
    # Checked BEFORE the shared-ledger scope opens — a deferred pull never pays the
    # whole-ledger materialisation (DEC-LEDGER-LOAD-SPACE-V1-001).
    if market_closed is not None and market_closed():
        _LOG.warning("pipeline.deferred.market_hours", reason=PHASE_MARKET_HOURS)
        return PipelinePullReport(
            spot=None,
            metadata=None,
            historical=None,
            stopped_after=PHASE_MARKET_HOURS,
            note=_market_hours_stop_note(),
        )
    # ONE whole-ledger materialisation for the fetch phases
    # (DEC-LEDGER-LOAD-SPACE-V1-001): the spot phase and the historical phase
    # each `begin_pull_ledger` the SAME lake — inside this scope the second
    # (and any later) live begin reuses the first load's instance instead of
    # re-materialising every recorded window row (measured 1,305 B/row RSS +
    # ~13.6 µs/row per full load; two distinct materialisations co-resided
    # across the phase boundary pre-scope). Per-phase SAVES are unchanged
    # (durability byte-identical); the scope closes BEFORE the post-steps
    # (prebuild/movers), releasing the ledger instead of holding it through
    # the O(symbols × bars) derive — and every market-hours defer below exits
    # (releases) it too. Safe by construction: the phases run sequentially on
    # this one thread under the pull single-flight lock (the scope is a
    # ContextVar — confined to this thread/context).
    with shared_pull_ledger_scope():
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

        # Market-hours belt BEFORE the metadata vendor phase (DEC-GROWW-MARKET-HOURS-GUARD-001):
        # spot finishing ~08:43 must NOT leak a `get_expiries`/`get_contracts` storm into the
        # trading window. Re-check the guard here (the top check covers a pull dispatched into
        # the window; this covers a spot phase that finished just before it opened). The
        # metadata phase itself ALSO consults the guard per-cell so a metadata pull that
        # CROSSES 08:45 halts mid-phase; this is the between-phases stop.
        if market_closed is not None and market_closed():
            _LOG.warning(
                "pipeline.deferred.market_hours", reason=PHASE_MARKET_HOURS, before="metadata"
            )
            return PipelinePullReport(
                spot=spot,
                metadata=None,
                historical=None,
                stopped_after=PHASE_MARKET_HOURS,
                note=_market_hours_stop_note(),
            )
        try:
            metadata = run_metadata()
        except MarketHoursHalt:
            # DEC-GROWW-MARKET-HOURS-GUARD-001: the metadata phase CROSSED 08:45 mid-flight
            # — its per-cell guard raised MarketHoursHalt and run_two_phase re-raised it as a
            # global stop. Committed expiries/contracts are already persisted (record-in-the-
            # fetcher); the un-persisted cells re-pull next out-of-hours run. Stop cleanly.
            _LOG.warning(
                "pipeline.deferred.market_hours", reason=PHASE_MARKET_HOURS, crossed="metadata"
            )
            return PipelinePullReport(
                spot=spot,
                metadata=None,
                historical=None,
                stopped_after=PHASE_MARKET_HOURS,
                note=_market_hours_stop_note(),
            )
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

        # PriorStepsVerdict (DEC-PULL-SIX-STEP-ORDER-001): spot + metadata
        # cleanliness threaded INTO the historical phase (the Greeks seam at the
        # historical tail cannot otherwise see a spot-side quarantine under the
        # strict barrier). Computed AFTER both gates passed — the gate arms are
        # True by construction here; the quarantine check is the strict extra.
        prior = PriorStepsVerdict(
            spot_clean=spot_phase_ok(spot) and not _spot_quarantine_names(spot),
            metadata_clean=metadata_phase_ok(metadata),
            strict=strict_gate,
        )
        # Market-hours belt BEFORE the historical (F&O) vendor phase
        # (DEC-GROWW-MARKET-HOURS-GUARD-001): a metadata phase that finished just before the
        # window opens must not leak the F&O candle storm into it. The F&O spine ALSO
        # consults the guard per-contract (a contract already started at 08:45 finishes its
        # few small windows — an acceptable, bounded residue), so this is the between-phases
        # stop; a phase that crosses mid-flight halts on its next contract.
        if market_closed is not None and market_closed():
            _LOG.warning(
                "pipeline.deferred.market_hours", reason=PHASE_MARKET_HOURS, before="historical"
            )
            return PipelinePullReport(
                spot=spot,
                metadata=metadata,
                historical=None,
                stopped_after=PHASE_MARKET_HOURS,
                note=_market_hours_stop_note(),
            )
        historical = run_historical(prior)
    # Post-fetch stages (optional). Derive the quarantined names FIRST
    # (DEC-PREBUILD-CLEANSET-001): the UNCAPPED set is the prebuild's
    # exclusion set — one bad symbol no longer starves the other ~N−1 of
    # derived timeframes (supersedes the #1145 fetch_clean all-or-nothing
    # gate); the capped view feeds the headline (chunk-3 honesty, unchanged).
    quarantined_names = derive_quarantined_name_set(spot, historical)
    quarantined, quarantined_count = _cap_quarantined(quarantined_names)
    # The strict post-step barrier (DEC-PULL-SIX-STEP-ORDER-001): the pure
    # settle verdict folds the three phase gates + the UNCAPPED quarantine
    # set. Settle-based, never bars>0 — a 0-bar EMPTY / far-expiry-all-
    # skipped step is settled. Cleanset (strict_gate=False) never skips.
    verdict = step_settle_verdict(
        spot_ok=spot_phase_ok(spot),
        metadata_ok=metadata_phase_ok(metadata),
        historical_ok=historical_phase_ok(historical),
        quarantined=quarantined_names,
    )
    strict_skipped = strict_gate and not verdict.clean
    # 1) PREBUILD — the FINAL data stage: derived timeframes are written ONLY
    #    here (DEC-PIPELINE-AUTOPREBUILD-001), never mid-pull. It ALWAYS runs
    #    on the completed-chain path, over the CLEAN symbol set (every target
    #    minus the uncapped quarantined names — DEC-PREBUILD-CLEANSET-001) —
    #    UNLESS the opt-in strict barrier withheld the post-steps (LOUD WARN,
    #    flagged on the report + note; nothing lost — the next pull derives).
    prebuild: PrebuildOutcome | None = None
    if strict_skipped:
        _LOG.warning(
            "pipeline.post_steps.strict_skipped",
            blocking=list(verdict.blocking),
            quarantined=sorted(quarantined_names),
        )
    elif run_prebuild is not None:
        prebuild = run_prebuild(quarantined_names)
    # 2) MOVERS WARM — strictly AFTER the prebuild (movers read the derived
    #    timeframes the prebuild just wrote); gated on the prebuild having
    #    SUCCEEDED over that clean set (`PrebuildOutcome.ok`) — a
    #    failed/partial prebuild leaves stale/absent derived partitions, so
    #    warming over them would cache a wrong snapshot. The `fetch_clean`
    #    arm is DROPPED (DEC-PREBUILD-CLEANSET-001): on a dirty-fetch day the
    #    movers warm over the freshly-derived CLEAN symbols, honestly
    #    omitting the quarantined ones the note names. A vendor-empty
    #    NO-SOURCE spot target does NOT withhold the warm
    #    (DEC-SPOTONLY-PREBUILD-EMPTY-EXCLUDE-001): nothing to derive AND
    #    structurally absent from warm discovery (no parquet ⇒ no dataset
    #    row, db_view._walk_candle_datasets' any-parquet gate) — counted +
    #    named in the note instead. The gate EXPRESSION below is unchanged —
    #    the behavioral flip is entirely `ok` now staying True.
    segment_movers_warmed: dict[str, int] = {}
    segment_movers_failed: dict[str, int] = {}
    if prebuild is not None and prebuild.ok:
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
    # never hidden behind a bare "pipeline complete" (`quarantined` /
    # `quarantined_count` derived above, before the prebuild).
    # ONE vendor-conflict-drift alert per pull (DEC-VENDOR-CONFLICT-SURFACE-001),
    # fired at THIS completion-headline seam with the reconciled totals: the
    # helper gates on the BRUTEX_CONFLICT_CLOSE_DELTA_ALERT threshold (default
    # 1.00 rupees), is best-effort, and is a silent no-op below threshold /
    # with Telegram disabled — it can never crash the pull. A gate-STOPPED run
    # (stopped_after set) deliberately does not alert here: its verdict already
    # demands operator action, and the phase report still carries the counters.
    total_conflicts, total_dup_rows, total_max_delta = vendor_surface_totals(spot, historical)
    emit_vendor_conflict_alert(
        conflicts=total_conflicts,
        max_close_delta=total_max_delta,
        dup_rows=total_dup_rows,
    )
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
            strict_blocking=verdict.blocking if strict_skipped else None,
        ),
        movers_warmed=_total_warmed(segment_movers_warmed),
        segment_movers_warmed=segment_movers_warmed,
        segment_movers_failed=segment_movers_failed,
        prebuild_built=prebuild.symbols_built if prebuild is not None else None,
        prebuild_total_bars=prebuild.total_bars if prebuild is not None else None,
        prebuild_failed=prebuild.failed if prebuild is not None else (),
        prebuild_no_source=prebuild.no_source if prebuild is not None else (),
        prebuild_no_source_count=(prebuild.no_source_count if prebuild is not None else 0),
        prebuild_crashed=prebuild.crashed if prebuild is not None else None,
        fs_allocation_note=prebuild.fs_note if prebuild is not None else None,
        prebuild_disk_guarded=prebuild.disk_guard_blocked if prebuild is not None else False,
        prebuild_opens_substituted=(prebuild.opens_substituted if prebuild is not None else None),
        quarantined_symbols=quarantined,
        quarantined_count=quarantined_count,
        post_steps_strict_skipped=strict_skipped,
    )
