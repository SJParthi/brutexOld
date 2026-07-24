"""SLA-honesty launch guard for the full grid sweep (DEC-SLA-LAUNCH-GUARD-001).

Before ``brutex sweep-all`` launches a grid, this module PROJECTS the sweep's
wall-clock as ``tuple_count × per-tuple seconds`` and compares it against the
CLAUDE.md §8 hardware-envelope SLA (**2-4 days** wall time on the M4 Pro
reference envelope). A projection past the envelope REFUSES the launch unless
the operator passes ``--acknowledge-sla-overrun`` — then the launch proceeds
with a LOUD warning. The decision (allowed / refused / acknowledged) is always
logged as one structured ``sweep.sla_projection`` event.

## Honest envelope (Golden Rule #10)

* :data:`SWEEP_PER_TUPLE_SECONDS` is an **UNBENCHMARKED, conservative
  placeholder ESTIMATE** — it has NOT been measured end-to-end on the §8
  reference envelope (Apple M4 Pro, 14-core, 48 GB) and MUST be re-measured
  there before the projection can be treated as calibrated. Until then the
  guard's verdict is a coarse plausibility screen, NOT a certification: the
  §8 "~33B trials in 2-4 days" SLA itself remains UNMEASURED, and this guard
  never claims it met. The operator can override the estimate per environment
  via :data:`ENV_PER_TUPLE_SECONDS` (``BRUTEX_SWEEP_PER_TUPLE_SECONDS``).
* The projection unit is one grid TUPLE (one
  ``instrument × timeframe × direction × K × SL × TP × fib-anchor`` cell —
  ``count_sweep_tuples``), deliberately NOT a §8 "trial" (one mask
  evaluation — a tuple evaluates its whole mask population, so the two units
  differ by orders of magnitude; the knob names say TUPLE so the grain can
  never be conflated). A tuple's true cost varies with its bar count and
  mask population, which a single constant cannot capture.
* **k-aware weighting (DEC-SWEEP-SCALE-GUARDS-001).** Flat linear-in-tuples is
  BLIND to the dominant real cost driver: a tuple's mask population is
  ``C(n_bits, k)``, which spans **~91,000×** between k=1 (60 masks) and k=5
  (5.46M masks) at the 60-condition vocabulary, so a k=5-heavy grid costs orders
  more per tuple than a k=1 grid of the same tuple count. When the caller
  supplies per-``(instrument, tf, k)`` cost cells (:class:`TupleCostCell`), the
  projection is instead ``per_tuple_seconds × Σ weight / reference_weight``,
  where ``weight = C(n_bits, k) × N_bars`` and ``reference_weight`` is the mean
  weight of the SMALLEST-k tuples — so the EXISTING 30 s calibration maps to the
  current average small-k tuple and a k=1-only grid projects the same order as
  the flat model, while a k=5-heavy grid projects orders higher. This weighted
  model is STILL UNBENCHMARKED (the ``C(n_bits, k) × N_bars`` proxy is a
  first-order work estimate, not a measured per-tuple time). Without cells the
  guard falls back to the flat linear-in-tuples model, byte-for-byte the prior
  behavior.
* The projection is **SERIAL** — it does not model the parallel sweep
  driver's multi-worker speedup (``BRUTEX_SWEEP_WORKERS``), so an over-budget
  verdict on a parallel launch errs in the SAFE direction (over-refusal, never
  under-warning); the refusal message names the worker count as a lever.
* The guard costs O(cells) — one weight per ``(instrument, tf, k)`` group (never
  per tuple: the direction/SL/TP/proximity/fib axes fold into a multiplicity),
  or O(1) in the flat fallback; it never touches the lake, the grid, or the
  sweep hot path.
"""

from __future__ import annotations

import math
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

import structlog

# DEC-RUST-SWEEP-OPS-001 (slice 1b): the default-ON Rust routing facade. The
# projection decision gates on `sweep_ops_rust_enabled()` FIRST so the
# flag-OFF default path is byte-identical; `_sla_decision_python` below is
# the live oracle. Env parses / validation raises / message rendering / the
# structlog event stay Python.
from brutex.engine.sweep_ops_rust import routed_sla_decision, sweep_ops_rust_enabled

