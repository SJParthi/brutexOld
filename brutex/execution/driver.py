"""Backtest driver \u2014 wires feed + strategy + execution simulator.

:class:`BacktestDriver` is the fully-integrated backtest loop. For
each :class:`FeedEvent` yielded by a :class:`ReplayFeed`:

1. Update the :class:`StrategyState` (per-bar price + bar count),
   mirroring the :class:`StrategyRunner` lifecycle.
2. Ask the :class:`Strategy` for signals via ``on_event``.
3. Forward every non-``HOLD`` signal to
   :meth:`ExecutionSimulator.execute`, collecting the resulting
   :class:`Fill` records.
4. Mark-to-market the event's symbol against the bar close and
   record an :class:`EquityPoint`.

At the end of the run, the driver returns a frozen
:class:`BacktestResult` that downstream consumers
(:class:`ComboSweep`, dashboard, JSON serialiser) can read without
any further hooks.

Symbol-whitelist, lookback gating, and signal contract enforcement
(non-``Signal`` emission, out-of-scope symbol, future timestamp)
all happen inside :class:`StrategyRunner`'s validation helpers,
which this driver reuses \u2014 the driver itself adds only the
execution-simulator wiring.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import TYPE_CHECKING

from brutex.execution.errors import ExecutionError
from brutex.execution.simulator import ExecutionSimulator
from brutex.execution.types import (
    BacktestResult,
    Fill,
    Position,
)
from brutex.strategies.errors import StrategyContractError
from brutex.strategies.types import Signal, StrategyState

if TYPE_CHECKING:  # pragma: no cover
    from brutex.feed.replay import ReplayFeed
    from brutex.strategies.protocol import Strategy


class BacktestDriver:
    """Drive a strategy + simulator through a replay feed.

    Parameters
    ----------
    strategy
        Concrete strategy instance. Must declare the same interval
        as the feed, or :class:`StrategyContractError` fires at
        :meth:`run` time.
    feed
        Prepared :class:`ReplayFeed`. Caller owns the catalog
        handle and must keep it open for the duration of
        :meth:`run`.
    simulator
        Built :class:`ExecutionSimulator`. The driver does not
        reset it between calls \u2014 construct a fresh simulator
        instance per backtest for clean semantics.
    state
        Optional pre-built :class:`StrategyState`. Reset before the
        loop starts \u2014 FR-STR-003 deterministic replay still holds.
    """

    def __init__(
        self,
        *,
        strategy: Strategy,
        feed: ReplayFeed,
        simulator: ExecutionSimulator,
        state: StrategyState | None = None,
    ) -> None:
        self._strategy = strategy
        self._feed = feed
        self._simulator = simulator
        self._state = state if state is not None else StrategyState()

        meta = strategy.META
        self._allowed_symbols = frozenset(meta.symbols)
        if meta.interval != feed.interval:
            raise StrategyContractError(
                f"strategy {meta.strategy_id!r} declares interval "
                f"{meta.interval!r} but feed is at {feed.interval!r}"
            )

    # ── Public API ────────────────────────────────────────────────

    @property
    def simulator(self) -> ExecutionSimulator:
        """Return the underlying :class:`ExecutionSimulator`."""
        return self._simulator

    @property
    def state(self) -> StrategyState:
        """Return the :class:`StrategyState` held by this driver."""
        return self._state

    def run(self) -> BacktestResult:
        """Drive the feed to completion and return a frozen :class:`BacktestResult`.

        Raises
        ------
        ExecutionError
            Propagated from :meth:`ExecutionSimulator.execute` on
            lot-size / freeze / funds violations.
        StrategyContractError
            Propagated from signal validation (non-``Signal``
            emission, out-of-scope symbol, future timestamp).
        """
        self._state.reset()
        meta = self._strategy.META
        lookback = meta.lookback_bars

        for event in self._feed:
            if event.symbol not in self._allowed_symbols:
                continue

            # Update state first \u2014 same contract as StrategyRunner.
            self._state.last_prices[event.symbol] = event.bar.close
            self._state.bar_count += 1

            if self._state.bar_count > lookback:
                emitted = self._strategy.on_event(event, self._state)
                for signal in emitted:
                    self._validate_signal(signal, event_ts=event.timestamp)
                    self._execute(signal, event)

            # Mark-to-market the event's symbol after any trading
            # decisions so the equity point reflects the post-fill
            # state of the book.
            self._simulator.mark_to_market(
                symbol=event.symbol,
                price=event.bar.close,
                timestamp=event.timestamp,
            )

        return self._build_result()

    # ── Private ────────────────────────────────────────────────────

    def _execute(self, signal: object, event: object) -> Fill | None:
        """Forward a validated signal to the simulator.

        Wrapped so :meth:`run` can stay readable. Any
        :class:`ExecutionError` from the simulator propagates \u2014
        the driver does not attempt to recover from funds /
        lot-size violations at backtest time.
        """
        if not isinstance(signal, Signal):  # pragma: no cover \u2014 guarded earlier
            return None
        bar = event.bar  # type: ignore[attr-defined]
        try:
            return self._simulator.execute(signal, bar)
        except ExecutionError:
            raise

    def _validate_signal(self, signal: object, *, event_ts: object) -> None:
        """Run the same signal-contract checks as :class:`StrategyRunner`.

        Duplicated intentionally rather than coupling to the
        runner's private method \u2014 the contract is small and
        spelling it out here keeps the driver standalone.
        """
        if not isinstance(signal, Signal):
            raise StrategyContractError(
                f"strategy emitted {type(signal).__name__!r}, expected brutex.strategies.Signal"
            )
        if signal.symbol not in self._allowed_symbols:
            raise StrategyContractError(
                f"strategy {self._strategy.META.strategy_id!r} emitted signal "
                f"for symbol {signal.symbol!r} which is not in its declared "
                f"symbols list {sorted(self._allowed_symbols)}"
            )
        if signal.timestamp > event_ts:  # type: ignore[operator]
            raise StrategyContractError(
                f"signal timestamp {signal.timestamp!r} > event ts "
                f"{event_ts!r}: look-ahead forbidden (FR-INT-004)"
            )
        self._feed.ensure_no_lookahead(signal.timestamp)

    def _build_result(self) -> BacktestResult:
        """Assemble the final :class:`BacktestResult` from simulator state."""
        fills = self._simulator.fills
        equity_curve = self._simulator.equity_curve
        sim_positions = self._simulator.positions

        final_positions: dict[str, Position] = {
            symbol: pos
            for symbol, pos in sim_positions.items()
            if pos.qty != 0 or pos.realized_pnl != Decimal("0")
        }

        total_realized = sum(
            (pos.realized_pnl for pos in sim_positions.values()),
            start=Decimal("0"),
        )
        total_costs = sum(
            (fill.costs.total for fill in fills),
            start=Decimal("0"),
        )

        final_cash = self._simulator.cash
        # Use the last equity point's total when available so the
        # reported final_equity matches the tail of the curve
        # exactly (including the last mark-to-market).
        final_equity = equity_curve[-1].total_equity if equity_curve else final_cash

        return BacktestResult(
            fills=fills,
            equity_curve=equity_curve,
            final_positions=_freeze_positions(final_positions),
            final_cash=final_cash,
            final_equity=final_equity,
            total_realized_pnl=total_realized,
            total_costs=total_costs,
        )


def _freeze_positions(positions: Mapping[str, Position]) -> Mapping[str, Position]:
    """Return an immutable view of *positions* for the frozen result.

    The simulator owns the mutable dict; the :class:`BacktestResult`
    needs a read-only snapshot so consumers can't accidentally
    mutate it back into the live simulator state.
    """
    return dict(positions)  # shallow copy \u2014 positions are not frozen themselves
