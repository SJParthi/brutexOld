"""VWAP/EMA price-source sweep axis — DEC-NIFTY-PRICE-SOURCE-AXIS-001.

The cell-coordinate axis mirroring the ``fib_anchor`` precedent, with the FOUR
build overrides:

* **Override 1 — DEFAULT-OMIT.** ``vwap_source`` / ``ema_source`` are OMITTED
  from every canonical params / store-address / cache key / grid identity when
  they equal the default ``(HLC3, CLOSE)``, so existing run_ids stay
  byte-identical. The decisive golden (:class:`TestOverride1DefaultOmit`) pins
  the default cell's ``canonical_trade_params`` + ``run_id`` byte-for-byte — it
  is impossible to pass under unconditional emit.
* **Override 2 — PYTHON ONLY.** No Rust touched; no ``indicator_version`` bump.
* **Override 3 — budget honesty.** The axis is cardinality-1 dormant by default;
  a 5-cell combined run does NOT fit ≤ 2 days (staged separate invocations do).
* **Override 4 — enum completeness.** All 5 ``PriceSource`` members are tested
  directly (incl. the HIGH/LOW deferred variants); ``hl2`` / ``ohlc4`` are
  deliberately absent from the enum (pinned here).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from brutex.data.models import CandleBar
from brutex.data.option_dedup import OptionDedupIndex
from brutex.engine.cascade_wire import CascadeTupleSpec, _spec_from_dict, _spec_to_dict
from brutex.engine.results_store import ResultsStore
from brutex.engine.stage1a import (
    _HLC3_DIVISOR,
    _price_to_paisa,
    _reduce_ema_source_float,
    _reduce_vwap_source_paisa,
    build_bar_bits,
)
from brutex.engine.sweep_kernel import ResidentSweepKernel
from brutex.engine.sweep_orchestrator import (
    SweepGridConfig,
    SweepTuple,
    Timeframe,
    _tuple_digest,
    bloom_capacity_hint,
    count_sweep_tuples,
    iter_sweep_tuples,
    make_store_for,
    run_sweep_grid,
    tuple_store_path,
)
from brutex.engine.sweep_run import (
    ComboDedup,
    SweepRunConfig,
    canonical_trade_params,
    mask_run_id,
    run_persisted_sweep,
)
from brutex.engine.sweep_supervisor import (
    _grid_descriptor,
    _tuple_cache_params,
    grid_identity,
    tuple_cache_key,
)
from brutex.orchestrator.audit import AuditWriter
from brutex.strategies.spec import canonical_spec_params
from brutex.strategies.types import (
    DEFAULT_PRICE_SOURCE_CELL,
    FibAnchorKind,
    PriceSource,
    PriceSourceCell,
    Side,
    parse_price_source_cells,
)

_IST = ZoneInfo("Asia/Kolkata")


def _bar(day: int, minute: int, o: float, c: float) -> CandleBar:
    """One IST-session minute bar (stored UTC) with Decimal OHLC."""
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
    bars: list[CandleBar] = []
    for d in range(n_days):
        price = 100.0
        for m in range(per_day):
            close = price + 0.6 if m % 3 else price - 0.3
            bars.append(_bar(d, m, price, close))
            price = close
    return bars


def _spike_bars(n_days: int = 8, per_day: int = 120) -> list[CandleBar]:
    """Close flat at 100, high spiked to 130 — so HLC3 ≫ close and OPEN/LOW ≪ HLC3.

    Engineered so a non-default VWAP source flips bits 52-53 and a non-default EMA
    source flips an EMA bit (0-5) — the bar_bits source-specificity is provable,
    not data-fragile.
    """
    bars: list[CandleBar] = []
    for d in range(n_days):
        for m in range(per_day):
            ts = (
                datetime(2026, 1, 5, 9, 16, tzinfo=_IST) + timedelta(days=d, minutes=m)
            ).astimezone(UTC)
            bars.append(
                CandleBar(
                    timestamp=ts,
                    open=Decimal("100"),
                    high=Decimal("130"),
                    low=Decimal("99"),
                    close=Decimal("100"),
                    volume=1000,
                    open_interest=None,
                )
            )
    return bars


# A fixed bar whose HLC3 paisa sum has remainder 2 (exercises ROUND_HALF_UP on
# thirds: 30014/3 = 10004.666… → 10005).
_GOLDEN_BAR = CandleBar(
    timestamp=datetime(2026, 1, 5, 9, 16, tzinfo=UTC),
    open=Decimal("100.03"),
    high=Decimal("100.07"),
    low=Decimal("100.02"),
    close=Decimal("100.05"),
    volume=100,
    open_interest=None,
)

#: A VALID (L ≤ O,C ≤ H) 2dp bar on which the two EMA-HLC3 float forms DIVERGE in
#: the last ULP: ``float((H+L+C)/Decimal(3)) == 13999.993333333334`` but
#: ``(float(H)+float(L)+float(C))/3.0 == 13999.993333333332``. Used by NIT-1's
#: teeth so a regression to float-add-then-divide is DETECTABLE (the golden bar
#: above coincides for both forms and could not catch it).
_HLC3_DIVERGING_BAR = CandleBar(
    timestamp=datetime(2026, 1, 5, 9, 17, tzinfo=UTC),
    open=Decimal("14000.00"),
    high=Decimal("14000.00"),
    low=Decimal("13999.98"),
    close=Decimal("14000.00"),
    volume=100,
    open_interest=None,
)


# ─────────────────────────── enum + cell + parser ───────────────────────────


class TestPriceSourceEnum:
    def test_exactly_five_members(self) -> None:
        assert {m.value for m in PriceSource} == {"open", "high", "low", "close", "hlc3"}

    def test_hl2_and_ohlc4_deliberately_absent(self) -> None:
        # Override 4 — the rounding-tie clones are NOT in the enum.
        values = {m.value for m in PriceSource}
        assert "hl2" not in values
        assert "ohlc4" not in values

    def test_default_cell_is_hlc3_close(self) -> None:
        assert PriceSourceCell() == DEFAULT_PRICE_SOURCE_CELL
        assert PriceSourceCell().vwap_source is PriceSource.HLC3
        assert PriceSourceCell().ema_source is PriceSource.CLOSE

    def test_cell_is_hashable_and_frozen(self) -> None:
        # Grid-axis product + dict.fromkeys de-dup require hashability.
        assert len({PriceSourceCell(), PriceSourceCell()}) == 1
        with pytest.raises((AttributeError, TypeError)):
            PriceSourceCell().vwap_source = PriceSource.OPEN  # type: ignore[misc]


class TestParsePriceSourceCells:
    def test_empty_yields_baseline(self) -> None:
        assert parse_price_source_cells("") == [DEFAULT_PRICE_SOURCE_CELL]
        assert parse_price_source_cells("  ,  ") == [DEFAULT_PRICE_SOURCE_CELL]

    def test_full_ofat_lattice(self) -> None:
        cells = parse_price_source_cells("hlc3:close,open:close,close:close,hlc3:open,hlc3:hlc3")
        assert cells == [
            PriceSourceCell(PriceSource.HLC3, PriceSource.CLOSE),
            PriceSourceCell(PriceSource.OPEN, PriceSource.CLOSE),
            PriceSourceCell(PriceSource.CLOSE, PriceSource.CLOSE),
            PriceSourceCell(PriceSource.HLC3, PriceSource.OPEN),
            PriceSourceCell(PriceSource.HLC3, PriceSource.HLC3),
        ]

    def test_whitespace_and_dedup(self) -> None:
        cells = parse_price_source_cells(" open:close , open:close ,hlc3:hlc3")
        assert cells == [
            PriceSourceCell(PriceSource.OPEN, PriceSource.CLOSE),
            PriceSourceCell(PriceSource.HLC3, PriceSource.HLC3),
        ]

    @pytest.mark.parametrize("bad", ["hlc3", "hlc3:close:open", "hlc3close"])
    def test_malformed_separator_rejected(self, bad: str) -> None:
        with pytest.raises(ValueError, match="exactly one ':'"):
            parse_price_source_cells(bad)

    @pytest.mark.parametrize("bad", ["hl2:close", "open:ohlc4", "junk:close"])
    def test_unknown_token_rejected(self, bad: str) -> None:
        with pytest.raises(ValueError):
            parse_price_source_cells(bad)


# ───────────────────────── reduction bit-identity ───────────────────────────


class TestReductionBitIdentity:
    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            (PriceSource.OPEN, 10003),
            (PriceSource.HIGH, 10007),
            (PriceSource.LOW, 10002),
            (PriceSource.CLOSE, 10005),
            (PriceSource.HLC3, 10005),  # 30014/3 = 10004.666… → ROUND_HALF_UP → 10005
        ],
    )
    def test_vwap_reduction_all_five(self, source: PriceSource, expected: int) -> None:
        op = _price_to_paisa(_GOLDEN_BAR.open)
        hp = _price_to_paisa(_GOLDEN_BAR.high)
        lp = _price_to_paisa(_GOLDEN_BAR.low)
        cp = _price_to_paisa(_GOLDEN_BAR.close)
        assert (
            _reduce_vwap_source_paisa(
                source, _GOLDEN_BAR, open_paisa=op, high_paisa=hp, low_paisa=lp, close_paisa=cp
            )
            == expected
        )

    def test_vwap_hlc3_is_the_verbatim_pre_axis_line(self) -> None:
        op = _price_to_paisa(_GOLDEN_BAR.open)
        hp = _price_to_paisa(_GOLDEN_BAR.high)
        lp = _price_to_paisa(_GOLDEN_BAR.low)
        cp = _price_to_paisa(_GOLDEN_BAR.close)
        assert _reduce_vwap_source_paisa(
            PriceSource.HLC3,
            _GOLDEN_BAR,
            open_paisa=op,
            high_paisa=hp,
            low_paisa=lp,
            close_paisa=cp,
        ) == _price_to_paisa(
            (_GOLDEN_BAR.high + _GOLDEN_BAR.low + _GOLDEN_BAR.close) / _HLC3_DIVISOR
        )

    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            (PriceSource.OPEN, 100.03),
            (PriceSource.HIGH, 100.07),
            (PriceSource.LOW, 100.02),
            (PriceSource.CLOSE, 100.05),
            (PriceSource.HLC3, 100.04666666666667),
        ],
    )
    def test_ema_reduction_all_five(self, source: PriceSource, expected: float) -> None:
        assert _reduce_ema_source_float(source, _GOLDEN_BAR) == expected

    def test_ema_close_is_the_verbatim_pre_axis_line(self) -> None:
        assert _reduce_ema_source_float(PriceSource.CLOSE, _GOLDEN_BAR) == float(_GOLDEN_BAR.close)

    def test_ema_hlc3_is_decimal_divide_then_one_cast(self) -> None:
        # DIVERGING-bar teeth (NIT-1): on _HLC3_DIVERGING_BAR the two forms differ
        # in the last ULP, so this pins the impl at the Decimal-divide-THEN-one-cast
        # value AND asserts it is NOT the float-add-then-divide form — a regression
        # to ``(f64(H)+f64(L)+f64(C))/3.0`` would now be CAUGHT (the old golden bar
        # coincided for both forms, so it could not detect that regression).
        high, low, close = (
            _HLC3_DIVERGING_BAR.high,
            _HLC3_DIVERGING_BAR.low,
            _HLC3_DIVERGING_BAR.close,
        )
        decimal_divide_then_cast = float((high + low + close) / _HLC3_DIVISOR)
        float_add_then_divide = (float(high) + float(low) + float(close)) / 3.0
        # The two forms genuinely diverge on this bar (guards against a coinciding
        # fixture silently re-tautologising the test).
        assert decimal_divide_then_cast != float_add_then_divide
        result = _reduce_ema_source_float(PriceSource.HLC3, _HLC3_DIVERGING_BAR)
        # Exact expected value pin (impl == the Decimal-divide-then-one-cast path).
        assert result == 13999.993333333334
        assert result == decimal_divide_then_cast
        assert result != float_add_then_divide


# ─────────────────── Override 1: the decisive default-omit golden ───────────


class TestOverride1DefaultOmit:
    # The captured pre-change default run_id (fixed provenance). Impossible to
    # reproduce under unconditional emit (which would add the two keys → new hash).
    _DEFAULT_TRADE_RUN_ID = "b264fd0b6af6fd2b43448db0952baaf793e90330a6dbcbabe4b48ae2ac2521f3"

    def _default_trade_params(self) -> dict[str, object]:
        return canonical_trade_params(
            sl_paisa=80,
            tp_paisa=50,
            prior_n=3,
            proximity=None,
            start_equity_paisa=10_000_000,
            fib_anchor=FibAnchorKind.PREV_DAY,
        )

    def test_default_trade_params_omit_the_new_keys(self) -> None:
        params = self._default_trade_params()
        assert "vwap_source" not in params
        assert "ema_source" not in params
        # Byte-identical key set to the pre-axis 6-key layout.
        assert set(params) == {
            "sl_paisa",
            "tp_paisa",
            "prior_n",
            "proximity_bps",
            "fib_anchor",
            "start_equity_paisa",
        }

    def test_default_trade_params_explicit_default_sources_omit_too(self) -> None:
        # Passing the defaults EXPLICITLY still omits (default-omit, not
        # argument-presence-omit).
        with_defaults = canonical_trade_params(
            sl_paisa=80,
            tp_paisa=50,
            prior_n=3,
            proximity=None,
            start_equity_paisa=10_000_000,
            fib_anchor=FibAnchorKind.PREV_DAY,
            vwap_source=PriceSource.HLC3,
            ema_source=PriceSource.CLOSE,
        )
        assert with_defaults == self._default_trade_params()

    def test_default_cell_run_id_is_byte_identical(self) -> None:
        # THE decisive golden — the default (HLC3, CLOSE) cell reproduces the
        # captured run_id exactly (Override 1 / the sibling twin-baseline invariant).
        run_id = mask_run_id(
            mask=0b10101,
            direction="bull",
            instrument="NIFTY",
            tf="1minute",
            mode="A",
            params=self._default_trade_params(),
            data_snapshot_hash="snap0001",
            indicator_version="ind-v1",
            code_commit_sha="deadbeef",
        )
        assert run_id == self._DEFAULT_TRADE_RUN_ID

    @pytest.mark.parametrize(
        ("vwap", "ema", "extra"),
        [
            (PriceSource.OPEN, PriceSource.CLOSE, "vwap_source"),
            (PriceSource.CLOSE, PriceSource.CLOSE, "vwap_source"),
            (PriceSource.HLC3, PriceSource.OPEN, "ema_source"),
            (PriceSource.HLC3, PriceSource.HLC3, "ema_source"),
        ],
    )
    def test_non_default_cell_emits_key_and_distinct_run_id(
        self, vwap: PriceSource, ema: PriceSource, extra: str
    ) -> None:
        params = canonical_trade_params(
            sl_paisa=80,
            tp_paisa=50,
            prior_n=3,
            proximity=None,
            start_equity_paisa=10_000_000,
            fib_anchor=FibAnchorKind.PREV_DAY,
            vwap_source=vwap,
            ema_source=ema,
        )
        assert extra in params
        run_id = mask_run_id(
            mask=0b10101,
            direction="bull",
            instrument="NIFTY",
            tf="1minute",
            mode="A",
            params=params,
            data_snapshot_hash="snap0001",
            indicator_version="ind-v1",
            code_commit_sha="deadbeef",
        )
        assert run_id != self._DEFAULT_TRADE_RUN_ID

    def test_spec_params_default_omit(self) -> None:
        params = canonical_spec_params(
            sl_paisa=80,
            tp_paisa=50,
            start_equity_paisa=10_000_000,
            fib_anchor=FibAnchorKind.PREV_DAY,
        )
        assert "vwap_source" not in params
        assert "ema_source" not in params
        assert set(params) == {"sl_paisa", "tp_paisa", "start_equity_paisa", "fib_anchor"}

    @pytest.mark.parametrize(
        ("vwap", "ema", "extra"),
        [
            (PriceSource.OPEN, PriceSource.CLOSE, "vwap_source"),
            (PriceSource.HLC3, PriceSource.HLC3, "ema_source"),
        ],
    )
    def test_spec_params_non_default_emits(
        self, vwap: PriceSource, ema: PriceSource, extra: str
    ) -> None:
        params = canonical_spec_params(
            sl_paisa=80,
            tp_paisa=50,
            start_equity_paisa=10_000_000,
            fib_anchor=FibAnchorKind.PREV_DAY,
            vwap_source=vwap,
            ema_source=ema,
        )
        assert extra in params


# ─────────────────────── build_bar_bits source specificity ──────────────────


class TestBuildBarBitsSourceSpecificity:
    def test_default_cell_is_byte_identical_to_pre_axis(self) -> None:
        bars = _trending_bars(n_days=6, per_day=60)
        # No source args == the pre-axis build; explicit defaults must match.
        assert build_bar_bits(bars) == build_bar_bits(
            bars, vwap_source=PriceSource.HLC3, ema_source=PriceSource.CLOSE
        )

    def test_non_default_source_changes_bar_bits(self) -> None:
        bars = _spike_bars()
        base = build_bar_bits(bars)
        # A non-default VWAP source flips bits 52-53 (VWAP position).
        vwap_low = build_bar_bits(bars, vwap_source=PriceSource.LOW)
        # A non-default EMA source flips an EMA bit (0-5).
        ema_hlc3 = build_bar_bits(bars, ema_source=PriceSource.HLC3)
        assert vwap_low != base
        assert ema_hlc3 != base


# ───────────────────────── kernel source-specificity guard ──────────────────


class TestKernelSourceGuard:
    def test_kernel_records_sources(self) -> None:
        bars = _trending_bars(n_days=4, per_day=40)
        kernel = ResidentSweepKernel(
            bars, vwap_source=PriceSource.OPEN, ema_source=PriceSource.HLC3
        )
        assert kernel.vwap_source is PriceSource.OPEN
        assert kernel.ema_source is PriceSource.HLC3

    def _cfg(self, **kw: object) -> SweepRunConfig:
        base: dict[str, object] = {
            "instrument": "NIFTY",
            "tf": "1minute",
            "direction": Side.BUY,
            "mode": "A",
            "condition_bits": [0, 1, 2],
            "k": 1,
            "data_snapshot_hash": "s",
            "indicator_version": "i",
            "code_commit_sha": "c",
            "tf_minutes": 1,
        }
        base.update(kw)
        return SweepRunConfig(**base)  # type: ignore[arg-type]

    def test_run_persisted_sweep_rejects_vwap_source_mismatch(self, tmp_path: Path) -> None:
        bars = _trending_bars(n_days=4, per_day=40)
        kernel = ResidentSweepKernel(bars, vwap_source=PriceSource.OPEN)
        cfg = self._cfg(vwap_source=PriceSource.HLC3)  # mismatch vs the OPEN kernel
        with (
            AuditWriter.open(tmp_path / "a.jsonl") as audit,
            pytest.raises(ValueError, match="kernel was built for VWAP source"),
        ):
            run_persisted_sweep(
                bars,
                cfg,
                store=ResultsStore(path=tmp_path / "r.parquet"),
                dedup=ComboDedup(OptionDedupIndex(capacity=1000, fpr_target=1e-6)),
                audit=audit,
                kernel=kernel,
            )

    def test_run_persisted_sweep_rejects_ema_source_mismatch(self, tmp_path: Path) -> None:
        bars = _trending_bars(n_days=4, per_day=40)
        kernel = ResidentSweepKernel(bars, ema_source=PriceSource.HLC3)
        cfg = self._cfg(ema_source=PriceSource.CLOSE)  # mismatch vs the HLC3 kernel
        with (
            AuditWriter.open(tmp_path / "a.jsonl") as audit,
            pytest.raises(ValueError, match="kernel was built for EMA source"),
        ):
            run_persisted_sweep(
                bars,
                cfg,
                store=ResultsStore(path=tmp_path / "r.parquet"),
                dedup=ComboDedup(OptionDedupIndex(capacity=1000, fpr_target=1e-6)),
                audit=audit,
                kernel=kernel,
            )


# ─────────────────────── grid axis: count / iter / identity ─────────────────


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


_OFAT_CELLS = (
    PriceSourceCell(PriceSource.HLC3, PriceSource.CLOSE),
    PriceSourceCell(PriceSource.OPEN, PriceSource.CLOSE),
    PriceSourceCell(PriceSource.HLC3, PriceSource.HLC3),
)


class TestGridAxis:
    def test_default_grid_is_cardinality_one(self) -> None:
        grid = _grid()
        assert grid.price_source_cells == (PriceSourceCell(),)
        assert count_sweep_tuples(grid) == 1

    def test_axis_multiplies_the_tuple_count(self) -> None:
        grid = _grid(price_source_cells=list(_OFAT_CELLS))
        assert count_sweep_tuples(grid) == 3
        tuples = list(iter_sweep_tuples(grid))
        assert [t.price_source for t in tuples] == list(_OFAT_CELLS)

    def test_axis_is_deduped(self) -> None:
        grid = _grid(price_source_cells=[_OFAT_CELLS[0], _OFAT_CELLS[0], _OFAT_CELLS[1]])
        assert grid.price_source_cells == (_OFAT_CELLS[0], _OFAT_CELLS[1])

    def test_empty_axis_rejected(self) -> None:
        with pytest.raises(ValueError, match="price_source_cells must be a non-empty axis"):
            _grid(price_source_cells=[])

    def test_bloom_capacity_hint_scales_with_cells(self) -> None:
        base = bloom_capacity_hint(_grid())
        scaled = bloom_capacity_hint(_grid(price_source_cells=list(_OFAT_CELLS)))
        assert scaled == base * 3

    def test_to_run_config_threads_sources(self) -> None:
        grid = _grid(price_source_cells=[_OFAT_CELLS[1]])
        tuple_ = next(iter_sweep_tuples(grid))
        cfg = tuple_.to_run_config(grid)
        assert cfg.vwap_source is PriceSource.OPEN
        assert cfg.ema_source is PriceSource.CLOSE


class TestStoreAddressDefaultOmit:
    def test_default_cell_store_path_byte_identical(self) -> None:
        # A tuple with the default cell hashes identically to a tuple built
        # without the axis (the store address is byte-identical).
        grid = _grid()
        no_axis = SweepTuple(
            instrument="NIFTY",
            tf=Timeframe("1minute", 1),
            direction=Side.BUY,
            k=1,
            sl_paisa=80,
            tp_paisa=50,
            proximity=None,
        )
        with_default = SweepTuple(
            instrument="NIFTY",
            tf=Timeframe("1minute", 1),
            direction=Side.BUY,
            k=1,
            sl_paisa=80,
            tp_paisa=50,
            proximity=None,
            price_source=PriceSourceCell(),
        )
        assert _tuple_digest(no_axis, grid) == _tuple_digest(with_default, grid)

    def test_non_default_cell_store_path_distinct(self) -> None:
        grid = _grid()
        base = SweepTuple(
            instrument="NIFTY",
            tf=Timeframe("1minute", 1),
            direction=Side.BUY,
            k=1,
            sl_paisa=80,
            tp_paisa=50,
            proximity=None,
        )
        variant = SweepTuple(
            instrument="NIFTY",
            tf=Timeframe("1minute", 1),
            direction=Side.BUY,
            k=1,
            sl_paisa=80,
            tp_paisa=50,
            proximity=None,
            price_source=PriceSourceCell(PriceSource.OPEN, PriceSource.CLOSE),
        )
        assert _tuple_digest(base, grid) != _tuple_digest(variant, grid)


class TestCacheKeyAndGridIdentityDefaultOmit:
    def test_default_cell_cache_key_omits_sources(self) -> None:
        grid = _grid()
        tuple_ = next(iter_sweep_tuples(grid))
        params = _tuple_cache_params(tuple_, grid)
        assert "vwap_source" not in params
        assert "ema_source" not in params

    @pytest.mark.parametrize(
        ("cell", "expected_key"),
        [
            # vwap non-default (ema default) — exercises the vwap emit branch.
            (PriceSourceCell(PriceSource.OPEN, PriceSource.CLOSE), "vwap_source"),
            # ema non-default (vwap default) — exercises the ema emit branch.
            (PriceSourceCell(PriceSource.HLC3, PriceSource.HLC3), "ema_source"),
        ],
    )
    def test_non_default_cell_cache_key_distinct(
        self, cell: PriceSourceCell, expected_key: str
    ) -> None:
        grid = _grid()
        base = next(iter_sweep_tuples(grid))
        variant = SweepTuple(
            instrument="NIFTY",
            tf=Timeframe("1minute", 1),
            direction=Side.BUY,
            k=1,
            sl_paisa=80,
            tp_paisa=50,
            proximity=None,
            price_source=cell,
        )
        assert expected_key in _tuple_cache_params(variant, grid)
        assert tuple_cache_key(base, grid) != tuple_cache_key(variant, grid)

    def test_baseline_grid_identity_byte_identical(self) -> None:
        # A grid with the sole-baseline axis has the SAME identity as a grid built
        # without the axis (Override 1 — the checkpoint key does not move).
        grid_no_axis = _grid()
        grid_baseline = _grid(price_source_cells=[PriceSourceCell()])
        descriptor = _grid_descriptor(grid_no_axis)
        assert "price_source_cells" not in descriptor
        assert grid_identity(grid_no_axis) == grid_identity(grid_baseline)

    def test_expanded_grid_identity_distinct(self) -> None:
        base = grid_identity(_grid())
        expanded = grid_identity(_grid(price_source_cells=list(_OFAT_CELLS)))
        assert base != expanded


# ─────────────────────────── cascade round-trip ─────────────────────────────


class TestCascadeSerializationRoundTrip:
    def _spec(self, **kw: object) -> CascadeTupleSpec:
        base: dict[str, object] = {
            "instrument": "NIFTY",
            "tf_label": "1minute",
            "tf_minutes": 1,
            "direction_label": "bull",
            "sl_paisa": 80,
            "tp_paisa": 50,
            "prior_n": 3,
            "proximity_bps": None,
            "start_equity_paisa": 10_000_000,
            "winners": (("a" * 64, 1),),
        }
        base.update(kw)
        return CascadeTupleSpec(**base)  # type: ignore[arg-type]

    def test_default_cell_manifest_omits_sources(self) -> None:
        d = _spec_to_dict(self._spec())
        assert "vwap_source" not in d
        assert "ema_source" not in d
        # Round-trips to the baseline sources.
        back = _spec_from_dict(d)
        assert back.vwap_source is PriceSource.HLC3
        assert back.ema_source is PriceSource.CLOSE

    def test_non_default_cell_round_trips(self) -> None:
        spec = self._spec(vwap_source=PriceSource.OPEN, ema_source=PriceSource.HLC3)
        d = _spec_to_dict(spec)
        assert d["vwap_source"] == "open"
        assert d["ema_source"] == "hlc3"
        back = _spec_from_dict(d)
        assert back.vwap_source is PriceSource.OPEN
        assert back.ema_source is PriceSource.HLC3


# ───────────────────────────── integration ──────────────────────────────────


class TestSweepIntegrationDistinctPerCell:
    def test_ofat_grid_yields_distinct_stores_and_run_ids(self, tmp_path: Path) -> None:
        pytest.importorskip("polars")
        bars = _trending_bars()
        grid = _grid(price_source_cells=list(_OFAT_CELLS))

        results_root = tmp_path / "results"
        with AuditWriter.open(tmp_path / "grid.jsonl") as audit:
            summary = run_sweep_grid(
                grid,
                bars_for=lambda _i, _t: bars,
                store_for=make_store_for(results_root, grid),
                dedup=ComboDedup(OptionDedupIndex(capacity=100_000, fpr_target=1e-6)),
                audit=audit,
            )
        assert summary.tuples_total == 3
        assert summary.tuples_run == 3

        # Each cell wrote a DISTINCT store file (default-omit → distinct addresses).
        paths = [tuple_store_path(results_root, t, grid) for t in iter_sweep_tuples(grid)]
        assert len(set(paths)) == 3
        for path in paths:
            assert path.exists()

        # Run-ids across cells are disjoint: the baseline cell's rows carry the
        # pre-axis identity; the perturbed cells carry distinct (keyed) identities.
        run_id_sets: list[set[str]] = []
        for path in paths:
            rows = ResultsStore.load(path).rows()
            assert rows  # each cell produced ranked survivors on the trending bars
            run_id_sets.append({r.run_id for r in rows})
        # No run_id is shared across two different cells.
        for i in range(len(run_id_sets)):
            for j in range(i + 1, len(run_id_sets)):
                assert run_id_sets[i].isdisjoint(run_id_sets[j])
