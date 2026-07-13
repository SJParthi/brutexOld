"""Unit tests for the futures cost stack (DEC-FUTURES-EXEC-TRANSLATION-001).

Pins the R15 fill law, the VERIFIED-rates-only refusal discipline (the
DEC-ETC-REGIME-HISTORY-001 precedent), the cost-free index scope, and the full
charge arithmetic via a SYNTHETIC circular-cited RegimeRow fixture — never a
shipped unverified production rate. Every golden number is recomputed by hand
in the comments.
"""

from __future__ import annotations

import inspect
from datetime import date, datetime
from typing import ClassVar
from zoneinfo import ZoneInfo

import pytest
from brutex.costs import futures_costs as fc
from brutex.costs.calculator import _apply_slippage
from brutex.costs.constants import (
    GROWW_BROKERAGE_PAISA,
    TICK_PAISA,
    ZERODHA_BROKERAGE_PAISA,
    RegimeRow,
    validate_regime_table,
)
from brutex.costs.futures_costs import (
    FuturesCostBreakdown,
    FuturesTradeInput,
    apply_futures_fill,
    compute_futures_trade_cost,
    futures_brokerage_leg_paisa,
    futures_etc_unverified_windows,
    futures_exchange_charge_bps_x100,
    futures_exchange_for_underlying,
    ipft_futures_bps_x100,
    is_cost_free_future_underlying,
    stamp_futures_rate_bps_x100,
    stt_futures_rate_bps_x100,
)
from brutex.costs.types import UnverifiedCostRegimeError
from brutex.execution.fill_mode import FillMode

_IST = ZoneInfo("Asia/Kolkata")
_ENTRY = datetime(2025, 1, 2, 9, 16, tzinfo=_IST)
_EXIT = datetime(2025, 1, 2, 15, 20, tzinfo=_IST)
_EXPIRY = date(2025, 1, 30)

#: A synthetic circular-cited table (the sanctioned coverage fixture — the
#: production tables stay all-refusal; DEC-ETC-REGIME-HISTORY-001 §9 allows a
#: rate ONLY as cited DATA, which a test fixture models without shipping one).
_SYNTH_ETC = validate_regime_table(
    (RegimeRow(date(2024, 10, 1), 173, "SYNTHETIC test circular — never shipped"),),
    label="synthetic ETC futures",
)
_SYNTH_IPFT = validate_regime_table(
    (RegimeRow(date(2024, 10, 1), 10, "SYNTHETIC test circular — never shipped"),),
    label="synthetic IPFT futures",
)
_SYNTH_STAMP = validate_regime_table(
    (RegimeRow(date(2024, 10, 1), 200, "SYNTHETIC test circular — never shipped"),),
    label="synthetic stamp futures",
)


def _trade(**overrides: object) -> FuturesTradeInput:
    """A valid NIFTY long trade; kwargs override (a rename fails loud)."""
    kwargs: dict[str, object] = {
        "entry_dt_ist": _ENTRY,
        "exit_dt_ist": _EXIT,
        "instrument": "NIFTY",
        "contract_symbol": "NSE-NIFTY-30Jan25-FUT",
        "expiry_date": _EXPIRY,
        "entry_bar_open_paisa": 2_200_000,  # ₹22,000.00
        "exit_bar_open_paisa": 2_210_000,  # ₹22,100.00
        "side": "long",
        "lot_size": 75,
    }
    kwargs.update(overrides)
    return FuturesTradeInput(**kwargs)  # type: ignore[arg-type]


class TestScopeAndVenue:
    def test_index_futures_underlyings_are_the_three_indices(self) -> None:
        assert frozenset({"NIFTY", "BANKNIFTY", "SENSEX"}) == fc.INDEX_FUTURES_UNDERLYINGS

    @pytest.mark.parametrize("label", ["NIFTY", "BANKNIFTY", "SENSEX"])
    def test_index_future_is_cost_free(self, label: str) -> None:
        assert is_cost_free_future_underlying(label)

    @pytest.mark.parametrize("label", ["RELIANCE", "M&M", "", "nifty"])
    def test_non_index_future_is_cost_bearing(self, label: str) -> None:
        assert not is_cost_free_future_underlying(label)

    def test_exchange_resolution_indices(self) -> None:
        assert futures_exchange_for_underlying("NIFTY") == "NSE"
        assert futures_exchange_for_underlying("BANKNIFTY") == "NSE"
        assert futures_exchange_for_underlying("SENSEX") == "BSE"

    def test_exchange_resolution_stock_defaults_nse(self) -> None:
        # The single-stock F&O venue (CLAUDE.md §2 Rule 3 / DEC-P0-063).
        assert futures_exchange_for_underlying("RELIANCE") == "NSE"


