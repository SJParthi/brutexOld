"""DEC-FILL-WORST-CASE-001 — the UNCONDITIONAL per-trade worst-case floor.

The futures/options executors re-price FIXED trade lists (entry/exit
instants inherited verbatim from the spot winner), so — unlike the spot
engine, whose SL/TP levels re-anchor on the worst entry and can move
trigger TIMING (the matched-trade-scoped floor of
``tests/property/test_trade_sim_worst_case_floor.py``) — the floor here is
UNCONDITIONAL per trade: for the SAME signal over the SAME bars,

* the worst-case BUY fill is ``>=`` the precise buy fill,
* the worst-case SELL fill is ``<=`` the precise sell fill,
* the worst-case net P&L is ``<=`` the precise net P&L,

with EQUALITY exactly when both fill bars are flat on their adverse sides
(the +tick discipline — the flat-bar counterexample). Bars are drawn on the
2dp tick grid (paisa ints ÷ 100 → exact Decimals, the lake-write snap law).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from brutex.data.models import CandleBar, OptionBar
from brutex.execution.fill_mode import FillMode
from brutex.futures.executor import (
    FuturesBarWindow,
    FuturesSignal,
    MaterializedFuturesTrade,
    materialize_futures_trade,
)
from brutex.options.executor import (
    MaterializedTrade,
    OptionBarPair,
    SpotSignal,
    materialize_trade,
)
from brutex.options.lot_size_history import OptionsUnderlying
from brutex.options.strike_rules import RULES_BY_NAME, TradeDirection
from hypothesis import given, settings
from hypothesis import strategies as st

_IST = ZoneInfo("Asia/Kolkata")
_F_ENTRY = datetime(2025, 1, 2, 9, 16, tzinfo=_IST)
_F_EXIT = datetime(2025, 1, 2, 15, 20, tzinfo=_IST)
_O_ENTRY = datetime(2024, 10, 3, 9, 16, tzinfo=_IST)
_O_EXIT = datetime(2024, 10, 3, 15, 20, tzinfo=_IST)
_O_EXPIRY = date(2024, 10, 31)

#: One priced bar on the 2dp grid: (open_paisa, up_band_paisa, down_band_paisa).
#: Opens well above the option tick-floor gate (> 25 paisa) so every drawn
#: trade ACCEPTS in both modes; bands bounded below the open so lows stay
#: positive.
_BAR = st.tuples(
    st.integers(min_value=1_000, max_value=3_000_000),
    st.integers(min_value=0, max_value=500),
    st.integers(min_value=0, max_value=500),
)


def _rupees(paisa: int) -> Decimal:
    return Decimal(paisa) / Decimal(100)


def _candle(ts: datetime, bar: tuple[int, int, int]) -> CandleBar:
    open_p, up, down = bar
    return CandleBar(
        timestamp=ts.astimezone(UTC),
        open=_rupees(open_p),
        high=_rupees(open_p + up),
        low=_rupees(open_p - down),
        close=_rupees(open_p),
        volume=100,
        open_interest=1_000,
    )


@dataclass
class _FutResolver:
    def resolve(self, *, instrument: str, on_date: date) -> str | None:
        return "NSE-NIFTY-30Jan25-FUT"


@dataclass
class _FutSource:
    window: FuturesBarWindow

    def load_window(
        self, *, contract_symbol: str, entry_ts: datetime, exit_ts: datetime
    ) -> FuturesBarWindow | None:
        return self.window


class TestFuturesWorstCaseFloor:
    @settings(max_examples=150, deadline=None)
    @given(entry=_BAR, exit_=_BAR, bull=st.booleans())
    def test_per_trade_floor_unconditional(
        self, entry: tuple[int, int, int], exit_: tuple[int, int, int], bull: bool
    ) -> None:
        entry_bar = _candle(_F_ENTRY, entry)
        exit_bar = _candle(_F_EXIT, exit_)
        window = FuturesBarWindow(
            entry_bar=entry_bar, exit_bar=exit_bar, path=(entry_bar, exit_bar)
        )
        signal = FuturesSignal(
            instrument="NIFTY",
            direction=TradeDirection.BULL if bull else TradeDirection.BEAR,
            entry_ts=_F_ENTRY,
            exit_ts=_F_EXIT,
        )
        worst = materialize_futures_trade(
            signal, resolver=_FutResolver(), source=_FutSource(window)
        )
        precise = materialize_futures_trade(
            signal,
            resolver=_FutResolver(),
            source=_FutSource(window),
            fill_mode=FillMode.PRECISE,
        )
        assert isinstance(worst, MaterializedFuturesTrade)
        assert isinstance(precise, MaterializedFuturesTrade)
        # Fill dominance — adverse on BOTH legs, never better.
        assert worst.buy_fill_paisa >= precise.buy_fill_paisa
        assert worst.sell_fill_paisa <= precise.sell_fill_paisa
        # The per-trade P&L floor (cost-free index: net == gross).
        assert worst.net_pnl_paisa <= precise.net_pnl_paisa
        # Flat adverse sides ⇒ exact equality (the +tick discipline).
        entry_up, entry_down = entry[1], entry[2]
        exit_up, exit_down = exit_[1], exit_[2]
        adverse_flat = (
            (entry_up == 0 and exit_down == 0) if bull else (entry_down == 0 and exit_up == 0)
        )
        if adverse_flat:
            assert worst.net_pnl_paisa == precise.net_pnl_paisa


class _OptSource:
    def __init__(self, entry: tuple[int, int, int], exit_: tuple[int, int, int]) -> None:
        self._entry = entry
        self._exit = exit_

    def load_pair(self, *, option_type: str, **_kw: object) -> OptionBarPair:
        def bar(spec: tuple[int, int, int]) -> OptionBar:
            open_p, up, down = spec
            return OptionBar(
                timestamp=_O_ENTRY,
                open=_rupees(open_p),
                high=_rupees(open_p + up),
                low=_rupees(open_p - down),
                close=_rupees(open_p),
                volume=10,
                open_interest=100,
                side=option_type,
                strike=Decimal("25000"),
                spot=Decimal("25012"),
                iv=Decimal("0.15"),
            )

        return OptionBarPair(entry_bar=bar(self._entry), exit_bar=bar(self._exit))


class TestOptionsWorstCaseFloor:
    @settings(max_examples=150, deadline=None)
    @given(entry=_BAR, exit_=_BAR, bull=st.booleans())
    def test_per_trade_floor_unconditional(
        self, entry: tuple[int, int, int], exit_: tuple[int, int, int], bull: bool
    ) -> None:
        signal = SpotSignal(
            instrument=OptionsUnderlying.NIFTY,
            direction=TradeDirection.BULL if bull else TradeDirection.BEAR,
            entry_ts=_O_ENTRY,
            exit_ts=_O_EXIT,
            spot_at_entry=Decimal("25012"),
            expiry_date=_O_EXPIRY,
        )
        source = _OptSource(entry, exit_)
        worst = materialize_trade(signal, RULES_BY_NAME["atm"], source=source)
        precise = materialize_trade(
            signal, RULES_BY_NAME["atm"], source=source, fill_mode=FillMode.PRECISE
        )
        assert isinstance(worst, MaterializedTrade)
        assert isinstance(precise, MaterializedTrade)
        # DEC-EXEC-008 BUY-only: the long leg buys the entry bar, sells the
        # exit bar in BOTH directions (CE for BULL, PE for BEAR).
        assert worst.breakdown.buy_fill_paisa >= precise.breakdown.buy_fill_paisa
        assert worst.breakdown.sell_fill_paisa <= precise.breakdown.sell_fill_paisa
        assert worst.net_pnl_paisa <= precise.net_pnl_paisa
        # Flat adverse sides ⇒ exact equality (long: entry HIGH / exit LOW).
        if entry[1] == 0 and exit_[2] == 0:
            assert worst.net_pnl_paisa == precise.net_pnl_paisa
