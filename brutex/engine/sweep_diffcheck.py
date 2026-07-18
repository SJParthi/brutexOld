"""R45 differential spot-check — an INDEPENDENT naive reference trade simulator.

Every run randomly samples K combos and re-simulates them through this
separate, deliberately naive reference implementation; results must match
the production engine bit-for-bit, any mismatch marks the run SUSPECT —
loud, never silent (requirement R45; design D-11). This complements the
compositional proof (golden answer key per condition + property-tested
combiner + golden trade sim) so construction never becomes faith.

## Independence contract (AST-pinned)

This module imports NONE of the production simulation / metrics modules:
``brutex.engine.trade_sim``, ``brutex.engine.rust_trade_sim``,
``brutex.engine.sweep_driver``, ``brutex.engine.rust_metrics``,
``brutex.metrics.compute``, ``brutex.engine.exit_index``,
``brutex.engine.exec_grid`` (the production signal→1m mapping —
DEC-FILL-EXEC-GRID-1M-001 design §3.11: importing it would kill the
mapping's independence) — pinned by an AST
test over the whole file (the DEC-SUSPICION-SWEEP-FIXES-001 #1492 idiom).
The production leg is INJECTED into :class:`DiffCheckHook` by the run core
(``simulate_trades`` + the equity/drawdown fold), so even the hook side of
this module stays structurally clean of the banned imports. What IS reused
are canonical INPUT seams only (design R45 note): the NSE session calendar
(session facts are data, not simulator logic), the canonical mask
enumeration order (:func:`brutex.engine.combo_mask.iter_masks_k_of_bits` +
conflict pruning — the addressing, not the simulation).
The signal projection, rupee→paisa snap, entry/exit state machine, equity
fold, drawdown scan and the float bit-compare key are all RE-DERIVED here
in a flat forward loop (the ``tests/unit/test_surface_trial_results.py``
independent-recomputation bar).

## Rules re-derived (the spec — ``trade_sim`` module docstring / sim-map c.1,
## on the DEC-FILL-EXEC-GRID-1M-001 1-minute execution grid, design §3.11)

Signal on DECISION bar N (``(bar_bits[N] & mask) == mask``) ⇒ fill on the
first stored 1-MINUTE bar at-or-after N's close instant ``ts_tf[N] +
tf_minutes`` — the reference re-derives that mapping with its OWN flat
forward two-pointer (deliberately NOT importing
:mod:`brutex.engine.exec_grid`, which joins the banned list): a fill
candidate on a DIFFERENT IST session date DROPS (the session-bounded slide
law — cross-session attempts never fill), a same-date candidate at/after
the arming session's close instant is REFUSED (the B6 bounded-distance
assertion), and when a ≥tf hole maps two decision bars onto ONE 1m bar the
EARLIEST-armed wins (B4 — the loser's signal drops). The walk itself runs
per 1-MINUTE bar. Every fill PRICE follows the independently RE-STATED
worst-case adverse-max law — the ONLY law (DEC-FILL-WORST-CASE-ONLY-001):
entry fills at the entry 1m bar's adverse extreme (BUY at HIGH / SELL at
LOW — SL/TP levels then anchor on that actual entry fill), never on a
forced-exit 1m bar; the entry 1m bar folds its FULL range into MFE/MAE but
runs NO exit detection (the same-bar deferral law §2.4 — management begins
on its successor); SL/TP touch on each LATER 1m bar's ``[low, high]`` in
int paisa with the pessimistic same-bar SL-first tie-break, and the
triggered SL **or TP** fills at ``min(level, bar low)`` for a BUY /
``max(level, bar high)`` for a SELL (a gap-through bar fills at the
extreme; a bar gapping PAST the level in the trade's favour stays clamped
AT the level — never an overstated favourable gap); the forced
session-close exit — the first 1m bar whose ``[open, open+1min)`` window
passes the session's close − 10 min boundary, overriding SL/TP — fills at
the forced bar's adverse extreme (its ``(open, open)`` excursion fold
unchanged); MFE/MAE fold per in-position 1m bar; an open position never
survives an IST session-date change (DEC-TRADESIM-OPEN-SESSION-GUARD-001,
MIGRATION.md #1743, on the 1m stream) — on the FIRST 1m bar whose IST
session date differs (pure ``!=``, either direction) from the position's
entry-session date it closes retroactively at the PRIOR session's last
managed 1m bar (that bar's timestamp / index; NO excursion fold from the
boundary bar) under ``session_gap``, and the boundary bar never fills an
entry; a position still open at end of data closes at the LAST managed 1m
bar under ``end_of_data`` — both retroactive closes price at that bar's
adverse extreme UNLESS it is the trade's own entry bar, where the fill is
the bar's CLOSE (the B3 terminal carve-out — pricing the terminal same-bar
close at the extreme would BE the full-range phantom loss the deferral
kills); ``holding_bars`` counts 1-MINUTE bars (``exit_idx − entry_idx +
1`` over the execution stream). P&L is the per-unit directional move in
int64 paisa — NO costs of any kind (Track-1 spot is signal-only;
CLAUDE.md §7).

## Determinism (design D-11)

The sampler seed is ``blake3("diffcheck|" + sweep_id + "|" + tuple_digest
+ "|" + data_snapshot_hash)`` — run identity only, no wall clock, no
global RNG. Same identity ⇒ same masks; a changed data snapshot re-keys
the sample.

## Never-adopt law (design I-4)

A mismatch produces a :class:`DiffCheckMismatch` (fan-out to the flight
recorder / SUSPECT latch / Telegram is injected via ``on_mismatch``); the
production value ALWAYS ships — this module has no channel to alter any
result byte, mirroring the burn-in ship-the-oracle stance and the
``_reattribute_chunk_failure`` never-adopt precedent.

## Honest envelope (Golden Rule #10)

Per tuple the hook's simulation work is bounded by K (bench-pinned flat
vs mask count), but the canonical enumeration it samples from is
O(n_masks) — a cheap integer walk strictly dominated by the tuple's own
production sweep (which simulates ALL n_masks). K=200 is a sampling
assurance, not a proof (~0.004% of a 5M-mask grid); the reference verifies
projection + trade sim + drawdown from CERTIFIED ``bar_bits`` — NOT the 60
indicator evaluators (they carry their own golden/property/shared-crate
surface).
"""