class TestR15ApplyFuturesFill:
    def test_r15_fill_open_plus_minus_one_tick_long(self) -> None:
        # [R15] long: buy = entry open + tick; sell = exit open − tick.
        buy, sell, slip = apply_futures_fill(2_200_000, 2_210_000, side="long")
        assert (buy, sell, slip) == (2_200_005, 2_209_995, 2 * TICK_PAISA)

    def test_r15_fill_adverse_short(self) -> None:
        # [R15] short: opening SELL = entry − tick (adverse LOWER); the cover
        # BUY = exit + tick (adverse HIGHER) — adverse on BOTH legs.
        buy, sell, slip = apply_futures_fill(2_200_000, 2_210_000, side="short")
        assert (buy, sell, slip) == (2_210_005, 2_199_995, 2 * TICK_PAISA)

    def test_r15_fill_long_parity_with_apply_slippage(self) -> None:
        # [R15/COMMON] the LONG branch is byte-equal to the options
        # calculator's long slippage (golden parity across a grid).
        for entry, exit_ in ((5, 5), (10, 5), (2_200_000, 2_210_000), (123_455, 5)):
            assert apply_futures_fill(entry, exit_, side="long") == _apply_slippage(
                entry, exit_, "long"
            )

    def test_r15_short_does_not_inherit_check_sign_ambiguity(self) -> None:
        # [R15] the futures short sells the ENTRY leg ADVERSE at entry − tick,
        # computed adverse-both-sides directly (never by delegating to the
        # calculator's short branch — mechanism independence AST-pinned in the
        # R45 test). After DEC-TRACK2-COSTS-SPEC-RECONCILE-001 (MIGRATION.md
        # #1552) the calculator's own '# CHECK SIGN' short ambiguity was
        # resolved to the §8 ADVERSE rule (entry + tick → entry − tick), so the
        # calculator's short sell now AGREES with the futures adverse fill.
        _cal_buy, cal_sell, _ = _apply_slippage(2_200_000, 2_210_000, "short_call")
        fut_buy, fut_sell, _ = apply_futures_fill(2_200_000, 2_210_000, side="short")
        assert cal_sell == 2_199_995  # adverse (reconciled — DEC-TRACK2-COSTS-SPEC-RECONCILE-001)
        assert fut_sell == 2_199_995  # adverse — computed independently
        assert fut_buy == 2_210_005

    def test_fill_floors_at_one_tick_long_exit(self) -> None:
        # A one-tick exit open would sell at ₹0.00 without the floor.
        buy, sell, slip = apply_futures_fill(100, TICK_PAISA, side="long")
        assert (buy, sell) == (105, TICK_PAISA)
        assert slip == TICK_PAISA  # the floored sell leg truly moved 0

    def test_fill_floors_at_one_tick_short_entry(self) -> None:
        # A one-tick entry open would open the short at ₹0.00 without the floor.
        buy, sell, slip = apply_futures_fill(TICK_PAISA, 100, side="short")
        assert (buy, sell) == (105, TICK_PAISA)
        assert slip == TICK_PAISA

    def test_unknown_side_fails_loud(self) -> None:
        with pytest.raises(ValueError, match="side 'banana'"):
            apply_futures_fill(100, 200, side="banana")  # type: ignore[arg-type]

    def test_r15_zero_range_bar_degenerates(self) -> None:
        # [R15] O==H==L==C degenerates cleanly to that single price ± tick.
        buy, sell, slip = apply_futures_fill(100_000, 100_000, side="long")
        assert (buy, sell, slip) == (100_005, 99_995, 2 * TICK_PAISA)


class TestBrokerage:
    def test_groww_min_cap_binds_on_real_notional(self) -> None:
        # 0.03% of ₹650,025.00 = ₹195.01 ≫ ₹20 → the flat cap binds.
        assert futures_brokerage_leg_paisa(65_002_500, "groww") == GROWW_BROKERAGE_PAISA

    def test_groww_turnover_arm_binds_on_tiny_notional(self) -> None:
        # 0.03% of ₹100.00 (10_000 paisa) = 3 paisa < ₹20 → the turnover arm.
        # 10_000 × 3_000 = 30_000_000; round_to_paisa → (30M+5M)//10M = 3.
        assert futures_brokerage_leg_paisa(10_000, "groww") == 3

    def test_zerodha_flat_per_repo_row(self) -> None:
        # COSTS_VERIFIED §8.2: flat ₹20 per executed F&O order.
        assert futures_brokerage_leg_paisa(10_000, "zerodha") == ZERODHA_BROKERAGE_PAISA


