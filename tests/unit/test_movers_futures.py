"""Unit tests for the Futures movers core (DEC-MOVERS-FUTURES-001) — pure, no FastAPI.

Covers every pure branch of :mod:`brutex.dashboard.movers_futures`: the contract
symbol parser, the near-month picker, the seven ranking functions, the
:class:`~brutex.dashboard.movers_futures.FuturesMover` ₹ rendering,
:func:`~brutex.dashboard.movers_futures.compute_futures_mover`, the payload
builder (every category key + the honest empty / no-mover notes), and the
``_prev_day_oi`` ``None`` branches (over a real ``tmp_path`` — the lake-reading
helper's filesystem branches, no DuckDB/route needed here).
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import ClassVar

import pytest

pytest.importorskip("polars")
pytest.importorskip("pyarrow")

from brutex.dashboard.movers_futures import (
    FuturesMover,
    PrevOiBaseline,
    _futures_row,
    _prev_day_oi,
    build_futures_payload,
    compute_futures_candidates,
    compute_futures_mover,
    format_contract_date,
    normalize_futures_tier,
    parse_future_symbol,
    rank_discount,
    rank_oi_gainers,
    rank_oi_losers,
    rank_premium,
    rank_price_gainers,
    rank_price_losers,
    rank_value,
    rank_volume,
    resolve_prev_oi,
    select_expiry_tier,
    select_near_month,
)
from brutex.data.models import CandleBar
from structlog.testing import capture_logs

# ── parse_future_symbol ───────────────────────────────────────────────────


class TestParseFutureSymbol:
    def test_index_future(self) -> None:
        assert parse_future_symbol("NSE-NIFTY-30Sep25-FUT") == ("NIFTY", date(2025, 9, 30))

    def test_stock_future(self) -> None:
        assert parse_future_symbol("NSE-RELIANCE-25Jun26-FUT") == ("RELIANCE", date(2026, 6, 25))

    # ── DEC-MOVERS-FUTURES-HYPHEN-PARSE-001: right-anchored via the canonical
    # grammar — a hyphenated underlying is preserved INTACT. ──

    def test_hyphenated_underlying_preserved(self) -> None:
        # Fails-before regression: the deleted LEFT-anchored local parse took
        # parts[1] → "BAJAJ", so the spot probe hit the nonexistent
        # bars/NSE/CASH/BAJAJ/ and premium was silently dropped for every
        # hyphenated F&O underlying (DEC-GROWW-HYPHEN-SYMBOL-RESOLVE-001).
        assert parse_future_symbol("NSE-BAJAJ-AUTO-30Sep25-FUT") == (
            "BAJAJ-AUTO",
            date(2025, 9, 30),
        )

    def test_multi_hyphen_underlying_preserved(self) -> None:
        # The grammar joins EVERY token between the exchange and the expiry —
        # a 3-token exotic name survives intact (right-anchored, not arity-bound).
        assert parse_future_symbol("NSE-A-B-C-30Sep25-FUT") == ("A-B-C", date(2025, 9, 30))

    def test_ampersand_underlying_was_and_remains_correct(self) -> None:
        # '&' names (M&M, M&MFIN) carry NO hyphen in the symbol grammar, so they
        # parsed correctly under the old left-anchored split too — this pins that
        # the delegation does not regress them (was-and-remains-correct).
        assert parse_future_symbol("NSE-M&M-30Sep25-FUT") == ("M&M", date(2025, 9, 30))
        assert parse_future_symbol("NSE-M&MFIN-25Jun26-FUT") == ("M&MFIN", date(2026, 6, 25))

    def test_delegation_parity_with_canonical_grammar(self) -> None:
        # parse_future_symbol IS the canonical parse_fno_symbol restricted to
        # FUT — pinned over well-formed, hyphenated, junk and option corpora so
        # the two grammars can never diverge again (CLAUDE.md §11 COMMON).
        from brutex.data.fno_symbol import parse_fno_symbol

        corpus = [
            "NSE-NIFTY-30Sep25-FUT",
            "NSE-BAJAJ-AUTO-30Sep25-FUT",
            "NSE-M&M-30Sep25-FUT",
            "BSE-SENSEX-25Sep25-FUT",
            "NSE-NIFTY-30Sep25-24000-CE",
            "NSE-NIFTY-30Xxx25-FUT",
            "NIFTY-FUT",
            "NSE--30Sep25-FUT",
            "NSE-NIFTY-30Jun75-FUT",
        ]
        for symbol in corpus:
            canonical = parse_fno_symbol(symbol)
            expected = (
                (canonical.underlying, canonical.expiry)
                if canonical is not None and canonical.instrument_type == "FUT"
                else None
            )
            assert parse_future_symbol(symbol) == expected, symbol

    def test_option_symbol_is_none(self) -> None:
        # An option ends -CE / -PE (decodes with instrument_type CE/PE) → not a future.
        assert parse_future_symbol("NSE-NIFTY-30Sep25-24000-CE") is None
        assert parse_future_symbol("NSE-NIFTY-30Sep25-24000-PE") is None

    def test_junk_too_few_parts_is_none(self) -> None:
        assert parse_future_symbol("NIFTY-FUT") is None

    def test_bad_month_is_none(self) -> None:
        assert parse_future_symbol("NSE-NIFTY-30Xxx25-FUT") is None

    def test_lenient_expiry_shapes_match_canonical(self) -> None:
        # SANCTIONED delta (DEC-MOVERS-FUTURES-HYPHEN-PARSE-001, disclosed in the
        # ledger): the deleted local twin was shape-stricter (exactly 7 chars,
        # title-case month) and REJECTED tokens the canonical strptime("%d%b%y")
        # grammar accepts — so a junk/foreign lake dir like NSE-NIFTY-30SEP25-FUT
        # was visible in /db + options but invisible on the Futures tab. The one
        # grammar now judges both identically. Every well-formed vendor symbol
        # (DDMmmYY title-case) is byte-identical either way.
        assert parse_future_symbol("NSE-NIFTY-3Sep25-FUT") == ("NIFTY", date(2025, 9, 3))
        assert parse_future_symbol("NSE-NIFTY-30SEP25-FUT") == ("NIFTY", date(2025, 9, 30))
        assert parse_future_symbol("NSE-NIFTY-30sep25-FUT") == ("NIFTY", date(2025, 9, 30))

    def test_non_numeric_day_is_none(self) -> None:
        assert parse_future_symbol("NSE-NIFTY-XXSep25-FUT") is None

    def test_non_numeric_year_is_none(self) -> None:
        assert parse_future_symbol("NSE-NIFTY-30SepYY-FUT") is None

    def test_day_out_of_range_is_none(self) -> None:
        # 31 Feb is not a real date → ValueError caught → None.
        assert parse_future_symbol("NSE-NIFTY-31Feb25-FUT") is None

    def test_empty_underlying_is_none(self) -> None:
        # EXCH then an empty underlying part then expiry then FUT.
        assert parse_future_symbol("NSE--30Sep25-FUT") is None

    # ── DEC-FNO-EXPIRY-YEAR-PIVOT-001: the SHARED [2019, 2059] year window ──

    def test_out_of_window_year_is_none(self) -> None:
        # Fails-before regression: _CENTURY=2000 decoded "75" to a fabricated
        # 2075 expiry (while the canonical fno_symbol parser pivoted it to 1975
        # — a live one-source-of-truth divergence). Both now reject via the ONE
        # shared expiry_year_in_window predicate.
        assert parse_future_symbol("NSE-NIFTY-30Jun75-FUT") is None

    def test_below_window_year_is_none(self) -> None:
        assert parse_future_symbol("NSE-NIFTY-01Jan18-FUT") is None

    def test_above_window_year_is_none(self) -> None:
        assert parse_future_symbol("NSE-NIFTY-30Jun60-FUT") is None

    def test_boundary_years_accepted(self) -> None:
        assert parse_future_symbol("NSE-NIFTY-31Dec19-FUT") == ("NIFTY", date(2019, 12, 31))
        assert parse_future_symbol("NSE-NIFTY-30Jun59-FUT") == ("NIFTY", date(2059, 6, 30))


# ── select_near_month ─────────────────────────────────────────────────────


class TestSelectNearMonth:
    def test_front_month_picked(self) -> None:
        items = [("FAR", date(2025, 12, 25)), ("NEAR", date(2025, 9, 30))]
        assert select_near_month(items, on_date=date(2025, 9, 1)) == "NEAR"

    def test_all_expired_is_none(self) -> None:
        items = [("A", date(2025, 1, 30)), ("B", date(2025, 2, 27))]
        assert select_near_month(items, on_date=date(2025, 9, 1)) is None

    def test_exact_on_date_is_live(self) -> None:
        # A contract expiring ON the snapshot date is still live (>=).
        items = [("TODAY", date(2025, 9, 30)), ("LATER", date(2025, 10, 30))]
        assert select_near_month(items, on_date=date(2025, 9, 30)) == "TODAY"

    def test_tie_breaks_on_symbol(self) -> None:
        # Two contracts sharing the earliest live expiry → deterministic symbol pick.
        items = [("ZZZ", date(2025, 9, 30)), ("AAA", date(2025, 9, 30))]
        assert select_near_month(items, on_date=date(2025, 9, 1)) == "AAA"


# ── select_expiry_tier (Near / Next / Far) ────────────────────────────────


class TestSelectExpiryTier:
    #: Three serial monthly contracts (out of expiry order, to prove sorting).
    _ITEMS: ClassVar[list[tuple[str, date]]] = [
        ("FAR", date(2025, 11, 27)),
        ("NEAR", date(2025, 9, 25)),
        ("NEXT", date(2025, 10, 30)),
    ]

    def test_near_is_front_month(self) -> None:
        assert select_expiry_tier(self._ITEMS, on_date=date(2025, 9, 1), tier="near") == "NEAR"

    def test_next_is_second_serial(self) -> None:
        assert select_expiry_tier(self._ITEMS, on_date=date(2025, 9, 1), tier="next") == "NEXT"

    def test_far_is_third_serial(self) -> None:
        assert select_expiry_tier(self._ITEMS, on_date=date(2025, 9, 1), tier="far") == "FAR"

    def test_default_tier_is_near(self) -> None:
        assert select_expiry_tier(self._ITEMS, on_date=date(2025, 9, 1)) == "NEAR"

    def test_unknown_tier_falls_back_to_near(self) -> None:
        assert select_expiry_tier(self._ITEMS, on_date=date(2025, 9, 1), tier="zzz") == "NEAR"

    def test_tier_deeper_than_available_is_none(self) -> None:
        # Only two live contracts but the Far (index 2) is requested → honest None,
        # never a fabricated or wrong-tier row (Golden Rule #1).
        two = [("NEAR", date(2025, 9, 25)), ("NEXT", date(2025, 10, 30))]
        assert select_expiry_tier(two, on_date=date(2025, 9, 1), tier="far") is None

    def test_expired_contracts_excluded_then_tiered(self) -> None:
        # An already-expired contract is dropped BEFORE tiering, so Near is the
        # earliest LIVE one, not the expired one.
        items = [
            ("DEAD", date(2025, 1, 30)),
            ("NEAR", date(2025, 9, 25)),
            ("NEXT", date(2025, 10, 30)),
        ]
        assert select_expiry_tier(items, on_date=date(2025, 9, 1), tier="near") == "NEAR"
        assert select_expiry_tier(items, on_date=date(2025, 9, 1), tier="next") == "NEXT"


# ── normalize_futures_tier ────────────────────────────────────────────────


class TestNormalizeFuturesTier:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Near", "near"),
            ("Next", "next"),
            ("Far", "far"),
            ("NEAR", "near"),
            ("near", "near"),
            (None, "near"),
            ("", "near"),
            ("foo", "near"),
        ],
    )
    def test_canonicalises(self, raw: str | None, expected: str) -> None:
        assert normalize_futures_tier(raw) == expected


# ── FuturesMover ₹ rendering + compute_futures_mover ──────────────────────


def _mover(
    *,
    underlying: str = "NIFTY",
    symbol: str = "NSE-NIFTY-30Sep25-FUT",
    ltp: str = "100",
    spot: str | None = "98",
    premium: str | None = "2",
    pct: str | None = "5",
    oi: int | None = 1000,
    oi_chg_pct: str | None = "3",
    volume: int = 50,
    value_paisa: int = 0,
) -> FuturesMover:
    """A FuturesMover with sensible defaults for the ranking / rendering tests."""
    parsed = parse_future_symbol(symbol)
    return FuturesMover(
        underlying=underlying,
        expiry=parsed[1] if parsed is not None else None,
        symbol=symbol,
        exchange="NSE",
        segment="FNO",
        ltp=Decimal(ltp),
        spot=Decimal(spot) if spot is not None else None,
        premium=Decimal(premium) if premium is not None else None,
        pct_change=Decimal(pct) if pct is not None else None,
        oi=oi,
        oi_chg_pct=Decimal(oi_chg_pct) if oi_chg_pct is not None else None,
        volume=volume,
        value_paisa=value_paisa,
    )


class TestFuturesMoverRendering:
    def test_inr_strings_present(self) -> None:
        m = _mover(ltp="23650", spot="23622.9", premium="27.1")
        assert m.ltp_inr == "₹23,650.00"
        assert m.spot_inr == "₹23,622.90"
        assert m.premium_inr == "₹27.10"

    def test_negative_premium_renders(self) -> None:
        m = _mover(ltp="100", spot="105", premium="-5")
        # format_inr prefixes ₹ then the signed amount (negative premium = discount).
        assert m.premium_inr == "₹-5.00"

    def test_none_spot_and_premium_render_none(self) -> None:
        m = _mover(spot=None, premium=None)
        assert m.spot_inr is None
        assert m.premium_inr is None


class TestFormatContractDate:
    """The shared deterministic ``DD-Mon-YYYY`` contract-date formatter."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (date(2025, 9, 30), "30-Sep-2025"),
            (date(2025, 1, 1), "01-Jan-2025"),
            (date(2026, 12, 5), "05-Dec-2026"),
            (date(2025, 6, 30), "30-Jun-2025"),
        ],
    )
    def test_deterministic_label(self, value: date, expected: str) -> None:
        # Locale-stable (uses the fixed English month map, never strftime("%b")).
        assert format_contract_date(value) == expected


