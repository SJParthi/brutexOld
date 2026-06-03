"""Stage-2 spot trade-sim walker (Mode A) — the producer that feeds the results store.

This is **engine PR-2 / sub-PR 1**: the bar-by-bar SL/TP + forced-15:20 walker
that turns entry signals into completed round-trips. It is the *producer* the
deterministic results store (DEC-RESULT-STORE-001) was built to receive — it
emits the existing :class:`brutex.metrics.types.ClosedTrade` (REUSED, not
reinvented — CLAUDE.md §11 COMMON), which the metrics layer will later aggregate
into a result row.

Signal-only research — **NOT** the Track-2 execution simulator
-------------------------------------------------------------
Track 1 spot indices (NIFTY / BANKNIFTY / SENSEX) are computed averages with no
order book, so there is **no slippage, no pessimistic fill, no brokerage / STT /
costs** (``TRACK1_BACKTEST_SPEC.md`` §"Non-Negotiable Correctness" rules 5-7).
This walker is therefore NEW int64-paisa signal-only code; it deliberately does
**not** use :class:`brutex.execution.simulator.ExecutionSimulator` (the
Decimal/cost/slippage Track-2 path) — per the DEC-ENGINE-DRIVER-001 (#423) §9
review finding C2.

The exact fill contract (every rule sourced)
--------------------------------------------
* **Entry:** a signal at bar ``N`` (evaluated at its close) → enter at bar
  ``N+1`` **OPEN**, the exact value (``spec:240`` / ``spec:640`` / DEC-RISK-012).
* **SL/TP exit:** while open, a bar ``M`` whose range breaches the stop or target
  triggers an exit filled at bar ``M+1`` **OPEN**, the exact value — never at the
  SL/TP level (``spec:640-649`` — *no pessimistic fill on spot*). The entry bar
  ``N+1`` can itself be the trigger bar (min holding = 1 bar).
* **Same-bar SL *and* TP:** if one bar breaches both, the exit still fills at
  ``M+1`` OPEN regardless of which level tripped, so the P&L is **identical**
  either way — the precedence question is *moot* under the M+1-open rule (and
  :class:`ClosedTrade` has no exit-reason field). Verified by a dedicated test.
* **Forced 15:20 exit (overrides everything — ``spec:241,658``):** if still open
  on reaching the first bar at/after ``FORCED_EXIT_IST`` (15:20 IST), exit at
  **that bar's OPEN** *before* any SL/TP check on it (15:19 close triggers →
  15:20 OPEN fills, DEC-RISK-012). A position can never survive past the 15:20
  open, so ``entry.date == exit.date`` (DEC-RISK-011 P-INTRA-1) holds.
* **P&L (int64 paisa):** long ``(exit_open − entry_open)``; short
  ``(entry_open − exit_open)``; ``× 100`` via :func:`brutex.money.rupees_to_paisa`
  (exact — 2dp lake prices). MFE/MAE are the max favourable / adverse excursions
  (paisa, ``>= 0``) over the held bars ``[N+1 … M]`` (the entry bar's range
  counts; the M+1 fill bar's does not, since the position exits at its open).

Scope of this sub-PR (honest envelope — CLAUDE.md §10)
-----------------------------------------------------
* **Mode A only** (SL/TP grid); Mode B (signal-exit) is a later sub-PR.
* **One IST session per call** (validated): the caller batches by trading day —
  this enforces the per-day 15:20 reset + P-INTRA-1 with no cross-day state.
* **Signals are an input** (bar indices) — decoupled from the 52-condition mask
  evaluation; wiring the mask → signals is a later sub-PR.
* **One position at a time**: signals that fire while a position is open are
  ignored; re-entry is eligible only from a bar strictly after the exit fill
  (no same-open double-fill).
* **Drop-incomplete (judgment call, documented):** a position with no SL/TP/15:20
  exit available (e.g. a truncated session with no 15:20 bar) emits **no**
  ``ClosedTrade`` — a fabricated exit would be an illusion (Golden Rule #10).
* No look-ahead: at bar ``M`` the *decision* reads only bars ``0..M``; the fill
  reads bar ``M+1``'s OPEN as a mechanic (not to decide). The instrumented
  ``evaluate(N)`` accessor (Property P-06) is the future runtime enforcer.

Two minor seams left as tracked follow-ups (honesty): ``stage1a._price_to_paisa``
predates :func:`brutex.money.rupees_to_paisa` (this PR's new canonical seam) and
should be DRYed to delegate to it; and circuit-breaker pre-emption of 15:20
(DEC-RISK-009, Track-2 risk) is out of this Track-1 spot scope.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

import structlog

from brutex.calendars.nse import FORCED_EXIT_IST, IST
from brutex.metrics.types import ClosedTrade
from brutex.models import BruteXError
from brutex.money import paisa_to_rupees, rupees_to_paisa
from brutex.strategies.types import Side

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from collections.abc import Iterable, Sequence
    from datetime import date

    from brutex.data.models import CandleBar

_LOG = structlog.get_logger("brutex.engine.trade_sim")


class TradeSimError(BruteXError):
    """Raised for an invalid trade-sim input.

    Covers an empty / multi-session / out-of-order bar list, a malformed
    direction, a non-positive SL/TP, or a signal index out of range.
    """


@dataclass(frozen=True, slots=True)
class _OpenPosition:
    """The mutable-free state of one open position (entry fixed at fill time)."""

    entry_index: int
    entry_price: Decimal
    sl_level: Decimal
    tp_level: Decimal


def simulate_session_mode_a(
    bars: Sequence[CandleBar],
    *,
    signal_bars: Iterable[int],
    direction: Side,
    sl_paisa: int,
    tp_paisa: int,
) -> list[ClosedTrade]:
    """Walk one IST session and return the Mode-A round-trips as ``ClosedTrade``\\ s.

    Parameters
    ----------
    bars
        One IST trading session's bars (one ``(instrument, interval)``), in
        strictly increasing timestamp order, Decimal-rupee OHLC. All bars must
        fall on the same IST date.
    signal_bars
        Bar indices ``N`` at whose *close* an entry signal fired. Deduplicated
        and processed in ascending order. A signal at the last bar (no ``N+1``)
        or while a position is open is dropped.
    direction
        ``Side.BUY`` (long) or ``Side.SELL`` (short). ``Side.HOLD`` is rejected.
    sl_paisa, tp_paisa
        Mode-A stop-loss / take-profit, in paisa offsets from the entry price.
        Both must be strictly positive.

    Returns
    -------
    list[ClosedTrade]
        One round-trip per completed trade, in entry order.

    Raises
    ------
    TradeSimError
        On an empty / multi-session / unordered bar list, a ``HOLD`` direction,
        a non-positive SL/TP, or a signal index outside ``[0, len(bars))``.
    """
    _validate_inputs(bars, direction=direction, sl_paisa=sl_paisa, tp_paisa=tp_paisa)
    forced_index = _forced_exit_index(bars)
    sl_offset = paisa_to_rupees(sl_paisa)
    tp_offset = paisa_to_rupees(tp_paisa)
    is_long = direction is Side.BUY

    ordered_signals = sorted({_checked_index(n, len(bars)) for n in signal_bars})

    trades: list[ClosedTrade] = []
    next_eligible = 0
    for signal_n in ordered_signals:
        if signal_n < next_eligible:
            continue  # a position was open across this signal — ignore it
        entry_index = signal_n + 1
        if entry_index >= len(bars):
            break  # signal on the last bar: no N+1 open to enter on
        if forced_index is not None and entry_index >= forced_index:
            break  # no room: entry would be at/after the forced 15:20 exit
        position = _open_at(bars, entry_index, is_long=is_long, sl=sl_offset, tp=tp_offset)
        trade = _walk_to_exit(
            bars, position, forced_index=forced_index, is_long=is_long, direction=direction
        )
        if trade is None:
            continue  # drop-incomplete: no SL/TP/15:20 fill available
        trades.append(trade)
        # Re-entry is eligible only from a bar strictly after the exit fill bar
        # (= entry_index + holding_bars), so we never enter on the exit's OPEN.
        next_eligible = entry_index + trade.holding_bars

    _LOG.info(
        "trade_sim.session",
        bars=len(bars),
        signals=len(ordered_signals),
        trades=len(trades),
        direction=direction.value,
    )
    return trades


# ── Private helpers ──────────────────────────────────────────────────────────


def _open_at(
    bars: Sequence[CandleBar], entry_index: int, *, is_long: bool, sl: Decimal, tp: Decimal
) -> _OpenPosition:
    """Build the open-position state for an entry at *entry_index*'s OPEN."""
    entry_price = bars[entry_index].open
    if is_long:
        sl_level = entry_price - sl
        tp_level = entry_price + tp
    else:
        sl_level = entry_price + sl
        tp_level = entry_price - tp
    return _OpenPosition(
        entry_index=entry_index, entry_price=entry_price, sl_level=sl_level, tp_level=tp_level
    )


