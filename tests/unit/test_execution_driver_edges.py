"""Edge-case unit tests for :class:`brutex.execution.driver.BacktestDriver`.

The integration suite (`tests/integration/test_execution_driver.py`) drives the
happy path over a real lake. This module pins the **guard + error branches** with
light fakes (cast to the declared protocol/types) so no Parquet/DuckDB lake is
needed: the interval-mismatch contract, the two property getters, the
out-of-scope-event skip, `ExecutionError` propagation, and the three
`_validate_signal` contract violations (non-`Signal`, out-of-scope symbol,
look-ahead). Closes the omitted-module coverage gap for `execution/driver.py`
(DEC-COVERAGE-DRIVER-001).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import cast

import pytest
from brutex.data.models import CandleBar
from brutex.execution.driver import BacktestDriver
from brutex.execution.errors import ExecutionError
from brutex.execution.simulator import ExecutionSimulator
from brutex.execution.types import Fill
from brutex.feed.replay import FeedEvent, ReplayFeed
from brutex.strategies.errors import StrategyContractError
from brutex.strategies.protocol import Strategy, StrategyMeta
from brutex.strategies.types import Side, Signal, StrategyState

_SYMBOL = "NSE-NIFTY"
_TS = datetime(2024, 1, 1, 9, 15, 0, tzinfo=UTC)


def _bar(close: float, *, ts: datetime = _TS) -> CandleBar:
    """A minimal valid candle at *ts*."""
    return CandleBar(
        timestamp=ts,
        open=Decimal(str(close)),
        high=Decimal(str(close + 1)),
        low=Decimal(str(close - 1)),
        close=Decimal(str(close)),
        volume=100,
        open_interest=0,
    )


def _meta(*, interval: str = "1minute", symbols: tuple[str, ...] = (_SYMBOL,)) -> StrategyMeta:
    return StrategyMeta(
        strategy_id="fake",
        version="1.0",
        symbols=symbols,
        interval=interval,
        lookback_bars=0,
    )


class _FakeStrategy:
    """Strategy double: fixed META, emits a caller-supplied signal list per event."""

    def __init__(self, meta: StrategyMeta, emit: Sequence[object] | None = None) -> None:
        self.META = meta
        self._emit: Sequence[object] = emit if emit is not None else []

    def on_event(self, event: object, state: object) -> list[object]:
        del event, state
        return list(self._emit)


class _FakeFeed:
    """Feed double: an explicit event list + interval + a no-op look-ahead guard."""

    def __init__(self, interval: str, events: list[FeedEvent]) -> None:
        self.interval = interval
        self._events = events
        self.lookahead_seen: list[datetime] = []

    def __iter__(self) -> object:
        return iter(self._events)

    def ensure_no_lookahead(self, ts: datetime) -> None:
        self.lookahead_seen.append(ts)


class _FakeSimulator:
    """Simulator double: optional ExecutionError on execute; records marks."""

    def __init__(self, *, execute_raises: bool = False) -> None:
        self._execute_raises = execute_raises
        self.fills: list[Fill] = []
        self.equity_curve: list[object] = []
        self.positions: dict[str, object] = {}
        self.cash = Decimal("0")
        self.marks: list[str] = []

    def execute(self, signal: object, bar: object) -> Fill | None:
        del signal, bar
        if self._execute_raises:
            raise ExecutionError("synthetic execution failure")
        return None

    def mark_to_market(self, *, symbol: str, price: object, timestamp: object) -> None:
        del price, timestamp
        self.marks.append(symbol)


def _driver(
    strategy: _FakeStrategy,
    feed: _FakeFeed,
    simulator: _FakeSimulator,
) -> BacktestDriver:
    return BacktestDriver(
        strategy=cast(Strategy, strategy),
        feed=cast(ReplayFeed, feed),
        simulator=cast(ExecutionSimulator, simulator),
    )


def test_interval_mismatch_raises() -> None:
    """`__init__` rejects a strategy whose interval differs from the feed (line 86)."""
    strat = _FakeStrategy(_meta(interval="5minute"))
    feed = _FakeFeed("1minute", [])
    with pytest.raises(StrategyContractError):
        _driver(strat, feed, _FakeSimulator())


def test_simulator_and_state_properties() -> None:
    """The `simulator` and `state` getters return the wired instances (lines 96, 101)."""
    sim = _FakeSimulator()
    state = StrategyState()
    driver = BacktestDriver(
        strategy=cast(Strategy, _FakeStrategy(_meta())),
        feed=cast(ReplayFeed, _FakeFeed("1minute", [])),
        simulator=cast(ExecutionSimulator, sim),
        state=state,
    )
    assert driver.simulator is cast(ExecutionSimulator, sim)
    assert driver.state is state


def test_out_of_scope_event_symbol_is_skipped() -> None:
    """An event whose symbol is not declared is skipped — no on_event, no mark (line 121)."""
    emitted = [Signal(symbol=_SYMBOL, side=Side.BUY, timestamp=_TS)]
    strat = _FakeStrategy(_meta(), emit=emitted)
    feed = _FakeFeed("1minute", [FeedEvent(symbol="NSE-OTHER", bar=_bar(100))])
    sim = _FakeSimulator()
    result = _driver(strat, feed, sim).run()
    assert sim.marks == []  # the out-of-scope event never reached mark_to_market
    assert not result.fills  # nothing traded


def test_execution_error_propagates() -> None:
    """An ExecutionError from the simulator propagates out of run() (lines 159-160)."""
    emitted = [Signal(symbol=_SYMBOL, side=Side.BUY, timestamp=_TS)]
    strat = _FakeStrategy(_meta(), emit=emitted)
    feed = _FakeFeed("1minute", [FeedEvent(symbol=_SYMBOL, bar=_bar(100))])
    sim = _FakeSimulator(execute_raises=True)
    with pytest.raises(ExecutionError):
        _driver(strat, feed, sim).run()


def test_non_signal_emission_raises() -> None:
    """A non-Signal emission trips the contract check (line 170)."""
    strat = _FakeStrategy(_meta(), emit=[object()])
    feed = _FakeFeed("1minute", [FeedEvent(symbol=_SYMBOL, bar=_bar(100))])
    with pytest.raises(StrategyContractError):
        _driver(strat, feed, _FakeSimulator()).run()


def test_out_of_scope_signal_symbol_raises() -> None:
    """A signal for an undeclared symbol trips the contract check (line 174)."""
    emitted = [Signal(symbol="NSE-OTHER", side=Side.BUY, timestamp=_TS)]
    strat = _FakeStrategy(_meta(), emit=emitted)
    feed = _FakeFeed("1minute", [FeedEvent(symbol=_SYMBOL, bar=_bar(100))])
    with pytest.raises(StrategyContractError):
        _driver(strat, feed, _FakeSimulator()).run()


def test_lookahead_signal_raises() -> None:
    """A signal timestamped after the event is look-ahead and is rejected (line 180)."""
    future = datetime(2024, 1, 1, 9, 16, 0, tzinfo=UTC)
    emitted = [Signal(symbol=_SYMBOL, side=Side.BUY, timestamp=future)]
    strat = _FakeStrategy(_meta(), emit=emitted)
    feed = _FakeFeed("1minute", [FeedEvent(symbol=_SYMBOL, bar=_bar(100))])
    with pytest.raises(StrategyContractError):
        _driver(strat, feed, _FakeSimulator()).run()
