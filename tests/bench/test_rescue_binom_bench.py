"""Bench pins for the rescue lane's binomial kernel — the LINEAR envelope.

DEC-STAGE1B-LOW-SUPPORT-RESCUE-001 (LANE W3). The BORN-RUST
``fno_math.stats_binom_tail_log_sf`` is honestly **O(n) per call** (module
docstring — never an O(1)-total claim): this file pins (a) the PER-ELEMENT
cost is FLAT as ``n`` grows 100× (i.e. total time is linear, no hidden
super-linear term), and (b) the production-band call (``n < 30``) is
micro-scale — the rescue gate can never become a sweep-wall term.

Tier note (the blake3-bench precedent): NOT in bench-m4pro.yml's curated
strict list, so the shared robust ceiling is the operative gate.
"""

from __future__ import annotations

import time

from brutex.math import stats_binom_tail_log_sf

from tests.bench._o1_tiers import ratio_ceiling

_SMALL_N = 1_000
_LARGE_N = 100_000
_REPS = 5


def _best_per_element_seconds(n: int) -> float:
    """Best-of-N per-ELEMENT wall time for one ln-sf call at size *n*."""
    best = float("inf")
    for _ in range(_REPS):
        start = time.perf_counter()
        stats_binom_tail_log_sf(n, 1, 0.5)  # k=1 ⇒ the full O(n) tail walk
        elapsed = time.perf_counter() - start
        best = min(best, elapsed / n)
    return best


def test_per_element_cost_is_flat_across_100x_n() -> None:
    """LINEARITY pin: per-element time at 100×n stays within the robust tier
    ceiling of the small-n per-element time (a super-linear regression —
    e.g. an accidental per-term re-derivation of ln C(n, k) — blows it)."""
    small = _best_per_element_seconds(_SMALL_N)
    large = _best_per_element_seconds(_LARGE_N)
    assert large <= small * ratio_ceiling(10.0), (
        f"per-element cost grew {large / small:.2f}× from n={_SMALL_N} to "
        f"n={_LARGE_N} — the kernel is no longer linear"
    )


def test_production_band_call_is_micro_scale() -> None:
    """The rescue band is n < 30: 1,000 gate calls must stay far under a
    generous 250 ms shared-CI budget (measured ~1-2 ms — pure headroom,
    never a tight flake bound)."""
    start = time.perf_counter()
    for _ in range(1_000):
        stats_binom_tail_log_sf(29, 22, 0.5)
    elapsed = time.perf_counter() - start
    assert elapsed < 0.25, f"1,000 band calls took {elapsed:.3f}s"
