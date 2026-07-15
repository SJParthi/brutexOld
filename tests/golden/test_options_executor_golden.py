"""Golden byte-equality oracle for the options executor (DEC-OPTIONS-EXECUTOR-001
+ DEC-ETC-REGIME-HISTORY-001).

Two fully hand-derived fixtures whose every emitted field — selected leg, paisa
conversions, and the full per-charge cost breakdown — is computed by hand below
and locked as a literal. Any change to the executor's composition (strike
selection, rupees→paisa snap, cost-stack wiring) that alters output breaks this
test. The cost-rate constants themselves are independently golden-tested in
``tests/golden`` / ``tests/unit/test_costs_calculator.py``; this test pins the
*executor's use* of them.

Entry date 2024-10-03 (RELOCATED from the pre-boundary 2024-05-02 by
DEC-ETC-REGIME-HISTORY-001 — a pre-2024-10-01 date now REJECTS on the
UNVERIFIED ETC refusal window, pinned by the refusal golden below). Date
hygiene: 2024-10-03 is a Thursday, 7 weeks clear of the 2024-11-20 NIFTY
lot transition (lot 25 since 2024-04-26; SENSEX lot 10 until 2024-11-20).
STT regime: 2024-10-01 → 0.10% (10_000 bps×100). 1-tick = 5 paisa.

Fixture A — NIFTY BULL ``atm`` (long_call), entry ₹120.00 → exit ₹150.00:
  ATM(25012, step 50) = 25000; CE strike 25000, offset 0.
  buy_fill = 12000 + 5 = 12005;  sell_fill = 15000 − 5 = 14995;  n_shares = 25.
  buy_notional = 300125;  sell_notional = 374875;  gross = +74750.
  brokerage = 2×2000 = 4000.
  STT = round₹(374875×10000//1e7 = 374) = 400.
  exch(NSE 3503) = ⌈300125×3503⌉ₚ + ⌈374875×3503⌉ₚ = 105 + 131 = 236.
  sebi(10) = 0 + 0 = 0;  ipft(NSE 50) = 2 + 2 = 4;  stamp(300, buy) = round₹(9) = 0.
  GST = round₹((4000+236+0+4)×18% = 763) = 800.
  total = 4000+400+236+0+4+0+800 = 5440;  net = 74750 − 5440 = +69310.

Fixture B — SENSEX BEAR ``atm_plus_1_in_dir`` (long_put), entry ₹200.00 → ₹50.00:
  ATM(81012, step 100) = 81000; PE "further OTM" = lower strike 80900, offset −1.
  buy_fill = 20005;  sell_fill = 4995;  n_shares = 10.
  buy_notional = 200050;  sell_notional = 49950;  gross = −150100.
  brokerage = 4000;  STT = round₹(49950×10000//1e7 = 49) = 0.
  exch(BSE 3250) = ⌈200050×3250⌉ₚ + ⌈49950×3250⌉ₚ = 65 + 16 = 81.
  sebi = 0;  ipft(BSE 0) = 0;  stamp = round₹(6) = 0.
  GST = round₹((4000+81)×18% = 734) = 700.
  total = 4000+0+81+0+0+0+700 = 4781;  net = −150100 − 4781 = −154881.
  (Every cell byte-identical to the pre-relocation 2024-05-02 fixture —
  STT rounds to 0 in both regimes; only the dates moved.)

DEC-FILL-WORST-CASE-001: Fixtures A/B use FLAT (limit-locked) bars, so their
numbers are provably MODE-INVARIANT (worst-case == precise on a flat bar —
the +tick discipline); they are additionally pinned with an EXPLICIT
``fill_mode=FillMode.PRECISE`` under the new WORST_CASE executor default and
must NEVER be regenerated. Fixture W below is the RANGE-bar worst-case
golden (hand-derived in its own docstring, locked once 2026-07-13).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from brutex.data.models import OptionBar
from brutex.execution.fill_mode import FillMode
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
    out = materialize_trade(
        sig,
        RULES_BY_NAME["atm"],
        source=_Source("120.00", "150.00"),
        fill_mode=FillMode.PRECISE,  # legacy-mode pin — never regenerate
    )
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
    assert b.buy_fill_paisa == 12005
    assert b.sell_fill_paisa == 14995
    assert b.brokerage_paisa == 4000
    assert b.stt_paisa == 400
    assert b.exch_paisa == 236
    assert b.sebi_paisa == 0
    assert b.ipft_paisa == 4
    assert b.stamp_paisa == 0
    assert b.gst_paisa == 800
    assert b.total_charges_paisa == 5440
    assert b.gross_pnl_paisa == 74750
    assert b.net_pnl_paisa == 69310
    assert out.net_pnl_paisa == 69310


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
        sig,
        RULES_BY_NAME["atm_plus_1_in_dir"],
        source=_Source("200.00", "50.00"),
        fill_mode=FillMode.PRECISE,  # legacy-mode pin — never regenerate
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
    assert b.stt_paisa == 0
    assert b.exch_paisa == 81
    assert b.sebi_paisa == 0
    assert b.ipft_paisa == 0
    assert b.stamp_paisa == 0
    assert b.gst_paisa == 700
    assert b.total_charges_paisa == 4781
    assert b.gross_pnl_paisa == -150100
    assert b.net_pnl_paisa == -154881


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
    """Fixture W — DEC-FILL-WORST-CASE-001 worst-case golden (locked once
    2026-07-13, hand-derived; RE-LOCKED ONCE the same day by the fix-round-2
    adverse-levy-rounding law — a BRANCH-NEW pin, the sanctioned single
    relock: under WORST_CASE every nearest-rounded levy now rounds
    ADVERSELY (ceiling), so exch/sebi/stamp moved 236/0/0 → 238/2/100 and
    net 64,310 → 64,206; the frozen origin/main-era goldens are untouched.
    The SAME signal as Fixture A over RANGE bars entry ₹120.00
    [119.00–121.00] → exit ₹150.00 [149.00–151.00]):

      buy_fill = entry HIGH 12100 + 5 = 12105 (one tick OUTSIDE the printed
      range — the honest adverse floor); sell_fill = exit LOW 14900 − 5 =
      14895; n_shares = 25.
      buy_notional = 302,625;  sell_notional = 372,375;  gross = +69,750.
      brokerage = 2×2000 = 4,000 (flat — rounding-free, mode-invariant).
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
    PRECISE on the SAME range bars anchors the opens AND keeps the legacy
    nearest levy rounding ⇒ byte-identical to Fixture A (net +69,310) —
    both pinned so the two arms can never collapse.
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
    # The bare call runs the executor's production WORST_CASE default.
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

    precise = materialize_trade(
        sig, RULES_BY_NAME["atm"], source=source, fill_mode=FillMode.PRECISE
    )
    assert isinstance(precise, MaterializedTrade)
    assert precise.breakdown.net_pnl_paisa == 69_310  # == Fixture A
    assert out.net_pnl_paisa < precise.net_pnl_paisa  # worst never better


