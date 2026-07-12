"""Canonical decoding of an F&O contract ``groww_symbol`` — ONE source of truth.

A lake F&O candle partition is keyed by the contract ``groww_symbol`` directory
name (``bars/<exch>/FNO/<groww_symbol>/<interval>/<yyyy>/<mm>.parquet`` — the
:mod:`brutex.data.partition` layout, ``segment="FNO"``). This module is the single
vendor-neutral parser that decodes that symbol into its identity coordinates
(exchange / underlying / expiry / strike / instrument_type / option_type), so every
reader — the ``/db`` F&O candle browser (:mod:`brutex.dashboard.db_view`), the
options-movers screen (:mod:`brutex.dashboard.movers_options`), and any future F&O
coverage view — shares ONE implementation (CLAUDE.md §11 COMMON; the jscpd
duplication gate enforces "one place").

Groww contract symbols are ``EXCH-UNDERLYING-DDMmmYY[-STRIKE]-TYPE`` (hyphen-joined),
e.g. ``NSE-NIFTY-30Sep25-24650-CE``, ``NSE-NIFTY-30Sep25-FUT``,
``BSE-SENSEX-25Sep25-79500-PE`` (``docs/groww/api-reference/03-backtesting.md:33-35``).
Parsing is **right-anchored** so a hyphenated underlying (the F&O-eligible
``BAJAJ-AUTO``, DEC-GROWW-HYPHEN-SYMBOL-RESOLVE-001) never confuses the type / strike
tokens: the LAST token is the instrument type (``FUT`` / ``CE`` / ``PE``) and, for an
option, the second-to-last is the strike and the third-to-last is the ``DDMmmYY``
expiry; the underlying is everything BETWEEN the exchange and the expiry (joined
through any interior hyphens).

This module is **pure** (stdlib only — no I/O, no clock, no Parquet / DuckDB
imports) so it is trivially unit-testable and importable from the data layer without
the P2 data-lake extras. It NEVER raises on a malformed / non-derivative symbol —
:func:`parse_fno_symbol` returns ``None`` so a caller iterating lake directories
simply skips a foreign / half-written name (Golden Rule #1: never fabricate, never
crash a browse on one junk directory).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Final

#: The trailing instrument-type token of a future contract symbol.
FUTURE_TYPE: Final[str] = "FUT"

#: The two option-leg instrument-type tokens (Call / Put).
OPTION_TYPES: Final[frozenset[str]] = frozenset({"CE", "PE"})

#: Every instrument type a lake F&O contract symbol can end with
#: (``constants.GROWW_INSTRUMENT_TYPES`` minus the cash ``EQ`` / ``IDX``).
FNO_INSTRUMENT_TYPES: Final[frozenset[str]] = OPTION_TYPES | {FUTURE_TYPE}

#: The vendor expiry-date format embedded in the symbol (``30Jun25``): a 2-digit
#: day, a title-case 3-letter English month, a 2-digit year. The SINGLE parse spec
#: shared with :mod:`brutex.dashboard.movers_options` (CLAUDE.md §11 COMMON).
EXPIRY_DATE_FORMAT: Final[str] = "%d%b%y"

#: Sanity window for the RESOLVED expiry calendar year (both endpoints inclusive) —
#: DEC-FNO-EXPIRY-YEAR-PIVOT-001. ``strptime``'s ``%y`` applies the POSIX 2-digit-year
#: pivot (``00–68`` → 2000–2068, ``69–99`` → 1969–1999), so a junk/corrupt lake
#: directory token like ``30Jun75`` silently decoded to a *plausible-looking* 1975
#: "expiry" and flowed into expiry tiers / days-to-expiry / moneyness. Any resolved
#: year outside ``[2019, 2059]`` is now rejected (→ ``None``): 2019 gives one year of
#: slack under the 2020 lake floor (``GROWW_HISTORY_FLOOR``, CLAUDE.md §3); 2059 caps
#: the future so every pivot-ambiguous 2-digit year (``60–68`` → 206x, ``69–99`` →
#: 19xx) is rejected by the same window. This is a sanity guard on OUR lake directory
#: names — the claim that the live vendor never lists such expiries is UNVERIFIED
#: from this environment (Golden Rule #1).
EXPIRY_YEAR_MIN: Final[int] = 2019
EXPIRY_YEAR_MAX: Final[int] = 2059

#: Minimum hyphen-token count of a derivative symbol: ``EXCH-SYMBOL-EXPIRY-FUT`` (4)
#: for a future; an option inserts a STRIKE token (``…-STRIKE-TYPE``) → 5.
_MIN_FUTURE_TOKENS: Final[int] = 4
_MIN_OPTION_TOKENS: Final[int] = 5


@dataclass(frozen=True, slots=True)
class FnoSymbolParts:
    """The decoded identity of one F&O contract ``groww_symbol``.

    Attributes
    ----------
    exchange
        The leading exchange token (``NSE`` / ``BSE``).
    underlying
        The underlying symbol (``NIFTY`` / ``RELIANCE`` / ``BAJAJ-AUTO``) — every
        token between the exchange and the expiry, joined through interior hyphens.
    expiry
        The contract expiry date (parsed from the ``DDMmmYY`` token).
    instrument_type
        ``"FUT"`` / ``"CE"`` / ``"PE"`` — the trailing token.
    strike
        The option strike (positive :class:`~decimal.Decimal` rupees), or ``None``
        for a future.
    option_type
        ``"CE"`` / ``"PE"`` for an option, ``None`` for a future. Equal to
        ``instrument_type`` for options; kept as a distinct field so a caller reading
        the "option side" never has to special-case ``FUT``.
    """

    exchange: str
    underlying: str
    expiry: date
    instrument_type: str
    strike: Decimal | None
    option_type: str | None

    @property
    def is_option(self) -> bool:
        """``True`` for a ``CE`` / ``PE`` leg, ``False`` for a future."""
        return self.option_type is not None


def expiry_year_in_window(year: int) -> bool:
    """Return ``True`` iff *year* is a sane F&O expiry calendar year.

    The ONE window predicate (CLAUDE.md §11 COMMON) applied by
    :func:`parse_expiry_token` — since DEC-MOVERS-FUTURES-HYPHEN-PARSE-001
    (MIGRATION.md #1646) the futures movers' formerly shape-stricter local twin
    is DELETED and :mod:`brutex.dashboard.movers_futures` delegates to this
    module's canonical grammar, so there is exactly ONE parser and ONE junk-year
    window (DEC-FNO-EXPIRY-YEAR-PIVOT-001).

    Parameters
    ----------
    year
        A resolved 4-digit calendar year (after any 2-digit-year pivot).

    Returns
    -------
    bool
        ``True`` when ``EXPIRY_YEAR_MIN <= year <= EXPIRY_YEAR_MAX`` (both
        inclusive), else ``False``.
    """
    return EXPIRY_YEAR_MIN <= year <= EXPIRY_YEAR_MAX


def parse_expiry_token(token: str) -> date | None:
    """Parse a ``DDMmmYY`` expiry token (``30Jun25``) into a date, or ``None``.

    Any other shape, an unknown month, or an out-of-range day raises
    :class:`ValueError` inside :meth:`~datetime.datetime.strptime`, caught and
    reported as ``None`` so the caller skips the malformed contract (never raising —
    Golden Rule #1). A token whose POSIX-``%y``-pivot-resolved year falls outside
    the ``[EXPIRY_YEAR_MIN, EXPIRY_YEAR_MAX]`` sanity window is likewise rejected
    as ``None`` — ``30Jun75`` is junk, NOT a 1975 expiry
    (DEC-FNO-EXPIRY-YEAR-PIVOT-001; the rejection rides the callers' existing
    ``None`` handling, e.g. the ``greeks_enrich.target_undecodable`` counter+log
    and the ``/db`` cascade ``unparsed`` bucket — this module stays pure/log-free).
    """
    try:
        parsed = datetime.strptime(token, EXPIRY_DATE_FORMAT).date()
    except ValueError:
        return None
    if not expiry_year_in_window(parsed.year):
        return None
    return parsed


def parse_strike_token(token: str) -> Decimal | None:
    """Parse a strike token (``24000`` / ``24000.5``) into a positive Decimal, or ``None``.

    A non-numeric token (:class:`~decimal.InvalidOperation`), a non-finite value
    (``inf`` / ``nan`` round-trip through :class:`~decimal.Decimal` without raising),
    or a non-positive strike all yield ``None`` — a traded option strike is always a
    finite positive rupee value, so anything else is a foreign / corrupt token the
    caller skips.
    """
    try:
        value = Decimal(token)
    except InvalidOperation:
        return None
    if not value.is_finite() or value <= 0:
        return None
    return value


def parse_fno_symbol(symbol: str) -> FnoSymbolParts | None:
    """Decode a lake F&O contract ``groww_symbol``, or ``None`` for a non-F&O / junk symbol.

    Right-anchored (see the module docstring): the LAST hyphen token is the instrument
    type; for an option the second-to-last is the strike and the third-to-last is the
    ``DDMmmYY`` expiry; for a future the second-to-last is the expiry. The underlying is
    every token between the exchange (token ``0``) and the expiry, joined through any
    interior hyphens so a hyphenated underlying (``BAJAJ-AUTO``) is preserved intact.

    Parameters
    ----------
    symbol
        A candidate contract ``groww_symbol`` (typically a lake ``FNO`` directory name).

    Returns
    -------
    FnoSymbolParts | None
        The decoded coordinates when *symbol* is a well-formed FUT / CE / PE contract,
        else ``None`` (a spot symbol, a future missing its expiry, an option missing
        its strike, an unparseable expiry / strike, or any junk) — never raising, so a
        directory walk skips the entry.
    """
    parts = symbol.split("-")
    instrument_type = parts[-1]
    if instrument_type == FUTURE_TYPE:
        if len(parts) < _MIN_FUTURE_TOKENS:
            return None
        expiry = parse_expiry_token(parts[-2])
        strike: Decimal | None = None
        option_type: str | None = None
        underlying_tokens = parts[1:-2]
    elif instrument_type in OPTION_TYPES:
        if len(parts) < _MIN_OPTION_TOKENS:
            return None
        expiry = parse_expiry_token(parts[-3])
        strike = parse_strike_token(parts[-2])
        option_type = instrument_type
        underlying_tokens = parts[1:-3]
        if strike is None:
            return None
    else:
        return None
    exchange = parts[0]
    underlying = "-".join(underlying_tokens)
    if not exchange or not underlying or expiry is None:
        return None
    return FnoSymbolParts(
        exchange=exchange,
        underlying=underlying,
        expiry=expiry,
        instrument_type=instrument_type,
        strike=strike,
        option_type=option_type,
    )


__all__ = [
    "EXPIRY_DATE_FORMAT",
    "EXPIRY_YEAR_MAX",
    "EXPIRY_YEAR_MIN",
    "FNO_INSTRUMENT_TYPES",
    "FUTURE_TYPE",
    "OPTION_TYPES",
    "FnoSymbolParts",
    "expiry_year_in_window",
    "parse_expiry_token",
    "parse_fno_symbol",
    "parse_strike_token",
]
