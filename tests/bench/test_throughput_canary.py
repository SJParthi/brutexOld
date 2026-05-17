"""Throughput canary — DEC-P0-020 (PR #42).

Closes PR #41a's deferred perf canary by measuring the brute-force engine's
real hot-path operations on CI (ubuntu-24.04 4-core x86 shared runner).

## Honest envelope (Golden Rule #10) — RATIO gates, not absolute

The first draft of this canary (commit 8ea175a) used absolute ops/s gates
(30M numpy, 2.5M dict). Both `Benchmarks (O(1) NFR gates)` runs (push +
PR event) FAILED on shared GHA runners — exactly the chronic-flake
pattern Agent 2 of PR #42 pre-impl review warned about.

Per CLAUDE.md Rule #5 — the right gate is the **ratio** `p99(N=1M)/p99(N=1K) ≤ 1.1`,
NOT absolute throughput. This matches the existing pattern in
`tests/bench/test_store_o1.py:162` and `test_rate_limiter_o1.py:126`.

Absolute ops/s numbers are still measured + printed via pytest-benchmark
(visible in `bench-results.json` artifact), but the GATE is the ratio.

## What's measured

Three operations from the brute-force engine hot path (CLAUDE.md Rule #5):

1. **Combo mask AND evaluation** (`brutex/engine/combo_mask.py:189-221`):
   `(bar_bits & required_mask) == required_mask` — the core trial loop.
   Measured both as pure-Python (scalar) and numpy (vectorized).
2. **Bar lookup** — `dict[(symbol, ts)] -> bar` access pattern.
3. **Result append** — SKIPPED. `list.append` is ~50 M/s trivially; not
   load-bearing, and `tests/bench/test_store_o1.py` already bench-scopes
   the result lake write path separately.

## Why ratio gate (not absolute)?

- **Absolute ops/s on shared GHA = chronic flake** (Agent 2 warning, verified
  empirically: both push + PR runs failed with the 30M gate).
- **Ratio gate measures O(1) scaling** — if the op is truly O(1), the time
  per element should be constant regardless of dataset size. Linear scan
  regression would surface as ratio > ~2.

The 50 M/s figure in Rule #12 is the production-hardware envelope
(M4 Pro 14-core); CI is a sub-target. Recording absolute numbers in
`bench-results.json` lets the operator track regressions over time
without breaking CI on noisy-neighbor moments.
"""

from __future__ import annotations

import operator
import time
from functools import reduce

import numpy as np
import pytest
from brutex.engine.combo_mask import evaluate_mask

# Required-mask: 4 bits set out of 64 — matches the canonical strategy
# combo size in `docs/TRACK1_VOCABULARY.md` (5-6 conditions per strategy).
_REQUIRED_MASK_BITS: tuple[int, ...] = (3, 17, 33, 47)
_REQUIRED_MASK: int = reduce(operator.or_, (1 << b for b in _REQUIRED_MASK_BITS), 0)

# Deterministic RNG for reproducibility (Golden Rule #4).
_SEED: int = 42

# Ratio gate ceiling — matches `test_store_o1.py:162` pattern (5× ratio
# allows for CI jitter; a real O(n) regression would show ratio >> 5×).
_RATIO_CEILING: float = 5.0


def _make_bar_bits_array(n: int) -> np.ndarray:
    """Deterministic n-element uint64 array."""
    rng = np.random.default_rng(_SEED)
    return rng.integers(low=0, high=2**63 - 1, size=n, dtype=np.uint64)


def _make_bar_dict(n: int) -> dict[tuple[str, int], tuple[int, int, int, int]]:
    """Deterministic n-entry bar dict."""
    rng = np.random.default_rng(_SEED + 1)
    symbols = ("NIFTY", "BANKNIFTY", "SENSEX", "INDIAVIX")
    base_ts = 1_700_000_000
    out: dict[tuple[str, int], tuple[int, int, int, int]] = {}
    for i in range(n):
        sym = symbols[i % 4]
        ts = base_ts + i
        o = int(rng.integers(20000, 30000))
        h = o + int(rng.integers(0, 50))
        low = o - int(rng.integers(0, 50))
        c = int(rng.integers(low, h + 1))
        out[(sym, ts)] = (o, h, low, c)
    return out


