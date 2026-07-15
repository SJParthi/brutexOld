"""Unit tests for ``brutex.costs.calculator.compute_trade_cost``.

Tests the canonical fixture from `docs/track2/CALCULATOR_SPEC.md §12.1`
byte-exact, plus the additional fixtures from §12.2 covering:

- Losing trades (negative gross PnL)
- STT regime-switch boundary dates (DEC-COST-002)
- Instrument variation (NIFTY / BANKNIFTY / SENSEX)
- gst_split inter_state
- Zero gross PnL edge case
- Pre-existing legacy lot-size eras

Each test is the byte-exact assertion against the spec table.

Per Golden Rule #12: this module must reach 100% line + branch
coverage on `brutex.costs.*` (the per-module strict gate in
.github/workflows/ci.yml).
"""

from __future__ import annotations

from datetime import date, datetime

import pytest
from brutex.costs import (
    Broker,
    GstSplit,
    Instrument,
    OptionType,
    TradeCostBreakdown,
    TradeInput,
    TradeOutcome,
    TradeSide,
    UnsupportedTradeOutcomeError,
    UnverifiedCostRegimeError,
    compute_trade_cost,
    constants,
)
from brutex.costs.calculator import (
    _apply_slippage,
    _round_levy_paisa,
    _round_levy_rupee,
    _round_to_paisa,
    _round_to_rupee,
)
from brutex.execution.fill_mode import FillMode
from brutex.options.lot_size_history import OptionsUnderlying, get_lot_size
from hypothesis import given, settings
from hypothesis import strategies as st

# ─────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────


def _make_trade(
    *,
    entry_dt: datetime = datetime(2026, 5, 15, 10, 0),
    exit_dt: datetime | None = None,
    instrument: Instrument = "NIFTY",
    option_type: OptionType = "CE",
    strike: int = 2_500_000,
    expiry_date: date = date(2026, 5, 21),
    entry_open_paisa: int = 9_995,
    exit_open_paisa: int = 12_005,
    side: TradeSide = "long_call",
    n_lots: int = 1,
    broker: Broker = "groww",
    gst_split: GstSplit = "intra_state",
    trade_outcome: TradeOutcome = "NORMAL_CLOSE",
) -> TradeInput:
    """Build a TradeInput with the §12.1 canonical defaults.

    Allows individual fields to be overridden for fixture variants.
    """
    return TradeInput(
        entry_dt_ist=entry_dt,
        exit_dt_ist=exit_dt if exit_dt is not None else entry_dt.replace(hour=11),
        instrument=instrument,
        option_type=option_type,
        strike=strike,
        expiry_date=expiry_date,
        entry_bar_open_paisa=entry_open_paisa,
        exit_bar_open_paisa=exit_open_paisa,
        side=side,
        n_lots=n_lots,
        broker=broker,
        gst_split=gst_split,
        trade_outcome=trade_outcome,
    )


# ─────────────────────────────────────────────────────────────────
# §12.1 — Canonical NIFTY fixture (byte-exact spec table)
# ─────────────────────────────────────────────────────────────────


def test_fixture_12_1_nifty_canonical() -> None:
    """§12.1 NIFTY ATM call, post-Apr-2026 STT (0.15%), lot=65.

    Premium ₹99.95 → ₹120.05 with ±1 tick slippage = ₹100.00 → ₹120.00.

    Every line item below is the spec table.
    """
    r = compute_trade_cost(_make_trade())

    assert r.n_shares_per_lot == 65  # Jan-2026 onwards NIFTY lot
    assert r.n_shares == 65
    assert r.buy_fill_paisa == 10_000  # ₹100.00 (after +1 tick)
    assert r.sell_fill_paisa == 12_000  # ₹120.00 (after -1 tick)
    assert r.buy_notional_paisa == 650_000  # ₹6,500
    assert r.sell_notional_paisa == 780_000  # ₹7,800

    assert r.brokerage_paisa == 4_000  # ₹40 = ₹20 × 2 legs
    assert r.stt_paisa == 1_200  # ₹12 = ₹11.70 rounded up (statutory)
    assert r.exch_paisa == 501  # ₹5.01
    assert r.sebi_paisa == 2  # ₹0.02
    assert r.ipft_paisa == 7  # ₹0.07
    assert r.stamp_paisa == 0  # ₹0 = ₹0.195 → ₹0 rounded (statutory)
    assert r.gst_paisa == 800  # ₹8 (DEC-COST-001: round 18% total once)

    assert r.slippage_paisa == 650  # ₹6.50 (2 ticks × 65 shares)
    assert r.total_charges_paisa == 6_510  # ₹65.10
    assert r.gross_pnl_paisa == 130_000  # ₹1,300
    assert r.net_pnl_paisa == 123_490  # ₹1,234.90

    assert r.used_exercise_branch is False
    assert r.n_orders_per_leg == 1


# ─────────────────────────────────────────────────────────────────
# §12.2 F2 — Losing trade (negative gross PnL)
# ─────────────────────────────────────────────────────────────────


def test_fixture_f2_losing_long_call() -> None:
    """NIFTY long_call ₹100 → ₹80 (losing). Negative gross PnL.

    Entry bar ₹99.95 (→ ₹100.00 fill), exit bar ₹80.05 (→ ₹80.00 fill).
    """
    r = compute_trade_cost(
        _make_trade(
            entry_open_paisa=9_995,
            exit_open_paisa=8_005,
        )
    )

    assert r.buy_fill_paisa == 10_000
    assert r.sell_fill_paisa == 8_000
    assert r.buy_notional_paisa == 650_000
    assert r.sell_notional_paisa == 520_000
    assert r.gross_pnl_paisa == -130_000  # ₹-1,300 loss
    # Net PnL is more negative because of charges
    assert r.net_pnl_paisa < r.gross_pnl_paisa
    assert r.net_pnl_paisa == r.gross_pnl_paisa - r.total_charges_paisa


# ─────────────────────────────────────────────────────────────────
# §12.2 F5 — Pre-Apr-2026 STT regime (0.10%)
# ─────────────────────────────────────────────────────────────────


def test_fixture_f5_pre_apr_2026_stt() -> None:
    """NIFTY long_call ₹100 → ₹120 on 2025-12-15 (pre-Apr-2026 era).

    STT rate is 0.10% (not 0.15%). Per DEC-COST-002, regime selected
    by entry_dt.date().
    """
    r = compute_trade_cost(
        _make_trade(
            entry_dt=datetime(2025, 12, 15, 10, 0),
            exit_dt=datetime(2025, 12, 15, 11, 0),
            expiry_date=date(2025, 12, 18),
        )
    )

    # Lot size Dec-2025: per lot_size_history, NIFTY is 75 (post Nov-2024)
    assert r.n_shares_per_lot == 75
    assert r.n_shares == 75
    # STT at 0.10%: 0.10% × ₹9,000 sell-side = ₹9 — wait depends on n_shares
    # buy_notional = 10000 × 75 = 750000 = ₹7500
    # sell_notional = 12000 × 75 = 900000 = ₹9000
    # STT = 0.10% × ₹9000 = ₹9 = 900 paisa
    assert r.sell_notional_paisa == 900_000
    assert r.stt_paisa == 900


# ─────────────────────────────────────────────────────────────────
# §12.2 F15/F16 — STT regime switch boundary (2026-04-01)
# ─────────────────────────────────────────────────────────────────


def test_stt_regime_boundary_2026_03_31() -> None:
    """Day BEFORE STT hike: 2026-03-31 uses 0.10% rate."""
    r = compute_trade_cost(_make_trade(entry_dt=datetime(2026, 3, 31, 10, 0)))
    # 0.10% × ₹7,800 (sell notional for 65 shares × ₹120) = ₹7.80 → ₹8 rounded
    # NOTE: lot size on 2026-03-31 is 75 per Jul-2025 transition; not 65
    # Let me verify: lot history says (date(2025, 12, 30), 75) and (date(2026, 1, 1), 65)
    # So 2026-03-31 uses 65 (post Jan-2026 transition)
    assert r.n_shares_per_lot == 65
    assert r.sell_notional_paisa == 780_000
    assert r.stt_paisa == 800  # ₹8 = ₹7.80 rounded up


def test_stt_regime_boundary_2026_04_01() -> None:
    """Day OF STT hike: 2026-04-01 uses 0.15% rate."""
    r = compute_trade_cost(_make_trade(entry_dt=datetime(2026, 4, 1, 10, 0)))
    assert r.n_shares_per_lot == 65
    assert r.sell_notional_paisa == 780_000
    # 0.15% × ₹7,800 = ₹11.70 → ₹12 rounded
    assert r.stt_paisa == 1_200


# ─────────────────────────────────────────────────────────────────
# §12.2 F8 — BANKNIFTY (lot 30, NSE)
# ─────────────────────────────────────────────────────────────────


