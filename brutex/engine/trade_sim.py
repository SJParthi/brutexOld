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
conservative choice (DEC-TRADESIM-001). Orthogonally, the policy carries the
global :class:`~brutex.execution.fill_mode.FillMode`
(DEC-FILL-WORST-CASE-001): under ``WORST_CASE`` every fill PRICE below is
replaced by the **adverse-max law** — entry / forced / next-open / signal
fills at the fill bar's adverse extreme (BUY at HIGH, SELL at LOW for entry;
the mirror for exits), an AT_LEVEL SL/TP fill at ``min(level, bar low)``
(BUY exit) / ``max(level, bar high)`` (SELL exit), and end-of-data /
session-gap at the last stored bar's adverse extreme — while every TIMING
rule, trigger comparison, exit reason, excursion fold, and exit-index
convention stays byte-identical to ``PRECISE`` (the library default):

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
* **Session boundary** — *locked*: an open position never survives an IST
  session-date change (DEC-TRADESIM-OPEN-SESSION-GUARD-001). When a session's
  stored bars end BEFORE any forced-exit-window bar (a session-tail data hole,
  or a stitched-series roll gap), the first bar of the NEXT session closes the
  position retroactively at the PRIOR session's last stored bar's CLOSE — the
  same pricing as end of data — under the ``session_gap`` reason; a pending
  next-open exit scheduled on the dead session is subsumed, and the boundary
  bar itself never arms a new entry.

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
from typing import TYPE_CHECKING, Final, cast

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
from brutex.execution.fill_mode import FillMode
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
    module (WHERE a fill happens — bar + event — is always the locked rule;
    ``fill_mode`` moves only the PRICE chosen within that bar,
    DEC-FILL-WORST-CASE-001):

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
    fill_mode
        The global :class:`~brutex.execution.fill_mode.FillMode`
        (DEC-FILL-WORST-CASE-001). The LIBRARY-level default is
        :data:`~brutex.execution.fill_mode.FillMode.PRECISE` so every direct
        construction — and every historical golden — is byte-identical
        legacy; the WORST_CASE production default is applied at the CONFIG
        layer (``SweepRunConfig`` / ``SweepGridConfig`` / the CLI launch),
        never silently here. Under ``WORST_CASE`` the module-docstring
        adverse-max law replaces every fill PRICE; timing / triggers /
        reasons / excursion folds are untouched. NOTE the honest interaction
        with SL/TP anchoring: levels anchor on the ACTUAL entry fill, so
        under ``WORST_CASE`` they anchor on the adverse-extreme entry —
        trigger timing can therefore differ between modes (documented,
        accepted — the honest simulation of the worst realized entry).
    """

    stop_loss: LevelFill = LevelFill.AT_LEVEL
    take_profit: LevelFill = LevelFill.AT_LEVEL
    same_bar: SameBarTie = SameBarTie.SL_FIRST
    fill_mode: FillMode = FillMode.PRECISE

    def __post_init__(self) -> None:
        # DEC-FILL-WORST-CASE-001 fix round 2 (NIT-4): a static type is NOT a
        # runtime guard — a junk fill_mode string would compare
        # `is FillMode.WORST_CASE` False at every fill site and SILENTLY walk
        # PRECISE (the exact silent-fallback trap the fail-loud resolver
        # exists to prevent). The cast defeats mypy's annotation-trusting
        # narrowing so the guard is not "unreachable" (the FuturesTradeInput
        # precedent).
        if not isinstance(cast("object", self.fill_mode), FillMode):
            msg = (
                f"FillPolicy.fill_mode {self.fill_mode!r} is not a FillMode member; "
                f"allowed: {sorted(m.value for m in FillMode)}"
            )
            raise ValueError(msg)


#: The conservative default fill policy (AT_LEVEL SL/TP + pessimistic same-bar,
#: PRECISE legacy fill prices — the library-level byte-identical default).
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
    # A session-tail data GAP: the position's session ended in the stored feed
    # BEFORE any forced-exit-window bar, so the first bar of the NEXT session
    # closed it retroactively at the prior session's last stored bar
    # (DEC-TRADESIM-OPEN-SESSION-GUARD-001). Appended LAST — the vocabulary is
    # append-only (wire-visible via the P3→P4 trade-row contract).
    SESSION_GAP = "session_gap"


#: The public exit-reason string vocabulary, in enum-definition order
#: (DEC-SWEEP-RESULTS-DEPTH-001): ``("stop_loss", "take_profit", "forced_1520",
#: "signal", "end_of_data", "session_gap")``. APPEND-ONLY — every member is
#: wire-visible (trade rows, per-reason aggregate counters, the top100 exits
#: dict, the display exit-mix line). Downstream persistence
#: (``brutex.engine.trade_store``) validates against THIS tuple — imported,
#: never copied (CLAUDE.md §11 COMMON).
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
        The exact entry fill price (int64 paisa) — under
        :data:`~brutex.execution.fill_mode.FillMode.PRECISE` the entry bar's
        OPEN; under ``WORST_CASE`` the entry bar's adverse extreme (HIGH for
        BUY, LOW for SELL — the DEC-FILL-WORST-CASE-001 adverse-max law).
    exit_fill_paisa
        The exact exit fill price (int64 paisa) — the price the trade
        actually filled at under the walk's fill mode (PRECISE: the level /
        open / close the event's locked rule names; WORST_CASE: the
        adverse-max counterpart). Invariant (by
        :meth:`TradeSimWalker._close`'s P&L arithmetic): ``pnl_paisa ==
        exit − entry`` for BUY and ``entry − exit`` for SELL.
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
    # The IST session date the position OPENED on — the open-position session
    # guard closes the position retroactively at the prior session's last
    # stored bar the moment a pushed bar's IST date differs
    # (DEC-TRADESIM-OPEN-SESSION-GUARD-001). Stamped from the SAME
    # ``bar_day_ist`` the (A) fill site already resolved — one session notion.
    entry_day_ist: date
    sl_level_paisa: int | None
    tp_level_paisa: int | None
    mfe_paisa: int  # running max favourable excursion (>= 0)
    mae_paisa: int  # running max adverse excursion magnitude (>= 0)
    # Most-recent in-position bar — the end-of-data fallback AND the (A2)
    # session-boundary close both price against it (the finish() idiom).
    # Held as the bar itself (not pre-converted paisa) so the hot per-bar path
    # avoids a Decimal→paisa conversion of every CLOSE; the conversion happens
    # once, only if the position survives to end of data / a session boundary
    # (DEC-TRADESIM-001).
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
    at or after the forced-exit instant. The OPEN-position twin of the entry
    gate (DEC-TRADESIM-OPEN-SESSION-GUARD-001): a position never survives an
    IST session-date change either — when the feed's next bar lands on a later
    session date (the session's stored tail never reached a forced-exit bar),
    the position closes retroactively at the prior session's last stored bar's
    CLOSE (the ``finish()`` pricing) under the ``session_gap`` reason, any
    pending next-open exit is subsumed, and the boundary bar never arms a new
    entry (earliest re-arm: the boundary bar's successor).

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
        # Cached once (O(1) per bar discipline): the adverse-max law engages
        # only under WORST_CASE (DEC-FILL-WORST-CASE-001); PRECISE keeps every
        # fill price byte-identical legacy.
        self._worst_case = policy.fill_mode is FillMode.WORST_CASE
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
            The trade that closed on this bar, or ``None``. A ``session_gap``
            close is EMITTED on this bar (the first bar of a new session) but
            PRICED at the prior session's last stored bar — the finish() idiom
            (DEC-TRADESIM-OPEN-SESSION-GUARD-001).
        """
        self._index += 1
        i = self._index
        # The forced-exit check (UTC→IST + tf-aware compare) is only consulted
        # when opening a pending entry or managing an open position, so skip it
        # entirely on a flat, no-pending bar (keeps the idle path cheap). The
        # whole (A)/(A2)/(B) ladder lives INSIDE this arm — each of its guards
        # requires a position or a pending entry, so a flat, no-pending bar
        # never reaches it, and the arm's non-Optional ``day_ist`` local is the
        # (A)/(A2) session key (the Optional ``bar_day_ist`` mirror survives
        # solely for step (C)'s reuse-or-resolve fallback expression).
        bar_day_ist: date | None = None
        closed: ClosedTrade | None = None
        if self._position is not None or self._pending_entry:
            ts_ist = bar.timestamp.astimezone(IST)
            day_ist = ts_ist.date()
            bar_day_ist = day_ist
            forced = is_forced_exit_bar(
                ts_ist.time(),
                self._tf_minutes,
                self._forced_exit_seconds(day_ist),
            )

            # (A) Open a pending entry at THIS bar's open — only within the
            # SAME IST session date the signal armed on (a fill bar on a LATER
            # session date is an overnight-gap fill and is DROPPED, R2-OC-2)
            # and never on a forced bar (cannot open at/after the session's
            # forced-exit instant). (A) and (A2) are mutually exclusive by
            # state invariant: step (C) arms only while ``_position is None``
            # and (A) clears the pending flag before opening, so
            # ``_pending_entry`` is False whenever a position is open — one
            # push runs (A) xor (A2), never both
            # (DEC-TRADESIM-OPEN-SESSION-GUARD-001 §2.2).
            if self._position is None and self._pending_entry:
                self._pending_entry = False
                same_session = day_ist == self._pending_entry_day
                self._pending_entry_day = None
                if not forced and same_session:
                    self._open_position(bar, i, entry_day_ist=day_ist)

            if self._position is not None:
                pos = self._position
                if day_ist != pos.entry_day_ist:
                    # (A2) Session-boundary guard: an open position never
                    # survives an IST session-date change (a session-tail data
                    # hole / stitched-series roll gap —
                    # DEC-TRADESIM-OPEN-SESSION-GUARD-001). Close retroactively
                    # at the PRIOR session's last stored bar — the finish()
                    # idiom (same fill price / ts / idx, no excursion fold from
                    # THIS bar) — BEFORE this bar is managed, so neither a
                    # pending next-open exit nor SL/TP/forced can ever fill
                    # against the overnight/roll gap. A pending exit is
                    # SUBSUMED (cleared — the flat walker never carries it into
                    # the new session). A single-position walker holds at most
                    # one position, and the inequality compare fires exactly
                    # once even across a multi-session hole. A just-opened
                    # position (same push, step (A)) carries THIS bar's date,
                    # so it can never trip the guard.
                    self._pending_exit = None
                    closed = self._close(
                        pos,
                        exit_price_paisa=self._last_bar_exit_paisa(pos),
                        exit_ts=pos.last_bar.timestamp,
                        exit_idx=pos.last_idx,
                        reason=_ExitReason.SESSION_GAP,
                    )
                else:
                    # (B) Manage an open position on this bar — unchanged.
                    closed = self._manage_position(
                        pos, bar, i, forced=forced, exit_signal=exit_signal
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
        """Close any still-open position at the last bar (end of data).

        Call once after the final :meth:`push`. A position can still be open at
        end of data when the feed stops before 15:20 IST and no stop / target /
        signal fired (or a next-open exit was pending with no next bar). Returns
        ``None`` when the walker is already flat. PRECISE prices the close at
        the last bar's CLOSE; WORST_CASE at its adverse extreme
        (DEC-FILL-WORST-CASE-001 — see :meth:`_last_bar_exit_paisa`).
        """
        pos = self._position
        if pos is None:
            return None
        return self._close(
            pos,
            exit_price_paisa=self._last_bar_exit_paisa(pos),
            exit_ts=pos.last_bar.timestamp,
            exit_idx=pos.last_idx,
            reason=_ExitReason.END_OF_DATA,
        )

    # ── Internals ─────────────────────────────────────────────────────────

    def _open_position(self, bar: CandleBar, idx: int, *, entry_day_ist: date) -> None:
        """Open a position on ``bar`` (the next-bar entry fill).

        PRECISE fills at ``bar``'s OPEN (the legacy locked rule); WORST_CASE
        fills at the entry bar's ADVERSE extreme — BUY at HIGH, SELL at LOW
        (the open-anchored arm of the adverse-max law,
        DEC-FILL-WORST-CASE-001). The SL/TP levels anchor on this ACTUAL
        entry fill (:meth:`_levels`), and MFE/MAE measure from it — both by
        construction, no extra wiring.

        ``entry_day_ist`` is the fill bar's IST session date, threaded from the
        (A) fill site — which already resolved it for the forced check — so the
        (A2) open-position session guard costs no extra UTC→IST conversion
        (DEC-TRADESIM-OPEN-SESSION-GUARD-001).
        """
        if self._worst_case:
            entry_price = _price_to_paisa(bar.high if self._direction is Side.BUY else bar.low)
        else:
            entry_price = _price_to_paisa(bar.open)
        sl_level, tp_level = self._levels(entry_price)
        self._position = _OpenPosition(
            entry_idx=idx,
            entry_price_paisa=entry_price,
            entry_ts=bar.timestamp,
            entry_day_ist=entry_day_ist,
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

        # (1) A next-open exit scheduled on a prior bar fills on THIS bar —
        # PRECISE at its open; WORST_CASE at its adverse extreme (the
        # open-anchored adverse-max arm, DEC-FILL-WORST-CASE-001). The
        # excursion fold stays the legacy (open, open) in BOTH modes — the
        # fold logic is deliberately untouched, only the fill PRICE moves.
        if self._pending_exit is not None:
            reason = self._pending_exit
            self._pending_exit = None
            open_paisa = _price_to_paisa(bar.open)
            self._update_excursion(pos, open_paisa, open_paisa)
            return self._close(
                pos,
                exit_price_paisa=self._open_anchored_exit_paisa(bar, open_paisa),
                exit_ts=bar.timestamp,
                exit_idx=idx,
                reason=reason,
            )

        # (2) Forced 15:20 IST exit overrides everything — fill at the open
        # (PRECISE) / the adverse extreme (WORST_CASE); fold unchanged.
        if forced:
            open_paisa = _price_to_paisa(bar.open)
            self._update_excursion(pos, open_paisa, open_paisa)
            return self._close(
                pos,
                exit_price_paisa=self._open_anchored_exit_paisa(bar, open_paisa),
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
                # PRECISE fills exactly at the level. WORST_CASE applies the
                # adverse-max law WITH the level (DEC-FILL-WORST-CASE-001):
                # a BUY exits (sells) at min(level, bar low) — a gap-through
                # bar fills at the extreme, while a gap PAST the level in the
                # trade's favour (e.g. a long-TP bar entirely ABOVE the
                # target, low > tp) stays clamped AT the level so the
                # worst-case fill can never overstate vs today's at-level
                # fill (the proven counterexample); a SELL exits (covers) at
                # max(level, bar high), the exact mirror.
                if self._worst_case:
                    exit_paisa = (
                        min(level_paisa, low_paisa)
                        if self._direction is Side.BUY
                        else max(level_paisa, high_paisa)
                    )
                else:
                    exit_paisa = level_paisa
                return self._close(
                    pos,
                    exit_price_paisa=exit_paisa,
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

    def _open_anchored_exit_paisa(self, bar: CandleBar, open_paisa: int) -> int:
        """The exit fill for an OPEN-anchored event on *bar* (pending / forced).

        PRECISE returns *open_paisa* (the legacy locked rule — the bar's OPEN,
        already converted by the caller so the hot path pays no second
        conversion). WORST_CASE returns the bar's ADVERSE extreme — a BUY
        position exits (sells) at the LOW, a SELL position exits (covers) at
        the HIGH (the open-anchored arm of the adverse-max law, no level
        exists for these events — DEC-FILL-WORST-CASE-001). O(1): at most one
        extra Decimal→paisa conversion, paid only on a worst-case exit bar.
        """
        if not self._worst_case:
            return open_paisa
        return _price_to_paisa(bar.low if self._direction is Side.BUY else bar.high)

    def _last_bar_exit_paisa(self, pos: _OpenPosition) -> int:
        """The end-of-data / session-gap exit fill — the last stored bar's price.

        PRECISE prices the retroactive close at ``pos.last_bar``'s CLOSE (the
        legacy ``finish()`` rule); WORST_CASE at its ADVERSE extreme — BUY at
        LOW, SELL at HIGH (DEC-FILL-WORST-CASE-001). Both are a REAL stored
        price at a REAL stored instant (never fabricated, Golden Rule #1);
        the honest stale-price envelope of the ``session_gap`` close
        (DEC-TRADESIM-OPEN-SESSION-GUARD-001) carries over unchanged.
        """
        bar = pos.last_bar
        if not self._worst_case:
            return _price_to_paisa(bar.close)
        return _price_to_paisa(bar.low if self._direction is Side.BUY else bar.high)

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