from __future__ import annotations

import contextlib
import math
import os
import random
import struct
from bisect import bisect_right
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Final, Protocol

import blake3
import structlog

from brutex.calendars.nse import IST, forced_exit_seconds_for_session, get_session_checked
from brutex.engine.combo_mask import iter_masks_k_of_bits
from brutex.engine.sweep_lookahead import env_flag_tristate
from brutex.vocabulary.conflicts import has_conflict

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from brutex.data.models import CandleBar

_LOG = structlog.get_logger(__name__)

#: Default per-run spot-check budget (design D-11 / R45's "e.g. 200").
DIFFCHECK_DEFAULT_K_PER_RUN: Final[int] = 200

#: Rehearsal-sized budget for the CI full-chain run (design D-10 graft C).
CI_K_SAMPLES: Final[int] = 8

#: The default-ON tristate kill switch (``env_flag_tristate`` vocabulary).
DIFFCHECK_ENV: Final[str] = "BRUTEX_SWEEP_DIFFCHECK"

#: Optional integer K override (``>= 0``; ``0`` disables). Fail-loud parse.
DIFFCHECK_K_ENV: Final[str] = "BRUTEX_SWEEP_DIFFCHECK_K"

#: Cap on the mismatched-mask hexes carried by a :class:`DiffCheckReport`.
MASKS_MISMATCHED_CAP: Final[int] = 8

#: Reference-side re-derivation of the standard forced-exit boundary:
#: 15:20 IST as seconds-since-midnight (regular 09:15–15:30 session close −
#: 10 min — ``docs/TRACK1_MICROSTRUCTURE.md`` §1.2), used when an
#: in-coverage date has no trading session (the production walker's own
#: fallback for a crafted feed dated on a holiday/weekend).
_DEFAULT_FORCED_EXIT_SECONDS: Final[int] = 15 * 3600 + 20 * 60

#: Reference-side re-derivation of the regular session CLOSE (15:30 IST as
#: seconds-since-midnight) — the B6 bounded-distance ceiling for an
#: in-coverage date with no trading session (mirrors the production
#: mapping's deterministic non-session fallback without importing it).
_DEFAULT_SESSION_CLOSE_SECONDS: Final[int] = 15 * 3600 + 30 * 60

#: 2-decimal tick-grid quantum for the re-derived rupee→paisa snap.
_PAISA_QUANTUM: Final[Decimal] = Decimal("0.01")

#: Accepted direction labels (``Side.BUY.value`` / ``Side.SELL.value``).
_VALID_DIRECTIONS: Final[frozenset[str]] = frozenset({"BUY", "SELL"})

#: The int/datetime-only trade compare surface — the fields shared by the
#: production ``ClosedTrade`` and :class:`RefTrade` (ints + tz-aware
#: datetimes compare exactly under ``==``; no float ever enters).
COMPARED_TRADE_FIELDS: Final[tuple[str, ...]] = (
    "pnl_paisa",
    "mfe_paisa",
    "mae_paisa",
    "holding_bars",
    "entry_ts",
    "exit_ts",
)


@dataclass(frozen=True, slots=True)
class RefTrade:
    """One reference round-trip — ints + datetimes ONLY on the compare surface.

    Attributes
    ----------
    entry_idx, exit_idx
        Bar indices of the entry fill and the exit.
    entry_ts, exit_ts
        The entry / exit BAR timestamps (bar-open timestamps, UTC).
    entry_paisa, exit_paisa
        Fill prices in int64 paisa.
    pnl_paisa
        Per-unit directional P&L in paisa (BUY: exit − entry; SELL mirrored).
    mfe_paisa, mae_paisa
        Max favourable / adverse excursion magnitudes (both ``>= 0``).
    holding_bars
        ``exit_idx − entry_idx + 1``.
    exit_reason
        Why the position closed (``stop_loss`` / ``take_profit`` /
        ``forced_1520`` / ``session_gap`` / ``end_of_data``) —
        reference-side diagnostic only (the production ``ClosedTrade``
        carries no reason field; the numeric compare surface is
        reason-free).
    """

    entry_idx: int
    exit_idx: int
    entry_ts: datetime
    exit_ts: datetime
    entry_paisa: int
    exit_paisa: int
    pnl_paisa: int
    mfe_paisa: int
    mae_paisa: int
    holding_bars: int
    exit_reason: str


@dataclass(slots=True)
class _RefPosition:
    """Mutable open-position state for the naive dual-stream loop (int paisa).

    ``last_exit_basis`` is the price a retroactive ``session_gap`` /
    ``end_of_data`` close prices against — the last managed 1m bar's
    direction-resolved ADVERSE extreme (LOW for a BUY, HIGH for a SELL)
    under the worst-case law (DEC-FILL-WORST-CASE-ONLY-001), or the entry
    bar's CLOSE while no successor was ever managed (the B3 terminal
    carve-out, DEC-FILL-EXEC-GRID-1M-001 §3.12) — resolved per bar so the
    close site never re-reads a bar.
    """

    entry_idx: int
    entry_paisa: int
    entry_ts: datetime
    entry_day: date
    sl_level: int | None
    tp_level: int | None
    mfe: int
    mae: int
    last_idx: int
    last_exit_basis: Decimal
    last_ts: datetime


def _to_paisa(price: Decimal) -> int:
    """Re-derived rupee→paisa snap: 2-dp ROUND_HALF_UP quantize × 100, exact int."""
    return int(price.quantize(_PAISA_QUANTUM, rounding=ROUND_HALF_UP) * 100)


