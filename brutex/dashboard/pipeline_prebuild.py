"""The pipeline's ONE automatic timeframe-prebuild phase (DEC-PIPELINE-AUTOPREBUILD-001).

Derived data (the 19 intraday timeframes + the derived ``1day``) is written
ONLY here — as the pull pipeline's FINAL data stage, run on EVERY
completed-chain pull over the CLEAN symbol set (quarantined symbols excluded
via ``exclude_symbols`` — DEC-PREBUILD-CLEANSET-001, superseding the #1145
fully-clean-fetch all-or-nothing gate) — never mid-pull (the always-on
post-pull daily derives and the ``BRUTEX_AUTO_DERIVE_TFS`` pull-settle
auto-derive are removed) and never from a manual button (the
``derive-timeframes`` surface is removed).
Coverage: SPOT underlyings + their FUTURES contracts + their **OPTIONS**
contracts (the CE/PE chain the F&O candle lake actually holds), each including
the ``1day`` partition (``include_daily=True`` on the injected materialiser).

Honest envelope (Golden Rule #10):

* the run is O(symbols × bars) — a full-universe prebuild re-reads + resamples
  every 1-minute partition once; NOT an O(1)-total claim;
* materialising the options chain costs **~2-3× the options 1-minute lake**
  once all 19 intraday + ``1day`` partitions exist — operator-accepted by the
  decree (supersedes the earlier options-on-demand design);
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
walkers (:func:`~brutex.dashboard.gated_prebuild.discover_futures_contracts` /
:func:`~brutex.dashboard.gated_prebuild.discover_option_contracts`), so the
whole flow is 100% unit-testable offline (CLAUDE.md §4 / §11).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import structlog

from brutex.dashboard.gated_prebuild import (
    discover_futures_contracts,
    discover_option_contracts,
    run_gated_prebuild,
    underlying_match_tokens,
)
from brutex.data.fno_candle_lake import FNO_CANDLE_LAKE_SEGMENT
from brutex.data.fs_probe import probe_allocation_granularity
from brutex.data.timeframe_materialize import PrebuildKind

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from brutex.dashboard.fno_cascade import FnoCascadeIndex, FnoContract
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
    """

    exchange: str
    segment: str
    symbol: str
    kind: PrebuildKind


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
        (:func:`run_pipeline_prebuild_guarded`) — never a silent zero.
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
    """

    symbols_built: int
    total_bars: int
    failed: tuple[str, ...]
    failed_count: int
    ok: bool
    crashed: str | None = None
    fs_note: str | None = None
    unchanged_skips: int = 0


def run_pipeline_prebuild(
    *,
    instruments: Sequence[Instrument],
    cascade: FnoCascadeIndex,
    materialize: Callable[[PrebuildTarget], MaterializeResult],
    fno_segment: str = FNO_CANDLE_LAKE_SEGMENT,
    source: str = "pipeline",
    exclude_symbols: frozenset[str] = frozenset(),
    lake_root: Path | None = None,
) -> PrebuildOutcome:
    """Compose + run the SPOT → FUTURES → OPTIONS prebuild over *instruments*.

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
        an underlying also excludes its FUTURES + OPTIONS contract discovery.
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

    Returns
    -------
    PrebuildOutcome
        The built/bars/failures rollup for the pipeline report.
    """
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
    instruments = kept
    futures_by_underlying: dict[str, tuple[FnoContract, ...]] = {}
    options_by_underlying: dict[str, tuple[FnoContract, ...]] = {}
    contract_exchange: dict[str, str] = {}
    for inst in instruments:
        futures = discover_futures_contracts(inst, cascade)
        options = discover_option_contracts(inst, cascade)
        futures_by_underlying[inst.symbol_name] = futures
        options_by_underlying[inst.symbol_name] = options
        for contract in (*futures, *options):
            contract_exchange[contract.symbol] = contract.exchange
    _LOG.info(
        "pipeline.prebuild.started",
        spot_symbols=len(instruments),
        futures_contracts=sum(len(v) for v in futures_by_underlying.values()),
        options_contracts=sum(len(v) for v in options_by_underlying.values()),
        # Honest payload extension (DEC-PREBUILD-CLEANSET-001): how many
        # quarantined underlyings the clean-set exclusion removed (0 on a
        # clean pull — the pre-cleanset payload plus this one key).
        excluded_symbols=excluded_count,
        fs_allocation=fs_note if fs_note is not None else "ok",
    )

    unchanged_skips = 0

    def _tracked(target: PrebuildTarget) -> MaterializeResult:
        """Run the injected materialiser, tallying incremental unchanged-skips.

        DEC-PREBUILD-INCREMENTAL-001: an ``unchanged=True`` result read+wrote
        nothing (the symbol's 1-minute source was byte-identical to its stamp)
        — count it so the done-log + pipeline note can report the saved work
        honestly (never a fake "built 0 bars").
        """
        nonlocal unchanged_skips
        result = materialize(target)
        if result.unchanged:
            unchanged_skips += 1
        return result

    def _materialize_spot(inst: Instrument) -> MaterializeResult:
        return _tracked(
            PrebuildTarget(
                exchange=inst.exch_id.value,
                segment=inst.segment.value,
                symbol=inst.symbol_name,
                kind=PrebuildKind.SPOT,
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
        materialize_spot=_materialize_spot,
        futures_contracts_for=lambda inst: [
            c.symbol for c in futures_by_underlying[inst.symbol_name]
        ],
        materialize_future=lambda symbol: _materialize_contract(symbol, PrebuildKind.FUTURES),
        options_contracts_for=lambda inst: [
            c.symbol for c in options_by_underlying[inst.symbol_name]
        ],
        materialize_option=lambda symbol: _materialize_contract(symbol, PrebuildKind.OPTIONS),
        source=source,
    )
    for phase in report.phases:
        _LOG.info(
            "pipeline.prebuild.phase_done",
            phase=phase.phase,
            attempted=len(phase.attempted),
            succeeded=len(phase.succeeded),
            failed=len(phase.failed),
            total_bars=phase.total_bars,
        )
    failed_names = tuple(s for phase in report.phases for s, _ in phase.failed)
    outcome = PrebuildOutcome(
        symbols_built=sum(len(phase.succeeded) for phase in report.phases),
        total_bars=report.total_bars,
        failed=failed_names[:MAX_REPORTED_FAILURES],
        failed_count=len(failed_names),
        ok=report.ok,
        fs_note=fs_note,
        unchanged_skips=unchanged_skips,
    )
    _LOG.info(
        "pipeline.prebuild.done",
        symbols_built=outcome.symbols_built,
        total_bars=outcome.total_bars,
        failed_count=outcome.failed_count,
        unchanged_skips=outcome.unchanged_skips,
        duration_s=round(time.monotonic() - started, 3),
    )
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
) -> PrebuildOutcome:
    """Crash-guarded :func:`run_pipeline_prebuild` for the route seam.

    ``exclude_symbols`` is forwarded verbatim — the clean-set exclusion of
    DEC-PREBUILD-CLEANSET-001 (see :func:`run_pipeline_prebuild`).

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
        return run_pipeline_prebuild(
            instruments=instruments,
            cascade=cascade,
            materialize=materialize,
            fno_segment=fno_segment,
            source=source,
            exclude_symbols=exclude_symbols,
            lake_root=lake_root,
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
]
