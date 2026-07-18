"""Unit suite for :mod:`brutex.engine.fill_sensitivity` (DEC-FILL-EXEC-GRID-1M-001, R13).

Pins the open-anchored reference law ARM-FOR-ARM with hand-derived paisa
expectations, the priced verdict shape + the haircut arithmetic law, all
three ``sign_flip`` arms, and every NAMED ``not_priced`` degrade — the
annotation is correct-or-absent, never a fabricated verdict (Golden Rule #1).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from brutex.data.models import CandleBar
from brutex.engine.fill_sensitivity import (
    FILL_REFERENCE_BASIS,
    FILL_SENSITIVITY_NOT_PRICED_REASONS,
    FILL_SENSITIVITY_STATUS_NOT_PRICED,
    FILL_SENSITIVITY_STATUS_PRICED,
    _ReferenceUnpriceable,
    fill_sensitivity_not_priced,
    fill_sensitivity_verdict,
    open_anchored_trade_pnl,
)
from brutex.engine.minute_path import PathStatus
from brutex.engine.trade_capture import CapturedTrade
from brutex.strategies.types import Side
from hypothesis import given, settings
from hypothesis import strategies as st

#: 09:16 IST on the fixture day, expressed in UTC (the stored-bar convention).
_T0 = datetime(2026, 1, 5, 3, 46, tzinfo=UTC)


def _ts(minute: int) -> datetime:
    """The fixture's minute-*minute* UTC timestamp."""
    return _T0 + timedelta(minutes=minute)


def _bar(minute: int, *, open_: str, close: str | None = None) -> CandleBar:
    """One 1m execution bar at fixture minute *minute* (ordering-valid)."""
    o = Decimal(open_)
    c = o if close is None else Decimal(close)
    return CandleBar(
        timestamp=_ts(minute),
        open=o,
        high=max(o, c),
        low=min(o, c),
        close=c,
        volume=100,
        open_interest=None,
    )


def _index(bars: list[CandleBar]) -> dict[datetime, CandleBar]:
    return {bar.timestamp: bar for bar in bars}


def _trade(
    *,
    entry_minute: int = 0,
    exit_minute: int = 1,
    direction: Side = Side.BUY,
    exit_reason: str = "signal",
    pnl_paisa: int = 0,
) -> CapturedTrade:
    """A valid captured trade — the P3→P4 trade-row construction idiom."""
    return CapturedTrade(
        entry_ts=_ts(entry_minute),
        entry_px=Decimal("100.00"),
        exit_ts=_ts(exit_minute),
        exit_px=Decimal("100.00"),
        direction=direction,
        lots=None,
        exit_reason=exit_reason,
        pnl_paisa=pnl_paisa,
        costs_paisa=None,
        mae_paisa=0,
        mfe_paisa=0,
        mae_1m_paisa=None,
        mfe_1m_paisa=None,
        path_1m_status=PathStatus.NO_1M_DATA,
        vix_entry=None,
        vix_exit=None,
    )


#: Two bars: entry minute 0 opens 100.00; exit minute 1 opens 100.50 and
#: closes 99.40 — distinct OPEN vs CLOSE so the exit-price arm is observable.
_BARS = [_bar(0, open_="100.00", close="100.20"), _bar(1, open_="100.50", close="99.40")]
_BARS_BY_TS = _index(_BARS)


class TestNotPriced:
    @pytest.mark.parametrize("reason", FILL_SENSITIVITY_NOT_PRICED_REASONS)
    def test_every_named_reason_builds_the_exact_object(self, reason: str) -> None:
        assert fill_sensitivity_not_priced(reason) == {
            "status": FILL_SENSITIVITY_STATUS_NOT_PRICED,
            "reason": reason,
        }

    def test_unknown_reason_fails_loud(self) -> None:
        """A typo'd reason must fail at the WRITER — never ship an unknown token."""
        with pytest.raises(ValueError, match="unknown reason 'typo'"):
            fill_sensitivity_not_priced("typo")


