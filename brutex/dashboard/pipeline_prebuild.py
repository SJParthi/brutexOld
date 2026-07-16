"""The pipeline's ONE automatic timeframe-prebuild phase (DEC-PIPELINE-AUTOPREBUILD-001).

Derived data (the 19 intraday timeframes + the derived ``1day``) is written
ONLY here — as the pull pipeline's FINAL data stage, run on EVERY
completed-chain pull over the CLEAN symbol set (quarantined symbols excluded
via ``exclude_symbols`` — DEC-PREBUILD-CLEANSET-001, superseding the #1145
fully-clean-fetch all-or-nothing gate) — never mid-pull (the always-on
post-pull daily derives and the ``BRUTEX_AUTO_DERIVE_TFS`` pull-settle
auto-derive are removed) and never from a manual button (the
``derive-timeframes`` surface is removed).
Coverage: SPOT underlyings + their FUTURES contracts, each including the
``1day`` partition (``include_daily=True`` on the injected materialiser).
OPTIONS are RETIRED from the prebuild (DEC-OPTIONS-PREBUILD-RETIRE-001 — the
operator ruling of 2026-07-15 as relayed via the coordinator, ADR-0021
RULING B, draft PR #1555): option data stays 1-minute-native (fills resolve
on the next 1-minute bar worst-case, so derived option TFs are pure cost),
NEITHER path discovers or materialises a CE/PE contract, the gated spine
reports the honest by-design no-op
(:data:`~brutex.dashboard.gated_prebuild.OPTIONS_PREBUILD_SKIPPED_NOTE`),
and already-stored derived option TFs stay INERT on disk (never deleted —
disk reclaim is an optional operator action).

Honest envelope (Golden Rule #10):

* the run is O(symbols × bars) — a full-universe prebuild re-reads + resamples
  every 1-minute partition once; NOT an O(1)-total claim;
* a fully-materialised lake costs a MEASURED multiple of its 1-minute
  footprint (DEC-LAKE-FOOTPRINT-MEASURED-001: the observed multiplier is
  persisted to ``schema_meta`` ``lake_footprint.*`` after every prebuild run;
  a conservative **6.5×** fallback applies before the first observation —
  the retired "~2-3×" estimate IGNORED the 17-column Greeks-enriched option
  1-minute rewrite of :mod:`brutex.data.greeks_enrich`, which alone pushes
  the realistic all-in cost toward ~5-6×); a PRE-RUN disk guard
  (:func:`brutex.data.lake_footprint.make_disk_guard`, escape hatch
  ``BRUTEX_PREBUILD_DISK_GUARD=off``) SKIPS the phase gracefully — never a
  half-write — when the volume's free space is below the estimated need;
  the operator ACCEPTED the materialisation cost (decree items #19+#21);
* per-symbol failures are quarantined + named (never a stage abort); a
  global-stop error (disk-full :class:`~brutex.data.errors.LakeWriteHalt`, a
  quota stop) escapes :func:`run_pipeline_prebuild` AND
  :func:`run_pipeline_prebuild_guarded` — the guard RE-RAISES exactly the
  pull-wide halt set (the job layer's COMMON
  :func:`~brutex.dashboard.pull_jobs.is_pull_halt` predicate) so
  ``BackgroundPullJob._run`` fires the CRITICAL Telegram pull-halt alert
  instead of the halt hiding behind a benign "built 0"; every OTHER
  unexpected error (a cascade-build fault, a genuine bug) is caught at the
  route seam so it can never kill the already-successful fetch job — an
  honest ``ok=False`` outcome carrying ``crashed=<ErrorType>`` after a LOUD
  ``pipeline.prebuild.failed`` log, never a silent zero.

Pure composition — the actual lake materialise is ONE injected callable
(``PrebuildTarget -> MaterializeResult``, bound by the route to
:func:`~brutex.data.timeframe_materialize.materialize_lake_timeframes` with
``include_daily=True``), and the contract discovery reuses the COMMON cascade
walker (:func:`~brutex.dashboard.gated_prebuild.discover_futures_contracts`),
so the whole flow is 100% unit-testable offline (CLAUDE.md §4 / §11).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, cast

import structlog

from brutex.dashboard.gated_prebuild import (
    OPTIONS_PREBUILD_SKIPPED_NOTE,
    SPOT_PREBUILD_SKIPPED_NOTE,
    GatedPrebuildReport,
    PhasePrebuildReport,
    discover_futures_contracts,
    run_gated_prebuild,
    underlying_match_tokens,
)
from brutex.dashboard.prebuild_parallel import PrebuildWorkItem, run_work_items
from brutex.data.derive_lock import derive_write_lock
from brutex.data.fno_candle_lake import FNO_CANDLE_LAKE_SEGMENT
from brutex.data.fs_probe import probe_allocation_granularity
from brutex.data.timeframe_materialize import PrebuildKind

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from datetime import date, datetime
    from pathlib import Path

    from brutex.dashboard.fno_cascade import FnoCascadeIndex, FnoContract
    from brutex.data.lake_footprint import DiskGuardDecision
    from brutex.data.timeframe_materialize import MaterializeResult
    from brutex.models import Instrument

_LOG = structlog.get_logger(__name__)

#: Cap on the failure NAMES carried by :attr:`PrebuildOutcome.failed` — the
#: producer truncates so a mass failure never floods the pipeline note / JSON
#: payload; the TRUE total stays in :attr:`PrebuildOutcome.failed_count`.
MAX_REPORTED_FAILURES: Final[int] = 10


@dataclass(frozen=True, slots=True)
class PrebuildTarget:
    """One lake partition the prebuild materialises (the injected-materialiser key).

    Attributes
    ----------
    exchange, segment, symbol
        The partition's lake coordinates
        (``bars/<exchange>/<segment>/<symbol>/…``).
    kind
        The :class:`~brutex.data.timeframe_materialize.PrebuildKind` selecting
        the resampler pair (intraday + daily).
    dense
        ``True`` for a SPOT INDEX underlying (``Instrument.is_index`` — the
        SAME density gate the #1031 demotion uses): selects per-session
        ``daily_open.substituted`` WARNs in the observe-only daily-open scan
        (DEC-CALENDAR-EXPECTED-BARS-DAILY-OPEN-001). Contracts and non-index spot
        keep ``False`` (count-only aggregate). Ignored for F&O kinds.
    """

    exchange: str
    segment: str
    symbol: str
    kind: PrebuildKind
    dense: bool = False


@dataclass(frozen=True, slots=True)
class PrebuildOutcome:
    """The honest outcome of one pipeline-prebuild run (DEC-PIPELINE-AUTOPREBUILD-001).

    Attributes
    ----------
    symbols_built
        Symbols (underlyings + contracts) materialised without error.
    total_bars
        Total derived bars written across every phase (intraday + ``1day``).
    failed
        The failing symbol names, CAPPED at :data:`MAX_REPORTED_FAILURES` by
        the producer (never a flooded note); ``failed_count`` is the true total.
    failed_count
        The TRUE number of per-symbol failures (may exceed ``len(failed)``).
    ok
        ``True`` iff the run completed with ZERO failures. ``False`` for any
        per-symbol failure AND for a guarded whole-run crash
        (:func:`run_pipeline_prebuild_guarded`) — never a silent zero. A
        vendor-empty NO-SOURCE spot target is NOT a failure — counted in
        ``no_source``, ``ok`` unaffected
        (DEC-SPOTONLY-PREBUILD-EMPTY-EXCLUDE-001) — so the
        ``pipeline_pull.py`` movers-warm gate opens over the healthy majority.
    crashed
        ``None`` for a run that COMPLETED (even with per-symbol failures);
        the escaping error's type name (e.g. ``"ValueError"``) when the
        WHOLE run crashed inside :func:`run_pipeline_prebuild_guarded` — so
        "ran, built 0" and "CRASHED before building anything" are honestly
        distinguishable in the pipeline note (Golden Rule #1). A pull-wide
        global-stop never sets this — it RE-RAISES out of the guard instead.
    unchanged_skips
        How many symbols the incremental derive SKIPPED because their 1-minute
        source was unchanged since the last stamped derive
        (DEC-PREBUILD-INCREMENTAL-001 — ``MaterializeResult.unchanged``). A
        skipped symbol still counts in ``symbols_built`` (its derived data is
        present + current); this tally makes the saved work visible in the
        done-log + the pipeline note instead of masquerading as "built with 0
        bars".
    fs_note
        The one-line COARSE-allocation warning from the lake-root probe
        (DEC-LAKE-FS-PROBE-001 — the prebuild writes ~0.5-1M tiny Parquet
        files, so an exFAT-class cluster wastes 30-300 GB of slack); ``None``
        when the filesystem is fine-grained, the probe was unavailable, or no
        ``lake_root`` was given. Folded into ``PipelinePullReport``'s
        ``fs_allocation_note`` + note by the pipeline.
    disk_guard_blocked
        ``True`` when the PRE-RUN disk guard (DEC-LAKE-FOOTPRINT-MEASURED-001)
        SKIPPED the whole prebuild because the lake volume's free space is
        below the estimated derived-write need — nothing was materialised
        (never a half-write), ``ok`` is ``False`` (so the movers warm — gated
        on ``ok`` — is consequently skipped), and the pipeline headline
        carries :attr:`disk_guard_detail`.
    disk_guard_detail
        The guard's honest one-line clause naming the ACTUAL numbers
        (estimated need, multiplier + its provenance, free space, the
        ``BRUTEX_PREBUILD_DISK_GUARD=off`` override); ``None`` unless
        :attr:`disk_guard_blocked`.
    opens_substituted
        The observe-only daily-open substitution total
        (DEC-CALENDAR-EXPECTED-BARS-DAILY-OPEN-001): (symbol, date) SESSIONS whose
        derived daily open silently substituted a non-09:15 first minute,
        summed over the run's SPOT targets
        (:attr:`~brutex.data.timeframe_materialize.MaterializeResult.opens_substituted`
        — F&O kinds are never scanned, an honest structural 0). A
        FULL-HISTORY count recomputed per run over each re-derived symbol's
        whole 1-minute frame — NOT "new this pull" (a symbol the incremental
        derive skips as unchanged contributes 0 that run). ``0`` for a
        guarded whole-run crash (nothing was scanned). Folded into the
        pipeline completion headline + ``PipelinePullReport``.
    no_source
        The SPOT-target names classified NO-SOURCE — ZERO stored 1-minute
        bars at derive time, nothing to derive (vendor-empty or never
        pulled; DEC-SPOTONLY-PREBUILD-EMPTY-EXCLUDE-001) — CAPPED at
        :data:`MAX_REPORTED_FAILURES` by the producer (never a flooded
        note); ``no_source_count`` is the TRUE total. NOT failures: ``ok``
        is unaffected, and one with NO parquet in ANY timeframe is
        structurally absent from movers-warm discovery (no parquet ⇒ no
        dataset row); a surviving previously stored DERIVED timeframe
        legitimately keeps it discoverable — genuine stored bars, never
        fabricated (refuter round 2, MIGRATION.md #1792) — counted + NAMED
        in the pipeline note instead of silently vanishing (Golden Rule #1).
    no_source_count
        The TRUE number of no-source spot targets (may exceed
        ``len(no_source)``).
    """

    symbols_built: int
    total_bars: int
    failed: tuple[str, ...]
    failed_count: int
    ok: bool
    crashed: str | None = None
    fs_note: str | None = None
    unchanged_skips: int = 0
    disk_guard_blocked: bool = False
    disk_guard_detail: str | None = None
    opens_substituted: int = 0
    no_source: tuple[str, ...] = ()
    no_source_count: int = 0


def _disk_guard_outcome(
    disk_guard: Callable[[], DiskGuardDecision | None] | None,
) -> PrebuildOutcome | None:
    """Run the injected pre-run disk guard; a BLOCKED verdict → the honest outcome.

    The ONE preflight both the sequential and the parallel prebuild paths share
    (CLAUDE.md §11 COMMON — DEC-LAKE-FOOTPRINT-MEASURED-001 over EITHER path, so
    ``workers > 1`` can never bypass the disk-space safety gate). Fails OPEN: a
    ``None`` guard, a raising guard (logged ``pipeline.prebuild.disk_guard_error``)
    or a non-blocked verdict all return ``None`` — the caller proceeds.
    """
    if disk_guard is None:
        return None
    decision: DiskGuardDecision | None = None
    try:
        decision = disk_guard()
    except Exception:  # pylint: disable=broad-except - the guard fails OPEN, never blocks
        _LOG.exception("pipeline.prebuild.disk_guard_error")
    if decision is None or not decision.blocked:
        return None
    _LOG.warning(
        "pipeline.prebuild.disk_guard",
        free_bytes=decision.free_bytes,
        estimated_need_bytes=decision.estimated_need_bytes,
        source_1m_bytes=decision.source_1m_bytes,
        derived_bytes=decision.derived_bytes,
        multiplier=str(decision.multiplier),
        multiplier_source=decision.multiplier_source,
        margin=str(decision.margin),
    )
    return PrebuildOutcome(
        symbols_built=0,
        total_bars=0,
        failed=(),
        failed_count=0,
        ok=False,
        disk_guard_blocked=True,
        disk_guard_detail=decision.detail,
    )


def run_pipeline_prebuild(
    *,
    instruments: Sequence[Instrument],
    cascade: FnoCascadeIndex,
    materialize: Callable[[PrebuildTarget], MaterializeResult],
    fno_segment: str = FNO_CANDLE_LAKE_SEGMENT,
    source: str = "pipeline",
    exclude_symbols: frozenset[str] = frozenset(),
    lake_root: Path | None = None,
    disk_guard: Callable[[], DiskGuardDecision | None] | None = None,
    include_spot: bool = True,
) -> PrebuildOutcome:
    """Compose + run the SPOT → FUTURES prebuild over *instruments*.

    The OPTIONS phase is RETIRED (DEC-OPTIONS-PREBUILD-RETIRE-001 — the
    operator ruling of 2026-07-15 as relayed via the coordinator, ADR-0021
    RULING B, draft PR #1555): CE/PE contracts are never discovered or
    materialised — the gated spine reports the honest by-design no-op.

    Discovery is EAGER (one cascade walk per underlying, pure in-memory) so the
    ``pipeline.prebuild.started`` log can honestly name the full scope up
    front; the phase execution then reuses the COMMON
    :func:`~brutex.dashboard.gated_prebuild.run_gated_prebuild` spine — the ONE
    quarantine/tally control flow (CLAUDE.md §11, no copy).

    A per-symbol data failure is quarantined + named (the phase and the chain
    continue); a global-stop error PROPAGATES out of this function — bind
    :func:`run_pipeline_prebuild_guarded` at a route seam that must never
    crash. Emits ``pipeline.prebuild.started`` / ``pipeline.prebuild.phase_done``
    (per phase) / ``pipeline.prebuild.done`` structured logs.

    Parameters
    ----------
    instruments
        The pulled SPOT underlyings (the lean store holds only underlyings;
        FUT/CE/PE contracts are discovered from the *cascade*).
    cascade
        The F&O candle-lake cascade
        (``build_fno_cascade(list_fno_candle_datasets(lake_root))``).
    materialize
        ``PrebuildTarget -> MaterializeResult`` — materialises ONE partition's
        derived timeframes (the route binds ``materialize_lake_timeframes``
        with ``include_daily=True``); raises on a per-symbol data error.
    fno_segment
        The F&O lake segment token for contract targets (default ``"FNO"``).
    source
        The report's selection-source label.
    exclude_symbols
        Quarantined-symbol names to EXCLUDE from the prebuild
        (DEC-PREBUILD-CLEANSET-001 — the UNCAPPED set from
        :func:`~brutex.dashboard.pipeline_pull.derive_quarantined_name_set`,
        never a display-capped tuple). An underlying is excluded when ANY of
        its COMMON :func:`~brutex.dashboard.gated_prebuild.underlying_match_tokens`
        (the raw ``symbol_name`` e.g. ``NSE-NIFTY``, the exchange-stripped
        bare ticker e.g. ``NIFTY``, and its own ``underlying_symbol``) is in
        the set — so both spot-quarantine names (``symbol_name``-shaped) and
        historical-quarantine names (bare-underlying-shaped) match; excluding
        an underlying also excludes its FUTURES contract discovery.
        HONEST envelope: an undecodable raw CONTRACT symbol in the set matches
        no underlying token and filters nothing (its underlying is unknown —
        reported by the pipeline note, never guessed). The default empty set
        is behaviour-identical to the pre-cleanset prebuild. A set that
        excludes EVERY instrument degrades honestly: the run still completes
        (a ``pipeline.prebuild.all_excluded`` warning, then an ``ok=True``
        0-built outcome), never a crash.
    lake_root
        When given, the lake root is allocation-probed at start
        (DEC-LAKE-FS-PROBE-001 — the prebuild is the ~0.5-1M-tiny-file
        writer, so a COARSE exFAT-class cluster is warned LOUDLY before the
        slack is spent); the probe's one-line note rides
        :attr:`PrebuildOutcome.fs_note` + the start log. ``None`` (the
        default) skips the probe — byte-identical to the pre-probe behaviour.
    disk_guard
        OPTIONAL pre-run disk guard (DEC-LAKE-FOOTPRINT-MEASURED-001 — the
        route binds :func:`brutex.data.lake_footprint.make_disk_guard`). A
        zero-arg callable returning a
        :class:`~brutex.data.lake_footprint.DiskGuardDecision` (or ``None`` =
        no verdict: guard disabled via ``BRUTEX_PREBUILD_DISK_GUARD=off``, or
        a measurement error — the guard FAILS OPEN). A ``blocked`` decision
        SKIPS the whole prebuild BEFORE any materialise (never a half-write):
        one loud ``pipeline.prebuild.disk_guard`` WARNING naming the actual
        numbers, an ``ok=False`` outcome flagged ``disk_guard_blocked`` (the
        movers warm — gated on ``ok`` — is consequently skipped). A guard
        callable that RAISES is caught + logged
        (``pipeline.prebuild.disk_guard_error``) and treated as no-verdict —
        a guard bug never blocks the data stage.
    include_spot
        ``False`` makes the SPOT phase an explicit REPORTED no-op while the
        FUTURES contract phase runs for real over the SAME
        *instruments*-driven cascade discovery — the fno-only pipeline's
        spot-exclusion arm (DEC-PULL-SCOPED-ALL-ONECLICK-001 round-6, the
        structural mirror of the spot-only empty-cascade arm). Default
        ``True`` is byte-identical to the pre-round-6 prebuild (modulo the
        retired OPTIONS phase).

    Returns
    -------
    PrebuildOutcome
        The built/bars/failures rollup for the pipeline report.
    """
    blocked = _disk_guard_outcome(disk_guard)
    if blocked is not None:
        return blocked
    started = time.monotonic()
    # Allocation probe FIRST (DEC-LAKE-FS-PROBE-001): warn about a coarse
    # cluster BEFORE the tiny-file fan-out spends the slack. Best-effort by
    # construction (the probe never raises).
    fs_probe = probe_allocation_granularity(lake_root) if lake_root is not None else None
    fs_note = fs_probe.note if fs_probe is not None else None
    kept = [inst for inst in instruments if not (underlying_match_tokens(inst) & exclude_symbols)]
    excluded_count = len(instruments) - len(kept)
    if excluded_count and not kept:
        # Fully-quarantined pull (DEC-PREBUILD-CLEANSET-001): nothing clean to
        # derive over — LOUD, then complete honestly with a 0-built outcome.
        _LOG.warning("pipeline.prebuild.all_excluded", excluded_symbols=excluded_count)
    instruments = _unique_spot_instruments(kept)
    futures_by_underlying: dict[str, tuple[FnoContract, ...]] = {}
    contract_exchange: dict[str, str] = {}
    # M4, sequential twin (DEC-HARDWARE-ENVELOPE-001): the loose token-set
    # discovery can match a #911 drift pair (``NSE-M&M`` + ``M&M``) to the SAME
    # contract partitions under two underlyings — dedupe by partition coords
    # (first underlying wins) so a partition is never materialised twice and
    # the sequential + parallel paths report IDENTICAL counts.
    seen_contract_coords: set[tuple[str, str, str]] = set()

    def _first_seen(contracts: tuple[FnoContract, ...]) -> tuple[FnoContract, ...]:
        out: list[FnoContract] = []
        for contract in contracts:
            coords = (contract.exchange, fno_segment, contract.symbol)
            if coords in seen_contract_coords:
                continue
            seen_contract_coords.add(coords)
            out.append(contract)
        return tuple(out)

    for inst in instruments:
        futures = _first_seen(discover_futures_contracts(inst, cascade))
        futures_by_underlying[inst.symbol_name] = futures
        for contract in futures:
            contract_exchange[contract.symbol] = contract.exchange
    _LOG.info(
        "pipeline.prebuild.started",
        # Honest scope on the fno-only spot-exclusion arm: the spot phase
        # materialises NOTHING there (the instruments still drive the F&O
        # contract discovery above).
        spot_symbols=len(instruments) if include_spot else 0,
        futures_contracts=sum(len(v) for v in futures_by_underlying.values()),
        # Honest scope key (DEC-OPTIONS-PREBUILD-RETIRE-001): CE/PE contracts
        # are never DISCOVERED any more, so a numeric "options_contracts=0"
        # would read as a real empty chain — the static token names the law
        # instead (the by-design skip, never a fake count).
        options_prebuild=OPTIONS_PREBUILD_SKIPPED_NOTE,
        # Honest payload extension (DEC-PREBUILD-CLEANSET-001): how many
        # quarantined underlyings the clean-set exclusion removed (0 on a
        # clean pull — the pre-cleanset payload plus this one key).
        excluded_symbols=excluded_count,
        fs_allocation=fs_note if fs_note is not None else "ok",
    )

    unchanged_skips = 0
    opens_substituted = 0

    def _tracked(target: PrebuildTarget) -> MaterializeResult:
        """Run the injected materialiser, tallying incremental unchanged-skips.

        DEC-PREBUILD-INCREMENTAL-001: an ``unchanged=True`` result read+wrote
        nothing (the symbol's 1-minute source was byte-identical to its stamp)
        — count it so the done-log + pipeline note can report the saved work
        honestly (never a fake "built 0 bars"). Also sums the observe-only
        ``opens_substituted`` count (DEC-CALENDAR-EXPECTED-BARS-DAILY-OPEN-001 —
        SPOT-only by construction; a contract result carries an honest 0).
        The phases run strictly sequentially (``run_gated_prebuild`` has no
        threading), so the nonlocal tallies are race-free.
        """
        nonlocal unchanged_skips, opens_substituted
        result = materialize(target)
        if result.unchanged:
            unchanged_skips += 1
        opens_substituted += result.opens_substituted
        return result

    def _materialize_spot(inst: Instrument) -> MaterializeResult:
        return _tracked(
            PrebuildTarget(
                exchange=inst.exch_id.value,
                segment=inst.segment.value,
                symbol=inst.symbol_name,
                kind=PrebuildKind.SPOT,
                # The #1031 density gate: per-session substitution WARNs only
                # for a SPOT INDEX; a dense non-index stock (RELIANCE) gets the
                # count-only aggregate — deliberate, mirrors the demotion gate.
                dense=inst.is_index,
            )
        )

    def _materialize_contract(symbol: str, kind: PrebuildKind) -> MaterializeResult:
        return _tracked(
            PrebuildTarget(
                exchange=contract_exchange[symbol],
                segment=fno_segment,
                symbol=symbol,
                kind=kind,
            )
        )

    report = run_gated_prebuild(
        underlyings=instruments,
        # ``None`` ⇒ the gated spine's REPORTED spot no-op phase (the fno-only
        # spot-exclusion arm — round-6 two-button split).
        materialize_spot=_materialize_spot if include_spot else None,
        futures_contracts_for=lambda inst: [
            c.symbol for c in futures_by_underlying[inst.symbol_name]
        ],
        materialize_future=lambda symbol: _materialize_contract(symbol, PrebuildKind.FUTURES),
        # The options callables are DELIBERATELY not injected
        # (DEC-OPTIONS-PREBUILD-RETIRE-001): the gated spine reports the
        # honest by-design no-op (OPTIONS_PREBUILD_SKIPPED_NOTE).
        source=source,
    )
    _log_phase_done(report)
    _warn_if_all_no_source(report)
    outcome = _build_outcome(
        report,
        fs_note=fs_note,
        unchanged_skips=unchanged_skips,
        opens_substituted=opens_substituted,
    )
    _log_prebuild_done(outcome, started)
    return outcome


def _log_phase_done(report: GatedPrebuildReport) -> None:
    """Emit ONE ``pipeline.prebuild.phase_done`` structured log per phase (COMMON)."""
    for phase in report.phases:
        _LOG.info(
            "pipeline.prebuild.phase_done",
            phase=phase.phase,
            attempted=len(phase.attempted),
            succeeded=len(phase.succeeded),
            failed=len(phase.failed),
            total_bars=phase.total_bars,
        )


def _log_prebuild_done(outcome: PrebuildOutcome, started: float) -> None:
    """Emit the ``pipeline.prebuild.done`` roll-up (COMMON to both paths)."""
    _LOG.info(
        "pipeline.prebuild.done",
        symbols_built=outcome.symbols_built,
        total_bars=outcome.total_bars,
        failed_count=outcome.failed_count,
        unchanged_skips=outcome.unchanged_skips,
        opens_substituted=outcome.opens_substituted,
        # Additive always-present kwarg (DEC-SPOTONLY-PREBUILD-EMPTY-EXCLUDE-001
        # — grep-able; logs are not a byte-pinned wire surface, the #1527
        # ``excluded_symbols`` precedent).
        no_source=outcome.no_source_count,
        duration_s=round(time.monotonic() - started, 3),
    )


def _warn_if_all_no_source(report: GatedPrebuildReport) -> None:
    """ONE loud WARN when EVERY attempted SPOT target read no-source (COMMON to both paths).

    DEC-SPOTONLY-PREBUILD-EMPTY-EXCLUDE-001 — the ``all_excluded`` degrade
    twin: the run still completes ``ok=True`` 0-built and the warm then fires
    (a truly empty lake's ``latest_movers_date`` is ``None`` → warms nothing,
    honest), but a WHOLE-universe no-source verdict is anomalous enough to
    warrant one WARNING beside the per-symbol INFOs.
    """
    for phase in report.phases:
        if (
            phase.phase == PrebuildKind.SPOT.value
            and phase.attempted
            and len(phase.no_source) == len(phase.attempted)
        ):
            _LOG.warning("pipeline.prebuild.all_no_source", count=len(phase.no_source))


def _unique_spot_instruments(instruments: Sequence[Instrument]) -> list[Instrument]:
    """First-wins dedupe of spot instruments by lake partition coords (M4, COMMON).

    The spot arm of the DEC-HARDWARE-ENVELOPE-001 partition dedup, shared by the
    sequential AND parallel paths so both report identical counts: two entries
    resolving to the SAME ``(exchange, segment, symbol)`` partition would
    materialise (and derive-stamp) it twice. O(instruments).
    """
    seen: set[tuple[str, str, str]] = set()
    out: list[Instrument] = []
    for inst in instruments:
        coords = (inst.exch_id.value, inst.segment.value, inst.symbol_name)
        if coords in seen:
            continue
        seen.add(coords)
        out.append(inst)
    return out


def _build_outcome(
    report: GatedPrebuildReport,
    *,
    fs_note: str | None,
    unchanged_skips: int,
    opens_substituted: int,
) -> PrebuildOutcome:
    """Roll a :class:`~brutex.dashboard.gated_prebuild.GatedPrebuildReport` into a :class:`PrebuildOutcome`.

    The ONE outcome-assembly point (CLAUDE.md §11 COMMON) shared by the
    sequential :func:`run_pipeline_prebuild` and the parallel
    :func:`run_pipeline_prebuild_parallel`, so both produce an identical outcome
    shape (the failure-name cap, the true failure count, the ``ok`` verdict, the
    incremental + daily-open tallies).
    """
    failed_names = tuple(s for phase in report.phases for s, _ in phase.failed)
    # NO-SOURCE fold (DEC-SPOTONLY-PREBUILD-EMPTY-EXCLUDE-001): only the SPOT
    # phase ever carries no_source (the sequential catch + the parallel
    # worker's kind gate), but the fold stays phase-generic — ONE law.
    no_source_names = tuple(s for phase in report.phases for s in phase.no_source)
    return PrebuildOutcome(
        symbols_built=sum(len(phase.succeeded) for phase in report.phases),
        total_bars=report.total_bars,
        failed=failed_names[:MAX_REPORTED_FAILURES],
        failed_count=len(failed_names),
        ok=report.ok,
        fs_note=fs_note,
        unchanged_skips=unchanged_skips,
        opens_substituted=opens_substituted,
        no_source=no_source_names[:MAX_REPORTED_FAILURES],
        no_source_count=len(no_source_names),
    )


def run_pipeline_prebuild_parallel(
    *,
    instruments: Sequence[Instrument],
    cascade: FnoCascadeIndex,
    lake_root: Path,
    workers: int,
    include_daily: bool = True,
    daily_before: date | None = None,
    daily_cutoff_now: datetime | None = None,
    incremental: bool | None = None,
    fno_segment: str = FNO_CANDLE_LAKE_SEGMENT,
    source: str = "pipeline",
    exclude_symbols: frozenset[str] = frozenset(),
    disk_guard: Callable[[], DiskGuardDecision | None] | None = None,
    include_spot: bool = True,
) -> PrebuildOutcome:
    """PARALLEL twin of :func:`run_pipeline_prebuild` — fan the per-symbol derive across cores.

    The CPU-bound win (DEC-HARDWARE-ENVELOPE-001): the SAME SPOT → FUTURES
    discovery + report + outcome as the sequential path (the OPTIONS phase is
    the SAME reported by-design no-op — DEC-OPTIONS-PREBUILD-RETIRE-001,
    never a planned CE/PE work item), but every
    symbol's :func:`~brutex.data.timeframe_materialize.materialize_lake_timeframes`
    runs in a :class:`~concurrent.futures.ThreadPoolExecutor` of *workers*
    threads (:func:`brutex.dashboard.prebuild_parallel.run_work_items` — THREADS
    because the heavy work is off-GIL; see that module's docstring). Different
    symbols write DIFFERENT lake partitions under a per-partition
    ``flock`` (DEC-LAKE-PARTITION-FLOCK-001), so the lake output is
    **byte-identical** to the sequential run (golden-pinned), and the per-symbol
    quarantine + the pull-wide global-stop propagation mirror the spine exactly.

    Unlike the sequential path this takes the materialise arguments EXPLICITLY
    (``lake_root`` / ``include_daily`` / ``daily_before`` / ``incremental``)
    instead of an injected closure — the worker rebuilds the call from
    :class:`~brutex.dashboard.prebuild_parallel.PrebuildWorkItem` DATA, keeping
    the work unit declarative + auditable (and process-pool-portable, though the
    shipped pool is threads). The route binds ``include_daily=True`` (matching
    the sequential ``_materialize``), so the two paths are equivalent.
    An EXPLICIT ``daily_before`` date is threaded verbatim (no grounding — it
    is LAW at the materialise layer). With ``daily_before=None`` the run's
    ``daily_cutoff_now`` IST INSTANT is pinned ONCE here (when the caller did
    not already pin it) so the forming-session cutoff is identical across
    every worker (M1 byte-identity across a midnight-IST boundary; never a
    per-worker clock skew) — since DEC-DAILY-CUTOFF-GROUNDED-001 the pinned
    value is the INSTANT, not a plain today-IST date, so each worker resolves
    the session-close-aware :func:`~brutex.calendars.nse.daily_derive_cutoff`
    from it (a post-close run derives the run day's own ``1day`` bar —
    restoring DEC-PREBUILD-SESSION-CLOSE-CUTOFF-001 on the production path)
    and a DENSE symbol's data-grounded verdict stays a pure function of
    ``(instant, own frame)`` — order/concurrency-independent. Cost:
    O(symbols × bars) work spread over *workers* cores — NOT an O(1) claim
    (Golden Rule #10).

    Parameters mirror :func:`run_pipeline_prebuild` except for the explicit
    materialise arguments above and *workers* (``>= 2``; the caller resolves it
    via :func:`~brutex.dashboard.prebuild_parallel.resolve_prebuild_workers` and
    routes ``<= 1`` to the sequential path). ``disk_guard`` runs the SAME
    pre-run disk preflight as the sequential path (:func:`_disk_guard_outcome`
    — DEC-LAKE-FOOTPRINT-MEASURED-001; a parallel run can never bypass it).
    """
    blocked = _disk_guard_outcome(disk_guard)
    if blocked is not None:
        return blocked
    started = time.monotonic()
    fs_probe = probe_allocation_granularity(lake_root)
    fs_note = fs_probe.note
    kept = [inst for inst in instruments if not (underlying_match_tokens(inst) & exclude_symbols)]
    excluded_count = len(instruments) - len(kept)
    if excluded_count and not kept:
        _LOG.warning("pipeline.prebuild.all_excluded", excluded_symbols=excluded_count)
    instruments = _unique_spot_instruments(kept)

    # M1 (byte-identity): pin the forming-session cutoff's IST INSTANT ONCE per
    # RUN. Without a pin, ``materialize_lake_timeframes`` would resolve
    # ``daily_before=None`` via ``daily_derive_cutoff(_now_ist())`` PER CALL
    # (the session-close-aware default of DEC-PREBUILD-SESSION-CLOSE-CUTOFF-001
    # — the prior comment here mis-described it as a plain "now-IST" date), so
    # a run straddling midnight IST or the session close would write a
    # different ``1day`` set for whichever symbols materialise after the
    # boundary → order/concurrency-dependent bytes (a byte-identity break the
    # golden would flag). Pinning the INSTANT here (DEC-DAILY-CUTOFF-
    # GROUNDED-001; corrects the #1401 M1 fix, whose plain today-IST DATE pin
    # bypassed the session-close-aware default and regressed the post-close
    # run-day derive) gives EVERY worker the SAME clock verdict while keeping
    # the dense symbols' data-grounded arm reachable (an explicit
    # ``daily_before`` DATE would be LAW and suppress it).
    if include_daily and daily_before is None and daily_cutoff_now is None:
        from brutex.calendars.nse import now_ist

        daily_cutoff_now = now_ist()

    def _item(
        exchange: str, segment: str, symbol: str, kind: PrebuildKind, *, dense: bool
    ) -> PrebuildWorkItem:
        return PrebuildWorkItem(
            lake_root=lake_root,
            exchange=exchange,
            segment=segment,
            symbol=symbol,
            kind=kind,
            include_daily=include_daily,
            daily_before=daily_before,
            incremental=incremental,
            dense_symbol=dense,
            daily_cutoff_now=daily_cutoff_now,
        )

    # Ordered per-phase work items — EAGER, in-parent cascade discovery (pure
    # in-memory), exactly like the sequential path. SPOT dense = is_index; the
    # #1031 density gate (contracts stay dense=False). The fno-only
    # spot-exclusion arm (include_spot=False — round-6 two-button split)
    # plans ZERO spot items; the phase reports the by-design note below.
    spot_items = (
        [
            (
                inst.symbol_name,
                _item(
                    inst.exch_id.value,
                    inst.segment.value,
                    inst.symbol_name,
                    PrebuildKind.SPOT,
                    dense=inst.is_index,
                ),
            )
            for inst in instruments
        ]
        if include_spot
        else []
    )
    futures_items: list[tuple[str, PrebuildWorkItem]] = []
    for inst in instruments:
        for contract in discover_futures_contracts(inst, cascade):
            futures_items.append(
                (
                    contract.symbol,
                    _item(
                        contract.exchange,
                        fno_segment,
                        contract.symbol,
                        PrebuildKind.FUTURES,
                        dense=False,
                    ),
                )
            )
    _LOG.info(
        "pipeline.prebuild.started",
        spot_symbols=len(spot_items),
        futures_contracts=len(futures_items),
        # DEC-OPTIONS-PREBUILD-RETIRE-001 (sequential-path parity): never a
        # numeric count for a phase that is skipped by design.
        options_prebuild=OPTIONS_PREBUILD_SKIPPED_NOTE,
        excluded_symbols=excluded_count,
        fs_allocation=fs_note if fs_note is not None else "ok",
        parallel_workers=workers,
    )

    phase_groups: tuple[tuple[PrebuildKind, list[tuple[str, PrebuildWorkItem]], str], ...] = (
        # The spot phase's empty-note: "" on the default arm (an empty spot
        # selection needs no note) vs the COMMON by-design skip note on the
        # fno-only spot-exclusion arm (sequential-path parity, §11 COMMON).
        (PrebuildKind.SPOT, spot_items, "" if include_spot else SPOT_PREBUILD_SKIPPED_NOTE),
        (
            PrebuildKind.FUTURES,
            futures_items,
            "no futures contracts found in the F&O candle lake for the selection",
        ),
        # The OPTIONS phase plans ZERO work items BY DESIGN
        # (DEC-OPTIONS-PREBUILD-RETIRE-001 — options are 1-minute-native);
        # the COMMON by-design note keeps the report shape identical to the
        # sequential path's gated no-op (§11 COMMON, never a silent skip).
        (PrebuildKind.OPTIONS, [], OPTIONS_PREBUILD_SKIPPED_NOTE),
    )

    # M4 (no double-write race): DEDUPE work by ``(exchange, segment, symbol)``
    # BEFORE sharding. The loose token-set contract discovery
    # (gated_prebuild.underlying_match_tokens) can match a #911 drift pair
    # (``NSE-M&M`` vs ``M&M``) to the SAME lake partition under two underlyings,
    # so the SAME partition + its derive-stamp could land in two workers (a
    # write/stamp race). One work unit per partition; the FIRST occurrence in
    # phase order (SPOT → FUTURES → OPTIONS) keeps the report slot.
    seen_coords: set[tuple[str, str, str]] = set()
    deduped_groups: list[tuple[PrebuildKind, list[tuple[str, PrebuildWorkItem]], str]] = []
    for kind, group, note in phase_groups:
        unique_group: list[tuple[str, PrebuildWorkItem]] = []
        for symbol, item in group:
            coords = (item.exchange, item.segment, item.symbol)
            if coords in seen_coords:
                continue
            seen_coords.add(coords)
            unique_group.append((symbol, item))
        deduped_groups.append((kind, unique_group, note))

    # ONE pool over ALL phases' UNIQUE items (cores stay busy ACROSS phase
    # boundaries); results looked up per phase by partition coords.
    flat_items = [item for _, group, _ in deduped_groups for _, item in group]
    # Hold the cross-process derive lock ONCE around the whole pool run
    # (DEC-ENGINE-MISC-PACK-001's writer signal): each worker's
    # ``materialize_lake_timeframes`` re-enters it via the process-local
    # reentrancy registry (``derive_lock._HELD_DEPTHS``), so the workers run
    # genuinely in parallel instead of serialising on the single exclusive
    # flock. Readers (``derive_in_progress``) see "in flight" for the whole
    # run — the conservative signal (the half-refreshed-TF-set hazard spans
    # the run); cross-process serialisation vs a peer derive is unchanged.
    with derive_write_lock(lake_root):
        results = run_work_items(flat_items, workers=workers)
    result_by_coords = {
        (it.exchange, it.segment, it.symbol): res
        for it, res in zip(flat_items, results, strict=True)
    }

    phases: list[PhasePrebuildReport] = []
    unchanged_skips = 0
    opens_substituted = 0
    for kind, group, empty_note in deduped_groups:
        attempted = tuple(symbol for symbol, _ in group)
        succeeded: list[str] = []
        failed: list[tuple[str, str]] = []
        no_source: list[str] = []
        total_bars = 0
        for symbol, item in group:
            res = result_by_coords[(item.exchange, item.segment, item.symbol)]
            if res.error is not None:
                failed.append((symbol, res.error))
                continue
            if res.no_source is not None:
                # DEC-SPOTONLY-PREBUILD-EMPTY-EXCLUDE-001: the worker's kind
                # gate guarantees this arm only ever fires for the SPOT group.
                no_source.append(symbol)
                continue
            # A non-error, non-no-source result ALWAYS carries a MaterializeResult
            # (a pure type-narrowing cast — no runtime branch, keeps 100% branch
            # cover).
            materialized = cast("MaterializeResult", res.result)
            succeeded.append(symbol)
            total_bars += materialized.total_bars
            if materialized.unchanged:
                unchanged_skips += 1
            opens_substituted += materialized.opens_substituted
        phases.append(
            PhasePrebuildReport(
                phase=kind.value,
                attempted=attempted,
                succeeded=tuple(succeeded),
                failed=tuple(failed),
                total_bars=total_bars,
                note=(empty_note if not attempted and not failed else ""),
                no_source=tuple(no_source),
            )
        )
    report = GatedPrebuildReport(phases=tuple(phases), source=source)
    _log_phase_done(report)
    _warn_if_all_no_source(report)
    outcome = _build_outcome(
        report,
        fs_note=fs_note,
        unchanged_skips=unchanged_skips,
        opens_substituted=opens_substituted,
    )
    _log_prebuild_done(outcome, started)
    return outcome


def run_pipeline_prebuild_guarded(
    *,
    instruments: Sequence[Instrument],
    cascade_or_error: Callable[[], FnoCascadeIndex],
    materialize: Callable[[PrebuildTarget], MaterializeResult],
    fno_segment: str = FNO_CANDLE_LAKE_SEGMENT,
    source: str = "pipeline",
    exclude_symbols: frozenset[str] = frozenset(),
    lake_root: Path | None = None,
    workers: int | None = None,
    include_daily: bool = True,
    daily_before: date | None = None,
    daily_cutoff_now: datetime | None = None,
    incremental: bool | None = None,
    disk_guard: Callable[[], DiskGuardDecision | None] | None = None,
    include_spot: bool = True,
) -> PrebuildOutcome:
    """Crash-guarded :func:`run_pipeline_prebuild` for the route seam.

    When *workers* resolves ``> 1`` AND a *lake_root* is given, the guard runs
    the PARALLEL path (:func:`run_pipeline_prebuild_parallel`) — the CPU-bound
    fan-out of DEC-HARDWARE-ENVELOPE-001; otherwise (``workers`` unset/``1``, or
    no ``lake_root``) it runs the sequential injected-callable path
    byte-identically (the kill switch). Both share the ONE crash guard below.

    ``exclude_symbols`` is forwarded verbatim — the clean-set exclusion of
    DEC-PREBUILD-CLEANSET-001 (see :func:`run_pipeline_prebuild`). ``disk_guard``
    is forwarded to WHICHEVER path runs (sequential or parallel) — the pre-run
    disk preflight of DEC-LAKE-FOOTPRINT-MEASURED-001 gates both.
    ``include_spot`` is likewise forwarded to both paths (the fno-only
    spot-exclusion arm, DEC-PULL-SCOPED-ALL-ONECLICK-001 round-6 — a
    reported spot no-op phase, F&O contract phases real; default ``True``
    byte-identical).
    ``daily_cutoff_now`` (the run's pinned IST instant,
    DEC-DAILY-CUTOFF-GROUNDED-001) reaches the PARALLEL path explicitly; on
    the sequential path the caller bakes the SAME instant into the injected
    *materialize* closure (the route does — one instant, both paths).

    The fetch already succeeded when the prebuild runs, so no ORDINARY error
    here may kill the job — an unexpected NON-halt error (a cascade-build
    fault, a genuine bug) is caught, logged LOUDLY (``pipeline.prebuild.failed``
    with the error type + repr) and turned into an honest ``ok=False`` outcome
    carrying ``crashed=<ErrorType>`` — never a silent zero, never mistakable
    for "ran, built 0". ``cascade_or_error`` is a zero-arg builder (not a
    value) so a cascade discovery fault is ALSO inside the guard.

    A pull-wide GLOBAL STOP is the deliberate exception: a disk-full/OOM
    :class:`~brutex.data.errors.LakeWriteHalt` or a quota stop RE-RAISES —
    gated on the job layer's COMMON
    :func:`~brutex.dashboard.pull_jobs.is_pull_halt` predicate (ONE source of
    truth, no second tuple) — so it reaches ``BackgroundPullJob._run``'s halt
    handling and the CRITICAL Telegram pull-halt alert fires
    (DEC-TELEGRAM-PULL-HALT-DASHBOARD-001); swallowing it here would silence
    the alert behind a benign "built 0" headline (Golden Rule #1).
    """
    try:
        cascade = cascade_or_error()
        if workers is not None and workers > 1 and lake_root is not None:
            return run_pipeline_prebuild_parallel(
                instruments=instruments,
                cascade=cascade,
                lake_root=lake_root,
                workers=workers,
                include_daily=include_daily,
                daily_before=daily_before,
                daily_cutoff_now=daily_cutoff_now,
                incremental=incremental,
                fno_segment=fno_segment,
                source=source,
                exclude_symbols=exclude_symbols,
                disk_guard=disk_guard,
                include_spot=include_spot,
            )
        return run_pipeline_prebuild(
            instruments=instruments,
            cascade=cascade,
            materialize=materialize,
            fno_segment=fno_segment,
            source=source,
            exclude_symbols=exclude_symbols,
            lake_root=lake_root,
            disk_guard=disk_guard,
            include_spot=include_spot,
        )
    except Exception as exc:  # pylint: disable=broad-except - the route seam must never crash the job
        # Imported inside the handler (mirrors is_pull_halt's own lazy-import
        # discipline): the predicate only runs on this rare crash path.
        from brutex.dashboard.pull_jobs import is_pull_halt

        if is_pull_halt(exc):
            # A pull-wide global stop (disk-full/OOM LakeWriteHalt, quota) must
            # reach the job layer's halt handling — the CRITICAL Telegram alert
            # fires there; NEVER swallow it into a benign ok=False outcome.
            raise
        _LOG.error(
            "pipeline.prebuild.failed",
            error_type=type(exc).__name__,
            error=repr(exc),
        )
        return PrebuildOutcome(
            symbols_built=0,
            total_bars=0,
            failed=(),
            failed_count=0,
            ok=False,
            crashed=type(exc).__name__,
        )


__all__ = [
    "MAX_REPORTED_FAILURES",
    "PrebuildOutcome",
    "PrebuildTarget",
    "run_pipeline_prebuild",
    "run_pipeline_prebuild_guarded",
    "run_pipeline_prebuild_parallel",
]
