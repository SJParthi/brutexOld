"""Stage-2 spot trade-simulation walker — mask-hit signal → round-trip ``ClosedTrade``.

The Stage-1A matrix builder (:mod:`brutex.engine.stage1a`) turns a bar feed into a
per-bar condition bitset, and the Stage-2 supermask kernel
(:func:`brutex.engine.combo_mask.count_supermasks`) turns a strategy mask into a
per-bar **entry-signal stream** (one ``bool`` per bar — "did mask M fire on this
bar?"). This module is the next hop: it consumes that ``bool``-per-bar entry
stream **plus** the :class:`~brutex.data.models.CandleBar` feed and walks them
bar-by-bar into round-trip :class:`~brutex.metrics.types.ClosedTrade` objects,
which :func:`brutex.metrics.compute.compute_metrics` then turns into Sharpe and
the rest of the result row.

It is a **signal-only** spot simulator (``TRACK1_BACKTEST_SPEC.md`` lines
640-657, §9 finding C2 of ``docs/VERIFICATION_2026-06-03_ENGINE_SCOPING.md``):
the index has no order book, so there is **no slippage, no brokerage, no STT**
and money stays in **int64 paisa** end-to-end. It deliberately does **not**
import :class:`brutex.execution.simulator.ExecutionSimulator` (which fills at
bar CLOSE and applies 5 bps slippage + ₹20 brokerage in ``Decimal`` — correct
for tradable Track-2 options, wrong for spot). P&L is the per-unit directional
price move in paisa (no position sizing — spot is research, not execution).

## Fill conventions (configurable — :class:`FillPolicy`)

The spec leaves two fill questions open (scoping doc §5 Q1/Q2); this module
**decides** them as a configurable :class:`FillPolicy` whose default is the
conservative choice (DEC-TRADESIM-001):

* **Entry** — *locked* to the next bar's OPEN (signal on bar ``N`` → enter at
  ``N+1`` OPEN; ``TRACK1_BACKTEST_SPEC.md`` line 240). Not a knob.
* **Stop-loss / take-profit fill** — :data:`LevelFill.AT_LEVEL` (default:
  fill exactly at the trigger price) or :data:`LevelFill.NEXT_OPEN` (fill at
  the next bar's OPEN — the spot "no order book, no fiction" stance of spec
  lines 640-654).
* **Same-bar SL+TP** — when one bar's range spans BOTH levels,
  :data:`SameBarTie.SL_FIRST` (default, *pessimistic* — assume the loss) or
  :data:`SameBarTie.TP_FIRST` (*optimistic*).
* **Forced session-close exit** — *locked*: fills at the OPEN of the first bar
  whose window reaches the SESSION's forced-exit instant and **overrides every
  other exit** (spec line 658). The boundary is the bar session's own close −
  10min — 15:20 IST for a regular day, the early close − 10min for a half-day /
  Muhurat session — resolved per bar via
  :func:`brutex.calendars.nse.forced_exit_seconds_for_session` and applied with
  the timeframe-aware :func:`brutex.calendars.nse.is_forced_exit_bar`
  (DEC-SESSION-FORCED-EXIT-001; timeframe-awareness from DEC-TRADESIM-001).
* **Mode-B signal exit** — *locked* to the next bar's OPEN (symmetry with
  entry; spec line 641). Supplied via the optional ``exit_signal`` stream.
* **End of data** — a position still open when the feed ends closes at the last
  bar's CLOSE (no 15:20 reached, no next open to fill against).

## No look-ahead (CLAUDE.md §4, Golden Rule #5)

A trade's outcome depends only on bars from its entry through its exit: the
walker is a forward-only state machine that never reads a future bar. The
next-bar-OPEN entry/exit fills make bar ``M+1`` *part of* the trade that fills
on it (its exit bar), never a peek. A property test asserts the invariant
directly: mutating any bar strictly after a trade's exit cannot change that
trade.

## O(1) per bar (Golden Rule #5)

Every :meth:`TradeSimWalker.push` does constant work — a fixed set of paisa
comparisons and at most one position open/close; there is no scan back over
history and no growing per-bar state. (The returned trade *list* grows with the
number of trades, which is inherent output, not per-bar cost — the O(1) claim
is **per bar / per operation**, never for the whole walk; §9 finding F-5/M3.)

## Determinism (Golden Rule #4)

Pure: no globals, no wall-clock, no RNG. The same bars + signals always produce
the same trades, bit-for-bit; a fresh walker is stateless across runs.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import TYPE_CHECKING, Final

import structlog

from brutex.calendars.nse import (
    IST,
    forced_exit_seconds_for_session,
    get_session,
    is_forced_exit_bar,
)

# Intentional friend access (DEC-P0-017 per-site convention): ``_price_to_paisa``
# is the canonical Decimal-rupees → int64-paisa seam (one source of truth,
# CLAUDE.md §11 COMMON — ``brutex.money`` only converts paisa→rupees). The
# Stage-2 walker reuses it rather than redefining the rounding rule (the task's
# explicit reuse instruction); the sibling-private import is deliberate.
from brutex.engine.stage1a import _price_to_paisa  # pyright: ignore[reportPrivateUsage]
from brutex.strategies.types import Side

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from brutex.data.models import CandleBar

    # ``ClosedTrade`` is imported lazily at its single construction site (``_close``)
    # rather than at module top: importing ``brutex.metrics.types`` runs the
    # ``brutex.metrics`` package ``__init__``, which eagerly imports numpy (via
    # ``metrics.spa``). Keeping that out of ``brutex.engine``'s import chain stops
    # the per-module strict-coverage gates (combo_mask / stage1a) from loading
    # numpy at collection — numpy 2.x raises "cannot load module more than once
    # per process" under the single-module ``--cov`` subprocess (DEC-TRADESIM-001;
    # mirrors the numpy-lazy convention in metrics/cpcv + engine/combo_mask).
    from brutex.metrics.types import ClosedTrade

_LOG = structlog.get_logger(__name__)


class LevelFill(Enum):
    """How a stop-loss / take-profit trigger is priced once a bar touches it."""

    #: Fill exactly at the trigger (SL / TP) price — the conservative default.
    AT_LEVEL = "at_level"
    #: Fill at the NEXT bar's OPEN — the spot "no order book, no fiction" mode
    #: (``TRACK1_BACKTEST_SPEC.md`` lines 640-654). Falls back to the trigger
    #: bar's CLOSE when there is no next bar (end of data).
    NEXT_OPEN = "next_open"


class SameBarTie(Enum):
    """Which leg fills first when ONE bar's range spans BOTH the stop and target."""

    #: Pessimistic — assume the stop filled first (worst case). The default.
    SL_FIRST = "sl_first"
    #: Optimistic — assume the target filled first.
    TP_FIRST = "tp_first"