def test_golden_flat_bars_worst_case_fills_equal_precise() -> None:
    """THE NAMED FLAT-BAR REGRESSION (options leg): on flat bars the two
    modes FILL identically — the +tick on top of the extreme keeps the
    worst-case buy at ``open + tick`` (== precise), never the bare ``high``
    (which would be one tick BETTER than today) — so gross P&L is equal.

    FIX-ROUND-2 REFRAME (the adverse-levy-rounding law, MIGRATION.md
    #1798): the original ``worst == precise`` byte-identity claim is DEAD
    for the cost-bearing options leg — at EQUAL notionals the worst-case
    levies are CEIL'd where precise rounds nearest, so the charge stack can
    only be HIGHER under worst (here: exch 238 vs 236, sebi 2 vs 0, stamp
    100 vs 0 → charges 5,544 vs 5,440; net 69,206 vs 69,310). The floor
    (never-better) is exactly what survives — pinned with the divergent
    charge values so the ceil arm has teeth on flat bars too.
    """
    sig = SpotSignal(
        instrument=OptionsUnderlying.NIFTY,
        direction=TradeDirection.BULL,
        entry_ts=_ENTRY_TS,
        exit_ts=_EXIT_TS,
        spot_at_entry=Decimal("25012"),
        expiry_date=_EXPIRY,
    )
    worst = materialize_trade(sig, RULES_BY_NAME["atm"], source=_Source("120.00", "150.00"))
    precise = materialize_trade(
        sig,
        RULES_BY_NAME["atm"],
        source=_Source("120.00", "150.00"),
        fill_mode=FillMode.PRECISE,
    )
    assert isinstance(worst, MaterializedTrade)
    assert isinstance(precise, MaterializedTrade)
    assert worst.breakdown.buy_fill_paisa == 12005  # open + tick, preserved
    assert worst.breakdown.buy_fill_paisa == precise.breakdown.buy_fill_paisa
    assert worst.breakdown.sell_fill_paisa == precise.breakdown.sell_fill_paisa
    assert worst.breakdown.gross_pnl_paisa == precise.breakdown.gross_pnl_paisa
    # The adverse levy rounding: worst charges HIGHER at equal notionals.
    assert worst.breakdown.total_charges_paisa == 5_544
    assert precise.breakdown.total_charges_paisa == 5_440
    assert worst.net_pnl_paisa == 69_206
    assert precise.net_pnl_paisa == 69_310
    assert worst.net_pnl_paisa <= precise.net_pnl_paisa
