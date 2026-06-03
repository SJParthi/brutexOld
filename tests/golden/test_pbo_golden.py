"""Golden-master regression test for :mod:`brutex.metrics.pbo` — PBO via CSCV.

Locks the PBO and the full per-split logit vector for a fixed deterministic
``(8, 4)`` performance matrix at ``S = 4`` (``C(4, 2) = 6`` splits). Any
drift in the CSCV split enumeration, the chunk-Sharpe statistic, the rank /
logit math, or the overfit-count tally produces different numbers and fails
this test loudly.

Regenerate only on a deliberate change and record it in the corresponding
``docs/MIGRATION.md`` entry. DEC-STAT-DSR-PBO-001 (entry #428).
"""

from __future__ import annotations

import pytest
from brutex.metrics.pbo import pbo_probability

#: A fixed, fully deterministic (T=8, N=4) matrix: M[t][n] = (7t + 3n) mod 5 − 2.
_GOLDEN_MATRIX = [[float((t * 7 + n * 3) % 5 - 2) for n in range(4)] for t in range(8)]
_GOLDEN_N_SPLITS = 4
_GOLDEN_PBO = 0.6666666666666666  # 4 of 6 splits overfit
_GOLDEN_LOGITS = (
    -1.3862943611198906,
    -0.4054651081081643,
    0.4054651081081642,
    -1.3862943611198906,
    -1.3862943611198906,
    0.4054651081081642,
)


def test_golden_pbo_value() -> None:
    """The PBO of the locked matrix is exactly 4/6."""
    result = pbo_probability(_GOLDEN_MATRIX, n_splits=_GOLDEN_N_SPLITS)
    assert result.pbo == _GOLDEN_PBO
    assert result.n_combinations == 6
    assert result.n_splits == _GOLDEN_N_SPLITS
    assert result.n_strategies == 4


def test_golden_pbo_logits() -> None:
    """The full per-split logit vector is locked bit-for-bit (tight tolerance)."""
    result = pbo_probability(_GOLDEN_MATRIX, n_splits=_GOLDEN_N_SPLITS)
    assert result.logits == pytest.approx(_GOLDEN_LOGITS, rel=1e-15)
