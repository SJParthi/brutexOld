"""Root pytest configuration for BruteX."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from brutex.models import Exchange, Instrument, InstrumentType, Segment

# ────────────────────────────────────────────────────────────────────────
# Raw-row fixtures mimicking the shape of Groww's pandas DataFrame rows.
# These are dicts (same shape returned by `df.to_dict('records')[i]` or
# `get_instrument_by_groww_symbol`). We deliberately include the 20th
# `internal_trading_symbol` column observed in the real SDK output to
# verify the normaliser handles it per docs/groww-sdk/instruments.md §12.1.
# ────────────────────────────────────────────────────────────────────────


@pytest.fixture
def raw_equity_row_reliance() -> dict[str, Any]:
    """Matches the exact docs example for NSE-RELIANCE."""
    return {
        "exchange": "NSE",
        "exchange_token": 2885,
        "trading_symbol": "RELIANCE",
        "groww_symbol": "NSE-RELIANCE",
        "name": "Reliance Industries",
        "instrument_type": "EQ",
        "segment": "CASH",
        "series": "EQ",
        "isin": "INE002A01018",
        "underlying_symbol": float("nan"),
        "underlying_exchange_token": float("nan"),
        "lot_size": 1,
        "expiry_date": float("nan"),
        "strike_price": float("nan"),
        "tick_size": 5,  # integer paise form (dict output)
        "freeze_quantity": float("nan"),
        "is_reserved": 0,
        "buy_allowed": 1,
        "sell_allowed": 1,
        "internal_trading_symbol": "RELIANCE",
    }


@pytest.fixture
def raw_index_row_nifty() -> dict[str, Any]:
    """Spot NIFTY index row."""
    return {
        "exchange": "NSE",
        "exchange_token": 26000,
        "trading_symbol": "NIFTY",
        "groww_symbol": "NSE-NIFTY",
        "name": "Nifty 50",
        "instrument_type": "IDX",
        "segment": "CASH",
        "series": "",
        "isin": "",
        "underlying_symbol": "",
        "underlying_exchange_token": float("nan"),
        "lot_size": 1,
        "expiry_date": "",
        "strike_price": float("nan"),
        "tick_size": 0.05,  # float rupees form (CSV-text form)
        "freeze_quantity": float("nan"),
        "is_reserved": 0,
        "buy_allowed": 1,
        "sell_allowed": 1,
        "internal_trading_symbol": "NIFTY",
    }


@pytest.fixture
def raw_banknifty_ce_row_dec2025() -> dict[str, Any]:
    """Matches the real BANKNIFTY Dec 2025 27000 CE row from the CSV sample.

    lot_size 35 (post-SEBI-revision — see docs/groww-sdk/instruments.md §12.3).
    """
    return {
        "exchange": "NSE",
        "exchange_token": 49445,
        "trading_symbol": "BANKNIFTY25DEC27000PE",
        "groww_symbol": "NSE-BANKNIFTY-24Dec25-27000-PE",
        "name": float("nan"),
        "instrument_type": "PE",
        "segment": "FNO",
        "series": float("nan"),
        "isin": float("nan"),
        "underlying_symbol": "BANKNIFTY",
        "underlying_exchange_token": 26009,
        "lot_size": 35,
        "expiry_date": "2025-12-24",
        "strike_price": 27000,
        "tick_size": 0.05,
        "freeze_quantity": 601,
        "is_reserved": 1,
        "buy_allowed": 1,
        "sell_allowed": 1,
        "internal_trading_symbol": "BANKNIFTY25DEC27000PE",
    }


@pytest.fixture
def raw_banknifty_ce_row_jun2025() -> dict[str, Any]:
    """Matches the real BANKNIFTY Jun 2025 35400 PE row — lot_size 30
    (pre-SEBI-revision). Used to prove temporal lot-size tracking.
    """
    return {
        "exchange": "NSE",
        "exchange_token": 41942,
        "trading_symbol": "BANKNIFTY25JUN35400PE",
        "groww_symbol": "NSE-BANKNIFTY-26Jun25-35400-PE",
        "name": float("nan"),
        "instrument_type": "PE",
        "segment": "FNO",
        "series": float("nan"),
        "isin": float("nan"),
        "underlying_symbol": "BANKNIFTY",
        "underlying_exchange_token": 26009,
        "lot_size": 30,
        "expiry_date": "2025-06-26",
        "strike_price": 35400,
        "tick_size": 0.05,
        "freeze_quantity": 601,
        "is_reserved": 1,
        "buy_allowed": 1,
        "sell_allowed": 1,
        "internal_trading_symbol": "BANKNIFTY25JUN35400PE",
    }


@pytest.fixture
def raw_sensex_ce_row() -> dict[str, Any]:
    """BSE SENSEX weekly Tuesday option — required for BSE scope coverage."""
    return {
        "exchange": "BSE",
        "exchange_token": 86000,
        "trading_symbol": "SENSEX25SEP79500CE",
        "groww_symbol": "BSE-SENSEX-25Sep25-79500-CE",
        "name": float("nan"),
        "instrument_type": "CE",
        "segment": "FNO",
        "series": float("nan"),
        "isin": float("nan"),
        "underlying_symbol": "SENSEX",
        "underlying_exchange_token": 1,
        "lot_size": 20,
        "expiry_date": "2025-09-25",
        "strike_price": 79500,
        "tick_size": 0.05,
        "freeze_quantity": 1000,
        "is_reserved": 0,
        "buy_allowed": 1,
        "sell_allowed": 1,
        "internal_trading_symbol": "SENSEX25SEP79500CE",
    }


@pytest.fixture
def nifty_option_instance() -> Instrument:
    """A pre-built Instrument useful for universe/scope tests."""
    return Instrument(
        exchange=Exchange.NSE,
        exchange_token=35044,
        trading_symbol="NIFTY25MAR29050PE",
        groww_symbol="NSE-NIFTY-27Mar25-29050-PE",
        name=None,
        instrument_type=InstrumentType.PE,
        segment=Segment.FNO,
        series=None,
        isin=None,
        underlying_symbol="NIFTY",
        underlying_exchange_token=26000,
        lot_size=25,
        tick_size=Decimal("0.05"),
        freeze_quantity=7200,
        expiry_date=date(2025, 3, 27),
        strike_price=Decimal("29050"),
        is_reserved=True,
        buy_allowed=True,
        sell_allowed=True,
        internal_trading_symbol="NIFTY25MAR29050PE",
    )