def test_fixture_f8_banknifty_long_put() -> None:
    """BANKNIFTY long_put ₹200 → ₹250 on 2026-05-15.

    Lot size 30 (post Nov-2024 era).
    """
    r = compute_trade_cost(
        _make_trade(
            instrument="BANKNIFTY",
            option_type="PE",
            side="long_put",
            entry_open_paisa=19_995,  # ₹199.95 → ₹200.00
            exit_open_paisa=25_005,  # ₹250.05 → ₹250.00
        )
    )

    assert r.n_shares_per_lot == 30  # Jan-2026 onwards
    assert r.n_shares == 30
    assert r.buy_fill_paisa == 20_000
    assert r.sell_fill_paisa == 25_000
    # Exchange charge uses NSE rate (same as NIFTY)
    assert r.gross_pnl_paisa == 150_000  # ₹1,500 profit


# ─────────────────────────────────────────────────────────────────
# §12.2 F9 — SENSEX (lot 20, BSE)
# ─────────────────────────────────────────────────────────────────


def test_fixture_f9_sensex_long_call() -> None:
    """SENSEX long_call ₹100 → ₹120 on 2026-05-15.

    SENSEX uses BSE rates (different exch + near-zero IPFT).
    Lot size 20.
    """
    r = compute_trade_cost(_make_trade(instrument="SENSEX"))

    assert r.n_shares_per_lot == 20
    assert r.n_shares == 20
    # BSE exchange charge: 0.03250%, IPFT: ~0
    assert r.ipft_paisa == 0  # BSE IPFT is 0
    # Exchange charge differs from NSE — just assert it's positive
    assert r.exch_paisa > 0


# ─────────────────────────────────────────────────────────────────
# §12.2 F6 — Multi-lot (brokerage stays flat)
# ─────────────────────────────────────────────────────────────────


def test_fixture_f6_10_lots_brokerage_flat() -> None:
    """10 lots: brokerage stays ₹40 total (per-order, not per-lot)."""
    r = compute_trade_cost(_make_trade(n_lots=10))

    assert r.n_shares_per_lot == 65
    assert r.n_shares == 650  # 10 × 65
    # Per-order brokerage: ₹20 × 2 legs = ₹40 flat regardless of lots
    assert r.brokerage_paisa == 4_000

    # And the 1-lot equivalent has the SAME brokerage
    r_1lot = compute_trade_cost(_make_trade(n_lots=1))
    assert r_1lot.brokerage_paisa == r.brokerage_paisa


# ─────────────────────────────────────────────────────────────────
# §12.2 F12 — inter_state GST
# ─────────────────────────────────────────────────────────────────


def test_fixture_f12_inter_state_gst() -> None:
    """inter_state gst_split: total is rounded once at 18% (same result
    as intra_state per DEC-COST-001 — gst_split only changes display)."""
    r_intra = compute_trade_cost(_make_trade(gst_split="intra_state"))
    r_inter = compute_trade_cost(_make_trade(gst_split="inter_state"))

    # Per DEC-COST-001, the total GST is the same regardless of split
    # (gst_split only affects how the total is DISPLAYED, not computed).
    assert r_intra.gst_paisa == r_inter.gst_paisa


# ─────────────────────────────────────────────────────────────────
# §12.2 F13 — Zero gross PnL (negative net)
# ─────────────────────────────────────────────────────────────────


def test_fixture_f13_zero_gross_pnl_negative_net() -> None:
    """₹100 → ₹100 entry == exit. Gross 0 but net is -charges."""
    # Use same paisa price both sides — note slippage still applies
    # buy = entry+1 = 10001, sell = exit-1 = 9999 → notional buy > sell
    r = compute_trade_cost(
        _make_trade(
            entry_open_paisa=10_000,
            exit_open_paisa=10_000,
        )
    )

    # gross_pnl is negative because slippage moves fills apart
    assert r.buy_fill_paisa == 10_005
    assert r.sell_fill_paisa == 9_995
    # gross = sell - buy = (9995 - 10005) × 65 = -650 paisa
    assert r.gross_pnl_paisa == -650
    # Net is even more negative
    assert r.net_pnl_paisa < r.gross_pnl_paisa


# ─────────────────────────────────────────────────────────────────
# §12.2 F11 — Pre-Jul-2021 lot history (75 shares) — SPLIT per
# DEC-ETC-REGIME-HISTORY-001: a 2021 entry date now REFUSES the whole
# compute (pre-2024-10-01 ETC refusal window), so the lot-75 pin moves
# to the lot-size lookup directly and the compute path pins the refusal.
# ─────────────────────────────────────────────────────────────────


def test_fixture_f11_pre_jul_2021_lot_75() -> None:
    """NIFTY 2021-06-15 has lot size 75 (pre-Jul-2021 default) — pinned at
    ``get_lot_size`` directly (compute_trade_cost now refuses the date on
    the ETC regime, so the lot value is no longer observable through it)."""
    assert get_lot_size(OptionsUnderlying.NIFTY, date(2021, 6, 15)) == 75


def test_pre_boundary_compute_raises_unverified() -> None:
    """DEC-ETC-REGIME-HISTORY-001 end-to-end: a pre-2024-10-01 entry date
    refuses the WHOLE trade cost — no component is fabricated."""
    with pytest.raises(UnverifiedCostRegimeError) as exc_info:
        compute_trade_cost(
            _make_trade(
                entry_dt=datetime(2021, 6, 15, 10, 0),
                exit_dt=datetime(2021, 6, 15, 11, 0),
                expiry_date=date(2021, 6, 24),
            )
        )
    err = exc_info.value
    assert err.exchange == "NSE"
    assert err.on_date == date(2021, 6, 15)
    assert err.verified_from == constants.ETC_REGIME_OCT_2024_START


# ─────────────────────────────────────────────────────────────────
# Broker variation: Zerodha (same flat ₹20)
# ─────────────────────────────────────────────────────────────────


def test_zerodha_broker_same_brokerage() -> None:
    """Zerodha and Groww both charge ₹20 flat per executed order."""
    r_groww = compute_trade_cost(_make_trade(broker="groww"))
    r_zerodha = compute_trade_cost(_make_trade(broker="zerodha"))

    assert r_groww.brokerage_paisa == r_zerodha.brokerage_paisa == 4_000


# ─────────────────────────────────────────────────────────────────
# Validation tests (TradeInput __post_init__)
# ─────────────────────────────────────────────────────────────────


def test_trade_input_rejects_exit_before_entry() -> None:
    """exit_dt_ist < entry_dt_ist must raise."""
    with pytest.raises(ValueError, match="exit_dt_ist .* must be >= entry_dt_ist"):
        TradeInput(
            entry_dt_ist=datetime(2026, 5, 15, 11, 0),
            exit_dt_ist=datetime(2026, 5, 15, 10, 0),
            instrument="NIFTY",
            option_type="CE",
            strike=2_500_000,
            expiry_date=date(2026, 5, 21),
            entry_bar_open_paisa=10_000,
            exit_bar_open_paisa=10_000,
            side="long_call",
        )


def test_trade_input_rejects_sub_tick_entry_price() -> None:
    """entry_bar_open_paisa < 5 (1 tick) must raise."""
    with pytest.raises(ValueError, match="entry_bar_open_paisa .* below 1 tick"):
        TradeInput(
            entry_dt_ist=datetime(2026, 5, 15, 10, 0),
            exit_dt_ist=datetime(2026, 5, 15, 11, 0),
            instrument="NIFTY",
            option_type="CE",
            strike=2_500_000,
            expiry_date=date(2026, 5, 21),
            entry_bar_open_paisa=4,
            exit_bar_open_paisa=10_000,
            side="long_call",
        )


def test_trade_input_rejects_sub_tick_exit_price() -> None:
    """exit_bar_open_paisa < 5 (1 tick) must raise."""
    with pytest.raises(ValueError, match="exit_bar_open_paisa .* below 1 tick"):
        TradeInput(
            entry_dt_ist=datetime(2026, 5, 15, 10, 0),
            exit_dt_ist=datetime(2026, 5, 15, 11, 0),
            instrument="NIFTY",
            option_type="CE",
            strike=2_500_000,
            expiry_date=date(2026, 5, 21),
            entry_bar_open_paisa=10_000,
            exit_bar_open_paisa=4,
            side="long_call",
        )


def test_trade_input_rejects_zero_lots() -> None:
    """n_lots < 1 must raise."""
    with pytest.raises(ValueError, match="n_lots .* must be >= 1"):
        TradeInput(
            entry_dt_ist=datetime(2026, 5, 15, 10, 0),
            exit_dt_ist=datetime(2026, 5, 15, 11, 0),
            instrument="NIFTY",
            option_type="CE",
            strike=2_500_000,
            expiry_date=date(2026, 5, 21),
            entry_bar_open_paisa=10_000,
            exit_bar_open_paisa=10_000,
            side="long_call",
            n_lots=0,
        )


def test_trade_input_rejects_zero_strike() -> None:
    """strike < 1 must raise."""
    with pytest.raises(ValueError, match="strike .* must be >= 1"):
        TradeInput(
            entry_dt_ist=datetime(2026, 5, 15, 10, 0),
            exit_dt_ist=datetime(2026, 5, 15, 11, 0),
            instrument="NIFTY",
            option_type="CE",
            strike=0,
            expiry_date=date(2026, 5, 21),
            entry_bar_open_paisa=10_000,
            exit_bar_open_paisa=10_000,
            side="long_call",
        )


