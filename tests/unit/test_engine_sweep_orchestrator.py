"""Unit tests for the full sweep orchestrator (`brutex.engine.sweep_orchestrator`).

Covers every public symbol and every branch of the new module for the strict
100% line + branch gate (CLAUDE.md §4 / Golden Rule #12):

* :class:`Timeframe` — both validation branches + the happy path.
* :class:`SweepGridConfig` — every ``__post_init__`` validation branch + the
  order-preserving axis de-dup.
* :class:`SweepTuple` — :meth:`to_run_config` field threading.
* :func:`count_sweep_tuples` / :func:`iter_sweep_tuples` — product cardinality +
  deterministic enumeration order.
* :func:`bloom_capacity_hint` — the per-grid mask-identity upper bound.
* :func:`tuple_store_path` / :func:`load_or_create_store` / :func:`make_store_for`
  — deterministic + collision-proof paths, idempotent load-or-create, the
  ``proximity`` None / not-None digest branches.
* :func:`run_sweep_grid` — happy multi-tuple path, the empty-bars skip + raise
  branches, the ``on_tuple`` None / not-None branches, the GRID audit framing.

Bars are small, deterministic, tz-aware synthetic sessions with ``Decimal`` OHLC
(Golden Rule #1 — real-shaped data, no fabrication).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.data.models import CandleBar
from brutex.data.option_dedup import OptionDedupIndex
from brutex.engine.results_store import ResultsStore
from brutex.engine.sweep_orchestrator import (
    DEFAULT_MAX_TUPLES,
    SweepGridConfig,
    SweepGridSummary,
    SweepTuple,
    Timeframe,
    bloom_capacity_hint,
    count_sweep_tuples,
    iter_sweep_tuples,
    load_or_create_store,
    make_store_for,
    run_sweep_grid,
    tuple_store_path,
)
from brutex.engine.sweep_run import ComboDedup, SweepRunConfig
from brutex.orchestrator.audit import AuditWriter, read_entries, verify_chain
from brutex.ranking import OverfitGateConfig
from brutex.strategies.types import Side
from brutex.vocabulary import ProximityParams

_IST = timezone(timedelta(hours=5, minutes=30))


# ────── shared fixtures ──────


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


def _dedup() -> ComboDedup:
    return ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6))


def _tuple(**overrides: object) -> SweepTuple:
    base: dict[str, object] = {
        "instrument": "NIFTY",
        "tf": Timeframe("1minute", 1),
        "direction": Side.BUY,
        "k": 1,
        "sl_paisa": 80,
        "tp_paisa": 50,
        "proximity": None,
    }
    base.update(overrides)
    return SweepTuple(**base)  # type: ignore[arg-type]


# ────── Timeframe ──────


class TestTimeframe:
    def test_valid_constructs(self) -> None:
        tf = Timeframe("5minute", 5)
        assert tf.label == "5minute"
        assert tf.minutes == 5

    def test_empty_label_rejected(self) -> None:
        with pytest.raises(ValueError, match="label must be non-empty"):
            Timeframe("", 5)

    def test_minutes_below_one_rejected(self) -> None:
        with pytest.raises(ValueError, match="minutes must be >= 1"):
            Timeframe("5minute", 0)


# ────── SweepGridConfig validation ──────


class TestSweepGridConfig:
    def test_valid_constructs(self) -> None:
        grid = _grid()
        assert grid.instruments == ("NIFTY",)
        assert grid.timeframes == (Timeframe("1minute", 1),)

    def test_axes_are_deduped_preserving_order(self) -> None:
        grid = _grid(
            instruments=["NIFTY", "BANKNIFTY", "NIFTY"],
            k_values=[1, 2, 1],
            stop_losses=[80, 80, None],
        )
        assert grid.instruments == ("NIFTY", "BANKNIFTY")
        assert grid.k_values == (1, 2)
        assert grid.stop_losses == (80, None)

    @pytest.mark.parametrize(
        "axis",
        [
            "instruments",
            "timeframes",
            "directions",
            "k_values",
            "condition_bits",
            "stop_losses",
            "take_profits",
            "proximities",
        ],
    )
    def test_empty_axis_rejected(self, axis: str) -> None:
        with pytest.raises(ValueError, match=f"{axis} must be a non-empty axis"):
            _grid(**{axis: []})

    def test_empty_instrument_symbol_rejected(self) -> None:
        with pytest.raises(ValueError, match="instruments entries must be non-empty"):
            _grid(instruments=[""])

    def test_hold_direction_rejected(self) -> None:
        with pytest.raises(ValueError, match="must not contain Side.HOLD"):
            _grid(directions=[Side.BUY, Side.HOLD])

    @pytest.mark.parametrize(
        "field", ["mode", "data_snapshot_hash", "indicator_version", "code_commit_sha"]
    )
    def test_empty_context_rejected(self, field: str) -> None:
        with pytest.raises(ValueError, match=f"{field} must be non-empty"):
            _grid(**{field: ""})

    def test_k_out_of_range_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"k_values contains 6, must be in \[0, 5\]"):
            _grid(k_values=[6])

    def test_k_zero_is_valid(self) -> None:
        grid = _grid(k_values=[0])
        assert grid.k_values == (0,)

    @pytest.mark.parametrize("axis", ["stop_losses", "take_profits"])
    def test_nonpositive_paisa_rejected(self, axis: str) -> None:
        with pytest.raises(ValueError, match=f"{axis} entries must be > 0 or None"):
            _grid(**{axis: [0]})

    def test_none_paisa_is_valid(self) -> None:
        grid = _grid(stop_losses=[None], take_profits=[None])
        assert grid.stop_losses == (None,)
        assert grid.take_profits == (None,)

    def test_prior_n_below_one_rejected(self) -> None:
        with pytest.raises(ValueError, match="prior_n must be >= 1"):
            _grid(prior_n=0)

    def test_nonpositive_start_equity_rejected(self) -> None:
        with pytest.raises(ValueError, match="start_equity_paisa must be > 0"):
            _grid(start_equity_paisa=0)

    def test_top_k_below_one_rejected(self) -> None:
        with pytest.raises(ValueError, match="top_k must be >= 1 or None"):
            _grid(top_k=0)

    def test_top_k_none_is_valid(self) -> None:
        assert _grid(top_k=None).top_k is None

    def test_negative_min_hits_rejected(self) -> None:
        with pytest.raises(ValueError, match="min_hits must be >= 0"):
            _grid(min_hits=-1)

    def test_over_max_combos_rejected(self) -> None:
        # C(5,2)=10 masks; cap at 5 → rejected before any run.
        with pytest.raises(ValueError, match="exceeding max_combos=5"):
            _grid(k_values=[2], max_combos=5)

    def test_over_max_tuples_rejected(self) -> None:
        # 2 instruments × 2 directions = 4 tuples; cap at 3.
        with pytest.raises(ValueError, match=r"4 tuples exceed max_tuples=3"):
            _grid(
                instruments=["NIFTY", "BANKNIFTY"],
                directions=[Side.BUY, Side.SELL],
                max_tuples=3,
            )


# ────── SweepTuple.to_run_config ──────


class TestToRunConfig:
    def test_threads_grid_context_and_tuple_axes(self) -> None:
        grid = _grid(top_k=3, min_hits=7, prior_n=2, start_equity_paisa=9_000)
        prox = ProximityParams(pivot_proximity_bps=12)
        t = _tuple(direction=Side.SELL, k=2, sl_paisa=None, tp_paisa=40, proximity=prox)
        cfg = t.to_run_config(grid)
        assert isinstance(cfg, SweepRunConfig)
        assert cfg.instrument == "NIFTY"
        assert cfg.tf == "1minute"
        assert cfg.tf_minutes == 1
        assert cfg.direction is Side.SELL
        assert cfg.k == 2
        assert cfg.sl_paisa is None
        assert cfg.tp_paisa == 40
        assert cfg.proximity is prox
        assert cfg.prior_n == 2
        assert cfg.start_equity_paisa == 9_000
        assert cfg.top_k == 3
        assert cfg.min_hits == 7
        assert cfg.mode == "A"
        assert cfg.code_commit_sha == "deadbeef"
        assert cfg.overfit is None  # default: legacy ranking

    def test_forwards_overfit_gate_config(self) -> None:
        # The opt-in overfit gate config is threaded onto every tuple's run config.
        overfit = OverfitGateConfig(dsr_min=0.9)
        grid = _grid(overfit=overfit)
        cfg = _tuple().to_run_config(grid)
        assert cfg.overfit is overfit


# ────── count / iter ──────


class TestEnumeration:
    def test_count_is_product_of_axes(self) -> None:
        grid = _grid(
            instruments=["NIFTY", "BANKNIFTY"],
            timeframes=[Timeframe("1minute", 1), Timeframe("5minute", 5)],
            directions=[Side.BUY, Side.SELL],
            k_values=[1, 2],
            stop_losses=[80, None],
            take_profits=[50],
            proximities=[None],
        )
        # 2·2·2·2·2·1·1 = 32
        assert count_sweep_tuples(grid) == 32

    def test_iter_yields_count_tuples_in_deterministic_order(self) -> None:
        grid = _grid(
            instruments=["NIFTY", "BANKNIFTY"],
            directions=[Side.BUY, Side.SELL],
        )
        tuples = list(iter_sweep_tuples(grid))
        assert len(tuples) == count_sweep_tuples(grid) == 4
        # Nested order: instrument outer, direction inner.
        assert [(t.instrument, t.direction) for t in tuples] == [
            ("NIFTY", Side.BUY),
            ("NIFTY", Side.SELL),
            ("BANKNIFTY", Side.BUY),
            ("BANKNIFTY", Side.SELL),
        ]
        # Reproducible across calls.
        assert list(iter_sweep_tuples(grid)) == tuples


# ────── bloom_capacity_hint ──────


class TestBloomCapacityHint:
    def test_is_sum_of_masks_times_other_axes(self) -> None:
        # C(5,1)+C(5,2) = 5 + 10 = 15 masks per tuple-of-k; × 2 instruments.
        grid = _grid(instruments=["NIFTY", "BANKNIFTY"], k_values=[1, 2])
        assert bloom_capacity_hint(grid) == 15 * 2

    def test_floor_is_one_for_zero_mask_grid(self) -> None:
        # k=0 over zero unique bits would be C(0,0)=1; force the max(1, …) floor
        # via k=0 which yields exactly 1 — still >= 1.
        grid = _grid(k_values=[0])
        assert bloom_capacity_hint(grid) >= 1


# ────── path derivation + store factory ──────


class TestPaths:
    def test_path_is_under_instrument_tf_with_digest(self, tmp_path: Path) -> None:
        grid = _grid()
        t = _tuple()
        path = tuple_store_path(tmp_path, t, grid)
        assert path.parent == tmp_path / "NIFTY" / "1minute"
        assert path.name.startswith("BUY_k1_")
        assert path.suffix == ".parquet"

    def test_distinct_tuples_get_distinct_paths(self, tmp_path: Path) -> None:
        grid = _grid()
        # sl=None vs sl=80 must NOT collide (the silent-overwrite guard).
        p_none = tuple_store_path(tmp_path, _tuple(sl_paisa=None), grid)
        p_val = tuple_store_path(tmp_path, _tuple(sl_paisa=80), grid)
        assert p_none != p_val

    def test_path_is_stable_across_calls(self, tmp_path: Path) -> None:
        grid = _grid()
        t = _tuple()
        assert tuple_store_path(tmp_path, t, grid) == tuple_store_path(tmp_path, t, grid)

    def test_proximity_changes_the_digest(self, tmp_path: Path) -> None:
        grid = _grid()
        p_default = tuple_store_path(tmp_path, _tuple(proximity=None), grid)
        p_prox = tuple_store_path(
            tmp_path, _tuple(proximity=ProximityParams(pivot_proximity_bps=20)), grid
        )
        assert p_default != p_prox

    def test_load_or_create_returns_fresh_when_absent(self, tmp_path: Path) -> None:
        store = load_or_create_store(tmp_path / "missing.parquet")
        assert isinstance(store, ResultsStore)
        assert len(store) == 0

    def test_load_or_create_loads_existing(self, tmp_path: Path) -> None:
        pytest.importorskip("polars")
        # Persist a real store, then re-load it through load_or_create_store.
        path = tmp_path / "NIFTY" / "1minute" / "x.parquet"
        bars = _trending_bars()
        grid = _grid()
        with AuditWriter.open(tmp_path / "a.jsonl") as audit:
            summary = run_sweep_grid(
                grid,
                bars_for=lambda _i, _t: bars,
                store_for=lambda _t: ResultsStore(path=path),
                dedup=_dedup(),
                audit=audit,
            )
        assert summary.persisted > 0
        reloaded = load_or_create_store(path)
        assert len(reloaded) == summary.persisted

    def test_make_store_for_resolves_load_or_create(self, tmp_path: Path) -> None:
        grid = _grid()
        store_for = make_store_for(tmp_path, grid)
        store = store_for(_tuple())
        assert isinstance(store, ResultsStore)
        assert store.path == tuple_store_path(tmp_path, _tuple(), grid)


# ────── run_sweep_grid ──────


class TestRunSweepGrid:
    def test_happy_grid_runs_skips_and_aggregates(self, tmp_path: Path) -> None:
        pytest.importorskip("polars")
        bars = _trending_bars()
        # 2 instruments: NIFTY has bars (runs), GHOST has none (skipped).
        grid = _grid(instruments=["NIFTY", "GHOST"])

        def bars_for(instrument: str, _tf: str) -> list[CandleBar]:
            return bars if instrument == "NIFTY" else []

        seen: list[tuple[str, int]] = []
        audit_path = tmp_path / "grid.jsonl"
        with AuditWriter.open(audit_path) as audit:
            summary = run_sweep_grid(
                grid,
                bars_for=bars_for,
                store_for=make_store_for(tmp_path / "results", grid),
                dedup=_dedup(),
                audit=audit,
                on_tuple=lambda t, s: seen.append((t.instrument, s.persisted)),
            )
        assert summary.tuples_total == 2
        assert summary.tuples_run == 1
        assert summary.tuples_skipped_no_bars == 1
        assert summary.persisted > 0
        assert summary.evaluated > 0
        # on_tuple fired exactly once (only for the tuple that ran).
        assert [s[0] for s in seen] == ["NIFTY"]
        # Audit chain = GRID_START + (3 per-tuple) + GRID_COMPLETE = 5 lines.
        assert verify_chain(audit_path) == 5
        actions = [e.action for e in read_entries(audit_path)]
        assert actions[0] == "GRID_SWEEP_START"
        assert actions[-1] == "GRID_SWEEP_COMPLETE"

    def test_empty_bars_raises_when_skip_empty_false(self, tmp_path: Path) -> None:
        grid = _grid()
        with (
            AuditWriter.open(tmp_path / "a.jsonl") as audit,
            pytest.raises(ValueError, match="no bars for NIFTY/1minute"),
        ):
            run_sweep_grid(
                grid,
                bars_for=lambda _i, _t: [],
                store_for=make_store_for(tmp_path, grid),
                dedup=_dedup(),
                audit=audit,
                skip_empty=False,
            )

    def test_runs_without_on_tuple_hook(self, tmp_path: Path) -> None:
        pytest.importorskip("polars")
        bars = _trending_bars()
        grid = _grid()
        with AuditWriter.open(tmp_path / "a.jsonl") as audit:
            summary = run_sweep_grid(
                grid,
                bars_for=lambda _i, _t: bars,
                store_for=make_store_for(tmp_path, grid),
                dedup=_dedup(),
                audit=audit,
            )
        assert isinstance(summary, SweepGridSummary)
        assert summary.tuples_run == 1

    def test_returns_summary_dataclass(self, tmp_path: Path) -> None:
        # A grid whose only tuple has no bars (skip) — no polars needed (no flush).
        grid = _grid()
        with AuditWriter.open(tmp_path / "a.jsonl") as audit:
            summary = run_sweep_grid(
                grid,
                bars_for=lambda _i, _t: [],
                store_for=make_store_for(tmp_path, grid),
                dedup=_dedup(),
                audit=audit,
            )
        assert summary == SweepGridSummary(
            tuples_total=1,
            tuples_run=0,
            tuples_skipped_no_bars=1,
            enumerated=0,
            conflict_pruned=0,
            skipped_dedup=0,
            evaluated=0,
            ranked=0,
            persisted=0,
        )


def test_default_max_tuples_is_exposed() -> None:
    assert DEFAULT_MAX_TUPLES == 100_000
