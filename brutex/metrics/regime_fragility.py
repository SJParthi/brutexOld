"""Regime-fragility cascade gate law (DEC-REGIME-FRAGILITY-GATE-001).

The default-OFF cascade filter #11 law: a strategy whose measured edge is
CONCENTRATED in one regime stratum, or whose expectancy FLIPS SIGN across
structural eras, is fragile — its headline numbers ride one market state.
The law judges a candidate's per-trade P&L series stratified along the
DEC-REGIME-TAXONOMY-001 axes this leaf consumes
(:mod:`brutex.metrics.regime_taxonomy` — a pure stdlib leaf, so the import
keeps this module pure + stdlib-only):

* the TREND axis — the session-lens trend token of each trade's ENTRY
  session, mapped through the taxonomy's
  :func:`~brutex.metrics.regime_taxonomy.trend_stratum_from_lens_token`
  (the UNKNOWN bucket is counted in the stats but EXCLUDED from the laws —
  an unlabeled session is never evidence for or against fragility);
* the ERA axis — the taxonomy's structural-era label of each trade's ENTRY
  date (:func:`~brutex.metrics.regime_taxonomy.era_for_date`).

An axis whose evidence is absent (``None``) — or whose label series does
not align with the P&L series — is SKIPPED and counted in the stats,
never fabricated and never a reject (Golden Rule #1). A candidate with NO
judgeable axis is a VISIBLE abstain (``"insufficient_strata"``), never a
rejection.

Honest deferrals (Golden Rule #10)
----------------------------------
* The VOL/VIX-band axis is NOT evaluated here: the cascade's candidate
  re-simulations are UNSTAMPED trade streams by design (the §2 Rule 3
  five-site law — stamps are provably discarded at the CandidateInput
  scalar conversion, DEC-VIX-STAMP-WIRE-001 #1645) and no INDIAVIX read
  exists at this seam — the SAME named follow-up as the extreme-vol
  filter #10's VIX arm (DEC-REGIME-MUST-SURVIVE-STRATUM-001).
* Variance-explosion-in-high-VIX is already covered for the extreme-vol
  overlay by filter #10 (its variance-ratio arm) — deliberately NEVER
  duplicated here.
* The concentration law's numerator AND denominator span every
  NON-EXCLUDED stratum, supported or not — only the axis-judgeability
  gate demands >= 2 SUPPORTED strata; the concentrated stratum itself
  need NOT be supported. Both directions of that choice are real: a
  below-:data:`MIN_STRATUM_TRADES` stratum (even a single windfall
  trade) can BE the > 80% holder and take the
  ``regime_concentration:<axis>:<stratum>`` attribution, and an
  unsupported stratum's wins inflate the positive-P&L denominator,
  DILUTING the supported strata's shares below the bar. Scoping the
  numerator/denominator to SUPPORTED strata only is a named candidate
  change for the ACTIVATION DEC, when the operator calibrates the
  thresholds — the current behavior is explicitly test-pinned
  (``test_concentration_attributes_to_unsupported_stratum_pinned``).

The thresholds are CONSERVATIVE defaults pending operator calibration —
they gate nothing until ``CascadeConfig.regime_fragility`` is flipped ON
(its own DEC). Pure + stdlib-only: no lake I/O, no engine imports,
deterministic, total on degenerate input (never raises on empty / short /
misaligned series).
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal

from brutex.metrics.regime_taxonomy import (
    TAXONOMY_VERSION,
    TrendStratum,
    trend_stratum_from_lens_token,
)

#: One stratum may contribute at most this share of an axis's summed
#: POSITIVE P&L (strict ``>`` — exactly at the share still passes). The
#: 0.80 bar mirrors the results-depth ``one_regime_wonder`` 80% convention
#: (DEC-SWEEP-RESULTS-DEPTH-001 regime lens) — a conservative default
#: pending operator calibration.
REGIME_CONCENTRATION_MAX: Final[float] = 0.80

#: Exact-rational form of :data:`REGIME_CONCENTRATION_MAX` for the
#: concentration compare (integer cross-multiplication over the int paisa
#: sums — no float share is ever compared, so the 80.0% boundary is exact).
_CONCENTRATION_NUM: Final[int] = 4
_CONCENTRATION_DEN: Final[int] = 5

#: Minimum trades for a stratum to count as SUPPORTED (evidence-bearing) —
#: the DSR ``MIN_TRADES = 30`` statistical-floor precedent (and the
#: extreme-vol filter #10's own ``MIN_STRATUM_TRADES``); own constant so
#: the two gates calibrate independently.
MIN_STRATUM_TRADES: Final[int] = 30

#: An axis is JUDGEABLE only with at least this many supported strata —
#: one supported stratum has nothing to be concentrated AGAINST.
_MIN_JUDGEABLE_STRATA: Final[int] = 2

#: The trend-axis stratum EXCLUDED from the laws (counted in stats only).
_UNKNOWN_LABEL: Final[str] = TrendStratum.UNKNOWN.value

#: Axis name tokens (stable wire vocabulary inside reasons/stats).
_AXIS_TREND: Final[str] = "trend"
_AXIS_ERA: Final[str] = "era"

#: The verdict vocabulary (stable wire tokens).
FragilityStatus = Literal["ok", "fail", "insufficient_strata"]

#: The honest-ABSTAIN member of :data:`FragilityStatus` — recorded, never a
#: reject; ``"insufficient_strata"`` is also minted by the cascade's
#: no-input arm (``brutex.metrics.cascade``). Exported so read-time
#: consumers (the /results ``regime_robust`` preset) derive their
#: vocabulary from THIS producer module — one vocabulary home
#: (CLAUDE.md §11 COMMON).
ABSTAIN_STATUSES: Final[tuple[str, ...]] = ("insufficient_strata",)


@dataclass(frozen=True, slots=True)
class FragilityVerdict:
    """The measured regime-fragility verdict for one candidate.

    ``status`` is the decision token; ``reasons`` carries the machine
    tokens (``"regime_concentration:<axis>:<stratum>"`` / ``"era_break"``);
    ``stats`` carries the per-axis per-stratum measured numbers
    (``trades`` / ``expectancy_paisa`` / ``positive_pnl_share``) plus
    ``taxonomy_version``, so an abstain is still fully visible evidence;
    ``detail`` is a short human string.
    """

    status: FragilityStatus
    reasons: tuple[str, ...]
    stats: dict[str, object]
    detail: str


def _axis_groups(pnls_paisa: Sequence[int], labels: Sequence[str]) -> dict[str, list[int]]:
    """Group *pnls_paisa* by aligned stratum *labels* (callers pre-check length)."""
    groups: dict[str, list[int]] = {}
    for pnl, label in zip(pnls_paisa, labels, strict=True):
        groups.setdefault(label, []).append(pnl)
    return groups


def _evaluate_axis(
    axis: str,
    pnls_paisa: Sequence[int],
    labels: Sequence[str],
    *,
    excluded: frozenset[str],
) -> tuple[dict[str, object], list[str], bool]:
    """Evaluate one axis; return ``(axis_stats, reasons, judgeable)``.

    ``excluded`` strata are counted in the stats but never enter the laws
    (the trend axis's UNKNOWN bucket). ``judgeable`` is True iff the axis
    carries >= 2 NON-excluded strata with support >= MIN_STRATUM_TRADES.
    """
    groups = _axis_groups(pnls_paisa, labels)
    eligible = {label: g for label, g in groups.items() if label not in excluded}
    pos_by_label = {label: sum(p for p in g if p > 0) for label, g in eligible.items()}
    total_pos = sum(pos_by_label.values())
    supported = [label for label, g in eligible.items() if len(g) >= MIN_STRATUM_TRADES]
    judgeable = len(supported) >= _MIN_JUDGEABLE_STRATA

    strata_stats: dict[str, object] = {}
    for label in sorted(groups):
        g = groups[label]
        share: float | None = None
        if label in eligible and total_pos > 0:
            share = pos_by_label[label] / total_pos
        strata_stats[label] = {
            "trades": len(g),
            # Every group is non-empty by construction (built by appending
            # each aligned trade), so the mean is always defined.
            "expectancy_paisa": float(statistics.fmean(g)),
            "positive_pnl_share": share,
        }

    reasons: list[str] = []
    # LAW regime_concentration (any axis): one stratum's share of the
    # summed POSITIVE P&L strictly exceeds 80% while the axis is judgeable.
    # Exact integer cross-multiplication — the 80.0% boundary passes.
    if total_pos > 0 and judgeable:
        for label in sorted(eligible):
            if pos_by_label[label] * _CONCENTRATION_DEN > total_pos * _CONCENTRATION_NUM:
                reasons.append(f"regime_concentration:{axis}:{label}")
    # LAW era_break (era axis only): among SUPPORTED era strata, expectancy
    # flips sign — at least one > 0 and at least one < 0 (zero is neither).
    if axis == _AXIS_ERA and supported:
        means = [statistics.fmean(eligible[label]) for label in supported]
        if any(m > 0 for m in means) and any(m < 0 for m in means):
            reasons.append("era_break")

    axis_stats: dict[str, object] = {
        "skipped": False,
        "supported_strata": len(supported),
        "strata": strata_stats,
    }
    return axis_stats, reasons, judgeable


def _skipped_axis(reason: str) -> dict[str, object]:
    """The stats entry for an axis that could not be evaluated."""
    return {"skipped": True, "reason": reason}


def evaluate_fragility(
    pnls_paisa: Sequence[int],
    *,
    trend_tokens: Sequence[str] | None,
    era_labels: Sequence[str] | None,
) -> FragilityVerdict:
    """Evaluate the regime-fragility law over one candidate's trade series.

    Decision law (pure, deterministic, never raises on degenerate input):

    * Each present axis (``trend_tokens`` / ``era_labels``) is evaluated;
      a ``None`` axis — or one whose length does not match *pnls_paisa* —
      is SKIPPED and counted in the stats (an honest totality choice: the
      cascade's ``CandidateInput`` guard refuses misaligned evidence at
      construction, so a mismatch here is out-of-band input, degraded not
      raised).
    * FAIL iff any reason fires: ``regime_concentration:<axis>:<stratum>``
      (one stratum > 80% of the axis's summed positive P&L while the axis
      has >= 2 strata with support >= :data:`MIN_STRATUM_TRADES`) or
      ``era_break`` (supported era strata with opposite-sign expectancy).
    * ``"insufficient_strata"`` iff NO axis had >= 2 supported strata —
      a VISIBLE abstain, never a reject.
    * Else ``"ok"``.

    Parameters
    ----------
    pnls_paisa
        Per-trade net P&L (paisa, signed), trade order.
    trend_tokens
        Per-trade session-lens trend token of the ENTRY session (the
        taxonomy maps it; junk degrades to UNKNOWN — excluded from laws),
        or ``None`` when unavailable.
    era_labels
        Per-trade structural-era label of the ENTRY date
        (:func:`~brutex.metrics.regime_taxonomy.era_for_date` ``.label``),
        or ``None`` when unavailable.

    Returns
    -------
    FragilityVerdict
        The measured verdict (status + reasons + the numbers behind them).
    """
    axes: dict[str, object] = {}
    reasons: list[str] = []
    any_judgeable = False

    def run_axis(axis: str, labels: Sequence[str] | None, *, excluded: frozenset[str]) -> None:
        nonlocal any_judgeable
        if labels is None:
            axes[axis] = _skipped_axis("no_input")
            return
        if len(labels) != len(pnls_paisa):
            axes[axis] = _skipped_axis("length_mismatch")
            return
        axis_stats, axis_reasons, judgeable = _evaluate_axis(
            axis, pnls_paisa, labels, excluded=excluded
        )
        axes[axis] = axis_stats
        reasons.extend(axis_reasons)
        any_judgeable = any_judgeable or judgeable

    trend_labels = (
        None
        if trend_tokens is None
        else tuple(trend_stratum_from_lens_token(t).value for t in trend_tokens)
    )
    run_axis(_AXIS_TREND, trend_labels, excluded=frozenset({_UNKNOWN_LABEL}))
    run_axis(_AXIS_ERA, era_labels, excluded=frozenset())

    stats: dict[str, object] = {"taxonomy_version": TAXONOMY_VERSION, "axes": axes}
    if reasons:
        return FragilityVerdict(
            status="fail",
            reasons=tuple(reasons),
            stats=stats,
            detail="regime fragility: " + ", ".join(reasons),
        )
    if not any_judgeable:
        return FragilityVerdict(
            status="insufficient_strata",
            reasons=(),
            stats=stats,
            detail=(
                f"no axis with >= 2 strata at support >= {MIN_STRATUM_TRADES} "
                "trade(s) — abstain, not rejected"
            ),
        )
    return FragilityVerdict(
        status="ok",
        reasons=(),
        stats=stats,
        detail="no regime-fragility law fired over the judgeable axes",
    )


__all__ = [
    "ABSTAIN_STATUSES",
    "MIN_STRATUM_TRADES",
    "REGIME_CONCENTRATION_MAX",
    "TAXONOMY_VERSION",
    "FragilityStatus",
    "FragilityVerdict",
    "evaluate_fragility",
]
