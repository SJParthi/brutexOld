"""Property invariants for the options executor (DEC-OPTIONS-EXECUTOR-001).

Hypothesis-driven checks of the executor's structural guarantees over random
signals + premiums + rules (mirrors the P-EXEC invariants of
``docs/track2/TRACK2_EXECUTION_SIMULATOR.md`` §8):

* leg selection is on-grid and direction-faithful (BULL→CE, BEAR→PE);
* ``net = gross − total_charges`` (cost stack never double-counts / drops);
* materialization is deterministic (same inputs → identical priced trade);
* aggregation counts are conserved (``accepted + rejected == signals``;
  ``winners + losers == accepted``; ``rejected == no_data + tick_floor``).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from brutex.data.models import OptionBar
from brutex.options.executor import (
    MaterializedTrade,
    OptionBarPair,
    RejectedTrade,
    SpotSignal,
    execute_options_sim,
    materialize_trade,
    run_rule_over_signals,
    select_option_leg,
)
from brutex.options.lot_size_history import OptionsUnderlying
from brutex.options.strike_grid import get_strike_step
from brutex.options.strike_rules import OFFSET_RULES, RULES_BY_NAME, TradeDirection
from hypothesis import given
from hypothesis import strategies as st

_IST = ZoneInfo("Asia/Kolkata")
# Canonical priced-path date: post-2024-10-01 (the verified ETC window —
# DEC-ETC-REGIME-HISTORY-001 made pre-boundary dates reject) and clear of
# the 2024-11-20 NIFTY lot transition.
_ENTRY = datetime(2024, 10, 3, 9, 16, tzinfo=_IST)
_EXIT = datetime(2024, 10, 3, 15, 20, tzinfo=_IST)
_EXPIRY = date(2024, 10, 31)

_instruments = st.sampled_from(list(OptionsUnderlying))
_directions = st.sampled_from(list(TradeDirection))
_offset_rules = st.sampled_from(OFFSET_RULES)
#: Spot rupees kept high enough that ATM ± 10 steps stay strictly positive.
_spots = st.integers(min_value=5_000, max_value=90_000)
#: Entry premium strictly above the ₹0.25 tick floor; exit at/above one tick.
_entry_paisa = st.integers(min_value=30, max_value=500_000)
_exit_paisa = st.integers(min_value=5, max_value=500_000)


def _bar(side: str, paisa: int) -> OptionBar:
    px = Decimal(paisa) / 100
    return OptionBar(
        timestamp=_ENTRY,
        open=px,
        high=px,
        low=px,
        close=px,
        volume=10,
        open_interest=100,
        side=side,
        strike=Decimal("25000"),
        spot=Decimal("25012"),
        iv=Decimal("0.15"),
    )


class _Source:
    def __init__(self, entry_paisa: int, exit_paisa: int) -> None:
        self._e = entry_paisa
        self._x = exit_paisa

    def load_pair(self, *, option_type: str, **_kw: object) -> OptionBarPair:
        return OptionBarPair(
            entry_bar=_bar(option_type, self._e),
            exit_bar=_bar(option_type, self._x),
        )


def _signal(instrument: OptionsUnderlying, direction: TradeDirection, spot: int) -> SpotSignal:
    return SpotSignal(
        instrument=instrument,
        direction=direction,
        entry_ts=_ENTRY,
        exit_ts=_EXIT,
        spot_at_entry=Decimal(spot),
        expiry_date=_EXPIRY,
    )


@given(instrument=_instruments, direction=_directions, spot=_spots, rule=_offset_rules)
def test_leg_is_on_grid_and_direction_faithful(
    instrument: OptionsUnderlying, direction: TradeDirection, spot: int, rule: object
) -> None:
    sig = _signal(instrument, direction, spot)
    leg = select_option_leg(sig, rule)  # type: ignore[arg-type]
    step = get_strike_step(instrument)
    # On the grid, and the offset reconstructs the strike exactly.
    assert leg.strike % step == 0
    assert leg.atm_strike + leg.strike_offset * step == leg.strike
    assert leg.strike > 0
    if direction == TradeDirection.BULL:
        assert leg.option_type == "CE" and leg.side == "long_call"
    else:
        assert leg.option_type == "PE" and leg.side == "long_put"


@given(
    instrument=_instruments,
    direction=_directions,
    spot=_spots,
    rule=_offset_rules,
    entry=_entry_paisa,
    exit_=_exit_paisa,
)
def test_net_equals_gross_minus_charges(
    instrument: OptionsUnderlying,
    direction: TradeDirection,
    spot: int,
    rule: object,
    entry: int,
    exit_: int,
) -> None:
    sig = _signal(instrument, direction, spot)
    out = materialize_trade(sig, rule, source=_Source(entry, exit_))  # type: ignore[arg-type]
    assert isinstance(out, MaterializedTrade)
    b = out.breakdown
    assert b.net_pnl_paisa == b.gross_pnl_paisa - b.total_charges_paisa
    assert out.net_pnl_paisa == b.net_pnl_paisa
    assert out.entry_open_paisa == entry  # 2dp grid: integer paisa survives the snap


@given(
    instrument=_instruments,
    direction=_directions,
    spot=_spots,
    rule=_offset_rules,
    entry=_entry_paisa,
    exit_=_exit_paisa,
)
def test_materialize_is_deterministic(
    instrument: OptionsUnderlying,
    direction: TradeDirection,
    spot: int,
    rule: object,
    entry: int,
    exit_: int,
) -> None:
    sig = _signal(instrument, direction, spot)
    a = materialize_trade(sig, rule, source=_Source(entry, exit_))  # type: ignore[arg-type]
    b = materialize_trade(sig, rule, source=_Source(entry, exit_))  # type: ignore[arg-type]
    assert a == b


@given(
    instrument=_instruments,
    direction=_directions,
    spots=st.lists(_spots, min_size=0, max_size=8),
    entry=_entry_paisa,
    exit_=_exit_paisa,
)
def test_aggregate_counts_are_conserved(
    instrument: OptionsUnderlying,
    direction: TradeDirection,
    spots: list[int],
    entry: int,
    exit_: int,
) -> None:
    signals = [_signal(instrument, direction, s) for s in spots]
    res = run_rule_over_signals(signals, RULES_BY_NAME["atm"], source=_Source(entry, exit_))
    assert res.n_accepted + res.n_rejected == res.n_signals == len(spots)
    assert res.n_winners + res.n_losers == res.n_accepted
    assert res.n_rejected == res.n_rejected_no_data + res.n_rejected_tick_floor
    # This source always returns data above the floor, so all accept.
    assert res.n_accepted == len(spots)
    assert 0.0 <= res.win_rate <= 1.0


@given(spot=_spots, entry=_entry_paisa, exit_=_exit_paisa)
def test_sweep_returns_one_result_per_offset_rule(spot: int, entry: int, exit_: int) -> None:
    sig = _signal(OptionsUnderlying.NIFTY, TradeDirection.BULL, spot)
    results = execute_options_sim([sig], source=_Source(entry, exit_))
    assert len(results) == len(OFFSET_RULES)
    assert {r.rule_name for r in results} == {r.name for r in OFFSET_RULES}
    assert all(not isinstance(r, RejectedTrade) for r in results)