logger = structlog.get_logger("brutex.engine.sweep_sla_guard")

#: The CLAUDE.md §8 production-sweep SLA envelope UPPER bound: "2-4 days wall
#: time" on the M4 Pro reference envelope — the guard projects against the
#: 4-day ceiling (a projection inside 4 days is within the stated envelope).
SLA_ENVELOPE_DAYS: Final[float] = 4.0

#: :data:`SLA_ENVELOPE_DAYS` in seconds — the comparison unit of the guard.
SLA_ENVELOPE_SECONDS: Final[float] = SLA_ENVELOPE_DAYS * 86_400.0

#: UNBENCHMARKED per-tuple wall-clock ESTIMATE (seconds) used to project a
#: grid's total wall-clock (Golden Rule #10 honesty): this value has NOT been
#: measured end-to-end on the §8 reference envelope and MUST be re-measured
#: there (a production-scale tuple evaluates its full mask population over the
#: swept window — its cost depends on bar count + mask count, not a constant).
#: 30 s/tuple is a deliberately conservative first-order placeholder so the
#: guard refuses implausibly large grids rather than blessing them. Override
#: per environment via :data:`ENV_PER_TUPLE_SECONDS`.
SWEEP_PER_TUPLE_SECONDS: Final[float] = 30.0

#: Environment variable overriding :data:`SWEEP_PER_TUPLE_SECONDS` — the
#: operator's calibration knob once a real per-tuple measurement exists for
#: their hardware. Must parse as a FINITE float ``> 0``.
ENV_PER_TUPLE_SECONDS: Final[str] = "BRUTEX_SWEEP_PER_TUPLE_SECONDS"

#: How many of the heaviest ``(instrument, tf, k)`` cost groups the weighted
#: refusal message NAMES (DEC-SWEEP-SCALE-GUARDS-001) so the operator sees WHICH
#: tuples dominate the projection (almost always the high-k groups).
DOMINANT_TUPLE_SAMPLE: Final[int] = 3


class SlaDecision(StrEnum):
    """The launch guard's verdict for one projected grid."""

    #: The projection fits the §8 envelope — launch proceeds silently.
    ALLOWED = "allowed"
    #: The projection exceeds the envelope and no acknowledgement was given —
    #: the launch is REFUSED.
    REFUSED = "refused"
    #: The projection exceeds the envelope but the operator passed
    #: ``--acknowledge-sla-overrun`` — the launch proceeds with a LOUD warning.
    ACKNOWLEDGED = "acknowledged"


def _require_positive_finite(value: float, *, name: str, raw: object) -> None:
    """Raise :class:`ValueError` unless *value* is a FINITE float ``> 0``.

    The ONE validation both the env override and the explicit parameter ride
    (§11 COMMON). ``nan`` fails every comparison, so a bare ``<= 0`` check
    would ACCEPT it and poison the projection into ``nan`` days (hostile-review
    MINOR-1); ``inf`` would fabricate an always-refuse. Both must be a clean
    user-config error instead.
    """
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive, finite number of seconds; got {raw!r}")


def resolve_per_tuple_seconds(env: Mapping[str, str] | None = None) -> float:
    """Return the per-tuple seconds estimate — env override, else the module constant.

    Parameters
    ----------
    env
        The environment mapping to consult (default ``os.environ``); injected
        so tests never mutate the process environment.

    Raises
    ------
    ValueError
        If :data:`ENV_PER_TUPLE_SECONDS` is set but is not a FINITE float
        ``> 0`` (``nan``/``inf`` included) — a mistyped calibration knob must
        be a clean user-config error, never a silently-wrong projection.
    """
    source = os.environ if env is None else env
    raw = source.get(ENV_PER_TUPLE_SECONDS)
    if raw is None:
        return SWEEP_PER_TUPLE_SECONDS
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(
            f"{ENV_PER_TUPLE_SECONDS} must be a positive, finite number of seconds; got {raw!r}"
        ) from exc
    _require_positive_finite(value, name=ENV_PER_TUPLE_SECONDS, raw=raw)
    return value


