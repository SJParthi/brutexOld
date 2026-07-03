"""Segment-aware GATED multi-symbol timeframe pre-build (DEC-PREBUILD-SELECT-001).

The on-demand ``POST /api/v1/universe/derive-timeframes`` surface accepts a
SELECTION of symbols (``?symbols=`` comma list, resolved via the COMMON
:func:`brutex.dashboard.pull_controller.resolve_pull_targets`) and pre-builds
their 19 derived intraday timeframes in GATED PHASES — **SPOT → FUTURES →
OPTIONS** — mirroring the one-click pipeline's gated order
(:mod:`brutex.dashboard.pipeline_pull`):

* every selected symbol of a phase is materialised before advancing;
* the next phase runs ONLY when the current phase FULLY succeeded (every
  attempted symbol materialised without error);
* the FIRST phase with any failure HALTS the run — later phases never start —
  and the structured report names which phase halted + the per-phase
  attempted / succeeded / failed lists.

The SPOT phase materialises each selected UNDERLYING's spot intraday. The
FUTURES phase, per underlying, DISCOVERS its FUT contracts in the F&O candle
lake (:func:`discover_futures_contracts`, bounded — a handful of serial-month
futures) and materialises each contract's intraday OI-aware. The OPTIONS phase
materialises NO intraday — option intraday is TB-scale and the brute-force
engine (NIFTY/BANKNIFTY/SENSEX) never reads it, so it stays ON-DEMAND
(CLAUDE.md §3). OPTIONS is an EXPLICIT, REPORTED no-op so the gated order +
honest scope are visible in the report (it never enumerates, never blocks, never
silently materialises).

Pure orchestration — the spot materialise, the futures discovery, and the
futures materialise are all INJECTED callables, so the whole gated control flow
(phase order, halt-on-failure) is 100% unit-testable with fakes (no lake, no
DuckDB, no FastAPI; CLAUDE.md §4 / §11). The cost is O(underlyings) for spot +
O(underlyings × futures-per-underlying) for the futures discovery/materialise —
NOT an O(1)-total claim (Golden Rule #10).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

from brutex.data.errors import ResampleInputError
from brutex.data.timeframe_materialize import PrebuildKind

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from brutex.dashboard.fno_cascade import FnoCascadeIndex, FnoContract
    from brutex.data.timeframe_materialize import MaterializeResult
    from brutex.models import Instrument

_LOG = structlog.get_logger(__name__)

#: The per-symbol DATA-fault quarantine set for the gated phases
#: (DEC-PREBUILD-SELECT-001; widened by DEC-LAKE-WRITE-UNIQUE-001).
#: ``ValueError`` / ``OSError`` are the materialise core's historical data-error
#: surface; :class:`~brutex.data.errors.ResampleInputError` is the canonical
#: resampler's input tripwire (DEC-RESAMPLER-INPUT-GUARD-001) — raised when ONE
#: symbol's stored 1-minute partition is POISONED (duplicate / out-of-order
#: timestamps). Before the widening that error escaped the per-symbol catch and
#: ABORTED the whole pre-build stage (and 500'd the derive-timeframes route);
#: now the poisoned symbol is quarantined + reported in ``failed`` and every
#: other symbol still runs. DELIBERATELY the narrow subclass, NOT
#: ``DataLakeError`` broadly: :class:`~brutex.data.errors.LakeWriteHalt` (a
#: sibling ``DataLakeError`` — disk-full / OOM, a pull-wide global stop) must
#: STILL propagate, never be miscounted as a per-symbol failure.
_PER_SYMBOL_DATA_ERRORS: tuple[type[Exception], ...] = (ValueError, OSError, ResampleInputError)

#: The instrument-type token the F&O candle lake's decoded ``instrument_type``
#: carries for a future (:func:`brutex.data.fno_symbol.parse_fno_symbol`).
_FUTURES_TYPE: str = "FUT"

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


def discover_futures_contracts(
    instrument: Instrument,
    cascade: FnoCascadeIndex,
) -> tuple[FnoContract, ...]:
    """Discover *instrument*'s FUTURES contracts in the F&O candle lake (DEC-PREBUILD-SELECT-001).

    Matches the selected underlying to the cascade's underlyings via
    :func:`underlying_match_tokens`, then returns every contract across that
    underlying's expiries whose decoded ``instrument_type`` is ``FUT`` — a
    handful of serial-month futures, not an unbounded scan (the cascade is
    pre-grouped, every accessor O(1); the work here is O(expiries × contracts)
    over ONE underlying). Empty tuple when the underlying has no F&O futures
    data — an honest skip, never a fabricated contract. Deterministic order
    (by expiry, then the cascade's futures-first contract order).
    """
    tokens = underlying_match_tokens(instrument)
    matched = next((u for u in cascade.underlyings() if u in tokens), None)
    if matched is None:
        return ()
    out: list[FnoContract] = []
    for expiry in cascade.expiries(matched):
        out.extend(
            c for c in cascade.contracts(matched, expiry) if c.instrument_type == _FUTURES_TYPE
        )
    return tuple(out)


#: The gated phase order — SPOT first, then FUTURES, then the on-demand OPTIONS
#: no-op (DEC-PREBUILD-SELECT-001). A phase advances only when the prior phase
#: had ZERO failures.
PREBUILD_PHASE_ORDER: tuple[PrebuildKind, ...] = (
    PrebuildKind.SPOT,
    PrebuildKind.FUTURES,
    PrebuildKind.OPTIONS,
)


@dataclass(frozen=True, slots=True)
class PhasePrebuildReport:
    """The outcome of ONE gated pre-build phase (DEC-PREBUILD-SELECT-001).

    Attributes
    ----------
    phase
        The phase's :class:`~brutex.data.timeframe_materialize.PrebuildKind`
        value (``"spot"`` / ``"futures"`` / ``"options"``).
    attempted
        The ``symbol_name`` s of every selected instrument in this phase, in
        selection order.
    succeeded
        The ``symbol_name`` s materialised without error (a subset of
        *attempted*, same order). For OPTIONS this is always empty (no-op).
    failed
        ``(symbol_name, error)`` for every instrument whose materialise RAISED,
        in the order they failed. A non-empty ``failed`` halts the run.
    total_bars
        The sum of bars written across this phase's successful symbols. Always
        ``0`` for the OPTIONS no-op phase.
    note
        A short human note (e.g. the OPTIONS on-demand explanation), or ``""``.
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
        """Return a JSON-friendly dict for the route + panel."""
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
    """The structured result of a gated multi-symbol pre-build (DEC-PREBUILD-SELECT-001).

    Attributes
    ----------
    phases
        Each phase that RAN, in execution order. A phase is absent when an
        earlier phase halted the run (the gate).
    halted_after
        The phase name whose failure halted the run (later phases never started),
        or ``None`` when every phase ran to completion.
    source
        The selection source from
        :func:`~brutex.dashboard.pull_controller.resolve_pull_targets`
        (``"selection"`` / ``"single"`` / ``"universe"``).
    """

    phases: tuple[PhasePrebuildReport, ...] = ()
    halted_after: str | None = None
    source: str = ""

    @property
    def ok(self) -> bool:
        """``True`` iff the run completed (never halted) AND every phase passed."""
        return self.halted_after is None and all(p.ok for p in self.phases)

    @property
    def total_bars(self) -> int:
        """Total bars written across every phase."""
        return sum(p.total_bars for p in self.phases)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly dict for the route + panel."""
        return {
            "ok": self.ok,
            "halted_after": self.halted_after,
            "source": self.source,
            "total_bars": self.total_bars,
            "phases": [p.to_dict() for p in self.phases],
        }


def _run_spot_phase(
    underlyings: Sequence[Instrument],
    materialize_spot: Callable[[Instrument], MaterializeResult],
) -> PhasePrebuildReport:
    """SPOT phase: materialise each selected underlying's spot intraday.

    Each underlying is materialised independently — one that RAISES a data error
    (:data:`_PER_SYMBOL_DATA_ERRORS` — ``ValueError`` / ``OSError`` / the
    resampler's :class:`~brutex.data.errors.ResampleInputError` on a poisoned
    stored partition) is recorded in ``failed`` (the rest still run, so ALL
    failures are named); a genuine bug (``KeyError`` / ``TypeError`` / …) or a
    pull-wide :class:`~brutex.data.errors.LakeWriteHalt` PROPAGATES (never
    miscounted as a per-symbol data failure). The GATE is applied by
    :func:`run_gated_prebuild` on a non-empty ``failed``.
    """
    attempted = tuple(inst.symbol_name for inst in underlyings)
    succeeded: list[str] = []
    failed: list[tuple[str, str]] = []
    total_bars = 0
    for inst in underlyings:
        try:
            result = materialize_spot(inst)
        except _PER_SYMBOL_DATA_ERRORS as exc:
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


def _run_futures_phase(
    underlyings: Sequence[Instrument],
    futures_contracts_for: Callable[[Instrument], Sequence[str]],
    materialize_future: Callable[[str], MaterializeResult],
) -> PhasePrebuildReport:
    """FUTURES phase: discover + materialise each underlying's FUT contracts (OI-aware).

    For each selected underlying, *futures_contracts_for* DISCOVERS its futures
    contract ``groww_symbol`` s in the F&O candle lake (bounded — a handful of
    serial-month futures per underlying), and *materialize_future* materialises
    each contract's intraday via the OI-aware resampler. The phase's
    ``attempted`` / ``succeeded`` / ``failed`` are keyed by CONTRACT symbol (not
    underlying), so a partial-failure names the exact contract. An underlying
    with NO discovered futures contributes nothing — an honest skip, never a
    fabricated bar. A data error (:data:`_PER_SYMBOL_DATA_ERRORS`) in discovery
    OR materialise is recorded as a failure (the rest still run); a genuine bug
    (and a pull-wide :class:`~brutex.data.errors.LakeWriteHalt`) propagates.
    """
    attempted: list[str] = []
    succeeded: list[str] = []
    failed: list[tuple[str, str]] = []
    total_bars = 0
    for inst in underlyings:
        try:
            contracts = futures_contracts_for(inst)
        except _PER_SYMBOL_DATA_ERRORS as exc:
            _LOG.warning(
                "universe.prebuild.futures_discovery_failed",
                underlying=inst.symbol_name,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            failed.append((inst.symbol_name, f"{type(exc).__name__}: {exc}"))
            continue
        for contract in contracts:
            attempted.append(contract)
            try:
                result = materialize_future(contract)
            except _PER_SYMBOL_DATA_ERRORS as exc:
                _LOG.warning(
                    "universe.prebuild.futures_failed",
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
        phase=PrebuildKind.FUTURES.value,
        attempted=tuple(attempted),
        succeeded=tuple(succeeded),
        failed=tuple(failed),
        total_bars=total_bars,
        note=(
            "no futures contracts found in the F&O candle lake for the selection"
            if not attempted and not failed
            else ""
        ),
    )


def _run_options_phase(underlyings: Sequence[Instrument]) -> PhasePrebuildReport:
    """OPTIONS phase: an EXPLICIT, REPORTED no-op (intraday stays on-demand).

    Option intraday is TB-scale and the brute-force engine never reads it
    (CLAUDE.md §3), so the OPTIONS phase neither ENUMERATES nor materialises any
    contract — it records the attempted underlyings, succeeds with zero work, and
    carries the on-demand note so the gated order + honest scope are visible.
    """
    return PhasePrebuildReport(
        phase=PrebuildKind.OPTIONS.value,
        attempted=tuple(inst.symbol_name for inst in underlyings),
        succeeded=(),
        failed=(),
        total_bars=0,
        note=(
            "options intraday: on-demand (not materialised) — TB-scale, "
            "the brute-force engine never reads option intraday"
        ),
    )


def run_gated_prebuild(
    *,
    underlyings: Sequence[Instrument],
    materialize_spot: Callable[[Instrument], MaterializeResult],
    futures_contracts_for: Callable[[Instrument], Sequence[str]],
    materialize_future: Callable[[str], MaterializeResult],
    source: str = "",
) -> GatedPrebuildReport:
    """Run the segment-aware GATED pre-build over the selected *underlyings* (DEC-PREBUILD-SELECT-001).

    Runs the phases in :data:`PREBUILD_PHASE_ORDER` — **SPOT → FUTURES →
    OPTIONS** — advancing to the next phase ONLY when the current phase had ZERO
    failures. The FIRST phase with any failure HALTS the run (``halted_after``
    set, later phases absent from ``phases``):

    * **SPOT** — materialise each selected underlying's spot intraday (the
      OI-dropping spot resampler), via *materialize_spot*.
    * **FUTURES** — for each underlying, *futures_contracts_for* DISCOVERS its
      FUT contracts in the F&O candle lake and *materialize_future* materialises
      each contract's intraday OI-aware. Keyed by contract symbol.
    * **OPTIONS** — an explicit reported no-op (on-demand only; never enumerated).

    Pure orchestration — all I/O (spot materialise, futures discovery, futures
    materialise) is injected, so the whole gated control flow is offline-testable
    (CLAUDE.md §4 / §11). O(underlyings) for spot + O(underlyings × futures-per)
    for futures discovery/materialise; NOT an O(1)-total claim (Golden Rule #10).

    Parameters
    ----------
    underlyings
        The resolved underlying instruments to pre-build (from
        ``resolve_pull_targets`` — the lean store yields spot underlyings).
    materialize_spot
        ``instrument -> MaterializeResult`` — pre-builds ONE underlying's SPOT
        intraday; raises on a per-symbol data error.
    futures_contracts_for
        ``instrument -> Sequence[contract_symbol]`` — the underlying's futures
        contract ``groww_symbol`` s discovered in the F&O candle lake (bounded).
    materialize_future
        ``contract_symbol -> MaterializeResult`` — pre-builds ONE futures
        contract's OI-aware intraday; raises on a per-contract data error.
    source
        The selection source label for the report (``"selection"`` / ``"single"``
        / ``"universe"``).

    Returns
    -------
    GatedPrebuildReport
        The per-phase reports + which phase (if any) halted the run.
    """
    phases: list[PhasePrebuildReport] = []
    halted_after: str | None = None
    runners: tuple[tuple[PrebuildKind, Callable[[], PhasePrebuildReport]], ...] = (
        (PrebuildKind.SPOT, lambda: _run_spot_phase(underlyings, materialize_spot)),
        (
            PrebuildKind.FUTURES,
            lambda: _run_futures_phase(underlyings, futures_contracts_for, materialize_future),
        ),
        (PrebuildKind.OPTIONS, lambda: _run_options_phase(underlyings)),
    )
    for kind, run_phase in runners:
        report = run_phase()
        phases.append(report)
        if not report.ok:
            halted_after = kind.value
            _LOG.warning(
                "universe.prebuild.phase_halted",
                phase=kind.value,
                attempted=list(report.attempted),
                failed=[s for s, _ in report.failed],
            )
            break
    _LOG.info(
        "universe.prebuild.gated_done",
        source=source,
        halted_after=halted_after,
        phases=[p.phase for p in phases],
        total_bars=sum(p.total_bars for p in phases),
    )
    return GatedPrebuildReport(
        phases=tuple(phases),
        halted_after=halted_after,
        source=source,
    )


__all__ = [
    "PREBUILD_PHASE_ORDER",
    "GatedPrebuildReport",
    "PhasePrebuildReport",
    "discover_futures_contracts",
    "run_gated_prebuild",
    "underlying_match_tokens",
]
