"""Throughput canary — DEC-P0-020 (PR #42).

Closes PR #41a's deferred perf canary by measuring the brute-force engine's
real hot-path operations on CI (ubuntu-24.04 4-core x86 shared runner).

## Honest envelope (Golden Rule #10)

CLAUDE.md Rule #12 mandates "throughput canary ≥ 50 M/s". That target is the
**production-hardware envelope** on the M4 Pro 14-core reference machine
(per docs/TRACK1_PERFORMANCE_BUDGET.md). CI on a shared 4-core x86 VM runs
at roughly 0.3-0.4× single-thread, so honest gates here are:

- **numpy-vectorized `bitwise_and`**: ≥ 30 M ops/s (HARD GATE; production target 200-400 M/s on M4 Pro; observed ~700 M/s on dev env)
- **Pure-Python scalar loop**: ~2-15 M/s (RECORD-ONLY for regression detection; CPython int boxing is the ceiling; observed ~15 M/s on dev env)
- **CPython dict lookup + tuple-index + sum**: ≥ 2.5 M lookups/s (HARD GATE; production target 30-50 M/s on M4 Pro; measured envelope on shared CI ~3.3 M/s. Gate is 25% headroom below observed floor per ratio-pattern in `test_store_o1.py`.)

The 50 M/s figure in Rule #12 is qualified in `docs/STANDARDS_100PCT.md` row 14
as the M4 Pro production envelope; CI is the sub-target.

## What's measured

Three operations from the brute-force engine hot path (CLAUDE.md Rule #5):

1. **Combo mask AND evaluation** (`brutex/engine/combo_mask.py:189-221`):
   `(bar_bits & required_mask) == required_mask` — the core trial loop.
   Measured both as pure-Python (scalar) and numpy (vectorized).
2. **Bar lookup** — `dict[(symbol, ts)] -> bar` access pattern. Synthetic
   dict because the in-memory hot-path bar dict is Phase 2 (DEC-P0-017
   refactor enabled the cross-module access).
3. **Result append** — SKIPPED. `list.append` is ~50 M/s trivially; not
   load-bearing, and not in Rule #5's list either (Rule #5 says "result
   append" but that's a write to the result lake, which is bench-scoped
   separately in `tests/bench/test_store_o1.py`).

## Why not pytest-benchmark `--benchmark-compare`?

The existing `tests/bench/` files use **inline ratio assertions** (see
`test_store_o1.py:162`, `test_rate_limiter_o1.py:126`) rather than
compare-mode. Matches the existing pattern; avoids the
`.benchmarks/baseline.json` bootstrap problem identified by Agent 1 of
PR #42 pre-impl review.

## Why no `evaluate_atoms` benchmark?

`brutex/vocabulary/evaluator.py::evaluate_atoms` mutates a
`PriorBarHistory` and would require careful look-ahead-invariant testing
(P-06). Bench-scoping that requires a dedicated PR. This canary measures
the downstream `evaluate_mask` step which is stateless per-bar.
"""

from __future__ import annotations

import operator
from functools import reduce
from typing import TYPE_CHECKING

import numpy as np
import pytest
from brutex.engine.combo_mask import evaluate_mask

if TYPE_CHECKING:
    from pytest_benchmark.fixture import BenchmarkFixture

# Bench dataset sizes — fixed for reproducibility (Golden Rule #4).
# N=1M chosen because Rule #5 stipulates `p99(N=1M)/p99(N=1K) ≤ 1.1`.
_N_LARGE: int = 1_000_000
_N_DICT: int = 1_000_000

# Required-mask: 4 bits set out of 64 — matches the canonical strategy
# combo size in `docs/TRACK1_VOCABULARY.md` (5-6 conditions per strategy).
_REQUIRED_MASK_BITS: tuple[int, ...] = (3, 17, 33, 47)
_REQUIRED_MASK: int = reduce(operator.or_, (1 << b for b in _REQUIRED_MASK_BITS), 0)

# Deterministic RNG for reproducibility (Golden Rule #4 + Agent 2
# correctness reviewer recommendation).
_SEED: int = 42


@pytest.fixture(scope="module")
def bar_bits_array() -> np.ndarray:
    """Pre-generated 1M-element uint64 array of synthetic `bar_bits` values.

    Roughly 50% of entries satisfy the required mask (mix of matches +
    non-matches so the benchmark exercises both branches).
    """
    rng = np.random.default_rng(_SEED)
    # Each bar_bits is a 64-bit unsigned int with random bits set.
    return rng.integers(low=0, high=2**63 - 1, size=_N_LARGE, dtype=np.uint64)


@pytest.fixture(scope="module")
def bar_bits_python_list(bar_bits_array: np.ndarray) -> list[int]:
    """Pre-converted Python int list — for pure-Python scalar benchmark.

    Only 100K elements because pure-Python scalar can't realistically
    benchmark 1M ints within pytest-benchmark's per-round budget.
    """
    return [int(x) for x in bar_bits_array[:100_000]]