class TestFuturesMoverName:
    """The FULL dated contract NAME label + its graceful fallbacks (DEC-MOVERS-FULL-CONTRACT-NAME-001)."""

    def test_dated_name_for_parseable_contract(self) -> None:
        m = _mover(underlying="NIFTY", symbol="NSE-NIFTY-30Sep25-FUT")
        assert m.name == "NIFTY 30-Sep-2025 FUT"

    def test_near_next_far_render_different_names(self) -> None:
        # The whole point: three serial-month contracts of ONE underlying must read
        # as DISTINCT names (regression for the Near==Next==Far ambiguity bug).
        near = _mover(underlying="NIFTY", symbol="NSE-NIFTY-25Sep25-FUT")
        nxt = _mover(underlying="NIFTY", symbol="NSE-NIFTY-30Oct25-FUT")
        far = _mover(underlying="NIFTY", symbol="NSE-NIFTY-27Nov25-FUT")
        names = {near.name, nxt.name, far.name}
        assert names == {
            "NIFTY 25-Sep-2025 FUT",
            "NIFTY 30-Oct-2025 FUT",
            "NIFTY 27-Nov-2025 FUT",
        }
        assert len(names) == 3

    def test_unparseable_symbol_falls_back_to_symbol(self) -> None:
        # expiry None (unparseable) but a symbol present → fall back to the raw symbol,
        # never blank, never a crash (Golden Rule #1).
        m = FuturesMover(
            underlying="NIFTY",
            expiry=None,
            symbol="NSE-NIFTY-GARBAGE",
            exchange="NSE",
            segment="FNO",
            ltp=Decimal("100"),
            spot=None,
            premium=None,
            pct_change=None,
            oi=None,
            oi_chg_pct=None,
            volume=0,
            value_paisa=0,
        )
        assert m.name == "NSE-NIFTY-GARBAGE"

    def test_unparseable_and_empty_symbol_falls_back_to_underlying(self) -> None:
        # expiry None AND an empty symbol → fall back to the bare underlying.
        m = FuturesMover(
            underlying="NIFTY",
            expiry=None,
            symbol="",
            exchange="NSE",
            segment="FNO",
            ltp=Decimal("100"),
            spot=None,
            premium=None,
            pct_change=None,
            oi=None,
            oi_chg_pct=None,
            volume=0,
            value_paisa=0,
        )
        assert m.name == "NIFTY"

    def test_compute_futures_mover_populates_expiry(self) -> None:
        m = compute_futures_mover(
            underlying="NIFTY",
            symbol="NSE-NIFTY-30Sep25-FUT",
            exchange="NSE",
            ltp=Decimal("100"),
            volume=0,
            value_paisa=0,
            oi=None,
            prev_close=None,
            spot_ltp=None,
            prev_oi=None,
        )
        assert m.expiry == date(2025, 9, 30)
        assert m.name == "NIFTY 30-Sep-2025 FUT"

    def test_compute_futures_mover_unparseable_symbol_expiry_none(self) -> None:
        m = compute_futures_mover(
            underlying="NIFTY",
            symbol="NSE-NIFTY-junk",
            exchange="NSE",
            ltp=Decimal("100"),
            volume=0,
            value_paisa=0,
            oi=None,
            prev_close=None,
            spot_ltp=None,
            prev_oi=None,
        )
        assert m.expiry is None
        assert m.name == "NSE-NIFTY-junk"


