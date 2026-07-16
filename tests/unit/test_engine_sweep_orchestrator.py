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
from brutex.strategies.types import PriceSource, PriceSourceCell, Side
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


#: An ADR-0012 roll schedule spanning the `_trending_bars` fixture (which
#: starts 2026-01-05 IST): one mid-stream roll boundary + a covering tail
#: entry, expressed as owning-expiry day-ordinals.
_FUT_SCHEDULE: tuple[int, ...] = (
    datetime(2026, 1, 8, tzinfo=_IST).date().toordinal(),
    datetime(2026, 3, 31, tzinfo=_IST).date().toordinal(),
)


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

    def test_vwap_variant_on_spot_index_warns_but_does_not_refuse(self) -> None:
        # DEC-NIFTY-PRICE-SOURCE-AXIS-001 (MINOR-4): a VWAP-variant cell on a spot
        # index (no traded volume) is a likely operator mistake — warned, NEVER
        # refused. The default cell in the axis exercises the filter's False arm.
        from structlog.testing import capture_logs

        with capture_logs() as logs:
            grid = _grid(
                instruments=["NIFTY"],
                price_source_cells=[
                    PriceSourceCell(),  # default HLC3:CLOSE — filtered out (False arm)
                    PriceSourceCell(PriceSource.OPEN, PriceSource.CLOSE),  # VWAP-variant
                ],
            )
        # Construction SUCCEEDS (non-blocking) and the advisory fired once, naming
        # the offending source + spot symbol.
        assert grid.instruments == ("NIFTY",)
        warns = [e for e in logs if e.get("event") == "stage1a.vwap_source_on_spot"]
        assert len(warns) == 1
        assert warns[0]["vwap_variant_sources"] == ["open"]
        assert warns[0]["spot_instruments"] == ["NIFTY"]

    def test_vwap_variant_on_nifty_futures_does_not_warn(self) -> None:
        # A VWAP-variant on NIFTY FUTURES (which DO carry volume) is legitimate —
        # no advisory (the warn `if` False arm on the spot-instruments side).
        # Post-DEC-ENGINE-SPOT-ONLY-001 the futures alias is out of engine scope,
        # so the grid needs the loud research escape hatch to construct at all
        # (the house pattern of the other out-of-scope tests in this file); the
        # advisory's non-spot False arm is unchanged by the scope retirement.
        from structlog.testing import capture_logs

        with capture_logs() as logs:
            _grid(
                instruments=["NSE-NIFTY-FUT"],
                unsafe_allow_out_of_scope_instruments=True,
                price_source_cells=[PriceSourceCell(PriceSource.OPEN, PriceSource.CLOSE)],
            )
        assert not any(e.get("event") == "stage1a.vwap_source_on_spot" for e in logs)

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

    @pytest.mark.parametrize("symbol", ["NIFTY", "BANKNIFTY", "SENSEX"])
    def test_in_scope_instruments_construct_by_default(self, symbol: str) -> None:
        # DEC-ENGINE-SPOT-ONLY-001: exactly the 3 spot indexes construct by
        # default — the ADR-0012 series alias moved to the refusal test below.
        assert list(_grid(instruments=[symbol]).instruments) == [symbol]

    def test_futures_series_alias_refused_at_construction_spot_only(self) -> None:
        # FLIPPED by DEC-ENGINE-SPOT-ONLY-001 (was: allowed at Layer 1 per
        # DEC-FUTURES-ADDRESSING-PIN-001): the engine is spot-only, so even
        # PROGRAMMATIC construction refuses the alias with the law citation
        # + the remediation — never a silent admit.
        with pytest.raises(ValueError, match="outside the brute-force engine scope") as exc:
            _grid(instruments=["NSE-NIFTY-FUT"])
        assert "DEC-ENGINE-SPOT-ONLY-001" in str(exc.value)
        assert "spot-only" in str(exc.value)
        assert "lake/REFERENCE_ONLY" in str(exc.value)

    @pytest.mark.parametrize("symbol", ["NIFTY-FUT-2026-07-30", "NSE-NIFTY-30Jul26-FUT"])
    def test_retired_futures_shapes_refused_naming_the_alias(self, symbol: str) -> None:
        """The two RETIRED pre-launch addressing shapes are refused at Layer 1.

        DEC-FUTURES-ADDRESSING-PIN-001 supersedes the dual-acceptance envelope:
        only the series alias is engine-sweepable, and the reworded refusal
        message names it so the operator learns the correct shape.
        """
        with pytest.raises(ValueError, match="outside the brute-force engine scope") as exc:
            _grid(instruments=[symbol])
        assert "NSE-NIFTY-FUT" in str(exc.value)
        assert "DEC-ENGINE-SPOT-ONLY-001" in str(exc.value)

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

    def test_threads_roll_schedule_for_the_futures_alias(self) -> None:
        # ADR-0012 (DEC-NIFTY-FUTURES-SWEEP-LAUNCH-001): the per-tuple schedule
        # is argument-plumbed into the SweepRunConfig, never a grid field.
        # DEC-ENGINE-SPOT-ONLY-001: the futures machinery below the gate is
        # RETAINED — exercised via the sanctioned research escape flag.
        grid = _grid(instruments=["NSE-NIFTY-FUT"], unsafe_allow_out_of_scope_instruments=True)
        t = _tuple(instrument="NSE-NIFTY-FUT")
        cfg = t.to_run_config(grid, roll_schedule=_FUT_SCHEDULE)
        assert cfg.roll_schedule == _FUT_SCHEDULE

    def test_default_roll_schedule_is_none_for_spot(self) -> None:
        cfg = _tuple().to_run_config(_grid())
        assert cfg.roll_schedule is None

    def test_spot_tuple_with_a_schedule_is_rejected_by_the_run_config_invariant(self) -> None:
        # The SweepRunConfig instrument⟺schedule invariant fires through the
        # threading seam too — a misrouted schedule can never mint an identity.
        with pytest.raises(ValueError, match="roll_schedule is required for"):
            _tuple().to_run_config(_grid(), roll_schedule=_FUT_SCHEDULE)


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


