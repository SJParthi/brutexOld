"""Composite ranking score over a per-(instrument, TF) leaderboard.

Implements the Stage-4 composite ranking score defined in
``docs/TRACK1_BACKTEST_SPEC.md`` §"Composite ranking score (Stage 4)"
(spec lines 602-618):

.. code-block:: text

    composite_score(s) =
        0.25 × normalize(s.sharpe_ratio)
      + 0.20 × normalize(s.sortino_ratio)
      + 0.15 × normalize(s.calmar_ratio)
      + 0.15 × normalize(s.profit_factor − 1.0)
      + 0.10 × normalize(s.recovery_factor)
      + 0.10 × normalize(s.win_rate × s.win_loss_ratio)
      + 0.05 × normalize(s.lockbox_sharpe)

where ``normalize(x)`` is the per-(instrument, TF) leaderboard z-score
``(x − mean) / std`` clamped to ``[-3, +3]`` (spec lines 617-618). The
weights and the clamp range are quoted verbatim from the spec; nothing
is invented (CLAUDE.md Golden Rule #1).

Because ``normalize`` is a z-score over the *leaderboard*, the score is
inherently a COLLECTION operation: a single result has no z-score in
isolation. :func:`rank_leaderboard` therefore takes the whole
leaderboard, applies the hard rejection gates (:mod:`brutex.ranking.gates`),
z-scores each scored metric across the survivors, sums the weighted
normalized terms, and returns the survivors sorted by composite
descending.

**Stage-4 lockbox conventions (DEC-RANKING-001 honest envelope):**

* ``lockbox_sharpe`` is a Stage-4 metric the spec marks "Stage 4 only,
  NULL before then" (spec line 586). When NO survivor carries a
  ``lockbox_sharpe`` (the pre-Stage-4 norm), the 7th term
  (``0.05 × normalize(lockbox_sharpe)``) is DROPPED and the remaining
  six weights are renormalized to sum to ``1.0`` (each divided by
  ``0.95``) so composites stay comparable in magnitude across results.
  When at least one survivor carries a ``lockbox_sharpe`` the full
  seven-term formula (weights summing to ``1.0`` already) is used.
* This mirrors the spec's own Day-1 note (spec line 628: "Simplified
  composite score (no lockbox term, weights normalized to sum to 1)").

**None-metric convention (disclosed, spec is silent):** a scored metric
can be ``None`` on :class:`~brutex.metrics.ResultMetrics` (undefined
Sharpe/Sortino/Calmar/recovery_factor when variance / drawdown is zero,
``win_loss_ratio`` when there are no losers). For the z-score:

* The ``None`` value is EXCLUDED from that metric's ``mean`` / ``std``
  (it would otherwise have no numeric value to contribute), so the
  distribution is computed only over the survivors where the metric is
  defined.
* The result with the ``None`` then receives the **worst clamp,
  ``-3.0``**, for that one term — the conservative choice: an undefined
  risk-adjusted ratio must not be rewarded over a defined positive one.
* Exception: when NO survivor defines the metric (the whole column is
  ``None``) there is no distribution to be "worst" against, so the term
  is a neutral ``0.0`` for everyone (ranking-neutral; avoids a phantom
  negative floor on every composite).

This convention is disclosed here and in MIGRATION #307 per Golden Rule
#10. Note ``win_loss_ratio is None`` means *no losing trades* (a strong
result), but it is still scored ``-3`` on the lone ``win_rate ×
win_loss_ratio`` term; the gates already let such a result through, and
the dominant Sharpe/Sortino/Calmar terms carry it on merit.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final

from brutex.metrics import ResultMetrics
from brutex.ranking.gates import RejectionResult, passes_gates

__all__ = [
    "CLAMP_HI",
    "CLAMP_LO",
    "RankInput",
    "RankedResult",
    "rank_leaderboard",
]

#: Lower z-score clamp (spec line 618).
CLAMP_LO: Final[float] = -3.0
#: Upper z-score clamp (spec line 618).
CLAMP_HI: Final[float] = 3.0

#: The six non-lockbox term weights (spec lines 608-613), keyed by a
#: stable term name. Order is preserved for deterministic iteration.
_BASE_WEIGHTS: Final[dict[str, float]] = {
    "sharpe": 0.25,
    "sortino": 0.20,
    "calmar": 0.15,
    "profit_factor": 0.15,
    "recovery_factor": 0.10,
    "win_quality": 0.10,
}
#: The Stage-4 lockbox term weight (spec line 614).
_LOCKBOX_WEIGHT: Final[float] = 0.05


@dataclass(frozen=True, slots=True)
class RankInput:
    """One leaderboard entry to be ranked.

    Attributes
    ----------
    result_id
        Opaque, caller-owned identifier (the reproducibility hash, a row
        index, etc.). Carried through untouched so the caller can map a
        :class:`RankedResult` back to its source row. Used as the final
        tie-breaker so ranking is deterministic.
    metrics
        The per-result :class:`~brutex.metrics.ResultMetrics` row (the
        single canonical metrics type — Golden Rule #11; no parallel
        metrics type is defined here).
    lockbox_sharpe
        The Stage-4 out-of-sample (lockbox) Sharpe, or ``None`` when not
        yet computed. Feeds both the overfit gate (spec line 600) and
        the 7th composite term (spec line 614).
    """

    result_id: str
    metrics: ResultMetrics
    lockbox_sharpe: float | None = None


@dataclass(frozen=True, slots=True)
class RankedResult:
    """A survivor of the gates with its composite score and rank.

    Attributes
    ----------
    result_id
        The opaque id carried from the :class:`RankInput`.
    composite_score
        The weighted sum of normalized terms (spec lines 607-615).
        Higher is better.
    rank
        1-based rank within the leaderboard (1 = best). Ties in
        ``composite_score`` are broken by ``result_id`` ascending, so the
        ranking is total and deterministic.
    rejection
        The :class:`~brutex.ranking.gates.RejectionResult` for this
        survivor (always passing — ``rejection.passed is True``); carried
        so callers can see whether the overfit gate was evaluated.
    normalized_terms
        The per-term clamped z-score *before* weighting, keyed by term
        name (``"sharpe"``, ``"sortino"``, ``"calmar"``,
        ``"profit_factor"``, ``"recovery_factor"``, ``"win_quality"``,
        and ``"lockbox"`` only when the lockbox term is active). Exposed
        for auditability / dashboards.
    """

    result_id: str
    composite_score: float
    rank: int
    rejection: RejectionResult
    normalized_terms: dict[str, float] = field(default_factory=dict)


def _raw_term_value(inp: RankInput, term: str) -> float | None:
    """Return the raw (pre-normalization) value of a scored term.

    Maps each composite term name to its source expression on the
    :class:`~brutex.metrics.ResultMetrics` (spec lines 608-614). Returns
    ``None`` when the underlying metric is undefined (the None-metric
    convention in the module docstring then applies).

    Parameters
    ----------
    inp
        The leaderboard entry.
    term
        One of the keys in :data:`_BASE_WEIGHTS` or ``"lockbox"``.

    Returns
    -------
    float | None
        The raw term value, or ``None`` when undefined.
    """
    m = inp.metrics
    if term == "sharpe":
        return m.sharpe_ratio
    if term == "sortino":
        return m.sortino_ratio
    if term == "calmar":
        return m.calmar_ratio
    if term == "profit_factor":
        # normalize(profit_factor − 1.0) (spec line 611). None iff no losers.
        return None if m.profit_factor is None else m.profit_factor - 1.0
    if term == "recovery_factor":
        return m.recovery_factor
    if term == "win_quality":
        # normalize(win_rate × win_loss_ratio) (spec line 613). win_rate is
        # always defined; win_loss_ratio is None iff no losers.
        return None if m.win_loss_ratio is None else m.win_rate * m.win_loss_ratio
    # term == "lockbox" (spec line 614).
    return inp.lockbox_sharpe


def _z_scores(values: list[float | None]) -> list[float]:
    """Z-score a column across the leaderboard, clamped to ``[-3, +3]``.

    Implements ``normalize(x) = clamp((x − mean) / std, -3, +3)`` (spec
    lines 617-618) over the *defined* values:

    * ``mean`` / ``std`` are population statistics over the non-``None``
      entries only (the None-metric convention, module docstring).
    * ``std == 0`` (every defined value identical, incl. a single
      defined value) → every defined entry normalizes to ``0.0`` (spec
      "when std==0 → 0 for all").
    * a ``None`` entry normalizes to the worst clamp ``-3.0``.

    Parameters
    ----------
    values
        One column of raw term values across the leaderboard, with
        ``None`` for entries where the metric is undefined.

    Returns
    -------
    list[float]
        The clamped z-score for each entry, in input order.
    """
    defined = [v for v in values if v is not None]
    if not defined:
        # No survivor defines this metric → the whole column is
        # informationless; every entry contributes a neutral 0.0 (a
        # uniform penalty would be ranking-neutral, but 0.0 keeps the
        # composite magnitude honest and avoids a phantom -0.15 floor).
        return [0.0 for _ in values]
    mean = sum(defined) / len(defined)
    variance = sum((v - mean) ** 2 for v in defined) / len(defined)
    if variance == 0.0:
        # All defined values identical → z-score 0 for them; None → worst.
        return [CLAMP_LO if v is None else 0.0 for v in values]
    std = variance**0.5
    out: list[float] = []
    for v in values:
        if v is None:
            out.append(CLAMP_LO)
            continue
        z = (v - mean) / std
        out.append(max(CLAMP_LO, min(CLAMP_HI, z)))
    return out


def rank_leaderboard(results: Sequence[RankInput]) -> list[RankedResult]:
    """Gate, score and rank a per-(instrument, TF) leaderboard.

    The full Stage-4 pipeline (spec lines 589-618):

    1. **Gate** — drop every entry that trips a hard rejection gate
       (:func:`~brutex.ranking.gates.passes_gates`, spec lines 589-600).
       The overfit gate uses each entry's ``lockbox_sharpe``.
    2. **Normalize** — z-score each scored metric *across the survivors*
       (the leaderboard), clamped ``[-3, +3]`` (spec lines 617-618).
    3. **Compose** — sum the weighted normalized terms (spec lines
       607-615). The lockbox term is included only when at least one
       survivor carries a ``lockbox_sharpe``; otherwise it is dropped and
       the six remaining weights are renormalized to sum to ``1.0``
       (DEC-RANKING-001, module docstring).
    4. **Sort** — survivors descending by ``composite_score``, ties
       broken by ``result_id`` ascending (deterministic total order).

    Parameters
    ----------
    results
        The leaderboard entries. ALL entries MUST belong to the same
        ``(instrument, TF)`` bucket — the z-score is defined per bucket
        (spec lines 617-618). Mixing buckets is the caller's bug; this
        function does not (and cannot) detect it. May be empty.

    Returns
    -------
    list[RankedResult]
        The survivors, ranked best-first (``rank == 1`` is the top
        result). Gate failures are omitted entirely. Empty when no entry
        survives the gates.
    """
    survivors: list[RankInput] = []
    rejections: list[RejectionResult] = []
    for inp in results:
        outcome = passes_gates(inp.metrics, lockbox_sharpe=inp.lockbox_sharpe)
        if outcome.passed:
            survivors.append(inp)
            rejections.append(outcome)

    if not survivors:
        return []

    # The lockbox term is active iff at least one survivor carries it.
    lockbox_active = any(s.lockbox_sharpe is not None for s in survivors)

    if lockbox_active:
        weights = {**_BASE_WEIGHTS, "lockbox": _LOCKBOX_WEIGHT}
    else:
        # Drop the lockbox term; renormalize the six base weights to 1.0
        # (each ÷ Σ base = ÷ 0.95) so composites stay comparable.
        base_sum = sum(_BASE_WEIGHTS.values())
        weights = {k: w / base_sum for k, w in _BASE_WEIGHTS.items()}

    # Z-score each active term column across the survivors.
    normalized_columns: dict[str, list[float]] = {}
    for term in weights:
        raw = [_raw_term_value(s, term) for s in survivors]
        normalized_columns[term] = _z_scores(raw)

    # Compose per-survivor weighted sums.
    scored: list[RankedResult] = []
    for idx, (inp, rej) in enumerate(zip(survivors, rejections, strict=True)):
        terms = {term: normalized_columns[term][idx] for term in weights}
        composite = sum(weights[term] * terms[term] for term in weights)
        scored.append(
            RankedResult(
                result_id=inp.result_id,
                composite_score=composite,
                rank=0,  # filled after sorting
                rejection=rej,
                normalized_terms=terms,
            )
        )

    # Sort: composite descending, then result_id ascending (deterministic).
    scored.sort(key=lambda r: (-r.composite_score, r.result_id))

    # Stamp 1-based ranks. RankedResult is frozen → rebuild with rank set.
    return [
        RankedResult(
            result_id=r.result_id,
            composite_score=r.composite_score,
            rank=position,
            rejection=r.rejection,
            normalized_terms=r.normalized_terms,
        )
        for position, r in enumerate(scored, start=1)
    ]
