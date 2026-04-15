"""Unit tests for :mod:`brutex.strategies.library` \u2014 pure per-bar logic.

These tests exercise each concrete strategy's ``on_event`` state
machine directly, feeding hand-built :class:`FeedEvent` objects and
asserting the emitted :class:`Signal` sequence. They do NOT spin up
a real :class:`ReplayFeed` or catalog \u2014 that is the job of
:mod:`tests.integration.test_strategies_library_runner`. Keeping
the logic-level assertions here means the tests run in CI's
``[dev]``-only environment without needing polars / duckdb / lmdb.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from brutex.data.models import CandleBar
from brutex.feed.replay import FeedEvent
from brutex.strategies.errors import StrategyError
from brutex.strategies.library import (
    BuyAndHold,
    ClosePreviousMomentum,
    DonchianBreakout,
    OpeningRangeBreakout,
)
from brutex.strategies.types import Side, StrategyState

# ────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────


def _bar(
    day: int,
    minute: int,
    *,
    close: float,
    high: float | None = None,
    low: float | None = None,
) -> CandleBar:
    """Build a deterministic :class:`CandleBar`.

    Leaving high / low unset gives a symmetric ``close \u00b1 1`` bar so
    callers who only care about close can stay terse.
    """
    return CandleBar(
        timestamp=datetime(2024, 1, day, 9, minute, 0, tzinfo=UTC),
        open=Decimal(str(close)),
        high=Decimal(str(high if high is not None else close + 1)),
        low=Decimal(str(low if low is not None else close - 1)),
        close=Decimal(str(close)),
        volume=10,
        open_interest=500,
    )


def _event(bar: CandleBar, *, symbol: str = "NSE-NIFTY") -> FeedEvent:
    return FeedEvent(symbol=symbol, bar=bar)


# ────────────────────────────────────────────────────────────────────────
# BuyAndHold
# ────────────────────────────────────────────────────────────────────────


class TestBuyAndHold:
    def test_first_bar_emits_single_buy(self) -> None:
        strat = BuyAndHold()
        state = StrategyState()
        sigs = strat.on_event(_event(_bar(1, 15, close=100)), state)
        assert len(sigs) == 1
        assert sigs[0].side == Side.BUY
        assert sigs[0].symbol == "NSE-NIFTY"
        assert sigs[0].reason == "buy_and_hold:first_bar"

    def test_subsequent_bars_are_silent(self) -> None:
        strat = BuyAndHold()
        state = StrategyState()
        strat.on_event(_event(_bar(1, 15, close=100)), state)
        for minute in (16, 17, 18, 19, 20):
            sigs = strat.on_event(_event(_bar(1, minute, close=100)), state)
            assert sigs == []

    def test_reset_rearms_the_trigger(self) -> None:
        """After ``state.reset()`` the strategy must emit again on the next bar."""
        strat = BuyAndHold()
        state = StrategyState()
        strat.on_event(_event(_bar(1, 15, close=100)), state)
        state.reset()
        sigs = strat.on_event(_event(_bar(2, 15, close=100)), state)
        assert len(sigs) == 1
        assert sigs[0].side == Side.BUY


# ────────────────────────────────────────────────────────────────────────
# DonchianBreakout
# ────────────────────────────────────────────────────────────────────────


class TestDonchianBreakoutValidation:
    def test_negative_n_rejected(self) -> None:
        with pytest.raises(StrategyError, match="positive"):
            DonchianBreakout({"n": -1})

    def test_zero_n_rejected(self) -> None:
        with pytest.raises(StrategyError, match="positive"):
            DonchianBreakout({"n": 0})

    def test_default_n_applied_when_omitted(self) -> None:
        strat = DonchianBreakout()
        assert strat.n == 20

    def test_custom_n_applied(self) -> None:
        assert DonchianBreakout({"n": 5}).n == 5


class TestDonchianBreakoutLogic:
    def test_warmup_bars_emit_nothing(self) -> None:
        """First ``n`` bars fill the window and emit no signals."""
        strat = DonchianBreakout({"n": 3})
        state = StrategyState()
        for m in range(3):
            sigs = strat.on_event(_event(_bar(1, 15 + m, close=100.0)), state)
            assert sigs == []

    def test_break_above_high_emits_buy(self) -> None:
        """Once the window is full, a close above prior max fires a BUY."""
        strat = DonchianBreakout({"n": 3})
        state = StrategyState()
        # Window fills with 100, 100, 100
        for m in range(3):
            strat.on_event(_event(_bar(1, 15 + m, close=100.0)), state)
        # Next bar closes at 105 > max(100, 100, 100) → BUY
        sigs = strat.on_event(_event(_bar(1, 18, close=105.0)), state)
        assert len(sigs) == 1
        assert sigs[0].side == Side.BUY
        assert "donchian" in sigs[0].reason

    def test_break_below_low_emits_sell(self) -> None:
        strat = DonchianBreakout({"n": 3})
        state = StrategyState()
        for m in range(3):
            strat.on_event(_event(_bar(1, 15 + m, close=100.0)), state)
        sigs = strat.on_event(_event(_bar(1, 18, close=95.0)), state)
        assert len(sigs) == 1
        assert sigs[0].side == Side.SELL

    def test_no_signal_when_inside_range(self) -> None:
        strat = DonchianBreakout({"n": 3})
        state = StrategyState()
        for close in (100, 105, 95):
            strat.on_event(_event(_bar(1, 15, close=close)), state)
        # Current max=105, min=95; close=100 is neither above nor below.
        sigs = strat.on_event(_event(_bar(1, 18, close=100.0)), state)
        assert sigs == []

    def test_no_double_fire_on_consecutive_breakouts(self) -> None:
        """Once long, another above-band close must not re-emit BUY."""
        strat = DonchianBreakout({"n": 3})
        state = StrategyState()
        for close in (100, 100, 100):
            strat.on_event(_event(_bar(1, 15, close=close)), state)
        first = strat.on_event(_event(_bar(1, 18, close=105.0)), state)
        assert len(first) == 1
        # Still above the rolling max — must stay silent (position=long already).
        second = strat.on_event(_event(_bar(1, 19, close=106.0)), state)
        assert second == []

    def test_long_to_short_transition_fires(self) -> None:
        """After a BUY, a clean break below fires a SELL (not a silent flip)."""
        strat = DonchianBreakout({"n": 3})
        state = StrategyState()
        for close in (100, 100, 100):
            strat.on_event(_event(_bar(1, 15, close=close)), state)
        strat.on_event(_event(_bar(1, 18, close=110.0)), state)  # BUY
        strat.on_event(_event(_bar(1, 19, close=109.0)), state)  # inside
        strat.on_event(_event(_bar(1, 20, close=108.0)), state)  # inside
        # Window now [110, 109, 108], min=108. Close 90 < 108 → SELL.
        sigs = strat.on_event(_event(_bar(1, 21, close=90.0)), state)
        assert len(sigs) == 1
        assert sigs[0].side == Side.SELL


# ────────────────────────────────────────────────────────────────────────
# ClosePreviousMomentum
# ────────────────────────────────────────────────────────────────────────


class TestClosePreviousMomentumValidation:
    def test_zero_k_rejected(self) -> None:
        with pytest.raises(StrategyError, match="positive"):
            ClosePreviousMomentum({"k": 0})

    def test_default_k_applied(self) -> None:
        assert ClosePreviousMomentum().k == 3


class TestClosePreviousMomentumLogic:
    def test_first_bar_is_silent(self) -> None:
        strat = ClosePreviousMomentum({"k": 2})
        state = StrategyState()
        sigs = strat.on_event(_event(_bar(1, 15, close=100.0)), state)
        assert sigs == []

    def test_k_consecutive_up_closes_emit_buy(self) -> None:
        strat = ClosePreviousMomentum({"k": 2})
        state = StrategyState()
        strat.on_event(_event(_bar(1, 15, close=100.0)), state)  # seed
        strat.on_event(_event(_bar(1, 16, close=101.0)), state)  # up streak = 1
        sigs = strat.on_event(_event(_bar(1, 17, close=102.0)), state)  # up streak = 2 → BUY
        assert len(sigs) == 1
        assert sigs[0].side == Side.BUY
        assert "up-closes" in sigs[0].reason

    def test_k_consecutive_down_closes_emit_sell(self) -> None:
        strat = ClosePreviousMomentum({"k": 2})
        state = StrategyState()
        strat.on_event(_event(_bar(1, 15, close=100.0)), state)
        strat.on_event(_event(_bar(1, 16, close=99.0)), state)
        sigs = strat.on_event(_event(_bar(1, 17, close=98.0)), state)
        assert len(sigs) == 1
        assert sigs[0].side == Side.SELL

    def test_flat_bar_resets_streak(self) -> None:
        strat = ClosePreviousMomentum({"k": 2})
        state = StrategyState()
        strat.on_event(_event(_bar(1, 15, close=100.0)), state)
        strat.on_event(_event(_bar(1, 16, close=101.0)), state)
        # Flat close — must reset up_streak.
        strat.on_event(_event(_bar(1, 17, close=101.0)), state)
        sigs = strat.on_event(_event(_bar(1, 18, close=102.0)), state)
        # Streak now at 1 (only the last up-move). No BUY.
        assert sigs == []

    def test_reversal_resets_opposite_streak(self) -> None:
        strat = ClosePreviousMomentum({"k": 2})
        state = StrategyState()
        strat.on_event(_event(_bar(1, 15, close=100.0)), state)
        strat.on_event(_event(_bar(1, 16, close=101.0)), state)  # up 1
        strat.on_event(_event(_bar(1, 17, close=100.5)), state)  # down 1, up resets
        sigs = strat.on_event(_event(_bar(1, 18, close=100.0)), state)  # down 2 → SELL
        assert len(sigs) == 1
        assert sigs[0].side == Side.SELL

    def test_no_double_fire_once_long(self) -> None:
        strat = ClosePreviousMomentum({"k": 2})
        state = StrategyState()
        strat.on_event(_event(_bar(1, 15, close=100.0)), state)
        strat.on_event(_event(_bar(1, 16, close=101.0)), state)
        strat.on_event(_event(_bar(1, 17, close=102.0)), state)  # BUY
        sigs = strat.on_event(_event(_bar(1, 18, close=103.0)), state)  # extends streak
        # Already long — extending the streak does NOT refire.
        assert sigs == []


# ────────────────────────────────────────────────────────────────────────
# OpeningRangeBreakout
# ────────────────────────────────────────────────────────────────────────


class TestOpeningRangeBreakoutValidation:
    def test_zero_range_bars_rejected(self) -> None:
        with pytest.raises(StrategyError, match="positive"):
            OpeningRangeBreakout({"range_bars": 0})

    def test_default_range_bars_applied(self) -> None:
        assert OpeningRangeBreakout().range_bars == 15


class TestOpeningRangeBreakoutLogic:
    def _day_one_bars(self, closes: list[float]) -> list[tuple[int, CandleBar]]:
        """Build a list of bars within day 1 with the given close sequence.

        Each bar uses high=close+2 and low=close-2 so the range
        expansion picks up meaningful highs/lows.
        """
        return [(i, _bar(1, 15 + i, close=c, high=c + 2, low=c - 2)) for i, c in enumerate(closes)]

    def test_warmup_bars_emit_nothing_and_record_range(self) -> None:
        strat = OpeningRangeBreakout({"range_bars": 3})
        state = StrategyState()
        # 3-bar opening range
        for _, bar in self._day_one_bars([100, 102, 98]):
            sigs = strat.on_event(_event(bar), state)
            assert sigs == []
        assert state.scratch["range_high"] == 104.0  # max(100+2, 102+2, 98+2) = 104
        assert state.scratch["range_low"] == 96.0  # min(100-2, 102-2, 98-2) = 96

    def test_close_above_range_high_emits_buy(self) -> None:
        strat = OpeningRangeBreakout({"range_bars": 3})
        state = StrategyState()
        for _, bar in self._day_one_bars([100, 100, 100]):
            strat.on_event(_event(bar), state)
        # Range high = 101 (= 100+1 default). Close 110 > 101 → BUY.
        sigs = strat.on_event(_event(_bar(1, 18, close=110.0)), state)
        assert len(sigs) == 1
        assert sigs[0].side == Side.BUY
        assert "orb" in sigs[0].reason

    def test_close_below_range_low_emits_sell(self) -> None:
        strat = OpeningRangeBreakout({"range_bars": 3})
        state = StrategyState()
        for _, bar in self._day_one_bars([100, 100, 100]):
            strat.on_event(_event(bar), state)
        sigs = strat.on_event(_event(_bar(1, 18, close=90.0)), state)
        assert len(sigs) == 1
        assert sigs[0].side == Side.SELL

    def test_close_inside_range_is_silent(self) -> None:
        strat = OpeningRangeBreakout({"range_bars": 3})
        state = StrategyState()
        for _, bar in self._day_one_bars([100, 100, 100]):
            strat.on_event(_event(bar), state)
        # Range [99, 101]. Close 100 is strictly inside.
        sigs = strat.on_event(_event(_bar(1, 18, close=100.0)), state)
        assert sigs == []

    def test_new_day_resets_range(self) -> None:
        strat = OpeningRangeBreakout({"range_bars": 2})
        state = StrategyState()
        # Day 1 warmup
        strat.on_event(_event(_bar(1, 15, close=100.0, high=105, low=95)), state)
        strat.on_event(_event(_bar(1, 16, close=101.0, high=104, low=96)), state)
        assert state.scratch["range_high"] == 105.0

        # Day 2 first bar — range must reset.
        strat.on_event(_event(_bar(2, 15, close=200.0, high=201, low=199)), state)
        assert state.scratch["day"].day == 2
        assert state.scratch["range_high"] == 201.0
        assert state.scratch["range_low"] == 199.0
        assert state.scratch["bars_this_day"] == 1

    def test_no_double_fire_on_subsequent_breakouts(self) -> None:
        strat = OpeningRangeBreakout({"range_bars": 2})
        state = StrategyState()
        strat.on_event(_event(_bar(1, 15, close=100.0)), state)
        strat.on_event(_event(_bar(1, 16, close=100.0)), state)
        first = strat.on_event(_event(_bar(1, 17, close=110.0)), state)  # BUY
        assert len(first) == 1
        second = strat.on_event(_event(_bar(1, 18, close=115.0)), state)  # still above
        assert second == []


# ────────────────────────────────────────────────────────────────────────
# Registry integration
# ────────────────────────────────────────────────────────────────────────


class TestLibraryRegistryIntegration:
    """Every library strategy must be discoverable via the default registry."""

    def test_all_four_strategies_are_registered(self) -> None:
        from brutex.strategies import default_registry

        ids = set(default_registry.ids())
        assert "buy_and_hold" in ids
        assert "donchian_breakout" in ids
        assert "close_prev_momentum" in ids
        assert "opening_range_breakout" in ids

    def test_registry_lookup_returns_the_class(self) -> None:
        from brutex.strategies import default_registry

        assert default_registry.get("buy_and_hold") is BuyAndHold
        assert default_registry.get("donchian_breakout") is DonchianBreakout


# ────────────────────────────────────────────────────────────────────────
# Chronological invariant — signals never reference future timestamps
# ────────────────────────────────────────────────────────────────────────


class TestNoLookAhead:
    """Every library strategy must stamp signals with the current event's ts."""

    def test_buy_and_hold_uses_current_ts(self) -> None:
        strat = BuyAndHold()
        state = StrategyState()
        bar_ts = datetime(2024, 1, 1, 9, 15, tzinfo=UTC)
        sigs = strat.on_event(FeedEvent(symbol="NSE-NIFTY", bar=_bar(1, 15, close=100.0)), state)
        assert sigs[0].timestamp == bar_ts

    def test_donchian_uses_current_ts(self) -> None:
        strat = DonchianBreakout({"n": 2})
        state = StrategyState()
        strat.on_event(_event(_bar(1, 15, close=100.0)), state)
        strat.on_event(_event(_bar(1, 16, close=100.0)), state)
        sigs = strat.on_event(_event(_bar(1, 17, close=110.0)), state)
        assert sigs[0].timestamp == datetime(2024, 1, 1, 9, 17, tzinfo=UTC)

    def test_momentum_uses_current_ts(self) -> None:
        strat = ClosePreviousMomentum({"k": 2})
        state = StrategyState()
        strat.on_event(_event(_bar(1, 15, close=100.0)), state)
        strat.on_event(_event(_bar(1, 16, close=101.0)), state)
        sigs = strat.on_event(_event(_bar(1, 17, close=102.0)), state)
        assert sigs[0].timestamp == datetime(2024, 1, 1, 9, 17, tzinfo=UTC)

    def test_orb_uses_current_ts(self) -> None:
        strat = OpeningRangeBreakout({"range_bars": 1})
        state = StrategyState()
        strat.on_event(_event(_bar(1, 15, close=100.0)), state)
        sigs = strat.on_event(_event(_bar(1, 16, close=110.0)), state)
        assert sigs[0].timestamp == datetime(2024, 1, 1, 9, 16, tzinfo=UTC)


