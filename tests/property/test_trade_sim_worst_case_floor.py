"""Property: the ONE worst-case fill law is a per-trade P&L FLOOR (1m grid).

DEC-FILL-WORST-CASE-ONLY-001 + DEC-FILL-EXEC-GRID-1M-001. Worst-case is the
ONLY fill model — there is no PRECISE run to diff against — so the floor law
binds each simulated trade to an INDEPENDENT open-anchored re-price of its
OWN legs (the retired precise conventions, restated in-test from the trade's
entry/exit bars + exit reason, never imported). Under the dual-feed 1-minute
execution grid the walker is driven at ``tf_minutes=1`` with the SAME stream
passed as both the decision and execution feed (the §6 degenerate fixed
point): a signal on bar ``N`` still fills on bar ``N+1``, fills are 1m-bar
prices, and every exit index is a 1m index — the property's laws are
unchanged in substance:

* **Open-anchored dominance** (the assurance register's monotonicity pin,
  [FILL-WORSTCASE-SIDE-SIGN]): for EVERY trade,
  ``worst.pnl_paisa <= open_anchored_pnl`` — the worst-case law can only
  UNDERSTATE a trade versus the open/at-level pricing, never overstate.
* **Entry side-sign law**: a BUY entry fills at the entry bar's HIGH exactly,
  a SELL entry at its LOW exactly — a high/low swap mutant fails both legs.
  (The B3 terminal carve-out moves only the EXIT fill to the entry bar's
  CLOSE; the ENTRY fill stays the adverse extreme, so this law is total.)
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

The B3 terminal same-bar carve-out (DEC-FILL-EXEC-GRID-1M-001 §3.12) needs NO
exemption from the floor: when the trade's entry bar IS the last stored bar,
the worst-case ``end_of_data`` exit fills at that bar's CLOSE (not the
adverse extreme), so ``pnl = sign*(close - adverse_entry)`` — and since the
adverse entry is weakly worse than the OPEN the reference uses
(``high >= open`` for BUY, ``low <= open`` for SELL), the dominance
``sign*(close - adverse_entry) <= sign*(close - open)`` still holds exactly;
the arm is exercised by the corpus (an entry filling on the final bar) and
audited by the same assertion.

Cross-law parity + end-to-end dominance (the round-1 review fold): the
production annotation law
(:func:`brutex.engine.fill_sensitivity.open_anchored_trade_pnl`, R13) and
this file's restated ``_open_anchored_pnl`` are two DELIBERATELY-separate
copies — the walker-floor assertions above never delegate to production
code. The tail section below binds them anyway: the restated ``_paisa``
snap + the whole reference law are pinned byte-equal to the production law
over the FULL Decimal price domain (non-2dp inputs included —
:class:`~brutex.data.models.CandleBar` does NOT snap to 2dp; the snap is a
lake-WRITE invariant), and the shipped ``haircut_paisa >= 0`` dominance is
pinned END-TO-END against real ``simulate_trades`` output through
:func:`~brutex.engine.fill_sensitivity.fill_sensitivity_verdict` — so
neither the oracle nor the annotation can silently fork.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

import pytest
from brutex.calendars.nse import IST
from brutex.data.models import CandleBar
from brutex.engine.fill_sensitivity import (
    FILL_SENSITIVITY_STATUS_PRICED,
    fill_sensitivity_verdict,
    open_anchored_trade_pnl,
)
from brutex.engine.minute_path import PathStatus
from brutex.engine.trade_capture import CapturedTrade
from brutex.engine.trade_sim import TradeDetail, simulate_trades
from brutex.metrics.types import ClosedTrade
from brutex.money import paisa_to_rupees, rupees_to_paisa
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
    # Dual-feed call shape (DEC-FILL-EXEC-GRID-1M-001): at tf_minutes=1 the
    # SAME stream is both the decision and the 1-minute execution feed.
    simulate_trades(
        bars,
        entry_signals,
        direction=direction,
        tf_minutes=1,
        exec_bars_1m=bars,
        stop_loss_paisa=sl,
        take_profit_paisa=tp,
        detail_sink=sink,
    )
    return sink


def _paisa(value: Decimal) -> int:
    """Rupees → int paisa, restating the production ``ROUND_HALF_UP`` snap.

    Deliberately RESTATED — never imported from :mod:`brutex.money` — so the
    floor oracle stays independent of production code; byte-equality with
    ``rupees_to_paisa`` over the FULL Decimal domain (non-2dp inputs
    included) is pinned by the cross-law parity tests below, so the two
    copies cannot silently fork.
    """
    return int((value * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


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

    The B3 terminal degenerate (entry bar == last bar → the worst-case exit
    fill is that bar's CLOSE, exit_ts = the entry bar's ts) rides the SAME
    close-priced reference: worst = sign*(close - adverse_entry) <=
    sign*(close - open) = this reference, because the adverse entry is weakly
    worse than the open on the side sign — no exemption needed (module
    docstring).
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


# ---------------------------------------------------------------------------
# Cross-law parity + end-to-end dominance (DEC-FILL-EXEC-GRID-1M-001 R13;
# the round-1 review fold). The floor oracle above never imports production
# code; the pins below bind the two deliberately-separate reference-law
# copies byte-equal and pin the shipped haircut's dominance against REAL
# walker output — module docstring, tail section.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Each tie/noise value diverges under the RETIRED int(v*100)
        # truncation (10_000 / 9_999 / 1_402_079 respectively) — the
        # deterministic revert-to-truncation mutant killers.
        ("100.005", 10_001),
        ("99.995", 10_000),
        ("14020.7999", 1_402_080),
    ],
)
def test_paisa_restatement_matches_production_rounding(raw: str, expected: int) -> None:
    """``_paisa`` == the production ``rupees_to_paisa`` on the half-up ties."""
    value = Decimal(raw)
    assert _paisa(value) == rupees_to_paisa(value) == expected


@st.composite
def _law_parity_case(
    draw: st.DrawFn,
) -> tuple[CandleBar, CandleBar, str, Side, int, int]:
    """(entry_bar, exit_bar, exit_reason, direction, sl, tp) — full price domain.

    Prices are drawn on the 1/10_000-rupee grid (up to FOUR decimals — the
    non-2dp domain ``CandleBar`` legally carries; the 2dp snap is a
    lake-WRITE invariant, not a constructor one), so half-up ties like
    ``x.xx5`` appear and a rounding fork between the two law copies fails.
    """

    def bar(ts: datetime) -> CandleBar:
        quantum = Decimal(10_000)
        lo = draw(st.integers(min_value=1, max_value=2_000_000))
        hi = lo + draw(st.integers(min_value=0, max_value=40_000))
        o = draw(st.integers(min_value=lo, max_value=hi))
        c = draw(st.integers(min_value=lo, max_value=hi))
        return CandleBar(
            timestamp=ts,
            open=Decimal(o) / quantum,
            high=Decimal(hi) / quantum,
            low=Decimal(lo) / quantum,
            close=Decimal(c) / quantum,
            volume=0,
            open_interest=None,
        )

    t0 = datetime(2024, 1, 2, 4, 0, tzinfo=UTC)
    entry_bar = bar(t0)
    # The B3 terminal degenerate (exit_ts == entry_ts) is part of the domain.
    same_bar = draw(st.booleans())
    exit_bar = entry_bar if same_bar else bar(t0 + timedelta(minutes=1))
    reason = draw(
        st.sampled_from(
            sorted(_OPEN_ANCHORED_EXITS | _CLOSE_PRICED_EXITS | {"take_profit", "stop_loss"})
        )
    )
    direction = draw(st.sampled_from([Side.BUY, Side.SELL]))
    sl = draw(st.integers(min_value=1, max_value=4_000))
    tp = draw(st.integers(min_value=1, max_value=4_000))
    return entry_bar, exit_bar, reason, direction, sl, tp


@given(case=_law_parity_case())
@settings(max_examples=200, deadline=None)
def test_open_anchored_laws_byte_equal_over_the_full_domain(
    case: tuple[CandleBar, CandleBar, str, Side, int, int],
) -> None:
    """The restated oracle == the production annotation law, every arm.

    Byte-equality of ``_open_anchored_pnl`` (over :class:`TradeDetail`) and
    :func:`brutex.engine.fill_sensitivity.open_anchored_trade_pnl` (over
    :class:`CapturedTrade`) on the SAME legs across all six exit reasons,
    both directions, and non-2dp prices — the cross-verification pin that
    makes a silent fork of the two copies fail loud.
    """
    entry_bar, exit_bar, reason, direction, sl, tp = case
    bars_by_ts = {entry_bar.timestamp: entry_bar, exit_bar.timestamp: exit_bar}
    trade = ClosedTrade(
        pnl_paisa=0,
        mfe_paisa=0,
        mae_paisa=0,
        holding_bars=1,
        entry_ts=entry_bar.timestamp,
        exit_ts=exit_bar.timestamp,
        direction=direction,
    )
    detail = TradeDetail(trade=trade, exit_reason=reason, entry_fill_paisa=0, exit_fill_paisa=0)
    captured = CapturedTrade(
        entry_ts=entry_bar.timestamp,
        entry_px=Decimal("1.00"),
        exit_ts=exit_bar.timestamp,
        exit_px=Decimal("1.00"),
        direction=direction,
        lots=None,
        exit_reason=reason,
        pnl_paisa=0,
        costs_paisa=None,
        mae_paisa=0,
        mfe_paisa=0,
        mae_1m_paisa=None,
        mfe_1m_paisa=None,
        path_1m_status=PathStatus.NO_1M_DATA,
        vix_entry=None,
        vix_exit=None,
    )
    assert _open_anchored_pnl(detail, bars_by_ts, direction, sl, tp) == open_anchored_trade_pnl(
        captured, bars_by_ts, sl_paisa=sl, tp_paisa=tp
    )


def _captured_from_details(details: list[TradeDetail], direction: Side) -> list[CapturedTrade]:
    """:func:`capture_combo`'s ``TradeDetail`` → ``CapturedTrade`` field map.

    The minimal restatement of the production conversion
    (:mod:`brutex.engine.trade_capture` — fills via ``paisa_to_rupees``,
    Ruling-18 ``None`` lots/costs, honest ``NO_1M_DATA`` path status) so the
    dominance pin drives :func:`fill_sensitivity_verdict` with exactly the
    trade shape the results-depth pass ships.
    """
    return [
        CapturedTrade(
            entry_ts=detail.trade.entry_ts,
            entry_px=paisa_to_rupees(detail.entry_fill_paisa),
            exit_ts=detail.trade.exit_ts,
            exit_px=paisa_to_rupees(detail.exit_fill_paisa),
            direction=direction,
            lots=None,
            exit_reason=detail.exit_reason,
            pnl_paisa=detail.trade.pnl_paisa,
            costs_paisa=None,
            mae_paisa=detail.trade.mae_paisa,
            mfe_paisa=detail.trade.mfe_paisa,
            mae_1m_paisa=None,
            mfe_1m_paisa=None,
            path_1m_status=PathStatus.NO_1M_DATA,
            vix_entry=None,
            vix_exit=None,
        )
        for detail in details
    ]


@given(case=_CASES)
@settings(max_examples=300, deadline=None)
def test_fill_sensitivity_haircut_nonnegative_on_real_walker_output(
    case: tuple[list[CandleBar], list[bool], Side, int | None, int | None],
) -> None:
    """END-TO-END dominance: ``simulate_trades`` → ``CapturedTrade`` →
    :func:`fill_sensitivity_verdict` ⇒ ``haircut_paisa >= 0``.

    The shipped annotation's dominance claim bound to REAL walker output over
    the 2dp-snapped corpus (the production lake invariant): the worst-case
    walker can only UNDERSTATE each trade vs the open-anchored reference, so
    the folded haircut can never read negative on genuine trades — the exact
    guarantee the fold itself deliberately never guards.
    """
    bars, entry_signals, direction, sl, tp = case
    details = _run(bars, entry_signals, direction, sl, tp)
    verdict = fill_sensitivity_verdict(
        _captured_from_details(details, direction), bars, sl_paisa=sl, tp_paisa=tp
    )
    assert verdict["status"] == FILL_SENSITIVITY_STATUS_PRICED
    assert verdict["n_trades"] == len(details)
    assert verdict["worst_case_net_pnl_paisa"] == sum(d.trade.pnl_paisa for d in details)
    haircut = verdict["haircut_paisa"]
    assert isinstance(haircut, int)
    assert haircut >= 0, (
        f"NEGATIVE haircut on real walker output: {haircut} "
        f"(reference={verdict['reference_net_pnl_paisa']} "
        f"worst={verdict['worst_case_net_pnl_paisa']})"
    )
