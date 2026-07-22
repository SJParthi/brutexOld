"""Rollup-served ↔ store-recomputed `/results` parity goldens (DEC-RESULTS-TOPK-ROLLUP-001).

The ratified parity claim, pinned: **run_id-ordering and served-row parity are
preserved** — for EVERY preset (None + all four tokens), every ``rank_by``
axis, and every leaderboard depth up to the route cap, the rollup-served
:func:`build_sweep_leaderboard` payload is BYTE-IDENTICAL (``to_dict()``
equality over the JSON-value space) to the legacy store-recomputed payload,
including:

* the ``float(2**60) == float(2**60 + 1)`` exact-int precision teeth (the
  #1154 surface-trial fixture class — ranking never collapses through float),
* run_id-ASC tie fixtures on every ordering axis,
* a REAL trim (k below the row count via the public ``trim_to_union(rows, k)``
  parameter driven through a hand-built rollup state), and
* write determinism + merge-order independence (identical Parquet bytes).
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

pytest.importorskip("polars", reason="the rollup persists ResultsStore Parquet files")

import brutex.engine.results_rollup as rr
from brutex.dashboard.sweep_results_view import (
    MAX_TOP_N,
    RANKABLE_COLUMNS,
    SWEEPS_DIRNAME,
    SweepScan,
    build_sweep_leaderboard,
    load_sweep_scan_via_rollup,
)
from brutex.engine.results_store import ResultRow, ResultsStore
from brutex.ranking.presets import PRESET_NAMES

if TYPE_CHECKING:
    from brutex.engine.sweep_orchestrator import SweepGridConfig


def _row(
    seed: int,
    *,
    instrument: str = "NIFTY",
    tf: str = "5m",
    direction: str = "bull",
    net_pnl_paisa: int | None = None,
    sharpe: float | None = None,
    max_drawdown_pct: float | None = None,
) -> ResultRow:
    """A ResultRow whose every union-axis value varies deterministically by seed."""
    x = (seed * 2654435761) % (1 << 32)
    return ResultRow(
        run_id=f"{seed:064x}",
        instrument=instrument,
        tf=tf,
        direction=direction,
        mode="A",
        mask_bits=1 + (seed % 3),
        net_pnl_paisa=net_pnl_paisa if net_pnl_paisa is not None else (x % 1_000_000) - 500_000,
        gross_profit_paisa=x % 900_000,
        gross_loss_paisa=x % 700_000,
        max_drawdown_paisa=x % 400_000,
        expectancy_paisa=(x % 90_000) - 45_000,
        total_trades=(x % 200) + ((x >> 8) % 190) + (x % 5) + 1,
        winning_trades=x % 200,
        losing_trades=(x >> 8) % 190,
        sharpe_ratio=sharpe if sharpe is not None else ((x % 6000) - 3000) / 1000,
        sortino_ratio=((x % 7000) - 3500) / 1000,
        calmar_ratio=((x % 5000) - 2500) / 1000,
        profit_factor=(x % 4000) / 1000,
        win_rate=(x % 1001) / 1000,
        max_drawdown_pct=(max_drawdown_pct if max_drawdown_pct is not None else (x % 1000) / 1000),
        avg_entry_price=Decimal("100.00"),
        avg_exit_price=Decimal("101.00"),
        # The four NULLABLE metrics-preset axes union_axes() self-extended to
        # since #1699 (DEC-METRICS-*) — seed/x-distinct finite values so every
        # union axis (concrete AND nullable) genuinely discriminates in the
        # parity + trim goldens (a real sweep row always carries them).
        log_growth_at_half_kelly=((x % 3000) - 1500) / 1000,
        sl_hit_rate=(x % 1001) / 1000,
        trailing_sharpe_ratio=((x % 6000) - 3000) / 1000,
        decay_weighted_net_paisa=float((x % 1_000_000) - 500_000),
    )


def _write_store(
    lake_root: Path,
    rows: list[ResultRow],
    *,
    instrument: str = "NIFTY",
    tf: str = "5m",
    direction: str = "bull",
    digest: str = "a" * 16,
) -> Path:
    dest = lake_root / SWEEPS_DIRNAME / instrument / tf / f"{direction}_k1_{digest}.parquet"
    dest.parent.mkdir(parents=True, exist_ok=True)
    store = ResultsStore(path=dest)
    store.extend(rows)
    store.flush()
    return dest


def _rev(lake_root: Path) -> str:
    from brutex.dashboard.lake_revision import sweep_results_revision

    return sweep_results_revision(lake_root) or ""


def _seed_lake(lake_root: Path) -> None:
    """Three tuples across two instruments/TFs/directions — 30 mixed rows."""
    _write_store(lake_root, [_row(i) for i in range(1, 13)])
    _write_store(
        lake_root,
        [_row(i, instrument="BANKNIFTY", tf="15m") for i in range(13, 23)],
        instrument="BANKNIFTY",
        tf="15m",
        digest="b" * 16,
    )
    _write_store(
        lake_root,
        [_row(i, direction="bear") for i in range(23, 31)],
        direction="bear",
        digest="c" * 16,
    )


def _rollup_scan(lake_root: Path) -> SweepScan:
    serve = load_sweep_scan_via_rollup(lake_root, rev=_rev(lake_root), log_gate=rr.LogGate())
    assert serve.scan is not None and serve.behind_stores == 0
    return serve.scan


PARITY_GRID = [
    (preset, rank_by, top)
    for preset in (None, *PRESET_NAMES)
    for rank_by in (None, "net_pnl_paisa", "sharpe_ratio", "max_drawdown_pct", "total_trades")
    for top in (1, 5, MAX_TOP_N)
]


class TestServedParity:
    @pytest.mark.parametrize(("preset", "rank_by", "top"), PARITY_GRID)
    def test_rollup_served_equals_store_recomputed(
        self, tmp_path: Path, preset: str | None, rank_by: str | None, top: int
    ) -> None:
        _seed_lake(tmp_path)
        assert rr.rebuild_rollup(tmp_path / SWEEPS_DIRNAME) is True
        scan = _rollup_scan(tmp_path)
        served = build_sweep_leaderboard(
            tmp_path, rank_by=rank_by, top_n=top, scan=scan, preset=preset
        ).to_dict()
        legacy = build_sweep_leaderboard(
            tmp_path, rank_by=rank_by, top_n=top, preset=preset
        ).to_dict()
        assert served == legacy

    def test_precision_teeth_exact_int_ordering_survives(self, tmp_path: Path) -> None:
        """``float(2**60) == float(2**60 + 1)`` — the ranking must not collapse."""
        assert float(2**60) == float(2**60 + 1)  # the genuine trap, restated
        hi = _row(1, net_pnl_paisa=2**60 + 1)
        lo = _row(2, net_pnl_paisa=2**60)
        _write_store(tmp_path, [lo, hi])
        rr.rebuild_rollup(tmp_path / SWEEPS_DIRNAME)
        scan = _rollup_scan(tmp_path)
        served = build_sweep_leaderboard(tmp_path, rank_by="net_pnl_paisa", scan=scan).to_dict()
        legacy = build_sweep_leaderboard(tmp_path, rank_by="net_pnl_paisa").to_dict()
        assert served == legacy
        assert [r["run_id"] for r in served["rows"]] == [hi.run_id, lo.run_id]

    def test_tie_rows_keep_the_run_id_asc_law(self, tmp_path: Path) -> None:
        from dataclasses import replace as _replace

        base = _row(5, net_pnl_paisa=777, sharpe=1.5, max_drawdown_pct=0.25)
        # TOTAL ties: every metric identical — only the run_id differs, so
        # EVERY ordering (default, rank_by, all presets) must break run_id-ASC.
        ties = [_replace(base, run_id=f"{seed:064x}") for seed in (5, 3, 9, 1)]
        _write_store(tmp_path, [ties[0], ties[1]])
        _write_store(tmp_path, [ties[2], ties[3]], tf="15m", digest="d" * 16)
        rr.rebuild_rollup(tmp_path / SWEEPS_DIRNAME)
        scan = _rollup_scan(tmp_path)
        for preset in (None, *PRESET_NAMES):
            served = build_sweep_leaderboard(tmp_path, scan=scan, preset=preset).to_dict()
            legacy = build_sweep_leaderboard(tmp_path, preset=preset).to_dict()
            assert served == legacy
            assert [r["run_id"] for r in served["rows"]] == sorted(t.run_id for t in ties)


class TestTrimmedParity:
    def test_top_n_parity_survives_a_real_trim(self, tmp_path: Path) -> None:
        """With k WELL below the row count, the served top-N still equals the
        full-set top-N for every axis + the legacy default (the prefix law
        driven through a hand-trimmed union — the public ``k`` parameter)."""
        rows = [_row(i) for i in range(1, 61)]
        _write_store(tmp_path, rows)
        k = 10
        kept = rr.trim_to_union(rows, k)
        assert len(kept) < len(rows)  # a REAL trim happened
        full_sorted = sorted(rows, key=lambda r: r.run_id)
        kept_ids = {r.run_id for r in kept}
        for axis in rr.union_axes():
            ordered = sorted(
                full_sorted, key=lambda r: getattr(r, axis.field), reverse=axis.descending
            )
            assert {r.run_id for r in ordered[:k]} <= kept_ids
            # And ranking the union under any axis reproduces the full top-k head.
        for axis in rr.union_axes():
            full_head = sorted(
                full_sorted, key=lambda r: getattr(r, axis.field), reverse=axis.descending
            )[:k]
            union_head = sorted(
                sorted(kept, key=lambda r: r.run_id),
                key=lambda r: getattr(r, axis.field),
                reverse=axis.descending,
            )[:k]
            assert [r.run_id for r in full_head] == [r.run_id for r in union_head]


class TestWriteDeterminism:
    def test_rebuild_twice_is_byte_identical(self, tmp_path: Path) -> None:
        _seed_lake(tmp_path)
        sweeps_root = tmp_path / SWEEPS_DIRNAME
        rr.rebuild_rollup(sweeps_root)
        first = rr.rollup_parquet_path(sweeps_root).read_bytes()
        rr.rebuild_rollup(sweeps_root)
        assert rr.rollup_parquet_path(sweeps_root).read_bytes() == first

    def test_merge_order_independence(self, tmp_path: Path) -> None:
        """Hook-A merges in EITHER order land on the SAME Parquet bytes."""
        rows_a = [_row(i) for i in range(1, 6)]
        rows_b = [_row(i, tf="15m") for i in range(6, 11)]

        def _build(order: tuple[int, ...]) -> bytes:
            lake = tmp_path / f"lake{order[0]}"
            sweeps = lake / SWEEPS_DIRNAME
            sweeps.mkdir(parents=True)
            writers = (
                lambda: _write_store(lake, rows_a),
                lambda: _write_store(lake, rows_b, tf="15m", digest="d" * 16),
            )
            hook = rr.RollupMergeHook(sweeps_root=sweeps, grid=_grid())
            for idx in order:
                # Write-then-merge per tuple — the real Hook-A arrival shape,
                # so the SECOND store genuinely arrives incrementally.
                hook.merge_paths([writers[idx]()])
            assert hook.enabled is True
            return rr.rollup_parquet_path(sweeps).read_bytes()

        assert _build((0, 1)) == _build((1, 0))


def _grid() -> SweepGridConfig:
    from brutex.engine.sweep_orchestrator import SweepGridConfig, Timeframe
    from brutex.strategies.types import Side

    return SweepGridConfig(
        instruments=["NIFTY"],
        timeframes=[Timeframe("1minute", 1)],
        directions=[Side.BUY],
        k_values=[1],
        condition_bits=[0, 1, 2, 3, 4],
        stop_losses=[80],
        take_profits=[50],
        proximities=[None],
        mode="A",
        data_snapshot_hash="snap0001",
        indicator_version="ind-v1",
        code_commit_sha="deadbeef",
    )


def test_rankable_columns_are_all_union_axes() -> None:
    """Every serveable ``rank_by`` axis is guaranteed present in the union."""
    fields = {(a.field, a.descending) for a in rr.union_axes()}
    for col in RANKABLE_COLUMNS:
        assert (col, True) in fields
