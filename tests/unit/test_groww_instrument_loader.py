"""Unit tests for ``brutex.adapters.groww.instrument_loader`` (DEC-DHAN-GROWW-REMIGRATE-001)."""

from __future__ import annotations

import pytest
from brutex.adapters.groww import instrument_loader as gl
from brutex.adapters.groww.constants import GROWW_INSTRUMENT_CSV_URL, INSTRUMENT_CSV_COLUMNS

_HEADER = ",".join(INSTRUMENT_CSV_COLUMNS)


def _nifty_row() -> str:
    cells = dict.fromkeys(INSTRUMENT_CSV_COLUMNS, "")
    cells.update(
        exchange="NSE",
        exchange_token="NIFTY",
        trading_symbol="NIFTY",
        groww_symbol="NSE-NIFTY",
        name="NIFTY 50",
        instrument_type="IDX",
        segment="CASH",
    )
    return ",".join(cells[c] for c in INSTRUMENT_CSV_COLUMNS)


def test_load_index_builds_index_from_fetched_csv() -> None:
    text = "\n".join([_HEADER, _nifty_row()]) + "\n"
    captured: dict[str, str] = {}

    def fake_get(url: str) -> str:
        captured["url"] = url
        return text

    index = gl.load_index(fake_get)
    assert captured["url"] == GROWW_INSTRUMENT_CSV_URL  # default live URL
    assert index.by_groww_symbol["NSE-NIFTY"].symbol_name == "NIFTY"


def test_load_index_rejects_non_https_url() -> None:
    with pytest.raises(gl.GrowwInstrumentFetchError, match="must be https"):
        gl.load_index(lambda _u: "", url="http://growwapi-assets.groww.in/x.csv")


def test_load_index_rejects_non_allowlisted_host() -> None:
    with pytest.raises(gl.GrowwInstrumentFetchError, match="not allowlisted"):
        gl.load_index(lambda _u: "", url="https://evil.example.com/instrument.csv")


def test_check_url_accepts_allowlisted_https() -> None:
    # the default URL passes the guard (no raise)
    gl._check_url(GROWW_INSTRUMENT_CSV_URL)
