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
    SWEPT_CONDITIONS,
    VWAP_DEAD_BITS,
    axes_product,
    count_true_trials,
    effective_condition_count,
    estimate_sweep_cost,
)
from brutex.vocabulary.bits import IMPLEMENTED_MASK


def test_swept_conditions_is_the_implemented_count() -> None:
    """The estimator sizes ``C(n, k)`` off the IMPLEMENTED count — ALL 74 on
    the retention layout (the VWAP pair 52/53 rejoined per
    DEC-VOCAB-VWAP-RETAIN-001, MIGRATION.md #2206; the express train grew
    the declared width to 74): ``conditions=None`` resolves to
    ``default_condition_bits()`` == every implemented bit."""
    assert SWEPT_CONDITIONS == IMPLEMENTED_MASK.bit_count() == 74


def test_count_true_trials_matches_hand_computed_combinatorics() -> None:
    # C(74,1) + C(74,2) = 74 + 2701 = 2775 (the IMPLEMENTED 74-bit set —
    # the retention layout, DEC-VOCAB-VWAP-RETAIN-001 #2206).
    assert count_true_trials([1, 2], other_axes_product=1) == 2775


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


def test_effective_condition_count_index_segment_vwap_dead_bits_subtract_two() -> None:
    """The gap-hunt-#8 INDEX-segment dead-bit annotation SELF-REVIVED at the
    retention (DEC-VOCAB-VWAP-RETAIN-001, MIGRATION.md #2206): the VWAP bits
    52/53 are IMPLEMENTED again, yet structurally dead on zero-volume INDEX
    spot candles — so the INDEX subtraction is TWO (the pre-wipe direction:
    effective == nominal - 2; flag-and-run, the vocabulary itself is never
    reduced)."""
    assert VWAP_DEAD_BITS & frozenset(
        i for i in range(IMPLEMENTED_MASK.bit_length()) if IMPLEMENTED_MASK >> i & 1
    ) == frozenset({52, 53})
    nominal, effective = effective_condition_count(segment="INDEX")
    assert (nominal, effective) == (SWEPT_CONDITIONS, SWEPT_CONDITIONS - 2)


def test_effective_condition_count_subtracts_only_implemented_dead_bits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Teeth for the ∩-IMPLEMENTED law: on the 74/74 retention layout both
    dead bits are implemented, so a drop-the-intersection mutant (bare
    ``len(VWAP_DEAD_BITS)``) is observationally EQUIVALENT — simulate a
    layout with bit 52 unimplemented, where the subtraction must read ONE,
    never the bare two (DEC-VOCAB-VWAP-RETAIN-001 #2206 teeth preservation).
    """
    from brutex.engine import sweep_estimator

    monkeypatch.setattr(
        sweep_estimator,
        "_IMPLEMENTED_BIT_SET",
        sweep_estimator._IMPLEMENTED_BIT_SET - {52},
    )
    nominal, effective = effective_condition_count(segment="INDEX", n=10)
    assert (nominal, effective) == (10, 9)


def test_effective_condition_count_index_segment_is_case_insensitive() -> None:
    assert effective_condition_count(segment="index") == effective_condition_count(segment="INDEX")


def test_effective_condition_count_non_index_segment_is_unreduced() -> None:
    nominal, effective = effective_condition_count(segment="FNO")
    assert nominal == effective == SWEPT_CONDITIONS


def test_effective_condition_count_custom_n() -> None:
    # The subtraction is the ∩-implemented dead-bit count — 2 on the 74/74
    # retention layout (#2206) — regardless of the caller's nominal n.
    assert effective_condition_count(segment="INDEX", n=10) == (10, 8)


def test_vwap_dead_bits_are_52_and_53() -> None:
    assert frozenset({52, 53}) == VWAP_DEAD_BITS


def test_axes_product_multiplies_every_axis() -> None:
    assert axes_product(2, 3, 5) == 30


def test_axes_product_no_axes_is_one() -> None:
    assert axes_product() == 1


@pytest.mark.parametrize(
    ("segment", "expected_delta"),
    [("INDEX", 2), ("FNO", 0)],
)
def test_estimate_sweep_cost_annotates_condition_counts(segment: str, expected_delta: int) -> None:
    # The retention (DEC-VOCAB-VWAP-RETAIN-001 #2206) self-revived the
    # gap-hunt-#8 annotation: the LIVE-but-volumeless VWAP pair subtracts 2
    # on the zero-volume INDEX segment (flag-and-run — never excluded from
    # the swept vocabulary); every other segment reads effective == nominal.
    estimate = estimate_sweep_cost(
        k_values=[1, 2],
        other_axes_product=3,
        tuple_count=6,
        top=1000,
        segment=segment,
        calibration=UNCALIBRATED,
    )
    assert estimate.effective_condition_count == estimate.nominal_condition_count - expected_delta
    assert estimate.nominal_condition_count == SWEPT_CONDITIONS


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


def test_estimate_sweep_cost_clamps_finite_but_over_ceiling_seconds() -> None:
    """Round-3 mutation-testing finding: the overflow-clamp guard is
    ``if not isfinite(est_seconds) or est_seconds > _MAX_EST_SECONDS`` — a
    mutation that drops the second clause (leaving only the ``isfinite``
    check) passed every existing test, because both prior tests only
    produced ``inf``/``OverflowError``, never a merely-huge-but-FINITE
    value. This constructs one directly (``4.19e18 trials * 3600s ≈
    1.5e22``, comfortably finite, no exception) to pin that the ``>
    _MAX_EST_SECONDS`` ceiling clause is independently required."""
    calibration = EstimatorCalibration(
        calibrated=True,
        seconds_per_trial=3600.0,
        bytes_per_row=100,
        sample_trials=1,
        measured_at_utc=None,
        source="x",
    )
    estimate = estimate_sweep_cost(
        k_values=[20],
        other_axes_product=1000,
        tuple_count=1,
        top=1,
        segment="FNO",
        calibration=calibration,
    )
    assert estimate.est_seconds_provisional == 1e18


def test_estimate_sweep_cost_clamps_est_bytes_ceiling() -> None:
    """Round-3 adversarial-refuter finding: ``est_bytes`` (plain Python ints,
    so never ``inf``/``OverflowError`` internally) had NO ceiling at all — a
    large-but-otherwise-valid ``top`` fed through the UNCALIBRATED default
    produces an int whose decimal string exceeds Python 3.11+'s
    ``sys.set_int_max_str_digits`` guard, raising ``ValueError`` deep inside
    the JSON encoder on a real, unauthenticated dashboard request. Pin the
    same finite ceiling on the byte projection."""
    calibration = EstimatorCalibration(
        calibrated=True,
        seconds_per_trial=1e-6,
        bytes_per_row=10**20,
        sample_trials=1,
        measured_at_utc=None,
        source="x",
    )
    estimate = estimate_sweep_cost(
        k_values=[1],
        other_axes_product=1,
        tuple_count=10**10,
        top=10**10,
        segment="FNO",
        calibration=calibration,
    )
    assert estimate.est_bytes == 10**24


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