# ─────────────────────────────────────────────────────────────────
# CHANGE 7 — runtime Literal-membership validation (fail loud on typo)
# ─────────────────────────────────────────────────────────────────


def _make_trade_kwargs() -> dict[str, object]:
    """A valid kwargs baseline for direct ``TradeInput(**kwargs)`` builds.

    Used by the Literal-membership tests so a single bad field can be
    overridden while every other field stays valid.
    """
    return {
        "entry_dt_ist": datetime(2026, 5, 15, 10, 0),
        "exit_dt_ist": datetime(2026, 5, 15, 11, 0),
        "instrument": "NIFTY",
        "option_type": "CE",
        "strike": 2_500_000,
        "expiry_date": date(2026, 5, 21),
        "entry_bar_open_paisa": 10_000,
        "exit_bar_open_paisa": 10_000,
        "side": "long_call",
        "n_lots": 1,
        "broker": "groww",
        "gst_split": "intra_state",
        "trade_outcome": "NORMAL_CLOSE",
    }


@pytest.mark.parametrize(
    ("field_name", "bad_value"),
    [
        ("instrument", "banana"),
        ("option_type", "XX"),
        ("side", "banana"),
        ("broker", "robinhood"),
        ("gst_split", "off_world"),
        ("trade_outcome", "EXPIRED_MAYBE"),
    ],
)
def test_trade_input_rejects_invalid_literal(field_name: str, bad_value: str) -> None:
    """CHANGE 7: a value outside a Literal field's allowed set fails loud.

    A typo'd enum string is SILENTLY ACCEPTED by the ``Literal`` type alone
    (it is a static-only annotation). ``__post_init__`` now runtime-validates
    membership and raises ``ValueError`` naming the field, the bad value, and
    the allowed set — never a silently-wrong cost.
    """
    kwargs = _make_trade_kwargs()
    kwargs[field_name] = bad_value
    with pytest.raises(ValueError, match=rf"{field_name} '{bad_value}' is not a valid value"):
        TradeInput(**kwargs)  # type: ignore[arg-type]


def test_trade_input_accepts_every_valid_literal_member() -> None:
    """CHANGE 7: every VALID member of every Literal field is accepted.

    Exercises the not-raised branch of the membership guard for each field,
    so the new validation loop is fully covered (line + branch).
    """
    valid_by_field: dict[str, tuple[str, ...]] = {
        "instrument": ("NIFTY", "BANKNIFTY", "SENSEX"),
        "option_type": ("CE", "PE"),
        "side": ("long_call", "long_put", "short_call", "short_put"),
        "broker": ("groww", "zerodha"),
        "gst_split": ("intra_state", "inter_state"),
        "trade_outcome": (
            "NORMAL_CLOSE",
            "EXPIRY_EXERCISE",
            "EXPIRY_ASSIGN",
            "EXPIRY_WORTHLESS",
        ),
    }
    for field_name, members in valid_by_field.items():
        for member in members:
            kwargs = _make_trade_kwargs()
            kwargs[field_name] = member
            trade = TradeInput(**kwargs)  # type: ignore[arg-type]
            assert getattr(trade, field_name) == member


# ─────────────────────────────────────────────────────────────────
# Short-side branches (forward-compat stubs per DEC-EXEC-008)
# ─────────────────────────────────────────────────────────────────


def test_short_call_inverts_pnl_sign() -> None:
    """short_call: PnL inverts (sold first, bought back to close).

    Track 2 (DEC-EXEC-008) never uses this branch but it exists as
    a forward-compat stub. Verify it computes a sensible result.
    """
    r = compute_trade_cost(_make_trade(side="short_call"))

    # For short, the engine inverts gross_pnl. The fills get computed
    # via the _apply_slippage short branch which is intentionally different
    # from long. Sanity check: result is internally consistent.
    assert r.net_pnl_paisa == r.gross_pnl_paisa - r.total_charges_paisa


def test_compute_rejects_non_normal_outcome() -> None:
    """MAJOR-1: a non-NORMAL_CLOSE outcome fails loud (no silent NORMAL_CLOSE).

    The §4.2 expiry state machine is not implemented; pricing an expiry
    outcome with the premium-based NORMAL_CLOSE math would be silently
    wrong, so ``compute_trade_cost`` raises ``UnsupportedTradeOutcomeError``.
    Verified safe: the only production caller (``options/executor.py``)
    passes ``NORMAL_CLOSE``.
    """
    for outcome in ("EXPIRY_EXERCISE", "EXPIRY_ASSIGN", "EXPIRY_WORTHLESS"):
        with pytest.raises(UnsupportedTradeOutcomeError, match="not supported"):
            compute_trade_cost(_make_trade(trade_outcome=outcome))  # type: ignore[arg-type]


def test_unsupported_outcome_is_value_error() -> None:
    """MAJOR-1: ``UnsupportedTradeOutcomeError`` is a ``ValueError`` subclass.

    Callers that catch ``ValueError`` for input validation still catch it,
    while a caller can pin the exact type. The message names the outcome.
    """
    assert issubclass(UnsupportedTradeOutcomeError, ValueError)
    with pytest.raises(ValueError, match="EXPIRY_EXERCISE"):
        compute_trade_cost(_make_trade(trade_outcome="EXPIRY_EXERCISE"))  # type: ignore[arg-type]


def test_normal_close_outcome_flag_false() -> None:
    """MAJOR-1: NORMAL_CLOSE never engages the §4.2 exercise branch."""
    r = compute_trade_cost(_make_trade(trade_outcome="NORMAL_CLOSE"))
    assert r.used_exercise_branch is False


# ─────────────────────────────────────────────────────────────────
# Rate-helper tests (brutex.costs.constants)
# ─────────────────────────────────────────────────────────────────


def test_stt_options_rate_pre_oct_2024() -> None:
    rate = constants.stt_options_rate_bps_x100(date(2024, 9, 30))
    assert rate == constants.STT_OPTIONS_PRE_OCT_2024_BPS_X100  # 0.0625%


def test_stt_options_rate_oct_2024_era() -> None:
    rate = constants.stt_options_rate_bps_x100(date(2024, 10, 1))
    assert rate == constants.STT_OPTIONS_OCT_2024_TO_MAR_2026_BPS_X100  # 0.10%


def test_stt_options_rate_post_apr_2026() -> None:
    rate = constants.stt_options_rate_bps_x100(date(2026, 4, 1))
    assert rate == constants.STT_OPTIONS_POST_APR_2026_BPS_X100  # 0.15%


def test_stt_exercise_rate_pre_apr_2026() -> None:
    rate = constants.stt_exercise_rate_bps_x100(date(2026, 3, 31))
    assert rate == constants.STT_EXERCISE_PRE_APR_2026_BPS_X100  # 0.125%


def test_stt_exercise_rate_post_apr_2026() -> None:
    rate = constants.stt_exercise_rate_bps_x100(date(2026, 4, 1))
    assert rate == constants.STT_EXERCISE_POST_APR_2026_BPS_X100  # 0.15%


def test_exchange_charge_nifty_nse() -> None:
    got = constants.exchange_charge_bps_x100("NIFTY", date(2026, 5, 15))
    assert got == constants.NSE_ETC_OPTIONS_BPS_X100


def test_exchange_charge_banknifty_nse() -> None:
    got = constants.exchange_charge_bps_x100("BANKNIFTY", date(2026, 5, 15))
    assert got == constants.NSE_ETC_OPTIONS_BPS_X100


def test_exchange_charge_sensex_bse() -> None:
    got = constants.exchange_charge_bps_x100("SENSEX", date(2026, 5, 15))
    assert got == constants.BSE_ETC_OPTIONS_BPS_X100


def test_ipft_nifty_nse() -> None:
    assert constants.ipft_bps_x100("NIFTY") == constants.NSE_IPFT_BPS_X100


def test_ipft_sensex_bse_zero() -> None:
    assert constants.ipft_bps_x100("SENSEX") == 0


def test_brokerage_groww() -> None:
    assert constants.brokerage_paisa("groww") == constants.GROWW_BROKERAGE_PAISA


def test_brokerage_zerodha() -> None:
    assert constants.brokerage_paisa("zerodha") == constants.ZERODHA_BROKERAGE_PAISA


# ─────────────────────────────────────────────────────────────────
# UnverifiedCostRegimeError — direct construction (DEC-ETC-REGIME-
# HISTORY-001). These live HERE because ci.yml gate 19d covers
# brutex.costs.types through test_costs_calculator.py ONLY.
# ─────────────────────────────────────────────────────────────────


