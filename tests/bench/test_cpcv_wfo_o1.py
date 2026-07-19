"""O(1) bench — the CPCV pass-rate's per-fold cost is flat in series length.

:func:`brutex.metrics.cpcv.cpcv_pass_rate` builds per-group ``(n, mean, M2)``
moments in one O(n) pass, then scores each of the ``C(S, k)`` folds in
``O(k)`` via a stable moment merge — so the dominant fold loop is **O(1)
per fold in the series length ``n``**. This gate asserts the total time at
``n = 10,000`` stays within a small multiple of the time at ``n = 1,000``
for the *same* ``S = 16, k = 8`` (an identical 12,870-fold loop): a
regression to per-fold slicing (``O(n)`` per fold) would blow the ratio by
~10×.

Honest scope (Golden Rule #10): this gates the per-fold cost *in n*, NOT
the combinatorial total over folds — CPCV is ``C(S, k)`` folds by
construction and the module never claims that total is O(1). Best-of-N
timing (DEC-RUST-SUPERMASK-KERNEL-BENCH-DEFLAKE-001 pattern) damps CI
jitter; the strict 1.1× tier applies on the M4-Pro runner via
:func:`ratio_ceiling`.
"""

from __future__ import annotations

import time

import numpy as np
import pytest
from brutex.metrics import rust_stats
from brutex.metrics.cpcv import cpcv_pass_rate

from tests.bench._o1_tiers import ratio_ceiling

_SMALL = 1_000
_LARGE = 10_000


def _best_time_ns(n: int, *, repeats: int = 5, inner: int = 3) -> float:
    """Best-of-``repeats`` mean wall time (ns) of one ``cpcv_pass_rate`` call."""
    returns = np.random.default_rng(0).normal(0.0005, 0.01, n)
    cpcv_pass_rate(returns)  # warm import / cache paths
    best = float("inf")
    for _ in range(repeats):
        t0 = time.perf_counter_ns()
        for _ in range(inner):
            cpcv_pass_rate(returns)
        best = min(best, (time.perf_counter_ns() - t0) / inner)
    return best


@pytest.mark.parametrize("flag", ["0", "1"])
def test_cpcv_pass_rate_per_fold_is_o1_in_n(flag: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Flatness holds on BOTH ``BRUTEX_STATS_RUST`` arms
    (DEC-METRICS-RUST-STATS-001 — the Rust fold kernel keeps the same
    O(1)-per-fold-in-n shape; the ON arm times the steady state, burn-in
    excluded via the documented reset seam)."""
    monkeypatch.setenv(rust_stats.STATS_RUST_ENV, flag)
    rust_stats.reset_self_check_for_tests("cpcv", remaining=0)
    t_small = _best_time_ns(_SMALL)
    t_large = _best_time_ns(_LARGE)
    ratio = t_large / max(t_small, 1.0)
    assert ratio < ratio_ceiling(3.0), (
        f"cpcv_pass_rate time ratio {ratio:.2f}× (n=10k vs n=1k, same "
        f"S=16/k=8, flag={flag}) exceeds the O(1)-per-fold ceiling — the "
        f"per-fold cost may have regressed to O(n) slicing. "
        f"t_1k={t_small:.0f}ns, t_10k={t_large:.0f}ns."
    )
