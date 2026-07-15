"""Golden byte-equality pins for the futures translation leg (DEC-FUTURES-EXEC-TRANSLATION-001).

Re-locked for DEC-FILL-WORST-CASE-ONLY-001: worst-case is the ONLY fill law —
the retired PRECISE open-anchored goldens (and their ``FillMode`` pins) are
DELETED with the mode axis; the worst-case constants below were hand-derived
and locked when the law first landed (DEC-FILL-WORST-CASE-ONLY-001) and are
UNCHANGED here — the one-law promotion moved the pins, never the numbers.

The LONG fill arm is pinned byte-equal to the options calculator's
``_apply_slippage("long", …)`` (CLAUDE.md §11 COMMON — one adverse-fill law,
never a drifting copy); the SHORT arm is pinned to its own hand-derived
adverse-both-sides table (the resolved ``# CHECK SIGN`` law); and one full
accept-path trade is pinned to hand-recomputed paisa, field for field.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import ClassVar
from zoneinfo import ZoneInfo

from brutex.costs.calculator import _apply_slippage
from brutex.costs.futures_costs import apply_futures_fill
from brutex.data.models import CandleBar
from brutex.futures.executor import (
    FuturesBarWindow,
    FuturesSignal,
    MaterializedFuturesTrade,
    materialize_futures_trade,
)
from brutex.options.strike_rules import TradeDirection

_IST = ZoneInfo("Asia/Kolkata")
_ENTRY = datetime(2025, 1, 2, 9, 16, tzinfo=_IST)
_EXIT = datetime(2025, 1, 2, 15, 20, tzinfo=_IST)
_SYMBOL = "NSE-NIFTY-30Jan25-FUT"

#: (entry_high, entry_low, exit_high, exit_low) extremes grid covering
#: normal, boundary and one-tick-floor cases — the worst-case anchors are
#: the ONLY fill inputs (the retired opens are gone from the fill law).
_FILL_GRID = (
    (5, 5, 5, 5),  # flat 1-tick bars: floor + flat law
    (110, 90, 110, 90),
    (2_201_000, 2_199_000, 2_211_000, 2_209_000),
    (123_455, 123_400, 987_700, 987_655),
)


class TestFillGolden:
    def test_long_arm_byte_equal_to_calculator_slippage(self) -> None:
        for eh, el, xh, xl in _FILL_GRID:
            assert apply_futures_fill(
                side="long",
                entry_high_paisa=eh,
                entry_low_paisa=el,
                exit_high_paisa=xh,
                exit_low_paisa=xl,
            ) == _apply_slippage(
                "long",
                entry_high_paisa=eh,
                entry_low_paisa=el,
                exit_high_paisa=xh,
                exit_low_paisa=xl,
            ), (eh, el, xh, xl)

    #: WORST-CASE fill grid golden (hand-derived; locked with
    #: DEC-FILL-WORST-CASE-ONLY-001, promoted to the ONLY law here):
    #: LONG — buy = entry HIGH + 5 (never floors: high ≥ 5 ⇒ buy ≥ 10);
    #: sell = max(exit LOW − 5, 5); slip = |buy − high| + |low − sell|.
    _LONG_GRID: ClassVar[dict[tuple[int, int, int, int], tuple[int, int, int]]] = {
        (5, 5, 5, 5): (10, 5, 5),  # sell floored at one tick → slip 5
        (110, 90, 110, 90): (115, 85, 10),
        (2_201_000, 2_199_000, 2_211_000, 2_209_000): (2_201_005, 2_208_995, 10),
        (123_455, 123_400, 987_700, 987_655): (123_460, 987_650, 10),
    }

    def test_long_arm_worst_case_grid_golden(self) -> None:
        for (eh, el, xh, xl), expected in self._LONG_GRID.items():
            assert (
                apply_futures_fill(
                    side="long",
                    entry_high_paisa=eh,
                    entry_low_paisa=el,
                    exit_high_paisa=xh,
                    exit_low_paisa=xl,
                )
                == expected
            ), (eh, el, xh, xl)

    def test_short_arm_golden_adverse_both_sides(self) -> None:
        # Hand-derived: opening sell anchors the entry LOW → sell =
        # max(entry_low − 5, 5); the cover anchors the exit HIGH → buy =
        # exit_high + 5; slip = |buy − exit_high| + |entry_low − sell|.
        golden = {
            (5, 5, 5, 5): (10, 5, 5),  # sell floored → slip 5
            (110, 90, 110, 90): (115, 85, 10),
            (2_201_000, 2_199_000, 2_211_000, 2_209_000): (2_211_005, 2_198_995, 10),
            (123_455, 123_400, 987_700, 987_655): (987_705, 123_395, 10),
        }
        for (eh, el, xh, xl), expected in golden.items():
            assert (
                apply_futures_fill(
                    side="short",
                    entry_high_paisa=eh,
                    entry_low_paisa=el,
                    exit_high_paisa=xh,
                    exit_low_paisa=xl,
                )
                == expected
            ), (eh, el, xh, xl)


@dataclass
class _Resolver:
    def resolve(self, *, instrument: str, on_date: date) -> str | None:
        return _SYMBOL


@dataclass
class _Source:
    window: FuturesBarWindow

    def load_window(
        self, *, contract_symbol: str, entry_ts: datetime, exit_ts: datetime
    ) -> FuturesBarWindow | None:
        return self.window


def _golden_window() -> FuturesBarWindow:
    def bar(ts: datetime, open_: str, high: str, low: str) -> CandleBar:
        return CandleBar(
            timestamp=ts.astimezone(UTC),
            open=Decimal(open_),
            high=Decimal(high),
            low=Decimal(low),
            close=Decimal(open_),
            volume=100,
            open_interest=25_000,
        )

    entry = bar(_ENTRY, "22000.00", "22010.00", "21990.00")
    mid = bar(_ENTRY + timedelta(minutes=1), "22050.00", "22080.00", "22040.00")
    exit_ = bar(_EXIT, "22100.00", "22110.00", "22095.00")
    return FuturesBarWindow(entry_bar=entry, exit_bar=exit_, path=(entry, mid, exit_))


class TestAcceptPathGolden:
    def test_full_trade_golden_field_for_field(self) -> None:
        # Hand-recomputed WORST-CASE accept path (NIFTY lot 75 on 2025-01-02,
        # cost-free index) — the DEC-FILL-WORST-CASE-ONLY-001 lock, now the ONE law:
        #   entry bar 22,000.00 [21,990.00 – 22,010.00]
        #   exit bar 22,100.00 [22,095.00 – 22,110.00]
        #   buy = entry HIGH 2,201,000 + 5 = 2,201,005
        #   sell = exit LOW 2,209,500 − 5 = 2,209,495
        #   gross = (2,209,495 − 2,201,005) × 75 = 8,490 × 75 = 636,750
        #   net == gross (zero charges, cost-free index)
        #   MAE/MFE vs the WORST entry fill 2,201,005 over the 3-bar path
        #   (path max high 2,211,000; min low 2,199,000):
        #   mfe = (2,211,000 − 2,201,005) × 75 = 9,995 × 75 = 749,625
        #   mae = (2,201,005 − 2,199,000) × 75 = 2,005 × 75 = 150,375
        out = materialize_futures_trade(
            FuturesSignal(
                instrument="NIFTY",
                direction=TradeDirection.BULL,
                entry_ts=_ENTRY,
                exit_ts=_EXIT,
                vix_at_entry=Decimal("13.42"),
                exit_reason="forced_1520",
            ),
            resolver=_Resolver(),
            source=_Source(window=_golden_window()),
        )
        assert isinstance(out, MaterializedFuturesTrade)
        golden = {
            "entry_open_paisa": 2_200_000,
            "exit_open_paisa": 2_210_000,
            "buy_fill_paisa": 2_201_005,
            "sell_fill_paisa": 2_209_495,
            "lot_size": 75,
            "n_lots": 1,
            "gross_pnl_paisa": 636_750,
            "total_charges_paisa": 0,
            "net_pnl_paisa": 636_750,
            "mae_paisa": 150_375,
            "mfe_paisa": 749_625,
            "vix_at_entry": Decimal("13.42"),
            "vix_at_exit": None,
            "cost_free": True,
            "exit_reason": "forced_1520",
        }
        for field_name, expected in golden.items():
            assert getattr(out, field_name) == expected, field_name
        assert out.leg.contract_symbol == _SYMBOL
        assert out.leg.expiry_date == date(2025, 1, 30)

    def test_rerun_is_bit_identical(self) -> None:
        # Golden Rule #4 / R16: same inputs → the same trade, bit for bit.
        signal = FuturesSignal(
            instrument="NIFTY", direction=TradeDirection.BULL, entry_ts=_ENTRY, exit_ts=_EXIT
        )
        source = _Source(window=_golden_window())
        first = materialize_futures_trade(signal, resolver=_Resolver(), source=source)
        second = materialize_futures_trade(signal, resolver=_Resolver(), source=source)
        assert first == second