@dataclass(frozen=True, slots=True)
class FillPolicy:
    """The configurable spot-fill conventions (DEC-TRADESIM-001).

    Only the two questions the spec leaves genuinely open (scoping doc §5
    Q1/Q2) are knobs; the rest are locked by the spec and documented on the
    module:

    * entry is ALWAYS the next bar's OPEN (spec line 240),
    * the 15:20 IST forced exit ALWAYS fills at the forced bar's OPEN and
      overrides everything (spec line 658),
    * a Mode-B signal exit ALWAYS fills at the next bar's OPEN (spec line 641).

    Parameters
    ----------
    stop_loss
        Fill rule for a stop-loss trigger. Defaults to
        :data:`LevelFill.AT_LEVEL`.
    take_profit
        Fill rule for a take-profit trigger. Defaults to
        :data:`LevelFill.AT_LEVEL`.
    same_bar
        Tie-break when a single bar touches both the stop and the target.
        Defaults to :data:`SameBarTie.SL_FIRST` (pessimistic).
    """

    stop_loss: LevelFill = LevelFill.AT_LEVEL
    take_profit: LevelFill = LevelFill.AT_LEVEL
    same_bar: SameBarTie = SameBarTie.SL_FIRST


#: The conservative default fill policy (AT_LEVEL SL/TP + pessimistic same-bar).
DEFAULT_FILL_POLICY: Final[FillPolicy] = FillPolicy()