# ────── _tuple_digest gate + selection identity (DEC-SWEEP-TUPLE-DIGEST-GATE-IDENTITY-001) ──────


class TestTupleDigestGateSelectionIdentity:
    """The store address folds the ACTIVE gate + non-default selection knobs.

    C1 (overfit) / C2 (top_k/min_hits/max_combos) / stage3 re-key the store
    Parquet + completion-sentinel address, while a DEFAULT (ungated,
    default-selection) sweep stays BYTE-STABLE — no existing on-disk store
    re-keys. The pre-fix default digest is the empirically-captured constant.
    """

    # The pre-fix default digest captured on 0f411437 (16-char _PATH_DIGEST_LEN).
    _PRE_FIX_DEFAULT_DIGEST = "5b020e892247deac"

    def test_default_digest_byte_stable(self) -> None:
        from brutex.engine.sweep_orchestrator import _tuple_digest

        assert _tuple_digest(_tuple(), _grid()) == self._PRE_FIX_DEFAULT_DIGEST

    def test_default_store_path_filename_unchanged(self, tmp_path: Path) -> None:
        name = tuple_store_path(tmp_path, _tuple(), _grid()).name
        assert name == f"BUY_k1_{self._PRE_FIX_DEFAULT_DIGEST}.parquet"

    def test_selection_defaults_elide_byte_stable(self) -> None:
        from brutex.engine.sweep_driver import DEFAULT_MAX_COMBOS
        from brutex.engine.sweep_orchestrator import _tuple_digest
        from brutex.ranking import MIN_TRADES

        d0 = _tuple_digest(_tuple(), _grid())
        # Explicitly setting a knob to its canonical default must NOT re-key.
        assert _tuple_digest(_tuple(), _grid(top_k=None)) == d0
        assert _tuple_digest(_tuple(), _grid(min_hits=MIN_TRADES)) == d0
        assert _tuple_digest(_tuple(), _grid(max_combos=DEFAULT_MAX_COMBOS)) == d0

    def test_overfit_rekeys_and_is_value_sensitive(self) -> None:
        from brutex.engine.sweep_orchestrator import _tuple_digest

        d0 = _tuple_digest(_tuple(), _grid())
        d90 = _tuple_digest(_tuple(), _grid(overfit=OverfitGateConfig(dsr_min=0.90)))
        d80 = _tuple_digest(_tuple(), _grid(overfit=OverfitGateConfig(dsr_min=0.80)))
        assert d90 != d0
        assert d90 != d80  # a distinct KNOB value re-keys again, not just presence

    def test_stage3_rekeys_but_disabled_knobs_inert(self) -> None:
        from brutex.engine.sweep_orchestrator import _tuple_digest

        d0 = _tuple_digest(_tuple(), _grid())
        assert _tuple_digest(_tuple(), _grid(stage3_enable=True)) != d0
        # A disabled Stage-3 pass leaves its knobs INERT → no re-key.
        assert _tuple_digest(_tuple(), _grid(stage3_top_k=999)) == d0

    def test_non_default_selection_rekeys_pairwise_distinct(self) -> None:
        from brutex.engine.sweep_driver import DEFAULT_MAX_COMBOS
        from brutex.engine.sweep_orchestrator import _tuple_digest

        d0 = _tuple_digest(_tuple(), _grid())
        d_topk = _tuple_digest(_tuple(), _grid(top_k=50))
        d_mh5 = _tuple_digest(_tuple(), _grid(min_hits=5))
        d_mh0 = _tuple_digest(_tuple(), _grid(min_hits=0))  # filter disabled
        d_mc = _tuple_digest(_tuple(), _grid(max_combos=DEFAULT_MAX_COMBOS - 1))
        digests = {d0, d_topk, d_mh5, d_mh0, d_mc}
        assert len(digests) == 5  # all pairwise-distinct incl. the default

    def test_selection_identity_fragment_branches(self) -> None:
        from brutex.engine.sweep_orchestrator import _selection_identity_fragment

        assert _selection_identity_fragment(_grid()) is None
        assert _selection_identity_fragment(_grid(top_k=50)) == {"top_k": 50}
        assert _selection_identity_fragment(_grid(min_hits=5)) == {"min_hits": 5}
        assert _selection_identity_fragment(_grid(max_combos=7)) == {"max_combos": 7}
        assert _selection_identity_fragment(_grid(top_k=3, min_hits=5, max_combos=7)) == {
            "top_k": 3,
            "min_hits": 5,
            "max_combos": 7,
        }

    def test_overfit_redo_targets_a_distinct_store_and_sentinel(self, tmp_path: Path) -> None:
        """C1: a changed overfit config maps to a DIFFERENT store + sentinel path."""
        from brutex.engine.tuple_complete import completion_sentinel_path

        t = _tuple()
        light = _grid()
        gated = _grid(overfit=OverfitGateConfig(dsr_min=0.90))
        p_light = tuple_store_path(tmp_path, t, light)
        p_gated = tuple_store_path(tmp_path, t, gated)
        assert p_light != p_gated
        assert completion_sentinel_path(p_light) != completion_sentinel_path(p_gated)

    def test_selection_redo_targets_a_distinct_store(self, tmp_path: Path) -> None:
        """C2: a changed top_k / min_hits each map to a fresh store path."""
        t = _tuple()
        base = _grid()
        p_base = tuple_store_path(tmp_path, t, base)
        p_topk = tuple_store_path(tmp_path, t, _grid(top_k=50))
        p_mh = tuple_store_path(tmp_path, t, _grid(min_hits=5))
        assert len({p_base, p_topk, p_mh}) == 3


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

    def test_grid_start_audit_emits_price_source_cells_only_when_non_default(
        self, tmp_path: Path
    ) -> None:
        # MINOR-1: the GRID_SWEEP_START audit config line OMITS price_source_cells
        # for a sole-baseline grid (byte-identical to pre-axis) and EMITS them for a
        # non-default grid (intended observability). An EMA-variant keeps the run off
        # the vwap-on-spot advisory path while still exercising the non-default emit.
        pytest.importorskip("polars")
        bars = _trending_bars()

        def bars_for(_instrument: str, _tf: str) -> list[CandleBar]:
            return bars

        # Default grid → key ABSENT.
        default_audit = tmp_path / "default.jsonl"
        with AuditWriter.open(default_audit) as audit:
            run_sweep_grid(
                _grid(),
                bars_for=bars_for,
                store_for=make_store_for(tmp_path / "r-default", _grid()),
                dedup=_dedup(),
                audit=audit,
            )
        default_start = next(
            e for e in read_entries(default_audit) if e.action == "GRID_SWEEP_START"
        )
        assert "price_source_cells" not in default_start.payload

        # Non-default (EMA-variant) grid → key PRESENT with the cells.
        nd_grid = _grid(price_source_cells=[PriceSourceCell(PriceSource.HLC3, PriceSource.OPEN)])
        nd_audit = tmp_path / "nd.jsonl"
        with AuditWriter.open(nd_audit) as audit:
            run_sweep_grid(
                nd_grid,
                bars_for=bars_for,
                store_for=make_store_for(tmp_path / "r-nd", nd_grid),
                dedup=_dedup(),
                audit=audit,
            )
        nd_start = next(e for e in read_entries(nd_audit) if e.action == "GRID_SWEEP_START")
        assert nd_start.payload["price_source_cells"] == [["hlc3", "open"]]

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

    def test_gate_conflict_at_persist_is_tuple_scoped_not_global_abort(
        self, tmp_path: Path
    ) -> None:
        """C5 (DEC-SWEEP-TUPLE-DIGEST-GATE-IDENTITY-001): a gate-config conflict at the
        finalist ``store.append`` is a TUPLE-scoped skip+count, never a grid abort.

        The store raises the typed :class:`ResultsStoreGateConflictError`;
        ``run_persisted_sweep`` converts it to a ``SweepComboError`` that sails
        PAST ``run_one_tuple``'s ``except ResultsStoreError`` (store_quarantine
        None) global-abort re-raise and lands on the driver's tuple-skip ladder —
        so the grid FINISHES, ``tuples_failed`` counts it, a TUPLE_FAILED audit
        line names the config cause, and no healthy store is quarantined.
        """
        pytest.importorskip("polars")
        from brutex.engine.results_store import ResultsStoreGateConflictError
        from brutex.engine.tuple_complete import completion_sentinel_path

        bars = _trending_bars()
        grid = _grid()  # single NIFTY tuple
        results_root = tmp_path / "results"

        class _GateConflictStore(ResultsStore):
            def append(self, row: object) -> bool:  # type: ignore[override]
                raise ResultsStoreGateConflictError(
                    "the ONLY differing field(s) (deflated_sharpe) — a gate-config change "
                    "(DEC-SWEEP-TUPLE-DIGEST-GATE-IDENTITY-001)."
                )

        def _store_for(t: SweepTuple) -> ResultsStore:
            return _GateConflictStore(path=tuple_store_path(results_root, t, grid))

        audit_path = tmp_path / "grid.jsonl"
        with AuditWriter.open(audit_path) as audit:
            summary = run_sweep_grid(
                grid,
                bars_for=lambda _i, _t: bars,
                store_for=_store_for,
                dedup=_dedup(),
                audit=audit,
            )
        # Tuple-scoped skip — the grid did NOT abort.
        assert summary.tuples_failed == 1
        assert summary.tuples_run == 0
        failed = next(e for e in read_entries(audit_path) if e.action == "TUPLE_FAILED")
        assert "gate-config conflict" in failed.payload["error"]
        # No sentinel written for the failed tuple (redo next run).
        store_path = tuple_store_path(results_root, next(iter_sweep_tuples(grid)), grid)
        assert not completion_sentinel_path(store_path).exists()

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


