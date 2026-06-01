"""Tests for `brutex.adapters.dhan.instrument_loader` — download → universe.

100% line+branch of the non-I/O surface (`load_universe`) via an injected
`http_get`. The real `urllib_http_get` is `# pragma: no cover` (network).
"""

from __future__ import annotations

import csv
import io

import pytest
from brutex.adapters.dhan.constants import INSTRUMENT_MASTER_DETAILED_URL
from brutex.adapters.dhan.instrument_loader import load_universe, load_universe_report
from brutex.adapters.dhan.instruments import DhanInstrumentError

_HEADER = [
    "EXCH_ID",
    "SECURITY_ID",
    "INSTRUMENT",
    "INSTRUMENT_TYPE",
    "SYMBOL_NAME",
    "DISPLAY_NAME",
    "SERIES",
    "ISIN",
    "LOT_SIZE",
    "TICK_SIZE",
    "SM_FREEZE_QTY",
    "UNDERLYING_SYMBOL",
    "UNDERLYING_SECURITY_ID",
    "OPTION_TYPE",
    "STRIKE_PRICE",
    "SM_EXPIRY_DATE",
]


def _row(**overrides: str) -> dict[str, str]:
    base = dict.fromkeys(_HEADER, "")
    base.update(overrides)
    return base


def _csv(rows: list[dict[str, str]], header: list[str] = _HEADER) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=header)
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def _universe_rows() -> list[dict[str, str]]:
    return [
        _row(
            EXCH_ID="NSE",
            SECURITY_ID="13",
            INSTRUMENT="INDEX",
            SYMBOL_NAME="NIFTY",
            DISPLAY_NAME="Nifty 50",
        ),
        _row(
            EXCH_ID="BSE",
            SECURITY_ID="51",
            INSTRUMENT="INDEX",
            SYMBOL_NAME="SENSEX",
            DISPLAY_NAME="BSE Sensex",
        ),
        _row(
            EXCH_ID="NSE",
            SECURITY_ID="2885",
            INSTRUMENT="EQUITY",
            SYMBOL_NAME="RELIANCE INDUSTRIES LTD",  # long company name
            UNDERLYING_SYMBOL="RELIANCE",  # ticker → the F&O join key
            DISPLAY_NAME="Reliance",
            SERIES="EQ",
            ISIN="INE002A01018",
            LOT_SIZE="1",
            TICK_SIZE="0.05",
        ),
        _row(
            EXCH_ID="NSE",
            SECURITY_ID="9999",
            INSTRUMENT="EQUITY",
            SYMBOL_NAME="TINY CO LTD",
            DISPLAY_NAME="Tiny Co",
            LOT_SIZE="1",
            TICK_SIZE="0.05",
        ),  # not F&O eligible (no UNDERLYING_SYMBOL)
        _row(
            EXCH_ID="NSE",
            SECURITY_ID="3001",
            INSTRUMENT="OPTSTK",
            SYMBOL_NAME="RELIANCE-OPT",
            DISPLAY_NAME="Reliance CE",
            LOT_SIZE="250",
            TICK_SIZE="0.05",
            OPTION_TYPE="CE",
            UNDERLYING_SYMBOL="RELIANCE",
            UNDERLYING_SECURITY_ID="2885",
            STRIKE_PRICE="2600",
            SM_EXPIRY_DATE="2025-01-30",
        ),  # makes RELIANCE F&O-eligible; leg itself dropped
    ]


class TestLoadUniverse:
    def test_happy_path_selects_indices_and_fno_spot(self) -> None:
        text = _csv(_universe_rows())
        universe = load_universe(lambda _url: text)
        symbols = sorted(i.trading_symbol for i in universe)
        assert symbols == ["NIFTY", "RELIANCE", "SENSEX"]

    def test_report_carries_parse_accounting(self) -> None:
        text = _csv(_universe_rows())
        report = load_universe_report(lambda _url: text)
        assert sorted(i.trading_symbol for i in report.universe) == ["NIFTY", "RELIANCE", "SENSEX"]
        assert report.parsed == 5  # all NSE/BSE rows (incl. TINYCO + option leg)
        assert report.persisted == 3
        assert report.legs_dropped == 2  # TINYCO + RELIANCE-OPT
        assert report.filtered == 0
        # Accounting invariant: every in-scope row is either persisted or dropped.
        assert report.parsed == report.persisted + report.legs_dropped

    def test_passes_default_url_to_http_get(self) -> None:
        seen: list[str] = []

        def _get(url: str) -> str:
            seen.append(url)
            return _csv(_universe_rows())

        load_universe(_get)
        assert seen == [INSTRUMENT_MASTER_DETAILED_URL]

    def test_custom_url(self) -> None:
        seen: list[str] = []

        def _get(url: str) -> str:
            seen.append(url)
            return _csv(
                [
                    _row(
                        EXCH_ID="NSE",
                        SECURITY_ID="13",
                        INSTRUMENT="INDEX",
                        SYMBOL_NAME="NIFTY",
                        DISPLAY_NAME="Nifty 50",
                    )
                ]
            )

        load_universe(_get, url="https://images.dhan.co/x.csv")
        assert seen == ["https://images.dhan.co/x.csv"]

    def test_missing_required_column_raises(self) -> None:
        short_header = [c for c in _HEADER if c != "SM_FREEZE_QTY"]
        text = _csv([dict.fromkeys(short_header, "")], header=short_header)
        with pytest.raises(DhanInstrumentError, match="missing required columns"):
            load_universe(lambda _url: text)

    def test_short_row_cells_default_empty(self) -> None:
        # A physically short row (fewer columns) → restval '' → still parses
        # as an index (lot/tick empty → 0). Exercises the `or ""` empty branch.
        text = _csv([_row()], header=_HEADER)  # all-empty row
        short = text.split("\n")
        # Columns now: EXCH_ID,SECURITY_ID,INSTRUMENT,INSTRUMENT_TYPE,SYMBOL_NAME,…
        short[1] = "NSE,13,INDEX,INDEX,NIFTY"  # truncate the data row's trailing cells
        universe = load_universe(lambda _url: "\n".join(short))
        assert [i.trading_symbol for i in universe] == ["NIFTY"]