def _walk_to_exit(
    bars: Sequence[CandleBar],
    position: _OpenPosition,
    *,
    forced_index: int | None,
    is_long: bool,
    direction: Side,
) -> ClosedTrade | None:
    """Walk forward from the entry bar to the exit; return the ``ClosedTrade``.

    Returns ``None`` (drop-incomplete) if the position reaches the end of the
    session with no SL/TP breach and no forced-15:20 bar to fill against.
    """
    entry = position.entry_price
    mfe = Decimal(0)
    mae = Decimal(0)
    exit_index: int | None = None
    for m in range(position.entry_index, len(bars)):
        if forced_index is not None and m == forced_index:
            exit_index = m  # forced 15:20: fill at this bar's OPEN, before its range
            break
        bar = bars[m]
        favourable = (bar.high - entry) if is_long else (entry - bar.low)
        adverse = (entry - bar.low) if is_long else (bar.high - entry)
        mfe = max(mfe, favourable)
        mae = max(mae, adverse)
        if _breached(bar, position, is_long=is_long):
            fill = m + 1
            if fill < len(bars):
                exit_index = fill  # SL/TP at bar M → fill at M+1 OPEN
            break
    if exit_index is None:
        return None  # ran off the end with no fillable exit
    exit_price = bars[exit_index].open
    gain = (exit_price - entry) if is_long else (entry - exit_price)
    return ClosedTrade(
        pnl_paisa=rupees_to_paisa(gain),
        mfe_paisa=rupees_to_paisa(mfe),
        mae_paisa=rupees_to_paisa(mae),
        holding_bars=exit_index - position.entry_index,
        entry_ts=bars[position.entry_index].timestamp,
        exit_ts=bars[exit_index].timestamp,
        direction=direction,
    )


