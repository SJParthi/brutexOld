"""Golden regression for the Dhan universe selection (DEC-DHAN-INDEX-ALLOWLIST-001
+ DEC-DHAN-UNIVERSE-TICKVAULT-PARITY-001 + DEC-DHAN-UNIVERSE-DEDUP-001 +
DEC-DHAN-VENDOR-SYMBOL-UNIQUE-001).

A curated slice of the **real** Dhan ``api-scrip-master-detailed`` (the same
31-column header) exercising every admit/drop rule in ONE pass, so the exact
admitted universe is locked byte-for-byte against accidental regression:

* allow-listed indices (NSE NIFTY/BANKNIFTY + BSE SENSEX) admitted;
* obscure NSE index + non-SENSEX BSE index dropped;
* NSE F&O single-stock spots admitted by ticker; their option legs dropped;
* the MOTHERSON duplicate listing collapses to the ONE canonical spot
  (the ``UNDERLYING_SECURITY_ID`` the derivative points to);
* a BSE single-stock equity dropped (NSE-only stock scope);
* a ``*NSETEST`` placeholder scrip dropped;
* an MCX row filtered (out-of-scope exchange);
* every admitted ``vendor_symbol`` is exchange-prefixed + globally unique.

Verified against the full 220,287-row master on 2026-06-01: 31 indices + 211
stocks, zero collisions, MOTHERSON → SECURITY_ID 4204.
"""

from __future__ import annotations

import csv
import io

from brutex.adapters.dhan.instruments import select_universe_report, validate_header

# The real detailed-CSV header (31 columns + the trailing empty column), verbatim.
_HEADER = (
    "EXCH_ID,SEGMENT,SECURITY_ID,ISIN,INSTRUMENT,UNDERLYING_SECURITY_ID,UNDERLYING_SYMBOL,"
    "SYMBOL_NAME,DISPLAY_NAME,INSTRUMENT_TYPE,SERIES,LOT_SIZE,SM_EXPIRY_DATE,STRIKE_PRICE,"
    "OPTION_TYPE,TICK_SIZE,EXPIRY_FLAG,BRACKET_FLAG,COVER_FLAG,ASM_GSM_FLAG,ASM_GSM_CATEGORY,"
    "BUY_SELL_INDICATOR,BUY_CO_MIN_MARGIN_PER,BUY_CO_SL_RANGE_MAX_PERC,BUY_CO_SL_RANGE_MIN_PERC,"
    "BUY_BO_MIN_MARGIN_PER,BUY_BO_PROFIT_RANGE_MAX_PERC,BUY_BO_PROFIT_RANGE_MIN_PERC,"
    "MTF_LEVERAGE,SM_UPPER_LIMIT,SM_LOWER_LIMIT,SM_FREEZE_QTY,"
).split(",")


def _row(**over: str) -> dict[str, str]:
    base = dict.fromkeys(_HEADER, "")
    base.update(over)
    return base


