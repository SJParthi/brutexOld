"""Unit tests for ``brutex.adapters.groww.instruments`` (DEC-GROWW-ADAPTER-LEAF-001 / PR-D)."""

from __future__ import annotations

import hashlib
from datetime import date
from decimal import Decimal

import pytest
from brutex.adapters.groww import instruments as gi
from brutex.adapters.groww.constants import INSTRUMENT_CSV_COLUMNS
from brutex.models import Exchange, InstrumentFormatError, InstrumentType, Segment

_HEADER = ",".join(INSTRUMENT_CSV_COLUMNS)


def _row(**over: str) -> str:
    """Build one 21-field CSV row; every column defaults blank, override by name."""
    cells = dict.fromkeys(INSTRUMENT_CSV_COLUMNS, "")
    cells.update(over)
    return ",".join(cells[col] for col in INSTRUMENT_CSV_COLUMNS)


# Representative in-scope + out-of-scope rows.
_NIFTY = _row(
    exchange="NSE",
    exchange_token="NIFTY",
    trading_symbol="NIFTY",
    groww_symbol="NSE-NIFTY",
    name="NIFTY 50",
    instrument_type="IDX",
    segment="CASH",
)
_VIX = _row(
    exchange="NSE",
    exchange_token="INDIAVIX",
    trading_symbol="INDIAVIX",
    groww_symbol="NSE-INDIAVIX",
    name="India Vix",
    instrument_type="IDX",
    segment="CASH",
)
_WIPRO = _row(
    exchange="NSE",
    exchange_token="3787",
    trading_symbol="WIPRO",
    groww_symbol="NSE-WIPRO",
    name="Wipro",
    instrument_type="EQ",
    segment="CASH",
    series="EQ",
    isin="INE075A01022",
    lot_size="1",
    tick_size="0.05",
)
_FUT = _row(
    exchange="NSE",
    exchange_token="53001",
    trading_symbol="NIFTY25Aug26FUT",
    groww_symbol="NSE-NIFTY-25Aug26-FUT",
    instrument_type="FUT",
    segment="FNO",
    underlying_symbol="NIFTY",
    underlying_exchange_token="26000",
    expiry_date="2026-08-25",
    lot_size="65",
    tick_size="0.05",
    freeze_quantity="1800",
)
_CE = _row(
    exchange="NSE",
    exchange_token="50467",
    trading_symbol="NIFTY2661621800CE",
    groww_symbol="NSE-NIFTY-16Jun26-21800-CE",
    instrument_type="CE",
    segment="FNO",
    underlying_symbol="NIFTY",
    underlying_exchange_token="26000",
    expiry_date="2026-06-16",
    strike_price="21800",
    lot_size="65",
    tick_size="0.05",
)
_COMMODITY = _row(
    exchange="MCX",
    exchange_token="1",
    trading_symbol="GOLD",
    groww_symbol="MCX-GOLD",
    instrument_type="FUT",
    segment="COMMODITY",
    underlying_symbol="GOLD",
    expiry_date="2026-08-25",
    lot_size="100",
    tick_size="1",
)
_UNKNOWN = _row(
    exchange="NSE",
    exchange_token="999",
    trading_symbol="ETFX",
    groww_symbol="NSE-ETFX",
    instrument_type="ETF",
    segment="CASH",
    lot_size="1",
    tick_size="0.05",
)
_NIFTY_DUP = _row(
    exchange="NSE",
    exchange_token="NIFTY",
    trading_symbol="NIFTY",
    groww_symbol="NSE-NIFTY",
    name="DUP",
    instrument_type="IDX",
    segment="CASH",
)


def _csv(*rows: str) -> str:
    return "\n".join([_HEADER, *rows]) + "\n"


# ── parse_instrument_csv + build_index (integration) ────────────────────────
def test_full_csv_parses_in_scope_only_and_dedups() -> None:
    text = _csv(_NIFTY, _VIX, _WIPRO, _FUT, _CE, _COMMODITY, _UNKNOWN, _NIFTY_DUP)
    pairs = gi.parse_instrument_csv(text)
    # COMMODITY (MCX) + ETF excluded; the two NIFTY rows both parse (dedup is the index's job)
    assert len(pairs) == 6
    index = gi.build_index(pairs)
    assert len(index.universe) == 5  # NIFTY (dedup) + VIX + WIPRO + FUT + CE


def test_index_resolves_o1_by_both_keys() -> None:
    index = gi.load_index_from_csv(_csv(_NIFTY, _WIPRO))
    assert gi.resolve(index, "NSE-NIFTY").symbol_name == "NIFTY"
    assert gi.resolve_by_pair(index, Exchange.NSE, "WIPRO").display_name == "Wipro"


def test_index_resolve_miss_raises_keyerror() -> None:
    index = gi.load_index_from_csv(_csv(_NIFTY))
    with pytest.raises(KeyError):
        gi.resolve(index, "NSE-MISSING")
    with pytest.raises(KeyError):
        gi.resolve_by_pair(index, Exchange.BSE, "NIFTY")


def test_index_dedup_first_wins() -> None:
    index = gi.load_index_from_csv(_csv(_NIFTY, _NIFTY_DUP))
    assert len(index.universe) == 1
    assert gi.resolve(index, "NSE-NIFTY").display_name == "NIFTY 50"  # first wins, not "DUP"


