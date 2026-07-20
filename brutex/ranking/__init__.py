"""Composite ranking + hard rejection gates per Track 1 spec.

Hard rejection gates (applied BEFORE composite scoring):
- total_trades < 30 → REJECT (statistical noise)
- profit_factor < 1.2 → REJECT (no edge)
- max_drawdown_pct > 30% → REJECT (uninvestable)
- win_rate < 25% AND win_loss_ratio < 2.0 → REJECT
- max_consecutive_losses > 10 → REJECT (account-blower)
- Deflated_Sharpe < dsr_min → REJECT (multiple-testing overfit)  [6th gate, opt-in]

Composite ranking score (after gates pass):
    0.25 × Sharpe + 0.20 × Sortino + 0.15 × Calmar
  + 0.15 × (Profit_Factor - 1.0) + 0.10 × Recovery_Factor
  + 0.10 × (Win_Rate × Win_Loss_Ratio) + 0.15 × Risk_Efficiency
  + 0.05 × Deflated_Sharpe   [8th term, opt-in via OverfitGateConfig]

where Risk_Efficiency = avg_MFE / avg_MAE — the "tight stop, big run" signal
(DEC-RANKING-RISK-EFFICIENCY-001): a high value means each trade tended to travel
far in our favour (large Maximum Favorable Excursion) for little adverse heat
(small Maximum Adverse Excursion), i.e. an extreme-tight effective stop *with* a
large profit — the operator's stated #1 objective. Each term is z-score
normalized per (instrument, TF) leaderboard, clamped [-3, +3].

## The 6th gate + 8th term: the Stage-3 overfit control (DEC-RANKING-OVERFIT-001)

The originally-reserved Stage-3 "lockbox" gate needed a train/test split that does
not exist yet. In its place, the **Deflated Sharpe Ratio** (Bailey & López de
Prado 2014) realises the overfit gate from scalars alone: it deflates each combo's
Sharpe against the *expected maximum* Sharpe of the ``N = len(candidates)`` trials
that were tried for this leaderboard (the per-(instrument, TF) trial count the spec
pins at ``TRACK1_BACKTEST_SPEC.md:383-385``). A combo whose Sharpe is no better
than that selection-bias bar is rejected as data-snooped; survivors are down-ranked
by their Deflated Sharpe. The gate + term are **opt-in** via
:class:`~brutex.ranking.overfit.OverfitGateConfig` (the engine lives in
:mod:`brutex.ranking.overfit`, single source of truth — COMMON); passing
``overfit=None`` (the default) keeps the legacy five-gate / seven-term ranking
**byte-identical**.

## Honest envelope (Golden Rule #10) — only DSR is scalar-computable; N is inline-only

DSR is the **only** member of the Stage-4 cascade (DSR / CPCV / SPA / PBO /
Romano-Wolf / WFO / Lockbox / Monte-Carlo, :mod:`brutex.metrics.cascade`)
computable from a leaderboard's scalars; every other filter needs each candidate's
per-period **return series**, absent from the persisted scalar result rows —
synthesising them would fabricate significance and is FORBIDDEN. The authoritative
trial count ``N`` is known only **inline** here (after persistence only ``top_k``
finalists survive and ``N`` is lost), so the gate runs at ranking time and W2-a's
persisted output is, by construction, the overfit-gated leaderboard; no
statistically-valid post-store re-gate exists. When the pool cannot be deflated
(< 2 finite Sharpes or zero cross-trial variance) the gate **abstains** rather than
fabricate a verdict (see :mod:`brutex.ranking.overfit`). The composite score's
absolute scale is irrelevant — it orders one leaderboard *relatively* (z-scores are
relative), so the enabled-path weight sum of 1.05 changes ranking power, never
correctness. Recorded in ``docs/MIGRATION.md`` (DEC-RANKING-001 / DEC-RANKING-OVERFIT-001).
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, cast

from brutex.metrics.compute import ResultMetrics
from brutex.ranking.overfit import (
    OverfitGateConfig,
    deflated_sharpe_for,
    passes_overfit_gate,
    pool_sharpe_dispersion,
)
from brutex.ranking.temporal import TemporalGateInput, TemporalTriple, apply_temporal_gate

# ── Hard rejection gate thresholds (the spec, above) ──
#: Minimum closed trades for a result to be rankable (below this is statistical
#: noise). PUBLIC because it is the single source of truth for the Stage-1B coarse
#: pre-filter too (DEC-STAGE1B-WIRE-001): a mask that fires on fewer than this many
#: bars can never reach this many trades (the trade simulator opens ≤ one trade per
#: entry signal — no pyramiding), so it is provably un-rankable and can be pruned
#: before the expensive trade sim. Keeping the gate + the pre-filter on ONE constant
#: means they can never drift (CLAUDE.md §11 COMMON).
MIN_TRADES = 30
_MIN_PROFIT_FACTOR = 1.2
_MAX_DRAWDOWN_PCT = 30.0
_MIN_WIN_RATE = 0.25
_MIN_WIN_LOSS_RATIO = 2.0
_MAX_CONSECUTIVE_LOSSES = 10

# ── Composite weights (the 7 available terms; the 0.05 lockbox term is Stage-3) ──
_W_SHARPE = 0.25
_W_SORTINO = 0.20
_W_CALMAR = 0.15
_W_PROFIT_FACTOR = 0.15
_W_RECOVERY = 0.10
_W_WIN_WLR = 0.10
#: Risk-efficiency (avg MFE / avg MAE) — the "extreme tight SL + massive profit"
#: priority (DEC-RANKING-RISK-EFFICIENCY-001). Co-equal with Calmar/PF (just under
#: Sharpe's 0.25): a strong first-class voice for the operator's #1 objective, but
#: BLENDED, not dominant — a single lucky huge-MFE trade cannot dictate the
#: leaderboard (the anti-overfit / no-illusion choice, Golden Rule #10).
_W_RISK_EFFICIENCY = 0.15

#: Z-score clamp (spec: per-leaderboard z-score clamped to [-3, +3]).
_Z_CLAMP = 3.0

#: How many z-normalised composite terms the leaderboard scores (the 7 columns
#: :func:`composite_term_inputs` emits, in its documented order). PUBLIC because
#: the bounded columnar rank (:mod:`brutex.engine.sweep_stream`,
#: DEC-SWEEP-STREAMING-RANK-001) sizes its term arrays off it — ONE constant so
#: the two paths can never disagree on the term count (CLAUDE.md §11 COMMON).
N_COMPOSITE_TERMS: Final[int] = 7


@dataclass(frozen=True, slots=True)
class RankedCombo:
    """One gate-passing combo with its leaderboard composite score.

    Attributes
    ----------
    mask
        The u64 strategy mask.
    metrics
        The combo's full :class:`~brutex.metrics.compute.ResultMetrics`.
    composite_score
        The weighted, z-normalised composite — higher is better. Only
        meaningful *relative* to the rest of the same leaderboard.
    """

    mask: int
    metrics: ResultMetrics
    composite_score: float
    #: The combo's Deflated Sharpe probability (Bailey & LdP 2014) when the opt-in
    #: 6th overfit gate ran for this leaderboard, else ``None`` (legacy ranking, or
    #: a pool that could not be deflated). Exposed for leaderboard transparency —
    #: the selection-bias-corrected confidence behind this combo's overfit survival.
    #: HONEST SCOPE (DEC-STATS-GATE-INTEGRITY-001, R2-SR-F3): this figure is
    #: **TUPLE-scoped** — deflated against ``N = len(candidates)`` of ONE grid
    #: cell's rank-time pool (see :attr:`dsr_n_scope` /
    #: :func:`_overfit_gate`), NOT the per-(instrument, tf) leaderboard N the
    #: Stage-4 cascade uses. The under-count materially inflates the value
    #: (measured effect: DSR 0.230 at ``n_trials=1e3`` vs 0.003 at ``1e7``),
    #: so the persisted figure is superseded by the Stage-4 cascade's
    #: leaderboard-N DSR GATE (filter #2), whose N/V provenance is stamped
    #: run-level on the ``CascadeReport`` (``dsr_n_source``/``dsr_n_trials``).
    #: The in-sweep gate DIRECTION is deliberately unchanged — lenient-safe
    #: (an inflated DSR only ever lets MORE combos through to the stricter
    #: cascade gate, never fewer).
    #: ``# pragma: no mutate`` (DEC-MUTATION-GATE-RANKING-002): mutmut's
    #: ``float | None`` → ``float & None`` annotation flip is provably equivalent
    #: (PEP 563 ``from __future__ import annotations`` — field annotations are
    #: never evaluated). The pragma is line-scoped, so the ``None`` default it
    #: also skips is pinned by ``test_deflated_sharpe_defaults_to_none`` instead.
    deflated_sharpe: float | None = None  # pragma: no mutate

    @property
    def dsr_n_scope(self) -> str | None:
        """The trial-population scope of :attr:`deflated_sharpe` — always ``"tuple"``.

        ``"tuple"`` when a Deflated Sharpe is present (it is ALWAYS the
        tuple-scoped ``N = len(candidates)`` figure — :func:`rank_combos` is
        the only site that sets it non-``None``), else ``None`` (no DSR: the
        gate was inactive, abstained, or a Stage-3 refined row). A read-only
        property, not a stored field, so the scope stamp can never drift from
        the value it describes (DEC-STATS-GATE-INTEGRITY-001, R2-SR-F3 — the
        transparency companion of the ``dsr@tupleN`` leaderboard label,
        ADR-0008 condition 7).
        """
        return "tuple" if self.deflated_sharpe is not None else None

    @property
    def risk_efficiency(self) -> float:
        """Raw favorable-to-adverse excursion ratio (avg MFE / avg MAE).

        Exposed for leaderboard transparency: the un-z-scored "tight stop, big
        run" score behind this combo's risk-efficiency contribution to
        :attr:`composite_score`. See :func:`_risk_efficiency`.
        """
        return _risk_efficiency(self.metrics)


def _non_finite_gate_input(value: float | None) -> bool:
    """Whether a float-valued gate input is present but non-finite (``NaN``/±``inf``).

    A ``None`` input is *not* non-finite here — it is mathematically undefined for
    the sample and keeps its existing per-gate ``None`` handling. Only a present
    ``NaN``/``inf`` value is flagged, so :func:`passes_gates` can FAIL CLOSED on it
    (a ``NaN`` silently passes a ``<`` / ``>`` comparison — ``NaN < x`` is always
    ``False`` — so an unguarded gate would wrongly ACCEPT malformed data).
    """
    return value is not None and not math.isfinite(value)


def passes_gates(m: ResultMetrics) -> bool:
    """Apply the hard rejection gates to one result (lockbox gate deferred).

    Returns ``True`` iff the result survives every applicable gate. A ``None``
    metric (mathematically undefined for the sample) is treated as failing —
    a combo with no real edge cannot be ranked above one with a measured edge.

    **Fail-closed on non-finite inputs (DEC-RANKING-TIEBREAK-001).** A ``NaN`` or
    ±``inf`` in any float-valued gate input (``profit_factor``, ``max_drawdown_pct``,
    ``win_rate``, ``win_loss_ratio``) would silently slip through a ``<`` / ``>``
    comparison as a PASS (``NaN < x`` and ``NaN > x`` are both ``False``), a silent
    wrong-ACCEPT on malformed data. Such a result is REJECTED up front. The
    integer-valued gates (``total_trades``, ``max_consecutive_losses``) cannot be
    non-finite. Behaviour for every FINITE value and for ``None`` is byte-identical
    to before this guard.
    """
    if any(
        _non_finite_gate_input(value)
        for value in (m.profit_factor, m.max_drawdown_pct, m.win_rate, m.win_loss_ratio)
    ):
        return False
    if m.total_trades < MIN_TRADES:
        return False
    if m.profit_factor is None or m.profit_factor < _MIN_PROFIT_FACTOR:
        return False
    if m.max_drawdown_pct > _MAX_DRAWDOWN_PCT:
        return False
    # win_rate < 25% AND win/loss-ratio < 2.0 → reject (a low hit-rate is only
    # acceptable if the winners are much bigger than the losers).
    # ``# pragma: no mutate`` (DEC-MUTATION-GATE-RANKING-002): the ``0.0`` fallback
    # feeds ONLY the ``wlr < _MIN_WIN_LOSS_RATIO`` comparison below, so every
    # sub-2.0 constant is behaviourally identical — mutmut's ``0.0 → 1.0`` mutant
    # is provably equivalent. The ``is not None`` branch the pragma also skips
    # stays pinned by ``test_low_winrate_with_none_wlr_rejected``.
    wlr = m.win_loss_ratio if m.win_loss_ratio is not None else 0.0  # pragma: no mutate
    if m.win_rate < _MIN_WIN_RATE and wlr < _MIN_WIN_LOSS_RATIO:
        return False
    return m.max_consecutive_losses <= _MAX_CONSECUTIVE_LOSSES


# ── Gate-door reason vocabulary (DEC-SWEEP-RESULTS-DEPTH-001 · gap-register
# ruling 12 / R28) — the STABLE wire tokens :func:`gate_reasons` emits. P2
# folds per-door kill COUNTERS from these tokens at its orchestrator seam into
# the run manifest (``per_door_kills``); the tokens are a wire vocabulary, so
# renaming one is a breaking contract change (pinned by test). ──
#: The fail-closed guard tripped: a float-valued gate input was present but
#: non-finite (``NaN``/±``inf``) — the metrics are malformed, so NO other door
#: is judged (judging garbage would fabricate door verdicts, Golden Rule #1).
GATE_NON_FINITE_INPUT: Final[str] = "non_finite_input"
#: Door 1 — ``total_trades < MIN_TRADES`` (statistical noise).
GATE_MIN_TRADES: Final[str] = "min_trades"
#: Door 2 — ``profit_factor`` ``None`` or ``< 1.2`` (no edge).
GATE_PROFIT_FACTOR: Final[str] = "profit_factor"
#: Door 3 — ``max_drawdown_pct > 30%`` (uninvestable).
GATE_MAX_DRAWDOWN: Final[str] = "max_drawdown"
#: Door 4 — ``win_rate < 25%`` AND ``win_loss_ratio < 2.0`` (``None`` ratio
#: counts as ``0.0``, exactly like :func:`passes_gates`).
GATE_WIN_RATE_WLR: Final[str] = "win_rate_wlr"
#: Door 5 — ``max_consecutive_losses > 10`` (account-blower).
GATE_CONSECUTIVE_LOSSES: Final[str] = "consecutive_losses"

#: The COMPLETE reason vocabulary in EVALUATION order — the exact order
#: :func:`passes_gates` checks its doors, so ``gate_reasons(m)[0]`` is always
#: the door ``passes_gates`` short-circuits on. Consumers (P2's manifest
#: counter fold) may rely on this order being stable.
GATE_REASON_ORDER: Final[tuple[str, ...]] = (
    GATE_NON_FINITE_INPUT,
    GATE_MIN_TRADES,
    GATE_PROFIT_FACTOR,
    GATE_MAX_DRAWDOWN,
    GATE_WIN_RATE_WLR,
    GATE_CONSECUTIVE_LOSSES,
)


def gate_reasons(m: ResultMetrics) -> tuple[str, ...]:
    """The reason-returning twin of :func:`passes_gates` — every failed door, named.

    DEC-SWEEP-RESULTS-DEPTH-001 (gap-register ruling 12 / R28 per-door kill
    counts): same input as :func:`passes_gates`, returns the ordered tuple of
    failed-door reason tokens from the STABLE :data:`GATE_REASON_ORDER`
    vocabulary — empty tuple ⇔ the result passes every gate. The two functions
    can never disagree: ``bool(passes_gates(m)) == (gate_reasons(m) == ())``
    holds for EVERY input (property-pinned, hypothesis-randomized), and both
    read the SAME threshold constants in the SAME order (CLAUDE.md §11 COMMON —
    this twin lives beside the predicate precisely so a threshold change moves
    both at once).

    Semantics, mirroring :func:`passes_gates` exactly:

    * **Fail-closed short-circuit** — a present-but-non-finite float gate input
      (``NaN``/±``inf`` in ``profit_factor`` / ``max_drawdown_pct`` /
      ``win_rate`` / ``win_loss_ratio``) returns
      ``(``:data:`GATE_NON_FINITE_INPUT```,)`` ALONE: the metrics are
      malformed, and judging the remaining doors on garbage would fabricate
      per-door verdicts (a ``NaN`` silently passes any ``<``/``>``), so no
      other token is emitted (Golden Rule #1).
    * **All doors judged** — on finite inputs every one of the 5 hard doors is
      evaluated INDEPENDENTLY (no short-circuit), so a combo failing three
      doors reports all three, in evaluation order. ``gate_reasons(m)[0]`` is
      therefore exactly the door ``passes_gates`` would short-circuit on;
      counter folds may count first-door-only or every-door as they choose.
    * **Scope** — the 5 hard gates + the fail-closed guard ONLY. The opt-in
      6th (overfit / Deflated-Sharpe) gate is deliberately OUT of this
      vocabulary: it runs pool-relatively inside :func:`rank_combos`
      (``N = len(candidates)`` — not a per-result predicate) and is counted at
      its own rank seam, never fabricated per-row here.

    O(1) per call (a handful of comparisons + one small tuple allocation);
    holds no state, so it is safe from any fold/counter thread.
    """
    if any(
        _non_finite_gate_input(value)
        for value in (m.profit_factor, m.max_drawdown_pct, m.win_rate, m.win_loss_ratio)
    ):
        return (GATE_NON_FINITE_INPUT,)
    reasons: list[str] = []
    if m.total_trades < MIN_TRADES:
        reasons.append(GATE_MIN_TRADES)
    if m.profit_factor is None or m.profit_factor < _MIN_PROFIT_FACTOR:
        reasons.append(GATE_PROFIT_FACTOR)
    if m.max_drawdown_pct > _MAX_DRAWDOWN_PCT:
        reasons.append(GATE_MAX_DRAWDOWN)
    # The SAME ``None`` → 0.0 projection passes_gates applies (a low hit-rate is
    # only acceptable if the winners are much bigger than the losers).
    wlr = m.win_loss_ratio if m.win_loss_ratio is not None else 0.0
    if m.win_rate < _MIN_WIN_RATE and wlr < _MIN_WIN_LOSS_RATIO:
        reasons.append(GATE_WIN_RATE_WLR)
    if m.max_consecutive_losses > _MAX_CONSECUTIVE_LOSSES:
        reasons.append(GATE_CONSECUTIVE_LOSSES)
    return tuple(reasons)


#: The :func:`persisted_hard_gates_recheck` wire verdicts — the serve-time
#: belt's two-state vocabulary (DEC-RESULTS-TRUST-HONESTY-001): ``"ok"`` = no
#: reconstructible door failed; ``"marked"`` = at least one did (the row is
#: MARKED, never dropped — the ``snapshot_era`` pattern).
HARD_GATES_RECHECK_OK: Final[str] = "ok"
HARD_GATES_RECHECK_MARKED: Final[str] = "marked"


def persisted_hard_gates_recheck(
    *,
    total_trades: int,
    profit_factor: float | None,
    max_drawdown_pct: float,
    win_rate: float,
) -> str:
    """Serve-time hard-gate RE-CHECK over the fields a stored ``ResultRow`` persists.

    DEC-RESULTS-TRUST-HONESTY-001 (the /results belt for legacy / non-gated
    stores): re-judges the reconstructible hard doors from the persisted
    scalars — the fail-closed non-finite guard (over the three persisted float
    inputs), door 1 (``total_trades < MIN_TRADES``), door 2 (``profit_factor``
    ``None``/``< 1.2``) and door 3 (``max_drawdown_pct > 30``) — reading the
    SAME threshold constants as :func:`passes_gates` (CLAUDE.md §11 COMMON;
    homed beside the predicate so a threshold change moves both at once).

    Returns :data:`HARD_GATES_RECHECK_MARKED` when any judged door fails, else
    :data:`HARD_GATES_RECHECK_OK`. The consumer MARKS, never drops (Golden
    Rule #1 — a legacy row is surfaced with its verdict, not hidden).

    HONEST envelope (Golden Rule #10): a PARTIAL recheck, one-way by
    construction — doors 4 (win-rate × win/loss-ratio) and 5 (consecutive
    losses) need ``win_loss_ratio`` / ``max_consecutive_losses``, which a
    ``ResultRow`` does NOT persist, so they are NOT judged (fabricating
    neutral fills would fake door verdicts). Therefore ``"marked"`` implies
    the full :func:`passes_gates` would also reject (property-pinned), while
    ``"ok"`` is necessary-not-sufficient — never presented as a full-gate
    pass. ``win_rate`` participates only in the fail-closed non-finite guard
    (its door needs the unpersisted ratio).
    """
    if any(_non_finite_gate_input(value) for value in (profit_factor, max_drawdown_pct, win_rate)):
        return HARD_GATES_RECHECK_MARKED
    if total_trades < MIN_TRADES:
        return HARD_GATES_RECHECK_MARKED
    if profit_factor is None or profit_factor < _MIN_PROFIT_FACTOR:
        return HARD_GATES_RECHECK_MARKED
    if max_drawdown_pct > _MAX_DRAWDOWN_PCT:
        return HARD_GATES_RECHECK_MARKED
    return HARD_GATES_RECHECK_OK


def _f(value: float | None) -> float:
    """A ``None`` composite term contributes 0 (survivors have real metrics)."""
    return value if value is not None else 0.0


def _risk_efficiency(m: ResultMetrics) -> float:
    """Favorable-to-adverse excursion ratio — the "tight SL + massive profit" signal.

    ``avg_mfe_paisa / avg_mae_paisa``: how far the average trade ran in our favour
    (Maximum Favorable Excursion) per unit of adverse heat it endured (Maximum
    Adverse Excursion). A high value is exactly the operator's objective — the move
    paid big (large MFE) while the effective stop never needed to be wide (small
    MAE). ``avg_mae_paisa`` is floored at 1 paisa so a never-adverse combo yields a
    large but FINITE ratio (the z-score needs finite inputs), never a division by
    zero. Both inputs are non-negative int64 paisa already aggregated on
    :class:`~brutex.metrics.compute.ResultMetrics`.
    """
    return m.avg_mfe_paisa / max(m.avg_mae_paisa, 1)


def _zscores(values: Sequence[float]) -> list[float]:
    """Population z-scores clamped to ``[-_Z_CLAMP, +_Z_CLAMP]``.

    Assumes a NON-empty input — :func:`rank_combos` only calls this once it has
    surviving combos. A zero-variance input (all values equal, incl. a single
    survivor) yields all-zeros: no term can discriminate.
    """
    n = len(values)
    mean = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / n
    if variance == 0.0:
        return [0.0] * n
    std = variance**0.5
    return [max(-_Z_CLAMP, min(_Z_CLAMP, (v - mean) / std)) for v in values]


def composite_term_inputs(
    m: ResultMetrics,
) -> tuple[float, float, float, float, float, float, float]:
    """The 7 composite z-term inputs for ONE result, in leaderboard column order.

    ``(sharpe, sortino, calmar, profit_factor − 1.0, recovery, win_rate ×
    win_loss_ratio, risk_efficiency)`` — each already ``_f``-projected
    (``None`` → ``0.0``), exactly as :func:`rank_combos` has always derived its
    z-score columns. PUBLIC single source of truth (CLAUDE.md §11 COMMON,
    DEC-SWEEP-STREAMING-RANK-001): the batch rank builds its columns through
    this function AND the bounded columnar rank
    (:class:`brutex.engine.sweep_stream.SurvivorColumns`) stores these exact
    float64 values at append time — the same expression on the same inputs, so
    the two paths are bit-identical by construction, not by test discipline.
    """
    return (
        _f(m.sharpe_ratio),
        _f(m.sortino_ratio),
        _f(m.calmar_ratio),
        # ``# pragma: no mutate`` (DEC-MUTATION-GATE-RANKING-002): z-scores are
        # translation-invariant — shifting every survivor's (PF − 1.0) by the same
        # constant leaves (v − mean)/std mathematically identical (float rounding can
        # differ at ULP level on some pools, which cannot meaningfully reorder a
        # leaderboard) — so mutmut's ``- 1.0`` → ``+ 1.0`` / ``- 2.0`` mutants are
        # provably equivalent. The spec's ``Profit_Factor - 1.0`` form is kept for
        # readability, not arithmetic effect.
        _f(m.profit_factor) - 1.0,  # pragma: no mutate
        _f(m.recovery_factor),
        m.win_rate * _f(m.win_loss_ratio),
        _risk_efficiency(m),
    )


def composite_scores(
    term_columns: Sequence[Sequence[float]], dsr_contrib: Sequence[float]
) -> list[float]:
    """Z-normalise the 7 term columns and fold the weighted composite per survivor.

    THE one composite expression (CLAUDE.md §11 COMMON) — the exact
    left-associated weighted sum the spec pins, shared by :func:`rank_combos`
    and the bounded columnar rank so a future weight change can never silently
    fork the two paths. *term_columns* must be the
    :data:`N_COMPOSITE_TERMS` columns in :func:`composite_term_inputs` order,
    each aligned to one (post-gate, post-overfit) survivor population;
    *dsr_contrib* is the per-survivor weighted Deflated-Sharpe z-term (all
    zeros when the 8th term is inactive — see :func:`dsr_z_contributions`).

    O(terms × survivors) time, O(survivors) space; each column goes through the
    SAME :func:`_zscores` left-fold (no vectorised/pairwise reduction — that
    would change bits).
    """
    if len(term_columns) != N_COMPOSITE_TERMS:
        raise ValueError(
            f"composite_scores expects {N_COMPOSITE_TERMS} term columns, got {len(term_columns)}"
        )
    z = [_zscores(column) for column in term_columns]
    return [
        (
            _W_SHARPE * z[0][i]
            + _W_SORTINO * z[1][i]
            + _W_CALMAR * z[2][i]
            + _W_PROFIT_FACTOR * z[3][i]
            + _W_RECOVERY * z[4][i]
            + _W_WIN_WLR * z[5][i]
            + _W_RISK_EFFICIENCY * z[6][i]
            + dsr_contrib[i]
        )
        for i in range(len(dsr_contrib))
    ]


def dsr_z_contributions(
    dsr_by_survivor: Sequence[float] | None,
    n_survivors: int,
    overfit: OverfitGateConfig | None,
) -> list[float]:
    """The per-survivor weighted Deflated-Sharpe z-term (the opt-in 8th term).

    ``dsr_by_survivor`` is non-``None`` exactly when the overfit gate ran AND
    was deflatable (then *overfit* is the active config whose ``weight`` scales
    the z-scored DSRs); otherwise every survivor contributes ``0.0`` — the
    legacy seven-term composite. Shared by both rank paths (COMMON).
    """
    if dsr_by_survivor is None:
        return [0.0] * n_survivors
    z_dsr = _zscores(dsr_by_survivor)
    # ``# pragma: no mutate`` (DEC-MUTATION-GATE-RANKING-002): ``typing.cast``
    # is a runtime no-op, so its type-string mutant is provably equivalent.
    # The cast sits on its own line so the ``weight * z`` arithmetic below
    # STAYS inside the mutation surface.
    overfit_cfg = cast("OverfitGateConfig", overfit)  # pragma: no mutate
    return [overfit_cfg.weight * z for z in z_dsr]


def ensure_unique_survivor_masks(masks: Sequence[int]) -> None:
    """ENFORCE the unique-mask precondition the deterministic total order depends on.

    The tie-break keys on ``mask`` (DEC-RANKING-TIEBREAK-001), so a DUPLICATE
    mask among the rank-reaching survivors would silently reintroduce
    input-order-dependent ordering; detect it and FAIL LOUD with the offending
    mask(s) named — a deterministic, auditable error instead of latent
    nondeterminism. ONE guard shared by :func:`rank_combos` and the bounded
    columnar rank (COMMON), so the two paths raise the byte-identical message.
    """
    if len(set(masks)) != len(masks):
        counts = Counter(masks)
        dupes = sorted(mask for mask, count in counts.items() if count > 1)
        raise ValueError(
            "rank_combos requires a unique mask per candidate (the deterministic "
            f"tie-break keys on it); duplicate survivor mask(s): {dupes}"
        )


def overfit_gate_positions(
    pool_sharpes: Sequence[float | None],
    survivor_inputs: Sequence[tuple[float | None, int, float | None, float | None]],
    overfit: OverfitGateConfig,
) -> tuple[list[int], list[float]] | None:
    """The 6th (overfit) gate CORE over scalar columns — shared by both rank paths.

    Deflates each survivor's Sharpe against the **whole** *pool_sharpes*
    population (the authoritative multiple-testing trial count
    ``N = len(pool_sharpes)`` + its cross-trial variance ``V`` — the TWO
    populations of the spec: the pool is every backtested candidate, the
    survivors are the gate-passing subset). Each *survivor_inputs* row is
    ``(sharpe_ratio, total_trades, skewness, kurtosis)`` — the exact
    :func:`deflated_sharpe_for` inputs, with ``None`` preserved (never
    NaN-encoded: the deflation's ``None`` fallbacks are semantic).

    Returns
    -------
    tuple[list[int], list[float]] | None
        ``None`` when the pool cannot be deflated (< 2 finite Sharpes or zero
        cross-trial variance — the gate ABSTAINS, Golden Rule #10); else the
        kept survivor POSITIONS (indices into *survivor_inputs*, ascending
        input order) and their aligned Deflated Sharpe values.
    """
    dispersion = pool_sharpe_dispersion(pool_sharpes)
    if not dispersion.deflatable:
        return None
    n_trials = len(pool_sharpes)
    kept_positions: list[int] = []
    kept_dsrs: list[float] = []
    for position, (sharpe, total_trades, skew, kurt) in enumerate(survivor_inputs):
        dsr = deflated_sharpe_for(
            sharpe,
            total_trades,
            skew,
            kurt,
            n_trials=n_trials,
            trial_sharpe_variance=dispersion.variance,
            cfg=overfit,
        )
        if passes_overfit_gate(dsr, overfit):
            kept_positions.append(position)
            # passes_overfit_gate guarantees dsr is a float (never None) here; the
            # cast keeps the typed DSR leaderboard precise without re-checking.
            # ``# pragma: no mutate`` (DEC-MUTATION-GATE-RANKING-002): ``typing.cast``
            # is a runtime no-op, so mutmut's type-string mutant ("float" →
            # "XXfloatXX") is provably equivalent — nothing behavioural on this line.
            kept_dsrs.append(cast("float", dsr))  # pragma: no mutate
    return kept_positions, kept_dsrs


def _overfit_gate(
    candidates: Sequence[tuple[int, ResultMetrics]],
    survivors: list[tuple[int, ResultMetrics]],
    overfit: OverfitGateConfig,
) -> tuple[list[tuple[int, ResultMetrics]], list[float] | None]:
    """Apply the 6th (overfit) gate to *survivors*; return ``(kept, kept_dsrs)``.

    Deflates every survivor's Sharpe against the **whole** ``candidates`` pool — the
    trial count ``N = len(candidates)`` and the cross-trial Sharpe variance ``V`` are
    taken over every combo that was tried for this leaderboard (the authoritative
    multiple-testing population, ``TRACK1_BACKTEST_SPEC.md:383-385``), NOT just the
    gate-survivors. A survivor is kept iff its Deflated Sharpe clears ``dsr_min``.

    When the pool cannot be deflated (< 2 finite Sharpes or zero cross-trial
    variance) the gate **abstains**: *survivors* pass through unchanged and the
    second element is ``None`` (no DSR term), so the leaderboard falls back to its
    seven single-result terms rather than a fabricated deflation (Golden Rule #10).

    A thin ``(mask, metrics)``-pairs adapter over :func:`overfit_gate_positions`
    (the shared scalar core — DEC-SWEEP-STREAMING-RANK-001, CLAUDE.md §11 COMMON).

    Returns
    -------
    tuple[list[tuple[int, ResultMetrics]], list[float] | None]
        The kept ``(mask, metrics)`` survivors and their per-survivor Deflated
        Sharpe values (aligned to ``kept``), or ``None`` DSR list when the pool
        was not deflatable (abstain).
    """
    outcome = overfit_gate_positions(
        [m.sharpe_ratio for _, m in candidates],
        [(m.sharpe_ratio, m.total_trades, m.skewness, m.kurtosis) for _, m in survivors],
        overfit,
    )
    if outcome is None:
        return survivors, None
    kept_positions, kept_dsrs = outcome
    return [survivors[position] for position in kept_positions], kept_dsrs


def _temporal_survivors(
    candidates: Sequence[tuple[int, ResultMetrics]],
    temporal: TemporalGateInput,
) -> list[tuple[int, ResultMetrics]]:
    """Hard doors + the temporal year gate in ONE pass (the gate-ON arm only).

    DEC-SWEEP-TEMPORAL-ROBUSTNESS-001: judges every candidate through the SAME
    :func:`passes_gates` doors the default arm uses (one shared predicate — the
    two arms cannot drift), collects each survivor's aligned
    :class:`~brutex.ranking.temporal.TemporalTriple`, and applies
    :func:`~brutex.ranking.temporal.apply_temporal_gate` (the ONE mode fold —
    ``observe`` counts would-be kills and filters nothing; a whole-gate
    abstention keeps everything and records it on ``temporal.stats``).

    Fail-loud wiring guard (Golden Rule #1): the config being active REQUIRES a
    triple per candidate — a length mismatch or a ``None`` triple on a
    gate-passing survivor raises :class:`ValueError` naming the mask, never a
    silent pass-all.
    """
    if temporal.triples is None:
        raise ValueError(
            "rank_combos: temporal gate is configured but no per-candidate triples "
            "were supplied (TemporalGateInput.triples is None on the batch path) — "
            "a missing fold must never become a silent pass-all"
        )
    if len(temporal.triples) != len(candidates):
        raise ValueError(
            f"rank_combos: {len(temporal.triples)} temporal triples for "
            f"{len(candidates)} candidates — the fold and the candidate stream "
            f"desynchronised (wiring bug)"
        )
    survivors: list[tuple[int, ResultMetrics]] = []
    survivor_triples: list[TemporalTriple] = []
    for index, (mask, m) in enumerate(candidates):
        if not passes_gates(m):
            continue
        triple = temporal.triples[index]
        if triple is None:
            raise ValueError(
                f"rank_combos: candidate mask 0x{mask:032x} passed the hard doors "
                f"but carries no TemporalTriple — a missing per-mask fold (wiring "
                f"bug), never a silent pass-all"
            )
        survivors.append((mask, m))
        survivor_triples.append(triple)
    kept = apply_temporal_gate(
        survivor_triples,
        cfg=temporal.cfg,
        n_judged_years=temporal.n_judged_years,
        stats=temporal.stats,
    )
    if len(kept) == len(survivors):
        return survivors
    return [survivors[position] for position in kept]


def rank_combos(
    candidates: Sequence[tuple[int, ResultMetrics]],
    *,
    overfit: OverfitGateConfig | None = None,
    temporal: TemporalGateInput | None = None,
) -> list[RankedCombo]:
    """Rank a leaderboard: gate, z-normalise the composite terms, score, sort.

    The composite terms are z-normalised **across the surviving population** (the
    spec's "per (instrument, TF) leaderboard"), so the caller is responsible for
    passing one instrument/TF leaderboard at a time.

    Parameters
    ----------
    candidates
        ``(mask, metrics)`` pairs for one (instrument, TF) leaderboard. ``len`` is
        the authoritative multiple-testing trial count ``N`` for the opt-in overfit
        gate (``TRACK1_BACKTEST_SPEC.md:383-385``). Each ``mask`` is the combo's
        identity and MUST be unique among the gate-passing survivors — the
        deterministic tie-break keys on it (see Raises).
    overfit
        Opt-in :class:`~brutex.ranking.overfit.OverfitGateConfig` activating the
        **6th (overfit) gate + the 8th (Deflated-Sharpe) ranking term**
        (DEC-RANKING-OVERFIT-001). ``None`` (the default) keeps the legacy
        five-gate / seven-term ranking byte-identical.
    temporal
        Opt-in :class:`~brutex.ranking.temporal.TemporalGateInput` activating
        the **temporal-robustness year gate**
        (DEC-SWEEP-TEMPORAL-ROBUSTNESS-001): the hard-door SURVIVORS are
        filtered through the P1/P2 predicates BEFORE the overfit gate — the
        candidate POOL feeding the DSR deflation (``N = len(candidates)`` and
        its Sharpe column) is UNTOUCHED in both rank paths, so survivors'
        ``deflated_sharpe`` is bit-identical gate-on vs gate-off
        (regression-pinned). Requires ``triples`` aligned 1:1 with
        *candidates* (a missing triple raises — never a silent pass-all);
        ``mode="observe"`` evaluates + counts on ``stats`` and filters
        nothing; a whole-gate abstention filters nothing and records itself.
        ``None`` (the default) is byte-identical legacy ranking.

    Returns
    -------
    list[RankedCombo]
        The gate-passing combos sorted by composite score (descending), with the
        combo's unique ``mask`` (ascending) as the deterministic final tie-break, so
        the ranking is a TOTAL order: a leaderboard ranks bit-identically regardless
        of the order ``candidates`` was enumerated in (Golden Rules #4/#7;
        DEC-RANKING-TIEBREAK-001). Empty if no candidate survives the gates. When
        ``overfit`` is supplied and the pool was deflatable, each :class:`RankedCombo`
        carries its ``deflated_sharpe`` and the composite includes the weighted
        Deflated-Sharpe z-term.

    Raises
    ------
    ValueError
        If two gate-passing survivors share the same ``mask``. The total-order
        tie-break depends on a unique mask per combo; a duplicate would silently
        reintroduce input-order-dependent ordering, so it is rejected LOUDLY with the
        offending mask(s) named (DEC-RANKING-TIEBREAK-001) — a deterministic,
        auditable error instead of latent nondeterminism. Also on a temporal
        wiring violation (missing/misaligned triples — see
        :func:`_temporal_survivors`).
    """
    # Hard doors → temporal (survivors filter) → overfit (full candidate pool).
    # The temporal=None default arm is LITERALLY the pre-change line — the
    # temporal machinery is never invoked (the gate-OFF zero-work spy pin).
    if temporal is None:
        survivors = [(mask, m) for mask, m in candidates if passes_gates(m)]
    else:
        survivors = _temporal_survivors(candidates, temporal)

    # ``# pragma: no mutate`` (DEC-MUTATION-GATE-RANKING-002): a local-variable
    # annotation is never evaluated at runtime, so mutmut's ``| None`` → ``& None``
    # flip is provably equivalent — nothing behavioural on this line.
    dsr_by_survivor: list[float] | None = None  # pragma: no mutate
    if overfit is not None and survivors:
        survivors, dsr_by_survivor = _overfit_gate(candidates, survivors, overfit)

    if not survivors:
        return []

    # The 7 z-term columns + the opt-in 8th (DSR) contribution + the weighted
    # composite fold all go through the SHARED internals
    # (``composite_term_inputs`` / ``dsr_z_contributions`` / ``composite_scores``
    # — DEC-SWEEP-STREAMING-RANK-001): the bounded columnar rank calls the SAME
    # functions on the SAME float64 values in the SAME survivor order, so batch
    # and bounded composites are bit-identical by construction. Each value below
    # is the exact expression this function always computed (transposing
    # per-survivor tuples into columns changes no arithmetic).
    term_rows = [composite_term_inputs(m) for _, m in survivors]
    term_columns = tuple([row[term] for row in term_rows] for term in range(N_COMPOSITE_TERMS))
    # 8th term (opt-in): the Deflated-Sharpe z-term — ``overfit`` is non-None
    # exactly when ``dsr_by_survivor`` is a list (the gate ran and was deflatable).
    dsr_contrib = dsr_z_contributions(dsr_by_survivor, len(survivors), overfit)
    composites = composite_scores(term_columns, dsr_contrib)

    ranked = [
        RankedCombo(
            mask=mask,
            metrics=m,
            composite_score=composites[i],
            deflated_sharpe=None if dsr_by_survivor is None else dsr_by_survivor[i],
        )
        for i, (mask, m) in enumerate(survivors)
    ]
    # ENFORCE the unique-mask precondition the total order depends on
    # (DEC-RANKING-TIEBREAK-001): the deterministic tie-break keys on ``mask``, so a
    # DUPLICATE mask would silently reintroduce the input-order-dependent ordering the
    # tie-break exists to remove (a caller — ``engine.sweep_run`` / ``sweep_driver`` —
    # passes the sweep's ``(mask, metrics)`` pairs VERBATIM with no dedup). A mask is
    # the per-(instrument, TF) combo identity and MUST be unique within one
    # leaderboard; detect a violation here and FAIL LOUD, converting latent silent
    # nondeterminism into a deterministic, auditable error rather than an
    # order-dependent ranking. (Checked over the gate SURVIVORS — the only rows that
    # reach the sort; a duplicate among gate-rejected combos is irrelevant to the
    # ordering and never raised. The guard body is the SHARED
    # ``ensure_unique_survivor_masks`` so the bounded rank raises byte-identically.)
    ensure_unique_survivor_masks([rc.mask for rc in ranked])
    # Deterministic TOTAL order: primary key is the composite score DESCENDING, with
    # the unique ``mask`` ASCENDING as the final tie-break, so the key
    # ``(-composite_score, mask)`` is a strict total order — two distinct survivors can
    # never compare equal. A bare ``key=composite_score, reverse=True`` left tied
    # scores in INPUT order (``list.sort`` is stable), so permuting the candidate
    # enumeration permuted the leaderboard for any score tie — a violation of
    # idempotence/reproducibility (Golden Rules #4/#7). The negation orders the score
    # descending while keeping ``mask`` ascending in ONE ``reverse=False`` sort. The
    # sort key is always finite: a non-finite metric field reaches ``_zscores``, but
    # the ``[-_Z_CLAMP, +_Z_CLAMP]`` min/max clamp maps any non-finite z to a finite
    # bound, so every composite — and thus ``-composite_score`` — is finite, never
    # ``NaN`` (a ``NaN`` key would make ``list.sort`` itself nondeterministic).
    ranked.sort(key=lambda rc: (-rc.composite_score, rc.mask))
    return ranked


__all__ = [
    "GATE_CONSECUTIVE_LOSSES",
    "GATE_MAX_DRAWDOWN",
    "GATE_MIN_TRADES",
    "GATE_NON_FINITE_INPUT",
    "GATE_PROFIT_FACTOR",
    "GATE_REASON_ORDER",
    "GATE_WIN_RATE_WLR",
    "HARD_GATES_RECHECK_MARKED",
    "HARD_GATES_RECHECK_OK",
    "MIN_TRADES",
    "N_COMPOSITE_TERMS",
    "OverfitGateConfig",
    "RankedCombo",
    "TemporalGateInput",
    "composite_scores",
    "composite_term_inputs",
    "dsr_z_contributions",
    "ensure_unique_survivor_masks",
    "gate_reasons",
    "overfit_gate_positions",
    "passes_gates",
    "persisted_hard_gates_recheck",
    "rank_combos",
]
