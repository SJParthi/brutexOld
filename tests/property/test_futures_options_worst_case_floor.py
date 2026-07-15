"""DEC-FILL-WORST-CASE-ONLY-001 — the UNCONDITIONAL per-trade worst-case floor.

The futures/options executors re-price FIXED trade lists (entry/exit instants
inherited verbatim from the spot winner), so — unlike the spot engine, whose
SL/TP levels re-anchor on the worst entry and can move trigger TIMING — the
floor here is UNCONDITIONAL per trade. Worst-case is the ONLY fill model (the
PRECISE arm is DELETED), so the reference is the retired OPEN ± one-tick
convention restated IN-TEST as pure arithmetic over the drawn bars (never an
executor run): for the SAME signal over the SAME bars,

* the worst-case BUY fill is ``>=`` open + tick (paying up to the bar HIGH
  plus the tick — the F&O ±1-tick discipline rides ON TOP of the adverse
  extreme),
* the worst-case SELL fill is ``<=`` open − tick (selling down to the bar LOW
  minus the tick),
* the per-unit worst-case spread ``sell − buy`` is ``<=`` the open-anchored
  spread — the [FILL-WORSTCASE-SIDE-SIGN] monotonicity pin: worst-case can
  only UNDERSTATE the open-anchored price, never overstate,

with EXACT fill equality when the fill bars are flat on their adverse sides
(extreme == open ⇒ fill == open ± tick — proving the tick discipline is
retained, not replaced, by the extreme law). Bars are drawn on the 2dp tick
grid (paisa ints ÷ 100 → exact Decimals, the lake-write snap law).

The refuter's fix-round-2 STT-boundary example (entry flat at ₹460.05, exit
low one paisa under the open — the 25-paisa notional gap crossing a ₹0.5 STT
nearest-rounding boundary) is kept as a permanent in-domain ``@example``: the
adverse-levy-rounding law (``_round_levy_*`` ceilings, now the ONLY rounding)
is exercised on exactly that boundary every run.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from brutex.data.models import CandleBar, OptionBar
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
from hypothesis import example, given, settings
from hypothesis import strategies as st

_IST = ZoneInfo("Asia/Kolkata")
_F_ENTRY = datetime(2025, 1, 2, 9, 16, tzinfo=_IST)
_F_EXIT = datetime(2025, 1, 2, 15, 20, tzinfo=_IST)
_O_ENTRY = datetime(2024, 10, 3, 9, 16, tzinfo=_IST)
_O_EXIT = datetime(2024, 10, 3, 15, 20, tzinfo=_IST)
_O_EXPIRY = date(2024, 10, 31)

#: The F&O one-tick adverse discipline in paisa (₹0.05 — TICK_PAISA).
_TICK = 5

#: One priced bar on the 2dp grid: (open_paisa, up_band_paisa, down_band_paisa).
#: Opens well above the option tick-floor gate (> 25 paisa) so every drawn
#: trade ACCEPTS; bands bounded below the open so lows stay positive.
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
        assert isinstance(worst, MaterializedFuturesTrade)
        # The retired open-anchored reference, restated as pure arithmetic:
        # LONG buys the entry bar / sells the exit bar; SHORT the reverse.
        buy_open, sell_open = (entry[0], exit_[0]) if bull else (exit_[0], entry[0])
        ref_buy = buy_open + _TICK
        ref_sell = sell_open - _TICK
        # Fill dominance — adverse on BOTH legs, never better than open±tick.
        assert worst.buy_fill_paisa >= ref_buy
        assert worst.sell_fill_paisa <= ref_sell
        # The per-trade P&L floor (cost-free index: net == gross; identical
        # lot sizing on both sides of the comparison, so per-unit suffices).
        n_units = worst.lot_size * worst.n_lots
        assert worst.net_pnl_paisa <= (ref_sell - ref_buy) * n_units
        # Flat adverse sides ⇒ extreme == open ⇒ EXACT open±tick fills (the
        # +tick discipline rides ON TOP of the extreme, never replaces it).
        entry_up, entry_down = entry[1], entry[2]
        exit_up, exit_down = exit_[1], exit_[2]
        adverse_flat = (
            (entry_up == 0 and exit_down == 0) if bull else (entry_down == 0 and exit_up == 0)
        )
        if adverse_flat:
            assert worst.buy_fill_paisa == ref_buy
            assert worst.sell_fill_paisa == ref_sell
            assert worst.net_pnl_paisa == (ref_sell - ref_buy) * n_units


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
    # The refuter's fix-round-2 STT-boundary shape, kept as a permanent
    # in-domain example: entry flat at ₹460.05, exit low 1 paisa under the
    # open — the sell notional crosses a ₹0.5 STT rounding boundary; the
    # adverse (ceiling) levy rounding is exercised on exactly that edge.
    @example(entry=(46005, 0, 0), exit_=(46005, 0, 1), bull=True)
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
        assert isinstance(worst, MaterializedTrade)
        # DEC-EXEC-008 BUY-only: the long leg buys the entry bar, sells the
        # exit bar in BOTH directions (CE for BULL, PE for BEAR) — so the
        # open-anchored reference is entry-open + tick / exit-open − tick.
        ref_buy = entry[0] + _TICK
        ref_sell = exit_[0] - _TICK
        assert worst.breakdown.buy_fill_paisa >= ref_buy
        assert worst.breakdown.sell_fill_paisa <= ref_sell
        # The gross floor at the trade's own share count: worst-case can only
        # UNDERSTATE the open-anchored gross (charges only ever ADD to the
        # loss — every levy rounds adversely — so the net floor follows).
        n_shares = worst.breakdown.n_shares
        ref_gross = (ref_sell - ref_buy) * n_shares
        assert worst.breakdown.gross_pnl_paisa <= ref_gross
        assert worst.net_pnl_paisa <= ref_gross
        # Flat adverse sides ⇒ EXACT open±tick fills (the +tick discipline).
        if entry[1] == 0 and exit_[2] == 0:
            assert worst.breakdown.buy_fill_paisa == ref_buy
            assert worst.breakdown.sell_fill_paisa == ref_sell
            assert worst.breakdown.gross_pnl_paisa == ref_gross
            # Charges are strictly positive, so net sits strictly below the
            # gross reference on the flat-bar arm too.
            assert worst.net_pnl_paisa < ref_gross
