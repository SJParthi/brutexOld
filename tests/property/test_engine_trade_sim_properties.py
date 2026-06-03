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
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise

from brutex.data.models import CandleBar
from brutex.engine.trade_sim import FillPolicy, LevelFill, SameBarTie, simulate_trades
from brutex.metrics.types import ClosedTrade
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


def _bar_at(minute: int, open_p: int, close_p: int, up_wick: int, down_wick: int) -> CandleBar:
    """A valid 1-minute bar whose high/low extend the open/close by the wicks."""
    high_p = max(open_p, close_p) + up_wick
    low_p = min(open_p, close_p) - down_wick
    return CandleBar(
        timestamp=_OPEN_UTC + timedelta(minutes=minute),
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
