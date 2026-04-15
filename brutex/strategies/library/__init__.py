"""Concrete reference strategies for BruteX (``FR-STR-004``).

This package holds the working-example strategies the engine and the
brute-force sweep orchestrator are driven against end-to-end. Each
strategy is:

* A plain class satisfying :class:`brutex.strategies.protocol.Strategy`
* Stateless except for the engine-managed :class:`StrategyState`
  (``FR-STR-003`` \u2014 deterministic replay)
* Registered on :data:`brutex.strategies.default_registry` at import
  time so a user can discover it via the CLI without a dedicated
  import step
* Tested by a unit-level test of pure state-machine logic **and** an
  integration test driving the strategy through a real
  :class:`ReplayFeed`

## Initial lineup (P3 kickoff)

Four single-symbol strategies that exercise the runner + sweep
pipeline without pulling in the yet-to-land indicator wrappers:

* :class:`~brutex.strategies.library.buy_and_hold.BuyAndHold` \u2014
  trivial baseline: emit one BUY on the first post-warmup bar,
  never exit.
* :class:`~brutex.strategies.library.donchian_breakout.DonchianBreakout`
  \u2014 N-bar Donchian breakout using a deque. Buys on a close above
  the rolling N-bar high, sells on a close below the rolling N-bar
  low. Uses stdlib ``max`` / ``min`` only \u2014 no indicator formula
  reimplementation (FR-MATH-001 compliant).
* :class:`~brutex.strategies.library.close_previous_momentum.ClosePreviousMomentum`
  \u2014 K consecutive bars where ``close > previous_close`` triggers
  a BUY; the reverse triggers a SELL. Pure bar comparison.
* :class:`~brutex.strategies.library.opening_range_breakout.OpeningRangeBreakout`
  \u2014 session-aware. First N bars of each UTC day define the
  opening range; a later close above the range high emits a BUY,
  below the range low emits a SELL. Tracks UTC-date rollover via
  ``state.scratch``.

Options-leg strategies (Iron Condor, Straddle, Strangle, etc.) from
FR-STR-004 land in a follow-up commit once the multi-symbol
coordination pattern is worked out \u2014 :class:`ReplayFeed` yields one
bar per event, so a multi-leg position needs to buffer bars by
timestamp before acting.
"""

from __future__ import annotations

from brutex.strategies.library.buy_and_hold import BuyAndHold
from brutex.strategies.library.close_previous_momentum import ClosePreviousMomentum
from brutex.strategies.library.donchian_breakout import DonchianBreakout
from brutex.strategies.library.opening_range_breakout import OpeningRangeBreakout

__all__ = [
    "BuyAndHold",
    "ClosePreviousMomentum",
    "DonchianBreakout",
    "OpeningRangeBreakout",
]
