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
    # DEC-FILL-WORST-CASE-ONLY-001: the four REQUIRED fill anchors default to
    # FLAT bars (extremes == the resolved opens) — the adverse-max fill is
    # then exactly ``open ± tick``, keeping the fixtures' fill literals
    # hand-derivable from their opens.
    for extreme, open_key in (
        ("entry_bar_high_paisa", "entry_bar_open_paisa"),
        ("entry_bar_low_paisa", "entry_bar_open_paisa"),
        ("exit_bar_high_paisa", "exit_bar_open_paisa"),
        ("exit_bar_low_paisa", "exit_bar_open_paisa"),
    ):
        kwargs.setdefault(extreme, kwargs[open_key])
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


class TestApplyFuturesFillLaw:
    """The ONE fill law: adverse extreme ± the SAME tick (flagless)."""

    def test_flat_bar_fill_is_open_plus_minus_one_tick_long(self) -> None:
        # Flat bars (extremes == opens): buy = entry + tick; sell = exit − tick.
        buy, sell, slip = apply_futures_fill(
            side="long",
            entry_high_paisa=2_200_000,
            entry_low_paisa=2_200_000,
            exit_high_paisa=2_210_000,
            exit_low_paisa=2_210_000,
        )
        assert (buy, sell, slip) == (2_200_005, 2_209_995, 2 * TICK_PAISA)

    def test_ranged_bar_fills_entry_high_exit_low_long(self) -> None:
        # long: buy = entry HIGH + tick; sell = exit LOW − tick.
        buy, sell, slip = apply_futures_fill(
            side="long",
            entry_high_paisa=2_201_000,
            entry_low_paisa=2_199_000,
            exit_high_paisa=2_211_000,
            exit_low_paisa=2_209_000,
        )
        assert (buy, sell, slip) == (2_201_005, 2_208_995, 2 * TICK_PAISA)

    def test_ranged_bar_fills_entry_low_exit_high_short(self) -> None:
        # short: opening SELL = entry LOW − tick; cover BUY = exit HIGH + tick
        # (adverse on BOTH legs — the resolved '# CHECK SIGN' law).
        buy, sell, slip = apply_futures_fill(
            side="short",
            entry_high_paisa=2_201_000,
            entry_low_paisa=2_199_000,
            exit_high_paisa=2_211_000,
            exit_low_paisa=2_209_000,
        )
        assert (buy, sell, slip) == (2_211_005, 2_198_995, 2 * TICK_PAISA)

    def test_long_parity_with_apply_slippage(self) -> None:
        # [COMMON] the LONG branch is byte-equal to the options calculator's
        # long slippage under the SAME four anchors (golden parity on a grid).
        for eh, el, xh, xl in (
            (5, 5, 5, 5),
            (10, 8, 5, 5),
            (2_201_000, 2_199_000, 2_211_000, 2_209_000),
            (123_455, 123_000, 10, 5),
        ):
            assert apply_futures_fill(
                side="long",
                entry_high_paisa=eh,
                entry_low_paisa=el,
                exit_high_paisa=xh,
                exit_low_paisa=xl,
            ) == _apply_slippage(
                "long",
                entry_high_paisa=eh,
                entry_low_paisa=el,
                exit_high_paisa=xh,
                exit_low_paisa=xl,
            )

    def test_fill_floors_at_one_tick_long_exit(self) -> None:
        # A one-tick exit LOW would sell at ₹0.00 without the floor.
        buy, sell, slip = apply_futures_fill(
            side="long",
            entry_high_paisa=100,
            entry_low_paisa=100,
            exit_high_paisa=TICK_PAISA,
            exit_low_paisa=TICK_PAISA,
        )
        assert (buy, sell) == (105, TICK_PAISA)
        assert slip == TICK_PAISA  # the floored sell leg truly moved 0

    def test_fill_floors_at_one_tick_short_entry(self) -> None:
        # A one-tick entry LOW would open the short at ₹0.00 without the floor.
        buy, sell, slip = apply_futures_fill(
            side="short",
            entry_high_paisa=TICK_PAISA,
            entry_low_paisa=TICK_PAISA,
            exit_high_paisa=100,
            exit_low_paisa=100,
        )
        assert (buy, sell) == (105, TICK_PAISA)
        assert slip == TICK_PAISA

    def test_unknown_side_fails_loud(self) -> None:
        with pytest.raises(ValueError, match="side 'banana'"):
            apply_futures_fill(
                side="banana",  # type: ignore[arg-type]
                entry_high_paisa=100,
                entry_low_paisa=100,
                exit_high_paisa=200,
                exit_low_paisa=200,
            )

    def test_no_mode_or_open_parameters_exist(self) -> None:
        # Killed-token pin: the fill law is flagless — no fill_mode, no
        # retired open anchors (DEC-FILL-WORST-CASE-ONLY-001).
        params = inspect.signature(apply_futures_fill).parameters
        assert "fill_mode" not in params
        assert "entry_open_paisa" not in params


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
        #   (every levy CEILs — the permanent adverse-rounding law,
        #   DEC-FILL-WORST-CASE-ONLY-001):
        #   STT: 65,497,500×2,000//1e7 = 13,099 → ceil₹ → 13,100
        #   ETC: ⌈1,124.54⌉ + ⌈1,133.11⌉ = 1,125 + 1,134 = 2,259
        #   SEBI: ⌈65.00⌉ + ⌈65.50⌉ = 66 + 66 = 132 · IPFT: same → 132
        #   stamp: 65,002,500×200//1e7 = 1,300 → ceil₹ → 1,300
        #   GST: 18% × (4,000+2,259+132+132=6,523) = 1,174 → ceil₹ → 1,200
        #   total = 4,000+13,100+2,259+132+132+1,300+1,200 = 22,123
        #   net = 495,000 − 22,123 = 472,877
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
        assert out.exch_paisa == 2_259
        assert out.sebi_paisa == 132
        assert out.ipft_paisa == 132
        assert out.stamp_paisa == 1_300
        assert out.gst_paisa == 1_200
        assert out.total_charges_paisa == 22_123
        assert out.net_pnl_paisa == 472_877
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