class TestFuturesRegimeTables:
    def test_stt_futures_regime_table(self) -> None:
        # [R14/R27] 0.02% @ 2024-10-01 · 0.05% @ 2026-04-01 · boundary-exact.
        assert stt_futures_rate_bps_x100(date(2024, 10, 1)) == 2_000
        assert stt_futures_rate_bps_x100(date(2026, 3, 31)) == 2_000
        assert stt_futures_rate_bps_x100(date(2026, 4, 1)) == 5_000

    def test_stt_futures_pre_boundary_refusal(self) -> None:
        # [R14] pre-2024-10-01 futures STT is UNVERIFIED → typed refusal; the
        # date.min anchor makes the pre-history ValueError unreachable.
        with pytest.raises(UnverifiedCostRegimeError, match="STT \\(futures turnover\\)"):
            stt_futures_rate_bps_x100(date(2024, 9, 30))
        with pytest.raises(UnverifiedCostRegimeError):
            stt_futures_rate_bps_x100(date(2020, 1, 1))

    @pytest.mark.parametrize(
        ("instrument", "exchange"), [("NIFTY", "NSE"), ("SENSEX", "BSE"), ("RELIANCE", "NSE")]
    )
    def test_futures_etc_all_windows_refuse(self, instrument: str, exchange: str) -> None:
        # [R14/honesty] no 'verified' futures ETC rate exists — EVERY window,
        # EVERY exchange refuses (this is the judge-pinned honesty core).
        for on_date in (date(2020, 1, 1), date(2024, 10, 1), date(2026, 7, 7)):
            with pytest.raises(UnverifiedCostRegimeError) as excinfo:
                futures_exchange_charge_bps_x100(instrument, on_date)
            assert excinfo.value.exchange == exchange

    def test_ipft_futures_nse_refuses_bse_assumed_zero(self) -> None:
        with pytest.raises(UnverifiedCostRegimeError, match="IPFT"):
            ipft_futures_bps_x100("NIFTY", date(2025, 1, 2))
        assert ipft_futures_bps_x100("SENSEX", date(2025, 1, 2)) == 0

    def test_stamp_futures_all_windows_refuse(self) -> None:
        for on_date in (date(2020, 1, 1), date(2026, 7, 7)):
            with pytest.raises(UnverifiedCostRegimeError, match="stamp duty \\(futures buy\\)"):
                stamp_futures_rate_bps_x100(on_date)

    def test_futures_regime_tables_validate_ascending(self) -> None:
        # [Golden#1] every shipped futures table passed import-time validation;
        # re-validate here so a future edit can never ship unsorted rows.
        for table, label in (
            (fc._STT_FUTURES_REGIMES, "STT futures"),
            (fc._NSE_ETC_FUTURES_REGIMES, "NSE ETC futures"),
            (fc._BSE_ETC_FUTURES_REGIMES, "BSE ETC futures"),
            (fc._NSE_IPFT_FUTURES_REGIMES, "NSE IPFT futures"),
            (fc._STAMP_FUTURES_REGIMES, "stamp futures"),
        ):
            assert validate_regime_table(table, label=label) is table
            assert table[0].start == date.min  # pre-history ValueError unreachable

    def test_futures_unverified_windows_derived_from_data(self) -> None:
        # [R14/honesty] the refusal spans derive from the tables, not prose.
        windows = futures_etc_unverified_windows()
        assert windows == {
            "NSE": ((date.min, None),),
            "BSE": ((date.min, None),),
        }

    def test_no_runtime_escape_hatch_for_refusal(self) -> None:
        # [R14] LOCKED: no flag/kwarg can bypass a refusal window — every rate
        # accessor's signature carries ONLY the identity/date inputs.
        assert list(inspect.signature(stt_futures_rate_bps_x100).parameters) == ["entry_date"]
        assert list(inspect.signature(futures_exchange_charge_bps_x100).parameters) == [
            "instrument",
            "on_date",
        ]
        assert list(inspect.signature(ipft_futures_bps_x100).parameters) == [
            "instrument",
            "on_date",
        ]
        assert list(inspect.signature(stamp_futures_rate_bps_x100).parameters) == ["entry_date"]
        assert list(inspect.signature(compute_futures_trade_cost).parameters) == ["trade"]

    def test_no_rate_sourced_from_deprecated_execution_costs(self) -> None:
        # [honesty/AST] the futures cost stack never imports the deprecated
        # execution/costs.py (its 0.0125% futures STT is the WRONG era).
        source = inspect.getsource(fc)
        assert "execution.costs" not in source
        assert "execution import costs" not in source


