"""The τ (confidence threshold) sweep over a FIXED grid
(DEC-META-LABEL-ANNOTATE-001).

For each τ in the fixed 0.50 → 0.95 grid (step 0.05, 10 thresholds) the
sweep records how the trade stream would shrink under a ``p_win >= τ`` keep
rule and the precision (win rate) AMONG the kept trades. ANNOTATION-ONLY:
the curve lands in ``.meta.json`` — nothing is filtered anywhere.

Anti-double-dipping disclosure (recorded in the DEC + every sidecar): the
sweep itself is a 10-way selection over the SAME trades — a future DEC that
GATES on a swept τ must fold :data:`N_THRESHOLDS_SWEPT` into its DSR trial
count (the deflated-Sharpe N), exactly like any other searched axis. The
curve's ``n_thresholds_swept`` field exists so that fold is mechanical.

Honesty floor (Golden Rule #1): a τ that keeps ZERO trades records
``precision: null`` — NEVER a fabricated 0.0 (a zero would read "certain
loser" where the truth is "no evidence").
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from collections.abc import Sequence

__all__ = [
    "N_THRESHOLDS_SWEPT",
    "TAU_GRID",
    "TauPoint",
    "tau_curve",
]

#: The FIXED τ grid: 0.50 → 0.95 inclusive, step 0.05 (exact 2-decimal
#: floats via round — the grid is a wire vocabulary, pinned by tests).
TAU_GRID: Final[tuple[float, ...]] = tuple(round(0.50 + 0.05 * i, 2) for i in range(10))

#: The sweep's trial count — the DSR anti-double-dipping fold input.
N_THRESHOLDS_SWEPT: Final[int] = len(TAU_GRID)


@dataclass(frozen=True, slots=True)
class TauPoint:
    """One grid point of the τ curve.

    ``n_kept`` counts scored trades with ``p_win >= tau`` (the EXACT ``>=``
    boundary — the abstention gate's pinned law); ``kept_pct`` is
    ``n_kept / n_scored`` (``None`` when NO trade carried an out-of-fold
    p_win); ``precision`` is the win rate among kept trades (``None`` when
    ``n_kept == 0`` — never a 0-fabrication). Unscored trades (p_win
    ``None`` — no out-of-fold coverage) are EXCLUDED from both denominators
    and counted once at the curve level by the caller.

    UNITS (refuter round-3 I3-3): ``kept_pct`` and ``precision`` are RAW
    0–1 FRACTIONS, never 0–100 percents — the house ``cpcv_pass_pct``
    artifact convention (raw fraction in artifacts, ×100 only at display);
    the ``_pct`` name is deliberately kept for that convention's sake.
    """

    tau: float
    n_kept: int
    kept_pct: float | None
    precision: float | None


def tau_curve(p_win: Sequence[float | None], wins: Sequence[bool]) -> tuple[TauPoint, ...]:
    """Sweep the fixed τ grid over one combo's out-of-fold P(win) column.

    Parameters
    ----------
    p_win
        Per-trade out-of-fold win probability, positionally aligned with
        *wins*; ``None`` = no out-of-fold coverage (excluded from every
        ratio, never imputed).
    wins
        Per-trade realised outcome (``pnl_paisa > 0``).

    Returns
    -------
    tuple[TauPoint, ...]
        One point per :data:`TAU_GRID` threshold, grid order.

    Raises
    ------
    ValueError
        On a length mismatch (positional alignment is the contract).
    """
    if len(p_win) != len(wins):
        raise ValueError(f"tau_curve: p_win length {len(p_win)} != wins length {len(wins)}")
    scored = [(p, win) for p, win in zip(p_win, wins, strict=True) if p is not None]
    n_scored = len(scored)
    points: list[TauPoint] = []
    for tau in TAU_GRID:
        kept = [win for p, win in scored if p >= tau]
        n_kept = len(kept)
        points.append(
            TauPoint(
                tau=tau,
                n_kept=n_kept,
                kept_pct=None if n_scored == 0 else n_kept / n_scored,
                precision=None if n_kept == 0 else sum(kept) / n_kept,
            )
        )
    return tuple(points)
