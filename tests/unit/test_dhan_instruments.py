"""Unit tests for :mod:`brutex.adapters.dhan.instruments` — 100% line+branch."""

from __future__ import annotations

from decimal import Decimal

import pytest
from brutex.adapters.dhan import instruments as m
from brutex.adapters.dhan.constants import DHAN_SCRIP_HEADERS_UNVERIFIED as H
from brutex.models import InstrumentFormatError

_REQUIRED = ("security_id", "trading_symbol", "instrument_type", "exch_id", "segment")


def _csv(rows: list[dict[str, str]], *, extra_headers: tuple[str, ...] = ()) -> str:
    headers = [H[k] for k in _REQUIRED] + list(extra_headers)
    lines = [",".join(headers)]
    for row in rows:
        lines.append(",".join(row.get(h, "") for h in headers))
    return "\n".join(lines) + "\n"


# ── exchange_segment_for + host allowlist ──────────────────────────────────


def test_exchange_segment_for_known() -> None:
    assert m.exchange_segment_for("NSE", "E") == "NSE_EQ"
    assert m.exchange_segment_for(" nse ", " i ") == "IDX_I"  # trimmed + upper


def test_exchange_segment_for_unknown_raises() -> None:
    with pytest.raises(InstrumentFormatError, match="unknown Dhan"):
        m.exchange_segment_for("ZZZ", "Q")


def test_is_allowed_scrip_host() -> None:
    assert m.is_allowed_scrip_host("https://images.dhan.co/api-data/api-scrip-master.csv")
    assert m.is_allowed_scrip_host("https://api.dhan.co/v2/charts/historical")
    assert not m.is_allowed_scrip_host("https://evil.example.com/x.csv")


# ── parse_scrip_master ─────────────────────────────────────────────────────


def test_parse_scrip_master_happy() -> None:
    csv = _csv(
        [
            {
                H["security_id"]: "13",
                H["trading_symbol"]: "NIFTY",
                H["instrument_type"]: "INDEX",
                H["exch_id"]: "NSE",
                H["segment"]: "I",
            },
            {
                H["security_id"]: "2885",
                H["trading_symbol"]: "RELIANCE",
                H["instrument_type"]: "EQUITY",
                H["exch_id"]: "NSE",
                H["segment"]: "E",
            },
        ],
        extra_headers=(H["lot_size"], H["tick_size"], H["strike_price"]),
    )
    # append optional columns to the RELIANCE row via a full re-parse using extras:
    rows = m.parse_scrip_master(csv)
    assert len(rows) == 2
    assert rows[0].security_id == 13
    assert rows[0].exchange_segment == "IDX_I"
    assert rows[1].trading_symbol == "RELIANCE"
    assert rows[1].exchange_segment == "NSE_EQ"


def test_parse_scrip_master_optional_fields_present() -> None:
    headers = [H[k] for k in _REQUIRED] + [
        H["lot_size"],
        H["tick_size"],
        H["strike_price"],
        H["series"],
        H["expiry_date"],
        H["option_type"],
    ]
    line = ",".join(
        [
            "50",
            "NIFTY26AUG24000CE",
            "OPTIDX",
            "NSE",
            "D",
            "75",
            "0.05",
            "24000",
            "OPT",
            "2026-08-27",
            "CE",
        ]
    )
    csv = ",".join(headers) + "\n" + line + "\n"
    rows = m.parse_scrip_master(csv)
    assert rows[0].lot_size == 75
    assert rows[0].tick_size == Decimal("0.05")
    assert rows[0].strike_price == Decimal("24000")
    assert rows[0].series == "OPT"
    assert rows[0].expiry_date == "2026-08-27"
    assert rows[0].option_type == "CE"


