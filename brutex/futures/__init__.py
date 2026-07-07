"""Futures execution-translation leg (DEC-FUTURES-EXEC-TRANSLATION-001).

Phase 5 of the one-click sweep law (R13 / R14 / R15): after the sweep ranks
winners on the PURE spot signal, an INDEX winner's trades are re-priced on the
FRONT-MONTH-AT-TRADE-TIME futures contract — the futures twin of the options
executor (:mod:`brutex.options.executor`), sharing its seams and reject
discipline while touching NOTHING on the options path (R12).

Modules:

- ``executor`` — the pure translation core: signals, legs, materialization,
  the pinned reject order, aggregation, the audited sim entrypoint.
- ``contract_resolver`` — front-month-at-entry resolution from the REAL
  ``registry.duckdb`` expiry calendar (lake-discovery fallback; NEVER a
  computed last-Thursday formula).
- ``lake_source`` — the exact-minute F&O candle-lake bar reader
  (dedup keep-first, corrupt-partition skip, schema-tolerant).
- ``preflight`` — the R4 futures-partition disk-verified refusal gate.
- ``reference_sim`` — the independent naive R45 differential simulator.
- ``leaderboard`` — display-only render + annotation + the R42 stamped
  sidecar (annotation-never-reranks).

Cost stack: :mod:`brutex.costs.futures_costs` (VERIFIED-rates-only; every
unverifiable futures rate is an explicit REFUSAL window — the
DEC-ETC-REGIME-HISTORY-001 precedent).

The brute-force ENGINE is UNCHANGED (NIFTY/BANKNIFTY/SENSEX) — this package is
a POST-sweep translation layer only; no scope/vocab/rate change.
"""

from brutex.futures.executor import (
    STOCK_SPOT_DEFERRAL_NOTE,
    AuditAppend,
    ExecutionKind,
    FrontMonthResolver,
    FuturesBarSource,
    FuturesBarWindow,
    FuturesLeg,
    FuturesRejectReason,
    FuturesRuleResult,
    FuturesSignal,
    MaterializedFuturesTrade,
    RejectedFuturesTrade,
    SpotTrade,
    execute_futures_sim,
    futures_lot_size,
    materialize_futures_trade,
    route_execution,
    run_futures_over_signals,
    select_futures_leg,
    signals_from_spot_trades,
)

__all__ = [
    "STOCK_SPOT_DEFERRAL_NOTE",
    "AuditAppend",
    "ExecutionKind",
    "FrontMonthResolver",
    "FuturesBarSource",
    "FuturesBarWindow",
    "FuturesLeg",
    "FuturesRejectReason",
    "FuturesRuleResult",
    "FuturesSignal",
    "MaterializedFuturesTrade",
    "RejectedFuturesTrade",
    "SpotTrade",
    "execute_futures_sim",
    "futures_lot_size",
    "materialize_futures_trade",
    "route_execution",
    "run_futures_over_signals",
    "select_futures_leg",
    "signals_from_spot_trades",
]
