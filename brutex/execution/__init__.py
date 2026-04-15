"""BruteX execution simulator (``FR-EXEC-*``) \u2014 P4 kickoff scope.

Turns a :class:`~brutex.strategies.Signal` stream into fills,
positions, and a P&L curve. The output of this module is what
:class:`~brutex.strategies.ComboSweep` will eventually record as its
per-combo metric (replacing the current ``signal_count`` placeholder).

## Package layout

* :mod:`brutex.execution.errors` \u2014 typed exceptions.
* :mod:`brutex.execution.types` \u2014 pure dataclasses: :class:`Fill`,
  :class:`Position`, :class:`FillCosts`, :class:`EquityPoint`,
  :class:`BacktestResult`, :class:`InstrumentMeta`.
* :mod:`brutex.execution.costs` \u2014 :class:`IndianCostModel`
  implementing FR-EXEC-002 (simplified P4 kickoff: STT, exchange
  fee, SEBI fee, GST, flat brokerage; stamp duty + DP charges
  land in follow-ups once day-1-vs-delivery tracking and EOD
  ISIN accounting are wired in).
* :mod:`brutex.execution.simulator` \u2014 :class:`ExecutionSimulator`
  with an inlined bar-VWAP \u00b1 bps slippage model.
* :mod:`brutex.execution.driver` \u2014 :class:`BacktestDriver` drives
  a :class:`~brutex.feed.ReplayFeed` through a
  :class:`~brutex.strategies.Strategy` plus
  :class:`ExecutionSimulator` and returns a
  :class:`BacktestResult`.

## Scope \u2014 P4 kickoff

MVP. Supported:

* Signal (BUY / SELL) \u2192 Fill pipeline with bar-VWAP \u00b1 bps slippage
* Fixed-quantity sizing per signal (configurable, default 1 lot)
* Intraday equity / F&O cost model
* Lot-size validation (``FR-EXEC-004``)
* Freeze-quantity validation (``FR-EXEC-005``)
* Position tracking (flat \u2194 long \u2194 short via flip fills)
* Realized + unrealized P&L per position
* Equity curve sampled on every mark-to-market

Out of scope for this commit \u2014 lands in follow-ups:

* Multiple slippage models (FR-EXEC-001 variants:
  ``next_bar_open_pessimistic``, ``sqrt_impact``)
* Circuit-filter rejection (FR-EXEC-003)
* Partial fills / queue-position penalty (FR-EXEC-006)
* SPAN margin + forced liquidation (\u00a713.1, \u00a713.2)
* Delivery equity (STT delivery + DP charges + T+1 settlement)
* PBO / Deflated Sharpe aggregation (FR-BF-006)
"""

from __future__ import annotations

from brutex.execution.costs import IndianCostModel
from brutex.execution.driver import BacktestDriver
from brutex.execution.errors import (
    ExecutionError,
    FreezeQuantityExceededError,
    InsufficientFundsError,
    InvalidLotSizeError,
)
from brutex.execution.simulator import ExecutionSimulator
from brutex.execution.types import (
    BacktestResult,
    EquityPoint,
    Fill,
    FillCosts,
    InstrumentMeta,
    Position,
)

__all__ = [
    "BacktestDriver",
    "BacktestResult",
    "EquityPoint",
    "ExecutionError",
    "ExecutionSimulator",
    "Fill",
    "FillCosts",
    "FreezeQuantityExceededError",
    "IndianCostModel",
    "InstrumentMeta",
    "InsufficientFundsError",
    "InvalidLotSizeError",
    "Position",
]