class TestFuturesMoverNameProperty:
    """Property: the NAME label is ALWAYS a non-empty string for any row."""

    @pytest.mark.parametrize(
        "symbol",
        [
            "NSE-NIFTY-30Sep25-FUT",  # parseable → dated label
            "NSE-NIFTY-30Xxx25-FUT",  # bad month → fallback to symbol
            "NSE-NIFTY-junk",  # unparseable → fallback to symbol
            "",  # empty symbol → fallback to underlying
        ],
    )
    def test_name_is_never_empty(self, symbol: str) -> None:
        parsed = parse_future_symbol(symbol)
        m = FuturesMover(
            underlying="NIFTY",
            expiry=parsed[1] if parsed is not None else None,
            symbol=symbol,
            exchange="NSE",
            segment="FNO",
            ltp=Decimal("100"),
            spot=None,
            premium=None,
            pct_change=None,
            oi=None,
            oi_chg_pct=None,
            volume=0,
            value_paisa=0,
        )
        assert isinstance(m.name, str)
        assert m.name != ""


class TestComputeFuturesMover:
    def test_full_row(self) -> None:
        m = compute_futures_mover(
            underlying="NIFTY",
            symbol="NSE-NIFTY-30Sep25-FUT",
            exchange="NSE",
            ltp=Decimal("110"),
            volume=70,
            value_paisa=5_000_000_000,
            oi=1100,
            prev_close=Decimal("100"),
            spot_ltp=Decimal("108"),
            prev_oi=1000,
        )
        assert m.ltp == Decimal("110.00")
        assert m.spot == Decimal("108.00")
        assert m.premium == Decimal("2.00")
        assert m.pct_change == Decimal("10")
        assert m.oi_chg_pct == Decimal("10")
        assert m.segment == "FNO"
        assert m.value_paisa == 5_000_000_000
        assert m.value_crore == "5.00"  # 5e9 paisa = ₹5,00,00,000 = 5.00 cr

    def test_none_baselines(self) -> None:
        # No prev_close → pct None; no spot → premium None; no prev_oi → oi% None.
        m = compute_futures_mover(
            underlying="X",
            symbol="NSE-X-30Sep25-FUT",
            exchange="NSE",
            ltp=Decimal("100"),
            volume=0,
            value_paisa=0,
            oi=500,
            prev_close=None,
            spot_ltp=None,
            prev_oi=None,
        )
        assert m.pct_change is None
        assert m.premium is None
        assert m.spot is None
        assert m.oi_chg_pct is None

    def test_zero_prev_oi_yields_none(self) -> None:
        m = compute_futures_mover(
            underlying="X",
            symbol="NSE-X-30Sep25-FUT",
            exchange="NSE",
            ltp=Decimal("100"),
            volume=0,
            value_paisa=0,
            oi=500,
            prev_close=Decimal("90"),
            spot_ltp=Decimal("99"),
            prev_oi=0,
        )
        assert m.oi_chg_pct is None

    def test_none_current_oi_yields_none_oi_pct(self) -> None:
        m = compute_futures_mover(
            underlying="X",
            symbol="NSE-X-30Sep25-FUT",
            exchange="NSE",
            ltp=Decimal("100"),
            volume=0,
            value_paisa=0,
            oi=None,
            prev_close=Decimal("90"),
            spot_ltp=Decimal("99"),
            prev_oi=1000,
        )
        assert m.oi_chg_pct is None
        assert m.oi is None


