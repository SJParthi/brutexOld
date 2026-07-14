"""O(1) benchmarks for the results store (`brutex.engine.results_store`).

Enforces the O(1) decree (CLAUDE.md §2 r5 / §12) for the store's two hot paths:

* :meth:`ResultsStore.get` — run-identity lookup must be latency-independent of
  how many rows are stored (a dict probe, not a scan);
* :meth:`ResultsStore.append` — per-row insert must be amortised O(1)
  (dict membership + insert), independent of the current row count.

Mirrors ``tests/bench/test_lmdb_membership_o1.py``: empirical ratio at 10k vs 1k
with CI-jitter headroom (5× on shared CI). BOTH ``get`` and ``append`` route
their M4-Pro strict ceiling through
:func:`tests.bench._o1_tiers.cache_tier_ceiling` (the DEC-O1-CACHE-TIER-001
measured 3× cache allowance): a 10k-row store's working set crosses a CPU cache
tier that a 1k-row one does not, so the amortised probe/insert picks up a small
constant memory-hierarchy factor (a cache-cliff not an O(n) regression) — the
same allowance the throughput canary routes its 1M-elem paths through
(DEC-O1-BENCH-GATE-COMPLETE-001). ``get`` was rerouted from the raw strict 1.1×
after a measured singleton overshoot on the quiet M4-Pro runner
(DEC-O1-CACHE-TIER-MARGINAL-001; see the in-test comment). A scan regression
would blow the ratio well past 3× and fail.
"""

from __future__ import annotations

import time
from decimal import Decimal
from pathlib import Path

from brutex.engine.results_store import ResultRow, ResultsStore

from tests.bench._o1_tiers import cache_tier_ceiling

_SMALL = 1_000
_LARGE = 10_000
#: The DEEP-depth pin's row count (DEC-O1-GUARANTEE-HARDEN-001 H3): 100× the
#: small store, so a genuine O(n) scan reads ~100× — no cache-tier allowance
#: can absorb it — while the honest dict probe stays flat (measured 51.7 ns at
#: BOTH 1k and 100k in the 2026-07-13 audit sandbox).
_DEEP = 100_000


def _rid(seed: int) -> str:
    return f"{seed:064x}"


def _row(seed: int) -> ResultRow:
    return ResultRow(
        run_id=_rid(seed),
        instrument="NIFTY",
        tf="5m",
        direction="bull",
        mode="A",
        mask_bits=seed % (1 << 52),
        net_pnl_paisa=seed,
        gross_profit_paisa=seed,
        gross_loss_paisa=0,
        max_drawdown_paisa=0,
        expectancy_paisa=0,
        total_trades=1,
        winning_trades=1,
        losing_trades=0,
        sharpe_ratio=float(seed % 7),
        sortino_ratio=0.0,
        calmar_ratio=0.0,
        profit_factor=1.0,
        win_rate=1.0,
        max_drawdown_pct=0.0,
        avg_entry_price=Decimal("1.00"),
        avg_exit_price=Decimal("2.00"),
    )


def _filled(n: int) -> ResultsStore:
    store = ResultsStore(path=Path("unused.parquet"))
    store.extend(_row(i) for i in range(n))
    return store