# ────── ADR-0012 roll_schedule threading through the grid ──────


class TestRunSweepGridRollSchedule:
    """`roll_schedule_by_instrument` — argument-plumbed, identity-safe.

    DEC-NIFTY-FUTURES-SWEEP-LAUNCH-001: the launcher resolves the schedule map
    ONCE and hands it alongside ``bars_for``; a futures tuple's run identity
    moves ONLY via ``SweepRunConfig.roll_schedule`` → ``canonical_trade_params``
    — a spot grid is byte-identical with and without the argument present.
    """

    def test_futures_grid_threads_the_schedule_and_runs(self, tmp_path: Path) -> None:
        pytest.importorskip("polars")
        bars = _trending_bars(n_days=4)
        # DEC-ENGINE-SPOT-ONLY-001: below-the-gate futures machinery test —
        # the research escape flag is the sanctioned construction path.
        grid = _grid(instruments=["NSE-NIFTY-FUT"], unsafe_allow_out_of_scope_instruments=True)
        with AuditWriter.open(tmp_path / "a.jsonl") as audit:
            summary = run_sweep_grid(
                grid,
                bars_for=lambda _i, _t: bars,
                store_for=make_store_for(tmp_path / "results", grid),
                dedup=_dedup(),
                audit=audit,
                roll_schedule_by_instrument={"NSE-NIFTY-FUT": _FUT_SCHEDULE},
            )
        assert summary.tuples_run == 1
        assert summary.persisted > 0

    def test_futures_grid_without_a_schedule_map_fails_the_invariant(self, tmp_path: Path) -> None:
        # A futures tuple with NO schedule cannot mint an identity — the
        # SweepRunConfig invariant propagates (a genuine wiring bug, never a
        # silent constant-key sweep).
        bars = _trending_bars(n_days=2)
        grid = _grid(instruments=["NSE-NIFTY-FUT"], unsafe_allow_out_of_scope_instruments=True)
        with (
            AuditWriter.open(tmp_path / "a.jsonl") as audit,
            pytest.raises(ValueError, match="roll_schedule is required for"),
        ):
            run_sweep_grid(
                grid,
                bars_for=lambda _i, _t: bars,
                store_for=make_store_for(tmp_path / "results", grid),
                dedup=_dedup(),
                audit=audit,
            )

    def test_spot_grid_is_byte_identical_with_and_without_the_argument(
        self, tmp_path: Path
    ) -> None:
        pytest.importorskip("polars")
        bars = _trending_bars(n_days=4)

        def _run(tag: str, schedule_map: dict[str, tuple[int, ...]] | None) -> bytes:
            grid = _grid()
            results = tmp_path / tag
            with AuditWriter.open(tmp_path / f"{tag}.jsonl") as audit:
                summary = run_sweep_grid(
                    grid,
                    bars_for=lambda _i, _t: bars,
                    store_for=make_store_for(results, grid),
                    dedup=_dedup(),
                    audit=audit,
                    roll_schedule_by_instrument=schedule_map,
                )
            assert summary.tuples_run == 1
            store_path = tuple_store_path(results, next(iter_sweep_tuples(grid)), grid)
            return store_path.read_bytes()

        # None, an empty map, and a map naming ONLY the alias (a mixed launch)
        # all leave a spot tuple's persisted bytes identical.
        base = _run("none", None)
        assert _run("empty", {}) == base
        assert _run("foreign", {"NSE-NIFTY-FUT": _FUT_SCHEDULE}) == base


