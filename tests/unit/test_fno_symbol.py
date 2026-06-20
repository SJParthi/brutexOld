"""Unit + property + golden tests for :mod:`brutex.data.fno_symbol`.

The canonical F&O contract ``groww_symbol`` parser (DEC-FNO-CANDLE-LAKE-001) — the
single source of truth shared by the ``/db`` F&O candle browser and the options-movers
screen. Covers every branch (FUT / CE / PE / junk / hyphenated underlying / bad expiry /
bad strike / empty exchange / empty underlying), a golden decode table, and a Hypothesis
property that the parser NEVER raises on arbitrary input (Golden Rule #1).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from brutex.data.fno_symbol import (
    EXPIRY_DATE_FORMAT,
    FNO_INSTRUMENT_TYPES,
    FUTURE_TYPE,
    OPTION_TYPES,
    FnoSymbolParts,
    parse_expiry_token,
    parse_fno_symbol,
    parse_strike_token,
)
from hypothesis import given
from hypothesis import strategies as st


class TestConstants:
    """The module-level vocabulary is the documented F&O instrument set."""

    def test_instrument_types_are_fut_ce_pe(self) -> None:
        assert frozenset({"FUT", "CE", "PE"}) == FNO_INSTRUMENT_TYPES
        assert FUTURE_TYPE == "FUT"
        assert frozenset({"CE", "PE"}) == OPTION_TYPES

    def test_expiry_format_is_the_vendor_ddmmmyy(self) -> None:
        assert EXPIRY_DATE_FORMAT == "%d%b%y"


class TestParseExpiryToken:
    """``DDMmmYY`` → date, or ``None`` (never raising)."""

    def test_valid_token(self) -> None:
        assert parse_expiry_token("30Jun25") == date(2025, 6, 30)

    @pytest.mark.parametrize("token", ["", "99Zzz99", "2025-06-30", "30Jun", "garbage"])
    def test_invalid_token_is_none(self, token: str) -> None:
        assert parse_expiry_token(token) is None


class TestParseStrikeToken:
    """Strike token → positive Decimal, or ``None``."""

    def test_integer_strike(self) -> None:
        assert parse_strike_token("24000") == Decimal("24000")

    def test_fractional_strike(self) -> None:
        assert parse_strike_token("24000.5") == Decimal("24000.5")

    def test_non_numeric_is_none(self) -> None:
        assert parse_strike_token("abc") is None

    @pytest.mark.parametrize("token", ["inf", "-inf", "nan", "Infinity"])
    def test_non_finite_is_none(self, token: str) -> None:
        assert parse_strike_token(token) is None

    @pytest.mark.parametrize("token", ["0", "0.0", "-5", "-24000.5"])
    def test_non_positive_is_none(self, token: str) -> None:
        assert parse_strike_token(token) is None


#: Golden decode table — canonical contract symbols → their decoded coordinates.
_GOLDEN: tuple[tuple[str, tuple[str, str, date, str, Decimal | None, str | None]], ...] = (
    (
        "NSE-NIFTY-30Jun25-FUT",
        ("NSE", "NIFTY", date(2025, 6, 30), "FUT", None, None),
    ),
    (
        "NSE-NIFTY-30Sep25-24650-CE",
        ("NSE", "NIFTY", date(2025, 9, 30), "CE", Decimal("24650"), "CE"),
    ),
    (
        "BSE-SENSEX-25Sep25-79500-PE",
        ("BSE", "SENSEX", date(2025, 9, 25), "PE", Decimal("79500"), "PE"),
    ),
    (
        "NSE-BAJAJ-AUTO-30Jun25-9000-CE",
        ("NSE", "BAJAJ-AUTO", date(2025, 6, 30), "CE", Decimal("9000"), "CE"),
    ),
    (
        "NSE-BAJAJ-AUTO-30Jun25-FUT",
        ("NSE", "BAJAJ-AUTO", date(2025, 6, 30), "FUT", None, None),
    ),
    (
        "NSE-NIFTY-30Jun25-24000.5-CE",
        ("NSE", "NIFTY", date(2025, 6, 30), "CE", Decimal("24000.5"), "CE"),
    ),
)


class TestParseFnoSymbolGolden:
    """Every canonical symbol decodes to its exact coordinates (byte-stable)."""

    @pytest.mark.golden
    @pytest.mark.parametrize(("symbol", "expected"), _GOLDEN)
    def test_golden_decode(
        self,
        symbol: str,
        expected: tuple[str, str, date, str, Decimal | None, str | None],
    ) -> None:
        parts = parse_fno_symbol(symbol)
        assert parts is not None
        assert (
            parts.exchange,
            parts.underlying,
            parts.expiry,
            parts.instrument_type,
            parts.strike,
            parts.option_type,
        ) == expected


class TestParseFnoSymbolRejections:
    """Every malformed / non-derivative symbol returns ``None`` (never raises)."""

    @pytest.mark.parametrize(
        "symbol",
        [
            "",  # empty → last token "" not a type
            "NSE-NIFTY",  # spot, no type token
            "NSE-NIFTY-30Jun25-24000-XX",  # unknown trailing type
            "NIFTY-FUT",  # future too few tokens (< 4)
            "A-B-FUT",  # future too few tokens (3 < 4)
            "NSE-NIFTY-30Jun25-CE",  # option missing strike (4 < 5)
            "NSE-NIFTY-30Jun25-abc-CE",  # option strike unparseable
            "NSE-NIFTY-30Jun25-0-CE",  # option strike non-positive
            "NSE-NIFTY-99Zzz99-24000-CE",  # option expiry unparseable
            "NSE-NIFTY-99Zzz99-FUT",  # future expiry unparseable
            "NSE--30Jun25-24000-CE",  # empty underlying
            "-X-30Jun25-FUT",  # empty exchange
        ],
    )
    def test_rejected_symbols_return_none(self, symbol: str) -> None:
        assert parse_fno_symbol(symbol) is None


class TestFnoSymbolParts:
    """The decoded record + its ``is_option`` convenience flag."""

    def test_is_option_true_for_option(self) -> None:
        parts = parse_fno_symbol("NSE-NIFTY-30Jun25-24000-CE")
        assert parts is not None
        assert parts.is_option is True

    def test_is_option_false_for_future(self) -> None:
        parts = parse_fno_symbol("NSE-NIFTY-30Jun25-FUT")
        assert parts is not None
        assert parts.is_option is False

    def test_record_is_frozen(self) -> None:
        parts = FnoSymbolParts(
            exchange="NSE",
            underlying="NIFTY",
            expiry=date(2025, 6, 30),
            instrument_type="FUT",
            strike=None,
            option_type=None,
        )
        with pytest.raises(AttributeError):
            parts.underlying = "BANKNIFTY"  # type: ignore[misc]


class TestParseFnoSymbolProperty:
    """Hypothesis: the parser NEVER raises, and a constructed symbol round-trips."""

    @pytest.mark.property
    @given(st.text())
    def test_never_raises_on_arbitrary_text(self, blob: str) -> None:
        # Golden Rule #1: a foreign / half-written lake directory name is skipped,
        # never crashes a browse. The result is either None or a valid record.
        result = parse_fno_symbol(blob)
        assert result is None or isinstance(result, FnoSymbolParts)

    @pytest.mark.property
    @given(
        underlying=st.sampled_from(["NIFTY", "BANKNIFTY", "SENSEX", "RELIANCE"]),
        day=st.integers(min_value=1, max_value=28),
        strike=st.integers(min_value=1, max_value=100000),
        opt=st.sampled_from(["CE", "PE"]),
    )
    def test_constructed_option_round_trips(
        self, underlying: str, day: int, strike: int, opt: str
    ) -> None:
        expiry = date(2025, 6, day)
        token = expiry.strftime(EXPIRY_DATE_FORMAT)
        symbol = f"NSE-{underlying}-{token}-{strike}-{opt}"
        parts = parse_fno_symbol(symbol)
        assert parts is not None
        assert parts.underlying == underlying
        assert parts.expiry == expiry
        assert parts.strike == Decimal(strike)
        assert parts.option_type == opt
        assert parts.instrument_type == opt
