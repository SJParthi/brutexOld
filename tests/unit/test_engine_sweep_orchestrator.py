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
from brutex.engine.sweep_run import ComboDedup, SweepComboError, SweepRunConfig
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

    # ── Layer-1 engine-scope hard invariant (DEC-ENGINE-SCOPE-GUARD-001) ──

    @pytest.mark.parametrize("symbol", ["INDIAVIX", "RELIANCE", "BANKNIFTY-FUT-2026-07-30"])
    def test_out_of_scope_instrument_rejected_at_construction(self, symbol: str) -> None:
        with pytest.raises(ValueError, match="outside the brute-force engine scope"):
            _grid(instruments=[symbol])

    def test_unsafe_flag_allows_out_of_scope_construction(self) -> None:
        grid = _grid(instruments=["INDIAVIX"], unsafe_allow_out_of_scope_instruments=True)
        assert list(grid.instruments) == ["INDIAVIX"]

    @pytest.mark.parametrize(
        "symbol", ["NIFTY", "BANKNIFTY", "SENSEX", "NIFTY-FUT-2026-07-30", "NSE-NIFTY-30Jul26-FUT"]
    )
    def test_in_scope_instruments_construct_by_default(self, symbol: str) -> None:
        assert list(_grid(instruments=[symbol]).instruments) == [symbol]

    def test_scope_flag_is_identity_safe(self) -> None:
        """Regression pin (DEC-ENGINE-SCOPE-GUARD-001): the new field enters NO identity.

        ``_tuple_digest`` / ``_grid_descriptor`` (via ``grid_identity``) /
        ``_tuple_cache_params`` (via ``tuple_cache_key``) are explicit field
        enumerations — a grid with the flag EXPLICIT (True or False) must be
        byte-identical in every digest/checkpoint/cache identity to the
        default-flag grid, so pre-existing checkpoints, dedup entries and
        store paths never churn.
        """
        from brutex.engine.sweep_orchestrator import _tuple_digest, tuple_store_path
        from brutex.engine.sweep_supervisor import grid_identity, tuple_cache_key

        default_grid = _grid()
        explicit_false = _grid(unsafe_allow_out_of_scope_instruments=False)
        explicit_true = _grid(unsafe_allow_out_of_scope_instruments=True)
        assert grid_identity(default_grid) == grid_identity(explicit_false)
        assert grid_identity(default_grid) == grid_identity(explicit_true)
        base_tuple = next(iter_sweep_tuples(default_grid))
        for other in (explicit_false, explicit_true):
            other_tuple = next(iter_sweep_tuples(other))
            assert _tuple_digest(base_tuple, default_grid) == _tuple_digest(other_tuple, other)
            assert tuple_cache_key(base_tuple, default_grid) == tuple_cache_key(other_tuple, other)
            assert tuple_store_path(Path("r"), base_tuple, default_grid) == tuple_store_path(
                Path("r"), other_tuple, other
            )

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

    def test_condition_bit_out_of_range_rejected_at_config_time(self) -> None:
        # GAP #16 (DEC-SWEEP-DEDUP-LMDB-001): a bit >= MAX_VOCAB_BITS in the
        # shared axis must fail AT CONFIG TIME (the CLI catches ValueError and
        # exits cleanly), NOT abort the whole grid mid-enumeration with a bare
        # ValueError from one tuple's lazy mask enumeration.
        with pytest.raises(ValueError, match=r"condition_bits \[199\] out of range \[0, 128\)"):
            _grid(condition_bits=[0, 1, 199])

    def test_negative_condition_bit_rejected_at_config_time(self) -> None:
        with pytest.raises(ValueError, match=r"condition_bits \[-1\] out of range \[0, 128\)"):
            _grid(condition_bits=[-1, 0, 1])

    def test_top_condition_bit_in_range_is_valid(self) -> None:
        from brutex.engine.combo_mask import MAX_VOCAB_BITS

        grid = _grid(condition_bits=[0, MAX_VOCAB_BITS - 1])
        assert MAX_VOCAB_BITS - 1 in grid.condition_bits

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

    # ── Stage-3 knobs (DEC-STAGE3-WIRE-001, hostile F5/F13 fail-fast) ──

    def test_stage3_defaults_are_off_and_spec_values(self) -> None:
        grid = _grid()
        assert grid.stage3_enable is False
        assert grid.stage3_top_k == 10_000
        assert grid.stage3_max_fevals == 200
        assert grid.stage3_max_restarts == 3

    def test_stage3_top_k_below_one_rejected(self) -> None:
        with pytest.raises(ValueError, match="stage3_top_k must be >= 1"):
            _grid(stage3_top_k=0)

    def test_stage3_fevals_below_one_rejected(self) -> None:
        with pytest.raises(ValueError, match="stage3_max_fevals and stage3_max_restarts"):
            _grid(stage3_max_fevals=0)

    def test_stage3_restarts_below_one_rejected(self) -> None:
        with pytest.raises(ValueError, match="stage3_max_fevals and stage3_max_restarts"):
            _grid(stage3_max_restarts=0)

    def test_stage3_enable_rejects_none_stop_loss(self) -> None:
        with pytest.raises(ValueError, match="stop_losses value\\s+to be a Mode-A grid"):
            _grid(stage3_enable=True, stop_losses=[None])

    def test_stage3_enable_rejects_off_grid_stop_loss(self) -> None:
        # 7 is inside [5, 200] but off the 5-paisa tick.
        with pytest.raises(ValueError, match="stop_losses value\\s+on the Mode-A grid"):
            _grid(stage3_enable=True, stop_losses=[7])

    def test_stage3_enable_rejects_none_take_profit(self) -> None:
        with pytest.raises(ValueError, match="take_profits value\\s+to be a Mode-A grid"):
            _grid(stage3_enable=True, take_profits=[None])

    def test_stage3_enable_rejects_off_grid_take_profit(self) -> None:
        # 505 exceeds the TP axis hi bound of 500.
        with pytest.raises(ValueError, match="take_profits value\\s+on the Mode-A grid"):
            _grid(stage3_enable=True, take_profits=[505])

    def test_stage3_enable_accepts_on_grid_axes(self) -> None:
        grid = _grid(stage3_enable=True, stop_losses=[80, 100], take_profits=[50, 120])
        assert grid.stage3_enable is True

    def test_stage3_disabled_skips_on_grid_validation(self) -> None:
        # Off-grid + None legs are FINE when the pass is off (the default).
        grid = _grid(stop_losses=[7, None], take_profits=[None])
        assert grid.stage3_enable is False


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
        # Synthetic GHOST name by test intent (a no-bars skip needs a symbol the
        # lake lacks) — Layer-1 scope opt-out (DEC-ENGINE-SCOPE-GUARD-001).
        grid = _grid(instruments=["NIFTY", "GHOST"], unsafe_allow_out_of_scope_instruments=True)

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
            tuples_failed=0,
        )


