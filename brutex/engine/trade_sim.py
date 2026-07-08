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
and money stays in **int64 paisa** end-to-end. Historical note: it
deliberately never imported the old ``ExecutionSimulator`` (which filled at
bar CLOSE and applied 5 bps slippage + ₹20 brokerage in ``Decimal`` via the
dormant, stale ``execution.costs`` model — wrong for spot, and NOT the
Track-2 cost authority either); that deprecated trio was DELETED per
DEC-EXECUTION-TRIO-DELETE-001, and the authoritative options cost model is
:func:`brutex.costs.calculator.compute_trade_cost` per DEC-COST-001. P&L is
the per-unit directional price move in paisa (no position sizing — spot is
research, not execution).

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
    get_session_checked,
    is_forced_exit_bar,
)
from brutex.engine.exit_index import bars_strictly_increasing

# Intentional friend access (DEC-P0-017 per-site convention): ``_price_to_paisa``
# is the canonical Decimal-rupees → int64-paisa seam (one source of truth,
# CLAUDE.md §11 COMMON — ``brutex.money`` only converts paisa→rupees). The
# Stage-2 walker reuses it rather than redefining the rounding rule (the task's
# explicit reuse instruction); the sibling-private import is deliberate.
from brutex.engine.stage1a import _price_to_paisa  # pyright: ignore[reportPrivateUsage]
from brutex.strategies.types import Side

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from brutex.data.models import CandleBar
    from brutex.engine.reference_series import TradeReferenceStamper

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
    """Why a position closed (structlog + the optional :class:`TradeDetail` sink).

    The enum stays module-private (no caller constructs one), but its **string
    values are the public exit-reason vocabulary** carried on
    :class:`TradeDetail.exit_reason` and pinned by the P3→P4 trade-row wire
    contract (DEC-SWEEP-RESULTS-DEPTH-001) — see :data:`EXIT_REASONS`. The
    strings are UNCHANGED from their original structlog-only life.
    """

    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    FORCED = "forced_1520"
    SIGNAL = "signal"
    END_OF_DATA = "end_of_data"


#: The public exit-reason string vocabulary, in enum-definition order
#: (DEC-SWEEP-RESULTS-DEPTH-001): ``("stop_loss", "take_profit", "forced_1520",
#: "signal", "end_of_data")``. Downstream persistence (``brutex.engine.trade_store``)
#: validates against THIS tuple — imported, never copied (CLAUDE.md §11 COMMON).
EXIT_REASONS: Final[tuple[str, ...]] = tuple(reason.value for reason in _ExitReason)