# ── ranking functions ─────────────────────────────────────────────────────


class TestRanking:
    def test_price_gainers_and_losers(self) -> None:
        up = _mover(symbol="UP", pct="5")
        down = _mover(symbol="DOWN", pct="-3")
        flat = _mover(symbol="FLAT", pct="0")
        none = _mover(symbol="NONE", pct=None)
        pool = [up, down, flat, none]
        assert [m.symbol for m in rank_price_gainers(pool, 10)] == ["UP"]
        assert [m.symbol for m in rank_price_losers(pool, 10)] == ["DOWN"]

    def test_premium_and_discount(self) -> None:
        prem = _mover(symbol="PREM", premium="3")
        disc = _mover(symbol="DISC", premium="-2")
        flat = _mover(symbol="FLAT", premium="0")
        none = _mover(symbol="NONE", premium=None)
        pool = [prem, disc, flat, none]
        assert [m.symbol for m in rank_premium(pool, 10)] == ["PREM"]
        assert [m.symbol for m in rank_discount(pool, 10)] == ["DISC"]

    def test_volume(self) -> None:
        a = _mover(symbol="A", volume=100)
        b = _mover(symbol="B", volume=0)
        assert [m.symbol for m in rank_volume([a, b], 10)] == ["A"]

    def test_value(self) -> None:
        # DEC-MOVERS-VWAP-VALUE-001: rank by turnover desc; a zero-value future drops out.
        big = _mover(symbol="BIG", value_paisa=900_000_000)
        small = _mover(symbol="SMALL", value_paisa=100_000_000)
        zero = _mover(symbol="ZERO", value_paisa=0)
        assert [m.symbol for m in rank_value([small, big, zero], 10)] == ["BIG", "SMALL"]

    def test_oi_gainers_and_losers(self) -> None:
        up = _mover(symbol="UP", oi_chg_pct="4")
        down = _mover(symbol="DOWN", oi_chg_pct="-1")
        flat = _mover(symbol="FLAT", oi_chg_pct="0")
        none = _mover(symbol="NONE", oi_chg_pct=None)
        pool = [up, down, flat, none]
        assert [m.symbol for m in rank_oi_gainers(pool, 10)] == ["UP"]
        assert [m.symbol for m in rank_oi_losers(pool, 10)] == ["DOWN"]

    def test_top_n_caps_and_orders_descending(self) -> None:
        big = _mover(symbol="BIG", pct="9")
        mid = _mover(symbol="MID", pct="5")
        small = _mover(symbol="SMALL", pct="1")
        assert [m.symbol for m in rank_price_gainers([small, big, mid], 2)] == ["BIG", "MID"]

    def test_tiebreak_is_deterministic(self) -> None:
        # Equal pct → ordered by symbol (the (symbol, exchange, segment) tiebreak).
        z = _mover(symbol="ZZZ", pct="5")
        a = _mover(symbol="AAA", pct="5")
        assert [m.symbol for m in rank_price_gainers([z, a], 10)] == ["AAA", "ZZZ"]


# ── build_futures_payload ─────────────────────────────────────────────────

