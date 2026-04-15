"""Unit tests for brutex.adapters.groww.normaliser.

Exercises the four concrete row shapes in conftest (equity, spot index,
BANKNIFTY CE — Dec 2025, BANKNIFTY PE — Jun 2025, BSE SENSEX CE) plus
error paths for malformed inputs.

Key specific assertions (from docs/groww-sdk/instruments.md §12):

* internal_trading_symbol == trading_symbol invariant
* tick_size normalised from both float rupees and int paise
* BANKNIFTY lot_size differs by expiry (30 in June, 35 in December)
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from brutex.adapters.groww.normaliser import normalise_instrument
from brutex.models import (
    Exchange,
    InstrumentFormatError,
    InstrumentType,
    Segment,
)

# ────────────────────────────────────────────────────────────────────────
# Happy paths — one test per row shape in conftest
# ────────────────────────────────────────────────────────────────────────


class TestEquityRow:
    def test_reliance_normalises(self, raw_equity_row_reliance: dict[str, Any]) -> None:
        inst = normalise_instrument(raw_equity_row_reliance)
        assert inst.exchange == Exchange.NSE
        assert inst.trading_symbol == "RELIANCE"
        assert inst.groww_symbol == "NSE-RELIANCE"
        assert inst.instrument_type == InstrumentType.EQ
        assert inst.segment == Segment.CASH
        assert inst.isin == "INE002A01018"
        assert inst.lot_size == 1
        # tick_size came in as paise integer 5 — must normalise to 0.05 rupees
        assert inst.tick_size == Decimal("0.05")
        # Derivatives-only fields are None for equity
        assert inst.expiry_date is None
        assert inst.strike_price is None
        assert inst.underlying_symbol is None
        # Permissions
        assert inst.is_reserved is False
        assert inst.buy_allowed is True
        assert inst.sell_allowed is True
        # internal_trading_symbol equals trading_symbol — invariant holds
        assert inst.internal_trading_symbol == "RELIANCE"

    def test_reliance_is_not_derivative(self, raw_equity_row_reliance: dict[str, Any]) -> None:
        inst = normalise_instrument(raw_equity_row_reliance)
        assert inst.is_derivative is False
        assert inst.is_option is False


class TestSpotIndexRow:
    def test_nifty_spot_normalises(self, raw_index_row_nifty: dict[str, Any]) -> None:
        inst = normalise_instrument(raw_index_row_nifty)
        assert inst.instrument_type == InstrumentType.IDX
        assert inst.segment == Segment.CASH
        # tick_size came in as float 0.05 rupees — still 0.05
        assert inst.tick_size == Decimal("0.05")
        assert inst.is_derivative is False
        # Empty strings and NaN are normalised to None
        assert inst.series is None
        assert inst.isin is None
        assert inst.underlying_symbol is None


class TestBankNiftyDec2025Put:
    def test_normalises(self, raw_banknifty_ce_row_dec2025: dict[str, Any]) -> None:
        inst = normalise_instrument(raw_banknifty_ce_row_dec2025)
        assert inst.exchange == Exchange.NSE
        assert inst.instrument_type == InstrumentType.PE
        assert inst.segment == Segment.FNO
        assert inst.underlying_symbol == "BANKNIFTY"
        assert inst.expiry_date == date(2025, 12, 24)
        assert inst.strike_price == Decimal("27000")
        assert inst.lot_size == 35  # ← post-SEBI revision
        assert inst.freeze_quantity == 601
        assert inst.is_option is True
        assert inst.is_reserved is True


class TestBankNiftyJun2025Put:
    def test_lot_size_is_thirty(self, raw_banknifty_ce_row_jun2025: dict[str, Any]) -> None:
        """BANKNIFTY Jun 2025 has lot_size 30 — pre-SEBI revision.

        Proves the adapter does NOT project a 'current' lot size onto
        historical rows. The temporal lot-size tracking is correct at
        row level.
        """
        inst = normalise_instrument(raw_banknifty_ce_row_jun2025)
        assert inst.lot_size == 30
        assert inst.expiry_date == date(2025, 6, 26)


class TestSensexRow:
    def test_bse_sensex_option(self, raw_sensex_ce_row: dict[str, Any]) -> None:
        inst = normalise_instrument(raw_sensex_ce_row)
        assert inst.exchange == Exchange.BSE
        assert inst.instrument_type == InstrumentType.CE
        assert inst.underlying_symbol == "SENSEX"
        assert inst.expiry_date == date(2025, 9, 25)
        assert inst.strike_price == Decimal("79500")
        assert inst.is_option is True


# ────────────────────────────────────────────────────────────────────────
# Error paths
# ────────────────────────────────────────────────────────────────────────


def _with(row: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    out = dict(row)
    out.update(overrides)
    return out


class TestErrorPaths:
    def test_missing_exchange_raises(self, raw_equity_row_reliance: dict[str, Any]) -> None:
        row = _with(raw_equity_row_reliance, exchange=float("nan"))
        with pytest.raises(InstrumentFormatError, match="exchange"):
            normalise_instrument(row)

    def test_unsupported_exchange_raises(self, raw_equity_row_reliance: dict[str, Any]) -> None:
        row = _with(raw_equity_row_reliance, exchange="MCX")
        with pytest.raises(InstrumentFormatError, match="unsupported exchange"):
            normalise_instrument(row)

    def test_unsupported_segment_raises(self, raw_equity_row_reliance: dict[str, Any]) -> None:
        row = _with(raw_equity_row_reliance, segment="COMMODITY")
        with pytest.raises(InstrumentFormatError, match="unsupported segment"):
            normalise_instrument(row)

    def test_unsupported_instrument_type_raises(
        self, raw_equity_row_reliance: dict[str, Any]
    ) -> None:
        row = _with(raw_equity_row_reliance, instrument_type="INDEX")
        with pytest.raises(InstrumentFormatError, match="unsupported instrument_type"):
            normalise_instrument(row)

    def test_internal_trading_symbol_mismatch_raises(
        self, raw_equity_row_reliance: dict[str, Any]
    ) -> None:
        row = _with(raw_equity_row_reliance, internal_trading_symbol="OTHER")
        with pytest.raises(InstrumentFormatError, match="internal_trading_symbol"):
            normalise_instrument(row)

    def test_missing_internal_trading_symbol_is_allowed(
        self, raw_equity_row_reliance: dict[str, Any]
    ) -> None:
        """The 20th column is undocumented — absent is OK, mismatch is not."""
        row = _with(raw_equity_row_reliance, internal_trading_symbol=float("nan"))
        inst = normalise_instrument(row)
        assert inst.internal_trading_symbol is None

    def test_option_without_strike_raises(
        self, raw_banknifty_ce_row_dec2025: dict[str, Any]
    ) -> None:
        row = _with(raw_banknifty_ce_row_dec2025, strike_price=float("nan"))
        with pytest.raises(InstrumentFormatError, match="strike_price"):
            normalise_instrument(row)

    def test_option_without_expiry_raises(
        self, raw_banknifty_ce_row_dec2025: dict[str, Any]
    ) -> None:
        row = _with(raw_banknifty_ce_row_dec2025, expiry_date=float("nan"))
        with pytest.raises(InstrumentFormatError, match="expiry_date"):
            normalise_instrument(row)

    def test_non_integer_exchange_token_raises(
        self, raw_equity_row_reliance: dict[str, Any]
    ) -> None:
        row = _with(raw_equity_row_reliance, exchange_token="not-a-number")
        with pytest.raises(InstrumentFormatError, match="exchange_token"):
            normalise_instrument(row)

    def test_invalid_date_format_raises(self, raw_banknifty_ce_row_dec2025: dict[str, Any]) -> None:
        row = _with(raw_banknifty_ce_row_dec2025, expiry_date="24-Dec-2025")
        with pytest.raises(InstrumentFormatError, match="ISO date"):
            normalise_instrument(row)
