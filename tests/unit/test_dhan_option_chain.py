"""Unit tests for :mod:`brutex.adapters.dhan.option_chain` — 100% line+branch."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from brutex.adapters.dhan.client import DhanClient
from brutex.adapters.dhan.option_chain import fetch_expiry_list, fetch_option_chain

from tests.fakes.fake_dhan_transport import FakeDhanTransport, Response


def _client(transport: FakeDhanTransport) -> DhanClient:
    return DhanClient(transport=transport, access_token="tok", client_id="1")


def test_fetch_expiry_list() -> None:
    transport = FakeDhanTransport(script=[Response(200, FakeDhanTransport.expiry_list_payload())])
    got = fetch_expiry_list(_client(transport), underlying_scrip=13, underlying_seg="IDX_I")
    assert got == [date(2026, 8, 27), date(2026, 9, 24)]
    assert transport.seen_bodies[0] == {"UnderlyingScrip": 13, "UnderlyingSeg": "IDX_I"}


def test_fetch_option_chain() -> None:
    transport = FakeDhanTransport(script=[Response(200, FakeDhanTransport.option_chain_payload())])
    snap = fetch_option_chain(
        _client(transport),
        underlying_scrip=13,
        underlying_seg="IDX_I",
        expiry=date(2026, 8, 27),
    )
    assert snap.last_price == Decimal("20050.0")
    assert [s.strike for s in snap.strikes] == [Decimal("20000.0"), Decimal("20100.0")]
    assert transport.seen_bodies[0]["Expiry"] == "2026-08-27"