_CATEGORY_KEYS = (
    "price_gainers",
    "price_losers",
    "premium",
    "discount",
    "volume",
    "value",
    "oi_gainers",
    "oi_losers",
)


class TestBuildFuturesPayload:
    def test_all_category_keys_present(self) -> None:
        payload = build_futures_payload(
            [_mover()], interval="1day", date="2025-09-01", time=None, top_n=5, skipped=0
        )
        for key in _CATEGORY_KEYS:
            assert key in payload
        assert payload["interval"] == "1day"
        assert payload["date"] == "2025-09-01"
        assert payload["instrument_count"] == 1
        assert payload["skipped_count"] == 0

    def test_row_fields_complete(self) -> None:
        m = _mover(
            ltp="100",
            spot="98",
            premium="2",
            pct="5",
            oi=1000,
            oi_chg_pct="3",
            volume=50,
            value_paisa=5_000_000_000,
        )
        payload = build_futures_payload(
            [m], interval="1day", date="2025-09-01", time=None, top_n=5, skipped=0
        )
        row = payload["price_gainers"][0]
        # The NAME is now the FULL dated contract label (DEC-MOVERS-FULL-CONTRACT-NAME-001),
        # and the raw symbol is STILL carried in the payload.
        assert row["name"] == "NIFTY 30-Sep-2025 FUT"
        assert row["symbol"] == "NSE-NIFTY-30Sep25-FUT"
        assert row["ltp"] == "₹100.00"
        assert row["spot"] == "₹98.00"
        assert row["premium"] == "₹2.00"
        assert row["pct_change"] == "5"
        assert row["oi"] == 1000
        assert row["oi_chg_pct"] == "3"
        assert row["volume"] == 50
        assert row["value_cr"] == "5.00"  # 5e9 paisa = 5.00 cr (DEC-MOVERS-VWAP-VALUE-001)

    def test_null_optional_row_fields(self) -> None:
        m = _mover(spot=None, premium=None, pct=None, oi=None, oi_chg_pct=None)
        payload = build_futures_payload(
            [m], interval="1day", date="2025-09-01", time=None, top_n=5, skipped=0
        )
        row = payload["volume"][0]
        assert row["spot"] is None
        assert row["premium"] is None
        assert row["pct_change"] is None
        assert row["oi"] is None
        assert row["oi_chg_pct"] is None

    def test_empty_lake_note(self) -> None:
        payload = build_futures_payload(
            [], interval="1day", date="2025-09-01", time=None, top_n=5, skipped=0
        )
        assert payload["instrument_count"] == 0
        assert "pull the F&O futures first" in payload["note"]

    def test_present_but_no_mover_note(self) -> None:
        # A future in the lake but every category empty (skipped-only) → "pick another".
        payload = build_futures_payload(
            [], interval="5minute", date="2025-09-01", time="09:20", top_n=5, skipped=2
        )
        assert payload["instrument_count"] == 2
        assert "none had a bar" in payload["note"]
        assert "09:20" in payload["note"]

    def test_happy_note_mentions_premium_and_previous_close(self) -> None:
        payload = build_futures_payload(
            [_mover()], interval="1day", date="2025-09-01", time=None, top_n=5, skipped=0
        )
        assert "previous close" in payload["note"]
        assert "premium" in payload["note"]


# ── _prev_day_oi (filesystem None branches over tmp_path) ──────────────────


def _day_bar(*, day: date, close: str, oi: int | None) -> CandleBar:
    """A 1-day OHLCV bar labelled at 09:15 IST (03:45 UTC) on *day*, with OI."""
    c = Decimal(close)
    return CandleBar(
        timestamp=datetime(day.year, day.month, day.day, 3, 45, tzinfo=UTC),
        open=c,
        high=c,
        low=c,
        close=c,
        volume=1,
        open_interest=oi,
    )


class TestPrevDayOi:
    def test_no_1day_dir_is_none(self, tmp_path: Path) -> None:
        assert (
            _prev_day_oi(
                tmp_path,
                exchange="NSE",
                segment="FNO",
                symbol="NSE-NIFTY-30Sep25-FUT",
                day=date(2025, 9, 1),
            )
            is None
        )

    def test_prior_day_oi_returned(self, tmp_path: Path) -> None:
        from brutex.data.parquet_writer import ParquetWriter

        ParquetWriter(lake_root=tmp_path).write(
            exchange="NSE",
            segment="FNO",
            symbol="NSE-NIFTY-30Sep25-FUT",
            interval="1day",
            bars=[
                _day_bar(day=date(2025, 8, 28), close="100", oi=900),
                _day_bar(day=date(2025, 8, 29), close="101", oi=1234),
            ],
        )
        got = _prev_day_oi(
            tmp_path,
            exchange="NSE",
            segment="FNO",
            symbol="NSE-NIFTY-30Sep25-FUT",
            day=date(2025, 9, 1),
        )
        assert got == 1234

    def test_only_same_day_has_no_prior(self, tmp_path: Path) -> None:
        from brutex.data.parquet_writer import ParquetWriter

        ParquetWriter(lake_root=tmp_path).write(
            exchange="NSE",
            segment="FNO",
            symbol="NSE-NIFTY-30Sep25-FUT",
            interval="1day",
            bars=[_day_bar(day=date(2025, 9, 1), close="100", oi=500)],
        )
        assert (
            _prev_day_oi(
                tmp_path,
                exchange="NSE",
                segment="FNO",
                symbol="NSE-NIFTY-30Sep25-FUT",
                day=date(2025, 9, 1),
            )
            is None
        )

    def test_prior_day_with_null_oi_is_none(self, tmp_path: Path) -> None:
        from brutex.data.parquet_writer import ParquetWriter

        ParquetWriter(lake_root=tmp_path).write(
            exchange="NSE",
            segment="FNO",
            symbol="NSE-NIFTY-30Sep25-FUT",
            interval="1day",
            bars=[_day_bar(day=date(2025, 8, 29), close="100", oi=None)],
        )
        assert (
            _prev_day_oi(
                tmp_path,
                exchange="NSE",
                segment="FNO",
                symbol="NSE-NIFTY-30Sep25-FUT",
                day=date(2025, 9, 1),
            )
            is None
        )


