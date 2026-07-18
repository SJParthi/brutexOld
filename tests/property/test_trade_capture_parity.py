"""Property tests for the WP1 capture core (DEC-SWEEP-RESULTS-DEPTH-001).

Three invariants, each over hypothesis-generated random walks:

1. **Golden safety** — ``simulate_trades`` output is IDENTICAL with and without
   a ``detail_sink`` (the seam can never perturb the frozen goldens), and the
   sink stays positionally aligned with the exit-fill arithmetic intact.
2. **Parity tooth** — a stored row whose headline counts were folded
   INDEPENDENTLY from the oracle's trades always captures with
   ``parity_ok=True``; any perturbed count flips it to a loud ``False``.
3. **Suffix independence** — a trade's 1-minute excursions depend only on bars
   in ``[entry_ts, exit_ts]``: mutating every bar strictly after the exit
   cannot change them (the P-06 look-ahead discipline, applied to the
   post-hoc path walk).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from brutex.calendars.nse import IST
from brutex.data.models import CandleBar
from brutex.engine.combo_mask import mask_to_entry_signals
from brutex.engine.minute_path import MinuteIndex, trade_excursions_1m
from brutex.engine.results_store import ResultRow
from brutex.engine.trade_capture import capture_combo
from brutex.engine.trade_sim import TradeDetail, simulate_trades
from brutex.strategies.types import Side
from hypothesis import given, settings
from hypothesis import strategies as st

_DAY = date(2024, 1, 2)  # a regular NSE Tuesday
_BASE_TS = datetime(_DAY.year, _DAY.month, _DAY.day, 9, 16, tzinfo=IST).astimezone(UTC)
_RUN_ID = "ab" * 32


@dataclass(frozen=True, slots=True)
class _BarSpec:
    open_paisa: int
    up: int
    down: int
    close_off: int  # close = open + close_off, clamped into [low, high]


_bar_specs = st.builds(
    _BarSpec,
    open_paisa=st.integers(min_value=9_000, max_value=11_000),
    up=st.integers(min_value=0, max_value=80),
    down=st.integers(min_value=0, max_value=80),
    close_off=st.integers(min_value=-80, max_value=80),
)


def _bars_from_specs(specs: list[_BarSpec]) -> list[CandleBar]:
    bars: list[CandleBar] = []
    for i, spec in enumerate(specs):
        open_p = spec.open_paisa
        high_p = open_p + spec.up
        low_p = open_p - spec.down
        close_p = min(max(open_p + spec.close_off, low_p), high_p)
        bars.append(
            CandleBar(
                timestamp=_BASE_TS + timedelta(minutes=i),
                open=Decimal(open_p) / 100,
                high=Decimal(high_p) / 100,
                low=Decimal(low_p) / 100,
                close=Decimal(close_p) / 100,
                volume=0,
                open_interest=None,
            )
        )
    return bars


_walks = st.builds(
    lambda specs, signals, sl, tp: (specs, signals, sl, tp),
    specs=st.lists(_bar_specs, min_size=5, max_size=40),
    signals=st.lists(st.booleans(), min_size=40, max_size=40),
    sl=st.one_of(st.none(), st.integers(min_value=1, max_value=100)),
    tp=st.one_of(st.none(), st.integers(min_value=1, max_value=100)),
)


class TestSinkGoldenSafety:
    @given(walk=_walks, direction=st.sampled_from([Side.BUY, Side.SELL]))
    @settings(max_examples=60, deadline=None)
    def test_with_and_without_sink_identical(
        self,
        walk: tuple[list[_BarSpec], list[bool], int | None, int | None],
        direction: Side,
    ) -> None:
        specs, signals, sl, tp = walk
        bars = _bars_from_specs(specs)
        entry_signals = signals[: len(bars)]
        without = simulate_trades(
            bars,
            entry_signals,
            direction=direction,
            tf_minutes=1,
            exec_bars_1m=bars,  # tf==1m fixed point (DEC-FILL-EXEC-GRID-1M-001)
            stop_loss_paisa=sl,
            take_profit_paisa=tp,
        )
        sink: list[TradeDetail] = []
        with_sink = simulate_trades(
            bars,
            entry_signals,
            direction=direction,
            tf_minutes=1,
            exec_bars_1m=bars,  # tf==1m fixed point
            stop_loss_paisa=sl,
            take_profit_paisa=tp,
            detail_sink=sink,
        )
        assert with_sink == without
        assert [d.trade for d in sink] == with_sink
        for detail in sink:
            expected = (
                detail.entry_fill_paisa + detail.trade.pnl_paisa
                if direction is Side.BUY
                else detail.entry_fill_paisa - detail.trade.pnl_paisa
            )
            assert detail.exit_fill_paisa == expected


def _result_row(direction: str, mask: int, trades_pnl: list[int]) -> ResultRow:
    return ResultRow(
        run_id=_RUN_ID,
        instrument="NIFTY",
        tf="1minute",
        direction=direction,
        mode="A",
        mask_bits=mask,
        net_pnl_paisa=sum(trades_pnl),
        gross_profit_paisa=sum(p for p in trades_pnl if p > 0),
        gross_loss_paisa=-sum(p for p in trades_pnl if p < 0),
        max_drawdown_paisa=0,
        expectancy_paisa=0,
        total_trades=len(trades_pnl),
        winning_trades=sum(1 for p in trades_pnl if p > 0),
        losing_trades=sum(1 for p in trades_pnl if p < 0),
        sharpe_ratio=0.0,
        sortino_ratio=0.0,
        calmar_ratio=0.0,
        profit_factor=0.0,
        win_rate=0.0,
        max_drawdown_pct=0.0,
        avg_entry_price=Decimal("100.00"),
        avg_exit_price=Decimal("100.00"),
    )


class TestParityTooth:
    @given(
        walk=_walks,
        bits=st.lists(st.integers(min_value=0, max_value=3), min_size=40, max_size=40),
        direction=st.sampled_from(["bull", "bear"]),
    )
    @settings(max_examples=60, deadline=None)
    def test_independently_folded_row_always_passes(
        self,
        walk: tuple[list[_BarSpec], list[bool], int | None, int | None],
        bits: list[int],
        direction: str,
    ) -> None:
        specs, _signals, sl, tp = walk
        bars = _bars_from_specs(specs)
        bar_bits = bits[: len(bars)]
        mask = 0b1
        side = Side.BUY if direction == "bull" else Side.SELL
        # INDEPENDENT fold: the oracle's own trades, counted here (not via
        # capture_combo internals).
        trades = simulate_trades(
            bars,
            mask_to_entry_signals(bar_bits, mask),
            direction=side,
            tf_minutes=1,
            exec_bars_1m=bars,  # tf==1m fixed point
            stop_loss_paisa=sl,
            take_profit_paisa=tp,
        )
        row = _result_row(direction, mask, [t.pnl_paisa for t in trades])
        capture = capture_combo(
            row,
            bars=bars,
            bar_bits=bar_bits,
            tf_minutes=1,
            exec_bars_1m=bars,  # tf==1m fixed point
            sl_paisa=sl,
            tp_paisa=tp,
            minute_index=MinuteIndex.from_bars(bars),
            stamper=None,
        )
        assert capture.parity_ok is True
        assert len(capture.trades) == len(trades)
        assert [t.pnl_paisa for t in capture.trades] == [t.pnl_paisa for t in trades]
        # And a perturbed stored count is ALWAYS caught, loudly.
        perturbed = _result_row(direction, mask, [*[t.pnl_paisa for t in trades], 1])
        broken = capture_combo(
            perturbed,
            bars=bars,
            bar_bits=bar_bits,
            tf_minutes=1,
            exec_bars_1m=bars,  # tf==1m fixed point
            sl_paisa=sl,
            tp_paisa=tp,
            minute_index=None,
            stamper=None,
        )
        assert broken.parity_ok is False
        assert broken.trades == ()


class TestMinutePathSuffixIndependence:
    @given(
        walk=_walks,
        exit_bar_full_range=st.booleans(),
        direction=st.sampled_from([Side.BUY, Side.SELL]),
    )
    @settings(max_examples=60, deadline=None)
    def test_bars_after_exit_cannot_change_excursions(
        self,
        walk: tuple[list[_BarSpec], list[bool], int | None, int | None],
        exit_bar_full_range: bool,
        direction: Side,
    ) -> None:
        specs, _signals, _sl, _tp = walk
        bars = _bars_from_specs(specs)
        cut = max(2, len(bars) // 2)
        entry_ts = bars[0].timestamp
        exit_ts = bars[cut - 1].timestamp
        entry_fill = int(bars[0].open * 100)
        original = trade_excursions_1m(
            MinuteIndex.from_bars(bars),
            entry_ts=entry_ts,
            exit_ts=exit_ts,
            entry_fill_paisa=entry_fill,
            direction=direction,
            exit_bar_full_range=exit_bar_full_range,
        )
        # Mutate EVERY bar strictly after the exit to an extreme value.
        mutated = list(bars[:cut])
        for i in range(cut, len(bars)):
            mutated.append(
                CandleBar(
                    timestamp=bars[i].timestamp,
                    open=Decimal("500.00"),
                    high=Decimal("900.00"),
                    low=Decimal("1.00"),
                    close=Decimal("2.00"),
                    volume=0,
                    open_interest=None,
                )
            )
        recomputed = trade_excursions_1m(
            MinuteIndex.from_bars(mutated),
            entry_ts=entry_ts,
            exit_ts=exit_ts,
            entry_fill_paisa=entry_fill,
            direction=direction,
            exit_bar_full_range=exit_bar_full_range,
        )
        assert recomputed == original