def test_parse_scrip_master_optional_fields_absent_default_none() -> None:
    csv = _csv(
        [
            {
                H["security_id"]: "1",
                H["trading_symbol"]: "X",
                H["instrument_type"]: "EQUITY",
                H["exch_id"]: "NSE",
                H["segment"]: "E",
            }
        ]
    )
    rows = m.parse_scrip_master(csv)
    assert rows[0].lot_size is None
    assert rows[0].tick_size is None
    assert rows[0].strike_price is None
    assert rows[0].series is None


def test_parse_scrip_master_blank_security_id_skipped() -> None:
    csv = _csv(
        [
            {
                H["security_id"]: "",
                H["trading_symbol"]: "GHOST",
                H["instrument_type"]: "EQUITY",
                H["exch_id"]: "NSE",
                H["segment"]: "E",
            }
        ]
    )
    assert m.parse_scrip_master(csv) == []


def test_parse_scrip_master_no_header_raises() -> None:
    with pytest.raises(InstrumentFormatError, match="no header row"):
        m.parse_scrip_master("")


def test_parse_scrip_master_missing_required_column() -> None:
    csv = "col_a,col_b\n1,2\n"
    with pytest.raises(InstrumentFormatError, match="missing required column"):
        m.parse_scrip_master(csv)


def test_parse_scrip_master_unparseable_security_id() -> None:
    csv = _csv(
        [
            {
                H["security_id"]: "abc",
                H["trading_symbol"]: "X",
                H["instrument_type"]: "EQUITY",
                H["exch_id"]: "NSE",
                H["segment"]: "E",
            }
        ]
    )
    with pytest.raises(InstrumentFormatError, match="unparseable security_id"):
        m.parse_scrip_master(csv)


def test_parse_scrip_master_unparseable_decimal() -> None:
    headers = [H[k] for k in _REQUIRED] + [H["tick_size"]]
    csv = ",".join(headers) + "\n" + ",".join(["1", "X", "EQUITY", "NSE", "E", "notanum"]) + "\n"
    with pytest.raises(InstrumentFormatError, match="unparseable tick_size"):
        m.parse_scrip_master(csv)


def test_parse_scrip_master_unparseable_int() -> None:
    headers = [H[k] for k in _REQUIRED] + [H["lot_size"]]
    csv = ",".join(headers) + "\n" + ",".join(["1", "X", "EQUITY", "NSE", "E", "lots"]) + "\n"
    with pytest.raises(InstrumentFormatError, match="unparseable lot_size"):
        m.parse_scrip_master(csv)


def test_parse_scrip_master_float_shaped_lot() -> None:
    headers = [H[k] for k in _REQUIRED] + [H["lot_size"]]
    csv = ",".join(headers) + "\n" + ",".join(["1", "X", "EQUITY", "NSE", "E", "75.0"]) + "\n"
    rows = m.parse_scrip_master(csv)
    assert rows[0].lot_size == 75


# ── DhanInstrumentIndex ────────────────────────────────────────────────────


def _row(sec: int, sym: str, seg: str = "NSE_EQ") -> m.DhanScripRow:
    return m.DhanScripRow(
        security_id=sec,
        trading_symbol=sym,
        exchange_segment=seg,
        instrument_type="EQUITY",
        series=None,
        expiry_date=None,
        option_type=None,
        strike_price=None,
        lot_size=None,
        tick_size=None,
    )


def test_index_build_and_resolve() -> None:
    index = m.DhanInstrumentIndex.build([_row(1, "A"), _row(2, "B", "IDX_I")])
    assert index.resolve_security_id("NSE_EQ", "A") == 1
    assert index.resolve_security_id("IDX_I", "B") == 2
    assert index.resolve_security_id("NSE_EQ", "ZZZ") is None
    assert index.by_security_id[1].trading_symbol == "A"


def test_index_blank_symbol_not_keyed() -> None:
    index = m.DhanInstrumentIndex.build([_row(1, "")])
    assert index.by_exchange_symbol == {}
    assert 1 in index.by_security_id  # still keyed by id