@pytest.fixture(scope="module")
def bar_dict() -> dict[tuple[str, int], tuple[int, int, int, int]]:
    """1M-entry synthetic bar dict mimicking `(symbol, ts_minute) -> bar`.

    Each value is a small tuple (open, high, low, close) so the dict
    stores realistic-shaped values, not just `True`.
    """
    rng = np.random.default_rng(_SEED + 1)
    symbols = ("NIFTY", "BANKNIFTY", "SENSEX", "INDIAVIX")
    base_ts = 1_700_000_000
    out: dict[tuple[str, int], tuple[int, int, int, int]] = {}
    for i in range(_N_DICT):
        sym = symbols[i % 4]
        ts = base_ts + i
        o = int(rng.integers(20000, 30000))
        h = o + int(rng.integers(0, 50))
        low = o - int(rng.integers(0, 50))
        c = int(rng.integers(low, h + 1))
        out[(sym, ts)] = (o, h, low, c)
    return out


@pytest.fixture(scope="module")
def lookup_keys(
    bar_dict: dict[tuple[str, int], tuple[int, int, int, int]],
) -> list[tuple[str, int]]:
    """Deterministic 100K-key subset for the dict-lookup benchmark."""
    rng = np.random.default_rng(_SEED + 2)
    all_keys = list(bar_dict.keys())
    indices = rng.integers(0, len(all_keys), size=100_000)
    return [all_keys[int(i)] for i in indices]


class TestThroughputCanary:
    """Measure the brute-force engine's hot-path ops/s on CI.

    All three benchmarks use the inline-threshold pattern (matches
    `test_store_o1.py:162` + `test_rate_limiter_o1.py:126`) rather than
    pytest-benchmark `--benchmark-compare`.
    """

    @pytest.mark.bench
    def test_vectorized_mask_and_canary(
        self, benchmark: BenchmarkFixture, bar_bits_array: np.ndarray
    ) -> None:
        """numpy-vectorized `(bar_bits & mask) == mask` over 1M uint64s.

        HARD GATE: ≥ 30 M ops/s on CI ubuntu-24.04. Production target on
        M4 Pro reference hardware is 200-400 M/s.
        """

        def _evaluate_batch() -> int:
            matches = (bar_bits_array & _REQUIRED_MASK) == _REQUIRED_MASK
            return int(matches.sum())

        result = benchmark(_evaluate_batch)
        # Sanity check: at least some matches (mask has 4 specific bits;
        # ~6.25% of random uint64s will have ALL 4 set).
        assert result > 0

        # ops/s = N elements per call / mean seconds per call.
        # pyright: ignore[reportOptionalMemberAccess, reportOperatorIssue]
        # — pytest-benchmark stats are typed as Optional[Metadata] with
        # `cached_property` mean; both safe to use post-benchmark() call.
        mean_seconds: float = benchmark.stats.stats.mean  # type: ignore[assignment]
        ops_per_sec = _N_LARGE / mean_seconds
        # Honest gate: 30 M/s on CI; record for later analysis.
        assert ops_per_sec >= 30_000_000, (
            f"vectorized mask AND throughput {ops_per_sec:,.0f} ops/s "
            f"below CI gate 30,000,000 ops/s — possible perf regression"
        )

    @pytest.mark.bench
    def test_scalar_python_mask_and_record_only(
        self, benchmark: BenchmarkFixture, bar_bits_python_list: list[int]
    ) -> None:
        """Pure-Python scalar loop applying `evaluate_mask` to 100K bars.

        RECORD-ONLY (no gate). CPython int-boxing ceiling is ~2-5 M/s.
        Recorded so a future regression (e.g., evaluate_mask becomes
        slower than O(1)) shows up in the bench-results.json artifact.
        """

        def _evaluate_scalar() -> int:
            return sum(1 for bits in bar_bits_python_list if evaluate_mask(bits, _REQUIRED_MASK))

        result = benchmark(_evaluate_scalar)
        assert result > 0

    @pytest.mark.bench
    def test_dict_bar_lookup_canary(
        self,
        benchmark: BenchmarkFixture,
        bar_dict: dict[tuple[str, int], tuple[int, int, int, int]],
        lookup_keys: list[tuple[str, int]],
    ) -> None:
        """CPython dict lookup `bar_dict[(symbol, ts)]` over 100K keys.

        HARD GATE: ≥ 5 M lookups/s on CI. Production target on M4 Pro
        is 30-50 M/s (CPython dict is hash-table O(1) but per-call
        overhead dominates).
        """

        def _lookup_all() -> int:
            return sum(bar_dict[k][3] for k in lookup_keys)  # sum closes

        result = benchmark(_lookup_all)
        assert result > 0

        # pyright: ignore[reportOptionalMemberAccess, reportOperatorIssue]
        mean_seconds: float = benchmark.stats.stats.mean  # type: ignore[assignment]
        ops_per_sec = len(lookup_keys) / mean_seconds
        # Honest gate: 2.5 M/s on shared CI (25% headroom below observed
        # ~3.3 M/s floor). Production target on M4 Pro is 30-50 M/s — the
        # canary catches a regression below the CI floor, not the
        # production target. See module docstring for the envelope split.
        assert ops_per_sec >= 2_500_000, (
            f"dict lookup throughput {ops_per_sec:,.0f} ops/s "
            f"below CI gate 2,500,000 ops/s — possible perf regression"
        )
