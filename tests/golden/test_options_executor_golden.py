"""Golden byte-equality oracle for the options executor (DEC-OPTIONS-EXECUTOR-001
+ DEC-ETC-REGIME-HISTORY-001), re-locked for DEC-FILL-WORST-CASE-ONLY-001.

Fully hand-derived fixtures whose every emitted field — selected leg, paisa
conversions, and the full per-charge cost breakdown — is computed by hand below
and locked as a literal. Any change to the executor's composition (strike
selection, rupees→paisa snap, cost-stack wiring, the one worst-case fill law)
that alters output breaks this test. The cost-rate constants themselves are
independently golden-tested in ``tests/golden`` /
``tests/unit/test_costs_calculator.py``; this test pins the *executor's use*
of them.

Entry date 2024-10-03 (RELOCATED from the pre-boundary 2024-05-02 by
DEC-ETC-REGIME-HISTORY-001 — a pre-2024-10-01 date now REJECTS on the
UNVERIFIED ETC refusal window, pinned by the refusal golden below). Date
hygiene: 2024-10-03 is a Thursday, 7 weeks clear of the 2024-11-20 NIFTY
lot transition (lot 25 since 2024-04-26; SENSEX lot 10 until 2024-11-20).
STT regime: 2024-10-01 → 0.10% (10_000 bps×100). 1-tick = 5 paisa.

ONE-FILL-LAW RE-LOCK (DEC-FILL-WORST-CASE-ONLY-001): the retired PRECISE
pins are DELETED with the mode axis. Fixtures A/B keep FLAT (limit-locked)
bars, so their FILLS are invariant (worst-case anchor == open on a flat
bar; the +tick discipline preserves ``open ± tick``); their LEVY stacks are
re-derived under the now-PERMANENT adverse (ceiling) levy rounding — the
DEC-FILL-WORST-CASE-ONLY-001 fix-round-2 law, unconditional once the mode axis
died. Fixture W is the RANGE-bar worst-case golden (numbers UNCHANGED from
its original 2026-07-13 lock — the promotion moved the pins, not the math).

Fixture A — NIFTY BULL ``atm`` (long_call), entry ₹120.00 → exit ₹150.00
(flat bars):
  ATM(25012, step 50) = 25000; CE strike 25000, offset 0.
  buy_fill = 12000 + 5 = 12005;  sell_fill = 15000 − 5 = 14995;  n_shares = 25.
  buy_notional = 300,125;  sell_notional = 374,875;  gross = +74,750.
  brokerage = 2×2000 = 4,000.
  STT = ceil₹(374,875×10,000//1e7 = 374) = 400.
  exch(NSE 3503) = ⌈105.13⌉ₚ + ⌈131.32⌉ₚ = 106 + 132 = 238.
  sebi(10) = ⌈0.30⌉ₚ + ⌈0.37⌉ₚ = 2;  ipft(NSE 50) = ⌈1.50⌉ₚ + ⌈1.87⌉ₚ = 4;
  stamp(300, buy) = ceil₹(9ₚ) = 100.
  GST = ceil₹((4,000+238+2+4)×18% = 763.92) = 800.
  total = 4,000+400+238+2+4+100+800 = 5,544;  net = 74,750 − 5,544 = +69,206.

Fixture B — SENSEX BEAR ``atm_plus_1_in_dir`` (long_put), entry ₹200.00 →
₹50.00 (flat bars):
  ATM(81012, step 100) = 81000; PE "further OTM" = lower strike 80900,
  offset −1.
  buy_fill = 20005;  sell_fill = 4995;  n_shares = 10.
  buy_notional = 200,050;  sell_notional = 49,950;  gross = −150,100.
  brokerage = 4,000;  STT = ceil₹(49,950×10,000//1e7 = 49) = 100.
  exch(BSE 3250) = ⌈65.02⌉ₚ + ⌈16.23⌉ₚ = 66 + 17 = 83.
  sebi = ⌈0.20⌉ₚ + ⌈0.05⌉ₚ = 2;  ipft(BSE 0) = 0;  stamp = ceil₹(6ₚ) = 100.
  GST = ceil₹((4,000+83+2)×18% = 735.3) = 800.
  total = 4,000+100+83+2+0+100+800 = 5,085;  net = −150,100 − 5,085 =
  −155,185.
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
    RejectReason,
    SpotSignal,
    materialize_trade,
)
from brutex.options.lot_size_history import OptionsUnderlying
from brutex.options.strike_rules import RULES_BY_NAME, TradeDirection

_IST = ZoneInfo("Asia/Kolkata")
_ENTRY_TS = datetime(2024, 10, 3, 9, 16, tzinfo=_IST)
_EXIT_TS = datetime(2024, 10, 3, 15, 20, tzinfo=_IST)
_EXPIRY = date(2024, 10, 31)

#: The pre-boundary date the two fixtures used to live on — now the refusal
#: golden's date (inside the UNVERIFIED [history-start → 2024-10-01) window).
_PRE_BOUNDARY_ENTRY_TS = datetime(2024, 5, 2, 9, 16, tzinfo=_IST)
_PRE_BOUNDARY_EXIT_TS = datetime(2024, 5, 2, 15, 20, tzinfo=_IST)
_PRE_BOUNDARY_EXPIRY = date(2024, 5, 30)


def _bar(side: str, open_rupees: str) -> OptionBar:
    px = Decimal(open_rupees)
    return OptionBar(
        timestamp=_ENTRY_TS,
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
    def __init__(self, entry_open: str, exit_open: str) -> None:
        self._entry = entry_open
        self._exit = exit_open

    def load_pair(self, *, option_type: str, **_kw: object) -> OptionBarPair:
        return OptionBarPair(
            entry_bar=_bar(option_type, self._entry),
            exit_bar=_bar(option_type, self._exit),
        )


class _NeverCalledSource:
    """A bar source that MUST NOT be read — proves the cost-regime precheck
    fires BEFORE any lake I/O (zero reads per rejected pre-boundary signal)."""

    def load_pair(self, **_kw: object) -> OptionBarPair | None:
        raise AssertionError("load_pair must not be called for a pre-boundary signal")


def test_golden_nifty_bull_atm() -> None:
    sig = SpotSignal(
        instrument=OptionsUnderlying.NIFTY,
        direction=TradeDirection.BULL,
        entry_ts=_ENTRY_TS,
        exit_ts=_EXIT_TS,
        spot_at_entry=Decimal("25012"),
        expiry_date=_EXPIRY,
    )
    out = materialize_trade(sig, RULES_BY_NAME["atm"], source=_Source("120.00", "150.00"))
    assert isinstance(out, MaterializedTrade)

    assert out.leg.option_type == "CE"
    assert out.leg.side == "long_call"
    assert out.leg.atm_strike == 25000
    assert out.leg.strike == 25000
    assert out.leg.strike_offset == 0
    assert out.entry_open_paisa == 12000
    assert out.exit_open_paisa == 15000
    assert out.iv_at_entry == Decimal("0.15")

    b = out.breakdown
    assert b.n_shares == 25
    assert b.buy_fill_paisa == 12005  # flat bar: HIGH == open → open + tick
    assert b.sell_fill_paisa == 14995  # flat bar: LOW == open → open − tick
    assert b.brokerage_paisa == 4000
    assert b.stt_paisa == 400
    assert b.exch_paisa == 238  # per-leg CEIL (nearest gave 236)
    assert b.sebi_paisa == 2  # per-leg CEIL (nearest gave 0)
    assert b.ipft_paisa == 4
    assert b.stamp_paisa == 100  # CEIL of 9ₚ to the rupee (nearest gave 0)
    assert b.gst_paisa == 800
    assert b.total_charges_paisa == 5440 + 104  # 5,544 (adverse-levy delta +104)
    assert b.gross_pnl_paisa == 74750
    assert b.net_pnl_paisa == 69206
    assert out.net_pnl_paisa == 69206


def test_golden_sensex_bear_atm_plus_1() -> None:
    sig = SpotSignal(
        instrument=OptionsUnderlying.SENSEX,
        direction=TradeDirection.BEAR,
        entry_ts=_ENTRY_TS,
        exit_ts=_EXIT_TS,
        spot_at_entry=Decimal("81012"),
        expiry_date=_EXPIRY,
    )
    out = materialize_trade(
        sig, RULES_BY_NAME["atm_plus_1_in_dir"], source=_Source("200.00", "50.00")
    )
    assert isinstance(out, MaterializedTrade)

    assert out.leg.option_type == "PE"
    assert out.leg.side == "long_put"
    assert out.leg.atm_strike == 81000
    assert out.leg.strike == 80900
    assert out.leg.strike_offset == -1
    assert out.entry_open_paisa == 20000
    assert out.exit_open_paisa == 5000

    b = out.breakdown
    assert b.n_shares == 10
    assert b.buy_fill_paisa == 20005
    assert b.sell_fill_paisa == 4995
    assert b.brokerage_paisa == 4000
    assert b.stt_paisa == 100  # CEIL of 49ₚ to the rupee (nearest gave 0)
    assert b.exch_paisa == 83  # per-leg CEIL (nearest gave 81)
    assert b.sebi_paisa == 2  # per-leg CEIL (nearest gave 0)
    assert b.ipft_paisa == 0
    assert b.stamp_paisa == 100  # CEIL of 6ₚ (nearest gave 0)
    assert b.gst_paisa == 800  # CEIL of 735.3ₚ (nearest gave 700)
    assert b.total_charges_paisa == 5085
    assert b.gross_pnl_paisa == -150100
    assert b.net_pnl_paisa == -155185


def test_golden_boundary_exact_oct_1_materializes() -> None:
    """Entry EXACTLY on 2024-10-01 (the verified ETC window start) returns a
    MaterializedTrade through the FULL executor stack — kills bisect_right→
    bisect_left mutants at the executor level (DEC-ETC-REGIME-HISTORY-001)."""
    boundary_entry = datetime(2024, 10, 1, 9, 16, tzinfo=_IST)
    boundary_exit = datetime(2024, 10, 1, 15, 20, tzinfo=_IST)
    sig = SpotSignal(
        instrument=OptionsUnderlying.NIFTY,
        direction=TradeDirection.BULL,
        entry_ts=boundary_entry,
        exit_ts=boundary_exit,
        spot_at_entry=Decimal("25012"),
        expiry_date=_EXPIRY,
    )
    out = materialize_trade(sig, RULES_BY_NAME["atm"], source=_Source("120.00", "150.00"))
    assert isinstance(out, MaterializedTrade)
    assert out.breakdown.exch_paisa > 0


def test_golden_pre_boundary_unverified_refusal() -> None:
    """DEC-ETC-REGIME-HISTORY-001: the SAME two signals dated 2024-05-02
    (inside the UNVERIFIED pre-2024-10-01 ETC window) are REJECTED with
    ``COST_REGIME_UNVERIFIED`` for NIFTY (NSE) AND SENSEX (BSE) — and the
    bar source is NEVER read (the precheck fires before any lake I/O)."""
    for instrument, direction, spot, rule in (
        (OptionsUnderlying.NIFTY, TradeDirection.BULL, Decimal("25012"), "atm"),
        (OptionsUnderlying.SENSEX, TradeDirection.BEAR, Decimal("81012"), "atm_plus_1_in_dir"),
    ):
        sig = SpotSignal(
            instrument=instrument,
            direction=direction,
            entry_ts=_PRE_BOUNDARY_ENTRY_TS,
            exit_ts=_PRE_BOUNDARY_EXIT_TS,
            spot_at_entry=spot,
            expiry_date=_PRE_BOUNDARY_EXPIRY,
        )
        out = materialize_trade(sig, RULES_BY_NAME[rule], source=_NeverCalledSource())
        assert isinstance(out, RejectedTrade)
        assert out.reason is RejectReason.COST_REGIME_UNVERIFIED


def _range_bar(side: str, open_rupees: str, high_rupees: str, low_rupees: str) -> OptionBar:
    px = Decimal(open_rupees)
    return OptionBar(
        timestamp=_ENTRY_TS,
        open=px,
        high=Decimal(high_rupees),
        low=Decimal(low_rupees),
        close=px,
        volume=10,
        open_interest=100,
        side=side,
        strike=Decimal("25000"),
        spot=Decimal("25012"),
        iv=Decimal("0.15"),
    )


class _RangeSource:
    """Range-bar source: (open, high, low) per leg — the worst-case anchors."""

    def __init__(self, entry: tuple[str, str, str], exit_: tuple[str, str, str]) -> None:
        self._entry = entry
        self._exit = exit_

    def load_pair(self, *, option_type: str, **_kw: object) -> OptionBarPair:
        return OptionBarPair(
            entry_bar=_range_bar(option_type, *self._entry),
            exit_bar=_range_bar(option_type, *self._exit),
        )


def test_golden_worst_case_nifty_bull_atm_range_bars() -> None:
    """Fixture W — the RANGE-bar worst-case golden (locked 2026-07-13 with
    DEC-FILL-WORST-CASE-ONLY-001 + its fix-round-2 adverse-levy-rounding law;
    numbers UNCHANGED by the DEC-FILL-WORST-CASE-ONLY-001 promotion — the
    bare call below ALWAYS ran this law).

    The SAME signal as Fixture A over RANGE bars entry ₹120.00
    [119.00–121.00] → exit ₹150.00 [149.00–151.00]:

      buy_fill = entry HIGH 12100 + 5 = 12105 (one tick OUTSIDE the printed
      range — the honest adverse floor); sell_fill = exit LOW 14900 − 5 =
      14895; n_shares = 25.
      buy_notional = 302,625;  sell_notional = 372,375;  gross = +69,750.
      brokerage = 2×2000 = 4,000 (flat — rounding-free).
      STT = ceil₹(372,375×10,000//1e7 = 372) = 400.
      exch(NSE 3503) = ⌈106.0095⌉ₚ + ⌈130.44⌉ₚ = 107 + 131 = 238.
      sebi(10) = ⌈0.30⌉ₚ + ⌈0.37⌉ₚ = 1 + 1 = 2;
      ipft(NSE 50) = ⌈1.51⌉ₚ + ⌈1.86⌉ₚ = 2 + 2 = 4;
      stamp(300, buy) = ceil₹(9 paisa) = 100.
      GST = ceil₹((4,000+238+2+4)×18% = 763.92 → //1e7 = 763) = 800.
      total = 4,000+400+238+2+4+100+800 = 5,544;  net = 69,750 − 5,544 =
      +64,206.
      slippage = 2 ticks × 25 = 250 (tick discipline — the open→extreme move
      is fill-anchor semantics carried by gross, not slippage).

    vs the flat-bar Fixture A (SAME opens, SAME levy law): gross 74,750 →
    69,750 = the two bars' open→extreme adverse depth (100 + 100 paisa) ×
    25 shares; net 69,206 → 64,206. The range bars can only ever price
    WORSE — pinned by the direct comparison below.
    """
    sig = SpotSignal(
        instrument=OptionsUnderlying.NIFTY,
        direction=TradeDirection.BULL,
        entry_ts=_ENTRY_TS,
        exit_ts=_EXIT_TS,
        spot_at_entry=Decimal("25012"),
        expiry_date=_EXPIRY,
    )
    source = _RangeSource(("120.00", "121.00", "119.00"), ("150.00", "151.00", "149.00"))
    out = materialize_trade(sig, RULES_BY_NAME["atm"], source=source)
    assert isinstance(out, MaterializedTrade)
    assert out.entry_open_paisa == 12000  # the record keeps OPEN semantics
    assert out.exit_open_paisa == 15000
    b = out.breakdown
    assert b.buy_fill_paisa == 12105
    assert b.sell_fill_paisa == 14895
    assert b.buy_notional_paisa == 302_625
    assert b.sell_notional_paisa == 372_375
    assert b.gross_pnl_paisa == 69_750
    assert b.brokerage_paisa == 4000
    assert b.stt_paisa == 400
    assert b.exch_paisa == 238
    assert b.sebi_paisa == 2
    assert b.ipft_paisa == 4
    assert b.stamp_paisa == 100
    assert b.gst_paisa == 800
    assert b.total_charges_paisa == 5544
    assert b.net_pnl_paisa == 64_206
    assert b.slippage_paisa == 250

    # The ranged fills can only ever price WORSE than the flat-bar twin
    # (Fixture A, net +69,206) — the worst-case floor with real teeth.
    flat = materialize_trade(sig, RULES_BY_NAME["atm"], source=_Source("120.00", "150.00"))
    assert isinstance(flat, MaterializedTrade)
    assert out.net_pnl_paisa < flat.net_pnl_paisa


def test_golden_flat_bars_fill_at_open_plus_tick() -> None:
    """THE NAMED FLAT-BAR PIN (options leg): on flat bars the worst-case
    fill is exactly ``open ± tick`` — the +tick ON TOP of the extreme keeps
    the buy at open+5 (never the bare ``high``, which would be one tick
    BETTER); the levy stack is Fixture A's adverse-ceiled lock."""
    sig = SpotSignal(
        instrument=OptionsUnderlying.NIFTY,
        direction=TradeDirection.BULL,
        entry_ts=_ENTRY_TS,
        exit_ts=_EXIT_TS,
        spot_at_entry=Decimal("25012"),
        expiry_date=_EXPIRY,
    )
    out = materialize_trade(sig, RULES_BY_NAME["atm"], source=_Source("120.00", "150.00"))
    assert isinstance(out, MaterializedTrade)
    assert out.breakdown.buy_fill_paisa == 12005  # open + tick, preserved
    assert out.breakdown.sell_fill_paisa == 14995  # open − tick, preserved
    assert out.breakdown.slippage_paisa == 250  # 2 ticks × 25 shares
    assert out.breakdown.total_charges_paisa == 5_544
    assert out.net_pnl_paisa == 69_206
