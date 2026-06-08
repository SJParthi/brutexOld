"""O(1) benchmark for the BLAKE3 combo-identity hash.

DEC-O1-BENCH-GAPS-001 (G1 of the 4-gap deep audit). CLAUDE.md Golden Rule #4
(reproducibility) identifies every combo by a BLAKE3 digest of its 9-field
identity (``brutex/strategies/combo.py:combo_hash`` →
``fno_math.blake3_hash``), and Rule #5 requires combo dedup to be O(1). The
deep audit flagged the hash primitive as a YELLOW gap: architecturally O(1) per
fixed-size combo, but never directly benched.

This closes it. :func:`combo_hash` is a **stateless pure function** over a
fixed-width canonical payload, so its per-call latency must be:

* independent of how many combos were hashed before it (no accumulating state /
  accidental memoization that would make sweep-wide dedup hashing super-linear),
  and
* constant for the canonical 9-field identity (the brute-force sweep hashes one
  such identity per combo).

A regression that introduced per-call growth (e.g. an unbounded cache keyed by
payload) would blow the ratio and fail the gate.

Mirrors the ``_o1_tiers`` two-tier pattern: robust ``< 5×`` on shared CI,
decreed ``1.1×`` on the M4-Pro strict tier.
"""

from __future__ import annotations

import time
from typing import Final

from brutex.strategies.combo import combo_hash

from tests.bench._o1_tiers import ratio_ceiling

#: Per-call latency must not grow between these two call-batch sizes.
_SMALL = 1_000
_LARGE = 100_000

#: A canonical 9-field combo identity (the real dedup primitive's input).
_PARAMS: Final[dict[str, object]] = {"k": 2, "sl": 1500, "tp": 3000, "min_hits": 30}


def _hash_once(i: int) -> str:
    """Hash one canonical combo identity (vary only the strategy id)."""
    return combo_hash(
        strategy_id=f"mask-{i & 0xFFFF}",
        direction="BUY",
        instrument="NSE:NIFTY",
        tf="5minute",
        mode="signal",
        params=_PARAMS,
        data_snapshot_hash="a" * 64,
        indicator_version="v1",
        code_commit_sha="b" * 40,
    )


class TestComboHashIsO1:
    """``combo_hash`` is O(1) per call — stateless, no growth over a sweep."""

    def test_hash_is_64_hex(self) -> None:
        digest = _hash_once(0)
        assert len(digest) == 64 and int(digest, 16) >= 0

    def test_hash_at_small_scale(self, benchmark) -> None:
        assert len(benchmark(lambda: _hash_once(7))) == 64

    def test_hash_at_large_scale(self, benchmark) -> None:
        # Same per-call work as small scale; benchmarked separately so the
        # harness records both points (the ratio test below is the gate).
        assert len(benchmark(lambda: _hash_once(123_457))) == 64

    def test_hash_ratio_below_5x(self) -> None:
        """Per-call latency over a 100k batch must match a 1k batch (< 5×)."""

        def _time(n: int) -> float:
            for i in range(50):  # warm
                _ = _hash_once(i)
            t0 = time.perf_counter_ns()
            for i in range(n):
                _ = _hash_once(i)
            t1 = time.perf_counter_ns()
            return (t1 - t0) / n

        t_small = _time(_SMALL)
        t_large = _time(_LARGE)
        ratio = t_large / max(t_small, 1.0)
        assert ratio < ratio_ceiling(5.0), (
            f"combo_hash per-call latency ratio {ratio:.2f}× (100k vs 1k calls) "
            f"exceeds 5× — the hash may have acquired growing per-call state "
            f"(O(1) dedup-hashing regression). "
            f"t_1k={t_small:.1f}ns, t_100k={t_large:.1f}ns."
        )
