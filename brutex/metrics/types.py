"""Canonical input types for per-result trade metrics.

The metrics module consumes two surfaces, both defined or re-exported
here so every downstream caller imports from exactly one place
(CLAUDE.md Golden Rule #11 — COMMON):

* :class:`ClosedTrade` — one round-trip (entry → exit) with its
  realised P&L, holding span and intra-trade excursions (MFE / MAE).
  Money is carried as **int64 paisa** per ``TRACK1_BACKTEST_SPEC.md``
  §"Non-Negotiable Correctness Requirements" rule 1 ("Money in int64
  paisa") and the Result Row Schema (``net_pnl_paisa``,
  ``avg_mfe_paisa``, ``avg_mae_paisa`` are all int64 paisa).

  This is the first home for a per-trade closed-trade record: the
  execution-types surface (:mod:`brutex.execution.types`) defines
  :class:`Fill` objects and an :class:`EquityPoint` curve, but no
  round-trip aggregation existed there. Per Golden Rule #11 the
  metrics consumer is the canonical owner; the live trade-builder
  (:mod:`brutex.engine.trade_sim`) imports this type rather than
  redefine it.

* The equity curve. We reuse :class:`brutex.execution.types.EquityPoint`
  for the rich curve, but the metrics only need the scalar equity
  value per bar. To keep this module dependency-light (no ``polars`` /
  ``pyarrow`` at import time, matching :mod:`brutex.execution.types`),
  callers pass a plain sequence of int64-paisa equity values via
  :class:`EquityCurve`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from brutex.strategies.types import Side

__all__ = ["ClosedTrade", "EquityCurve"]


@dataclass(frozen=True, slots=True)
class ClosedTrade:
    """One completed round-trip trade.

    All money fields are signed (or, where the schema says so, absolute)
    int64 **paisa** — never floats — per ``TRACK1_BACKTEST_SPEC.md`` rule 1.
    The integer paisa fields rely on the ``int`` type contract +
    ``mypy --strict`` for finiteness (an ``int`` can never be NaN/inf), so
    no runtime ``math.isfinite`` guard is added to them.

    Parameters
    ----------
    pnl_paisa
        Net realised P&L of the round-trip, in paisa. Positive for a
        winning trade, negative for a loser, zero for a scratch.
        Track 1 is signal-only (no costs, rule 7), so this is the raw
        directional P&L.
    mfe_paisa
        Maximum Favourable Excursion — the largest unrealised profit
        (in paisa, ``>= 0``) seen at any point during the trade.
        Feeds ``avg_mfe_paisa``.
    mae_paisa
        Maximum Adverse Excursion — the largest unrealised loss seen
        during the trade, stored as a **non-negative** magnitude
        (paisa, ``>= 0``). Feeds ``avg_mae_paisa`` ("absolute").
    holding_bars
        Number of bars the trade was open (``>= 1``). Feeds
        ``avg_holding_time_bars`` / ``max_holding_time_bars``.
    entry_ts
        Timezone-aware entry timestamp.
    exit_ts
        Timezone-aware exit timestamp (``>= entry_ts``).
    direction
        ``Side.BUY`` (long) or ``Side.SELL`` (short). ``HOLD`` is not a
        trade and is rejected.
    vix_at_entry, vix_at_exit
        The **India VIX** index level (a :class:`~decimal.Decimal`) at the
        trade's entry / exit instant, or ``None`` when no VIX bar exists at that
        timestamp. India VIX is **REFERENCE ONLY** (CLAUDE.md §1/§3): it is
        stamped on every trade for downstream filter analysis but is **NEVER**
        part of the brute-force pattern combinations or the shared condition
        vocabulary (DEC-VIX-TRADE-STAMP-001). It is a volatility index level,
        **not money**, so it keeps full Decimal precision and is never routed
        through paisa rounding (CLAUDE.md §4). ``None`` is the no-hallucination
        sentinel — a missing VIX bar is never replaced by a fabricated number
        (Golden Rule #1). Both default to ``None`` for callers that do not
        stamp.

    Raises
    ------
    ValueError
        If ``mfe_paisa`` or ``mae_paisa`` is negative, ``holding_bars``
        is not strictly positive, timestamps are naive, ``exit_ts`` is
        before ``entry_ts``, ``direction`` is ``HOLD``, or a supplied
        ``vix_at_entry`` / ``vix_at_exit`` is a non-finite (NaN / inf)
        Decimal.
    """

    pnl_paisa: int
    mfe_paisa: int
    mae_paisa: int
    holding_bars: int
    entry_ts: datetime
    exit_ts: datetime
    direction: Side
    vix_at_entry: Decimal | None = None
    vix_at_exit: Decimal | None = None

    def __post_init__(self) -> None:
        if self.mfe_paisa < 0:
            raise ValueError(f"ClosedTrade.mfe_paisa must be >= 0, got {self.mfe_paisa}")
        if self.mae_paisa < 0:
            raise ValueError(f"ClosedTrade.mae_paisa must be >= 0, got {self.mae_paisa}")
        if self.holding_bars <= 0:
            raise ValueError(f"ClosedTrade.holding_bars must be >= 1, got {self.holding_bars}")
        if self.entry_ts.tzinfo is None:
            raise ValueError(f"ClosedTrade.entry_ts must be tz-aware, got naive {self.entry_ts!r}")
        if self.exit_ts.tzinfo is None:
            raise ValueError(f"ClosedTrade.exit_ts must be tz-aware, got naive {self.exit_ts!r}")
        if self.exit_ts < self.entry_ts:
            raise ValueError(
                f"ClosedTrade.exit_ts {self.exit_ts!r} is before entry_ts {self.entry_ts!r}"
            )
        if self.direction is Side.HOLD:
            raise ValueError("ClosedTrade.direction must be BUY or SELL, not HOLD")
        # VIX is a metric, not money: never rounded — but a non-finite Decimal
        # (NaN / inf) would silently corrupt any downstream filter, so reject it
        # here (the int paisa fields can never be non-finite; only these can).
        if self.vix_at_entry is not None and not self.vix_at_entry.is_finite():
            raise ValueError(
                f"ClosedTrade.vix_at_entry must be a finite Decimal or None, "
                f"got {self.vix_at_entry!r}"
            )
        if self.vix_at_exit is not None and not self.vix_at_exit.is_finite():
            raise ValueError(
                f"ClosedTrade.vix_at_exit must be a finite Decimal or None, "
                f"got {self.vix_at_exit!r}"
            )

    @property
    def is_win(self) -> bool:
        """Return ``True`` iff the trade closed with strictly positive P&L."""
        return self.pnl_paisa > 0

    @property
    def is_loss(self) -> bool:
        """Return ``True`` iff the trade closed with strictly negative P&L."""
        return self.pnl_paisa < 0


@dataclass(frozen=True, slots=True)
class EquityCurve:
    """Scalar equity curve sampled once per bar, in int64 paisa.

    Parameters
    ----------
    equity_paisa
        One equity value per bar, in chronological order. The first
        element is the starting equity. Money in int64 paisa per
        ``TRACK1_BACKTEST_SPEC.md`` rule 1.
    start_equity_paisa
        Capital base used as the denominator for ``total_return_pct``
        and the CAGR / drawdown-percentage calculations. Must be
        strictly positive (a zero or negative base makes percentage
        returns undefined).
    span_years
        Calendar span of the curve in years, used to annualise
        ``cagr_pct`` (``TRACK1_BACKTEST_SPEC.md`` line 535,
        "cagr_pct ... annualized"). Must be strictly positive.

    Raises
    ------
    ValueError
        If the curve is empty, ``start_equity_paisa`` is not strictly
        positive, or ``span_years`` is not strictly positive (a non-finite
        NaN/inf span is also rejected here).

    Notes
    -----
    The integer paisa fields rely on the ``int`` type contract +
    ``mypy --strict`` for finiteness (an ``int`` can never be NaN/inf);
    only the float ``span_years`` is guarded against non-finite values at
    runtime.
    """

    equity_paisa: tuple[int, ...]
    start_equity_paisa: int
    span_years: float

    def __post_init__(self) -> None:
        if not self.equity_paisa:
            raise ValueError("EquityCurve.equity_paisa must be non-empty")
        if self.start_equity_paisa <= 0:
            raise ValueError(
                f"EquityCurve.start_equity_paisa must be > 0, got {self.start_equity_paisa}"
            )
        if not math.isfinite(self.span_years) or self.span_years <= 0.0:
            raise ValueError(f"EquityCurve.span_years must be > 0, got {self.span_years}")
