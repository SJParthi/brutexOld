"""O(K)-serve bench pins for the /results rollup (DEC-RESULTS-TOPK-ROLLUP-001).

Two kinds of teeth (the house #1533/#1614 shape):

* **Deterministic op-count pins** (PRIMARY — no wall-clock flake): a
  rollup-served poll loads exactly ONE Parquet file (the K-union) and reads
  ZERO audit bytes, INDEPENDENT of store count; the legacy scan loads one file
  PER store — the fails-before shape this PR exists to remove.
* **Measured flatness**: the full rollup serve (classify → load → rank →
  project) at 10× the store count stays within the METADATA-SWEEP ceiling
  (:func:`tests.bench._o1_tiers.metadata_sweep_ceiling` — the V3
  classification honestly pays an O(stores) stat-level glob, this module's
  OWN documented metadata sweep; the Parquet-load term is flat by
  construction).

Honest envelope (Golden Rule #10): the serve is O(K-union) + an O(stores)
stat walk — NOT literal O(1); "O(1) repeat poll" is the route's level-2
payload cache, pinned in the route suite, not re-measured here.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import time
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("polars", reason="the rollup persists ResultsStore Parquet files")

from brutex.dashboard.sweep_results_view import (
    SWEEPS_DIRNAME,
    build_sweep_leaderboard,
    load_sweep_scan_via_rollup,
)
from brutex.engine.results_rollup import LogGate, rebuild_rollup, rollup_parquet_path
from brutex.engine.results_store import ResultRow, ResultsStore

from tests.bench._o1_tiers import metadata_sweep_ceiling


def _row(seed: int, *, tf: str) -> ResultRow:
    x = (seed * 2654435761) % (1 << 32)
    wins = x % 50
    losses = (x >> 6) % 50
    return ResultRow(
        run_id=f"{seed:064x}",
        instrument="NIFTY",
        tf=tf,
        direction="bull",
        mode="A",
        mask_bits=1 + (x % 7),
        net_pnl_paisa=(x % 100_000) - 50_000,
        gross_profit_paisa=x % 90_000,
        gross_loss_paisa=(x >> 3) % 70_000,
        max_drawdown_paisa=(x >> 5) % 40_000,
        expectancy_paisa=((x >> 2) % 9_000) - 4_500,
        total_trades=wins + losses + 1,
        winning_trades=wins,
        losing_trades=losses,
        sharpe_ratio=((x % 600) - 300) / 100,
        sortino_ratio=(((x >> 4) % 700) - 350) / 100,
        calmar_ratio=(((x >> 7) % 500) - 250) / 100,
        profit_factor=((x >> 2) % 400) / 100,
        win_rate=(x % 101) / 100,
        max_drawdown_pct=((x >> 9) % 100) / 100,
        avg_entry_price=Decimal("100.00"),
        avg_exit_price=Decimal("101.00"),
    )


def _build_lake(lake_root: Path, *, stores: int, rows_per_store: int) -> None:
    """The arch-eval store-builder recipe (scratchpad ``db_bench_results.py``)."""
    seed = 1
    for i in range(stores):
        tf = f"tf{i}"
        dest = lake_root / SWEEPS_DIRNAME / "NIFTY" / tf / f"bull_k3_{i:016x}.parquet"
        dest.parent.mkdir(parents=True, exist_ok=True)
        store = ResultsStore(path=dest)
        store.extend([_row(seed + j, tf=tf) for j in range(rows_per_store)])
        store.flush()
        seed += rows_per_store


def _rev(lake_root: Path) -> str:
    from brutex.dashboard.lake_revision import sweep_results_revision

    return sweep_results_revision(lake_root) or ""


def _served_poll(lake_root: Path) -> None:
    """One full rollup-served /results poll: classify → load → rank → project."""
    serve = load_sweep_scan_via_rollup(lake_root, rev=_rev(lake_root), log_gate=LogGate())
    assert serve.scan is not None
    board = build_sweep_leaderboard(lake_root, top_n=50, scan=serve.scan)
    assert board.available is True


def _best_of(n: int, fn: object) -> float:
    best = float("inf")
    for _ in range(n):
        start = time.perf_counter()
        fn()  # type: ignore[operator]
        best = min(best, time.perf_counter() - start)
    return best


class TestRollupServeOpCounts:
    def test_served_poll_loads_one_parquet_and_zero_audit_bytes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The fails-before pin: pre-rollup, a cold poll loaded O(stores)
        Parquet files; the rollup-served poll loads exactly ONE."""
        _build_lake(tmp_path, stores=20, rows_per_store=5)
        sweeps_root = tmp_path / SWEEPS_DIRNAME
        assert rebuild_rollup(sweeps_root) is True

        loads: list[Path] = []
        real_load = ResultsStore.load.__func__  # type: ignore[attr-defined]

        def _spy(cls: type[ResultsStore], path: Path) -> ResultsStore:
            loads.append(path)
            return real_load(cls, path)

        monkeypatch.setattr(ResultsStore, "load", classmethod(_spy))

        import brutex.orchestrator.audit as audit_module

        def _forbidden(path: Path) -> object:
            raise AssertionError("a rollup-served poll must not stream the audit chain")

        monkeypatch.setattr(audit_module, "iter_entries", _forbidden)
        _served_poll(tmp_path)
        assert loads == [rollup_parquet_path(sweeps_root)]

    def test_legacy_scan_is_the_o_stores_shape(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The teeth twin: the legacy loader really is O(stores) file loads —
        the quantity the rollup removes from the poll path."""
        from brutex.dashboard.sweep_results_view import load_sweep_scan

        stores = 20
        _build_lake(tmp_path, stores=stores, rows_per_store=5)
        loads: list[Path] = []
        real_load = ResultsStore.load.__func__  # type: ignore[attr-defined]

        def _spy(cls: type[ResultsStore], path: Path) -> ResultsStore:
            loads.append(path)
            return real_load(cls, path)

        monkeypatch.setattr(ResultsStore, "load", classmethod(_spy))
        scan = load_sweep_scan(tmp_path)
        assert scan.store_count == stores
        assert len(loads) == stores


class TestRollupServeFlatness:
    def test_served_poll_flat_vs_store_count(self, tmp_path: Path) -> None:
        """10× the stores (10 → 100) must not scale the served poll past the
        metadata-sweep ceiling (the O(stores) stat-glob is the ONLY store-count
        term left on the serve path — the Parquet-load term is K-bounded)."""
        small = tmp_path / "small"
        large = tmp_path / "large"
        _build_lake(small, stores=10, rows_per_store=10)
        _build_lake(large, stores=100, rows_per_store=10)
        assert rebuild_rollup(small / SWEEPS_DIRNAME) is True
        assert rebuild_rollup(large / SWEEPS_DIRNAME) is True
        # Warm both paths (filesystem cache + import warmup); best-of-5 keeps
        # the ratio robust when a combined-suite run trashes the page cache
        # (observed flake class: the SMALL side catching a slow first stat).
        for _ in range(2):
            _served_poll(small)
            _served_poll(large)
        t_small = _best_of(5, lambda: _served_poll(small))
        t_large = _best_of(5, lambda: _served_poll(large))
        ratio = t_large / t_small if t_small > 0 else 1.0
        assert ratio <= metadata_sweep_ceiling(8.0), (
            f"rollup-served poll scaled with store count: {t_small * 1e3:.2f} ms @10 stores → "
            f"{t_large * 1e3:.2f} ms @100 stores (ratio {ratio:.2f}×)"
        )
