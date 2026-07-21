"""Stage-2 spot trade-simulation walker — decision-TF signals, 1-MINUTE execution grid.

The Stage-1A matrix builder (:mod:`brutex.engine.stage1a`) turns a bar feed into a
per-bar condition bitset, and the Stage-2 supermask kernel
(:func:`brutex.engine.combo_mask.count_supermasks`) turns a strategy mask into a
per-DECISION-bar **entry-signal stream**. This module is the next hop: it walks
the **1-minute EXECUTION stream** (DEC-FILL-EXEC-GRID-1M-001 — the PR-B dual
feed), projecting the decision-TF signals onto their next-1-minute fill bars
through the precomputed :class:`~brutex.engine.exec_grid.ExecGridMap`, into
round-trip :class:`~brutex.metrics.types.ClosedTrade` objects, which
:func:`brutex.metrics.compute.compute_metrics` then turns into Sharpe and the
rest of the result row.

It is a **signal-only** spot simulator (``TRACK1_BACKTEST_SPEC.md`` lines
640-657): the index has no order book, so there is **no slippage, no brokerage,
no STT** and money stays in **int64 paisa** end-to-end. P&L is the per-unit
directional price move in paisa (no position sizing — spot is research, not
execution).

## Fill conventions (HARDCODED — worst-case on the 1-MINUTE grid is the only law)

DEC-FILL-WORST-CASE-ONLY-001 (the adverse-max law) + DEC-FILL-EXEC-GRID-1M-001
(the 1-minute execution grid): whatever TF generates a signal, the fill
resolves on the **next 1-minute bar strictly after the signal bar's CLOSE**,
priced worst-case WITHIN that 1-minute bar — the operator law:

* **Entry** — signal on decision bar ``N`` → fill on 1m bar ``exec_idx(N)``
  (the :mod:`~brutex.engine.exec_grid` mapping — on a hole, the next STORED
  same-session minute: the RATIFIED session-bounded SLIDE law) at that 1m
  bar's ADVERSE extreme — BUY at HIGH, SELL at LOW. SL/TP levels anchor on
  this ACTUAL worst 1m entry fill. Gates: the fill bar shares the arming
  bar's IST session (baked into ``arm_source`` at map build — the kernel FFI
  carries no decision session column) and is not a forced-exit 1m bar.
* **Stop-loss / take-profit** — detection AND fill per 1-MINUTE bar:
  detection on the 1m ``[low, high]`` (inclusive touch); the fill is the
  adverse-max-vs-level ON THAT SAME 1m bar — BUY exit ``min(level, 1m low)``,
  SELL exit ``max(level, 1m high)``. THE GAP-OPEN LAW (design B5): a 1m bar
  gapping past the level fills at the adverse-max vs the level WITHIN that
  bar — never the untouched open, never the level when the extreme is worse.
* **Same-bar SL+TP tie** — one 1m bar spanning BOTH levels fills the STOP
  first, always (the pessimistic tie, unchanged).
* **Same-bar deferral (design §2.4/§3.12)** — the entry 1m bar folds its
  range into MFE/MAE but NEVER exits: SL/TP detection begins on the 1m bar
  AFTER the entry bar (counted ``exits_deferred`` when the entry bar's range
  already touched a level). No fill anywhere depends on an assumed intra-bar
  path order.
* **Forced session-close exit** — the first 1m bar reaching the SESSION's
  forced-exit instant (close − 10min, session-resolved via
  :func:`brutex.calendars.nse.forced_exit_seconds_for_session` — never a
  15:20/375 literal) closes at that 1m bar's adverse extreme; an entry never
  fills at/after the boundary. A missing boundary minute displaces the fill
  to the first STORED at-or-after bar — a SLID fill, counted
  ``forced_fill_slid_minutes``.
* **Mode-B signal exit** — exit signal on decision bar ``N`` → fill on 1m
  bar ``exec_idx(N)`` at its adverse extreme (symmetry with entry). An exit
  due on the trade's OWN entry 1m bar defers to that bar's successor (the
  same-bar deferral law).
* **End of data** — a still-open position closes at the LAST STORED 1m bar's
  adverse extreme — UNLESS that bar IS the trade's entry bar (the legalized
  terminal degenerate): then the fill is the bar's **CLOSE** (the design B3
  carve-out on the #1743 basis — pricing the terminal same-bar close at the
  adverse extreme would be the full-range phantom loss).
* **Session boundary** — an open position never survives an IST session-date
  change ON THE 1M STREAM (DEC-TRADESIM-OPEN-SESSION-GUARD-001 re-keyed):
  the first 1m bar of a new session closes it retroactively at the PRIOR
  session's last stored 1m bar's adverse extreme (the ``finish()`` idiom),
  with the SAME B3 terminal CLOSE carve-out when that bar is the trade's own
  entry bar, under the ``session_gap`` reason; pending exits are subsumed and
  the boundary bar never opens a new fill (earliest re-fill: a later bar).
* **Volume-0 firewall (design §5b)** — a printed 1m bar is REAL + TRADABLE
  regardless of volume (spot indices are structurally volume-less); NO
  volume predicate exists anywhere on this fill path. Deliberately DISTINCT
  from the options/futures EXECUTOR always-reject ruling — two laws, two
  surfaces.

## No look-ahead (CLAUDE.md §4, Golden Rule #5)

The mapping anchors on the decision bucket's CLOSE instant
(``label + tf_minutes``), never its label, so a signal is fully formed
strictly before its fill bar's first trade; the walker is a forward-only
state machine over the 1m stream that never reads a future bar. Property
tests assert the invariant directly (fill-ts ≥ close-instant + dual-stream
suffix independence).

## O(1) per bar (Golden Rule #5)

Every :meth:`TradeSimWalker.push_1m` does constant work — a fixed set of
paisa comparisons and at most one position open/close; there is no scan back
over history and no growing per-bar state. (The returned trade *list* grows
with the number of trades — inherent output, not per-bar cost.) The whole
walk is inherently ``O(N_1m)`` per mask; the Rust kernel's flat-skip lever is
the hot-path mitigation — this Python walker deliberately does NOT flat-skip
(it is the naive-full-walk independence oracle, design §1.5).

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

# Intentional friend access (DEC-P0-017 per-site convention):
# ``_FORCED_EXIT_SECONDS`` is the canonical 15:20 default boundary
# ``is_forced_exit_bar`` itself falls back to — imported (never re-minted) so
# the forced-SLID detection compares against the exact same instant the
# forced classification used (one boundary notion, CLAUDE.md §11 COMMON).
from brutex.calendars.nse import (
    _FORCED_EXIT_SECONDS,  # pyright: ignore[reportPrivateUsage]
    IST,
    forced_exit_seconds_for_session,
    get_session_checked,
    is_forced_exit_bar,
)
from brutex.engine.exec_grid import ExecGridMap, build_exec_grid_map
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

#: Position of ``"stop_loss"`` in :data:`EXIT_REASONS` — the ONE index the
#: metrics tail reads out of a per-mask exit-reason counters tuple to derive
#: ``sl_hit_rate`` (DEC-METRICS-LOSSRATE-FIRSTCLASS-001 Stage B). Derived from
#: the vocabulary, never a re-pinned literal (CLAUDE.md §11 COMMON); the
#: vocabulary is append-only, so the index can never move.
STOP_LOSS_REASON_INDEX: Final[int] = EXIT_REASONS.index(_ExitReason.STOP_LOSS.value)

#: Enum member → :data:`EXIT_REASONS` position, for the walker's O(1)/trade
#: per-reason counter increment at the single ``_close`` seam.
_EXIT_REASON_INDEX: Final[dict[_ExitReason, int]] = {
    reason: index for index, reason in enumerate(_ExitReason)
}


@dataclass(frozen=True, slots=True)
class TradeDetail:
    """One closed trade's exit detail — the additive capture-sink record.

    Emitted by :class:`TradeSimWalker` into an optional ``detail_sink`` list
    (DEC-SWEEP-RESULTS-DEPTH-001 WP1): the walker already KNOWS each trade's
    exit reason and exact fill prices at the single ``_close`` seam. The sink
    surfaces them WITHOUT touching :class:`~brutex.metrics.types.ClosedTrade`
    or the Rust ``TradeRecord`` tuple — a pure additive annotation. Under
    DEC-FILL-EXEC-GRID-1M-001 the SHAPE is unchanged; the fill prices are now
    1-MINUTE bar prices (values move, fields do not).

    Parameters
    ----------
    trade
        The :class:`~brutex.metrics.types.ClosedTrade` this detail annotates —
        the SAME object the walker returned, positionally aligned with the
        walker's output (detail ``i`` annotates trade ``i``).
    exit_reason
        Why the position closed — one of :data:`EXIT_REASONS`.
    entry_fill_paisa
        The exact entry fill price (int64 paisa) — the entry 1m bar's adverse
        extreme (HIGH for BUY, LOW for SELL — the DEC-FILL-WORST-CASE-ONLY-001
        adverse-max law on the 1m grid).
    exit_fill_paisa
        The exact exit fill price (int64 paisa) — the adverse-max fill the
        event's locked rule names (level-clamped extreme for SL/TP; the 1m
        bar's adverse extreme for signal / forced / end-of-data /
        session-gap closes; the entry bar's CLOSE for the B3 terminal
        same-bar degenerate). Invariant (by :meth:`TradeSimWalker._close`'s
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
    # The IST session date the position OPENED on — the open-position session
    # guard closes the position retroactively at the prior session's last
    # stored bar the moment a pushed bar's IST date differs
    # (DEC-TRADESIM-OPEN-SESSION-GUARD-001). Stamped from the SAME
    # ``day_ist`` the fill site already resolved — one session notion.
    entry_day_ist: date
    sl_level_paisa: int | None
    tp_level_paisa: int | None
    mfe_paisa: int  # running max favourable excursion (>= 0)
    mae_paisa: int  # running max adverse excursion magnitude (>= 0)
    # Most-recent in-position bar — the end-of-data fallback AND the (A2)
    # session-boundary close both price against it (the finish() idiom).
    # Held as the bar itself (not pre-converted paisa) so the hot per-bar path
    # avoids a Decimal→paisa conversion of every bar; the conversion happens
    # once, only if the position survives to end of data / a session boundary.
    # When ``last_idx == entry_idx`` the terminal close prices at the bar's
    # CLOSE, not its adverse extreme (the B3 carve-out).
    last_idx: int
    last_bar: CandleBar


class TradeSimWalker:
    """Walk the 1-minute execution stream + due-fill projections into ``ClosedTrade``\\s.

    Streaming-first (CLAUDE.md §11 COMPREHENSIVE): feed 1-minute EXECUTION
    bars one at a time with :meth:`push_1m` (returning a :class:`ClosedTrade`
    on the bar a position closes, else ``None``) then call :meth:`finish`
    once at the end, **or** hand the dual feed to :meth:`run` /
    :meth:`run_with_exit_indices` for the batch path. Both share the same
    per-bar state machine. The walker is single-position: while a position is
    open, entry fills are ignored (Track 1 is non-overlapping).

    Dual-feed contract (DEC-FILL-EXEC-GRID-1M-001): the batch entries take
    the decision-TF bars (signal domain) AND the 1-minute execution bars
    (fill/walk domain) and project each decision signal onto its fill bar
    through the :class:`~brutex.engine.exec_grid.ExecGridMap` (built once
    when not injected). The streaming :meth:`push_1m` takes the projections
    PRECOMPUTED (``entry_due`` / ``exit_due``) — the walker never re-times
    signals. The mapping bakes the same-session gate + the B6 selector
    assertion + the B4 collision winner law in at build, so a due fill
    reaching the walker is already session-legal; the walker's own gates are
    the forced-bar entry gate and the open-position session guard.

    Observability counters (public, additive — reset only by constructing a
    fresh walker; per-tuple totals per the design's counter law):

    * ``exits_deferred`` — trades whose ENTRY 1m bar's range already touched
      an SL/TP level, detection deferred by the same-bar deferral (§3.12).
    * ``signals_dropped_collision`` — decision signals dropped by the B4
      earliest-armed-wins collision law (batch path; counted from the map's
      ``collision_losers`` × the actual signal streams).
    * ``fill_slid_minutes`` / ``exit_fill_slid_minutes`` /
      ``forced_fill_slid_minutes`` — the widened SLID-fill counter family
      (§5.2): entry fills / Mode-B exit fills whose MAPPED instant
      ``exec_idx(N)`` (the arm bar — NOT the physical landing bar a §2.4
      deferral relocates the fill to; design law 6) is a slid bar, and
      forced closes displaced past the exact boundary minute. Each physical
      fill counts AT MOST ONCE per class.

    Parameters
    ----------
    direction
        :data:`Side.BUY` (long) or :data:`Side.SELL` (short). The same
        direction for every trade this walker emits. ``HOLD`` is rejected.
    tf_minutes
        The DECISION timeframe in whole minutes (``>= 1``) — sets each
        decision bar's close instant for the exec-grid mapping. The walk
        itself is always on the 1-minute grid (forced-exit classification
        uses ``tf = 1``).
    stop_loss_paisa
        Stop-loss distance from entry in paisa (``> 0``), or ``None`` to
        disable the stop leg (Mode B).
    take_profit_paisa
        Take-profit distance from entry in paisa (``> 0``), or ``None`` to
        disable the target leg (Mode B).
    reference_stamper
        Optional :class:`~brutex.engine.reference_series.TradeReferenceStamper`.
        When supplied, every emitted :class:`ClosedTrade` is stamped with the
        India VIX level at its (1-minute-granular) entry + exit instants
        (``vix_at_entry`` / ``vix_at_exit``) — REFERENCE ONLY, never a signal
        input (DEC-VIX-TRADE-STAMP-001). A VIX bar missing at an instant
        stamps ``None`` there (never a fabricated value).
    detail_sink
        Optional caller-owned list the walker appends one :class:`TradeDetail`
        to per closed trade (positionally aligned with the returned trades) —
        the exit reason + exact fill prices the ``_close`` seam already knew
        (DEC-SWEEP-RESULTS-DEPTH-001 WP1). ``None`` (the default) skips the
        append entirely.

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
        self._reference_stamper = reference_stamper
        self._detail_sink = detail_sink
        self._position: _OpenPosition | None = None
        # A Mode-B exit due on the trade's OWN entry 1m bar defers to the
        # bar's successor (the same-bar deferral law §2.4) — the ONLY
        # remaining next-bar scheduler; the mapped exit_due already carries
        # the "next 1m" timing for every other case.
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
        # Per-reason close counters in EXIT_REASONS order — incremented once
        # per ``_close`` (O(1)/trade; the seam already holds the reason), read
        # out via :attr:`exit_reason_counts`
        # (DEC-METRICS-LOSSRATE-FIRSTCLASS-001 Stage B). A pure OBSERVER: no
        # branch reads it, so the emitted trades are byte-identical with the
        # counters present.
        self._exit_reason_counts: list[int] = [0] * len(EXIT_REASONS)
        # Exit reason of the most recently closed trade (the slid-Mode-B
        # counter's consumption probe in the batch loop) — same contract as
        # ``_last_exit_idx`` (DEC-FILL-EXEC-GRID-1M-001).
        self._last_exit_reason: str | None = None
        # Whether the most recently closed trade's exit consumed a DEFERRED
        # (same-bar §2.4) pending Mode-B exit rather than a directly-mapped
        # one — recorded by ``_close`` (default ``False`` on every non-deferred
        # close, so it can never read stale). Read by the batch loop's slid
        # counter to DEDUPE a deferred exit that was already counted at its
        # mapped arm bar (design law 6: the exit's slide is a property of
        # ``exec_idx(N)`` = the arm bar, NOT its physical landing successor).
        self._last_exit_was_deferred: bool = False
        # Public observability counters (class docstring).
        self.exits_deferred: int = 0
        self.signals_dropped_collision: int = 0
        self.fill_slid_minutes: int = 0
        self.exit_fill_slid_minutes: int = 0
        self.forced_fill_slid_minutes: int = 0

    @property
    def exit_reason_counts(self) -> tuple[int, ...]:
        """Closed-trade counts per exit reason, in :data:`EXIT_REASONS` order.

        The additive counter surface (DEC-METRICS-LOSSRATE-FIRSTCLASS-001
        Stage B — the ``_last_exit_idx`` exposure precedent): one integer per
        vocabulary member, accumulated across every trade this walker has
        closed so far; ``sum(exit_reason_counts) == n_closed_trades`` by
        construction (each close increments exactly one counter). A snapshot
        tuple — mutating walker state never aliases into a caller's copy.
        """
        return tuple(self._exit_reason_counts)

    # ── Batch path ────────────────────────────────────────────────────────

    def run(
        self,
        decision_bars: Sequence[CandleBar],
        exec_bars_1m: Sequence[CandleBar],
        entry_signals: Sequence[bool],
        exit_signals: Sequence[bool] | None = None,
        *,
        exec_map: ExecGridMap | None = None,
    ) -> list[ClosedTrade]:
        """Walk the dual feed and return every closed trade in chronological order.

        Parameters
        ----------
        decision_bars
            Chronologically ordered decision-TF bars (the signal domain). At
            ``tf_minutes == 1`` pass the SAME sequence as ``exec_bars_1m``.
        exec_bars_1m
            Chronologically ordered 1-minute EXECUTION bars (the fill/walk
            domain) — REQUIRED, never optional (design §3.3).
        entry_signals
            One ``bool`` per DECISION bar — ``True`` means "enter (in
            ``direction``) at this bar's next-1m fill". Must be the same
            length as ``decision_bars``.
        exit_signals
            Optional one ``bool`` per DECISION bar — ``True`` means "exit at
            this bar's next-1m fill" (Mode B). Must match ``decision_bars``
            length when supplied.
        exec_map
            Optional pre-built :class:`~brutex.engine.exec_grid.ExecGridMap`
            for this exact stream pair (the preload-scoped memo); ``None``
            derives it once here.

        Returns
        -------
        list[ClosedTrade]
            The closed round-trips, in the order they closed.

        Raises
        ------
        ValueError
            If a signal stream's length does not match ``decision_bars``, or
            a supplied ``exec_map``'s dimensions do not match the streams.
        """
        trades, _ = self.run_with_exit_indices(
            decision_bars, exec_bars_1m, entry_signals, exit_signals, exec_map=exec_map
        )
        return trades

    def run_with_exit_indices(
        self,
        decision_bars: Sequence[CandleBar],
        exec_bars_1m: Sequence[CandleBar],
        entry_signals: Sequence[bool],
        exit_signals: Sequence[bool] | None = None,
        *,
        exec_map: ExecGridMap | None = None,
    ) -> tuple[list[ClosedTrade], list[int]]:
        """Walk the dual feed; return the closed trades AND each trade's exit 1m index.

        The index-carrying batch walk (DEC-SWEEP-EXIT-INDEX-FASTPATH-001 on
        the 1m grid): identical to :meth:`run` — same state machine, same
        trades in the same order — plus a positionally aligned list of each
        trade's exit bar INDEX within *exec_bars_1m* (1-MINUTE indices; the
        metrics tail translates them to owning decision bars via
        ``ExecGridMap.decision_of``, design §3.8). :meth:`run` is a thin
        delegate over this method, so the two batch entries can never drift.

        The returned indices are the walker's TRUE exit indices. Whether
        they may substitute for the ``exit_ts`` dict lookup downstream is a
        separate certification
        (:func:`~brutex.engine.exit_index.bars_strictly_increasing` over the
        EXECUTION stream) the module-level
        :func:`simulate_trades_with_exit_indices` applies.

        Parameters and raises are exactly :meth:`run`'s.
        """
        n_decision = len(decision_bars)
        if len(entry_signals) != n_decision:
            raise ValueError(
                f"entry_signals length {len(entry_signals)} != decision_bars length {n_decision}"
            )
        if exit_signals is not None and len(exit_signals) != n_decision:
            raise ValueError(
                f"exit_signals length {len(exit_signals)} != decision_bars length {n_decision}"
            )
        if exec_map is None:
            exec_map = build_exec_grid_map(decision_bars, exec_bars_1m, tf_minutes=self._tf_minutes)
        elif exec_map.n_decision != n_decision or exec_map.n_exec != len(exec_bars_1m):
            raise ValueError(
                f"exec_map covers (n_decision={exec_map.n_decision}, "
                f"n_exec={exec_map.n_exec}) != streams "
                f"({n_decision}, {len(exec_bars_1m)})"
            )
        # B4 collision accounting: a LOSING decision index drops its
        # signal(s) — counted against the ACTUAL streams (a loser with no
        # signal drops nothing).
        for loser in exec_map.collision_losers:
            if entry_signals[loser]:
                self.signals_dropped_collision += 1
            if exit_signals is not None and exit_signals[loser]:
                self.signals_dropped_collision += 1

        arm = exec_map.arm_source
        slid = exec_map.arm_slid
        trades: list[ClosedTrade] = []
        exit_indices: list[int] = []
        for j, bar in enumerate(exec_bars_1m):
            src = arm[j]
            entry_due = src >= 0 and bool(entry_signals[src])
            exit_due = src >= 0 and exit_signals is not None and bool(exit_signals[src])
            pos_before = self._position
            closed = self.push_1m(bar, entry_due=entry_due, exit_due=exit_due)
            if closed is not None:
                trades.append(closed)
                exit_indices.append(self._last_exit_idx)
            if slid[j]:
                # SLID-fill accounting (§5.2, the widened counter family):
                # entry fills + Mode-B exit fills whose MAPPED instant
                # ``exec_idx(N)`` = this slid arm bar j (design law 6/9).
                # Mask-dependent, hence counted here (silent — the capped
                # WARNs are the map build's, mask-independent). Each physical
                # exit fill contributes AT MOST ONE to
                # ``exit_fill_slid_minutes``: the two exit arms below are
                # MUTUALLY EXCLUSIVE by construction —
                #   * ``opened_here`` owns a DEFERRED exit (entry + exit on
                #     one decision bar → the exit's mapped instant IS this arm
                #     bar j; §2.4 physically relocates its FILL to a successor,
                #     but law 6 keeps its slide keyed to bar j — so it is
                #     counted HERE, at its mapped slide, never at its landing);
                #   * the direct arm owns a DIRECTLY-mapped exit whose fill bar
                #     IS its mapped instant (``exec_idx(N) == j``), gated
                #     ``not _last_exit_was_deferred`` so a deferred exit that
                #     was already counted at its arm bar is NOT re-counted when
                #     it lands on a slid successor whose own decision carried an
                #     exit signal (the fix-round-6 consecutive-slid dedupe).
                pos_after = self._position
                opened_here = pos_after is not None and pos_after.entry_idx == j
                if opened_here:
                    self.fill_slid_minutes += 1
                if exit_due and (
                    opened_here  # deferred at its mapped arm bar (§2.4/law 6)
                    or (
                        pos_before is not None
                        and closed is not None
                        and self._last_exit_reason == _ExitReason.SIGNAL.value
                        and not self._last_exit_was_deferred
                    )
                ):
                    self.exit_fill_slid_minutes += 1
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

    def push_1m(
        self,
        bar_1m: CandleBar,
        *,
        entry_due: bool,
        exit_due: bool = False,
    ) -> ClosedTrade | None:
        """Advance one 1-MINUTE bar; return a :class:`ClosedTrade` iff a position closed.

        Parameters
        ----------
        bar_1m
            The next 1-minute EXECUTION bar in chronological order (UTC
            timestamp, Decimal rupees).
        entry_due
            ``True`` iff THIS 1m bar is a mapped entry-fill bar with a live
            signal — the projection ``entry_signals[arm_source[j]]`` (the
            batch wrapper computes it; the mapping already applied the
            same-session/B6/B4 gates). Ignored while a position is open, and
            ignored on a bar where a position just closed (the exit bar is
            consumed) or a forced-exit 1m bar (an entry never fills at/after
            the boundary).
        exit_due
            ``True`` iff THIS 1m bar is a mapped Mode-B exit-fill bar with a
            live exit signal. Only meaningful while a position is open; on
            the trade's own entry bar it defers to the bar's successor (the
            same-bar deferral law).

        Returns
        -------
        ClosedTrade | None
            The trade that closed on this bar, or ``None``. A ``session_gap``
            close is EMITTED on this bar (the first bar of a new session) but
            PRICED at the prior session's last stored 1m bar — the finish()
            idiom (DEC-TRADESIM-OPEN-SESSION-GUARD-001 on the 1m stream).
        """
        self._index += 1
        i = self._index
        closed: ClosedTrade | None = None
        # The forced-exit check (UTC→IST + boundary compare) is only consulted
        # when filling a due entry or managing an open position, so a flat,
        # no-fill bar stays on the cheap path (O(1) discipline; the kernel's
        # flat-skip is the Rust twin of this early-out).
        if self._position is not None or entry_due:
            ts_ist = bar_1m.timestamp.astimezone(IST)
            day_ist = ts_ist.date()
            boundary = self._forced_exit_seconds(day_ist)
            forced = is_forced_exit_bar(ts_ist.time(), 1, boundary)

            if self._position is not None:
                pos = self._position
                if day_ist != pos.entry_day_ist:
                    # (A2) Session-boundary guard: an open position never
                    # survives an IST session-date change on the 1m stream
                    # (DEC-TRADESIM-OPEN-SESSION-GUARD-001). Close
                    # retroactively at the PRIOR session's last stored 1m
                    # bar — the finish() idiom with the B3 terminal CLOSE
                    # carve-out — BEFORE this bar is managed. A pending
                    # Mode-B exit is SUBSUMED (cleared). The inequality
                    # compare fires exactly once even across a multi-session
                    # hole; a just-opened position carries its own bar's
                    # date, so it can never trip the guard on its entry bar.
                    self._pending_exit = None
                    closed = self._close(
                        pos,
                        exit_price_paisa=self._last_bar_exit_paisa(pos),
                        exit_ts=pos.last_bar.timestamp,
                        exit_idx=pos.last_idx,
                        reason=_ExitReason.SESSION_GAP,
                    )
                else:
                    # (B) Manage the open position on this 1m bar.
                    closed = self._manage_position(
                        pos,
                        bar_1m,
                        i,
                        forced=forced,
                        exit_due=exit_due,
                        ts_ist_seconds=(ts_ist.hour * 3600 + ts_ist.minute * 60 + ts_ist.second),
                        boundary=boundary,
                    )

            # (A) Fill a due entry on THIS bar — the map already timed +
            # session-gated it; the walker's own gates: never on a bar a
            # trade just closed (the exit/boundary bar is consumed — the
            # earliest re-fill is a later bar) and never on a forced-exit 1m
            # bar (cannot open at/after the session's forced instant).
            if self._position is None and closed is None and entry_due and not forced:
                self._open_position(bar_1m, i, entry_day_ist=day_ist, exit_due=exit_due)

        return closed

    def finish(self) -> ClosedTrade | None:
        """Close any still-open position at the last stored 1m bar (end of data).

        Call once after the final :meth:`push_1m`. Priced at the last stored
        1m bar's adverse extreme — UNLESS that bar is the trade's own entry
        bar, where the fill is the bar's CLOSE (the B3 terminal carve-out;
        see :meth:`_last_bar_exit_paisa`). Returns ``None`` when the walker
        is already flat. A still-pending Mode-B exit is IGNORED here (closed
        at the terminal basis, not a would-be successor fill).
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

    def _open_position(
        self, bar: CandleBar, idx: int, *, entry_day_ist: date, exit_due: bool
    ) -> None:
        """Open a position on 1m bar ``bar`` (the mapped next-1m entry fill).

        The fill PRICE is the entry 1m bar's ADVERSE extreme — BUY at HIGH,
        SELL at LOW (DEC-FILL-WORST-CASE-ONLY-001 on the 1m grid). The SL/TP
        levels anchor on this ACTUAL worst entry fill (:meth:`_levels`).

        The same-bar deferral (§2.4) is implemented structurally HERE: the
        entry bar's full range folds into MFE/MAE immediately, but NO exit
        detection runs on it — management begins on the bar's successor. When
        the entry bar's range already touched a level, ``exits_deferred``
        counts the deferral (the B3 observability law). A Mode-B ``exit_due``
        landing on the entry bar defers into ``_pending_exit`` (fills on the
        successor at its adverse extreme).
        """
        entry_price = _price_to_paisa(bar.high if self._direction is Side.BUY else bar.low)
        sl_level, tp_level = self._levels(entry_price)
        pos = _OpenPosition(
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
        self._position = pos
        # §2.4: the entry bar folds its range but NEVER exits.
        high_paisa = _price_to_paisa(bar.high)
        low_paisa = _price_to_paisa(bar.low)
        self._update_excursion(pos, high_paisa, low_paisa)
        if self._resolve_level_hit(pos, high_paisa, low_paisa) is not None:
            self.exits_deferred += 1
        if exit_due:
            self._pending_exit = _ExitReason.SIGNAL
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
        self,
        pos: _OpenPosition,
        bar: CandleBar,
        idx: int,
        *,
        forced: bool,
        exit_due: bool,
        ts_ist_seconds: int,
        boundary: int | None,
    ) -> ClosedTrade | None:
        """Process one in-position 1m bar: excursion, exits, deferral consumption.

        ``pos`` is the live (non-``None``) position the caller already
        narrowed. Runs only on bars strictly AFTER the entry bar (the entry
        bar is fully handled at :meth:`_open_position` — the same-bar
        deferral law). Precedence per managed bar: Mode-B signal fill (a
        pending entry-bar deferral OR a directly-due mapped fill) → forced →
        SL/TP → still open.
        """
        pos.last_idx = idx
        pos.last_bar = bar

        # (1) A Mode-B signal exit fills on THIS bar — either the mapped
        # exit_due fill or a pending entry-bar-deferred one — at the bar's
        # adverse extreme (DEC-FILL-WORST-CASE-ONLY-001). The excursion fold
        # stays the legacy (open, open) — only the fill PRICE is worst-case.
        # Precedence over forced mirrors the historical pending-beats-forced
        # ladder (both fill at the same adverse extreme; the REASON reads
        # ``signal``).
        if self._pending_exit is not None or exit_due:
            was_deferred = self._pending_exit is not None
            reason = self._pending_exit if self._pending_exit is not None else _ExitReason.SIGNAL
            self._pending_exit = None
            open_paisa = _price_to_paisa(bar.open)
            self._update_excursion(pos, open_paisa, open_paisa)
            return self._close(
                pos,
                exit_price_paisa=self._open_anchored_exit_paisa(bar),
                exit_ts=bar.timestamp,
                exit_idx=idx,
                reason=reason,
                deferred=was_deferred,
            )

        # (2) Forced session-close exit overrides SL/TP — fill at the
        # adverse extreme; fold unchanged (open, open). A fill bar opening
        # STRICTLY after the boundary instant is a displaced (slid) forced
        # fill — the boundary minute itself was not stored — counted per the
        # widened slid-fill law (§2.5/§5.2).
        if forced:
            if ts_ist_seconds > (boundary if boundary is not None else _FORCED_EXIT_SECONDS):
                self.forced_fill_slid_minutes += 1
            open_paisa = _price_to_paisa(bar.open)
            self._update_excursion(pos, open_paisa, open_paisa)
            return self._close(
                pos,
                exit_price_paisa=self._open_anchored_exit_paisa(bar),
                exit_ts=bar.timestamp,
                exit_idx=idx,
                reason=_ExitReason.FORCED,
            )

        # (3) Full-range in-position bar: fold its high/low into MFE/MAE.
        high_paisa = _price_to_paisa(bar.high)
        low_paisa = _price_to_paisa(bar.low)
        self._update_excursion(pos, high_paisa, low_paisa)

        # (4) Stop-loss / take-profit — detection AND fill on THIS 1m bar
        # (design §3b.1 option (b): the 1m path IS the monitor). The fill is
        # the adverse-max law WITH the level: a BUY exits (sells) at
        # min(level, 1m low) — a gap-through bar fills at the extreme, while
        # a bar entirely past the level in the trade's favour stays clamped
        # AT the level (the gap-open law, design B5); a SELL exits (covers)
        # at max(level, 1m high), the exact mirror.
        hit = self._resolve_level_hit(pos, high_paisa, low_paisa)
        if hit is not None:
            reason, level_paisa = hit
            exit_paisa = (
                min(level_paisa, low_paisa)
                if self._direction is Side.BUY
                else max(level_paisa, high_paisa)
            )
            return self._close(
                pos,
                exit_price_paisa=exit_paisa,
                exit_ts=bar.timestamp,
                exit_idx=idx,
                reason=reason,
            )

        # (5) Still open.
        return None

    def _resolve_level_hit(
        self, pos: _OpenPosition, high_paisa: int, low_paisa: int
    ) -> tuple[_ExitReason, int] | None:
        """Decide which of SL/TP a bar's ``[low, high]`` touched (SL-first tie).

        Returns ``(reason, level_paisa)`` — the chosen leg and its exact
        trigger level — or ``None`` if neither level was touched. Each leg's
        tuple is built inside its own ``is not None`` guard, so
        ``level_paisa`` is always a live (non-``None``) level and the fill
        site never re-checks. A bar spanning BOTH levels always fills the
        STOP first — the pessimistic tie, hardcoded
        (DEC-FILL-WORST-CASE-ONLY-001).
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
        # Same bar spans both levels — the stop fills first, always.
        return stop if stop is not None else target

    def _open_anchored_exit_paisa(self, bar: CandleBar) -> int:
        """The exit fill for a signal/forced event on 1m *bar* — its adverse extreme.

        A BUY position exits (sells) at the LOW, a SELL position exits
        (covers) at the HIGH (no level exists for these events —
        DEC-FILL-WORST-CASE-ONLY-001). Retained as the ONE side-sign core the
        other fill seams mirror (design §3.6's freeze-time consolidation
        note: under the 1m law the Mode-B fill is no longer open-ANCHORED —
        the seam's SIDE-SIGN logic is the survivor). O(1): one Decimal→paisa
        conversion per exit bar.
        """
        return _price_to_paisa(bar.low if self._direction is Side.BUY else bar.high)

    def _last_bar_exit_paisa(self, pos: _OpenPosition) -> int:
        """The end-of-data / session-gap exit fill — the last stored 1m bar's price.

        ``pos.last_bar``'s ADVERSE extreme — BUY at LOW, SELL at HIGH — with
        the **B3 terminal carve-out** (design §3.12): when the last stored
        bar IS the trade's own entry bar (``last_idx == entry_idx`` — the
        legalized same-bar terminal degenerate), the fill is that bar's
        **CLOSE** (the #1743 basis), never the adverse extreme — pricing the
        terminal same-bar close at the extreme would BE the full-range
        phantom loss the deferral exists to kill. A REAL stored price at a
        REAL stored instant either way (never fabricated, Golden Rule #1).
        """
        bar = pos.last_bar
        if pos.last_idx == pos.entry_idx:
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
        deferred: bool = False,
    ) -> ClosedTrade:
        """Build the :class:`ClosedTrade`, flatten the walker, and emit a log line.

        ``deferred`` records whether this exit consumed a DEFERRED (§2.4)
        pending Mode-B exit — read by the batch loop's slid counter to dedupe
        an already-counted deferred exit (see ``_last_exit_was_deferred``).
        Every non-deferred close passes the default ``False``.
        """
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
            # A 1-MINUTE-bar count under DEC-FILL-EXEC-GRID-1M-001 (design
            # §7.2 Q4: the walk grid IS the hold measure; the B3 terminal
            # same-bar degenerate reads 1).
            holding_bars=exit_idx - pos.entry_idx + 1,
            entry_ts=pos.entry_ts,
            exit_ts=exit_ts,
            direction=self._direction,
            vix_at_entry=vix_at_entry,
            vix_at_exit=vix_at_exit,
        )
        # Additive capture sink (DEC-SWEEP-RESULTS-DEPTH-001 WP1): surface the
        # reason + exact fills this seam already computed. ``None`` (the
        # default) is a pure skip — the returned trade is byte-identical
        # either way.
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
        self._exit_reason_counts[_EXIT_REASON_INDEX[reason]] += 1
        self._last_exit_reason = reason.value
        self._last_exit_was_deferred = deferred
        _LOG.debug(
            "trade_sim.exit",
            exit_idx=exit_idx,
            reason=reason.value,
            pnl_paisa=pnl,
            holding_bars=trade.holding_bars,
        )
        return trade


def simulate_trades(
    decision_bars: Sequence[CandleBar],
    entry_signals: Sequence[bool],
    *,
    direction: Side,
    tf_minutes: int,
    exec_bars_1m: Sequence[CandleBar],
    exec_map: ExecGridMap | None = None,
    stop_loss_paisa: int | None = None,
    take_profit_paisa: int | None = None,
    exit_signals: Sequence[bool] | None = None,
    reference_stamper: TradeReferenceStamper | None = None,
    detail_sink: list[TradeDetail] | None = None,
) -> list[ClosedTrade]:
    """Walk the dual feed into closed trades — the one-call batch wrapper.

    ``exec_bars_1m`` is REQUIRED (no default): no silent single-feed call
    shape survives DEC-FILL-EXEC-GRID-1M-001 (design §3.3). At
    ``tf_minutes == 1`` pass the same sequence for both streams.

    Parameters
    ----------
    decision_bars
        Chronologically ordered decision-TF bars (the signal domain).
    entry_signals
        One ``bool`` per DECISION bar (enter at that bar's next-1m fill).
        Same length as ``decision_bars``.
    direction
        :data:`Side.BUY` (long) or :data:`Side.SELL` (short).
    tf_minutes
        Decision timeframe in whole minutes (``>= 1``).
    exec_bars_1m
        The 1-minute EXECUTION stream (the fill/walk domain). REQUIRED.
    exec_map
        Optional pre-built :class:`~brutex.engine.exec_grid.ExecGridMap` for
        this exact stream pair; ``None`` derives it once.
    stop_loss_paisa, take_profit_paisa
        Stop / target distances from entry in paisa (``> 0``), or ``None`` to
        disable that leg.
    exit_signals
        Optional Mode-B exit stream (one ``bool`` per DECISION bar; exit at
        that bar's next-1m fill, at its adverse extreme). Same length as
        ``decision_bars`` when supplied.
    reference_stamper
        Optional India VIX
        :class:`~brutex.engine.reference_series.TradeReferenceStamper` — stamps
        ``vix_at_entry`` / ``vix_at_exit`` on every trade (REFERENCE ONLY,
        DEC-VIX-TRADE-STAMP-001). ``None`` leaves both VIX fields ``None``.
    detail_sink
        Optional caller-owned list receiving one :class:`TradeDetail` per
        closed trade (exit reason + exact fills, positionally aligned with
        the returned trades — DEC-SWEEP-RESULTS-DEPTH-001 WP1).

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
        reference_stamper=reference_stamper,
        detail_sink=detail_sink,
    )
    return walker.run(decision_bars, exec_bars_1m, entry_signals, exit_signals, exec_map=exec_map)


def simulate_trades_with_exit_indices(
    decision_bars: Sequence[CandleBar],
    entry_signals: Sequence[bool],
    *,
    direction: Side,
    tf_minutes: int,
    exec_bars_1m: Sequence[CandleBar],
    exec_map: ExecGridMap | None = None,
    stop_loss_paisa: int | None = None,
    take_profit_paisa: int | None = None,
    exit_signals: Sequence[bool] | None = None,
    reference_stamper: TradeReferenceStamper | None = None,
    detail_sink: list[TradeDetail] | None = None,
) -> tuple[list[ClosedTrade], list[int] | None]:
    """Walk the dual feed into closed trades PLUS their certified exit 1m indices.

    The index-carrying twin of :func:`simulate_trades`
    (DEC-SWEEP-EXIT-INDEX-FASTPATH-001 on the 1m grid): the trades are
    byte-identical to :func:`simulate_trades` on the same inputs (both ride
    ONE :meth:`TradeSimWalker.run_with_exit_indices` walk). The second
    element is each trade's exit bar index WITHIN THE EXECUTION STREAM,
    returned iff the EXEC stream is CERTIFIED —
    :func:`~brutex.engine.exit_index.bars_strictly_increasing` over
    ``exec_bars_1m`` — so that the index provably equals the last-wins
    ``exit_ts`` dict lookup the metrics tail would otherwise perform; an
    uncertified window returns ``None`` and the consumer stays on the
    bit-identical dict path.

    Parameters are exactly :func:`simulate_trades`'s.

    Returns
    -------
    tuple[list[ClosedTrade], list[int] | None]
        ``(trades, exit_indices)`` — positionally aligned; ``exit_indices``
        is ``None`` for an uncertified execution window.
    """
    trades, exit_indices, _counts = simulate_trades_with_exit_counts(
        decision_bars,
        entry_signals,
        direction=direction,
        tf_minutes=tf_minutes,
        exec_bars_1m=exec_bars_1m,
        exec_map=exec_map,
        stop_loss_paisa=stop_loss_paisa,
        take_profit_paisa=take_profit_paisa,
        exit_signals=exit_signals,
        reference_stamper=reference_stamper,
        detail_sink=detail_sink,
    )
    return trades, exit_indices


def simulate_trades_with_exit_counts(
    decision_bars: Sequence[CandleBar],
    entry_signals: Sequence[bool],
    *,
    direction: Side,
    tf_minutes: int,
    exec_bars_1m: Sequence[CandleBar],
    exec_map: ExecGridMap | None = None,
    stop_loss_paisa: int | None = None,
    take_profit_paisa: int | None = None,
    exit_signals: Sequence[bool] | None = None,
    reference_stamper: TradeReferenceStamper | None = None,
    detail_sink: list[TradeDetail] | None = None,
) -> tuple[list[ClosedTrade], list[int] | None, tuple[int, ...]]:
    """Walk a feed into trades + certified exit indices + exit-reason counters.

    The counter-carrying twin of :func:`simulate_trades_with_exit_indices`
    (DEC-METRICS-LOSSRATE-FIRSTCLASS-001 Stage B, the additive-twin
    precedent) — and the ONE batch spine both thinner surfaces delegate to
    (:func:`simulate_trades_with_exit_indices` drops the counters;
    :func:`simulate_trades` drops indices too), so the three entries can
    never drift. The trades + indices are byte-identical to the pre-counter
    surfaces on the same inputs; the third element is the walker's
    :attr:`TradeSimWalker.exit_reason_counts` — one integer per
    :data:`EXIT_REASONS` member in vocabulary order, ``sum == len(trades)``
    by construction. The metrics tail reads position
    :data:`STOP_LOSS_REASON_INDEX` to derive ``sl_hit_rate``; counters are
    pure observers, so results are byte-identical whether or not a caller
    consumes them.

    Parameters are exactly :func:`simulate_trades`'s.

    Returns
    -------
    tuple[list[ClosedTrade], list[int] | None, tuple[int, ...]]
        ``(trades, exit_indices, exit_reason_counts)`` — trades/indices
        positionally aligned (``exit_indices`` ``None`` for an uncertified
        window, exactly the exit-indices twin's contract).
    """
    certified = bars_strictly_increasing(exec_bars_1m)
    walker = TradeSimWalker(
        direction=direction,
        tf_minutes=tf_minutes,
        stop_loss_paisa=stop_loss_paisa,
        take_profit_paisa=take_profit_paisa,
        reference_stamper=reference_stamper,
        detail_sink=detail_sink,
    )
    trades, exit_indices = walker.run_with_exit_indices(
        decision_bars, exec_bars_1m, entry_signals, exit_signals, exec_map=exec_map
    )
    return trades, exit_indices if certified else None, walker.exit_reason_counts


__all__ = [
    "EXIT_REASONS",
    "STOP_LOSS_REASON_INDEX",
    "TradeDetail",
    "TradeSimWalker",
    "simulate_trades",
    "simulate_trades_with_exit_counts",
    "simulate_trades_with_exit_indices",
]
