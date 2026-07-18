"""Unit tests for :mod:`brutex.adapters.dhan.expired_options` — 100% line+branch."""

from __future__ import annotations

from decimal import Decimal

import pytest
from brutex.adapters.dhan.client import DhanClient
from brutex.adapters.dhan.expired_options import (
    DEFAULT_REQUIRED_DATA,
    ROLLING_OPTION_INSTRUMENTS,
    fetch_rolling_option,
)

from tests.fakes.fake_dhan_transport import FakeDhanTransport, Response


def _client(transport: FakeDhanTransport) -> DhanClient:
    return DhanClient(transport=transport, access_token="tok", client_id="1")


def test_constants() -> None:
    assert DEFAULT_REQUIRED_DATA == ("open", "high", "low", "close", "volume")
    assert {"OPTIDX", "OPTSTK"} == ROLLING_OPTION_INSTRUMENTS


def test_fetch_rolling_option_happy() -> None:
    transport = FakeDhanTransport(
        script=[Response(200, FakeDhanTransport.rolling_option_payload(n=2))]
    )
    bars = fetch_rolling_option(
        _client(transport),
        exchange_segment="NSE_FNO",
        security_id="13",
        instrument="OPTIDX",
        expiry_flag="MONTH",
        expiry_code=0,
        strike="ATM",
        drv_option_type="CALL",
        interval="1",
        from_date="2024-01-01 09:15:00",
        to_date="2024-01-30 15:30:00",
    )
    assert len(bars) == 4  # 2 CE + 2 PE
    assert {b.side for b in bars} == {"CE", "PE"}
    assert bars[0].strike == Decimal("20000.0")
    body = transport.seen_bodies[0]
    assert body["strike"] == "ATM"
    assert body["drvOptionType"] == "CALL"
    assert body["requiredData"] == list(DEFAULT_REQUIRED_DATA)


def test_fetch_rolling_option_rejects_non_option_instrument() -> None:
    transport = FakeDhanTransport()
    with pytest.raises(ValueError, match="expired futures history is UNVERIFIED-absent"):
        fetch_rolling_option(
            _client(transport),
            exchange_segment="NSE_FNO",
            security_id="13",
            instrument="FUTIDX",  # expired FUTURES have no path — fail loud
            expiry_flag="MONTH",
            expiry_code=0,
            strike="ATM",
            drv_option_type="CALL",
            interval="1",
            from_date="2024-01-01 09:15:00",
            to_date="2024-01-30 15:30:00",
        )
    assert transport.calls == 0  # refused BEFORE any network call
