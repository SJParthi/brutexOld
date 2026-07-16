"""Property tests for the Stage-2 trade-sim walker (`brutex.engine.trade_sim`).

Invariants (hold for any chronologically-ordered bar feed + signal stream):

* **Determinism** (Golden Rule #4) — the same bars + signals + config always
  produce the same trades, bit-for-bit; a fresh walker is stateless across runs.
* **No look-ahead** (CLAUDE.md §4, Golden Rule #5) — mutating any bar STRICTLY
  AFTER a trade's exit bar can never change that trade. We assert it directly:
  perturb every bar after index ``k`` and every trade that closed on or before
  bar ``k`` is byte-identical.
* **Single position / non-overlap** — trades never overlap; each trade's entry
  bar is strictly after the previous trade's exit bar.
* **ClosedTrade invariants** — every emitted trade already satisfies the
  ``ClosedTrade`` post-init contract (MFE/MAE ``>= 0``, ``holding_bars >= 1``,
  ``exit_ts >= entry_ts``); a run that produced any trade proves it.

This file is also the live home of three verification-pyramid invariants
(``docs/TRACK1_VERIFICATION_PYRAMID.md`` §3) — promoted from the
``test_invariants_catalog.py`` ROADMAP stubs by
DEC-PROPERTY-INVARIANTS-LIVE-001 (``docs/MIGRATION.md``):

* **P-03** (``test_p03_per_trade_pnl_sums_to_balance_delta``) — Σ per-trade
  paper P&L equals ``final_balance − initial_balance`` to the exact paisa, with
  the metrics aggregator (:func:`brutex.metrics.compute.compute_metrics`)
  agreeing on every random scenario.
* **P-04** (``test_p04_gross_minus_costs_equals_net``) — Track-1 spot is
  signal-only (costs ``= 0``), so ``gross − Σcosts == net`` collapses to
  ``net_pnl == gross_profit − gross_loss`` exactly, never deducting a cost leg.
* **P-09** (``test_p09_forced_exit_no_trade_survives_1520``) — no trade survives
  the 15:20 IST forced exit: every emitted trade opens before, and closes on or
  before, the first bar whose window reaches 15:20 IST.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from itertools import pairwise

from brutex.calendars.nse import IST, is_forced_exit_bar
from brutex.data.models import CandleBar
from brutex.engine.trade_sim import FillPolicy, LevelFill, SameBarTie, simulate_trades
from brutex.metrics.compute import compute_metrics
from brutex.metrics.types import ClosedTrade, EquityCurve
from brutex.strategies.types import Side
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# pyright: reportCallIssue=false
# Rationale (same as tests/property/test_engine_stage1a_properties.py): the
# `@st.composite` strategies are invoked with no args because the decorator
# injects `draw` at runtime; pyright does not model the signature rewrite.

# 09:15:00 IST == 03:45 UTC — the session-open anchor (well before 15:20 so the
# forced-exit path is exercised by the unit/golden suites, not these invariants).
_OPEN_UTC = datetime(2024, 1, 2, 3, 45, tzinfo=UTC)

#: Per-bar paisa band: open/close in [9950, 10050], wicks up to 30 paisa each.
_PRICE_PAISA = st.integers(min_value=9950, max_value=10050)
_WICK_PAISA = st.integers(min_value=0, max_value=30)
_LEG_PAISA = st.integers(min_value=5, max_value=60)


def _rupees(paisa: int) -> Decimal:
    """Exact Decimal-rupees for an int paisa value (snaps to the 2dp grid)."""
    return Decimal(paisa) / Decimal(100)


def _bar_at(
    minute: int,
    open_p: int,
    close_p: int,
    up_wick: int,
    down_wick: int,
    *,
    anchor: datetime = _OPEN_UTC,
) -> CandleBar:
    """A valid 1-minute bar whose high/low extend the open/close by the wicks.

    ``anchor`` is the UTC timestamp of the ``minute == 0`` bar; it defaults to
    the 09:15 IST session open (well clear of 15:20) used by the accounting /
    look-ahead scenarios, and is overridden by :func:`_forced_scenarios` to
    straddle the 15:20 IST forced-exit boundary.
    """
    high_p = max(open_p, close_p) + up_wick
    low_p = min(open_p, close_p) - down_wick
    return CandleBar(
        timestamp=anchor + timedelta(minutes=minute),
        open=_rupees(open_p),
        high=_rupees(high_p),
        low=_rupees(low_p),
        close=_rupees(close_p),
        volume=0,
        open_interest=None,
    )


@st.composite
def _scenarios(draw: st.DrawFn) -> tuple[list[CandleBar], list[bool], list[bool]]:
    """Draw ``(bars, entry_signals, exit_signals)`` of equal length (2..30 bars)."""
    n = draw(st.integers(min_value=2, max_value=30))
    bars: list[CandleBar] = []
    for minute in range(n):
        open_p = draw(_PRICE_PAISA)
        close_p = draw(_PRICE_PAISA)
        bars.append(_bar_at(minute, open_p, close_p, draw(_WICK_PAISA), draw(_WICK_PAISA)))
    entry = draw(st.lists(st.booleans(), min_size=n, max_size=n))
    exit_ = draw(st.lists(st.booleans(), min_size=n, max_size=n))
    return bars, entry, exit_


_POLICIES = st.builds(
    FillPolicy,
    stop_loss=st.sampled_from(list(LevelFill)),
    take_profit=st.sampled_from(list(LevelFill)),
    same_bar=st.sampled_from(list(SameBarTie)),
)


@given(
    scenario=_scenarios(),
    direction=st.sampled_from([Side.BUY, Side.SELL]),
    sl=st.none() | _LEG_PAISA,
    tp=st.none() | _LEG_PAISA,
    policy=_POLICIES,
)
@settings(suppress_health_check=[HealthCheck.too_slow], max_examples=200)
def test_deterministic(
    scenario: tuple[list[CandleBar], list[bool], list[bool]],
    direction: Side,
    sl: int | None,
    tp: int | None,
    policy: FillPolicy,
) -> None:
    bars, entry, exit_ = scenario
    first = simulate_trades(
        bars,
        entry,
        direction=direction,
        tf_minutes=1,
        stop_loss_paisa=sl,
        take_profit_paisa=tp,
        exit_signals=exit_,
        policy=policy,
    )
    second = simulate_trades(
        bars,
        entry,
        direction=direction,
        tf_minutes=1,
        stop_loss_paisa=sl,
        take_profit_paisa=tp,
        exit_signals=exit_,
        policy=policy,
    )
    assert first == second


@given(
    scenario=_scenarios(),
    direction=st.sampled_from([Side.BUY, Side.SELL]),
    sl=st.none() | _LEG_PAISA,
    tp=st.none() | _LEG_PAISA,
    policy=_POLICIES,
    data=st.data(),
)
@settings(suppress_health_check=[HealthCheck.too_slow], max_examples=200)
def test_no_lookahead_future_mutation(
    scenario: tuple[list[CandleBar], list[bool], list[bool]],
    direction: Side,
    sl: int | None,
    tp: int | None,
    policy: FillPolicy,
    data: st.DataObject,
) -> None:
    bars, entry, exit_ = scenario
    n = len(bars)
    k = data.draw(st.integers(min_value=0, max_value=n - 2))
    cutoff = bars[k].timestamp

    # Replace every bar strictly after index k with arbitrary new OHLC, keeping
    # the original timestamps so the bar grid is unchanged.
    mutated = list(bars)
    for j in range(k + 1, n):
        open_p = data.draw(_PRICE_PAISA)
        close_p = data.draw(_PRICE_PAISA)
        mutated[j] = _bar_at(j, open_p, close_p, data.draw(_WICK_PAISA), data.draw(_WICK_PAISA))

    def _run(feed: list[CandleBar]) -> list[ClosedTrade]:
        return simulate_trades(
            feed,
            entry,
            direction=direction,
            tf_minutes=1,
            stop_loss_paisa=sl,
            take_profit_paisa=tp,
            exit_signals=exit_,
            policy=policy,
        )

    base = [t for t in _run(bars) if t.exit_ts <= cutoff]
    perturbed = [t for t in _run(mutated) if t.exit_ts <= cutoff]
    # Every trade that closed on or before bar k reads only bars 0..k → identical.
    assert base == perturbed


@given(
    scenario=_scenarios(),
    direction=st.sampled_from([Side.BUY, Side.SELL]),
    sl=st.none() | _LEG_PAISA,
    tp=st.none() | _LEG_PAISA,
    policy=_POLICIES,
)
@settings(suppress_health_check=[HealthCheck.too_slow], max_examples=200)
def test_trades_never_overlap(
    scenario: tuple[list[CandleBar], list[bool], list[bool]],
    direction: Side,
    sl: int | None,
    tp: int | None,
    policy: FillPolicy,
) -> None:
    bars, entry, exit_ = scenario
    trades = simulate_trades(
        bars,
        entry,
        direction=direction,
        tf_minutes=1,
        stop_loss_paisa=sl,
        take_profit_paisa=tp,
        exit_signals=exit_,
        policy=policy,
    )
    # Single-position: each trade's entry is strictly after the prior exit, and
    # every trade already satisfies the ClosedTrade invariants by construction.
    for prev, nxt in pairwise(trades):
        assert nxt.entry_ts > prev.exit_ts
    for trade in trades:
        assert trade.holding_bars >= 1
        assert trade.exit_ts >= trade.entry_ts
        assert trade.mfe_paisa >= 0
        assert trade.mae_paisa >= 0


# ────────────────────────────────────────────────────────────────────────────
# P-03 / P-04 — int64-paisa accounting (verification pyramid §3, guarantee G4)
#
# The accounting invariants drive the SAME ``_scenarios`` feed (anchored at the
# 09:15 IST session open, so the 15:20 forced-exit path is *not* exercised here —
# it has its own P-09 test below) through :func:`simulate_trades` and then build
# an independent int64-paisa ledger / equity curve, reconciling it against the
# metrics aggregator. DEC-PROPERTY-INVARIANTS-LIVE-001.
# ────────────────────────────────────────────────────────────────────────────

#: Starting balance for the P-03/P-04 ledger, in int64 paisa (``> 0`` for a
#: valid :class:`~brutex.metrics.types.EquityCurve`). The exact value is
#: irrelevant — the invariants are about the *delta* — so any positive base
#: exercises the reconciliation.
_INITIAL_BALANCE_PAISA = st.integers(min_value=1, max_value=10_000_000)


def _equity_curve(initial_paisa: int, pnls: Sequence[int]) -> tuple[EquityCurve, list[int]]:
    """Build the per-trade equity curve by applying each trade's paisa P&L in order.

    Returns the :class:`~brutex.metrics.types.EquityCurve` (for the metrics
    aggregator) plus the raw ``list[int]`` of per-trade balance snapshots
    (``len == len(pnls) + 1``; ``curve[0]`` is the opening balance) so the test
    can assert the per-trade ledger granularity directly.
    """
    balance = initial_paisa
    curve = [initial_paisa]
    for pnl in pnls:
        balance += pnl
        curve.append(balance)
    return (
        EquityCurve(tuple(curve), start_equity_paisa=initial_paisa, span_years=1.0),
        curve,
    )


@given(
    scenario=_scenarios(),
    direction=st.sampled_from([Side.BUY, Side.SELL]),
    sl=st.none() | _LEG_PAISA,
    tp=st.none() | _LEG_PAISA,
    policy=_POLICIES,
    initial=_INITIAL_BALANCE_PAISA,
)
@settings(suppress_health_check=[HealthCheck.too_slow], max_examples=200)
def test_p03_per_trade_pnl_sums_to_balance_delta(
    scenario: tuple[list[CandleBar], list[bool], list[bool]],
    direction: Side,
    sl: int | None,
    tp: int | None,
    policy: FillPolicy,
    initial: int,
) -> None:
    """P-03: Σ per-trade paper_pnl == final_balance − initial_balance (exact paise)."""
    bars, entry, exit_ = scenario
    trades = simulate_trades(
        bars,
        entry,
        direction=direction,
        tf_minutes=1,
        stop_loss_paisa=sl,
        take_profit_paisa=tp,
        exit_signals=exit_,
        policy=policy,
    )
    pnls = [t.pnl_paisa for t in trades]
    equity, curve = _equity_curve(initial, pnls)
    final = curve[-1]

    # Per-trade granularity: each ledger step is exactly that trade's paisa P&L.
    for prev, cur, trade in zip(curve[:-1], curve[1:], trades, strict=True):
        assert cur - prev == trade.pnl_paisa

    # Σ identity, in exact integer paise (no float drift ever creeps in).
    delta = final - initial
    assert sum(pnls) == delta
    assert isinstance(delta, int)

    # The metrics aggregator's net P&L agrees with the balance delta to the paisa.
    metrics = compute_metrics(trades, equity, total_bars=max(1, len(bars)))
    assert metrics.net_pnl_paisa == delta
    assert isinstance(metrics.net_pnl_paisa, int)


@given(
    scenario=_scenarios(),
    direction=st.sampled_from([Side.BUY, Side.SELL]),
    sl=st.none() | _LEG_PAISA,
    tp=st.none() | _LEG_PAISA,
    policy=_POLICIES,
    initial=_INITIAL_BALANCE_PAISA,
)
@settings(suppress_health_check=[HealthCheck.too_slow], max_examples=200)
def test_p04_gross_minus_costs_equals_net(
    scenario: tuple[list[CandleBar], list[bool], list[bool]],
    direction: Side,
    sl: int | None,
    tp: int | None,
    policy: FillPolicy,
    initial: int,
) -> None:
    """P-04: gross_pnl − Σ costs == net_pnl (Track 1: costs=0, so gross == net)."""
    bars, entry, exit_ = scenario
    trades = simulate_trades(
        bars,
        entry,
        direction=direction,
        tf_minutes=1,
        stop_loss_paisa=sl,
        take_profit_paisa=tp,
        exit_signals=exit_,
        policy=policy,
    )
    pnls = [t.pnl_paisa for t in trades]
    equity, _curve = _equity_curve(initial, pnls)
    metrics = compute_metrics(trades, equity, total_bars=max(1, len(bars)))

    # Gross profit / loss are non-negative magnitudes (loss is stored absolute).
    assert metrics.gross_profit_paisa >= 0
    assert metrics.gross_loss_paisa >= 0
    # costs == 0 for signal-only spot (CLAUDE.md §7), so the cost-free identity
    # gross − Σcosts == net collapses to net == gross_profit − gross_loss, exact.
    assert metrics.net_pnl_paisa == metrics.gross_profit_paisa - metrics.gross_loss_paisa
    # ...and net is the RAW directional sum with no cost leg deducted anywhere.
    assert metrics.net_pnl_paisa == sum(pnls)
    assert isinstance(metrics.net_pnl_paisa, int)


# ────────────────────────────────────────────────────────────────────────────
# P-09 — forced 15:20 IST exit (verification pyramid §3, guarantee G4)
# ────────────────────────────────────────────────────────────────────────────

#: 15:10 IST == 09:40 UTC. Anchoring the feed here makes every drawn scenario
#: (>= 11 one-minute bars) straddle the 15:20 IST forced-exit boundary, so the
#: forced-exit path is always exercised (bar index 10 opens at 15:20 IST).
_FORCED_OPEN_UTC = datetime(2024, 1, 2, 9, 40, tzinfo=UTC)


@st.composite
def _forced_scenarios(draw: st.DrawFn) -> tuple[list[CandleBar], list[bool], list[bool]]:
    """Draw a feed that straddles the 15:20 IST forced exit (11..40 1-minute bars)."""
    n = draw(st.integers(min_value=11, max_value=40))
    bars: list[CandleBar] = []
    for minute in range(n):
        open_p = draw(_PRICE_PAISA)
        close_p = draw(_PRICE_PAISA)
        bars.append(
            _bar_at(
                minute,
                open_p,
                close_p,
                draw(_WICK_PAISA),
                draw(_WICK_PAISA),
                anchor=_FORCED_OPEN_UTC,
            )
        )
    entry = draw(st.lists(st.booleans(), min_size=n, max_size=n))
    exit_ = draw(st.lists(st.booleans(), min_size=n, max_size=n))
    return bars, entry, exit_


@given(
    scenario=_forced_scenarios(),
    direction=st.sampled_from([Side.BUY, Side.SELL]),
    sl=st.none() | _LEG_PAISA,
    tp=st.none() | _LEG_PAISA,
    policy=_POLICIES,
)
@settings(suppress_health_check=[HealthCheck.too_slow], max_examples=200)
def test_p09_forced_exit_no_trade_survives_1520(
    scenario: tuple[list[CandleBar], list[bool], list[bool]],
    direction: Side,
    sl: int | None,
    tp: int | None,
    policy: FillPolicy,
) -> None:
    """P-09: no trade survives 15:20 IST — entries open before, exits land by, the boundary."""
    bars, entry, exit_ = scenario
    # The first bar whose [open, open + 1m) window reaches 15:20 IST. Guaranteed
    # to exist (>= 11 bars from 15:10 IST), so ``next`` never raises.
    forced_idx = next(
        i
        for i, bar in enumerate(bars)
        if is_forced_exit_bar(bar.timestamp.astimezone(IST).time(), 1)
    )
    boundary_ts = bars[forced_idx].timestamp
    trades = simulate_trades(
        bars,
        entry,
        direction=direction,
        tf_minutes=1,
        stop_loss_paisa=sl,
        take_profit_paisa=tp,
        exit_signals=exit_,
        policy=policy,
    )
    for trade in trades:
        # No position opens on or after the forced bar...
        assert trade.entry_ts < boundary_ts
        # ...and none survives past it (force-closed at the boundary at the latest).
        assert trade.exit_ts <= boundary_ts


# ── Cross-session pending-entry drop (R2-OC-2) ──────────────────────────────
#
# No trade's entry FILL may land on a different IST session date than the bar
# its signal armed on. The walker fills an armed entry on the VERY NEXT bar, so
# the invariant is exactly: the entry bar and its immediate predecessor share
# one IST calendar date — equivalently, no trade ever ENTERS on the first bar
# of a session. Pre-fix, a signal on day-1's last stored bar filled at day-2's
# 09:15 open (the overnight-entry leak this property would have caught).

# Day-2 09:15 IST == 03:45 UTC — the next NSE session's open anchor.
_DAY2_OPEN_UTC = datetime(2024, 1, 3, 3, 45, tzinfo=UTC)


@st.composite
def _two_session_scenarios(draw: st.DrawFn) -> tuple[list[CandleBar], list[bool], list[bool]]:
    """Draw a feed whose bars from ``split`` onward roll to the NEXT session.

    Day-1 bars anchor at 15:10 IST (so some windows also cross the 15:20
    forced boundary); day-2 bars restart at the 09:15 IST session open.
    ``split in {0, n}`` degenerates to a single-session feed.
    """
    n = draw(st.integers(min_value=2, max_value=30))
    split = draw(st.integers(min_value=0, max_value=n))
    # 15:10 IST day-1 — ``_FORCED_OPEN_UTC`` IS 09:40 UTC == 15:10 IST already.
    # (Generator-repair fold, DEC-TRADESIM-OPEN-SESSION-GUARD-001: the original
    # ``+ timedelta(minutes=330)`` landed every day-1 bar at 20:40 IST — ALWAYS
    # a forced bar, so day-1 could never open a position and the day-1 arm of
    # this generator was vacuous. Anchoring at the documented 15:10 IST makes
    # short splits (< 11 bars) end day-1 BEFORE the forced window — exactly the
    # session-tail-hole shape the truncation-equivalence property needs —
    # while longer splits still cross 15:20 as the docstring promises.)
    day1_anchor = _FORCED_OPEN_UTC
    bars: list[CandleBar] = []
    for minute in range(n):
        open_p = draw(_PRICE_PAISA)
        close_p = draw(_PRICE_PAISA)
        if minute < split:
            anchor, offset = day1_anchor, minute
        else:
            anchor, offset = _DAY2_OPEN_UTC, minute - split
        bars.append(
            _bar_at(offset, open_p, close_p, draw(_WICK_PAISA), draw(_WICK_PAISA), anchor=anchor)
        )
    entry = draw(st.lists(st.booleans(), min_size=n, max_size=n))
    exit_ = draw(st.lists(st.booleans(), min_size=n, max_size=n))
    return bars, entry, exit_


@given(
    scenario=_two_session_scenarios(),
    direction=st.sampled_from([Side.BUY, Side.SELL]),
    sl=st.none() | _LEG_PAISA,
    tp=st.none() | _LEG_PAISA,
    policy=_POLICIES,
)
@settings(suppress_health_check=[HealthCheck.too_slow], max_examples=200)
def test_entry_fill_never_crosses_a_session_boundary(
    scenario: tuple[list[CandleBar], list[bool], list[bool]],
    direction: Side,
    sl: int | None,
    tp: int | None,
    policy: FillPolicy,
) -> None:
    """R2-OC-2: every entry fill shares its arming bar's IST session date."""
    bars, entry, exit_ = scenario
    trades = simulate_trades(
        bars,
        entry,
        direction=direction,
        tf_minutes=1,
        stop_loss_paisa=sl,
        take_profit_paisa=tp,
        exit_signals=exit_,
        policy=policy,
    )
    ts_to_idx = {bar.timestamp: i for i, bar in enumerate(bars)}
    for trade in trades:
        k = ts_to_idx[trade.entry_ts]
        # An entry always fills on the bar AFTER its arming bar, so k >= 1 and
        # the arming bar is bars[k - 1]; both must share one IST session date.
        assert k >= 1
        armed_day = bars[k - 1].timestamp.astimezone(IST).date()
        fill_day = bars[k].timestamp.astimezone(IST).date()
        assert armed_day == fill_day


