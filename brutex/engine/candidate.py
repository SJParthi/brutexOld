"""Stage-3 capstone — one composed VALIDATED/rejected verdict per candidate combo.

The individual validators answer one question each; this composes them into the
single honest answer the operator wants: *is this combo real?* A candidate is
``validated`` iff it clears ALL of:

* the out-of-sample holdout (gates + Sharpe/drawdown tolerance, ``OOSValidation.held_up``),
* the Deflated Sharpe Ratio (selection-bias / multiple-testing — ``>= dsr_min``), and
* the CPCV pass-rate (cross-period robustness — ``>= cpcv_min``).

Every component result is carried on the verdict, so a *rejection* is attributable:
did it fail out-of-sample, fail the multiple-testing deflation, or fail across
periods? No single number hides why.

This is pure composition over the already-merged validators (``validate_out_of_sample``
+ ``deflate_sweep`` + ``cpcv_robustness``/``combo_returns``); it re-fits nothing and
adds no statistics of its own. The combo is GIVEN — the caller selects it on the
train period; the leaderboard's ``sweep_results`` supply the DSR's trial population.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from brutex.data.models import CandleBar
from brutex.engine.deflate import deflate_sweep
from brutex.engine.robustness import combo_returns, cpcv_robustness
from brutex.engine.sweep_driver import DEFAULT_START_EQUITY_PAISA
from brutex.engine.validation import OOSValidation, validate_out_of_sample
from brutex.metrics.compute import ResultMetrics
from brutex.metrics.deflated_sharpe import DeflatedSharpeResult
from brutex.strategies.types import Side
from brutex.vocabulary import ProximityParams

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from brutex.engine.reference_series import TradeReferenceStamper

#: Deflated-Sharpe acceptance floor — Bailey & López de Prado's institutional
#: convention: ``DSR >= 0.95`` = ≥95% confidence the true Sharpe is positive AFTER
#: the selection-bias correction. Tunable per run.
_DEFAULT_DSR_MIN: Final[float] = 0.95

#: CPCV pass-rate floor — robust in a MAJORITY of the held-out folds (``>= 0.5``).
#: Tunable per run.
_DEFAULT_CPCV_MIN: Final[float] = 0.5


@dataclass(frozen=True, slots=True)
class CandidateVerdict:
    """The composed out-of-sample / DSR / CPCV verdict for one candidate combo.

    Attributes
    ----------
    mask
        The u64 strategy combo.
    oos
        The out-of-sample holdout result (gates + Sharpe/drawdown tolerance).
    deflated_sharpe
        The combo's Deflated Sharpe Ratio result, or ``None`` if undefined.
    cpcv_pass_rate
        The combo's CPCV robustness pass-rate, or ``None`` if undefined.
    validated
        ``True`` iff the combo clears the holdout AND ``DSR >= dsr_min`` AND
        ``CPCV >= cpcv_min`` — the single honest "this is real" answer.
    """

    mask: int
    oos: OOSValidation
    deflated_sharpe: DeflatedSharpeResult | None
    cpcv_pass_rate: float | None
    validated: bool


def _compose_verdict(
    held_up: bool,
    deflated_sharpe: DeflatedSharpeResult | None,
    cpcv_pass_rate: float | None,
    *,
    dsr_min: float,
    cpcv_min: float,
) -> bool:
    """Compose the three layers into the single VALIDATED boolean.

    All three must hold: the holdout (``held_up``), a DEFINED DSR at/above
    ``dsr_min``, and a DEFINED CPCV pass-rate at/above ``cpcv_min``. A ``None``
    (undefined) DSR or CPCV fails — an unprovable combo is not "validated".
    """
    return (
        held_up
        and deflated_sharpe is not None
        and deflated_sharpe.deflated_sharpe_ratio >= dsr_min
        and cpcv_pass_rate is not None
        and cpcv_pass_rate >= cpcv_min
    )


def validate_candidate(
    bars: Sequence[CandleBar],
    required_mask: int,
    sweep_results: Sequence[tuple[int, ResultMetrics]],
    *,
    direction: Side,
    tf_minutes: int,
    stop_loss_paisa: int | None = None,
    take_profit_paisa: int | None = None,
    start_equity_paisa: int = DEFAULT_START_EQUITY_PAISA,
    train_frac: float = 0.7,
    dsr_min: float = _DEFAULT_DSR_MIN,
    cpcv_min: float = _DEFAULT_CPCV_MIN,
    proximity: ProximityParams | None = None,
    prior_n: int = 3,
    roll_schedule: tuple[int, ...] | None = None,
    apply_1day_mask: bool = False,
    reference_stamper: TradeReferenceStamper | None = None,
) -> CandidateVerdict:
    """Run a candidate combo through OOS + DSR + CPCV → one composed verdict.

    Parameters
    ----------
    bars, required_mask, direction, tf_minutes, stop_loss_paisa, take_profit_paisa, start_equity_paisa, proximity, prior_n
        As :func:`brutex.engine.sweep_driver.evaluate_combo`.
    sweep_results
        The ``(mask, ResultMetrics)`` leaderboard the candidate was selected from —
        the trial population for the DSR's multiple-testing correction.
    train_frac
        Train fraction for the out-of-sample holdout (default 0.7).
    dsr_min, cpcv_min
        Acceptance floors for the Deflated Sharpe Ratio and the CPCV pass-rate.
    roll_schedule, apply_1day_mask
        The ADR-0012 futures pass-throughs (DEC-NIFTY-FUTURES-SWEEP-LAUNCH-001,
        review round-2 finding 2), threaded into BOTH composed ``bar_bits``
        builds — :func:`validate_out_of_sample` AND
        :func:`~brutex.engine.robustness.combo_returns` (the CPCV producer) —
        so roll-stitched futures bars are never validated schedule-blind.
        Defaults (``None`` / ``False``) are the byte-identical spot path.
    reference_stamper
        Optional India-VIX stamper (DEC-VIX-STAMP-WIRE-001) threaded to BOTH
        re-simulations (the OOS holdout + the CPCV return series). REFERENCE
        ONLY — the composed verdict is byte-identical with or without it.

    Returns
    -------
    CandidateVerdict
        The OOS / DSR / CPCV components + the single ``validated`` answer.

    Raises
    ------
    ValueError
        Propagated from :func:`validate_out_of_sample` (empty ``bars`` / too few
        trading days / ``train_frac`` domain).
    """
    oos = validate_out_of_sample(
        bars,
        required_mask,
        direction=direction,
        tf_minutes=tf_minutes,
        stop_loss_paisa=stop_loss_paisa,
        take_profit_paisa=take_profit_paisa,
        start_equity_paisa=start_equity_paisa,
        train_frac=train_frac,
        proximity=proximity,
        prior_n=prior_n,
        roll_schedule=roll_schedule,
        apply_1day_mask=apply_1day_mask,
        reference_stamper=reference_stamper,
    )
    deflated = deflate_sweep(sweep_results).get(required_mask)
    cpcv = cpcv_robustness(
        combo_returns(
            bars,
            required_mask,
            direction=direction,
            tf_minutes=tf_minutes,
            stop_loss_paisa=stop_loss_paisa,
            take_profit_paisa=take_profit_paisa,
            start_equity_paisa=start_equity_paisa,
            proximity=proximity,
            prior_n=prior_n,
            roll_schedule=roll_schedule,
            apply_1day_mask=apply_1day_mask,
            reference_stamper=reference_stamper,
        )
    )
    return CandidateVerdict(
        mask=required_mask,
        oos=oos,
        deflated_sharpe=deflated,
        cpcv_pass_rate=cpcv,
        validated=_compose_verdict(oos.held_up, deflated, cpcv, dsr_min=dsr_min, cpcv_min=cpcv_min),
    )


__all__ = [
    "CandidateVerdict",
    "validate_candidate",
]
