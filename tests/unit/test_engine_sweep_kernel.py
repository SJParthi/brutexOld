"""Unit tests for the resident sweep kernel (`brutex.engine.sweep_kernel`).

DEC-SWEEP-RESIDENT-KERNEL-001. Covers every public symbol + every branch of the
new module for the strict 100% line + branch gate (CLAUDE.md §4 / Golden Rule
#12), PLUS the new ``kernel`` reuse branches threaded into
:func:`brutex.engine.sweep_run.run_persisted_sweep` and
:func:`brutex.engine.sweep_orchestrator.run_sweep_grid` (which the strict
``--cov=brutex.engine`` gate measures across all ``tests/unit/test_engine_*.py``).

Worst-case hardening (CLAUDE.md Golden Rule #10 + the standing G&A block): empty
bars, empty masks, ``mask == 0``, ``min_hits`` disabled/negative, capacity ``< 1``,
LRU eviction + rebuild, key sensitivity, a bar-count mismatch, and a
reuse-vs-rebuild **byte-identical parity** proof so the speedup can never change
a result (Golden Rule #4).

The bars are small, deterministic, tz-aware synthetic sessions with ``Decimal``
OHLC (Golden Rule #1 — real-shaped data, no fabrication).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from brutex.data.models import CandleBar
from brutex.data.option_dedup import OptionDedupIndex
from brutex.engine.combo_mask import count_supermasks
from brutex.engine.results_store import ResultsStore
from brutex.engine.sweep_kernel import (
    DEFAULT_KERNEL_CACHE_CAPACITY,
    ResidentKernelCache,
    ResidentSweepKernel,
)
from brutex.engine.sweep_orchestrator import (
    SweepGridConfig,
    Timeframe,
    make_store_for,
    run_sweep_grid,
)
from brutex.engine.sweep_run import (
    ComboDedup,
    SweepRunConfig,
    SweepRunSummary,
    run_persisted_sweep,
)
from brutex.orchestrator.audit import AuditWriter
from brutex.strategies.types import FibAnchorKind, PriceSource, Side
from brutex.vocabulary import ProximityParams

if TYPE_CHECKING:
    from brutex.engine.sweep_orchestrator import SweepTuple

_IST = timezone(timedelta(hours=5, minutes=30))


def _bar(day: int, minute: int, o: float, c: float) -> CandleBar:
    """Build one IST-session minute bar (stored UTC) with Decimal OHLC."""
    ts = (
        datetime(2026, 1, 5, 9, 16, tzinfo=_IST) + timedelta(days=day, minutes=minute)
    ).astimezone(UTC)
    hi = max(o, c) + 0.5
    lo = min(o, c) - 0.5
    return CandleBar(
        timestamp=ts,
        open=Decimal(str(round(o, 2))),
        high=Decimal(str(round(hi, 2))),
        low=Decimal(str(round(lo, 2))),
        close=Decimal(str(round(c, 2))),
        volume=100,
        open_interest=None,
    )


def _trending_bars(n_days: int = 15, per_day: int = 120) -> list[CandleBar]:
    """A multi-day upward-drifting session — BUY combos win often via the TP leg."""
    bars: list[CandleBar] = []
    for d in range(n_days):
        price = 100.0
        for m in range(per_day):
            close = price + 0.6 if m % 3 else price - 0.3
            bars.append(_bar(d, m, price, close))
            price = close
    return bars


def _dedup() -> ComboDedup:
    return ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6))


def _make_config(**overrides: object) -> SweepRunConfig:
    base: dict[str, object] = {
        "instrument": "NIFTY",
        "tf": "1m",
        "direction": Side.BUY,
        "mode": "A",
        "condition_bits": [0, 1, 2, 3, 4],
        "k": 1,
        "data_snapshot_hash": "snap0001",
        "indicator_version": "ind-v1",
        "code_commit_sha": "deadbeef",
        "tf_minutes": 1,
        "sl_paisa": 80,
        "tp_paisa": 50,
    }
    base.update(overrides)
    return SweepRunConfig(**base)  # type: ignore[arg-type]


def _grid(**overrides: object) -> SweepGridConfig:
    base: dict[str, object] = {
        "instruments": ["NIFTY"],
        "timeframes": [Timeframe("1minute", 1)],
        "directions": [Side.BUY],
        "k_values": [1],
        "condition_bits": [0, 1, 2, 3, 4],
        "stop_losses": [80],
        "take_profits": [50],
        "proximities": [None],
        "mode": "A",
        "data_snapshot_hash": "snap0001",
        "indicator_version": "ind-v1",
        "code_commit_sha": "deadbeef",
    }
    base.update(overrides)
    return SweepGridConfig(**base)  # type: ignore[arg-type]


# ════════════════════════════════════════════════════════════════════════════
# ResidentSweepKernel
# ════════════════════════════════════════════════════════════════════════════


class TestResidentSweepKernel:
    def test_empty_bars_rejected(self) -> None:
        with pytest.raises(ValueError, match="bars must be non-empty"):
            ResidentSweepKernel([])

    def test_builds_bar_bits_once_and_exposes_window(self) -> None:
        bars = _trending_bars(n_days=2, per_day=30)
        kernel = ResidentSweepKernel(bars)
        assert kernel.n_bars == len(bars)
        assert len(kernel.bar_bits) == len(bars)
        # bars property returns a tuple copy of the feed (the trade-sim source).
        assert kernel.bars == tuple(bars)

    def test_count_hits_is_bit_identical_to_free_count_supermasks(self) -> None:
        bars = _trending_bars(n_days=3, per_day=40)
        kernel = ResidentSweepKernel(bars)
        masks = [0b1, 0b11, 0b101, 1 << 3, 0]
        assert kernel.count_hits(masks) == count_supermasks(kernel.bar_bits, masks)

    def test_count_hits_empty_masks_is_empty(self) -> None:
        kernel = ResidentSweepKernel(_trending_bars(n_days=1, per_day=20))
        assert kernel.count_hits([]) == []

    def test_count_hits_zero_mask_matches_every_bar(self) -> None:
        bars = _trending_bars(n_days=1, per_day=25)
        kernel = ResidentSweepKernel(bars)
        assert kernel.count_hits([0]) == [kernel.n_bars]

    def test_prefilter_disabled_keeps_every_mask(self) -> None:
        kernel = ResidentSweepKernel(_trending_bars(n_days=1, per_day=20))
        masks = [0b1, 0b10, 0b100]
        assert kernel.prefilter(masks, min_hits=0) == masks
        # A negative threshold also disables the pre-filter (defensive boundary).
        assert kernel.prefilter(masks, min_hits=-5) == masks

    def test_prefilter_empty_masks_is_empty(self) -> None:
        kernel = ResidentSweepKernel(_trending_bars(n_days=1, per_day=20))
        assert kernel.prefilter([], min_hits=10) == []

    def test_prefilter_drops_low_hit_masks_preserving_order(self) -> None:
        bars = _trending_bars(n_days=4, per_day=60)
        kernel = ResidentSweepKernel(bars)
        masks = [0, 0b1, 0b10, 0b100, 0b1000]
        hits = kernel.count_hits(masks)
        threshold = max(hits) // 2 + 1
        expected = [m for m, h in zip(masks, hits, strict=True) if h >= threshold]
        assert kernel.prefilter(masks, min_hits=threshold) == expected
        # Every kept mask really clears the threshold; order matches the input.
        assert all(h >= threshold for m, h in zip(masks, hits, strict=True) if m in expected)

    def test_proximity_and_prior_n_flow_into_bar_bits(self) -> None:
        # Distinct proximity tolerances can change the near_* condition bits, so
        # the two kernels are independent windows (no shared mutable state).
        bars = _trending_bars(n_days=2, per_day=40)
        tight = ResidentSweepKernel(bars, proximity=ProximityParams(5, 5, 5), prior_n=2)
        wide = ResidentSweepKernel(bars, proximity=ProximityParams(50, 50, 50), prior_n=4)
        assert tight.n_bars == wide.n_bars == len(bars)


# ════════════════════════════════════════════════════════════════════════════
# ResidentKernelCache
# ════════════════════════════════════════════════════════════════════════════


class _FakeKernel:
    """A stand-in kernel so cache tests assert build-once without a real build."""

    def __init__(
        self,
        bars: object,
        *,
        proximity: object,
        prior_n: int,
        fib_anchor: object,
        vwap_source: object = PriceSource.HLC3,
        ema_source: object = PriceSource.CLOSE,
    ) -> None:
        self.bars = bars
        self.proximity = proximity
        self.prior_n = prior_n
        self.fib_anchor = fib_anchor
        self.vwap_source = vwap_source
        self.ema_source = ema_source


def _fake_builder() -> tuple[list[int], object]:
    """Return ``(call_log, builder)`` — the builder appends 1 per build call."""
    calls: list[int] = []

    def build(
        bars: object,
        *,
        proximity: object,
        prior_n: int,
        fib_anchor: object,
        vwap_source: object = PriceSource.HLC3,
        ema_source: object = PriceSource.CLOSE,
    ) -> _FakeKernel:
        calls.append(1)
        return _FakeKernel(
            bars,
            proximity=proximity,
            prior_n=prior_n,
            fib_anchor=fib_anchor,
            vwap_source=vwap_source,
            ema_source=ema_source,
        )

    return calls, build


class TestResidentKernelCache:
    def test_capacity_below_one_rejected(self) -> None:
        with pytest.raises(ValueError, match="capacity must be >= 1"):
            ResidentKernelCache(capacity=0)

    def test_default_capacity_constant(self) -> None:
        assert DEFAULT_KERNEL_CACHE_CAPACITY == 8
        assert len(ResidentKernelCache()) == 0

    def test_miss_builds_then_hit_reuses_same_object(self) -> None:
        calls, build = _fake_builder()
        cache = ResidentKernelCache(builder=build)  # type: ignore[arg-type]
        first = cache.get([], instrument="NIFTY", tf_label="1m", proximity=None, prior_n=3)
        second = cache.get([], instrument="NIFTY", tf_label="1m", proximity=None, prior_n=3)
        assert first is second  # reused, not rebuilt
        assert cache.builds == 1
        assert cache.hits == 1
        assert sum(calls) == 1
        assert len(cache) == 1

    @pytest.mark.parametrize(
        "differing",
        [
            {"instrument": "BANKNIFTY"},
            {"tf_label": "5m"},
            {"proximity": ProximityParams(5, 5, 5)},
            {"prior_n": 4},
            # DEC-NIFTY-FIB-AXIS-001: the Fib anchor is part of the key — the
            # bar_bits matrix is anchor-specific, so a different anchor must build
            # its own kernel, never reuse a stale one (R-1).
            {"fib_anchor": FibAnchorKind.ROLLING_WEEKLY},
            # DEC-NIFTY-PRICE-SOURCE-AXIS-001: the VWAP / EMA price source is part
            # of the key — bar_bits bits 52-53 / 0-5 are source-specific, so a
            # different source must build its own kernel, never reuse a stale one.
            {"vwap_source": PriceSource.OPEN},
            {"ema_source": PriceSource.HLC3},
        ],
    )
    def test_each_key_field_distinguishes_kernels(self, differing: dict[str, object]) -> None:
        calls, build = _fake_builder()
        cache = ResidentKernelCache(builder=build)  # type: ignore[arg-type]
        base = {
            "instrument": "NIFTY",
            "tf_label": "1m",
            "proximity": None,
            "prior_n": 3,
            "fib_anchor": FibAnchorKind.PREV_DAY,
        }
        a = cache.get([], **base)  # type: ignore[arg-type]
        b = cache.get([], **{**base, **differing})  # type: ignore[arg-type]
        assert a is not b
        assert cache.builds == 2
        assert cache.hits == 0
        assert sum(calls) == 2

    def test_lru_eviction_then_rebuild(self) -> None:
        calls, build = _fake_builder()
        cache = ResidentKernelCache(capacity=2, builder=build)  # type: ignore[arg-type]
        a = cache.get([], instrument="A", tf_label="1m", proximity=None, prior_n=3)
        b = cache.get([], instrument="B", tf_label="1m", proximity=None, prior_n=3)
        # Touch A so B is now the least-recently-used.
        a_again = cache.get([], instrument="A", tf_label="1m", proximity=None, prior_n=3)
        assert a_again is a
        # Inserting C evicts B (LRU), not A.
        cache.get([], instrument="C", tf_label="1m", proximity=None, prior_n=3)
        assert len(cache) == 2
        # A is still resident (a hit, no rebuild); B was evicted, so re-getting it
        # builds a fresh object (identity differs from the evicted original).
        a_third = cache.get([], instrument="A", tf_label="1m", proximity=None, prior_n=3)
        assert a_third is a
        b_rebuilt = cache.get([], instrument="B", tf_label="1m", proximity=None, prior_n=3)
        assert b_rebuilt is not b
        assert cache.builds == 4  # A, B, C, B-again
        assert sum(calls) == 4

    def test_default_builder_is_resident_sweep_kernel(self) -> None:
        cache = ResidentKernelCache()
        bars = _trending_bars(n_days=1, per_day=20)
        kernel = cache.get(bars, instrument="NIFTY", tf_label="1m", proximity=None, prior_n=3)
        assert isinstance(kernel, ResidentSweepKernel)
        assert kernel.n_bars == len(bars)


# ════════════════════════════════════════════════════════════════════════════
# run_persisted_sweep — the optional ``kernel`` reuse branches
# ════════════════════════════════════════════════════════════════════════════


class TestRunPersistedSweepWithKernel:
    def test_kernel_path_matches_no_kernel_byte_for_byte(self, tmp_path: Path) -> None:
        bars = _trending_bars()
        cfg = _make_config()  # default min_hits = MIN_TRADES > 0 → prefilter runs
        kernel = ResidentSweepKernel(bars, proximity=cfg.proximity, prior_n=cfg.prior_n)

        store_a = ResultsStore(path=tmp_path / "a.parquet")
        with AuditWriter.open(tmp_path / "a.jsonl") as audit:
            summary_a = run_persisted_sweep(bars, cfg, store=store_a, dedup=_dedup(), audit=audit)

        store_b = ResultsStore(path=tmp_path / "b.parquet")
        with AuditWriter.open(tmp_path / "b.jsonl") as audit:
            summary_b = run_persisted_sweep(
                bars, cfg, store=store_b, dedup=_dedup(), audit=audit, kernel=kernel
            )

        assert isinstance(summary_b, SweepRunSummary)
        # Same content hash ⇒ the resident-kernel path produced a byte-identical
        # result (Golden Rule #4); the speedup changed only *when* work ran.
        assert summary_b == summary_a
        assert summary_b.store_content_hash == summary_a.store_content_hash

    def test_kernel_path_with_prefilter_disabled(self, tmp_path: Path) -> None:
        # min_hits=0 exercises the kernel branch that skips the Stage-1B count.
        bars = _trending_bars()
        cfg = _make_config(min_hits=0)
        kernel = ResidentSweepKernel(bars, proximity=cfg.proximity, prior_n=cfg.prior_n)
        store = ResultsStore(path=tmp_path / "r.parquet")
        with AuditWriter.open(tmp_path / "a.jsonl") as audit:
            summary = run_persisted_sweep(
                bars, cfg, store=store, dedup=_dedup(), audit=audit, kernel=kernel
            )
        assert isinstance(summary, SweepRunSummary)
        assert summary.evaluated == summary.enumerated  # nothing dedup/conflict pruned here

    def test_kernel_bar_count_mismatch_raises(self, tmp_path: Path) -> None:
        bars = _trending_bars(n_days=3, per_day=40)
        other = _trending_bars(n_days=2, per_day=40)  # different length
        kernel = ResidentSweepKernel(other)
        store = ResultsStore(path=tmp_path / "r.parquet")
        with (
            AuditWriter.open(tmp_path / "a.jsonl") as audit,
            pytest.raises(ValueError, match="kernel has .* bars but .* bars were supplied"),
        ):
            run_persisted_sweep(
                bars, _make_config(), store=store, dedup=_dedup(), audit=audit, kernel=kernel
            )


# ════════════════════════════════════════════════════════════════════════════
# run_sweep_grid — reuse_bar_bits parity + cache integration
# ════════════════════════════════════════════════════════════════════════════


class TestGridReuseBarBits:
    def _run(self, *, reuse: bool, root: Path) -> list[tuple[SweepTuple, SweepRunSummary]]:
        """Run a multi-tuple grid and capture every per-tuple summary."""
        grid = _grid(
            directions=[Side.BUY, Side.SELL],
            stop_losses=[80, 120],
            proximities=[None, ProximityParams(25, 25, 25)],
        )
        bars = _trending_bars()
        seen: list[tuple[SweepTuple, SweepRunSummary]] = []
        with AuditWriter.open(root / "audit.jsonl") as audit:
            run_sweep_grid(
                grid,
                bars_for=lambda _i, _t: bars,
                store_for=make_store_for(root, grid),
                dedup=_dedup(),
                audit=audit,
                on_tuple=lambda t, s: seen.append((t, s)),
                reuse_bar_bits=reuse,
            )
        return seen

    def test_reuse_is_byte_identical_to_rebuild(self, tmp_path: Path) -> None:
        reuse = self._run(reuse=True, root=tmp_path / "reuse")
        rebuild = self._run(reuse=False, root=tmp_path / "rebuild")
        assert [s for _, s in reuse] == [s for _, s in rebuild]
        # Same content hashes per tuple ⇒ the cache never changed a result.
        assert [s.store_content_hash for _, s in reuse] == [
            s.store_content_hash for _, s in rebuild
        ]

    def test_reuse_default_runs_every_tuple(self, tmp_path: Path) -> None:
        # 2 directions × 2 sl × 2 proximities = 8 tuples; all run (bars present).
        seen = self._run(reuse=True, root=tmp_path / "g")
        assert len(seen) == 8