def _forced_boundary(day: date, cache: dict[date, int | None]) -> int:
    """Resolve *day*'s forced-exit boundary (seconds-since-midnight IST).

    Session facts come from the NSE calendar (data, not simulator logic);
    an in-coverage non-session date falls back to the standard 15:20
    boundary — the production walker's own deterministic fallback. An
    out-of-coverage weekday raises
    :class:`~brutex.calendars.nse.HolidayDataUnavailableError` through
    ``get_session_checked`` (loud, exactly like production).
    """
    if day not in cache:
        session = get_session_checked(day)
        cache[day] = None if session is None else forced_exit_seconds_for_session(session)
    seconds = cache[day]
    return _DEFAULT_FORCED_EXIT_SECONDS if seconds is None else seconds


def _fold_excursion(pos: _RefPosition, high_paisa: int, low_paisa: int, *, buy: bool) -> None:
    """Fold one ``(high, low)`` paisa pair into the running MFE/MAE (both >= 0)."""
    if buy:
        favourable = high_paisa - pos.entry_paisa
        adverse = pos.entry_paisa - low_paisa
    else:
        favourable = pos.entry_paisa - low_paisa
        adverse = high_paisa - pos.entry_paisa
    pos.mfe = max(pos.mfe, favourable)
    pos.mae = max(pos.mae, adverse)


def _close_position(
    pos: _RefPosition,
    *,
    exit_paisa: int,
    exit_ts: datetime,
    exit_idx: int,
    reason: str,
    buy: bool,
) -> RefTrade:
    """Materialise the closed round-trip record."""
    pnl = exit_paisa - pos.entry_paisa if buy else pos.entry_paisa - exit_paisa
    return RefTrade(
        entry_idx=pos.entry_idx,
        exit_idx=exit_idx,
        entry_ts=pos.entry_ts,
        exit_ts=exit_ts,
        entry_paisa=pos.entry_paisa,
        exit_paisa=exit_paisa,
        pnl_paisa=pnl,
        mfe_paisa=pos.mfe,
        mae_paisa=pos.mae,
        holding_bars=exit_idx - pos.entry_idx + 1,
        exit_reason=reason,
    )


def _reference_arm_map(
    bars: Sequence[CandleBar],
    exec_bars: Sequence[CandleBar],
    *,
    tf_minutes: int,
) -> list[int]:
    """The reference's OWN signal→1m-fill mapping (a flat forward two-pointer).

    Independently re-states the DEC-FILL-EXEC-GRID-1M-001 mapping law
    (design §3.11 — :mod:`brutex.engine.exec_grid` is a banned import):
    decision bar ``N``'s fill candidate is the first stored 1m bar whose
    timestamp is at-or-after ``ts_tf[N] + tf_minutes``; a candidate on a
    DIFFERENT IST session date DROPS (the session-bounded slide law), a
    same-date candidate at/after the arming session's close instant is
    REFUSED (the B6 bounded-distance assertion — session close from the
    canonical calendar, data not simulator logic; an in-coverage
    non-session date falls back to the regular 15:30 close), and when two
    decision bars map onto ONE 1m bar the EARLIEST-armed wins (B4).

    Returns one entry per 1m bar: the winning decision index, else ``-1``.
    """
    n_exec = len(exec_bars)
    armed = [-1] * n_exec
    close_cache: dict[date, int] = {}
    j = 0
    for n, dbar in enumerate(bars):
        close_instant = dbar.timestamp + timedelta(minutes=tf_minutes)
        while j < n_exec and exec_bars[j].timestamp < close_instant:
            j += 1
        if j >= n_exec:
            # The stored 1m tail ended — this and every later decision bar
            # simply never fills (the design §5.2 cause-4 shape).
            break
        fill_ist = exec_bars[j].timestamp.astimezone(IST)
        arming_day = dbar.timestamp.astimezone(IST).date()
        if fill_ist.date() != arming_day:
            continue  # cross-session slide attempts DROP.
        if arming_day not in close_cache:
            session = get_session_checked(arming_day)
            close_cache[arming_day] = (
                _DEFAULT_SESSION_CLOSE_SECONDS
                if session is None
                else session.close_time_ist.hour * 3600 + session.close_time_ist.minute * 60
            )
        fill_seconds = fill_ist.hour * 3600 + fill_ist.minute * 60 + fill_ist.second
        if fill_seconds >= close_cache[arming_day]:
            continue  # B6: at/after the arming session's close — refused.
        if armed[j] >= 0:
            continue  # B4: an earlier decision bar already claimed j.
        armed[j] = n
    return armed


