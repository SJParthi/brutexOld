"""Unit tests for the multi-key pool-admission union (`brutex.engine.pool_union`).

LANE W1 item 3 (DEC-RANK-POOL-UNION-ADMIT-001) — 100% line+branch of the ONE
shared admission law both the Stage-3 and cascade seams consume: the fixed
source order, the per-source better-first keys (incl. the frontier
qualification filter + the reverse-preset negation), the interleaved
round-robin dedup-union (single-source reduction == the legacy top-K prefix —
the union-vs-single-key parity pin), and the emitted pool's unchanged
Sharpe-law ordering discipline.
"""

from __future__ import annotations

import random
from decimal import Decimal

import pytest
from brutex.engine.pool_union import (
    POOL_UNION_SOURCE_ORDER,
    axis_better_key,
    rank_record_of_row,
    round_robin_union,
    union_select_rows,
)
from brutex.engine.results_store import ResultRow
from brutex.ranking.presets import PRESETS, dd_band_index, resolve_preset


def _rid(seed: int) -> str:
    return f"{seed:064x}"


def _row(
    seed: int,
    *,
    sharpe: float = 1.5,
    max_drawdown_pct: float = 12.5,
    win_rate: float = 0.625,
    pareto_member: bool | None = None,
) -> ResultRow:
    return ResultRow(
        run_id=_rid(seed),
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
        sharpe_ratio=sharpe,
        sortino_ratio=2.0,
        calmar_ratio=1.25,
        profit_factor=1.8,
        win_rate=win_rate,
        max_drawdown_pct=max_drawdown_pct,
        avg_entry_price=Decimal("14020.50"),
        avg_exit_price=Decimal("14100.25"),
        pareto_member=pareto_member,
    )


class TestSourceOrder:
    def test_fixed_vocabulary_sharpe_first(self) -> None:
        # Sharpe MUST stay source #1 — the single-source reduction is the
        # legacy admission (the parity law below leans on this).
        assert POOL_UNION_SOURCE_ORDER == ("sharpe", "preset", "dd", "win_rate", "frontier")


class TestRankRecordOfRow:
    def test_projection_carries_row_metrics_and_uniform_standins(self) -> None:
        row = _row(7, sharpe=2.0, max_drawdown_pct=8.5, win_rate=0.7)
        record = rank_record_of_row(row)
        assert record.run_id == row.run_id
        assert record.max_drawdown_pct == 8.5
        assert record.win_rate == 0.7
        assert record.profit_factor == row.profit_factor
        assert record.calmar_ratio == row.calmar_ratio
        # The disclosed uniform stand-ins (aggregate-side quantities).
        assert record.composite_score is None
        assert record.max_consecutive_losses == 0


class TestAxisBetterKey:
    def test_sharpe_key_is_negated_sharpe(self) -> None:
        preset = resolve_preset("min_dd_first")
        assert axis_better_key("sharpe", _row(1, sharpe=2.5), preset=preset) == (-2.5,)

    def test_dd_key_is_raw_dd(self) -> None:
        preset = resolve_preset("min_dd_first")
        assert axis_better_key("dd", _row(1, max_drawdown_pct=7.25), preset=preset) == (7.25,)

    def test_win_rate_key_is_negated(self) -> None:
        preset = resolve_preset("min_dd_first")
        assert axis_better_key("win_rate", _row(1, win_rate=0.8), preset=preset) == (-0.8,)

    def test_preset_key_ascending_preset_passes_through(self) -> None:
        # min_dd_first (reverse=False): the preset key rides as-is — the
        # banded-DD 4-tuple over the row projection.
        preset = resolve_preset("min_dd_first")
        key = axis_better_key("preset", _row(1, max_drawdown_pct=12.5), preset=preset)
        assert key == (dd_band_index(12.5), 1, -0.0, 0)

    def test_preset_key_descending_preset_is_negated(self) -> None:
        # A reverse=True preset (win_rate) is negated into the one ascending
        # better-first convention: better (larger) raw value → smaller key.
        preset = PRESETS["win_rate"]
        better = axis_better_key("preset", _row(1, win_rate=0.9), preset=preset)
        worse = axis_better_key("preset", _row(2, win_rate=0.4), preset=preset)
        assert better is not None and worse is not None
        assert better < worse

    def test_frontier_requires_persisted_true(self) -> None:
        preset = resolve_preset("min_dd_first")
        member = _row(1, sharpe=1.25, pareto_member=True)
        assert axis_better_key("frontier", member, preset=preset) == (-1.25,)
        # None (pre-W1 store) AND False (tagged non-member) both disqualify.
        assert axis_better_key("frontier", _row(2, pareto_member=None), preset=preset) is None
        assert axis_better_key("frontier", _row(3, pareto_member=False), preset=preset) is None

    def test_unknown_source_refused(self) -> None:
        with pytest.raises(ValueError, match="unknown admission source"):
            axis_better_key("bogus", _row(1), preset=resolve_preset("min_dd_first"))