class TestRunSweepGridResilientSkip:
    """A tuple that raises SweepComboError is SKIPPED; the grid finishes the rest.

    "Resilient: skip + continue": ``tuples_failed`` counts it, a ``TUPLE_FAILED``
    audit line names the offending combo, and the failed tuple's store is never
    sentineled (so it redoes next run). Only ``SweepComboError`` is caught.
    """

    def test_one_tuple_fails_others_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pytest.importorskip("polars")
        from brutex.engine.sweep_orchestrator import run_one_tuple as real_run_one

        bars = _trending_bars()
        grid = _grid(instruments=["NIFTY", "BANKNIFTY"])  # 2 tuples (index 0, 1)

        def _maybe_raise(grid_: object, tuple_: object, **kwargs: object) -> object:
            if tuple_.instrument == "BANKNIFTY":  # type: ignore[attr-defined]
                raise SweepComboError("combo 0x0000000000000004 (run_id=deadbeef) boom")
            return real_run_one(grid_, tuple_, **kwargs)  # type: ignore[arg-type]

        # Patch the name as resolved inside run_sweep_grid (string target avoids
        # the re-export attr check).
        monkeypatch.setattr("brutex.engine.sweep_orchestrator.run_one_tuple", _maybe_raise)

        seen: list[str] = []
        audit_path = tmp_path / "grid.jsonl"
        with AuditWriter.open(audit_path) as audit:
            summary = run_sweep_grid(
                grid,
                bars_for=lambda _i, _t: bars,
                store_for=make_store_for(tmp_path / "results", grid),
                dedup=_dedup(),
                audit=audit,
                on_tuple=lambda t, _s: seen.append(t.instrument),
            )
        # NIFTY ran; BANKNIFTY was skipped as failed.
        assert summary.tuples_run == 1
        assert summary.tuples_failed == 1
        assert summary.tuples_skipped_no_bars == 0
        assert seen == ["NIFTY"]  # on_tuple never fired for the failed tuple
        actions = [e.action for e in read_entries(audit_path)]
        assert "TUPLE_FAILED" in actions
        # The TUPLE_FAILED payload names the tuple coordinate + the error string.
        failed = next(e for e in read_entries(audit_path) if e.action == "TUPLE_FAILED")
        assert "BANKNIFTY" in failed.payload["tuple"]
        assert "SweepComboError" in failed.payload["error"]
        # The GRID_SWEEP_COMPLETE payload carries the failed count.
        complete = next(e for e in read_entries(audit_path) if e.action == "GRID_SWEEP_COMPLETE")
        assert complete.payload["tuples_failed"] == 1
        assert verify_chain(audit_path) > 0

    def test_real_raise_leaves_failed_store_unsentineled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A REAL combo raise skips the tuple WITHOUT sentineling its store (redo next run)."""
        pytest.importorskip("polars")
        from brutex.engine.tuple_complete import completion_sentinel_path

        bars = _trending_bars()
        grid = _grid()  # single NIFTY tuple
        results_root = tmp_path / "results"

        def _always_raise(*_args: object, **_kwargs: object) -> object:
            raise RuntimeError("corrupt window")

        # The wired per-mask METRICS TAIL (DEC-SWEEP-RUST-TRADESIM-WIRE-001) —
        # raising here produces the real fingerprinted SweepComboError exactly
        # as the pre-wiring per-mask metrics_for_mask seam did.
        monkeypatch.setattr("brutex.engine.sweep_run.metrics_from_trades", _always_raise)

        with AuditWriter.open(tmp_path / "grid.jsonl") as audit:
            summary = run_sweep_grid(
                grid,
                bars_for=lambda _i, _t: bars,
                store_for=make_store_for(results_root, grid),
                dedup=_dedup(),
                audit=audit,
            )
        assert summary.tuples_failed == 1
        assert summary.tuples_run == 0
        # The failed tuple's store + completion sentinel were never written.
        store_path = tuple_store_path(results_root, next(iter_sweep_tuples(grid)), grid)
        assert not store_path.exists()
        assert not completion_sentinel_path(store_path).exists()

    def test_non_combo_error_still_propagates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-SweepComboError raised by run_one_tuple is NOT caught (global abort)."""
        grid = _grid()

        def _boom(*_args: object, **_kwargs: object) -> object:
            raise RuntimeError("genuinely fatal")

        monkeypatch.setattr("brutex.engine.sweep_orchestrator.run_one_tuple", _boom)
        with (
            AuditWriter.open(tmp_path / "grid.jsonl") as audit,
            pytest.raises(RuntimeError, match="genuinely fatal"),
        ):
            run_sweep_grid(
                grid,
                bars_for=lambda _i, _t: [],
                store_for=make_store_for(tmp_path, grid),
                dedup=_dedup(),
                audit=audit,
            )

    def test_keyboard_interrupt_aborts_grid_not_a_tuple_skip(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A KeyboardInterrupt in run_one_tuple ABORTS the grid (NOT a tuple skip).

        The grid catch is ``except SweepComboError`` — a ``BaseException`` such as
        an operator Ctrl-C is neither caught nor downgraded, so it propagates and
        aborts the WHOLE grid (DEC-ENGINE-DEDUP-NAN-CANON-001 / audit H1). The
        first tuple's failure must NOT bump ``tuples_failed`` nor write a
        ``TUPLE_FAILED`` audit line, and the second tuple must never run.
        """
        # 2 tuples: index 0 raises KeyboardInterrupt, index 1 would run if reached.
        grid = _grid(instruments=["NIFTY", "BANKNIFTY"])
        ran: list[str] = []

        def _interrupt(grid_: object, tuple_: object, **_kwargs: object) -> object:
            ran.append(tuple_.instrument)  # type: ignore[attr-defined]
            raise KeyboardInterrupt("operator ctrl-c")

        monkeypatch.setattr("brutex.engine.sweep_orchestrator.run_one_tuple", _interrupt)
        audit_path = tmp_path / "grid.jsonl"
        with (
            AuditWriter.open(audit_path) as audit,
            pytest.raises(KeyboardInterrupt, match="operator ctrl-c"),
        ):
            run_sweep_grid(
                grid,
                bars_for=lambda _i, _t: _trending_bars(),
                store_for=make_store_for(tmp_path / "results", grid),
                dedup=_dedup(),
                audit=audit,
            )
        # The grid aborted on the FIRST tuple — the second never ran.
        assert ran == ["NIFTY"]
        # No TUPLE_FAILED was recorded (the catch is narrow; Ctrl-C is not a skip),
        # and no terminal GRID_SWEEP_COMPLETE was written (the loop aborted).
        actions = [e.action for e in read_entries(audit_path)]
        assert "TUPLE_FAILED" not in actions
        assert "GRID_SWEEP_COMPLETE" not in actions


class TestRunSweepGridResilientHardening:
    """HARDENING permutations of the resilient tuple-skip path (test-only).

    Reachable edge-cases the SHIP-reviewed code already handles, now pinned:
    every tuple failing, and the buckets always reconciling to the grid total
    (the "no tuple silently lost" invariant).
    """

    def test_all_tuples_fail(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """EVERY tuple raises SweepComboError: tuples_failed == total, run == 0.

        The grid still completes cleanly (no global abort), each failed tuple gets
        its own ``TUPLE_FAILED`` audit line, the chain still verifies, and the
        returned summary's ``tuples_failed`` equals the full grid size.
        """
        bars = _trending_bars()
        # 3 tuples (2 instruments × … no, keep it a clean 2×2 = 4-tuple grid).
        grid = _grid(instruments=["NIFTY", "BANKNIFTY"], directions=[Side.BUY, Side.SELL])
        total = count_sweep_tuples(grid)
        assert total == 4

        def _always_raise(*_args: object, **_kwargs: object) -> object:
            raise SweepComboError("combo 0x0000000000000004 (run_id=dead) boom")

        monkeypatch.setattr("brutex.engine.sweep_orchestrator.run_one_tuple", _always_raise)

        seen: list[str] = []
        audit_path = tmp_path / "grid.jsonl"
        with AuditWriter.open(audit_path) as audit:
            summary = run_sweep_grid(
                grid,
                bars_for=lambda _i, _t: bars,
                store_for=make_store_for(tmp_path / "results", grid),
                dedup=_dedup(),
                audit=audit,
                on_tuple=lambda t, _s: seen.append(t.instrument),
            )
        assert summary.tuples_failed == total
        assert summary.tuples_run == 0
        assert summary.tuples_skipped_no_bars == 0
        assert summary.persisted == 0
        assert seen == []  # on_tuple never fires for a failed tuple
        # One TUPLE_FAILED line per tuple (+ GRID_START + GRID_COMPLETE).
        actions = [e.action for e in read_entries(audit_path)]
        assert actions.count("TUPLE_FAILED") == total
        assert actions[0] == "GRID_SWEEP_START"
        assert actions[-1] == "GRID_SWEEP_COMPLETE"
        # The audit chain (despite every tuple failing) still verifies fully.
        assert verify_chain(audit_path) == total + 2
        # The terminal GRID payload carries the full failed count.
        complete = next(e for e in read_entries(audit_path) if e.action == "GRID_SWEEP_COMPLETE")
        assert complete.payload["tuples_failed"] == total

    def test_buckets_reconcile_to_total(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The no-tuple-silently-lost invariant: run + skipped_no_bars + failed == total.

        A mixed 3-tuple grid where index 0 RUNS, index 1 has NO BARS, index 2
        FAILS — proving every disposition is accounted for and their sum is the
        grid size.
        """
        pytest.importorskip("polars")
        from brutex.engine.sweep_orchestrator import run_one_tuple as real_run_one

        # 3 instruments → 3 tuples in enumeration order (NIFTY, BANKNIFTY, SENSEX).
        grid = _grid(instruments=["NIFTY", "BANKNIFTY", "SENSEX"])
        total = count_sweep_tuples(grid)
        assert total == 3
        bars = _trending_bars()

        def _bars_for(instrument: str, _t: str) -> list[CandleBar]:
            return [] if instrument == "BANKNIFTY" else bars  # index 1 has no bars

        def _maybe_raise(grid_: object, tuple_: object, **kwargs: object) -> object:
            if tuple_.instrument == "SENSEX":  # type: ignore[attr-defined]  # index 2 fails
                raise SweepComboError("combo 0x0000000000000004 (run_id=dead) boom")
            return real_run_one(grid_, tuple_, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr("brutex.engine.sweep_orchestrator.run_one_tuple", _maybe_raise)

        with AuditWriter.open(tmp_path / "grid.jsonl") as audit:
            summary = run_sweep_grid(
                grid,
                bars_for=_bars_for,
                store_for=make_store_for(tmp_path / "results", grid),
                dedup=_dedup(),
                audit=audit,
            )
        assert summary.tuples_run == 1
        assert summary.tuples_skipped_no_bars == 1
        assert summary.tuples_failed == 1
        # Every tuple is accounted for in exactly one bucket — none silently lost.
        assert (
            summary.tuples_run + summary.tuples_skipped_no_bars + summary.tuples_failed
            == summary.tuples_total
            == total
        )


def test_default_max_tuples_is_exposed() -> None:
    assert DEFAULT_MAX_TUPLES == 100_000
