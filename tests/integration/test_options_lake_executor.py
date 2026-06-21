"""Integration: the F&O-lake option source priced through the REAL executor.

DEC-OPTIONS-LAKE-READER-001. Proves :class:`~brutex.options.lake_source.LakeOptionBarSource`
structurally satisfies the executor's ``OptionBarSource`` seam AND prices a real
round-trip: a BULL spot signal → ATM rule → the lake's CE contract → a
:class:`~brutex.options.executor.MaterializedTrade` with the entry/exit OPEN
premiums; an absent strike → an honest ``RejectedTrade(NO_DATA)``. Real tmp lake +
the production cost calculator; no network.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

pytest.importorskip("polars")
pytest.importorskip("pyarrow")

from brutex.options import (
    LakeOptionBarSource,
    MaterializedTrade,
    RejectedTrade,
    RejectReason,
    SpotSignal,
    TradeDirection,
    materialize_trade,
)
from brutex.options.lot_size_history import OptionsUnderlying
from brutex.options.strike_rules import RULES_BY_NAME

from brutex.data.models import CandleBar  # isort: skip
from brutex.data.parquet_writer import ParquetWriter  # isort: skip
from brutex.data.sink import ParquetBarSink  # isort: skip

_IST = ZoneInfo("Asia/Kolkata")
_ENTRY = datetime(2025, 1, 2, 9, 15, tzinfo=_IST)
_EXIT = datetime(2025, 1, 2, 15, 20, tzinfo=_IST)


def _bar(ts_ist: datetime, premium: str) -> CandleBar:
    c = Decimal(premium)
    return CandleBar(
        timestamp=ts_ist.astimezone(UTC),
        open=c,
        high=c,
        low=c,
        close=c,
        volume=50,
        open_interest=1000,
    )


@pytest.fixture
def lake(tmp_path: Path) -> Path:
    root = tmp_path / "lake"
    ParquetBarSink(writer=ParquetWriter(lake_root=root)).append(
        exchange="NSE",
        segment="FNO",
        symbol="NSE-NIFTY-02Jan25-24000-CE",  # ATM CE for a 24000 spot
        interval="1minute",
        bars=[_bar(_ENTRY, "150.00"), _bar(_EXIT, "210.00")],
    )
    return root


def _bull_signal() -> SpotSignal:
    return SpotSignal(
        instrument=OptionsUnderlying.NIFTY,
        direction=TradeDirection.BULL,  # → buy CE
        entry_ts=_ENTRY,
        exit_ts=_EXIT,
        spot_at_entry=Decimal("24000"),  # ATM = 24000 (grid step 50)
        expiry_date=date(2025, 1, 2),
    )


def test_lake_source_prices_a_real_trade_through_the_executor(lake: Path) -> None:
    out = materialize_trade(_bull_signal(), RULES_BY_NAME["atm"], source=LakeOptionBarSource(lake))
    assert isinstance(out, MaterializedTrade)
    assert out.leg.option_type == "CE" and out.leg.strike == 24000
    # The entry/exit OPEN premiums (₹150 / ₹210) reach the trade as int64 paisa.
    assert out.entry_open_paisa == 15000
    assert out.exit_open_paisa == 21000
    # The F&O candle lake stores no IV → honest None (never fabricated).
    assert out.iv_at_entry is None


def test_absent_strike_is_rejected_no_data(lake: Path) -> None:
    # A BULL signal at a spot whose ATM (25000) has no contract in the lake → NO_DATA.
    signal = SpotSignal(
        instrument=OptionsUnderlying.NIFTY,
        direction=TradeDirection.BULL,
        entry_ts=_ENTRY,
        exit_ts=_EXIT,
        spot_at_entry=Decimal("25000"),  # ATM 25000 — not ingested
        expiry_date=date(2025, 1, 2),
    )
    out = materialize_trade(signal, RULES_BY_NAME["atm"], source=LakeOptionBarSource(lake))
    assert isinstance(out, RejectedTrade)
    assert out.reason == RejectReason.NO_DATA
