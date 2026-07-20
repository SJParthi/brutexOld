"""Bounded-overhead bench for the shared tod seam (DEC-TOD-SECONDS-RUST-SEAM-001).

**Honest, narrow claim (Golden Rule #10):** the tod derivation moved from three
inline Python ``h*3600 + m*60 + s`` copies onto ONE Rust FFI call
(``trading_core.seconds_of_day``) — a per-bar CONSTANT-cost swap. A ratio bench
across bar-count tiers is mathematically BLIND to a uniform per-bar constant
(both tiers move equally; the ratio stays ~1.0 for ANY overhead), so THIS bench
is the constant-cost-sensitive gate the design review demanded: a same-process
comparative micro-bench asserting the binding's per-call cost stays within a
BOUNDED multiple of the inline arithmetic it replaced, with an absolute floor so
a sub-ns inline baseline on a fast box cannot make the bound vacuous.

It does NOT claim the FFI call is free (it is an extra crossing per bar — ~2 of
them on the Stage-1A path, stated in the ledger), only that the overhead is a
small bounded constant, far below anything that could move a real sweep whose
per-bar cost is dominated by the evaluate/market-structure kernels (and,
before its bits-52/53 retirement, the VWAP state).
"""

from __future__ import annotations

import time
from collections.abc import Callable

import trading_core

#: Iterations per timing pass — enough to average out scheduler noise while
#: keeping the whole bench sub-second (it runs twice via the discovery gate).
_N = 100_000

#: The binding may cost at most this MULTIPLE of the inline arithmetic...
_RATIO_CEILING = 60.0

#: ...OR this absolute per-call budget, whichever is LARGER — the floor keeps
#: the bound meaningful when the inline baseline is a few tens of ns (a pure
#: int expression), and generous enough for a loaded shared CI runner.
_ABS_FLOOR_NS = 5_000.0


def _per_call_ns(fn: Callable[[int, int, int], object], n: int) -> float:
    """Average ns/call of ``fn(h, m, s)`` over *n* iterations (one warm pass)."""
    call = fn
    for _ in range(1_000):  # warm-up (import caches, branch predictors)
        call(10, 30, 15)
    start = time.perf_counter()
    for _ in range(n):
        call(10, 30, 15)
    return (time.perf_counter() - start) * 1e9 / n


def _inline(hour: int, minute: int, second: int) -> int:
    # The REPLACED inline arithmetic — the comparison baseline (deliberately
    # kept as the independent expression; do not fold onto the binding).
    return hour * 3600 + minute * 60 + second


def test_seconds_of_day_binding_overhead_is_bounded() -> None:
    inline_ns = _per_call_ns(_inline, _N)
    binding_ns = _per_call_ns(trading_core.seconds_of_day, _N)
    budget_ns = max(_ABS_FLOOR_NS, _RATIO_CEILING * inline_ns)
    assert binding_ns <= budget_ns, (
        f"seconds_of_day binding {binding_ns:.0f} ns/call exceeds the bounded "
        f"budget {budget_ns:.0f} ns (inline baseline {inline_ns:.0f} ns) — the "
        "tod seam's constant per-bar cost regressed"
    )


def test_seconds_of_day_binding_agrees_with_inline_baseline() -> None:
    # The bench's two subjects must compute the SAME value, or the timing
    # comparison is meaningless.
    assert int(trading_core.seconds_of_day(10, 30, 15)) == _inline(10, 30, 15)