class TestRoundRobinUnion:
    def test_single_source_is_the_legacy_prefix(self) -> None:
        # THE union-vs-single-key parity law: one source ⇒ set(source[:cap]).
        assert round_robin_union([[5, 3, 9, 1]], cap=2) == {5, 3}
        assert round_robin_union([[5, 3, 9, 1]], cap=10) == {5, 3, 9, 1}

    def test_interleaves_sources_in_order(self) -> None:
        # Round 1 admits each source's best in order; cap stops mid-round.
        assert round_robin_union([[1, 2], [3, 4], [5, 6]], cap=3) == {1, 3, 5}
        assert round_robin_union([[1, 2], [3, 4], [5, 6]], cap=2) == {1, 3}

    def test_admitted_candidates_do_not_consume_a_turn(self) -> None:
        # The cursor skips already-admitted ids WITHOUT spending the
        # source's turn — source 2's duplicate 1 falls through to its 7.
        assert round_robin_union([[1, 2], [1, 7]], cap=3) == {1, 2, 7}

    def test_exhausted_sources_are_skipped(self) -> None:
        # A short source runs dry; the others keep contributing.
        assert round_robin_union([[1], [2, 3, 4]], cap=4) == {1, 2, 3, 4}

    def test_all_sources_exhausted_below_cap(self) -> None:
        assert round_robin_union([[1], [1], []], cap=5) == {1}
        assert round_robin_union([], cap=5) == set()

    def test_cap_below_one_refused(self) -> None:
        with pytest.raises(ValueError, match="cap must be >= 1"):
            round_robin_union([[1]], cap=0)


class TestUnionSelectRows:
    def test_sharpe_only_reduction_matches_the_legacy_slice(self) -> None:
        # The parity pin: sources=("sharpe",) == today's two-pass top-K law.
        rows = [_row(i, sharpe=float(i % 4)) for i in range(1, 10)]
        legacy = sorted(rows, key=lambda row: row.run_id)
        legacy.sort(key=lambda row: row.sharpe_ratio, reverse=True)
        for cap in (1, 3, 9, 20):
            assert union_select_rows(rows, cap=cap, sources=("sharpe",)) == tuple(legacy[:cap])

    def test_pool_at_or_below_cap_admits_everything_in_legacy_order(self) -> None:
        rows = [_row(i, sharpe=float(i)) for i in (3, 1, 2)]
        selected = union_select_rows(rows, cap=5)
        assert [r.sharpe_ratio for r in selected] == [3.0, 2.0, 1.0]

    def test_dd_optimal_row_beyond_the_sharpe_cap_is_admitted(self) -> None:
        # The motivating shape: the DD-best row is Sharpe-last — union in.
        rows = [_row(i, sharpe=float(10 + i), max_drawdown_pct=20.0) for i in range(1, 6)]
        dd_optimal = _row(99, sharpe=0.5, max_drawdown_pct=2.0, win_rate=0.9)
        rows.append(dd_optimal)
        selected = union_select_rows(rows, cap=3)
        assert dd_optimal in selected
        assert len(selected) == 3
        # Emitted in TODAY's discipline: Sharpe desc, run_id asc ties.
        sharpes = [r.sharpe_ratio for r in selected]
        assert sharpes == sorted(sharpes, reverse=True)

    def test_frontier_source_included_and_nonmembers_filtered(self) -> None:
        rows = [_row(i, sharpe=float(10 + i), max_drawdown_pct=2.0, win_rate=0.9) for i in range(1, 7)]
        frontier = _row(50, sharpe=0.1, max_drawdown_pct=28.0, win_rate=0.1, pareto_member=True)
        rows.append(frontier)
        assert frontier in union_select_rows(rows, cap=5)
        # With the frontier claim removed the same row is NOT admitted.
        no_claim = [
            _row(50, sharpe=0.1, max_drawdown_pct=28.0, win_rate=0.1) if r is frontier else r
            for r in rows
        ]
        admitted = union_select_rows(no_claim, cap=5)
        assert all(r.run_id != _rid(50) for r in admitted)

    def test_explicit_preset_overrides_the_default(self) -> None:
        # Threading a preset swaps source #2's law (win_rate preset ⇒ the
        # high-win-rate row is admitted on the preset turn).
        rows = [_row(i, sharpe=float(10 + i), win_rate=0.2) for i in range(1, 6)]
        winner = _row(77, sharpe=0.5, win_rate=0.95, max_drawdown_pct=20.0)
        rows.append(winner)
        selected = union_select_rows(rows, cap=2, preset=PRESETS["win_rate"])
        assert winner in selected

    def test_discovery_order_free(self) -> None:
        # The selection is a pure function of the row SET (total-order keys
        # everywhere) — any input permutation yields the identical pool.
        rng = random.Random(42)
        rows = [
            _row(
                i,
                sharpe=float(rng.randrange(5)),
                max_drawdown_pct=float(rng.randrange(1, 30)),
                win_rate=rng.randrange(1, 100) / 100.0,
                pareto_member=rng.choice((None, True, False)),
            )
            for i in range(1, 40)
        ]
        baseline = union_select_rows(rows, cap=7)
        for _ in range(5):
            rng.shuffle(rows)
            assert union_select_rows(rows, cap=7) == baseline
