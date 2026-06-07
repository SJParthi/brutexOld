"""Drive a :class:`~brutex.strategies.Strategy` through a replay feed.

Implements the event-driven execution half of ``FR-STR-001`` and the
deterministic-replay guarantee of ``FR-STR-003``. Wraps a
:class:`~brutex.feed.ReplayFeed` iteration with:

1. **Per-bar state maintenance** — updates ``state.last_prices`` and
   increments ``state.bar_count`` *before* the strategy sees the
   event, so strategies never have to re-derive the current close.
2. **Lookback gating** — skips ``on_event`` until the strategy has
   observed at least :attr:`StrategyMeta.lookback_bars` bars. This
   matches FR-STR-002's "pre-slice data" contract.
3. **Symbol whitelist** — events whose ``event.symbol`` is not in
   :attr:`StrategyMeta.symbols` are filtered out before the strategy
   sees them. Catches wiring bugs early and keeps strategies free of
   defensive ``if event.symbol not in self.symbols: return`` boilerplate.
4. **Signal contract enforcement** — every emitted :class:`Signal`
   is checked against :class:`StrategyMeta`, the feed's current
   timestamp (FR-INT-004 via
   :meth:`~brutex.feed.ReplayFeed.ensure_no_lookahead`), and the
   signal's own timestamp bound. Violations raise
   :class:`~brutex.strategies.errors.StrategyContractError`.
5. **Deterministic reset** — ``state.reset()`` is called at the
   start of every run, so two back-to-back runs over the same feed
   produce bit-identical :class:`StrategyRunResult` values. This is
   the property the FR-STR-003 test pins.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from brutex.feed.lookahead import LookAheadBarrier
from brutex.strategies.errors import StrategyContractError
from brutex.strategies.types import Signal, StrategyState

if TYPE_CHECKING:  # pragma: no cover
    from brutex.feed.replay import ReplayFeed
    from brutex.strategies.protocol import Strategy


@dataclass(frozen=True, slots=True)
class StrategyRunResult:
    """Summary of one strategy replay run.

    Attributes
    ----------
    strategy_id
        ``strategy.META.strategy_id`` at the time of the run.
    bars_observed
        Total number of in-scope (symbol-filtered) bars the strategy
        saw — the final value of ``state.bar_count`` after iteration.
    bars_skipped_lookback
        Number of bars filtered out by the lookback gate. Sum of
        ``bars_skipped_lookback + bars_observed_after_warmup`` equals
        :attr:`bars_observed`.
    signals
        Every :class:`Signal` the strategy emitted, in emission
        order. Emission order ties break by symbol order (see
        :class:`~brutex.feed.replay.ReplayFeed`).
    """

    strategy_id: str
    bars_observed: int
    bars_skipped_lookback: int
    signals: tuple[Signal, ...]


class StrategyRunner:
    """Drive a strategy through a :class:`ReplayFeed`.

    Parameters
    ----------
    strategy
        Concrete strategy instance. Must implement the
        :class:`~brutex.strategies.protocol.Strategy` protocol.
    feed
        Prepared :class:`~brutex.feed.ReplayFeed`. The runner does
        NOT take ownership — the caller is responsible for holding
        the catalog handle open for the duration of ``run``.
    state
        Optional pre-built :class:`StrategyState`. When omitted, the
        runner creates a fresh one. Either way,
        :meth:`StrategyState.reset` is called before iteration starts.
    barrier
        Optional injected :class:`~brutex.feed.lookahead.LookAheadBarrier`
        — the runtime look-ahead enforcer (Property **P-06**). When
        omitted, the runner builds a default ``record=False`` barrier so
        the guard is **active on every run by default** (the AUTOMATIC
        half of the manual⇄automatic duality) at O(1) memory. Inject a
        ``record=True`` barrier (e.g. via the ``brutex verify-lookahead``
        manual recheck) to capture the touched-index trail.
    """

    def __init__(
        self,
        *,
        strategy: Strategy,
        feed: ReplayFeed,
        state: StrategyState | None = None,
        barrier: LookAheadBarrier | None = None,
    ) -> None:
        self._strategy = strategy
        self._feed = feed
        self._state = state if state is not None else StrategyState()
        self._barrier = barrier if barrier is not None else LookAheadBarrier(record=False)

        meta = strategy.META
        self._allowed_symbols = frozenset(meta.symbols)
        if meta.interval != feed.interval:
            raise StrategyContractError(
                f"strategy {meta.strategy_id!r} declares interval "
                f"{meta.interval!r} but feed is at {feed.interval!r}"
            )

    # ── Public API ────────────────────────────────────────────────

    @property
    def state(self) -> StrategyState:
        """Return the (post-reset, possibly mid-run) :class:`StrategyState`."""
        return self._state

    @property
    def barrier(self) -> LookAheadBarrier:
        """Return the runtime look-ahead barrier (Property **P-06**)."""
        return self._barrier

    def run(self) -> StrategyRunResult:
        """Drive the feed through the strategy and return a summary.

        The method is **idempotent per instance** — calling ``run``
        twice in a row produces bit-identical results because
        ``state.reset()`` is called at the top of every invocation.
        Callers that want to preserve intermediate state should
        construct a fresh :class:`StrategyRunner` instead.
        """
        self._state.reset()
        self._barrier.reset()
        meta = self._strategy.META
        lookback = meta.lookback_bars

        signals: list[Signal] = []
        bars_skipped_lookback = 0

        for step, event in enumerate(self._feed):
            # Advance the runtime look-ahead barrier (Property P-06) to the
            # bar the feed just yielded, BEFORE any symbol filtering — the
            # cursor tracks every consumed feed position.
            self._barrier.advance_to(step)
            if event.symbol not in self._allowed_symbols:
                continue

            # Route the bar through the barrier: it asserts the index the
            # strategy is about to see is ≤ the current cursor (never the
            # future) and returns the same bar unchanged.
            bar = self._barrier.guard(step, event.bar)

            # Update state BEFORE invoking the strategy — FR-STR-003
            # pins that strategies only see state that already
            # reflects the current bar.
            self._state.last_prices[event.symbol] = bar.close
            self._state.bar_count += 1

            if self._state.bar_count <= lookback:
                bars_skipped_lookback += 1
                continue

            emitted = self._strategy.on_event(event, self._state)
            for signal in emitted:
                self._validate_signal(signal, event_ts=event.timestamp)
                signals.append(signal)

        return StrategyRunResult(
            strategy_id=meta.strategy_id,
            bars_observed=self._state.bar_count,
            bars_skipped_lookback=bars_skipped_lookback,
            signals=tuple(signals),
        )

    # ── Private ────────────────────────────────────────────────────

    def _validate_signal(self, signal: object, *, event_ts: object) -> None:
        """Assert a strategy-emitted value satisfies the :class:`Signal` contract.

        Called once per emitted signal. Raises
        :class:`StrategyContractError` on any violation — the runner
        never silently swallows a bad signal.
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
        # Also check against the feed's current cursor — catches
        # strategies that hold references to old events and misfire.
        self._feed.ensure_no_lookahead(signal.timestamp)