def reference_simulate(
    bars: Sequence[CandleBar],
    entry_fires: Sequence[bool],
    *,
    direction: str,
    tf_minutes: int,
    sl_paisa: int | None,
    tp_paisa: int | None,
    exec_bars: Sequence[CandleBar],
) -> list[RefTrade]:
    """Naive dual-stream re-simulation of the locked Track-1 fill rules.

    Implements the ONE hardcoded worst-case fill law on the 1-minute
    execution grid exactly (adverse-max fills, pessimistic same-bar
    SL-first tie, the same-bar deferral, the B3 terminal CLOSE carve-out,
    no Mode-B exit signals — DEC-FILL-WORST-CASE-ONLY-001 +
    DEC-FILL-EXEC-GRID-1M-001) — see the module docstring for the full
    independently re-derived rule list.

    Parameters
    ----------
    bars
        Chronologically ordered DECISION-TF bars for one instrument (UTC
        timestamps, Decimal-rupee prices) — the signal domain.
    entry_fires
        One ``bool`` per DECISION bar — ``True`` arms an entry at that
        bar's next-1m fill (same length as *bars*).
    direction
        ``"BUY"`` or ``"SELL"`` (the ``Side`` value strings).
    tf_minutes
        DECISION bar timeframe in whole minutes (``>= 1``); the walk is
        always on the 1-minute grid.
    sl_paisa, tp_paisa
        Stop / target distances from entry in paisa (``> 0``) or ``None``
        to disable that leg.
    exec_bars
        Chronologically ordered 1-MINUTE execution bars — the fill/walk
        domain (at ``tf_minutes == 1`` pass the same sequence as *bars*).

    Returns
    -------
    list[RefTrade]
        Closed round-trips in the order they closed (indices are 1-MINUTE
        indices into *exec_bars*).

    Raises
    ------
    ValueError
        On an unknown direction, ``tf_minutes < 1``, a non-positive
        stop/target distance, or a signal/bar length mismatch.
    """
    if direction not in _VALID_DIRECTIONS:
        raise ValueError(f"direction must be one of {sorted(_VALID_DIRECTIONS)}, got {direction!r}")
    if tf_minutes < 1:
        raise ValueError(f"tf_minutes must be >= 1, got {tf_minutes}")
    if sl_paisa is not None and sl_paisa <= 0:
        raise ValueError(f"sl_paisa must be > 0 or None, got {sl_paisa}")
    if tp_paisa is not None and tp_paisa <= 0:
        raise ValueError(f"tp_paisa must be > 0 or None, got {tp_paisa}")
    if len(entry_fires) != len(bars):
        raise ValueError(f"entry_fires length {len(entry_fires)} != bars length {len(bars)}")

    buy = direction == "BUY"
    armed = _reference_arm_map(bars, exec_bars, tf_minutes=tf_minutes)
    trades: list[RefTrade] = []
    fe_cache: dict[date, int | None] = {}
    pos: _RefPosition | None = None

    for j, bar in enumerate(exec_bars):
        src = armed[j]
        due = src >= 0 and bool(entry_fires[src])
        closed: RefTrade | None = None
        # Session/forced resolution only when a position or a due fill needs
        # it (the calendar-idle law: the idle path never touches the
        # calendar, so an out-of-coverage date on a flat stretch cannot
        # raise).
        if pos is not None or due:
            ts_ist = bar.timestamp.astimezone(IST)
            day = ts_ist.date()
            open_seconds = ts_ist.hour * 3600 + ts_ist.minute * 60 + ts_ist.second
            # A 1-MINUTE bar is forced iff its [open, open+1min) window
            # passes the session's close − 10 min boundary (the walk grid is
            # the 1m stream — tf is 1 here regardless of the decision TF).
            forced = open_seconds + 60 > _forced_boundary(day, fe_cache)

            if pos is not None and day != pos.entry_day:
                # (A2) Session-boundary close (DEC-TRADESIM-OPEN-SESSION-
                # GUARD-001 on the 1m stream): an open position never
                # survives an IST session-date change. On the FIRST 1m bar
                # whose session date differs (pure ``!=``, either direction
                # — fires at most once even across a multi-session hole)
                # close retroactively at the PRIOR session's last MANAGED 1m
                # bar — its resolved exit basis / timestamp / index, read
                # BEFORE this bar touches ``last_*`` — with NO excursion
                # fold from this boundary bar, whose forced flag is then
                # irrelevant (the reference is flat for the rest of this
                # iteration). ``closed`` non-None blocks the (A) fill below,
                # so the boundary bar never opens a new position.
                closed = _close_position(
                    pos,
                    exit_paisa=_to_paisa(pos.last_exit_basis),
                    exit_ts=pos.last_ts,
                    exit_idx=pos.last_idx,
                    reason="session_gap",
                    buy=buy,
                )
                pos = None

            # (B) Manage the open position on this 1m bar — bars strictly
            # AFTER the entry bar only (the entry bar is fully handled at
            # the (A) fill below: full-range excursion fold, NO exit
            # detection — the same-bar deferral law §2.4).
            elif pos is not None:
                pos.last_idx = j
                # The retroactive-close basis: this bar's adverse extreme
                # (a managed bar is never the entry bar, so the B3 terminal
                # CLOSE carve-out no longer applies once any bar is managed).
                pos.last_exit_basis = bar.low if buy else bar.high
                pos.last_ts = bar.timestamp
                if forced:
                    open_paisa = _to_paisa(bar.open)
                    # The excursion fold stays the legacy (open, open) —
                    # only the fill PRICE is worst-case (the forced bar's
                    # adverse extreme; an open-anchored event, no level).
                    _fold_excursion(pos, open_paisa, open_paisa, buy=buy)
                    forced_paisa = _to_paisa(bar.low if buy else bar.high)
                    closed = _close_position(
                        pos,
                        exit_paisa=forced_paisa,
                        exit_ts=bar.timestamp,
                        exit_idx=j,
                        reason="forced_1520",
                        buy=buy,
                    )
                    pos = None
                else:
                    high_paisa = _to_paisa(bar.high)
                    low_paisa = _to_paisa(bar.low)
                    _fold_excursion(pos, high_paisa, low_paisa, buy=buy)
                    sl = pos.sl_level
                    tp = pos.tp_level
                    sl_hit = sl is not None and (low_paisa <= sl if buy else high_paisa >= sl)
                    tp_hit = tp is not None and (high_paisa >= tp if buy else low_paisa <= tp)
                    # Pessimistic same-bar tie: the stop fills first
                    # (SL-first, hardcoded). The chosen level fills with the
                    # adverse-max clamp — min(level, low) for a BUY exit /
                    # max(level, high) for a SELL exit: a gap-through bar
                    # fills at the extreme, a bar gapping PAST the level in
                    # the trade's favour stays AT the level (never
                    # overstating) — DEC-FILL-WORST-CASE-ONLY-001.
                    if sl_hit:
                        # sl is narrowed non-None by sl_hit's construction.
                        level = sl if sl is not None else 0
                        closed = _close_position(
                            pos,
                            exit_paisa=min(level, low_paisa) if buy else max(level, high_paisa),
                            exit_ts=bar.timestamp,
                            exit_idx=j,
                            reason="stop_loss",
                            buy=buy,
                        )
                        pos = None
                    elif tp_hit:
                        level = tp if tp is not None else 0
                        closed = _close_position(
                            pos,
                            exit_paisa=min(level, low_paisa) if buy else max(level, high_paisa),
                            exit_ts=bar.timestamp,
                            exit_idx=j,
                            reason="take_profit",
                            buy=buy,
                        )
                        pos = None

            # (A) Fill a due entry on THIS 1m bar — the mapping already
            # timed + session-gated it; the walk-side gates: never while a
            # position is open, never on a bar a trade just closed (the
            # exit/boundary bar is consumed), and never on a forced-exit 1m
            # bar (cannot open at/after the session's forced instant).
            if pos is None and closed is None and due and not forced:
                # The re-stated adverse-max law: the entry fills at the
                # entry 1m bar's ADVERSE extreme — BUY at HIGH, SELL at LOW
                # (DEC-FILL-WORST-CASE-ONLY-001). The SL/TP levels anchor
                # on the ACTUAL worst entry fill.
                entry_paisa = _to_paisa(bar.high if buy else bar.low)
                if buy:
                    sl_level = None if sl_paisa is None else entry_paisa - sl_paisa
                    tp_level = None if tp_paisa is None else entry_paisa + tp_paisa
                else:
                    sl_level = None if sl_paisa is None else entry_paisa + sl_paisa
                    tp_level = None if tp_paisa is None else entry_paisa - tp_paisa
                pos = _RefPosition(
                    entry_idx=j,
                    entry_paisa=entry_paisa,
                    entry_ts=bar.timestamp,
                    # The IST SESSION date the fill resolved (never the
                    # UTC date) — the (A2) session key.
                    entry_day=day,
                    sl_level=sl_level,
                    tp_level=tp_level,
                    mfe=0,
                    mae=0,
                    last_idx=j,
                    # The B3 terminal carve-out basis: while the LAST bar is
                    # the trade's OWN entry bar, a retroactive close prices
                    # at its CLOSE (never the adverse extreme — that would
                    # be the full-range phantom loss the same-bar deferral
                    # exists to kill). Every managed successor overwrites
                    # this with its adverse extreme.
                    last_exit_basis=bar.close,
                    last_ts=bar.timestamp,
                )
                # §2.4: the entry bar folds its FULL range but NEVER exits.
                _fold_excursion(pos, _to_paisa(bar.high), _to_paisa(bar.low), buy=buy)
        if closed is not None:
            trades.append(closed)

    # End of data: a still-open position closes at the LAST managed 1m
    # bar's resolved exit basis (adverse extreme, or the entry bar's CLOSE
    # under the B3 terminal carve-out).
    if pos is not None:
        trades.append(
            _close_position(
                pos,
                exit_paisa=_to_paisa(pos.last_exit_basis),
                exit_ts=pos.last_ts,
                exit_idx=pos.last_idx,
                reason="end_of_data",
                buy=buy,
            )
        )
    return trades