def _breached(bar: CandleBar, position: _OpenPosition, *, is_long: bool) -> bool:
    """Return ``True`` iff *bar*'s range hit the stop or the target."""
    if is_long:
        return bar.low <= position.sl_level or bar.high >= position.tp_level
    return bar.high >= position.sl_level or bar.low <= position.tp_level


def _forced_exit_index(bars: Sequence[CandleBar]) -> int | None:
    """Return the index of the first bar at/after 15:20 IST (the forced-exit fill bar).

    ``None`` if the session has no such bar (a truncated session — open trades
    are then dropped). The bar's OPEN is the forced-exit fill (DEC-RISK-012).
    """
    for i, bar in enumerate(bars):
        if bar.timestamp.astimezone(IST).time() >= FORCED_EXIT_IST:
            return i
    return None


def _validate_inputs(
    bars: Sequence[CandleBar], *, direction: Side, sl_paisa: int, tp_paisa: int
) -> None:
    """Validate the session bars + Mode-A params; raise :class:`TradeSimError`."""
    if not bars:
        raise TradeSimError("simulate_session_mode_a: bars must be non-empty")
    if direction is Side.HOLD:
        raise TradeSimError("simulate_session_mode_a: direction must be BUY or SELL, not HOLD")
    if sl_paisa <= 0:
        raise TradeSimError(f"simulate_session_mode_a: sl_paisa must be > 0, got {sl_paisa}")
    if tp_paisa <= 0:
        raise TradeSimError(f"simulate_session_mode_a: tp_paisa must be > 0, got {tp_paisa}")
    sessions: set[date] = set()
    previous = bars[0].timestamp
    for index, bar in enumerate(bars):
        if index > 0 and bar.timestamp <= previous:
            raise TradeSimError(
                f"simulate_session_mode_a: bars must be strictly time-ordered; "
                f"bar {index} {bar.timestamp.isoformat()} <= {previous.isoformat()}"
            )
        previous = bar.timestamp
        sessions.add(bar.timestamp.astimezone(IST).date())
    if len(sessions) != 1:
        raise TradeSimError(
            f"simulate_session_mode_a: bars must be ONE IST session, got {len(sessions)} dates "
            f"{sorted(sessions)} — the caller batches by trading day (DEC-RISK-011 P-INTRA-1)"
        )


def _checked_index(index: int, n_bars: int) -> int:
    """Return *index* if it is a valid bar index, else raise :class:`TradeSimError`."""
    if not 0 <= index < n_bars:
        raise TradeSimError(
            f"simulate_session_mode_a: signal bar index {index} out of range [0, {n_bars})"
        )
    return index


__all__ = ["TradeSimError", "simulate_session_mode_a"]
