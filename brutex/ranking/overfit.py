"""Stage-3 overfit gate — multiple-testing Deflated-Sharpe deflation (DEC-RANKING-OVERFIT-001).

The 6th rejection gate + its composite ranking term promised (but deferred) by the
``brutex.ranking`` docstring's "Stage-3 lockbox/overfit" slot. It deflates a
leaderboard's Sharpe ratios for **selection bias across the trials that were
tried** (Bailey & López de Prado 2014, the Deflated Sharpe Ratio), so a combo
whose Sharpe is no better than the *expected maximum* of ``N`` zero-edge trials is
rejected as data-snooped, and the survivors are down-ranked by how far their DSR
clears that bar.

It is a **pure** module: standard library + the closed-form DSR engine in
:mod:`brutex.metrics.deflated_sharpe` (itself stdlib-only). It deliberately does
**not** import :mod:`brutex.metrics.cascade` / ``numpy`` — keeping the whole
``brutex.ranking`` import graph free of the heavier statistics stack so its strict
per-module 100%-coverage gate stays stable (CLAUDE.md §11 COMMON; the same
import-discipline ``brutex.engine.sweep_run`` documents). The DSR is computed by
the *same* Bailey-LdP engine the Stage-4 cascade's filter #2 uses, so the two
surfaces can never drift (COMMON).

Honest envelope (Golden Rule #10) — DSR is the ONLY scalar-computable overfit control
------------------------------------------------------------------------------------
Of the Stage-4 cascade's filters (DSR / CPCV / SPA-Reality-Check / PBO /
Romano-Wolf / WFO / Lockbox / Monte-Carlo — :mod:`brutex.metrics.cascade`), the
Deflated Sharpe Ratio is the **only** member computable from a leaderboard's
*scalars* — each combo's observed Sharpe, the per-(instrument, TF) trial count
``N``, the cross-trial Sharpe variance ``V``, and the combo's own skew/kurtosis.
Every other filter needs each candidate's real per-period **return series**, which
the persisted :class:`~brutex.engine.results_store.ResultRow` does not carry (it
stores scalar metrics, not return paths). Reconstructing or synthesising those
series from win/loss aggregates would **fabricate** statistical significance and
is FORBIDDEN (Golden Rule #1). The full cascade is reachable only via a future
return-series sidecar adapter (the same adapter ``cascade.py`` already documents as
out of scope); this module is its scalar-only DSR subset, not a replacement.

``N`` is authoritative only INLINE — or via the rank-time-persisted sidecar
---------------------------------------------------------------------------
``TRACK1_BACKTEST_SPEC.md`` §"Stage 4" (lines 383-385) pins ``N`` to the
**per-(instrument, TF) leaderboard trial count** (``N <= 1e7``), and warns that a
wrong ``N`` corrupts the threshold (pool-wide ``N=1e12`` ⇒ an impossible Sharpe-23
bar; per-leaderboard ``N=1e7`` ⇒ a realistic Sharpe 2-3 bar). The correct ``N`` is
``len(candidates)`` — the combos a sweep actually backtested for one tuple — and it
is known **only at ranking time**: after persistence only the ``top_k`` finalists
survive and ``N`` is gone *from the rows*. A post-store re-deflation against
``N = len(rows)`` would deflate against far too few trials and stamp a 0.95+
"overfit-cleared" verdict on noise — the precise dishonesty the DSR exists to
prevent. So the gate runs **inline in** :func:`brutex.ranking.rank_combos`
(consumed by W2-a's ``run_persisted_sweep``); W2-a's persisted ``ResultsStore``
output is, by construction, the overfit-gated leaderboard.

**The ONE valid post-persistence ``N`` (ADR-0008 / DEC-TRIAL-STATS-CONSUME-001):**
the rank-time-recorded trial counts are now THEMSELVES persisted — each sweep
tuple writes a ``*.trials.json`` sidecar at flush time
(:mod:`brutex.engine.trial_stats`), aggregated per (instrument, TF) into the
cascade manifest's frozen ``leaderboard_trials`` records and consumed by the
Stage-4 cascade's filter #2 as
:class:`~brutex.metrics.cascade.DsrTrialStats` (``dsr_n_source:
"leaderboard"``). That path replays the rank-time ``N``, it does not re-derive
one — re-deriving ``N`` from persisted rows (``len(rows)``) remains FORBIDDEN
and is never offered here.

Distribution + dispersion assumptions (state them, don't over-claim significance)
---------------------------------------------------------------------------------
* **Per-trade i.i.d. Sharpe.** The observed Sharpe is the per-trade,
  non-annualised, zero-risk-free ratio (:func:`brutex.metrics.compute.compute_metrics`),
  with ``sample_length = total_trades``. The PSR ``sqrt(n - 1)`` asymptotics assume
  i.i.d. returns; per-trade returns from a single-position intraday strategy can be
  mildly serially dependent, which slightly inflates the effective track length —
  a known limitation of per-trade PSR, not a bug.
* **Real moments, Gaussian only as a flagged fallback.** Each combo's own
  ``skewness`` + (excess) ``kurtosis`` from :class:`~brutex.metrics.compute.ResultMetrics`
  are used (the excess kurtosis is converted to the non-excess form the PSR radicand
  expects via ``+ GAUSSIAN_KURTOSIS``). Only when a combo's moment is mathematically
  undefined for its sample (``None``) does the gate fall back to the config's
  Gaussian default — so the gate still applies rather than silently skipping. A
  Gaussian assumption is **anti-conservative** for the typically negatively-skewed,
  fat-tailed return series, so real moments are always preferred.
* **Abstain, never fabricate.** When the pool cannot be deflated — fewer than two
  finite Sharpes, or zero cross-trial Sharpe variance (all trials identical) — the
  expected-maximum benchmark ``SR_0`` collapses to ``0`` and the deflation
  vanishes. Rather than report the resulting un-deflated probability as an
  "overfit-cleared" pass, the gate **abstains**: it neither certifies nor rejects,
  the 6th gate is a no-op, and the DSR ranking term is dropped (the leaderboard
  falls back to its seven single-result terms). For a real sweep (thousands of
  varied trials) the pool is always deflatable; abstention only guards degenerate /
  tiny pools.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass

from brutex.metrics.deflated_sharpe import GAUSSIAN_KURTOSIS, deflated_sharpe_ratio

__all__ = [
    "OverfitGateConfig",
    "PoolDispersion",
    "deflated_sharpe_for",
    "passes_overfit_gate",
    "pool_sharpe_dispersion",
]

#: Minimum finite Sharpe observations for a meaningful cross-trial variance (and
#: thus a non-degenerate deflation). Below this the multiple-testing correction is
#: undefined and the gate abstains. Mirrors ``statistics.pvariance`` needing >= 2
#: and ``brutex.metrics.cascade._MIN_FOR_DISPERSION``.
_MIN_FINITE_SHARPES = 2


@dataclass(frozen=True, slots=True)
class OverfitGateConfig:
    """Opt-in configuration for the 6th (overfit) gate + its ranking term.

    Passing an instance to :func:`brutex.ranking.rank_combos` (or threading it
    through a sweep config) activates the gate; ``None`` keeps the legacy
    seven-term ranking byte-identical.

    Parameters
    ----------
    dsr_min
        The Deflated Sharpe probability (in ``[0, 1]``) a combo must clear to
        survive the 6th gate. Default ``0.95`` — the cascade convention
        (:class:`brutex.metrics.cascade.CascadeConfig` ``dsr_min``); a combo whose
        true Sharpe has under a 95% chance of being positive *after* the
        selection-bias correction is rejected as data-snooped.
    weight
        The composite weight of the DSR ranking term. Default ``0.05`` — the
        Stage-3 lockbox/overfit slot reserved by ``DEC-RANKING-001``. The composite
        score is used only for *relative* ordering, so adding this term (enabled
        sum ``1.05``) changes ranking power, never correctness.
    default_skew
        Gaussian fallback skewness used ONLY when a combo's own ``skewness`` is
        ``None`` (undefined for its sample). Default ``0.0`` (Gaussian).
    default_excess_kurtosis
        Gaussian fallback **excess** kurtosis used ONLY when a combo's own
        ``kurtosis`` is ``None``. Default ``0.0`` (Gaussian excess kurtosis;
        ``GAUSSIAN_KURTOSIS`` is added internally to recover the non-excess form).

    Raises
    ------
    ValueError
        If ``dsr_min`` is outside ``[0, 1]``, ``weight`` is non-finite or negative,
        or a Gaussian fallback moment is non-finite.
    """

    dsr_min: float = 0.95
    weight: float = 0.05
    default_skew: float = 0.0
    default_excess_kurtosis: float = 0.0

    def __post_init__(self) -> None:
        if not 0.0 <= self.dsr_min <= 1.0:
            raise ValueError(
                f"OverfitGateConfig.dsr_min must be a probability in [0, 1], got {self.dsr_min}"
            )
        if not math.isfinite(self.weight):
            raise ValueError(f"OverfitGateConfig.weight must be finite, got {self.weight!r}")
        if self.weight < 0.0:
            raise ValueError(f"OverfitGateConfig.weight must be >= 0, got {self.weight}")
        for name in ("default_skew", "default_excess_kurtosis"):
            value: float = getattr(self, name)
            if not math.isfinite(value):
                raise ValueError(f"OverfitGateConfig.{name} must be finite, got {value!r}")


@dataclass(frozen=True, slots=True)
class PoolDispersion:
    """The leaderboard-pool dispersion the deflation benchmark ``SR_0`` needs.

    Attributes
    ----------
    variance
        Population variance ``V`` of the pool's **finite** Sharpe ratios (the
        ``None`` / non-finite ones are excluded). ``0.0`` when fewer than two
        finite Sharpes exist (then the pool is not deflatable).
    n_finite
        How many finite Sharpe ratios the pool contained (the trials that produced
        a measurable Sharpe).
    """

    variance: float
    n_finite: int

    @property
    def deflatable(self) -> bool:
        """Whether the pool supports a non-degenerate deflation.

        ``True`` iff there are at least :data:`_MIN_FINITE_SHARPES` finite Sharpe
        ratios *and* their variance is strictly positive — i.e. ``SR_0`` will be
        ``> 0`` so the deflation actually corrects something. When ``False`` the
        gate abstains (see the module's "Abstain, never fabricate" rule).
        """
        return self.n_finite >= _MIN_FINITE_SHARPES and self.variance > 0.0


def pool_sharpe_dispersion(sharpes: Sequence[float | None]) -> PoolDispersion:
    """Return the finite-Sharpe :class:`PoolDispersion` of a leaderboard pool.

    ``None`` and non-finite Sharpes are excluded (a combo with no measurable
    risk-adjusted edge is not a deflation trial). The variance is the *population*
    variance (the convention the DSR's extreme-value benchmark assumes), or ``0.0``
    when fewer than two finite Sharpes remain.

    Parameters
    ----------
    sharpes
        The pool's per-combo Sharpe ratios (``float`` or ``None``).

    Returns
    -------
    PoolDispersion
        ``variance`` (``V``) + ``n_finite`` — feeds the deflation benchmark.
    """
    finite = [s for s in sharpes if s is not None and math.isfinite(s)]
    if len(finite) < _MIN_FINITE_SHARPES:
        return PoolDispersion(variance=0.0, n_finite=len(finite))
    return PoolDispersion(variance=statistics.pvariance(finite), n_finite=len(finite))


def deflated_sharpe_for(
    observed_sharpe: float | None,
    sample_length: int,
    skew: float | None,
    excess_kurtosis: float | None,
    *,
    n_trials: int,
    trial_sharpe_variance: float,
    cfg: OverfitGateConfig,
) -> float | None:
    """Return one combo's Deflated Sharpe probability, or ``None`` if undeflatable.

    Deflates ``observed_sharpe`` against the expected maximum Sharpe of ``n_trials``
    zero-edge trials with cross-trial variance ``trial_sharpe_variance`` (Bailey &
    LdP 2014), via the shared :func:`brutex.metrics.deflated_sharpe.deflated_sharpe_ratio`
    engine. The combo's **excess** kurtosis is converted to the non-excess form the
    PSR radicand expects (``+ GAUSSIAN_KURTOSIS``); a ``None`` skew/kurtosis falls
    back to the config's Gaussian default (see :class:`OverfitGateConfig`).

    Returns ``None`` (undeflatable — the gate treats it as a fail) when:

    * ``observed_sharpe`` is ``None`` or non-finite (no measurable Sharpe to
      deflate), or
    * the deflation is mathematically undefined for the inputs — ``sample_length``
      below 2, or the PSR estimator-variance radicand out of domain — surfaced by
      the engine as a :class:`ValueError` and caught here (it never aborts the
      leaderboard).

    Parameters
    ----------
    observed_sharpe
        ``SR_hat`` — the combo's observed (per-trade, non-annualised) Sharpe.
    sample_length
        ``n`` — the combo's return-observation count (``total_trades``).
    skew, excess_kurtosis
        The combo's own skewness + **excess** kurtosis, or ``None`` to use the
        Gaussian fallbacks.
    n_trials
        ``N`` — the per-leaderboard trial count the Sharpe is deflated against.
    trial_sharpe_variance
        ``V`` — the cross-trial Sharpe variance (from :func:`pool_sharpe_dispersion`).
    cfg
        The :class:`OverfitGateConfig` (Gaussian fallbacks).

    Returns
    -------
    float | None
        The DSR probability in ``[0, 1]``, or ``None`` when undeflatable.
    """
    if observed_sharpe is None or not math.isfinite(observed_sharpe):
        return None
    skew_value = cfg.default_skew if skew is None else skew
    excess_value = cfg.default_excess_kurtosis if excess_kurtosis is None else excess_kurtosis
    try:
        result = deflated_sharpe_ratio(
            observed_sharpe,
            n_trials=n_trials,
            trial_sharpe_variance=trial_sharpe_variance,
            sample_length=sample_length,
            skew=skew_value,
            kurtosis=excess_value + GAUSSIAN_KURTOSIS,
        )
    except ValueError:
        return None
    return result.deflated_sharpe_ratio


def passes_overfit_gate(dsr: float | None, cfg: OverfitGateConfig) -> bool:
    """Return ``True`` iff a combo clears the 6th (overfit) gate.

    A combo passes iff its DSR is defined (not ``None``) and at least ``dsr_min``
    (``>=`` keeps the boundary). A ``None`` DSR — undeflatable, see
    :func:`deflated_sharpe_for` — fails: a combo with no defensible deflated edge
    cannot rank above one with a measured, selection-bias-corrected edge (the same
    "``None`` metric fails" philosophy as the five single-result gates).

    Parameters
    ----------
    dsr
        The combo's Deflated Sharpe probability, or ``None``.
    cfg
        The :class:`OverfitGateConfig` (``dsr_min`` threshold).

    Returns
    -------
    bool
        Whether the combo survives the overfit gate.
    """
    return dsr is not None and dsr >= cfg.dsr_min
