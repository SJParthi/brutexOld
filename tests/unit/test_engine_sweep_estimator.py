"""100% line + branch coverage of :mod:`brutex.engine.sweep_estimator`.

Pins the trials-vs-tuples distinction (the gap-hunt undercount fix), the
VWAP dead-bits flag-and-run annotation (gap-hunt #8 — NEVER excluded from
the swept vocabulary, only surfaced), and the calibrated/provisional
discriminator (ruling #14).
"""

from __future__ import annotations

import pytest
from brutex.engine.estimator_calibration import UNCALIBRATED, EstimatorCalibration
from brutex.engine.sweep_estimator import (
    CALIBRATED_BASIS,
    PROVISIONAL_BASIS,
    VWAP_DEAD_BITS,
    axes_product,
    count_true_trials,
    effective_condition_count,
    estimate_sweep_cost,
)
from brutex.vocabulary.bits import TOTAL_CONDITIONS


def test_count_true_trials_matches_hand_computed_combinatorics() -> None:
    # C(60,1) + C(60,2) = 60 + 1770 = 1830.
    assert count_true_trials([1, 2], other_axes_product=1) == 1830


def test_count_true_trials_scales_with_other_axes() -> None:
    single = count_true_trials([1], other_axes_product=1)
    assert count_true_trials([1], other_axes_product=6) == single * 6


def test_count_true_trials_exceeds_naive_tuple_product() -> None:
    """The undercount fix: trials != len(k_values) * other_axes_product for k>1."""
    naive_tuple_count = 2 * 1  # len(k_values) * other_axes_product
    trials = count_true_trials([1, 2], other_axes_product=1)
    assert trials > naive_tuple_count


def test_count_true_trials_empty_k_values_raises() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        count_true_trials([], other_axes_product=1)


def test_count_true_trials_negative_other_axes_product_raises() -> None:
    with pytest.raises(ValueError, match="other_axes_product must be >= 0"):
        count_true_trials([1], other_axes_product=-1)


def test_count_true_trials_zero_other_axes_product_is_zero_trials() -> None:
    assert count_true_trials([1, 2, 3], other_axes_product=0) == 0


def test_count_true_trials_custom_n() -> None:
    assert count_true_trials([1], other_axes_product=1, n=5) == 5


def test_effective_condition_count_index_segment_drops_vwap_bits() -> None:
    nominal, effective = effective_condition_count(segment="INDEX")
    assert nominal == TOTAL_CONDITIONS
    assert effective == TOTAL_CONDITIONS - len(VWAP_DEAD_BITS)


def test_effective_condition_count_index_segment_is_case_insensitive() -> None:
    nominal, effective = effective_condition_count(segment="index")
    assert (nominal, effective) == (TOTAL_CONDITIONS, TOTAL_CONDITIONS - 2)


def test_effective_condition_count_non_index_segment_is_unreduced() -> None:
    nominal, effective = effective_condition_count(segment="FNO")
    assert nominal == effective == TOTAL_CONDITIONS


def test_effective_condition_count_custom_n() -> None:
    assert effective_condition_count(segment="INDEX", n=10) == (10, 8)


def test_vwap_dead_bits_are_52_and_53() -> None:
    assert frozenset({52, 53}) == VWAP_DEAD_BITS


def test_axes_product_multiplies_every_axis() -> None:
    assert axes_product(2, 3, 5) == 30


def test_axes_product_no_axes_is_one() -> None:
    assert axes_product() == 1


@pytest.mark.parametrize(
    ("segment", "expect_effective_lt_nominal"),
    [("INDEX", True), ("FNO", False)],
)
def test_estimate_sweep_cost_annotates_condition_counts(
    segment: str, expect_effective_lt_nominal: bool
) -> None:
    estimate = estimate_sweep_cost(
        k_values=[1, 2],
        other_axes_product=3,
        tuple_count=6,
        top=1000,
        segment=segment,
        calibration=UNCALIBRATED,
    )
    if expect_effective_lt_nominal:
        assert estimate.effective_condition_count < estimate.nominal_condition_count
    else:
        assert estimate.effective_condition_count == estimate.nominal_condition_count


def test_estimate_sweep_cost_uses_provisional_basis_when_uncalibrated() -> None:
    estimate = estimate_sweep_cost(
        k_values=[1],
        other_axes_product=1,
        tuple_count=1,
        top=1,
        segment="INDEX",
        calibration=UNCALIBRATED,
    )
    assert estimate.estimate_basis == PROVISIONAL_BASIS