def reference_drawdown(
    ref_trades: Sequence[RefTrade],
    *,
    bars: Sequence[CandleBar],
    start_equity_paisa: int,
) -> tuple[int, float, int, float]:
    """Re-derived equity fold + drawdown scan over the reference trades.

    Each trade's P&L lands on its OWNING DECISION bar — the LAST decision
    bar whose label is at-or-before the trade's ``exit_ts``, re-derived
    here with a flat backward-compatible ``bisect`` over the decision
    labels (DEC-FILL-EXEC-GRID-1M-001 §3.8: exits resolve on the 1-minute
    grid, so a coarse-TF ``exit_ts`` is an INTERIOR instant of its owning
    bucket; the label-aligned exit is the ``exit_ts == label`` special case
    of the same fold). Equity is the running sum from *start_equity_paisa*,
    one sample per DECISION bar. The scan re-derives the documented
    drawdown definitions (largest peak-to-trough drop in paisa,
    fraction-of-peak percentage ×100 — unclamped, zero-or-negative peak
    contributes no percentage — longest below-peak bar run, and the Ulcer
    Index = RMS of the percentage-drawdown series ×100) with the SAME
    arithmetic (int subtraction, correctly-rounded ``int/int`` division,
    float square accumulation, ``sqrt(sum/n)``) so a matching engine is
    bit-identical.

    Parameters
    ----------
    ref_trades
        The :func:`reference_simulate` output.
    bars
        The DECISION-TF bars of the window (non-empty, chronological).
    start_equity_paisa
        Notional capital base in paisa.

    Returns
    -------
    tuple[int, float, int, float]
        ``(max_dd_paisa, max_dd_pct, max_dd_bars, ulcer_index)``.

    Raises
    ------
    ValueError
        If *bars* is empty or a trade's ``exit_ts`` precedes the first
        decision label (out of window).
    """
    n_bars = len(bars)
    if n_bars <= 0:
        raise ValueError(f"bars must be non-empty, got {n_bars}")
    labels = [bar.timestamp for bar in bars]
    realized = [0] * n_bars
    for trade in ref_trades:
        owner = bisect_right(labels, trade.exit_ts) - 1
        if owner < 0:
            raise ValueError(
                f"exit_ts {trade.exit_ts!r} precedes the first decision label {labels[0]!r}"
            )
        realized[owner] += trade.pnl_paisa
    running = start_equity_paisa
    equity: list[int] = []
    for pnl in realized:
        running += pnl
        equity.append(running)
    peak = equity[0]
    max_dd_paisa = 0
    max_dd_pct = 0.0
    sum_sq_pct = 0.0
    current_dd_bars = 0
    max_dd_bars = 0
    for value in equity:
        peak = max(peak, value)
        drop = peak - value
        if drop > 0:
            current_dd_bars += 1
        else:
            current_dd_bars = 0
        max_dd_bars = max(max_dd_bars, current_dd_bars)
        max_dd_paisa = max(max_dd_paisa, drop)
        if peak > 0:
            dd_pct = drop / peak
            max_dd_pct = max(max_dd_pct, dd_pct)
            sum_sq_pct += dd_pct * dd_pct
    ulcer = math.sqrt(sum_sq_pct / n_bars)
    return max_dd_paisa, max_dd_pct * 100.0, max_dd_bars, ulcer * 100.0