@dataclass(frozen=True, slots=True)
class TupleCostCell:
    """One ``(instrument, tf, k)`` cost group of the grid — the weighted unit.

    The weighted projection (DEC-SWEEP-SCALE-GUARDS-001) does NOT enumerate every
    tuple: the per-tuple cost depends only on ``(instrument, tf, k)`` (the mask
    population is ``C(n_bits, k)``, the bar count is ``N_bars(instrument, tf)``),
    so the direction / SL / TP / proximity / fib axes fold into a *multiplicity*.
    One cell therefore stands for ``multiplicity`` real tuples of identical cost.

    Attributes
    ----------
    label
        A human-readable sample tuple label, e.g. ``"NIFTY 5minute k=5"`` — used
        VERBATIM in the refusal message's "dominant tuples" clause.
    k
        The tuple's K (mask cardinality); ``0 <= k <= n_bits``.
    n_bits
        The condition-vocabulary size (``>= k``); ``C(n_bits, k)`` is the mask
        population per tuple.
    n_bars
        The loaded bar count for this ``(instrument, tf)`` slice (``>= 1``).
    multiplicity
        How many actual grid tuples share this cost (the folded
        direction/SL/TP/proximity/fib fan-out; ``>= 1``).
    """

    label: str
    k: int
    n_bits: int
    n_bars: int
    multiplicity: int

    def __post_init__(self) -> None:
        """Fail loud on a malformed cell — a silently-wrong weight poisons the ETA."""
        if self.k < 0 or self.n_bits < self.k:
            raise ValueError(
                f"TupleCostCell({self.label!r}): need 0 <= k <= n_bits; "
                f"got k={self.k!r}, n_bits={self.n_bits!r}"
            )
        if self.n_bars < 1:
            raise ValueError(
                f"TupleCostCell({self.label!r}): n_bars must be >= 1 (a loaded "
                f"slice); got {self.n_bars!r}"
            )
        if self.multiplicity < 1:
            raise ValueError(
                f"TupleCostCell({self.label!r}): multiplicity must be >= 1; "
                f"got {self.multiplicity!r}"
            )

    def weight(self) -> int:
        """The per-tuple cost weight ``C(n_bits, k) × N_bars`` (the work proxy)."""
        return math.comb(self.n_bits, self.k) * self.n_bars


@dataclass(frozen=True, slots=True)
class WeightedGridCost:
    """The k-aware projection over a grid's :class:`TupleCostCell` groups.

    Attributes
    ----------
    total_tuples
        ``Σ multiplicity`` — the tuple count the cells cover.
    total_weight
        ``Σ (weight × multiplicity)`` — the whole grid's work proxy.
    reference_weight
        The mean per-tuple weight of the SMALLEST-k cells — the anchor that maps
        the 30 s ``per_tuple_seconds`` calibration onto the current grid's
        average small-k tuple (so a k=1-only grid projects the flat model's
        order).
    per_tuple_seconds
        The calibration used.
    projected_seconds
        ``per_tuple_seconds × total_weight / reference_weight``.
    dominant_labels
        The heaviest cells' labels (up to :data:`DOMINANT_TUPLE_SAMPLE`) — the
        tuples the refusal names as the projection's cost drivers.
    """

    total_tuples: int
    total_weight: int
    reference_weight: float
    per_tuple_seconds: float
    projected_seconds: float
    dominant_labels: tuple[str, ...]


