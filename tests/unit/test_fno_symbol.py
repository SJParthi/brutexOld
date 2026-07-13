# pyright: reportPrivateUsage=none
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
    _STRIKE_TOKEN_RE,
    EXPIRY_DATE_FORMAT,
    EXPIRY_YEAR_MAX,
    EXPIRY_YEAR_MIN,
    FNO_INSTRUMENT_TYPES,
    FUTURE_TYPE,
    OPTION_TYPES,
    FnoSymbolParts,
    expiry_year_in_window,
    parse_expiry_token,
    parse_fno_symbol,
    parse_strike_token,
)
from hypothesis import assume, given
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


class TestExpiryYearWindow:
    """DEC-FNO-EXPIRY-YEAR-PIVOT-001: resolved years outside [2019, 2059] are junk.

    Regression: before the window, ``strptime``'s POSIX ``%y`` pivot silently
    accepted ``30Jun75`` as ``date(1975, 6, 30)`` — a fabricated 1975 "expiry".
    """

    def test_window_constants(self) -> None:
        assert EXPIRY_YEAR_MIN == 2019
        assert EXPIRY_YEAR_MAX == 2059

    @pytest.mark.parametrize(
        ("year", "expected"), [(2019, True), (2059, True), (2018, False), (2060, False)]
    )
    def test_predicate_boundaries(self, year: int, expected: bool) -> None:
        assert expiry_year_in_window(year) is expected

    def test_min_boundary_year_accepted(self) -> None:
        assert parse_expiry_token("31Dec19") == date(2019, 12, 31)

    def test_max_boundary_year_accepted(self) -> None:
        assert parse_expiry_token("30Jun59") == date(2059, 6, 30)

    @pytest.mark.parametrize(
        "token",
        [
            "30Jun75",  # POSIX pivot → 1975 — the fails-before regression token
            "31Dec99",  # POSIX pivot → 1999
            "01Jan18",  # 2018 < EXPIRY_YEAR_MIN
            "30Jun60",  # 2060 > EXPIRY_YEAR_MAX
            "30Jun68",  # POSIX pivot upper edge → 2068, still out of window
        ],
    )
    def test_out_of_window_year_is_none(self, token: str) -> None:
        assert parse_expiry_token(token) is None

    @pytest.mark.parametrize(
        "symbol",
        [
            "NSE-NIFTY-30Jun75-24000-CE",  # option leg with a pivot-junk expiry
            "NSE-NIFTY-30Jun75-FUT",  # future with a pivot-junk expiry
        ],
    )
    def test_parse_fno_symbol_rejects_out_of_window_expiry(self, symbol: str) -> None:
        assert parse_fno_symbol(symbol) is None

    @pytest.mark.property
    @given(two_digit_year=st.integers(min_value=0, max_value=99))
    def test_every_two_digit_year_accepted_iff_in_window(self, two_digit_year: int) -> None:
        # The POSIX %y pivot: 00–68 → 2000–2068, 69–99 → 1969–1999. A token is
        # accepted iff its pivot-RESOLVED year lands inside [2019, 2059].
        resolved = 2000 + two_digit_year if two_digit_year <= 68 else 1900 + two_digit_year
        result = parse_expiry_token(f"15Jan{two_digit_year:02d}")
        if EXPIRY_YEAR_MIN <= resolved <= EXPIRY_YEAR_MAX:
            assert result == date(resolved, 1, 15)
        else:
            assert result is None


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

    @pytest.mark.parametrize(
        "token",
        [
            "٢٤٠٠٠",  # Arabic-Indic digits — Decimal-coercible, NOT ASCII-canonical
            "２４０００",  # fullwidth digits — Decimal-coercible, NOT ASCII-canonical
            "24_000",  # underscore-grouped
            "+24000",  # signed
            " 24000",  # leading whitespace
            "24000\xa0",  # trailing NBSP — Decimal strips it, the regex does not
            "1e4",  # exponent form
            "24000.",  # trailing dot (no fraction digits)
            ".5",  # leading dot (no integer digits)
        ],
    )
    def test_non_canonical_token_is_none(self, token: str) -> None:
        # DEC-FNO-STRIKE-TOKEN-ASCII-001: a Decimal-coercible-but-non-canonical
        # token is rejected at the ASCII-grammar layer, so it can never decode
        # IDENTITY-EQUAL to the genuine contract.
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

    def test_unicode_strike_symbol_never_aliases_the_real_contract(self) -> None:
        # DEC-FNO-STRIKE-TOKEN-ASCII-001: a junk lake dir whose strike token is a
        # Unicode/fullwidth-digit run must NOT decode to the genuine contract's
        # identity — it is rejected outright (None), never aliased to `…-24000-CE`.
        genuine = parse_fno_symbol("NSE-NIFTY-30Jun25-24000-CE")
        assert genuine is not None
        assert genuine.strike == Decimal("24000")
        assert parse_fno_symbol("NSE-NIFTY-30Jun25-٢٤٠٠٠-CE") is None


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

    @pytest.mark.property
    @given(tok=st.text(max_size=12))
    def test_non_canonical_strike_token_rejects_whole_symbol(self, tok: str) -> None:
        # DEC-FNO-STRIKE-TOKEN-ASCII-001: any token that misses the ASCII-canonical
        # strike grammar yields None at the token layer AND rejects the whole option
        # symbol (never an identity-equal decode). The grammar splits on "-", so
        # assume the drawn token carries none.
        assume("-" not in tok)
        assume(_STRIKE_TOKEN_RE.fullmatch(tok) is None)
        assert parse_strike_token(tok) is None
        assert parse_fno_symbol(f"NSE-NIFTY-30Jun25-{tok}-CE") is None
