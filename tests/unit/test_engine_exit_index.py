"""Unit tests for the exit-bar index helpers (engine/exit_index.py).

DEC-SWEEP-EXIT-INDEX-FASTPATH-001. Covers the COMMON ``build_ts_index`` map
(including the last-wins duplicate semantics the three consolidated
``sweep_driver`` call sites relied on), the producer-side
``bars_strictly_increasing`` certificate, and the consumer-side
``exit_indices_consistent`` validation belt — every branch, for the strict
100% line+branch gate.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from brutex.data.models import CandleBar
from brutex.engine.exit_index import (
    bars_strictly_increasing,
    build_ts_index,
    exit_indices_consistent,
)
from brutex.metrics.types import ClosedTrade
from brutex.strategies.types import Side

_OPEN_UTC = datetime(2024, 1, 2, 3, 46, tzinfo=UTC)  # 09:16 IST


def _bar(minute_offset: int) -> CandleBar:
    price = Decimal("100.00")
    return CandleBar(
        timestamp=_OPEN_UTC + timedelta(minutes=minute_offset),
        open=price,
        high=price + Decimal("1.00"),
        low=price - Decimal("1.00"),
        close=price,
        volume=10,
        open_interest=None,
    )


def _bars(n: int = 5) -> list[CandleBar]:
    return [_bar(i) for i in range(n)]


def _trade(bars: list[CandleBar], exit_idx: int, pnl: int = 100) -> ClosedTrade:
    return ClosedTrade(
        pnl_paisa=pnl,
        mfe_paisa=max(pnl, 0),
        mae_paisa=max(-pnl, 0),
        holding_bars=1,
        entry_ts=bars[0].timestamp,
        exit_ts=bars[exit_idx].timestamp,
        direction=Side.BUY,
    )


# ── build_ts_index ───────────────────────────────────────────────────────


class TestBuildTsIndex:
    def test_maps_every_timestamp_to_its_index(self) -> None:
        bars = _bars()
        index = build_ts_index(bars)
        assert index == {bar.timestamp: i for i, bar in enumerate(bars)}

    def test_empty_window_is_an_empty_map(self) -> None:
        assert build_ts_index([]) == {}

    def test_duplicate_timestamp_is_last_wins(self) -> None:
        """The consolidated sweep_driver semantics: the LAST duplicate's index
        wins — byte-identical to the historical inline comprehensions."""
        bars = [_bar(0), _bar(1), _bar(1)]  # bars[1] and bars[2] share a ts
        index = build_ts_index(bars)
        assert index[bars[1].timestamp] == 2
        assert len(index) == 2


# ── bars_strictly_increasing ─────────────────────────────────────────────


class TestBarsStrictlyIncreasing:
    def test_ordered_unique_window_is_certified(self) -> None:
        assert bars_strictly_increasing(_bars()) is True

    def test_empty_and_single_bar_are_trivially_certified(self) -> None:
        assert bars_strictly_increasing([]) is True
        assert bars_strictly_increasing(_bars(1)) is True

    def test_duplicate_timestamp_fails_the_certificate(self) -> None:
        assert bars_strictly_increasing([_bar(0), _bar(1), _bar(1)]) is False

    def test_reordered_timestamp_fails_the_certificate(self) -> None:
        assert bars_strictly_increasing([_bar(0), _bar(2), _bar(1)]) is False


# ── exit_indices_consistent ──────────────────────────────────────────────


class TestExitIndicesConsistent:
    def test_true_indices_are_consistent(self) -> None:
        bars = _bars()
        trades = [_trade(bars, 1), _trade(bars, 3)]
        assert exit_indices_consistent(bars, trades, [1, 3]) is True

    def test_empty_streams_are_consistent(self) -> None:
        assert exit_indices_consistent(_bars(), [], []) is True

    def test_length_mismatch_is_inconsistent(self) -> None:
        bars = _bars()
        trades = [_trade(bars, 1), _trade(bars, 3)]
        assert exit_indices_consistent(bars, trades, [1]) is False
        assert exit_indices_consistent(bars, trades, [1, 3, 3]) is False

    def test_out_of_range_index_is_inconsistent(self) -> None:
        bars = _bars()
        trades = [_trade(bars, 1)]
        assert exit_indices_consistent(bars, trades, [len(bars)]) is False

    def test_negative_index_is_inconsistent(self) -> None:
        """A negative index would silently WRAP under Python indexing — the
        belt must refuse it even when the wrapped bar's ts happens to match."""
        bars = _bars()
        trades = [_trade(bars, len(bars) - 1)]
        # bars[-1] IS the exit bar, so a naive ts-only check would pass.
        assert bars[-1].timestamp == trades[0].exit_ts
        assert exit_indices_consistent(bars, trades, [-1]) is False

    def test_wrong_bar_for_the_exit_ts_is_inconsistent(self) -> None:
        bars = _bars()
        trades = [_trade(bars, 1)]
        assert exit_indices_consistent(bars, trades, [2]) is False