def test_estimate_sweep_cost_uses_calibrated_basis_when_calibrated() -> None:
    calibration = EstimatorCalibration(
        calibrated=True,
        seconds_per_trial=1e-6,
        bytes_per_row=100,
        sample_trials=1000,
        measured_at_utc="2026-07-08T00:00:00Z",
        source="smoke_run",
    )
    estimate = estimate_sweep_cost(
        k_values=[1],
        other_axes_product=1,
        tuple_count=1,
        top=1,
        segment="INDEX",
        calibration=calibration,
    )
    assert estimate.estimate_basis == CALIBRATED_BASIS
    assert estimate.est_seconds_provisional == estimate.trials * calibration.seconds_per_trial


def test_estimate_sweep_cost_bytes_scale_with_tuple_count_and_top() -> None:
    calibration = UNCALIBRATED
    small = estimate_sweep_cost(
        k_values=[1],
        other_axes_product=1,
        tuple_count=2,
        top=10,
        segment="FNO",
        calibration=calibration,
    )
    large = estimate_sweep_cost(
        k_values=[1],
        other_axes_product=1,
        tuple_count=4,
        top=10,
        segment="FNO",
        calibration=calibration,
    )
    assert large.est_bytes == small.est_bytes * 2


def test_estimate_sweep_cost_clamps_overflow_to_inf() -> None:
    """Adversarial-refuter finding: even the MAX-allowed calibrated
    ``seconds_per_trial`` (3600.0) can overflow ``trials * seconds_per_trial``
    to ``inf`` when ``trials`` itself (via ``other_axes_product``, unbounded
    by :func:`count_true_trials`) is astronomically large — the calibration
    file's own caps only bound one operand. This produces a silent ``inf``
    without raising (float*float overflow, not an int-to-float conversion
    failure) — must be clamped to the finite JSON-safe ceiling."""
    calibration = EstimatorCalibration(
        calibrated=True,
        seconds_per_trial=3600.0,
        bytes_per_row=100,
        sample_trials=1,
        measured_at_utc=None,
        source="x",
    )
    estimate = estimate_sweep_cost(
        k_values=[30],
        other_axes_product=10**291,
        tuple_count=1,
        top=1,
        segment="FNO",
        calibration=calibration,
    )
    assert estimate.est_seconds_provisional == 1e18


def test_estimate_sweep_cost_clamps_overflow_error_to_inf() -> None:
    """Same overflow gap, but with ``trials`` large enough that converting it
    to a Python ``float`` raises ``OverflowError`` outright (rather than
    silently producing ``inf``) — both overflow shapes must clamp the same way."""
    calibration = EstimatorCalibration(
        calibrated=True,
        seconds_per_trial=3600.0,
        bytes_per_row=100,
        sample_trials=1,
        measured_at_utc=None,
        source="x",
    )
    estimate = estimate_sweep_cost(
        k_values=[30],
        other_axes_product=10**305,
        tuple_count=1,
        top=1,
        segment="FNO",
        calibration=calibration,
    )
    assert estimate.est_seconds_provisional == 1e18


def test_estimate_sweep_cost_negative_tuple_count_raises() -> None:
    with pytest.raises(ValueError, match="tuple_count must be >= 0"):
        estimate_sweep_cost(
            k_values=[1],
            other_axes_product=1,
            tuple_count=-1,
            top=1,
            segment="FNO",
            calibration=UNCALIBRATED,
        )


def test_estimate_sweep_cost_negative_top_raises() -> None:
    with pytest.raises(ValueError, match="top must be >= 0"):
        estimate_sweep_cost(
            k_values=[1],
            other_axes_product=1,
            tuple_count=1,
            top=-1,
            segment="FNO",
            calibration=UNCALIBRATED,
        )


def test_estimate_sweep_cost_to_dict_shape() -> None:
    estimate = estimate_sweep_cost(
        k_values=[1],
        other_axes_product=1,
        tuple_count=1,
        top=1,
        segment="FNO",
        calibration=UNCALIBRATED,
    )
    assert estimate.to_dict() == {
        "trials": estimate.trials,
        "est_seconds_provisional": estimate.est_seconds_provisional,
        "est_bytes": estimate.est_bytes,
        "estimate_basis": estimate.estimate_basis,
        "nominal_condition_count": estimate.nominal_condition_count,
        "effective_condition_count": estimate.effective_condition_count,
    }
