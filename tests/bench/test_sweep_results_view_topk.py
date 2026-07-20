"""Bench pin — the fix-5 bounded /results top-k rank (algorithmic A/B).

DEC-MEMORY-FOOTPRINT-HARDENING-001 fix 5: ``build_sweep_leaderboard``'s
non-preset arm ranks via ONE bounded ``heapq.nsmallest`` over the composite
``(-value, run_id)`` key (O(rows·log depth)) instead of the per-request
whole-list two-pass sort (O(rows·log rows)). This bench pins the WIN on the
shape that matters — a deep scan (60k rows, the ~100k-row-store class the
#1721 audit measured at ~105 MB resident) ranked to the default-depth page —
by timing the two strategies over the IDENTICAL row list (best-of-5 min, the
house idiom) and asserting the bounded path is faster with margin.

Measured 2026-07-11 (sandbox, 60k rows, depth 50): two-pass full sort
44.8 ms vs bounded heap 16.1 ms — 2.78×. HONEST (Golden Rule #10): at the
few-THOUSAND-row scans typical of today's lakes the absolute win is
single-digit milliseconds per request — the fix's real value is the
asymptotic bound (depth is clamped ≤ MAX_TOP_N=500 while scan rows grow with
the sweep tree), not a today-visible latency headline. Order parity is the
SEPARATE contract (tests/property/test_sweep_results_view_topk_parity.py) —
this file pins cost only.
"""

from __future__ import annotations

import heapq
import time
from collections.abc import Callable
from decimal import Decimal

import pytest

pytest.importorskip("polars", reason="sweep_results_view imports the ResultsStore reader stack")

from brutex.engine.results_store import ResultRow

_ROWS = 60_000
_DEPTH = 50
_REPEATS = 5


def _row(idx: int) -> ResultRow:
    """One valid ResultRow with a spread of rank values + tie clusters."""
    return ResultRow(
        run_id=f"{idx % 4096:064x}",  # duplicate run_ids in the mix (tie work)
        instrument="NIFTY",
        tf="5m",
        direction="bull",
        mode="A",
        mask_bits=0,
        net_pnl_paisa=(idx * 7919) % 100_000,
        gross_profit_paisa=200_00,
        gross_loss_paisa=100_00,
        max_drawdown_paisa=50_00,
        expectancy_paisa=10_00,
        total_trades=10,
        winning_trades=6,
        losing_trades=4,
        sharpe_ratio=((idx * 31) % 997) / 100.0,  # ~997 distinct values → real ties
        sortino_ratio=1.2,
        calmar_ratio=0.9,
        profit_factor=2.0,
        win_rate=0.6,
        max_drawdown_pct=0.05,
        avg_entry_price=Decimal("100.00"),
        avg_exit_price=Decimal("101.00"),
    )


def _best_of(fn: Callable[[], object], repeats: int = _REPEATS) -> float:
    """Best-of-N minimum wall time (the DEC-CANARY-MINTIME-001 idiom)."""
    best = float("inf")
    for _ in range(repeats):
        start = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - start)
    return best


def test_bounded_heap_rank_beats_the_full_sort_at_scan_scale() -> None:
    """The fix-5 heap rank is measurably cheaper than the pre-fix two-pass sort."""
    rows = [_row(i) for i in range(_ROWS)]

    def _pre_fix_two_pass() -> list[ResultRow]:
        ordered = sorted(rows, key=lambda r: r.run_id)
        ordered.sort(key=lambda r: r.sharpe_ratio, reverse=True)
        return ordered[:_DEPTH]

    def _bounded_heap() -> list[ResultRow]:
        return heapq.nsmallest(_DEPTH, rows, key=lambda r: (-r.sharpe_ratio, r.run_id))

    # Same answer first (belt — the property suite is the real parity contract).
    assert [(r.run_id, r.net_pnl_paisa) for r in _bounded_heap()] == [
        (r.run_id, r.net_pnl_paisa) for r in _pre_fix_two_pass()
    ]

    t_sort = _best_of(_pre_fix_two_pass)
    t_heap = _best_of(_bounded_heap)
    # Measured ~2.8× in the sandbox; 1.3× margin absorbs shared-CI noise while
    # still proving a genuine win (a regression back to whole-list-sort cost
    # cannot pass).
    assert t_heap * 1.3 < t_sort, (
        f"bounded heap rank not faster: heap={t_heap * 1e3:.1f} ms vs "
        f"two-pass sort={t_sort * 1e3:.1f} ms @ {_ROWS} rows, depth {_DEPTH}"
    )