class TestOpenAnchoredTradePnl:
    def test_missing_entry_bar_raises_bar_missing(self) -> None:
        trade = _trade(entry_minute=99)
        with pytest.raises(_ReferenceUnpriceable) as excinfo:
            open_anchored_trade_pnl(trade, _BARS_BY_TS, sl_paisa=80, tp_paisa=50)
        assert excinfo.value.reason == "bar_missing"

    @pytest.mark.parametrize("direction", [Side.BUY, Side.SELL])
    def test_take_profit_is_exactly_the_level(self, direction: Side) -> None:
        # tp anchors on the OPEN entry: pnl exactly +tp regardless of side.
        trade = _trade(direction=direction, exit_reason="take_profit")
        assert open_anchored_trade_pnl(trade, _BARS_BY_TS, sl_paisa=80, tp_paisa=50) == 50

    def test_take_profit_without_a_level_is_unpriceable(self) -> None:
        trade = _trade(exit_reason="take_profit")
        with pytest.raises(_ReferenceUnpriceable) as excinfo:
            open_anchored_trade_pnl(trade, _BARS_BY_TS, sl_paisa=80, tp_paisa=None)
        assert excinfo.value.reason == "unknown_exit_reason"

    def test_stop_loss_is_exactly_minus_the_level(self) -> None:
        trade = _trade(exit_reason="stop_loss")
        assert open_anchored_trade_pnl(trade, _BARS_BY_TS, sl_paisa=80, tp_paisa=50) == -80

    def test_stop_loss_without_a_level_is_unpriceable(self) -> None:
        trade = _trade(exit_reason="stop_loss")
        with pytest.raises(_ReferenceUnpriceable) as excinfo:
            open_anchored_trade_pnl(trade, _BARS_BY_TS, sl_paisa=None, tp_paisa=50)
        assert excinfo.value.reason == "unknown_exit_reason"

    def test_missing_exit_bar_raises_bar_missing(self) -> None:
        trade = _trade(exit_minute=99, exit_reason="signal")
        with pytest.raises(_ReferenceUnpriceable) as excinfo:
            open_anchored_trade_pnl(trade, _BARS_BY_TS, sl_paisa=80, tp_paisa=50)
        assert excinfo.value.reason == "bar_missing"

    def test_signal_buy_prices_at_the_exit_open(self) -> None:
        # entry = minute-0 OPEN 100.00 → 10_000 paisa; exit = minute-1 OPEN
        # 100.50 → 10_050 paisa; sign(BUY)=+1 → +50 paisa.
        trade = _trade(exit_reason="signal", direction=Side.BUY)
        assert open_anchored_trade_pnl(trade, _BARS_BY_TS, sl_paisa=80, tp_paisa=50) == 50

    def test_signal_sell_negates_the_move(self) -> None:
        # Same legs, sign(SELL)=-1 → -(10_050 - 10_000) = -50 paisa.
        trade = _trade(exit_reason="signal", direction=Side.SELL)
        assert open_anchored_trade_pnl(trade, _BARS_BY_TS, sl_paisa=80, tp_paisa=50) == -50

    def test_forced_1520_prices_at_the_exit_open(self) -> None:
        # forced_1520 rides the SAME open-anchored arm as signal: +50 paisa.
        trade = _trade(exit_reason="forced_1520", direction=Side.BUY)
        assert open_anchored_trade_pnl(trade, _BARS_BY_TS, sl_paisa=80, tp_paisa=50) == 50

    def test_end_of_data_buy_prices_at_the_exit_close(self) -> None:
        # entry OPEN 100.00 → 10_000; exit CLOSE 99.40 → 9_940; BUY → -60 paisa.
        trade = _trade(exit_reason="end_of_data", direction=Side.BUY)
        assert open_anchored_trade_pnl(trade, _BARS_BY_TS, sl_paisa=80, tp_paisa=50) == -60

    def test_session_gap_sell_prices_at_the_exit_close(self) -> None:
        # Same close-priced legs, sign(SELL)=-1 → -(9_940 - 10_000) = +60 paisa.
        trade = _trade(exit_reason="session_gap", direction=Side.SELL)
        assert open_anchored_trade_pnl(trade, _BARS_BY_TS, sl_paisa=80, tp_paisa=50) == 60

    def test_exit_reason_outside_the_priced_vocabulary_is_unpriceable(self) -> None:
        trade = _trade(exit_reason="signal")
        # The frozen validator admits only today's EXIT_REASONS — bypass it the
        # way frozen dataclasses themselves do, modelling a FUTURE append-only
        # vocabulary growth the reference law does not yet price (the
        # defensive-totality arm's honest degrade).
        object.__setattr__(trade, "exit_reason", "alien_reason")
        with pytest.raises(_ReferenceUnpriceable) as excinfo:
            open_anchored_trade_pnl(trade, _BARS_BY_TS, sl_paisa=80, tp_paisa=50)
        assert excinfo.value.reason == "unknown_exit_reason"


