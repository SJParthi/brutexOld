"""Speedup gate for the flag-gated Rust CPCV fold kernel (slice 6, PR-1).

DEC-METRICS-RUST-STATS-001. Times :func:`brutex.metrics.cpcv.cpcv_pass_rate`
END-TO-END (validation + numpy moments + the fold loop + the exact division)
at the production shape S=16, k=8 → 12,870 folds, OFF arm vs ON arm — so a
conversion-dominated port can never claim a modeled payoff its real call
path does not deliver (the slice risk-register FFI-copy rule).

Both arms are asserted result-BIT-equal FIRST (a diverging fast arm must
never pass as "fast"), the ON arm must genuinely ENGAGE the kernel
(anti-vacuous-pass law), the OFF arm is pinned by an explicit ``setenv "0"``
(flip-proof against a future default change), and the ON arm times the
steady state (burn-in excluded via the documented reset seam).

HONEST ENVELOPE (Golden Rule #10): sandbox ratios ONLY — this is NOT the §8
M4 Pro and no absolute-throughput claim is made; the kernel is a
CONSTANT-FACTOR win (the call stays ``O(n + C(S, k)·k)``). MEASURED
2026-07-11 sandbox (median-of-9, steady state): 63.8× at n=5,000
(off 56.97 ms → on 0.89 ms); 60.9× at n=500, 56.5× at n=40,000, 26.6× at
n=605,000 (the O(n) numpy moments pass dilutes the end-to-end ratio at the
1-minute slice — the fold loop itself is the constant ~55 ms removed). The
floor of ≥ 3× sits deliberately FAR below those measurements so shared-CI
noise cannot flake the gate while a real regression (e.g. the kernel
silently falling back per call) fails loudly.
"""

from __future__ import annotations

import struct
import time
from collections.abc import Callable

import numpy as np
import pytest
from brutex.metrics import rust_stats
from brutex.metrics.cpcv import cpcv_pass_rate
from structlog.testing import capture_logs

#: Production CPCV shape (CascadeConfig defaults): C(16, 8) = 12,870 folds.
_N = 5_000

#: Conservative shared-CI floor, far below the sandbox measurement (~19×) so
#: noise cannot flake while an order-of-magnitude regression still fails.
_FLOOR = 3.0


def _median_seconds(fn: Callable[[], float], rounds: int = 9) -> float:
    samples: list[float] = []
    for _ in range(rounds):
        t0 = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - t0)
    samples.sort()
    return samples[len(samples) // 2]


def test_cpcv_fold_kernel_speedup(monkeypatch: pytest.MonkeyPatch) -> None:
    returns = np.random.default_rng(7).normal(0.0005, 0.01, _N)

    def _run() -> float:
        return cpcv_pass_rate(returns)

    # OFF arm: explicit "0" (not delenv) — flip-proof against a future
    # default change; this arm must stay the pure-Python oracle.
    monkeypatch.setenv(rust_stats.STATS_RUST_ENV, "0")
    off_result = _run()  # warm-up + reference
    off = _median_seconds(_run)

    # ON arm: result equality + engagement BEFORE any timing claim.
    monkeypatch.setenv(rust_stats.STATS_RUST_ENV, "1")
    rust_stats.reset_observability_memo_for_tests()
    rust_stats.reset_self_check_for_tests("cpcv")
    with capture_logs() as logs:
        on_result = _run()
    assert struct.pack("<d", on_result) == struct.pack("<d", off_result), (
        "ON≡OFF bit-equality must hold before any speed claim"
    )
    assert any(e["event"] == "rust_stats.engaged" for e in logs), (
        "the ON arm must engage the Rust kernel (anti-vacuous-pass law)"
    )
    assert not rust_stats.kernel_disabled("cpcv")

    # Steady-state timing: burn-in excluded (the amortised-nil startup cost
    # is not the claim under test).
    rust_stats.reset_self_check_for_tests("cpcv", remaining=0)
    on = _median_seconds(_run)

    ratio = off / max(on, 1e-12)
    assert ratio >= _FLOOR, (
        f"Rust CPCV fold speedup {ratio:.1f}× is below the {_FLOOR}× floor "
        f"(off={off * 1e3:.2f} ms, on={on * 1e3:.2f} ms at n={_N}, S=16/k=8) "
        "— the kernel may be silently falling back per call."
    )