def _curated_rows() -> list[dict[str, str]]:
    return [
        # --- indices: allow-listed (kept) ---
        _row(EXCH_ID="NSE", INSTRUMENT="INDEX", SECURITY_ID="13", SYMBOL_NAME="NIFTY"),
        _row(EXCH_ID="NSE", INSTRUMENT="INDEX", SECURITY_ID="25", SYMBOL_NAME="BANKNIFTY"),
        _row(EXCH_ID="BSE", INSTRUMENT="INDEX", SECURITY_ID="51", SYMBOL_NAME="SENSEX"),
        # --- indices: NOT allow-listed (dropped) ---
        _row(EXCH_ID="NSE", INSTRUMENT="INDEX", SECURITY_ID="900", SYMBOL_NAME="NIFTY ALPHA 50"),
        _row(EXCH_ID="BSE", INSTRUMENT="INDEX", SECURITY_ID="52", SYMBOL_NAME="BANKEX"),
        # --- NSE F&O stocks (kept) + their option legs (dropped) ---
        _row(
            EXCH_ID="NSE",
            INSTRUMENT="EQUITY",
            SECURITY_ID="2885",
            LOT_SIZE="1",
            TICK_SIZE="0.05",
            SYMBOL_NAME="RELIANCE INDUSTRIES LTD",
            UNDERLYING_SYMBOL="RELIANCE",
        ),
        _row(
            EXCH_ID="NSE",
            INSTRUMENT="OPTSTK",
            SECURITY_ID="40001",
            SYMBOL_NAME="RELIANCE26JAN3000CE",
            UNDERLYING_SYMBOL="RELIANCE",
            UNDERLYING_SECURITY_ID="2885",
            OPTION_TYPE="CE",
            STRIKE_PRICE="3000",
            SM_EXPIRY_DATE="2026-01-29",
        ),
        _row(
            EXCH_ID="NSE",
            INSTRUMENT="EQUITY",
            SECURITY_ID="11536",
            LOT_SIZE="1",
            TICK_SIZE="0.05",
            SYMBOL_NAME="TATA CONSULTANCY SERVICES LTD",
            UNDERLYING_SYMBOL="TCS",
        ),
        _row(
            EXCH_ID="NSE",
            INSTRUMENT="FUTSTK",
            SECURITY_ID="40002",
            SYMBOL_NAME="TCS26JANFUT",
            UNDERLYING_SYMBOL="TCS",
            UNDERLYING_SECURITY_ID="11536",
            SM_EXPIRY_DATE="2026-01-29",
        ),
        # --- MOTHERSON duplicate listing: canonical (4204) + decoy (99999) ---
        _row(
            EXCH_ID="NSE",
            INSTRUMENT="EQUITY",
            SECURITY_ID="99999",
            LOT_SIZE="1",
            TICK_SIZE="0.05",
            SYMBOL_NAME="SAMVRDHNA MTHRSN INTL LTD",
            UNDERLYING_SYMBOL="MOTHERSON",
        ),
        _row(
            EXCH_ID="NSE",
            INSTRUMENT="EQUITY",
            SECURITY_ID="4204",
            LOT_SIZE="1",
            TICK_SIZE="0.05",
            SYMBOL_NAME="SAMVARDHANA MOTHERSON INT",
            UNDERLYING_SYMBOL="MOTHERSON",
        ),
        _row(
            EXCH_ID="NSE",
            INSTRUMENT="OPTSTK",
            SECURITY_ID="40003",
            SYMBOL_NAME="MOTHERSON26JAN100CE",
            UNDERLYING_SYMBOL="MOTHERSON",
            UNDERLYING_SECURITY_ID="4204",
            OPTION_TYPE="CE",
            STRIKE_PRICE="100",
            SM_EXPIRY_DATE="2026-01-29",
        ),
        # --- BSE single-stock equity with an F&O ticker → dropped (NSE-only) ---
        _row(
            EXCH_ID="BSE",
            INSTRUMENT="EQUITY",
            SECURITY_ID="500325",
            SYMBOL_NAME="RELIANCE INDUSTRIES LTD",
            UNDERLYING_SYMBOL="RELIANCE",
        ),
        # --- TEST placeholder scrip → dropped ---
        _row(
            EXCH_ID="NSE",
            INSTRUMENT="OPTSTK",
            SECURITY_ID="40009",
            SYMBOL_NAME="11NSETEST26JAN100CE",
            UNDERLYING_SYMBOL="11NSETEST",
            UNDERLYING_SECURITY_ID="55555",
            OPTION_TYPE="CE",
        ),
        _row(
            EXCH_ID="NSE",
            INSTRUMENT="EQUITY",
            SECURITY_ID="55555",
            SYMBOL_NAME="11NSETEST",
            UNDERLYING_SYMBOL="11NSETEST",
        ),
        # --- out-of-scope exchange (filtered) ---
        _row(EXCH_ID="MCX", INSTRUMENT="FUTCOM", SECURITY_ID="7777", SYMBOL_NAME="GOLD"),
    ]


def _csv_text(rows: list[dict[str, str]]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_HEADER)
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def test_universe_selection_golden() -> None:
    text = _csv_text(_curated_rows())
    reader = csv.DictReader(io.StringIO(text))
    validate_header(reader.fieldnames)
    report = select_universe_report(list(reader))

    # EXACT admitted universe (the golden lock): 3 indices + 3 stocks.
    assert sorted(i.vendor_symbol for i in report.universe) == [
        "BSE-SENSEX",
        "NSE-BANKNIFTY",
        "NSE-MOTHERSON",
        "NSE-NIFTY",
        "NSE-RELIANCE",
        "NSE-TCS",
    ]

    by_sym = {i.vendor_symbol: i for i in report.universe}
    # MOTHERSON collapses to the canonical spot (the derivative's underlying id).
    assert by_sym["NSE-MOTHERSON"].exchange_token == 4204
    # Indices carry IDX type; stocks carry EQ.
    assert by_sym["BSE-SENSEX"].instrument_type.value == "IDX"
    assert by_sym["NSE-RELIANCE"].instrument_type.value == "EQ"
    # Every global key is unique (no PRIMARY-KEY collision on upsert).
    keys = [i.vendor_symbol for i in report.universe]
    assert len(keys) == len(set(keys))
    # MCX row filtered (out of scope); everything else is parsed.
    assert report.filtered == 1
    assert report.persisted == 6