# ────── DEC-SWEEP-APRIORI-DEFAULT-001 (MIGRATION.md #1752) ──────
# The grid driver's apriori wiring: the ONE hit_memo_cell_key builder, the
# per-cell memo threading (reset on a cell change, None on legacy), the
# EMIT-ON-apriori START/COMPLETE payload keys, and the DISTINCT
# tuples_failed_apriori_budget counter.


class TestHitMemoCellKey:
    """The memo's cell key == the kernel-cache determinant set (§11 COMMON)."""

    def test_key_components(self) -> None:
        from brutex.engine.sweep_orchestrator import hit_memo_cell_key

        grid = _grid()
        tuple_ = next(iter_sweep_tuples(grid))
        key = hit_memo_cell_key(tuple_, grid, (738885,))
        assert key == (
            tuple_.instrument,
            tuple_.tf.label,
            None,  # proximity None flattens to None
            grid.prior_n,
            tuple_.fib_anchor.value,
            (738885,),
            False,  # apply_1day_mask for a spot 1minute tuple
        )

    def test_direction_sl_tp_k_are_excluded(self) -> None:
        """The key survives the whole cell's k-ladder — the memo's point."""
        from brutex.engine.sweep_orchestrator import hit_memo_cell_key

        grid = _grid(
            directions=[Side.BUY, Side.SELL],
            k_values=[1, 2],
            stop_losses=[80, None],
            take_profits=[50, None],
        )
        keys = {hit_memo_cell_key(t, grid, None) for t in iter_sweep_tuples(grid)}
        assert len(keys) == 1  # ONE cell across every direction/k/sl/tp tuple

    def test_proximity_flattens_to_bps(self) -> None:
        from brutex.engine.sweep_orchestrator import hit_memo_cell_key

        prox = ProximityParams(pivot_proximity_bps=12)
        grid = _grid(proximities=[prox])
        tuple_ = next(iter_sweep_tuples(grid))
        key = hit_memo_cell_key(tuple_, grid, None)
        assert key[2] == (
            prox.pivot_proximity_bps,
            prox.pdh_pdl_proximity_bps,
            prox.fib_proximity_bps,
        )


