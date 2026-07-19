"""Unit tests for the futures translation executor core (DEC-FUTURES-EXEC-TRANSLATION-001).

The accept path (golden paisa recomputed test-side), routing, signal lifting,
fills (R15 edge pins), MAE/MFE (R22), VIX passthrough (R31), aggregation, and
the audited sim entrypoint (R28/R41/R43/R45).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from brutex.costs.constants import TICK_PAISA
from brutex.data.models import CandleBar
from brutex.futures import executor as executor_module
from brutex.futures import reference_sim
from brutex.futures.executor import (
    _REJECT_FIELD_BY_REASON,
    ExecutionKind,
    FuturesBarWindow,
    FuturesLeg,
    FuturesRejectReason,
    FuturesRuleResult,
    FuturesSignal,
    MaterializedFuturesTrade,
    RejectedFuturesTrade,
    execute_futures_sim,
    futures_lot_size,
    materialize_futures_trade,
    route_execution,
    run_futures_over_signals,
    select_futures_leg,
    signals_from_spot_trades,
)
from brutex.metrics.types import ClosedTrade
from brutex.options.strike_rules import TradeDirection
from brutex.strategies.types import Side
from structlog.testing import capture_logs

_IST = ZoneInfo("Asia/Kolkata")
_ENTRY = datetime(2025, 1, 2, 9, 16, tzinfo=_IST)
_EXIT = datetime(2025, 1, 2, 15, 20, tzinfo=_IST)
_SYMBOL = "NSE-NIFTY-30Jan25-FUT"


def _bar(ts: datetime, open_: str, *, high: str, low: str, volume: int = 100) -> CandleBar:
    o = Decimal(open_)
    return CandleBar(
        timestamp=ts.astimezone(UTC),
        open=o,
        high=Decimal(high),
        low=Decimal(low),
        close=o,
        volume=volume,
        open_interest=1_000,
    )


def _window(
    *,
    entry_open: str = "22000.00",
    exit_open: str = "22100.00",
    entry_volume: int = 100,
    exit_volume: int = 100,
) -> FuturesBarWindow:
    # Default-open goldens use the hand-computed high/low band (the MAE/MFE
    # oracle); a custom open derives its band ±10.00/−5.00 around itself so
    # the bar stays OHLC-valid.
    entry_bar = _bar(_ENTRY, entry_open, high="22010.00", low="21990.00", volume=entry_volume)
    mid_bar = _bar(_ENTRY + timedelta(minutes=1), "22050.00", high="22080.00", low="22040.00")
    if exit_open == "22100.00":
        exit_bar = _bar(_EXIT, exit_open, high="22110.00", low="22095.00", volume=exit_volume)
    else:
        e = Decimal(exit_open)
        exit_bar = _bar(
            _EXIT,
            exit_open,
            high=str(e + Decimal("10.00")),
            low=str(e - Decimal("5.00")),
            volume=exit_volume,
        )
    return FuturesBarWindow(
        entry_bar=entry_bar, exit_bar=exit_bar, path=(entry_bar, mid_bar, exit_bar)
    )


@dataclass
class _Resolver:
    """A recording fake FrontMonthResolver."""

    symbol: str | None = _SYMBOL
    calls: list[tuple[str, date]] = field(default_factory=list)

    def resolve(self, *, instrument: str, on_date: date) -> str | None:
        self.calls.append((instrument, on_date))
        return self.symbol


@dataclass
class _Source:
    """A recording fake FuturesBarSource."""

    window: FuturesBarWindow | None = None
    calls: list[tuple[str, datetime, datetime]] = field(default_factory=list)

    def load_window(
        self, *, contract_symbol: str, entry_ts: datetime, exit_ts: datetime
    ) -> FuturesBarWindow | None:
        self.calls.append((contract_symbol, entry_ts, exit_ts))
        return self.window


class _Audit:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, dict[str, object]]] = []

    def append(self, *, action: str, subject: str, payload: dict[str, object]) -> object:
        self.rows.append((action, subject, payload))
        return None


def _signal(**overrides: Any) -> FuturesSignal:
    kwargs: dict[str, Any] = {
        "instrument": "NIFTY",
        "direction": TradeDirection.BULL,
        "entry_ts": _ENTRY,
        "exit_ts": _EXIT,
    }
    kwargs.update(overrides)
    return FuturesSignal(**kwargs)


class TestRouteExecution:
    def test_r11_index_routes_to_futures(self) -> None:
        for label in ("NIFTY", "BANKNIFTY", "SENSEX"):
            assert route_execution(label) is ExecutionKind.INDEX_FUTURES

    def test_r11_stock_winner_deferred_not_spot_executed(self) -> None:
        # [R11] a stock label routes to the DEFERRED spot arm — never futures.
        assert route_execution("RELIANCE") is ExecutionKind.STOCK_SPOT_DEFERRED
        assert route_execution("BAJAJ-AUTO") is ExecutionKind.STOCK_SPOT_DEFERRED

    def test_blank_and_vix_unsupported(self) -> None:
        assert route_execution("") is ExecutionKind.UNSUPPORTED
        assert route_execution("   ") is ExecutionKind.UNSUPPORTED
        assert route_execution("INDIAVIX") is ExecutionKind.UNSUPPORTED
        assert route_execution("IndiaVix") is ExecutionKind.UNSUPPORTED


class TestFuturesLotSize:
    def test_r24_lots_from_real_lot_size(self) -> None:
        # [R24] the date-keyed derivative-lot history: NIFTY 75 on 2025-01-02
        # (the 2024-11-20 SEBI revision), 25 on 2024-05-02, 50 on 2022-01-03.
        assert futures_lot_size("NIFTY", date(2025, 1, 2)) == 75
        assert futures_lot_size("NIFTY", date(2024, 5, 2)) == 25
        assert futures_lot_size("NIFTY", date(2022, 1, 3)) == 50

    def test_lot_size_unknown_pre_history(self) -> None:
        # [R24] the recorded history floor is 2021-01-01; 2020 refuses.
        with pytest.raises(ValueError, match="before recorded history"):
            futures_lot_size("NIFTY", date(2020, 6, 1))

    def test_lot_size_unknown_underlying(self) -> None:
        with pytest.raises(ValueError, match="no verifiable futures lot-size history"):
            futures_lot_size("RELIANCE", date(2025, 1, 2))


class TestSignalValidation:
    def test_valid(self) -> None:
        sig = _signal(vix_at_entry=Decimal("13.5"), exit_reason="tp")
        assert sig.exit_reason == "tp"

    def test_blank_instrument(self) -> None:
        with pytest.raises(ValueError, match="non-blank underlying"):
            _signal(instrument=" ")

    def test_naive_timestamps(self) -> None:
        with pytest.raises(ValueError, match="entry_ts must be timezone-aware"):
            _signal(entry_ts=datetime(2025, 1, 2, 9, 16))
        with pytest.raises(ValueError, match="exit_ts must be timezone-aware"):
            _signal(exit_ts=datetime(2025, 1, 2, 15, 20))

    def test_exit_before_entry(self) -> None:
        with pytest.raises(ValueError, match="must be >= entry_ts"):
            _signal(entry_ts=_EXIT, exit_ts=_ENTRY)

    def test_non_finite_vix(self) -> None:
        with pytest.raises(ValueError, match="vix_at_entry"):
            _signal(vix_at_entry=Decimal("NaN"))
        with pytest.raises(ValueError, match="vix_at_exit"):
            _signal(vix_at_exit=Decimal("Infinity"))

    def test_blank_exit_reason(self) -> None:
        with pytest.raises(ValueError, match="exit_reason"):
            _signal(exit_reason="  ")


class TestBarWindowValidation:
    def test_empty_path(self) -> None:
        w = _window()
        with pytest.raises(ValueError, match="non-empty"):
            FuturesBarWindow(entry_bar=w.entry_bar, exit_bar=w.exit_bar, path=())

    def test_endpoint_mismatch(self) -> None:
        w = _window()
        with pytest.raises(ValueError, match="path\\[0\\]"):
            FuturesBarWindow(entry_bar=w.path[1], exit_bar=w.exit_bar, path=w.path)
        with pytest.raises(ValueError, match="path\\[-1\\]"):
            FuturesBarWindow(entry_bar=w.entry_bar, exit_bar=w.path[1], path=w.path)

    def test_non_ascending_path(self) -> None:
        w = _window()
        with pytest.raises(ValueError, match="strictly ascending"):
            FuturesBarWindow(
                entry_bar=w.entry_bar,
                exit_bar=w.entry_bar,
                path=(w.entry_bar, w.path[1], w.entry_bar),
            )


class TestSelectFuturesLeg:
    def test_resolves_leg_with_verbatim_expiry_token(self) -> None:
        # [R14] the leg's expiry decodes VERBATIM from the DDMmmYY token —
        # holiday shifts arrive through the symbol, never recomputed.
        leg = select_futures_leg(_signal(), resolver=_Resolver())
        assert isinstance(leg, FuturesLeg)
        assert leg.contract_symbol == _SYMBOL
        assert leg.expiry_date == date(2025, 1, 30)
        assert leg.side == "long"

    def test_bear_maps_to_short(self) -> None:
        leg = select_futures_leg(_signal(direction=TradeDirection.BEAR), resolver=_Resolver())
        assert isinstance(leg, FuturesLeg)
        assert leg.side == "short"

    def test_r14_no_contract_when_calendar_empty(self) -> None:
        # [R14] resolver returns None → honest NO_CONTRACT, leg None.
        out = select_futures_leg(_signal(), resolver=_Resolver(symbol=None))
        assert isinstance(out, RejectedFuturesTrade)
        assert out.reason is FuturesRejectReason.NO_CONTRACT
        assert out.leg is None

    def test_unparseable_symbol_rejects_with_warn(self) -> None:
        with capture_logs() as logs:
            out = select_futures_leg(_signal(), resolver=_Resolver(symbol="garbage"))
        assert isinstance(out, RejectedFuturesTrade)
        assert out.reason is FuturesRejectReason.NO_CONTRACT
        assert [x["event"] for x in logs] == ["futures.reject.unparseable_contract"]

    def test_option_symbol_rejected_not_a_future(self) -> None:
        out = select_futures_leg(_signal(), resolver=_Resolver(symbol="NSE-NIFTY-30Jan25-22000-CE"))
        assert isinstance(out, RejectedFuturesTrade)
        assert out.reason is FuturesRejectReason.NO_CONTRACT

    def test_resolution_keyed_on_entry_ist_date(self) -> None:
        resolver = _Resolver()
        select_futures_leg(_signal(), resolver=resolver)
        assert resolver.calls == [("NIFTY", date(2025, 1, 2))]

    def test_blank_leg_symbol_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-blank"):
            FuturesLeg(
                instrument="NIFTY",
                direction=TradeDirection.BULL,
                side="long",
                contract_symbol=" ",
                expiry_date=date(2025, 1, 30),
            )


class TestMaterializeAcceptPath:
    def test_golden_long_accept(self) -> None:
        # Hand-recomputed WORST-CASE golden (DEC-FILL-WORST-CASE-ONLY-001 —
        # the ONE fill law): NIFTY long over the default _window — entry
        # HIGH 22,010.00, exit LOW 22,095.00, lot 75.
        #   buy = 2,201,000 + 5 = 2,201,005
        #   sell = 2,209,500 − 5 = 2,209,495
        #   gross = (2,209,495 − 2,201,005) × 75 = 8,490 × 75 = 636,750
        #   MAE/MFE vs the WORST entry fill 2,201,005 over the 3-bar path:
        #   mfe/unit = 2,211,000 − 2,201,005 = 9,995 → ×75 = 749,625
        #   mae/unit = 2,201,005 − 2,199,000 = 2,005 → ×75 = 150,375
        source = _Source(window=_window())
        out = materialize_futures_trade(_signal(), resolver=_Resolver(), source=source)
        assert isinstance(out, MaterializedFuturesTrade)
        assert (out.entry_open_paisa, out.exit_open_paisa) == (2_200_000, 2_210_000)
        assert (out.buy_fill_paisa, out.sell_fill_paisa) == (2_201_005, 2_209_495)
        assert out.lot_size == 75
        assert out.gross_pnl_paisa == 636_750
        assert out.total_charges_paisa == 0
        assert out.net_pnl_paisa == 636_750
        assert out.mfe_paisa == 749_625
        assert out.mae_paisa == 150_375
        assert out.cost_free is True
        assert out.exit_reason == "signal"
        assert out.entry_ts == _ENTRY
        assert out.exit_ts == _EXIT

    def test_golden_short_accept(self) -> None:
        # Worst-case short: opening sell = entry LOW − tick = 2,198,995;
        # cover buy = exit HIGH + tick = 2,211,005; gross = (sell − buy) ×
        # 75 = −12,010 × 75 = −900,750. Entry fill = the SELL fill 2,198,995
        # — BELOW the path's min low 2,199,000, so no bar ever shows a
        # favourable excursion (mfe = 0); mae/unit = 2,211,000 − 2,198,995 =
        # 12,005.
        out = materialize_futures_trade(
            _signal(direction=TradeDirection.BEAR),
            resolver=_Resolver(),
            source=_Source(window=_window()),
        )
        assert isinstance(out, MaterializedFuturesTrade)
        assert (out.buy_fill_paisa, out.sell_fill_paisa) == (2_211_005, 2_198_995)
        assert out.gross_pnl_paisa == -900_750
        assert out.mfe_paisa == 0
        assert out.mae_paisa == 12_005 * 75
        assert out.leg.side == "short"

    def test_gap_open_worst_case_fills_on_the_real_bar_extreme(self) -> None:
        # DEC-FILL-WORST-CASE-ONLY-001: a gap-open beyond the SL still uses
        # the REAL exit bar — its adverse extreme (LOW − tick for the long
        # sell), never the SL fantasy level and never a fabricated bar.
        # Exit bar opens 21,700.00, band low 21,695.00 (the _window
        # derivation) → sell = 2,169,500 − 5.
        window = _window(exit_open="21700.00")
        out = materialize_futures_trade(
            _signal(exit_reason="sl"), resolver=_Resolver(), source=_Source(window=window)
        )
        assert isinstance(out, MaterializedFuturesTrade)
        assert out.sell_fill_paisa == 2_169_500 - TICK_PAISA  # REAL bar LOW − tick
        assert out.sell_fill_paisa != 2_195_000  # NOT the SL fantasy level

    def test_r15_1520_forced_exit_fills_at_inherited_minute_open(self) -> None:
        # [R15/R18] the 15:20 forced exit fills at THAT minute's real open.
        source = _Source(window=_window())
        out = materialize_futures_trade(
            _signal(exit_reason="forced_1520"), resolver=_Resolver(), source=source
        )
        assert isinstance(out, MaterializedFuturesTrade)
        assert source.calls == [(_SYMBOL, _ENTRY, _EXIT)]
        assert out.exit_ts.astimezone(_IST).hour == 15
        assert out.exit_ts.astimezone(_IST).minute == 20
        assert out.exit_reason == "forced_1520"

    def test_r15_muhurat_session_exit_variant(self) -> None:
        # [R15/R18] an evening special-session trade (same IST date) prices
        # normally — the executor inherits the session-end exit_ts verbatim.
        entry = datetime(2024, 11, 1, 18, 20, tzinfo=_IST)
        exit_ = datetime(2024, 11, 1, 19, 10, tzinfo=_IST)
        entry_bar = _bar(entry, "22000.00", high="22010.00", low="21990.00")
        exit_bar = _bar(exit_, "22020.00", high="22030.00", low="22010.00")
        window = FuturesBarWindow(
            entry_bar=entry_bar, exit_bar=exit_bar, path=(entry_bar, exit_bar)
        )
        out = materialize_futures_trade(
            _signal(
                entry_ts=entry,
                exit_ts=exit_,
                exit_reason="forced_session_end",
            ),
            resolver=_Resolver(symbol="NSE-NIFTY-28Nov24-FUT"),
            source=_Source(window=window),
        )
        assert isinstance(out, MaterializedFuturesTrade)
        assert out.exit_reason == "forced_session_end"

    def test_r15_zero_range_bar_degenerates(self) -> None:
        # [R15] O==H==L==C entry and exit bars still produce a valid fill.
        flat_entry = _bar(_ENTRY, "22000.00", high="22000.00", low="22000.00")
        flat_exit = _bar(_EXIT, "22000.00", high="22000.00", low="22000.00")
        window = FuturesBarWindow(
            entry_bar=flat_entry, exit_bar=flat_exit, path=(flat_entry, flat_exit)
        )
        out = materialize_futures_trade(
            _signal(), resolver=_Resolver(), source=_Source(window=window)
        )
        assert isinstance(out, MaterializedFuturesTrade)
        assert (out.buy_fill_paisa, out.sell_fill_paisa) == (2_200_005, 2_199_995)
        assert out.gross_pnl_paisa == -10 * 75  # exactly the 2-tick haircut

    def test_zero_volume_expiry_bar_rejects_unconditionally(self) -> None:
        # DEC-FILL-WORST-CASE-ONLY-001 (scope G) — CONSCIOUSLY SUPERSEDING
        # the R15 "a volume-0 printed futures bar is REAL and TRADABLE"
        # clause: a zero-volume print is a stale/carried price no real order
        # demonstrably filled at, so it ALWAYS rejects (the survivorship-tilt
        # cost is recorded in the DEC — dropped, never over-priced).
        window = _window(entry_volume=0, exit_volume=0)
        out = materialize_futures_trade(
            _signal(), resolver=_Resolver(), source=_Source(window=window)
        )
        assert isinstance(out, RejectedFuturesTrade)
        assert out.reason is FuturesRejectReason.STALE_BAR

    def test_worst_case_is_the_only_law_no_mode_or_stale_knobs(self) -> None:
        # DEC-FILL-WORST-CASE-ONLY-001 killed-token pins: no fill_mode and no
        # reject_stale_bars parameter survives on any executor surface (the
        # worst leg IS the law; the stale-bar reject is unconditional).
        import inspect

        from brutex.costs.futures_costs import apply_futures_fill

        for fn in (materialize_futures_trade, run_futures_over_signals, execute_futures_sim):
            params = inspect.signature(fn).parameters
            assert "fill_mode" not in params, fn.__name__
            assert "reject_stale_bars" not in params, fn.__name__
        assert "fill_mode" not in inspect.signature(apply_futures_fill).parameters

    def test_r31_vix_stamped_on_trade(self) -> None:
        # [R31] VIX passes through verbatim; missing stays an honest None.
        out = materialize_futures_trade(
            _signal(vix_at_entry=Decimal("13.5"), vix_at_exit=Decimal("14.25")),
            resolver=_Resolver(),
            source=_Source(window=_window()),
        )
        assert isinstance(out, MaterializedFuturesTrade)
        assert (out.vix_at_entry, out.vix_at_exit) == (Decimal("13.5"), Decimal("14.25"))

    def test_roll5_single_trade_reads_one_contract_window_only(self) -> None:
        # [ROLL-5/ROLL-6] the leg reads EXACTLY ONE load_window call bounded
        # [entry, exit] on ONE contract — no PDH/PDL/prev-close/prior-contract
        # read exists on the seam (the Protocol has no other read method).
        source = _Source(window=_window())
        materialize_futures_trade(_signal(), resolver=_Resolver(), source=source)
        assert source.calls == [(_SYMBOL, _ENTRY, _EXIT)]

    def test_r18_entry_fills_at_the_inherited_signal_minute_never_earlier(self) -> None:
        # [R18] the futures fill uses the SAME N+1-open minute the spot signal
        # chose — the source is asked for exactly signal.entry_ts.
        source = _Source(window=_window())
        materialize_futures_trade(_signal(), resolver=_Resolver(), source=source)
        assert source.calls[0][1] == _ENTRY


class TestMaterializedTradeInvariants:
    def test_net_consistency(self) -> None:
        import dataclasses

        out = materialize_futures_trade(
            _signal(), resolver=_Resolver(), source=_Source(window=_window())
        )
        assert isinstance(out, MaterializedFuturesTrade)
        with pytest.raises(ValueError, match="net"):
            dataclasses.replace(out, net_pnl_paisa=out.net_pnl_paisa + 1)

    def test_size_and_excursion_bounds(self) -> None:
        import dataclasses

        out = materialize_futures_trade(
            _signal(), resolver=_Resolver(), source=_Source(window=_window())
        )
        assert isinstance(out, MaterializedFuturesTrade)
        with pytest.raises(ValueError, match="lot_size"):
            dataclasses.replace(out, lot_size=0)
        with pytest.raises(ValueError, match="mae_paisa"):
            dataclasses.replace(out, mae_paisa=-1)


@dataclass(frozen=True)
class _ReasonedTrade:
    """A SpotTrade-shaped round-trip that ALSO carries an exit_reason."""

    entry_ts: datetime
    exit_ts: datetime
    vix_at_entry: Decimal | None
    vix_at_exit: Decimal | None
    exit_reason: str


class TestSignalsFromSpotTrades:
    def test_lifts_closed_trades_with_vix_passthrough(self) -> None:
        trades = [
            ClosedTrade(
                pnl_paisa=100,
                mfe_paisa=10,
                mae_paisa=5,
                holding_bars=3,
                entry_ts=_ENTRY.astimezone(UTC),
                exit_ts=_EXIT.astimezone(UTC),
                direction=Side.BUY,
                vix_at_entry=Decimal("12.75"),
                vix_at_exit=None,
            )
        ]
        signals = signals_from_spot_trades(
            trades, instrument="NIFTY", direction=TradeDirection.BULL
        )
        assert len(signals) == 1
        sig = signals[0]
        assert sig.entry_ts == _ENTRY  # localised to IST, same instant
        assert sig.entry_ts.tzinfo is not None
        assert sig.vix_at_entry == Decimal("12.75")
        assert sig.vix_at_exit is None
        assert sig.exit_reason == "signal"  # ClosedTrade carries no reason

    def test_exit_reason_passthrough_when_present(self) -> None:
        trade = _ReasonedTrade(
            entry_ts=_ENTRY,
            exit_ts=_EXIT,
            vix_at_entry=None,
            vix_at_exit=None,
            exit_reason="tp",
        )
        (sig,) = signals_from_spot_trades(
            [trade], instrument="NIFTY", direction=TradeDirection.BULL
        )
        assert sig.exit_reason == "tp"

    def test_blank_exit_reason_defaults_honestly(self) -> None:
        trade = _ReasonedTrade(
            entry_ts=_ENTRY,
            exit_ts=_EXIT,
            vix_at_entry=None,
            vix_at_exit=None,
            exit_reason="   ",
        )
        (sig,) = signals_from_spot_trades(
            [trade], instrument="NIFTY", direction=TradeDirection.BULL
        )
        assert sig.exit_reason == "signal"


class TestRunFuturesOverSignals:
    def test_r24_trade_record_every_field_and_counts(self) -> None:
        # [R24/R28] a mixed batch: 1 accepted + 1 NO_DATA (named reject) —
        # rejects are visible counts, never silent omissions.
        good = _signal()
        bad_exit = _signal(exit_ts=_EXIT + timedelta(minutes=1))
        source = _KeyedSource({(_ENTRY, _EXIT): _window()})
        res = run_futures_over_signals(
            [good, bad_exit],
            instrument="NIFTY",
            direction=TradeDirection.BULL,
            resolver=_Resolver(),
            source=source,
        )
        assert res.n_signals == 2
        assert res.n_accepted == 1
        assert res.n_rejected_no_data == 1
        assert res.n_rejected == 1
        assert res.n_winners == 1
        assert res.net_pnl_paisa == 636_750  # the worst-case golden long
        assert res.n_vix_missing == 1  # the accepted trade carried no VIX

    def test_mixed_instrument_batch_fails_loud(self) -> None:
        with pytest.raises(ValueError, match="does not match the stated row identity"):
            run_futures_over_signals(
                [_signal(instrument="BANKNIFTY")],
                instrument="NIFTY",
                direction=TradeDirection.BULL,
                resolver=_Resolver(),
                source=_Source(),
            )

    def test_mixed_direction_batch_fails_loud(self) -> None:
        with pytest.raises(ValueError, match="does not match the stated row identity"):
            run_futures_over_signals(
                [_signal(direction=TradeDirection.BEAR)],
                instrument="NIFTY",
                direction=TradeDirection.BULL,
                resolver=_Resolver(),
                source=_Source(),
            )

    def test_losers_bucket_live(self) -> None:
        # A losing trade (exit below entry) lands in n_losers.
        window = _window(exit_open="21900.00")
        res = run_futures_over_signals(
            [_signal()],
            instrument="NIFTY",
            direction=TradeDirection.BULL,
            resolver=_Resolver(),
            source=_Source(window=window),
        )
        assert (res.n_winners, res.n_losers) == (0, 1)
        assert res.win_rate == 0.0

    def test_r31_vix_present_not_counted_missing(self) -> None:
        # [R31] a VIX-carrying accepted trade does NOT bump n_vix_missing.
        res = run_futures_over_signals(
            [_signal(vix_at_entry=Decimal("13.5"))],
            instrument="NIFTY",
            direction=TradeDirection.BULL,
            resolver=_Resolver(),
            source=_Source(window=_window()),
        )
        assert res.n_accepted == 1
        assert res.n_vix_missing == 0

    def test_empty_batch(self) -> None:
        res = run_futures_over_signals(
            [],
            instrument="NIFTY",
            direction=TradeDirection.BULL,
            resolver=_Resolver(),
            source=_Source(),
        )
        assert res.n_signals == 0
        assert res.win_rate == 0.0


@dataclass
class _KeyedSource:
    """Returns a window only for known (entry, exit) keys."""

    windows: dict[tuple[datetime, datetime], FuturesBarWindow]

    def load_window(
        self, *, contract_symbol: str, entry_ts: datetime, exit_ts: datetime
    ) -> FuturesBarWindow | None:
        return self.windows.get((entry_ts, exit_ts))


class TestRuleResultInvariants:
    def _result(self, **overrides: Any) -> FuturesRuleResult:
        kwargs: dict[str, Any] = {
            "instrument": "NIFTY",
            "direction": "bull",
            "n_signals": 2,
            "n_accepted": 1,
            "n_rejected_lot_size_unknown": 0,
            "n_rejected_cost_regime_unverified": 0,
            "n_rejected_no_contract": 1,
            "n_rejected_no_data": 0,
            "n_rejected_stale_bar": 0,
            "gross_pnl_paisa": 100,
            "total_charges_paisa": 0,
            "net_pnl_paisa": 100,
            "n_winners": 1,
            "n_losers": 0,
        }
        kwargs.update(overrides)
        return FuturesRuleResult(**kwargs)

    def test_negative_count_rejected(self) -> None:
        with pytest.raises(ValueError, match="n_signals"):
            self._result(n_signals=-1)

    def test_count_mismatch_rejected(self) -> None:
        with pytest.raises(ValueError, match="must equal n_signals"):
            self._result(n_signals=5)

    def test_winners_bound(self) -> None:
        with pytest.raises(ValueError, match="cannot exceed n_accepted"):
            self._result(n_winners=2, n_losers=1)

    def test_win_rate(self) -> None:
        assert self._result().win_rate == 1.0

    def test_reject_field_projection_is_complete(self) -> None:
        # [R28] every FuturesRejectReason member maps to exactly ONE result
        # field — a new member without a field fails THIS pin, never a silent
        # catch-all bucket.
        assert set(_REJECT_FIELD_BY_REASON) == set(FuturesRejectReason)
        fields = set(FuturesRuleResult.__dataclass_fields__)
        for reject_field in _REJECT_FIELD_BY_REASON.values():
            assert reject_field in fields
        assert len(set(_REJECT_FIELD_BY_REASON.values())) == len(FuturesRejectReason)


class TestExecuteFuturesSim:
    def test_r28_r41_r43_audit_row_stamped_with_run_id(self) -> None:
        audit = _Audit()
        result = execute_futures_sim(
            [_signal()],
            instrument="NIFTY",
            direction=TradeDirection.BULL,
            run_id="abc123def456ghi",
            resolver=_Resolver(),
            source=_Source(window=_window()),
            audit=audit,
        )
        assert result.n_accepted == 1
        assert len(audit.rows) == 1
        action, subject, payload = audit.rows[0]
        assert action == "FUTURES_EXEC_SIM"
        assert subject == "NIFTY:bull:abc123def456ghi"
        assert payload["run_id"] == "abc123def456ghi"
        assert payload["n_accepted"] == 1
        assert payload["suspect"] is False
        assert payload["differential_checked"] == 1

    def test_r45_differential_clean_run_not_suspect(self) -> None:
        with capture_logs() as logs:
            execute_futures_sim(
                [_signal()],
                instrument="NIFTY",
                direction=TradeDirection.BULL,
                run_id="run-1",
                resolver=_Resolver(),
                source=_Source(window=_window()),
            )
        assert "futures.sim.differential_mismatch" not in [x["event"] for x in logs]

    def test_r45_differential_naive_reference_mismatch_marks_suspect(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # [R45] an intentionally divergent naive stub trips SUSPECT + loud.
        monkeypatch.setattr(reference_sim, "naive_reference_futures_pnl", lambda *a, **k: 42)
        audit = _Audit()
        with capture_logs() as logs:
            execute_futures_sim(
                [_signal()],
                instrument="NIFTY",
                direction=TradeDirection.BULL,
                run_id="run-2",
                resolver=_Resolver(),
                source=_Source(window=_window()),
                audit=audit,
            )
        events = [x["event"] for x in logs]
        assert "futures.sim.differential_mismatch" in events
        assert audit.rows[0][2]["suspect"] is True

    def test_differential_disabled_and_empty(self) -> None:
        audit = _Audit()
        execute_futures_sim(
            [_signal()],
            instrument="NIFTY",
            direction=TradeDirection.BULL,
            run_id="run-3",
            resolver=_Resolver(),
            source=_Source(window=_window()),
            audit=audit,
            spot_check_k=0,
        )
        assert audit.rows[0][2]["differential_checked"] == 0
        # Empty signals short-circuit the sample arm too.
        result = execute_futures_sim(
            [],
            instrument="NIFTY",
            direction=TradeDirection.BULL,
            run_id="run-4",
            resolver=_Resolver(),
            source=_Source(),
        )
        assert result.n_signals == 0

    def test_deterministic_sample_reproducible_and_bounded(self) -> None:
        signals = [_signal(entry_ts=_ENTRY + timedelta(minutes=i)) for i in range(10)]
        s1 = executor_module._deterministic_sample(signals, k=3, run_id="fixed-run")
        s2 = executor_module._deterministic_sample(signals, k=3, run_id="fixed-run")
        assert s1 == s2
        assert len(s1) == 3
        assert executor_module._deterministic_sample(signals, k=100, run_id="x") == signals

    def test_stale_law_reject_excluded_from_differential(self) -> None:
        # A stale-rejected sample (the UNCONDITIONAL zero-volume law,
        # DEC-FILL-WORST-CASE-ONLY-001) is excluded from the arithmetic
        # differential OPENLY — no false SUSPECT, no silent drop.
        window = _window(entry_volume=0)
        audit = _Audit()
        with capture_logs() as logs:
            result = execute_futures_sim(
                [_signal()],
                instrument="NIFTY",
                direction=TradeDirection.BULL,
                run_id="run-5",
                resolver=_Resolver(),
                source=_Source(window=window),
                audit=audit,
            )
        assert result.n_rejected_stale_bar == 1
        assert audit.rows[0][2]["suspect"] is False
        assert audit.rows[0][2]["differential_checked"] == 0
        assert "futures.sim.differential_mismatch" not in [x["event"] for x in logs]

    def test_aggregate_unverified_cost_window_warn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # [R28] ONE aggregate WARN when any trade fell in an unverified cost
        # window; spans derived from the regime-table DATA. A cost-bearing
        # stock-future batch needs a lot fixture (no in-repo stock lot history
        # — the monkeypatch is the synthetic lot, never shipped).
        monkeypatch.setattr(executor_module, "futures_lot_size", lambda *_a: 500)
        with capture_logs() as logs:
            result = execute_futures_sim(
                [_signal(instrument="RELIANCE")],
                instrument="RELIANCE",
                direction=TradeDirection.BULL,
                run_id="run-6",
                resolver=_Resolver(),
                source=_Source(),
            )
        assert result.n_rejected_cost_regime_unverified == 1
        warn = [x for x in logs if x["event"] == "futures.sim.unverified_cost_window"]
        assert len(warn) == 1
        assert "NSE" in warn[0]["windows"]


class TestWorstCaseFloorLaw:
    """DEC-FILL-WORST-CASE-ONLY-001 — the one-law floor + differential teeth."""

    def test_range_bar_fills_dominated_by_open_anchors(self) -> None:
        # The floor half of the worst-case law on a RANGE bar: the buy fill
        # sits at/above the retired open-anchored fill (open + tick) and the
        # sell fill at/below (open − tick) — worst can never be BETTER than
        # the retired precise pricing, on any bar.
        out = materialize_futures_trade(
            _signal(), resolver=_Resolver(), source=_Source(window=_window())
        )
        assert isinstance(out, MaterializedFuturesTrade)
        assert out.buy_fill_paisa >= out.entry_open_paisa + TICK_PAISA
        assert out.sell_fill_paisa <= out.exit_open_paisa - TICK_PAISA
        # And STRICT dominance here — the default _window bars carry range.
        assert out.buy_fill_paisa > out.entry_open_paisa + TICK_PAISA
        assert out.sell_fill_paisa < out.exit_open_paisa - TICK_PAISA

    def test_no_data_reject_unchanged_by_the_law(self) -> None:
        # The fill law moves PRICES only — an absent window is still the
        # counted NO_DATA reject, never a fabricated bar to price worst-case.
        out = materialize_futures_trade(
            _signal(), resolver=_Resolver(), source=_Source(window=None)
        )
        assert isinstance(out, RejectedFuturesTrade)
        assert out.reason is FuturesRejectReason.NO_DATA

    def test_r45_differential_green_under_the_one_law(self) -> None:
        # The R45 differential: the naive reference RE-STATES the worst-case
        # law independently, so a clean production run is never SUSPECT.
        audit = _Audit()
        with capture_logs() as logs:
            execute_futures_sim(
                [_signal()],
                instrument="NIFTY",
                direction=TradeDirection.BULL,
                run_id="run-one-law",
                resolver=_Resolver(),
                source=_Source(window=_window()),
                audit=audit,
            )
        assert audit.rows[0][2]["suspect"] is False
        assert audit.rows[0][2]["differential_checked"] == 1
        assert "futures.sim.differential_mismatch" not in [x["event"] for x in logs]

    def test_differential_teeth_wrong_net_trips_suspect(self) -> None:
        # Anti-vacuity teeth: a production net that DIVERGES from the naive
        # worst-case restatement (here off by one paisa) trips SUSPECT — the
        # differential is not trivially green.
        prod = materialize_futures_trade(
            _signal(), resolver=_Resolver(), source=_Source(window=_window())
        )
        assert isinstance(prod, MaterializedFuturesTrade)
        report = reference_sim.differential_spot_check(
            [_signal()],
            [prod.net_pnl_paisa + 1],
            resolver=_Resolver(),
            source=_Source(window=_window()),
        )
        assert report.suspect
        clean = reference_sim.differential_spot_check(
            [_signal()],
            [prod.net_pnl_paisa],
            resolver=_Resolver(),
            source=_Source(window=_window()),
        )
        assert not clean.suspect


# ─────────────────────────────────────────────────────────────────────────────
# DEC-EXEC-RUST-CORE-001 — the BRUTEX_EXEC_RUST dispatch tail (slice 7 / P8)
# ─────────────────────────────────────────────────────────────────────────────


class TestExecRustDispatch:
    """ON-arm parity of ``_materialize_tail_rust`` through the REAL
    ``materialize_futures_trade`` entry point.

    The flag-less default (every test above) is the Python tail; these flip
    ``BRUTEX_EXEC_RUST=1`` and prove: the kernel-served trade (cost-free
    index / zerodha / synthetic-cited cost-bearing arms) is WHOLE-OBJECT
    equal to the OFF arm (engagement-spied), the stale verdict maps onto the
    SAME reject + single WARN, and every fallback shape lands byte-
    identically on the Python tail.
    """

    def _off(self, monkeypatch: pytest.MonkeyPatch, window: FuturesBarWindow, **kw: Any) -> object:
        from brutex import exec_rust

        monkeypatch.setenv(exec_rust.ENV_FLAG, "0")
        return materialize_futures_trade(
            kw.pop("signal", _signal()),
            resolver=_Resolver(),
            source=_Source(window=window),
            **kw,
        )

    def test_on_arm_trade_equals_off_arm_and_engages(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import fno_math
        from brutex import exec_rust

        window = _window()
        off = self._off(monkeypatch, window)

        calls: dict[str, int] = {}
        real = fno_math.exec_futures_materialize

        def _spy(*a: Any) -> object:
            calls["exec_futures_materialize"] = calls.get("exec_futures_materialize", 0) + 1
            return real(*a)

        monkeypatch.setattr(
            exec_rust, "_rust", type("K", (), {"exec_futures_materialize": staticmethod(_spy)})()
        )
        monkeypatch.setenv(exec_rust.ENV_FLAG, "1")
        on = materialize_futures_trade(
            _signal(), resolver=_Resolver(), source=_Source(window=window)
        )
        assert calls == {"exec_futures_materialize": 1}
        assert isinstance(on, MaterializedFuturesTrade)
        assert on == off
        assert on.cost_free is True

    def test_on_arm_zerodha_equals_off_arm(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from brutex import exec_rust

        window = _window()
        off = self._off(monkeypatch, window, broker="zerodha")
        monkeypatch.setenv(exec_rust.ENV_FLAG, "1")
        on = materialize_futures_trade(
            _signal(), resolver=_Resolver(), source=_Source(window=window), broker="zerodha"
        )
        assert isinstance(on, MaterializedFuturesTrade)
        assert on == off

    def test_on_arm_short_side_equals_off_arm(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from brutex import exec_rust

        window = _window()
        signal = _signal(direction=TradeDirection.BEAR)
        off = self._off(monkeypatch, window, signal=signal)
        monkeypatch.setenv(exec_rust.ENV_FLAG, "1")
        on = materialize_futures_trade(signal, resolver=_Resolver(), source=_Source(window=window))
        assert isinstance(on, MaterializedFuturesTrade)
        assert on == off

    def test_on_arm_cost_bearing_synthetic_cited_equals_off_arm(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The cost-bearing rate-resolution arm (STT → ETC → NSE-IPFT →
        stamp) under the sanctioned synthetic-cited regime fixture + the
        synthetic stock lot (no in-repo stock lot history)."""
        from brutex import exec_rust
        from brutex.costs import futures_costs as fc
        from brutex.costs.constants import RegimeRow, validate_regime_table

        synth_etc = validate_regime_table(
            (RegimeRow(date(2024, 10, 1), 173, "SYNTHETIC test circular — never shipped"),),
            label="synthetic ETC futures",
        )
        synth_ipft = validate_regime_table(
            (RegimeRow(date(2024, 10, 1), 10, "SYNTHETIC test circular — never shipped"),),
            label="synthetic IPFT futures",
        )
        synth_stamp = validate_regime_table(
            (RegimeRow(date(2024, 10, 1), 200, "SYNTHETIC test circular — never shipped"),),
            label="synthetic stamp futures",
        )
        monkeypatch.setattr(executor_module, "futures_lot_size", lambda *_a: 500)
        monkeypatch.setitem(fc._ETC_FUTURES_REGIMES_BY_EXCHANGE, "NSE", synth_etc)
        monkeypatch.setattr(fc, "_NSE_IPFT_FUTURES_REGIMES", synth_ipft)
        monkeypatch.setattr(fc, "_STAMP_FUTURES_REGIMES", synth_stamp)

        window = _window()
        signal = _signal(instrument="RELIANCE")
        off = self._off(monkeypatch, window, signal=signal)
        monkeypatch.setenv(exec_rust.ENV_FLAG, "1")
        on = materialize_futures_trade(signal, resolver=_Resolver(), source=_Source(window=window))
        assert isinstance(on, MaterializedFuturesTrade)
        assert on == off
        assert on.cost_free is False
        assert on.total_charges_paisa > 0

    def test_on_arm_stale_bar_unconditional_matches_off_arm_with_one_warn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from brutex import exec_rust

        window = _window(entry_volume=0)
        off = self._off(monkeypatch, window)
        monkeypatch.setenv(exec_rust.ENV_FLAG, "1")
        with capture_logs() as logs:
            on = materialize_futures_trade(
                _signal(), resolver=_Resolver(), source=_Source(window=window)
            )
        assert isinstance(on, RejectedFuturesTrade)
        assert on.reason is FuturesRejectReason.STALE_BAR
        assert on == off
        events = [x for x in logs if x["event"] == "futures.reject.stale_bar"]
        assert len(events) == 1
        assert events[0]["entry_volume"] == 0

    @pytest.mark.parametrize(
        "tail_result",
        [None, ("bogus_kind", None, 0, 0), ("trade", None, 0, 0)],
        ids=["tail-none", "unknown-kind", "trade-without-stack"],
    )
    def test_fallback_shapes_land_on_the_python_tail(
        self, monkeypatch: pytest.MonkeyPatch, tail_result: object
    ) -> None:
        from brutex import exec_rust

        window = _window()
        off = self._off(monkeypatch, window)
        monkeypatch.setenv(exec_rust.ENV_FLAG, "1")
        monkeypatch.setattr(exec_rust, "materialize_futures_tail", lambda *a, **k: tail_result)
        on = materialize_futures_trade(
            _signal(), resolver=_Resolver(), source=_Source(window=window)
        )
        assert isinstance(on, MaterializedFuturesTrade)
        assert on == off

    def test_prep_or_kernel_raise_falls_back_to_the_python_tail(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from brutex import exec_rust

        window = _window()
        off = self._off(monkeypatch, window)

        def _boom(*a: object, **k: object) -> object:
            raise TypeError("kernel prep fault")

        monkeypatch.setenv(exec_rust.ENV_FLAG, "1")
        monkeypatch.setattr(exec_rust, "materialize_futures_tail", _boom)
        on = materialize_futures_trade(
            _signal(), resolver=_Resolver(), source=_Source(window=window)
        )
        assert isinstance(on, MaterializedFuturesTrade)
        assert on == off

    @pytest.mark.parametrize(
        "wire",
        [
            list(range(16)),
            list(range(17)),
            list(range(18)),
            list(range(20)),
            list("abcdefghijklmnopqrs"),  # the pinned length 19, junk element types
        ],
        ids=["len-16", "len-17", "len-18", "len-20", "len-19-wrong-type"],
    )
    def test_malformed_kernel_wire_warns_and_serves_the_python_arm(
        self, monkeypatch: pytest.MonkeyPatch, wire: list[object]
    ) -> None:
        """Review round 2 (R2-MINOR/R3-F2): a 17/18-slot futures wire from
        the KERNEL previously raised an UNCAUGHT ``IndexError`` out of
        ``materialize_futures_trade`` (a raw crash — outside the seam catch
        AND this executor's broad catch), and a 20-slot wire was silently
        truncated-adopted. Every malformed shape now lands on the
        byte-identical Python arm with ONE loud seam-side
        ``exec_rust.kernel_fallback`` (``KernelWireFault``)."""
        import fno_math
        from brutex import exec_rust

        window = _window()
        off = self._off(monkeypatch, window)
        exec_rust.reset_warn_memo_for_tests()
        monkeypatch.setenv(exec_rust.ENV_FLAG, "1")
        # Malformed ONLY at the tail symbol; every other kernel entry
        # delegates to the real wheel (the Python tail's own inner
        # charge-stack dispatch must not trip the stale-wheel guard).
        fake = SimpleNamespace(
            exec_futures_materialize=lambda *_a: ("trade", wire),
            **{
                n: getattr(fno_math, n)
                for n in ("exec_futures_charge_stack", "exec_apply_futures_fill")
            },
        )
        monkeypatch.setattr(exec_rust, "_rust", fake)
        with capture_logs() as logs:
            on = materialize_futures_trade(
                _signal(), resolver=_Resolver(), source=_Source(window=window)
            )
        assert isinstance(on, MaterializedFuturesTrade)
        assert on == off
        events = [x for x in logs if x["event"] == "exec_rust.kernel_fallback"]
        assert len(events) == 1
        assert events[0]["site"] == "materialize_futures_tail"
        assert events[0]["error_type"] == "KernelWireFault"
        exec_rust.reset_warn_memo_for_tests()