class TestExtremesRequired:
    """DEC-FILL-WORST-CASE-ONLY-001 — the REQUIRED FuturesTradeInput anchors."""

    def test_missing_extremes_refuse_fail_loud(self) -> None:
        # An input without its adverse anchors is unpriceable — the refusal
        # names the DEC and the missing fields (never a silent open fill).
        base: dict[str, object] = {
            "entry_dt_ist": _ENTRY,
            "exit_dt_ist": _EXIT,
            "instrument": "NIFTY",
            "contract_symbol": "NSE-NIFTY-30Jan25-FUT",
            "expiry_date": _EXPIRY,
            "entry_bar_open_paisa": 2_200_000,
            "exit_bar_open_paisa": 2_210_000,
            "side": "long",
            "lot_size": 75,
        }
        with pytest.raises(ValueError, match="DEC-FILL-WORST-CASE-ONLY-001"):
            FuturesTradeInput(**base)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="missing .'exit_bar_low_paisa'"):
            FuturesTradeInput(  # type: ignore[arg-type]
                **base,
                entry_bar_high_paisa=2_201_000,
                entry_bar_low_paisa=2_199_000,
                exit_bar_high_paisa=2_211_000,
            )

    def test_no_fill_mode_field_exists(self) -> None:
        # Killed-token pin: FuturesTradeInput carries no mode axis.
        import dataclasses

        assert "fill_mode" not in {f.name for f in dataclasses.fields(FuturesTradeInput)}

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
    def test_ranged_bars_thread_through_compute_futures_trade_cost(self) -> None:
        # Hand-recomputed (NIFTY long, cost-free index, lot 75):
        #   buy = entry HIGH 2,201,000 + 5 = 2,201,005
        #   sell = exit LOW 2,209,000 − 5 = 2,208,995
        #   gross = (2,208,995 − 2,201,005) × 75 = 7,990 × 75 = 599,250
        out = compute_futures_trade_cost(
            _trade(
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

    def test_ranged_bars_never_beat_flat_bars(self) -> None:
        # The per-trade floor on this re-pricing layer: same opens, a WIDER
        # fill bar can only fill WORSE — gross strictly below the flat-bar
        # gross (equality only when the extremes collapse onto the opens).
        flat = compute_futures_trade_cost(_trade())
        ranged = compute_futures_trade_cost(
            _trade(
                entry_bar_high_paisa=2_200_500,
                entry_bar_low_paisa=2_199_500,
                exit_bar_high_paisa=2_210_500,
                exit_bar_low_paisa=2_209_500,
            )
        )
        assert ranged.gross_pnl_paisa < flat.gross_pnl_paisa

    def test_cost_bearing_levies_round_adversely(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The PERMANENT adverse-levy-rounding law on the FULL cost-bearing
        # charge stack under the synthetic cited fixture — every levy CEILs,
        # so a rounding boundary can never make a worse fill pay LESS.
        # Hand-recomputed (RELIANCE long, lot 500, entry 2025-01-02):
        # FLAT bars (extremes == opens ₹1,300.00 / ₹1,310.00):
        #   buy 130,005 · sell 130,995 → notionals 65,002,500 / 65,497,500
        #   gross 495,000
        #   brokerage: flat ₹20/leg wins the min → 4,000
        #   STT(2000): 65,497,500×2000//1e7 = 13,099 → ceil₹ 13,100
        #   ETC(173): ⌈1,124.54⌉ + ⌈1,133.11⌉ = 1,125 + 1,134 = 2,259
        #   SEBI(10): ⌈65.00⌉ + ⌈65.50⌉ = 66 + 66 = 132
        #   IPFT(10): same shape → 132
        #   stamp(200): 65,002,500×200//1e7 = 1,300 → ceil₹ 1,300
        #   GST: base 4,000+2,259+132+132 = 6,523 → raw 1,174 → ceil₹ 1,200
        #   total 22,123 · net 495,000 − 22,123 = 472,877
        # RANGED bars (extremes ±₹1 around the opens):
        #   buy 130,105 · sell 130,895 → notionals 65,052,500 / 65,447,500
        #   gross 395,000
        #   STT: 65,447,500×2000//1e7 = 13,089 → ceil₹ 13,100 (the historical
        #     refuter boundary — nearest would have paid ₹11 here)
        #   ETC: ⌈1,125.41⌉ + ⌈1,132.24⌉ = 1,126 + 1,133 = 2,259
        #   SEBI/IPFT: ⌈65.05⌉ + ⌈65.45⌉ = 66 + 66 = 132 each
        #   stamp: 65,052,500×200//1e7 = 1,301 → ceil₹ 1,400
        #   GST: same base 6,523 → 1,200
        #   total 22,223 · net 372,777 ≤ 472,877 (the floor holds)
        monkeypatch.setitem(fc._ETC_FUTURES_REGIMES_BY_EXCHANGE, "NSE", _SYNTH_ETC)
        monkeypatch.setattr(fc, "_NSE_IPFT_FUTURES_REGIMES", _SYNTH_IPFT)
        monkeypatch.setattr(fc, "_STAMP_FUTURES_REGIMES", _SYNTH_STAMP)
        common: dict[str, object] = {
            "instrument": "RELIANCE",
            "contract_symbol": "NSE-RELIANCE-30Jan25-FUT",
            "lot_size": 500,
            "entry_bar_open_paisa": 130_000,
            "exit_bar_open_paisa": 131_000,
        }
        flat = compute_futures_trade_cost(_trade(**common))
        ranged = compute_futures_trade_cost(
            _trade(
                **common,
                entry_bar_high_paisa=130_100,
                entry_bar_low_paisa=129_900,
                exit_bar_high_paisa=131_100,
                exit_bar_low_paisa=130_900,
            )
        )
        assert (flat.buy_fill_paisa, flat.sell_fill_paisa) == (130_005, 130_995)
        assert flat.gross_pnl_paisa == 495_000
        assert (flat.stt_paisa, flat.exch_paisa) == (13_100, 2_259)
        assert (flat.sebi_paisa, flat.ipft_paisa) == (132, 132)
        assert (flat.stamp_paisa, flat.gst_paisa) == (1_300, 1_200)
        assert flat.total_charges_paisa == 22_123
        assert (ranged.buy_fill_paisa, ranged.sell_fill_paisa) == (130_105, 130_895)
        assert ranged.gross_pnl_paisa == 395_000
        assert ranged.brokerage_paisa == 4_000
        assert (ranged.stt_paisa, ranged.exch_paisa) == (13_100, 2_259)
        assert (ranged.sebi_paisa, ranged.ipft_paisa) == (132, 132)
        assert (ranged.stamp_paisa, ranged.gst_paisa) == (1_400, 1_200)
        assert ranged.total_charges_paisa == 22_223
        assert ranged.net_pnl_paisa == 372_777
        assert ranged.net_pnl_paisa <= flat.net_pnl_paisa


class TestBrokerageTurnoverArmCeils:
    def test_turnover_arm_ceils_unconditionally(self) -> None:
        # The min(₹20, 0.03%×turnover) TURNOVER arm is a rounded charge, so
        # it rides the permanent adverse-rounding law: 1,000,500 paisa ×
        # 0.03% = 300.15 → ceil 301 (nearest would have said 300).
        assert futures_brokerage_leg_paisa(1_000_500, "groww") == 301

    def test_flat_arm_and_zerodha_are_rounding_free(self) -> None:
        # A big notional lands on the flat ₹20 cap; zerodha is flat-only.
        assert futures_brokerage_leg_paisa(65_002_500, "groww") == 2_000
        assert futures_brokerage_leg_paisa(1_000_500, "zerodha") == 2_000