class TestFuturesTradeInputValidation:
    def test_valid_trade_constructs(self) -> None:
        assert _trade().lot_size == 75

    def test_exit_before_entry_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be >= entry_dt_ist"):
            _trade(exit_dt_ist=_ENTRY, entry_dt_ist=_EXIT)

    def test_overnight_trade_rejected(self) -> None:
        # R18: intraday-only — the same-IST-date invariant also guarantees no
        # trade can straddle its contract's expiry (ROLL pin support).
        with pytest.raises(ValueError, match="intraday-only"):
            _trade(exit_dt_ist=datetime(2025, 1, 3, 9, 16, tzinfo=_IST))

    def test_blank_instrument_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-blank underlying"):
            _trade(instrument="  ")

    def test_blank_contract_symbol_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-blank groww_symbol"):
            _trade(contract_symbol=" ")

    def test_dead_contract_rejected(self) -> None:
        with pytest.raises(ValueError, match="dead contract"):
            _trade(expiry_date=date(2025, 1, 1))

    def test_sub_tick_opens_rejected(self) -> None:
        with pytest.raises(ValueError, match="entry_bar_open_paisa"):
            _trade(entry_bar_open_paisa=4)
        with pytest.raises(ValueError, match="exit_bar_open_paisa"):
            _trade(exit_bar_open_paisa=0)

    def test_bad_sizes_rejected(self) -> None:
        with pytest.raises(ValueError, match="lot_size"):
            _trade(lot_size=0)
        with pytest.raises(ValueError, match="n_lots"):
            _trade(n_lots=0)

    @pytest.mark.parametrize(
        ("field", "value"),
        [("side", "banana"), ("broker", "robinhood"), ("gst_split", "offshore")],
    )
    def test_bad_enum_strings_rejected(self, field: str, value: str) -> None:
        with pytest.raises(ValueError, match=f"{field} '{value}'"):
            _trade(**{field: value})


class TestBreakdownInvariants:
    def test_net_consistency_enforced(self) -> None:
        with pytest.raises(ValueError, match="net_pnl_paisa"):
            FuturesCostBreakdown(
                n_shares_per_lot=75,
                n_shares=75,
                buy_fill_paisa=100,
                sell_fill_paisa=200,
                buy_notional_paisa=7_500,
                sell_notional_paisa=15_000,
                brokerage_paisa=0,
                stt_paisa=0,
                exch_paisa=0,
                sebi_paisa=0,
                ipft_paisa=0,
                stamp_paisa=0,
                gst_paisa=0,
                slippage_paisa=0,
                total_charges_paisa=0,
                gross_pnl_paisa=7_500,
                net_pnl_paisa=1,  # != gross - charges
                cost_free=True,
            )

    def test_non_positive_shares_rejected(self) -> None:
        with pytest.raises(ValueError, match="n_shares"):
            FuturesCostBreakdown(
                n_shares_per_lot=0,
                n_shares=0,
                buy_fill_paisa=100,
                sell_fill_paisa=200,
                buy_notional_paisa=0,
                sell_notional_paisa=0,
                brokerage_paisa=0,
                stt_paisa=0,
                exch_paisa=0,
                sebi_paisa=0,
                ipft_paisa=0,
                stamp_paisa=0,
                gst_paisa=0,
                slippage_paisa=0,
                total_charges_paisa=0,
                gross_pnl_paisa=0,
                net_pnl_paisa=0,
                cost_free=True,
            )


