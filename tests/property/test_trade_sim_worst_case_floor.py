"""Property: the WORST_CASE fill mode is a per-trade P&L FLOOR of PRECISE.

DEC-FILL-WORST-CASE-001. Over hypothesis-generated bar feeds, directions and
SL/TP configurations, every trade the two modes AGREE on must be weakly worse
under WORST_CASE — the adverse-max law can only understate, never overstate:

* **Matched-trade P&L floor** — for trades matched on
  ``(entry_ts, exit_ts, exit_reason)``, ``worst.pnl_paisa <= precise.pnl_paisa``.
* **Entry-fill dominance** — for trades matched on ``entry_ts`` alone, the
  worst-case entry fill is weakly adverse: ``worst_entry >= precise_entry``
  for BUY (paying up to the bar HIGH) and ``worst_entry <= precise_entry``
  for SELL (selling down to the bar LOW).

Why MATCHED-TRADE-scoped and not a whole-run aggregate law: the SL/TP LEVELS
anchor on the ACTUAL entry fill (the mandate's re-anchoring rule), so a
worst-case entry at the bar HIGH moves a long's SL/TP levels UP relative to
the precise run's — the trigger comparisons themselves then run against
different levels, and a level that fires on bar B under one mode can fire on
a different bar (or not at all) under the other. Trade COUNT, exit TIMING and
exit REASON may therefore legitimately diverge between modes; only a trade
whose (entry instant, exit instant, exit reason) coincide in both runs is a
like-for-like pair the floor law can bind. Sum-of-P&L across a whole run is
NOT monotone under re-anchoring (a worst-case run can skip a precise run's
large loser entirely), which is exactly why the property matches trades
first. The entry-fill dominance leg needs only the entry instant to match —
entry TIMING is mode-independent (signal on bar N -> fill on bar N+1 in both
modes), so every shared entry_ts is comparable on its fill price.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from brutex.calendars.nse import IST
from brutex.data.models import CandleBar
from brutex.engine.trade_sim import (
    FillPolicy,
    LevelFill,
    SameBarTie,
    TradeDetail,
    simulate_trades,
)
from brutex.execution.fill_mode import FillMode
from brutex.strategies.types import Side
from hypothesis import given, settings
from hypothesis import strategies as st

pytestmark = pytest.mark.property

_DAY = date(2024, 1, 2)
# 09:15 (long runway) + 15:16 (windows quickly cross the 15:20 forced exit),
# so both the pending/forced open-anchored fills and the SL/TP level fills
# appear in the corpus.
_START_TIMES = [(9, 15), (15, 16)]


@st.composite
def _feed_and_params(
    draw: st.DrawFn,
) -> tuple[list[CandleBar], list[bool], Side, int | None, int | None, FillPolicy]:
    """Generate (bars, entry_signals, direction, sl_paisa, tp_paisa, base policy).

    The returned policy carries ``FillMode.PRECISE``; the test derives its
    WORST_CASE twin via ``dataclasses.replace`` so every OTHER knob (level
    fills, same-bar tie) is byte-identical between the two runs.
    """
    n = draw(st.integers(min_value=2, max_value=30))
    start_h, start_m = draw(st.sampled_from(_START_TIMES))
    bars: list[CandleBar] = []
    for i in range(n):
        low = draw(st.integers(min_value=9000, max_value=11000))
        high = low + draw(st.integers(min_value=0, max_value=2000))
        o = draw(st.integers(min_value=low, max_value=high))
        c = draw(st.integers(min_value=low, max_value=high))
        minute_total = start_h * 60 + start_m + i
        hh, mm = divmod(minute_total, 60)
        ts = datetime(_DAY.year, _DAY.month, _DAY.day, hh % 24, mm, tzinfo=IST).astimezone(UTC)
        bars.append(
            CandleBar(
                timestamp=ts,
                open=Decimal(o) / Decimal(100),
                high=Decimal(high) / Decimal(100),
                low=Decimal(low) / Decimal(100),
                close=Decimal(c) / Decimal(100),
                volume=0,
                open_interest=None,
            )
        )
    entry_signals = draw(st.lists(st.booleans(), min_size=n, max_size=n))
    direction = draw(st.sampled_from([Side.BUY, Side.SELL]))
    sl = draw(st.none() | st.integers(min_value=1, max_value=4000))
    tp = draw(st.none() | st.integers(min_value=1, max_value=4000))
    policy = FillPolicy(
        stop_loss=draw(st.sampled_from([LevelFill.AT_LEVEL, LevelFill.NEXT_OPEN])),
        take_profit=draw(st.sampled_from([LevelFill.AT_LEVEL, LevelFill.NEXT_OPEN])),
        same_bar=draw(st.sampled_from([SameBarTie.SL_FIRST, SameBarTie.TP_FIRST])),
        fill_mode=FillMode.PRECISE,
    )
    return bars, entry_signals, direction, sl, tp, policy


def _run(
    bars: list[CandleBar],
    entry_signals: list[bool],
    direction: Side,
    sl: int | None,
    tp: int | None,
    policy: FillPolicy,
) -> list[TradeDetail]:
    sink: list[TradeDetail] = []
    simulate_trades(
        bars,
        entry_signals,
        direction=direction,
        tf_minutes=1,
        stop_loss_paisa=sl,
        take_profit_paisa=tp,
        policy=policy,
        detail_sink=sink,
    )
    return sink


@given(case=_feed_and_params())
@settings(max_examples=300, deadline=None)
def test_worst_case_is_a_matched_trade_pnl_floor(
    case: tuple[list[CandleBar], list[bool], Side, int | None, int | None, FillPolicy],
) -> None:
    """Matched (entry_ts, exit_ts, exit_reason) trades: worst pnl <= precise pnl."""
    import dataclasses

    bars, entry_signals, direction, sl, tp, precise_policy = case
    worst_policy = dataclasses.replace(precise_policy, fill_mode=FillMode.WORST_CASE)
    precise = _run(bars, entry_signals, direction, sl, tp, precise_policy)
    worst = _run(bars, entry_signals, direction, sl, tp, worst_policy)

    # ── Matched-trade P&L floor ──────────────────────────────────────────
    # Entry timing is mode-independent, so at most one trade per entry_ts in
    # each run — key by (entry_ts, exit_ts, exit_reason) for the exact pair.
    precise_by_key = {(d.trade.entry_ts, d.trade.exit_ts, d.exit_reason): d for d in precise}
    for d_worst in worst:
        key = (d_worst.trade.entry_ts, d_worst.trade.exit_ts, d_worst.exit_reason)
        d_precise = precise_by_key.get(key)
        if d_precise is None:
            continue  # re-anchored levels legitimately diverged the exit
        assert d_worst.trade.pnl_paisa <= d_precise.trade.pnl_paisa, (
            f"worst-case OVERSTATED a matched trade: {key} worst="
            f"{d_worst.trade.pnl_paisa} > precise={d_precise.trade.pnl_paisa}"
        )

    # ── Entry-fill dominance (matched on entry_ts alone) ────────────────
    precise_entry_by_ts = {d.trade.entry_ts: d.entry_fill_paisa for d in precise}
    for d_worst in worst:
        precise_entry = precise_entry_by_ts.get(d_worst.trade.entry_ts)
        if precise_entry is None:
            continue
        if direction is Side.BUY:
            assert d_worst.entry_fill_paisa >= precise_entry, (
                "worst-case BUY entry filled BELOW the precise entry: "
                f"{d_worst.entry_fill_paisa} < {precise_entry}"
            )
        else:
            assert d_worst.entry_fill_paisa <= precise_entry, (
                "worst-case SELL entry filled ABOVE the precise entry: "
                f"{d_worst.entry_fill_paisa} > {precise_entry}"
            )


@given(case=_feed_and_params())
@settings(max_examples=150, deadline=None)
def test_precise_mode_matches_default_policy_object(
    case: tuple[list[CandleBar], list[bool], Side, int | None, int | None, FillPolicy],
) -> None:
    """An explicit PRECISE fill_mode is byte-identical to the legacy library run.

    The library default (``FillPolicy.fill_mode = PRECISE``) IS the legacy
    behaviour; asserting an explicitly-constructed PRECISE policy equals a
    policy that never names fill_mode pins the no-behaviour-drift law for
    every pre-DEC-FILL-WORST-CASE-001 caller.
    """
    bars, entry_signals, direction, sl, tp, precise_policy = case
    legacy_policy = FillPolicy(
        stop_loss=precise_policy.stop_loss,
        take_profit=precise_policy.take_profit,
        same_bar=precise_policy.same_bar,
    )
    explicit = _run(bars, entry_signals, direction, sl, tp, precise_policy)
    legacy = _run(bars, entry_signals, direction, sl, tp, legacy_policy)
    assert explicit == legacy