# ── compute_futures_candidates: per-exchange discovery filter ──────────────
#
# Standalone unit coverage of the per-exchange discovery ``continue`` branch
# of ``compute_futures_candidates`` so the touched-module 100% coverage gate
# (which runs this stem file in isolation) sees it. The corrupt-Parquet skip
# block is covered by the stem integration file's
# ``TestComputeFuturesCandidatesBranches`` (Golden Rule #11 — not duplicated).


def _fut_min_bar(*, close: str, volume: int, oi: int) -> CandleBar:
    """A flat 1-minute futures bar at 09:15 IST (03:45 UTC) on 2024-06-13."""
    c = Decimal(close)
    return CandleBar(
        timestamp=datetime(2024, 6, 13, 3, 45, tzinfo=UTC),
        open=c,
        high=c,
        low=c,
        close=c,
        volume=volume,
        open_interest=oi,
    )


class TestComputeFuturesCandidatesExchangeFilter:
    def test_exchange_filter_skips_other_exchange(self, tmp_path: Path) -> None:
        """A future whose exchange != the requested one hits the discovery ``continue``."""
        from brutex.data.parquet_writer import ParquetWriter

        writer = ParquetWriter(lake_root=tmp_path)
        writer.write(
            exchange="NSE",
            segment="FNO",
            symbol="NSE-NIFTY-27Jun24-FUT",
            interval="1minute",
            bars=[_fut_min_bar(close="22010", volume=100, oi=1000)],
        )
        writer.write(
            exchange="BSE",
            segment="FNO",
            symbol="BSE-SENSEX-27Jun24-FUT",
            interval="1minute",
            bars=[_fut_min_bar(close="77010", volume=50, oi=500)],
        )
        nse = compute_futures_candidates(
            lake_root=tmp_path, interval="1day", date="2024-06-13", time=None, exchange="NSE"
        )
        # The BSE future is filtered out by the per-exchange discovery ``continue``.
        assert {m.underlying for m in nse.movers} == {"NIFTY"}


# ── DEC-MOVERS-FUTURES-HYPHEN-PARSE-001: end-to-end over a real lake ──
#
# The fails-before flip on the REAL compute path: with the left-anchored local
# parse, ``NSE-BAJAJ-AUTO-30Sep25-FUT`` bucketed under ``"BAJAJ"``, so
# ``_underlying_spot_ltp`` probed the nonexistent ``bars/NSE/CASH/BAJAJ/`` and
# the row shipped ``spot=None`` / ``premium=None`` despite the spot partition
# existing under the correct ``BAJAJ-AUTO`` key.


def _spot_min_bar(*, close: str) -> CandleBar:
    """A flat 1-minute SPOT bar at 09:15 IST (03:45 UTC) on 2024-06-13."""
    c = Decimal(close)
    return CandleBar(
        timestamp=datetime(2024, 6, 13, 3, 45, tzinfo=UTC),
        open=c,
        high=c,
        low=c,
        close=c,
        volume=100,
        open_interest=None,
    )


def _write_hyphen_lake(tmp_path: Path) -> None:
    """A real lake: one hyphenated + one plain future, each with its spot partition."""
    from brutex.data.parquet_writer import ParquetWriter

    writer = ParquetWriter(lake_root=tmp_path)
    writer.write(
        exchange="NSE",
        segment="FNO",
        symbol="NSE-BAJAJ-AUTO-27Jun24-FUT",
        interval="1minute",
        bars=[_fut_min_bar(close="8130", volume=70, oi=700)],
    )
    writer.write(
        exchange="NSE",
        segment="CASH",
        symbol="BAJAJ-AUTO",
        interval="1minute",
        bars=[_spot_min_bar(close="8010")],
    )
    writer.write(
        exchange="NSE",
        segment="FNO",
        symbol="NSE-NIFTY-27Jun24-FUT",
        interval="1minute",
        bars=[_fut_min_bar(close="22010", volume=100, oi=1000)],
    )
    writer.write(
        exchange="NSE",
        segment="INDEX",
        symbol="NIFTY",
        interval="1minute",
        bars=[_spot_min_bar(close="22000")],
    )


