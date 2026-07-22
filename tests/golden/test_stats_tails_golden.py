"""FROZEN goldens for the stats-tail Rust seams (DEC-METRICS-RUST-STATS-002).

Every literal below was captured from the PRE-change Python at the branch's
merge base (``git show 98b9a732:brutex/metrics/{deflated_sharpe,pbo,wfo}.py``
+ ``ranking/overfit.py``, executed hermetically on the CI platform) — the
true pre-seam oracle, not a same-tree echo. Both ``BRUTEX_STATS_RUST`` arms
must reproduce them bit-for-bit (``repr`` round-trip is bit-exact for
floats), and the ON arm must genuinely ENGAGE each kernel (the
anti-vacuous-pass law of ``tests/golden/test_cpcv_wfo_golden.py``).

The overfit.py case pins the TRANSITIVE consumption law: the Stage-3
overfit gate is served by the Rust DSR pair through
:mod:`brutex.metrics.deflated_sharpe` with ZERO changes to
``brutex/ranking/overfit.py`` (the narrowest seam — no ranking-package
edit, the PR-overlap constraint of the lane).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import numpy as np
import pytest
from brutex.metrics import rust_stats
from brutex.metrics.deflated_sharpe import (
    deflated_sharpe_ratio,
    expected_max_sharpe_ratio,
    probabilistic_sharpe_ratio,
)
from brutex.metrics.pbo import pbo_probability
from brutex.metrics.wfo import wfo_pass_rates
from brutex.ranking.overfit import OverfitGateConfig, deflated_sharpe_for
from structlog.testing import capture_logs

_FLAGS = ["0", "1"]


@pytest.fixture(autouse=True)
def _fresh_stats_seam() -> None:
    """Fresh burn-in budgets / latches / log memo per test."""
    rust_stats.reset_self_check_for_tests()
    rust_stats.reset_observability_memo_for_tests()


def _assert_engagement(logs: Sequence[Mapping[str, object]], flag: str, fn: str) -> None:
    engaged = [e for e in logs if e["event"] == "rust_stats.engaged" and e["fn"] == fn]
    if flag == "1":
        assert engaged, f"the ON arm must engage the {fn} kernel (anti-vacuous-pass law)"
    else:
        assert not engaged


@pytest.mark.parametrize("flag", _FLAGS)
def test_psr_golden(flag: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(rust_stats.STATS_RUST_ENV, flag)
    with capture_logs() as logs:
        got = probabilistic_sharpe_ratio(0.15, 0.0, 250, -0.6, 5.5)
        clipped = probabilistic_sharpe_ratio(-0.02, 0.1, 5000, 1.2, 9.0)
    assert got == 0.9874954488927481
    assert clipped == 0.0  # a deeply-negative z underflows the CDF to exact 0.0
    _assert_engagement(logs, flag, "psr")


@pytest.mark.parametrize("flag", _FLAGS)
def test_expected_max_sharpe_golden(flag: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(rust_stats.STATS_RUST_ENV, flag)
    with capture_logs() as logs:
        got = expected_max_sharpe_ratio(1000, 0.04)
        big = expected_max_sharpe_ratio(10_000_000, 0.5)
    assert got == 0.6510243027305447
    assert big == 3.7511277733518633  # the spec's N<=1e7 leaderboard scale
    _assert_engagement(logs, flag, "sr_max")


@pytest.mark.parametrize("flag", _FLAGS)
def test_dsr_full_chain_golden(flag: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(rust_stats.STATS_RUST_ENV, flag)
    result = deflated_sharpe_ratio(
        0.12,
        n_trials=5000,
        trial_sharpe_variance=0.02,
        sample_length=180,
        skew=-0.4,
        kurtosis=4.7,
    )
    assert result.deflated_sharpe_ratio == 9.216985297211266e-08
    assert result.expected_max_sharpe == 0.5215199719891647
    assert result.probabilistic_sharpe_ratio == 0.9404331722600482


@pytest.mark.parametrize("flag", _FLAGS)
def test_pbo_golden(flag: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(rust_stats.STATS_RUST_ENV, flag)
    performance = [[float((i * j + 3) % 11 - 5) * 0.01 for j in range(1, 6)] for i in range(64)]
    with capture_logs() as logs:
        result = pbo_probability(performance, n_splits=8)
    assert result.pbo == 0.9
    assert result.n_combinations == 70
    assert result.n_strategies == 5
    assert result.logits[0] == -0.6931471805599454
    assert result.logits[69] == 0.0
    _assert_engagement(logs, flag, "pbo")


@pytest.mark.parametrize("flag", _FLAGS)
def test_wfo_triple_golden(flag: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(rust_stats.STATS_RUST_ENV, flag)
    returns = np.array([0.01 * ((i * 13 % 17) - 8) for i in range(200)], dtype=np.float64)
    with capture_logs() as logs:
        rates = wfo_pass_rates(returns, train_size=60, test_size=25, step=15, embargo=3)
    assert rates.rolling == 0.5
    assert rates.anchored == 0.5
    assert rates.expanding == 0.4
    _assert_engagement(logs, flag, "wfo")


@pytest.mark.parametrize("flag", _FLAGS)
def test_overfit_gate_transitive_golden(flag: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The Stage-3 overfit gate rides the Rust DSR pair TRANSITIVELY —
    ``brutex/ranking/overfit.py`` is byte-untouched by the lane."""
    monkeypatch.setenv(rust_stats.STATS_RUST_ENV, flag)
    with capture_logs() as logs:
        dsr = deflated_sharpe_for(
            0.2,
            120,
            -0.3,
            1.5,
            n_trials=800,
            trial_sharpe_variance=0.05,
            cfg=OverfitGateConfig(),
        )
    assert dsr == 4.308761969440411e-08
    _assert_engagement(logs, flag, "psr")
    _assert_engagement(logs, flag, "sr_max")
