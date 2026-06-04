"""Stage-3 Deflated Sharpe Ratio (DSR) over a sweep — the multiple-testing penalty.

The deepest anti-fluke defense. When you search ``N`` combos, the BEST in-sample
Sharpe is inflated by selection alone — even on pure noise, the maximum of ``N``
zero-edge trials is positive. The DSR (Bailey & López de Prado 2014) deflates each
combo's observed Sharpe against the EXPECTED MAXIMUM Sharpe of ``N`` zero-edge
trials and returns the probability the true Sharpe is positive AFTER that
correction. A combo that "wins" only because we tried millions gets a LOW DSR — the
honest "is this real, or did we just data-mine it?" number.

This wires the pure :func:`brutex.metrics.deflated_sharpe.deflated_sharpe_ratio` to
a sweep's ``(mask, ResultMetrics)`` results: it supplies the cross-trial inputs the
DSR needs — the trial count ``N`` (combos searched) and the variance ``V`` of the
trial Sharpes — which a single combo's metrics cannot carry alone.

Convention (safety-critical — Golden Rule #2): ``compute_metrics`` measures Sharpe /
skewness / kurtosis on the PER-TRADE return series (one return per closed trade), so
the DSR's ``sample_length`` is the combo's ``total_trades`` (matching the Sharpe's
frequency) and its ``kurtosis`` is the metrics' EXCESS kurtosis +
:data:`~brutex.metrics.deflated_sharpe.GAUSSIAN_KURTOSIS` (the PSR radicand uses the
non-excess ``g4``). Pure + no I/O.
"""

from __future__ import annotations

from collections.abc import Sequence
from statistics import pvariance
from typing import Final

from brutex.metrics.compute import ResultMetrics
from brutex.metrics.deflated_sharpe import (
    GAUSSIAN_KURTOSIS,
    DeflatedSharpeResult,
    deflated_sharpe_ratio,
)

#: A per-trade-return Sharpe needs ``>= 2`` trades for the DSR's track-length term
#: (``sqrt(n - 1)``); fewer → the DSR is undefined for that combo (omitted, not a
#: crash).
_MIN_DSR_TRADES: Final[int] = 2


def deflate_sweep(
    results: Sequence[tuple[int, ResultMetrics]],
) -> dict[int, DeflatedSharpeResult]:
    """Deflate every combo's Sharpe by the multiple-testing it was selected from.

    ``N`` (the trial count the candidate was selected from) is the number of combos
    in ``results``; ``V`` is the population variance of the defined trial Sharpes.
    Returns ``mask -> DeflatedSharpeResult`` for every combo whose DSR is DEFINED
    (Sharpe present, ``>= 2`` trades, and a valid moment radicand). Combos whose DSR
    is mathematically undefined are OMITTED — never a crash (Golden Rule #10: surface
    "undefined", do not fabricate a number).

    Parameters
    ----------
    results
        ``(mask, ResultMetrics)`` pairs from a sweep (e.g.
        :func:`brutex.engine.sweep_driver.sweep_masks`) — ONE
        ``(instrument, TF, direction, params)`` leaderboard's trials.

    Returns
    -------
    dict[int, DeflatedSharpeResult]
        Per-mask DSR (the probability the true Sharpe is positive AFTER the
        selection-bias correction), for the combos where it is defined.
    """
    n_trials = len(results)
    if n_trials == 0:
        return {}
    trial_sharpes = [m.sharpe_ratio for _, m in results if m.sharpe_ratio is not None]
    variance = pvariance(trial_sharpes) if trial_sharpes else 0.0
    deflated: dict[int, DeflatedSharpeResult] = {}
    for mask, m in results:
        sharpe = m.sharpe_ratio
        skew = m.skewness
        kurt = m.kurtosis
        if sharpe is None or skew is None or kurt is None or m.total_trades < _MIN_DSR_TRADES:
            continue
        try:
            deflated[mask] = deflated_sharpe_ratio(
                sharpe,
                n_trials=n_trials,
                trial_sharpe_variance=variance,
                sample_length=m.total_trades,
                skew=skew,
                kurtosis=kurt + GAUSSIAN_KURTOSIS,
            )
        except ValueError:
            # Invalid moment radicand (1 - g3·SR + (g4-1)/4·SR² <= 0) → the DSR is
            # undefined for this combo; omit it rather than crash the leaderboard.
            continue
    return deflated


__all__ = ["deflate_sweep"]