def test_unverified_error_attrs_and_message() -> None:
    """Bounded-window construction: every structured attr stored verbatim
    and the message carries window + source + remediation."""
    err = UnverifiedCostRegimeError(
        charge="exchange transaction charge (options premium)",
        exchange="NSE",
        on_date=date(2024, 5, 2),
        window_start=date.min,
        verified_from=date(2024, 10, 1),
        source="the citation-gap stamp",
    )
    assert err.charge == "exchange transaction charge (options premium)"
    assert err.exchange == "NSE"
    assert err.on_date == date(2024, 5, 2)
    assert err.window_start == date.min
    assert err.verified_from == date(2024, 10, 1)
    assert err.source == "the citation-gap stamp"
    # The remediation DEFAULT text: how the window closes (data-only).
    assert "add ONE dated" in err.remediation
    assert "RegimeRow" in err.remediation
    assert "MIGRATION.md" in err.remediation
    msg = str(err)
    assert "(NSE)" in msg
    assert "UNVERIFIED for 2024-05-02" in msg
    assert "→ 2024-10-01)" in msg
    assert "the citation-gap stamp" in msg
    assert "Golden Rule #1" in msg


def test_unverified_error_open_window() -> None:
    """``verified_from=None`` == an open-ended trailing refusal window —
    the message's window end reads 'open'."""
    err = UnverifiedCostRegimeError(
        charge="synthetic charge",
        exchange="BSE",
        on_date=date(2025, 1, 1),
        window_start=date(2024, 10, 1),
        verified_from=None,
        source="trailing refusal",
        remediation="custom remediation text",
    )
    assert err.verified_from is None
    assert err.remediation == "custom remediation text"
    assert "→ open)" in str(err)
    assert "custom remediation text" in str(err)


def test_unverified_error_no_exchange_label() -> None:
    """``exchange=""`` (a non-exchange-scoped table) omits the parenthesised
    exchange label from the message."""
    err = UnverifiedCostRegimeError(
        charge="synthetic charge",
        exchange="",
        on_date=date(2024, 5, 2),
        window_start=date.min,
        verified_from=date(2024, 10, 1),
        source="no exchange scope",
    )
    assert err.exchange == ""
    assert "(" not in str(err).split(" is UNVERIFIED")[0]
    assert str(err).startswith("synthetic charge is UNVERIFIED")


def test_unverified_error_is_value_error_subclass() -> None:
    """Catch-ValueError callers keep working (the UnsupportedTradeOutcome
    precedent); a caller can pin the exact type."""
    assert issubclass(UnverifiedCostRegimeError, ValueError)


# ─────────────────────────────────────────────────────────────────
# ETC regime boundary through the FULL calculator stack
# ─────────────────────────────────────────────────────────────────


def test_boundary_exact_oct_2024_computes() -> None:
    """Entry EXACTLY on 2024-10-01 computes successfully at the verified
    rates (full-stack bisect-boundary mutant killer: bisect_right→
    bisect_left would refuse this date)."""
    r = compute_trade_cost(
        _make_trade(
            entry_dt=datetime(2024, 10, 1, 10, 0),
            exit_dt=datetime(2024, 10, 1, 11, 0),
            expiry_date=date(2024, 10, 31),
        )
    )
    assert r.total_charges_paisa > 0
    assert r.exch_paisa > 0


def test_day_before_boundary_refuses() -> None:
    """2024-09-30 (the day before the verified window opens) refuses."""
    with pytest.raises(UnverifiedCostRegimeError):
        compute_trade_cost(
            _make_trade(
                entry_dt=datetime(2024, 9, 30, 10, 0),
                exit_dt=datetime(2024, 9, 30, 11, 0),
                expiry_date=date(2024, 10, 31),
            )
        )


@pytest.mark.property
@settings(max_examples=200, deadline=None)
@given(
    entry_dt=st.datetimes(
        min_value=datetime(2020, 1, 1, 9, 15),
        max_value=datetime(2024, 9, 30, 15, 20),
    ),
    instrument=st.sampled_from(["NIFTY", "BANKNIFTY", "SENSEX"]),
)
def test_property_pre_boundary_always_refuses(entry_dt: datetime, instrument: Instrument) -> None:
    """Any pre-2024-10-01 entry refuses — NEVER a wrong number.

    2021-01-01 → 2024-09-30 raises ``UnverifiedCostRegimeError`` (the ETC
    refusal window); 2020 dates raise the lot-size-history ``ValueError``
    FIRST (Step 2 precedes Step 6; the lot floor is 2021-01-01 —
    COSTS_VERIFIED §20 row 4). Either way no cost is fabricated.
    """
    with pytest.raises(ValueError) as exc_info:  # subtype asserted below
        compute_trade_cost(
            _make_trade(
                entry_dt=entry_dt,
                exit_dt=entry_dt,
                expiry_date=entry_dt.date(),
                instrument=instrument,
            )
        )
    if entry_dt.date() >= date(2021, 1, 1):
        assert isinstance(exc_info.value, UnverifiedCostRegimeError)


# ─────────────────────────────────────────────────────────────────
# Return-type sanity
# ─────────────────────────────────────────────────────────────────


def test_compute_trade_cost_returns_breakdown() -> None:
    """compute_trade_cost returns a TradeCostBreakdown frozen dataclass."""
    r = compute_trade_cost(_make_trade())
    assert isinstance(r, TradeCostBreakdown)
    # All numeric fields are ints (no floats — per Golden Rule "no float drift")
    assert all(
        isinstance(getattr(r, f), int) or isinstance(getattr(r, f), bool)
        for f in (
            "n_shares_per_lot",
            "n_shares",
            "buy_fill_paisa",
            "sell_fill_paisa",
            "buy_notional_paisa",
            "sell_notional_paisa",
            "brokerage_paisa",
            "stt_paisa",
            "exch_paisa",
            "sebi_paisa",
            "ipft_paisa",
            "stamp_paisa",
            "gst_paisa",
            "slippage_paisa",
            "total_charges_paisa",
            "gross_pnl_paisa",
            "net_pnl_paisa",
            "used_exercise_branch",
            "n_orders_per_leg",
        )
    )


# ────────────────────────────────────────────────────────────────────────
# Spec ↔ test crosswalk anchors (DEC-P0-004)
# ────────────────────────────────────────────────────────────────────────
# `scripts/ci/spec_test_crosswalk.py` requires these exact function names
# to exist (mapped to CALCULATOR_SPEC sections). Each performs a real
# minimal assertion against the spec's claim; substantive coverage of
# each spec section is in the byte-exact fixtures above.


def test_fixture_12_2() -> None:
    """Anchor for CALCULATOR_SPEC §12.2 'Additional fixtures'.

    §12.2 prescribes additional fixture variants beyond §12.1's canonical
    NIFTY trade. The compute_trade_cost function must handle losing
    trades (negative gross PnL). This anchor verifies that minimum.
    """
    trade = _make_trade(
        entry_open_paisa=12_000,
        exit_open_paisa=10_000,  # losing trade
    )
    breakdown = compute_trade_cost(trade)
    assert breakdown.gross_pnl_paisa < 0, "losing trade must produce negative gross_pnl"


def test_fixture_12_3_multi_lot() -> None:
    """Anchor for CALCULATOR_SPEC §12.3 'Multi-lot scaling'.

    §12.3 prescribes that increasing n_lots scales notional + taxes
    linearly (with brokerage-cap applied per leg). Verifies linear
    gross-PnL scaling.
    """
    one_lot = _make_trade(n_lots=1)
    ten_lots = _make_trade(n_lots=10)
    one_lot_pnl = compute_trade_cost(one_lot).gross_pnl_paisa
    ten_lot_pnl = compute_trade_cost(ten_lots).gross_pnl_paisa
    assert ten_lot_pnl == one_lot_pnl * 10, "gross_pnl must scale linearly with n_lots"


def test_fixture_12_7_side_symmetry() -> None:
    """Anchor for CALCULATOR_SPEC §12.7 'Long vs Short'.

    §12.7 prescribes that long_call and long_put pay the same charge
    structure (brokerage, STT, exchange fee, IPFT, SEBI, stamp duty,
    GST) for the same notional. Only the side label differs.
    """
    long_call = _make_trade(option_type="CE", side="long_call")
    long_put = _make_trade(option_type="PE", side="long_put")
    call_bd = compute_trade_cost(long_call)
    put_bd = compute_trade_cost(long_put)
    assert call_bd.brokerage_paisa == put_bd.brokerage_paisa, "CE/PE brokerage must match"


def test_trade_outcome_state_machine() -> None:
    """Anchor for CALCULATOR_SPEC §4.2 'Trade outcome state machine'.

    §4.2 prescribes TradeOutcome Literal: NORMAL_CLOSE / EXPIRY_EXERCISE /
    EXPIRY_ASSIGN / EXPIRY_WORTHLESS. ``TradeInput`` ACCEPTS each value, but
    per MAJOR-1 the calculator only IMPLEMENTS NORMAL_CLOSE: pricing an
    expiry outcome fails loud with ``UnsupportedTradeOutcomeError`` rather
    than silently returning NORMAL_CLOSE numbers (the §4.2 charge state
    machine is not yet implemented).
    """
    valid_outcomes: list[TradeOutcome] = [
        "NORMAL_CLOSE",
        "EXPIRY_EXERCISE",
        "EXPIRY_ASSIGN",
        "EXPIRY_WORTHLESS",
    ]
    assert len(valid_outcomes) == 4, "spec §4.2 prescribes exactly 4 outcomes"
    for outcome in valid_outcomes:
        trade = _make_trade(trade_outcome=outcome)
        assert trade.trade_outcome == outcome  # TradeInput accepts every outcome
        if outcome == "NORMAL_CLOSE":
            breakdown = compute_trade_cost(trade)
            assert breakdown is not None
        else:
            with pytest.raises(UnsupportedTradeOutcomeError):
                compute_trade_cost(trade)


