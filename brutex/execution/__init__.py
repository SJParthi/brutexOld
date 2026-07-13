"""BruteX execution surface — shared execution types + typed errors.

What remains of the original P4-kickoff execution package after
**DEC-EXECUTION-TRIO-DELETE-001** deleted the deprecated, dormant
``IndianCostModel`` / ``ExecutionSimulator`` / ``BacktestDriver`` trio
(stale cost rates; nothing in the live sweep ever constructed any of
the three — the flagged R2-OC-8 follow-up executed in full). The live
Track-1 simulation path is the signal-only
:mod:`brutex.engine.trade_sim` walker, and the authoritative cost
model is :func:`brutex.costs.calculator.compute_trade_cost` per
DEC-COST-001 / ``docs/track2/COSTS_VERIFIED.md``.

## Package layout

* :mod:`brutex.execution.errors` — typed exceptions: the
  :class:`ExecutionError` hierarchy under
  :class:`~brutex.models.BruteXError`.
* :mod:`brutex.execution.fill_mode` — the stdlib-only global fill-mode
  vocabulary (DEC-FILL-WORST-CASE-001): :class:`FillMode`
  (``WORST_CASE``/``PRECISE``), the production default
  :data:`DEFAULT_FILL_MODE`, and the fail-loud ``$BRUTEX_FILL_MODE``
  resolver :func:`resolve_fill_mode`.
* :mod:`brutex.execution.types` — pure, heavy-dep-free dataclasses:
  :class:`Fill`, :class:`Position`, :class:`FillCosts`,
  :class:`EquityPoint`, :class:`BacktestResult`,
  :class:`InstrumentMeta`. The lean instrument store
  (:mod:`brutex.data.instrument_store`) builds a
  ``{symbol: InstrumentMeta}`` map for O(1) per-symbol metadata
  handoff (``meta_map()`` / ``meta()``) — the package's live
  production consumer.
"""

from __future__ import annotations

from brutex.execution.errors import (
    ExecutionError,
    FreezeQuantityExceededError,
    InsufficientFundsError,
    InvalidLotSizeError,
    InvalidPriceError,
    NonMonotonicBarError,
)
from brutex.execution.fill_mode import (
    DEFAULT_FILL_MODE,
    FILL_MODE_ENV,
    FillMode,
    resolve_fill_mode,
)
from brutex.execution.types import (
    BacktestResult,
    EquityPoint,
    Fill,
    FillCosts,
    InstrumentMeta,
    Position,
)

__all__ = [
    "DEFAULT_FILL_MODE",
    "FILL_MODE_ENV",
    "BacktestResult",
    "EquityPoint",
    "ExecutionError",
    "Fill",
    "FillCosts",
    "FillMode",
    "FreezeQuantityExceededError",
    "InstrumentMeta",
    "InsufficientFundsError",
    "InvalidLotSizeError",
    "InvalidPriceError",
    "NonMonotonicBarError",
    "Position",
    "resolve_fill_mode",
]
