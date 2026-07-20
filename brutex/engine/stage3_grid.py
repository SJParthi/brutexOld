"""Stage-3 normalization/quantization — the [0,1] ↔ int-paisa Mode-A grid bijection.

DEC-STAGE3-GRID-001 (ADR-0007 / DEC-STAGE3-DESIGN-LOCK-001 §5 slice 3, condition
3), math re-hosted by DEC-RUST-STAGE3-REFINE-001. The Stage-3 CMA-ES inner
search proposes continuous (SL, TP) points in [0,1]² which must be
"denormalized onto the FIXED Mode-A grid bounds (SL ∈ [5,200] paisa, TP ∈
[10,500] paisa — constants, never data-derived) and snapped to the 5/10-paisa
ticks BEFORE every evaluation and BEFORE hashing" (`TRACK1_BACKTEST_SPEC.md`
§Stage 3; the grid itself is the spec's Mode-A definition at
`TRACK1_BACKTEST_SPEC.md:243-245` — SL 40 steps × TP 50 steps = 2,000 cells per
mask). This module remains the ONE hard-coded axis source (CLAUDE.md §11
COMMON), but the BIJECTION MATH — the clamp + round-half-even snap and the
exact inverse coordinate — lives in the ``brutex_refine`` Rust wheel
(``rust/brutex-refine/src/grid.rs``); :class:`GridAxis` is a thin wrapper:

* Construction VALIDATION stays pure Python (`__post_init__` — plain bound
  checks), so importing this module (and the locked Mode-A axis constants)
  NEVER touches the wheel: the dashboard boots wheel-free, and a
  wheel-absent/flag-off environment fails LOUDLY at the first denormalize /
  normalize CALL instead (the typed
  :class:`~brutex.engine.refine_rust.RefineRustUnavailableError` — no
  fallback exists, the Python math is deleted).
* ``denormalize`` / ``normalize`` delegate to the wheel and re-raise its
  verdicts as :class:`Stage3GridError` with BYTE-IDENTICAL message text (the
  crate formats the same strings; pinned by the unchanged unit suite).
* **Floats never reach the 9-field combo hash (condition 3):** ``denormalize``
  returns ``int`` unconditionally — the refined candidate's identity is the
  unchanged :func:`brutex.strategies.combo.combo_hash` over quantized int
  params, so dedup against the grid survivors stays exact. (Inside a
  refinement run the snap executes wholly in Rust —
  ``RefineSession.ask`` returns quantized ints; these wrappers serve the
  x0 seeding, the orchestrator's fail-fast axis validation, and the tests.)

**Honest envelope (Golden Rule #10):** every function here is O(1) per call —
a constant handful of int/float ops plus one FFI hop, no I/O — and NOTHING
MORE is claimed: this is not one of the Rule-#5 CI-benchmarked hot paths and
no throughput figure is stated. Snapping uses only IEEE-754 binary64
multiply/compare/round-ties-even (no transcendental libm calls), so identical
inputs give identical paisa on any platform — unlike the CMA-ES
eigendecomposition feeding it, for which ADR-0007 condition 5 claims
reproducibility per commit+wheel+platform ONLY.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from brutex.engine.refine_rust import require_refine_rust
from brutex.models import BruteXError


class Stage3GridError(BruteXError):
    """Raised for an invalid axis definition, a NaN coordinate, or off-grid paisa.

    Covers a non-positive bound or tick, a span that is not a whole number of
    ticks, ``denormalize(NaN)`` (NaN admits no nearest grid point), and a
    ``normalize`` input that is below/above the axis bounds or off the tick
    grid (normalize is defined on the grid ONLY — silent snapping there would
    let a never-evaluated (SL, TP) masquerade as a grid point).
    """


@dataclass(frozen=True, slots=True)
class GridAxis:
    """One FIXED grid axis — the int-paisa points ``lo, lo+tick, …, hi``.

    The axis is closed under both directions of the bijection: every
    ``denormalize`` result is one of its :attr:`steps` grid points, and
    ``normalize`` accepts exactly those points. Bounds are CONSTANTS by
    construction (ADR-0007 condition 3 — "never data-derived"); validation
    rejects any axis that could break the bijection (``hi <= lo`` would leave
    ``steps < 2`` and a zero denominator in ``normalize``). The math methods
    delegate to the ``brutex_refine`` wheel (DEC-RUST-STAGE3-REFINE-001);
    the crate re-validates the axis per call with the SAME rules (a
    defensive twin — unreachable from a constructed instance).

    Parameters
    ----------
    name
        Axis label used in error messages (``"sl_paisa"`` / ``"tp_paisa"``).
    lo_paisa, hi_paisa
        Inclusive int-paisa bounds, ``0 < lo_paisa < hi_paisa``.
    tick_paisa
        Grid spacing in paisa (``> 0``); ``hi_paisa - lo_paisa`` must be a
        whole number of ticks.

    Raises
    ------
    Stage3GridError
        If any bound/tick constraint above is violated.
    """

    name: str
    lo_paisa: int
    hi_paisa: int
    tick_paisa: int

    def __post_init__(self) -> None:
        """Validate the axis invariants at construction time (pure Python —
        the wheel-free import path; message text mirrored by the crate)."""
        if self.lo_paisa <= 0:
            raise Stage3GridError(f"{self.name}: lo_paisa must be > 0, got {self.lo_paisa}")
        if self.tick_paisa <= 0:
            raise Stage3GridError(f"{self.name}: tick_paisa must be > 0, got {self.tick_paisa}")
        if self.hi_paisa <= self.lo_paisa:
            raise Stage3GridError(
                f"{self.name}: hi_paisa must be > lo_paisa, got [{self.lo_paisa}, {self.hi_paisa}]"
            )
        if (self.hi_paisa - self.lo_paisa) % self.tick_paisa != 0:
            raise Stage3GridError(
                f"{self.name}: span {self.hi_paisa - self.lo_paisa} is not a whole "
                f"number of {self.tick_paisa}-paisa ticks"
            )

    @property
    def steps(self) -> int:
        """Number of on-grid points (the spec's "steps"), always ``>= 2`` — O(1)."""
        return (self.hi_paisa - self.lo_paisa) // self.tick_paisa + 1

    def denormalize(self, u: float) -> int:
        """Map *u* to the nearest on-grid int paisa — clamp, then snap. O(1)/call.

        Total over every non-NaN float: *u* is first clamped into ``[0, 1]``
        (CMA-ES proposals may stray outside the box; ±inf clamp to the edges),
        then snapped to the nearest grid index via round-half-even on
        ``u * (steps - 1)`` — exact halves round to the even index,
        deterministically (the crate's ``round_ties_even`` is CPython
        ``round()``'s exact law). ``u=0.0`` → ``lo_paisa``; ``u=1.0`` →
        ``hi_paisa``.

        Parameters
        ----------
        u
            The normalized coordinate (any non-NaN float).

        Returns
        -------
        int
            An on-grid paisa value in ``[lo_paisa, hi_paisa]`` — always
            ``int``, so floats can never reach the 9-field combo hash
            (ADR-0007 condition 3).

        Raises
        ------
        Stage3GridError
            If *u* is NaN (no nearest grid point exists).
        brutex.engine.refine_rust.RefineRustUnavailableError
            When the Rust runtime cannot be engaged (loud, no fallback).
        """
        runtime = require_refine_rust()
        try:
            return runtime.grid_denormalize(
                self.name, self.lo_paisa, self.hi_paisa, self.tick_paisa, u
            )
        except ValueError as exc:
            raise Stage3GridError(str(exc)) from exc

    def normalize(self, paisa: int) -> float:
        """Map an ON-GRID int paisa back to its exact ``[0, 1]`` coordinate. O(1)/call.

        The exact inverse of :meth:`denormalize` on the grid:
        ``denormalize(normalize(p)) == p`` for every grid point *p* (the
        round-trip is exhaustively pinned by the test suite). Off-grid input
        raises — silently snapping here would let a never-evaluated (SL, TP)
        pair masquerade as a measured grid point.

        Parameters
        ----------
        paisa
            An on-grid paisa value: within ``[lo_paisa, hi_paisa]`` and on the
            tick grid.

        Returns
        -------
        float
            ``index / (steps - 1)`` in ``[0, 1]``; ``lo_paisa`` → ``0.0``,
            ``hi_paisa`` → ``1.0``.

        Raises
        ------
        Stage3GridError
            If *paisa* is outside the bounds or not on the tick grid.
        brutex.engine.refine_rust.RefineRustUnavailableError
            When the Rust runtime cannot be engaged (loud, no fallback).
        """
        runtime = require_refine_rust()
        try:
            return runtime.grid_normalize(
                self.name, self.lo_paisa, self.hi_paisa, self.tick_paisa, paisa
            )
        except ValueError as exc:
            raise Stage3GridError(str(exc)) from exc


#: The locked Mode-A stop-loss axis — SL ∈ [5, 200] paisa on the 5-paisa tick,
#: 40 steps (`TRACK1_BACKTEST_SPEC.md:243-245`; constants, never data-derived).
MODE_A_SL_AXIS: Final[GridAxis] = GridAxis(name="sl_paisa", lo_paisa=5, hi_paisa=200, tick_paisa=5)

#: The locked Mode-A take-profit axis — TP ∈ [10, 500] paisa on the 10-paisa
#: tick, 50 steps (`TRACK1_BACKTEST_SPEC.md:243-245`).
MODE_A_TP_AXIS: Final[GridAxis] = GridAxis(
    name="tp_paisa", lo_paisa=10, hi_paisa=500, tick_paisa=10
)

#: The Mode-A cell count per mask — 40 × 50 = 2,000 (`TRACK1_BACKTEST_SPEC.md:245`).
MODE_A_GRID_CELLS: Final[int] = MODE_A_SL_AXIS.steps * MODE_A_TP_AXIS.steps


def denormalize_sl_tp(u_sl: float, u_tp: float) -> tuple[int, int]:
    """Map a CMA-ES [0,1]² proposal to the on-grid int ``(sl_paisa, tp_paisa)``.

    The per-evaluation quantization step (ADR-0007 condition 3): the returned
    ints are what the engine evaluates AND what enters the 9-field combo hash
    — floats never reach either. O(1) per call.

    Parameters
    ----------
    u_sl, u_tp
        The proposal's normalized SL / TP coordinates (any non-NaN floats;
        clamped into ``[0, 1]``).

    Returns
    -------
    tuple[int, int]
        ``(sl_paisa, tp_paisa)`` — each on its Mode-A axis grid.

    Raises
    ------
    Stage3GridError
        If either coordinate is NaN.
    """
    return MODE_A_SL_AXIS.denormalize(u_sl), MODE_A_TP_AXIS.denormalize(u_tp)


def normalize_sl_tp(sl_paisa: int, tp_paisa: int) -> tuple[float, float]:
    """Map on-grid int ``(sl_paisa, tp_paisa)`` to its exact [0,1]² coordinate.

    The exact inverse of :func:`denormalize_sl_tp` on the Mode-A grid — used
    to seed the optimizer at a survivor's incumbent (SL, TP). O(1) per call.

    Parameters
    ----------
    sl_paisa, tp_paisa
        On-grid Mode-A paisa values.

    Returns
    -------
    tuple[float, float]
        ``(u_sl, u_tp)``, each in ``[0, 1]``.

    Raises
    ------
    Stage3GridError
        If either value is outside its axis bounds or off its tick grid.
    """
    return MODE_A_SL_AXIS.normalize(sl_paisa), MODE_A_TP_AXIS.normalize(tp_paisa)


__all__ = [
    "MODE_A_GRID_CELLS",
    "MODE_A_SL_AXIS",
    "MODE_A_TP_AXIS",
    "GridAxis",
    "Stage3GridError",
    "denormalize_sl_tp",
    "normalize_sl_tp",
]