def test_groww_worthless_brokerage() -> None:
    """Anchor for CALCULATOR_SPEC §4.3 'broker-flat-brokerage gotcha'.

    §4.3 prescribes the broker's flat brokerage applies even when an option
    expires worthless. Per MAJOR-1 the EXPIRY_WORTHLESS charge path (§4.2) is
    not yet implemented, so the calculator fails loud rather than silently
    pricing it as NORMAL_CLOSE. This anchor pins that fail-loud contract
    (the implemented brokerage-only worthless path is future work).
    """
    worthless = _make_trade(
        broker="groww",
        trade_outcome="EXPIRY_WORTHLESS",
    )
    assert worthless.trade_outcome == "EXPIRY_WORTHLESS"  # input round-trips
    with pytest.raises(UnsupportedTradeOutcomeError, match="not supported"):
        compute_trade_cost(worthless)


# ─────────────────────────────────────────────────────────────────
# CHANGE 2 (MINOR-3) — post-slippage fill floored at 1 tick
# ─────────────────────────────────────────────────────────────────


def test_long_exit_fill_floored_at_one_tick() -> None:
    """MINOR-3: a long exit on a 1-tick bar floors the sell fill at 1 tick.

    ``exit_bar_open_paisa = 5`` (exactly 1 tick) passes the ``>= 5``
    validation, but 1-tick adverse long slippage gives ``5 - 5 = 0`` — an
    impossible ₹0.00 sale. The post-slippage fill is clamped to
    ``TICK_PAISA`` so the sell fill is 5 paise, never 0.
    """
    r = compute_trade_cost(
        _make_trade(
            entry_open_paisa=10_000,
            exit_open_paisa=5,  # exactly 1 tick
            side="long_call",
        )
    )
    # Without the floor this would be 0 (a free sale); clamped to 1 tick.
    assert r.sell_fill_paisa == constants.TICK_PAISA  # 5 paise, NOT 0
    assert r.sell_fill_paisa > 0
    assert r.sell_notional_paisa == 5 * r.n_shares


def test_realized_slippage_normal_path_two_ticks() -> None:
    """NIT-1: on the normal path (fills ≥ 2 ticks) slippage_paisa == 2*tick*shares.

    Both fills move a full tick adverse, so the realized slippage equals the
    nominal ``2 × TICK_PAISA × n_shares`` — byte-identical to the pre-NIT-1
    nominal value (the canonical §12.1 NIFTY fixture's 650 paisa is unchanged).
    """
    r = compute_trade_cost(
        _make_trade(entry_open_paisa=9_995, exit_open_paisa=12_005, side="long_call")
    )
    assert r.slippage_paisa == 2 * constants.TICK_PAISA * r.n_shares


def test_realized_slippage_floored_exit_one_tick() -> None:
    """NIT-1: a 5-paise (1-tick) long exit floors the sell fill, so the
    realized slippage is 1*tick*shares — NOT the nominal 2*tick*shares.

    ``exit_bar_open_paisa = 5`` slips to ``5 - 5 = 0``, floored UP to one
    tick (5 paise). The sell leg therefore moved 0 paise — only the buy leg
    actually slipped — so the realized slippage is exactly ``1 × TICK_PAISA ×
    n_shares``, never over-stating the slippage carried by the notionals/PnL.
    """
    r = compute_trade_cost(
        _make_trade(entry_open_paisa=10_000, exit_open_paisa=5, side="long_call")
    )
    assert r.sell_fill_paisa == constants.TICK_PAISA  # floored to 5, not 0
    # Realized: only the buy leg moved (1 tick); the floored sell leg moved 0.
    assert r.slippage_paisa == 1 * constants.TICK_PAISA * r.n_shares
    assert r.slippage_paisa != 2 * constants.TICK_PAISA * r.n_shares


def test_long_exit_fill_above_floor_unchanged() -> None:
    """MINOR-3: the normal path (exit ≥ 2 ticks) is byte-unchanged by the floor.

    A ₹0.10 (2-tick) exit slips to ₹0.05 (1 tick), already at the floor —
    so the clamp is a no-op and the fill equals the un-clamped value.
    """
    r = compute_trade_cost(
        _make_trade(
            entry_open_paisa=10_000,
            exit_open_paisa=10,  # 2 ticks → slips to 5 (1 tick), no clamp change
            side="long_call",
        )
    )
    assert r.sell_fill_paisa == 5  # 10 - 5, equals the floor, un-clamped value


# ─────────────────────────────────────────────────────────────────
# CHANGE 4 (MINOR-4) — byte-exact slippage fills for all 4 leg combos
# ─────────────────────────────────────────────────────────────────


def test_slippage_long_fills_byte_exact() -> None:
    """CALCULATOR_SPEC §8: long entry = open+1tick, exit = open-1tick.

    Byte-exact concrete paisa values (replacing tautological assertions).
    """
    r = compute_trade_cost(
        _make_trade(entry_open_paisa=10_000, exit_open_paisa=12_000, side="long_call")
    )
    assert r.buy_fill_paisa == 10_005  # entry 10000 + 5 tick (adverse, higher)
    assert r.sell_fill_paisa == 11_995  # exit 12000 - 5 tick (adverse, lower)


def test_slippage_short_fills_byte_exact_adverse() -> None:
    """Short fills match the resolved §4/§8 adverse rule, byte-exact.

    DEC-TRACK2-COSTS-SPEC-RECONCILE-001 resolved the #963 MINOR-4 spec
    contradiction: CALCULATOR_SPEC §8's ADVERSE table is authoritative and §4's
    pseudocode was rewritten to agree — short entry SELL = ``entry_bar_open − 1
    tick`` (adverse, LOWER; the old pin was the favorable ``+ 1 tick``) and
    cover BUY = ``exit_bar_open + 1 tick`` (adverse, HIGHER — unchanged). Track
    2 stays BUY-only (DEC-EXEC-008): this branch is production-unreachable
    forward-compat. The long side cannot move under this flip — the standing
    witness is ``test_slippage_long_fills_byte_exact`` above (10_005 / 11_995).
    """
    r = compute_trade_cost(
        _make_trade(entry_open_paisa=10_000, exit_open_paisa=12_000, side="short_call")
    )
    assert r.buy_fill_paisa == 12_005  # cover: exit 12000 + 5 tick (adverse, higher)
    # Resolved §4/§8 adverse rule (DEC-TRACK2-COSTS-SPEC-RECONCILE-001):
    assert r.sell_fill_paisa == 9_995  # entry 10000 - 5 tick (adverse, lower)


def test_short_entry_fill_floored_at_one_tick() -> None:
    """The adverse re-pin's NEW floor case: a short ENTRY on a 1-tick bar.

    Before DEC-TRACK2-COSTS-SPEC-RECONCILE-001 the short entry SELL filled
    at ``entry_open + tick`` (favorable) and could never reach ₹0.00; the
    adverse re-pin (``entry_open − tick``) makes ``5 − 5 = 0`` reachable on
    the short ENTRY leg — the exact scenario the ``_apply_slippage``
    docstring now claims the MINOR-3 one-tick floor covers. Pin it: the
    sell fill floors at ``TICK_PAISA`` (5 paise, never a "free" ₹0.00
    sale), the cover buy is untouched, and the realized slippage is
    ``1 × tick × shares`` (only the buy leg truly moved — the floored sell
    leg moved 0), mirroring ``test_realized_slippage_floored_exit_one_tick``
    on the long side. Production-unreachable under DEC-EXEC-008 (BUY-only);
    forward-compat teeth only.
    """
    r = compute_trade_cost(
        _make_trade(entry_open_paisa=5, exit_open_paisa=12_000, side="short_call")
    )
    assert r.sell_fill_paisa == constants.TICK_PAISA  # floored from 5 - 5 = 0
    assert r.buy_fill_paisa == 12_005  # cover: exit 12000 + 5 tick (adverse, higher)
    # Realized: only the cover buy moved (1 tick); the floored sell leg moved 0.
    assert r.slippage_paisa == 1 * constants.TICK_PAISA * r.n_shares


# ─────────────────────────────────────────────────────────────────
# CHANGE 3 (NIT-1) — direct ROUND_HALF_UP rounding-helper tests
# ─────────────────────────────────────────────────────────────────


def test_round_to_paisa_positive_path_byte_identical() -> None:
    """NIT-1: positive inputs are byte-identical to the historical formula."""
    for x in (0, 1, 4_999_999, 5_000_000, 5_000_001, 14_999_999, 15_000_000, 99_999_999):
        assert _round_to_paisa(x) == (x + 5_000_000) // 10_000_000