class TestGetIsO1:
    def test_get_at_small_scale(self, benchmark) -> None:  # type: ignore[no-untyped-def]
        store = _filled(_SMALL)
        probe = _rid(_SMALL // 2)
        result = benchmark(lambda: store.get(probe))
        assert result is not None

    def test_get_at_large_scale(self, benchmark) -> None:  # type: ignore[no-untyped-def]
        store = _filled(_LARGE)
        probe = _rid(_LARGE // 2)
        result = benchmark(lambda: store.get(probe))
        assert result is not None

    def test_get_ratio_is_o1(self) -> None:
        """`get` latency at 10k / 1k rows must stay O(1) (< ceiling)."""

        def _time(n: int, *, iterations: int = 5_000) -> float:
            store = _filled(n)
            probe = _rid(n // 2)
            for _ in range(50):  # warm
                store.get(probe)
            t0 = time.perf_counter_ns()
            for _ in range(iterations):
                store.get(probe)
            t1 = time.perf_counter_ns()
            return (t1 - t0) / iterations

        t_small = _time(_SMALL)
        t_large = _time(_LARGE)
        ratio = t_large / max(t_small, 1.0)
        # DEC-O1-CACHE-TIER-MARGINAL-001: routed through cache_tier_ceiling
        # (3.0× on M4-Pro strict, 5× shared CI) after a measured singleton
        # overshoot of the raw strict 1.1×: 1.134× (t_1k=31ns → t_10k=35ns)
        # on the quiet M4-Pro runner, 2026-07-04, PR #1085 head 291605e (job
        # 85094686407) — a PR touching neither this module nor ResultsStore —
        # and PASSED on the immediate rerun (job 85094981848), proving
        # dict-probe memory-hierarchy noise across the 1k→10k cache-tier
        # boundary, not a scan (same physics as DEC-O1-CACHE-TIER-001 for the
        # append sibling below). A genuine O(n) scan reads 10×+, far above
        # the 3.0× tier, so the O(1) oracle keeps real teeth.
        assert ratio < cache_tier_ceiling(5.0), (
            f"ResultsStore.get ratio {ratio:.2f}× (10k vs 1k) exceeds 5× — "
            f"lookup may have regressed to a scan. "
            f"t_1k={t_small:.0f}ns, t_10k={t_large:.0f}ns."
        )

    def test_get_ratio_is_o1_deep_depth(self) -> None:
        """`get` latency at 100k / 1k rows stays O(1) — the DEEP-depth pin.

        DEC-O1-GUARANTEE-HARDEN-001 (H3). TRIAGE NOTE for the next
        strict-tier ratio report on this path (write it here so the next
        "1.87×" ticket resolves at the test, not in a fresh investigation):

        * ``ResultsStore.get`` is a ONE-LINE in-memory ``dict.get``
          (results_store.py) — there is no parse/scan/I/O to creep. Measured
          in the 2026-07-13 audit sandbox at 51.7 ns (1k) / 53.5 ns (10k) /
          51.7 ns (100k): dead flat at 100× depth (ratios 1.034× / 0.999×).
        * a sub-3× strict-tier reading (e.g. the ledgered 1.134× of
          MIGRATION.md #1301, DEC-O1-CACHE-TIER-MARGINAL-001, or a
          hypothetical 1.87×) is dict-probe CACHE-TIER physics — the large
          store's bucket array + row objects spill L2/SLC into DRAM, the
          DEC-O1-CACHE-TIER-001 calibration measured the dict-lookup band at
          up to 2.11× on the real M4-Pro — and PASSES today's gate
          (``cache_tier_ceiling`` → 3.0× strict / 5.0× shared, the #1301
          reroute). A red on an older head predating that reroute is stale
          evidence: confirm the failing job's head imports
          ``cache_tier_ceiling`` in this file before triaging further.
        * a GENUINE scan regression reads ≥10× at 10k-vs-1k and ≥100× here —
          far above every tier; this 100×-depth pin exists so such a
          regression cannot hide inside the cache-tier allowance at the
          shallower 10× spread.
        """

        def _time(n: int, *, iterations: int = 5_000) -> float:
            store = _filled(n)
            probe = _rid(n // 2)
            for _ in range(50):  # warm
                store.get(probe)
            t0 = time.perf_counter_ns()
            for _ in range(iterations):
                store.get(probe)
            t1 = time.perf_counter_ns()
            return (t1 - t0) / iterations

        t_small = _time(_SMALL)
        t_deep = _time(_DEEP)
        ratio = t_deep / max(t_small, 1.0)
        assert ratio < cache_tier_ceiling(5.0), (
            f"ResultsStore.get ratio {ratio:.2f}× (100k vs 1k) exceeds the tier — "
            f"a genuine scan reads ~100× here; a sub-3× reading is cache-tier "
            f"physics (see the triage note). "
            f"t_1k={t_small:.0f}ns, t_100k={t_deep:.0f}ns."
        )


class TestAppendIsO1:
    def test_append_dup_noop_at_small_scale(self, benchmark) -> None:  # type: ignore[no-untyped-def]
        store = _filled(_SMALL)
        existing = _row(_SMALL // 2)
        result = benchmark(lambda: store.append(existing))
        assert result is False

    def test_append_dup_noop_at_large_scale(self, benchmark) -> None:  # type: ignore[no-untyped-def]
        store = _filled(_LARGE)
        existing = _row(_LARGE // 2)
        result = benchmark(lambda: store.append(existing))
        assert result is False

    def test_append_ratio_is_o1(self) -> None:
        """Inserting fresh rows costs the same at 10k base as at 1k base (O(1))."""

        def _time(n: int, *, iterations: int = 2_000) -> float:
            store = _filled(n)
            # Pre-build fresh rows (ids beyond n) so we time append, not validation.
            fresh = [_row(n + i) for i in range(iterations)]
            t0 = time.perf_counter_ns()
            for row in fresh:
                store.append(row)
            t1 = time.perf_counter_ns()
            return (t1 - t0) / iterations

        t_small = _time(_SMALL)
        t_large = _time(_LARGE)
        ratio = t_large / max(t_small, 1.0)
        assert ratio < cache_tier_ceiling(5.0), (
            f"ResultsStore.append ratio {ratio:.2f}× (10k vs 1k base) exceeds 5× — "
            f"insert may have regressed from amortised O(1). "
            f"t_1k={t_small:.0f}ns, t_10k={t_large:.0f}ns."
        )
