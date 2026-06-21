"""Unit tests for the 🎯 Options movers PURE core (DEC-MOVERS-OPTIONS-001).

No FastAPI, no lake — every function in :mod:`brutex.dashboard.movers_options`
that does NOT touch the filesystem is exercised here against plain in-memory
:class:`~brutex.dashboard.movers_options.OptionMover` values + raw symbol
strings: the contract-symbol parser (CE / PE / a FUT symbol → ``None`` / junk →
``None`` / bad strike / bad expiry → ``None``), the seven rankers, the payload
builder (every per-category key + the empty / no-mover / pull-first notes), and
the :class:`OptionMover` display ``name`` + ``₹`` rendering.

The lake-reading orchestration (``run_options_snapshot`` /
``_session_value_paisa`` / ``_discover_options``) is covered by the integration
suite (``tests/integration/test_dashboard_movers_options.py``) against a real
Parquet lake.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

from datetime import date
from decimal import Decimal

from brutex.dashboard.movers_options import (
    OptionMover,
    _monthly_expiries,
    _tier_expiry_by_underlying,
    _track_pool,
    build_options_payload,
    compute_option_mover,
    parse_option_symbol,
    rank_highest_oi,
    rank_oi_gainers,
    rank_oi_losers,
    rank_price_gainers,
    rank_price_losers,
    rank_value,
    rank_volume,
)
from brutex.data.moneyness import Moneyness

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
    )


class TestOptionMoverRendering:
    def test_name_whole_strike(self) -> None:
        assert _mover().name == "NIFTY 30Jun25 24000 CE"

    def test_name_fractional_strike(self) -> None:
        m = _mover(strike=Decimal("24000.5"))
        assert m.name == "NIFTY 30Jun25 24000.5 CE"

    def test_name_with_trailing_zero_strike_normalizes(self) -> None:
        # A strike carrying ``.00`` must still read as the integer 24000.
        m = _mover(strike=Decimal("24000.00"))
        assert m.name == "NIFTY 30Jun25 24000 CE"

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
        assert row["name"] == "NIFTY 30Jun25 24000 CE"
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
