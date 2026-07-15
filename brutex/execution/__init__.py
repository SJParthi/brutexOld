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
* :mod:`brutex.execution.fill_mode` — the stdlib-only global fill
  vocabulary (DEC-FILL-WORST-CASE-ONLY-001): the SINGLE-member
  :class:`FillMode` typed token (``WORST_CASE`` — the only fill law) and
  the launch-time refusal guard :func:`require_fill_mode_env_unset`
  (:class:`FillModeRemovedError` on a stale ``$BRUTEX_FILL_MODE``).
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
    FILL_MODE_ENV,
    FillMode,
    FillModeRemovedError,
    require_fill_mode_env_unset,
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
    "FILL_MODE_ENV",
    "BacktestResult",
    "EquityPoint",
    "ExecutionError",
    "Fill",
    "FillCosts",
    "FillMode",
    "FillModeRemovedError",
    "FreezeQuantityExceededError",
    "InstrumentMeta",
    "InsufficientFundsError",
    "InvalidLotSizeError",
    "InvalidPriceError",
    "NonMonotonicBarError",
    "Position",
    "require_fill_mode_env_unset",
]