class TestCostFreeIndexPath:
    def test_r11_r27_cost_free_index_future_zero_charges(self) -> None:
        # [R11/R27] golden, recomputed by hand: NIFTY long 22,000.00 → 22,100.00,
        # lot 75. buy = 2,200,005; sell = 2,209,995; gross = 9,990 × 75 = 749,250.
        out = compute_futures_trade_cost(_trade())
        assert out.cost_free is True
        assert (out.buy_fill_paisa, out.sell_fill_paisa) == (2_200_005, 2_209_995)
        assert out.n_shares == 75
        assert out.gross_pnl_paisa == 749_250
        assert out.total_charges_paisa == 0
        assert out.net_pnl_paisa == 749_250  # net == gross (signal-only decree)
        assert (
            out.brokerage_paisa
            == out.stt_paisa
            == out.exch_paisa
            == out.sebi_paisa
            == out.ipft_paisa
            == out.stamp_paisa
            == out.gst_paisa
            == 0
        )
        assert out.slippage_paisa == 2 * TICK_PAISA * 75

    def test_cost_free_short_direction(self) -> None:
        # Short the same move: sell = 2,199,995; buy = 2,210,005;
        # gross = (2,199,995 − 2,210,005) × 75 = −750,750.
        out = compute_futures_trade_cost(_trade(side="short"))
        assert out.cost_free is True
        assert (out.buy_fill_paisa, out.sell_fill_paisa) == (2_210_005, 2_199_995)
        assert out.gross_pnl_paisa == -750_750
        assert out.net_pnl_paisa == -750_750

    def test_cost_free_never_touches_refusal_tables(self) -> None:
        # A pre-2024-10-01 INDEX trade prices fine — the cost-free arm never
        # consults the STT/ETC tables (the signal-only decree; the refusals
        # guard the COST-BEARING arm only).
        early_entry = datetime(2021, 6, 1, 9, 16, tzinfo=_IST)
        early_exit = datetime(2021, 6, 1, 15, 20, tzinfo=_IST)
        out = compute_futures_trade_cost(
            _trade(entry_dt_ist=early_entry, exit_dt_ist=early_exit, expiry_date=date(2021, 6, 24))
        )
        assert out.cost_free is True
        assert out.total_charges_paisa == 0


