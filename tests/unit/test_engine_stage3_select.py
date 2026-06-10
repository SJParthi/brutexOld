"""Unit tests for the Stage-3 survivor selection (`brutex.engine.stage3_select`).

DEC-STAGE3-TOPK-001 (ADR-0007 §5 slice 2) — 100% line+branch of the top-K query
over the persisted leaderboard: per-(instrument, TF, mode) grouping (direction
competes INSIDE a leaderboard, never as a key), the Sharpe-descending /
run_id-ascending deterministic ordering (the proven store discipline), the K
trim + the spec-default constant (10,000 — coded nowhere before this slice),
the ``top_k < 1`` guards on BOTH entry points, the deterministic leaderboard
iteration order, and the real-Parquet disk round-trip through
``load_results_tree`` (missing root raises; empty tree yields {}).
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from brutex.engine.results_store import ResultRow, ResultsStore, ResultsStoreError
from brutex.engine.stage3_select import (
    DEFAULT_STAGE3_TOP_K,
    LeaderboardKey,
    group_survivors,
    select_stage3_survivors,
)


def _rid(seed: int) -> str:
    """Return a valid 64-char lowercase-hex run-identity from *seed*."""
    return f"{seed:064x}"


def _row(**overrides: object) -> ResultRow:
    """Build a valid :class:`ResultRow`, overriding any field by keyword."""
    base: dict[str, object] = {
        "run_id": _rid(1),
        "instrument": "NIFTY",
        "tf": "5m",
        "direction": "bull",
        "mode": "A",
        "mask_bits": 0b101,
        "net_pnl_paisa": 1_000,
        "gross_profit_paisa": 1_500,
        "gross_loss_paisa": 500,
        "max_drawdown_paisa": 250,
        "expectancy_paisa": 25,
        "total_trades": 40,
        "winning_trades": 25,
        "losing_trades": 15,
        "sharpe_ratio": 1.5,
        "sortino_ratio": 2.0,
        "calmar_ratio": 1.25,
        "profit_factor": 1.8,
        "win_rate": 0.625,
        "max_drawdown_pct": 12.5,
        "avg_entry_price": Decimal("14020.50"),
        "avg_exit_price": Decimal("14100.25"),
    }
    base.update(overrides)
    return ResultRow(**base)  # type: ignore[arg-type]


class TestGroupSurvivors:
    """The pure per-leaderboard grouping + ordering + trim core."""

    def test_groups_by_instrument_tf_mode(self) -> None:
        rows = [
            _row(run_id=_rid(1), instrument="NIFTY", tf="5m", mode="A"),
            _row(run_id=_rid(2), instrument="NIFTY", tf="15m", mode="A"),
            _row(run_id=_rid(3), instrument="BANKNIFTY", tf="5m", mode="A"),
            _row(run_id=_rid(4), instrument="NIFTY", tf="5m", mode="B"),
        ]
        boards = group_survivors(rows)
        assert set(boards) == {
            LeaderboardKey("NIFTY", "5m", "A"),
            LeaderboardKey("NIFTY", "15m", "A"),
            LeaderboardKey("BANKNIFTY", "5m", "A"),
            LeaderboardKey("NIFTY", "5m", "B"),
        }
        assert all(len(board) == 1 for board in boards.values())

    def test_direction_competes_inside_one_leaderboard(self) -> None:
        # Spec §Stage 3: direction is NOT a leaderboard key — a bear mask can
        # outrank a bull mask on the SAME (instrument, TF, mode) board.
        rows = [
            _row(run_id=_rid(1), direction="bull", sharpe_ratio=1.0),
            _row(run_id=_rid(2), direction="bear", sharpe_ratio=2.0),
        ]
        boards = group_survivors(rows, top_k=1)
        (board,) = boards.values()
        assert [r.run_id for r in board] == [_rid(2)]  # the bear mask won

    def test_leaderboard_order_sharpe_desc_runid_tiebreak(self) -> None:
        # The proven store discipline: Sharpe descending, ties run_id ASC —
        # identical for any insertion order (Golden Rule #4).
        rows = [
            _row(run_id=_rid(3), sharpe_ratio=1.0),
            _row(run_id=_rid(1), sharpe_ratio=1.0),
            _row(run_id=_rid(2), sharpe_ratio=2.0),
        ]
        boards = group_survivors(rows)
        (board,) = boards.values()
        assert [r.run_id for r in board] == [_rid(2), _rid(1), _rid(3)]
        # Insertion-order independence: reversed input → identical selection.
        boards_rev = group_survivors(list(reversed(rows)))
        assert boards_rev == boards

    def test_trims_each_leaderboard_to_top_k(self) -> None:
        rows = [_row(run_id=_rid(i), sharpe_ratio=float(i)) for i in range(1, 6)]
        boards = group_survivors(rows, top_k=2)
        (board,) = boards.values()
        assert [r.sharpe_ratio for r in board] == [5.0, 4.0]

    def test_smaller_leaderboard_yields_all_rows(self) -> None:
        boards = group_survivors([_row()], top_k=DEFAULT_STAGE3_TOP_K)
        (board,) = boards.values()
        assert len(board) == 1

    def test_spec_default_is_ten_thousand(self) -> None:
        # TRACK1_BACKTEST_SPEC.md:407 / row #37 — documented everywhere,
        # coded nowhere until this slice.
        assert DEFAULT_STAGE3_TOP_K == 10_000

    def test_iteration_order_is_sorted_leaderboard_keys(self) -> None:
        rows = [
            _row(run_id=_rid(1), instrument="NIFTY", tf="5m"),
            _row(run_id=_rid(2), instrument="BANKNIFTY", tf="5m"),
            _row(run_id=_rid(3), instrument="NIFTY", tf="15m"),
        ]
        keys = list(group_survivors(rows))
        assert keys == sorted(keys, key=lambda k: (k.instrument, k.tf, k.mode))

    def test_empty_rows_yield_empty_dict(self) -> None:
        assert group_survivors([]) == {}

    def test_top_k_below_one_rejected(self) -> None:
        with pytest.raises(ResultsStoreError, match="must be >= 1"):
            group_survivors([_row()], top_k=0)


class TestSelectStage3Survivors:
    """The disk-facing composition over the persisted per-tuple tree."""

    def _persist(self, root: Path, *stores: tuple[str, str, list[ResultRow]]) -> None:
        """Flush each (instrument, tf, rows) group as one per-tuple Parquet."""
        for index, (instrument, tf, rows) in enumerate(stores):
            store = ResultsStore(path=root / instrument / tf / f"part{index}.parquet")
            store.extend(rows)
            store.flush()

    def test_round_trips_persisted_tree_per_leaderboard(self, tmp_path: Path) -> None:
        self._persist(
            tmp_path,
            (
                "NIFTY",
                "5m",
                [
                    _row(run_id=_rid(1), sharpe_ratio=1.0),
                    _row(run_id=_rid(2), sharpe_ratio=3.0),
                    _row(run_id=_rid(3), sharpe_ratio=2.0),
                ],
            ),
            (
                "BANKNIFTY",
                "5m",
                [_row(run_id=_rid(4), instrument="BANKNIFTY", sharpe_ratio=9.9)],
            ),
        )
        boards = select_stage3_survivors(tmp_path, top_k=2)
        nifty = boards[LeaderboardKey("NIFTY", "5m", "A")]
        assert [r.run_id for r in nifty] == [_rid(2), _rid(3)]  # top-2, Sharpe desc
        bank = boards[LeaderboardKey("BANKNIFTY", "5m", "A")]
        assert [r.run_id for r in bank] == [_rid(4)]

    def test_existing_empty_tree_yields_empty_dict(self, tmp_path: Path) -> None:
        assert select_stage3_survivors(tmp_path) == {}

    def test_missing_root_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ResultsStoreError, match="results root not found"):
            select_stage3_survivors(tmp_path / "absent")

    def test_top_k_guard_fires_before_the_tree_load(self, tmp_path: Path) -> None:
        # Fail-fast contract: a bad K must raise even when root is ALSO bad —
        # proving the guard precedes the (potentially large) disk load.
        with pytest.raises(ResultsStoreError, match="must be >= 1"):
            select_stage3_survivors(tmp_path / "absent", top_k=0)