class _ExitReason(Enum):
    """Why a position closed (internal — for structlog only; not on ``ClosedTrade``)."""

    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    FORCED = "forced_1520"
    SIGNAL = "signal"
    END_OF_DATA = "end_of_data"


@dataclass(slots=True)
class _OpenPosition:
    """Mutable per-trade state while a position is open (int64 paisa)."""

    entry_idx: int
    entry_price_paisa: int
    entry_ts: datetime
    sl_level_paisa: int | None
    tp_level_paisa: int | None
    mfe_paisa: int  # running max favourable excursion (>= 0)
    mae_paisa: int  # running max adverse excursion magnitude (>= 0)
    # Most-recent in-position bar — the end-of-data fallback closes against it.
    # Held as the bar itself (not pre-converted paisa) so the hot per-bar path
    # avoids a Decimal→paisa conversion of every CLOSE; the conversion happens
    # once, only if the position survives to end of data (DEC-TRADESIM-001).
    last_idx: int
    last_bar: CandleBar


class TradeSimWalker:
    """Walk a bar feed + entry-signal stream into round-trip ``ClosedTrade`` objects.

    Streaming-first (CLAUDE.md §11 COMPREHENSIVE): feed bars one at a time with
    :meth:`push` (returning a :class:`ClosedTrade` on the bar a position closes,
    else ``None``) then call :meth:`finish` once at the end, **or** hand the
    whole feed to :meth:`run` for the batch path. Both share the same per-bar
    state machine, so the trade list is identical regardless of entry point. The
    walker is single-position: while a position is open, entry signals are
    ignored (Track 1 is non-overlapping — ``time_in_market_pct`` assumes it).

    Feed contract: the walker opens an armed entry at the *very next bar's* open
    regardless of any calendar gap between bars, so the caller must supply a
    contiguous intraday feed for one instrument (the lake /
    :class:`~brutex.feed.replay.ReplayFeed` already do). The session forced exit
    (15:20 IST on a regular day, close − 10min on a half-day / Muhurat session)
    means an entry signal on a session's last usable bar is dropped (its next
    bar is a forced-exit bar), so signals are not expected to arm across a
    trading-day boundary.

    Parameters
    ----------
    direction
        :data:`Side.BUY` (long) or :data:`Side.SELL` (short). The same
        direction for every trade this walker emits. ``HOLD`` is rejected.
    tf_minutes
        The bar timeframe in whole minutes (``>= 1``). Passed to the
        timeframe-aware :func:`~brutex.calendars.nse.is_forced_exit_bar` so a
        coarse-TF bar whose span straddles 15:20 IST forces an exit.
    stop_loss_paisa
        Stop-loss distance from entry in paisa (``> 0``), or ``None`` to
        disable the stop leg (Mode B).
    take_profit_paisa
        Take-profit distance from entry in paisa (``> 0``), or ``None`` to
        disable the target leg (Mode B).
    policy
        The :class:`FillPolicy`. Defaults to :data:`DEFAULT_FILL_POLICY`.

    Raises
    ------
    ValueError
        If ``direction`` is ``HOLD``, ``tf_minutes < 1``, or a non-``None``
        stop/target distance is not strictly positive.
    """

    def __init__(
        self,
        *,
        direction: Side,
        tf_minutes: int,
        stop_loss_paisa: int | None = None,
        take_profit_paisa: int | None = None,
        policy: FillPolicy = DEFAULT_FILL_POLICY,
    ) -> None:
        if direction is Side.HOLD:
            raise ValueError("TradeSimWalker.direction must be BUY or SELL, not HOLD")
        if tf_minutes < 1:
            raise ValueError(f"TradeSimWalker.tf_minutes must be >= 1, got {tf_minutes}")
        if stop_loss_paisa is not None and stop_loss_paisa <= 0:
            raise ValueError(
                f"TradeSimWalker.stop_loss_paisa must be > 0 or None, got {stop_loss_paisa}"
            )
        if take_profit_paisa is not None and take_profit_paisa <= 0:
            raise ValueError(
                f"TradeSimWalker.take_profit_paisa must be > 0 or None, got {take_profit_paisa}"
            )
        self._direction = direction
        self._tf_minutes = tf_minutes
        self._sl_paisa = stop_loss_paisa
        self._tp_paisa = take_profit_paisa
        self._policy = policy
        self._position: _OpenPosition | None = None
        self._pending_entry = False
        self._pending_exit: _ExitReason | None = None
        self._index = -1
        # Session-aware forced-exit boundary cache (DEC-SESSION-FORCED-EXIT-001).
        # The boundary (seconds-since-midnight IST) is the bar session's own
        # close − 10min: 15:20 for a regular day, the early close − 10min for a
        # half-day / Muhurat session. It is constant within one IST trading day,
        # so we resolve it ONCE per date and memoise the LAST date only (O(1)
        # per bar, O(1) space — a bar stream is chronological so a 1-slot cache
        # hits on every bar after the first of each day).
        self._fe_cache_date: date | None = None
        self._fe_cache_seconds: int | None = None

    # ── Batch path ────────────────────────────────────────────────────────

    def run(
        self,
        bars: Sequence[CandleBar],
        entry_signals: Sequence[bool],
        exit_signals: Sequence[bool] | None = None,
    ) -> list[ClosedTrade]:
        """Walk a whole feed and return every closed trade in chronological order.

        Parameters
        ----------
        bars
            Chronologically ordered bars for one ``(instrument, interval)``.
        entry_signals
            One ``bool`` per bar — ``True`` means "enter (in ``direction``) at
            the NEXT bar's open". Must be the same length as ``bars``.
        exit_signals
            Optional one ``bool`` per bar — ``True`` means "exit at the next
            bar's open" (Mode B). Must match ``bars`` length when supplied.

        Returns
        -------
        list[ClosedTrade]
            The closed round-trips, in the order they closed.

        Raises
        ------
        ValueError
            If ``entry_signals`` (or a supplied ``exit_signals``) length does
            not match ``bars``.
        """
        if len(entry_signals) != len(bars):
            raise ValueError(
                f"entry_signals length {len(entry_signals)} != bars length {len(bars)}"
            )
        if exit_signals is not None and len(exit_signals) != len(bars):
            raise ValueError(f"exit_signals length {len(exit_signals)} != bars length {len(bars)}")
        trades: list[ClosedTrade] = []
        for i, bar in enumerate(bars):
            exit_signal = exit_signals[i] if exit_signals is not None else False
            closed = self.push(bar, entry_signal=entry_signals[i], exit_signal=exit_signal)
            if closed is not None:
                trades.append(closed)
        final = self.finish()
        if final is not None:
            trades.append(final)
        return trades

    # ── Session-aware forced-exit boundary ─────────────────────────────────

    def _forced_exit_seconds(self, bar_day_ist: date) -> int | None:
        """Resolve ``bar_day_ist``'s forced-exit boundary (seconds-since-midnight IST).

        Returns the bar session's own close − 10min: 15:20 for a regular day,
        the early close − 10min for a half-day / Muhurat session
        (DEC-SESSION-FORCED-EXIT-001). Returns ``None`` when the IST date has no
        trading session (:func:`~brutex.calendars.nse.get_session` ``None``):
        ``is_forced_exit_bar`` then falls back to its standard 15:20 boundary, so
        a bar dated on a non-session day is handled deterministically rather than
        crashing — a real, contiguous intraday feed never hits this, but a
        crafted / malformed feed must not raise.

        Memoises the LAST IST date only (O(1) per bar, O(1) space): a
        chronological feed re-resolves only on the first bar of each new day.
        """
        if bar_day_ist != self._fe_cache_date:
            session = get_session(bar_day_ist)
            self._fe_cache_date = bar_day_ist
            self._fe_cache_seconds = (
                None if session is None else forced_exit_seconds_for_session(session)
            )
        return self._fe_cache_seconds

    # ── Streaming path ────────────────────────────────────────────────────

    def push(
        self,
        bar: CandleBar,
        *,
        entry_signal: bool,
        exit_signal: bool = False,
    ) -> ClosedTrade | None:
        """Advance one bar; return a :class:`ClosedTrade` iff a position closed on it.

        Parameters
        ----------
        bar
            The next bar in chronological order (UTC timestamp, Decimal rupees).
        entry_signal
            ``True`` iff an entry fired on this bar (enter at the NEXT bar's
            open). Ignored while a position is open, and ignored on a bar where
            a position just closed (the exit bar is consumed).
        exit_signal
            ``True`` iff a Mode-B signal exit fired on this bar (exit at the
            NEXT bar's open). Only meaningful while a position is open.

        Returns
        -------
        ClosedTrade | None
            The trade that closed on this bar, or ``None``.
        """
        self._index += 1
        i = self._index
        # The forced-exit check (UTC→IST + tf-aware compare) is only consulted
        # when opening a pending entry or managing an open position, so skip it
        # entirely on a flat, no-pending bar (keeps the idle path cheap).
        need_forced = self._position is not None or self._pending_entry
        if need_forced:
            ts_ist = bar.timestamp.astimezone(IST)
            forced = is_forced_exit_bar(
                ts_ist.time(),
                self._tf_minutes,
                self._forced_exit_seconds(ts_ist.date()),
            )
        else:
            forced = False

        # (A) Open a pending entry at THIS bar's open (cannot open at/after 15:20).
        if self._position is None and self._pending_entry:
            self._pending_entry = False
            if not forced:
                self._open_position(bar, i)

        # (B) Manage an open position on this bar.
        closed: ClosedTrade | None = None
        if self._position is not None:
            closed = self._manage_position(
                self._position, bar, i, forced=forced, exit_signal=exit_signal
            )

        # (C) While flat (and not on a bar a trade just closed), arm the next entry.
        if self._position is None and closed is None and entry_signal:
            self._pending_entry = True

        return closed

    def finish(self) -> ClosedTrade | None:
        """Close any still-open position at the last bar's CLOSE (end of data).

        Call once after the final :meth:`push`. A position can still be open at
        end of data when the feed stops before 15:20 IST and no stop / target /
        signal fired (or a next-open exit was pending with no next bar). Returns
        ``None`` when the walker is already flat.
        """
        pos = self._position
        if pos is None:
            return None
        return self._close(
            pos,
            exit_price_paisa=_price_to_paisa(pos.last_bar.close),
            exit_ts=pos.last_bar.timestamp,
            exit_idx=pos.last_idx,
            reason=_ExitReason.END_OF_DATA,
        )

    # ── Internals ─────────────────────────────────────────────────────────

    def _open_position(self, bar: CandleBar, idx: int) -> None:
        """Open a position at ``bar``'s OPEN (the next-bar-open entry fill)."""
        entry_price = _price_to_paisa(bar.open)
        sl_level, tp_level = self._levels(entry_price)
        self._position = _OpenPosition(
            entry_idx=idx,
            entry_price_paisa=entry_price,
            entry_ts=bar.timestamp,
            sl_level_paisa=sl_level,
            tp_level_paisa=tp_level,
            mfe_paisa=0,
            mae_paisa=0,
            last_idx=idx,
            last_bar=bar,
        )
        _LOG.debug(
            "trade_sim.entry",
            idx=idx,
            direction=self._direction.value,
            entry_price_paisa=entry_price,
        )

    def _levels(self, entry_price: int) -> tuple[int | None, int | None]:
        """Return ``(sl_level, tp_level)`` in paisa for the configured direction."""
        if self._direction is Side.BUY:
            sl = entry_price - self._sl_paisa if self._sl_paisa is not None else None
            tp = entry_price + self._tp_paisa if self._tp_paisa is not None else None
        else:  # Side.SELL — short: stop is above, target is below.
            sl = entry_price + self._sl_paisa if self._sl_paisa is not None else None
            tp = entry_price - self._tp_paisa if self._tp_paisa is not None else None
        return sl, tp

    def _manage_position(
        self, pos: _OpenPosition, bar: CandleBar, idx: int, *, forced: bool, exit_signal: bool
    ) -> ClosedTrade | None:
        """Process one in-position bar: excursion, exits, next-open scheduling.

        ``pos`` is the live (non-``None``) position the caller already narrowed,
        so there is no defensive ``None`` re-check here.
        """
        pos.last_idx = idx
        pos.last_bar = bar

        # (1) A next-open exit scheduled on a prior bar fills at THIS bar's open.
        if self._pending_exit is not None:
            reason = self._pending_exit
            self._pending_exit = None
            open_paisa = _price_to_paisa(bar.open)
            self._update_excursion(pos, open_paisa, open_paisa)
            return self._close(
                pos, exit_price_paisa=open_paisa, exit_ts=bar.timestamp, exit_idx=idx, reason=reason
            )

        # (2) Forced 15:20 IST exit overrides everything — fill at the open.
        if forced:
            open_paisa = _price_to_paisa(bar.open)
            self._update_excursion(pos, open_paisa, open_paisa)
            return self._close(
                pos,
                exit_price_paisa=open_paisa,
                exit_ts=bar.timestamp,
                exit_idx=idx,
                reason=_ExitReason.FORCED,
            )

        # (3) Full-range in-position bar: fold its high/low into MFE/MAE.
        high_paisa = _price_to_paisa(bar.high)
        low_paisa = _price_to_paisa(bar.low)
        self._update_excursion(pos, high_paisa, low_paisa)

        # (4) Stop-loss / take-profit. ``_resolve_level_hit`` returns the chosen
        # leg AND its exact level price (never None), so the fill site needs no
        # re-check (keeps branch coverage tight).
        hit = self._resolve_level_hit(pos, high_paisa, low_paisa)
        if hit is not None:
            reason, level_paisa = hit
            fill = (
                self._policy.stop_loss
                if reason is _ExitReason.STOP_LOSS
                else self._policy.take_profit
            )
            if fill is LevelFill.AT_LEVEL:
                return self._close(
                    pos,
                    exit_price_paisa=level_paisa,
                    exit_ts=bar.timestamp,
                    exit_idx=idx,
                    reason=reason,
                )
            # LevelFill.NEXT_OPEN — fill on the next bar's open.
            self._pending_exit = reason
            return None

        # (5) Mode-B signal exit — fill on the next bar's open.
        if exit_signal:
            self._pending_exit = _ExitReason.SIGNAL
            return None

        # (6) Still open.
        return None

    def _resolve_level_hit(
        self, pos: _OpenPosition, high_paisa: int, low_paisa: int
    ) -> tuple[_ExitReason, int] | None:
        """Decide which of SL/TP a bar's ``[low, high]`` touched (same-bar tie applied).

        Returns ``(reason, level_paisa)`` — the chosen leg and the exact price it
        fills at under :data:`LevelFill.AT_LEVEL` — or ``None`` if neither level
        was touched. Each leg's tuple is built inside its own ``is not None``
        guard, so ``level_paisa`` is always a live (non-``None``) level and the
        fill site never re-checks.
        """
        sl = pos.sl_level_paisa
        tp = pos.tp_level_paisa
        buy = self._direction is Side.BUY
        stop: tuple[_ExitReason, int] | None = None
        target: tuple[_ExitReason, int] | None = None
        if sl is not None and (low_paisa <= sl if buy else high_paisa >= sl):
            stop = (_ExitReason.STOP_LOSS, sl)
        if tp is not None and (high_paisa >= tp if buy else low_paisa <= tp):
            target = (_ExitReason.TAKE_PROFIT, tp)
        if stop is not None and target is not None:
            # Same bar spans both levels — break the tie by policy.
            return stop if self._policy.same_bar is SameBarTie.SL_FIRST else target
        return stop if stop is not None else target

    def _update_excursion(self, pos: _OpenPosition, high_paisa: int, low_paisa: int) -> None:
        """Fold a ``(high, low)`` pair into the running MFE/MAE (both ``>= 0``)."""
        if self._direction is Side.BUY:
            favourable = high_paisa - pos.entry_price_paisa
            adverse = pos.entry_price_paisa - low_paisa
        else:  # Side.SELL — profit when price falls.
            favourable = pos.entry_price_paisa - low_paisa
            adverse = high_paisa - pos.entry_price_paisa
        pos.mfe_paisa = max(pos.mfe_paisa, favourable)
        pos.mae_paisa = max(pos.mae_paisa, adverse)

    def _close(
        self,
        pos: _OpenPosition,
        *,
        exit_price_paisa: int,
        exit_ts: datetime,
        exit_idx: int,
        reason: _ExitReason,
    ) -> ClosedTrade:
        """Build the :class:`ClosedTrade`, flatten the walker, and emit a log line."""
        # Lazy import (see the TYPE_CHECKING note above): keeps numpy out of the
        # ``brutex.engine`` collection-time import chain so the per-module
        # coverage gates don't trip numpy's "load once" guard. Cached after the
        # first trade closes, so this stays O(1) on the per-trade path.
        from brutex.metrics.types import ClosedTrade

        if self._direction is Side.BUY:
            pnl = exit_price_paisa - pos.entry_price_paisa
        else:  # Side.SELL
            pnl = pos.entry_price_paisa - exit_price_paisa
        trade = ClosedTrade(
            pnl_paisa=pnl,
            mfe_paisa=pos.mfe_paisa,
            mae_paisa=pos.mae_paisa,
            holding_bars=exit_idx - pos.entry_idx + 1,
            entry_ts=pos.entry_ts,
            exit_ts=exit_ts,
            direction=self._direction,
        )
        self._position = None
        _LOG.debug(
            "trade_sim.exit",
            exit_idx=exit_idx,
            reason=reason.value,
            pnl_paisa=pnl,
            holding_bars=trade.holding_bars,
        )
        return trade


