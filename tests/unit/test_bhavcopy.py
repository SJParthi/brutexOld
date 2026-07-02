"""Unit tests for ``brutex.data.bhavcopy`` (DEC-DOCTOR-LIVE-PROOF-KIT-001, G7 / kit stage 8)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from brutex.data import bhavcopy as bc
from brutex.data.models import CandleBar

_HEADER = (
    "Index Name,Index Date,Open Index Value,High Index Value,Low Index Value,"
    "Closing Index Value,Points Change,Change(%)"
)
_CSV = (
    f"{_HEADER}\n"
    "Nifty 50,01-07-2026,24800.10,24910.55,24750.00,24818.85,120.5,0.49\n"
    'Nifty Bank,01-07-2026,"52,100.00","52,400.00","51,900.05","52,300.10",200.1,0.38\n'
    "Nifty Broken,01-07-2026,-,,-,x,0,0\n"
    "Nifty Short,01-07-2026\n"
    ",01-07-2026,1,2,3,4,0,0\n"
)


def _bar(close: str = "24818.85") -> CandleBar:
    return CandleBar(
        timestamp=datetime(2026, 6, 30, 18, 30, tzinfo=UTC),  # midnight IST 2026-07-01
        open=Decimal("24800.10"),
        high=Decimal("24910.55"),
        low=Decimal("24750.00"),
        close=Decimal(close),
        volume=0,
        open_interest=None,
    )


class TestUrlAndSsrf:
    def test_url_stamp_is_ddmmyyyy(self) -> None:
        assert bc.bhavcopy_url(date(2026, 7, 1)) == (
            "https://archives.nseindia.com/content/indices/ind_close_all_01072026.csv"
        )

    @pytest.mark.parametrize(
        "url",
        [
            "https://evil.example.com/ind_close_all_01072026.csv",
            "http://archives.nseindia.com/content/indices/ind_close_all_01072026.csv",
        ],
    )
    def test_off_allowlist_url_refused(self, url: str) -> None:
        with pytest.raises(bc.BhavcopyError, match="refusing non-allowlisted"):
            bc.fetch_index_bhavcopy(lambda _u: _CSV, date(2026, 7, 1), url=url)

    def test_fetch_uses_default_url_and_parses(self) -> None:
        seen: list[str] = []

        def _get(url: str) -> str:
            seen.append(url)
            return _CSV

        rows = bc.fetch_index_bhavcopy(_get, date(2026, 7, 1))
        assert seen == [bc.bhavcopy_url(date(2026, 7, 1))]
        assert "nifty 50" in rows

    def test_network_error_propagates(self) -> None:
        def _get(_url: str) -> str:
            raise OSError("connection reset")

        with pytest.raises(OSError, match="connection reset"):
            bc.fetch_index_bhavcopy(_get, date(2026, 7, 1))


class TestParse:
    def test_parses_rows_with_thousand_separators(self) -> None:
        rows = bc.parse_index_bhavcopy(_CSV)
        nifty = rows["nifty 50"]
        assert nifty.index_name == "Nifty 50"
        assert nifty.close == Decimal("24818.85")
        bank = rows["nifty bank"]
        assert bank.low == Decimal("51900.05")
        # Unparseable / blank-name rows are skipped, never fatal.
        assert "nifty broken" not in rows
        assert "nifty short" not in rows  # short row → missing cells → skipped
        assert len(rows) == 2

    def test_header_drift_fails_loud_with_observed_header(self) -> None:
        with pytest.raises(bc.BhavcopyError, match="unrecognised NSE indices snapshot header"):
            bc.parse_index_bhavcopy("A,B,C\n1,2,3\n")

    def test_missing_name_column_fails_loud(self) -> None:
        text = "Open Index Value,High Index Value,Low Index Value,Closing Index Value\n1,2,3,4\n"
        with pytest.raises(bc.BhavcopyError, match="unrecognised"):
            bc.parse_index_bhavcopy(text)

    def test_empty_text_fails_loud(self) -> None:
        with pytest.raises(bc.BhavcopyError, match="unrecognised"):
            bc.parse_index_bhavcopy("")


class TestMapping:
    def test_known_symbols_map(self) -> None:
        assert bc.nse_index_name_for("NSE-NIFTY") == "nifty 50"
        assert bc.nse_index_name_for("NSE-BANKNIFTY") == "nifty bank"

    def test_bse_sensex_is_honestly_unmapped(self) -> None:
        assert bc.nse_index_name_for("BSE-SENSEX") is None


class TestCompare:
    def test_exact_match(self) -> None:
        official = bc.parse_index_bhavcopy(_CSV)["nifty 50"]
        comparison = bc.compare_daily_bar(_bar(), official)
        assert comparison.matches is True
        assert comparison.max_delta == Decimal("0")

    def test_within_one_tick_matches(self) -> None:
        official = bc.parse_index_bhavcopy(_CSV)["nifty 50"]
        comparison = bc.compare_daily_bar(_bar(close="24818.80"), official)
        assert comparison.matches is True
        assert comparison.max_delta == Decimal("0.05")

    def test_beyond_tolerance_mismatch_names_field(self) -> None:
        official = bc.parse_index_bhavcopy(_CSV)["nifty 50"]
        comparison = bc.compare_daily_bar(_bar(close="24812.40"), official)
        assert comparison.matches is False
        assert comparison.max_delta == Decimal("6.45")
        assert comparison.deltas["close"] == Decimal("6.45")
        assert comparison.deltas["open"] == Decimal("0")

    def test_custom_tolerance(self) -> None:
        official = bc.parse_index_bhavcopy(_CSV)["nifty 50"]
        comparison = bc.compare_daily_bar(_bar(close="24812.40"), official, tolerance=Decimal("10"))
        assert comparison.matches is True
        assert comparison.tolerance == Decimal("10")