class TestHyphenatedUnderlyingEndToEnd:
    def test_spot_and_premium_present_for_hyphenated_underlying(self, tmp_path: Path) -> None:
        """The fails-before flip: BAJAJ-AUTO keeps its full name + spot + premium."""
        _write_hyphen_lake(tmp_path)
        got = compute_futures_candidates(
            lake_root=tmp_path, interval="1day", date="2024-06-13", time=None, exchange="NSE"
        )
        by_underlying = {m.underlying: m for m in got.movers}
        # The underlying is the FULL hyphenated name, never the "BAJAJ" truncation.
        assert set(by_underlying) == {"BAJAJ-AUTO", "NIFTY"}
        bajaj = by_underlying["BAJAJ-AUTO"]
        # Spot resolved from the REAL bars/NSE/CASH/BAJAJ-AUTO partition
        # (pre-fix: spot=None / premium=None — the silent drop).
        assert bajaj.spot == Decimal("8010.00")
        assert bajaj.premium == Decimal("120.00")
        assert bajaj.symbol == "NSE-BAJAJ-AUTO-27Jun24-FUT"
        assert got.skipped == 0

    def test_non_hyphenated_row_byte_parity(self, tmp_path: Path) -> None:
        """A plain underlying's row is byte-identical to the pre-delegation output."""
        _write_hyphen_lake(tmp_path)
        got = compute_futures_candidates(
            lake_root=tmp_path, interval="1day", date="2024-06-13", time=None, exchange="NSE"
        )
        nifty = next(m for m in got.movers if m.underlying == "NIFTY")
        # The exact pre-fix NIFTY values (the left-anchored parse already handled
        # plain names) — zero behavior change for non-hyphenated underlyings.
        assert nifty.symbol == "NSE-NIFTY-27Jun24-FUT"
        assert nifty.expiry == date(2024, 6, 27)
        assert nifty.ltp == Decimal("22010.00")
        assert nifty.spot == Decimal("22000.00")
        assert nifty.premium == Decimal("10.00")
        assert nifty.oi == 1000
        assert nifty.volume == 100

    def test_warm_grid_cache_serves_correct_hyphenated_rows(self, tmp_path: Path) -> None:
        """The warmed cache entry carries the CORRECT rows (never re-cached truncations).

        Mirrors the ``futures_warm_grid`` → ``candidate_key`` →
        ``compute_futures_candidates`` wiring the app's post-pull warmer uses
        (DEC-MOVERS-WARM-GRID-001): the grid coordinate is warmed once, then the
        route-shaped second read is an O(1) cache HIT (compute NOT re-run) serving
        the full hyphenated underlying + spot + premium — pre-fix the warmer
        persisted the truncated spot-less rows as cache hits.
        """
        from brutex.dashboard.movers_cache import MoversSnapshotCache, candidate_key
        from brutex.dashboard.movers_warm import futures_warm_grid

        _write_hyphen_lake(tmp_path)
        grid = futures_warm_grid()
        coord = next(c for c in grid if c["exchange"] == "NSE" and c["expiry_tier"] == "near")
        cache = MoversSnapshotCache(capacity=16)
        key = candidate_key(
            asset="futures",
            exchange=coord["exchange"],
            interval="1day",
            date="2024-06-13",
            time=None,
            expiry_tier=coord["expiry_tier"],
        )
        computes: list[int] = []

        def _compute() -> object:
            computes.append(1)
            return compute_futures_candidates(
                lake_root=tmp_path,
                interval="1day",
                date="2024-06-13",
                time=None,
                exchange=coord["exchange"],
                expiry_tier=coord["expiry_tier"] or "near",
            )

        warmed = cache.get_or_compute(key, generation=1, compute=_compute)
        served = cache.get_or_compute(key, generation=1, compute=_compute)
        assert len(computes) == 1  # the second read is a cache HIT, no re-scan.
        assert served is warmed
        bajaj = next(m for m in served.movers if m.underlying == "BAJAJ-AUTO")  # type: ignore[attr-defined]
        assert bajaj.spot == Decimal("8010.00")
        assert bajaj.premium == Decimal("120.00")


# ── DEC-MOVERS-MIN-VOLUME-001: the opt-in Top-Volume floor (futures) ──


class TestFuturesMinVolume:
    def test_rank_volume_default_zero_byte_identical(self) -> None:
        movers = [
            _mover(symbol="NSE-A-30Sep25-FUT", volume=0),
            _mover(symbol="NSE-B-30Sep25-FUT", volume=5),
        ]
        assert rank_volume(movers, 10) == rank_volume(movers, 10, min_volume=0)
        assert [m.volume for m in rank_volume(movers, 10)] == [5]

    def test_rank_volume_floor_drops_thin(self) -> None:
        movers = [
            _mover(symbol="NSE-A-30Sep25-FUT", volume=100),
            _mover(symbol="NSE-B-30Sep25-FUT", volume=2000),
        ]
        assert [m.volume for m in rank_volume(movers, 10, min_volume=1000)] == [2000]

    def test_payload_floor_applies_to_volume_only(self) -> None:
        thin = _mover(symbol="NSE-A-30Sep25-FUT", volume=100)
        thick = _mover(symbol="NSE-B-30Sep25-FUT", volume=9000)
        payload = build_futures_payload(
            [thin, thick],
            interval="1day",
            date="2025-06-13",
            time=None,
            top_n=10,
            skipped=0,
            min_volume=1000,
        )
        assert payload["min_volume"] == 1000
        assert len(payload["volume"]) == 1
        assert "Min volume ≥ 1,000." in payload["note"]
        # The price rankings ignore the volume floor.
        assert len(payload["price_gainers"]) == 2

    def test_payload_default_floor_no_note(self) -> None:
        payload = build_futures_payload(
            [_mover()], interval="1day", date="2025-06-13", time=None, top_n=10, skipped=0
        )
        assert payload["min_volume"] == 0
        assert "Min volume" not in payload["note"]

    def test_negative_floor_normalises(self) -> None:
        payload = build_futures_payload(
            [_mover()],
            interval="1day",
            date="2025-06-13",
            time=None,
            top_n=10,
            skipped=0,
            min_volume=-3,
        )
        assert payload["min_volume"] == 0


# ── resolve_prev_oi: the enriched OI baseline (value + found session date) ──
# DEC-MOVERS-PREVCLOSE-CONTIGUITY-001.