class TestRunSweepGridAprioriWiring:
    """START/COMPLETE keys + memo threading + the budget-trip counter."""

    @staticmethod
    def _capture_memos(
        tmp_path: Path, grid: SweepGridConfig, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[SweepGridSummary, list[object], Path]:
        """Run the grid with a fake run_one_tuple capturing hit_count_memo."""
        memos: list[object] = []

        def _fake(grid_: object, tuple_: object, **kwargs: object) -> object:
            memos.append(kwargs["hit_count_memo"])
            return None  # classify as no-bars: no engine work

        monkeypatch.setattr("brutex.engine.sweep_orchestrator.run_one_tuple", _fake)
        audit_path = tmp_path / "grid.jsonl"
        with AuditWriter.open(audit_path) as audit:
            summary = run_sweep_grid(
                grid,
                bars_for=lambda _i, _t: [],
                store_for=make_store_for(tmp_path / "results", grid),
                dedup=_dedup(),
                audit=audit,
            )
        return summary, memos, audit_path

    def test_legacy_start_and_complete_payloads_carry_no_apriori_keys(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("BRUTEX_SWEEP_APRIORI", raising=False)
        grid = _grid(k_values=[1, 2])
        _summary, memos, audit_path = self._capture_memos(tmp_path, grid, monkeypatch)
        assert memos == [None, None]  # legacy: memo structurally absent
        start = next(e for e in read_entries(audit_path) if e.action == "GRID_SWEEP_START")
        assert "apriori" not in start.payload
        complete = next(e for e in read_entries(audit_path) if e.action == "GRID_SWEEP_COMPLETE")
        assert "tuples_failed_apriori_budget" not in complete.payload

    def test_apriori_start_records_the_mode_and_complete_the_counter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from brutex.engine.sweep_apriori import DEFAULT_MAX_MASK_BARS

        monkeypatch.setenv("BRUTEX_SWEEP_APRIORI", "1")
        grid = _grid(k_values=[1, 2])
        _summary, memos, audit_path = self._capture_memos(tmp_path, grid, monkeypatch)
        # ONE memo dict shared across the whole cell's k-ladder.
        assert len(memos) == 2
        assert isinstance(memos[0], dict)
        assert memos[0] is memos[1]
        start = next(e for e in read_entries(audit_path) if e.action == "GRID_SWEEP_START")
        assert start.payload["apriori"] == {
            "enabled": True,
            "max_mask_bars": DEFAULT_MAX_MASK_BARS,
        }
        complete = next(e for e in read_entries(audit_path) if e.action == "GRID_SWEEP_COMPLETE")
        assert complete.payload["tuples_failed_apriori_budget"] == 0

    def test_overfit_gated_start_records_gate_config_overfit_leg(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DEC-SWEEP-OVERFIT-RECOUNT-RECOVERY-001 (MIGRATION.md #1851): an
        overfit-gated grid's GRID_SWEEP_START payload records the #1336
        overfit fragment ``[dsr_min, weight, default_skew,
        default_excess_kurtosis]`` under ``gate_config`` — the MINT side of
        the recount recovery channel `recorded_overfit_from_audit` reads."""
        monkeypatch.delenv("BRUTEX_SWEEP_APRIORI", raising=False)
        grid = _grid(overfit=OverfitGateConfig(dsr_min=0.9))
        _summary, _memos, audit_path = self._capture_memos(tmp_path, grid, monkeypatch)
        start = next(e for e in read_entries(audit_path) if e.action == "GRID_SWEEP_START")
        assert start.payload["gate_config"] == {"overfit": [0.9, 0.05, 0.0, 0.0]}

    def test_ungated_start_carries_no_gate_config_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The EMIT-ON-active absence pin (#1851): an UNGATED grid writes NO
        ``gate_config`` key — absent == legacy, never a recorded default."""
        monkeypatch.delenv("BRUTEX_SWEEP_APRIORI", raising=False)
        grid = _grid(k_values=[1])
        _summary, _memos, audit_path = self._capture_memos(tmp_path, grid, monkeypatch)
        start = next(e for e in read_entries(audit_path) if e.action == "GRID_SWEEP_START")
        assert "gate_config" not in start.payload

    def test_memo_resets_on_a_cell_change(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two cells (two timeframes) → two DISTINCT memo dicts (reset-on-change)."""
        monkeypatch.setenv("BRUTEX_SWEEP_APRIORI", "1")
        grid = _grid(timeframes=[Timeframe("1minute", 1), Timeframe("5minute", 5)], k_values=[1, 2])
        _summary, memos, _audit = self._capture_memos(tmp_path, grid, monkeypatch)
        # Enumeration order: tf-major → [1m k1, 1m k2, 5m k1, 5m k2].
        assert len(memos) == 4
        assert memos[0] is memos[1]
        assert memos[2] is memos[3]
        assert memos[0] is not memos[2]

    def test_budget_trip_counts_distinctly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A SweepAprioriBudgetError is counted APART from generic failures —
        by EXACT isinstance, so a LOOK-ALIKE class name is never miscounted
        (review fix round 1 m8)."""
        from brutex.engine.sweep_run import SweepAprioriBudgetError

        class SweepAprioriBudgetErrorX(SweepComboError):
            """The impostor: a name-PREFIX collision that must never count."""

        monkeypatch.setenv("BRUTEX_SWEEP_APRIORI", "1")
        grid = _grid(k_values=[1, 2, 3])

        def _fake(grid_: object, tuple_: object, **kwargs: object) -> object:
            k = tuple_.k  # type: ignore[attr-defined]
            if k == 1:
                raise SweepAprioriBudgetErrorX("an impostor class name")
            if k == 2:
                raise SweepAprioriBudgetError("apriori cell 'x': k=2 ... exceeds ...")
            if k == 3:
                raise SweepComboError("generic combo failure")
            return None

        monkeypatch.setattr("brutex.engine.sweep_orchestrator.run_one_tuple", _fake)
        audit_path = tmp_path / "grid.jsonl"
        with AuditWriter.open(audit_path) as audit:
            summary = run_sweep_grid(
                grid,
                bars_for=lambda _i, _t: [],
                store_for=make_store_for(tmp_path / "results", grid),
                dedup=_dedup(),
                audit=audit,
            )
        assert summary.tuples_failed == 3
        assert summary.tuples_failed_apriori_budget == 1  # the k=2 trip ONLY
        complete = next(e for e in read_entries(audit_path) if e.action == "GRID_SWEEP_COMPLETE")
        assert complete.payload["tuples_failed_apriori_budget"] == 1

    def test_summary_field_defaults_zero(self) -> None:
        """The defaulted-field back-compat law (the quarantined_prior precedent)."""
        summary = SweepGridSummary(
            tuples_total=1,
            tuples_run=1,
            tuples_skipped_no_bars=0,
            enumerated=1,
            conflict_pruned=0,
            skipped_dedup=0,
            evaluated=1,
            ranked=0,
            persisted=0,
            tuples_failed=0,
        )
        assert summary.tuples_failed_apriori_budget == 0
