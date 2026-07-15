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

with GROSS equality exactly when both fill bars are flat on their adverse
sides (the +tick discipline — the flat-bar counterexample; on the
COST-FREE index-futures leg ``net == gross``, so the flat-bar equality
holds at the NET level there too). Bars are drawn on the 2dp tick grid
(paisa ints ÷ 100 → exact Decimals, the lake-write snap law).

THE NET FLOOR NEEDS THE ADVERSE-LEVY-ROUNDING LAW (fix round 2,
MIGRATION.md #1798): as originally shipped, this file's net-floor clause
asserted a FALSE theorem — the refuter's executed counterexample
(``@example`` below: entry flat at ₹460.05, exit low ONE paisa under the
open, entry date 2024-10-03) had the worst-case sell notional 25 paisa
smaller crossing a ₹0.5 STT nearest-rupee rounding boundary DOWNWARD, so
STT dropped ₹1 while gross dropped only 25 paisa: worst net −7,095 BEAT
precise net −7,170 by 75 paisa. The fix: under WORST_CASE every
nearest-rounded levy in the cost stack rounds ADVERSELY (ceiling) —
``calculator._round_levy_rupee`` / ``_round_levy_paisa`` — while PRECISE
keeps the legacy nearest rounding byte-identical.

DOMINANCE PROOF (why the ceiling restores the unconditional net floor):
let ``Δb = worst_buy_notional − precise_buy_notional ≥ 0`` and
``Δs = precise_sell_notional − worst_sell_notional ≥ 0`` (fill dominance ×
n_shares), so ``precise_gross − worst_gross = Δ = Δb + Δs``. Per levy with
rounding unit ``u`` (₹1 statutory / 1 paisa commercial) and rate ``r``:

* a BUY-anchored levy's worst raw value is ``>=`` its precise raw value,
  and ``ceil(x) >= nearest(y)`` whenever ``x >= y`` — so the worst levy is
  never SMALLER than the precise one (no shortfall, only extra charge);
* a SELL-anchored levy's raw gap is ``d = r·Δs`` (plus at most 1 paisa of
  statutory floor-division truncation): when ``d < u/2``,
  ``nearest(raw_worst + d) <= ceil(raw_worst)`` — shortfall ``<= 0``; when
  ``d >= u/2`` the shortfall is ``<= d + u/2 <= 2d = 2·r·Δs``;
* GST compounds the other levies' gaps through its base at 18%.

Summing: ``charges_precise − charges_worst <= 2·(Σ sell-side rates)·
(1 + 2×18%)·Δs + O(paisa truncation) << Δs <= Δ`` because the summed levy
rates (STT 0.1% + ETC ~0.035% + SEBI 0.0001% + IPFT 0.0005% + the futures
brokerage turnover arm 0.03%) are ``<< 36%``. Hence
``net_worst <= net_precise`` UNCONDITIONALLY. On flat adverse bars the
notionals are equal, so every worst levy is ``ceil >= nearest`` — charges
can only be HIGHER under worst — which is why the flat-bar clause pins
GROSS equality (net equality survives only on the cost-free futures leg).
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
from hypothesis import example, given, settings
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
    # The refuter's executed net-floor counterexample (fix round 2): entry
    # flat at ₹460.05, exit low 1 paisa under the open — the 25-paisa-smaller
    # worst sell notional crossed a ₹0.5 STT rounding boundary downward and
    # the PRE-fix worst net (−7,095) BEAT precise (−7,170). Pinned as a
    # permanent in-domain example; the adverse-levy-rounding law keeps it
    # below the floor now (worst −7,297 <= precise −7,170).
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
        # The cost stack itself can never round in the worst trade's favor
        # (the adverse-levy-rounding law, fix round 2): at equal-or-worse
        # notionals every worst levy is ceil'd, so a SMALLER worst charge
        # total can only come from the smaller sell notional — bounded by
        # the dominance proof (module docstring), never a boundary flip.
        if entry[1] == 0 and exit_[2] == 0:
            # Flat adverse sides ⇒ identical fills ⇒ GROSS equality; the
            # ceil'd levies may exceed the nearest-rounded precise ones, so
            # net equality is deliberately NOT asserted (charges_worst >=
            # charges_precise is — the fix-round-2 reframe).
            assert worst.breakdown.buy_fill_paisa == precise.breakdown.buy_fill_paisa
            assert worst.breakdown.sell_fill_paisa == precise.breakdown.sell_fill_paisa
            assert worst.breakdown.gross_pnl_paisa == precise.breakdown.gross_pnl_paisa
            assert worst.breakdown.total_charges_paisa >= precise.breakdown.total_charges_paisa