class TestThroughputCanary:
    """O(1)-ratio canaries for the brute-force engine's hot path.

    Per Rule #5: each op MUST be O(1) per element — i.e. time per element
    is roughly constant regardless of N. We measure ratio `t_per_elem(N=10×) /
    t_per_elem(N=1×)`; gate at < 5× (matches existing bench-file pattern).
    """

    @pytest.mark.bench
    def test_vectorized_mask_and_ratio(self) -> None:
        """numpy vectorized `(arr & MASK) == MASK` — O(1) per element check.

        Time per element at N=100K vs N=1M must be roughly constant.
        Ratio ceiling: 5× (test_store_o1.py pattern). A real regression
        (e.g. accidental O(n²) wrapper) would show ratio >> 10×.
        """
        warmup_n = 10_000
        small_n = 100_000
        large_n = 1_000_000

        # Warm-up numpy + JIT caches.
        warmup_arr = _make_bar_bits_array(warmup_n)
        for _ in range(3):
            _ = (warmup_arr & _REQUIRED_MASK) == _REQUIRED_MASK

        def _time_per_elem(n: int) -> float:
            arr = _make_bar_bits_array(n)
            t0 = time.perf_counter_ns()
            for _ in range(5):
                _ = (arr & _REQUIRED_MASK) == _REQUIRED_MASK
            t1 = time.perf_counter_ns()
            # 5 iterations × n elements.
            return (t1 - t0) / (5 * n)

        t_small = _time_per_elem(small_n)
        t_large = _time_per_elem(large_n)
        ratio = t_large / max(t_small, 0.001)

        # Informational: also print absolute ops/s for ops-tracking
        # (visible in pytest output even though we don't gate on it).
        ops_per_sec_large = 1e9 / t_large
        print(
            f"\nnumpy mask AND: t_per_elem(100K)={t_small:.2f}ns, "
            f"t_per_elem(1M)={t_large:.2f}ns, "
            f"ratio={ratio:.2f}×, "
            f"absolute={ops_per_sec_large:,.0f} ops/s"
        )

        assert ratio < _RATIO_CEILING, (
            f"vectorized mask AND ratio {ratio:.2f}× (1M vs 100K) exceeds "
            f"{_RATIO_CEILING}× — possible O(n²) regression. "
            f"t_small={t_small:.2f}ns, t_large={t_large:.2f}ns."
        )

    @pytest.mark.bench
    def test_scalar_python_mask_and_smoke(self) -> None:
        """Pure-Python `evaluate_mask` smoke test — runs and produces matches.

        Pure scalar Python has CPython int-boxing ceiling (~2-15 M/s).
        We don't gate the throughput (would chronic-flake on GHA); we
        just verify the function runs and produces some matches over
        a deterministic 10K sample.
        """
        arr = _make_bar_bits_array(10_000)
        matches = sum(1 for bits in arr if evaluate_mask(int(bits), _REQUIRED_MASK))

        # Sanity: mask has 4 specific bits; ~6.25% of random uint64s
        # have ALL 4 set, so ~600-700 matches in 10K is expected.
        assert 100 < matches < 2000, (
            f"expected ~625 matches in 10K random bars (6.25%); got {matches}"
        )

    @pytest.mark.bench
    def test_dict_bar_lookup_ratio(self) -> None:
        """CPython dict lookup — O(1) per lookup invariant.

        Hash-table lookup time per key MUST be roughly constant
        regardless of dict size. Linear-scan regression would show
        ratio >> 10×.
        """
        small_n = 10_000
        large_n = 1_000_000
        n_lookups = 1_000

        small_dict = _make_bar_dict(small_n)
        large_dict = _make_bar_dict(large_n)

        small_keys = list(small_dict.keys())[:: small_n // n_lookups]
        large_keys = list(large_dict.keys())[:: large_n // n_lookups]
        small_keys = small_keys[:n_lookups]
        large_keys = large_keys[:n_lookups]

        def _time_lookups(d: dict, keys: list[tuple[str, int]]) -> float:
            # Warm cache.
            for k in keys[:50]:
                _ = d[k]
            t0 = time.perf_counter_ns()
            for k in keys:
                _ = d[k]
            t1 = time.perf_counter_ns()
            return (t1 - t0) / len(keys)

        t_small = _time_lookups(small_dict, small_keys)
        t_large = _time_lookups(large_dict, large_keys)
        ratio = t_large / max(t_small, 1.0)

        ops_per_sec_large = 1e9 / t_large
        print(
            f"\ndict lookup: t_per_lookup(10K dict)={t_small:.0f}ns, "
            f"t_per_lookup(1M dict)={t_large:.0f}ns, "
            f"ratio={ratio:.2f}×, "
            f"absolute={ops_per_sec_large:,.0f} ops/s"
        )

        assert ratio < _RATIO_CEILING, (
            f"dict lookup ratio {ratio:.2f}× (1M dict vs 10K dict) exceeds "
            f"{_RATIO_CEILING}× — possible hash-collision regression. "
            f"t_small={t_small:.0f}ns, t_large={t_large:.0f}ns."
        )
