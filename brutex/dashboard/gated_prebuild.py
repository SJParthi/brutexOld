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

The OPTIONS phase materialises NO intraday — option intraday is TB-scale and
the brute-force engine (NIFTY/BANKNIFTY/SENSEX) never reads it, so it stays
ON-DEMAND (CLAUDE.md §3). It is an EXPLICIT, REPORTED no-op so the gated order +
honest scope are visible in the report (the OPTIONS phase always "succeeds" with
zero work, after FUTURES — it never blocks, and never silently materialises).

Pure orchestration — the per-symbol materialise is an INJECTED callable, so the
whole gated control flow (group-by-kind, phase order, halt-on-failure) is 100%
unit-testable with fakes (no lake, no DuckDB, no FastAPI; CLAUDE.md §4 / §11).
The per-symbol cost is whatever ``materialize_one`` pays (one 1m read + resample,
O(bars)); the grouping is O(targets) with O(1) per-symbol classification — NOT an
O(1)-total claim (Golden Rule #10).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

from brutex.data.timeframe_materialize import PrebuildKind, prebuild_kind_for

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from brutex.data.timeframe_materialize import MaterializeResult
    from brutex.models import Instrument

_LOG = structlog.get_logger(__name__)

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


def _group_by_kind(
    targets: Sequence[Instrument],
) -> dict[PrebuildKind, list[Instrument]]:
    """Group selected instruments by their :class:`PrebuildKind` (O(targets))."""
    grouped: dict[PrebuildKind, list[Instrument]] = {kind: [] for kind in PREBUILD_PHASE_ORDER}
    for inst in targets:
        grouped[prebuild_kind_for(inst)].append(inst)
    return grouped


def _run_options_phase(targets: Sequence[Instrument]) -> PhasePrebuildReport:
    """The OPTIONS phase: an EXPLICIT, REPORTED no-op (intraday stays on-demand).

    Option intraday is TB-scale and the brute-force engine never reads it
    (CLAUDE.md §3), so the OPTIONS phase materialises NOTHING — it records the
    attempted symbols, succeeds with zero work, and carries the on-demand note
    so the gated order + honest scope are visible.
    """
    attempted = tuple(inst.symbol_name for inst in targets)
    return PhasePrebuildReport(
        phase=PrebuildKind.OPTIONS.value,
        attempted=attempted,
        succeeded=(),
        failed=(),
        total_bars=0,
        note=(
            "options intraday: on-demand (not materialised) — TB-scale, "
            "the brute-force engine never reads option intraday"
        ),
    )


def _run_materialize_phase(
    kind: PrebuildKind,
    targets: Sequence[Instrument],
    materialize_one: Callable[[Instrument], MaterializeResult],
) -> PhasePrebuildReport:
    """Materialise every selected instrument of one (SPOT / FUTURES) phase.

    Each symbol is materialised independently — a symbol that RAISES is recorded
    in ``failed`` (with its specific exception message) and the rest of the phase
    still runs, so the report names ALL failures, not just the first; the GATE
    (halt before the next phase) is applied by :func:`run_gated_prebuild` on a
    non-empty ``failed``.
    """
    attempted = tuple(inst.symbol_name for inst in targets)
    succeeded: list[str] = []
    failed: list[tuple[str, str]] = []
    total_bars = 0
    for inst in targets:
        try:
            result = materialize_one(inst)
        except (ValueError, OSError) as exc:
            # ValueError: no stored 1m bars / bad partition; OSError: lake read/
            # write fault. A genuine bug (KeyError/TypeError/…) is NOT swallowed —
            # it propagates so it is never silently counted as a data failure.
            _LOG.warning(
                "universe.prebuild.symbol_failed",
                phase=kind.value,
                symbol=inst.symbol_name,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            failed.append((inst.symbol_name, f"{type(exc).__name__}: {exc}"))
            continue
        succeeded.append(inst.symbol_name)
        total_bars += result.total_bars
    return PhasePrebuildReport(
        phase=kind.value,
        attempted=attempted,
        succeeded=tuple(succeeded),
        failed=tuple(failed),
        total_bars=total_bars,
    )


def run_gated_prebuild(
    *,
    targets: Sequence[Instrument],
    materialize_one: Callable[[Instrument], MaterializeResult],
    source: str = "",
) -> GatedPrebuildReport:
    """Run the segment-aware GATED pre-build over *targets* (DEC-PREBUILD-SELECT-001).

    Groups the selected instruments by :class:`PrebuildKind`, then runs the
    phases in :data:`PREBUILD_PHASE_ORDER` — SPOT → FUTURES → OPTIONS — advancing
    to the next phase ONLY when the current phase had ZERO failures. The FIRST
    phase with any failure HALTS the run (``halted_after`` set, later phases
    absent from ``phases``). SPOT and FUTURES materialise via *materialize_one*;
    OPTIONS is an explicit reported no-op (on-demand).

    Pure orchestration — *materialize_one* is injected so the whole gated control
    flow is offline-testable (CLAUDE.md §4 / §11). O(targets) grouping +
    O(bars) per materialised symbol; NOT an O(1)-total claim.

    Parameters
    ----------
    targets
        The resolved instruments to pre-build (from ``resolve_pull_targets``).
    materialize_one
        ``instrument -> MaterializeResult`` callable that pre-builds ONE
        instrument's intraday timeframes (segment-aware resampler chosen by the
        caller via :func:`brutex.data.timeframe_materialize.intraday_resampler_for`);
        raises on a per-symbol failure.
    source
        The selection source label for the report (``"selection"`` / ``"single"``
        / ``"universe"``).

    Returns
    -------
    GatedPrebuildReport
        The per-phase reports + which phase (if any) halted the run.
    """
    grouped = _group_by_kind(targets)
    phases: list[PhasePrebuildReport] = []
    halted_after: str | None = None
    for kind in PREBUILD_PHASE_ORDER:
        phase_targets = grouped[kind]
        if kind is PrebuildKind.OPTIONS:
            report = _run_options_phase(phase_targets)
        else:
            report = _run_materialize_phase(kind, phase_targets, materialize_one)
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
    "run_gated_prebuild",
]