def _ist_min_bar(*, day: date, hh: int, mm: int, close: str, oi: int | None) -> CandleBar:
    """A flat 1-minute bar at IST ``hh:mm`` on *day* (IST = UTC + 05:30, no DST)."""
    c = Decimal(close)
    total = hh * 60 + mm - (5 * 60 + 30)
    return CandleBar(
        timestamp=datetime(day.year, day.month, day.day, total // 60, total % 60, tzinfo=UTC),
        open=c,
        high=c,
        low=c,
        close=c,
        volume=10,
        open_interest=oi,
    )


_RPO_FUT = "NSE-NIFTY-30Sep25-FUT"


def _rpo_write(tmp_path: Path, *, interval: str, bars: list[CandleBar]) -> None:
    from brutex.data.parquet_writer import ParquetWriter

    ParquetWriter(lake_root=tmp_path).write(
        exchange="NSE", segment="FNO", symbol=_RPO_FUT, interval=interval, bars=bars
    )


class TestResolvePrevOi:
    def test_1day_arm_carries_the_prior_bars_ist_date(self, tmp_path: Path) -> None:
        _rpo_write(
            tmp_path,
            interval="1day",
            bars=[
                _day_bar(day=date(2025, 8, 28), close="100", oi=900),
                _day_bar(day=date(2025, 8, 29), close="101", oi=1234),
            ],
        )
        got = resolve_prev_oi(
            tmp_path, exchange="NSE", segment="FNO", symbol=_RPO_FUT, day=date(2025, 9, 1)
        )
        assert got == PrevOiBaseline(value=1234, found_date=date(2025, 8, 29))

    def test_minute_fallback_arm_carries_the_recovered_date(self, tmp_path: Path) -> None:
        _rpo_write(
            tmp_path,
            interval="1minute",
            bars=[_ist_min_bar(day=date(2025, 8, 29), hh=15, mm=29, close="99.00", oi=4321)],
        )
        got = resolve_prev_oi(
            tmp_path, exchange="NSE", segment="FNO", symbol=_RPO_FUT, day=date(2025, 9, 1)
        )
        assert got == PrevOiBaseline(value=4321, found_date=date(2025, 8, 29))

    def test_neither_source_is_none_none(self, tmp_path: Path) -> None:
        got = resolve_prev_oi(
            tmp_path, exchange="NSE", segment="FNO", symbol=_RPO_FUT, day=date(2025, 9, 1)
        )
        assert got == PrevOiBaseline(value=None, found_date=None)

    def test_noncontiguous_1day_arm_judged_false(self, tmp_path: Path) -> None:
        """1day arm: the prior OI bar sits on Wed 09-24 while the calendar expects
        Fri 09-26 (the lake hole) — provenance judges strictly False."""
        from brutex.dashboard.movers_snapshot import prev_baseline_provenance

        _rpo_write(
            tmp_path,
            interval="1day",
            bars=[_day_bar(day=date(2025, 9, 24), close="100", oi=777)],
        )
        got = resolve_prev_oi(
            tmp_path, exchange="NSE", segment="FNO", symbol=_RPO_FUT, day=date(2025, 9, 29)
        )
        assert got == PrevOiBaseline(value=777, found_date=date(2025, 9, 24))
        assert prev_baseline_provenance(got.found_date, date(2025, 9, 29), exchange="NSE") == (
            "2025-09-24",
            False,
        )

    def test_noncontiguous_minute_arm_judged_false(self, tmp_path: Path) -> None:
        """Minute-fallback arm: the recovered OI day (Thu 09-25) is not the
        immediately-preceding session (Fri 09-26 exists on the calendar)."""
        from brutex.dashboard.movers_snapshot import prev_baseline_provenance

        _rpo_write(
            tmp_path,
            interval="1minute",
            bars=[_ist_min_bar(day=date(2025, 9, 25), hh=15, mm=29, close="99.00", oi=555)],
        )
        got = resolve_prev_oi(
            tmp_path, exchange="NSE", segment="FNO", symbol=_RPO_FUT, day=date(2025, 9, 29)
        )
        assert got == PrevOiBaseline(value=555, found_date=date(2025, 9, 25))
        assert prev_baseline_provenance(got.found_date, date(2025, 9, 29), exchange="NSE") == (
            "2025-09-25",
            False,
        )

    def test_wrapper_value_seam_is_byte_identical(self, tmp_path: Path) -> None:
        """_prev_day_oi(...) == resolve_prev_oi(...).value (the value-seam law)."""
        assert (
            _prev_day_oi(
                tmp_path, exchange="NSE", segment="FNO", symbol=_RPO_FUT, day=date(2025, 9, 1)
            )
            is None
        )
        _rpo_write(
            tmp_path,
            interval="1day",
            bars=[_day_bar(day=date(2025, 8, 29), close="101", oi=1234)],
        )
        assert (
            _prev_day_oi(
                tmp_path, exchange="NSE", segment="FNO", symbol=_RPO_FUT, day=date(2025, 9, 1)
            )
            == 1234
            == resolve_prev_oi(
                tmp_path, exchange="NSE", segment="FNO", symbol=_RPO_FUT, day=date(2025, 9, 1)
            ).value
        )

    def test_null_oi_1day_divergence_close_true_oi_false(self, tmp_path: Path) -> None:
        """The null-OI-1day edge (DEC-MOVERS-PREVCLOSE-CONTIGUITY-001): a prior
        1-day bar with a REAL close but NULL open_interest makes the CLOSE
        baseline contiguous (Fri 09-26, the true previous session) while the OI
        baseline falls to the minute fallback on Thu 09-25 — the two flags MUST
        diverge, and only the OI kind warns."""
        # Prior 1-day: Fri 2025-09-26, real close, NULL OI.
        _rpo_write(
            tmp_path,
            interval="1day",
            bars=[_day_bar(day=date(2025, 9, 26), close="100.00", oi=None)],
        )
        # 1-minute: OI only on Thu 2025-09-25 (nothing on Fri) + the snapshot
        # session bars on Mon 2025-09-29.
        _rpo_write(
            tmp_path,
            interval="1minute",
            bars=[
                _ist_min_bar(day=date(2025, 9, 25), hh=15, mm=29, close="99.00", oi=8765),
                _ist_min_bar(day=date(2025, 9, 29), hh=9, mm=15, close="101.00", oi=9000),
            ],
        )
        with capture_logs() as logs:
            cands = compute_futures_candidates(
                lake_root=tmp_path, interval="1day", date="2025-09-29", time=None
            )
        assert len(cands.movers) == 1
        mover = cands.movers[0]
        # Served values untouched: % change baselines on the 1-day close 100.00.
        assert mover.pct_change == Decimal("1")
        assert mover.oi == 9000
        # The DIVERGENT flags: close contiguous, OI provably not.
        assert mover.prev_close_session == "2025-09-26"
        assert mover.prev_close_contiguous is True
        assert mover.oi_baseline_session == "2025-09-25"
        assert mover.oi_baseline_contiguous is False
        # Exactly ONE warn — the OI kind; the contiguous close never warns.
        warns = [e for e in logs if e["event"] == "movers.prev_baseline_noncontiguous"]
        assert [(e["kind"], e["symbol"]) for e in warns] == [("oi", _RPO_FUT)]
        assert warns[0]["found_session"] == "2025-09-25"
        assert warns[0]["expected_session"] == "2025-09-26"
        # EMIT-ON-FALSE row projection: only the OI pair appears.
        row = _futures_row(mover)
        assert row["oi_baseline_session"] == "2025-09-25"
        assert row["oi_baseline_contiguous"] is False
        assert "prev_close_session" not in row
        assert "prev_close_contiguous" not in row
