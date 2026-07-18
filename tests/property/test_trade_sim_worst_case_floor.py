"""Property: the ONE worst-case fill law is a per-trade P&L FLOOR.

DEC-FILL-WORST-CASE-ONLY-001. Worst-case is the ONLY fill model — there is no
PRECISE run to diff against — so the floor law binds each simulated trade to
an INDEPENDENT open-anchored re-price of its OWN legs (the retired precise
conventions, restated in-test from the trade's entry/exit bars + exit reason,
never imported):

* **Open-anchored dominance** (the assurance register's monotonicity pin,
  [FILL-WORSTCASE-SIDE-SIGN]): for EVERY trade,
  ``worst.pnl_paisa <= open_anchored_pnl`` — the worst-case law can only
  UNDERSTATE a trade versus the open/at-level pricing, never overstate.
* **Entry side-sign law**: a BUY entry fills at the entry bar's HIGH exactly,
  a SELL entry at its LOW exactly — a high/low swap mutant fails both legs.
* **Determinism**: the one law has no hidden mode input — two identical runs
  are bit-identical.

Why per-trade against the trade's OWN bars: the SL/TP LEVELS anchor on the
ACTUAL (worst-case) entry fill, so a whole-run diff against an open-anchored
SIMULATION would compare trades with different trigger timing. The dominance
law needs no simulation: given the worst-case walker's (entry bar, exit bar,
exit reason), the open-anchored counterpart of each leg is a pure function —
entry = entry bar OPEN; take_profit/stop_loss = the level anchored on that
OPEN entry (pnl exactly ``+tp`` / ``-sl``); the open-anchored exit classes
(``signal`` / ``forced_1520``) = the exit bar's OPEN; ``end_of_data`` /
``session_gap`` = the priced bar's CLOSE (the exit_ts lands on that bar in
both laws) — and each worst-case leg is weakly adverse of it by construction.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from brutex.calendars.nse import IST
from brutex.data.models import CandleBar
from brutex.engine.trade_sim import TradeDetail, simulate_trades
from brutex.strategies.types import Side
from hypothesis import given, settings
from hypothesis import strategies as st

pytestmark = pytest.mark.property

_DAY = date(2024, 1, 2)
# 09:15 (long runway) + 15:16 (windows quickly cross the 15:20 forced exit),
# so both the open-anchored forced fills and the SL/TP level fills appear in
# the corpus.
_START_TIMES = [(9, 15), (15, 16)]

#: Exit reasons priced open-anchored at the EXIT bar's open under the retired
#: precise conventions (every non-level, non-terminal exit class).
_OPEN_ANCHORED_EXITS = frozenset({"signal", "forced_1520"})
#: Terminal exits priced at the exit_ts bar's close under the retired law.
_CLOSE_PRICED_EXITS = frozenset({"end_of_data", "session_gap"})


@st.composite
def _feed_and_params(
    draw: st.DrawFn,
) -> tuple[list[CandleBar], list[bool], Side, int | None, int | None]:
    """Generate (bars, entry_signals, direction, sl_paisa, tp_paisa)."""
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
    return bars, entry_signals, direction, sl, tp


def _run(
    bars: list[CandleBar],
    entry_signals: list[bool],
    direction: Side,
    sl: int | None,
    tp: int | None,
) -> list[TradeDetail]:
    sink: list[TradeDetail] = []
    simulate_trades(
        bars,
        entry_signals,
        direction=direction,
        tf_minutes=1,
        stop_loss_paisa=sl,
        take_profit_paisa=tp,
        detail_sink=sink,
    )
    return sink


def _paisa(value: Decimal) -> int:
    return int(value * 100)


def _open_anchored_pnl(
    detail: TradeDetail,
    bars_by_ts: dict[datetime, CandleBar],
    direction: Side,
    sl: int | None,
    tp: int | None,
) -> int:
    """Re-price *detail*'s legs under the retired open-anchored conventions.

    Entry = the entry bar's OPEN; take_profit / stop_loss = the level anchored
    on that OPEN entry (pnl exactly ``+tp`` / ``-sl``); the open-anchored exit
    classes = the exit bar's OPEN; ``end_of_data`` / ``session_gap`` = the
    exit_ts bar's CLOSE. Pure arithmetic over the SAME bars the worst-case
    trade used — no simulation, so trigger timing cannot diverge from the
    trade under audit.
    """
    entry_ref = _paisa(bars_by_ts[detail.trade.entry_ts].open)
    sign = 1 if direction is Side.BUY else -1
    if detail.exit_reason == "take_profit":
        assert tp is not None
        return tp
    if detail.exit_reason == "stop_loss":
        assert sl is not None
        return -sl
    if detail.exit_reason in _CLOSE_PRICED_EXITS:
        return sign * (_paisa(bars_by_ts[detail.trade.exit_ts].close) - entry_ref)
    assert detail.exit_reason in _OPEN_ANCHORED_EXITS, detail.exit_reason
    exit_ref = _paisa(bars_by_ts[detail.trade.exit_ts].open)
    return sign * (exit_ref - entry_ref)


_CASES = _feed_and_params()


@given(case=_CASES)
@settings(max_examples=300, deadline=None)
def test_worst_case_never_overstates_the_open_anchored_price(
    case: tuple[list[CandleBar], list[bool], Side, int | None, int | None],
) -> None:
    """EVERY trade: worst-case pnl <= its own open-anchored re-price."""
    bars, entry_signals, direction, sl, tp = case
    details = _run(bars, entry_signals, direction, sl, tp)
    bars_by_ts = {bar.timestamp: bar for bar in bars}
    for detail in details:
        reference = _open_anchored_pnl(detail, bars_by_ts, direction, sl, tp)
        assert detail.trade.pnl_paisa <= reference, (
            f"worst-case OVERSTATED a trade: reason={detail.exit_reason} "
            f"worst={detail.trade.pnl_paisa} > open_anchored={reference}"
        )


@given(case=_CASES)
@settings(max_examples=300, deadline=None)
def test_entry_fill_is_the_adverse_extreme_side_correct(
    case: tuple[list[CandleBar], list[bool], Side, int | None, int | None],
) -> None:
    """[FILL-WORSTCASE-SIDE-SIGN]: BUY entry == bar HIGH, SELL entry == bar LOW.

    Exact-equality teeth — a high/low swap mutant in the entry fill law fails
    this on the first non-flat entry bar (and the floor property above on the
    P&L side).
    """
    bars, entry_signals, direction, sl, tp = case
    details = _run(bars, entry_signals, direction, sl, tp)
    bars_by_ts = {bar.timestamp: bar for bar in bars}
    for detail in details:
        entry_bar = bars_by_ts[detail.trade.entry_ts]
        expected = entry_bar.high if direction is Side.BUY else entry_bar.low
        assert detail.entry_fill_paisa == _paisa(expected), (
            f"{direction} entry fill {detail.entry_fill_paisa} != adverse extreme "
            f"{_paisa(expected)} (high={entry_bar.high}, low={entry_bar.low})"
        )


@given(case=_CASES)
@settings(max_examples=150, deadline=None)
def test_one_law_is_deterministic(
    case: tuple[list[CandleBar], list[bool], Side, int | None, int | None],
) -> None:
    """Two identical runs are bit-identical — no hidden mode input survives."""
    bars, entry_signals, direction, sl, tp = case
    assert _run(bars, entry_signals, direction, sl, tp) == _run(
        bars, entry_signals, direction, sl, tp
    )
