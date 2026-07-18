"""Unit gate for :mod:`brutex.meta.logistic` (DEC-META-LABEL-ANNOTATE-001).

The inference ORACLE's structural refusals + pinned accumulation order +
saturation/NaN semantics, and the offline IRLS fit's determinism +
degenerate-input honesty (``None``, never a fabricated model).
"""

from __future__ import annotations

import math
import struct

import numpy as np
import pytest
from brutex.meta.logistic import (
    DEFAULT_MAX_ITER,
    DEFAULT_RIDGE,
    LogisticFit,
    fit_logistic,
    score_logistic,
)


def bits(values: list[float]) -> bytes:
    return struct.pack(f"<{len(values)}d", *values)


class TestScoreOracle:
    def test_empty_batch_scores_empty(self) -> None:
        assert score_logistic([], 3, [0.1, 0.2, 0.3], 0.0) == []

    def test_zero_logit_is_exact_half(self) -> None:
        assert score_logistic([1.0, 2.0, 3.0], 3, [0.0, 0.0, 0.0], 0.0) == [0.5]

    def test_bias_only_matches_sigmoid(self) -> None:
        (p,) = score_logistic([0.0], 1, [1.0], 3.0)
        assert p == 1.0 / (1.0 + math.exp(-3.0))

    def test_large_logits_saturate_exactly(self) -> None:
        assert score_logistic([1.0, -1.0], 1, [1000.0], 0.0) == [1.0, 0.0]

    def test_nan_feature_propagates_nan(self) -> None:
        (p,) = score_logistic([float("nan")], 1, [1.0], 0.0)
        assert math.isnan(p)

    def test_signed_zero_feature_is_exact_half(self) -> None:
        assert score_logistic([-0.0], 1, [5.0], 0.0) == [0.5]

    def test_accumulation_order_is_ascending_index(self) -> None:
        # Catastrophic-cancellation probe (the Rust kernel's exact pin):
        # ((0 + 1e16) + 1.0) + -1e16 == 0.0 → sigmoid(0) == 0.5 exactly;
        # any other association order lands elsewhere.
        assert score_logistic([1.0, 1.0, 1.0], 3, [1e16, 1.0, -1e16], 0.0) == [0.5]

    def test_batch_equals_per_row_bit_for_bit(self) -> None:
        w = [0.3, -0.7]
        rows = [0.5, 1.5, -2.0, 0.25]
        batch = score_logistic(rows, 2, w, 0.1)
        singles = [score_logistic(rows[i : i + 2], 2, w, 0.1)[0] for i in (0, 2)]
        assert bits(batch) == bits(singles[:1] + singles[1:])

    def test_zero_n_features_refuses(self) -> None:
        with pytest.raises(ValueError, match="n_features"):
            score_logistic([], 0, [], 0.0)

    def test_weight_length_mismatch_refuses(self) -> None:
        with pytest.raises(ValueError, match="weights length"):
            score_logistic([1.0, 2.0], 2, [1.0], 0.0)

    def test_ragged_features_refuses(self) -> None:
        with pytest.raises(ValueError, match="not a multiple"):
            score_logistic([1.0, 2.0, 3.0], 2, [1.0, 1.0], 0.0)


class TestFit:
    def separable(self, n: int = 40) -> tuple[np.ndarray, np.ndarray]:
        # Column 0 = intercept; column 1 separates the classes.
        x = np.asarray([[1.0, 1.0 if i % 2 == 0 else -1.0] for i in range(n)], dtype=np.float64)
        y = np.asarray([1.0 if i % 2 == 0 else 0.0 for i in range(n)], dtype=np.float64)
        return x, y

    def test_fit_recovers_the_separating_direction(self) -> None:
        x, y = self.separable()
        fit = fit_logistic(x, y)
        assert isinstance(fit, LogisticFit)
        assert fit.bias == 0.0
        assert fit.weights[1] > 0.0  # feature 1 predicts a win
        probs = score_logistic([1.0, 1.0], 2, list(fit.weights), fit.bias)
        assert probs[0] > 0.9

    def test_fit_is_deterministic_bit_for_bit(self) -> None:
        x, y = self.separable()
        a = fit_logistic(x, y)
        b = fit_logistic(x, y)
        assert a is not None and b is not None
        assert bits(list(a.weights)) == bits(list(b.weights))
        assert a.iterations == b.iterations
        assert a.converged is b.converged

    def test_converges_within_the_cap(self) -> None:
        x, y = self.separable()
        fit = fit_logistic(x, y)
        assert fit is not None
        assert fit.converged is True
        assert 1 <= fit.iterations <= DEFAULT_MAX_ITER

    def test_iteration_cap_without_convergence(self) -> None:
        x, y = self.separable()
        fit = fit_logistic(x, y, max_iter=1)
        assert fit is not None
        assert fit.converged is False
        assert fit.iterations == 1

    @pytest.mark.parametrize(
        ("x", "y"),
        [
            (np.zeros((0, 2)), np.zeros(0)),  # empty
            (np.zeros((3, 2)), np.zeros(2)),  # row mismatch
            (np.zeros(3), np.zeros(3)),  # 1-D design matrix
            (np.ones((3, 2)), np.asarray([1.0, 1.0, 1.0])),  # single class
            (np.ones((3, 2)), np.asarray([0.0, 0.5, 1.0])),  # junk labels
            (
                np.asarray([[1.0, float("nan")], [1.0, 1.0]]),
                np.asarray([0.0, 1.0]),
            ),  # non-finite design
            (
                np.asarray([[1.0, 1.0], [1.0, -1.0]]),
                np.asarray([float("inf"), 0.0]),
            ),  # non-finite labels
        ],
    )
    def test_degenerate_inputs_read_none(self, x: np.ndarray, y: np.ndarray) -> None:
        assert (
            fit_logistic(np.asarray(x, dtype=np.float64), np.asarray(y, dtype=np.float64)) is None
        )

    @pytest.mark.parametrize("ridge", [0.0, -1.0, float("nan"), float("inf")])
    def test_bad_ridge_refuses(self, ridge: float) -> None:
        x, y = self.separable()
        with pytest.raises(ValueError, match="ridge"):
            fit_logistic(x, y, ridge=ridge)

    def test_bad_max_iter_refuses(self) -> None:
        x, y = self.separable()
        with pytest.raises(ValueError, match="max_iter"):
            fit_logistic(x, y, max_iter=0)

    def test_default_ridge_is_positive(self) -> None:
        assert DEFAULT_RIDGE > 0.0

    def test_failed_solve_reads_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        x, y = self.separable()

        def _boom(*_args: object, **_kwargs: object) -> object:
            raise np.linalg.LinAlgError("singular")

        monkeypatch.setattr(np.linalg, "solve", _boom)
        assert fit_logistic(x, y) is None

    def test_non_finite_step_reads_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        x, y = self.separable()

        def _nan_step(*_args: object, **_kwargs: object) -> np.ndarray:
            return np.asarray([float("nan"), 0.0])

        monkeypatch.setattr(np.linalg, "solve", _nan_step)
        assert fit_logistic(x, y) is None