# ────────────────────────────────────────────────────────────────────────
# Determinism — state.reset() produces identical replays
# ────────────────────────────────────────────────────────────────────────


class TestDeterministicReplay:
    """FR-STR-003: replay(A) == replay(A) after state.reset()."""

    @staticmethod
    def _run(
        strat: object,
        bars: list[CandleBar],
        state: StrategyState,
    ) -> list[tuple[Side, datetime]]:
        """Drive *strat* through *bars* and return the (side, ts) trace."""
        out: list[tuple[Side, datetime]] = []
        for bar in bars:
            sigs = strat.on_event(_event(bar), state)  # type: ignore[attr-defined]
            out.extend((s.side, s.timestamp) for s in sigs)
        return out

    def test_donchian_replay_is_identical(self) -> None:
        strat = DonchianBreakout({"n": 3})
        bars = [_bar(1, 15 + i, close=100 + i) for i in range(10)]
        s1 = StrategyState()
        trace1 = self._run(strat, bars, s1)
        s1.reset()
        # Reconstruct: the same strategy instance + reset state must
        # produce an identical trace.
        trace2 = self._run(strat, bars, s1)
        assert trace1 == trace2

    def test_momentum_replay_is_identical(self) -> None:
        strat = ClosePreviousMomentum({"k": 2})
        # Alternating up-down-up sequences. Spread timestamps across
        # successive minutes so every :class:`CandleBar` stays valid.
        closes = [100, 101, 102, 103, 100, 99, 98, 99, 100, 101, 102]
        bars = [_bar(1, 15 + i, close=c) for i, c in enumerate(closes)]
        s1 = StrategyState()
        trace1 = self._run(strat, bars, s1)
        s1.reset()
        trace2 = self._run(strat, bars, s1)
        assert trace1 == trace2


# ────────────────────────────────────────────────────────────────────────
# Unused helper silencer
# ────────────────────────────────────────────────────────────────────────
# ``timedelta`` is imported for readability in a few tests; the
# linter sees it only under certain call paths.
_ = timedelta
