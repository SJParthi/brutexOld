"""Unit tests for the 🎯 Options movers PURE core (DEC-MOVERS-OPTIONS-001).

No FastAPI, no lake — every function in :mod:`brutex.dashboard.movers_options`
that does NOT touch the filesystem is exercised here against plain in-memory
:class:`~brutex.dashboard.movers_options.OptionMover` values + raw symbol
strings: the contract-symbol parser (CE / PE / a FUT symbol → ``None`` / junk →
``None`` / bad strike / bad expiry → ``None``), the seven rankers, the payload
builder (every per-category key + the empty / no-mover / pull-first notes), and
the :class:`OptionMover` display ``name`` + ``₹`` rendering.

The lake-reading orchestration (``run_options_snapshot`` /
``_discover_options``) is covered by the integration
suite (``tests/integration/test_dashboard_movers_options.py``) against a real
Parquet lake.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from typing import Any

from brutex.dashboard.movers_options import (
    OptionMover,
    OptionsCandidates,
    _monthly_expiries,
    _tier_expiry_by_underlying,
    _tier_universe,
    _track_pool,
    build_options_payload,
    compute_option_mover,
    load_registry_expiries,
    parse_option_symbol,
    rank_highest_oi,
    rank_oi_gainers,
    rank_oi_losers,
    rank_price_gainers,
    rank_price_losers,
    rank_value,
    rank_volume,
)
from brutex.dashboard.movers_pcr import PcrLeg, UnderlyingPcr, compute_pcr_rows, pcr_row_json
from brutex.data.moneyness import Moneyness, StepConfidence

# ── _tier_expiry_by_underlying (Near/Next/Far resolution) ──────────────────


def _c(
    underlying: str, expiry: date, *, strike: str = "24000", typ: str = "CE"
) -> tuple[str, str, str, date, Decimal, str]:
    """A `_discover_options`-shaped tuple (symbol/exchange irrelevant to the tier)."""
    return (f"NSE-{underlying}-x-{strike}-{typ}", "NSE", underlying, expiry, Decimal(strike), typ)


class TestTierExpiry:
    _ON = date(2025, 6, 13)

    def test_near_next_far_pick_1st_2nd_3rd_distinct_live_expiry(self) -> None:
        contracts = [
            _c("NIFTY", date(2025, 6, 26)),
            _c("NIFTY", date(2025, 7, 31), typ="PE"),
            _c("NIFTY", date(2025, 8, 28)),
            _c("NIFTY", date(2025, 6, 26), strike="24500"),  # duplicate expiry, distinct contract
        ]
        assert _tier_expiry_by_underlying(contracts, on_date=self._ON, tier="near") == {
            "NIFTY": date(2025, 6, 26)
        }
        assert _tier_expiry_by_underlying(contracts, on_date=self._ON, tier="next") == {
            "NIFTY": date(2025, 7, 31)
        }
        assert _tier_expiry_by_underlying(contracts, on_date=self._ON, tier="far") == {
            "NIFTY": date(2025, 8, 28)
        }

    def test_unknown_tier_falls_back_to_near(self) -> None:
        contracts = [_c("NIFTY", date(2025, 6, 26)), _c("NIFTY", date(2025, 7, 31))]
        assert _tier_expiry_by_underlying(contracts, on_date=self._ON, tier="bogus") == {
            "NIFTY": date(2025, 6, 26)
        }

    def test_expired_contracts_are_excluded(self) -> None:
        # An expiry BEFORE the snapshot date is dropped (covers the `< on_date` arm);
        # NIFTY's only live expiry is the July one → that becomes Near.
        contracts = [
            _c("NIFTY", date(2025, 5, 29)),  # expired (< 2025-06-13)
            _c("NIFTY", date(2025, 7, 31)),
        ]
        assert _tier_expiry_by_underlying(contracts, on_date=self._ON, tier="near") == {
            "NIFTY": date(2025, 7, 31)
        }

    def test_tier_beyond_available_omits_the_underlying(self) -> None:
        # Only ONE live expiry → Far (3rd) has nothing → the underlying drops out.
        contracts = [_c("NIFTY", date(2025, 6, 26))]
        assert _tier_expiry_by_underlying(contracts, on_date=self._ON, tier="far") == {}


# ── _monthly_expiries (structural last-in-month classifier) ────────────────


class TestMonthlyExpiries:
    def test_empty_input_is_empty(self) -> None:
        assert _monthly_expiries(set()) == set()

    def test_single_expiry_in_a_month_is_its_own_monthly(self) -> None:
        # A stock / weekly-less index has one expiry per month → it IS the monthly.
        assert _monthly_expiries({date(2025, 6, 26)}) == {date(2025, 6, 26)}

    def test_last_in_month_wins_over_earlier_weeklies(self) -> None:
        # Four weeklies in one month → only the last (the monthly settlement) is kept.
        weeklies = {
            date(2025, 6, 5),
            date(2025, 6, 12),
            date(2025, 6, 19),
            date(2025, 6, 26),
        }
        assert _monthly_expiries(weeklies) == {date(2025, 6, 26)}

    def test_one_monthly_per_calendar_month(self) -> None:
        # Two months, each with weeklies → one monthly per (year, month).
        expiries = {
            date(2025, 6, 12),
            date(2025, 6, 26),  # June monthly
            date(2025, 7, 3),
            date(2025, 7, 31),  # July monthly
        }
        assert _monthly_expiries(expiries) == {date(2025, 6, 26), date(2025, 7, 31)}

    def test_same_day_number_different_months_are_distinct(self) -> None:
        # Guards the (year, month) key — June 30 and July 30 do not collide.
        assert _monthly_expiries({date(2025, 6, 30), date(2025, 7, 30)}) == {
            date(2025, 6, 30),
            date(2025, 7, 30),
        }


# ── _track_pool (All / Monthly / Weekly narrowing) ─────────────────────────


class TestTrackPool:
    @staticmethod
    def _expiries() -> set[date]:
        return {
            date(2025, 6, 12),  # weekly
            date(2025, 6, 26),  # June monthly
            date(2025, 7, 31),  # July monthly (sole July expiry)
        }

    def test_all_returns_every_expiry(self) -> None:
        assert _track_pool(self._expiries(), "all") == self._expiries()

    def test_monthly_keeps_only_last_in_month(self) -> None:
        assert _track_pool(self._expiries(), "monthly") == {date(2025, 6, 26), date(2025, 7, 31)}

    def test_weekly_keeps_the_rest(self) -> None:
        assert _track_pool(self._expiries(), "weekly") == {date(2025, 6, 12)}

    def test_unknown_track_behaves_as_all(self) -> None:
        # Belt-and-suspenders: an un-normalised value still returns everything.
        assert _track_pool(self._expiries(), "bogus") == self._expiries()


# ── _tier_expiry_by_underlying WITHIN a track ──────────────────────────────


class TestTierWithinTrack:
    _ON = date(2025, 6, 13)

    def _book(self) -> list[tuple[str, str, str, date, Decimal, str]]:
        # NIFTY: weeklies + monthlies across June/July/August.
        return [
            _c("NIFTY", date(2025, 6, 19)),  # June weekly
            _c("NIFTY", date(2025, 6, 26)),  # June monthly
            _c("NIFTY", date(2025, 7, 3)),  # July weekly
            _c("NIFTY", date(2025, 7, 31)),  # July monthly
            _c("NIFTY", date(2025, 8, 28)),  # August monthly (sole August expiry)
        ]

    def test_monthly_near_is_the_first_monthly(self) -> None:
        assert _tier_expiry_by_underlying(
            self._book(), on_date=self._ON, tier="near", track="monthly"
        ) == {"NIFTY": date(2025, 6, 26)}

    def test_monthly_next_is_the_second_monthly(self) -> None:
        assert _tier_expiry_by_underlying(
            self._book(), on_date=self._ON, tier="next", track="monthly"
        ) == {"NIFTY": date(2025, 7, 31)}

    def test_weekly_near_is_the_first_weekly(self) -> None:
        assert _tier_expiry_by_underlying(
            self._book(), on_date=self._ON, tier="near", track="weekly"
        ) == {"NIFTY": date(2025, 6, 19)}

    def test_weekly_track_empty_for_a_monthly_only_underlying(self) -> None:
        # A stock with one expiry per month → its weekly track is empty → it drops out.
        contracts = [_c("RELIANCE", date(2025, 6, 26)), _c("RELIANCE", date(2025, 7, 31))]
        assert (
            _tier_expiry_by_underlying(contracts, on_date=self._ON, tier="near", track="weekly")
            == {}
        )

    def test_default_track_is_all(self) -> None:
        # No track kwarg → every expiry ranked (prior behaviour preserved).
        assert _tier_expiry_by_underlying(self._book(), on_date=self._ON, tier="near") == {
            "NIFTY": date(2025, 6, 19)
        }


# ── parse_option_symbol ───────────────────────────────────────────────────


class TestParseOptionSymbol:
    def test_parses_a_call(self) -> None:
        got = parse_option_symbol("NSE-NIFTY-30Jun25-24000-CE")
        assert got == ("NIFTY", date(2025, 6, 30), Decimal("24000"), "CE")

    def test_parses_a_put(self) -> None:
        got = parse_option_symbol("NSE-BANKNIFTY-25Jul24-56000-PE")
        assert got == ("BANKNIFTY", date(2024, 7, 25), Decimal("56000"), "PE")

    def test_parses_a_fractional_strike(self) -> None:
        got = parse_option_symbol("NSE-NIFTY-30Jun25-24000.5-CE")
        assert got is not None
        assert got[2] == Decimal("24000.5")

    def test_future_symbol_is_none(self) -> None:
        # Ends -FUT, not CE/PE → not an option.
        assert parse_option_symbol("NSE-NIFTY-30Jun25-FUT") is None

    def test_junk_too_few_parts_is_none(self) -> None:
        assert parse_option_symbol("NSE-NIFTY-CE") is None

    def test_bad_strike_is_none(self) -> None:
        assert parse_option_symbol("NSE-NIFTY-30Jun25-ABC-CE") is None

    def test_bad_expiry_is_none(self) -> None:
        # Unknown month token → strptime fails → None.
        assert parse_option_symbol("NSE-NIFTY-30Xxx25-24000-CE") is None

    def test_out_of_range_day_expiry_is_none(self) -> None:
        # 31 Feb is not a real date → strptime ValueError → None.
        assert parse_option_symbol("NSE-NIFTY-31Feb25-24000-CE") is None

    def test_empty_underlying_is_none(self) -> None:
        # A leading-hyphen symbol parses an EMPTY underlying → None.
        assert parse_option_symbol("NSE--30Jun25-24000-CE") is None


# ── OptionMover display rendering ─────────────────────────────────────────


def _mover(
    *,
    symbol: str = "NSE-NIFTY-30Jun25-24000-CE",
    underlying: str = "NIFTY",
    expiry: date = date(2025, 6, 30),
    strike: Decimal = Decimal("24000"),
    opt_type: str = "CE",
    ltp: Decimal = Decimal("164.00"),
    spot: Decimal | None = Decimal("23622.90"),
    pct_change: Decimal | None = Decimal("179.62"),
    oi: int | None = 76,
    oi_chg: int | None = -3,
    oi_chg_pct: Decimal | None = Decimal("-3.80"),
    volume: int = 1000,
    value_paisa: int = 50_000,
    exchange: str = "NSE",
    moneyness: Moneyness = Moneyness.ITM,
    atm_offset: int | None = -8,
    moneyness_label: str = "ITM -8",
    iv: float | None = None,
    delta: float | None = None,
) -> OptionMover:
    """An :class:`OptionMover` with sensible defaults (override per test)."""
    return OptionMover(
        underlying=underlying,
        expiry=expiry,
        strike=strike,
        opt_type=opt_type,
        symbol=symbol,
        exchange=exchange,
        segment="FNO",
        ltp=ltp,
        spot=spot,
        pct_change=pct_change,
        oi=oi,
        oi_chg=oi_chg,
        oi_chg_pct=oi_chg_pct,
        volume=volume,
        value_paisa=value_paisa,
        moneyness=moneyness,
        atm_offset=atm_offset,
        moneyness_label=moneyness_label,
        iv=iv,
        delta=delta,
    )


class TestOptionMoverRendering:
    def test_name_whole_strike(self) -> None:
        # FULL dated/strike label, deterministic DD-Mon-YYYY (DEC-MOVERS-FULL-CONTRACT-NAME-001).
        assert _mover().name == "NIFTY 30-Jun-2025 24000 CE"

    def test_name_put_contract(self) -> None:
        m = _mover(opt_type="PE")
        assert m.name == "NIFTY 30-Jun-2025 24000 PE"

    def test_name_fractional_strike(self) -> None:
        m = _mover(strike=Decimal("24000.5"))
        assert m.name == "NIFTY 30-Jun-2025 24000.5 CE"

    def test_name_with_trailing_zero_strike_normalizes(self) -> None:
        # A strike carrying ``.00`` must still read as the integer 24000.
        m = _mover(strike=Decimal("24000.00"))
        assert m.name == "NIFTY 30-Jun-2025 24000 CE"

    def test_name_different_expiries_render_different_names(self) -> None:
        # Near vs Far ``24000 CE`` of the same underlying must read DISTINCT.
        near = _mover(expiry=date(2025, 6, 26))
        far = _mover(expiry=date(2025, 7, 31))
        assert near.name == "NIFTY 26-Jun-2025 24000 CE"
        assert far.name == "NIFTY 31-Jul-2025 24000 CE"
        assert near.name != far.name

    def test_ltp_inr(self) -> None:
        assert _mover().ltp_inr == "₹164.00"

    def test_spot_inr_present(self) -> None:
        assert _mover().spot_inr == "₹23,622.90"

    def test_spot_inr_absent(self) -> None:
        assert _mover(spot=None).spot_inr is None


# ── compute_option_mover (pure reduction) ─────────────────────────────────


class TestComputeOptionMover:
    def test_full_row(self) -> None:
        m = compute_option_mover(
            underlying="NIFTY",
            expiry=date(2025, 6, 30),
            strike=Decimal("24000"),
            opt_type="CE",
            symbol="NSE-NIFTY-30Jun25-24000-CE",
            exchange="NSE",
            ltp=Decimal("164"),
            volume=1000,
            value_paisa=16_400_000,
            oi=1100,
            prev_close=Decimal("100"),
            spot_ltp=Decimal("23622.9"),
            prev_oi=1000,
        )
        assert m.ltp == Decimal("164.00")
        assert m.spot == Decimal("23622.90")
        assert m.pct_change == Decimal("64")  # (164-100)/100*100
        assert m.oi == 1100
        assert m.oi_chg == 100  # 1100 - 1000
        assert m.oi_chg_pct == Decimal("10")  # 1100 vs 1000
        assert m.value_paisa == 16_400_000
        # spot 23622.90 < strike 24000, CE → OTM; no step passed → no ATM±N.
        assert m.moneyness is Moneyness.OTM
        assert m.atm_offset is None
        assert m.moneyness_label == "OTM"  # no grid → bucket only
        # Greeks default to an honest None when the caller has no stored overlay
        # (DEC-GREEKS-ENGINE-PR3B-001) — never a fabricated value.
        assert m.iv is None
        assert m.delta is None

    def test_greeks_passthrough_full_precision(self) -> None:
        """Stored iv/delta pass through VERBATIM — full precision, never rounded
        (statistical values, CLAUDE.md §4; DEC-GREEKS-ENGINE-PR3B-001)."""
        m = compute_option_mover(
            underlying="NIFTY",
            expiry=date(2025, 6, 30),
            strike=Decimal("24000"),
            opt_type="CE",
            symbol="NSE-NIFTY-30Jun25-24000-CE",
            exchange="NSE",
            ltp=Decimal("164"),
            volume=1000,
            value_paisa=16_400_000,
            oi=1100,
            prev_close=Decimal("100"),
            spot_ltp=Decimal("23622.9"),
            prev_oi=1000,
            iv=0.1812345678901234,
            delta=-0.2260012345678901,
        )
        assert m.iv == 0.1812345678901234
        assert m.delta == -0.2260012345678901

    def test_moneyness_with_dynamic_step(self) -> None:
        # spot 23622.90, strike 24000, step 50 → CE OTM, ATM offset round((24000-23622.90)/50)=8.
        m = compute_option_mover(
            underlying="NIFTY",
            expiry=date(2025, 6, 30),
            strike=Decimal("24000"),
            opt_type="CE",
            symbol="NSE-NIFTY-30Jun25-24000-CE",
            exchange="NSE",
            ltp=Decimal("164"),
            volume=1000,
            value_paisa=16_400_000,
            oi=1100,
            prev_close=Decimal("100"),
            spot_ltp=Decimal("23622.9"),
            prev_oi=1000,
            strike_step=Decimal("50"),
        )
        assert m.moneyness is Moneyness.OTM
        assert m.atm_offset == 8
        assert m.moneyness_label == "OTM +8"  # the combined "bucket + depth" label

    def test_moneyness_unknown_without_spot(self) -> None:
        m = compute_option_mover(
            underlying="NIFTY",
            expiry=date(2025, 6, 30),
            strike=Decimal("24000"),
            opt_type="CE",
            symbol="NSE-NIFTY-30Jun25-24000-CE",
            exchange="NSE",
            ltp=Decimal("164"),
            volume=1000,
            value_paisa=16_400_000,
            oi=1100,
            prev_close=Decimal("100"),
            spot_ltp=None,
            prev_oi=1000,
            strike_step=Decimal("50"),
        )
        # No spot → honest UNKNOWN, never a fabricated bucket (Golden Rule #1).
        assert m.moneyness is Moneyness.UNKNOWN
        assert m.atm_offset is None
        assert m.moneyness_label == "?"

    def test_no_prior_day_nulls_pct_and_oi_change(self) -> None:
        m = compute_option_mover(
            underlying="NIFTY",
            expiry=date(2025, 6, 30),
            strike=Decimal("24000"),
            opt_type="CE",
            symbol="NSE-NIFTY-30Jun25-24000-CE",
            exchange="NSE",
            ltp=Decimal("164"),
            volume=1000,
            value_paisa=16_400_000,
            oi=1100,
            prev_close=None,
            spot_ltp=None,
            prev_oi=None,
        )
        assert m.pct_change is None
        assert m.spot is None
        assert m.oi_chg is None
        assert m.oi_chg_pct is None

    def test_zero_prev_oi_nulls_oi_pct_but_keeps_oi_chg(self) -> None:
        m = compute_option_mover(
            underlying="NIFTY",
            expiry=date(2025, 6, 30),
            strike=Decimal("24000"),
            opt_type="CE",
            symbol="NSE-NIFTY-30Jun25-24000-CE",
            exchange="NSE",
            ltp=Decimal("164"),
            volume=1000,
            value_paisa=16_400_000,
            oi=1100,
            prev_close=Decimal("100"),
            spot_ltp=None,
            prev_oi=0,
        )
        assert m.oi_chg == 1100  # 1100 - 0
        assert m.oi_chg_pct is None  # divide-by-zero guard

    def test_missing_current_oi_nulls_both_oi_metrics(self) -> None:
        m = compute_option_mover(
            underlying="NIFTY",
            expiry=date(2025, 6, 30),
            strike=Decimal("24000"),
            opt_type="CE",
            symbol="NSE-NIFTY-30Jun25-24000-CE",
            exchange="NSE",
            ltp=Decimal("164"),
            volume=1000,
            value_paisa=16_400_000,
            oi=None,
            prev_close=Decimal("100"),
            spot_ltp=None,
            prev_oi=1000,
        )
        assert m.oi is None
        assert m.oi_chg is None
        assert m.oi_chg_pct is None


# ── the seven rankers ─────────────────────────────────────────────────────


class TestRankers:
    def test_highest_oi_desc_skips_none(self) -> None:
        a = _mover(symbol="A", oi=100)
        b = _mover(symbol="B", oi=300)
        c = _mover(symbol="C", oi=None)
        ranked = rank_highest_oi([a, b, c], 10)
        assert [m.symbol for m in ranked] == ["B", "A"]

    def test_highest_oi_top_n_caps(self) -> None:
        movers = [_mover(symbol=f"S{i}", oi=i) for i in range(5)]
        assert len(rank_highest_oi(movers, 2)) == 2

    def test_oi_gainers_positive_desc(self) -> None:
        a = _mover(symbol="A", oi_chg_pct=Decimal("5"))
        b = _mover(symbol="B", oi_chg_pct=Decimal("9"))
        c = _mover(symbol="C", oi_chg_pct=Decimal("-1"))
        d = _mover(symbol="D", oi_chg_pct=None)
        ranked = rank_oi_gainers([a, b, c, d], 10)
        assert [m.symbol for m in ranked] == ["B", "A"]

    def test_oi_losers_negative_asc(self) -> None:
        a = _mover(symbol="A", oi_chg_pct=Decimal("-2"))
        b = _mover(symbol="B", oi_chg_pct=Decimal("-9"))
        c = _mover(symbol="C", oi_chg_pct=Decimal("3"))
        ranked = rank_oi_losers([a, b, c], 10)
        assert [m.symbol for m in ranked] == ["B", "A"]

    def test_volume_positive_desc(self) -> None:
        a = _mover(symbol="A", volume=10)
        b = _mover(symbol="B", volume=50)
        c = _mover(symbol="C", volume=0)
        ranked = rank_volume([a, b, c], 10)
        assert [m.symbol for m in ranked] == ["B", "A"]

    def test_value_positive_desc(self) -> None:
        a = _mover(symbol="A", value_paisa=10)
        b = _mover(symbol="B", value_paisa=50)
        c = _mover(symbol="C", value_paisa=0)
        ranked = rank_value([a, b, c], 10)
        assert [m.symbol for m in ranked] == ["B", "A"]

    def test_price_gainers_positive_desc(self) -> None:
        a = _mover(symbol="A", pct_change=Decimal("5"))
        b = _mover(symbol="B", pct_change=Decimal("9"))
        c = _mover(symbol="C", pct_change=Decimal("-3"))
        d = _mover(symbol="D", pct_change=None)
        ranked = rank_price_gainers([a, b, c, d], 10)
        assert [m.symbol for m in ranked] == ["B", "A"]

    def test_price_losers_negative_asc(self) -> None:
        a = _mover(symbol="A", pct_change=Decimal("-2"))
        b = _mover(symbol="B", pct_change=Decimal("-9"))
        c = _mover(symbol="C", pct_change=Decimal("4"))
        ranked = rank_price_losers([a, b, c], 10)
        assert [m.symbol for m in ranked] == ["B", "A"]

    def test_tiebreak_is_deterministic(self) -> None:
        # Equal metric → ascending (symbol, exchange, segment).
        a = _mover(symbol="ZZZ", oi=100)
        b = _mover(symbol="AAA", oi=100)
        ranked = rank_highest_oi([a, b], 10)
        assert [m.symbol for m in ranked] == ["AAA", "ZZZ"]


# ── build_options_payload ─────────────────────────────────────────────────


class TestBuildOptionsPayload:
    _CATEGORIES = (
        "highest_oi",
        "oi_gainers",
        "oi_losers",
        "volume",
        "value",
        "price_gainers",
        "price_losers",
    )

    def test_all_category_keys_present(self) -> None:
        payload = build_options_payload(
            [_mover()], interval="1day", date="2025-06-13", time=None, top_n=10, skipped=0
        )
        for category in self._CATEGORIES:
            assert category in payload

    def test_echo_and_counts(self) -> None:
        payload = build_options_payload(
            [_mover()], interval="5minute", date="2025-06-13", time="09:20", top_n=7, skipped=2
        )
        assert payload["interval"] == "5minute"
        assert payload["date"] == "2025-06-13"
        assert payload["time"] == "09:20"
        assert payload["top_n"] == 7
        assert payload["instrument_count"] == 3  # 1 mover + 2 skipped
        assert payload["skipped_count"] == 2

    def test_row_projection_fields(self) -> None:
        payload = build_options_payload(
            [_mover()], interval="1day", date="2025-06-13", time=None, top_n=10, skipped=0
        )
        row = payload["highest_oi"][0]
        assert row["name"] == "NIFTY 30-Jun-2025 24000 CE"
        assert row["spot"] == "₹23,622.90"
        assert row["ltp"] == "₹164.00"
        assert row["pct_change"] == "179.62"
        assert row["oi"] == 76
        assert row["oi_chg"] == -3
        assert row["oi_chg_pct"] == "-3.80"
        assert row["volume"] == 1000
        # The single shown moneyness cell is the combined "bucket + signed depth" label
        # (DEC-FNO-MONEYNESS-001); the raw bucket + ATM±N stay in the payload too.
        assert row["moneyness"] == "ITM -8"
        assert row["moneyness_bucket"] == "ITM"
        assert row["atm_offset"] == -8
        # No stored overlay on the default mover → honest null cells (the screen
        # renders "—"; DEC-GREEKS-ENGINE-PR3B-001).
        assert row["iv"] is None
        assert row["delta"] is None

    def test_row_projection_stored_greeks_verbatim(self) -> None:
        """Stored iv/delta reach the payload as FULL-PRECISION floats — never
        stringified/rounded (DEC-GREEKS-ENGINE-PR3B-001; CLAUDE.md §4)."""
        payload = build_options_payload(
            [_mover(iv=0.1812345678901234, delta=-0.2260012345678901)],
            interval="1day",
            date="2025-06-13",
            time=None,
            top_n=10,
            skipped=0,
        )
        row = payload["highest_oi"][0]
        assert row["iv"] == 0.1812345678901234
        assert row["delta"] == -0.2260012345678901

    def test_null_metrics_render_none(self) -> None:
        m = _mover(spot=None, pct_change=None, oi_chg=None, oi_chg_pct=None)
        payload = build_options_payload(
            [m], interval="1day", date="2025-06-13", time=None, top_n=10, skipped=0
        )
        row = payload["highest_oi"][0]
        assert row["spot"] is None
        assert row["pct_change"] is None
        assert row["oi_chg"] is None
        assert row["oi_chg_pct"] is None

    def test_empty_lake_note(self) -> None:
        payload = build_options_payload(
            [], interval="1day", date="2025-06-13", time=None, top_n=10, skipped=0
        )
        assert payload["instrument_count"] == 0
        assert "pull the F&O options first" in payload["note"]

    def test_present_but_no_mover_note(self) -> None:
        # Contracts present (all skipped) but no ranked rows → "pick another time".
        payload = build_options_payload(
            [], interval="1day", date="2025-06-13", time=None, top_n=10, skipped=3
        )
        assert payload["instrument_count"] == 3
        assert "none had a bar" in payload["note"]

    def test_ranked_note_names_the_expiry_tier(self) -> None:
        payload = build_options_payload(
            [_mover()],
            interval="1day",
            date="2025-06-13",
            time=None,
            top_n=10,
            skipped=0,
            expiry_tier="next",
        )
        assert "Next expiry" in payload["note"]
        assert "Near/Next/Far" in payload["note"]

    def test_default_track_omits_the_cadence_label(self) -> None:
        # The default ``all`` track adds no cadence suffix → just the tier label.
        payload = build_options_payload(
            [_mover()],
            interval="1day",
            date="2025-06-13",
            time=None,
            top_n=10,
            skipped=0,
            expiry_tier="near",
            expiry_track="all",
        )
        assert "Near expiry" in payload["note"]
        # No cadence appended to the tier label (the explanatory "(All/Monthly/Weekly)"
        # legend may still mention the words — the LABEL is what must stay bare).
        assert "Near Monthly" not in payload["note"]
        assert "Near Weekly" not in payload["note"]

    def test_ranked_note_appends_the_cadence_track(self) -> None:
        payload = build_options_payload(
            [_mover()],
            interval="1day",
            date="2025-06-13",
            time=None,
            top_n=10,
            skipped=0,
            expiry_tier="next",
            expiry_track="monthly",
        )
        assert "Next Monthly expiry" in payload["note"]

    def test_empty_note_carries_the_cadence_track(self) -> None:
        payload = build_options_payload(
            [],
            interval="1day",
            date="2025-06-13",
            time=None,
            top_n=10,
            skipped=0,
            expiry_tier="near",
            expiry_track="weekly",
        )
        assert "No Near Weekly-expiry option contracts" in payload["note"]

    def test_present_but_no_mover_note_carries_the_cadence_track(self) -> None:
        payload = build_options_payload(
            [],
            interval="1day",
            date="2025-06-13",
            time=None,
            top_n=10,
            skipped=3,
            expiry_tier="near",
            expiry_track="monthly",
        )
        assert "Near Monthly-expiry option contract(s)" in payload["note"]

    def test_daily_note_shows_daily_label(self) -> None:
        payload = build_options_payload(
            [_mover()], interval="1day", date="2025-06-13", time=None, top_n=10, skipped=0
        )
        assert "(daily)" in payload["note"]


# ── DEC-MOVERS-CALENDAR-TIERS-001: authoritative vendor-calendar tier selection ──


class TestTierUniverse:
    """:func:`_tier_universe` — the vendor calendar wins, else the lake set (the fallback)."""

    _ON = date(2025, 6, 25)

    def test_no_calendar_falls_back_to_lake_set(self) -> None:
        lake = {date(2025, 7, 7), date(2025, 7, 14)}
        assert _tier_universe("NIFTY", lake, on_date=self._ON, calendar_expiries=None) == lake

    def test_underlying_absent_from_calendar_falls_back_to_lake_set(self) -> None:
        lake = {date(2025, 7, 7)}
        cal = {"BANKNIFTY": {date(2025, 7, 31)}}
        assert _tier_universe("NIFTY", lake, on_date=self._ON, calendar_expiries=cal) == lake

    def test_empty_calendar_set_falls_back_to_lake_set(self) -> None:
        # A present-but-empty calendar entry is treated as "no calendar" (the guard
        # uses truthiness, so an empty set never shadows real lake data).
        lake = {date(2025, 7, 7)}
        cal: dict[str, set[date]] = {"NIFTY": set()}
        assert _tier_universe("NIFTY", lake, on_date=self._ON, calendar_expiries=cal) == lake

    def test_calendar_wins_and_is_filtered_to_on_or_after_on_date(self) -> None:
        # The FULL July calendar (incl. an already-expired 13-Jun) → only >= on_date kept,
        # and it OVERRIDES the sparse lake set (which is missing 21/28-Jul).
        cal = {
            "NIFTY": {
                date(2025, 6, 13),  # < on_date (2025-06-25) → excluded
                date(2025, 7, 7),
                date(2025, 7, 14),
                date(2025, 7, 21),
                date(2025, 7, 28),
            }
        }
        lake = {date(2025, 7, 7), date(2025, 7, 14)}  # the sparsely-pulled subset
        assert _tier_universe("NIFTY", lake, on_date=self._ON, calendar_expiries=cal) == {
            date(2025, 7, 7),
            date(2025, 7, 14),
            date(2025, 7, 21),
            date(2025, 7, 28),
        }


class TestCalendarDrivenTiers:
    """Calendar-driven tiers: the FIX yields the correct full-calendar tiers, and the
    sparse-lake tier-mislabel/skip bug CLASS is demonstrated.

    These use a REPRESENTATIVE, simplified lake + calendar — they do NOT reconstruct the
    operator's exact reported lake (we do not have it). The point is the behaviour: the
    calendar set drives the tier, and the sparse-lake-only derivation mislabels/skips
    (Golden Rule #1 — honest about what is proven).
    """

    _ON = date(2025, 6, 25)

    @staticmethod
    def _sparse_contracts() -> list[tuple[str, str, str, date, Decimal, str]]:
        # A REPRESENTATIVE sparse lake: only 07-Jul + 04-Aug contracts on disk (the
        # intermediate July weeklies 14/21/28-Jul are not pulled) — enough to exercise the
        # lake-only-derivation mislabel/skip, not a copy of the operator's real lake.
        return [
            _c("NIFTY", date(2025, 7, 7)),
            _c("NIFTY", date(2025, 8, 4)),
        ]

    @staticmethod
    def _full_calendar() -> dict[str, set[date]]:
        # The AUTHORITATIVE registry calendar: the full July weeklies + the 28-Jul monthly.
        return {
            "NIFTY": {
                date(2025, 7, 7),
                date(2025, 7, 14),
                date(2025, 7, 21),
                date(2025, 7, 28),  # July monthly (last-in-month)
                date(2025, 8, 4),
            }
        }

    def test_operator_case_weekly_near_next_far_07_14_21(self) -> None:
        # FIX PROOF (as-of 2025-06-25): fed the full registry calendar, the Weekly track
        # = {07,14,21-Jul} (28-Jul is the monthly), so Near/Next/Far = 07/14/21-Jul — the
        # CORRECT full-calendar tiers (vs the sparse-lake derivation, which mislabels —
        # see test_without_calendar_the_sparse_lake_mislabels_far).
        cal = self._full_calendar()
        contracts = self._sparse_contracts()
        near = _tier_expiry_by_underlying(
            contracts, on_date=self._ON, tier="near", track="weekly", calendar_expiries=cal
        )
        nxt = _tier_expiry_by_underlying(
            contracts, on_date=self._ON, tier="next", track="weekly", calendar_expiries=cal
        )
        far = _tier_expiry_by_underlying(
            contracts, on_date=self._ON, tier="far", track="weekly", calendar_expiries=cal
        )
        assert near["NIFTY"] == date(2025, 7, 7)
        assert nxt["NIFTY"] == date(2025, 7, 14)
        assert far["NIFTY"] == date(2025, 7, 21)

    def test_28jul_classified_monthly_on_the_full_calendar(self) -> None:
        # 28-Jul is the LAST July expiry → the July monthly, so Monthly + Near = 28-Jul.
        cal = self._full_calendar()
        contracts = self._sparse_contracts()
        monthly_near = _tier_expiry_by_underlying(
            contracts, on_date=self._ON, tier="near", track="monthly", calendar_expiries=cal
        )
        assert monthly_near["NIFTY"] == date(2025, 7, 28)

    def test_without_calendar_the_sparse_lake_mislabels_far(self) -> None:
        # BUG-CLASS DEMONSTRATION: WITHOUT the calendar, the lake-only derivation over the
        # representative sparse lake {07-Jul, 04-Aug} does NOT resolve a correct Far weekly
        # — each is the sole expiry of its month, so both classify as "monthly" and the
        # weekly track is EMPTY → no Far weekly at all. This is the mislabel/skip the
        # calendar path fixes (not a reconstruction of the operator's exact 04-Aug screen).
        contracts = self._sparse_contracts()
        far_lake = _tier_expiry_by_underlying(
            contracts, on_date=self._ON, tier="far", track="weekly", calendar_expiries=None
        )
        assert "NIFTY" not in far_lake


class TestLoadRegistryExpiries:
    """:func:`load_registry_expiries` — read the full calendar, gracefully fall back."""

    def test_missing_registry_returns_none(self, tmp_path: object) -> None:
        from pathlib import Path

        assert isinstance(tmp_path, Path)
        assert load_registry_expiries(tmp_path / "registry.duckdb") is None

    def test_empty_registry_returns_none(self, tmp_path: object) -> None:
        from pathlib import Path

        from brutex.data.registry_store import RegistryStore

        assert isinstance(tmp_path, Path)
        path = tmp_path / "registry.duckdb"
        with RegistryStore.open(path):
            pass  # create the schema, persist nothing
        assert load_registry_expiries(path) is None

    def test_populated_registry_returns_calendar_per_underlying(self, tmp_path: object) -> None:
        from datetime import UTC, datetime
        from pathlib import Path

        from brutex.data.registry_store import RegistryStore

        assert isinstance(tmp_path, Path)
        path = tmp_path / "registry.duckdb"
        now = datetime(2025, 6, 25, tzinfo=UTC)
        with RegistryStore.open(path) as reg:
            for exp in (date(2025, 7, 7), date(2025, 7, 14), date(2025, 7, 28)):
                reg.upsert_expiry("NSE", "NIFTY", exp, run_id="r1", fetched_at=now)
            reg.upsert_expiry("NSE", "BANKNIFTY", date(2025, 7, 31), run_id="r1", fetched_at=now)
        cal = load_registry_expiries(path)
        assert cal is not None
        assert cal["NIFTY"] == {date(2025, 7, 7), date(2025, 7, 14), date(2025, 7, 28)}
        assert cal["BANKNIFTY"] == {date(2025, 7, 31)}

    def test_exchange_filter_scopes_the_calendar(self, tmp_path: object) -> None:
        from datetime import UTC, datetime
        from pathlib import Path

        from brutex.data.registry_store import RegistryStore

        assert isinstance(tmp_path, Path)
        path = tmp_path / "registry.duckdb"
        now = datetime(2025, 6, 25, tzinfo=UTC)
        with RegistryStore.open(path) as reg:
            reg.upsert_expiry("NSE", "NIFTY", date(2025, 7, 7), run_id="r1", fetched_at=now)
            reg.upsert_expiry("BSE", "SENSEX", date(2025, 7, 8), run_id="r1", fetched_at=now)
        nse = load_registry_expiries(path, exchange="NSE")
        assert nse is not None
        assert set(nse) == {"NIFTY"}

    def test_registry_error_is_swallowed_to_none(
        self, tmp_path: object, monkeypatch: object
    ) -> None:
        from pathlib import Path

        import pytest
        from brutex.data.registry_store import RegistryError

        assert isinstance(tmp_path, Path)
        assert isinstance(monkeypatch, pytest.MonkeyPatch)
        path = tmp_path / "registry.duckdb"
        path.write_text("not a real duckdb file")  # exists but unreadable

        def _boom(_p: Path) -> object:
            raise RegistryError("corrupt cache")

        # Patch the `RegistryStore.open` the production module looks up — addressed by
        # the dotted string so the module is referenced ONE way (the top-of-file
        # `from brutex.dashboard.movers_options import ...`), never re-imported as a
        # second binding (resolves the CodeQL "imported with both import and import-from").
        monkeypatch.setattr("brutex.dashboard.movers_options.RegistryStore.open", _boom)
        assert load_registry_expiries(path) is None


class TestOptionsMinVolume:
    """DEC-MOVERS-MIN-VOLUME-001 — the OPT-IN Top-Volume floor on the Options rank/payload."""

    def test_rank_volume_default_zero_is_byte_identical(self) -> None:
        movers = [_mover(volume=0), _mover(volume=5)]
        assert rank_volume(movers, 10) == rank_volume(movers, 10, min_volume=0)
        # Zero-volume still excluded; the positive one kept.
        assert [m.volume for m in rank_volume(movers, 10)] == [5]

    def test_rank_volume_floor_drops_thin_contracts(self) -> None:
        movers = [_mover(volume=100), _mover(volume=900), _mover(volume=1500)]
        kept = rank_volume(movers, 10, min_volume=1000)
        assert [m.volume for m in kept] == [1500]

    def test_payload_floor_applies_only_to_volume_category(self) -> None:
        thin = _mover(symbol="NSE-NIFTY-30Jun25-26000-CE", strike=Decimal("26000"), volume=50)
        thick = _mover(symbol="NSE-NIFTY-30Jun25-24000-CE", strike=Decimal("24000"), volume=9000)
        payload = build_options_payload(
            [thin, thick],
            interval="1day",
            date="2025-06-13",
            time=None,
            top_n=10,
            skipped=0,
            min_volume=1000,
        )
        assert payload["min_volume"] == 1000
        # The volume ranking drops the thin contract; price rankings are untouched.
        assert len(payload["volume"]) == 1
        assert "Min volume ≥ 1,000." in payload["note"]

    def test_payload_default_floor_zero_no_floor_note(self) -> None:
        payload = build_options_payload(
            [_mover()], interval="1day", date="2025-06-13", time=None, top_n=10, skipped=0
        )
        assert payload["min_volume"] == 0
        assert "Min volume" not in payload["note"]

    def test_negative_floor_normalises_to_zero(self) -> None:
        payload = build_options_payload(
            [_mover()],
            interval="1day",
            date="2025-06-13",
            time=None,
            top_n=10,
            skipped=0,
            min_volume=-5,
        )
        assert payload["min_volume"] == 0

    def test_floor_note_on_present_but_no_mover_branch(self) -> None:
        # When the lake has contracts but none clear the floor at this bar, the no-mover
        # note still carries the applied floor.
        payload = build_options_payload(
            [_mover(volume=10, pct_change=None, oi=None, oi_chg_pct=None, value_paisa=0)],
            interval="1day",
            date="2025-06-13",
            time=None,
            top_n=10,
            skipped=0,
            min_volume=99999,
        )
        assert "Min volume ≥ 99,999." in payload["note"]


class TestCalendarOnlyExpiryNote:
    """DEC-MOVERS-CALENDAR-TIERS-001 — the honest "no data pulled for this expiry" note."""

    def test_calendar_only_expiry_note(self) -> None:
        payload = build_options_payload(
            [],
            interval="1day",
            date="2025-06-25",
            time=None,
            top_n=10,
            skipped=0,
            expiry_tier="far",
            expiry_track="weekly",
            calendar_only_expiry=True,
        )
        assert "vendor calendar" in payload["note"]
        assert "NO option contract candles" in payload["note"]

    def test_not_calendar_only_keeps_the_pull_first_note(self) -> None:
        payload = build_options_payload(
            [],
            interval="1day",
            date="2025-06-25",
            time=None,
            top_n=10,
            skipped=0,
            expiry_tier="near",
            calendar_only_expiry=False,
        )
        assert "pull the F&O" in payload["note"]


class TestStrikeStepConfidence:
    """DEC-MONEYNESS-REGISTRY-STEP-001 — the honest strike-grid confidence label."""

    def _m(
        self,
        *,
        strike_step: Decimal | None,
        strike_step_confidence: StepConfidence | None,
    ) -> OptionMover:
        return compute_option_mover(
            underlying="NIFTY",
            expiry=date(2025, 6, 30),
            strike=Decimal("24000"),
            opt_type="CE",
            symbol="NSE-NIFTY-30Jun25-24000-CE",
            exchange="NSE",
            ltp=Decimal("164"),
            volume=1000,
            value_paisa=16_400_000,
            oi=1100,
            prev_close=Decimal("100"),
            spot_ltp=Decimal("23622.9"),
            prev_oi=1000,
            strike_step=strike_step,
            strike_step_confidence=strike_step_confidence,
        )

    def test_registry_confidence_is_surfaced_with_a_step(self) -> None:
        m = self._m(strike_step=Decimal("50"), strike_step_confidence=StepConfidence.REGISTRY)
        assert m.strike_step_confidence is StepConfidence.REGISTRY
        assert m.moneyness_label == "OTM +8"

    def test_observed_confidence_is_surfaced_with_a_step(self) -> None:
        m = self._m(strike_step=Decimal("50"), strike_step_confidence=StepConfidence.OBSERVED)
        assert m.strike_step_confidence is StepConfidence.OBSERVED

    def test_confidence_is_dropped_without_a_step(self) -> None:
        # No step ⇒ nothing displayed to qualify ⇒ never a dangling flag (Rule #1).
        m = self._m(strike_step=None, strike_step_confidence=StepConfidence.OBSERVED)
        assert m.strike_step_confidence is None
        assert m.atm_offset is None

    def test_step_without_a_label_floors_to_observed(self) -> None:
        # DEC-FNO-HONESTY-PACK-001: a real step passed WITHOUT a confidence gets the
        # honest LOW-confidence floor — a displayed grid can never serialise
        # `strike_step_confidence: null`, which the payload doc defines as
        # "no grid presented" (the latent split guarded in BOTH directions).
        m = self._m(strike_step=Decimal("50"), strike_step_confidence=None)
        assert m.strike_step_confidence is StepConfidence.OBSERVED
        assert m.atm_offset is not None  # the grid really is presented

    def test_no_step_and_no_label_is_none(self) -> None:
        m = self._m(strike_step=None, strike_step_confidence=None)
        assert m.strike_step_confidence is None

    def test_row_projection_serialises_the_confidence(self) -> None:
        payload = build_options_payload(
            [
                self._m(
                    strike_step=Decimal("50"),
                    strike_step_confidence=StepConfidence.OBSERVED,
                )
            ],
            interval="1day",
            date="2025-06-13",
            time=None,
            top_n=10,
            skipped=0,
        )
        assert payload["highest_oi"][0]["strike_step_confidence"] == "observed"

    def test_row_projection_serialises_none_confidence(self) -> None:
        payload = build_options_payload(
            [self._m(strike_step=None, strike_step_confidence=None)],
            interval="1day",
            date="2025-06-13",
            time=None,
            top_n=10,
            skipped=0,
        )
        assert payload["highest_oi"][0]["strike_step_confidence"] is None


# ── the PCR payload thread (DEC-MOVERS-OPTIONS-PCR-001) ────────────────────


class TestOptionsPcrPayload:
    """The per-underlying PCR rows ride the payload untouched by rank-time knobs."""

    @staticmethod
    def _pcr_rows() -> tuple[UnderlyingPcr, ...]:
        return compute_pcr_rows(
            [
                PcrLeg(underlying="NIFTY", opt_type="PE", oi=300, volume=30),
                PcrLeg(underlying="NIFTY", opt_type="CE", oi=200, volume=20),
            ],
            legs_total={"NIFTY": 2},
            target_expiry={"NIFTY": date(2025, 6, 26)},
        )

    def test_candidates_backcompat_defaults_pcr_empty(self) -> None:
        # Defaulted-LAST field: every pre-PCR constructor call site stays valid.
        cands = OptionsCandidates(
            movers=(), skipped=0, date="2025-06-13", expiry_tier="near", expiry_track="all"
        )
        assert cands.pcr == ()

    def test_payload_carries_the_pcr_key_default_empty(self) -> None:
        payload = build_options_payload(
            [], interval="1day", date="2025-06-13", time=None, top_n=10, skipped=0
        )
        assert payload["pcr"] == []

    def test_payload_serializes_the_rows(self) -> None:
        payload = build_options_payload(
            [_mover()],
            interval="1day",
            date="2025-06-13",
            time=None,
            top_n=10,
            skipped=0,
            pcr=self._pcr_rows(),
        )
        assert payload["pcr"] == [pcr_row_json(row) for row in self._pcr_rows()]
        assert payload["pcr"][0]["oi_pcr"] == "1.5"

    def test_pcr_is_byte_identical_across_top_n_and_min_volume(self) -> None:
        # PCR is a scan-time per-underlying aggregate over the FULL tier chain —
        # the rank-time top_n / min_volume knobs must never touch it (edge 18).
        movers = [_mover(), _mover(symbol="NSE-NIFTY-30Jun25-24000-PE", opt_type="PE")]
        payloads = [
            build_options_payload(
                movers,
                interval="1day",
                date="2025-06-13",
                time=None,
                top_n=top_n,
                skipped=0,
                min_volume=min_volume,
                pcr=self._pcr_rows(),
            )
            for top_n, min_volume in ((1, 0), (500, 0), (1, 10**9), (500, 10**9))
        ]
        first = json.dumps(payloads[0]["pcr"], sort_keys=True)
        assert all(json.dumps(p["pcr"], sort_keys=True) == first for p in payloads[1:])

    def test_other_payload_keys_are_byte_identical_with_and_without_pcr(self) -> None:
        # Adding PCR must not perturb ANY existing key (byte-identity pin).
        movers = [_mover()]
        kwargs: dict[str, Any] = {
            "interval": "1day",
            "date": "2025-06-13",
            "time": None,
            "top_n": 10,
            "skipped": 0,
        }
        with_pcr = build_options_payload(movers, **kwargs, pcr=self._pcr_rows())
        without = build_options_payload(movers, **kwargs)
        with_pcr.pop("pcr")
        without.pop("pcr")
        assert json.dumps(with_pcr, sort_keys=True) == json.dumps(without, sort_keys=True)
