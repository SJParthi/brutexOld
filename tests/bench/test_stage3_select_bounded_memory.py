"""Bounded-memory bench for the Stage-3 survivor selection (`stage3_select`).

DEC-SWEEP-STREAMING-RANK-002 (#1294, part 2 of DEC-SWEEP-STREAMING-RANK-001
#1184/#1187). ``group_survivors`` used to ACCUMULATE every candidate row per
:class:`LeaderboardKey` and two-pass sort — O(candidates) RAM. It now streams
the rows through a bounded per-key top-K heap, so for a FIXED ``top_k`` the
retained state is O(top_k) per key and the tracemalloc PEAK must be FLAT in
the candidate count: selecting from 100k streamed candidates may not peak
meaningfully above selecting from 10k (a 10× input growth). The old
implementation retained all N rows and fails this bound by construction.

HONEST envelope (Golden Rule #10): this is a tracemalloc (Python-allocator)
peak bound, NOT an RSS/absolute-memory claim; rows are STREAMED via a
generator (the function's contract — one pass over an iterable), because a
caller who materialises the full list first pays O(N) for the list itself
regardless of the selection algorithm (``select_stage3_survivors``'s
``load_results_tree`` upstream is such a caller and is out of this PR's
scope, named in the ledger). Byte-identity of the selection is the UNIT
suite's golden-equality job, not this bench's.

The gate's TEETH are executable, per the repo precedent
(DEC-FETCH-PER-WINDOW-GUARANTEE-001, MIGRATION.md #1026):
``test_gate_has_teeth_accumulate_all_exceeds_ceiling`` runs the pre-#1294
accumulate-all shape through the SAME harness and asserts it BLOWS the
ceiling — so the bound can never silently go vacuous.
"""

from __future__ import annotations

import gc
import tracemalloc
from collections.abc import Callable, Iterator
from decimal import Decimal

from brutex.engine.results_store import ResultRow
from brutex.engine.stage3_select import LeaderboardKey, group_survivors

_Boards = dict[LeaderboardKey, tuple[ResultRow, ...]]
_SelectFn = Callable[[Iterator[ResultRow]], _Boards]

_SMALL = 10_000
_LARGE = 100_000
_TOP_K = 64

#: Flat-peak ceiling: a 10× candidate growth may not even double the traced
#: peak (measured ratio ≈ 1.0 — the heap holds the SAME 64 entries either
#: way; per-row allocations are transient). The pre-#1294 accumulate-all
#: implementation measures ≈ 10× here — PINNED executably by the teeth test
#: below, not just this comment — so the bound has real teeth while
#: carrying generous headroom against allocator/CI noise.
_PEAK_RATIO_CEILING = 2.0

_ENTRY = Decimal("14020.50")
_EXIT = Decimal("14100.25")


def _candidates(n: int) -> Iterator[ResultRow]:
    """Yield *n* rows one at a time — churny Sharpe so the heap keeps evicting."""
    for i in range(n):
        yield ResultRow(
            run_id=f"{i:064x}",
            instrument="NIFTY",
            tf="5m",
            direction="bull",
            mode="A",
            mask_bits=0b101,
            net_pnl_paisa=1_000,
            gross_profit_paisa=1_500,
            gross_loss_paisa=500,
            max_drawdown_paisa=250,
            expectancy_paisa=25,
            total_trades=40,
            winning_trades=25,
            losing_trades=15,
            # Knuth-hash spread: late rows keep beating the heap root, so the
            # eviction path (not just the fill path) dominates the run.
            sharpe_ratio=float((i * 2654435761) % 1_000_003),
            sortino_ratio=2.0,
            calmar_ratio=1.25,
            profit_factor=1.8,
            win_rate=0.625,
            max_drawdown_pct=12.5,
            avg_entry_price=_ENTRY,
            avg_exit_price=_EXIT,
        )


def _select_bounded(rows: Iterator[ResultRow]) -> _Boards:
    """The SHIPPED bounded-heap selection under measurement."""
    return group_survivors(rows, top_k=_TOP_K)


def _select_accumulate_all(rows: Iterator[ResultRow]) -> _Boards:
    """TEETH oracle — the pre-#1294 accumulate-ALL-then-two-pass-sort shape.

    Mirrors the unit suite's ``_group_survivors_reference`` (the verbatim
    old implementation) but consumes the SAME streamed iterator as the
    shipped path, so the two arms differ ONLY in retention strategy. Kept
    in the TEST only — it exists to prove the flat-peak gate is not
    vacuous (a revert to accumulate-all must blow the ceiling).
    """
    grouped: dict[LeaderboardKey, list[ResultRow]] = {}
    for row in rows:
        key = LeaderboardKey(instrument=row.instrument, tf=row.tf, mode=row.mode)
        grouped.setdefault(key, []).append(row)
    selected: _Boards = {}
    for key in sorted(grouped, key=lambda k: (k.instrument, k.tf, k.mode)):
        board = sorted(grouped[key], key=lambda r: r.run_id)
        board.sort(key=lambda r: r.sharpe_ratio, reverse=True)
        selected[key] = tuple(board[:_TOP_K])
    return selected


def _peak_bytes(n: int, select: _SelectFn = _select_bounded) -> int:
    """Traced allocation peak of one streamed selection over *n* candidates."""
    gc.collect()
    tracemalloc.start()
    try:
        boards = select(_candidates(n))
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    (board,) = boards.values()
    assert len(board) == _TOP_K  # the selection genuinely ran + trimmed
    return peak


class TestGroupSurvivorsPeakRamIsBoundedInCandidates:
    def test_peak_is_flat_from_10k_to_100k_candidates(self) -> None:
        """10× more streamed candidates → traced peak within the flat ceiling."""
        _peak_bytes(_SMALL)  # warm-up: one-time caches/interning off the books
        small = _peak_bytes(_SMALL)
        large = _peak_bytes(_LARGE)
        ratio = large / small
        assert ratio <= _PEAK_RATIO_CEILING, (
            f"group_survivors peak RAM grew with candidate count: "
            f"{small} B @ {_SMALL} rows -> {large} B @ {_LARGE} rows "
            f"(ratio {ratio:.2f} > {_PEAK_RATIO_CEILING}) — the top-K heap "
            f"bound (DEC-SWEEP-STREAMING-RANK-002) has regressed"
        )

    def test_gate_has_teeth_accumulate_all_exceeds_ceiling(self) -> None:
        """The pre-#1294 accumulate-all shape BLOWS the same ceiling (~10×).

        Runs the old retention strategy through the IDENTICAL harness
        (same generator, same K, same ratio law) and asserts it FAILS the
        bound — proof the flat-peak gate is not vacuous and a silent
        revert to accumulate-all cannot pass it. Repo precedent for
        executable teeth: DEC-FETCH-PER-WINDOW-GUARANTEE-001 (#1026).
        """
        _peak_bytes(_SMALL, _select_accumulate_all)  # warm-up, same as above
        small = _peak_bytes(_SMALL, _select_accumulate_all)
        large = _peak_bytes(_LARGE, _select_accumulate_all)
        ratio = large / small
        assert ratio > _PEAK_RATIO_CEILING, (
            f"teeth lost: the accumulate-all reference peaked {small} B @ "
            f"{_SMALL} rows -> {large} B @ {_LARGE} rows (ratio {ratio:.2f} "
            f"<= {_PEAK_RATIO_CEILING}) — the harness no longer distinguishes "
            f"O(candidates) retention, so the flat-peak gate is vacuous"
        )
