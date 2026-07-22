"""Offline ridge logistic regression + the pure-Python inference ORACLE
(DEC-META-LABEL-ANNOTATE-001).

Two surfaces:

* :func:`fit_logistic` — the OFFLINE training fit: iteratively reweighted
  least squares (IRLS) with an L2 ridge penalty, a FIXED iteration cap and
  a deterministic zero initialisation. Pure numpy (NO sklearn — verified
  absent from the dependency tree; adding a heavyweight ML dependency for
  a 16-feature logistic fit would violate the lean-deps posture). HONEST
  complexity (Golden Rule #10): training is an OFFLINE batch
  O(trades · features² · iters) computation — sanctioned by the standing
  Rust-conversion contract's offline-statistics allowance (the CPCV/DSR
  family precedent); it never runs on any per-bar/per-mask hot path. ONLY
  inference (:func:`score_logistic`) is the O(1)/trade hot path, and its
  production home is the Rust kernel (``rust/brutex-meta/src/score.rs``)
  behind the :mod:`brutex.meta.meta_rust` facade.
* :func:`score_logistic` — the retained pure-Python inference ORACLE,
  mirrored token-for-token by the Rust kernel: ascending-index dot-product
  accumulation over one running float + the same branch-stable sigmoid.
  ``+``/``*`` are IEEE-754-defined; the ONE platform-dependent primitive is
  ``exp`` (CPython ``math.exp`` vs Rust ``f64::exp``, both the platform
  libm) — the facade's 16-call burn-in bit-compare + the hypothesis parity
  property are the runtime teeth for that seam.

Deterministic end-to-end: no RNG, no wall clock, fixed init, fixed
iteration order — the same inputs always produce bit-identical weights.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from collections.abc import Sequence

    import numpy as np
    import numpy.typing as npt

__all__ = [
    "DEFAULT_MAX_ITER",
    "DEFAULT_RIDGE",
    "LogisticFit",
    "fit_logistic",
    "score_logistic",
]

#: Fixed IRLS iteration cap (determinism: the same inputs walk the same
#: trajectory; convergence-tolerance early exit below is itself
#: deterministic). 50 Newton steps is far past convergence for a
#: 16-feature ridge logistic on any realistic trade count.
DEFAULT_MAX_ITER: Final[int] = 50

#: Default L2 ridge strength — stabilises separable/collinear folds so the
#: fit stays defined on tiny CPCV training sets (never applied to the
#: intercept column — see :func:`fit_logistic`).
DEFAULT_RIDGE: Final[float] = 1.0

#: Convergence tolerance on the max absolute Newton step.
_STEP_TOL: Final[float] = 1e-10

#: A design matrix is 2-D; a fit needs both classes present.
_MATRIX_NDIM: Final[int] = 2
_MIN_CLASSES: Final[int] = 2

#: Floor on the IRLS working weights ``p(1-p)`` (a saturated probability
#: would zero the Hessian row and blow up the solve).
_WEIGHT_FLOOR: Final[float] = 1e-10


@dataclass(frozen=True, slots=True)
class LogisticFit:
    """One fitted model: weights ordered by the caller's feature order.

    ``bias`` is always ``0.0`` in this package's convention — the intercept
    rides as feature 0 (``FEATURE_NAMES[0] == "intercept"``, value 1.0);
    the field exists so the fit is a complete logistic model for the Rust
    kernel's ``(weights, bias)`` signature.
    """

    weights: tuple[float, ...]
    bias: float
    converged: bool
    iterations: int


def _sigmoid(z: float) -> float:
    """Numerically-stable logistic sigmoid — the ONE shared expression.

    Token-for-token the Rust kernel's ``sigmoid``
    (``rust/brutex-meta/src/score.rs``): ``z >= 0`` → ``1/(1+exp(-z))``;
    else ``exp(z)/(1+exp(z))``. A NaN ``z`` takes the negative branch
    (``NaN >= 0.0`` is false) and returns NaN — identical in Rust.
    """
    if z >= 0.0:
        return 1.0 / (1.0 + math.exp(-z))
    ez = math.exp(z)
    return ez / (1.0 + ez)


def score_logistic(
    features: Sequence[float],
    n_features: int,
    weights: Sequence[float],
    bias: float,
) -> list[float]:
    """The pure-Python inference ORACLE of ``brutex_meta.score_logistic``.

    Scores a flattened row-major ``n_rows × n_features`` batch: per row,
    ``sigmoid(bias + Σ_j weights[j]·features[row·n_features + j])`` with the
    PINNED ascending-index accumulation over one running float — the exact
    loop shape of the Rust kernel, so the two arms are bit-comparable
    (module docstring). O(n_features) per row = O(1)/trade for the fixed
    documented vector.

    Raises
    ------
    ValueError
        On the kernel's structural refusals: ``n_features < 1``, a weights
        length mismatch, or a ragged (non-multiple) features length.
    """
    if n_features < 1:
        raise ValueError("score_logistic: n_features must be >= 1")
    if len(weights) != n_features:
        raise ValueError(
            f"score_logistic: weights length {len(weights)} != n_features {n_features}"
        )
    if len(features) % n_features != 0:
        raise ValueError(
            f"score_logistic: features length {len(features)} is not a multiple "
            f"of n_features {n_features}"
        )
    out: list[float] = []
    for base in range(0, len(features), n_features):
        z = bias
        for j in range(n_features):
            z += weights[j] * features[base + j]
        out.append(_sigmoid(z))
    return out


def fit_logistic(
    x: npt.NDArray[np.float64],
    y: npt.NDArray[np.float64],
    *,
    ridge: float = DEFAULT_RIDGE,
    max_iter: int = DEFAULT_MAX_ITER,
) -> LogisticFit | None:
    """Fit a ridge logistic model by IRLS — deterministic, offline, or ``None``.

    Parameters
    ----------
    x
        ``(n, k)`` float64 design matrix. Column 0 is the intercept by this
        package's convention (:data:`brutex.meta.features.FEATURE_NAMES`)
        and is NEVER ridge-penalised; every other column carries *ridge*.
    y
        ``(n,)`` labels in ``{0.0, 1.0}``.
    ridge
        L2 penalty strength (must be ``> 0`` — the guarantee the Hessian is
        positive-definite on the penalised columns; the intercept column
        stays solvable through the data term).
    max_iter
        Fixed Newton-step cap (determinism; early exit on
        :data:`_STEP_TOL` is deterministic too).

    Returns
    -------
    LogisticFit | None
        ``None`` — the honest "no model" — when the inputs are degenerate:
        empty/mismatched shapes, a single-class label vector (nothing to
        discriminate), non-finite inputs, or a numerically failed solve.
        The caller annotates ``p_win = null`` for the affected trades,
        never a fabricated probability (Golden Rule #1).
    """
    import numpy as np

    if x.ndim != _MATRIX_NDIM or y.ndim != 1 or x.shape[0] != y.shape[0] or x.shape[0] == 0:
        return None
    if not (bool(np.isfinite(x).all()) and bool(np.isfinite(y).all())):
        return None
    if ridge <= 0.0 or not math.isfinite(ridge):
        raise ValueError(f"fit_logistic: ridge must be finite and > 0, got {ridge!r}")
    if max_iter < 1:
        raise ValueError(f"fit_logistic: max_iter must be >= 1, got {max_iter!r}")
    classes = np.unique(y)
    if classes.size < _MIN_CLASSES:
        return None
    if not set(classes.tolist()) <= {0.0, 1.0}:
        return None

    n_features = x.shape[1]
    penalty = np.full(n_features, ridge, dtype=np.float64)
    penalty[0] = 0.0  # the intercept column is never penalised
    w = np.zeros(n_features, dtype=np.float64)
    converged = False
    iterations = 0
    for iteration in range(1, max_iter + 1):
        iterations = iteration
        z = x @ w
        # Vectorised stable sigmoid (training-internal only — inference
        # parity rides the scalar oracle above).
        p = np.where(
            z >= 0.0,
            1.0 / (1.0 + np.exp(-np.abs(z))),
            np.exp(-np.abs(z)) / (1.0 + np.exp(-np.abs(z))),
        )
        working = np.maximum(p * (1.0 - p), _WEIGHT_FLOOR)
        grad = x.T @ (y - p) - penalty * w
        hessian = (x * working[:, None]).T @ x + np.diag(penalty)
        try:
            step = np.linalg.solve(hessian, grad)
        except np.linalg.LinAlgError:
            return None
        if not bool(np.isfinite(step).all()):
            return None
        w = w + step
        if float(np.max(np.abs(step))) < _STEP_TOL:
            converged = True
            break
    return LogisticFit(
        weights=tuple(float(value) for value in w),
        bias=0.0,
        converged=converged,
        iterations=iterations,
    )