def drawdown_bit_key(stats: tuple[int, float, int, float]) -> tuple[int, bytes, int, bytes]:
    """Map a drawdown 4-tuple to its BIT-exact comparison key.

    Ints compare exactly under ``==``; the two floats compare by their
    IEEE-754 bit pattern (``struct.pack('<d', …)``) so a signed zero or
    payload difference can never hide behind float equality — the burn-in
    self-check idiom, RE-DERIVED here on the reference side (the production
    ``rust_metrics.drawdown_compare_key`` is a banned import; parity between
    the two keys is unit-pinned test-side). Ints are deliberately NOT
    packed — an unbounded Python int could overflow the pack.
    """
    max_dd_paisa, max_dd_pct, max_dd_bars, ulcer = stats
    return (max_dd_paisa, struct.pack("<d", max_dd_pct), max_dd_bars, struct.pack("<d", ulcer))


def sample_masks(
    *,
    sweep_id: str,
    tuple_digest: str,
    data_snapshot_hash: str,
    masks: Sequence[int],
    k: int,
) -> tuple[int, ...]:
    """Deterministically sample ``min(k, len(masks))`` masks from canonical order.

    Seed = ``int(blake3(f"diffcheck|{sweep_id}|{tuple_digest}|{data_snapshot_hash}")
    .hexdigest()[:16], 16)`` (design D-11) — run identity only, NO wall
    clock, no global RNG. Indices are sampled with ``random.Random(seed)``
    and returned SORTED, so the picked masks preserve the canonical
    enumeration order of *masks*.

    Parameters
    ----------
    sweep_id, tuple_digest, data_snapshot_hash
        The run-identity seed material.
    masks
        The tuple's masks in canonical (post-conflict-prune) enumeration
        order.
    k
        Sample budget (``>= 0``; ``0`` or an empty *masks* returns ``()``).

    Raises
    ------
    ValueError
        If ``k`` is negative.
    """
    if k < 0:
        raise ValueError(f"k must be >= 0, got {k}")
    if k == 0 or not masks:
        return ()
    material = f"diffcheck|{sweep_id}|{tuple_digest}|{data_snapshot_hash}"
    seed = int(blake3.blake3(material.encode("utf-8")).hexdigest()[:16], 16)
    rng = random.Random(seed)  # noqa: S311  # nosec B311 — deterministic identity-seeded sampling, not crypto
    picked = sorted(rng.sample(range(len(masks)), min(k, len(masks))))
    return tuple(masks[i] for i in picked)


def resolve_diffcheck_k(env: Mapping[str, str] | None = None) -> int:
    """Resolve the per-run diff-check budget from the environment (fail-loud).

    ``BRUTEX_SWEEP_DIFFCHECK`` is the default-ON tristate kill switch
    (:func:`~brutex.engine.sweep_lookahead.env_flag_tristate` vocabulary):
    unset / empty ⇒ ON; an explicit falsy literal ⇒ ``0`` (disabled); an
    explicit truthy literal ⇒ ON; anything else RAISES ``ValueError`` —
    resolved at launch, so a typo'd kill switch refuses loudly instead of
    silently arming or disarming (the fail-loud tier of the house env-flag
    conventions). When ON, ``BRUTEX_SWEEP_DIFFCHECK_K`` overrides the
    :data:`DIFFCHECK_DEFAULT_K_PER_RUN` budget (integer ``>= 0``; junk or a
    negative RAISES).

    Parameters
    ----------
    env
        Environment mapping; defaults to ``os.environ`` (injectable).

    Returns
    -------
    int
        The per-run K budget (``0`` = disabled).
    """
    src: Mapping[str, str] = os.environ if env is None else env
    raw = src.get(DIFFCHECK_ENV, "")
    if raw.strip() == "":
        enabled = True
    else:
        flag = env_flag_tristate(raw)
        if flag is None:
            raise ValueError(
                f"{DIFFCHECK_ENV}={raw!r} is not a recognised flag value "
                "(accepted: 1/true/yes/on or 0/false/no/off)"
            )
        enabled = flag
    if not enabled:
        return 0
    raw_k = src.get(DIFFCHECK_K_ENV, "")
    if raw_k.strip() == "":
        return DIFFCHECK_DEFAULT_K_PER_RUN
    try:
        k = int(raw_k.strip())
    except ValueError as exc:
        raise ValueError(f"{DIFFCHECK_K_ENV}={raw_k!r} must be an integer >= 0") from exc
    if k < 0:
        raise ValueError(f"{DIFFCHECK_K_ENV}={raw_k!r} must be an integer >= 0")
    return k


@dataclass(frozen=True, slots=True)
class DiffCheckMismatch:
    """One production-vs-reference divergence (the DIFFCHECK_MISMATCH payload).

    Attributes
    ----------
    tuple_digest
        The tuple's store digest.
    mask_hex
        The mismatching mask as 32-hex (u128).
    field
        The first diverging field (``n_trades`` / ``trade[j].<field>`` /
        ``drawdown``).
    prod, ref
        ``repr`` of the production / reference values.
    """

    tuple_digest: str
    mask_hex: str
    field: str
    prod: str
    ref: str

    def payload(self) -> dict[str, str]:
        """The flight-recorder record payload (design §3 field names)."""
        return {
            "tuple_digest": self.tuple_digest,
            "mask_hex": self.mask_hex,
            "field": self.field,
            "prod": self.prod,
            "ref": self.ref,
        }


@dataclass(frozen=True, slots=True)
class DiffCheckReport:
    """Per-tuple spot-check outcome.

    Attributes
    ----------
    checked
        Masks actually diff-checked this tuple.
    mismatches
        How many of them diverged.
    masks_mismatched
        Mismatching mask hexes, capped at :data:`MASKS_MISMATCHED_CAP`.
    """

    checked: int
    mismatches: int
    masks_mismatched: tuple[str, ...]


