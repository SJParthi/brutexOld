"""O(1) + bounded-memory benchmarks for the sweep dedup digest (`sweep_cache`).

DEC-ENGINE-DEDUP-NAN-CANON-001 (audit O(1) coverage). The membership-dedup cache
key is :func:`brutex.engine.sweep_cache.combo_digest` — the per-combo
canonicalise-then-BLAKE3 step run once per evaluated combo (~33B times in a full
sweep; CLAUDE.md §2 Rule 5 / §12). This bench pins two scaling invariants of that
hot path:

* **Per-dedup latency is O(1).** One digest costs the same whether it is the
  1,000th or the 100,000th digest computed — a stateless canonicalise + hash, not
  a function of how many combos came before. Routed through the
  single-source-of-truth :func:`tests.bench._o1_tiers.ratio_ceiling` (5× on shared
  CI, the strict 1.1× tier on the M4-Pro runner), mirroring
  ``tests/bench/test_engine_results_store_o1.py``.

* **Bounded memory across N=1k vs 100k.** ``combo_digest`` holds NO state across
  calls (it allocates one transient JSON string + one 32-byte digest per call and
  returns), so computing 100k digests has the SAME steady-state Python-heap
  footprint as 1k — a genuine leak (per-call retained allocation) would grow with
  N and cross the absolute, N-independent ``tracemalloc`` ceiling (a tracemalloc
  recipe modelled on ``tests/stress/test_memory_growth.py``).
"""

from __future__ import annotations

import gc
import time
import tracemalloc

from brutex.engine.sweep_cache import combo_digest

from tests.bench._o1_tiers import ratio_ceiling

_SMALL = 1_000
_LARGE = 100_000

#: A representative FINITE combo with a finite float param (exercises the very
#: ``allow_nan=False`` path the change added).
_PARAMS = {"sl_paisa": 80, "tp_paisa": 50, "prior_n": 3, "proximity": 0.25}

#: Absolute, N-independent net ceiling — combo_digest retains nothing per call,
#: so 100k calls grow the heap by a flat one-time amount. 64 KiB carries large
#: headroom over the measured ~0 net; even 1 byte/call ≈ 100 KB at N=100k would
#: cross it (honest O(1)-memory floor, recalibrate before tightening).
_NET_CEILING_BYTES = 64 * 1024


def _digest(seed: int) -> bytes:
    return combo_digest(
        seed % (1 << 52),
        "NIFTY",
        "5m",
        "bull",
        _PARAMS,
        # The three STRUCTURAL reproducibility hashes
        # (DEC-COMBO-DIGEST-REPRO-HASHES-001) — fixed here; the perf/memory
        # envelope is what these suites measure, not the identity.
        data_snapshot_hash="snap-aaaa",
        indicator_version="ind-v1",
        code_commit_sha="c0ffee01",
    )


class TestComboDigestLatencyIsO1:
    def test_digest_at_small_scale(self, benchmark) -> None:  # type: ignore[no-untyped-def]
        result = benchmark(lambda: _digest(_SMALL // 2))
        assert len(result) == 32

    def test_digest_per_call_ratio_is_o1(self) -> None:
        """Per-digest latency is flat at 100k calls vs 1k calls (stateless O(1))."""

        def _time(n: int) -> float:
            for i in range(50):  # warm
                _digest(i)
            t0 = time.perf_counter_ns()
            for i in range(n):
                _digest(i)
            t1 = time.perf_counter_ns()
            return (t1 - t0) / n

        t_small = _time(_SMALL)
        t_large = _time(_LARGE)
        ratio = t_large / max(t_small, 1.0)
        assert ratio < ratio_ceiling(5.0), (
            f"combo_digest per-call ratio {ratio:.2f}× (100k vs 1k) exceeds 5× — "
            f"the canonicalise+hash dedup key may have regressed from O(1). "
            f"t_1k={t_small:.1f}ns, t_100k={t_large:.1f}ns."
        )


class TestComboDigestMemoryIsBounded:
    def test_memory_flat_across_1k_vs_100k(self) -> None:
        """100k digests hold the same steady-state heap as 1k (no per-call leak)."""
        # UNTRACKED warmup absorbs one-time allocations (interned strings, the
        # blake3 code path, the JSON encoder's internal caches).
        for i in range(2_000):
            _digest(i)
        tracemalloc.start()
        # TRACKED warmup turns the working set over once so the sustained phase is
        # measured tracked-for-tracked.
        for i in range(2_000):
            _digest(i)
        gc.collect()
        base_current, _ = tracemalloc.get_traced_memory()
        tracemalloc.reset_peak()
        # The sustained phase: 100k digests, far more than the 1k baseline.
        for i in range(_LARGE):
            _digest(i)
        gc.collect()
        end_current, _ = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        net = end_current - base_current
        assert net <= _NET_CEILING_BYTES, (
            f"combo_digest net heap grew {net} B over {_LARGE} digests "
            f"(ceiling {_NET_CEILING_BYTES} B) — a per-call retained allocation "
            f"(leak) would scale with N and cross this N-independent floor."
        )

    def test_digests_are_deterministic_across_the_run(self) -> None:
        """Bounded memory must not come at the cost of determinism."""
        first = _digest(12_345)
        # Re-compute after a long run of OTHER digests churns the heap.
        for i in range(_LARGE):
            _digest(i)
        assert _digest(12_345) == first