@given(
    scenario=_two_session_scenarios(),
    direction=st.sampled_from([Side.BUY, Side.SELL]),
    sl=st.none() | _LEG_PAISA,
    tp=st.none() | _LEG_PAISA,
    policy=_POLICIES,
)
@settings(suppress_health_check=[HealthCheck.too_slow], max_examples=200)
def test_session_boundary_close_is_truncation_equivalent(
    scenario: tuple[list[CandleBar], list[bool], list[bool]],
    direction: Side,
    sl: int | None,
    tp: int | None,
    policy: FillPolicy,
) -> None:
    """DEC-TRADESIM-OPEN-SESSION-GUARD-001 — suffix independence by construction.

    Day-1's trade list is identical whether the feed CONTINUES into day-2 or
    TRUNCATES at day-1's last stored bar: the (A2) session-boundary guard
    closes an open position at exactly the record ``finish()`` would emit for
    the truncated feed (same fill price / instants / excursions — only the
    ``TradeDetail`` reason differs, and ``ClosedTrade`` carries no reason).
    Pre-fix this FAILED: an open position (or a pending next-open exit) on
    day-1's last stored bar closed against day-2's prices in the full world,
    while the truncated world closed it at day-1's last close.
    """
    bars, entry, exit_ = scenario
    day1 = date(2024, 1, 2)
    n1 = sum(1 for bar in bars if bar.timestamp.astimezone(IST).date() == day1)

    def _run(feed: list[CandleBar], entries: list[bool], exits: list[bool]) -> list[ClosedTrade]:
        return simulate_trades(
            feed,
            entries,
            direction=direction,
            tf_minutes=1,
            stop_loss_paisa=sl,
            take_profit_paisa=tp,
            exit_signals=exits,
            policy=policy,
        )

    full = _run(bars, entry, exit_)
    truncated = _run(bars[:n1], entry[:n1], exit_[:n1])
    day1_closes = [t for t in full if t.exit_ts.astimezone(IST).date() == day1]
    assert day1_closes == truncated
