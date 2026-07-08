"""Golden cross-check: the Rust trade-sim kernel is byte-identical to the oracle.

DEC-SWEEP-RUST-TRADESIM-001 (MIGRATION.md #563). Two guarantees:

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
_NEXT_DAY = date(2024, 1, 3)  # the next NSE session (Wednesday)


def _bar(hh: int, mm: int, o: str, h: str, lo: str, c: str, *, day: date = _DAY) -> CandleBar:
    ts = datetime(day.year, day.month, day.day, hh, mm, tzinfo=IST).astimezone(UTC)
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
    # A window stepping ACROSS a trading-day boundary (session's last stored
    # bar → the next session's 09:15 open) — the R2-OC-2 overnight-gap shape.
    # mask=0 fires every bar, so the corpus case exercises drop + re-arm + the
    # legitimate same-session fill on day-2.
    two_session = [
        _bar(15, 28, "100", "101", "99", "100"),
        _bar(15, 29, "100", "101", "99", "100"),  # day-1's last stored bar
        _bar(9, 15, "119", "120", "118", "119", day=_NEXT_DAY),  # +19 gap
        _bar(9, 16, "119.50", "120.50", "119", "119.50", day=_NEXT_DAY),
    ]
    default = FillPolicy()
    return [
        ("cross_session_boundary", two_session, 0, Side.BUY, None, None, default),
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

    def test_cross_session_last_bar_signal_yields_no_trade_both_sides(self) -> None:
        # R2-OC-2 ABSOLUTE pin: a signal on day-1's LAST stored bar (15:29 IST)
        # followed by day-2's 09:15 open produces ZERO trades on BOTH the Rust
        # kernel AND the Python oracle — a lock-step drift that re-grew the
        # overnight-entry leak in both at once (pre-fix BOTH produced one trade
        # entering at 119.00, the +19 overnight gap at zero slippage) fails here.
        bars = [
            _bar(15, 28, "100", "101", "99", "100"),
            _bar(15, 29, "100", "101", "99", "100"),  # signal (bit0) fires HERE only
            _bar(9, 15, "119", "120", "118", "119", day=_NEXT_DAY),
            _bar(9, 16, "119.50", "120.50", "119", "119.50", day=_NEXT_DAY),
        ]
        signals = [False, True, False, False]
        raw = build_bar_bits(bars)
        bits = [(b | 1) if sig else (b & ~1) for b, sig in zip(raw, signals, strict=True)]
        kernel = ResidentTradeSim(bars, bits, tf_minutes=1).simulate_masks([1], direction=Side.BUY)
        oracle = simulate_trades(
            bars, mask_to_entry_signals(bits, 1), direction=Side.BUY, tf_minutes=1
        )
        assert kernel == [[]]  # Rust kernel: NO overnight trade
        assert oracle == []  # Python oracle: NO overnight trade
        assert kernel == [oracle]  # byte-identical verdicts

    def test_cross_session_day1_trade_is_untouched_exact_values(self) -> None:
        # The anti-shared-drift twin for the SAME two-session shape: a mid-day
        # day-1 signal still yields exactly its day-1 trade (hand-computed) on
        # both sides — the drop rule removes ONLY the overnight fill.
        bars = [
            _bar(11, 0, "100", "101", "99", "100"),  # signal (bit0), well before 15:20
            _bar(11, 1, "100.50", "102", "100", "101"),  # entry @ open 100.50
            _bar(11, 2, "101", "103", "100.25", "102"),  # in-session ride
            _bar(9, 15, "119", "120", "118", "119", day=_NEXT_DAY),
            _bar(9, 16, "119.50", "120.50", "119", "119.50", day=_NEXT_DAY),
        ]
        signals = [True, False, False, False, False]
        raw = build_bar_bits(bars)
        bits = [(b | 1) if sig else (b & ~1) for b, sig in zip(raw, signals, strict=True)]
        kernel = ResidentTradeSim(bars, bits, tf_minutes=1).simulate_masks([1], direction=Side.BUY)
        oracle = simulate_trades(
            bars, mask_to_entry_signals(bits, 1), direction=Side.BUY, tf_minutes=1
        )
        # Entry open[1]=100.50=10050p. bar1 (102,100): fav=150, adv=50. bar2
        # (103,100.25): fav=250, adv=25. Position is OPEN at the session step —
        # the walker holds through the gap (exit-side semantics unchanged) —
        # bar3 (120,118): fav=1950, adv=0; bar4 (120.50,119): fav=2000. End of
        # data → close at close[4]=119.50=11950p.
        expected = ClosedTrade(
            pnl_paisa=1900,
            mfe_paisa=2000,
            mae_paisa=50,
            holding_bars=4,
            entry_ts=bars[1].timestamp,
            exit_ts=bars[4].timestamp,
            direction=Side.BUY,
        )
        assert oracle == [expected]
        assert kernel == [[expected]]


# ── wide-mask golden (DEC-TRADE-SIM-U128-KERNEL-001) ──────────────────────────
#
# The u64 corpus above is byte-UNTOUCHED (the frozen pre-u128 pin). This class
# adds the >64-bit twin: parity across exit kinds through the u128 kernel plus
# one hand-computed ABSOLUTE scenario (the anti-shared-drift teeth for the wide
# path — a lock-step kernel+oracle drift cannot hide behind pure parity).


class TestWideMaskGolden:
    """>64-bit masks: Rust-served, byte-identical to the oracle, absolute-pinned."""

    _WIDE_MASK = (1 << 64) | (1 << 100)

    def _wide_feed_and_bits(self) -> tuple[list[CandleBar], list[int]]:
        """The `long_end_of_data` morning shape with the signal moved to wide bits.

        Bits 64+100 are set ONLY on bar0 (Stage-1A emits ≤60-bit words, so no
        other bar can carry them) — the wide mask fires exactly where mask=0
        fired in the frozen absolute scenario above, entry at open[1].
        """
        bars = [
            _bar(9, 15, "100", "101", "99", "100"),
            _bar(9, 16, "100", "110", "92", "100"),
            _bar(9, 17, "100", "108", "95", "104"),
        ]
        bits = build_bar_bits(bars)
        bits[0] |= self._WIDE_MASK
        return bars, bits

    def test_wide_mask_long_end_of_data_exact_trade(self) -> None:
        # HAND-COMPUTED (paisa): entry at open[1] = 100.00 = 10000p; bar1
        # (110, 92) folds fav = 1000 / adv = 800; bar2 (108, 95) folds 800/500
        # (no new max); end of data → close[2] = 104.00 = 10400p → pnl 400,
        # holding 2. Identical numbers to the frozen u64 `long_end_of_data`
        # absolute pin — proving the WIDE path reproduces the exact walk.
        bars, bits = self._wide_feed_and_bits()
        sim = ResidentTradeSim(bars, bits, tf_minutes=1)
        assert sim.rust_active is True
        trades = sim.simulate_masks([self._WIDE_MASK], direction=Side.BUY)[0]
        assert trades == [
            ClosedTrade(
                pnl_paisa=400,
                mfe_paisa=1000,
                mae_paisa=800,
                holding_bars=2,
                entry_ts=bars[1].timestamp,
                exit_ts=bars[2].timestamp,
                direction=Side.BUY,
            )
        ]

    @pytest.mark.parametrize(
        ("label", "sl", "tp", "policy"),
        [
            ("wide_end_of_data", None, None, FillPolicy()),
            ("wide_sl_at_level", 800, None, FillPolicy()),
            ("wide_tp_at_level", None, 600, FillPolicy()),
            ("wide_sl_next_open", 800, None, FillPolicy(stop_loss=LevelFill.NEXT_OPEN)),
            ("wide_same_bar_tp_first", 800, 600, FillPolicy(same_bar=SameBarTie.TP_FIRST)),
        ],
        ids=lambda v: v if isinstance(v, str) else "",
    )
    def test_wide_mask_kernel_byte_identical_to_oracle(
        self, label: str, sl: int | None, tp: int | None, policy: FillPolicy
    ) -> None:
        bars, bits = self._wide_feed_and_bits()
        for direction in (Side.BUY, Side.SELL):
            kernel = ResidentTradeSim(bars, bits, tf_minutes=1).simulate_masks(
                [self._WIDE_MASK, 0],
                direction=direction,
                stop_loss_paisa=sl,
                take_profit_paisa=tp,
                policy=policy,
            )
            oracle = [
                simulate_trades(
                    bars,
                    mask_to_entry_signals(bits, m),
                    direction=direction,
                    tf_minutes=1,
                    stop_loss_paisa=sl,
                    take_profit_paisa=tp,
                    policy=policy,
                )
                for m in (self._WIDE_MASK, 0)
            ]
            assert kernel == oracle
            assert len(kernel[0]) >= 1  # the wide mask genuinely traded