# ── field mapping per instrument kind ───────────────────────────────────────
def test_index_row_maps_to_index_segment_zero_lot_surrogate_id() -> None:
    inst = gi.load_index_from_csv(_csv(_NIFTY)).by_groww_symbol["NSE-NIFTY"]
    assert inst.instrument is InstrumentType.IDX
    assert inst.segment is Segment.INDEX  # IDX → INDEX, not CASH
    assert inst.lot_size == 0 and inst.tick_size == Decimal(0)
    assert inst.is_index is True
    # string token → deterministic surrogate from the groww_symbol
    expected = int.from_bytes(hashlib.blake2b(b"NSE-NIFTY", digest_size=4).digest(), "big")
    assert inst.security_id == expected


def test_vix_present_and_parses() -> None:
    inst = gi.load_index_from_csv(_csv(_VIX)).by_groww_symbol["NSE-INDIAVIX"]
    assert inst.instrument is InstrumentType.IDX
    assert inst.display_name == "India Vix"


def test_equity_row_numeric_security_id() -> None:
    inst = gi.load_index_from_csv(_csv(_WIPRO)).by_groww_symbol["NSE-WIPRO"]
    assert inst.security_id == 3787  # numeric token used directly
    assert inst.segment is Segment.CASH
    assert inst.lot_size == 1 and inst.tick_size == Decimal("0.05")
    assert inst.isin == "INE075A01022"


def test_future_row() -> None:
    inst = gi.load_index_from_csv(_csv(_FUT)).by_groww_symbol["NSE-NIFTY-25Aug26-FUT"]
    assert inst.is_derivative is True
    assert inst.underlying_symbol == "NIFTY"
    assert inst.sm_expiry_date == date(2026, 8, 25)
    assert inst.segment is Segment.FNO


def test_option_row() -> None:
    inst = gi.load_index_from_csv(_csv(_CE)).by_groww_symbol["NSE-NIFTY-16Jun26-21800-CE"]
    assert inst.is_option is True
    assert inst.strike_price == Decimal("21800")
    assert inst.option_type == "CE"


# ── failure modes ───────────────────────────────────────────────────────────
def test_header_drift_raises() -> None:
    bad = "exchange,wrong_col\nNSE,x\n"
    with pytest.raises(InstrumentFormatError, match="header drift"):
        gi.parse_instrument_csv(bad)


def test_missing_groww_symbol_raises() -> None:
    with pytest.raises(InstrumentFormatError, match="missing groww_symbol"):
        gi.parse_instrument_row(dict.fromkeys(INSTRUMENT_CSV_COLUMNS, ""))


def test_malformed_lot_raises() -> None:
    bad = _row(
        exchange="NSE",
        exchange_token="1",
        trading_symbol="X",
        groww_symbol="NSE-X",
        instrument_type="EQ",
        segment="CASH",
        lot_size="abc",
        tick_size="0.05",
    )
    with pytest.raises(InstrumentFormatError, match="unparseable lot_size"):
        gi.parse_instrument_csv(_csv(bad))


# ── is_in_scope_row branches ────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("row", "in_scope"),
    [
        ({"exchange": "NSE", "segment": "CASH", "instrument_type": "IDX"}, True),
        ({"exchange": "MCX", "segment": "COMMODITY", "instrument_type": "FUT"}, False),
        ({"exchange": "NSE", "segment": "COMMODITY", "instrument_type": "FUT"}, False),
        ({"exchange": "NSE", "segment": "CASH", "instrument_type": "ETF"}, False),
    ],
)
def test_is_in_scope_row(row: dict[str, str], in_scope: bool) -> None:
    assert gi.is_in_scope_row(row) is in_scope


# ── helper branch coverage (paths unreachable via well-formed CSV) ──────────
def test_clean_handles_none_and_whitespace() -> None:
    assert gi._clean(None) is None
    assert gi._clean("   ") is None
    assert gi._clean("  x ") == "x"


def test_security_id_blank_token_uses_surrogate() -> None:
    expected = int.from_bytes(hashlib.blake2b(b"NSE-FOO", digest_size=4).digest(), "big")
    assert gi._security_id(None, gsym="NSE-FOO") == expected


def test_opt_int_and_int_or_zero_branches() -> None:
    assert gi._opt_int("", field="f", gsym="g") is None
    assert gi._opt_int("7", field="f", gsym="g") == 7
    assert gi._int_or_zero("", field="f", gsym="g") == 0
    with pytest.raises(InstrumentFormatError):
        gi._opt_int("x", field="f", gsym="g")


def test_opt_decimal_branches() -> None:
    assert gi._opt_decimal("", field="f", gsym="g") is None
    assert gi._opt_decimal("1.5", field="f", gsym="g") == Decimal("1.5")
    with pytest.raises(InstrumentFormatError, match="unparseable"):
        gi._opt_decimal("x", field="f", gsym="g")
    with pytest.raises(InstrumentFormatError, match="non-finite"):
        gi._opt_decimal("inf", field="f", gsym="g")


def test_opt_date_branches() -> None:
    assert gi._opt_date("", field="f", gsym="g") is None
    assert gi._opt_date("2026-08-25", field="f", gsym="g") == date(2026, 8, 25)
    with pytest.raises(InstrumentFormatError):
        gi._opt_date("nope", field="f", gsym="g")


def test_segment_for_unmappable_raises() -> None:
    with pytest.raises(InstrumentFormatError, match="unmappable segment"):
        gi._segment_for(InstrumentType.FUT, "WEIRD", gsym="g")
