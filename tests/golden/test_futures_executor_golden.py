"""Golden byte-equality pins for the futures translation leg (DEC-FUTURES-EXEC-TRANSLATION-001).

The LONG fill arm is pinned byte-equal to the options calculator's
``_apply_slippage(..., "long")`` (CLAUDE.md §11 COMMON — one adverse-fill law,
never a drifting copy); the SHORT arm is pinned to its own hand-derived
adverse-both-sides table (deliberately NOT the calculator's ``# CHECK SIGN``
short branch); and one full accept-path trade is pinned to hand-recomputed
paisa, field for field.

DEC-FILL-WORST-CASE-001: the pre-worst-case goldens below are pinned with an
EXPLICIT ``fill_mode=FillMode.PRECISE`` — the byte-identical legacy opt-out
under the new WORST_CASE executor default. NEVER REGENERATE these numbers;
the companion ``TestWorstCaseGolden`` locks the worst-case law's own
hand-recomputed constants (locked once, audit comments in-line).
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
from brutex.execution.fill_mode import FillMode
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

#: (entry_open, exit_open) grid covering normal, boundary and floor cases.
_FILL_GRID = (
    (5, 5),
    (5, 10),
    (10, 5),
    (100, 100),
    (2_200_000, 2_210_000),
    (2_210_000, 2_200_000),
    (123_455, 987_655),
)


class TestFillGolden:
    def test_long_arm_byte_equal_to_calculator_slippage(self) -> None:
        for entry, exit_ in _FILL_GRID:
            assert apply_futures_fill(entry, exit_, side="long") == _apply_slippage(
                entry, exit_, "long"
            ), (entry, exit_)

    def test_short_arm_golden_adverse_both_sides(self) -> None:
        # Hand-derived: sell = max(entry − 5, 5); buy = exit + 5 (never floors
        # below tick since exit ≥ 5 ⇒ buy ≥ 10); realized = |buy−exit| + |entry−sell|.
        golden = {
            (5, 5): (10, 5, 5),
            (5, 10): (15, 5, 5),
            (10, 5): (10, 5, 10),
            (100, 100): (105, 95, 10),
            (2_200_000, 2_210_000): (2_210_005, 2_199_995, 10),
            (2_210_000, 2_200_000): (2_200_005, 2_209_995, 10),
            (123_455, 987_655): (987_660, 123_450, 10),
        }
        for (entry, exit_), expected in golden.items():
            assert apply_futures_fill(entry, exit_, side="short") == expected, (entry, exit_)


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
        # Hand-recomputed (NIFTY lot 75 on 2025-01-02, cost-free index):
        #   entry_open 2,200,000 · exit_open 2,210,000
        #   buy 2,200,005 · sell 2,209,995 · gross (9,990 × 75) = 749,250
        #   net == gross (zero charges) · mfe (10,995 × 75) = 824,625
        #   mae (1,005 × 75) = 75,375
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
            fill_mode=FillMode.PRECISE,  # legacy-mode pin — never regenerate
        )
        assert isinstance(out, MaterializedFuturesTrade)
        golden = {
            "entry_open_paisa": 2_200_000,
            "exit_open_paisa": 2_210_000,
            "buy_fill_paisa": 2_200_005,
            "sell_fill_paisa": 2_209_995,
            "lot_size": 75,
            "n_lots": 1,
            "gross_pnl_paisa": 749_250,
            "total_charges_paisa": 0,
            "net_pnl_paisa": 749_250,
            "mae_paisa": 75_375,
            "mfe_paisa": 824_625,
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
        # Golden Rule #4 / R16: same inputs → the same trade, bit for bit
        # (runs the executor's worst-case DEFAULT — determinism is
        # mode-independent).
        signal = FuturesSignal(
            instrument="NIFTY", direction=TradeDirection.BULL, entry_ts=_ENTRY, exit_ts=_EXIT
        )
        source = _Source(window=_golden_window())
        first = materialize_futures_trade(signal, resolver=_Resolver(), source=source)
        second = materialize_futures_trade(signal, resolver=_Resolver(), source=source)
        assert first == second


class TestWorstCaseGolden:
    """DEC-FILL-WORST-CASE-001 — worst-case goldens, locked once (audit below)."""

    #: WORST_CASE fill grid golden (locked 2026-07-13, hand-derived):
    #: (entry_open, entry_high, entry_low, exit_open, exit_high, exit_low) →
    #: (buy, sell, slip) for a LONG — buy = entry_high + 5 (no floor case
    #: reaches it since high ≥ open ≥ 5 ⇒ buy ≥ 10); sell = max(exit_low − 5,
    #: 5); slip = |buy − entry_high| + |exit_low − sell|.
    _LONG_GRID: ClassVar[dict[tuple[int, int, int, int, int, int], tuple[int, int, int]]] = {
        (5, 5, 5, 5, 5, 5): (10, 5, 5),  # flat 1-tick bars: floor + flat law
        (100, 110, 90, 100, 110, 90): (115, 85, 10),
        (2_200_000, 2_201_000, 2_199_000, 2_210_000, 2_211_000, 2_209_000): (
            2_201_005,
            2_208_995,
            10,
        ),
        (123_455, 123_455, 123_400, 987_655, 987_700, 987_655): (
            123_460,
            987_650,
            10,
        ),  # flat-at-open extremes on the adverse side ⇒ equals PRECISE
    }

    def test_worst_case_fill_grid_golden(self) -> None:
        for (eo, eh, el, xo, xh, xl), expected in self._LONG_GRID.items():
            assert (
                apply_futures_fill(
                    eo,
                    xo,
                    side="long",
                    fill_mode=FillMode.WORST_CASE,
                    entry_high_paisa=eh,
                    entry_low_paisa=el,
                    exit_high_paisa=xh,
                    exit_low_paisa=xl,
                )
                == expected
            ), (eo, xo)

    def test_worst_case_full_trade_golden_field_for_field(self) -> None:
        # Hand-recomputed WORST_CASE accept path over the SAME _golden_window
        # (locked once — the precise twin above must NEVER move):
        #   entry bar 22,000.00 [21,990.00 – 22,010.00]
        #   exit bar 22,100.00 [22,095.00 – 22,110.00]
        #   buy = entry HIGH 2,201,000 + 5 = 2,201,005
        #   sell = exit LOW 2,209,500 − 5 = 2,209,495
        #   gross = (2,209,495 − 2,201,005) × 75 = 8,490 × 75 = 636,750
        #   net == gross (cost-free index)
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
            ),
            resolver=_Resolver(),
            source=_Source(window=_golden_window()),
            fill_mode=FillMode.WORST_CASE,
        )
        assert isinstance(out, MaterializedFuturesTrade)
        golden = {
            "entry_open_paisa": 2_200_000,
            "exit_open_paisa": 2_210_000,
            "buy_fill_paisa": 2_201_005,
            "sell_fill_paisa": 2_209_495,
            "gross_pnl_paisa": 636_750,
            "total_charges_paisa": 0,
            "net_pnl_paisa": 636_750,
            "mae_paisa": 150_375,
            "mfe_paisa": 749_625,
            "cost_free": True,
        }
        for field_name, expected in golden.items():
            assert getattr(out, field_name) == expected, field_name

    def test_worst_case_default_matches_explicit(self) -> None:
        # The bare executor call IS the worst-case golden (production default).
        signal = FuturesSignal(
            instrument="NIFTY", direction=TradeDirection.BULL, entry_ts=_ENTRY, exit_ts=_EXIT
        )
        bare = materialize_futures_trade(
            signal, resolver=_Resolver(), source=_Source(window=_golden_window())
        )
        explicit = materialize_futures_trade(
            signal,
            resolver=_Resolver(),
            source=_Source(window=_golden_window()),
            fill_mode=FillMode.WORST_CASE,
        )
        assert bare == explicit
