"""O(1)/non-quadratic benches for the hardened combo canonicaliser.

DEC-STRATEGIES-CANONICAL-HARDEN. The hardening PR added REJECT-ONLY guards to
``brutex/strategies/combo.py`` (``\x1f`` injection, non-finite float/Decimal,
float-subclass, top-level Mapping, and a nesting-depth cap). None of them may
introduce per-call growing state or super-linear payload scaling.

Two gates, mirroring the ``test_blake3_combo_hash_o1.py`` two-tier pattern
(robust ``< ceiling`` on shared CI, decreed ``1.1×`` on the M4-Pro strict tier):

* :func:`combo_hash` per-call latency is independent of how many combos were
  hashed before it (Golden Rule #5: O(1) per combo wrt sweep cardinality N).
* :func:`canonicalise_params` is linear (not quadratic) in payload node count —
  the depth-cap recursion adds one bounded ``_depth`` int per frame, never a
  per-node rescan.
"""

from __future__ import annotations

import time
from typing import Final

from brutex.strategies.combo import canonicalise_params, combo_hash

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


def _flat_params(n: int) -> dict[str, int]:
    """A flat params dict with *n* integer nodes."""
    return {f"p{i}": i for i in range(n)}


class TestComboHashPerComboO1:
    """``combo_hash`` per-call cost is independent of sweep cardinality."""

    def test_combo_hash_per_combo_independent_of_grid_size(self) -> None:
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
            f"exceeds the ceiling — the hardened path may have acquired growing "
            f"per-call state. t_1k={t_small:.1f}ns, t_100k={t_large:.1f}ns."
        )


class TestCanonicaliseNotQuadratic:
    """``canonicalise_params`` scales linearly in payload node count."""

    def test_canonicalise_not_quadratic(self) -> None:
        small = _flat_params(1_000)
        large = _flat_params(10_000)

        def _time(payload: dict[str, int], reps: int) -> float:
            for _ in range(3):  # warm
                _ = canonicalise_params(payload)
            t0 = time.perf_counter_ns()
            for _ in range(reps):
                _ = canonicalise_params(payload)
            t1 = time.perf_counter_ns()
            return (t1 - t0) / reps

        t_small = _time(small, 200)
        t_large = _time(large, 200)
        # 10× the nodes — a linear canonicaliser is ~10×; quadratic would be
        # ~100×. A generous < 5× per-node ratio (t_large/node vs t_small/node)
        # proves it is not super-linear.
        per_node_small = t_small / 1_000
        per_node_large = t_large / 10_000
        ratio = per_node_large / max(per_node_small, 1.0)
        assert ratio < 5.0, (
            f"canonicalise_params per-node cost ratio {ratio:.2f}× (10k vs 1k "
            f"nodes) exceeds 5× — the canonicaliser may be quadratic. "
            f"t_1k={t_small:.1f}ns, t_10k={t_large:.1f}ns."
        )
