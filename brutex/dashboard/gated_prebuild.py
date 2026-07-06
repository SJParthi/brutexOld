"""Segment-aware multi-symbol timeframe pre-build (DEC-PREBUILD-SELECT-001).

The pipeline's automatic FINAL prebuild phase (DEC-PIPELINE-AUTOPREBUILD-001 —
the manual ``POST /api/v1/universe/derive-timeframes`` surface is RETIRED)
pre-builds the selected underlyings' derived timeframes in ORDERED PHASES —
**SPOT → FUTURES → OPTIONS**:

* every selected symbol of a phase is materialised before advancing;
* a per-symbol DATA failure is QUARANTINED — recorded in the phase's
  ``failed``, the phase CONTINUES to the next symbol, AND the chain CONTINUES
  to the later phases (DEC-PIPELINE-AUTOPREBUILD-001: "one bad symbol never
  aborts a stage" — this SUPERSEDES the halt-on-first-phase-failure gate of
  DEC-PREBUILD-SELECT-001, whose ``halted_after`` field is removed);
* only a GLOBAL-STOP error (:class:`~brutex.data.errors.LakeWriteHalt` —
  disk-full / OOM — or a genuine code bug outside
  :func:`_per_symbol_data_errors`) PROPAGATES and halts the run.

The SPOT phase materialises each selected UNDERLYING's spot timeframes. The
FUTURES phase, per underlying, DISCOVERS its FUT contracts in the F&O candle
lake (:func:`discover_futures_contracts`, bounded — a handful of serial-month
futures) and materialises each contract OI-aware. The OPTIONS phase does the
SAME for the underlying's CE/PE contracts (:func:`discover_option_contracts`)
when the options callables are injected — DEC-PIPELINE-AUTOPREBUILD-001
supersedes the earlier "options stay on-demand" no-op; honest cost (Golden
Rule #10, DEC-LAKE-FOOTPRINT-MEASURED-001): a fully-materialised lake costs a
MEASURED multiple of its 1-minute footprint (observed multiplier persisted to
``schema_meta``; conservative 6.5× fallback — the retired "~2-3×" guess ignored
the Greeks-enriched 17-column 1-minute rewrite), operator-accepted. When the
options callables are NOT injected the phase is an explicit, REPORTED no-op
(never a silent skip).

Pure orchestration — the spot materialise, the contract discovery, and the
contract materialise are all INJECTED callables, so the whole control flow
(phase order, per-symbol quarantine, global-stop propagation) is 100%
unit-testable with fakes (no lake, no DuckDB, no FastAPI; CLAUDE.md §4 / §11).
The cost is O(underlyings) for spot + O(underlyings × contracts-per-underlying)
for the futures/options discovery + materialise — NOT an O(1)-total claim
(Golden Rule #10).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

from brutex.data.errors import ResampleInputError, parquet_read_skip_errors
from brutex.data.timeframe_materialize import PrebuildKind
from brutex.observability.progress_beat import emit_progress_beat

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from brutex.dashboard.fno_cascade import FnoCascadeIndex, FnoContract
    from brutex.data.timeframe_materialize import MaterializeResult
    from brutex.models import Instrument

_LOG = structlog.get_logger(__name__)


def _per_symbol_data_errors() -> tuple[type[BaseException], ...]:
    """The per-symbol DATA-fault quarantine set, resolved LAZILY at except time.

    (DEC-PREBUILD-SELECT-001; widened by DEC-LAKE-WRITE-UNIQUE-001 +
    DEC-PREBUILD-CORRUPT-SKIP-001.) ``ValueError`` is the materialise core's
    historical data-error surface; :class:`~brutex.data.errors.ResampleInputError`
    is the canonical resampler's input tripwire (DEC-RESAMPLER-INPUT-GUARD-001) —
    raised when ONE symbol's stored 1-minute partition is POISONED (duplicate /
    out-of-order timestamps); the rest is the COMMON data-layer
    unreadable-Parquet set :func:`brutex.data.errors.parquet_read_skip_errors`
    (``polars.exceptions.PolarsError`` + ``OSError`` — the corrupt / truncated /
    version-incompatible month-file surface, the same one-bad-file-never-kills-
    the-pass discipline as DEC-MOVERS-CORRUPT-SKIP-001 #886 /
    DEC-FETCH-PARSER-HARDENING-001 #1038). A quarantined symbol is reported in
    ``failed`` and every other symbol still runs.

    DELIBERATELY narrow, NOT ``DataLakeError`` broadly:
    :class:`~brutex.data.errors.LakeWriteHalt` (a sibling ``DataLakeError`` —
    disk-full / OOM, a pull-wide global stop) must STILL propagate, never be
    miscounted as a per-symbol failure; a genuine code bug (``KeyError`` /
    ``TypeError`` / …) propagates too.

    A FUNCTION, not a module constant, because ``parquet_read_skip_errors``
    imports ``polars`` inside its call: the tuple is built only when an
    exception is already unwinding (O(1); after the first import, one
    ``sys.modules`` hit) and this module stays importable without polars.
    HONEST wheel-free fallback (Golden Rule #10): where polars is NOT installed
    (it lives in the optional ``[data]`` extra; the unit suite runs wheel-free)
    ``parquet_read_skip_errors`` raises ``ModuleNotFoundError`` — caught here,
    falling back to the file-level ``(OSError,)`` alone. Nothing is lost: with
    no polars installed, no ``PolarsError`` can ever be raised, so the fallback
    is NOT a swallowed signal.
    """
    try:
        skip: tuple[type[BaseException], ...] = parquet_read_skip_errors()
    except ModuleNotFoundError:
        skip = (OSError,)
    return (ValueError, ResampleInputError, *skip)


#: The instrument-type token the F&O candle lake's decoded ``instrument_type``
#: carries for a future (:func:`brutex.data.fno_symbol.parse_fno_symbol`).
_FUTURES_TYPE: str = "FUT"

#: The instrument-type tokens for an option leg (call / put).
_OPTION_TYPES: frozenset[str] = frozenset({"CE", "PE"})

#: The exchange prefixes a ``groww_symbol`` carries (``NSE-NIFTY`` →
#: lake-underlying ``NIFTY``). Stripped when matching a selected underlying's
#: ``symbol_name`` against the lake's decoded ``underlying`` token.
_EXCHANGE_PREFIXES: tuple[str, ...] = ("NSE-", "BSE-")


def underlying_match_tokens(instrument: Instrument) -> frozenset[str]:
    """Candidate lake-``underlying`` tokens for a selected underlying (DEC-PREBUILD-SELECT-001).

    The F&O candle lake's decoded ``underlying`` is the BARE ticker
    (``NIFTY`` / ``RELIANCE`` / ``BAJAJ-AUTO``) while a selected instrument's
    ``symbol_name`` may be the exchange-prefixed groww_symbol (``NSE-NIFTY``) or
    already bare. This returns the set of tokens to match against
    :meth:`brutex.dashboard.fno_cascade.FnoCascadeIndex.underlyings`: the raw
    ``symbol_name``, the same with any ``NSE-``/``BSE-`` prefix stripped, and the
    instrument's own ``underlying_symbol`` when set (for a derivative selection).
    Pure — no I/O.
    """
    tokens: set[str] = {instrument.symbol_name}
    for prefix in _EXCHANGE_PREFIXES:
        if instrument.symbol_name.startswith(prefix):
            tokens.add(instrument.symbol_name[len(prefix) :])
    if instrument.underlying_symbol:
        tokens.add(instrument.underlying_symbol)
    return frozenset(tokens)


def _discover_contracts(
    instrument: Instrument,
    cascade: FnoCascadeIndex,
    *,
    want: Callable[[str | None], bool],
) -> tuple[FnoContract, ...]:
    """Shared cascade walk behind the futures + options discovery (CLAUDE.md §11 COMMON).

    Matches the selected underlying to the cascade's underlyings via
    :func:`underlying_match_tokens`, then returns every contract across that
    underlying's expiries whose decoded ``instrument_type`` satisfies *want* —
    the cascade is pre-grouped, every accessor O(1); the work here is
    O(expiries × contracts) over ONE underlying. Empty tuple when the
    underlying has no matching F&O data — an honest skip, never a fabricated
    contract. Deterministic order (by expiry, then the cascade's futures-first
    contract order).
    """
    tokens = underlying_match_tokens(instrument)
    matched = next((u for u in cascade.underlyings() if u in tokens), None)
    if matched is None:
        return ()
    out: list[FnoContract] = []
    for expiry in cascade.expiries(matched):
        out.extend(c for c in cascade.contracts(matched, expiry) if want(c.instrument_type))
    return tuple(out)


def discover_futures_contracts(
    instrument: Instrument,
    cascade: FnoCascadeIndex,
) -> tuple[FnoContract, ...]:
    """Discover *instrument*'s FUTURES contracts in the F&O candle lake (DEC-PREBUILD-SELECT-001).

    Every contract across the matched underlying's expiries whose decoded
    ``instrument_type`` is ``FUT`` — a handful of serial-month futures, not an
    unbounded scan. See :func:`_discover_contracts` for the shared walk.
    """
    return _discover_contracts(instrument, cascade, want=lambda t: t == _FUTURES_TYPE)


def discover_option_contracts(
    instrument: Instrument,
    cascade: FnoCascadeIndex,
) -> tuple[FnoContract, ...]:
    """Discover *instrument*'s OPTION (CE/PE) contracts in the F&O candle lake.

    The options twin of :func:`discover_futures_contracts`
    (DEC-PIPELINE-AUTOPREBUILD-001 — options now materialise like futures):
    every contract across the matched underlying's expiries whose decoded
    ``instrument_type`` is ``CE`` or ``PE``. Bounded to the contracts the lake
    ACTUALLY holds candles for (the cascade is derived from the stored
    partitions), so an unpulled strike is never enumerated. Honest cost
    (Golden Rule #10, DEC-LAKE-FOOTPRINT-MEASURED-001): the chain is large
    (every pulled strike × CE/PE) — O(contracts-in-lake) per underlying, and
    materialising them all costs a MEASURED multiple of the 1-minute footprint
    (observed ``schema_meta`` multiplier; conservative 6.5× fallback — the
    retired "~2-3×" guess ignored the Greeks-enriched 1-minute rewrite),
    operator-accepted.
    """
    return _discover_contracts(instrument, cascade, want=lambda t: t in _OPTION_TYPES)


#: The phase order — SPOT first, then FUTURES, then OPTIONS
#: (DEC-PREBUILD-SELECT-001). Since DEC-PIPELINE-AUTOPREBUILD-001 EVERY phase
#: always runs (per-symbol failures are quarantined, never a chain halt); only
#: a global-stop error propagates.
PREBUILD_PHASE_ORDER: tuple[PrebuildKind, ...] = (
    PrebuildKind.SPOT,
    PrebuildKind.FUTURES,
    PrebuildKind.OPTIONS,
)


@dataclass(frozen=True, slots=True)
class PhasePrebuildReport:
    """The outcome of ONE pre-build phase (DEC-PREBUILD-SELECT-001).

    Attributes
    ----------
    phase
        The phase's :class:`~brutex.data.timeframe_materialize.PrebuildKind`
        value (``"spot"`` / ``"futures"`` / ``"options"``).
    attempted
        The symbols attempted in this phase, in selection order — underlying
        ``symbol_name`` s for SPOT, contract symbols for FUTURES/OPTIONS.
    succeeded
        The symbols materialised without error (a subset of *attempted*, same
        order).
    failed
        ``(symbol, error)`` for every symbol whose materialise RAISED a
        per-symbol data error, in the order they failed. Failures are honestly
        reported here; they never halt the phase or the chain
        (DEC-PIPELINE-AUTOPREBUILD-001).
    total_bars
        The sum of bars written across this phase's successful symbols.
    note
        A short human note (e.g. the options-not-injected explanation), or ``""``.
    """

    phase: str
    attempted: tuple[str, ...]
    succeeded: tuple[str, ...]
    failed: tuple[tuple[str, str], ...]
    total_bars: int
    note: str = ""

    @property
    def ok(self) -> bool:
        """``True`` iff every attempted symbol succeeded (no failures)."""
        return not self.failed

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly dict for the pipeline report + panel."""
        return {
            "phase": self.phase,
            "ok": self.ok,
            "attempted": list(self.attempted),
            "succeeded": list(self.succeeded),
            "failed": [{"symbol": s, "error": e} for s, e in self.failed],
            "total_bars": self.total_bars,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class GatedPrebuildReport:
    """The structured result of a multi-symbol pre-build (DEC-PREBUILD-SELECT-001).

    Since DEC-PIPELINE-AUTOPREBUILD-001 every phase always runs (per-symbol
    failures are quarantined into each phase's ``failed``, never a chain halt),
    so the former ``halted_after`` field is REMOVED — a global-stop error
    (disk-full / OOM / a genuine bug) PROPAGATES out of
    :func:`run_gated_prebuild` instead of producing a report at all.

    Attributes
    ----------
    phases
        Each phase, in execution order (always all of
        :data:`PREBUILD_PHASE_ORDER` when the run returns).
    source
        The selection source label (e.g. ``"pipeline"``).
    """

    phases: tuple[PhasePrebuildReport, ...] = ()
    source: str = ""

    @property
    def ok(self) -> bool:
        """``True`` iff every phase passed (zero per-symbol failures)."""
        return all(p.ok for p in self.phases)

    @property
    def total_bars(self) -> int:
        """Total bars written across every phase."""
        return sum(p.total_bars for p in self.phases)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly dict for the pipeline report + panel."""
        return {
            "ok": self.ok,
            "source": self.source,
            "total_bars": self.total_bars,
            "phases": [p.to_dict() for p in self.phases],
        }


def _run_spot_phase(
    underlyings: Sequence[Instrument],
    materialize_spot: Callable[[Instrument], MaterializeResult],
) -> PhasePrebuildReport:
    """SPOT phase: materialise each selected underlying's spot timeframes.

    Each underlying is materialised independently — one that RAISES a data error
    (:func:`_per_symbol_data_errors` — ``ValueError`` / ``OSError`` / a corrupt
    stored Parquet month file (``polars.exceptions.PolarsError``,
    DEC-PREBUILD-CORRUPT-SKIP-001) / the resampler's
    :class:`~brutex.data.errors.ResampleInputError` on a poisoned stored
    partition) is recorded in ``failed`` (the rest still run, so ALL
    failures are named); a genuine bug (``KeyError`` / ``TypeError`` / …) or a
    pull-wide :class:`~brutex.data.errors.LakeWriteHalt` PROPAGATES (never
    miscounted as a per-symbol data failure).
    """
    attempted = tuple(inst.symbol_name for inst in underlyings)
    succeeded: list[str] = []
    failed: list[tuple[str, str]] = []
    total_bars = 0
    for inst in underlyings:
        # Liveness beat per underlying (DEC-PIPELINE-STALL-HEARTBEAT-001): the prebuild
        # phase runs minutes-to-hours with NO fetch heartbeat, so without this the pull
        # stall watchdog false-declared a HEALTHY pipeline HUNG (forensically proven).
        # No-op outside a dashboard pull; best-effort; O(1).
        emit_progress_beat()
        try:
            result = materialize_spot(inst)
        except _per_symbol_data_errors() as exc:
            _LOG.warning(
                "universe.prebuild.spot_failed",
                symbol=inst.symbol_name,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            failed.append((inst.symbol_name, f"{type(exc).__name__}: {exc}"))
            continue
        succeeded.append(inst.symbol_name)
        total_bars += result.total_bars
    return PhasePrebuildReport(
        phase=PrebuildKind.SPOT.value,
        attempted=attempted,
        succeeded=tuple(succeeded),
        failed=tuple(failed),
        total_bars=total_bars,
    )


def _run_contract_phase(
    *,
    phase: PrebuildKind,
    underlyings: Sequence[Instrument],
    contracts_for: Callable[[Instrument], Sequence[str]],
    materialize_contract: Callable[[str], MaterializeResult],
    empty_note: str,
) -> PhasePrebuildReport:
    """Shared contract phase (FUTURES / OPTIONS): discover + materialise per underlying.

    The ONE control flow behind both derivative phases (CLAUDE.md §11 COMMON —
    the options phase of DEC-PIPELINE-AUTOPREBUILD-001 reuses the futures loop
    instead of copying it). For each selected underlying, *contracts_for*
    DISCOVERS its contract symbols in the F&O candle lake and
    *materialize_contract* materialises each. The phase's ``attempted`` /
    ``succeeded`` / ``failed`` are keyed by CONTRACT symbol (not underlying), so
    a partial failure names the exact contract. An underlying with NO discovered
    contracts contributes nothing — an honest skip, never a fabricated bar. A
    data error (:func:`_per_symbol_data_errors`) in discovery OR materialise is
    recorded as a failure and the loop CONTINUES; a genuine bug (and a pull-wide
    :class:`~brutex.data.errors.LakeWriteHalt`) propagates.
    """
    attempted: list[str] = []
    succeeded: list[str] = []
    failed: list[tuple[str, str]] = []
    total_bars = 0
    for inst in underlyings:
        try:
            contracts = contracts_for(inst)
        except _per_symbol_data_errors() as exc:
            _LOG.warning(
                "universe.prebuild.contract_discovery_failed",
                phase=phase.value,
                underlying=inst.symbol_name,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            failed.append((inst.symbol_name, f"{type(exc).__name__}: {exc}"))
            continue
        for contract in contracts:
            attempted.append(contract)
            # Liveness beat per contract (DEC-PIPELINE-STALL-HEARTBEAT-001) — see the
            # spot phase's beat; the contract fan-out is the prebuild's long tail.
            emit_progress_beat()
            try:
                result = materialize_contract(contract)
            except _per_symbol_data_errors() as exc:
                _LOG.warning(
                    "universe.prebuild.contract_failed",
                    phase=phase.value,
                    underlying=inst.symbol_name,
                    contract=contract,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                failed.append((contract, f"{type(exc).__name__}: {exc}"))
                continue
            succeeded.append(contract)
            total_bars += result.total_bars
    return PhasePrebuildReport(
        phase=phase.value,
        attempted=tuple(attempted),
        succeeded=tuple(succeeded),
        failed=tuple(failed),
        total_bars=total_bars,
        note=(empty_note if not attempted and not failed else ""),
    )


def _run_options_noop_phase(underlyings: Sequence[Instrument]) -> PhasePrebuildReport:
    """OPTIONS phase when the options callables are NOT injected: a reported no-op.

    Honest (Golden Rule #10): with no ``options_contracts_for`` /
    ``materialize_option`` injected the phase can materialise nothing — it
    records the attempted underlyings, succeeds with zero work, and carries the
    not-injected note so the skip is visible, never silent.
    """
    return PhasePrebuildReport(
        phase=PrebuildKind.OPTIONS.value,
        attempted=tuple(inst.symbol_name for inst in underlyings),
        succeeded=(),
        failed=(),
        total_bars=0,
        note="options materialisation not injected — skipped (no-op)",
    )


def run_gated_prebuild(
    *,
    underlyings: Sequence[Instrument],
    materialize_spot: Callable[[Instrument], MaterializeResult],
    futures_contracts_for: Callable[[Instrument], Sequence[str]],
    materialize_future: Callable[[str], MaterializeResult],
    options_contracts_for: Callable[[Instrument], Sequence[str]] | None = None,
    materialize_option: Callable[[str], MaterializeResult] | None = None,
    source: str = "",
) -> GatedPrebuildReport:
    """Run the segment-aware pre-build over the selected *underlyings*.

    Runs the phases in :data:`PREBUILD_PHASE_ORDER` — **SPOT → FUTURES →
    OPTIONS**. Since DEC-PIPELINE-AUTOPREBUILD-001 EVERY phase runs to
    completion: a per-symbol data failure is quarantined into its phase's
    ``failed`` (the phase continues to the next symbol AND the chain continues
    to the later phases — "one bad symbol never aborts a stage"); only a
    global-stop error (:class:`~brutex.data.errors.LakeWriteHalt`, or a genuine
    code bug outside :func:`_per_symbol_data_errors`) PROPAGATES. This
    supersedes the halt-on-first-phase-failure gate of DEC-PREBUILD-SELECT-001.

    * **SPOT** — materialise each selected underlying's spot timeframes (the
      OI-dropping spot resampler), via *materialize_spot*.
    * **FUTURES** — for each underlying, *futures_contracts_for* DISCOVERS its
      FUT contracts in the F&O candle lake and *materialize_future* materialises
      each contract OI-aware. Keyed by contract symbol.
    * **OPTIONS** — the SAME per-contract loop over the underlying's CE/PE
      contracts (*options_contracts_for* + *materialize_option*,
      DEC-PIPELINE-AUTOPREBUILD-001 — supersedes the on-demand-only no-op;
      honest cost per DEC-LAKE-FOOTPRINT-MEASURED-001: a MEASURED multiple of
      the 1-minute footprint once fully materialised — observed ``schema_meta``
      multiplier, conservative 6.5× fallback; the retired "~2-3×" guess
      ignored the Greeks-enriched 1-minute rewrite).
      When EITHER options callable is ``None`` the phase is an explicit,
      REPORTED no-op (back-compat — never a silent skip).

    Pure orchestration — all I/O is injected, so the whole control flow is
    offline-testable (CLAUDE.md §4 / §11). O(underlyings) for spot +
    O(underlyings × contracts-per) for the contract phases; NOT an O(1)-total
    claim (Golden Rule #10).

    Parameters
    ----------
    underlyings
        The resolved underlying instruments to pre-build.
    materialize_spot
        ``instrument -> MaterializeResult`` — pre-builds ONE underlying's SPOT
        timeframes; raises on a per-symbol data error.
    futures_contracts_for
        ``instrument -> Sequence[contract_symbol]`` — the underlying's futures
        contract symbols discovered in the F&O candle lake (bounded).
    materialize_future
        ``contract_symbol -> MaterializeResult`` — pre-builds ONE futures
        contract's OI-aware timeframes; raises on a per-contract data error.
    options_contracts_for
        ``instrument -> Sequence[contract_symbol]`` — the underlying's CE/PE
        contract symbols discovered in the F&O candle lake, or ``None`` (the
        options phase becomes a reported no-op).
    materialize_option
        ``contract_symbol -> MaterializeResult`` — pre-builds ONE option
        contract's OI-aware timeframes, or ``None`` (reported no-op).
    source
        The selection source label for the report (e.g. ``"pipeline"``).

    Returns
    -------
    GatedPrebuildReport
        The per-phase reports (all phases always present).
    """
    phases: list[PhasePrebuildReport] = []

    if options_contracts_for is not None and materialize_option is not None:
        # Bind the narrowed callables so the closure below is statically non-None.
        opts_for = options_contracts_for
        materialize_opt = materialize_option

        def _options_phase() -> PhasePrebuildReport:
            return _run_contract_phase(
                phase=PrebuildKind.OPTIONS,
                underlyings=underlyings,
                contracts_for=opts_for,
                materialize_contract=materialize_opt,
                empty_note="no option contracts found in the F&O candle lake for the selection",
            )
    else:

        def _options_phase() -> PhasePrebuildReport:
            return _run_options_noop_phase(underlyings)

    runners: tuple[tuple[PrebuildKind, Callable[[], PhasePrebuildReport]], ...] = (
        (PrebuildKind.SPOT, lambda: _run_spot_phase(underlyings, materialize_spot)),
        (
            PrebuildKind.FUTURES,
            lambda: _run_contract_phase(
                phase=PrebuildKind.FUTURES,
                underlyings=underlyings,
                contracts_for=futures_contracts_for,
                materialize_contract=materialize_future,
                empty_note="no futures contracts found in the F&O candle lake for the selection",
            ),
        ),
        (PrebuildKind.OPTIONS, _options_phase),
    )
    for kind, run_phase in runners:
        report = run_phase()
        phases.append(report)
        if not report.ok:
            # LOUD, non-halting (DEC-PIPELINE-AUTOPREBUILD-001): the failures are
            # named; the chain continues to the later phases.
            _LOG.warning(
                "universe.prebuild.phase_failures",
                phase=kind.value,
                attempted=len(report.attempted),
                failed=[s for s, _ in report.failed],
            )
    _LOG.info(
        "universe.prebuild.done",
        source=source,
        phases=[p.phase for p in phases],
        total_bars=sum(p.total_bars for p in phases),
        failed=sum(len(p.failed) for p in phases),
    )
    return GatedPrebuildReport(phases=tuple(phases), source=source)


__all__ = [
    "PREBUILD_PHASE_ORDER",
    "GatedPrebuildReport",
    "PhasePrebuildReport",
    "discover_futures_contracts",
    "discover_option_contracts",
    "run_gated_prebuild",
    "underlying_match_tokens",
]
