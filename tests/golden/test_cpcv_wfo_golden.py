"""Golden byte-equality oracle for CPCV + WFO splits and pass-rates.

Every value below is hand-derived in the comments and locked as a literal.
Any change to the group partitioning, the purge/embargo arithmetic, the
walk placement, the Sharpe convention, or the pass predicate that alters
output breaks this test (Golden Rule #4 — reproducibility; §12 golden
test type). Inputs are plain Python lists so the oracle is dependency-light.
"""

from __future__ import annotations

import numpy as np
import pytest
from brutex.metrics.cpcv import cpcv_pass_rate, cpcv_splits, sharpe_of_returns
from brutex.metrics.wfo import WFOScheme, walk_forward_splits, wfo_pass_rate, wfo_pass_rates
from structlog.testing import capture_logs

# ─────────────────────────────────────────────────────────────────────
# CPCV splits — n=8, S=4 (groups [0,1] [2,3] [4,5] [6,7]), k=2,
# label_span=1, embargo=1. C(4,2)=6 splits. Each train set is the
# complement minus [start-1, end+1+1] around every contiguous test block.
# ─────────────────────────────────────────────────────────────────────
_EXPECTED_CPCV: dict[tuple[int, ...], tuple[list[int], list[int]]] = {
    # (test_groups): (test_indices, train_indices)
    (0, 1): ([0, 1, 2, 3], [6, 7]),  # block[0,3] → excl[0,5]
    (0, 2): ([0, 1, 4, 5], []),  # blocks[0,1]+[4,5] → excl[0,3]+[3,7]=[0,7]
    (0, 3): ([0, 1, 6, 7], [4]),  # excl[0,3]+[5,7]
    (1, 2): ([2, 3, 4, 5], [0]),  # block[2,5] → excl[1,7]
    (1, 3): ([2, 3, 6, 7], [0]),  # excl[1,5]+[5,7]
    (2, 3): ([4, 5, 6, 7], [0, 1, 2]),  # block[4,7] → excl[3,7]
}


def test_cpcv_splits_golden() -> None:
    splits = cpcv_splits(8, n_groups=4, n_test_groups=2, label_span=1, embargo=1)
    actual = {s.test_groups: (s.test.tolist(), s.train.tolist()) for s in splits}
    assert actual == _EXPECTED_CPCV


@pytest.mark.parametrize("flag", ["0", "1"])
def test_cpcv_pass_rate_golden(flag: str, monkeypatch: pytest.MonkeyPatch) -> None:
    # n=4, S=4, k=2 → 4 singleton groups; each fold is two returns.
    # returns [1, 2, -3, -4]; Sharpe sign == mean sign (population std > 0):
    #   (0,1)[1,2] +  (0,2)[1,-3] -  (0,3)[1,-4] -
    #   (1,2)[2,-3] - (1,3)[2,-4] - (2,3)[-3,-4] -  → 1 of 6 pass.
    #
    # Parametrized over BOTH ``BRUTEX_STATS_RUST`` arms
    # (DEC-METRICS-RUST-STATS-001): the OFF arm is pinned by an EXPLICIT
    # setenv "0" (flip-proof against a future default change), and the ON
    # arm must genuinely ENGAGE the Rust kernel — a wheel-absent silent
    # fallback fails this test loudly, never passes vacuously.
    from brutex.metrics import rust_stats

    monkeypatch.setenv(rust_stats.STATS_RUST_ENV, flag)
    rust_stats.reset_self_check_for_tests("cpcv")
    rust_stats.reset_observability_memo_for_tests()
    with capture_logs() as logs:
        rate = cpcv_pass_rate([1.0, 2.0, -3.0, -4.0], n_groups=4, n_test_groups=2)
    assert rate == 1.0 / 6.0
    engaged = [e for e in logs if e["event"] == "rust_stats.engaged"]
    if flag == "1":
        assert engaged, "the ON arm must engage the Rust kernel (anti-vacuous-pass law)"
    else:
        assert not engaged


# ─────────────────────────────────────────────────────────────────────
# WFO walks — n=10, rolling, train=3, test=2, step=2.
# first test at index 3; test_start ∈ {3, 5, 7} (3+2 .. 8+2<=10).
# ─────────────────────────────────────────────────────────────────────
_EXPECTED_ROLLING_WALKS = [
    (0, 3, 3, 5),
    (2, 5, 5, 7),
    (4, 7, 7, 9),
]


def test_wfo_rolling_walks_golden() -> None:
    walks = walk_forward_splits(10, WFOScheme.ROLLING, train_size=3, test_size=2, step=2)
    actual = [(w.train_start, w.train_stop, w.test_start, w.test_stop) for w in walks]
    assert actual == _EXPECTED_ROLLING_WALKS


def test_wfo_pass_rate_golden() -> None:
    # returns indexed 0..9; rolling test windows are [3,5) [5,7) [7,9):
    #   {1,2}  → Sharpe +  → pass
    #   {0,0}  → zero variance → undefined → fail
    #   {-1,-2}→ Sharpe -  → fail            → 1 of 3 pass.
    returns = [0.0, 0.0, 0.0, 1.0, 2.0, 0.0, 0.0, -1.0, -2.0, 0.0]
    rate = wfo_pass_rate(returns, WFOScheme.ROLLING, train_size=3, test_size=2, step=2)
    assert rate == 1.0 / 3.0


def test_wfo_pass_rates_triple_golden() -> None:
    # Same series. rolling/anchored share test windows (first test at 3) → 1/3.
    # expanding starts at train_size//2 = 1 → windows [1,3) [3,5) [5,7) [7,9):
    #   {0,0} fail, {1,2} pass, {0,0} fail, {-1,-2} fail → 1/4.
    returns = [0.0, 0.0, 0.0, 1.0, 2.0, 0.0, 0.0, -1.0, -2.0, 0.0]
    rates = wfo_pass_rates(returns, train_size=3, test_size=2, step=2)
    assert rates.rolling == 1.0 / 3.0
    assert rates.anchored == 1.0 / 3.0
    assert rates.expanding == 1.0 / 4.0


# ─────────────────────────────────────────────────────────────────────
# sharpe_of_returns — golden population-Sharpe constant + the overflow
# degrade-to-None case (DEC-METRICS-NONFINITE-GUARD-001).
# series [0.01, -0.02, 0.03, -0.01, 0.02]:
#   mean = 0.03/5 = 0.006
#   var  = Σ(x-mean)²/5 = 0.00172/5 = 0.000344  (population, ÷n)
#   sharpe = 0.006 / sqrt(0.000344) = 0.3234983196103152
# ─────────────────────────────────────────────────────────────────────
_GOLDEN_SHARPE: float = 0.3234983196103152


def test_sharpe_of_returns_golden() -> None:
    """The population Sharpe of a fixed fractional series is locked full-precision."""
    result = sharpe_of_returns([0.01, -0.02, 0.03, -0.01, 0.02])
    assert result == _GOLDEN_SHARPE


def test_sharpe_of_returns_overflow_is_none_golden() -> None:
    """A finite-but-huge series whose squared deviations overflow degrades to ``None``."""
    with np.errstate(all="ignore"):
        assert sharpe_of_returns(np.array([1e300, 2e300, 3e300, 4e300])) is None
