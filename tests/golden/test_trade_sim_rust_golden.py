"""Golden cross-check: the Rust trade-sim kernel is byte-identical to the oracle.

DEC-SWEEP-RUST-TRADESIM-001 (MIGRATION.md #562). Two guarantees:

1. **Parity** — over a frozen curated scenario corpus (every exit kind: end-of-
   data, SL/TP at level, next-open, forced 15:20, same-bar tie, BUY + SELL) the
   resident Rust kernel :class:`brutex.engine.rust_trade_sim.ResidentTradeSim`
   produces exactly the :func:`brutex.engine.trade_sim.simulate_trades` trades.
2. **Anti-shared-drift** — one hand-computed scenario asserts the ABSOLUTE trade
   values, so a regression that moved BOTH the kernel and the oracle in lock-step
   (which a pure parity test would miss) is still caught.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from brutex.calendars.nse import IST
from brutex.data.models import CandleBar
from brutex.engine.combo_mask import mask_to_entry_signals
from brutex.engine.rust_trade_sim import ResidentTradeSim
from brutex.engine.stage1a import build_bar_bits
from brutex.engine.trade_sim import FillPolicy, LevelFill, SameBarTie, simulate_trades
from brutex.metrics.types import ClosedTrade
from brutex.strategies.types import Side

pytestmark = pytest.mark.golden

_DAY = date(2024, 1, 2)


def _bar(hh: int, mm: int, o: str, h: str, lo: str, c: str) -> CandleBar:
    ts = datetime(_DAY.year, _DAY.month, _DAY.day, hh, mm, tzinfo=IST).astimezone(UTC)
    return CandleBar(
        timestamp=ts,
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(lo),
        close=Decimal(c),
        volume=0,
        open_interest=None,
    )


# A frozen curated corpus: (label, bars, mask, direction, sl, tp, policy).
def _corpus() -> list[tuple[str, list[CandleBar], int, Side, int | None, int | None, FillPolicy]]:
    morning = [
        _bar(9, 15, "100", "101", "99", "100"),
        _bar(9, 16, "100", "110", "92", "104"),
        _bar(9, 17, "104", "108", "95", "100"),
        _bar(9, 18, "100", "112", "88", "108"),
    ]
    # A window that crosses the 15:20 forced exit on its last bar.
    afternoon = [
        _bar(15, 17, "100", "101", "99", "100"),
        _bar(15, 18, "100", "106", "97", "103"),
        _bar(15, 19, "103", "109", "100", "105"),
        _bar(15, 20, "105", "112", "104", "110"),  # >= 15:20 → forced
    ]
    default = FillPolicy()
    return [
        ("long_end_of_data", morning, 0, Side.BUY, None, None, default),
        ("long_sl_at_level", morning, 0, Side.BUY, 800, None, default),
        ("long_tp_at_level", morning, 0, Side.BUY, None, 600, default),
        ("short_end_of_data", morning, 0, Side.SELL, None, None, default),
        ("short_sl_at_level", morning, 0, Side.SELL, 500, None, default),
        ("short_tp_at_level", morning, 0, Side.SELL, None, 700, default),
        (
            "long_sl_next_open",
            morning,
            0,
            Side.BUY,
            800,
            None,
            FillPolicy(stop_loss=LevelFill.NEXT_OPEN),
        ),
        (
            "long_tp_next_open",
            morning,
            0,
            Side.BUY,
            None,
            600,
            FillPolicy(take_profit=LevelFill.NEXT_OPEN),
        ),
        ("long_same_bar_sl_first", morning, 0, Side.BUY, 800, 600, default),
        (
            "long_same_bar_tp_first",
            morning,
            0,
            Side.BUY,
            800,
            600,
            FillPolicy(same_bar=SameBarTie.TP_FIRST),
        ),
        ("forced_exit", afternoon, 0, Side.BUY, 5000, 5000, default),
        ("never_fires", morning, 1 << 63, Side.BUY, None, None, default),
    ]


class TestKernelMatchesOracle:
    @pytest.mark.parametrize("case", _corpus(), ids=lambda c: c[0])
    def test_kernel_byte_identical_to_oracle(
        self,
        case: tuple[str, list[CandleBar], int, Side, int | None, int | None, FillPolicy],
    ) -> None:
        _label, bars, mask, direction, sl, tp, policy = case
        bar_bits = build_bar_bits(bars)
        kernel = ResidentTradeSim(bars, bar_bits, tf_minutes=1).simulate_masks(
            [mask], direction=direction, stop_loss_paisa=sl, take_profit_paisa=tp, policy=policy
        )
        oracle = [
            simulate_trades(
                bars,
                mask_to_entry_signals(bar_bits, mask),
                direction=direction,
                tf_minutes=1,
                stop_loss_paisa=sl,
                take_profit_paisa=tp,
                policy=policy,
            )
        ]
        assert kernel == oracle


class TestAbsoluteFrozenValues:
    """Hand-computed scenario — catches a drift that moved kernel AND oracle alike."""

    def test_long_end_of_data_exact_trade(self) -> None:
        # Prices in rupees; the canonical seam converts to paisa (×100, 2-dp grid).
        # mask=0 fires every bar → entry arms bar0, opens at open[1]=100.00=10000p.
        bars = [
            _bar(9, 15, "100", "101", "99", "100"),
            _bar(9, 16, "100", "110", "92", "100"),  # fav=11000-10000=1000, adv=10000-9200=800
            _bar(9, 17, "100", "108", "95", "104"),  # fav=800, adv=500 → no new max
        ]
        bar_bits = build_bar_bits(bars)
        trades = ResidentTradeSim(bars, bar_bits, tf_minutes=1).simulate_masks(
            [0], direction=Side.BUY
        )[0]
        assert len(trades) == 1
        t = trades[0]
        # End of data → close at close[2]=104.00=10400p; entry 10000p.
        assert t == ClosedTrade(
            pnl_paisa=400,
            mfe_paisa=1000,
            mae_paisa=800,
            holding_bars=2,
            entry_ts=bars[1].timestamp,
            exit_ts=bars[2].timestamp,
            direction=Side.BUY,
        )