class TestFillSensitivityVerdict:
    def test_zero_trades_prices_with_null_sign_flip(self) -> None:
        # No trades folded → no verdict on sign_flip (null, never a fabricated
        # False); nets + haircut are exact zeros.
        assert fill_sensitivity_verdict([], [], sl_paisa=80, tp_paisa=50) == {
            "status": FILL_SENSITIVITY_STATUS_PRICED,
            "reference_basis": FILL_REFERENCE_BASIS,
            "n_trades": 0,
            "worst_case_net_pnl_paisa": 0,
            "reference_net_pnl_paisa": 0,
            "haircut_paisa": 0,
            "sign_flip": None,
        }

    def test_priced_shape_and_haircut_arithmetic(self) -> None:
        # trade 1: take_profit → reference +50; shipped worst-case pnl +50.
        # trade 2: signal BUY, entry OPEN 100.00 / exit OPEN 100.50 →
        #   reference +50; shipped worst-case pnl -20 (adverse fills).
        # reference_net = 100, worst_net = 30, haircut = 100 - 30 = 70;
        # worst_net > 0 → sign_flip False.
        trades = [
            _trade(exit_reason="take_profit", pnl_paisa=50),
            _trade(exit_reason="signal", pnl_paisa=-20),
        ]
        assert fill_sensitivity_verdict(trades, _BARS, sl_paisa=80, tp_paisa=50) == {
            "status": FILL_SENSITIVITY_STATUS_PRICED,
            "reference_basis": FILL_REFERENCE_BASIS,
            "n_trades": 2,
            "worst_case_net_pnl_paisa": 30,
            "reference_net_pnl_paisa": 100,
            "haircut_paisa": 70,
            "sign_flip": False,
        }

    def test_sign_flip_true_when_reference_positive_and_worst_nonpositive(self) -> None:
        # reference +50 (signal at the exit OPEN) vs shipped worst-case -20:
        # the worst-case pricing alone flips the verdict's sign.
        trades = [_trade(exit_reason="signal", pnl_paisa=-20)]
        verdict = fill_sensitivity_verdict(trades, _BARS, sl_paisa=80, tp_paisa=50)
        assert verdict["reference_net_pnl_paisa"] == 50
        assert verdict["worst_case_net_pnl_paisa"] == -20
        assert verdict["sign_flip"] is True

    def test_sign_flip_false_when_reference_nonpositive(self) -> None:
        # A losing reference (stop_loss → -80) can never sign-flip, whatever
        # the worst-case net reads.
        trades = [_trade(exit_reason="stop_loss", pnl_paisa=-100)]
        verdict = fill_sensitivity_verdict(trades, _BARS, sl_paisa=80, tp_paisa=50)
        assert verdict["reference_net_pnl_paisa"] == -80
        assert verdict["sign_flip"] is False

    def test_exec_bar_index_is_keep_first_on_a_duplicated_timestamp(self) -> None:
        # Belt-and-braces keep-FIRST: the duplicate minute-0 bar (open 999.00)
        # must NOT displace the first (open 100.00) — reference stays +50.
        bars = [*_BARS, _bar(0, open_="999.00")]
        verdict = fill_sensitivity_verdict(
            [_trade(exit_reason="signal")], bars, sl_paisa=80, tp_paisa=50
        )
        assert verdict["reference_net_pnl_paisa"] == 50

    def test_missing_bar_degrades_the_whole_verdict(self) -> None:
        # One unpriceable trade → the WHOLE verdict is the named not_priced
        # object (never a mixed partial fold) — even with a priceable sibling.
        trades = [
            _trade(exit_reason="take_profit", pnl_paisa=50),
            _trade(entry_minute=99, exit_reason="signal"),
        ]
        assert fill_sensitivity_verdict(trades, _BARS, sl_paisa=80, tp_paisa=50) == {
            "status": FILL_SENSITIVITY_STATUS_NOT_PRICED,
            "reason": "bar_missing",
        }

    def test_levelless_level_exit_degrades_as_unknown_exit_reason(self) -> None:
        trades = [_trade(exit_reason="take_profit")]
        assert fill_sensitivity_verdict(trades, _BARS, sl_paisa=80, tp_paisa=None) == {
            "status": FILL_SENSITIVITY_STATUS_NOT_PRICED,
            "reason": "unknown_exit_reason",
        }

    @given(
        legs=st.lists(
            st.tuples(
                st.sampled_from(["take_profit", "stop_loss"]),
                st.integers(min_value=-10_000, max_value=10_000),
            ),
            max_size=8,
        )
    )
    @settings(max_examples=25, deadline=None)
    def test_haircut_law_over_generated_trade_lists(self, legs: list[tuple[str, int]]) -> None:
        """haircut == reference_net − worst_net; worst_net == Σ shipped pnl."""
        trades = [_trade(exit_reason=reason, pnl_paisa=pnl) for reason, pnl in legs]
        verdict = fill_sensitivity_verdict(trades, _BARS, sl_paisa=80, tp_paisa=50)
        assert verdict["status"] == FILL_SENSITIVITY_STATUS_PRICED
        assert verdict["n_trades"] == len(trades)
        assert verdict["worst_case_net_pnl_paisa"] == sum(pnl for _, pnl in legs)
        # The level exits are the exact levels: +50 per tp, -80 per sl.
        assert verdict["reference_net_pnl_paisa"] == sum(
            50 if reason == "take_profit" else -80 for reason, _ in legs
        )
        reference_net = verdict["reference_net_pnl_paisa"]
        worst_net = verdict["worst_case_net_pnl_paisa"]
        assert isinstance(reference_net, int)
        assert isinstance(worst_net, int)
        assert verdict["haircut_paisa"] == reference_net - worst_net