class _TimeframeLike(Protocol):
    """The tuple's timeframe surface the hook reads (``Timeframe.minutes``)."""

    @property
    def minutes(self) -> int:
        """Bar length of the tuple's timeframe, in minutes."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed


class _DirectionLike(Protocol):
    """The tuple's direction surface (``Side.value`` → ``"BUY"``/``"SELL"``)."""

    @property
    def value(self) -> str:
        """Direction token — ``"BUY"`` or ``"SELL"``."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed


class DiffCheckTupleLike(Protocol):
    """Structural view of a sweep tuple (``sweep_orchestrator.SweepTuple``)."""

    @property
    def k(self) -> int:
        """Number of active condition bits in the tuple's mask."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed

    @property
    def sl_paisa(self) -> int | None:
        """Stop-loss distance in paisa, or ``None`` when unset."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed

    @property
    def tp_paisa(self) -> int | None:
        """Take-profit distance in paisa, or ``None`` when unset."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed

    @property
    def tf(self) -> _TimeframeLike:
        """The tuple's timeframe surface."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed

    @property
    def direction(self) -> _DirectionLike:
        """The tuple's trade direction surface."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed


class DiffCheckGridLike(Protocol):
    """Structural view of a sweep grid (``sweep_orchestrator.SweepGridConfig``)."""

    @property
    def condition_bits(self) -> Sequence[int]:
        """The grid's active condition bit indices."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed

    @property
    def data_snapshot_hash(self) -> str:
        """Full-content snapshot digest of the loaded bars."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed

    @property
    def start_equity_paisa(self) -> int:
        """Starting equity for the simulation, in paisa."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed


class ProductionTradeLike(Protocol):
    """The compare surface of a production ``ClosedTrade`` (ints + datetimes)."""

    @property
    def pnl_paisa(self) -> int:
        """Realised profit-and-loss of the trade, in paisa."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed

    @property
    def mfe_paisa(self) -> int:
        """Maximum favourable excursion of the trade, in paisa."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed

    @property
    def mae_paisa(self) -> int:
        """Maximum adverse excursion of the trade, in paisa."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed

    @property
    def holding_bars(self) -> int:
        """Number of bars the trade was held."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed

    @property
    def entry_ts(self) -> datetime:
        """Entry timestamp of the trade."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed

    @property
    def exit_ts(self) -> datetime:
        """Exit timestamp of the trade."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed


class ProductionSimulate(Protocol):
    """The injected production trade-sim leg (a ``simulate_trades`` adapter).

    ``exec_bars`` is the 1-minute EXECUTION stream (DEC-FILL-EXEC-GRID-1M-001
    — the adapter threads it as ``simulate_trades(..., exec_bars_1m=…)``).
    """

    def __call__(
        self,
        bars: Sequence[CandleBar],
        entry_signals: Sequence[bool],
        *,
        direction: str,
        tf_minutes: int,
        stop_loss_paisa: int | None,
        take_profit_paisa: int | None,
        exec_bars: Sequence[CandleBar],
    ) -> Sequence[ProductionTradeLike]:
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed


class ProductionDrawdown(Protocol):
    """The injected production equity-fold + drawdown leg.

    Expected wiring (run core): ``_drawdown_stats(build_equity_curve(bars,
    trades, start_equity_paisa=…, tf_minutes=…))``.
    """

    def __call__(
        self,
        bars: Sequence[CandleBar],
        trades: Sequence[ProductionTradeLike],
        *,
        start_equity_paisa: int,
        tf_minutes: int,
    ) -> tuple[int, float, int, float]:
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed


class DiffCheckHook:
    """Per-grid R45 spot-check hook — built once by the run core, called per tuple.

    The production legs are INJECTED (design D-11 + the module import ban):
    the run core passes ``simulate_trades`` and the equity/drawdown fold as
    callables, so this module never imports them. Strictly post-persist +
    read-only: the hook returns a :class:`DiffCheckReport` and fans a
    mismatch out through ``on_mismatch`` — it has NO channel to alter a
    result byte (never-adopt law, I-4).

    Parameters
    ----------
    sweep_id
        The run identity (``grid_identity(grid)``) — sampler seed material.
    tuple_digest_of
        Callable mapping ``(tuple_, grid)`` to the tuple's canonical store
        digest (the run core passes the orchestrator's digest fn).
    production_simulate
        The production trade-sim adapter (:class:`ProductionSimulate`).
    production_drawdown
        The production drawdown adapter (:class:`ProductionDrawdown`).
    on_mismatch
        Fan-out for each :class:`DiffCheckMismatch` (DIFFCHECK_MISMATCH
        record + SUSPECT latch + alert — composed by the run core).
        Guarded: a raising sink is WARNed, never re-raised.

    Raises
    ------
    ValueError
        If ``sweep_id`` is empty.
    """

    def __init__(
        self,
        *,
        sweep_id: str,
        tuple_digest_of: Callable[[DiffCheckTupleLike, DiffCheckGridLike], str],
        production_simulate: ProductionSimulate,
        production_drawdown: ProductionDrawdown,
        on_mismatch: Callable[[DiffCheckMismatch], None],
    ) -> None:
        if not sweep_id:
            raise ValueError("sweep_id must be non-empty")
        self._sweep_id = sweep_id
        self._tuple_digest_of = tuple_digest_of
        self._production_simulate = production_simulate
        self._production_drawdown = production_drawdown
        self._on_mismatch = on_mismatch

    def run_for_tuple(
        self,
        *,
        bars: Sequence[CandleBar],
        bar_bits: Sequence[int],
        exec_bars: Sequence[CandleBar],
        tuple_: DiffCheckTupleLike,
        grid: DiffCheckGridLike,
        budget_left: int,
    ) -> DiffCheckReport:
        """Spot-check up to *budget_left* sampled masks of one tuple.

        Enumerates the tuple's masks in the canonical production order
        (:func:`iter_masks_k_of_bits` + conflict pruning — an O(n_masks)
        integer walk, honest envelope), samples deterministically, then for
        each sampled mask re-derives the signal projection inline
        (``(bits & mask) == mask``) and compares the injected production
        legs against the reference field-for-field (ints/datetimes exact;
        drawdown 4-tuple by bit key). Fail-soft: an internal error is
        WARNed and the accumulated report returned — never raised into the
        sweep (I-3).

        Parameters
        ----------
        bars, bar_bits
            The tuple's certified DECISION bar window + Stage-1A condition
            bitsets.
        exec_bars
            The tuple's 1-minute EXECUTION stream
            (DEC-FILL-EXEC-GRID-1M-001) — threaded verbatim to BOTH legs.
        tuple_, grid
            The sweep tuple + owning grid (structural views).
        budget_left
            Remaining run-level K budget; ``<= 0`` returns an empty report.
        """
        if budget_left <= 0:
            return DiffCheckReport(checked=0, mismatches=0, masks_mismatched=())
        checked = 0
        mismatches = 0
        hexes: list[str] = []
        try:
            masks = [
                mask
                for mask in iter_masks_k_of_bits(grid.condition_bits, tuple_.k)
                if not has_conflict(mask)
            ]
            digest = self._tuple_digest_of(tuple_, grid)
            picked = sample_masks(
                sweep_id=self._sweep_id,
                tuple_digest=digest,
                data_snapshot_hash=grid.data_snapshot_hash,
                masks=masks,
                k=budget_left,
            )
            for mask in picked:
                mismatch = self._check_mask(
                    mask=mask,
                    digest=digest,
                    bars=bars,
                    bar_bits=bar_bits,
                    exec_bars=exec_bars,
                    tuple_=tuple_,
                    grid=grid,
                )
                checked += 1
                if mismatch is not None:
                    mismatches += 1
                    if len(hexes) < MASKS_MISMATCHED_CAP:
                        hexes.append(mismatch.mask_hex)
        except Exception as exc:  # fail-soft law (I-3) — observability never kills the run
            with contextlib.suppress(Exception):
                _LOG.warning(
                    "sweep_diffcheck.error",
                    error=str(exc),
                    error_type=type(exc).__name__,
                    checked=checked,
                )
        return DiffCheckReport(
            checked=checked, mismatches=mismatches, masks_mismatched=tuple(hexes)
        )

    def _check_mask(
        self,
        *,
        mask: int,
        digest: str,
        bars: Sequence[CandleBar],
        bar_bits: Sequence[int],
        exec_bars: Sequence[CandleBar],
        tuple_: DiffCheckTupleLike,
        grid: DiffCheckGridLike,
    ) -> DiffCheckMismatch | None:
        """Diff one mask; return the first divergence (fan-out fired) or ``None``."""
        # Signal projection re-derived INLINE (never mask_to_entry_signals).
        fires = [(bits & mask) == mask for bits in bar_bits]
        direction = tuple_.direction.value
        tf_minutes = tuple_.tf.minutes
        prod_trades = self._production_simulate(
            bars,
            fires,
            direction=direction,
            tf_minutes=tf_minutes,
            stop_loss_paisa=tuple_.sl_paisa,
            take_profit_paisa=tuple_.tp_paisa,
            exec_bars=exec_bars,
        )
        ref_trades = reference_simulate(
            bars,
            fires,
            direction=direction,
            tf_minutes=tf_minutes,
            sl_paisa=tuple_.sl_paisa,
            tp_paisa=tuple_.tp_paisa,
            exec_bars=exec_bars,
        )
        diff = _first_trade_mismatch(prod_trades, ref_trades)
        if diff is None:
            prod_dd = self._production_drawdown(
                bars,
                prod_trades,
                start_equity_paisa=grid.start_equity_paisa,
                tf_minutes=tf_minutes,
            )
            ref_dd = reference_drawdown(
                ref_trades, bars=bars, start_equity_paisa=grid.start_equity_paisa
            )
            if drawdown_bit_key(prod_dd) != drawdown_bit_key(ref_dd):
                diff = ("drawdown", repr(prod_dd), repr(ref_dd))
        if diff is None:
            return None
        field, prod_repr, ref_repr = diff
        mismatch = DiffCheckMismatch(
            tuple_digest=digest,
            mask_hex=f"{mask:032x}",
            field=field,
            prod=prod_repr,
            ref=ref_repr,
        )
        _LOG.error("sweep_diffcheck.mismatch", **mismatch.payload())
        try:
            self._on_mismatch(mismatch)
        except Exception as exc:  # a failing sink must never re-enter the sweep
            with contextlib.suppress(Exception):
                _LOG.warning(
                    "sweep_diffcheck.on_mismatch_failed",
                    mask_hex=mismatch.mask_hex,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
        return mismatch


def _first_trade_mismatch(
    prod: Sequence[ProductionTradeLike], ref: Sequence[RefTrade]
) -> tuple[str, str, str] | None:
    """First diverging ``(field, prod_repr, ref_repr)`` on the shared surface."""
    if len(prod) != len(ref):
        return ("n_trades", str(len(prod)), str(len(ref)))
    for j, (p, r) in enumerate(zip(prod, ref, strict=True)):
        for field in COMPARED_TRADE_FIELDS:
            prod_value = getattr(p, field)
            ref_value = getattr(r, field)
            if prod_value != ref_value:
                return (f"trade[{j}].{field}", repr(prod_value), repr(ref_value))
    return None


__all__ = [
    "CI_K_SAMPLES",
    "COMPARED_TRADE_FIELDS",
    "DIFFCHECK_DEFAULT_K_PER_RUN",
    "DIFFCHECK_ENV",
    "DIFFCHECK_K_ENV",
    "MASKS_MISMATCHED_CAP",
    "DiffCheckGridLike",
    "DiffCheckHook",
    "DiffCheckMismatch",
    "DiffCheckReport",
    "DiffCheckTupleLike",
    "ProductionDrawdown",
    "ProductionSimulate",
    "ProductionTradeLike",
    "RefTrade",
    "drawdown_bit_key",
    "reference_drawdown",
    "reference_simulate",
    "resolve_diffcheck_k",
    "sample_masks",
]