def simulate_trades(
    bars: Sequence[CandleBar],
    entry_signals: Sequence[bool],
    *,
    direction: Side,
    tf_minutes: int,
    stop_loss_paisa: int | None = None,
    take_profit_paisa: int | None = None,
    exit_signals: Sequence[bool] | None = None,
    policy: FillPolicy = DEFAULT_FILL_POLICY,
) -> list[ClosedTrade]:
    """Walk a feed into closed trades — the one-call batch wrapper over :class:`TradeSimWalker`.

    Parameters
    ----------
    bars
        Chronologically ordered bars for one ``(instrument, interval)``.
    entry_signals
        One ``bool`` per bar (enter at the next bar's open). Same length as
        ``bars``.
    direction
        :data:`Side.BUY` (long) or :data:`Side.SELL` (short).
    tf_minutes
        Bar timeframe in whole minutes (``>= 1``).
    stop_loss_paisa, take_profit_paisa
        Stop / target distances from entry in paisa (``> 0``), or ``None`` to
        disable that leg.
    exit_signals
        Optional Mode-B exit stream (exit at the next bar's open). Same length
        as ``bars`` when supplied.
    policy
        The :class:`FillPolicy` (defaults to :data:`DEFAULT_FILL_POLICY`).

    Returns
    -------
    list[ClosedTrade]
        The closed round-trips in the order they closed.
    """
    walker = TradeSimWalker(
        direction=direction,
        tf_minutes=tf_minutes,
        stop_loss_paisa=stop_loss_paisa,
        take_profit_paisa=take_profit_paisa,
        policy=policy,
    )
    return walker.run(bars, entry_signals, exit_signals)


__all__ = [
    "DEFAULT_FILL_POLICY",
    "FillPolicy",
    "LevelFill",
    "SameBarTie",
    "TradeSimWalker",
    "simulate_trades",
]
