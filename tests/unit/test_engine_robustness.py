"""Unit tests for the CPCV robustness wiring (DEC-CPCV-ROBUSTNESS-001).

Covers ``cpcv_robustness``: the too-short → ``None`` guard (never a crash), a robust
all-positive series (passes most folds), a fail-everywhere all-negative series, and a
custom ``n_groups`` that lowers the fold-count threshold — for the strict 100%
``brutex.engine`` package gate.
"""

from __future__ import annotations

from brutex.engine.robustness import cpcv_robustness


class TestCpcvRobustness:
    def test_too_few_returns_is_undefined(self) -> None:
        # Fewer than n_groups (16) returns → can't form the folds → None, not a crash.
        assert cpcv_robustness([0.01] * 5) is None

    def test_all_positive_returns_pass_most_folds(self) -> None:
        # 20 varied POSITIVE returns → every fold's held-out mean is positive (with
        # nonzero variance) → a high pass-rate in (0, 1].
        returns = [float(i % 5 + 1) for i in range(20)]
        rate = cpcv_robustness(returns)
        assert rate is not None
        assert 0.0 < rate <= 1.0

    def test_all_negative_returns_fail_every_fold(self) -> None:
        # 20 varied NEGATIVE returns → every fold's held-out Sharpe <= 0 → 0.0.
        returns = [-float(i % 5 + 1) for i in range(20)]
        assert cpcv_robustness(returns) == 0.0

    def test_custom_groups_lowers_the_threshold(self) -> None:
        # With n_groups=4 a 5-return series is now long enough → defined (not None).
        rate = cpcv_robustness([1.0, -1.0, 2.0, -2.0, 3.0], n_groups=4, n_test_groups=2)
        assert rate is not None
        assert 0.0 <= rate <= 1.0