class TestCostBearingRefusals:
    def test_cost_bearing_etc_refusal_2025(self) -> None:
        # [R14/R27] a cost-bearing stock future on a verified-STT date still
        # refuses at the ETC step (every futures-ETC window refuses).
        with pytest.raises(UnverifiedCostRegimeError, match="exchange transaction charge"):
            compute_futures_trade_cost(_trade(instrument="RELIANCE", lot_size=500))

    def test_cost_bearing_stt_refusal_pre_oct_2024(self) -> None:
        # Pre-boundary the STT step refuses FIRST (step order: STT before ETC).
        entry = datetime(2024, 9, 2, 9, 16, tzinfo=_IST)
        exit_ = datetime(2024, 9, 2, 15, 20, tzinfo=_IST)
        with pytest.raises(UnverifiedCostRegimeError, match="STT \\(futures turnover\\)"):
            compute_futures_trade_cost(
                _trade(
                    instrument="RELIANCE",
                    lot_size=500,
                    entry_dt_ist=entry,
                    exit_dt_ist=exit_,
                    expiry_date=date(2024, 9, 26),
                )
            )

    def test_cost_bearing_ipft_refusal_after_synthetic_etc(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # With ONLY the ETC synthetically cited, the NSE IPFT refusal is the
        # next honest stop (defence-in-depth ordering).
        monkeypatch.setitem(fc._ETC_FUTURES_REGIMES_BY_EXCHANGE, "NSE", _SYNTH_ETC)
        with pytest.raises(UnverifiedCostRegimeError, match="IPFT"):
            compute_futures_trade_cost(_trade(instrument="RELIANCE", lot_size=500))

    def test_cost_bearing_stamp_refusal_after_synthetic_etc_ipft(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(fc._ETC_FUTURES_REGIMES_BY_EXCHANGE, "NSE", _SYNTH_ETC)
        monkeypatch.setattr(fc, "_NSE_IPFT_FUTURES_REGIMES", _SYNTH_IPFT)
        with pytest.raises(UnverifiedCostRegimeError, match="stamp duty"):
            compute_futures_trade_cost(_trade(instrument="RELIANCE", lot_size=500))


class TestChargePathViaSyntheticCitedFixture:
    def test_charge_path_100pct_via_synthetic_cited_regime_fixture(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # [R14/coverage] the FULL charge arithmetic, reachable only through a
        # synthetic circular-cited fixture (production tables stay refusal).
        # Hand-recomputed: RELIANCE long ₹1,300.00 → ₹1,310.00, lot 500,
        # entry 2025-01-02 (STT 0.02% VERIFIED; ETC 173 / IPFT 10 / stamp 200
        # SYNTHETIC):
        #   buy_fill 130,005 · sell_fill 130,995 · n_shares 500
        #   buy_notional 65,002,500 · sell_notional 65,497,500
        #   gross = 495,000
        #   brokerage: min(2000, 0.03%×notional≈₹195) = 2,000 per leg → 4,000
        #   STT: 65,497,500×2,000//1e7 = 13,099 → rupee-round → 13,100
        #   ETC: 1,125 + 1,133 = 2,258
        #   SEBI: 65 + 65 = 130 · IPFT: 65 + 65 = 130
        #   stamp: 65,002,500×200//1e7 = 1,300 → rupee-round → 1,300
        #   GST: 18% × (4,000+2,258+130+130=6,518) = 1,173 → rupee-round → 1,200
        #   total = 4,000+13,100+2,258+130+130+1,300+1,200 = 22,118
        #   net = 495,000 − 22,118 = 472,882
        monkeypatch.setitem(fc._ETC_FUTURES_REGIMES_BY_EXCHANGE, "NSE", _SYNTH_ETC)
        monkeypatch.setattr(fc, "_NSE_IPFT_FUTURES_REGIMES", _SYNTH_IPFT)
        monkeypatch.setattr(fc, "_STAMP_FUTURES_REGIMES", _SYNTH_STAMP)
        out = compute_futures_trade_cost(
            _trade(
                instrument="RELIANCE",
                lot_size=500,
                entry_bar_open_paisa=130_000,
                exit_bar_open_paisa=131_000,
            )
        )
        assert out.cost_free is False
        assert (out.buy_fill_paisa, out.sell_fill_paisa) == (130_005, 130_995)
        assert (out.buy_notional_paisa, out.sell_notional_paisa) == (65_002_500, 65_497_500)
        assert out.gross_pnl_paisa == 495_000
        assert out.brokerage_paisa == 4_000
        assert out.stt_paisa == 13_100
        assert out.exch_paisa == 2_258
        assert out.sebi_paisa == 130
        assert out.ipft_paisa == 130
        assert out.stamp_paisa == 1_300
        assert out.gst_paisa == 1_200
        assert out.total_charges_paisa == 22_118
        assert out.net_pnl_paisa == 472_882
        assert out.slippage_paisa == 2 * TICK_PAISA * 500
        assert out.lot_size_basis == "trade_date"

    def test_charge_path_bse_arm_ipft_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The BSE arm covers the assumed-0 IPFT branch inside the charge stack.
        # SENSEX is cost-FREE, so route a synthetic cost-bearing BSE label by
        # patching the scope set — narrow, test-only (production scope pinned
        # in TestScopeAndVenue).
        monkeypatch.setitem(fc._ETC_FUTURES_REGIMES_BY_EXCHANGE, "BSE", _SYNTH_ETC)
        monkeypatch.setattr(fc, "_STAMP_FUTURES_REGIMES", _SYNTH_STAMP)
        monkeypatch.setattr(fc, "INDEX_FUTURES_UNDERLYINGS", frozenset({"NIFTY", "BANKNIFTY"}))
        out = compute_futures_trade_cost(
            _trade(
                instrument="SENSEX",
                contract_symbol="BSE-SENSEX-28Jan25-FUT",
                lot_size=20,
                entry_bar_open_paisa=7_200_000,
                exit_bar_open_paisa=7_210_000,
            )
        )
        assert out.cost_free is False
        assert out.ipft_paisa == 0  # BSE: the assumed-0 options-precedent stance
        assert out.exch_paisa > 0


class TestWorstCaseApplyFuturesFill:
    """DEC-FILL-WORST-CASE-001 — the adverse-extreme ± the SAME tick arm."""

    def test_worst_case_long_fills_entry_high_exit_low(self) -> None:
        # long: buy = entry HIGH + tick; sell = exit LOW − tick.
        buy, sell, slip = apply_futures_fill(
            2_200_000,
            2_210_000,
            side="long",
            fill_mode=FillMode.WORST_CASE,
            entry_high_paisa=2_201_000,
            entry_low_paisa=2_199_000,
            exit_high_paisa=2_211_000,
            exit_low_paisa=2_209_000,
        )
        assert (buy, sell, slip) == (2_201_005, 2_208_995, 2 * TICK_PAISA)

    def test_worst_case_short_fills_entry_low_exit_high(self) -> None:
        # short: opening SELL = entry LOW − tick; cover BUY = exit HIGH + tick.
        buy, sell, slip = apply_futures_fill(
            2_200_000,
            2_210_000,
            side="short",
            fill_mode=FillMode.WORST_CASE,
            entry_high_paisa=2_201_000,
            entry_low_paisa=2_199_000,
            exit_high_paisa=2_211_000,
            exit_low_paisa=2_209_000,
        )
        assert (buy, sell, slip) == (2_211_005, 2_198_995, 2 * TICK_PAISA)

    def test_flat_bar_worst_case_equals_precise_never_better(self) -> None:
        # THE NAMED FLAT-BAR COUNTEREXAMPLE (DEC-FILL-WORST-CASE-001): on a
        # flat entry bar (high == open) a bare buy-at-HIGH would fill at
        # `high < open + tick` — BETTER than today's precise fill. Keeping the
        # +tick ON TOP of the extreme makes the flat bar EXACTLY EQUAL, so the
        # worst-case fill can never beat precise (tick discipline preserved).
        precise = apply_futures_fill(2_200_000, 2_210_000, side="long")
        worst_flat = apply_futures_fill(
            2_200_000,
            2_210_000,
            side="long",
            fill_mode=FillMode.WORST_CASE,
            entry_high_paisa=2_200_000,  # flat: high == open
            entry_low_paisa=2_200_000,
            exit_high_paisa=2_210_000,  # flat: low == open
            exit_low_paisa=2_210_000,
        )
        assert worst_flat == precise
        # And on a NON-flat bar worst is strictly adverse on both legs.
        worst_range = apply_futures_fill(
            2_200_000,
            2_210_000,
            side="long",
            fill_mode=FillMode.WORST_CASE,
            entry_high_paisa=2_200_500,
            entry_low_paisa=2_199_500,
            exit_high_paisa=2_210_500,
            exit_low_paisa=2_209_500,
        )
        assert worst_range[0] > precise[0]  # buy fill worse (higher)
        assert worst_range[1] < precise[1]  # sell fill worse (lower)

    def test_worst_case_short_flat_bar_equals_precise(self) -> None:
        precise = apply_futures_fill(2_200_000, 2_210_000, side="short")
        worst = apply_futures_fill(
            2_200_000,
            2_210_000,
            side="short",
            fill_mode=FillMode.WORST_CASE,
            entry_high_paisa=2_200_000,
            entry_low_paisa=2_200_000,
            exit_high_paisa=2_210_000,
            exit_low_paisa=2_210_000,
        )
        assert worst == precise

    def test_worst_case_floors_at_one_tick(self) -> None:
        # A one-tick exit LOW would sell at ₹0.00 without the floor; the
        # realized-slippage line reads the floored truth (sell leg moved 0).
        buy, sell, slip = apply_futures_fill(
            100,
            10,
            side="long",
            fill_mode=FillMode.WORST_CASE,
            entry_high_paisa=110,
            entry_low_paisa=95,
            exit_high_paisa=15,
            exit_low_paisa=TICK_PAISA,
        )
        assert (buy, sell) == (115, TICK_PAISA)
        assert slip == TICK_PAISA

    def test_worst_case_long_missing_anchors_fails_loud(self) -> None:
        # Never a silent PRECISE degrade: the side's two adverse anchors are
        # REQUIRED under worst_case.
        with pytest.raises(ValueError, match="worst_case .long. needs"):
            apply_futures_fill(100, 200, side="long", fill_mode=FillMode.WORST_CASE)
        with pytest.raises(ValueError, match="entry_high_paisa"):
            apply_futures_fill(
                100, 200, side="long", fill_mode=FillMode.WORST_CASE, exit_low_paisa=190
            )

    def test_worst_case_short_missing_anchors_fails_loud(self) -> None:
        with pytest.raises(ValueError, match="worst_case .short. needs"):
            apply_futures_fill(
                100, 200, side="short", fill_mode=FillMode.WORST_CASE, entry_high_paisa=110
            )

    def test_precise_ignores_supplied_extremes(self) -> None:
        # PRECISE with extremes present is byte-identical to PRECISE without
        # (the extremes are worst-case anchors, not precise inputs).
        assert apply_futures_fill(
            2_200_000,
            2_210_000,
            side="long",
            entry_high_paisa=2_290_000,
            entry_low_paisa=2_190_000,
            exit_high_paisa=2_290_000,
            exit_low_paisa=2_190_000,
        ) == apply_futures_fill(2_200_000, 2_210_000, side="long")


class TestWorstCaseTradeInput:
    """DEC-FILL-WORST-CASE-001 — FuturesTradeInput's additive fill-mode surface."""

    _EXTREMES: ClassVar[dict[str, object]] = {
        "entry_bar_high_paisa": 2_201_000,
        "entry_bar_low_paisa": 2_199_000,
        "exit_bar_high_paisa": 2_211_000,
        "exit_bar_low_paisa": 2_209_000,
    }

    def test_default_is_precise_with_no_extremes(self) -> None:
        # The additive-defaulted-field pattern: a pre-worst-case construction
        # is byte-identical (PRECISE, extremes None).
        t = _trade()
        assert t.fill_mode is FillMode.PRECISE
        assert t.entry_bar_high_paisa is None
        assert t.exit_bar_low_paisa is None

    def test_worst_case_requires_all_four_extremes(self) -> None:
        with pytest.raises(ValueError, match="missing .'entry_bar_high_paisa'"):
            _trade(fill_mode=FillMode.WORST_CASE)
        partial = dict(self._EXTREMES)
        partial.pop("exit_bar_low_paisa")
        with pytest.raises(ValueError, match="missing .'exit_bar_low_paisa'"):
            _trade(fill_mode=FillMode.WORST_CASE, **partial)

    def test_worst_case_with_extremes_constructs(self) -> None:
        t = _trade(fill_mode=FillMode.WORST_CASE, **self._EXTREMES)
        assert t.fill_mode is FillMode.WORST_CASE

    def test_junk_fill_mode_string_fails_loud(self) -> None:
        # A junk string would compare `is FillMode.WORST_CASE` False and
        # SILENTLY price PRECISE — the exact trap the guard closes.
        with pytest.raises(ValueError, match="not a FillMode member"):
            _trade(fill_mode="worst_case")

    @pytest.mark.parametrize("label", ["entry", "exit"])
    def test_extreme_ordering_validated_against_open(self, label: str) -> None:
        with pytest.raises(ValueError, match=f"{label}_bar_high_paisa"):
            _trade(**{f"{label}_bar_high_paisa": 1})
        with pytest.raises(ValueError, match=f"{label}_bar_low_paisa"):
            _trade(**{f"{label}_bar_low_paisa": 99_999_999})

    def test_sub_tick_low_extreme_is_legal(self) -> None:
        # A degenerate sub-tick LOW must NOT crash the boundary — the fill
        # floor absorbs it (never a sim crash on one degenerate bar).
        t = _trade(
            fill_mode=FillMode.WORST_CASE,
            entry_bar_open_paisa=10,
            exit_bar_open_paisa=10,
            entry_bar_high_paisa=20,
            entry_bar_low_paisa=1,
            exit_bar_high_paisa=20,
            exit_bar_low_paisa=1,
        )
        out = compute_futures_trade_cost(t)
        assert out.sell_fill_paisa == TICK_PAISA  # floored, not crashed


class TestWorstCaseComputeCost:
    def test_worst_case_threads_through_compute_futures_trade_cost(self) -> None:
        # Hand-recomputed (NIFTY long, cost-free index, lot 75):
        #   buy = entry HIGH 2,201,000 + 5 = 2,201,005
        #   sell = exit LOW 2,209,000 − 5 = 2,208,995
        #   gross = (2,208,995 − 2,201,005) × 75 = 7,990 × 75 = 599,250
        out = compute_futures_trade_cost(
            _trade(
                fill_mode=FillMode.WORST_CASE,
                entry_bar_high_paisa=2_201_000,
                entry_bar_low_paisa=2_199_000,
                exit_bar_high_paisa=2_211_000,
                exit_bar_low_paisa=2_209_000,
            )
        )
        assert (out.buy_fill_paisa, out.sell_fill_paisa) == (2_201_005, 2_208_995)
        assert out.gross_pnl_paisa == 599_250
        assert out.net_pnl_paisa == 599_250  # cost-free index: net == gross
        assert out.slippage_paisa == 2 * TICK_PAISA * 75

    def test_worst_case_never_beats_precise_on_the_same_bars(self) -> None:
        # The unconditional per-trade floor on this re-pricing layer: same
        # bars, same side — worst-case gross ≤ precise gross (equality only
        # on flat bars; here strict).
        precise = compute_futures_trade_cost(_trade())
        worst = compute_futures_trade_cost(
            _trade(
                fill_mode=FillMode.WORST_CASE,
                entry_bar_high_paisa=2_200_500,
                entry_bar_low_paisa=2_199_500,
                exit_bar_high_paisa=2_210_500,
                exit_bar_low_paisa=2_209_500,
            )
        )
        assert worst.gross_pnl_paisa < precise.gross_pnl_paisa