def test_round_to_paisa_half_up_away_from_zero() -> None:
    """NIT-1: ties round AWAY from zero (ROUND_HALF_UP), both signs."""
    assert _round_to_paisa(5_000_000) == 1  # +0.5 → +1 (just-on tie)
    assert _round_to_paisa(4_999_999) == 0  # just below half → 0
    assert _round_to_paisa(5_000_001) == 1  # just above half → 1
    assert _round_to_paisa(15_000_000) == 2  # +1.5 → +2
    assert _round_to_paisa(0) == 0  # exact zero
    assert _round_to_paisa(-5_000_000) == -1  # -0.5 → -1 (away from zero)
    assert _round_to_paisa(-15_000_000) == -2  # -1.5 → -2
    assert _round_to_paisa(-4_999_999) == 0  # just-above -half → 0
    assert _round_to_paisa(-5_000_001) == -1  # just-below -half → -1


def test_round_to_rupee_positive_path_byte_identical() -> None:
    """NIT-1: positive inputs are byte-identical to the historical formula."""
    for x in (0, 1, 49, 50, 51, 149, 150, 1_170, 99_999):
        assert _round_to_rupee(x) == ((x + 50) // 100) * 100


def test_round_to_rupee_half_up_away_from_zero() -> None:
    """NIT-1: ties round AWAY from zero (ROUND_HALF_UP), both signs."""
    assert _round_to_rupee(50) == 100  # +0.5 rupee → +1
    assert _round_to_rupee(49) == 0  # just below half → 0
    assert _round_to_rupee(51) == 100  # just above half → 1
    assert _round_to_rupee(150) == 200  # +1.5 → +2
    assert _round_to_rupee(0) == 0  # exact zero
    assert _round_to_rupee(-50) == -100  # -0.5 → -1 (away from zero)
    assert _round_to_rupee(-150) == -200  # -1.5 → -2
    assert _round_to_rupee(-49) == 0  # just-above -half → 0
    assert _round_to_rupee(-51) == -100  # just-below -half → -1


def test_round_levy_paisa_precise_arm_is_round_to_paisa() -> None:
    """`worst_case=False` IS the legacy nearest rounding — byte-identical."""
    for x in (0, 1, 4_999_999, 5_000_000, 5_000_001, 15_000_000, 123_456_789, -5_000_000):
        assert _round_levy_paisa(x, worst_case=False) == _round_to_paisa(x)


def test_round_levy_paisa_worst_arm_ceils() -> None:
    """The adverse (ceiling) arm — DEC-FILL-WORST-CASE-001 fix round 2.

    Any fractional paisa rounds UP (a levy is a charge — adverse = larger);
    an exact multiple is untouched; the ceiling is toward +∞ for any sign
    (totality — the live calculator never feeds a negative product).
    """
    assert _round_levy_paisa(0, worst_case=True) == 0
    assert _round_levy_paisa(1, worst_case=True) == 1  # any fraction → up
    assert _round_levy_paisa(4_999_999, worst_case=True) == 1  # nearest says 0
    assert _round_levy_paisa(5_000_000, worst_case=True) == 1
    assert _round_levy_paisa(10_000_000, worst_case=True) == 1  # exact multiple
    assert _round_levy_paisa(10_000_001, worst_case=True) == 2
    assert _round_levy_paisa(-1, worst_case=True) == 0  # toward +∞
    assert _round_levy_paisa(-10_000_000, worst_case=True) == -1


def test_round_levy_rupee_precise_arm_is_round_to_rupee() -> None:
    """`worst_case=False` IS the legacy nearest statutory rounding."""
    for x in (0, 1, 49, 50, 51, 150, 1_149, 1_150, 12_345, -50):
        assert _round_levy_rupee(x, worst_case=False) == _round_to_rupee(x)


def test_round_levy_rupee_worst_arm_ceils() -> None:
    """The adverse (ceiling) statutory arm — the refuter's boundary killed.

    1,149 paisa is the refuter counterexample's worst-case STT raw value:
    nearest rounds it DOWN to 1,100 (the ₹1 drop that beat the 25-paisa
    gross drop); the ceiling keeps it at 1,200.
    """
    assert _round_levy_rupee(0, worst_case=True) == 0
    assert _round_levy_rupee(1, worst_case=True) == 100  # any fraction → up
    assert _round_levy_rupee(49, worst_case=True) == 100  # nearest says 0
    assert _round_levy_rupee(100, worst_case=True) == 100  # exact multiple
    assert _round_levy_rupee(101, worst_case=True) == 200
    assert _round_levy_rupee(1_149, worst_case=True) == 1_200  # the boundary
    assert _round_levy_rupee(-1, worst_case=True) == 0  # toward +∞
    assert _round_levy_rupee(-100, worst_case=True) == -100


# ─────────────────────────────────────────────────────────────────
# Boundary LOTS / PRICES
# ─────────────────────────────────────────────────────────────────


def test_boundary_one_lot_minimum() -> None:
    """Minimum n_lots = 1 produces n_shares == one lot."""
    r = compute_trade_cost(_make_trade(n_lots=1))
    assert r.n_shares == r.n_shares_per_lot


def test_boundary_large_lot_count() -> None:
    """A large lot count scales notional linearly; brokerage stays flat."""
    r = compute_trade_cost(_make_trade(n_lots=10_000))
    assert r.n_shares == 10_000 * r.n_shares_per_lot
    assert r.brokerage_paisa == 4_000  # per-order flat, lot-count independent


def test_boundary_minimum_valid_price_one_tick() -> None:
    """Both bars at exactly 1 tick (5 paise) — the minimum valid price.

    Entry fill floors at 1 tick (5 + 5 = 10, above floor); exit fill
    5 - 5 = 0 floors UP to 1 tick. No exception, all charges ≥ 0.
    """
    r = compute_trade_cost(_make_trade(entry_open_paisa=5, exit_open_paisa=5, side="long_call"))
    assert r.buy_fill_paisa == 10  # 5 + 5 tick
    assert r.sell_fill_paisa == 5  # floored from 0
    assert r.total_charges_paisa >= 0
    assert r.stt_paisa >= 0


def test_boundary_price_exactly_on_tick() -> None:
    """A price exactly on the tick grid slips by exactly ±1 tick."""
    r = compute_trade_cost(
        _make_trade(entry_open_paisa=10_000, exit_open_paisa=20_000, side="long_call")
    )
    assert r.buy_fill_paisa == 10_005
    assert r.sell_fill_paisa == 19_995


# ─────────────────────────────────────────────────────────────────
# STT regime-boundary timestamps (Oct-2024 + Apr-2026)
# ─────────────────────────────────────────────────────────────────


def test_stt_regime_boundary_oct_2024() -> None:
    """STT regime flips at 2024-10-01: 2024-09-30 → 0.0625%, 2024-10-01 → 0.10%."""
    assert (
        constants.stt_options_rate_bps_x100(date(2024, 9, 30))
        == constants.STT_OPTIONS_PRE_OCT_2024_BPS_X100
    )
    assert (
        constants.stt_options_rate_bps_x100(date(2024, 10, 1))
        == constants.STT_OPTIONS_OCT_2024_TO_MAR_2026_BPS_X100
    )


def test_stt_regime_boundary_apr_2026_flip() -> None:
    """STT regime flips at 2026-04-01: 2026-03-31 → 0.10%, 2026-04-01 → 0.15%."""
    assert (
        constants.stt_options_rate_bps_x100(date(2026, 3, 31))
        == constants.STT_OPTIONS_OCT_2024_TO_MAR_2026_BPS_X100
    )
    assert (
        constants.stt_options_rate_bps_x100(date(2026, 4, 1))
        == constants.STT_OPTIONS_POST_APR_2026_BPS_X100
    )


# ─────────────────────────────────────────────────────────────────
# Property test (CHANGE 5) — invariants over random valid TradeInputs
# ─────────────────────────────────────────────────────────────────


@pytest.mark.property
@settings(max_examples=300, deadline=None)
@given(
    entry_open=st.integers(min_value=5, max_value=5_000_000),
    exit_open=st.integers(min_value=5, max_value=5_000_000),
    n_lots=st.integers(min_value=1, max_value=5_000),
    instrument=st.sampled_from(["NIFTY", "BANKNIFTY", "SENSEX"]),
    side=st.sampled_from(["long_call", "long_put"]),
    broker=st.sampled_from(["groww", "zerodha"]),
    entry_dt=st.datetimes(
        # Floor at the verified ETC window start (DEC-ETC-REGIME-HISTORY-001):
        # a pre-2024-10-01 entry now REFUSES pricing — pinned by
        # test_property_pre_boundary_always_refuses below.
        min_value=datetime(2024, 10, 1, 9, 15),
        max_value=datetime(2026, 12, 31, 15, 20),
    ),
)
def test_property_cost_invariants(
    entry_open: int,
    exit_open: int,
    n_lots: int,
    instrument: Instrument,
    side: TradeSide,
    broker: Broker,
    entry_dt: datetime,
) -> None:
    """CHANGE 5: invariants over random valid NORMAL_CLOSE long trades.

    - total_charges == sum of the 7 components
    - net == gross - total
    - every component ≥ 0, total ≥ 0
    - fills ≥ 1 tick (the MINOR-3 floor)
    - rounding idempotence on the rupee-rounded statutory levies
    """
    r = compute_trade_cost(
        _make_trade(
            entry_dt=entry_dt,
            exit_dt=entry_dt,  # same instant is valid (exit >= entry)
            instrument=instrument,
            entry_open_paisa=entry_open,
            exit_open_paisa=exit_open,
            side=side,
            n_lots=n_lots,
            broker=broker,
        )
    )

    components = (
        r.brokerage_paisa,
        r.stt_paisa,
        r.exch_paisa,
        r.sebi_paisa,
        r.ipft_paisa,
        r.stamp_paisa,
        r.gst_paisa,
    )
    assert r.total_charges_paisa == sum(components)
    assert r.net_pnl_paisa == r.gross_pnl_paisa - r.total_charges_paisa
    assert all(c >= 0 for c in components)
    assert r.total_charges_paisa >= 0
    # MINOR-3 floor: no fill below one tick.
    assert r.buy_fill_paisa >= constants.TICK_PAISA
    assert r.sell_fill_paisa >= constants.TICK_PAISA
    # Rounding idempotence: re-rounding a rupee-rounded value is a no-op.
    assert _round_to_rupee(r.stt_paisa) == r.stt_paisa
    assert _round_to_rupee(r.gst_paisa) == r.gst_paisa
    assert _round_to_rupee(r.stamp_paisa) == r.stamp_paisa


# ─────────────────────────────────────────────────────────────────
# Guard completeness — every __post_init__ validation (CHANGE 5)
# ─────────────────────────────────────────────────────────────────


def test_trade_input_accepts_equal_entry_exit() -> None:
    """exit_dt_ist == entry_dt_ist is valid (the boundary of the >= rule)."""
    dt = datetime(2026, 5, 15, 10, 0)
    trade = TradeInput(
        entry_dt_ist=dt,
        exit_dt_ist=dt,
        instrument="NIFTY",
        option_type="CE",
        strike=2_500_000,
        expiry_date=date(2026, 5, 21),
        entry_bar_open_paisa=10_000,
        exit_bar_open_paisa=10_000,
        side="long_call",
    )
    assert trade.exit_dt_ist == trade.entry_dt_ist


def test_trade_input_accepts_min_valid_boundaries() -> None:
    """The minimum valid values (price 5, n_lots 1, strike 1) are accepted."""
    trade = TradeInput(
        entry_dt_ist=datetime(2026, 5, 15, 10, 0),
        exit_dt_ist=datetime(2026, 5, 15, 11, 0),
        instrument="NIFTY",
        option_type="CE",
        strike=1,
        expiry_date=date(2026, 5, 21),
        entry_bar_open_paisa=5,
        exit_bar_open_paisa=5,
        side="long_call",
        n_lots=1,
    )
    assert trade.entry_bar_open_paisa == 5
    assert trade.n_lots == 1
    assert trade.strike == 1


def test_trade_input_rejects_negative_lots() -> None:
    """n_lots < 0 (negative, not just zero) must raise."""
    with pytest.raises(ValueError, match="n_lots .* must be >= 1"):
        TradeInput(
            entry_dt_ist=datetime(2026, 5, 15, 10, 0),
            exit_dt_ist=datetime(2026, 5, 15, 11, 0),
            instrument="NIFTY",
            option_type="CE",
            strike=2_500_000,
            expiry_date=date(2026, 5, 21),
            entry_bar_open_paisa=10_000,
            exit_bar_open_paisa=10_000,
            side="long_call",
            n_lots=-3,
        )


def test_trade_input_rejects_negative_strike() -> None:
    """strike < 0 (negative) must raise (the < 1 guard)."""
    with pytest.raises(ValueError, match="strike .* must be >= 1"):
        TradeInput(
            entry_dt_ist=datetime(2026, 5, 15, 10, 0),
            exit_dt_ist=datetime(2026, 5, 15, 11, 0),
            instrument="NIFTY",
            option_type="CE",
            strike=-1,
            expiry_date=date(2026, 5, 21),
            entry_bar_open_paisa=10_000,
            exit_bar_open_paisa=10_000,
            side="long_call",
        )


# ─────────────────────────────────────────────────────────────────
# DEC-FILL-WORST-CASE-001 — the worst-case fill arm (_apply_slippage +
# TradeInput extremes + compute_trade_cost threading)
# ─────────────────────────────────────────────────────────────────


class TestWorstCaseApplySlippage:
    """The adverse-extreme ± the SAME tick arm, all four legs."""

    def test_worst_case_long_entry_buys_high_exit_sells_low(self) -> None:
        # long: buy = entry HIGH + tick; sell = exit LOW − tick.
        buy, sell, slip = _apply_slippage(
            12_000,
            15_000,
            "long_call",
            fill_mode=FillMode.WORST_CASE,
            entry_high_paisa=12_100,
            entry_low_paisa=11_900,
            exit_high_paisa=15_100,
            exit_low_paisa=14_900,
        )
        assert (buy, sell, slip) == (12_105, 14_895, 2 * constants.TICK_PAISA)

    def test_worst_case_short_entry_sells_low_exit_buys_high(self) -> None:
        # short (production-unreachable under DEC-EXEC-008, forward-compat):
        # opening SELL = entry LOW − tick; cover BUY = exit HIGH + tick.
        buy, sell, slip = _apply_slippage(
            12_000,
            15_000,
            "short_call",
            fill_mode=FillMode.WORST_CASE,
            entry_high_paisa=12_100,
            entry_low_paisa=11_900,
            exit_high_paisa=15_100,
            exit_low_paisa=14_900,
        )
        assert (buy, sell, slip) == (15_105, 11_895, 2 * constants.TICK_PAISA)

    def test_flat_bar_worst_case_equals_precise_never_better(self) -> None:
        # THE NAMED FLAT-BAR COUNTEREXAMPLE: high == open ⇒ a bare buy-at-HIGH
        # would fill BELOW today's open + tick; the kept +tick makes the flat
        # bar EXACTLY equal — worst never beats precise (both legs, both
        # sides).
        for side in ("long_call", "short_put"):
            precise = _apply_slippage(12_000, 15_000, side)
            worst_flat = _apply_slippage(
                12_000,
                15_000,
                side,
                fill_mode=FillMode.WORST_CASE,
                entry_high_paisa=12_000,
                entry_low_paisa=12_000,
                exit_high_paisa=15_000,
                exit_low_paisa=15_000,
            )
            assert worst_flat == precise, side

    def test_worst_case_sub_tick_low_floors_never_crashes(self) -> None:
        # A degenerate sub-tick exit LOW floors the sell at one tick; the
        # realized slippage reads the floored truth (sell leg moved 0…
        # here 3−5 < 5 ⇒ floored; |3 − 5| = 2 realized on that leg).
        buy, sell, slip = _apply_slippage(
            30,
            10,
            "long_put",
            fill_mode=FillMode.WORST_CASE,
            entry_high_paisa=40,
            entry_low_paisa=25,
            exit_high_paisa=12,
            exit_low_paisa=3,
        )
        assert (buy, sell) == (45, constants.TICK_PAISA)
        assert slip == constants.TICK_PAISA + abs(3 - constants.TICK_PAISA)

    def test_worst_case_long_missing_anchors_fail_loud(self) -> None:
        with pytest.raises(ValueError, match="worst_case .long. needs"):
            _apply_slippage(12_000, 15_000, "long_call", fill_mode=FillMode.WORST_CASE)
        with pytest.raises(ValueError, match="entry_high_paisa"):
            _apply_slippage(
                12_000,
                15_000,
                "long_call",
                fill_mode=FillMode.WORST_CASE,
                entry_high_paisa=12_100,  # exit_low missing
            )

    def test_worst_case_short_missing_anchors_fail_loud(self) -> None:
        with pytest.raises(ValueError, match="worst_case .short. needs"):
            _apply_slippage(12_000, 15_000, "short_call", fill_mode=FillMode.WORST_CASE)
        with pytest.raises(ValueError, match="entry_low_paisa"):
            _apply_slippage(
                12_000,
                15_000,
                "short_call",
                fill_mode=FillMode.WORST_CASE,
                exit_high_paisa=15_100,  # entry_low missing
            )

    def test_precise_ignores_supplied_extremes(self) -> None:
        assert _apply_slippage(
            12_000,
            15_000,
            "long_call",
            entry_high_paisa=99_000,
            entry_low_paisa=1_000,
            exit_high_paisa=99_000,
            exit_low_paisa=1_000,
        ) == _apply_slippage(12_000, 15_000, "long_call")


_WORST_EXTREMES: dict[str, int] = {
    "entry_bar_high_paisa": 10_100,
    "entry_bar_low_paisa": 9_900,
    "exit_bar_high_paisa": 12_100,
    "exit_bar_low_paisa": 11_900,
}


def _worst_trade(**overrides: object) -> TradeInput:
    kwargs: dict[str, object] = {
        "entry_dt_ist": datetime(2026, 5, 15, 10, 0),
        "exit_dt_ist": datetime(2026, 5, 15, 11, 0),
        "instrument": "NIFTY",
        "option_type": "CE",
        "strike": 2_500_000,
        "expiry_date": date(2026, 5, 21),
        "entry_bar_open_paisa": 9_995,
        "exit_bar_open_paisa": 12_005,
        "side": "long_call",
        "fill_mode": FillMode.WORST_CASE,
        **_WORST_EXTREMES,
    }
    kwargs.update(overrides)
    return TradeInput(**kwargs)  # type: ignore[arg-type]


class TestWorstCaseTradeInput:
    def test_legacy_construction_is_precise_with_no_extremes(self) -> None:
        # The additive-defaulted-field pattern: pre-worst-case constructions
        # are byte-identical (PRECISE, extremes None).
        t = _make_trade()
        assert t.fill_mode is FillMode.PRECISE
        assert t.entry_bar_high_paisa is None
        assert t.exit_bar_low_paisa is None

    def test_worst_case_requires_all_four_extremes(self) -> None:
        with pytest.raises(ValueError, match="missing .'entry_bar_high_paisa'"):
            _worst_trade(
                entry_bar_high_paisa=None,
                entry_bar_low_paisa=None,
                exit_bar_high_paisa=None,
                exit_bar_low_paisa=None,
            )
        with pytest.raises(ValueError, match="missing .'exit_bar_low_paisa'"):
            _worst_trade(exit_bar_low_paisa=None)

    def test_junk_fill_mode_string_fails_loud(self) -> None:
        # A junk string would compare `is FillMode.WORST_CASE` False and
        # SILENTLY price PRECISE — the trap the runtime guard closes.
        with pytest.raises(ValueError, match="not a FillMode member"):
            _worst_trade(fill_mode="worst_case")

    @pytest.mark.parametrize("label", ["entry", "exit"])
    def test_extreme_ordering_validated_against_open(self, label: str) -> None:
        with pytest.raises(ValueError, match=f"{label}_bar_high_paisa"):
            _worst_trade(**{f"{label}_bar_high_paisa": 1})
        with pytest.raises(ValueError, match=f"{label}_bar_low_paisa"):
            _worst_trade(**{f"{label}_bar_low_paisa": 99_999_999})

    def test_worst_case_with_extremes_constructs(self) -> None:
        assert _worst_trade().fill_mode is FillMode.WORST_CASE


class TestWorstCaseComputeTradeCost:
    def test_worst_case_threads_through_compute_trade_cost(self) -> None:
        # Same trade, PRECISE vs WORST_CASE: fills move to the extremes ±
        # tick; the worst net can never beat the precise net.
        precise = compute_trade_cost(_worst_trade(fill_mode=FillMode.PRECISE))
        worst = compute_trade_cost(_worst_trade())
        assert (precise.buy_fill_paisa, precise.sell_fill_paisa) == (10_000, 12_000)
        assert (worst.buy_fill_paisa, worst.sell_fill_paisa) == (10_105, 11_895)
        assert worst.net_pnl_paisa < precise.net_pnl_paisa

    def test_worst_case_flat_bars_fills_identical_levies_ceiled(self) -> None:
        # FIX-ROUND-2 REFRAME (the adverse-levy-rounding law, MIGRATION.md
        # #1798): the original `worst == precise` byte-identity on flat bars
        # is DEAD — the fills (and hence notionals + gross) stay identical
        # (the +tick discipline), but under WORST_CASE every nearest-rounded
        # levy is CEIL'd, so the charge stack can only be HIGHER: here exch
        # 502 vs 501, ipft 8 vs 7, stamp 100 vs 0, GST 900 vs 800. The floor
        # (never-better) is what survives — pinned exactly so the ceil arm
        # has teeth at the calculator level too.
        flat = {
            "entry_bar_high_paisa": 9_995,
            "entry_bar_low_paisa": 9_995,
            "exit_bar_high_paisa": 12_005,
            "exit_bar_low_paisa": 12_005,
        }
        worst = compute_trade_cost(_worst_trade(**flat))
        precise = compute_trade_cost(_make_trade(entry_dt=datetime(2026, 5, 15, 10, 0)))
        assert (worst.buy_fill_paisa, worst.sell_fill_paisa) == (
            precise.buy_fill_paisa,
            precise.sell_fill_paisa,
        )
        assert worst.gross_pnl_paisa == precise.gross_pnl_paisa
        assert (worst.exch_paisa, precise.exch_paisa) == (502, 501)
        assert (worst.ipft_paisa, precise.ipft_paisa) == (8, 7)
        assert (worst.stamp_paisa, precise.stamp_paisa) == (100, 0)
        assert (worst.gst_paisa, precise.gst_paisa) == (900, 800)
        assert (worst.stt_paisa, worst.sebi_paisa) == (precise.stt_paisa, precise.sebi_paisa)
        assert worst.total_charges_paisa == 6_712
        assert precise.total_charges_paisa == 6_510
        assert worst.net_pnl_paisa <= precise.net_pnl_paisa

    def test_worst_case_net_floor_refuter_boundary_regression(self) -> None:
        # THE FIX-ROUND-2 NAMED REGRESSION (refuter-confirmed MAJOR-1,
        # MIGRATION.md #1798): entry FLAT at ₹460.05, exit low ONE paisa
        # under the open, entry date 2024-10-03 (post-Oct-2024 STT regime,
        # lot 25). PRE-fix the worst-case sell notional — 25 paisa smaller —
        # crossed a ₹0.5 STT nearest-rupee boundary DOWNWARD (STT ₹11 vs
        # ₹12), so worst net −7,095 BEAT precise net −7,170 by 75 paisa.
        # With adverse (ceiling) levy rounding the worst STT stays ₹12 and
        # the floor holds: worst −7,297 <= precise −7,170. Exact pins.
        entry_dt = datetime(2024, 10, 3, 10, 0)
        precise = compute_trade_cost(
            _make_trade(
                entry_dt=entry_dt,
                expiry_date=date(2024, 10, 31),
                entry_open_paisa=46_005,
                exit_open_paisa=46_005,
            )
        )
        worst = compute_trade_cost(
            _worst_trade(
                entry_dt_ist=entry_dt,
                exit_dt_ist=entry_dt.replace(hour=11),
                expiry_date=date(2024, 10, 31),
                entry_bar_open_paisa=46_005,
                exit_bar_open_paisa=46_005,
                entry_bar_high_paisa=46_005,  # entry flat: buy fills equal
                entry_bar_low_paisa=46_005,
                exit_bar_high_paisa=46_005,
                exit_bar_low_paisa=46_004,  # exit low 1 paisa under the open
            )
        )
        assert (precise.buy_fill_paisa, precise.sell_fill_paisa) == (46_010, 46_000)
        assert (worst.buy_fill_paisa, worst.sell_fill_paisa) == (46_010, 45_999)
        # The boundary: precise STT round₹(1,150.0 → 1,150) = 1,200; the
        # worst raw 1,149 would round to 1,100 under nearest (the pre-fix
        # violation) — the ceiling keeps it 1,200.
        assert precise.stt_paisa == 1_200
        assert worst.stt_paisa == 1_200
        assert precise.net_pnl_paisa == -7_170
        assert worst.net_pnl_paisa == -7_297
        assert worst.net_pnl_paisa <= precise.net_pnl_paisa

    def test_worst_case_net_floor_boundary_scan(self) -> None:
        # FIX-ROUND-2 boundary-scan regression: several opens straddling
        # ₹0.5 statutory rounding boundaries (the refuter reported a
        # violation at EVERY ₹40 open boundary pre-fix — 46_005 ± k×4_000
        # paisa all crossed), each with the exit low 1 paisa under the open.
        # The adverse-levy-rounding law must hold the net floor at ALL of
        # them, both entry dates flat.
        for open_p in (42_005, 46_005, 50_005, 54_005, 58_005, 62_005):
            entry_dt = datetime(2024, 10, 3, 10, 0)
            precise = compute_trade_cost(
                _make_trade(
                    entry_dt=entry_dt,
                    expiry_date=date(2024, 10, 31),
                    entry_open_paisa=open_p,
                    exit_open_paisa=open_p,
                )
            )
            worst = compute_trade_cost(
                _worst_trade(
                    entry_dt_ist=entry_dt,
                    exit_dt_ist=entry_dt.replace(hour=11),
                    expiry_date=date(2024, 10, 31),
                    entry_bar_open_paisa=open_p,
                    exit_bar_open_paisa=open_p,
                    entry_bar_high_paisa=open_p,
                    entry_bar_low_paisa=open_p,
                    exit_bar_high_paisa=open_p,
                    exit_bar_low_paisa=open_p - 1,
                )
            )
            assert worst.net_pnl_paisa <= precise.net_pnl_paisa, open_p
