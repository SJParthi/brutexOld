"""Promoted P-10 / P-12 cost-model property invariants (DEC-TEST-APPARATUS-DEBTS-001(h)).

These were ``pytest.skip("Phase 2 implementation pending …")`` stubs in
``test_invariants_catalog.py``. The subject — the Track 2 per-trade cost
calculator ``brutex.costs.calculator.compute_trade_cost`` — has long since
landed, so this file turns the stubs into LIVE Hypothesis properties driving
the real calculator (the DEC-PHASE2-INVARIANTS-PROMOTE-001 promotion pattern).
The catalog rows now point here.

Promoted invariants
-------------------
* **P-10** — STT is applied on the SELL leg only, three ways:

  (a) ``stt_paisa`` is INVARIANT in ``entry_bar_open_paisa`` (the buy-leg
      bar price): varying only the buy leg never moves STT.
  (b) independent recomputation: ``stt_paisa`` equals a TEST-SIDE
      half-up-to-rupee rounding of ``(sell_notional_paisa ×
      stt_options_rate_bps_x100(entry_date)) // 10_000_000`` — the oracle
      rounder is built here from ``decimal`` (never the calculator's private
      ``_round_to_rupee``), so the two implementations are independent.
  (c) the buy-leg mirror: ``stamp_paisa`` (stamp duty is buy-leg-only per
      CALCULATOR_SPEC §4 Step 9) is INVARIANT in ``exit_bar_open_paisa``
      (the sell-leg bar price) — cementing "only" in BOTH directions.

* **P-12** — brokerage is the VERIFIED flat law: ₹20 per executed order,
  ₹40 (4_000 paise) per round trip, for EVERY trade — constant in notional,
  ``n_lots``, instrument, broker and date (``docs/track2/CALCULATOR_SPEC.md``
  §4.4, ``docs/track2/COSTS_VERIFIED.md`` §8.1-8.2). The catalog/pyramid's
  old ``min(₹20, 0.03% notional)`` wording was the stale equity-delivery
  formula — asserting it here would fail immediately and CORRECTLY; the
  corrected docs + this property land together so neither lies. A pairwise
  shape check (``notional_a ≤ notional_b ⇒ brokerage_a ≤ brokerage_b``)
  preserves the invariant ID's original "monotone non-decreasing until
  floor, then flat" SHAPE, of which the flat law is the degenerate case.

Strategy-bug guardrails (so a future red is diagnosable)
--------------------------------------------------------
* Sides are RESTRICTED to ``long_call`` / ``long_put`` (Track 2's BUY-only
  DEC-EXEC-008 envelope). The ``short_*`` forward-compat stubs SWAP the
  leg→bar mapping (``calculator._apply_slippage``: short's SELL leg reads
  the ENTRY bar), so a naive vary-the-entry-bar check would false-fail
  P-10(a) on a short side — that would be a strategy bug, not an STT leak.
* ``trade_outcome`` is FIXED at ``"NORMAL_CLOSE"`` — every other outcome
  raises ``UnsupportedTradeOutcomeError`` by design (calculator Step 0).
* Entry dates for the P-10/P-12 value properties are BOUNDED to the
  honestly-PRICEABLE window ``[ETC_REGIME_OCT_2024_START → 2026-12-31]``:
  per DEC-ETC-REGIME-HISTORY-001 (MIGRATION.md #1524) the full
  ``compute_trade_cost`` REFUSES any options trade dated before
  2024-10-01 (the exchange-transaction-charge regime tables carry
  rate-less refusal rows there — ``UnverifiedCostRegimeError``), so the
  priceable window spans the TWO reachable STT regimes
  (2024-10-01..2026-03-31 / ≥2026-04-01); the pre-2024-10-01 STT regime
  constant is unreachable through the full calculator by design. The
  pre-boundary window (lot-size history start 2021-01-01 →
  2024-09-30) is NOT dropped — a dedicated refusal property pins that
  every trade there raises exactly ``UnverifiedCostRegimeError``. An
  out-of-range-date raise elsewhere would be a strategy bug, not a
  finding (``brutex.options.lot_size_history`` starts 2021-01-01;
  ``get_lot_size`` raises before it).
* Bar opens are ≥ 5 paise (1 tick — the ``TradeInput.__post_init__``
  floor) and ``exit_dt ≥ entry_dt`` by construction (same-day 15:20 exit).
* Every property carries an explicit ``deadline=None`` so the derandomized
  ``ci`` hypothesis profile (DEC-TEST-APPARATUS-DEBTS-001(b)) can never
  deadline-flake the calculator + lot-history lookup on a slow runner.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

import pytest
from brutex.costs import constants as cost_constants
from brutex.costs.calculator import compute_trade_cost
from brutex.costs.types import (
    Broker,
    Instrument,
    OptionType,
    TradeInput,
    TradeSide,
    UnverifiedCostRegimeError,
)
from hypothesis import given, settings
from hypothesis import strategies as st

# ────────────────────────────────────────────────────────────────────
# Input strategy — VALID TradeInputs only (types.py __post_init__)
# ────────────────────────────────────────────────────────────────────

_INSTRUMENTS: Final[tuple[Instrument, ...]] = ("NIFTY", "BANKNIFTY", "SENSEX")
_BROKERS: Final[tuple[Broker, ...]] = ("groww", "zerodha")
_OPTION_TYPES: Final[tuple[OptionType, ...]] = ("CE", "PE")
#: Track 2 BUY-only envelope (DEC-EXEC-008). short_* deliberately excluded —
#: see the strategy-bug guardrails in the module docstring.
_LONG_SIDES: Final[tuple[TradeSide, ...]] = ("long_call", "long_put")

#: Lot-size history recorded floor (lot_size_history._LOT_HISTORY_*[0]).
_LOT_HISTORY_START: Final[date] = date(2021, 1, 1)
#: The ETC verified-rate boundary (DEC-ETC-REGIME-HISTORY-001, MIGRATION.md
#: #1524): ``compute_trade_cost`` REFUSES any options trade dated before it
#: (``UnverifiedCostRegimeError`` from the rate-less ETC refusal rows), so
#: the value properties draw entry dates from here onward and the refusal
#: property pins the window before it.
_ETC_VERIFIED_START: Final[date] = cost_constants.ETC_REGIME_OCT_2024_START
#: Far enough to exercise the post-2026-04-01 Finance Act 2026 STT regime.
_DATE_MAX: Final[date] = date(2026, 12, 31)

#: Bar-open premium range: 1 tick (5 paise) .. ₹20,000 (deep-ITM premium).
_open_paisa = st.integers(min_value=5, max_value=2_000_000)
_n_lots = st.integers(min_value=1, max_value=500)
#: PRICEABLE-window entry dates for the P-10/P-12 value properties.
_entry_dates = st.dates(min_value=_ETC_VERIFIED_START, max_value=_DATE_MAX)
#: PRE-boundary entry dates (lot-history floor → the day before the ETC
#: boundary) — the honest-REFUSAL window pinned by the dedicated property.
_refusal_entry_dates = st.dates(
    min_value=_LOT_HISTORY_START,
    max_value=_ETC_VERIFIED_START - timedelta(days=1),
)


@st.composite
def trade_inputs(
    draw: st.DrawFn,
    entry_dates: st.SearchStrategy[date] = _entry_dates,
) -> TradeInput:
    """Draw a VALID Track 2 ``TradeInput`` (long-only, NORMAL_CLOSE)."""
    entry_date = draw(entry_dates)
    return TradeInput(
        entry_dt_ist=datetime.combine(entry_date, time(10, 0)),
        # Track 2 forced 15:20 IST same-day exit (DEC-RISK-011) — always
        # satisfies the exit_dt >= entry_dt validator.
        exit_dt_ist=datetime.combine(entry_date, time(15, 20)),
        instrument=draw(st.sampled_from(_INSTRUMENTS)),
        option_type=draw(st.sampled_from(_OPTION_TYPES)),
        strike=draw(st.integers(min_value=1, max_value=20_000_000)),
        expiry_date=entry_date + timedelta(days=draw(st.integers(min_value=0, max_value=35))),
        entry_bar_open_paisa=draw(_open_paisa),
        exit_bar_open_paisa=draw(_open_paisa),
        side=draw(st.sampled_from(_LONG_SIDES)),
        n_lots=draw(_n_lots),
        broker=draw(st.sampled_from(_BROKERS)),
        trade_outcome="NORMAL_CLOSE",
    )


# ────────────────────────────────────────────────────────────────────
# Test-side oracle rounder (INDEPENDENT of calculator._round_to_rupee)
# ────────────────────────────────────────────────────────────────────


def _half_up_rupee(paisa: int) -> int:
    """Round non-negative paise to the nearest whole rupee, half-up.

    Built from :mod:`decimal` (``ROUND_HALF_UP``) so it shares NO code with
    the calculator's private integer-arithmetic ``_round_to_rupee`` — an
    independent oracle per the promotion spec. Returns paise (a multiple
    of 100). Only non-negative inputs occur here (premium notionals).
    """
    rupees = (Decimal(paisa) / Decimal(100)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(rupees) * 100


# ────────────────────────────────────────────────────────────────────
# P-10 — STT applied only on the SELL leg
# ────────────────────────────────────────────────────────────────────


@given(trade=trade_inputs(), other_entry_open=_open_paisa)
@settings(max_examples=200, deadline=None)
def test_p_10a_stt_invariant_in_buy_leg_price(trade: TradeInput, other_entry_open: int) -> None:
    """P-10(a): varying ONLY ``entry_bar_open_paisa`` (the buy-leg bar for a
    long side) never changes ``stt_paisa`` — STT reads the sell leg only."""
    base = compute_trade_cost(trade)
    varied = compute_trade_cost(replace(trade, entry_bar_open_paisa=other_entry_open))
    assert varied.stt_paisa == base.stt_paisa


@given(trade=trade_inputs())
@settings(max_examples=200, deadline=None)
def test_p_10b_stt_independent_recomputation(trade: TradeInput) -> None:
    """P-10(b): ``stt_paisa`` equals an INDEPENDENT test-side recomputation
    from the sell notional alone — the regime rate for ``entry_dt_ist.date()``
    (DEC-COST-002/003), floor-scaled by the rate encoding, then rounded
    half-up to the rupee by the test's own :func:`_half_up_rupee` oracle."""
    breakdown = compute_trade_cost(trade)
    rate_bps_x100 = cost_constants.stt_options_rate_bps_x100(trade.entry_dt_ist.date())
    expected = _half_up_rupee((breakdown.sell_notional_paisa * rate_bps_x100) // 10_000_000)
    assert breakdown.stt_paisa == expected


@given(trade=trade_inputs(), other_exit_open=_open_paisa)
@settings(max_examples=200, deadline=None)
def test_p_10c_stamp_invariant_in_sell_leg_price(trade: TradeInput, other_exit_open: int) -> None:
    """P-10(c): the buy-leg mirror — ``stamp_paisa`` (stamp duty is
    buy-leg-only, calculator Step 9) never moves when ONLY
    ``exit_bar_open_paisa`` (the sell-leg bar for a long side) varies —
    cementing the "only" of P-10 in both directions."""
    base = compute_trade_cost(trade)
    varied = compute_trade_cost(replace(trade, exit_bar_open_paisa=other_exit_open))
    assert varied.stamp_paisa == base.stamp_paisa


# ────────────────────────────────────────────────────────────────────
# P-12 — brokerage: the VERIFIED flat ₹20/order (₹40 round-trip) law
# ────────────────────────────────────────────────────────────────────


@given(trade=trade_inputs())
@settings(max_examples=200, deadline=None)
def test_p_12_brokerage_flat_40_rupees_round_trip(trade: TradeInput) -> None:
    """P-12 (flat law): every round-trip trade pays EXACTLY
    ``2 × brokerage_paisa(broker)`` = 4_000 paise (₹40) — constant in
    notional, n_lots, instrument, broker and date (CALCULATOR_SPEC §4.4;
    COSTS_VERIFIED §8.1-8.2 — Groww and Zerodha both flat ₹20/order)."""
    breakdown = compute_trade_cost(trade)
    assert breakdown.brokerage_paisa == 2 * cost_constants.brokerage_paisa(trade.broker)
    assert breakdown.brokerage_paisa == 4_000


@given(
    trade=trade_inputs(),
    entry_open_b=_open_paisa,
    exit_open_b=_open_paisa,
    n_lots_b=_n_lots,
)
@settings(max_examples=200, deadline=None)
def test_p_12_brokerage_pairwise_monotone_in_notional(
    trade: TradeInput,
    entry_open_b: int,
    exit_open_b: int,
    n_lots_b: int,
) -> None:
    """P-12 (pairwise shape): for two trades differing only in premium/lots,
    ``notional_a ≤ notional_b ⇒ brokerage_a ≤ brokerage_b`` — the invariant
    ID's original "monotone non-decreasing until floor, then flat" shape,
    of which the verified flat law is the degenerate (trivially-equal) case."""
    trade_b = replace(
        trade,
        entry_bar_open_paisa=entry_open_b,
        exit_bar_open_paisa=exit_open_b,
        n_lots=n_lots_b,
    )
    breakdown_a = compute_trade_cost(trade)
    breakdown_b = compute_trade_cost(trade_b)
    lo, hi = sorted(
        (breakdown_a, breakdown_b),
        key=lambda b: b.buy_notional_paisa + b.sell_notional_paisa,
    )
    assert lo.brokerage_paisa <= hi.brokerage_paisa


# ────────────────────────────────────────────────────────────────────
# Pre-ETC-boundary window — the honest REFUSAL is itself an invariant
# ────────────────────────────────────────────────────────────────────


@given(trade=trade_inputs(entry_dates=_refusal_entry_dates))
@settings(max_examples=200, deadline=None)
def test_pre_etc_boundary_trade_refuses_loudly(trade: TradeInput) -> None:
    """DEC-ETC-REGIME-HISTORY-001 (MIGRATION.md #1524): EVERY options trade
    dated in ``[2021-01-01 → 2024-09-30]`` — before the first VERIFIED
    exchange-transaction-charge rate row (NSE/BSE eff. 2024-10-01) — is
    REFUSED by ``compute_trade_cost`` with exactly
    :class:`UnverifiedCostRegimeError`, never silently priced at an
    invented/current rate. This pins the refusal window so the P-10/P-12
    value properties' priceable-window date bound can never silently rot:
    if the boundary moves (a cited pre-2024 circular lands), this property
    fails loudly and both windows get consciously re-derived."""
    with pytest.raises(UnverifiedCostRegimeError):
        compute_trade_cost(trade)
