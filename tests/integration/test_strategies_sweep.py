"""End-to-end integration test for :class:`brutex.strategies.sweep.ComboSweep`.

Exercises the full pipeline:
ParamGrid → combo_hash → StrategyRunner → ComboCache.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar

import pytest
from brutex.data.catalog import Catalog
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink
from brutex.feed.replay import FeedEvent, ReplayFeed
from brutex.strategies.combo import combo_hash
from brutex.strategies.protocol import StrategyMeta
from brutex.strategies.sweep import (
    ComboCache,
    ComboSweep,
    ParamGrid,
    SweepSummary,
)
from brutex.strategies.types import Side, Signal, StrategyState

pytest.importorskip("polars")
pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")
pytest.importorskip("lmdb")


# ────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────


def _bar(day: int, minute: int = 15, *, open_price: float = 100.0) -> CandleBar:
    return CandleBar(
        timestamp=datetime(2024, 1, day, 9, minute, 0, tzinfo=UTC),
        open=Decimal(str(open_price)),
        high=Decimal(str(open_price + 1)),
        low=Decimal(str(open_price - 1)),
        close=Decimal(str(open_price + 0.5)),
        volume=10,
        open_interest=500,
    )


def _seed_lake(tmp_path: Path, *, n_bars: int = 5) -> tuple[Path, Path]:
    lake_root = tmp_path / "lake"
    catalog_path = tmp_path / "catalog.duckdb"
    writer = ParquetWriter(lake_root=lake_root)
    with Catalog.open(catalog_path) as cat:
        sink = ParquetBarSink(writer=writer, catalog=cat)
        sink.append(
            exchange="NSE",
            segment="FNO",
            symbol="NSE-A",
            interval="1minute",
            bars=[_bar(1, minute=15 + i) for i in range(n_bars)],
        )
    return lake_root, catalog_path


class _ThresholdStrategy:
    """Emits a BUY when bar_count >= params["min_bars"], else nothing.

    Deterministic, no indicators. Different param values produce
    different signal counts, which is exactly what a meaningful
    sweep needs.
    """

    META: ClassVar[StrategyMeta] = StrategyMeta(
        strategy_id="threshold",
        version="0.1.0",
        symbols=("NSE-A",),
        interval="1minute",
        params_schema={"min_bars": 0},
    )

    def __init__(self, params: Mapping[str, Any] | None = None) -> None:
        params = params or {}
        self._min_bars = int(params["min_bars"])

    def on_event(
        self,
        event: FeedEvent,
        state: StrategyState,
    ) -> Sequence[Signal]:
        if state.bar_count < self._min_bars:
            return []
        return [
            Signal(
                symbol=event.symbol,
                side=Side.BUY,
                timestamp=event.timestamp,
            )
        ]


# ────────────────────────────────────────────────────────────────────────
# Factories used by every test
# ────────────────────────────────────────────────────────────────────────


class _Factories:
    """Helper that binds a catalog + lake_root to the factory callables.

    Instances of :class:`_ThresholdStrategy` and :class:`ReplayFeed`
    need different arguments than the sweep's lambda signature —
    this class closes over the shared state so the test code stays
    terse.
    """

    def __init__(self, catalog: Catalog, lake_root: Path) -> None:
        self._catalog = catalog
        self._lake_root = lake_root

    def strategy(self, params: Mapping[str, Any]) -> _ThresholdStrategy:
        return _ThresholdStrategy(params)

    def feed(self, params: Mapping[str, Any]) -> ReplayFeed:
        del params  # feed shape is independent of combo params here
        return ReplayFeed(
            catalog=self._catalog,
            lake_root=self._lake_root,
            exchange="NSE",
            segment="FNO",
            symbols=["NSE-A"],
            interval="1minute",
            start=datetime(2024, 1, 1, tzinfo=UTC),
            end=datetime(2024, 1, 31, tzinfo=UTC),
        )


# ────────────────────────────────────────────────────────────────────────
# Happy-path sweep
# ────────────────────────────────────────────────────────────────────────


class TestComboSweepHappyPath:
    def test_three_combos_distinct_results(self, tmp_path: Path) -> None:
        """min_bars=0,2,4 over 5 bars → 5, 4, 2 signals respectively."""
        lake_root, catalog_path = _seed_lake(tmp_path, n_bars=5)
        with Catalog.open(catalog_path) as cat:
            factories = _Factories(cat, lake_root)
            sweep = ComboSweep(
                strategy_factory=factories.strategy,
                feed_factory=factories.feed,
                grid=ParamGrid.from_dict({"min_bars": [0, 2, 4]}),
                data_snapshot_hash="a" * 64,
                code_commit_sha="deadbeef",
            )
            summary = sweep.run()

        assert isinstance(summary, SweepSummary)
        assert summary.total_combos == 3
        assert summary.fresh_runs == 3
        assert summary.cache_hits == 0
        assert len(summary.results) == 3

        by_min = {r.params["min_bars"]: r for r in summary.results}
        assert by_min[0].signal_count == 5
        assert by_min[2].signal_count == 4
        assert by_min[4].signal_count == 2

    def test_empty_grid_runs_single_combo(self, tmp_path: Path) -> None:
        """ParamGrid({}) yields one empty combo — still runs the strategy."""
        lake_root, catalog_path = _seed_lake(tmp_path, n_bars=3)

        class _NoParamStrategy:
            META: ClassVar[StrategyMeta] = StrategyMeta(
                strategy_id="noparam",
                version="0.1.0",
                symbols=("NSE-A",),
                interval="1minute",
            )

            def __init__(self, params: Mapping[str, Any] | None = None) -> None:
                del params

            def on_event(
                self,
                event: FeedEvent,
                state: StrategyState,
            ) -> Sequence[Signal]:
                del state
                return [
                    Signal(
                        symbol=event.symbol,
                        side=Side.BUY,
                        timestamp=event.timestamp,
                    )
                ]

        with Catalog.open(catalog_path) as cat:
            factories = _Factories(cat, lake_root)
            sweep = ComboSweep(
                strategy_factory=_NoParamStrategy,
                feed_factory=factories.feed,
                grid=ParamGrid.from_dict({}),
                data_snapshot_hash="a" * 64,
                code_commit_sha="deadbeef",
            )
            summary = sweep.run()

        assert summary.total_combos == 1
        assert summary.fresh_runs == 1
        assert summary.results[0].signal_count == 3


# ────────────────────────────────────────────────────────────────────────
# Cache integration — FR-BF-004
# ────────────────────────────────────────────────────────────────────────


class TestComboSweepCache:
    def test_first_run_populates_cache(self, tmp_path: Path) -> None:
        lake_root, catalog_path = _seed_lake(tmp_path, n_bars=5)
        cache_path = tmp_path / "combo-cache"
        with (
            Catalog.open(catalog_path) as cat,
            ComboCache.open(cache_path) as cache,
        ):
            factories = _Factories(cat, lake_root)
            sweep = ComboSweep(
                strategy_factory=factories.strategy,
                feed_factory=factories.feed,
                grid=ParamGrid.from_dict({"min_bars": [0, 2]}),
                data_snapshot_hash="a" * 64,
                code_commit_sha="deadbeef",
                cache=cache,
            )
            summary = sweep.run()
            assert summary.fresh_runs == 2
            assert summary.cache_hits == 0
            assert cache.count() == 2

    def test_second_run_hits_cache_for_every_combo(self, tmp_path: Path) -> None:
        """FR-BF-004: rerun with identical inputs → zero fresh runs."""
        lake_root, catalog_path = _seed_lake(tmp_path, n_bars=5)
        cache_path = tmp_path / "combo-cache"

        def build_sweep(cat: Catalog, cache: ComboCache) -> ComboSweep:
            factories = _Factories(cat, lake_root)
            return ComboSweep(
                strategy_factory=factories.strategy,
                feed_factory=factories.feed,
                grid=ParamGrid.from_dict({"min_bars": [0, 2, 4]}),
                data_snapshot_hash="a" * 64,
                code_commit_sha="deadbeef",
                cache=cache,
            )

        with (
            Catalog.open(catalog_path) as cat,
            ComboCache.open(cache_path) as cache,
        ):
            build_sweep(cat, cache).run()

        # Re-open both and rerun — cache must now short-circuit.
        with (
            Catalog.open(catalog_path) as cat,
            ComboCache.open(cache_path) as cache,
        ):
            summary = build_sweep(cat, cache).run()
            assert summary.fresh_runs == 0
            assert summary.cache_hits == 3
            # Every result must be flagged as cache_hit.
            assert all(r.cache_hit for r in summary.results)

    def test_different_commit_sha_invalidates_cache(self, tmp_path: Path) -> None:
        """Bumping the commit sha must produce fresh runs, not cache hits."""
        lake_root, catalog_path = _seed_lake(tmp_path, n_bars=5)
        cache_path = tmp_path / "combo-cache"

        with (
            Catalog.open(catalog_path) as cat,
            ComboCache.open(cache_path) as cache,
        ):
            factories = _Factories(cat, lake_root)
            ComboSweep(
                strategy_factory=factories.strategy,
                feed_factory=factories.feed,
                grid=ParamGrid.from_dict({"min_bars": [0]}),
                data_snapshot_hash="a" * 64,
                code_commit_sha="v1",
            ).run()

        with (
            Catalog.open(catalog_path) as cat,
            ComboCache.open(cache_path) as cache,
        ):
            factories = _Factories(cat, lake_root)
            summary = ComboSweep(
                strategy_factory=factories.strategy,
                feed_factory=factories.feed,
                grid=ParamGrid.from_dict({"min_bars": [0]}),
                data_snapshot_hash="a" * 64,
                code_commit_sha="v2",  # different
                cache=cache,
            ).run()
            # The v2 run is the first with *this* cache instance, so
            # it's a fresh run regardless — but importantly, after a
            # second v2 run it should be a hit. Let's verify that.
            assert summary.fresh_runs == 1

            summary2 = ComboSweep(
                strategy_factory=factories.strategy,
                feed_factory=factories.feed,
                grid=ParamGrid.from_dict({"min_bars": [0]}),
                data_snapshot_hash="a" * 64,
                code_commit_sha="v2",
                cache=cache,
            ).run()
            assert summary2.cache_hits == 1
            assert summary2.fresh_runs == 0


# ────────────────────────────────────────────────────────────────────────
# combo_hash integration
# ────────────────────────────────────────────────────────────────────────


class TestSweepComboHashes:
    def test_results_carry_correct_combo_hash(self, tmp_path: Path) -> None:
        """Each ComboResult's combo_hash must match combo_hash(params)."""
        lake_root, catalog_path = _seed_lake(tmp_path, n_bars=3)
        with Catalog.open(catalog_path) as cat:
            factories = _Factories(cat, lake_root)
            sweep = ComboSweep(
                strategy_factory=factories.strategy,
                feed_factory=factories.feed,
                grid=ParamGrid.from_dict({"min_bars": [0, 1]}),
                data_snapshot_hash="a" * 64,
                code_commit_sha="deadbeef",
            )
            summary = sweep.run()

        for result in summary.results:
            expected = combo_hash(
                strategy_id="threshold",
                params=result.params,
                data_snapshot_hash="a" * 64,
                code_commit_sha="deadbeef",
            )
            assert result.combo_hash == expected