@dataclass(frozen=True, slots=True)
class TradeDetail:
    """One closed trade's exit detail — the additive capture-sink record.

    Emitted by :class:`TradeSimWalker` into an optional ``detail_sink`` list
    (DEC-SWEEP-RESULTS-DEPTH-001 WP1): the walker already KNOWS each trade's
    exit reason and exact fill prices at the single ``_close`` seam but
    historically discarded them (the reason was structlog-only; the fills were
    a "tracked follow-up" per ``sweep_run.ranked_combo_to_result_row``). The
    sink surfaces them WITHOUT touching :class:`~brutex.metrics.types.ClosedTrade`
    or the Rust ``TradeRecord`` tuple — a pure additive annotation, so every
    existing caller (and the frozen sweep goldens) is byte-identical.

    Parameters
    ----------
    trade
        The :class:`~brutex.metrics.types.ClosedTrade` this detail annotates —
        the SAME object the walker returned, positionally aligned with the
        walker's output (detail ``i`` annotates trade ``i``).
    exit_reason
        Why the position closed — one of :data:`EXIT_REASONS`.
    entry_fill_paisa
        The exact entry fill price (int64 paisa) — the entry bar's OPEN.
    exit_fill_paisa
        The exact exit fill price (int64 paisa) — the level / open / close the
        trade actually filled at. Invariant (by :meth:`TradeSimWalker._close`'s
        P&L arithmetic): ``pnl_paisa == exit − entry`` for BUY and
        ``entry − exit`` for SELL.
    """

    trade: ClosedTrade
    exit_reason: str
    entry_fill_paisa: int
    exit_fill_paisa: int


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

    Feed contract: the caller supplies a chronologically ordered intraday feed
    for one instrument (the lake / :class:`~brutex.feed.replay.ReplayFeed`
    already do). An armed entry fills at the *very next bar's* open ONLY when
    that bar falls on the SAME IST session date the signal armed on: a pending
    entry whose fill bar lands on a LATER session date (the feed stepped across
    a trading-day boundary — an overnight gap) is DROPPED, never filled at the
    next session's open (R2-OC-2). The session identity is the bar's IST
    calendar date — exactly the key the session forced-exit resolution uses
    (:meth:`_forced_exit_seconds` / :func:`~brutex.calendars.nse.get_session`)
    — so the two session notions can never diverge. Within a session, the
    forced exit (15:20 IST on a regular day, close − 10min on a half-day /
    Muhurat session) additionally drops an entry signal armed on the session's
    last usable bar (its next bar is a forced-exit bar), so no entry can open
    at or after the forced-exit instant.

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
    reference_stamper
        Optional :class:`~brutex.engine.reference_series.TradeReferenceStamper`.
        When supplied, every emitted :class:`ClosedTrade` is stamped with the
        India VIX level at its entry + exit instants (``vix_at_entry`` /
        ``vix_at_exit``) — REFERENCE ONLY, never a signal input (CLAUDE.md
        §1/§3; DEC-VIX-TRADE-STAMP-001). ``None`` (the default) leaves both VIX
        fields ``None``. A VIX bar missing at an instant stamps ``None`` there
        (never a fabricated value) and the stamper warns; the engine output is
        otherwise byte-identical with or without the stamper, so it is a pure
        downstream-analysis annotation.
    detail_sink
        Optional caller-owned list the walker appends one :class:`TradeDetail`
        to per closed trade (positionally aligned with the returned trades) —
        the exit reason + exact fill prices the ``_close`` seam already knew
        (DEC-SWEEP-RESULTS-DEPTH-001 WP1). ``None`` (the default) skips the
        append entirely: the walker's returned trades are byte-identical with
        or without a sink (regression-pinned), so every existing caller and
        the frozen sweep goldens are untouched.

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
        reference_stamper: TradeReferenceStamper | None = None,
        detail_sink: list[TradeDetail] | None = None,
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
        self._reference_stamper = reference_stamper
        self._detail_sink = detail_sink
        self._position: _OpenPosition | None = None
        self._pending_entry = False
        # The IST session date the pending entry ARMED on (``None`` while no
        # entry is pending). An armed entry may only fill on a bar of the SAME
        # IST session date — a fill bar on a later date is an overnight-gap
        # fill and is dropped, never opened (R2-OC-2). The IST calendar date is
        # exactly the key the session forced-exit resolution uses
        # (``_forced_exit_seconds`` / ``get_session``), so the drop rule and
        # the forced-exit rule share ONE session notion (CLAUDE.md §11 COMMON).
        self._pending_entry_day: date | None = None
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
        # Bar index of the most recently CLOSED trade — recorded by ``_close``
        # (the one seam every exit flows through), read by
        # ``run_with_exit_indices`` right after a push/finish returns a trade.
        # ``-1`` = no trade has closed yet; never read before a close by
        # construction (DEC-SWEEP-EXIT-INDEX-FASTPATH-001).
        self._last_exit_idx: int = -1

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
        trades, _ = self.run_with_exit_indices(bars, entry_signals, exit_signals)
        return trades

    def run_with_exit_indices(
        self,
        bars: Sequence[CandleBar],
        entry_signals: Sequence[bool],
        exit_signals: Sequence[bool] | None = None,
    ) -> tuple[list[ClosedTrade], list[int]]:
        """Walk a whole feed; return the closed trades AND each trade's exit bar index.

        The index-carrying batch walk (DEC-SWEEP-EXIT-INDEX-FASTPATH-001):
        identical to :meth:`run` — same state machine, same trades in the
        same order — plus a positionally aligned list of each trade's exit
        bar INDEX within *bars* (the value ``_close`` already knew and then
        discarded). :meth:`run` is a thin delegate over this method, so the
        two batch entries can never drift.

        The returned indices are the walker's TRUE exit indices. Whether
        they may substitute for the ``exit_ts`` dict lookup downstream is a
        separate certification (:func:`~brutex.engine.exit_index.bars_strictly_increasing`)
        the module-level :func:`simulate_trades_with_exit_indices` applies —
        this method just reports what happened.

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
        exit_indices: list[int] = []
        for i, bar in enumerate(bars):
            exit_signal = exit_signals[i] if exit_signals is not None else False
            closed = self.push(bar, entry_signal=entry_signals[i], exit_signal=exit_signal)
            if closed is not None:
                trades.append(closed)
                exit_indices.append(self._last_exit_idx)
        final = self.finish()
        if final is not None:
            trades.append(final)
            exit_indices.append(self._last_exit_idx)
        return trades, exit_indices

    # ── Session-aware forced-exit boundary ─────────────────────────────────

    def _forced_exit_seconds(self, bar_day_ist: date) -> int | None:
        """Resolve ``bar_day_ist``'s forced-exit boundary (seconds-since-midnight IST).

        Returns the bar session's own close − 10min: 15:20 for a regular day,
        the early close − 10min for a half-day / Muhurat session
        (DEC-SESSION-FORCED-EXIT-001). Returns ``None`` when the IST date has no
        trading session (:func:`~brutex.calendars.nse.get_session_checked`
        ``None`` — an IN-COVERAGE holiday / weekend): ``is_forced_exit_bar``
        then falls back to its standard 15:20 boundary, so a bar dated on a
        known non-session day is handled deterministically rather than
        crashing — a real, contiguous intraday feed never hits this, but a
        crafted / malformed feed on an IN-COVERAGE date must not raise.

        LOUD out-of-coverage refusal (calendar-correctness pack): a bar dated on
        a weekday OUTSIDE the published holiday span raises
        :class:`~brutex.calendars.nse.HolidayDataUnavailableError` (via the
        strict ``get_session_checked``) instead of silently fabricating a
        REGULAR 15:20 boundary — a silently-wrong forced-exit classification
        corrupts a backtest (charter: Correctness first). The engine's tuple
        isolation (DEC-ENGINE-TUPLE-SKIP-RESILIENT-001) surfaces the raise as a
        loud ``TUPLE_FAILED``, never a silent wrong number.

        Memoises the LAST IST date only (O(1) per bar, O(1) space): a
        chronological feed re-resolves only on the first bar of each new day.
        """
        if bar_day_ist != self._fe_cache_date:
            session = get_session_checked(bar_day_ist)
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
            open — dropped, never filled, if that bar falls on a LATER IST
            session date or is a forced-exit bar). Ignored while a position is
            open, and ignored on a bar where a position just closed (the exit
            bar is consumed).
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
            bar_day_ist: date | None = ts_ist.date()
            forced = is_forced_exit_bar(
                ts_ist.time(),
                self._tf_minutes,
                self._forced_exit_seconds(ts_ist.date()),
            )
        else:
            bar_day_ist = None
            forced = False

        # (A) Open a pending entry at THIS bar's open — only within the SAME IST
        # session date the signal armed on (a fill bar on a LATER session date is
        # an overnight-gap fill and is DROPPED, R2-OC-2) and never on a forced
        # bar (cannot open at/after the session's forced-exit instant).
        # ``bar_day_ist`` is always resolved here (``need_forced`` was True).
        if self._position is None and self._pending_entry:
            self._pending_entry = False
            same_session = bar_day_ist == self._pending_entry_day
            self._pending_entry_day = None
            if not forced and same_session:
                self._open_position(bar, i)

        # (B) Manage an open position on this bar.
        closed: ClosedTrade | None = None
        if self._position is not None:
            closed = self._manage_position(
                self._position, bar, i, forced=forced, exit_signal=exit_signal
            )

        # (C) While flat (and not on a bar a trade just closed), arm the next
        # entry, recording THIS bar's IST session date — the fill gate for (A).
        # The session identity of bar i derives from bar i's own timestamp, so
        # it is knowable AT bar i (P-06: no future access). Reuse the date
        # already resolved for the forced check when present (O(1) discipline —
        # no second UTC→IST conversion on a bar that needed the forced check).
        if self._position is None and closed is None and entry_signal:
            self._pending_entry = True
            self._pending_entry_day = (
                bar_day_ist if bar_day_ist is not None else bar.timestamp.astimezone(IST).date()
            )

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
        # Reference-series stamp (DEC-VIX-TRADE-STAMP-001): stamp the India VIX
        # level at entry + exit when a stamper is wired, else leave both None. A
        # missing VIX bar stamps None (no hallucination, Golden Rule #1).
        if self._reference_stamper is not None:
            vix_at_entry, vix_at_exit = self._reference_stamper.stamp(pos.entry_ts, exit_ts)
        else:
            vix_at_entry, vix_at_exit = None, None
        trade = ClosedTrade(
            pnl_paisa=pnl,
            mfe_paisa=pos.mfe_paisa,
            mae_paisa=pos.mae_paisa,
            holding_bars=exit_idx - pos.entry_idx + 1,
            entry_ts=pos.entry_ts,
            exit_ts=exit_ts,
            direction=self._direction,
            vix_at_entry=vix_at_entry,
            vix_at_exit=vix_at_exit,
        )
        # Additive capture sink (DEC-SWEEP-RESULTS-DEPTH-001 WP1): surface the
        # reason + exact fills this seam already computed. ``None`` (the
        # default) is a pure skip — the returned trade is byte-identical either
        # way, so the frozen goldens never move.
        if self._detail_sink is not None:
            self._detail_sink.append(
                TradeDetail(
                    trade=trade,
                    exit_reason=reason.value,
                    entry_fill_paisa=pos.entry_price_paisa,
                    exit_fill_paisa=exit_price_paisa,
                )
            )
        self._position = None
        self._last_exit_idx = exit_idx
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
    reference_stamper: TradeReferenceStamper | None = None,
    detail_sink: list[TradeDetail] | None = None,
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
    reference_stamper
        Optional India VIX
        :class:`~brutex.engine.reference_series.TradeReferenceStamper` — stamps
        ``vix_at_entry`` / ``vix_at_exit`` on every trade (REFERENCE ONLY,
        DEC-VIX-TRADE-STAMP-001). ``None`` leaves both VIX fields ``None``.
    detail_sink
        Optional caller-owned list receiving one :class:`TradeDetail` per
        closed trade (exit reason + exact fills, positionally aligned with the
        returned trades — DEC-SWEEP-RESULTS-DEPTH-001 WP1). ``None`` (the
        default) is byte-identical to the pre-seam behaviour.

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
        reference_stamper=reference_stamper,
        detail_sink=detail_sink,
    )
    return walker.run(bars, entry_signals, exit_signals)


def simulate_trades_with_exit_indices(
    bars: Sequence[CandleBar],
    entry_signals: Sequence[bool],
    *,
    direction: Side,
    tf_minutes: int,
    stop_loss_paisa: int | None = None,
    take_profit_paisa: int | None = None,
    exit_signals: Sequence[bool] | None = None,
    policy: FillPolicy = DEFAULT_FILL_POLICY,
    reference_stamper: TradeReferenceStamper | None = None,
    detail_sink: list[TradeDetail] | None = None,
) -> tuple[list[ClosedTrade], list[int] | None]:
    """Walk a feed into closed trades PLUS their certified exit bar indices.

    The index-carrying twin of :func:`simulate_trades`
    (DEC-SWEEP-EXIT-INDEX-FASTPATH-001): the trades are byte-identical to
    :func:`simulate_trades` on the same inputs (both ride ONE
    :meth:`TradeSimWalker.run_with_exit_indices` walk). The second element
    is each trade's exit bar index, returned iff the window is CERTIFIED —
    :func:`~brutex.engine.exit_index.bars_strictly_increasing` — so that the
    index provably equals the last-wins ``exit_ts`` dict lookup the metrics
    tail would otherwise perform; an uncertified (duplicate/reordered
    timestamp) window returns ``None`` and the consumer stays on the
    bit-identical dict path. Honest cost note: the certification is ONE
    extra ``O(N_bars)`` timestamp scan on top of the already-``O(N_bars)``
    Python walk — a small constant, paid only by callers that WANT indices.

    Parameters are exactly :func:`simulate_trades`'s.

    Returns
    -------
    tuple[list[ClosedTrade], list[int] | None]
        ``(trades, exit_indices)`` — positionally aligned; ``exit_indices``
        is ``None`` for an uncertified window.
    """
    certified = bars_strictly_increasing(bars)
    walker = TradeSimWalker(
        direction=direction,
        tf_minutes=tf_minutes,
        stop_loss_paisa=stop_loss_paisa,
        take_profit_paisa=take_profit_paisa,
        policy=policy,
        reference_stamper=reference_stamper,
        detail_sink=detail_sink,
    )
    trades, exit_indices = walker.run_with_exit_indices(bars, entry_signals, exit_signals)
    return trades, exit_indices if certified else None


__all__ = [
    "DEFAULT_FILL_POLICY",
    "EXIT_REASONS",
    "FillPolicy",
    "LevelFill",
    "SameBarTie",
    "TradeDetail",
    "TradeSimWalker",
    "simulate_trades",
    "simulate_trades_with_exit_indices",
]