def project_weighted_grid_cost(
    cells: Sequence[TupleCostCell],
    *,
    per_tuple_seconds: float,
    dominant_count: int = DOMINANT_TUPLE_SAMPLE,
) -> WeightedGridCost:
    """Project a grid's wall-clock from per-``(instrument, tf, k)`` cost cells.

    The k-aware model (DEC-SWEEP-SCALE-GUARDS-001):
    ``projected = per_tuple_seconds × Σ(weight × mult) / reference_weight``,
    where ``weight = C(n_bits, k) × N_bars`` and ``reference_weight`` is the
    multiplicity-weighted MEAN weight of the cells at the grid's smallest K. That
    anchoring means the EXISTING 30 s calibration maps to the current average
    small-k tuple — a grid whose K is uniform (e.g. k=1 only) projects
    ``per_tuple_seconds × total_tuples`` EXACTLY (the reference is the mean, so
    it cancels), matching the flat model's order; a grid with high-k tuples
    projects orders higher, tracking the ``C(60,1)→C(60,5) ≈ 91,000×`` spread.

    Honest (Golden Rule #10): ``C(n_bits, k) × N_bars`` is a first-order work
    proxy, NOT a measured per-tuple time — the projection stays UNBENCHMARKED.
    O(cells) — pure arithmetic, no enumeration of the tuple product.

    Parameters
    ----------
    cells
        The grid's cost groups (non-empty; each already validated).
    per_tuple_seconds
        The calibration seconds (already validated ``> 0``, finite).
    dominant_count
        How many heaviest cells to name (``>= 1``).

    Raises
    ------
    ValueError
        If *cells* is empty or *dominant_count* ``< 1``.
    """
    if not cells:
        raise ValueError("project_weighted_grid_cost: cells must be non-empty")
    if dominant_count < 1:
        raise ValueError(
            f"project_weighted_grid_cost: dominant_count must be >= 1; got {dominant_count!r}"
        )
    k_min = min(cell.k for cell in cells)
    min_k_cells = [cell for cell in cells if cell.k == k_min]
    ref_weight_sum = sum(cell.weight() * cell.multiplicity for cell in min_k_cells)
    ref_multiplicity = sum(cell.multiplicity for cell in min_k_cells)
    # ref_multiplicity >= 1 (min_k_cells is non-empty and every multiplicity >= 1)
    # and ref_weight_sum >= 1 (every weight >= C(n_bits, k) * 1 >= 1) — so the
    # division below can never hit zero (cell invariants guarantee it).
    reference_weight = ref_weight_sum / ref_multiplicity
    total_weight = sum(cell.weight() * cell.multiplicity for cell in cells)
    total_tuples = sum(cell.multiplicity for cell in cells)
    projected_seconds = per_tuple_seconds * total_weight / reference_weight
    dominant = sorted(cells, key=lambda cell: (-cell.weight(), cell.label))[:dominant_count]
    return WeightedGridCost(
        total_tuples=total_tuples,
        total_weight=total_weight,
        reference_weight=reference_weight,
        per_tuple_seconds=per_tuple_seconds,
        projected_seconds=projected_seconds,
        dominant_labels=tuple(cell.label for cell in dominant),
    )


@dataclass(frozen=True, slots=True)
class SlaProjection:
    """One launch-guard projection + verdict (immutable).

    Attributes
    ----------
    tuple_count
        The grid's tuple count (``count_sweep_tuples``).
    per_tuple_seconds
        The per-tuple estimate the projection used (env-resolved).
    projected_seconds
        ``tuple_count × per_tuple_seconds`` (flat model), or the k-aware weighted
        projection when *weighted* (DEC-SWEEP-SCALE-GUARDS-001).
    envelope_seconds
        The §8 envelope ceiling compared against.
    decision
        The verdict — see :class:`SlaDecision`.
    weighted
        ``True`` iff the projection is the k-aware per-tuple-cost-weighted model
        (cost cells were supplied), else the flat linear-in-tuples model.
    dominant_tuples
        When *weighted*, the labels of the heaviest ``(instrument, tf, k)`` cost
        groups — named in the refusal so the operator sees the cost drivers.
    """

    tuple_count: int
    per_tuple_seconds: float
    projected_seconds: float
    envelope_seconds: float
    decision: SlaDecision
    weighted: bool = False
    dominant_tuples: tuple[str, ...] = field(default_factory=tuple)

    @property
    def projected_days(self) -> float:
        """The projection in days (display convenience)."""
        return self.projected_seconds / 86_400.0

    def _model_clause(self) -> str:
        """Name the projection model (flat vs k-aware weighted) + the honesty tail."""
        if self.weighted:
            return (
                f"{self.tuple_count} tuples, per-tuple-cost-weighted "
                f"(∝ C(n_bits, k) × N_bars), an UNBENCHMARKED estimate — override "
                f"the base rate via {ENV_PER_TUPLE_SECONDS}"
            )
        return (
            f"{self.tuple_count} tuples × {self.per_tuple_seconds:g} s/tuple, an "
            f"UNBENCHMARKED estimate — override via {ENV_PER_TUPLE_SECONDS}"
        )

    def _projection_clause(self) -> str:
        """The shared honest projection sentence both messages carry."""
        return (
            f"projected wall-clock ≈ {self.projected_days:.1f} days "
            f"({self._model_clause()}) exceeds "
            f"the CLAUDE.md §8 envelope of {self.envelope_seconds / 86_400.0:.0f} days"
        )

    def _dominant_clause(self) -> str:
        """Name the dominant tuples on the weighted path (empty otherwise)."""
        if not self.dominant_tuples:
            return ""
        return " Dominant tuples (highest per-tuple cost): " + ", ".join(self.dominant_tuples) + "."

    def refusal_message(self) -> str:
        """The clear operator-facing refusal (honest: an estimate, not a certification)."""
        return (
            f"REFUSED: {self._projection_clause()}.{self._dominant_clause()} The "
            "projection is SERIAL — it does not model the parallel driver's "
            "multi-worker speedup, so BRUTEX_SWEEP_WORKERS is a lever alongside "
            "shrinking the grid (--max-tuples / fewer symbols, intervals, K, "
            "SL/TP legs) — or re-run with --acknowledge-sla-overrun to proceed "
            "deliberately. This guard projects from an estimate; it does NOT "
            "certify the §8 SLA either way."
        )

    def acknowledged_warning(self) -> str:
        """The loud proceed-anyway warning for an acknowledged overrun."""
        return (
            f"SLA OVERRUN ACKNOWLEDGED: {self._projection_clause()}."
            f"{self._dominant_clause()} Proceeding because "
            "--acknowledge-sla-overrun was passed — expect the sweep to run past "
            "the §8 envelope (the projection is an estimate, not a certification)."
        )


