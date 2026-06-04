"""Stage-3 CPCV robustness — does a combo work across MANY time-splits, not one?

The last Stage-3 layer. A combo can pass the out-of-sample holdout (ONE train/test
split) and survive the DSR (multiple-testing) yet still owe its whole edge to a
single lucky stretch. Combinatorial Purged Cross-Validation (López de Prado 2018,
Ch. 12) tests it across ALL ``C(16, 8) = 12,870`` held-out fold combinations: the
pass-rate is the fraction whose held-out Sharpe is positive. A genuinely robust
combo passes most folds; a one-lucky-period combo passes few.

This wires the pure :func:`brutex.metrics.cpcv.cpcv_pass_rate` to a combo's PER-TRADE
return series with a safe guard: a series too short to partition into ``n_groups``
CPCV groups (``< 16`` by default) is CPCV-UNDEFINED → returns ``None`` (not a crash,
Golden Rule #10), so the caller computes it only for combos with enough trades.

Convention (Golden Rule #2): the input ``returns`` are the combo's PER-TRADE returns
(one per closed trade, ``pnl_paisa / start_equity_paisa`` — the same series
:func:`brutex.metrics.compute.compute_metrics` uses for Sharpe), so the fold Sharpes
this computes match the rest of the pipeline. Pure + O(n) over the series + the fixed
``C(S, k)`` fold merges (O(1) per fold in the series length).
"""

from __future__ import annotations

from collections.abc import Sequence

from brutex.metrics.cpcv import (
    DEFAULT_N_GROUPS,
    DEFAULT_N_TEST_GROUPS,
    cpcv_pass_rate,
)


def cpcv_robustness(
    returns: Sequence[float],
    *,
    n_groups: int = DEFAULT_N_GROUPS,
    n_test_groups: int = DEFAULT_N_TEST_GROUPS,
    min_sharpe: float = 0.0,
) -> float | None:
    """CPCV pass-rate of a combo's per-trade returns, or ``None`` if too short.

    Parameters
    ----------
    returns
        The combo's per-trade return series (``pnl_paisa / start_equity_paisa`` per
        closed trade — the convention :func:`brutex.metrics.compute.compute_metrics`
        uses).
    n_groups, n_test_groups, min_sharpe
        CPCV structural parameters (defaults ``16`` / ``8`` / ``0.0`` — "positive
        held-out Sharpe", matching the Stage-4 walk-forward criterion).

    Returns
    -------
    float | None
        The fraction of CPCV test folds whose held-out Sharpe ``> min_sharpe`` — a
        robustness score in ``[0, 1]`` (higher = robust across more time-splits).
        ``None`` when ``len(returns) < n_groups`` — too few returns to form the
        folds, so CPCV is undefined for this combo (never raised — Golden Rule #10).
    """
    if len(returns) < n_groups:
        return None
    return cpcv_pass_rate(
        returns,
        n_groups=n_groups,
        n_test_groups=n_test_groups,
        min_sharpe=min_sharpe,
    )


__all__ = ["cpcv_robustness"]
