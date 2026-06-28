"""Unit tests for :mod:`brutex.metrics.monte_carlo` — Stage-4 filter #9.

Targets 100% line + branch coverage: the real-edge / no-edge / dominated
outcomes, the non-finite-permutation conservative branch, the
``n_entries == n_bars`` degenerate case, determinism, and every validation
guard.
"""

from __future__ import annotations

import numpy as np
import pytest
from brutex.metrics.monte_carlo import MonteCarloResult, monte_carlo_permutation_test


def _zero(positions: np.ndarray) -> float:
    """Exit-logic stub: null edge (constant 0 statistic)."""
    return 0.0


def _mean_pos(positions: np.ndarray) -> float:
    """Exit-logic stub whose statistic (mean entry bar) spans the test range."""
    return float(positions.mean())


def _nan(positions: np.ndarray) -> float:
    """Exit-logic stub that always returns a non-finite statistic."""
    return float("nan")


def _pos_inf(positions: np.ndarray) -> float:
    """Exit-logic stub that always returns ``+inf`` (beats any finite observed)."""
    return float("inf")


def _neg_inf(positions: np.ndarray) -> float:
    """Exit-logic stub that always returns ``-inf`` (cannot beat any finite observed)."""
    return float("-inf")


def _too_good(positions: np.ndarray) -> float:
    """Exit-logic stub that always beats any observed <= 1e9."""
    return 1.0e9


def test_real_edge_low_pvalue() -> None:
    res = monte_carlo_permutation_test(
        observed_statistic=2.0,
        n_bars=1000,
        n_entries=50,
        evaluate=_zero,
        seed=42,
        n_permutations=500,
    )
    assert isinstance(res, MonteCarloResult)
    assert res.n_beaten == 0
    assert res.pvalue == pytest.approx(1 / 501)
    assert res.passed is True


def test_no_edge_high_pvalue() -> None:
    # observed sits at the centre of what _mean_pos produces (~500) → roughly
    # half the permutations beat it → high p-value, fails the guard.
    res = monte_carlo_permutation_test(
        observed_statistic=500.0,
        n_bars=1000,
        n_entries=50,
        evaluate=_mean_pos,
        seed=42,
        n_permutations=500,
    )
    assert res.n_beaten > 0
    assert res.pvalue > 0.01
    assert res.passed is False


def test_dominated_observed_pvalue_one() -> None:
    res = monte_carlo_permutation_test(
        observed_statistic=1.0,
        n_bars=100,
        n_entries=10,
        evaluate=_too_good,
        seed=1,
        n_permutations=200,
    )
    assert res.n_beaten == res.n_permutations
    assert res.pvalue == pytest.approx(201 / 201)
    assert res.passed is False


def test_nonfinite_permutation_does_not_beat() -> None:
    # A NaN exit statistic can never beat a finite observed one (conservative).
    res = monte_carlo_permutation_test(
        observed_statistic=1.0,
        n_bars=100,
        n_entries=10,
        evaluate=_nan,
        seed=1,
        n_permutations=200,
    )
    assert res.n_beaten == 0
    assert res.pvalue == pytest.approx(1 / 201)
    assert res.passed is True


def test_neg_inf_permutation_does_not_beat() -> None:
    # A -inf exit statistic can never beat a finite observed one (``-inf >= x``
    # is False) → did-not-beat, mirroring the NaN case.
    res = monte_carlo_permutation_test(
        observed_statistic=1.0,
        n_bars=100,
        n_entries=10,
        evaluate=_neg_inf,
        seed=1,
        n_permutations=200,
    )
    assert res.n_beaten == 0
    assert res.pvalue == pytest.approx(1 / 201)
    assert res.passed is True


def test_pos_inf_permutation_counts_as_beating() -> None:
    """A ``+inf`` permuted statistic IS counted as beating a finite observed one.

    Locks the corrected docstring/comment behaviour (DEC-METRICS-NONFINITE-GUARD-001):
    ``+inf >= observed`` is True, so every such permutation beats it, driving
    ``n_beaten == n_permutations`` and the p-value to its maximum ``1.0`` — the
    conservative (fail-safe) direction.
    """
    res = monte_carlo_permutation_test(
        observed_statistic=1.0,
        n_bars=100,
        n_entries=10,
        evaluate=_pos_inf,
        seed=1,
        n_permutations=200,
    )
    assert res.n_beaten == res.n_permutations
    assert res.pvalue == 1.0
    assert res.passed is False


def test_n_entries_equals_n_bars() -> None:
    # Every permutation selects all bars (sorted → identical) → same statistic.
    res = monte_carlo_permutation_test(
        observed_statistic=0.0,
        n_bars=5,
        n_entries=5,
        evaluate=_zero,
        seed=3,
        n_permutations=50,
    )
    assert res.n_beaten == res.n_permutations  # 0.0 >= 0.0 every time
    assert res.pvalue == pytest.approx(51 / 51)


def test_determinism() -> None:
    kw = {"observed_statistic": 250.0, "n_bars": 500, "n_entries": 20, "evaluate": _mean_pos}
    a = monte_carlo_permutation_test(**kw, seed=7, n_permutations=200)  # type: ignore[arg-type]
    b = monte_carlo_permutation_test(**kw, seed=7, n_permutations=200)  # type: ignore[arg-type]
    assert a == b
    c = monte_carlo_permutation_test(**kw, seed=8, n_permutations=200)  # type: ignore[arg-type]
    assert c.pvalue != a.pvalue or c.n_beaten != a.n_beaten


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"seed": 1.5}, "seed must be an int"),
        ({"seed": -1}, "seed must be >= 0"),
        ({"observed_statistic": float("nan")}, "observed_statistic must be finite"),
        ({"observed_statistic": float("inf")}, "observed_statistic must be finite"),
        ({"n_bars": 0}, "n_bars must be >= 1"),
        ({"n_entries": 0}, r"n_entries must be in \[1, n_bars\]"),
        ({"n_entries": 101}, r"n_entries must be in \[1, n_bars\]"),
        ({"n_permutations": 0}, "n_permutations must be >= 1"),
        ({"alpha": 0.0}, "alpha must be in the open interval"),
        ({"alpha": 1.0}, "alpha must be in the open interval"),
    ],
)
def test_validation(kwargs: dict[str, float], match: str) -> None:
    base: dict[str, object] = {
        "observed_statistic": 1.0,
        "n_bars": 100,
        "n_entries": 10,
        "evaluate": _zero,
        "seed": 0,
        "n_permutations": 10,
    }
    base.update(kwargs)
    with pytest.raises(ValueError, match=match):
        monte_carlo_permutation_test(**base)  # type: ignore[arg-type]


def test_metadata_echoed() -> None:
    res = monte_carlo_permutation_test(
        observed_statistic=1.5,
        n_bars=200,
        n_entries=8,
        evaluate=_zero,
        seed=0,
        n_permutations=100,
        alpha=0.05,
    )
    assert res.observed_statistic == 1.5
    assert res.n_bars == 200
    assert res.n_entries == 8
    assert res.n_permutations == 100
    assert res.alpha == 0.05