def check_sweep_sla(
    tuple_count: int,
    *,
    acknowledge: bool,
    per_tuple_seconds: float | None = None,
    envelope_seconds: float = SLA_ENVELOPE_SECONDS,
    env: Mapping[str, str] | None = None,
    cells: Sequence[TupleCostCell] | None = None,
) -> SlaProjection:
    """Project a grid's wall-clock and decide allowed / refused / acknowledged.

    Emits ONE loud ``sweep.sla_projection`` structlog event (INFO when allowed,
    WARNING otherwise) carrying the tuple count, the per-tuple estimate, the
    projection, the envelope, the decision, and the honesty flags (the estimate
    is unbenchmarked; the model is serial) — so every launch verdict is
    observable (Golden Rule #8 spirit; the caller additionally audit-stamps the
    launch).

    When *cells* is supplied (DEC-SWEEP-SCALE-GUARDS-001) the projection is the
    k-aware :func:`project_weighted_grid_cost` model — per-tuple cost weighted by
    ``C(n_bits, k) × N_bars``, anchored so the 30 s calibration maps to the
    current average small-k tuple — and the refusal NAMES the dominant tuples.
    Without cells it is the flat linear-in-tuples model (byte-for-byte the prior
    behavior). The decision (allowed / refused / acknowledged) and the message
    contract are identical either way; only the projected seconds differ.

    Parameters
    ----------
    tuple_count
        The grid's tuple count. Must be ``>= 0``. This is the count REPORTED in
        the projection/log; when *cells* drive the projection they need not sum
        to it exactly (missing slices contribute no cells but count as tuples).
    acknowledge
        ``True`` when the operator passed ``--acknowledge-sla-overrun``.
    per_tuple_seconds
        Explicit per-tuple estimate; ``None`` resolves via
        :func:`resolve_per_tuple_seconds` (env override → module constant). In
        the weighted model this is the BASE rate for the reference small-k tuple.
    envelope_seconds
        The SLA ceiling (default the §8 4-day bound). Must be a FINITE float
        ``> 0``.
    env
        Environment mapping forwarded to :func:`resolve_per_tuple_seconds`.
    cells
        Optional per-``(instrument, tf, k)`` cost cells enabling the k-aware
        weighted projection; ``None`` (default) keeps the flat model.

    Raises
    ------
    ValueError
        On a negative *tuple_count*, a non-positive / non-finite
        *envelope_seconds* / *per_tuple_seconds*, a malformed env override, or
        an empty *cells* sequence.
    """
    if tuple_count < 0:
        raise ValueError(f"tuple_count must be >= 0; got {tuple_count!r}")
    _require_positive_finite(envelope_seconds, name="envelope_seconds", raw=envelope_seconds)
    if per_tuple_seconds is None:
        per_tuple_seconds = resolve_per_tuple_seconds(env)
    else:
        _require_positive_finite(per_tuple_seconds, name="per_tuple_seconds", raw=per_tuple_seconds)

    dominant_tuples: tuple[str, ...] = ()
    if cells is None:
        # Flat model — exactly the law the DEC-RUST-SWEEP-OPS-001 facade
        # mirrors (`_sla_decision_python` is the live oracle): route through
        # the default-ON seam; flag-OFF is the verbatim oracle.
        weighted = False
        if sweep_ops_rust_enabled():
            projected, decision_token = routed_sla_decision(
                tuple_count,
                per_tuple_seconds,
                envelope_seconds,
                acknowledge,
                python_law=lambda: _sla_decision_python(
                    tuple_count, per_tuple_seconds, envelope_seconds, acknowledge
                ),
            )
        else:
            projected, decision_token = _sla_decision_python(
                tuple_count, per_tuple_seconds, envelope_seconds, acknowledge
            )
        decision = SlaDecision(decision_token)
    else:
        # k-aware weighted projection (DEC-SWEEP-SCALE-GUARDS-001) — OUTSIDE
        # the facade's flat-law domain (the routed law multiplies
        # tuple_count x per_tuple_seconds), so the weighted arm's decision
        # ladder deliberately stays Python.
        weighted = True
        cost = project_weighted_grid_cost(cells, per_tuple_seconds=per_tuple_seconds)
        projected = cost.projected_seconds
        dominant_tuples = cost.dominant_labels
        if projected <= envelope_seconds:
            decision = SlaDecision.ALLOWED
        elif acknowledge:
            decision = SlaDecision.ACKNOWLEDGED
        else:
            decision = SlaDecision.REFUSED

    projection = SlaProjection(
        tuple_count=tuple_count,
        per_tuple_seconds=per_tuple_seconds,
        projected_seconds=projected,
        envelope_seconds=envelope_seconds,
        decision=decision,
        weighted=weighted,
        dominant_tuples=dominant_tuples,
    )
    log = logger.info if decision is SlaDecision.ALLOWED else logger.warning
    log(
        "sweep.sla_projection",
        tuple_count=tuple_count,
        per_tuple_seconds=per_tuple_seconds,
        projected_wall_clock_seconds=projected,
        envelope_seconds=envelope_seconds,
        decision=decision.value,
        per_tuple_estimate_benchmarked=False,
        projection_model="k_weighted" if weighted else "serial",
        dominant_tuples=list(dominant_tuples),
    )
    return projection


def _sla_decision_python(
    tuple_count: int,
    per_tuple_seconds: float,
    envelope_seconds: float,
    acknowledge: bool,
) -> tuple[float, str]:
    """The verbatim projection-decision law — the live parity oracle
    (DEC-RUST-SWEEP-OPS-001; retained until the final slice's default flip).

    Returns ``(projected_seconds, decision_token)`` — the token is the
    :class:`SlaDecision` wire value the caller re-wraps.
    """
    projected = tuple_count * per_tuple_seconds
    if projected <= envelope_seconds:
        return projected, SlaDecision.ALLOWED.value
    if acknowledge:
        return projected, SlaDecision.ACKNOWLEDGED.value
    return projected, SlaDecision.REFUSED.value


__all__ = [
    "DOMINANT_TUPLE_SAMPLE",
    "ENV_PER_TUPLE_SECONDS",
    "SLA_ENVELOPE_DAYS",
    "SLA_ENVELOPE_SECONDS",
    "SWEEP_PER_TUPLE_SECONDS",
    "SlaDecision",
    "SlaProjection",
    "TupleCostCell",
    "WeightedGridCost",
    "check_sweep_sla",
    "project_weighted_grid_cost",
    "resolve_per_tuple_seconds",
]
